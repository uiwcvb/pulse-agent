from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from computer_desktop import CloudDesktop
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
        'auto_compact_tokens': 100_000,
    })
    return app, app.state.brain, store


@pytest.mark.asyncio
async def test_multi_segment_auto_continuation_blocks_waiting_routine(tmp_path: Path, monkeypatch):
    """A long foreground task hitting per-segment step limit MUST seamlessly continue and block due routines until finished."""
    _, brain, store = _build_test_app(tmp_path, monkeypatch)
    brain.max_loop_steps_override = 2
    brain.max_foreground_segments = 4

    store.state['routines'] = [{
        'id': 'rt-due-1',
        'bot_id': 'companion',
        'goal': '每日晚间日程例行汇总',
        'interval_hours': 24,
        'next_run': time.time() + 120.0,
        'enabled': True,
    }]
    store.save()

    events_order: list[str] = []
    fg_calls = 0
    step2_entered = asyncio.Event()
    step2_release = asyncio.Event()

    async def fake_complete(messages, tools=None, big=False, **kw):
        nonlocal fg_calls
        sys_txt = str((messages[0] if messages else {}).get('content') or '')
        tail_txt = str((messages[-1] if messages else {}).get('content') or '')
        if '[例行巡检' in sys_txt or '[定点例行' in sys_txt or '到点触发]：每日晚间日程例行汇总' in tail_txt:
            events_order.append('routine_complete')
            return {'role': 'assistant', 'content': '⏰ 晚间例行任务已完成整理。', 'tool_calls': []}

        fg_calls += 1
        events_order.append(f'fg_call_{fg_calls}')
        if fg_calls == 2:
            # Make routine due right in the middle of active foreground step 2 (before segment 1->2 transition!)
            store.state['routines'][0]['next_run'] = time.time() - 5.0
            store.save()
            step2_entered.set()
            await asyncio.wait_for(step2_release.wait(), timeout=3.0)
        if fg_calls <= 4:
            # Segment 1 has 2 steps (calls 1..2), Segment 2 has 2 steps (calls 3..4) -> crosses segment boundary!
            return {
                'role': 'assistant',
                'content': '',
                'tool_calls': [{
                    'id': f'call_fg_{fg_calls}',
                    'type': 'function',
                    'function': {
                        'name': 'blackboard',
                        'arguments': json.dumps({'action': 'peek', '_probe': fg_calls}),
                    },
                }],
            }
        return {
            'role': 'assistant',
            'content': '喵～全部 4 步连续核查都已做完并整理好了！',
            'tool_calls': [],
        }

    brain.complete = fake_complete  # type: ignore[method-assign]
    sched_task = asyncio.create_task(brain.scheduler())
    try:
        fg_task = asyncio.create_task(brain.chat('请连续做完 4 步检查再回复', bot_id='companion', source='phone'))
        await asyncio.wait_for(step2_entered.wait(), timeout=3.0)
        # Wake scheduler repeatedly while foreground task spans segment boundary; routine MUST defer!
        for _ in range(3):
            brain.wake.set()
            await asyncio.sleep(0.05)
        assert 'routine_complete' not in events_order

        step2_release.set()
        res = await asyncio.wait_for(fg_task, timeout=5.0)
        assert '全部 4 步连续核查都已做完' in str(res.get('text') or '')
        assert fg_calls == 5

        # Once foreground finishes completely, due routine runs cleanly!
        for _ in range(40):
            brain.wake.set()
            if 'routine_complete' in events_order:
                break
            await asyncio.sleep(0.04)
        assert 'routine_complete' in events_order
        assert events_order.index('routine_complete') > events_order.index('fg_call_5')
    finally:
        sched_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sched_task


