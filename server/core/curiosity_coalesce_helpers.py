"""Coalescing helpers shared between CuriosityEngine and periodic scheduling guards."""
from __future__ import annotations

from typing import Any

_NOTIF_INSPECTION_HINTS = ('通知栏', '通知巡检', '通知整理', '手机通知')
_ALARM_BLOCK_HINTS = ('叫醒', '起床', '闹钟')


def is_coalesceable_routine_for_curiosity(routine: dict[str, Any]) -> bool:
    """Return True if a background periodic routine safely merges into curiosity cycles."""
    if not isinstance(routine, dict) or not routine.get('enabled', True):
        return False
    if routine.get('daily_time'):
        return False
    goal = str(routine.get('goal') or '').strip()
    if not goal or any(h in goal for h in _ALARM_BLOCK_HINTS):
        return False
    if routine.get('merge_with_curiosity') is False:
        return False
    if routine.get('merge_with_curiosity') is True:
        return True
    try:
        iv_hours = float(routine.get('interval_hours') or 24.0)
    except (TypeError, ValueError):
        iv_hours = 24.0
    if 0 < iv_hours <= 1.0:
        return True
    return any(h in goal for h in _NOTIF_INSPECTION_HINTS) and iv_hours <= 2.0


def is_coalesceable_blackboard_for_curiosity(post: dict[str, Any]) -> bool:
    """Return True if a scheduled periodic blackboard item belongs in the merged curiosity check."""
    if not isinstance(post, dict) or post.get('status') != 'scheduled':
        return False
    if str(post.get('exec_action') or '').strip().lower() == 'ring_alarm':
        return False
    if post.get('watch'):
        return False
    if post.get('merge_with_curiosity') is False:
        return False
    text = str(post.get('text') or post.get('content') or post.get('title') or '').strip()
    if not text or any(h in text for h in _ALARM_BLOCK_HINTS):
        return False
    if post.get('merge_with_curiosity') is True:
        return True
    sched = post.get('schedule') if isinstance(post.get('schedule'), dict) else {}
    if sched.get('daily_time'):
        return False
    try:
        iv_hours = float(sched.get('interval_hours') or post.get('interval_hours') or 0.0)
    except (TypeError, ValueError):
        iv_hours = 0.0
    try:
        iv_mins = float(sched.get('interval_minutes') or post.get('interval_minutes') or 0.0)
    except (TypeError, ValueError):
        iv_mins = 0.0
    if (0 < iv_hours <= 1.0) or (0 < iv_mins <= 60.0):
        return True
    return any(h in text for h in _NOTIF_INSPECTION_HINTS)


def curiosity_will_coalesce_soon(
    store: Any,
    gate: Any,
    scheduler_engine: Any,
    bot_id: str,
    now_ts: float,
    lookahead_sec: float = 120.0,
) -> bool:
    """Return True if an active curiosity cycle for `bot_id` will fire within `lookahead_sec`."""
    cfg = getattr(store, 'config', None)
    if cfg is None or not getattr(cfg, 'enabled', True):
        return False
    if scheduler_engine is not None and scheduler_engine.is_quiet_hour(now_ts):
        return False
    if float(store.state.get('snooze_until') or 0.0) > now_ts:
        return False
    if gate is not None and gate.unanswered_count(now_ts=now_ts) >= gate.MAX_CONSECUTIVE_UNANSWERED:
        return False
    from assistants import resolve_bot_for_source
    target_id = str(bot_id or store.state.get('primary_bot_id') or 'companion')
    bot_enabled = False
    for b in (store.state.get('assistants') or []):
        if isinstance(b, dict) and b.get('id') == target_id and b.get('enabled', True):
            bot_enabled = any(
                bool(resolve_bot_for_source(b, src).get('curiosity_enabled'))
                for src in ('phone', 'phone2', 'pc', 'pc2')
            )
            break
    if not bot_enabled:
        return False
    cur_min = max(1, int(getattr(cfg, 'curiosity_interval_minutes', 30) or 30))
    last_cur = float(store.state.get('last_curiosity') or 0.0)
    cur_due = float(store.state.get('curiosity_next_run') or 0.0) or (last_cur + cur_min * 60.0)
    return cur_due <= (now_ts + lookahead_sec)


def detect_periodic_coalesce_opportunities(
    store: Any,
    text: str,
    interval_hours: float | None = None,
    exclude_id: str = '',
) -> str | None:
    """Return a concise awareness hint when a task overlaps curiosity or duplicates another periodic item."""
    txt = str(text or '').strip()
    if not txt or any(h in txt for h in _ALARM_BLOCK_HINTS):
        return None
    cur_min = max(1, int(getattr(getattr(store, 'config', None), 'curiosity_interval_minutes', 30) or 30))
    is_notif = any(h in txt for h in _NOTIF_INSPECTION_HINTS)
    iv_h = float(interval_hours or 0.0)
    is_short = 0 < iv_h <= 1.5
    similar_ids: list[str] = []
    for r in (store.state.get('routines') or []):
        if not isinstance(r, dict) or not r.get('enabled', True) or str(r.get('id') or '') == exclude_id:
            continue
        r_goal = str(r.get('goal') or '')
        if is_notif and any(h in r_goal for h in _NOTIF_INSPECTION_HINTS):
            similar_ids.append(f"routine:{r.get('id')}")
    for p in (store.state.get('blackboard_posts') or []):
        if not isinstance(p, dict) or p.get('status') != 'scheduled' or str(p.get('post_id') or '') == exclude_id:
            continue
        p_txt = str(p.get('text') or p.get('content') or '')
        if is_notif and any(h in p_txt for h in _NOTIF_INSPECTION_HINTS):
            similar_ids.append(f"blackboard:{p.get('post_id')}")
    parts: list[str] = []
    if is_notif or is_short:
        parts.append(f"当前好奇心节奏为每 {cur_min} 分钟一次且自带通知栏检查，你可传 merge_with_curiosity=True 并入好奇心周期一次性执行并汇报，避免重复唤醒")
    if similar_ids:
        parts.append(f"同时检测到已有同类周期项 {', '.join(similar_ids[:4])}，若属重复建议用 cancel/remove 合并留一")
    return '；'.join(parts) if parts else None
