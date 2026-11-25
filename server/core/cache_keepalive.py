"""
[Module Responsibility]
KV Prefix Cache Keepalive & Idle Hot-Compaction Controller (`server/core/cache_keepalive.py`).

Prevents upstream 5-minute KV cache TTL expiry on large sessions (>=40K tokens) by issuing
at most bounded 1-token zero-persistence keepalive pings during short pauses, then running
one hot-cache compaction at `idle_compact_minutes` (default 9m) and sleeping indefinitely
with 0 overnight burn until the user sends a new message.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from core.task_continuity import TaskContinuityManager
from storage import normalize_node_source

logger = logging.getLogger(__name__)

MIN_KEEPALIVE_TOKENS = 40_000
IDLE_COMPACT_RATIO = 0.60


class CacheKeepaliveManager:
    """Bounded cache keepalive & idle hot-compaction state machine per `(src:bot_id)`."""

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._tick_busy: bool = False
        self._idle_compact_tasks: dict[str, asyncio.Task] = {}
        self._activity_gen: dict[str, int] = {}

    def touch(self, source: str, bot_id: str, now_ts: float | None = None) -> None:
        """Record foreground turn activity, cancel any in-flight idle compaction, and wake from idle sleep."""
        src = normalize_node_source(source or 'phone')
        bid = str(bot_id or 'companion').strip()
        nb_key = f'{src}:{bid}'
        ts = float(now_ts if now_ts is not None else time.time())
        self._activity_gen[nb_key] = self._activity_gen.get(nb_key, 0) + 1
        t = self._idle_compact_tasks.get(nb_key)
        if t is not None and not t.done():
            t.cancel()
        self._sessions[nb_key] = {
            'source': src,
            'bot_id': bid,
            'last_active_at': ts,
            'last_ping_at': ts,
            'ping_count': 0,
            'idle_slept': False,
        }

    def is_idle_compacting(self, source: str, bot_id: str) -> bool:
        nb_key = f'{normalize_node_source(source or "phone")}:{str(bot_id or "companion").strip()}'
        t = self._idle_compact_tasks.get(nb_key)
        return bool(t is not None and not t.done())

    async def cancel_idle_compaction(self, source: str, bot_id: str) -> bool:
        """Immediately cancel an in-flight idle hot-compaction so an incoming user turn never collides."""
        src = normalize_node_source(source or 'phone')
        bid = str(bot_id or 'companion').strip()
        nb_key = f'{src}:{bid}'
        self._activity_gen[nb_key] = self._activity_gen.get(nb_key, 0) + 1
        t = self._idle_compact_tasks.get(nb_key)
        if t is None or t.done():
            return False
        t.cancel()
        with contextlib.suppress(BaseException):
            await t
        return True

    def mark_compacted(self, source: str, bot_id: str, now_ts: float | None = None) -> None:
        """Stop all keepalive & idle compaction after any compaction until a fresh user turn arrives."""
        src = normalize_node_source(source or 'phone')
        bid = str(bot_id or 'companion').strip()
        nb_key = f'{src}:{bid}'
        ts = float(now_ts if now_ts is not None else time.time())
        rec = self._sessions.get(nb_key)
        if rec is None:
            self._sessions[nb_key] = {
                'source': src, 'bot_id': bid, 'last_active_at': ts, 'last_ping_at': ts,
                'ping_count': 0, 'idle_slept': True,
            }
        else:
            rec['idle_slept'] = True
            rec['last_ping_at'] = ts

    def get_state(self, source: str, bot_id: str) -> dict[str, Any] | None:
        src = normalize_node_source(source or 'phone')
        bid = str(bot_id or 'companion').strip()
        rec = self._sessions.get(f'{src}:{bid}')
        return dict(rec) if rec else None

    def hydrate_from_state_if_needed(self, brain: Any, now_ts: float) -> None:
        """Recover idle-compaction tracking after service restart so large active sessions still auto-compact."""
        store = getattr(brain, 'store', None)
        state = getattr(store, 'state', None)
        if not isinstance(state, dict):
            return
        pt_map = getattr(brain, 'last_prompt_tokens', None)
        if not isinstance(pt_map, dict) or not pt_map:
            return
        msgs = state.get('messages') or []
        for raw_key, raw_tok in list(pt_map.items()):
            tok = int(raw_tok or 0)
            if tok < MIN_KEEPALIVE_TOKENS or ':' not in str(raw_key):
                continue
            src_raw, bid = str(raw_key).split(':', 1)
            src = normalize_node_source(src_raw)
            nb_key = f'{src}:{bid}'
            if nb_key in self._sessions:
                continue
            # Find newest dialog timestamp and newest compact_summary timestamp for this session
            last_msg_ts = 0.0
            last_comp_ts = 0.0
            trailing_dialog = 0
            for m in reversed(msgs):
                if m.get('bot_id', 'companion') != bid or normalize_node_source(m.get('source')) != src:
                    continue
                k = m.get('kind')
                ts = float(m.get('created_at') or 0.0)
                if k == 'compact_summary':
                    last_comp_ts = max(last_comp_ts, ts)
                    break
                if m.get('role') in ('user', 'assistant') and k not in ('notification', 'error'):
                    trailing_dialog += 1
                    last_msg_ts = max(last_msg_ts, ts)
            if trailing_dialog < 2 or last_msg_ts <= last_comp_ts:
                continue
            # Only auto-track sessions active within the last 6 hours (avoid compacting stale days-old rooms on boot)
            if last_msg_ts <= 0 or (now_ts - last_msg_ts) > 6 * 3600:
                continue
            self._sessions[nb_key] = {
                'source': src,
                'bot_id': bid,
                'last_active_at': last_msg_ts,
                'last_ping_at': last_msg_ts,
                'ping_count': 0,
                'idle_slept': False,
            }

    async def tick(self, brain: Any, now_ts: float | None = None) -> list[dict[str, Any]]:
        """Evaluate all tracked active sessions once per pulse tick."""
        if self._tick_busy:
            return []
        self._tick_busy = True
        try:
            return await self._tick_impl(brain, now_ts=now_ts)
        finally:
            self._tick_busy = False

    async def _tick_impl(self, brain: Any, now_ts: float | None = None) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        cfg = getattr(getattr(brain, 'store', None), 'config', None)
        if cfg is None or not getattr(cfg, 'cache_keepalive_enabled', True):
            return actions

        now = float(now_ts if now_ts is not None else time.time())
        self.hydrate_from_state_if_needed(brain, now)
        interval_sec = max(60, min(600, int(getattr(cfg, 'cache_keepalive_interval_sec', 260) or 260)))
        cfg_max_pings = max(0, min(5, int(getattr(cfg, 'cache_keepalive_max_pings', 1) if getattr(cfg, 'cache_keepalive_max_pings', None) is not None else 1)))
        idle_compact_min = max(0, min(120, int(getattr(cfg, 'idle_compact_minutes', 9) or 0)))
        idle_compact_sec = idle_compact_min * 60
        hot_compact_threshold = MIN_KEEPALIVE_TOKENS

        for nb_key, rec in list(self._sessions.items()):
            if rec.get('idle_slept'):
                continue
            src = rec['source']
            bid = rec['bot_id']
            if TaskContinuityManager.is_foreground_busy(brain, bid, src):
                rec['last_active_at'] = now
                rec['last_ping_at'] = now
                continue

            prompt_tokens = int(
                (getattr(brain, 'last_prompt_tokens', None) or {}).get(nb_key)
                or ((getattr(brain, 'last_prompt_tokens', None) or {}).get(bid) if src == 'phone' else 0)
                or 0
            )
            if prompt_tokens < MIN_KEEPALIVE_TOKENS:
                continue

            idle_sec = max(0.0, now - float(rec.get('last_active_at') or now))
            since_ping = max(0.0, now - float(rec.get('last_ping_at') or rec.get('last_active_at') or now))

            # 1. Idle hot-compaction deadline reached -> compact once silently on hot cache; cancel immediately on user activity!
            if idle_compact_sec > 0 and idle_sec >= idle_compact_sec:
                manual_map = getattr(brain, '_active_manual_compact_tasks', None)
                if isinstance(manual_map, dict):
                    mt = manual_map.get(nb_key)
                    if mt is not None and not mt.done():
                        continue
                compacted = False
                cancelled = False
                start_gen = self._activity_gen.get(nb_key, 0)
                if prompt_tokens >= hot_compact_threshold and hasattr(brain, '_auto_compact_if_needed'):
                    import inspect as _inspect
                    fn = brain._auto_compact_if_needed
                    kwargs: dict[str, Any] = {'force': True, 'source': src}
                    try:
                        sig = _inspect.signature(fn)
                        if 'silent' in sig.parameters:
                            kwargs['silent'] = True
                        if 'cancel_check' in sig.parameters:
                            kwargs['cancel_check'] = lambda k=nb_key, g=start_gen: self._activity_gen.get(k, 0) != g
                    except (TypeError, ValueError):
                        pass
                    task = asyncio.create_task(fn(bid, **kwargs))
                    self._idle_compact_tasks[nb_key] = task
                    try:
                        res = await task
                        if self._activity_gen.get(nb_key, 0) != start_gen or (isinstance(res, dict) and res.get('cancelled')):
                            cancelled = True
                        else:
                            compacted = bool(isinstance(res, dict) and res.get('compacted'))
                            if compacted:
                                after_tok = int((res or {}).get('after_tokens') or 0) if isinstance(res, dict) else 0
                                if hasattr(brain, 'last_prompt_tokens') and isinstance(brain.last_prompt_tokens, dict):
                                    if after_tok > 0:
                                        brain.last_prompt_tokens[nb_key] = after_tok
                                        if src == 'phone':
                                            brain.last_prompt_tokens[bid] = after_tok
                                    elif int(brain.last_prompt_tokens.get(nb_key) or 0) >= prompt_tokens:
                                        brain.last_prompt_tokens[nb_key] = 0
                                        if src == 'phone':
                                            brain.last_prompt_tokens[bid] = 0
                                if hasattr(brain, 'last_cached_tokens') and isinstance(brain.last_cached_tokens, dict):
                                    brain.last_cached_tokens[nb_key] = 0
                                    if src == 'phone':
                                        brain.last_cached_tokens[bid] = 0
                                if hasattr(brain, 'dsh_adapter'):
                                    with contextlib.suppress(Exception):
                                        await brain.dsh_adapter.reset_session(bid, src)
                    except asyncio.CancelledError:
                        cancelled = True
                        cur_t = asyncio.current_task()
                        if cur_t and hasattr(cur_t, 'cancelling') and cur_t.cancelling():
                            raise
                    except Exception as exc:
                        logger.warning('Idle hot-compaction failed (%s): %s', nb_key, exc)
                    finally:
                        if self._idle_compact_tasks.get(nb_key) is task:
                            self._idle_compact_tasks.pop(nb_key, None)
                if cancelled:
                    logger.info('CacheKeepalive idle_hot_compact cancelled by incoming user activity (%s)', nb_key)
                    actions.append({'session': nb_key, 'action': 'idle_compact_cancelled', 'prompt_tokens': prompt_tokens})
                    continue
                rec['idle_slept'] = True
                logger.info(
                    'CacheKeepalive %s (%s): prompt_tokens=%d idle=%.1fs',
                    'idle_hot_compact' if compacted else 'idle_sleep', nb_key, prompt_tokens, idle_sec,
                )
                actions.append({
                    'session': nb_key,
                    'action': 'idle_hot_compact' if compacted else 'idle_sleep',
                    'prompt_tokens': prompt_tokens,
                    'idle_sec': round(idle_sec, 1),
                })
                continue

            # 2. Strict keepalive cap controlled by user setting `cache_keepalive_max_pings` (and never ping within 60s of compact)
            max_pings = cfg_max_pings if idle_compact_sec == 0 else min(cfg_max_pings, max(0, int((idle_compact_sec - 60) // interval_sec)))
            if int(rec.get('ping_count') or 0) >= max_pings:
                if idle_compact_sec == 0:
                    rec['idle_slept'] = True
                    actions.append({'session': nb_key, 'action': 'idle_sleep', 'prompt_tokens': prompt_tokens})
                continue
            if idle_compact_sec > 0 and (idle_compact_sec - idle_sec) <= 60:
                continue

            # 3. Issue a single zero-persistence 1-token keepalive ping before 5-minute TTL expires
            if since_ping >= interval_sec:
                dsh = getattr(brain, 'dsh_adapter', None)
                ping_res: dict[str, Any] = {'ok': False, 'skipped': True}
                if dsh is not None and hasattr(dsh, 'keepalive_session'):
                    try:
                        ping_res = await dsh.keepalive_session(bid, src)
                    except Exception as exc:
                        logger.warning('Cache keepalive ping failed (%s): %s', nb_key, exc)
                rec['last_ping_at'] = now
                rec['ping_count'] = int(rec.get('ping_count') or 0) + 1
                if ping_res.get('reason') == 'session_not_warm':
                    # Stop further pings when sidecar isn't warm, but NEVER cancel scheduled idle_hot_compact!
                    rec['ping_count'] = max_pings
                    if idle_compact_sec == 0:
                        rec['idle_slept'] = True
                logger.info('CacheKeepalive ping (%s): count=%d res=%s', nb_key, rec['ping_count'], ping_res.get('reason') or ping_res.get('kept_alive'))
                actions.append({
                    'session': nb_key,
                    'action': 'keepalive_ping',
                    'prompt_tokens': prompt_tokens,
                    'ping_count': rec['ping_count'],
                    'result': ping_res,
                })
        return actions