@pytest.mark.asyncio
async def test_urgent_interrupt_watchdog_never_fires_while_step_busy_and_preserves_task(tmp_path: Path, monkeypatch):
    """Urgent step-boundary interrupt watchdog MUST NOT fire while a slow tool step exceeds poll window, and foreground task must continue."""
    _, brain, store = _build_test_app(tmp_path, monkeypatch)
    brain.clock_wheel.watchdog_poll_interval = 0.02

    fallback_runs = 0
    seen_interrupt_prompt = ''
    slow_tool_started = asyncio.Event()
    slow_tool_finish = asyncio.Event()

    async def slow_read_file(args: dict):
        tag = str((args or {}).get('path') or '')
        if tag == 'slow-step':
            slow_tool_started.set()
            await asyncio.wait_for(slow_tool_finish.wait(), timeout=3.0)
            return {'ok': True, 'step': 'slow-step-done'}
        return {'ok': True, 'step': f'done:{tag}'}

    brain.harness.custom_handlers['read_file'] = slow_read_file

    step_num = 0

    async def fake_complete(messages, tools=None, big=False, **kw):
        nonlocal step_num, seen_interrupt_prompt
        step_num += 1
        for m in messages:
            c = str(m.get('content') or '')
            if '<urgent_scheduled_interrupt>' in c:
                seen_interrupt_prompt = c
        if step_num == 1:
            return {
                'role': 'assistant',
                'content': '',
                'tool_calls': [{
                    'id': 'c_slow_1',
                    'type': 'function',
                    'function': {'name': 'read_file', 'arguments': json.dumps({'path': 'slow-step'})},
                }],
            }
        if step_num == 2:
            # Verify urgent interrupt was injected before step 2, then keep working on original goal!
            return {
                'role': 'assistant',
                'content': '',
                'tool_calls': [{
                    'id': 'c_next_2',
                    'type': 'function',
                    'function': {'name': 'read_file', 'arguments': json.dumps({'path': 'final-step'})},
                }],
            }
        return {'role': 'assistant', 'content': '喵，加急提醒已顺手看过了，原来的最终步骤也已经顺利完成！', 'tool_calls': []}

    brain.complete = fake_complete  # type: ignore[method-assign]

    async def fallback_fn():
        nonlocal fallback_runs
        fallback_runs += 1

    fg_task = asyncio.create_task(brain.chat('请先跑慢步骤再完成最终步骤', bot_id='companion', source='phone'))
    await asyncio.wait_for(slow_tool_started.wait(), timeout=3.0)

    brain.clock_wheel.enqueue_urgent_interrupt(
        'companion',
        'phone',
        '⏰ 到点喝水加急提醒',
        item_id='tm-urg-1',
        kind='timer',
        fallback_coro_factory=fallback_fn,
    )
    # Wait > 20 poll ticks (0.45s > 15 * 0.02s) while step 1 is still actively blocked!
    await asyncio.sleep(0.45)
    assert fallback_runs == 0

    slow_tool_finish.set()
    res = await asyncio.wait_for(fg_task, timeout=3.0)
    await asyncio.sleep(0.1)

    assert fallback_runs == 0
    assert '<urgent_scheduled_interrupt>' in seen_interrupt_prompt
    assert '严禁丢弃或终止用户当前正在进行的任务' in seen_interrupt_prompt
    assert step_num == 3
    assert '原来的最终步骤也已经顺利完成' in str(res.get('text') or '')


