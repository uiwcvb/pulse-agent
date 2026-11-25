from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
import pytest

from main import create_app
from core.compaction_service import execute_streaming_compaction
from core.observation_masker import extract_ephemeral_ui_from_result, maybe_mask_room_observations
from core.turn_history import build_compact_turn_step


def _build_test_app(tmp_path: Path, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setenv('AGENT_TOKEN', 'test')
    app = create_app(tmp_path, scheduler=False)
    store = app.state.store
    store.update_config({
        'api_key': 'sk-test-key',
        'default_model': 'deepseek-reasoner',
        'enabled': True,
        'quiet_start_hour': 0,
        'quiet_end_hour': 0,
        'auto_compact_tokens': 100_000,
    })
    return app, app.state.brain, store


@pytest.mark.asyncio
async def test_clock_wheel_fast_lane_alarm_while_bot_busy(tmp_path: Path):
    """BUSY bot running a slow turn MUST NOT block a `direct_alarm` fast-lane alert on the 0.5s clock wheel."""
    _, brain, store = _build_test_app(tmp_path)
    published: list[dict] = []

    async def capture(ev: dict):
        published.append(ev)

    brain.publish = capture  # type: ignore[method-assign]

    bot_lock = brain._get_bot_lock('companion', 'phone')
    await bot_lock.acquire()
    wheel_task = None
    try:
        brain.executions_by_node_bot['phone:companion'] = {'bot_id': 'companion', 'goal': 'long_slow_turn', 'trace': []}
        assert brain.is_node_bot_busy('companion', 'phone') is True

        res = await brain._tool_schedule_task({
            'action': 'add_timer',
            'goal': '快车道紧急叫醒闹钟',
            'bot_id': 'companion',
            'delay_seconds': 0.05,  # _tool_schedule_task clamps min 5s, override due_at for sub-second wheel test
            'priority': 'direct_alarm',
            'exec_action': 'ring_alarm',
            'direct_only': True,
        })
        assert res['scheduled'] is True
        store.state['timers'][-1]['due_at'] = time.time() - 0.1
        store.save()

        wheel_task = asyncio.create_task(brain.clock_wheel.run_forever())
        t0 = time.perf_counter()
        while (time.perf_counter() - t0) < 0.8:
            if any(ev.get('type') == 'emergency_alarm' for ev in published):
                break
            await asyncio.sleep(0.05)

        elapsed = time.perf_counter() - t0
        alarms = [ev for ev in published if ev.get('type') == 'emergency_alarm']
        assert len(alarms) == 1
        assert elapsed <= 0.65
        assert store.state['timers'][-1]['done'] is True
    finally:
        if wheel_task is not None:
            wheel_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await wheel_task
        brain.executions_by_node_bot.pop('phone:companion', None)
        bot_lock.release()


@pytest.mark.asyncio
async def test_cross_pool_deduplication_timer_and_blackboard(tmp_path: Path):
    """schedule_task and blackboard scheduled posts deduplicate within +/-3 min and upgrade exec_action."""
    _, brain, store = _build_test_app(tmp_path)

    t_res = await brain._tool_schedule_task({
        'action': 'add_timer',
        'goal': '提醒我吃钙片',
        'bot_id': 'companion',
        'delay_minutes': 10,
        'priority': 'normal',
    })
    assert t_res['scheduled'] is True
    assert len(store.state['timers']) == 1

    bb_res = await brain.blackboard_engine.execute({
        'action': 'post',
        'title': '吃钙片提醒',
        'text': '@self 帮我提醒一下吃钙片',
        'delay_minutes': 10,
        'exec_action': 'ring_alarm',
        '_caller_bot_id': 'companion',
        '_caller_source': 'phone',
    })
    assert bb_res['ok'] is True
    assert bb_res.get('deduplicated') is True
    assert bb_res.get('duplicate_pool') == 'timer'
    assert len(store.state.get('blackboard_posts', [])) == 0
    assert store.state['timers'][0]['exec_action'] == 'ring_alarm'

    # Reverse direction: blackboard -> timer
    bb2 = await brain.blackboard_engine.execute({
        'action': 'post',
        'title': '取快递',
        'text': '@self 取楼下快递',
        'delay_minutes': 30,
        '_caller_bot_id': 'companion',
        '_caller_source': 'phone',
    })
    assert bb2['ok'] is True and not bb2.get('deduplicated')
    t_res2 = await brain._tool_schedule_task({
        'action': 'add_timer',
        'goal': '记得去取楼下快递',
        'bot_id': 'companion',
        'delay_minutes': 31,
        'exec_action': 'notify_user',
    })
    assert t_res2.get('deduplicated') is True
    assert t_res2.get('duplicate_pool') == 'blackboard'
    assert store.state['blackboard_posts'][0]['exec_action'] == 'notify_user'


@pytest.mark.asyncio
async def test_deadline_on_late_and_step_boundary_interrupt_injection(tmp_path: Path):
    """`on_late=drop/notify_only` grading and step-boundary `<urgent_scheduled_interrupt>` injection in busy ReAct loop."""
    _, brain, store = _build_test_app(tmp_path)
    published: list[dict] = []

    async def capture(ev: dict):
        published.append(ev)

    brain.publish = capture  # type: ignore[method-assign]

    now = time.time()
    store.state['timers'] = [
        {
            'id': 'tm-drop',
            'goal': '过期的秒杀提醒',
            'bot_id': 'companion',
            'due_at': now - 300,
            'deadline_seconds': 60,
            'on_late': 'drop',
            'done': False,
        },
        {
            'id': 'tm-notify-only',
            'goal': '迟到的喝水提醒',
            'bot_id': 'companion',
            'due_at': now - 200,
            'deadline_seconds': 60,
            'on_late': 'notify_only',
            'done': False,
        },
    ]
    store.save()

    await brain.clock_wheel.tick_once()
    await asyncio.sleep(0.05)
    await brain.clock_wheel.tick_once()
    await asyncio.sleep(0.08)

    assert all(t['done'] for t in store.state['timers'])
    notifs = [ev for ev in published if ev.get('type') == 'user_notification']
    assert len(notifs) == 1
    assert '迟到提醒' in notifs[0]['title']

    # Now verify ReAct step-boundary urgent interrupt injection while slow turn is active!
    seen_steps_prompts: list[list[dict]] = []
    call_idx = 0

    async def slow_multi_step_complete(messages, tools=None, big=False, **kw):
        nonlocal call_idx
        call_idx += 1
        seen_steps_prompts.append(list(messages))
        if call_idx == 1:
            # Enqueue urgent interrupt mid-turn right as step 1 returns a tool call
            brain.clock_wheel.enqueue_urgent_interrupt(
                'companion',
                'phone',
                '⏰ 紧急日程：导师提前到 5 分钟后面试，请立刻转告用户！',
                item_id='tm-int-1',
            )
            return {
                'content': '先查一下当前列表',
                'tool_calls': [{'id': 'call_1', 'type': 'function', 'function': {'name': 'list_dir', 'arguments': '{"path": "."}'}}],
                '_metrics': {'prompt_tokens': 18000, 'cached_tokens': 0},
            }
        return {
            'content': '收到，已立刻在回复中通知用户面试提前！',
            'tool_calls': [],
            '_metrics': {'prompt_tokens': 18500, 'cached_tokens': 17000},
        }

    brain.complete = slow_multi_step_complete  # type: ignore[method-assign]
    out = await brain.chat('帮我看看工作区目录有什么文件', bot_id='companion', source='phone')
    assert out['kind'] == 'chat'
    assert call_idx >= 2
    step2_user_texts = [str(m.get('content', '')) for m in seen_steps_prompts[1] if m.get('role') == 'user']
    assert any('<urgent_scheduled_interrupt>' in t and '导师提前到 5 分钟后面试' in t for t in step2_user_texts)
    assert not brain.urgent_interrupts_by_node_bot.get('phone:companion')


@pytest.mark.asyncio
async def test_section43_notification_hygiene_and_android_guards(tmp_path: Path):
    """Regular chat emits notify=False; curiosity/deadline follow bot.notify; collapse_key & Android guards intact."""
    _, brain, store = _build_test_app(tmp_path)
    published: list[dict] = []

    async def capture(ev: dict):
        published.append(ev)

    brain.publish = capture  # type: ignore[method-assign]

    chat_msg = await brain.emit('普通聊天回复', 'chat', track=False, bot_id='companion', source='phone')
    assert chat_msg['notify'] is False

    cur_msg = await brain.emit('主动搭话测试', 'curiosity', track=False, bot_id='companion', source='phone')
    assert cur_msg['notify'] is True

    n_res = await brain._tool_notify_user({'title': '折叠测试', 'body': '内容A', 'urgent': False})
    assert n_res['notified'] is True
    notif_evs = [ev for ev in published if ev.get('type') == 'user_notification']
    assert notif_evs[-1]['collapse_key'] == 'bot:companion:notify'

    # Also ensure run_scheduled skips extra user_notification when trace already called emergency_alarm
    published.clear()
    sched_call = 0

    async def sched_alarm_complete(msgs, tools=None, big=False, **kw):
        nonlocal sched_call
        sched_call += 1
        if sched_call == 1:
            return {'content': '', 'tool_calls': [{'id': 'al1', 'type': 'function', 'function': {'name': 'emergency_alarm', 'arguments': json.dumps({'title': '闹钟', 'reason': '该起床啦'})}}]}
        return {'content': '闹钟响过啦，用户快起床！', 'tool_calls': []}

    brain.complete = sched_alarm_complete  # type: ignore[method-assign]
    em = await brain.run_scheduled('起床闹钟', bot_id='companion', label='定时闹钟')
    assert em['notify'] is False
    assert not [ev for ev in published if ev.get('type') == 'user_notification']

    # Android Java notification guards sanity check (skip gracefully on server-only deployments)
    svc_path = Path(__file__).resolve().parents[2] / 'android' / 'app' / 'src' / 'main' / 'java' / 'dev' / 'bubuki' / 'proactive' / 'AgentForegroundService.java'
    rcv_path = Path(__file__).resolve().parents[2] / 'android' / 'app' / 'src' / 'main' / 'java' / 'dev' / 'bubuki' / 'proactive' / 'AgentActionReceiver.java'
    if svc_path.is_file() and rcv_path.is_file():
        svc_java = svc_path.read_text('utf-8')
        rcv_java = rcv_path.read_text('utf-8')
        assert 'boolean shouldNotify=m.optBoolean("notify"' in svc_java
        assert 'pulse_updates' in svc_java
        assert 'collapse_key' in svc_java
        assert 'notifId' in svc_java and 'notifId' in rcv_java


@pytest.mark.asyncio
async def test_l0_ghost_peak_reset_and_compaction_self_healing(tmp_path: Path):
    """Multi-step tool bloat resets `last_prompt_tokens` at turn end & compaction heals isolated >=64K ghost peaks."""
    _, brain, store = _build_test_app(tmp_path)
    step_no = 0

    async def bloated_loop_complete(messages, tools=None, big=False, **kw):
        nonlocal step_no
        step_no += 1
        nb_key = 'phone:companion'
        if step_no == 1:
            brain.llm._record_usage('companion', {'prompt_tokens': 18_000, 'prompt_cache_hit_tokens': 0}, is_foreground=True, source='phone')
            return {
                'content': '读取中',
                'tool_calls': [{'id': 'c1', 'type': 'function', 'function': {'name': 'list_dir', 'arguments': '{}'}}],
                '_metrics': {'prompt_tokens': 18_000, 'cached_tokens': 0},
            }
        if step_no == 2:
            brain.llm._record_usage('companion', {'prompt_tokens': 88_000, 'prompt_cache_hit_tokens': 18_000}, is_foreground=True, source='phone')
            return {
                'content': '读取完成',
                'tool_calls': [],
                '_metrics': {'prompt_tokens': 88_000, 'cached_tokens': 18_000},
            }
        return {'content': 'done', 'tool_calls': []}

    brain.complete = bloated_loop_complete  # type: ignore[method-assign]
    await brain.chat('帮我扫一下文件', bot_id='companion', source='phone')

    post_pt = brain.last_prompt_tokens.get('phone:companion', 0)
    assert 18_000 <= post_pt < 25_000, f'Expected ghost peak reset near 18K, got {post_pt}'

    # Seed isolated 95K ghost peak on a 100K window with only 2 small messages -> must heal to ~est_base, NOT compact!
    store.update_config({'auto_compact_tokens': 100_000})
    brain.last_prompt_tokens['phone:companion'] = 95_000
    brain.last_prompt_tokens['companion'] = 95_000
    store.state.setdefault('last_prompt_tokens', {})['phone:companion'] = 95_000

    res = await execute_streaming_compaction(brain.memory_engine, bot_id='companion', source='phone', force=False, sys_tokens=14800)
    assert res is None
    assert brain.last_prompt_tokens['phone:companion'] < 30_000


@pytest.mark.asyncio
async def test_l1_l2_l3_ephemeral_ui_scrub_observation_masking_and_subtask(tmp_path: Path):
    """Verify L1 ephemeral UI split + write-arg sha256 scrub, L2 cold/hot masking, and L3 run_subtask isolation."""
    _, brain, store = _build_test_app(tmp_path)

    # L1a: Ephemeral UI Tree split (generous structured receipt <= 8000 chars)
    huge_nodes = [{'id': f'@e{i}', 'text': f'控件标题{i} ' + ('x' * 40), 'bounds': [0, i * 10, 100, i * 10 + 10]} for i in range(40)]
    raw_res = {'ok': True, 'op': 'inspect', 'app': 'com.test.demo', 'ui_tree': huge_nodes, '_image_attached': True}
    serialized = json.dumps(raw_res, ensure_ascii=False)
    compact_receipt, eph_tail = extract_ephemeral_ui_from_result('phone_workspace', raw_res, serialized)
    assert len(compact_receipt) <= 8000
    assert eph_tail is not None and '<ephemeral_ui_state' in eph_tail

    # L1b: Write-file arg preservation (<12000 intact, >12000 head+tail slice without sha256 corruption)
    small_code = 'print("hello")\n' * 100
    step_small = build_compact_turn_step(
        '写入短脚本',
        [{'id': 'cw1', 'type': 'function', 'function': {'name': 'write_file', 'arguments': json.dumps({'path': 'demo.py', 'content': small_code})}}],
        [('cw1', '{"ok": true}')],
    )
    assert json.loads(step_small[0]['tool_calls'][0]['function']['arguments'])['content'] == small_code
    assert 'sha256=' not in step_small[0]['tool_calls'][0]['function']['arguments']

    huge_code = ('A' * 7000) + ('M' * 2000) + ('Z' * 7000)
    step = build_compact_turn_step(
        '写入大脚本',
        [{'id': 'cw2', 'type': 'function', 'function': {'name': 'write_file', 'arguments': json.dumps({'path': 'big.py', 'content': huge_code})}}],
        [('cw2', '{"ok": true}')],
    )
    archived_args = step[0]['tool_calls'][0]['function']['arguments']
    assert '已保留前后共 6000 字符' in archived_args and 'sha256=' not in archived_args and len(archived_args) < 7000

    # L2: Dual-mode observation masking preserves errors and tool_calls while shrinking old large tool outputs
    old_ts = time.time() - 900.0
    m1 = store.message('user', '第一轮问题', 'chat', bot_id='companion', source='phone')
    m1['created_at'] = old_ts
    m2 = store.message('assistant', '第一轮结果', 'chat', bot_id='companion', source='phone', turn_steps=[
        [
            {'role': 'assistant', 'content': '查', 'tool_calls': [{'id': 'tc1', 'type': 'function', 'function': {'name': 'run_bash', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'tc1', 'content': 'NORMAL_LOG_DATA ' * 50},
        ],
        [
            {'role': 'assistant', 'content': '再查', 'tool_calls': [{'id': 'tc2', 'type': 'function', 'function': {'name': 'run_bash', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'tc2', 'content': '{"ok": false, "error": "FATAL_ERR ' + ('e' * 400) + '"}'},
        ],
    ])
    m2['created_at'] = old_ts
    m3 = store.message('user', '第二轮', 'chat', bot_id='companion', source='phone')
    m3['created_at'] = old_ts
    m4 = store.message('assistant', '第二轮完成', 'chat', bot_id='companion', source='phone', turn_steps=[
        [
            {'role': 'assistant', 'content': '步3', 'tool_calls': [{'id': 'tc3', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'tc3', 'content': 'RECENT_STEP ' * 50},
        ],
        [
            {'role': 'assistant', 'content': '步4', 'tool_calls': [{'id': 'tc4', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'tc4', 'content': 'LATEST_STEP ' * 50},
        ],
    ])
    m4['created_at'] = old_ts

    masked_count = maybe_mask_room_observations(store, brain.memory_engine, 'companion', 'phone', now_ts=time.time())
    assert masked_count == 1
    assert '[已归档观察结果' in m2['turn_steps'][0][1]['content']
    assert 'FATAL_ERR' in m2['turn_steps'][1][1]['content']  # Error observation NEVER masked
    assert m2['turn_steps'][0][0]['tool_calls'][0]['function']['name'] == 'run_bash'  # tool_calls preserved
    assert 'LATEST_STEP' in m4['turn_steps'][1][1]['content']  # Recent tail preserved

    # L3: run_subtask folds sub-steps without polluting parent token metrics
    brain.last_prompt_tokens['phone:companion'] = 16_000
    brain.last_prompt_tokens['companion'] = 16_000
    store.state.setdefault('last_prompt_tokens', {})['phone:companion'] = 16_000

    sub_step = 0

    async def sub_complete(msgs, tools=None, big=False, **kw):
        nonlocal sub_step
        sub_step += 1
        brain.llm._record_usage('companion', {'prompt_tokens': 42_000, 'prompt_cache_hit_tokens': 0}, is_foreground=True, source='phone')
        if sub_step == 1:
            return {'content': '列出工作区', 'tool_calls': [{'id': 's1', 'type': 'function', 'function': {'name': 'list_dir', 'arguments': '{}'}}]}
        return {'content': '调研结论：工作区目录正常，无异常文件。', 'tool_calls': []}

    brain.complete = sub_complete  # type: ignore[method-assign]
    sub_res = await brain._tool_run_subtask({'goal': '检查工作区结构并返回简短摘要', 'max_steps': 5, 'wait': True})
    assert sub_res['ok'] is True
    assert sub_res['steps_used'] == 2
    assert '工作区目录正常' in sub_res['result_summary']
    assert sub_res.get('ctx_id')
    assert brain.last_prompt_tokens['phone:companion'] == 16_000
    assert brain.subtask_depth == 0
