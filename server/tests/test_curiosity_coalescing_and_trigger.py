from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brain import Brain
from core.curiosity_engine import (
    curiosity_will_coalesce_soon,
    is_coalesceable_blackboard_for_curiosity,
    is_coalesceable_routine_for_curiosity,
)
from harness import Harness
from storage import Store
from vault_manager import Vault


def _make_brain(tmp_path: Path):
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
    store.update_config({
        'quiet_start_hour': 0,
        'quiet_end_hour': 0,
        'curiosity_interval_minutes': 30,
        'small_brain': {'api_key': 'test-key', 'model': 'mock-model'},
    })
    store.state['last_user'] = time.time() - 900.0
    store.save()
    return store, vault, harness, brain, published


def test_coalesceable_classifiers_and_lookahead_gate(tmp_path):
    store, _, _, brain, _ = _make_brain(tmp_path)
    now_ts = time.time()
    store.state['curiosity_next_run'] = now_ts + 45.0
    store.save()

    short_routine = {
        'id': 'rt-notif-30m',
        'bot_id': 'companion',
        'goal': '每半小时检查手机通知栏，提取重要事项',
        'interval_hours': 0.5,
        'enabled': True,
    }
    clock_routine = {
        'id': 'rt-wakeup-0745',
        'bot_id': 'companion',
        'goal': '上课前 15 分钟叫醒',
        'daily_time': '07:45',
        'interval_hours': 24,
        'enabled': True,
    }
    notif_post = {
        'post_id': 'bb-notif-1',
        'status': 'scheduled',
        'author_bot_id': 'companion',
        'target_bot_id': 'companion',
        'text': '【通知栏静默巡检 · 每小时】只留重要验证码/缴费',
        'due_at': now_ts - 10.0,
    }
    alarm_post = {
        'post_id': 'bb-alarm-1',
        'status': 'scheduled',
        'exec_action': 'ring_alarm',
        'text': '响铃叫醒',
        'due_at': now_ts - 5.0,
    }

    assert is_coalesceable_routine_for_curiosity(short_routine) is True
    assert is_coalesceable_routine_for_curiosity(clock_routine) is False
    assert is_coalesceable_blackboard_for_curiosity(notif_post) is True
    assert is_coalesceable_blackboard_for_curiosity(alarm_post) is False
    assert curiosity_will_coalesce_soon(store, brain.proactive_gate, brain.scheduler_engine, 'companion', now_ts) is True

    # If curiosity is far away (e.g. 20m left), independent periodic runner is not held back
    store.state['curiosity_next_run'] = now_ts + 1200.0
    assert curiosity_will_coalesce_soon(store, brain.proactive_gate, brain.scheduler_engine, 'companion', now_ts) is False


@pytest.mark.asyncio
async def test_curiosity_coalesces_periodic_routine_and_blackboard_in_single_cycle(tmp_path):
    store, _, _, brain, published = _make_brain(tmp_path)
    now_ts = time.time()

    store.state['routines'] = [{
        'id': 'rt-notif-halfhour',
        'bot_id': 'companion',
        'goal': '每半小时检查一次手机通知栏，重要事件顺带汇报',
        'interval_hours': 0.5,
        'enabled': True,
        'next_run': now_ts - 20.0,
    }]
    store.state['blackboard_posts'] = [{
        'post_id': 'bb-notif-hourly',
        'status': 'scheduled',
        'author_bot_id': 'companion',
        'target_bot_id': 'companion',
        'text': '【定期通知栏整理】过滤闲聊只报紧急事项',
        'due_at': now_ts - 15.0,
        'schedule': {'interval_hours': 1},
    }]
    brain.notification_hub.ingest_raw_notifications([{
        'package': 'com.eg.android.AlipayGphone',
        'app': '支付宝',
        'title': '动账通知',
        'text': '验证码 842910 用于账单验证',
        'timestamp_ms': int(now_ts * 1000),
    }])
    store.save()

    captured_prompts: list[str] = []

    async def fake_complete(messages, tools=None, big=False, **kwargs):
        captured_prompts.append(str(messages[-1].get('content') or ''))
        return {'role': 'assistant', 'content': '顺手看了一眼通知栏：支付宝发来一条验证码 842910，别忘了看下；其他都安静。'}

    brain.llm.complete_silent = fake_complete
    res = await brain.curiosity(manual=False, bot_id='companion', source='phone')

    assert res.get('coalesced_count') == 2
    assert len(captured_prompts) == 1
    assert '支付宝' in captured_prompts[0]
    assert '本轮已并入好奇心的周期巡检任务' in captured_prompts[0]

    # Only ONE combined proactive message emitted
    cur_msgs = [m for m in store.state['messages'] if m.get('kind') == 'curiosity']
    assert len(cur_msgs) == 1
    assert '842910' in cur_msgs[0]['text']

    # Routine, blackboard post, and notification seen-state all advanced atomically
    rt = store.state['routines'][0]
    assert rt['last_status'] == 'coalesced_ok'
    assert rt['next_run'] > now_ts + 600.0
    bb = store.state['blackboard_posts'][0]
    assert bb['due_at'] > now_ts + 600.0
    assert store.state['notification_inbox'][0].get('seen_by_curiosity') is True
    assert any(lg.get('kind') == 'routine_coalesced' for lg in store.state.get('task_run_logs', []))