@pytest.mark.asyncio
async def test_system_or_peer_preempt_auto_resumes_foreground_task_but_user_stop_does_not(tmp_path: Path, monkeypatch):
    """Peer/system steer preemption MUST auto-resume interrupted foreground goal; explicit user_stop MUST NOT."""
    _, brain, store = _build_test_app(tmp_path, monkeypatch)

    step1_blocked = asyncio.Event()
    resume_prompts: list[str] = []
    resumed_done = asyncio.Event()

    async def blocking_read(args: dict):
        if str((args or {}).get('path') or '') == 'wait-forever':
            step1_blocked.set()
            await asyncio.sleep(30.0)
        return {'ok': True}

    brain.harness.custom_handlers['read_file'] = blocking_read

    async def fake_complete(messages, tools=None, big=False, **kw):
        tail = str((messages[-1] if messages else {}).get('content') or '')
        if '<auto_resume_interrupted_task' in tail:
            resume_prompts.append(tail)
            resumed_done.set()
            return {'role': 'assistant', 'content': '喵～刚才插进来的同事消息已经看完了，现在已把「填完研究申报表」全部续跑搞定！', 'tool_calls': []}
        if '协作来信' in tail or '紧急对齐' in tail:
            return {'role': 'assistant', 'content': '收到探微对齐通知喵。', 'tool_calls': []}
        if '填完研究申报表' in tail:
            return {
                'role': 'assistant',
                'content': '余正在操作表格中…',
                'tool_calls': [{
                    'id': 'c_blk_1',
                    'type': 'function',
                    'function': {'name': 'read_file', 'arguments': json.dumps({'path': 'wait-forever'})},
                }],
            }
        if '被用户停止的任务' in tail:
            return {
                'role': 'assistant',
                'content': '准备执行…',
                'tool_calls': [{
                    'id': 'c_blk_2',
                    'type': 'function',
                    'function': {'name': 'read_file', 'arguments': json.dumps({'path': 'wait-forever'})},
                }],
            }
        return {'role': 'assistant', 'content': 'ok', 'tool_calls': []}

    brain.complete = fake_complete  # type: ignore[method-assign]

    # Part A: peer steer preempts foreground task -> finishes wake -> auto-resumes original task cleanly with NO fake user message!
    fg1 = asyncio.create_task(brain.chat('帮我填完研究申报表', bot_id='companion', source='phone'))
    await asyncio.wait_for(step1_blocked.wait(), timeout=3.0)
    await brain.blackboard_engine.execute({
        'action': 'post',
        'title': '紧急对齐',
        'text': '@companion 请先看一眼紧急对齐消息',
        'mentions': ['@companion'],
        'steer': True,
        '_caller_bot_id': 'research',
        '_caller_source': 'phone',
    })
    await asyncio.wait_for(fg1, timeout=3.0)
    await asyncio.wait_for(resumed_done.wait(), timeout=4.0)
    await asyncio.sleep(0.05)

    assert len(resume_prompts) == 1
    assert '填完研究申报表' in resume_prompts[0]
    user_bubbles = [m for m in store.state['messages'] if m.get('role') == 'user' and m.get('kind') != 'bot_message']
    assert len(user_bubbles) == 1  # No synthetic duplicate user bubble added!
    assert any('全部续跑搞定' in str(m.get('text') or '') for m in store.state['messages'])

    # Part B: explicit user_stop MUST clear continuity and NEVER auto-resume.
    step1_blocked.clear()
    resume_count_before = len(resume_prompts)
    fg2 = asyncio.create_task(brain.chat('被用户停止的任务', bot_id='companion', source='phone'))
    await asyncio.wait_for(step1_blocked.wait(), timeout=3.0)
    await brain.stop_immediately(bot_id='companion', reason='user_stop', source='phone')
    await asyncio.wait_for(fg2, timeout=3.0)
    await asyncio.sleep(0.2)
    assert len(resume_prompts) == resume_count_before


@pytest.mark.asyncio
async def test_long_turn_completion_refreshes_last_user_preventing_immediate_curiosity(tmp_path: Path, monkeypatch):
    """Completing a long foreground turn MUST refresh `last_user` so curiosity does not fire seconds later."""
    _, brain, store = _build_test_app(tmp_path, monkeypatch)

    async def fake_complete(messages, tools=None, big=False, **kw):
        # Simulate that turn took 20 minutes and `last_user` started stale.
        store.state['last_user'] = time.time() - 1200.0
        return {'role': 'assistant', 'content': '喵，长任务完成啦！', 'tool_calls': []}

    brain.complete = fake_complete  # type: ignore[method-assign]
    t_before = time.time() - 1.0
    await brain.chat('运行一个很长的任务', bot_id='companion', source='phone')

    assert float(store.state.get('last_user') or 0.0) >= t_before
    cur_res = await brain.curiosity(manual=False, bot_id='companion', source='phone')
    assert cur_res.get('suppressed') is True


@pytest.mark.asyncio
async def test_cloud_desktop_stop_preserves_agent_control_unless_takeover_locked(tmp_path: Path, monkeypatch):
    """Programmatic POST /stop must keep control='agent' unless user explicitly locked takeover."""
    monkeypatch.setenv('HOME', str(tmp_path))
    daemon = CloudDesktop()
    st0 = await daemon.stop()
    assert st0['control'] == 'agent'
    assert st0['user_takeover_locked'] is False

    await daemon.takeover()
    st_locked = await daemon.stop()
    assert st_locked['control'] == 'user'
    assert st_locked['user_takeover_locked'] is True

    await daemon.handback()
    assert daemon.control == 'agent'
    assert daemon.user_takeover_locked is False
