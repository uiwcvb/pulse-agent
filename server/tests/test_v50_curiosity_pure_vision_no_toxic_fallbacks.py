from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assistants import save_assistant, AssistantInput
from brain import Brain
from core.cloud_stuck_detector import CloudStuckDetector
from core.curiosity_engine import CuriosityEngine
from core.proactive_gate import ProactiveGate
from core.vfs_router import VirtualPathRouter
from harness import Harness
from main import create_app
from storage import Store
from vault_manager import TZ, Vault


def _make_brain(tmp_path: Path, monkeypatch=None):
    store = Store(tmp_path / 'store')
    vault = Vault(tmp_path / 'vault')
    harness = Harness(tmp_path / 'workspace', store=store, vault=vault)
    published: list[dict] = []

    async def pub(ev: dict):
        published.append(ev)

    class _NoopEsc:
        async def track(self, m, demo=False):
            pass

        async def ack(self, ids):
            pass

    brain = Brain(store, vault, harness, pub, _NoopEsc())
    return store, vault, harness, brain, published


def test_sqlite_curiosity_tables_wal_bots_roundtrip_and_legacy_timers(tmp_path):
    store_dir = tmp_path / 'store'
    store_dir.mkdir(parents=True, exist_ok=True)
    future_due = time.time() + 1800
    legacy_state = {
        'timers': [
            {
                'id': 'legacy1',
                'goal': '记得喝水并复查邮件',
                'bot_id': 'companion',
                'due_at': future_due,
                'source': 'phone',
                'done': False,
            }
        ],
        'assistants': [],
        'messages': [],
        'routines': [],
        'deadlines': [],
        'opportunities': [],
        'pending': {},
    }
    (store_dir / 'state.json').write_text(json.dumps(legacy_state, ensure_ascii=False), encoding='utf-8')
    store = Store(store_dir)
    sql = store.sql

    # 1. Legacy timer migration -> blackboard_posts with due_at/text/author_bot_id/target_bot_id/exec_action/wake_mode
    assert store.state['timers'] == []
    migrated = [p for p in store.state.get('blackboard_posts', []) if p.get('migrated_from_timer')]
    assert len(migrated) == 1
    mp = migrated[0]
    assert mp['due_at'] == pytest.approx(future_due, abs=1.0)
    assert mp['text'] == '记得喝水并复查邮件'
    assert mp['author_bot_id'] == 'companion'
    assert mp['target_bot_id'] == 'companion'
    assert mp['exec_action'] == 'wake_bot'
    assert mp['wake_mode'] == 'proactive'

    # 2. Relational SQLite 3 curiosity tables CRUD + checkpoint_wal
    tid = sql.upsert_curiosity_topic(
        category='工作',
        label='近期项目进展',
        depth=1,
        known_summary='在做个人助手重构',
        open_question='最耗时的环节是什么？',
        priority=0.8,
        increment_ask=True,
    )
    topics = sql.list_curiosity_topics(status='open')
    assert any(t['topic_id'] == tid and t['depth'] == 1 and t['ask_count'] == 1 for t in topics)
    cands = sql.pick_candidate_curiosity_topics(now_ts=time.time() + 10, limit=3)
    assert any(c['topic_id'] == tid for c in cands)

    seeded = sql.seed_topics_from_impression_markdown(
        '## 2. 生活作息\n- **常喝饮品**：(待了解)\n- **运动习惯**：（待了解）\n'
    )
    assert seeded == 2

    fid = sql.add_proactive_followup(
        bot_id='companion',
        source='phone',
        topic='确认体检报告',
        context='昨天约了今天下午出结果',
        due_at=time.time() - 5,
    )
    due_list = sql.list_due_followups(now_ts=time.time())
    assert any(f['followup_id'] == fid for f in due_list)
    assert sql.resolve_followup(fid, status='resolved', increment_attempts=True) is True
    assert not any(f['followup_id'] == fid for f in sql.list_due_followups(now_ts=time.time()))

    eid = sql.record_curiosity_episode(
        intent='genuine_curiosity',
        topic_id=tid,
        bot_id='companion',
        message_text='近期项目里哪块最顺手呀？',
        tool_calls_count=0,
        outcome='sent',
    )
    eps = sql.list_recent_curiosity_episodes(limit=5)
    assert eps and eps[0]['episode_id'] == eid and eps[0]['user_replied'] is False
    assert sql.mark_latest_episode_user_replied(bot_id='companion') == eid
    assert sql.list_recent_curiosity_episodes(limit=5)[0]['user_replied'] is True

    st = sql.stats()
    assert st['curiosity_topic_count'] >= 3
    assert st['curiosity_episode_count'] >= 1
    cp = sql.checkpoint_wal('TRUNCATE')
    assert cp is None or (isinstance(cp, tuple) and len(cp) == 3)

    # 3. bots.json home_node / node_settings roundtrip
    created = save_assistant(
        store,
        AssistantInput(name='桌面专员', engine='small', home_node='pc', model='pc-special-v2', curiosity_enabled=False, notify=True),
        source='pc',
    )
    assert created['home_node'] == 'pc'
    bots_ws_data = json.loads(store.bots_ws_path.read_text('utf-8'))
    ws_entry = next(b for b in bots_ws_data if b['id'] == created['id'])
    assert ws_entry['home_node'] == 'pc'
    assert ws_entry['node_settings']['pc']['model'] == 'pc-special-v2'
    reloaded = Store(store_dir)
    re_entry = next(b for b in reloaded.state['assistants'] if b['id'] == created['id'])
    assert re_entry['home_node'] == 'pc'
    assert re_entry['node_settings']['pc']['model'] == 'pc-special-v2'


