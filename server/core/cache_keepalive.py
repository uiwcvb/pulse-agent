"""
[Module Responsibility]
KV Prefix Cache Keepalive & Idle Hot-Compaction Controller (`server/core/cache_keepalive.py`).

Prevents upstream 5-minute KV cache TTL expiry on large sessions (>=40K tokens) by issuing
at most bounded 1-token zero-persistence keepalive pings during short pauses, then running
one hot-cache compaction at `idle_compact_minutes` (default 9m) and sleeping indefinitely
with 0 overnight burn until the user sends a new message.
"""
from __future__ import annotations

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

    def touch(self, source: str, bot_id: str, now_ts: float | None = None) -> None:
        """Record foreground turn activity and wake the session from idle sleep."""
        src = normalize_node_source(source or 'phone')
        bid = str(bot_id or 'companion').strip()
        nb_key = f'{src}:{bid}'
        ts = float(now_ts if now_ts is not None else time.time())
        self._sessions[nb_key] = {
            'source': src,
            'bot_id': bid,
            'last_active_at': ts,
            'last_ping_at': ts,
            'ping_count': 0,
            'idle_slept': False,
        }

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

    async def tick(self, brain: Any, now_ts: float | None = None) -> list[dict[str, Any]]:
        """Evaluate all tracked active sessions once per pulse tick."""
        actions: list[dict[str, Any]] = []
        cfg = getattr(getattr(brain, 'store', None), 'config', None)
        if cfg is None or not getattr(cfg, 'cache_keepalive_enabled', True):
            return actions

        now = float(now_ts if now_ts is not None else time.time())
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

            # 1. Idle hot-compaction deadline reached -> compact once on hot cache if large enough, then sleep!
            if idle_compact_sec > 0 and idle_sec >= idle_compact_sec:
                compacted = False
                if prompt_tokens >= hot_compact_threshold and hasattr(brain, '_auto_compact_if_needed'):
                    try:
                        res = await brain._auto_compact_if_needed(bid, force=True, source=src)
                        compacted = bool(isinstance(res, dict) and res.get('compacted'))
                        if compacted:
                            if hasattr(brain, 'last_prompt_tokens') and isinstance(brain.last_prompt_tokens, dict):
                                brain.last_prompt_tokens[nb_key] = 0
                                if src == 'phone':
                                    brain.last_prompt_tokens[bid] = 0
                            if hasattr(brain, 'dsh_adapter'):
                                with contextlib.suppress(Exception):
                                    await brain.dsh_adapter.reset_session(bid, src)
                    except Exception as exc:
                        logger.warning('Idle hot-compaction failed (%s): %s', nb_key, exc)
                rec['idle_slept'] = True
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
                    rec['idle_slept'] = True
                actions.append({
                    'session': nb_key,
                    'action': 'keepalive_ping',
                    'prompt_tokens': prompt_tokens,
                    'ping_count': rec['ping_count'],
                    'result': ping_res,
                })
        return actions
