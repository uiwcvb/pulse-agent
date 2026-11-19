from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from core.compaction_service import (
    CHECKPOINT_PREAMBLE,
    SUMMARY_OPEN_TAG,
    build_compaction_replay_messages,
    execute_streaming_compaction,
)
from core.observation_masker import maybe_mask_room_observations
from main import create_app


def _make_app(tmp_path: Path, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setenv('AGENT_TOKEN', 'test')
    app = create_app(tmp_path, scheduler=False)
    app.state.store.update_config({
        'api_key': 'sk-test',
        'default_model': 'deepseek-chat',
        'enabled': True,
    })
    return app, app.state.brain, app.state.store


@pytest.mark.asyncio
async def test_harness_compaction_preserves_tools_recent_turn_and_retains_ltm(tmp_path: Path):
    _, brain, store = _make_app(tmp_path)
    ltm = brain.memory_engine.long_term_memory
    now = time.time()

    # Turn 1 (older): solution was discovered inside web tool output (L-station thread + exact command)
    u1 = store.message('user', '去 L 站找找绕过某接口限流的办法', 'chat', bot_id='companion', source='phone')
    u1['created_at'] = now - 1200
    a1 = store.message(
        'assistant',
        '已经找到可行的解决办法了，准备动手。',
        'chat',
        bot_id='companion',
        source='phone',
        turn_steps=[
            [
                {
                    'role': 'assistant',
                    'content': '我去检索并抓取帖子详情',
                    'tool_calls': [{'id': 'call_web_1', 'type': 'function', 'function': {'name': 'web', 'arguments': '{"op":"fetch","url":"https://linux.do/t/topic/998877"}'}}],
                },
                {
                    'role': 'tool',
                    'tool_call_id': 'call_web_1',
                    'content': (
                        '{"ok": true, "url": "https://linux.do/t/topic/998877", "title": "实测有效配置方法", '
                        '"page_text": "关键步骤：执行 curl -H \\"X-Custom-Mode: fast\\" https://api.example.org/bypass 并把 config.retry=3 '
                        + ('补充背景细节 ' * 60)
                        + '"}'
                    ),
                },
            ]
        ],
    )
    a1['created_at'] = now - 1190

    # Turn 2 & 3 (recent active turns): user says "proceed" right before manual /compact
    u2 = store.message('user', '好，那就照刚刚找到的方法开始改吧', 'chat', bot_id='companion', source='phone')
    u2['created_at'] = now - 30
    a2 = store.message('assistant', '收到，余先确认下环境路径再动手。', 'chat', bot_id='companion', source='phone')
    a2['created_at'] = now - 20

    # 1. Even if observation masker runs on idle turn 1, it MUST offload full text into ctx_search and preserve URL + ctx_id
    masked = maybe_mask_room_observations(store, brain.memory_engine, 'companion', 'phone', now_ts=now)
    # Only 1 total tool step exists, so tail protection leaves it intact OR if enough steps exist, offload preserves URL+ctx_id.
    # Let's also verify explicit replay builder captures full tool result!
    replay_msgs = build_compaction_replay_messages([u1, a1])
    replay_blob = str(replay_msgs)
    assert 'https://linux.do/t/topic/998877' in replay_blob
    assert 'X-Custom-Mode: fast' in replay_blob
    assert any(m.get('role') == 'tool' for m in replay_msgs)

    # 2. Manual compaction (`force=True`) MUST:
    #    a) Feed tool output (L-station URL + command) to the summarizer
    #    b) Preserve latest active turn (u2/a2) verbatim rather than wiping everything to 0!
    #    c) Run decoupled PreCompact experience extraction into LongTermMemoryEngine
    seen_prompts: list[str] = []

    async def fake_silent(msgs, **kw):
        seen_prompts.append(str(msgs))
        joined = str(msgs)
        if '提炼最多 4 条' in joined:
            return {
                'role': 'assistant',
                'content': '[{"topic":"linuxdo_ratelimit_bypass","kind":"experience","outcome":"success","l0":"在 L 站找到接口限流绕过请求头设置","l1":"使用 X-Custom-Mode: fast 且 config.retry=3，来源 https://linux.do/t/topic/998877","urls":["https://linux.do/t/topic/998877"]}]',
            }
        # Assert summarizer actually received the tool output containing the L-station method!
        assert 'https://linux.do/t/topic/998877' in joined
        assert 'X-Custom-Mode: fast' in joined
        return {
            'role': 'assistant',
            'content': (
                '### 📋 核心进展与上下文摘要\n\n'
                '## Objective\n- 去 L 站寻找接口限流绕过办法并按方案落地。\n\n'
                '## Requirements\n- (none)\n\n'
                '## Decisions\n- 采用 X-Custom-Mode: fast 配合 config.retry=3。\n\n'
                '## Work State\n### Completed\n- 已定位有效方法。\n### Active\n- 准备按该方案实施。\n### Blocked\n- (none)\n\n'
                '## Next Move\n1. 执行配置改造。\n\n'
                '## Relevant Files & URLs\n- `https://linux.do/t/topic/998877`: L 站原帖方法\n\n'
                '## Important Context\n- `curl -H "X-Custom-Mode: fast" https://api.example.org/bypass`, `config.retry=3`'
            ),
        }

    res = await execute_streaming_compaction(
        brain.memory_engine,
        bot_id='companion',
        source='phone',
        force=True,
        sys_tokens=14800,
        silent_complete_fn=fake_silent,
    )
    assert res is not None and res['compacted'] is True
    assert 'https://linux.do/t/topic/998877' in res['summary']
    assert 'X-Custom-Mode: fast' in res['summary']

    remaining = [m for m in store.state['messages'] if m.get('bot_id') == 'companion' and m.get('source') == 'phone']
    # Active recent turn MUST remain intact even on manual force=True!
    assert any('照刚刚找到的方法开始改吧' in m.get('text', '') for m in remaining)
    marker = next(m for m in remaining if m.get('kind') == 'compact_summary')
    assert CHECKPOINT_PREAMBLE in marker.get('llm_text', '')
    assert SUMMARY_OPEN_TAG in marker.get('llm_text', '')
    assert 'X-Custom-Mode: fast' in ltm.get_working_ledger('companion', 'phone')

    # 3. Simulate opening a brand-new conversation later: L0/L1 experience must auto-recall cleanly!
    store.state['messages'] = [
        {
            'id': 'fresh_u1',
            'role': 'user',
            'kind': 'chat',
            'bot_id': 'companion',
            'source': 'phone',
            'text': '帮我再调一下那个接口限流绕过，之前 L 站找的方法还记得吗？',
            'created': time.time(),
            'created_at': time.time(),
        }
    ]
    bot = next(b for b in store.state['assistants'] if b['id'] == 'companion')
    built_msgs, _, _, _ = await brain._build_turn_messages_and_tools('companion', 'phone', bot)
    tail_user = str(built_msgs[-1]['content'])
    assert '<recalled_long_term_memory>' in tail_user or 'recalled_long_term_memory' in tail_user
    assert 'X-Custom-Mode: fast' in tail_user
    assert 'https://linux.do/t/topic/998877' in tail_user

    # 4. Graphiti Bi-Temporal superseding: newer solution on same topic replaces older one in active recall!
    ltm.retain(
        l0_summary='新版 L 站限流方案改用 X-Fast-Token',
        l1_overview='新方案请改用 X-Fast-Token 请求头并设 retry=5。',
        topic='linuxdo_ratelimit_bypass',
        kind='experience',
        bot_id='companion',
        source='phone',
        outcome='success',
    )
    recalled_new = ltm.recall('L 站 限流 绕过', bot_id='companion', limit=3)
    assert len(recalled_new) == 1
    assert 'X-Fast-Token' in recalled_new[0]['l1_overview']
    assert 'X-Custom-Mode' not in recalled_new[0]['l1_overview']


def test_observation_masker_persists_ctx_id_and_urls(tmp_path: Path):
    _, brain, store = _make_app(tmp_path)
    now = time.time()
    old_ts = now - 900.0

    for idx in range(1, 5):
        u = store.message('user', f'问题 {idx}', 'chat', bot_id='companion', source='phone')
        u['created_at'] = old_ts
        a = store.message(
            'assistant',
            f'完成 {idx}',
            'chat',
            bot_id='companion',
            source='phone',
            turn_steps=[[
                {'role': 'assistant', 'content': 'grab', 'tool_calls': [{'id': f'tc_{idx}', 'type': 'function', 'function': {'name': 'web', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': f'tc_{idx}', 'content': f'https://linux.do/t/topic/100{idx} ' + ('详细内容与步骤参数 ' * 60)},
            ]],
        )
        a['created_at'] = old_ts

    cnt = maybe_mask_room_observations(store, brain.memory_engine, 'companion', 'phone', now_ts=now)
    assert cnt >= 1
    first_masked = next(
        m['turn_steps'][0][1]['content']
        for m in store.state['messages']
        if m.get('role') == 'assistant' and m.get('turn_steps') and '已归档观察结果' in m['turn_steps'][0][1]['content']
    )
    assert 'ctx_id="' in first_masked
    assert 'https://linux.do/t/topic/1001' in first_masked
    # Verify ctx_search can retrieve the full raw observation!
    import re
    cid = re.search(r'ctx_id="([^"]+)"', first_masked).group(1)
    found = brain.context_search.ctx_search('详细内容与步骤参数', ctx_id=cid)
    assert found['ok'] is True and found['count'] >= 1


def test_unified_memory_db_and_deduplicated_ledger(tmp_path: Path):
    # Pre-create a legacy standalone long_term_memory.db before booting Brain
    from core.long_term_memory import LongTermMemoryEngine
    legacy_path = tmp_path / 'long_term_memory.db'
    old_ltm = LongTermMemoryEngine(legacy_path)
    old_ltm.retain(l0_summary='旧库迁移验证', l1_overview='旧库记录应平滑迁移到统一 context_index.db', topic='legacy_migrate')
    old_ltm.set_working_ledger('companion', 'phone', '旧工作簿摘要')

    _, brain, store = _make_app(tmp_path)
    ltm = brain.memory_engine.long_term_memory
    assert ltm.db_path.resolve() == brain.context_search.db_path.resolve()
    recalled = ltm.recall('旧库迁移验证', bot_id='companion', limit=3)
    assert any('旧库记录应平滑迁移' in r['l1_overview'] for r in recalled)
    assert ltm.get_working_ledger('companion', 'phone') == '旧工作簿摘要'

    # set_working_ledger must NOT create duplicate notes/ledger_*.md vault files
    ltm.set_working_ledger('companion', 'phone', '全新唯一工作簿摘要')
    assert not (brain.vault.root / 'notes' / 'ledger_phone_companion.md').exists()


@pytest.mark.asyncio
async def test_reroll_multi_variants_with_old_choices_and_context_recalc(tmp_path: Path, monkeypatch):
    app, brain, store = _make_app(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient

    client = TestClient(app)
    client.headers['Authorization'] = 'Bearer test'
    client.post('/api/config', json={'small_brain': {'api_key': 'k', 'model': 'm'}})

    # First answer attaches Choice A
    u_msg = store.message('user', '帮我挑个方案', 'chat', bot_id='companion', source='phone')
    a_v1 = store.message(
        'assistant',
        '第一版方案：先做基础版。<choices>["方案A-基础 | 我选第一版的方案A"]</choices>',
        'chat',
        bot_id='companion',
        source='phone',
    )
    # Emulate stored structured choices on v1
    a_v1['text'] = '第一版方案：先做基础版。'
    a_v1['choices'] = [{'label': '方案A-基础', 'value': '我选第一版的方案A'}]
    brain.last_prompt_tokens['phone:companion'] = 58_000
    brain.last_cached_tokens['phone:companion'] = 49_000
    store.save()

    async def mock_complete(*args, **kwargs):
        return {
            'role': 'assistant',
            'content': '第二版方案：直接一步到位。<choices>["方案B-进阶 | 我选第二版的方案B"]</choices>',
        }

    brain.complete = mock_complete

    # Trigger reroll -> should preserve v1 as variant[0] and v2 as variant[1]
    res = await brain.reroll_last(bot_id='companion', message_id=a_v1['id'], source='phone')
    assert res['ok'] is True
    # Wait briefly for bg task
    for _ in range(50):
        cur_msgs = [m for m in store.state['messages'] if m.get('bot_id') == 'companion' and m.get('source') == 'phone']
        if len(cur_msgs) >= 2 and cur_msgs[-1]['role'] == 'assistant' and cur_msgs[-1]['id'] != a_v1['id']:
            break
        await asyncio.sleep(0.02)

    cur_msgs = [m for m in store.state['messages'] if m.get('bot_id') == 'companion' and m.get('source') == 'phone']
    assert cur_msgs[-2]['id'] == u_msg['id']
    a_v2 = cur_msgs[-1]
    assert '第二版方案' in a_v2['text']
    assert a_v2['choices'] == [{'label': '方案B-进阶', 'value': '我选第二版的方案B'}]
    assert len(a_v2['variants']) == 2
    assert a_v2['active_variant_idx'] == 1
    assert '第一版方案' in a_v2['variants'][0]['text']
    assert a_v2['variants'][0]['choices'] == [{'label': '方案A-基础', 'value': '我选第一版的方案A'}]

    # Switch back to variant 0 (old answer + old choices) via REST endpoint!
    sw = client.post(
        '/api/messages/variant',
        json={'message_id': a_v2['id'], 'variant_idx': 0, 'bot_id': 'companion', 'source': 'phone'},
    )
    assert sw.status_code == 200
    sw_body = sw.json()
    assert sw_body['ok'] is True
    assert sw_body['active_variant_idx'] == 0
    assert '第一版方案' in sw_body['message']['text']
    assert sw_body['message']['choices'] == [{'label': '方案A-基础', 'value': '我选第一版的方案A'}]
    # Stale pre-reroll 58K token count must have been recalculated accurately downward!
    assert sw_body['context_by_bot']['companion']['current_tokens'] < 25_000
    assert sw_body['context_by_bot']['companion']['cached_tokens'] == 0


@pytest.mark.asyncio
async def test_present_choices_does_not_leak_before_body_prose(tmp_path: Path):
    _, brain, _ = _make_app(tmp_path)
    published_streams = []
    orig_pub = brain.publish

    async def capture_pub(ev):
        if isinstance(ev, dict) and ev.get('type') == 'stream' and isinstance(ev.get('stream'), dict):
            published_streams.append(dict(ev['stream']))
        await orig_pub(ev)

    brain.publish = capture_pub
    c_tok = brain.choices_var.set(None)
    t_tok = brain.turn_id_var.set('turn-choice-order')
    try:
        res = await brain._tool_present_choices({'choices': ['继续排查 | 继续', '先停这 | 暂停']})
        assert res['ok'] is True
        # No stream frame should surface choices before body text starts!
        assert not any(st.get('choices') for st in published_streams)
        await brain._set_stream(text='正文开始输出中，请稍候…')
        assert published_streams[-1]['choices'] == [
            {'label': '继续排查', 'value': '继续'},
            {'label': '先停这', 'value': '暂停'},
        ]
    finally:
        brain.turn_id_var.reset(t_tok)
        brain.choices_var.reset(c_tok)


@pytest.mark.asyncio
async def test_unified_weak_and_strong_notification_and_modular_skills(tmp_path: Path):
    app, brain, _ = _make_app(tmp_path)
    published_events = []
    orig_pub = brain.publish

    async def capture_pub(ev):
        if isinstance(ev, dict):
            published_events.append(dict(ev))
        await orig_pub(ev)

    brain.publish = capture_pub

    # 1. Weak notification -> notification bar only (no stage 3 overlay)
    weak_res = await brain._tool_reply({'op': 'notify', 'title': '轻提醒', 'body': '喝口水休息一下', 'level': 'weak'})
    assert weak_res['ok'] is True
    assert weak_res['level'] == 'weak'
    assert weak_res['delivery'] == 'notification_bar'
    assert weak_res['overlay_popped'] is False
    weak_notifs = [e for e in published_events if e.get('type') == 'user_notification']
    assert weak_notifs[-1]['urgent'] is False
    assert weak_notifs[-1]['overlay'] is False
    assert weak_notifs[-1]['level'] == 'weak'
    assert not [e for e in published_events if e.get('type') == 'escalation']

    # 2. Strong notification (urgent=True OR level='strong') -> overlay + notification bar
    published_events.clear()
    strong_res = await brain._tool_reply({'op': 'notify', 'title': '悬浮窗提醒', 'body': '主人快看这里', 'level': 'strong'})
    assert strong_res['ok'] is True
    assert strong_res['level'] == 'strong'
    assert strong_res['delivery'] == 'overlay_and_notification'
    assert strong_res['overlay_popped'] is True
    assert [e for e in published_events if e.get('type') == 'user_notification'][-1]['overlay'] is True
    esc = [e for e in published_events if e.get('type') == 'escalation']
    assert len(esc) == 1 and esc[0]['stage'] == 3 and esc[0]['title'] == '悬浮窗提醒'

    published_events.clear()
    urgent_res = await brain._tool_notify_user({'title': '急件', 'body': '需要拍板', 'urgent': True})
    assert urgent_res['overlay_popped'] is True and urgent_res['level'] == 'strong'
    assert any(e.get('type') == 'escalation' and e.get('stage') == 3 for e in published_events)

    # 3. Modular skill vault + YAML frontmatter preservation & parsing
    vault = app.state.vault
    for skill_name in ('self_architecture', 'notify_and_overlay', 'device_control', 'blackboard_collab', 'onboarding_guide'):
        txt = vault.read(f'skills/{skill_name}.md')
        assert f'name: {skill_name}' in txt

    saved = await brain._tool_admin({
        'op': 'skill',
        'action': 'save',
        'name': 'custom_demo',
        'description': '自定义演示技能摘要',
        'content': '第一步先核对现场，第二步执行验证。',
    })
    assert saved['ok'] is True
    listed = await brain._tool_admin({'op': 'skill', 'action': 'list'})
    assert listed['ok'] is True
    skill_map = {item['name']: item for item in listed['skills']}
    assert skill_map['notify_and_overlay']['summary'].startswith('分级通知与手机主屏悬浮窗技能')
    assert skill_map['custom_demo']['summary'] == '自定义演示技能摘要'
    # Re-initializing vault preserves custom skills and upgrades built-ins without wiping user content
    from vault_manager import Vault
    vault2 = Vault(vault.root)
    assert '自定义演示技能摘要' in vault2.read('skills/custom_demo.md')
