"""
[Module Responsibility]
超越传统静态 RAG 的混合长久记忆引擎 (`server/core/long_term_memory.py`)。
融合 2025-2026 前沿 Agent 记忆架构：
- **Hindsight (`vectorize-io/hindsight`)**: 区分语义事实 (Fact)、亲历经验 (Experience, 含成功/失败结果) 与决策 (Decision)，支持 `retain / recall`。
- **OpenViking (`volcengine/OpenViking`)**: `L0 (一句话索引与触发条件) -> L1 (可执行方法/命令/URL 概览) -> L2 (关联 ctx_id 原文)` 渐进分层，并在 `memory_vault/experiences/` 保持透明 Markdown 镜像。
- **Graphiti (`getzep/graphiti`)**: Bi-Temporal 双时间轴覆盖机制 (`valid_until` + `superseded_by`)，新方案生效后自动让冲突旧方案失效而不丢失审计记录。
- **SimpleMem / 正统 Harness 序列提取**: 提取对话时完整展开 `turn_steps` 与工具输出细节（URL、命令、文件路径、`ctx_id`），彻底杜绝“方案藏在工具里一压就忘”。
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from vault_manager import now_iso

_URL_RX = re.compile(r'https?://[^\s<>"\')\]}]+', re.IGNORECASE)


def extract_urls(text: str, limit: int = 8) -> list[str]:
    seen: list[str] = []
    for m in _URL_RX.finditer(text or ''):
        u = m.group(0).rstrip('.,;:!?`')
        if u and u not in seen:
            seen.append(u)
            if len(seen) >= limit:
                break
    return seen


def _extract_terms(text: str) -> list[str]:
    low = (text or '').lower()
    raw = re.findall(r'[a-z0-9_./:-]{2,}|[\u4e00-\u9fff]{2,}', low)
    out: list[str] = []
    seen: set[str] = set()
    for tok in raw:
        if len(tok) > 3 and re.fullmatch(r'[\u4e00-\u9fff]+', tok):
            for i in range(len(tok) - 1):
                bg = tok[i : i + 2]
                if bg not in seen:
                    seen.add(bg)
                    out.append(bg)
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
        if len(out) >= 48:
            break
    return out


def _compact_tool_payload(raw: Any, limit: int = 1600) -> str:
    if isinstance(raw, str):
        s = raw.strip()
        try:
            parsed = json.loads(s)
            if isinstance(parsed, dict):
                return _compact_tool_payload(parsed, limit=limit)
        except Exception:
            pass
    elif isinstance(raw, dict):
        # Preserve high-signal fields first (`url`, `title`, `ctx_id`, `stdout`, `content`, `page_text`, `note`, `error`)
        slim: dict[str, Any] = {}
        for k in (
            'ok', 'op', 'action', 'url', 'title', 'query', 'path', 'command',
            'ctx_id', 'ctx_offloaded', 'error', 'note', 'summary', 'message',
            'head_preview', 'key_highlights', 'tail_preview',
            'stdout', 'output', 'page_text', 'content', 'results', 'hits', 'chunks',
        ):
            if k in raw and raw[k] not in (None, '', [], {}):
                val = raw[k]
                if isinstance(val, str) and len(val) > 900:
                    slim[k] = val[:620] + '…' + val[-220:]
                elif isinstance(val, list) and len(val) > 8:
                    slim[k] = val[:8]
                else:
                    slim[k] = val
        if not slim:
            slim = raw
        s = json.dumps(slim, ensure_ascii=False)
    else:
        s = str(raw or '').strip()

    if len(s) <= limit:
        return s
    head = int(limit * 0.7)
    tail = max(160, limit - head - 32)
    return f'{s[:head]}\n…(省略 {len(s) - head - tail} 字)…\n{s[-tail:]}'


def format_harness_transcript(
    messages: list[dict[str, Any]],
    *,
    max_total_chars: int = 120_000,
    max_msg_chars: int = 3_600,
    max_tool_chars: int = 1_600,
) -> str:
    """
    Serialize conversation messages *including* their `turn_steps` (`assistant.tool_calls` + `role:"tool"` results)
    and `trace` URLs/findings so compaction and long-term consolidation NEVER lose solutions buried in tool outputs.
    """
    blocks: list[str] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = str(m.get('role') or 'user')
        kind = str(m.get('kind') or 'chat')
        if kind == 'compact_summary':
            txt = str(m.get('text') or '').strip()
            if txt:
                blocks.append(f"[历史锚定工作簿摘要]:\n{txt[:6000]}")
            continue
        speaker = str(m.get('from_bot_name') or ('用户' if role == 'user' else '助手'))
        msg_txt = str(m.get('text') or '').strip()
        if len(msg_txt) > max_msg_chars:
            keep_h = int(max_msg_chars * 0.68)
            keep_t = max(250, max_msg_chars - keep_h - 32)
            msg_txt = f"{msg_txt[:keep_h]}\n…(省略 {len(msg_txt) - keep_h - keep_t} 字)…\n{msg_txt[-keep_t:]}"

        lines: list[str] = []
        if role == 'assistant':
            # Extract real tool execution sequence first so solutions found via web/read_file/bash appear inline
            steps = m.get('turn_steps')
            tool_lines: list[str] = []
            if isinstance(steps, list) and steps:
                # Flatten nested or flat step list
                flat_steps: list[dict] = []
                for it in steps:
                    if isinstance(it, list):
                        flat_steps.extend(x for x in it if isinstance(x, dict))
                    elif isinstance(it, dict):
                        flat_steps.append(it)
                tc_meta: dict[str, tuple[str, str]] = {}
                for it in flat_steps:
                    if it.get('role') == 'assistant':
                        for tc in it.get('tool_calls') or []:
                            if not isinstance(tc, dict):
                                continue
                            cid = str(tc.get('id') or '')
                            fn = tc.get('function') if isinstance(tc.get('function'), dict) else {}
                            tname = str(fn.get('name') or 'tool')
                            targs = str(fn.get('arguments') or '')[:320]
                            tc_meta[cid] = (tname, targs)
                    elif it.get('role') == 'tool':
                        cid = str(it.get('tool_call_id') or '')
                        tname, targs = tc_meta.get(cid, ('tool', ''))
                        out_str = _compact_tool_payload(it.get('content', ''), limit=max_tool_chars)
                        tool_lines.append(f"  ↳ [{tname}({targs})] => {out_str}")
            elif isinstance(m.get('trace'), list) and m['trace']:
                for tr in m['trace'][-16:]:
                    if not isinstance(tr, dict):
                        continue
                    tname = str(tr.get('tool') or 'tool')
                    targs = json.dumps(tr.get('arguments') or {}, ensure_ascii=False)[:280]
                    res_s = _compact_tool_payload(tr.get('result'), limit=max_tool_chars)
                    tool_lines.append(f"  ↳ [{tname}({targs})] => {res_s}")

            if tool_lines:
                lines.append(f"[{speaker} · 工具执行实录]:\n" + "\n".join(tool_lines[-20:]))
            if msg_txt:
                lines.append(f"[{speaker}]: {msg_txt}")
        else:
            if msg_txt:
                lines.append(f"[{speaker}]: {msg_txt}")
        if lines:
            blocks.append("\n".join(lines))

    joined = "\n\n".join(blocks)
    if len(joined) <= max_total_chars:
        return joined
    # Preserve tail (most recent turns in the archived span) preferentially
    return "…(更早期历史已截断)…\n\n" + joined[-(max_total_chars - 32):]


class LongTermMemoryEngine:
    """Persistent post-RAG Agent Memory (Experiences / Facts / Decisions / Working Ledgers)."""

    MAX_MEMORIES = 1200

    def __init__(self, db_path: Path, vault=None, legacy_db_path: Path | None = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.vault = vault
        self._lock = threading.RLock()
        self._fts5_available = False
        self._init_db()
        if legacy_db_path is not None:
            self._migrate_legacy_db_if_needed(Path(legacy_db_path))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL;')
        conn.execute('PRAGMA synchronous=NORMAL;')
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memories_v2 (
                        id TEXT PRIMARY KEY,
                        bot_id TEXT NOT NULL,
                        source TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        topic TEXT NOT NULL,
                        l0_summary TEXT NOT NULL,
                        l1_overview TEXT NOT NULL,
                        l2_ctx_id TEXT NOT NULL,
                        source_urls_json TEXT NOT NULL,
                        outcome TEXT NOT NULL,
                        keywords TEXT NOT NULL,
                        importance REAL NOT NULL DEFAULT 1.0,
                        recall_count INTEGER NOT NULL DEFAULT 0,
                        last_recalled_at REAL NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        valid_from REAL NOT NULL,
                        valid_until REAL,
                        superseded_by TEXT
                    )
                    """
                )
                conn.execute('CREATE INDEX IF NOT EXISTS idx_mem_v2_active ON memories_v2(bot_id, valid_until, updated_at DESC)')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_mem_v2_topic ON memories_v2(bot_id, topic, valid_until)')
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS working_ledgers (
                        nb_key TEXT PRIMARY KEY,
                        bot_id TEXT NOT NULL,
                        source TEXT NOT NULL,
                        ledger_md TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                try:
                    conn.execute(
                        """
                        CREATE VIRTUAL TABLE IF NOT EXISTS memories_v2_fts USING fts5(
                            mem_id UNINDEXED,
                            topic,
                            l0_summary,
                            l1_overview,
                            keywords
                        )
                        """
                    )
                    self._fts5_available = True
                except Exception:
                    self._fts5_available = False
                conn.commit()
            finally:
                conn.close()

    def _migrate_legacy_db_if_needed(self, legacy_path: Path) -> None:
        """One-shot merge of standalone `long_term_memory.db` into unified `context_index.db`."""
        try:
            if not legacy_path.exists() or legacy_path.resolve() == self.db_path.resolve():
                return
        except Exception:
            return
        with self._lock:
            src_conn = None
            dst_conn = self._connect()
            try:
                src_conn = sqlite3.connect(str(legacy_path), timeout=5.0)
                src_conn.row_factory = sqlite3.Row
                tables = {r[0] for r in src_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                if 'working_ledgers' in tables:
                    for row in src_conn.execute('SELECT nb_key, bot_id, source, ledger_md, updated_at FROM working_ledgers').fetchall():
                        dst_conn.execute(
                            'INSERT OR IGNORE INTO working_ledgers(nb_key, bot_id, source, ledger_md, updated_at) VALUES(?, ?, ?, ?, ?)',
                            (row['nb_key'], row['bot_id'], row['source'], row['ledger_md'], row['updated_at']),
                        )
                if 'memories_v2' in tables:
                    for row in src_conn.execute('SELECT * FROM memories_v2').fetchall():
                        cur = dst_conn.execute(
                            """
                            INSERT OR IGNORE INTO memories_v2(
                                id, bot_id, source, kind, topic, l0_summary, l1_overview, l2_ctx_id,
                                source_urls_json, outcome, keywords, importance, recall_count, last_recalled_at,
                                created_at, updated_at, valid_from, valid_until, superseded_by
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            tuple(row[k] for k in (
                                'id', 'bot_id', 'source', 'kind', 'topic', 'l0_summary', 'l1_overview', 'l2_ctx_id',
                                'source_urls_json', 'outcome', 'keywords', 'importance', 'recall_count', 'last_recalled_at',
                                'created_at', 'updated_at', 'valid_from', 'valid_until', 'superseded_by',
                            )),
                        )
                        if cur.rowcount > 0 and self._fts5_available and row['valid_until'] is None:
                            dst_conn.execute(
                                'INSERT INTO memories_v2_fts(mem_id, topic, l0_summary, l1_overview, keywords) VALUES (?, ?, ?, ?, ?)',
                                (row['id'], row['topic'], row['l0_summary'], row['l1_overview'], row['keywords']),
                            )
                dst_conn.commit()
            except Exception:
                return
            finally:
                if src_conn is not None:
                    try:
                        src_conn.close()
                    except Exception:
                        pass
                dst_conn.close()
            try:
                bak_path = legacy_path.with_suffix('.db.migrated.bak')
                if not bak_path.exists():
                    legacy_path.replace(bak_path)
            except Exception:
                pass

    def set_working_ledger(self, bot_id: str, source: str, ledger_md: str) -> None:
        clean = (ledger_md or '').strip()[:16000]
        if not clean:
            return
        nb_key = f"{source or 'phone'}:{bot_id or 'companion'}"
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    'INSERT OR REPLACE INTO working_ledgers(nb_key, bot_id, source, ledger_md, updated_at) VALUES(?, ?, ?, ?, ?)',
                    (nb_key, bot_id or 'companion', source or 'phone', clean, now),
                )
                conn.commit()
            finally:
                conn.close()

    def get_working_ledger(self, bot_id: str, source: str) -> str:
        nb_key = f"{source or 'phone'}:{bot_id or 'companion'}"
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute('SELECT ledger_md FROM working_ledgers WHERE nb_key = ? LIMIT 1', (nb_key,)).fetchone()
                return str(row['ledger_md']) if row and row['ledger_md'] else ''
            finally:
                conn.close()

    def retain(
        self,
        *,
        l0_summary: str,
        l1_overview: str,
        topic: str = '',
        kind: str = 'experience',
        bot_id: str = 'companion',
        source: str = 'phone',
        l2_ctx_id: str = '',
        source_urls: list[str] | None = None,
        outcome: str = 'success',
        keywords: str = '',
        importance: float = 1.0,
        supersede_topic: bool = True,
    ) -> dict[str, Any]:
        """Store an atomic long-term memory (Experience / Fact / Decision) with Bi-Temporal superseding."""
        l0 = ' '.join(str(l0_summary or '').strip().splitlines())[:200]
        l1 = str(l1_overview or '').strip()[:2400]
        if not l0 and not l1:
            return {'ok': False, 'error': 'empty memory'}
        if not l0:
            l0 = l1[:120]
        if not l1:
            l1 = l0

        norm_topic = re.sub(r'\s+', '_', str(topic or '').strip().lower())[:96]
        if not norm_topic:
            norm_topic = '_'.join(_extract_terms(l0)[:4]) or ('mem_' + secrets.token_hex(3))

        urls = list(dict.fromkeys([str(u).strip() for u in (source_urls or []) if str(u).strip()] + extract_urls(l1)))[:10]
        kw_list = list(dict.fromkeys(_extract_terms(f'{norm_topic} {l0} {keywords}')))[:24]
        kw_str = ' '.join(kw_list)
        eff_kind = kind if kind in ('experience', 'fact', 'decision') else 'experience'
        eff_outcome = outcome if outcome in ('success', 'failed', 'partial', 'unknown') else 'unknown'
        mem_id = 'ltm-' + secrets.token_hex(6)
        now = time.time()

        superseded_ids: list[str] = []
        with self._lock:
            conn = self._connect()
            try:
                # Bi-temporal dedup & superseding: mark older active entries on the same topic invalid (`valid_until`)
                existing_rows = conn.execute(
                    'SELECT id, l0_summary, l1_overview, topic FROM memories_v2 WHERE bot_id = ? AND valid_until IS NULL',
                    (bot_id or 'companion',),
                ).fetchall()
                for er in existing_rows:
                    same_topic = (str(er['topic']) == norm_topic and bool(norm_topic))
                    exact_dup = (str(er['l0_summary']).strip() == l0 and str(er['l1_overview']).strip()[:240] == l1[:240])
                    if exact_dup:
                        # Refresh timestamp/ctx pointer in-place instead of creating duplicates
                        conn.execute(
                            'UPDATE memories_v2 SET l1_overview = ?, l2_ctx_id = CASE WHEN ? != "" THEN ? ELSE l2_ctx_id END, '
                            'source_urls_json = ?, keywords = ?, outcome = ?, updated_at = ?, importance = MAX(importance, ?) '
                            'WHERE id = ?',
                            (l1, l2_ctx_id, l2_ctx_id, json.dumps(urls, ensure_ascii=False), kw_str, eff_outcome, now, float(importance), er['id']),
                        )
                        conn.commit()
                        return {'ok': True, 'id': str(er['id']), 'updated': True, 'topic': norm_topic}
                    if supersede_topic and same_topic:
                        superseded_ids.append(str(er['id']))
                        conn.execute(
                            'UPDATE memories_v2 SET valid_until = ?, superseded_by = ?, updated_at = ? WHERE id = ?',
                            (now, mem_id, now, er['id']),
                        )
                        if self._fts5_available:
                            conn.execute('DELETE FROM memories_v2_fts WHERE mem_id = ?', (er['id'],))

                conn.execute(
                    """
                    INSERT INTO memories_v2(
                        id, bot_id, source, kind, topic, l0_summary, l1_overview, l2_ctx_id,
                        source_urls_json, outcome, keywords, importance, recall_count, last_recalled_at,
                        created_at, updated_at, valid_from, valid_until, superseded_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, NULL, NULL)
                    """,
                    (
                        mem_id,
                        bot_id or 'companion',
                        source or 'phone',
                        eff_kind,
                        norm_topic,
                        l0,
                        l1,
                        l2_ctx_id or '',
                        json.dumps(urls, ensure_ascii=False),
                        eff_outcome,
                        kw_str,
                        float(importance),
                        now,
                        now,
                        now,
                    ),
                )
                if self._fts5_available:
                    conn.execute(
                        'INSERT INTO memories_v2_fts(mem_id, topic, l0_summary, l1_overview, keywords) VALUES (?, ?, ?, ?, ?)',
                        (mem_id, norm_topic, l0, l1, kw_str),
                    )
                conn.commit()
            finally:
                conn.close()

        # Mirror human-inspectable card into memory_vault/experiences/ (OpenViking philosophy)
        if self.vault is not None and eff_kind in ('experience', 'decision'):
            try:
                safe_slug = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fff_-]+', '_', norm_topic)[:48].strip('_') or mem_id
                url_md = ('\n- **参考来源**：' + ' | '.join(urls[:4])) if urls else ''
                ctx_md = f'\n- **逐字原文索引**：`ctx_id="{l2_ctx_id}"`' if l2_ctx_id else ''
                card_md = (
                    f"# 💡 {l0}\n\n"
                    f"- **类型**：`{eff_kind}` | **结果**：`{eff_outcome}` | **主题键**：`{norm_topic}`\n"
                    f"- **记录时间**：{now_iso()}{url_md}{ctx_md}\n\n"
                    f"## 方法与要点 (L1)\n{l1}\n"
                )
                self.vault.write(f'experiences/{safe_slug}.md', card_md)
            except Exception:
                pass

        return {'ok': True, 'id': mem_id, 'topic': norm_topic, 'superseded': superseded_ids}

    def recall(
        self,
        query: str,
        *,
        bot_id: str = '',
        limit: int = 3,
        max_chars: int = 1800,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        """Adaptive multi-signal recall (FTS5 BM25 + CJK 2-gram + Temporal + Outcome Weighting)."""
        q = (query or '').strip()
        if not q or len(q) < 2:
            return []
        terms = _extract_terms(q)
        if not terms:
            return []
        eff_limit = max(1, min(12, int(limit or 3)))

        scored: dict[str, tuple[float, dict[str, Any]]] = {}
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                where_active = '' if include_inactive else 'AND m.valid_until IS NULL'
                where_bot = 'AND (m.bot_id = ? OR m.bot_id = "all" OR ? = "")'
                if self._fts5_available:
                    safe_fts = [re.sub(r'["\'()*^:\-+]', ' ', t).strip() for t in terms if re.sub(r'["\'()*^:\-+]', ' ', t).strip()]
                    if safe_fts:
                        expr = ' OR '.join(f'"{t}"' for t in safe_fts[:8])
                        try:
                            sql = f"""
                                SELECT m.*, bm25(memories_v2_fts) AS bscore
                                FROM memories_v2_fts f
                                JOIN memories_v2 m ON m.id = f.mem_id
                                WHERE memories_v2_fts MATCH ? {where_active} {where_bot}
                                ORDER BY bscore ASC LIMIT ?
                            """
                            for r in conn.execute(sql, [expr, bot_id, bot_id, eff_limit * 4]).fetchall():
                                d = dict(r)
                                bm = max(0.2, -float(d.get('bscore') or 0.0) + 1.0)
                                scored[d['id']] = (bm * 2.0, d)
                        except Exception:
                            pass

                # Substring + bigram pass (catches CJK technical combos reliably even when FTS tokenization varies)
                scan_sql = f'SELECT * FROM memories_v2 m WHERE 1=1 {where_active} {where_bot} ORDER BY updated_at DESC LIMIT 200'
                for r in conn.execute(scan_sql, [bot_id, bot_id]).fetchall():
                    d = dict(r)
                    hay = f"{d.get('topic', '')} {d.get('l0_summary', '')} {d.get('l1_overview', '')} {d.get('keywords', '')}".lower()
                    hit_terms = sum(1 for t in terms if t in hay)
                    if hit_terms <= 0:
                        continue
                    coverage = hit_terms / max(1.0, min(len(terms), 6))
                    age_days = max(0.0, (now - float(d.get('updated_at') or now)) / 86400.0)
                    recency_boost = 1.0 + 0.35 / (1.0 + age_days * 0.2)
                    kind_boost = 1.35 if d.get('kind') == 'experience' and d.get('outcome') == 'success' else 1.0
                    imp_boost = max(0.5, min(2.0, float(d.get('importance') or 1.0)))
                    base_score = hit_terms * 1.4 + coverage * 2.5
                    prev = scored.get(d['id'])
                    total_sc = ((prev[0] if prev else 0.0) + base_score) * recency_boost * kind_boost * imp_boost
                    scored[d['id']] = (total_sc, d)

                ranked = sorted(scored.values(), key=lambda x: x[0], reverse=True)
                picked: list[dict[str, Any]] = []
                used_chars = 0
                picked_ids: list[str] = []
                for sc, item in ranked:
                    if len(picked) >= eff_limit:
                        break
                    # Require meaningful overlap so unrelated chit-chat does not pull noise
                    if sc < 2.2:
                        continue
                    try:
                        urls = json.loads(item.get('source_urls_json') or '[]')
                    except Exception:
                        urls = []
                    l1 = str(item.get('l1_overview') or '')
                    est_len = len(str(item.get('l0_summary') or '')) + len(l1) + 80
                    if picked and used_chars + est_len > max_chars:
                        l1 = l1[:240] + '…'
                        est_len = len(str(item.get('l0_summary') or '')) + len(l1) + 80
                        if used_chars + est_len > max_chars:
                            break
                    used_chars += est_len
                    picked_ids.append(item['id'])
                    picked.append({
                        'id': item['id'],
                        'kind': item['kind'],
                        'topic': item['topic'],
                        'outcome': item['outcome'],
                        'l0_summary': item['l0_summary'],
                        'l1_overview': l1,
                        'l2_ctx_id': item.get('l2_ctx_id') or '',
                        'source_urls': urls,
                        'score': round(sc, 3),
                        'updated_at': item.get('updated_at'),
                    })

                if picked_ids:
                    placeholders = ','.join('?' for _ in picked_ids)
                    conn.execute(
                        f'UPDATE memories_v2 SET recall_count = recall_count + 1, last_recalled_at = ? WHERE id IN ({placeholders})',
                        [now, *picked_ids],
                    )
                    conn.commit()
                return picked
            finally:
                conn.close()

    @staticmethod
    def format_recalled_block(items: list[dict[str, Any]]) -> str:
        if not items:
            return ''
        lines = [
            '<recalled_long_term_memory hint="自动从跨会话长期经验库召回（L0/L1），若需原文细节可调用 ctx_search(ctx_id=...)">'
        ]
        for idx, it in enumerate(items, 1):
            badge = f"[{it.get('kind', 'experience')}:{it.get('outcome', 'success')}]"
            ctx_note = f" (原文 ctx_id=\"{it['l2_ctx_id']}\")" if it.get('l2_ctx_id') else ''
            url_note = (' | URL: ' + ', '.join(it.get('source_urls')[:2])) if it.get('source_urls') else ''
            lines.append(f"{idx}. {badge} **{it.get('l0_summary', '')}**{ctx_note}{url_note}")
            lines.append(f"   {it.get('l1_overview', '')}")
        lines.append('</recalled_long_term_memory>')
        return '\n'.join(lines)
