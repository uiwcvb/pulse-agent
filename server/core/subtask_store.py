"""
[Module Responsibility]
Persistent Async Subtask Run Store & Auto-GC (`server/core/subtask_store.py`).

Backed by `subtask_runs` inside `context_index.db` using short-lived WAL connections
(zero lingering Windows file locks). Stores full generous outputs (30K chars) and
conversation steer bubbles so both the 1:1 chat-style secondary panel AND parent AI
`blackboard(action="peek")` read the exact same live state.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


def _safe_json_loads(raw: Any, default: Any) -> Any:
    if isinstance(raw, (list, dict)):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except Exception:
            return default
    return default


class SubtaskStore:
    """Thread-safe SQLite store for fire-and-forget async subtasks, steer bubbles & auto-GC."""

    def __init__(self, db_path: Path, *, conn: sqlite3.Connection | None = None, lock: threading.RLock | None = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = lock if lock is not None else threading.RLock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.db_path), timeout=10.0)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=NORMAL')
        if not self._initialized:
            self._init_schema_on(c)
            self._initialized = True
        return c

    def _init_schema_on(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS subtask_runs (
                task_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                preset TEXT NOT NULL DEFAULT '',
                goal TEXT NOT NULL DEFAULT '',
                parent_bot_id TEXT NOT NULL DEFAULT 'companion',
                parent_source TEXT NOT NULL DEFAULT 'phone',
                model TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'running',
                stage_idx INTEGER NOT NULL DEFAULT 0,
                stage_total INTEGER NOT NULL DEFAULT 0,
                stage_label TEXT NOT NULL DEFAULT '',
                sub_done INTEGER NOT NULL DEFAULT 0,
                sub_total INTEGER NOT NULL DEFAULT 0,
                progress_pct INTEGER NOT NULL DEFAULT 0,
                steps_executed INTEGER NOT NULL DEFAULT 0,
                max_steps INTEGER NOT NULL DEFAULT 200,
                recent_steps_json TEXT NOT NULL DEFAULT '[]',
                messages_json TEXT NOT NULL DEFAULT '[]',
                last_output TEXT NOT NULL DEFAULT '',
                live_thought TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                result_summary TEXT NOT NULL DEFAULT '',
                metrics_json TEXT NOT NULL DEFAULT '{}',
                checkpoint_json TEXT NOT NULL DEFAULT '{}',
                backup_id TEXT NOT NULL DEFAULT '',
                report_path TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                finished_at REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_sub_parent_status ON subtask_runs(parent_bot_id, status, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_sub_updated ON subtask_runs(updated_at DESC);
            """
        )
        existing = {r['name'] for r in conn.execute('PRAGMA table_info(subtask_runs)').fetchall()}
        if 'messages_json' not in existing:
            conn.execute("ALTER TABLE subtask_runs ADD COLUMN messages_json TEXT NOT NULL DEFAULT '[]'")
        conn.commit()

    def close(self) -> None:
        return

    def mark_interrupted_on_boot(self) -> int:
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """UPDATE subtask_runs
                       SET status='failed',
                           last_error=CASE WHEN last_error='' THEN '服务重启导致子任务中断，可点击继续或重派' ELSE last_error END,
                           updated_at=?,
                           finished_at=?
                       WHERE status='running'""",
                    (now, now),
                )
                conn.commit()
                return int(cur.rowcount or 0)
            finally:
                conn.close()

    def count_running(self, parent_bot_id: str) -> int:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM subtask_runs WHERE parent_bot_id=? AND status='running'",
                    (str(parent_bot_id or 'companion'),),
                ).fetchone()
                return int(row[0] if row else 0)
            finally:
                conn.close()

    def create_run(
        self,
        *,
        task_id: str,
        title: str,
        preset: str = '',
        goal: str = '',
        parent_bot_id: str = 'companion',
        parent_source: str = 'phone',
        model: str = '',
        stage_total: int = 0,
        max_steps: int = 200,
    ) -> dict[str, Any]:
        now = time.time()
        clean_title = str(title or '').strip()[:80] or '子任务'
        clean_goal = str(goal or '').strip()[:20000]
        init_msgs = [{'role': 'user', 'text': clean_goal or clean_title, 'ts': round(now, 3)}]
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT INTO subtask_runs(
                           task_id, title, preset, goal, parent_bot_id, parent_source, model,
                           status, stage_idx, stage_total, stage_label, sub_done, sub_total,
                           progress_pct, steps_executed, max_steps, recent_steps_json, messages_json,
                           last_output, live_thought, last_error, result_summary,
                           metrics_json, checkpoint_json, backup_id, report_path,
                           created_at, updated_at, finished_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', 0, ?, '', 0, 0, 0, 0, ?, '[]', ?, '', '', '', '', '{}', '{}', '', '', ?, ?, 0)""",
                    (
                        task_id,
                        clean_title,
                        str(preset or '').strip(),
                        clean_goal,
                        str(parent_bot_id or 'companion').strip(),
                        str(parent_source or 'phone').strip(),
                        str(model or '').strip()[:160],
                        int(stage_total or 0),
                        int(max_steps or 200),
                        json.dumps(init_msgs, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return self.get_run(task_id) or {}

    def append_message(self, task_id: str, role: str, text: str, *, max_keep: int = 80) -> dict[str, Any] | None:
        clean = str(text or '').strip()[:30000]
        if not clean:
            return self.get_run(task_id)
        now = time.time()
        entry = {'role': 'user' if role == 'user' else 'assistant', 'text': clean, 'ts': round(now, 3)}
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute('SELECT messages_json FROM subtask_runs WHERE task_id=?', (task_id,)).fetchone()
                if row is None:
                    return None
                msgs = _safe_json_loads(row['messages_json'], [])
                if not isinstance(msgs, list):
                    msgs = []
                msgs.append(entry)
                if len(msgs) > max_keep:
                    msgs = msgs[-max_keep:]
                conn.execute(
                    'UPDATE subtask_runs SET messages_json=?, updated_at=? WHERE task_id=?',
                    (json.dumps(msgs, ensure_ascii=False), now, task_id),
                )
                conn.commit()
            finally:
                conn.close()
        return self.get_run(task_id)

    def update_progress(
        self,
        task_id: str,
        *,
        status: str | None = None,
        stage_idx: int | None = None,
        stage_total: int | None = None,
        stage_label: str | None = None,
        sub_done: int | None = None,
        sub_total: int | None = None,
        progress_pct: int | None = None,
        steps_executed: int | None = None,
        last_output: str | None = None,
        live_thought: str | None = None,
        last_error: str | None = None,
        metrics: dict[str, Any] | None = None,
        checkpoint: dict[str, Any] | None = None,
        backup_id: str | None = None,
        report_path: str | None = None,
    ) -> dict[str, Any] | None:
        sets: list[str] = ['updated_at=?']
        vals: list[Any] = [time.time()]
        if status is not None:
            sets.append('status=?')
            vals.append(str(status))
        if stage_idx is not None:
            sets.append('stage_idx=?')
            vals.append(int(stage_idx))
        if stage_total is not None:
            sets.append('stage_total=?')
            vals.append(int(stage_total))
        if stage_label is not None:
            sets.append('stage_label=?')
            vals.append(str(stage_label)[:200])
        if sub_done is not None:
            sets.append('sub_done=?')
            vals.append(int(sub_done))
        if sub_total is not None:
            sets.append('sub_total=?')
            vals.append(int(sub_total))
        if progress_pct is not None:
            sets.append('progress_pct=?')
            vals.append(max(0, min(100, int(progress_pct))))
        if steps_executed is not None:
            sets.append('steps_executed=?')
            vals.append(int(steps_executed))
        if last_output is not None:
            sets.append('last_output=?')
            vals.append(str(last_output)[:30000])
        if live_thought is not None:
            sets.append('live_thought=?')
            vals.append(str(live_thought)[:8000])
        if last_error is not None:
            sets.append('last_error=?')
            vals.append(str(last_error)[:4000])
        if metrics is not None:
            sets.append('metrics_json=?')
            vals.append(json.dumps(metrics, ensure_ascii=False))
        if checkpoint is not None:
            sets.append('checkpoint_json=?')
            vals.append(json.dumps(checkpoint, ensure_ascii=False))
        if backup_id is not None:
            sets.append('backup_id=?')
            vals.append(str(backup_id)[:120])
        if report_path is not None:
            sets.append('report_path=?')
            vals.append(str(report_path)[:260])
        vals.append(task_id)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(f"UPDATE subtask_runs SET {', '.join(sets)} WHERE task_id=?", vals)
                conn.commit()
            finally:
                conn.close()
        return self.get_run(task_id)

    def append_step(
        self,
        task_id: str,
        *,
        tool: str,
        stage: str = '',
        args: str = '',
        ok: bool = True,
        result: str = '',
        error: str = '',
        max_keep: int = 200,
    ) -> dict[str, Any] | None:
        now = time.time()
        entry = {
            'ts': round(now, 3),
            'tool': str(tool or '')[:120],
            'stage': str(stage or '')[:160],
            'args': str(args or '')[:4000],
            'ok': bool(ok),
            'result': str(result or '')[:6000],
            'error': str(error or '')[:4000],
        }
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    'SELECT recent_steps_json, last_error FROM subtask_runs WHERE task_id=?',
                    (task_id,),
                ).fetchone()
                if row is None:
                    return None
                steps = _safe_json_loads(row['recent_steps_json'], [])
                if not isinstance(steps, list):
                    steps = []
                steps.append(entry)
                if len(steps) > max_keep:
                    steps = steps[-max_keep:]
                new_err = entry['error'] if entry['error'] else str(row['last_error'] or '')
                conn.execute(
                    'UPDATE subtask_runs SET recent_steps_json=?, last_error=?, updated_at=? WHERE task_id=?',
                    (json.dumps(steps, ensure_ascii=False), new_err[:4000], now, task_id),
                )
                conn.commit()
            finally:
                conn.close()
        return self.get_run(task_id)

    def finish_run(
        self,
        task_id: str,
        *,
        status: str,
        result_summary: str = '',
        last_error: str = '',
        metrics: dict[str, Any] | None = None,
        backup_id: str | None = None,
        report_path: str | None = None,
        progress_pct: int | None = None,
    ) -> dict[str, Any] | None:
        now = time.time()
        st = status if status in ('completed', 'failed', 'cancelled') else 'completed'
        pct = 100 if (st == 'completed' and progress_pct is None) else progress_pct
        full_sum = str(result_summary or '')[:30000]
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute('SELECT metrics_json, backup_id, report_path, progress_pct FROM subtask_runs WHERE task_id=?', (task_id,)).fetchone()
                if row is None:
                    return None
                cur_metrics = _safe_json_loads(row['metrics_json'], {})
                if isinstance(metrics, dict):
                    cur_metrics.update(metrics)
                conn.execute(
                    """UPDATE subtask_runs
                       SET status=?, result_summary=?, last_output=CASE WHEN ?!='' THEN ? ELSE last_output END,
                           last_error=?, metrics_json=?, backup_id=?, report_path=?,
                           progress_pct=?, updated_at=?, finished_at=?
                       WHERE task_id=?""",
                    (
                        st,
                        full_sum,
                        full_sum,
                        full_sum,
                        str(last_error or '')[:4000],
                        json.dumps(cur_metrics, ensure_ascii=False),
                        str(backup_id if backup_id is not None else row['backup_id'] or '')[:120],
                        str(report_path if report_path is not None else row['report_path'] or '')[:260],
                        int(pct if pct is not None else row['progress_pct'] or 0),
                        now,
                        now,
                        task_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return self.get_run(task_id)

    def get_raw_checkpoint(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute('SELECT checkpoint_json FROM subtask_runs WHERE task_id=?', (task_id,)).fetchone()
            finally:
                conn.close()
        if not row:
            return {}
        parsed = _safe_json_loads(row['checkpoint_json'], {})
        return parsed if isinstance(parsed, dict) else {}

    def get_run(self, task_id: str, *, recent_steps_limit: int = 200) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute('SELECT * FROM subtask_runs WHERE task_id=?', (str(task_id or '').strip(),)).fetchone()
            finally:
                conn.close()
        return self.serialize_run(row, recent_steps_limit=recent_steps_limit) if row is not None else None

    def list_runs(
        self,
        *,
        parent_bot_id: str | None = None,
        limit: int = 30,
        recent_steps_limit: int = 60,
    ) -> list[dict[str, Any]]:
        lim = max(1, min(60, int(limit or 30)))
        with self._lock:
            conn = self._connect()
            try:
                if parent_bot_id and parent_bot_id not in ('all', '*'):
                    rows = conn.execute(
                        """SELECT * FROM subtask_runs
                           WHERE parent_bot_id=?
                           ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END, updated_at DESC
                           LIMIT ?""",
                        (parent_bot_id, lim),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT * FROM subtask_runs
                           ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END, updated_at DESC
                           LIMIT ?""",
                        (lim,),
                    ).fetchall()
            finally:
                conn.close()
        return [self.serialize_run(r, recent_steps_limit=recent_steps_limit) for r in rows]

    def gc_runs(self, *, max_per_bot: int = 12, max_total: int = 28, max_age_sec: float = 5 * 86400) -> list[str]:
        """Recycle finished subtasks so they never stack unboundedly. Never touches running tasks."""
        now = time.time()
        removed: list[str] = []
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT task_id, parent_bot_id, finished_at, updated_at FROM subtask_runs WHERE status != 'running' ORDER BY updated_at DESC"
                ).fetchall()
                to_delete: set[str] = set()
                per_bot_counts: dict[str, int] = {}
                kept_total = 0
                for r in rows:
                    tid = str(r['task_id'])
                    bid = str(r['parent_bot_id'] or 'companion')
                    ts = float(r['finished_at'] or r['updated_at'] or 0)
                    if ts > 0 and (now - ts) > max_age_sec:
                        to_delete.add(tid)
                        continue
                    cnt = per_bot_counts.get(bid, 0)
                    if cnt >= max_per_bot or kept_total >= max_total:
                        to_delete.add(tid)
                        continue
                    per_bot_counts[bid] = cnt + 1
                    kept_total += 1
                if to_delete:
                    placeholders = ','.join('?' for _ in to_delete)
                    conn.execute(f'DELETE FROM subtask_runs WHERE task_id IN ({placeholders})', list(to_delete))
                    conn.commit()
                    removed = sorted(to_delete)
            finally:
                conn.close()
        return removed

    @staticmethod
    def serialize_run(row: sqlite3.Row, *, recent_steps_limit: int = 200) -> dict[str, Any]:
        steps = _safe_json_loads(row['recent_steps_json'], [])
        if not isinstance(steps, list):
            steps = []
        if recent_steps_limit > 0 and len(steps) > recent_steps_limit:
            steps = steps[-recent_steps_limit:]
        metrics = _safe_json_loads(row['metrics_json'], {})
        if not isinstance(metrics, dict):
            metrics = {}
        created_at = float(row['created_at'] or 0.0)
        finished_at = float(row['finished_at'] or 0.0)
        end_ts = finished_at if (row['status'] != 'running' and finished_at > 0) else time.time()
        elapsed = max(0.0, round(end_ts - created_at, 1)) if created_at > 0 else 0.0
        msgs = _safe_json_loads(row['messages_json'], []) if 'messages_json' in row.keys() else []
        if not isinstance(msgs, list) or not msgs:
            g = str(row['goal'] or row['title'] or '').strip()
            msgs = [{'role': 'user', 'text': g, 'ts': created_at}] if g else []
        return {
            'task_id': str(row['task_id']),
            'title': str(row['title']),
            'preset': str(row['preset'] or ''),
            'goal': str(row['goal'] or ''),
            'parent_bot_id': str(row['parent_bot_id'] or 'companion'),
            'parent_source': str(row['parent_source'] or 'phone'),
            'model': str(row['model'] or ''),
            'status': str(row['status'] or 'running'),
            'stage_idx': int(row['stage_idx'] or 0),
            'stage_total': int(row['stage_total'] or 0),
            'stage_label': str(row['stage_label'] or ''),
            'sub_done': int(row['sub_done'] or 0),
            'sub_total': int(row['sub_total'] or 0),
            'progress_pct': int(row['progress_pct'] or 0),
            'steps_executed': int(row['steps_executed'] or 0),
            'max_steps': int(row['max_steps'] or 200),
            'elapsed_sec': elapsed,
            'recent_steps': steps,
            'messages': msgs,
            'last_output': str(row['last_output'] or ''),
            'live_thought': str(row['live_thought'] or ''),
            'last_error': str(row['last_error'] or ''),
            'result_summary': str(row['result_summary'] or ''),
            'metrics': metrics,
            'backup_id': str(row['backup_id'] or ''),
            'report_path': str(row['report_path'] or ''),
            'created_at': created_at,
            'updated_at': float(row['updated_at'] or 0.0),
            'finished_at': finished_at,
        }
