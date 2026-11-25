"""
[Module Responsibility]
Long-Term Anti-Rot 4-Table Memory Store (`server/core/memory_tables.py`).

Implements the 1–3 year durable memory schema inside `context_index.db`:
1. `core_blocks`      — <=2K token static core persona/user/project/commitment blocks (L0-cache safe)
2. `entity_facts`     — Bi-temporal entity-attribute state table with 4-way write reconciliation
                        (`NOOP / UPDATE / SUPERSEDE / INSERT`) and 3-tier TTL (`permanent | project | ephemeral`)
3. `episodes`         — Chronological cross-compaction episodic timeline linked by entities & `ctx_id`
4. `memory_vectors`   — Model-versioned (`model_id`) int8-quantized vector index with soft-invalidation
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

from core.embedding_engine import cosine_similarity_blobs, text_sha1
from core.long_term_memory import _extract_terms
from vault_manager import now_iso

CORE_BLOCK_KEYS = ('identity_rules', 'user_profile', 'active_projects', 'ongoing_commitments')
CORE_BLOCK_TITLES = {
    'identity_rules': '核心铁律与称呼偏好',
    'user_profile': '主人稳定画像',
    'active_projects': '当前活跃项目与环境',
    'ongoing_commitments': '长期约定与持续事项',
}
VALID_TTL_TYPES = {'permanent', 'project', 'stateful', 'ephemeral'}
VALID_FACT_KINDS = {'identity_rule', 'core_fact', 'project_state', 'procedural_lesson', 'ephemeral'}
VALID_EPISODE_TIERS = {'L0_detail', 'L1_summary', 'L2_monthly', 'L3_yearly'}


def infer_fact_kind(ttl_type: str, text: str = '') -> str:
    ttl = str(ttl_type or 'stateful').strip().lower()
    s = str(text or '')
    if ttl == 'ephemeral':
        return 'ephemeral'
    if ttl == 'permanent' and any(k in s for k in ('铁律', '禁止', '必须', '原则', '称呼', '底线', '规矩')):
        return 'identity_rule'
    if ttl == 'project':
        return 'project_state'
    if any(k in s for k in ('踩坑', '根因', '经验', '教训', '避免', '修复方法', '排障')):
        return 'procedural_lesson'
    return 'core_fact'


def _norm_key(s: str) -> str:
    return re.sub(r'\s+', ' ', str(s or '').strip()).lower()


def _safe_json_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            return [x.strip() for x in re.split(r'[,，、;；]+', raw) if x.strip()]
    return []


class MemoryTablesStore:
    """Thread-safe 4-table long-term memory backend sharing `context_index.db`."""

    def __init__(self, db_path: Path, embedding_engine: Any = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.embedding_engine = embedding_engine
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute('PRAGMA journal_mode=WAL')
            self._conn.execute('PRAGMA synchronous=NORMAL')
            self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS core_blocks (
                block_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                max_chars INTEGER NOT NULL DEFAULT 1000,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS entity_facts (
                id TEXT PRIMARY KEY,
                canonical_entity TEXT NOT NULL,
                aliases_json TEXT NOT NULL DEFAULT '[]',
                attr TEXT NOT NULL,
                value TEXT NOT NULL,
                ttl_type TEXT NOT NULL DEFAULT 'stateful',
                fact_kind TEXT NOT NULL DEFAULT 'core_fact',
                status TEXT NOT NULL DEFAULT 'active',
                confidence REAL NOT NULL DEFAULT 0.9,
                access_count INTEGER NOT NULL DEFAULT 0,
                last_accessed_at REAL NOT NULL DEFAULT 0,
                evidence_quotes_json TEXT NOT NULL DEFAULT '[]',
                valid_from REAL NOT NULL,
                valid_until REAL,
                expires_at REAL,
                superseded_by TEXT NOT NULL DEFAULT '',
                source_ref TEXT NOT NULL DEFAULT '',
                search_cues TEXT NOT NULL DEFAULT '',
                bot_id TEXT NOT NULL DEFAULT 'all',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ef_entity_attr ON entity_facts(canonical_entity, attr, status);
            CREATE INDEX IF NOT EXISTS idx_ef_status_exp ON entity_facts(status, ttl_type, expires_at);

            CREATE TABLE IF NOT EXISTS entity_relations (
                id TEXT PRIMARY KEY,
                src_entity TEXT NOT NULL,
                relation TEXT NOT NULL,
                dst_entity TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                valid_from REAL NOT NULL,
                valid_until REAL,
                source_fact_id TEXT NOT NULL DEFAULT '',
                bot_id TEXT NOT NULL DEFAULT 'all',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_er_src ON entity_relations(src_entity, status);
            CREATE INDEX IF NOT EXISTS idx_er_dst ON entity_relations(dst_entity, status);

            CREATE TABLE IF NOT EXISTS episodes (
                id TEXT PRIMARY KEY,
                bot_id TEXT NOT NULL DEFAULT 'companion',
                source TEXT NOT NULL DEFAULT 'phone',
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                entities_json TEXT NOT NULL DEFAULT '[]',
                decisions_json TEXT NOT NULL DEFAULT '[]',
                source_ref TEXT NOT NULL DEFAULT '',
                ctx_id TEXT NOT NULL DEFAULT '',
                tier TEXT NOT NULL DEFAULT 'L0_detail',
                status TEXT NOT NULL DEFAULT 'active',
                occurred_at REAL NOT NULL,
                created_at REAL NOT NULL,
                rolled_up_into TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_ep_bot_time ON episodes(bot_id, occurred_at DESC);

            CREATE TABLE IF NOT EXISTS memory_vectors (
                item_type TEXT NOT NULL,
                item_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                dim INTEGER NOT NULL,
                vec_blob BLOB NOT NULL,
                text_hash TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL,
                PRIMARY KEY (item_type, item_id, model_id)
            );
            CREATE INDEX IF NOT EXISTS idx_mv_model_active ON memory_vectors(model_id, item_type, active);
            """
        )
        self._ensure_columns_locked('entity_facts', {
            'fact_kind': "TEXT NOT NULL DEFAULT 'core_fact'",
            'access_count': 'INTEGER NOT NULL DEFAULT 0',
            'last_accessed_at': 'REAL NOT NULL DEFAULT 0',
            'evidence_quotes_json': "TEXT NOT NULL DEFAULT '[]'",
        })
        self._ensure_columns_locked('episodes', {
            'tier': "TEXT NOT NULL DEFAULT 'L0_detail'",
            'status': "TEXT NOT NULL DEFAULT 'active'",
        })
        self._conn.commit()

    def _ensure_columns_locked(self, table: str, cols: dict[str, str]) -> None:
        existing = {r['name'] for r in self._conn.execute(f'PRAGMA table_info({table})').fetchall()}
        for col, ddl in cols.items():
            if col not in existing:
                self._conn.execute(f'ALTER TABLE {table} ADD COLUMN {col} {ddl}')

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ── Table 1: core_blocks ──────────────────────────────────────────────

    def set_core_block(self, block_key: str, content: str, *, max_chars: int = 1000) -> dict[str, Any]:
        k = str(block_key or '').strip().lower()
        if k not in CORE_BLOCK_TITLES:
            k = 'user_profile'
        now = time.time()
        clean = str(content or '').strip()[:max(200, int(max_chars or 1000))]
        with self._lock:
            self._conn.execute(
                """INSERT INTO core_blocks(block_key, title, content, max_chars, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(block_key) DO UPDATE SET content=excluded.content, max_chars=excluded.max_chars, updated_at=excluded.updated_at""",
                (k, CORE_BLOCK_TITLES[k], clean, max_chars, now),
            )
            self._conn.commit()
        return {'ok': True, 'block_key': k, 'chars': len(clean)}

    def list_core_blocks(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute('SELECT * FROM core_blocks').fetchall()
        by_k = {r['block_key']: dict(r) for r in rows}
        return [by_k[k] for k in CORE_BLOCK_KEYS if k in by_k and str(by_k[k].get('content') or '').strip()]

    def build_static_core_blocks_text(self, max_total_chars: int = 3600) -> str:
        blocks = self.list_core_blocks()
        if not blocks:
            return ''
        parts: list[str] = []
        used = 0
        for b in blocks:
            sec = f"### {b['title']}\n{b['content'].strip()}"
            if used + len(sec) > max_total_chars:
                rem = max_total_chars - used
                if rem > 80:
                    parts.append(sec[:rem])
                break
            parts.append(sec)
            used += len(sec) + 2
        return '\n\n'.join(parts)

    # ── Table 2: entity_facts (4-way reconciliation) ──────────────────────

    def _fact_embed_text(self, entity: str, aliases: list[str], attr: str, value: str, cues: str) -> str:
        al = f" ({', '.join(aliases[:6])})" if aliases else ''
        cu = f' | 线索: {cues}' if cues else ''
        return f'{entity}{al} · {attr}: {value}{cu}'

    def _upsert_vector_locked(self, item_type: str, item_id: str, text: str, active: bool = True) -> bool:
        eng = self.embedding_engine
        if eng is None:
            return False
        model_id = eng.active_model_id()
        if not model_id or model_id == 'off':
            return False
        th = text_sha1(text)
        existing = self._conn.execute(
            'SELECT text_hash, active FROM memory_vectors WHERE item_type=? AND item_id=? AND model_id=?',
            (item_type, item_id, model_id),
        ).fetchone()
        if existing and existing['text_hash'] == th and bool(existing['active']) == bool(active):
            return True
        blob = eng.embed_one(text, is_query=False)
        if not blob:
            return False
        dim = max(0, len(blob) - 4)
        self._conn.execute(
            """INSERT INTO memory_vectors(item_type, item_id, model_id, dim, vec_blob, text_hash, active, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(item_type, item_id, model_id) DO UPDATE SET
                   dim=excluded.dim, vec_blob=excluded.vec_blob, text_hash=excluded.text_hash,
                   active=excluded.active, updated_at=excluded.updated_at""",
            (item_type, item_id, model_id, dim, sqlite3.Binary(blob), th, 1 if active else 0, time.time()),
        )
        return True

    def _deactivate_vector_locked(self, item_type: str, item_id: str) -> None:
        self._conn.execute(
            'UPDATE memory_vectors SET active=0, updated_at=? WHERE item_type=? AND item_id=?',
            (time.time(), item_type, item_id),
        )

    def _find_matching_active_fact_locked(self, canonical_entity: str, aliases: list[str], attr: str) -> sqlite3.Row | None:
        ent_norms = {_norm_key(canonical_entity), *(_norm_key(a) for a in aliases if a)}
        ent_norms.discard('')
        attr_n = _norm_key(attr)
        rows = self._conn.execute(
            "SELECT * FROM entity_facts WHERE status='active' ORDER BY updated_at DESC"
        ).fetchall()
        for r in rows:
            if _norm_key(r['attr']) != attr_n:
                continue
            r_ents = {_norm_key(r['canonical_entity']), *(_norm_key(a) for a in _safe_json_list(r['aliases_json']))}
            if ent_norms & r_ents:
                return r
        return None

    def upsert_fact_reconciled(
        self,
        *,
        entity: str,
        attr: str,
        value: str,
        aliases: list[str] | None = None,
        ttl_type: str = 'stateful',
        fact_kind: str = '',
        evidence_quotes: list[str] | None = None,
        expires_at: float | None = None,
        source_ref: str = '',
        search_cues: str = '',
        bot_id: str = 'all',
        confidence: float = 0.9,
        mode: str = 'auto',
        embed: bool = True,
    ) -> dict[str, Any]:
        """4-way write reconciliation: NOOP / UPDATE / SUPERSEDE / INSERT."""
        ent = str(entity or '').strip()[:120]
        at = str(attr or '').strip()[:120]
        val = str(value or '').strip()[:1200]
        if not (ent and at and val):
            return {'ok': False, 'action': 'INVALID', 'error': 'entity, attr, value 均不能为空'}
        ttl = str(ttl_type or 'stateful').strip().lower()
        if ttl not in VALID_TTL_TYPES:
            ttl = 'stateful'
        fkind = str(fact_kind or '').strip().lower()
        if fkind not in VALID_FACT_KINDS:
            fkind = infer_fact_kind(ttl, f'{ent} {at} {val}')
        al_list = list(dict.fromkeys([a.strip() for a in (aliases or []) if str(a).strip() and a.strip().lower() != ent.lower()]))[:12]
        ev_list = [str(x).strip()[:200] for x in (evidence_quotes or []) if str(x).strip()][:6]
        cues = str(search_cues or '').strip()[:400]
        now = time.time()

        with self._lock:
            existing = self._find_matching_active_fact_locked(ent, al_list, at)
            if existing is not None:
                old_val = str(existing['value'] or '').strip()
                merged_aliases = list(dict.fromkeys([*_safe_json_list(existing['aliases_json']), *al_list]))[:16]
                if ent.lower() != str(existing['canonical_entity']).lower() and ent not in merged_aliases:
                    merged_aliases.append(ent)
                merged_cues = existing['search_cues'] or ''
                if cues and cues.lower() not in merged_cues.lower():
                    merged_cues = (f'{merged_cues} {cues}').strip()[:400]
                old_ev = _safe_json_list(existing['evidence_quotes_json']) if 'evidence_quotes_json' in existing.keys() else []
                merged_ev = list(dict.fromkeys([*old_ev, *ev_list]))[:6]

                # 1. NOOP: semantically identical value
                if _norm_key(old_val) == _norm_key(val):
                    self._conn.execute(
                        """UPDATE entity_facts SET aliases_json=?, search_cues=?, fact_kind=?, evidence_quotes_json=?, updated_at=?,
                           expires_at=COALESCE(?, expires_at), source_ref=CASE WHEN ?!='' THEN ? ELSE source_ref END
                           WHERE id=?""",
                        (json.dumps(merged_aliases, ensure_ascii=False), merged_cues, fkind,
                         json.dumps(merged_ev, ensure_ascii=False), now, expires_at, source_ref, source_ref, existing['id']),
                    )
                    if embed:
                        self._upsert_vector_locked('fact', existing['id'], self._fact_embed_text(existing['canonical_entity'], merged_aliases, at, old_val, merged_cues), active=True)
                    self._conn.commit()
                    return {'ok': True, 'action': 'NOOP', 'id': existing['id'], 'entity': existing['canonical_entity'], 'attr': at}

                # 2. UPDATE: explicit additive update or strict superset of old value
                if mode == 'update' or (_norm_key(old_val) in _norm_key(val) and len(val) > len(old_val)):
                    self._conn.execute(
                        """UPDATE entity_facts SET value=?, aliases_json=?, search_cues=?, ttl_type=?, fact_kind=?,
                           evidence_quotes_json=?, expires_at=?, confidence=?, source_ref=?, updated_at=? WHERE id=?""",
                        (val, json.dumps(merged_aliases, ensure_ascii=False), merged_cues, ttl, fkind,
                         json.dumps(merged_ev, ensure_ascii=False), expires_at, float(confidence), source_ref or existing['source_ref'], now, existing['id']),
                    )
                    if embed:
                        self._upsert_vector_locked('fact', existing['id'], self._fact_embed_text(existing['canonical_entity'], merged_aliases, at, val, merged_cues), active=True)
                    self._conn.commit()
                    return {'ok': True, 'action': 'UPDATE', 'id': existing['id'], 'entity': existing['canonical_entity'], 'attr': at}

                # 3. SUPERSEDE: conflicting new truth retires old row while keeping history
                new_id = 'ef-' + secrets.token_hex(6)
                canon = existing['canonical_entity']
                self._conn.execute(
                    "UPDATE entity_facts SET status='superseded', valid_until=?, superseded_by=?, updated_at=? WHERE id=?",
                    (now, new_id, now, existing['id']),
                )
                self._deactivate_vector_locked('fact', existing['id'])
                self._conn.execute(
                    """INSERT INTO entity_facts(id, canonical_entity, aliases_json, attr, value, ttl_type, fact_kind, status,
                       confidence, access_count, last_accessed_at, evidence_quotes_json, valid_from, valid_until, expires_at,
                       superseded_by, source_ref, search_cues, bot_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, 0, 0, ?, ?, NULL, ?, '', ?, ?, ?, ?, ?)""",
                    (new_id, canon, json.dumps(merged_aliases, ensure_ascii=False), at, val, ttl, fkind,
                     float(confidence), json.dumps(merged_ev, ensure_ascii=False), now, expires_at, source_ref, merged_cues, bot_id, now, now),
                )
                if embed:
                    self._upsert_vector_locked('fact', new_id, self._fact_embed_text(canon, merged_aliases, at, val, merged_cues), active=True)
                self._conn.commit()
                return {'ok': True, 'action': 'SUPERSEDE', 'id': new_id, 'superseded_id': existing['id'], 'entity': canon, 'attr': at}

            # 4. INSERT: brand new entity/attribute fact
            new_id = 'ef-' + secrets.token_hex(6)
            self._conn.execute(
                """INSERT INTO entity_facts(id, canonical_entity, aliases_json, attr, value, ttl_type, fact_kind, status,
                   confidence, access_count, last_accessed_at, evidence_quotes_json, valid_from, valid_until, expires_at,
                   superseded_by, source_ref, search_cues, bot_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, 0, 0, ?, ?, NULL, ?, '', ?, ?, ?, ?, ?)""",
                (new_id, ent, json.dumps(al_list, ensure_ascii=False), at, val, ttl, fkind,
                 float(confidence), json.dumps(ev_list, ensure_ascii=False), now, expires_at, source_ref, cues, bot_id, now, now),
            )
            if embed:
                self._upsert_vector_locked('fact', new_id, self._fact_embed_text(ent, al_list, at, val, cues), active=True)
            self._conn.commit()
            return {'ok': True, 'action': 'INSERT', 'id': new_id, 'entity': ent, 'attr': at}

    def delete_fact(self, fact_id: str = '', *, entity: str = '', attr: str = '', hard: bool = False) -> dict[str, Any]:
        """Explicitly delete a fact by `fact_id` or `(entity, attr)` (soft delete or physical hard purge)."""
        fid = str(fact_id or '').strip()
        ent_q = _norm_key(entity)
        attr_q = _norm_key(attr)
        if not fid and not ent_q:
            return {'ok': False, 'deleted': 0, 'error': '需要提供 fact_id 或 entity'}
        now = time.time()
        with self._lock:
            if fid:
                rows = self._conn.execute('SELECT id, canonical_entity, attr FROM entity_facts WHERE id=?', (fid,)).fetchall()
            else:
                cand = self._conn.execute("SELECT id, canonical_entity, aliases_json, attr FROM entity_facts WHERE status!='deleted'").fetchall()
                rows = []
                for r in cand:
                    r_ents = {_norm_key(r['canonical_entity']), *(_norm_key(a) for a in _safe_json_list(r['aliases_json']))}
                    if not any(ent_q == e or ent_q in e for e in r_ents if e):
                        continue
                    if attr_q and attr_q not in _norm_key(r['attr']):
                        continue
                    rows.append(r)
            if not rows:
                return {'ok': False, 'deleted': 0, 'existed': False, 'fact_id': fid, 'error': '未找到匹配的事实'}
            deleted_ids: list[str] = []
            for r in rows:
                rid = str(r['id'])
                if hard:
                    self._conn.execute('DELETE FROM entity_facts WHERE id=?', (rid,))
                    self._conn.execute('DELETE FROM entity_relations WHERE source_fact_id=?', (rid,))
                    self._conn.execute("DELETE FROM memory_vectors WHERE item_type='fact' AND item_id=?", (rid,))
                else:
                    self._conn.execute(
                        "UPDATE entity_facts SET status='deleted', valid_until=?, updated_at=? WHERE id=?",
                        (now, now, rid),
                    )
                    self._conn.execute(
                        "UPDATE entity_relations SET status='deleted', valid_until=?, updated_at=? WHERE source_fact_id=?",
                        (now, now, rid),
                    )
                    self._deactivate_vector_locked('fact', rid)
                deleted_ids.append(rid)
            self._conn.commit()
        return {
            'ok': True,
            'deleted': len(deleted_ids),
            'deleted_ids': deleted_ids,
            'existed': True,
            'hard': bool(hard),
            'fact_id': deleted_ids[0],
        }

    # ── Table 2.5: entity_relations (lightweight 1-hop graph) ─────────────

    def upsert_relation(
        self,
        *,
        src_entity: str,
        relation: str,
        dst_entity: str,
        source_fact_id: str = '',
        bot_id: str = 'all',
    ) -> dict[str, Any]:
        src = str(src_entity or '').strip()[:120]
        rel = str(relation or '').strip()[:80]
        dst = str(dst_entity or '').strip()[:120]
        if not (src and rel and dst) or _norm_key(src) == _norm_key(dst):
            return {'ok': False, 'action': 'INVALID'}
        now = time.time()
        with self._lock:
            dup = self._conn.execute(
                "SELECT id FROM entity_relations WHERE lower(src_entity)=lower(?) AND lower(relation)=lower(?) AND lower(dst_entity)=lower(?) AND status='active' LIMIT 1",
                (src, rel, dst),
            ).fetchone()
            if dup:
                self._conn.execute(
                    'UPDATE entity_relations SET updated_at=?, source_fact_id=CASE WHEN ?!="" THEN ? ELSE source_fact_id END WHERE id=?',
                    (now, source_fact_id, source_fact_id, dup['id']),
                )
                self._conn.commit()
                return {'ok': True, 'action': 'NOOP', 'id': dup['id']}
            rid = 'er-' + secrets.token_hex(6)
            self._conn.execute(
                """INSERT INTO entity_relations(id, src_entity, relation, dst_entity, status, valid_from, valid_until, source_fact_id, bot_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'active', ?, NULL, ?, ?, ?, ?)""",
                (rid, src, rel, dst, now, source_fact_id, bot_id, now, now),
            )
            self._conn.commit()
            return {'ok': True, 'action': 'INSERT', 'id': rid}

    def query_relations(self, entities: list[str], *, limit: int = 10) -> list[dict[str, Any]]:
        norms = {_norm_key(e) for e in (entities or []) if _norm_key(e)}
        if not norms:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM entity_relations WHERE status='active' ORDER BY updated_at DESC LIMIT 300"
            ).fetchall()
        matched: list[dict[str, Any]] = []
        for r in rows:
            s_n = _norm_key(r['src_entity'])
            d_n = _norm_key(r['dst_entity'])
            if any(n in s_n or s_n in n or n in d_n or d_n in n for n in norms):
                matched.append({
                    'id': r['id'],
                    'src': r['src_entity'],
                    'relation': r['relation'],
                    'dst': r['dst_entity'],
                    'source_fact_id': r['source_fact_id'],
                })
                if len(matched) >= max(1, int(limit or 10)):
                    break
        return matched

    def has_active_vectors(self, item_type: str = '') -> bool:
        eng = self.embedding_engine
        if eng is None:
            return False
        mid = eng.active_model_id()
        if not mid or mid == 'off':
            return False
        with self._lock:
            if item_type:
                row = self._conn.execute(
                    'SELECT 1 FROM memory_vectors WHERE item_type=? AND model_id=? AND active=1 LIMIT 1',
                    (item_type, mid),
                ).fetchone()
            else:
                row = self._conn.execute(
                    'SELECT 1 FROM memory_vectors WHERE model_id=? AND active=1 LIMIT 1',
                    (mid,),
                ).fetchone()
        return row is not None

    def embed_query_fast(self, query: str, *, non_blocking_warmup: bool = True) -> bytes | None:
        """Compute query vector at most once per turn; 0ms short-circuit on empty vector index or cold session."""
        q = str(query or '').strip()
        if not q or not self.has_active_vectors():
            return None
        eng = self.embedding_engine
        if eng is None:
            return None
        try:
            return eng.embed_one(q, is_query=True, non_blocking_warmup=non_blocking_warmup)
        except TypeError:
            return eng.embed_one(q, is_query=True)

    def query_facts(
        self,
        *,
        query: str = '',
        entity: str = '',
        attr: str = '',
        include_history: bool = False,
        limit: int = 12,
        query_vec: bytes | None = None,
        non_blocking_warmup: bool = False,
    ) -> list[dict[str, Any]]:
        """Hybrid lookup over `entity_facts`: exact entity/alias + lexical terms + optional ONNX vector RRF."""
        now = time.time()
        q = str(query or '').strip()
        ent_q = _norm_key(entity)
        attr_q = _norm_key(attr)
        terms = _extract_terms(f'{q} {entity} {attr}')
        with self._lock:
            sql = 'SELECT * FROM entity_facts'
            if not include_history:
                sql += " WHERE status='active' AND (expires_at IS NULL OR expires_at > ?)"
                rows = self._conn.execute(sql + ' ORDER BY updated_at DESC', (now,)).fetchall()
            else:
                rows = self._conn.execute(sql + ' ORDER BY updated_at DESC').fetchall()

            vec_scores: dict[str, float] = {}
            if rows and q and self.embedding_engine is not None and self.embedding_engine.active_model_id() != 'off':
                mid = self.embedding_engine.active_model_id()
                vrows = self._conn.execute(
                    "SELECT item_id, vec_blob FROM memory_vectors WHERE item_type='fact' AND model_id=? AND active=1",
                    (mid,),
                ).fetchall()
                if vrows:
                    q_blob = query_vec if query_vec is not None else self.embed_query_fast(q, non_blocking_warmup=non_blocking_warmup)
                    if q_blob:
                        for vr in vrows:
                            sim = cosine_similarity_blobs(q_blob, bytes(vr['vec_blob']))
                            if sim > 0.35:
                                vec_scores[vr['item_id']] = sim

        scored: list[tuple[float, dict[str, Any]]] = []
        q_low = q.lower()
        for r in rows:
            aliases = _safe_json_list(r['aliases_json'])
            ent_set = {_norm_key(r['canonical_entity']), *(_norm_key(a) for a in aliases)}
            if ent_q and not any(ent_q in e or e in ent_q for e in ent_set if e):
                continue
            if attr_q and attr_q not in _norm_key(r['attr']):
                continue
            hay = f"{r['canonical_entity']} {' '.join(aliases)} {r['attr']} {r['value']} {r['search_cues']}".lower()
            lex = 0.0
            if q_low and len(q_low) >= 2 and q_low in hay:
                lex += 6.0
            if ent_q and ent_q in ent_set:
                lex += 8.0
            hit_t = sum(1 for t in terms if t in hay)
            lex += hit_t * 1.4
            v_sim = vec_scores.get(r['id'], 0.0)
            total = lex + (v_sim * 10.0 if v_sim > 0 else 0.0)
            if (q or ent_q or attr_q) and total < 1.2:
                continue
            if not (q or ent_q or attr_q):
                total = float(r['updated_at'] or 0) / 1e10
            if r['status'] != 'active':
                total *= 0.65
            rkeys = r.keys()
            acc = int(r['access_count'] or 0) if 'access_count' in rkeys else 0
            fkind = str(r['fact_kind'] or 'core_fact') if 'fact_kind' in rkeys else 'core_fact'
            ev_q = _safe_json_list(r['evidence_quotes_json']) if 'evidence_quotes_json' in rkeys else []
            if (q or ent_q or attr_q) and acc > 0:
                total += min(1.2, acc * 0.12)
            scored.append((total, {
                'id': r['id'],
                'entity': r['canonical_entity'],
                'aliases': aliases,
                'attr': r['attr'],
                'value': r['value'],
                'ttl_type': r['ttl_type'],
                'fact_kind': fkind,
                'access_count': acc,
                'evidence_quotes': ev_q,
                'status': r['status'],
                'expires_at': r['expires_at'],
                'superseded_by': r['superseded_by'],
                'source_ref': r['source_ref'],
                'search_cues': r['search_cues'],
                'updated_at': r['updated_at'],
                'score': round(total, 3),
                'vec_sim': round(v_sim, 3),
            }))
        scored.sort(key=lambda x: x[0], reverse=True)
        out = [x[1] for x in scored[:max(1, int(limit or 12))]]
        if out and (q or ent_q or attr_q):
            hit_ids = [item['id'] for item in out if item.get('status') == 'active']
            if hit_ids:
                with self._lock:
                    placeholders = ','.join('?' for _ in hit_ids)
                    self._conn.execute(
                        f'UPDATE entity_facts SET access_count = access_count + 1, last_accessed_at = ? WHERE id IN ({placeholders})',
                        [now, *hit_ids],
                    )
                    self._conn.commit()
        return out

    # ── Table 3: episodes (4-tier time pyramid) ───────────────────────────

    def add_episode(
        self,
        *,
        title: str,
        summary: str,
        entities: list[str] | None = None,
        decisions: list[str] | None = None,
        bot_id: str = 'companion',
        source: str = 'phone',
        source_ref: str = '',
        ctx_id: str = '',
        tier: str = 'L0_detail',
        occurred_at: float | None = None,
        embed: bool = True,
    ) -> dict[str, Any]:
        ttl_s = str(title or '').strip()[:180]
        sum_s = str(summary or '').strip()[:3000]
        if not (ttl_s and sum_s):
            return {'ok': False, 'error': 'episode title/summary 不能为空'}
        ents = list(dict.fromkeys([str(e).strip() for e in (entities or []) if str(e).strip()]))[:16]
        decs = [str(d).strip()[:300] for d in (decisions or []) if str(d).strip()][:10]
        tr = str(tier or 'L0_detail').strip()
        if tr not in VALID_EPISODE_TIERS:
            tr = 'L0_detail'
        now = time.time()
        occ = float(occurred_at if occurred_at is not None else now)
        ep_id = 'ep-' + secrets.token_hex(6)
        with self._lock:
            if source_ref:
                dup = self._conn.execute("SELECT id FROM episodes WHERE source_ref=? AND status='active' LIMIT 1", (source_ref,)).fetchone()
                if dup:
                    return {'ok': True, 'action': 'NOOP', 'id': dup['id']}
            self._conn.execute(
                """INSERT INTO episodes(id, bot_id, source, title, summary, entities_json, decisions_json,
                   source_ref, ctx_id, tier, status, occurred_at, created_at, rolled_up_into)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, '')""",
                (ep_id, bot_id, source, ttl_s, sum_s, json.dumps(ents, ensure_ascii=False),
                 json.dumps(decs, ensure_ascii=False), source_ref, ctx_id, tr, occ, now),
            )
            if embed:
                self._upsert_vector_locked('episode', ep_id, f"{ttl_s} ({', '.join(ents)}): {sum_s[:700]}", active=True)
            self._conn.commit()
        return {'ok': True, 'action': 'INSERT', 'id': ep_id, 'tier': tr}

    def delete_episode(self, episode_id: str = '', *, title_keyword: str = '', hard: bool = True) -> dict[str, Any]:
        """Explicitly delete an episode by `episode_id` or `title_keyword`."""
        eid = str(episode_id or '').strip()
        kw = _norm_key(title_keyword)
        if not eid and not kw:
            return {'ok': False, 'deleted': 0, 'error': '需要提供 episode_id 或 title_keyword'}
        with self._lock:
            if eid:
                rows = self._conn.execute('SELECT id, title FROM episodes WHERE id=?', (eid,)).fetchall()
            else:
                cand = self._conn.execute("SELECT id, title, summary FROM episodes WHERE status='active'").fetchall()
                rows = [r for r in cand if kw in _norm_key(f"{r['title']} {r['summary']}")]
            if not rows:
                return {'ok': False, 'deleted': 0, 'existed': False, 'episode_id': eid, 'error': '未找到匹配的情景'}
            deleted_ids: list[str] = []
            for r in rows:
                rid = str(r['id'])
                if hard:
                    self._conn.execute('DELETE FROM episodes WHERE id=?', (rid,))
                    self._conn.execute("DELETE FROM memory_vectors WHERE item_type='episode' AND item_id=?", (rid,))
                else:
                    self._conn.execute("UPDATE episodes SET status='deleted' WHERE id=?", (rid,))
                    self._deactivate_vector_locked('episode', rid)
                deleted_ids.append(rid)
            self._conn.commit()
        return {'ok': True, 'deleted': len(deleted_ids), 'deleted_ids': deleted_ids, 'existed': True, 'hard': bool(hard), 'episode_id': deleted_ids[0]}

    def query_episodes(
        self,
        query: str,
        *,
        bot_id: str = '',
        limit: int = 5,
        query_vec: bytes | None = None,
        non_blocking_warmup: bool = False,
    ) -> list[dict[str, Any]]:
        q = str(query or '').strip()
        terms = _extract_terms(q)
        with self._lock:
            if bot_id and bot_id != 'all':
                rows = self._conn.execute(
                    "SELECT * FROM episodes WHERE rolled_up_into='' AND status='active' AND (bot_id=? OR bot_id='all') ORDER BY occurred_at DESC LIMIT 400",
                    (bot_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM episodes WHERE rolled_up_into='' AND status='active' ORDER BY occurred_at DESC LIMIT 400"
                ).fetchall()
            vec_scores: dict[str, float] = {}
            if rows and q and self.embedding_engine is not None and self.embedding_engine.active_model_id() != 'off':
                mid = self.embedding_engine.active_model_id()
                vrows = self._conn.execute(
                    "SELECT item_id, vec_blob FROM memory_vectors WHERE item_type='episode' AND model_id=? AND active=1",
                    (mid,),
                ).fetchall()
                if vrows:
                    q_blob = query_vec if query_vec is not None else self.embed_query_fast(q, non_blocking_warmup=non_blocking_warmup)
                    if q_blob:
                        for vr in vrows:
                            sim = cosine_similarity_blobs(q_blob, bytes(vr['vec_blob']))
                            if sim > 0.35:
                                vec_scores[vr['item_id']] = sim
        scored: list[tuple[float, dict[str, Any]]] = []
        q_low = q.lower()
        for r in rows:
            ents = _safe_json_list(r['entities_json'])
            hay = f"{r['title']} {' '.join(ents)} {r['summary']}".lower()
            lex = (5.0 if q_low and q_low in hay else 0.0) + sum(1.3 for t in terms if t in hay)
            v_sim = vec_scores.get(r['id'], 0.0)
            total = lex + (v_sim * 10.0 if v_sim > 0 else 0.0)
            if q and total < 1.5:
                continue
            scored.append((total, {
                'id': r['id'],
                'title': r['title'],
                'summary': r['summary'],
                'entities': ents,
                'decisions': _safe_json_list(r['decisions_json']),
                'source_ref': r['source_ref'],
                'ctx_id': r['ctx_id'],
                'tier': str(r['tier'] or 'L0_detail') if 'tier' in r.keys() else 'L0_detail',
                'occurred_at': r['occurred_at'],
                'score': round(total, 3),
                'vec_sim': round(v_sim, 3),
            }))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [x[1] for x in scored[:max(1, int(limit or 5))]]

    # ── Table 4: vector reindex + ledger export + stats ───────────────────

    def reindex_vectors(self, batch_size: int = 24) -> dict[str, Any]:
        """Re-embed all active facts and episodes for the currently configured `model_id`."""
        eng = self.embedding_engine
        if eng is None or eng.active_model_id() == 'off':
            return {'ok': True, 'skipped': True, 'reason': 'embedding_off', 'indexed': 0}
        mid = eng.active_model_id()
        now = time.time()
        with self._lock:
            facts = self._conn.execute(
                "SELECT * FROM entity_facts WHERE status='active' AND (expires_at IS NULL OR expires_at > ?)", (now,)
            ).fetchall()
            eps = self._conn.execute("SELECT * FROM episodes WHERE rolled_up_into='' AND status='active'").fetchall()
            existing_hashes = {
                (r['item_type'], r['item_id']): r['text_hash']
                for r in self._conn.execute('SELECT item_type, item_id, text_hash FROM memory_vectors WHERE model_id=? AND active=1', (mid,)).fetchall()
            }

        pending: list[tuple[str, str, str, str]] = []
        for f in facts:
            txt = self._fact_embed_text(f['canonical_entity'], _safe_json_list(f['aliases_json']), f['attr'], f['value'], f['search_cues'])
            th = text_sha1(txt)
            if existing_hashes.get(('fact', f['id'])) != th:
                pending.append(('fact', f['id'], txt, th))
        for e in eps:
            txt = f"{e['title']} ({', '.join(_safe_json_list(e['entities_json']))}): {str(e['summary'])[:700]}"
            th = text_sha1(txt)
            if existing_hashes.get(('episode', e['id'])) != th:
                pending.append(('episode', e['id'], txt, th))

        indexed = 0
        from core.embedding_engine import pack_int8_vector
        for i in range(0, len(pending), max(1, batch_size)):
            chunk = pending[i : i + batch_size]
            vecs = eng.embed_texts([c[2] for c in chunk], is_query=False)
            if not vecs:
                return {'ok': False, 'model_id': mid, 'indexed': indexed, 'error': eng.status().get('last_error') or 'embed_unavailable'}
            with self._lock:
                for (itype, iid, _txt, th), vec in zip(chunk, vecs):
                    blob = pack_int8_vector(vec)
                    self._conn.execute(
                        """INSERT INTO memory_vectors(item_type, item_id, model_id, dim, vec_blob, text_hash, active, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                           ON CONFLICT(item_type, item_id, model_id) DO UPDATE SET
                               dim=excluded.dim, vec_blob=excluded.vec_blob, text_hash=excluded.text_hash, active=1, updated_at=excluded.updated_at""",
                        (itype, iid, mid, max(0, len(blob) - 4), sqlite3.Binary(blob), th, time.time()),
                    )
                    indexed += 1
                self._conn.commit()
        return {'ok': True, 'model_id': mid, 'indexed': indexed, 'total_active': len(facts) + len(eps)}

    def stats(self) -> dict[str, Any]:
        with self._lock:
            core_n = self._conn.execute('SELECT COUNT(*) FROM core_blocks').fetchone()[0]
            ef_active = self._conn.execute("SELECT COUNT(*) FROM entity_facts WHERE status='active'").fetchone()[0]
            ef_hist = self._conn.execute("SELECT COUNT(*) FROM entity_facts WHERE status!='active'").fetchone()[0]
            er_active = self._conn.execute("SELECT COUNT(*) FROM entity_relations WHERE status='active'").fetchone()[0]
            ep_active = self._conn.execute("SELECT COUNT(*) FROM episodes WHERE rolled_up_into='' AND status='active'").fetchone()[0]
            vec_active = self._conn.execute('SELECT COUNT(*) FROM memory_vectors WHERE active=1').fetchone()[0]
        return {
            'core_blocks': int(core_n),
            'active_facts': int(ef_active),
            'historical_facts': int(ef_hist),
            'active_relations': int(er_active),
            'active_episodes': int(ep_active),
            'active_vectors': int(vec_active),
            'embedding': self.embedding_engine.status() if self.embedding_engine else {'tier': 'off'},
        }

    def render_memory_ledger_markdown(self) -> str:
        """Render a clean human-auditable Markdown mirror (`memory_vault/profile/memory_ledger.md`)."""
        lines = ['# 长期记忆结构化账本 (Memory Ledger)\n', f'> 更新时间：{now_iso()} · 由 5 表长期记忆系统自动镜像生成，支持 `/memory reorganize` 深度整理与 `/memory rollback` 回滚\n']
        core_txt = self.build_static_core_blocks_text(max_total_chars=4000)
        if core_txt:
            lines.append('## 一、核心常驻块 (`core_blocks`)\n')
            lines.append(core_txt + '\n')
        facts = self.query_facts(limit=300)
        by_ent: dict[str, list[dict[str, Any]]] = {}
        for f in facts:
            by_ent.setdefault(f['entity'], []).append(f)
        lines.append(f'## 二、实体状态事实表 (`entity_facts`, 活跃 {len(facts)} 条)\n')
        for ent, items in sorted(by_ent.items()):
            lines.append(f'### {ent}')
            for it in items:
                ttl_tag = f"[{it['ttl_type']}/{it.get('fact_kind', 'core_fact')}]"
                lines.append(f"- **{it['attr']}** {ttl_tag} (`{it['id']}`): {it['value']}")
            lines.append('')
        with self._lock:
            rels = self._conn.execute("SELECT src_entity, relation, dst_entity FROM entity_relations WHERE status='active' ORDER BY updated_at DESC LIMIT 80").fetchall()
        if rels:
            lines.append(f'## 三、实体关联图 (`entity_relations`, 活跃 {len(rels)} 条)\n')
            for rl in rels:
                lines.append(f"- `{rl['src_entity']}` --[{rl['relation']}]--> `{rl['dst_entity']}`")
            lines.append('')
        eps = self.query_episodes('', limit=30)
        lines.append(f'## 四、情景时间金字塔 (`episodes`, 近期 {len(eps)} 条)\n')
        for ep in eps:
            lines.append(f"- **[{ep.get('tier', 'L0_detail')}] {ep['title']}** (`{ep['id']}`): {ep['summary'][:220]}")
        return '\n'.join(lines).strip() + '\n'
