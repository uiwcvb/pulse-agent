"""SQLite WAL persistence engine for state domains, ordered messages, and delta events."""
from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from .sqlite_curiosity_mixin import SQLiteCuriosityMixin


def _fast_msg_sig(msg: dict) -> tuple[Any, ...]:
    txt = str(msg.get('text') or '')
    tr = msg.get('trace') or []
    ch = msg.get('choices') or []
    ext_blob = json.dumps(
        {
            'llm_text': msg.get('llm_text') or '',
            'turn_steps': msg.get('turn_steps') or [],
            'timeline': msg.get('timeline') or [],
            'attachments': msg.get('attachments') or [],
            'stickers': msg.get('stickers') or [],
            'variants': msg.get('variants') or [],
            'active_variant_idx': msg.get('active_variant_idx'),
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return (
        int(msg.get('order_seq') or msg.get('seq') or 0),
        hashlib.sha1(txt.encode('utf-8', 'ignore')).hexdigest()[:12],
        hashlib.sha1(ext_blob.encode('utf-8', 'ignore')).hexdigest()[:12],
        len(tr),
        len(ch),
        str(msg.get('decision') or ''),
        str(msg.get('thought') or '')[:32],
        float(msg.get('created_at') or 0.0),
        str(msg.get('turn_id') or ''),
        str(msg.get('client_msg_id') or ''),
        bool(msg.get('stopped_early')),
        str(msg.get('sticker') or ''),
        str(msg.get('edited_at') or ''),
    )


class SQLiteStateBackend(SQLiteCuriosityMixin):
    """Thread-safe SQLite WAL backend replacing full-blob state.json rewrites."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._wal_initialized = False
        self._last_msg_sigs: dict[str, tuple[Any, ...]] = {}
        self._last_domain_json: dict[str, str] = {}
        self._init_schema()

    @contextlib.contextmanager
    def _conn_scope(self) -> Iterator[sqlite3.Connection]:
        """Open a scoped connection and close it on exit to release Windows WAL file locks."""
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            check_same_thread=False,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA busy_timeout=5000;')
            if not self._wal_initialized:
                conn.execute('PRAGMA journal_mode=WAL;')
                self._wal_initialized = True
            conn.execute('PRAGMA synchronous=NORMAL;')
            conn.execute('PRAGMA temp_store=MEMORY;')
            yield conn
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    def close(self) -> None:
        with self._lock:
            if not self.db_path.exists():
                return
            with self._conn_scope() as conn:
                with contextlib.suppress(Exception):
                    conn.execute('PRAGMA wal_checkpoint(TRUNCATE);')

    def _init_schema(self) -> None:
        with self._lock, self._conn_scope() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    val TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    order_seq INTEGER NOT NULL,
                    bot_id TEXT NOT NULL DEFAULT 'companion',
                    source TEXT NOT NULL DEFAULT 'phone',
                    role TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'chat',
                    created_at REAL NOT NULL DEFAULT 0.0,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_order_seq ON messages(order_seq ASC, created_at ASC);
                CREATE INDEX IF NOT EXISTS idx_messages_bot_source_seq ON messages(bot_id, source, order_seq ASC);
                CREATE TABLE IF NOT EXISTS events_log (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    ver TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_log_seq ON events_log(seq ASC);
                CREATE TABLE IF NOT EXISTS kv_domains (
                    domain TEXT PRIMARY KEY,
                    updated_at REAL NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS curiosity_topics (
                    topic_id TEXT PRIMARY KEY,
                    category TEXT,
                    label TEXT,
                    depth INTEGER DEFAULT 0,
                    known_summary TEXT,
                    open_question TEXT,
                    status TEXT DEFAULT 'open',
                    priority REAL DEFAULT 0.5,
                    ask_count INTEGER DEFAULT 0,
                    last_asked_at REAL DEFAULT 0,
                    cooldown_until REAL DEFAULT 0,
                    updated_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_curiosity_topics_pick
                    ON curiosity_topics(status, depth, cooldown_until, priority DESC);
                CREATE TABLE IF NOT EXISTS proactive_followups (
                    followup_id TEXT PRIMARY KEY,
                    bot_id TEXT,
                    source TEXT,
                    topic TEXT,
                    context TEXT,
                    due_at REAL,
                    status TEXT DEFAULT 'open',
                    attempts INTEGER DEFAULT 0,
                    created_at REAL,
                    resolved_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_proactive_followups_due
                    ON proactive_followups(status, due_at ASC);
                CREATE TABLE IF NOT EXISTS curiosity_episodes (
                    episode_id TEXT PRIMARY KEY,
                    intent TEXT,
                    topic_id TEXT,
                    bot_id TEXT,
                    message_text TEXT,
                    tool_calls_count INTEGER DEFAULT 0,
                    outcome TEXT,
                    user_replied INTEGER DEFAULT 0,
                    created_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_curiosity_episodes_recent
                    ON curiosity_episodes(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_curiosity_episodes_intent_ts
                    ON curiosity_episodes(intent, outcome, created_at DESC);
                """
            )

    def _get_meta(self, conn: sqlite3.Connection, key: str, default: str = '') -> str:
        row = conn.execute('SELECT val FROM meta WHERE key = ?', (key,)).fetchone()
        return str(row['val']) if row else default

    def _set_meta(self, conn: sqlite3.Connection, key: str, val: str) -> None:
        conn.execute('INSERT OR REPLACE INTO meta(key, val) VALUES(?, ?)', (key, str(val)))

    def load_or_migrate(
        self,
        legacy_json_path: Path,
        default_state_factory: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        with self._lock, self._conn_scope() as conn:
            if self._get_meta(conn, 'initialized') == '1':
                return self._load_from_sqlite(conn, default_state_factory)

            state = default_state_factory()
            migrated = False
            if legacy_json_path.exists():
                bak_path = legacy_json_path.with_name(f'{legacy_json_path.name}.v3.bak')
                if not bak_path.exists():
                    with contextlib.suppress(Exception):
                        shutil.copy2(legacy_json_path, bak_path)
                with contextlib.suppress(Exception):
                    raw = json.loads(legacy_json_path.read_text('utf-8'))
                    if isinstance(raw, dict):
                        state.update(raw)
                        migrated = True

            raw_msgs = state.get('messages')
            msgs: list[dict[str, Any]] = [m for m in raw_msgs if isinstance(m, dict)] if isinstance(raw_msgs, list) else []
            indexed = list(enumerate(msgs))
            indexed.sort(key=lambda pair: (float(pair[1].get('created_at') or 0.0), int(pair[1].get('order_seq') or 0), pair[0]))
            next_seq = 1
            ordered_msgs: list[dict[str, Any]] = []
            for _, m in indexed:
                m_copy = dict(m)
                cur_seq = int(m_copy.get('order_seq') or m_copy.get('seq') or 0)
                assigned = cur_seq if cur_seq >= next_seq else next_seq
                m_copy['order_seq'] = assigned
                m_copy['seq'] = assigned
                next_seq = assigned + 1
                if not m_copy.get('id'):
                    m_copy['id'] = f'migrated-{assigned}'
                ordered_msgs.append(m_copy)
            state['messages'] = ordered_msgs

            now_ts = time.time()
            conn.execute('BEGIN IMMEDIATE;')
            try:
                for m in ordered_msgs:
                    pjson = json.dumps(m, ensure_ascii=False, separators=(',', ':'))
                    conn.execute(
                        'INSERT OR REPLACE INTO messages(id, order_seq, bot_id, source, role, kind, created_at, payload_json) '
                        'VALUES(?, ?, ?, ?, ?, ?, ?, ?)',
                        (
                            str(m['id']),
                            int(m['order_seq']),
                            str(m.get('bot_id') or 'companion'),
                            str(m.get('source') or 'phone'),
                            str(m.get('role') or ''),
                            str(m.get('kind') or 'chat'),
                            float(m.get('created_at') or 0.0),
                            pjson,
                        ),
                    )
                    self._last_msg_sigs[str(m['id'])] = _fast_msg_sig(m)

                for dom, val in state.items():
                    if dom == 'messages':
                        continue
                    djson = json.dumps(val, ensure_ascii=False, separators=(',', ':'))
                    conn.execute(
                        'INSERT OR REPLACE INTO kv_domains(domain, updated_at, payload_json) VALUES(?, ?, ?)',
                        (str(dom), now_ts, djson),
                    )
                    self._last_domain_json[str(dom)] = djson

                self._set_meta(conn, 'schema_version', '4.0')
                self._set_meta(conn, 'next_order_seq', str(max(next_seq, 1)))
                self._set_meta(conn, 'migrated_from_json', '1' if migrated else '0')
                self._set_meta(conn, 'migrated_at', str(now_ts))
                self._set_meta(conn, 'initialized', '1')
                conn.execute('COMMIT;')
            except Exception:
                conn.execute('ROLLBACK;')
                raise
            return state

    def _load_from_sqlite(
        self,
        conn: sqlite3.Connection,
        default_state_factory: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        state = default_state_factory()
        self._last_msg_sigs.clear()
        self._last_domain_json.clear()

        for row in conn.execute('SELECT domain, payload_json FROM kv_domains').fetchall():
            dom = str(row['domain'])
            djson = str(row['payload_json'])
            self._last_domain_json[dom] = djson
            with contextlib.suppress(Exception):
                state[dom] = json.loads(djson)

        msg_rows = conn.execute('SELECT id, order_seq, payload_json FROM messages ORDER BY order_seq ASC, created_at ASC').fetchall()
        msgs: list[dict[str, Any]] = []
        max_seq = 0
        for row in msg_rows:
            try:
                m = json.loads(str(row['payload_json']))
            except Exception:
                continue
            if not isinstance(m, dict):
                continue
            oseq = int(row['order_seq'] or m.get('order_seq') or 0)
            m['order_seq'] = oseq
            m['seq'] = oseq
            max_seq = max(max_seq, oseq)
            msgs.append(m)
            self._last_msg_sigs[str(m.get('id') or row['id'])] = _fast_msg_sig(m)

        cur_next = int(self._get_meta(conn, 'next_order_seq', '1') or 1)
        if max_seq >= cur_next:
            self._set_meta(conn, 'next_order_seq', str(max_seq + 1))

        state['messages'] = msgs
        return state

    def alloc_order_seq(self) -> int:
        with self._lock, self._conn_scope() as conn:
            conn.execute('BEGIN IMMEDIATE;')
            try:
                cur = int(self._get_meta(conn, 'next_order_seq', '1') or 1)
                row = conn.execute('SELECT COALESCE(MAX(order_seq), 0) AS mx FROM messages').fetchone()
                mx = int(row['mx']) if row else 0
                assigned = max(cur, mx + 1)
                self._set_meta(conn, 'next_order_seq', str(assigned + 1))
                conn.execute('COMMIT;')
                return assigned
            except Exception:
                conn.execute('ROLLBACK;')
                raise

    def upsert_message(self, msg: dict[str, Any]) -> None:
        if not isinstance(msg, dict) or not msg.get('id'):
            return
        mid = str(msg['id'])
        oseq = int(msg.get('order_seq') or 0)
        if oseq > 0 and int(msg.get('seq') or 0) != oseq:
            msg['seq'] = oseq
        pjson = json.dumps(msg, ensure_ascii=False, separators=(',', ':'))
        with self._lock, self._conn_scope() as conn:
            conn.execute('BEGIN IMMEDIATE;')
            try:
                if oseq <= 0:
                    cur = int(self._get_meta(conn, 'next_order_seq', '1') or 1)
                    oseq = cur
                    msg['order_seq'] = oseq
                    msg['seq'] = oseq
                    self._set_meta(conn, 'next_order_seq', str(oseq + 1))
                    pjson = json.dumps(msg, ensure_ascii=False, separators=(',', ':'))
                conn.execute(
                    'INSERT OR REPLACE INTO messages(id, order_seq, bot_id, source, role, kind, created_at, payload_json) '
                    'VALUES(?, ?, ?, ?, ?, ?, ?, ?)',
                    (
                        mid,
                        oseq,
                        str(msg.get('bot_id') or 'companion'),
                        str(msg.get('source') or 'phone'),
                        str(msg.get('role') or ''),
                        str(msg.get('kind') or 'chat'),
                        float(msg.get('created_at') or 0.0),
                        pjson,
                    ),
                )
                conn.execute('COMMIT;')
            except Exception:
                if conn.in_transaction:
                    conn.execute('ROLLBACK;')
                raise
        self._last_msg_sigs[mid] = _fast_msg_sig(msg)

    def sync_state(self, state: dict[str, Any]) -> dict[str, int]:
        """Incrementally persist changed kv_domains and diffed messages in a single WAL transaction."""
        now_ts = time.time()
        stats = {'messages_written': 0, 'messages_deleted': 0, 'domains_written': 0}
        with self._lock, self._conn_scope() as conn:
            raw_msgs = state.get('messages')
            msgs: list[dict[str, Any]] = [m for m in raw_msgs if isinstance(m, dict) and m.get('id')] if isinstance(raw_msgs, list) else []

            current_ids: set[str] = set()
            dirty_msgs: list[dict[str, Any]] = []
            max_seen_seq = int(self._get_meta(conn, 'next_order_seq', '1') or 1) - 1

            for m in msgs:
                mid = str(m['id'])
                current_ids.add(mid)
                oseq = int(m.get('order_seq') or 0)
                if oseq <= 0:
                    oseq = max_seen_seq + 1
                    m['order_seq'] = oseq
                if int(m.get('seq') or 0) != oseq:
                    m['seq'] = oseq
                max_seen_seq = max(max_seen_seq, oseq)
                sig = _fast_msg_sig(m)
                if self._last_msg_sigs.get(mid) != sig:
                    dirty_msgs.append(m)

            deleted_ids = [old_id for old_id in list(self._last_msg_sigs.keys()) if old_id not in current_ids]

            dirty_domains: list[tuple[str, str]] = []
            for dom, val in state.items():
                if dom == 'messages':
                    continue
                djson = json.dumps(val, ensure_ascii=False, separators=(',', ':'))
                if self._last_domain_json.get(dom) != djson:
                    dirty_domains.append((dom, djson))

            deleted_domains = [d for d in list(self._last_domain_json.keys()) if d not in state]

            if not dirty_msgs and not deleted_ids and not dirty_domains and not deleted_domains:
                return stats

            conn.execute('BEGIN IMMEDIATE;')
            try:
                for did in deleted_ids:
                    conn.execute('DELETE FROM messages WHERE id = ?', (did,))
                    self._last_msg_sigs.pop(did, None)
                    stats['messages_deleted'] += 1

                for m in dirty_msgs:
                    mid = str(m['id'])
                    pjson = json.dumps(m, ensure_ascii=False, separators=(',', ':'))
                    conn.execute(
                        'INSERT OR REPLACE INTO messages(id, order_seq, bot_id, source, role, kind, created_at, payload_json) '
                        'VALUES(?, ?, ?, ?, ?, ?, ?, ?)',
                        (
                            mid,
                            int(m['order_seq']),
                            str(m.get('bot_id') or 'companion'),
                            str(m.get('source') or 'phone'),
                            str(m.get('role') or ''),
                            str(m.get('kind') or 'chat'),
                            float(m.get('created_at') or 0.0),
                            pjson,
                        ),
                    )
                    self._last_msg_sigs[mid] = _fast_msg_sig(m)
                    stats['messages_written'] += 1

                cur_next = int(self._get_meta(conn, 'next_order_seq', '1') or 1)
                if max_seen_seq >= cur_next:
                    self._set_meta(conn, 'next_order_seq', str(max_seen_seq + 1))

                for dom in deleted_domains:
                    conn.execute('DELETE FROM kv_domains WHERE domain = ?', (dom,))
                    self._last_domain_json.pop(dom, None)

                for dom, djson in dirty_domains:
                    conn.execute(
                        'INSERT OR REPLACE INTO kv_domains(domain, updated_at, payload_json) VALUES(?, ?, ?)',
                        (dom, now_ts, djson),
                    )
                    self._last_domain_json[dom] = djson
                    stats['domains_written'] += 1

                conn.execute('COMMIT;')
            except Exception:
                conn.execute('ROLLBACK;')
                raise
        return stats

    def append_event(
        self,
        kind: str,
        source: str = '',
        ver: str = '',
        payload: dict[str, Any] | None = None,
    ) -> int:
        now_ts = time.time()
        pjson = json.dumps(payload or {}, ensure_ascii=False, separators=(',', ':'), default=str)
        with self._lock, self._conn_scope() as conn:
            conn.execute('BEGIN IMMEDIATE;')
            try:
                cur = conn.execute(
                    'INSERT INTO events_log(kind, source, ver, payload_json, created_at) VALUES(?, ?, ?, ?, ?)',
                    (str(kind), str(source or ''), str(ver or ''), pjson, now_ts),
                )
                seq = int(cur.lastrowid or 0)
                if seq % 200 == 0:
                    conn.execute('DELETE FROM events_log WHERE seq <= ?', (max(0, seq - 2000),))
                conn.execute('COMMIT;')
                return seq
            except Exception:
                conn.execute('ROLLBACK;')
                raise

    def events_since(self, since_seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        cap = max(1, min(500, int(limit or 200)))
        with self._lock, self._conn_scope() as conn:
            rows = conn.execute(
                'SELECT seq, kind, source, ver, payload_json, created_at FROM events_log WHERE seq > ? ORDER BY seq ASC LIMIT ?',
                (int(since_seq or 0), cap),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                payload = json.loads(str(r['payload_json']))
            except Exception:
                payload = {}
            out.append({
                'seq': int(r['seq']),
                'kind': str(r['kind']),
                'source': str(r['source']),
                'ver': str(r['ver']),
                'payload': payload,
                'created_at': float(r['created_at']),
            })
        return out

    def latest_event_seq(self) -> int:
        with self._lock, self._conn_scope() as conn:
            row = conn.execute('SELECT COALESCE(MAX(seq), 0) AS mx FROM events_log').fetchone()
            return int(row['mx']) if row else 0

    def checkpoint_wal(self, mode: str = 'PASSIVE') -> tuple[int, int, int] | None:
        """Run PRAGMA wal_checkpoint(mode) safely under the SQLite lock."""
        clean_mode = str(mode or 'PASSIVE').strip().upper()
        if clean_mode not in ('PASSIVE', 'FULL', 'RESTART', 'TRUNCATE'):
            clean_mode = 'PASSIVE'
        with self._lock:
            if not self.db_path.exists():
                return None
            with self._conn_scope() as conn:
                row = conn.execute(f'PRAGMA wal_checkpoint({clean_mode});').fetchone()
                if row is not None and len(row) >= 3:
                    return (int(row[0]), int(row[1]), int(row[2]))
                return None

    def stats(self) -> dict[str, Any]:
        with self._lock, self._conn_scope() as conn:
            msg_cnt = int((conn.execute('SELECT COUNT(*) AS c FROM messages').fetchone() or {'c': 0})['c'])
            ev_cnt = int((conn.execute('SELECT COUNT(*) AS c FROM events_log').fetchone() or {'c': 0})['c'])
            next_seq = int(self._get_meta(conn, 'next_order_seq', '1') or 1)
            wal_p = Path(f'{self.db_path}-wal')
            out = {
                'backend': 'sqlite-wal',
                'db_path': str(self.db_path),
                'db_bytes': self.db_path.stat().st_size if self.db_path.exists() else 0,
                'wal_bytes': wal_p.stat().st_size if wal_p.exists() else 0,
                'message_count': msg_cnt,
                'event_count': ev_cnt,
                'next_order_seq': next_seq,
            }
            out.update(self.curiosity_stats())
            return out