@pytest.mark.asyncio
async def test_unseen_notifications_not_lost_on_guard_suppress_and_stale_followup_breaks_loop(tmp_path):
    store, _, _, brain, _ = _make_brain(tmp_path)
    now_ts = time.time()

    brain.notification_hub.ingest_raw_notifications([{
        'package': 'com.sf.express',
        'app': '顺丰速运',
        'title': '快件派送',
        'text': '您的快递已放入丰巢柜，取件码 991234',
        'timestamp_ms': int(now_ts * 1000),
    }])
    fid = store.sql.add_proactive_followup(
        bot_id='companion',
        source='phone',
        topic='旧跟进主题',
        context='之前问过一次，无需重复催',
        due_at=now_ts - 60.0,
    )

    # 1. User speaks mid-generation -> guard suppresses delivery -> notification MUST remain unseen!
    async def guard_trip_complete(messages, tools=None, big=False, **kwargs):
        store.state['last_user'] = time.time() + 1.0
        return {'role': 'assistant', 'content': '快递到了 991234'}

    brain.llm.complete_silent = guard_trip_complete
    sup_res = await brain.curiosity(manual=False, bot_id='companion', source='phone')
    assert sup_res.get('suppressed') is True
    assert sup_res.get('reason') == 'user_spoke_during_generation'
    assert store.state['notification_inbox'][0].get('seen_by_curiosity') is not True

    # Reset last_user backward so next cycle is allowed
    store.state['last_user'] = time.time() - 900.0
    store.save()

    # 2. Model decides stay_silent on the stale followup -> followup advances & defers 6h (breaks 30m silent loop)
    async def silent_tool_complete(messages, tools=None, big=False, **kwargs):
        return {
            'role': 'assistant',
            'content': None,
            'tool_calls': [{
                'id': 'tc_sil_1',
                'type': 'function',
                'function': {'name': 'reply', 'arguments': json.dumps({'op': 'silent', 'reason': '无事不打扰'})},
            }],
        }

    brain.llm.complete_silent = silent_tool_complete
    sil_res = await brain.curiosity(manual=False, bot_id='companion', source='phone')
    assert sil_res.get('silent') is True
    assert store.state['notification_inbox'][0].get('seen_by_curiosity') is True
    # Followup was pushed out 6h and attempts incremented -> no longer due immediately!
    due_now = store.sql.list_due_followups(now_ts=now_ts + 1800.0, limit=5)
    assert all(f['followup_id'] != fid for f in due_now)


@pytest.mark.asyncio
async def test_ai_perceives_duplicate_and_self_merges_via_blackboard_and_curiosity_view(tmp_path):
    store, _, _, brain, _ = _make_brain(tmp_path)
    now_ts = time.time()

    # 1. Calling blackboard(add_routine) with 0.5h preserves float interval & returns coalesce_hint
    r_add = await brain._tool_blackboard({
        'action': 'add_routine',
        'goal': '每半小时检查手机通知栏，重要事件汇总汇报',
        'interval_hours': 0.5,
    })
    assert r_add.get('ok') is True
    assert r_add['routine']['interval_hours'] == 0.5
    assert 'coalesce_hint' in r_add and '30 分钟' in r_add['coalesce_hint']

    # 2. Scheduling a duplicate blackboard periodic notification check triggers duplicate awareness hint
    bb_add = await brain._tool_blackboard({
        'action': 'schedule',
        'delay_minutes': 30,
        'text': '【定期通知栏整理】每半小时检查通知栏',
    })
    assert bb_add.get('ok') is True
    assert 'coalesce_hint' in bb_add and '同类周期项' in bb_add['coalesce_hint']

    # 3. Curiosity awareness prompt shows both items & invites AI to cancel duplicates / merge_with_curiosity
    awareness = brain._get_curiosity_engine()._build_periodic_awareness_summary(
        target_id='companion',
        coalesced=[],
    )
    assert '周期任务自省视图' in awareness
    assert '同类重复巡检' in awareness

    # 4. AI itself can update routine with merge_with_curiosity=True and cancel duplicate blackboard post
    upd = await brain._tool_blackboard({
        'action': 'update_routine',
        'task_id': r_add['routine']['id'],
        'merge_with_curiosity': True,
    })
    assert upd.get('ok') is True and upd['routine']['merge_with_curiosity'] is True
    cnl = await brain._tool_blackboard({'action': 'cancel', 'post_id': bb_add['post_id']})
    assert cnl.get('ok') is True and cnl.get('status') == 'cancelled'
