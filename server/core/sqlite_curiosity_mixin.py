"""Relational curiosity, follow-up, and episode CRUD mixin for SQLiteStateBackend."""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from typing import Any


_WAIT_MARKER_RE = re.compile(r"\(\s*待了解\s*\)|（\s*待了解\s*）")
_SECTION_RE = re.compile(r"^##\s*(.+?)\s*$")
_ITEM_RE = re.compile(r"^\s*[-*]\s*(?:\*\*)?([^:：\n*]+?)(?:\*\*)?\s*[:：]\s*(.*)$")


def _stable_topic_id(category: str, label: str) -> str:
    raw = f"{category.strip().lower()}::{label.strip().lower()}".encode("utf-8")
    return "topic-" + hashlib.sha1(raw).hexdigest()[:12]


class SQLiteCuriosityMixin:
    """SQLite persistence helpers for curiosity topics, followups, and episodes."""

    @staticmethod
    def _row_to_topic(r: Any) -> dict[str, Any]:
        return {
            "topic_id": r[0], "category": r[1], "label": r[2], "depth": int(r[3] or 0),
            "known_summary": r[4] or "", "open_question": r[5] or "", "status": r[6] or "open",
            "priority": float(r[7] if r[7] is not None else 0.5), "ask_count": int(r[8] or 0),
            "last_asked_at": float(r[9] or 0.0), "cooldown_until": float(r[10] or 0.0), "updated_at": float(r[11] or 0.0),
        }

    # ------------------------------------------------------------------
    # curiosity_topics
    # ------------------------------------------------------------------
    def upsert_curiosity_topic(
        self,
        topic_id: str | None = None,
        *,
        category: str = "general",
        label: str = "",
        depth: int | None = None,
        known_summary: str | None = None,
        open_question: str | None = None,
        status: str | None = None,
        priority: float | None = None,
        ask_count: int | None = None,
        last_asked_at: float | None = None,
        cooldown_until: float | None = None,
        increment_ask: bool = False,
        now_ts: float | None = None,
    ) -> str:
        now = float(now_ts if now_ts is not None else time.time())
        clean_cat = (category or "general").strip()[:80]
        clean_label = (label or topic_id or "untitled").strip()[:160]
        tid = (topic_id or "").strip() or _stable_topic_id(clean_cat, clean_label)

        with self._lock, self._conn_scope() as conn:
            cur = conn.execute(
                "SELECT depth, known_summary, open_question, status, priority, ask_count, last_asked_at, cooldown_until "
                "FROM curiosity_topics WHERE topic_id = ?",
                (tid,),
            )
            row = cur.fetchone()
            if row is None:
                d = max(0, min(3, int(depth if depth is not None else 0)))
                ks = (known_summary or "").strip()
                oq = (open_question or "").strip()
                st = (status or "open").strip() or "open"
                pr = float(priority if priority is not None else 0.5)
                ac = int(ask_count if ask_count is not None else (1 if increment_ask else 0))
                la = float(last_asked_at if last_asked_at is not None else (now if increment_ask else 0.0))
                cu = float(cooldown_until if cooldown_until is not None else 0.0)
                conn.execute(
                    """
                    INSERT INTO curiosity_topics(
                        topic_id, category, label, depth, known_summary, open_question,
                        status, priority, ask_count, last_asked_at, cooldown_until, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (tid, clean_cat, clean_label, d, ks, oq, st, pr, ac, la, cu, now),
                )
            else:
                d = max(0, min(3, int(depth if depth is not None else row[0])))
                ks = (known_summary if known_summary is not None else (row[1] or "")).strip()
                oq = (open_question if open_question is not None else (row[2] or "")).strip()
                st = (status if status is not None else (row[3] or "open")).strip() or "open"
                pr = float(priority if priority is not None else (row[4] if row[4] is not None else 0.5))
                ac = int(ask_count if ask_count is not None else int(row[5] or 0) + (1 if increment_ask else 0))
                la = float(last_asked_at if last_asked_at is not None else (now if increment_ask else float(row[6] or 0.0)))
                cu = float(cooldown_until if cooldown_until is not None else float(row[7] or 0.0))
                conn.execute(
                    """
                    UPDATE curiosity_topics
                    SET category = ?, label = ?, depth = ?, known_summary = ?, open_question = ?,
                        status = ?, priority = ?, ask_count = ?, last_asked_at = ?, cooldown_until = ?, updated_at = ?
                    WHERE topic_id = ?
                    """,
                    (clean_cat, clean_label, d, ks, oq, st, pr, ac, la, cu, now, tid),
                )
        return tid

    def list_curiosity_topics(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        lim = max(1, int(limit))
        sql = (
            "SELECT topic_id, category, label, depth, known_summary, open_question, "
            "status, priority, ask_count, last_asked_at, cooldown_until, updated_at "
            "FROM curiosity_topics "
        )
        params: list[Any] = []
        if status:
            sql += "WHERE status = ? "
            params.append(status)
        sql += "ORDER BY priority DESC, depth ASC, updated_at DESC LIMIT ?"
        params.append(lim)
        with self._lock, self._conn_scope() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_topic(r) for r in rows]

    def pick_candidate_curiosity_topics(self, *, now_ts: float | None = None, limit: int = 3, max_depth: int = 3) -> list[dict[str, Any]]:
        now = float(now_ts if now_ts is not None else time.time())
        lim = max(1, int(limit))
        with self._lock, self._conn_scope() as conn:
            rows = conn.execute(
                """
                SELECT topic_id, category, label, depth, known_summary, open_question,
                       status, priority, ask_count, last_asked_at, cooldown_until, updated_at
                FROM curiosity_topics
                WHERE status IN ('open', 'active') AND depth < ? AND (cooldown_until IS NULL OR cooldown_until <= ?)
                ORDER BY priority DESC, depth ASC, last_asked_at ASC, ask_count ASC
                LIMIT ?
                """,
                (int(max_depth), now, lim),
            ).fetchall()
        return [self._row_to_topic(r) for r in rows]

    def seed_topics_from_impression_markdown(
        self,
        markdown_text: str,
        *,
        now_ts: float | None = None,
    ) -> int:
        """Scan impression markdown for `(待了解)` slots and seed missing open topics."""
        if not markdown_text:
            return 0
        now = float(now_ts if now_ts is not None else time.time())
        current_section = "profile"
        seeded = 0

        for line in markdown_text.splitlines():
            sec_m = _SECTION_RE.match(line)
            if sec_m:
                current_section = sec_m.group(1).strip()[:80]
                continue
            if not _WAIT_MARKER_RE.search(line):
                continue
            item_m = _ITEM_RE.match(line)
            if item_m:
                label = item_m.group(1).strip()
                val = _WAIT_MARKER_RE.sub("", item_m.group(2)).strip(" ：:;-—")
            else:
                cleaned = _WAIT_MARKER_RE.sub("", line).lstrip("-* ").strip()
                if not cleaned:
                    continue
                label = cleaned[:80]
                val = ""
            tid = _stable_topic_id(current_section, label)
            with self._lock, self._conn_scope() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM curiosity_topics WHERE topic_id = ?",
                    (tid,),
                ).fetchone()
                if exists is None:
                    open_q = f"关于用户的「{label}」，目前还缺少具体细节，适合找个轻松时机自然了解。"
                    conn.execute(
                        """
                        INSERT INTO curiosity_topics(
                            topic_id, category, label, depth, known_summary, open_question,
                            status, priority, ask_count, last_asked_at, cooldown_until, updated_at
                        ) VALUES (?, ?, ?, 0, ?, ?, 'open', 0.55, 0, 0.0, 0.0, ?)
                        """,
                        (tid, current_section, label, val[:300], open_q, now),
                    )
                    seeded += 1
        return seeded

    # ------------------------------------------------------------------
    # proactive_followups
    # ------------------------------------------------------------------
    def add_proactive_followup(
        self,
        *,
        followup_id: str | None = None,
        bot_id: str = "companion",
        source: str = "phone",
        topic: str = "",
        context: str = "",
        due_at: float | None = None,
        now_ts: float | None = None,
    ) -> str:
        now = float(now_ts if now_ts is not None else time.time())
        fid = (followup_id or "").strip() or f"fu-{uuid.uuid4().hex[:12]}"
        due = float(due_at if due_at is not None else now)
        with self._lock, self._conn_scope() as conn:
            conn.execute(
                """
                INSERT INTO proactive_followups(
                    followup_id, bot_id, source, topic, context, due_at,
                    status, attempts, created_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'open', 0, ?, 0.0)
                ON CONFLICT(followup_id) DO UPDATE SET
                    bot_id = excluded.bot_id,
                    source = excluded.source,
                    topic = excluded.topic,
                    context = excluded.context,
                    due_at = excluded.due_at
                """,
                (fid, bot_id or "companion", source or "phone", topic.strip()[:200], context.strip()[:1000], due, now),
            )
        return fid

    def list_due_followups(
        self,
        *,
        now_ts: float | None = None,
        bot_id: str | None = None,
        limit: int = 10,
        include_future: bool = False,
    ) -> list[dict[str, Any]]:
        now = float(now_ts if now_ts is not None else time.time())
        lim = max(1, int(limit))
        clauses = ["status = 'open'"]
        params: list[Any] = []
        if not include_future:
            clauses.append("due_at <= ?")
            params.append(now)
        if bot_id:
            clauses.append("bot_id = ?")
            params.append(bot_id)
        sql = (
            "SELECT followup_id, bot_id, source, topic, context, due_at, status, attempts, created_at, resolved_at "
            "FROM proactive_followups WHERE " + " AND ".join(clauses) + " ORDER BY due_at ASC, created_at ASC LIMIT ?"
        )
        params.append(lim)
        with self._lock, self._conn_scope() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "followup_id": r[0],
                "bot_id": r[1] or "companion",
                "source": r[2] or "phone",
                "topic": r[3] or "",
                "context": r[4] or "",
                "due_at": float(r[5] or 0.0),
                "status": r[6] or "open",
                "attempts": int(r[7] or 0),
                "created_at": float(r[8] or 0.0),
                "resolved_at": float(r[9] or 0.0),
            }
            for r in rows
        ]

    def resolve_followup(
        self,
        followup_id: str,
        *,
        status: str = "resolved",
        increment_attempts: bool = False,
        next_due_at: float | None = None,
        now_ts: float | None = None,
    ) -> bool:
        now = float(now_ts if now_ts is not None else time.time())
        with self._lock, self._conn_scope() as conn:
            row = conn.execute(
                "SELECT attempts, due_at FROM proactive_followups WHERE followup_id = ?",
                (followup_id,),
            ).fetchone()
            if row is None:
                return False
            attempts = int(row[0] or 0) + (1 if increment_attempts else 0)
            due = float(next_due_at if next_due_at is not None else (row[1] or now))
            resolved_at = now if status in ("resolved", "done", "cancelled", "expired") else 0.0
            conn.execute(
                """
                UPDATE proactive_followups
                SET status = ?, attempts = ?, due_at = ?, resolved_at = ?
                WHERE followup_id = ?
                """,
                (status, attempts, due, resolved_at, followup_id),
            )
        return True

    # ------------------------------------------------------------------
    # curiosity_episodes
    # ------------------------------------------------------------------
    def record_curiosity_episode(
        self,
        *,
        episode_id: str | None = None,
        intent: str,
        topic_id: str = "",
        bot_id: str = "companion",
        message_text: str = "",
        tool_calls_count: int = 0,
        outcome: str = "sent",
        user_replied: int | bool = 0,
        created_at: float | None = None,
    ) -> str:
        ts = float(created_at if created_at is not None else time.time())
        eid = (episode_id or "").strip() or f"ep-{uuid.uuid4().hex[:12]}"
        with self._lock, self._conn_scope() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO curiosity_episodes(
                    episode_id, intent, topic_id, bot_id, message_text,
                    tool_calls_count, outcome, user_replied, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    eid,
                    (intent or "silent_noop").strip()[:64],
                    (topic_id or "").strip()[:128],
                    (bot_id or "companion").strip()[:64],
                    (message_text or "")[:1000],
                    int(tool_calls_count or 0),
                    (outcome or "sent").strip()[:64],
                    1 if user_replied else 0,
                    ts,
                ),
            )
        return eid

    def list_recent_curiosity_episodes(
        self,
        *,
        limit: int = 10,
        since_ts: float | None = None,
    ) -> list[dict[str, Any]]:
        lim = max(1, int(limit))
        params: list[Any] = []
        where = ""
        if since_ts is not None:
            where = "WHERE created_at >= ? "
            params.append(float(since_ts))
        params.append(lim)
        with self._lock, self._conn_scope() as conn:
            rows = conn.execute(
                f"""
                SELECT episode_id, intent, topic_id, bot_id, message_text,
                       tool_calls_count, outcome, user_replied, created_at
                FROM curiosity_episodes
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            {
                "episode_id": r[0],
                "intent": r[1] or "",
                "topic_id": r[2] or "",
                "bot_id": r[3] or "companion",
                "message_text": r[4] or "",
                "tool_calls_count": int(r[5] or 0),
                "outcome": r[6] or "",
                "user_replied": bool(r[7]),
                "created_at": float(r[8] or 0.0),
            }
            for r in rows
        ]

    def mark_latest_episode_user_replied(
        self,
        *,
        bot_id: str | None = None,
        since_ts: float | None = None,
    ) -> str | None:
        clauses = ["outcome = 'sent'", "user_replied = 0"]
        params: list[Any] = []
        if bot_id:
            clauses.append("bot_id = ?")
            params.append(bot_id)
        if since_ts is not None:
            clauses.append("created_at >= ?")
            params.append(float(since_ts))
        with self._lock, self._conn_scope() as conn:
            row = conn.execute(
                "SELECT episode_id FROM curiosity_episodes WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                return None
            eid = str(row[0])
            conn.execute(
                "UPDATE curiosity_episodes SET user_replied = 1 WHERE episode_id = ?",
                (eid,),
            )
            return eid

    def count_today_curiosity_questions(self, *, day_start_ts: float) -> int:
        with self._lock, self._conn_scope() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM curiosity_episodes
                WHERE intent = 'genuine_curiosity' AND outcome = 'sent' AND created_at >= ?
                """,
                (float(day_start_ts),),
            ).fetchone()
        return int(row[0] if row else 0)

    def curiosity_stats(self) -> dict[str, int]:
        with self._lock, self._conn_scope() as conn:
            topics = conn.execute("SELECT COUNT(*) FROM curiosity_topics").fetchone()[0]
            open_topics = conn.execute(
                "SELECT COUNT(*) FROM curiosity_topics WHERE status IN ('open', 'active')"
            ).fetchone()[0]
            followups = conn.execute(
                "SELECT COUNT(*) FROM proactive_followups WHERE status = 'open'"
            ).fetchone()[0]
            episodes = conn.execute("SELECT COUNT(*) FROM curiosity_episodes").fetchone()[0]
        return {
            "curiosity_topic_count": int(topics),
            "curiosity_open_topic_count": int(open_topics),
            "proactive_open_followup_count": int(followups),
            "curiosity_episode_count": int(episodes),
        }
