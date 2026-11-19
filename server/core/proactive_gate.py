"""
[Module Responsibility]
ProactiveGate 主动打扰防线与未读退避控制器 (`server/core/proactive_gate.py`)。
负责在不改变任何紧急闹钟快车道的前提下，对所有主动关怀/早晚安节拍实施只收紧（tighten-only）防骚扰保护：
1. 免打扰 / 夜间静默窗口拦截 (`is_quiet_hour` / `snooze_until`)；
2. 活跃用户与锁占用抑制 (用户在 90 秒内活跃或对话锁未释放时禁止插话)；
3. 指数级未回复退避 (`1.0x -> 2.5x -> 6.0x -> pause after 3 consecutive unanswered nudges`)，用户发言即刻清零；
4. Pre-push 乐观锁守卫 (`capture_guard()` / `verify_guard()`)，防止生成期间用户刚刚发言导致过时搭话推送出去。
"""
from __future__ import annotations

import contextlib
import time
from typing import Any


class ProactiveGate:
    BACKOFF_MULTIPLIERS = (1.0, 2.5, 6.0)
    MAX_CONSECUTIVE_UNANSWERED = 3
    DECAY_HALF_LIFE_SEC = 12 * 3600.0

    def __init__(self, brain: Any):
        self.brain = brain
        self.store = brain.store

    def unanswered_count(self, now_ts: float | None = None) -> int:
        cnt = max(0, int(self.store.state.get('unanswered_proactive_count') or 0))
        if cnt >= self.MAX_CONSECUTIVE_UNANSWERED:
            now = float(now_ts if now_ts is not None else time.time())
            last_sent = float(
                self.store.state.get('last_proactive_sent_at')
                or self.store.state.get('last_curiosity')
                or 0.0
            )
            if last_sent > 0 and (now - last_sent) >= self.DECAY_HALF_LIFE_SEC:
                elapsed = now - last_sent
                decayed = max(0, int(cnt * (0.5 ** (elapsed / self.DECAY_HALF_LIFE_SEC))))
                if decayed < cnt:
                    self.store.state['unanswered_proactive_count'] = decayed
                    self.store.save()
                    return decayed
        return cnt

    def multiplier(self, now_ts: float | None = None) -> float:
        cnt = self.unanswered_count(now_ts=now_ts)
        if cnt >= self.MAX_CONSECUTIVE_UNANSWERED:
            return 6.0
        idx = min(len(self.BACKOFF_MULTIPLIERS) - 1, cnt)
        return self.BACKOFF_MULTIPLIERS[idx]

    def reset_on_user_turn(self, now_ts: float | None = None, bot_id: str | None = None) -> None:
        now = float(now_ts if now_ts is not None else time.time())
        prev_cnt = self.unanswered_count()
        self.store.state['unanswered_proactive_count'] = 0
        self.store.state['last_user'] = max(float(self.store.state.get('last_user') or 0.0), now)
        sql = getattr(self.store, 'sql', None)
        if sql is not None and hasattr(sql, 'mark_latest_episode_user_replied'):
            with contextlib.suppress(Exception):
                sql.mark_latest_episode_user_replied(bot_id=bot_id, since_ts=now - 7 * 86400.0)
        if prev_cnt > 0:
            self.store.save()

    def record_proactive_sent(self, now_ts: float | None = None) -> int:
        now = float(now_ts if now_ts is not None else time.time())
        cnt = self.unanswered_count() + 1
        self.store.state['unanswered_proactive_count'] = cnt
        self.store.state['last_proactive_sent_at'] = now
        self.store.save()
        return cnt

    def can_emit_proactive(
        self,
        *,
        manual: bool = False,
        allow_alarm: bool = False,
        cooldown_sec: float = 90.0,
        now_ts: float | None = None,
    ) -> tuple[bool, str, float]:
        """Return `(allowed, reason, suggested_next_delay_sec)`."""
        now = float(now_ts if now_ts is not None else time.time())
        snooze_until = float(self.store.state.get('snooze_until') or 0.0)
        if snooze_until > now:
            return False, 'snooze_active', max(60.0, snooze_until - now)

        if manual:
            return True, 'manual_override', 0.0

        cfg = getattr(self.store, 'config', None)
        base_interval = max(1, int(getattr(cfg, 'curiosity_interval_minutes', 30) or 30)) * 60.0

        if not allow_alarm and getattr(self.brain, 'scheduler_engine', None) is not None:
            if self.brain.scheduler_engine.is_quiet_hour(now):
                return False, 'quiet_hours', base_interval

        from core.task_continuity import TaskContinuityManager
        if TaskContinuityManager.is_any_foreground_busy(self.brain) or (
            hasattr(self.brain, 'is_any_bot_busy') and self.brain.is_any_bot_busy()
        ):
            return False, 'bot_busy', 120.0

        last_user = float(self.store.state.get('last_user') or 0.0)
        if last_user > 0 and (now - last_user) < cooldown_sec:
            return False, 'user_recently_active', max(cooldown_sec, (last_user + cooldown_sec) - now)

        cnt = self.unanswered_count(now_ts=now)
        if cnt >= self.MAX_CONSECUTIVE_UNANSWERED:
            return False, 'paused_after_unanswered_nudges', base_interval * 6.0

        mult = self.multiplier(now_ts=now)
        last_sent = float(self.store.state.get('last_proactive_sent_at') or self.store.state.get('last_curiosity') or 0.0)
        if last_sent > 0 and mult > 1.0:
            req_gap = base_interval * mult
            if (now - last_sent) < req_gap:
                return False, f'exponential_backoff_x{mult}', max(60.0, req_gap - (now - last_sent))

        return True, 'ok', base_interval * mult

    def capture_guard(self) -> dict[str, Any]:
        se = getattr(self.brain, 'sync_engine', None)
        return {
            'captured_at': time.time(),
            'last_user': float(self.store.state.get('last_user') or 0.0),
            'state_seq': int(getattr(se, 'state_seq', 0) or self.store.state.get('state_seq') or 0),
        }

    def verify_guard(self, guard: dict[str, Any] | None, *, manual: bool = False) -> tuple[bool, str]:
        if manual or not isinstance(guard, dict):
            return True, 'ok'
        cur_last_user = float(self.store.state.get('last_user') or 0.0)
        if cur_last_user > float(guard.get('last_user') or 0.0) + 0.01:
            return False, 'user_spoke_during_generation'
        if float(self.store.state.get('snooze_until') or 0.0) > time.time():
            return False, 'snooze_activated_during_generation'
        return True, 'ok'
