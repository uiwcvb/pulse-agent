"""Unified delta synchronization, version hashing, and WebSocket RPC engine."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import threading
import time
from typing import Any, Callable

from assistants import primary_bot_id_for_source, resolve_bots_for_source
from storage import normalize_node_source


def summarize_trace_brief(trace_list: list[dict], limit: int = 6) -> str:
    """Summarize execution trace steps into a compact readable string."""
    items: list[str] = []
    for t in (trace_list or [])[:limit]:
        if not isinstance(t, dict) or not t.get('tool'):
            continue
        tname = str(t['tool'])
        targs = t.get('arguments') if isinstance(t.get('arguments'), dict) else {}
        hint = ''
        for k in ('op', 'action', 'url', 'query', 'target', 'path', 'to_bot_id', 'command', 'text'):
            v = targs.get(k)
            if v and isinstance(v, (str, int, float)):
                sv = str(v).strip().replace('\n', ' ')[:36]
                if sv:
                    hint = f'{hint}:{sv}' if (hint and k not in ('op', 'action')) else sv
                    if len(hint) >= 30:
                        break
        items.append(f'{tname}({hint})' if hint else tname)
    return ', '.join(items)


def _hash_json(obj: Any) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str).encode('utf-8')
    return hashlib.sha1(raw).hexdigest()[:12]


class SyncEngine:
    """Tracks monotonic state sequence, domain version hashes, and WS RPC dispatch."""

    def __init__(
        self,
        store,
        vault,
        brain,
        publish,
        deps: dict[str, Callable[..., Any]] | None = None,
    ):
        self.store = store
        self.vault = vault
        self.brain = brain
        self.publish = publish
        self.deps: dict[str, Callable[..., Any]] = dict(deps or {})
        sql_seq = store.sql.latest_event_seq() if hasattr(store, 'sql') else 0
        self._seq_lock = threading.Lock()
        self.state_seq = max(1, int(store.state.get('state_seq') or 1), int(sql_seq or 0))
        self._last_ver: dict[str, str] = {}
        self._dsh_waiters: set[asyncio.Event] = set()
        self.refresh_baseline_versions()

    def wake_dsh_waiters(self) -> None:
        for ev in list(self._dsh_waiters):
            with contextlib.suppress(Exception):
                ev.set()

    def register_dsh_waiter(self) -> asyncio.Event:
        ev = asyncio.Event()
        self._dsh_waiters.add(ev)
        return ev

    def unregister_dsh_waiter(self, ev: asyncio.Event) -> None:
        self._dsh_waiters.discard(ev)

    def sync_versions(self, source: str = 'pc') -> dict[str, Any]:
        return {
            'state_seq': int(self.state_seq),
            'pins_ver': self.pins_ver(),
            'bots_ver': self.bots_ver(source),
            'config_ver': self.config_ver(),
            'schedules_ver': self.schedules_ver(),
            'messages_epoch': int(self.store.state.get('messages_epoch') or 1),
        }

    def record_event(self, kind: str, source: str = '', ver: str = '', payload: dict | None = None) -> int:
        with self._seq_lock:
            sql_seq = 0
            if hasattr(self.store, 'sql'):
                with contextlib.suppress(Exception):
                    sql_seq = self.store.sql.append_event(kind=kind, source=source, ver=ver, payload=payload)
            self.state_seq = max(self.state_seq + 1, int(sql_seq or 0))
            self.store.state['state_seq'] = self.state_seq
            seq_val = self.state_seq
        self.wake_dsh_waiters()
        return seq_val

    def _brain_ready_flags(self, has_key: bool, has_model: bool) -> tuple[bool, bool]:
        cfg = self.store.config
        small_ready = bool(cfg.small_brain.api_key and cfg.small_brain.model) or (has_key and has_model)
        big_ready = bool(cfg.big_brain.api_key and cfg.big_brain.model) or (has_key and has_model)
        return small_ready, big_ready

    @staticmethod
    def _sort_messages(msgs: list[dict]) -> list[dict]:
        return sorted(
            msgs,
            key=lambda m: (
                int(m.get('order_seq') or m.get('seq') or 0),
                float(m.get('created_at') or m.get('created') or 0.0),
            ),
        )

    def attach_deps(self, deps: dict[str, Callable[..., Any]]) -> None:
        self.deps.update(deps)

    def _watchdog_summary(self) -> list[dict]:
        watchdogs = getattr(self.brain, 'watchdogs', {}) or {}
        out: list[dict] = []
        for k in sorted(watchdogs.keys()):
            w = watchdogs.get(k)
            if isinstance(w, dict):
                out.append({wk: wv for wk, wv in w.items() if wk != 'task'})
        return out

    def bots_ver(self, source: str = 'phone') -> str:
        req_src = normalize_node_source(source)
        return _hash_json({
            'bots': resolve_bots_for_source(self.store.state.get('assistants', []), req_src),
            'primary_bot_id': primary_bot_id_for_source(self.store, req_src),
        })

    def _raw_bots_ver(self) -> str:
        return _hash_json({
            'assistants': self.store.state.get('assistants', []),
            'primary_bot_id': self.store.state.get('primary_bot_id'),
        })

    def pins_ver(self) -> str:
        return _hash_json(self.brain.memory_engine.list_pins())

    def config_ver(self) -> str:
        return _hash_json({
            'config': self.store.config.public(),
            'auto_compact_tokens': self.store.config.auto_compact_tokens,
            'curiosity_interval_minutes': self.store.config.curiosity_interval_minutes,
            'onboarding_completed': bool(self.store.config.onboarding_completed),
            'enabled': self.store.config.enabled,
        })

    def schedules_ver(self) -> str:
        return _hash_json({
            'routines': self.store.state.get('routines', []),
            'timers': self.store.state.get('timers', []),
            'deadlines': self.store.state.get('deadlines', []),
            'watchdogs': self._watchdog_summary(),
        })

    def vault_ver(self) -> str:
        files = [
            {
                'path': f.get('path'),
                'bytes': f.get('bytes', f.get('size')),
                'modified': f.get('modified', f.get('updated_at')),
            }
            for f in self.vault.files()
            if f.get('path') not in ('INDEX.md', 'skills/INDEX.md')
        ]
        return _hash_json(files)

    def refresh_baseline_versions(self) -> None:
        self._last_ver['bots'] = self._raw_bots_ver()
        self._last_ver['pins'] = self.pins_ver()
        self._last_ver['config'] = self.config_ver()
        self._last_ver['schedules'] = self.schedules_ver()
        self._last_ver['vault'] = self.vault_ver()

    def _bump_seq(self, kind: str = 'state_change', source: str = '', ver: str = '', payload: dict | None = None) -> int:
        return self.record_event(kind=kind, source=source, ver=ver, payload=payload)

    def _dispatch(self, event: dict) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.publish(event))

    def _prepare_pins_event(self, force: bool = False, source: str | None = None) -> dict | None:
        pins = self.brain.memory_engine.list_pins()
        cur = _hash_json(pins)
        if not force and cur == self._last_ver.get('pins'):
            return None
        self._last_ver['pins'] = cur
        norm_src = normalize_node_source(source) if source else ''
        seq = self._bump_seq(kind='pins_changed', source=norm_src, ver=cur, payload={'pins_count': len(pins)})
        ev = {'type': 'pins_changed', 'pins': pins, 'pins_ver': cur, 'state_seq': seq}
        if source:
            ev['source'] = norm_src
        return ev

    def trigger_pins_changed(self, force: bool = False, source: str | None = None) -> bool:
        ev = self._prepare_pins_event(force=force, source=source)
        if not ev:
            return False
        self._dispatch(ev)
        return True

    async def notify_pins_changed(self, force: bool = False, source: str | None = None) -> bool:
        ev = self._prepare_pins_event(force=force, source=source)
        if not ev:
            return False
        await self.publish(ev)
        return True

    def _prepare_vault_event(self, path: str = '', action: str = 'write', force: bool = False) -> dict | None:
        cur = self.vault_ver()
        if not force and cur == self._last_ver.get('vault'):
            return None
        self._last_ver['vault'] = cur
        seq = self._bump_seq(kind='vault_changed', ver=cur, payload={'path': path, 'action': action})
        return {'type': 'vault_changed', 'path': path, 'action': action, 'vault_ver': cur, 'state_seq': seq}

    def trigger_vault_changed(self, path: str = '', action: str = 'write', force: bool = False) -> bool:
        ev = self._prepare_vault_event(path=path, action=action, force=force)
        if not ev:
            return False
        self._dispatch(ev)
        return True

    async def notify_vault_changed(self, path: str = '', action: str = 'write', force: bool = False) -> bool:
        ev = self._prepare_vault_event(path=path, action=action, force=force)
        if not ev:
            return False
        await self.publish(ev)
        return True

    def _prepare_schedules_event(self, kind: str = 'all', task_id: str = '', force: bool = False) -> dict | None:
        cur = self.schedules_ver()
        if not force and cur == self._last_ver.get('schedules'):
            return None
        self._last_ver['schedules'] = cur
        seq = self._bump_seq(kind='schedules_changed', ver=cur, payload={'kind': kind, 'task_id': task_id})
        return {
            'type': 'schedules_changed',
            'kind': kind,
            'task_id': task_id,
            'schedules_ver': cur,
            'routine_count': len(self.store.state.get('routines', [])),
            'state_seq': seq,
        }

    def trigger_schedules_changed(self, kind: str = 'all', task_id: str = '', force: bool = False) -> bool:
        ev = self._prepare_schedules_event(kind=kind, task_id=task_id, force=force)
        if not ev:
            return False
        self._dispatch(ev)
        return True

    async def notify_schedules_changed(self, kind: str = 'all', task_id: str = '', force: bool = False) -> bool:
        ev = self._prepare_schedules_event(kind=kind, task_id=task_id, force=force)
        if not ev:
            return False
        await self.publish(ev)
        return True

    def _prepare_bots_event(
        self,
        bot_id: str = '',
        source: str | None = None,
        deleted_bot_id: str = '',
        force: bool = False,
    ) -> dict | None:
        cur = self._raw_bots_ver()
        if not force and cur == self._last_ver.get('bots'):
            return None
        self._last_ver['bots'] = cur
        req_src = normalize_node_source(source or 'phone')
        b_ver = self.bots_ver(req_src)
        seq = self._bump_seq(kind='bots_changed', source=req_src, ver=b_ver, payload={'bot_id': bot_id, 'deleted_bot_id': deleted_bot_id})
        ev: dict[str, Any] = {
            'type': 'bots_changed',
            'bot_id': bot_id,
            'bots': self.store.state.get('assistants', []),
            'bots_ver': b_ver,
            'primary_bot_id': primary_bot_id_for_source(self.store, req_src),
            'state_seq': seq,
        }
        if source:
            ev['source'] = req_src
        if deleted_bot_id:
            ev['deleted_bot_id'] = deleted_bot_id
        return ev

    def trigger_bots_changed(
        self,
        bot_id: str = '',
        source: str | None = None,
        deleted_bot_id: str = '',
        force: bool = False,
    ) -> bool:
        ev = self._prepare_bots_event(bot_id=bot_id, source=source, deleted_bot_id=deleted_bot_id, force=force)
        if not ev:
            return False
        self._dispatch(ev)
        return True

    async def notify_bots_changed(
        self,
        bot_id: str = '',
        source: str | None = None,
        deleted_bot_id: str = '',
        force: bool = False,
    ) -> bool:
        ev = self._prepare_bots_event(bot_id=bot_id, source=source, deleted_bot_id=deleted_bot_id, force=force)
        if not ev:
            return False
        await self.publish(ev)
        return True

    def on_vault_write(self, rel_name: str, _content: str | None = None) -> None:
        if rel_name == 'profile/pinned_memories.md':
            self._last_ver['vault'] = self.vault_ver()
            self.trigger_pins_changed()
            return
        self.trigger_vault_changed(path=rel_name, action='write')

    def on_workspace_synced(self, bots_changed: bool, routines_changed: bool) -> None:
        if bots_changed:
            self.trigger_bots_changed(source='phone')
        if routines_changed:
            self.trigger_schedules_changed(kind='routine')

    async def build_status_response(
        self,
        *,
        after_id: str | None = None,
        source: str | None = None,
        messages_epoch: int | None = None,
        bots_ver: str | None = None,
        pins_ver: str | None = None,
        config_ver: str | None = None,
        schedules_ver: str | None = None,
        include_pins: bool = False,
    ) -> dict[str, Any]:
        self.store.sync_from_workspace()
        s = self.store.state
        req_src = normalize_node_source(source)
        cur_epoch = int(s.get('messages_epoch') or 1)
        desktop = await self.deps['fast_desktop_status']()
        has_key = bool(self.store.config.effective_api_key(big=False) or self.store.config.effective_api_key(big=True))
        has_model = bool(self.store.config.effective_model(big=False) or self.store.config.effective_model(big=True))
        raw_done = (
            self.brain.last_completed_stream
            if (self.brain.last_completed_stream and time.time() - float(self.brain.last_completed_stream.get('completed_at') or 0) < 15.0)
            else None
        )
        last_done = raw_done if (raw_done and normalize_node_source(raw_done.get('source')) == req_src) else None
        all_msgs = self._sort_messages(self.deps['filter_msgs_by_source'](s['messages'], req_src))
        epoch_reset = bool(messages_epoch is not None and int(messages_epoch) != cur_epoch)
        if epoch_reset:
            recent_tail = all_msgs[-120:]
        elif after_id:
            idx = next((i for i in range(len(all_msgs) - 1, -1, -1) if all_msgs[i].get('id') == after_id), -1)
            if idx >= 0:
                recent_tail = all_msgs[idx + 1:]
            else:
                epoch_reset = True
                recent_tail = all_msgs[-120:]
        else:
            recent_tail = all_msgs[-3:]
        slim_execs = self.deps['get_execs_for_source'](req_src)
        filtered_streams = self.deps['get_streams_for_source'](req_src)
        slim_exec_fn = self.deps['slim_exec']
        cur_exec = next(iter(slim_execs.values()), None) or (
            slim_exec_fn(self.brain.execution)
            if (isinstance(self.brain.execution, dict) and normalize_node_source(self.brain.execution.get('source')) == req_src)
            else None
        )
        cur_stream = next(iter(filtered_streams.values()), None) or (
            slim_exec_fn(self.brain.live_stream)
            if (isinstance(self.brain.live_stream, dict) and normalize_node_source(self.brain.live_stream.get('source')) == req_src)
            else None
        )
        dsh_on = self.deps['is_dsh_online']()
        dsh_peers = self.deps['slim_dsh_peers'](self.deps['get_dsh_peers']())
        dsh_st = self.deps['slim_dsh_status'](self.brain.dsh_bridge_info.get('status', {}))
        src_msg_ids = {m['id'] for m in all_msgs}
        src_pending = {k: v for k, v in s['pending'].items() if k in src_msg_ids}
        clients_map = self.deps['clients']

        cur_bots_ver = self.bots_ver(req_src)
        cur_pins_ver = self.pins_ver()
        cur_config_ver = self.config_ver()
        cur_schedules_ver = self.schedules_ver()
        bots_unchanged = bool(bots_ver and bots_ver == cur_bots_ver)

        res: dict[str, Any] = {
            'ok': True,
            'online': True,
            'product_abbr': 'DSB',
            'activity': self.brain.activity,
            'enabled': self.store.config.enabled,
            'clients': len(clients_map),
            'phone_online': len(clients_map) > 0,
            'snooze_until': s['snooze_until'],
            'unread': len(src_pending),
            'routine_count': len(s.get('routines', [])),
            'small_ready': self._brain_ready_flags(has_key, has_model)[0],
            'big_ready': self._brain_ready_flags(has_key, has_model)[1],
            'last_error': s['last_error'],
            'stage': max([p['stage'] for p in src_pending.values()] or [0]),
            'execution': cur_exec,
            'stream': cur_stream,
            'executions_by_bot': slim_execs,
            'streams_by_bot': filtered_streams,
            'context_by_bot': self.brain.memory_engine.get_all_bots_context_stats(source=req_src),
            'auto_compact_tokens': self.store.config.auto_compact_tokens,
            'curiosity_interval_minutes': self.store.config.curiosity_interval_minutes,
            'last_completed_stream': last_done,
            'recent_tail': recent_tail,
            'epoch_reset': epoch_reset,
            'messages_epoch': cur_epoch,
            **({'messages': recent_tail} if epoch_reset else {}),
            'primary_bot_id': primary_bot_id_for_source(self.store, req_src),
            'boot_id': getattr(self.brain, 'boot_id', ''),
            'seq': self.brain.stream_seq,
            'state_seq': self.state_seq,
            'bots_ver': cur_bots_ver,
            'pins_ver': cur_pins_ver,
            'config_ver': cur_config_ver,
            'schedules_ver': cur_schedules_ver,
            'bots_unchanged': bots_unchanged,
            'computer': desktop,
            'dsh_online': dsh_on,
            'dsh_peers': dsh_peers,
            'dsh': {
                'online': dsh_on,
                'dsh_online': dsh_on,
                'peers': dsh_peers,
                'last_seen': self.brain.dsh_bridge_info.get('last_seen', 0.0),
                'status': dsh_st,
            },
            'time': time.time(),
        }
        if not bots_unchanged:
            res['bots'] = resolve_bots_for_source(s['assistants'], req_src)
        if include_pins or (pins_ver is not None and pins_ver != cur_pins_ver):
            res['pins'] = self.brain.memory_engine.list_pins()
            res['pins_unchanged'] = False
        elif pins_ver and pins_ver == cur_pins_ver:
            res['pins_unchanged'] = True
        if config_ver and config_ver == cur_config_ver:
            res['config_unchanged'] = True
        if schedules_ver and schedules_ver == cur_schedules_ver:
            res['schedules_unchanged'] = True
        return res

    async def build_sync_response(
        self,
        *,
        since_seq: int = 0,
        bot_id: str | None = None,
        source: str | None = None,
        after_id: str | None = None,
        messages_epoch: int | None = None,
        limit: int = 120,
        bots_ver: str | None = None,
        pins_ver: str | None = None,
        config_ver: str | None = None,
        schedules_ver: str | None = None,
    ) -> dict[str, Any]:
        self.store.sync_from_workspace()
        s = self.store.state
        req_src = normalize_node_source(source)
        cur_epoch = int(s.get('messages_epoch') or 1)
        cap = max(10, min(240, int(limit or 120)))
        desktop = await self.deps['fast_desktop_status']()
        has_key = bool(self.store.config.effective_api_key(big=False) or self.store.config.effective_api_key(big=True))
        has_model = bool(self.store.config.effective_model(big=False) or self.store.config.effective_model(big=True))
        raw_done = (
            self.brain.last_completed_stream
            if (self.brain.last_completed_stream and time.time() - float(self.brain.last_completed_stream.get('completed_at') or 0) < 15.0)
            else None
        )
        last_done = raw_done if (raw_done and normalize_node_source(raw_done.get('source')) == req_src) else None
        all_msgs = self._sort_messages(self.deps['filter_msgs_by_source'](s['messages'], req_src))
        if bot_id:
            all_msgs = [m for m in all_msgs if m.get('bot_id', 'companion') == bot_id]
        epoch_reset = bool(messages_epoch is not None and int(messages_epoch) != cur_epoch)
        is_delta = False
        if epoch_reset:
            delta_msgs = all_msgs[-cap:]
            is_delta = False
        elif after_id:
            idx = next((i for i in range(len(all_msgs) - 1, -1, -1) if all_msgs[i].get('id') == after_id), -1)
            if idx >= 0:
                delta_msgs = all_msgs[idx + 1:]
                is_delta = True
            else:
                delta_msgs = all_msgs[-cap:]
                is_delta = False
                epoch_reset = True
        elif since_seq > 0:
            delta_msgs = [
                m for m in all_msgs
                if (int(m.get('order_seq') or 0) if int(m.get('order_seq') or 0) > 0 else int(m.get('seq') or 0)) > since_seq
            ]
            is_delta = True
        else:
            delta_msgs = all_msgs[-cap:]
            is_delta = False
        slim_execs = self.deps['get_execs_for_source'](req_src)
        filtered_streams = self.deps['get_streams_for_source'](req_src)
        slim_exec_fn = self.deps['slim_exec']
        cur_exec = next(iter(slim_execs.values()), None) or (
            slim_exec_fn(self.brain.execution)
            if (isinstance(self.brain.execution, dict) and normalize_node_source(self.brain.execution.get('source')) == req_src)
            else None
        )
        cur_stream = next(iter(filtered_streams.values()), None) or (
            slim_exec_fn(self.brain.live_stream)
            if (isinstance(self.brain.live_stream, dict) and normalize_node_source(self.brain.live_stream.get('source')) == req_src)
            else None
        )
        dsh_on = self.deps['is_dsh_online']()
        dsh_peers = self.deps['slim_dsh_peers'](self.deps['get_dsh_peers']())
        dsh_st = self.deps['slim_dsh_status'](self.brain.dsh_bridge_info.get('status', {}))
        src_msg_ids = {m['id'] for m in all_msgs}
        src_pending = {k: v for k, v in s['pending'].items() if k in src_msg_ids}
        clients_map = self.deps['clients']
        computer = self.deps['computer']

        cur_bots_ver = self.bots_ver(req_src)
        cur_pins_ver = self.pins_ver()
        cur_config_ver = self.config_ver()
        cur_schedules_ver = self.schedules_ver()
        bots_unchanged = bool(bots_ver and bots_ver == cur_bots_ver)
        config_unchanged = bool(config_ver and config_ver == cur_config_ver)

        res: dict[str, Any] = {
            'ok': True,
            'online': True,
            'product_abbr': 'DSB',
            'activity': self.brain.activity,
            'enabled': self.store.config.enabled,
            'phone_online': len(clients_map) > 0,
            'snooze_until': s['snooze_until'],
            'pending': src_pending,
            'unread': len(src_pending),
            'routine_count': len(s.get('routines', [])),
            'stage': max([p['stage'] for p in src_pending.values()] or [0]),
            'small_ready': self._brain_ready_flags(has_key, has_model)[0],
            'big_ready': self._brain_ready_flags(has_key, has_model)[1],
            'last_error': s['last_error'],
            'execution': cur_exec,
            'stream': cur_stream,
            'executions_by_bot': slim_execs,
            'streams_by_bot': filtered_streams,
            'context_by_bot': self.brain.memory_engine.get_all_bots_context_stats(source=req_src),
            'auto_compact_tokens': self.store.config.auto_compact_tokens,
            'curiosity_interval_minutes': self.store.config.curiosity_interval_minutes,
            'onboarding_completed': bool(self.store.config.onboarding_completed),
            'cloud_desktop_configured': bool(computer.client.configured),
            'last_completed_stream': last_done,
            'delta': is_delta,
            'epoch_reset': epoch_reset,
            'messages_epoch': cur_epoch,
            'delta_messages': delta_msgs,
            'messages': delta_msgs,
            'recent_tail': all_msgs[-3:],
            'primary_bot_id': primary_bot_id_for_source(self.store, req_src),
            'boot_id': getattr(self.brain, 'boot_id', ''),
            'seq': self.brain.stream_seq,
            'state_seq': self.state_seq,
            'bots_ver': cur_bots_ver,
            'pins_ver': cur_pins_ver,
            'config_ver': cur_config_ver,
            'schedules_ver': cur_schedules_ver,
            'bots_unchanged': bots_unchanged,
            'config_unchanged': config_unchanged,
            'computer': desktop,
            'dsh_online': dsh_on,
            'dsh_peers': dsh_peers,
            'dsh': {
                'online': dsh_on,
                'dsh_online': dsh_on,
                'peers': dsh_peers,
                'last_seen': self.brain.dsh_bridge_info.get('last_seen', 0.0),
                'status': dsh_st,
            },
            'time': time.time(),
        }
        if not bots_unchanged:
            res['bots'] = resolve_bots_for_source(s['assistants'], req_src)
        if not config_unchanged:
            res['config'] = self.store.config.public()
        if pins_ver is not None:
            if pins_ver and pins_ver == cur_pins_ver:
                res['pins_unchanged'] = True
            else:
                res['pins'] = self.brain.memory_engine.list_pins()
                res['pins_unchanged'] = False
        if schedules_ver and schedules_ver == cur_schedules_ver:
            res['schedules_unchanged'] = True
        if since_seq > 0 and hasattr(self.store, 'sql'):
            res['events'] = self.store.sql.events_since(since_seq, limit=200)
        res['sync_versions'] = {
            'state_seq': int(self.state_seq),
            'pins_ver': cur_pins_ver,
            'bots_ver': cur_bots_ver,
            'config_ver': cur_config_ver,
            'schedules_ver': cur_schedules_ver,
            'messages_epoch': cur_epoch,
        }
        return res

    async def handle_ws_rpc(
        self,
        app_call: Callable[[str, str, dict | None, Any], Any],
        event: dict,
        client_src: str,
    ) -> dict[str, Any]:
        req_id = str(event.get('id') or '')
        method = str(event.get('method') or 'GET').upper()
        path = str(event.get('path') or '')
        if method not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE'):
            return {'type': 'rpc_res', 'id': req_id, 'status': 405, 'body': {'detail': f'不支持的 HTTP 方法: {method}'}}
        if not path.startswith('/api/') or path in ('/api/secure', '/api/transport') or '?' in path or '#' in path or '..' in path or '//' in path:
            return {'type': 'rpc_res', 'id': req_id, 'status': 400, 'body': {'detail': '非法 API 路径'}}
        raw_query = event.get('query')
        query = dict(raw_query) if isinstance(raw_query, dict) else {}
        query.setdefault('source', client_src)
        body = event.get('body')
        try:
            response = await app_call(method, path, query, body)
            try:
                resp_body = response.json()
            except Exception:
                resp_body = {'detail': response.text[:1000] if response.text else f'HTTP {response.status_code}'}
            return {'type': 'rpc_res', 'id': req_id, 'status': response.status_code, 'body': resp_body}
        except Exception as e:
            return {'type': 'rpc_res', 'id': req_id, 'status': 500, 'body': {'detail': str(e)[:300]}}
