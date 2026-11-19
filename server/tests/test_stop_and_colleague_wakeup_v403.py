from __future__ import annotations

import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import pytest

from main import create_app


def _build_test_app(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('AGENT_TOKEN', 'test')
    app = create_app(tmp_path, scheduler=False)
    store = app.state.store
    store.update_config({
        'api_key': 'sk-test-key',
        'default_model': 'deepseek-chat',
        'enabled': True,
        'quiet_start_hour': 0,
        'quiet_end_hour': 0,
    })
    return app, app.state.brain, store


@pytest.mark.asyncio
async def test_concurrent_colleague_blackboard_wakeups_preserve_order_and_never_reanswer_user(tmp_path: Path, monkeypatch):
    _, brain, store = _build_test_app(tmp_path, monkeypatch)
    seen_calls: list[dict] = []
    bb1_entered = asyncio.Event()
    bb1_release = asyncio.Event()

    async def fake_complete(messages, tools=None, big=False, **kw):
        tail_user = str((messages[-1] if messages else {}).get('content') or '')
        seen_calls.append({
            'last_role': (messages[-1] if messages else {}).get('role'),
            'messages': list(messages),
            'tail': tail_user,
        })
        if '我现在有多少钱' in tail_user and '协作来信' not in tail_user:
            return {'role': 'assistant', 'content': '喵，用户目前总计约 4.19 万，马上能动用的约 2.53 万。', 'tool_calls': []}
        if '探微核对账目' in tail_user:
            bb1_entered.set()
            await asyncio.wait_for(bb1_release.wait(), timeout=3.0)
            return {'role': 'assistant', 'content': '收到探微的核对结果喵，已同步黑板。', 'tool_calls': []}
        return {'role': 'assistant', 'content': '收到审校复核建议喵，这边只针对该复核条目跟进。', 'tool_calls': []}

    brain.complete = fake_complete  # type: ignore[method-assign]

    # Step 1: User asks initial question and gets normal answer.
    await brain.chat('我现在有多少钱？', bot_id='companion', source='phone')

    # Step 2: Colleague A posts @companion while nothing else is running -> enters fake_complete & pauses.
    p1 = await brain.blackboard_engine.execute({
        'action': 'post',
        'title': '探微核对账目',
        'text': '@companion 请确认探微核对账目里的冻结金额。',
        'mentions': ['@companion'],
        '_caller_bot_id': 'research',
        '_caller_source': 'phone',
    })
    assert p1['ok'] is True
    await asyncio.wait_for(bb1_entered.wait(), timeout=3.0)

    # Step 3: While colleague A turn holds bot_lock, colleague B also @mentions companion!
    p2 = await brain.blackboard_engine.execute({
        'action': 'post',
        'title': '审校复核建议',
        'text': '@companion 请确认审校复核建议里的风险提示。',
        'mentions': ['@companion'],
        '_caller_bot_id': 'research',
        '_caller_source': 'phone',
    })
    assert p2['ok'] is True
    await asyncio.sleep(0.05)

    # Critical invariant 1: Colleague B's bot_message MUST NOT be written into store BEFORE Colleague A finishes!
    room_msgs_mid = [
        m for m in store.state['messages']
        if m.get('bot_id') == 'companion' and m.get('source', 'phone') == 'phone'
    ]
    assert not any('审校复核建议' in str(m.get('text') or '') for m in room_msgs_mid)

    # Release Colleague A and wait for Colleague B to run & finish.
    bb1_release.set()
    for _ in range(80):
        if len(seen_calls) >= 3 and not brain.is_node_bot_busy('companion', 'phone'):
            break
        await asyncio.sleep(0.03)

    assert len(seen_calls) == 3
    bb2_call = seen_calls[2]
    assert bb2_call['last_role'] == 'user'
    assert '【跨端/同事 Bot 协作来信' in bb2_call['tail']
    assert '审校复核建议' in bb2_call['tail']
    assert '[历史协作归档' not in bb2_call['tail']
    assert '严禁重复回答用户此前已经得到回复的历史提问' in bb2_call['tail']

    room_msgs_end = [
        (m.get('role'), m.get('kind'), str(m.get('text') or '')[:30])
        for m in store.state['messages']
        if m.get('bot_id') == 'companion' and m.get('source', 'phone') == 'phone'
    ]
    assert [r for r, _, _ in room_msgs_end] == ['user', 'assistant', 'user', 'assistant', 'user', 'assistant']


@pytest.mark.asyncio
async def test_user_stop_cancels_active_stream_and_queued_blackboard_wakeups_with_monotonic_seq(tmp_path: Path, monkeypatch):
    app, brain, store = _build_test_app(tmp_path, monkeypatch)
    published: list[dict] = []
    orig_publish = brain.publish

    async def capture_publish(ev: dict):
        published.append(dict(ev))
        await orig_publish(ev)

    brain.publish = capture_publish  # type: ignore[method-assign]

    active_started = asyncio.Event()
    never_finish = asyncio.Event()
    wakeup_complete_calls = 0

    async def slow_complete(messages, tools=None, big=False, **kw):
        nonlocal wakeup_complete_calls
        tail = str((messages[-1] if messages else {}).get('content') or '')
        if '协作来信' in tail:
            wakeup_complete_calls += 1
            return {'role': 'assistant', 'content': '不应执行到的排队唤醒回复', 'tool_calls': []}
        await brain._set_stream(text='用户余正在帮您核查数据，请稍候…', speaker_id='companion', speaker_name='默认助手')
        active_started.set()
        await never_finish.wait()
        return {'role': 'assistant', 'content': 'done', 'tool_calls': []}

    brain.complete = slow_complete  # type: ignore[method-assign]

    # Start main user turn (which acquires phone:companion lock & streams text)
    enq = await brain.enqueue_chat('帮我查一下全部明细', bot_id='companion', source='phone')
    active_turn_id = enq['turn_id']
    await asyncio.wait_for(active_started.wait(), timeout=3.0)

    # Enqueue two queued blackboard @companion wakeups while main turn holds lock
    await brain.blackboard_engine.execute({
        'action': 'post',
        'title': '排队提醒1',
        'text': '@companion 请看第一条排队委托',
        'mentions': ['@companion'],
        '_caller_bot_id': 'research',
        '_caller_source': 'phone',
    })
    await brain.blackboard_engine.execute({
        'action': 'post',
        'title': '排队提醒2',
        'text': '@companion 请看第二条排队委托',
        'mentions': ['@companion'],
        '_caller_bot_id': 'research',
        '_caller_source': 'phone',
    })
    await asyncio.sleep(0.05)

    # Trigger POST /api/stop?source=phone with only {"bot_id": "companion"} in JSON body!
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url='http://test', headers={'Authorization': 'Bearer test'}) as client:
        resp = await client.post('/api/stop?source=phone', json={'bot_id': 'companion'})
        assert resp.status_code == 200
        stop_json = resp.json()
        assert stop_json['stopped'] is True

    await asyncio.sleep(0.15)

    # Active stream & queued wakeups MUST all be cancelled without running complete()
    assert wakeup_complete_calls == 0
    assert 'phone:companion' not in brain.executions_by_node_bot
    assert 'phone:companion' not in brain.streams_by_node_bot
    assert not brain.queued_tasks_by_node_bot.get('phone:companion')
    assert active_turn_id in brain._stopped_turn_ids

    stop_stream_clears = [e for e in published if e.get('type') == 'stream' and e.get('stream') is None and e.get('bot_id') == 'companion']
    stop_exec_clears = [e for e in published if e.get('type') == 'execution' and e.get('execution') is None and e.get('bot_id') == 'companion']
    stopped_evs = [e for e in published if e.get('type') == 'stopped' and e.get('bot_id') == 'companion']
    assert stop_stream_clears and stop_exec_clears and stopped_evs
    assert stop_exec_clears[0].get('turn_id') == active_turn_id
    assert stopped_evs[-1].get('turn_id') == active_turn_id
    assert stopped_evs[-1].get('seq', 0) > stop_exec_clears[0].get('seq', 0) > 0
    assert stop_exec_clears[-1].get('seq', 0) >= stopped_evs[-1].get('seq', 0)