@pytest.mark.asyncio
async def test_curiosity_v5_modes_gate_decay_and_scheduler_decoupling(tmp_path):
    store, vault, harness, brain, _ = _make_brain(tmp_path)
    store.update_config({'quiet_start_hour': 0, 'quiet_end_hour': 0, 'small_brain': {'api_key': 'fixture', 'model': 'mock'}})
    store.state['deadlines'] = []
    store.state['opportunities'] = []
    store.save()

    llm_calls = 0

    async def fake_silent(messages, tools=None, big=False, bot_id='companion', **kwargs):
        nonlocal llm_calls
        llm_calls += 1
        return {'role': 'assistant', 'content': f'第 {llm_calls} 次主动问候，今天忙完打算去吃点好的吗喵？'}

    brain.llm.complete_silent = fake_silent
    engine = brain.curiosity_engine
    assert isinstance(engine, CuriosityEngine)

    # 1. Zero-token silent_noop when no pending slots/followups/long silence
    vault.write('profile/personal_impression.md', '# 个人印象档案\n\n## 1. 基本背景\n- **城市**：上海\n')
    store.state['last_user'] = time.time() - 3600  # 1h (< 4h)
    store.save()
    noop_res = await engine.run_cycle(manual=False, bot_id='companion', source='phone')
    assert noop_res['silent'] is True and noop_res['intent'] == 'silent_noop'
    assert llm_calls == 0
    assert brain.proactive_gate.unanswered_count() == 0

    # 2. genuine_curiosity when (待了解) topic exists
    store.sql.upsert_curiosity_topic(category='兴趣', label='喜欢的电影', depth=0, open_question='最近看过什么好片')
    store.state['last_user'] = time.time() - 3600
    store.state['last_proactive_sent_at'] = 0.0
    store.save()
    cur_res = await engine.run_cycle(manual=False, bot_id='companion', source='phone')
    assert cur_res.get('kind') == 'curiosity' and cur_res.get('intent') == 'genuine_curiosity'
    assert llm_calls == 1
    assert brain.proactive_gate.unanswered_count() == 1

    # 3. followup_check_in (P1 overrides P4)
    fu_id = store.sql.add_proactive_followup(
        bot_id='companion', source='phone', topic='快递签收', context='顺丰快递预计下午到', due_at=time.time() - 10
    )
    store.state['last_proactive_sent_at'] = time.time() - 4 * 3600
    store.state['last_curiosity'] = time.time() - 4 * 3600
    store.save()
    fu_res = await engine.run_cycle(manual=False, bot_id='companion', source='phone')
    assert fu_res.get('intent') == 'followup_check_in'
    assert brain.proactive_gate.unanswered_count() == 2
    fu_rows = store.sql.list_due_followups(include_future=True)
    assert next(r for r in fu_rows if r['followup_id'] == fu_id)['attempts'] == 1

    # 4. Silent reply(op="silent") / SILENT_OK never increments unanswered_proactive_count
    async def silent_llm(*args, **kwargs):
        return {'role': 'assistant', 'content': 'SILENT_OK'}

    brain.llm.complete_silent = silent_llm
    store.state['last_proactive_sent_at'] = 0.0
    store.save()
    s_res = await engine.run_cycle(manual=True, bot_id='companion', source='phone')
    assert s_res['silent'] is True
    assert brain.proactive_gate.unanswered_count() == 2

    # 5. ProactiveGate 12h half-life decay from MAX_CONSECUTIVE_UNANSWERED (3)
    gate = brain.proactive_gate
    t0 = time.time()
    store.state['unanswered_proactive_count'] = ProactiveGate.MAX_CONSECUTIVE_UNANSWERED
    store.state['last_proactive_sent_at'] = t0
    store.save()
    allowed_now, reason_now, _ = gate.can_emit_proactive(manual=False, now_ts=t0 + 60)
    assert allowed_now is False and reason_now == 'paused_after_unanswered_nudges'
    decayed = gate.unanswered_count(now_ts=t0 + 12 * 3600 + 5)
    assert decayed == 1  # int(3 * 0.5) == 1 < MAX_CONSECUTIVE_UNANSWERED
    allowed_later, reason_later, _ = gate.can_emit_proactive(manual=False, now_ts=t0 + 12 * 3600 + 5)
    assert allowed_later is True and reason_later == 'ok'

    # 6. quiet_deferred single-write fix & no circuit_broken_until skipping & routine vs curiosity decoupling
    cur_bj_hour = datetime.now(TZ).hour
    store.update_config({'quiet_start_hour': cur_bj_hour, 'quiet_end_hour': (cur_bj_hour + 2) % 24})
    r_quiet = {
        'id': 'rt-quiet-1',
        'goal': '夜间顺延测试例行任务',
        'bot_id': 'companion',
        'interval_hours': 6,
        'enabled': True,
        'next_run': time.time() - 30,
        'last_run': time.time() - 3600,
    }
    store.state['routines'] = [r_quiet]
    store.state['task_run_logs'] = []
    store.state['unanswered_proactive_count'] = 0
    store.save()

    task = asyncio.create_task(brain.scheduler())
    try:
        brain.wake.set()
        await asyncio.sleep(0.25)
        brain.wake.set()
        await asyncio.sleep(0.25)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert r_quiet['last_status'] == 'quiet_deferred'
    assert r_quiet['next_run'] > time.time()
    qd_logs = [lg for lg in store.state.get('task_run_logs', []) if lg.get('task_id') == 'rt-quiet-1' and lg.get('status') == 'quiet_deferred']
    assert len(qd_logs) == 1

    # Now disable quiet hours and verify routine executes cleanly without circuit-breaker blocking
    store.update_config({'quiet_start_hour': 0, 'quiet_end_hour': 0})
    r_cb = {
        'id': 'rt-cb-1',
        'goal': '清理临时列表',
        'bot_id': 'companion',
        'interval_hours': 6,
        'enabled': True,
        'next_run': time.time() - 10,
        'last_run': time.time() - 3600,
    }
    store.state['routines'] = [r_cb]
    store.save()

    async def rt_complete(messages, tools=None, big=False, **kwargs):
        return {'role': 'assistant', 'content': '例行巡检完成，一切正常。'}

    brain.complete = rt_complete
    task2 = asyncio.create_task(brain.scheduler())
    try:
        brain.wake.set()
        for _ in range(25):
            if r_cb.get('last_status') == 'ok':
                break
            await asyncio.sleep(0.05)
    finally:
        task2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task2

    assert r_cb.get('last_status') == 'ok'
    # Routine execution does not pollute curiosity's unanswered counter
    assert brain.proactive_gate.unanswered_count() == 0


@pytest.mark.asyncio
async def test_sleep_time_consolidate_preserves_6_sections_over_5000_chars(tmp_path):
    store, vault, _, _, _ = _make_brain(tmp_path)
    from core.memory_engine import MemoryEngine

    mem = MemoryEngine(store, vault)
    long_filler = '这是一段详实的长期观察与背景细节记录，用于模拟超过五千字的立体个人印象档案。' * 35
    imp_doc = '\n'.join([
        '# 个人印象档案',
        '',
        '## 0. 核心底色',
        f'- **表达偏好**：喜欢直接高效的结论 {long_filler}',
        '## 1. 基本背景',
        '- **职业方向**：(待了解)',
        f'- **长期背景**：{long_filler}',
        '## 2. 作息与习惯',
        '- **咖啡偏好**：（待了解）',
        '## 3. 兴趣与审美',
        f'- **音乐风格**：{long_filler}',
        '## 4. 关系与协作',
        f'- **协作模式**：{long_filler}',
        '## 5. 近期关注',
        '- **本周计划**：（待了解）',
        '',
    ])
    assert len(imp_doc) > 5000
    vault.write('profile/personal_impression.md', imp_doc)

    now_t = time.time()
    store.state['last_user'] = now_t - 300
    store.state['last_sleep_consolidate'] = 0.0
    store.state['messages'] = [
        {'id': 'u1', 'role': 'user', 'text': '我平时写后端架构，最近喜欢喝冰美式。', 'created_at': now_t - 310},
        {'id': 'a1', 'role': 'assistant', 'text': '记下来啦，后端架构配冰美式很提神呢喵～', 'created_at': now_t - 305},
    ]
    store.save()

    payload = {
        'new_traits': ['后端架构师，偏爱冰美式'],
        'impression_patches': [
            {'section': '1', 'item_key': '职业方向', 'fact': '后端与分布式系统架构'},
            {'section': '2', 'item_key': '咖啡偏好', 'fact': '喜欢冰美式'},
        ],
        'curiosity_topic_updates': [
            {'category': '1. 基本背景', 'label': '职业方向', 'depth': 2, 'known_summary': '后端架构', 'open_question': '最常用的语言栈', 'status': 'open'}
        ],
        'followups_to_add': [
            {'topic': '周末咖啡豆补货', 'context': '提到冰美式快喝完了', 'due_hours': 12}
        ],
    }

    async def fake_silent_fn(msgs):
        return {'role': 'assistant', 'content': json.dumps(payload, ensure_ascii=False)}

    await mem.sleep_time_consolidate(fake_silent_fn)
    after_imp = vault.read('profile/personal_impression.md')
    assert len(after_imp) > 5000
    for sec in ('## 0.', '## 1.', '## 2.', '## 3.', '## 4.', '## 5.'):
        assert sec in after_imp
    assert '后端与分布式系统架构' in after_imp
    assert '喜欢冰美式' in after_imp
    assert any(t['label'] == '职业方向' and t['depth'] == 2 for t in store.sql.list_curiosity_topics())
    assert any(f['topic'] == '周末咖啡豆补货' for f in store.sql.list_due_followups(include_future=True))


def test_main_publish_records_sync_engine_events(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_TOKEN', 'test')
    app = create_app(tmp_path, scheduler=False)
    with TestClient(app, headers={'Authorization': 'Bearer test'}):
        async def _pub_events():
            await app.state.brain.publish({
                'type': 'message',
                'source': 'phone',
                'message': {'id': 'm-sync-1', 'bot_id': 'companion', 'order_seq': 77},
            })
            await app.state.brain.publish({'type': 'curiosity_changed', 'source': 'phone', 'seq': 78})

        asyncio.run(_pub_events())
        events = app.state.store.sql.events_since(0)
        kinds = [e['kind'] for e in events]
        assert 'new_message' in kinds
        assert 'curiosity_changed' in kinds


@pytest.mark.asyncio
async def test_honest_failures_and_zero_tool_circuit_breaker(tmp_path, monkeypatch):
    store, vault, harness, brain, _ = _make_brain(tmp_path)

    # 1. vfs_router + remote PC failure -> ok=False
    assert VirtualPathRouter.is_pc_explicit_path({'path': r'C:\Users\demo\secret.txt'}) is True
    assert VirtualPathRouter.is_vault_rel_path('memory_vault/notes/a.md') is True
    brain.dsh_bridge_info['ws_connected'] = True
    brain.update_dsh_peer({'device_id': 'pc_dsh'})

    async def fail_dsh_dispatch(pkt):
        brain.resolve_dsh_command({'request_id': pkt['request_id'], 'result': {'ok': False, 'error': 'remote read denied'}})

    brain.dsh_dispatch = fail_dsh_dispatch
    pc_err = await brain._raw_dsh_rpc('pc_fs', {'fs_op': 'read_file', 'path': r'C:\missing\a.txt'})
    assert pc_err.get('ok') is False and 'denied' in pc_err.get('error', '')

    # 2. cloud out-of-range / missing coords -> ok=False (never blind (0,0))
    from types import SimpleNamespace

    brain.computer = SimpleNamespace(client=None)
    bad_oob = await brain._tool_cloud_desktop({'op': 'click', 'nx': 1500, 'ny': 500})
    assert bad_oob.get('ok') is False
    bad_miss = await brain._tool_cloud_desktop({'op': 'click'})
    assert bad_miss.get('ok') is False

    # 3. computer_desktop missing type_ref -> ok=False without blind (0,0) paste
    import computer_desktop as cd_mod

    desk = cd_mod.CloudDesktop()
    desk._list_windows = lambda: []
    desk._desktop_icon_elements = lambda: []
    monkeypatch.setattr(cd_mod, 'cdp_click_or_type_ref', lambda *a, **kw: None)
    tr_miss = await desk._exec_single_input('type_ref', ref='@b999', text='never-paste')
    assert tr_miss is not None and tr_miss.get('ok') is False and '@b999' in tr_miss.get('error', '')

    # 4. brain_tools_mixin continues past 4 consecutive tool errors without goal_aborted_by_breaker
    store.update_config({'small_brain': {'api_key': 'test', 'model': 'mock'}})
    call_round = 0

    async def err_then_ok_complete(messages, tools=None, big=False, **kwargs):
        nonlocal call_round
        call_round += 1
        if call_round <= 5:
            return {
                'role': 'assistant',
                'content': None,
                'tool_calls': [{
                    'id': f'tc_{call_round}',
                    'type': 'function',
                    'function': {'name': 'read_file', 'arguments': json.dumps({'path': f'nonexistent_{call_round}.txt'})},
                }],
            }
        return {'role': 'assistant', 'content': '前面 5 次文件都不存在，我已核实并诚实汇报结果。'}

    brain.complete = err_then_ok_complete
    chat_res = await brain.chat('试试连续读取不存在的文件然后总结', bot_id='companion', source='phone')
    assert '诚实汇报结果' in chat_res['text']
    assert len(chat_res['trace']) == 5
    assert 'goal_aborted_by_breaker' not in json.dumps(chat_res, ensure_ascii=False)

    # 5. CloudStuckDetector catches ±15 jitter, A<->B ping-pong, and dHash revisit with 'nudge' only (never 'stuck')
    det_j = CloudStuckDetector()
    v1 = det_j.observe('cloud', {'op': 'click', 'nx': 400, 'ny': 400}, {'ok': True, 'ui_changed': False, 'frame_hash': 'h1'})
    v2 = det_j.observe('cloud', {'op': 'click', 'nx': 415, 'ny': 385}, {'ok': True, 'ui_changed': False, 'frame_hash': 'h1'})
    v3 = det_j.observe('cloud', {'op': 'click', 'nx': 390, 'ny': 410}, {'ok': True, 'ui_changed': False, 'frame_hash': 'h1'})
    v4 = det_j.observe('cloud', {'op': 'click', 'nx': 405, 'ny': 395}, {'ok': True, 'ui_changed': False, 'frame_hash': 'h1'})
    assert v1.stage == 'ok' and v2.stage == 'ok'
    assert v3.stage == 'nudge' and v3.rule == 'visual_stagnation'
    assert v4.stage == 'nudge' and v4.stage != 'stuck'

    det_rev = CloudStuckDetector()
    revisit_verdicts = [
        det_rev.observe('cloud', {'op': 'click', 'nx': 100 + i * 110, 'ny': 200 + i * 90}, {'ok': True, 'ui_changed': True, 'frame_hash': 'dhash:abcd1234'})
        for i in range(4)
    ]
    assert revisit_verdicts[-1].stage == 'nudge' and revisit_verdicts[-1].rule == 'state_revisit_loop'
    assert all(v.stage != 'stuck' for v in revisit_verdicts)

    # 6. Bug Box 46-7: root_state must never report 'su_unavailable' when uid=0 shell probe succeeds
    from core.vfs_router import reconcile_device_root_status

    rec = reconcile_device_root_status({'ok': True, 'root': False}, {'ok': True, 'output': '0\n'}, '2026-11-16T02:40:00')
    assert rec['root_functional'] is True
    assert rec['root_degraded'] is False
    assert rec['root_state'] == 'granted'

    # 7. BUG-11-19 #1: Brain.emit indexes tool result summary into context_search before trace compaction
    emitted_trace = [{'tool': 'read_file', 'arguments': {'path': 'a.md'}, 'result': {'ok': True, 'content': 'UniqueSearchMarker95'}, 'status': 'done', 'duration_ms': 12}]
    await brain.emit('已经帮用户核对好文件啦。', kind='chat', notify=False, trace=emitted_trace)
    cs_res = brain.context_search.conversation_search('UniqueSearchMarker95', limit=5)
    assert cs_res.get('count', 0) >= 1
    assert any('UniqueSearchMarker95' in json.dumps(h, ensure_ascii=False) for h in cs_res.get('results', []))


@pytest.mark.asyncio
async def test_curiosity_force_asks_question_even_with_followup_or_initial_silent_tool(tmp_path):
    store, vault, harness, brain, _ = _make_brain(tmp_path)
    store.update_config({'quiet_start_hour': 0, 'quiet_end_hour': 0, 'small_brain': {'api_key': 'fixture', 'model': 'mock'}})
    store.sql.upsert_curiosity_topic(
        topic_id='topic-force-1',
        category='饮食与生活',
        label='周末专属充电方式',
        depth=0,
        priority=0.99,
        open_question='周末最喜欢怎么放松充电呀？',
    )
    store.sql.add_proactive_followup(
        bot_id='companion', source='phone', topic='实验报告进度', context='晚上再确认一次', due_at=time.time() - 10
    )
    store.state['last_user'] = time.time() - 3600
    store.save()

    round_no = 0
    seen_prompts: list[str] = []

    async def silent_then_question(messages, tools=None, big=False, bot_id='companion', **kwargs):
        nonlocal round_no
        round_no += 1
        seen_prompts.append(json.dumps(messages, ensure_ascii=False))
        if round_no == 1:
            return {
                'role': 'assistant',
                'content': '',
                'tool_calls': [{
                    'id': 'tc_silent_1',
                    'type': 'function',
                    'function': {'name': 'reply', 'arguments': json.dumps({'op': 'silent', 'reason': '无通知异常'})},
                }],
            }
        return {'role': 'assistant', 'content': '顺便好奇问一句，主人周末最喜欢用什么方式放松充电呀？'}

    brain.llm.complete_silent = silent_then_question
    res = await brain.curiosity_engine.run_cycle(manual=False, bot_id='companion', source='phone')
    assert round_no == 2
    assert res.get('kind') == 'curiosity'
    assert res.get('intent') == 'followup_check_in'
    assert '？' in str(res.get('text') or '')
    assert '周末专属充电方式' in seen_prompts[0]
    topics_after = [t for t in store.sql.list_curiosity_topics() if t['topic_id'] == 'topic-force-1']
    assert topics_after and topics_after[0]['ask_count'] == 1 and topics_after[0]['depth'] == 1
