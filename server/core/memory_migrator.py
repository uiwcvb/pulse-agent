"""
[Module Responsibility]
8-Stage Full-Auto Deep Memory Consolidation, One-Click Rollback & 4-Tier Time Pyramid Gardener
(`server/core/memory_migrator.py`).

Runs cleanly as a fire-and-forget async `subtask(preset="memory_reorganize")` with Staging
isolation, local ONNX semantic clustering, Pass A entity canonicalization, Pass B chronological
cluster adjudication + 1-hop relations, Pass C independent Critic grounding/anti-misdeletion guard,
atomic commit + legacy archive retirement, and reversible `.migration_backups/` rollback.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from core.embedding_engine import cosine_similarity_blobs, pack_int8_vector
from core.memory_tables import infer_fact_kind
from core.unified_recall import extract_entities
from vault_manager import TZ, atomic_write, now_iso

logger = logging.getLogger(__name__)

_EXPIRED_DATE_RE = re.compile(r'(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})')
_EPHEMERAL_HINTS = ('今天', '明天', '后天', '今晚', '明早', '临时', '验证码', '报错', '刚才', '闹钟', '提醒')


def _guess_ttl_and_expiry(text: str, now_ts: float) -> tuple[str, float | None, bool]:
    s = str(text or '')
    if any(k in s for k in ('永久', '铁律', '禁止', '称呼', '生日', '偏好', '习惯', '原则')):
        return 'permanent', None, False
    is_eph = any(k in s for k in _EPHEMERAL_HINTS)
    m = _EXPIRED_DATE_RE.search(s)
    if m and is_eph:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 23, 59, tzinfo=TZ)
            exp_ts = dt.timestamp()
            if exp_ts < now_ts - 14 * 86400:
                return 'ephemeral', exp_ts, True
            return 'ephemeral', exp_ts, False
        except Exception:
            pass
    if is_eph:
        return 'ephemeral', now_ts + 14 * 86400, False
    if any(k in s for k in ('项目', '仓库', '部署', 'VPS', 'server', 'android', '分支', '版本')):
        return 'project', None, False
    return 'stateful', None, False


def _episode_tier_for_age(occurred_at: float, now_ts: float) -> str:
    age_days = max(0.0, (now_ts - float(occurred_at or now_ts)) / 86400.0)
    if age_days <= 30:
        return 'L0_detail'
    if age_days <= 90:
        return 'L1_summary'
    if age_days <= 365:
        return 'L2_monthly'
    return 'L3_yearly'


def _extract_json_payload(raw: str) -> dict[str, Any] | None:
    s = str(raw or '').strip()
    if not s:
        return None
    m = re.search(r'```(?:json)?\s*([\s\S]*?)```', s, flags=re.IGNORECASE)
    if m:
        s = m.group(1).strip()
    start = s.find('{')
    end = s.rfind('}')
    if start >= 0 and end > start:
        s = s[start : end + 1]
    try:
        parsed = json.loads(s)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _backup_legacy_memory(vault_root: Path, db_path: Path, *, prefix: str = 'reorg') -> Path:
    stamp = datetime.now(TZ).strftime('%Y%m%d-%H%M%S')
    bdir = vault_root / '.migration_backups' / f'{prefix}-{stamp}'
    bdir.mkdir(parents=True, exist_ok=True)
    for child in vault_root.iterdir():
        if child.name in ('.migration_backups', '.history'):
            continue
        dst = bdir / 'memory_vault' / child.name
        if child.is_dir():
            shutil.copytree(child, dst, dirs_exist_ok=True)
        elif child.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, dst)
    if db_path.is_file():
        dst_db = bdir / db_path.name
        try:
            src_conn = sqlite3.connect(str(db_path))
            dst_conn = sqlite3.connect(str(dst_db))
            src_conn.backup(dst_conn)
            dst_conn.close()
            src_conn.close()
        except Exception:
            shutil.copy2(db_path, dst_db)
    return bdir


def list_memory_backups(vault_root: Path) -> list[dict[str, Any]]:
    broot = Path(vault_root) / '.migration_backups'
    if not broot.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for d in sorted(broot.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        db_f = d / 'context_index.db'
        out.append({
            'backup_id': d.name,
            'path': str(d),
            'has_db': db_f.is_file(),
            'created_at': round(d.stat().st_mtime, 1),
        })
    return out[:20]


def rollback_memory_backup(brain: Any, backup_id: str = '') -> dict[str, Any]:
    """Safely restore `context_index.db` & `memory_vault/` from a `.migration_backups/<id>` snapshot."""
    tables = brain.memory_engine.get_memory_tables()
    vault = getattr(brain, 'vault', None)
    vault_root = Path(vault.root) if vault is not None else (brain.store.root / 'memory_vault')
    backups = list_memory_backups(vault_root)
    if not backups:
        return {'ok': False, 'error': '未找到可回滚的整理备份快照'}

    target = None
    bid = str(backup_id or '').strip()
    if bid:
        target = next((b for b in backups if b['backup_id'] == bid), None)
        if target is None:
            return {'ok': False, 'error': f'未找到指定备份 {bid}', 'available': [b['backup_id'] for b in backups[:8]]}
    else:
        target = next((b for b in backups if b['backup_id'].startswith('reorg-')), backups[0])

    target_dir = Path(target['path'])
    safety_dir = _backup_legacy_memory(vault_root, tables.db_path, prefix='pre-rollback')

    # Restore SQLite DB cleanly via sqlite3.backup into active connection
    src_db = target_dir / tables.db_path.name
    if src_db.is_file():
        with tables._lock:
            src_conn = sqlite3.connect(str(src_db))
            try:
                src_conn.backup(tables._conn)
            finally:
                src_conn.close()
            tables._init_schema()

    # Restore vault directories/files cleanly (keep .migration_backups and .history untouched)
    src_vault = target_dir / 'memory_vault'
    if src_vault.is_dir():
        for child in list(vault_root.iterdir()):
            if child.name in ('.migration_backups', '.history'):
                continue
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                elif child.is_file():
                    child.unlink()
            except Exception:
                pass
        for child in src_vault.iterdir():
            dst = vault_root / child.name
            if child.is_dir():
                shutil.copytree(child, dst, dirs_exist_ok=True)
            elif child.is_file():
                shutil.copy2(child, dst)

    if vault is not None and hasattr(vault, 'reindex'):
        try:
            vault.reindex()
        except Exception:
            pass
    return {
        'ok': True,
        'restored_backup': target['backup_id'],
        'safety_backup': safety_dir.name,
        'tables': tables.stats(),
    }


def _collect_legacy_experience_items(brain: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen_sigs: set[str] = set()
    ltm = getattr(getattr(brain, 'memory_engine', None), 'long_term_memory', None)
    if ltm is not None and hasattr(ltm, '_conn'):
        try:
            rows = ltm._conn.execute(
                'SELECT id, bot_id, kind, l0_summary, l1_overview, tags_json, l2_ctx_id, created_at '
                'FROM memories_v2 WHERE valid_until IS NULL ORDER BY created_at ASC'
            ).fetchall()
            for r in rows:
                l0 = str(r['l0_summary'] or '').strip()
                l1 = str(r['l1_overview'] or '').strip()
                sig = re.sub(r'\s+', ' ', (l0 + '|' + l1[:120]).lower())
                if not l0 or sig in seen_sigs:
                    continue
                seen_sigs.add(sig)
                items.append({
                    'id': str(r['id']),
                    'bot_id': str(r['bot_id'] or 'all'),
                    'kind': str(r['kind'] or 'experience'),
                    'title': l0,
                    'body': l1,
                    'source_ref': f"memories_v2:{r['id']}",
                    'ctx_id': str(r['l2_ctx_id'] or ''),
                    'created_at': float(r['created_at'] or time.time()),
                })
        except Exception as exc:
            logger.warning('Failed reading memories_v2 for migration: %s', exc)

    vault = getattr(brain, 'vault', None)
    if vault is not None:
        exp_dir = Path(vault.root) / 'experiences'
        if exp_dir.is_dir():
            for md in sorted(exp_dir.glob('*.md')):
                try:
                    raw = md.read_text('utf-8', errors='replace').strip()
                except Exception:
                    continue
                if not raw:
                    continue
                lines = [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith('---')]
                title = re.sub(r'^#+\s*', '', lines[0])[:120] if lines else md.stem
                body = '\n'.join(lines[1:])[:800] if len(lines) > 1 else raw[:800]
                sig = re.sub(r'\s+', ' ', (title + '|' + body[:120]).lower())
                if sig in seen_sigs:
                    continue
                seen_sigs.add(sig)
                items.append({
                    'id': md.stem,
                    'bot_id': 'all',
                    'kind': 'experience_md',
                    'title': title,
                    'body': body,
                    'source_ref': f'experiences/{md.name}',
                    'ctx_id': '',
                    'created_at': md.stat().st_mtime,
                })
    items.sort(key=lambda x: float(x.get('created_at') or 0))
    return items


def _rule_extract_fact(item: dict[str, Any], now_ts: float) -> dict[str, Any] | None:
    title = str(item.get('title') or '').strip()
    body = str(item.get('body') or '').strip()
    combined = f'{title}\n{body}'.strip()
    if len(combined) < 8:
        return None
    ttl_type, expires_at, is_already_expired = _guess_ttl_and_expiry(combined, now_ts)
    if is_already_expired:
        return {'_expired': True, 'title': title}
    ents = extract_entities(combined)
    entity = ents[0] if ents else ('用户偏好' if any(k in combined for k in ('主人', '用户', '偏好', '习惯')) else title[:24])
    attr = title[:48] if title else '记录'
    value = body[:500] if body else title[:500]
    return {
        'entity': entity,
        'aliases': ents[1:5],
        'attr': attr,
        'value': value,
        'ttl_type': ttl_type,
        'fact_kind': infer_fact_kind(ttl_type, combined),
        'evidence_quotes': [title[:120]],
        'expires_at': expires_at,
        'search_cues': ' '.join(ents[:6]),
        'source_ref': item.get('source_ref', ''),
        'bot_id': item.get('bot_id', 'all'),
        'protected': bool(item.get('protected', False) or ttl_type == 'permanent'),
    }


def _cluster_items_semantically(items: list[dict[str, Any]], embedding_engine: Any = None, max_cluster_size: int = 12) -> list[list[dict[str, Any]]]:
    """Stage 3: Cluster items by entity overlap + local ONNX cosine >= 0.80, sorted chronologically."""
    if not items:
        return []
    ent_sets = [set(e.lower() for e in extract_entities(f"{it.get('title','')} {it.get('body','')[:240]}")[:6]) for it in items]
    blobs: list[bytes | None] = [None] * len(items)
    if embedding_engine is not None and embedding_engine.active_model_id() != 'off':
        try:
            texts = [f"{it.get('title','')} {it.get('body','')[:200]}" for it in items]
            for start in range(0, len(texts), 32):
                vecs = embedding_engine.embed_texts(texts[start : start + 32], is_query=False)
                if not vecs:
                    break
                for offset, v in enumerate(vecs):
                    blobs[start + offset] = pack_int8_vector(v)
        except Exception:
            pass

    clusters: list[dict[str, Any]] = []
    for idx, it in enumerate(items):
        b = blobs[idx]
        es = ent_sets[idx]
        best_ci = -1
        best_sc = 0.0
        for ci, cl in enumerate(clusters):
            if len(cl['items']) >= max_cluster_size:
                continue
            sc = 0.0
            if es and cl['ents'] and (es & cl['ents']):
                sc = max(sc, 0.86)
            if b is not None and cl['rep_blob'] is not None:
                sim = cosine_similarity_blobs(b, cl['rep_blob'])
                if sim >= 0.80:
                    sc = max(sc, sim)
            if sc > best_sc:
                best_sc = sc
                best_ci = ci
        if best_ci >= 0:
            clusters[best_ci]['items'].append(it)
            clusters[best_ci]['ents'] |= es
        else:
            clusters.append({'items': [it], 'ents': set(es), 'rep_blob': b})

    # Merge tiny singletons of size 1 into small review batches of up to 8 so LLM calls stay compact
    grouped: list[list[dict[str, Any]]] = []
    singletons: list[dict[str, Any]] = []
    for cl in clusters:
        if len(cl['items']) == 1:
            singletons.append(cl['items'][0])
        else:
            grouped.append(sorted(cl['items'], key=lambda x: float(x.get('created_at') or 0)))
    for i in range(0, len(singletons), 6):
        grouped.append(sorted(singletons[i : i + 6], key=lambda x: float(x.get('created_at') or 0)))
    return grouped


async def _call_worker_json(brain: Any, system_prompt: str, user_prompt: str, *, timeout_sec: float = 600.0, retries: int = 1) -> tuple[dict[str, Any] | None, str]:
    llm = getattr(brain, 'llm', None)
    cfg = getattr(getattr(brain, 'store', None), 'config', None)
    if llm is None or cfg is None:
        return None, 'llm_unavailable'
    worker_model = cfg.effective_worker_model() if hasattr(cfg, 'effective_worker_model') else cfg.effective_model()
    api_key = cfg.effective_api_key(big=False) or cfg.effective_api_key(big=True)
    if not (worker_model and api_key):
        return None, 'missing_worker_model_or_key'
    last_err = ''
    for attempt in range(retries + 1):
        try:
            resp = await asyncio.wait_for(
                llm.complete_stream(
                    messages=[{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}],
                    bot_id='companion', bot_model=worker_model, tools=None, big=False, stream_prefix='', on_stream=None,
                ),
                timeout=timeout_sec,
            )
            raw_content = str(resp.get('content') or '')
            parsed = _extract_json_payload(raw_content)
            if parsed is not None:
                return parsed, ''
            last_err = f'invalid_json ({len(raw_content)} chars)'
        except asyncio.TimeoutError:
            last_err = f'TimeoutError (>{int(timeout_sec)}s)'
            logger.warning('Worker LLM attempt %d/%d failed: %s', attempt + 1, retries + 1, last_err)
        except Exception as exc:
            last_err = f'{type(exc).__name__}: {str(exc)[:180]}'
            logger.warning('Worker LLM attempt %d/%d failed: %s', attempt + 1, retries + 1, last_err)
        if attempt < retries:
            await asyncio.sleep(2.0 * (attempt + 1))
    return None, last_err


async def _pass_a_canonicalize_entities(brain: Any, clusters: list[list[dict[str, Any]]]) -> dict[str, str]:
    counts: dict[str, int] = {}
    for cl in clusters:
        for it in cl:
            for e in extract_entities(f"{it.get('title','')} {it.get('body','')[:200]}")[:4]:
                counts[e] = counts.get(e, 0) + 1
    top_ents = [k for k, _ in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:80]]
    if len(top_ents) < 4:
        return {}
    prompt = '识别以下实体名中的同义别名并映射到规范名。只输出紧凑 JSON：`{"alias_map": {"别名": "规范名"}}`。\n\n' + '、'.join(top_ents)
    parsed, _err = await _call_worker_json(brain, '只输出合法紧凑 JSON。', prompt, timeout_sec=600.0, retries=1)
    raw_map = parsed.get('alias_map') if isinstance(parsed, dict) else None
    if not isinstance(raw_map, dict):
        return {}
    return {str(k).strip(): str(v).strip() for k, v in raw_map.items() if str(k).strip() and str(v).strip() and str(k).strip() != str(v).strip()}


async def _pass_b_adjudicate_cluster(
    brain: Any,
    cluster: list[dict[str, Any]],
    alias_map: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, str]:
    lines = []
    for idx, it in enumerate(cluster, 1):
        ts_s = datetime.fromtimestamp(float(it.get('created_at') or time.time()), TZ).strftime('%Y-%m-%d')
        prot = '[保护] ' if it.get('protected') else ''
        lines.append(f"[{idx}] {ts_s} {prot}{it.get('title','')[:110]} | {it.get('body','')[:180]}")
    alias_hint = f'\n实体规范映射：{json.dumps(alias_map, ensure_ascii=False)[:400]}\n' if alias_map else ''
    prompt = (
        '按时间线合并去重下面记录（新状态覆盖旧状态，淘汰过时一次性琐事，带[保护]的不可删）。'
        f'{alias_hint}'
        '直接输出紧凑 JSON：`{"facts":[{"entity":"规范名","aliases":[],"attr":"属性","value":"最新精炼值","ttl_type":"permanent|project|stateful|ephemeral","fact_kind":"identity_rule|core_fact|project_state|procedural_lesson|ephemeral","evidence_quote":"原文短词"}],"relations":[{"src":"A","relation":"uses|deployed_on|owns|depends_on","dst":"B"}]}`\n\n'
        + '\n'.join(lines)
    )
    parsed, err = await _call_worker_json(brain, '只输出合法紧凑 JSON，精简直接。', prompt, timeout_sec=600.0, retries=1)
    if not parsed or not isinstance(parsed.get('facts'), list):
        return [], [], False, err or 'empty_facts_json'
    facts = [f for f in parsed['facts'] if isinstance(f, dict) and f.get('entity') and f.get('attr') and f.get('value')]
    rels = [r for r in (parsed.get('relations') or []) if isinstance(r, dict) and r.get('src') and r.get('relation') and r.get('dst')]
    return facts, rels, True, ''


def _is_fact_grounded(fact: dict[str, Any], corpus_lower: str) -> bool:
    quote = str(fact.get('evidence_quote') or '').strip().lower()
    val = str(fact.get('value') or '').strip().lower()
    ent = str(fact.get('entity') or '').strip().lower()
    if not corpus_lower:
        return True
    if quote and len(quote) >= 4 and quote[:16] in corpus_lower:
        return True
    tokens = [t for t in re.split(r'[\s,，。.:：;；/\-_()（）\[\]]+', f'{ent} {val}') if len(t) >= 2]
    if not tokens:
        return True
    hits = sum(1 for t in tokens[:10] if t in corpus_lower)
    return hits >= 1


async def reorganize_legacy_memory(
    brain: Any,
    *,
    dry_run: bool = False,
    use_llm: bool = True,
    batch_size: int = 20,
    resume_checkpoint: dict[str, Any] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    on_progress: Callable[..., Any] | None = None,
    task_id: str = '',
) -> dict[str, Any]:
    """Full-auto 8-stage reorganization with Staging isolation, Critic guard, legacy archive & rollback."""
    tables = brain.memory_engine.get_memory_tables()
    vault = getattr(brain, 'vault', None)
    vault_root = Path(vault.root) if vault is not None else (brain.store.root / 'memory_vault')
    now_ts = time.time()

    async def _emit(stage_idx: int, stage_label: str, sub_done: int, sub_total: int, pct: int, step: dict[str, Any] | None = None, ckpt: dict[str, Any] | None = None, backup_id: str = '') -> None:
        if on_progress is None:
            return
        res = on_progress(
            stage_idx=stage_idx,
            stage_total=8,
            stage_label=stage_label,
            sub_done=sub_done,
            sub_total=sub_total,
            progress_pct=pct,
            step=step,
            checkpoint=ckpt,
            backup_id=backup_id,
        )
        if asyncio.iscoroutine(res):
            await res

    def _is_cancelled() -> bool:
        return bool(cancel_check and cancel_check())

    # Stage 1: Snapshot & Inventory
    legacy_exps = _collect_legacy_experience_items(brain)
    compact_files = sorted((vault_root / 'notes').glob('compact_*.md')) if (vault_root / 'notes').is_dir() else []
    pins = brain.memory_engine.list_pins() if hasattr(brain.memory_engine, 'list_pins') else []

    if dry_run:
        expired_preview = sum(1 for it in legacy_exps if (_rule_extract_fact(it, now_ts) or {}).get('_expired'))
        clusters_preview = _cluster_items_semantically(legacy_exps, None)
        return {
            'ok': True,
            'dry_run': True,
            'legacy_experiences_count': len(legacy_exps),
            'legacy_compacts_count': len(compact_files),
            'pins_count': len(pins),
            'estimated_clusters': len(clusters_preview),
            'estimated_expired_noise': expired_preview,
            'tables_current': tables.stats(),
        }

    ckpt = dict(resume_checkpoint) if isinstance(resume_checkpoint, dict) else {}
    backup_id = str(ckpt.get('backup_id') or '')
    backup_dir = str(ckpt.get('backup_dir') or '')
    if not backup_id:
        bpath = _backup_legacy_memory(vault_root, tables.db_path, prefix='reorg')
        backup_dir = str(bpath)
        backup_id = bpath.name
    await _emit(
        1, '快照备份与全域盘点', 1, 1, 8,
        step={'tool': 'stage_1_backup', 'stage': '快照备份与盘点', 'args': backup_id, 'ok': True,
              'result': f'已备份 {backup_id} · 盘点 {len(legacy_exps)} 旧经验 + {len(compact_files)} 压缩笔记 + {len(pins)} 置顶'},
        ckpt={'backup_id': backup_id, 'backup_dir': backup_dir},
        backup_id=backup_id,
    )
    if _is_cancelled():
        return {'ok': False, 'cancelled': True, 'backup_id': backup_id}

    # Stage 2: Zero-Token Pre-clean & Hard Whitelist Lock
    cleaned_items: list[dict[str, Any]] = []
    pre_expired = 0
    protected_items: list[dict[str, Any]] = []
    for p in pins:
        ptxt = str(p.get('text') or '').strip()
        if ptxt:
            protected_items.append({
                'entity': '主人核心铁律', 'attr': ptxt[:36], 'value': ptxt,
                'ttl_type': 'permanent', 'fact_kind': 'identity_rule',
                'evidence_quotes': [ptxt[:120]], 'source_ref': 'profile/pinned_memories.md', 'protected': True,
            })
    for it in legacy_exps:
        ttl_t, _exp, is_exp = _guess_ttl_and_expiry(f"{it.get('title','')} {it.get('body','')}", now_ts)
        if is_exp:
            pre_expired += 1
            continue
        is_prot = ttl_t == 'permanent'
        enriched = {**it, 'protected': is_prot}
        cleaned_items.append(enriched)
        if is_prot:
            rf = _rule_extract_fact(enriched, now_ts)
            if rf and not rf.get('_expired'):
                protected_items.append(rf)
    await _emit(
        2, '零Token预清洗与白名单加锁', 1, 1, 16,
        step={'tool': 'stage_2_preclean', 'stage': '预清洗与白名单', 'args': f'{len(legacy_exps)} 条', 'ok': True,
              'result': f'保留 {len(cleaned_items)} 条待整理 · 预清过期琐事 {pre_expired} 条 · 白名单锁定 {len(protected_items)} 条'},
    )
    if _is_cancelled():
        return {'ok': False, 'cancelled': True, 'backup_id': backup_id}

    # Stage 3: Semantic Clustering
    clusters = _cluster_items_semantically(cleaned_items, tables.embedding_engine)
    await _emit(
        3, '本地语义聚类分组', len(clusters), max(1, len(clusters)), 24,
        step={'tool': 'stage_3_cluster', 'stage': '本地语义聚类', 'args': f'{len(cleaned_items)} 条', 'ok': True,
              'result': f'聚成 {len(clusters)} 个时间线主题簇'},
    )
    if _is_cancelled():
        return {'ok': False, 'cancelled': True, 'backup_id': backup_id}

    # Stage 4: Pass A Global Entity Canonicalization
    alias_map: dict[str, str] = ckpt.get('alias_map') if isinstance(ckpt.get('alias_map'), dict) else {}
    llm_calls = int(ckpt.get('llm_calls') or 0)
    if use_llm and not alias_map and clusters:
        alias_map = await _pass_a_canonicalize_entities(brain, clusters)
        llm_calls += 1
    await _emit(
        4, 'Pass A · 全局实体名归一', 1, 1, 32,
        step={'tool': 'stage_4_canonicalize', 'stage': '全局实体归一', 'args': f'{len(clusters)} 簇', 'ok': True,
              'result': f'识别并统一 {len(alias_map)} 组同义实体别名'},
        ckpt={'backup_id': backup_id, 'backup_dir': backup_dir, 'alias_map': alias_map, 'llm_calls': llm_calls},
    )
    if _is_cancelled():
        return {'ok': False, 'cancelled': True, 'backup_id': backup_id}

    # Stage 5: Pass B Cluster Timeline Adjudication + Relations (Staging ONLY, Resumable!)
    done_cluster_idxs: set[int] = set(ckpt.get('done_clusters') or [])
    staged_facts: list[dict[str, Any]] = list(ckpt.get('staged_facts') or [])
    staged_rels: list[dict[str, Any]] = list(ckpt.get('staged_rels') or [])
    fallback_batches = int(ckpt.get('fallback_batches') or 0)
    total_cl = len(clusters)

    for ci, cl in enumerate(clusters):
        if _is_cancelled():
            return {'ok': False, 'cancelled': True, 'backup_id': backup_id}
        if ci in done_cluster_idxs:
            continue
        cl_title = str(cl[0].get('title') or f'簇#{ci + 1}')[:36]
        cl_corpus = ' '.join(f"{x.get('title','')} {x.get('body','')}" for x in cl).lower()
        used_llm_ok = False
        cl_err = ''
        cl_facts: list[dict[str, Any]] = []
        cl_rels: list[dict[str, Any]] = []
        if use_llm:
            cl_facts, cl_rels, used_llm_ok, cl_err = await _pass_b_adjudicate_cluster(brain, cl, alias_map)
            llm_calls += 1
        if not used_llm_ok:
            if use_llm:
                fallback_batches += 1
            for it in cl:
                rf = _rule_extract_fact(it, now_ts)
                if rf and not rf.get('_expired'):
                    cl_facts.append(rf)
        # Apply alias_map normalization + mark cluster corpus hash for Stage 6 grounding
        for f in cl_facts:
            raw_ent = str(f.get('entity') or '').strip()
            canon_ent = alias_map.get(raw_ent, raw_ent)
            aliases = list(f.get('aliases') or []) if isinstance(f.get('aliases'), list) else []
            if canon_ent != raw_ent and raw_ent not in aliases:
                aliases.append(raw_ent)
            f['entity'] = canon_ent
            f['aliases'] = aliases[:10]
            f['_grounded'] = _is_fact_grounded(f, cl_corpus)
            staged_facts.append(f)
        for r in cl_rels:
            staged_rels.append({
                'src': alias_map.get(str(r.get('src') or '').strip(), str(r.get('src') or '').strip()),
                'relation': str(r.get('relation') or '').strip(),
                'dst': alias_map.get(str(r.get('dst') or '').strip(), str(r.get('dst') or '').strip()),
            })
        done_cluster_idxs.add(ci)
        pct = 32 + int(42 * (len(done_cluster_idxs) / max(1, total_cl)))
        await _emit(
            5, 'Pass B · 簇内时间线裁决与关系抽取', len(done_cluster_idxs), max(1, total_cl), pct,
            step={
                'tool': 'stage_5_adjudicate',
                'stage': '簇内时间线裁决',
                'args': f'[{ci + 1}/{total_cl}] {cl_title} ({len(cl)}条)',
                'ok': used_llm_ok or (not use_llm),
                'result': f'提炼 {len(cl_facts)} 条事实 / {len(cl_rels)} 条关系' + ('' if (used_llm_ok or not use_llm) else '（已降级保留原文）'),
                'error': '' if (used_llm_ok or not use_llm) else (cl_err or '该簇模型调用失败'),
            },
            ckpt={
                'backup_id': backup_id, 'backup_dir': backup_dir, 'alias_map': alias_map, 'llm_calls': llm_calls,
                'done_clusters': sorted(done_cluster_idxs), 'staged_facts': staged_facts, 'staged_rels': staged_rels,
                'fallback_batches': fallback_batches,
            },
        )

    # Stage 6: Pass C Independent Critic Guard + Core Blocks Rebuild
    critic_rejected = 0
    critic_rescued = 0
    verified_facts: list[dict[str, Any]] = []
    for f in staged_facts:
        if not f.get('_grounded', True):
            critic_rejected += 1
            continue
        verified_facts.append(f)

    # Hard whitelist rescue: ensure every protected pin/permanent item is represented
    ver_hay = '\n'.join(f"{f.get('entity','')} {f.get('attr','')} {f.get('value','')}" for f in verified_facts).lower()
    for pf in protected_items:
        probe = str(pf.get('value') or pf.get('attr') or '').strip().lower()[:24]
        if probe and probe not in ver_hay:
            verified_facts.append(pf)
            ver_hay += '\n' + probe
            critic_rescued += 1

    # Build clean core_blocks proposals (<= 1800 tokens total)
    staged_blocks: dict[str, str] = {}
    if pins:
        pin_lines = [f"- {str(p.get('text') or '').strip()}" for p in pins if str(p.get('text') or '').strip()]
        if pin_lines:
            staged_blocks['identity_rules'] = '\n'.join(pin_lines)[:1100]
    profile_parts: list[str] = []
    for rel in ('profile/user_traits.md', 'profile/personal_impression.md'):
        p_file = vault_root / rel
        if p_file.is_file():
            txt = p_file.read_text('utf-8', errors='replace').strip()
            if txt:
                profile_parts.append(txt[:800])
    perm_lines = [
        f"- {f['entity']} · {f['attr']}: {str(f['value'])[:120]}"
        for f in verified_facts if f.get('ttl_type') == 'permanent' and f.get('fact_kind') != 'identity_rule'
    ][:12]
    proj_lines = [
        f"- {f['entity']} · {f['attr']}: {str(f['value'])[:120]}"
        for f in verified_facts if f.get('ttl_type') == 'project'
    ][:12]
    if profile_parts or perm_lines:
        staged_blocks['user_profile'] = ('\n\n'.join(profile_parts) + ('\n' + '\n'.join(perm_lines) if perm_lines else '')).strip()[:1200]
    if proj_lines:
        staged_blocks['active_projects'] = '\n'.join(proj_lines)[:1000]

    await _emit(
        6, 'Pass C · Critic防幻觉复审与核心块重铸', 1, 1, 82,
        step={'tool': 'stage_6_critic', 'stage': 'Critic复审与核心块', 'args': f'审查 {len(staged_facts)} 条候选', 'ok': True,
              'result': f'通过 {len(verified_facts)} 条 · 白名单捞回 {critic_rescued} 条 · 拦截无依据 {critic_rejected} 条'},
    )
    if _is_cancelled():
        return {'ok': False, 'cancelled': True, 'backup_id': backup_id}

    # Stage 7: Atomic Commit to Live 5-Table Store + Legacy Track Retirement
    counts = {'INSERT': 0, 'UPDATE': 0, 'SUPERSEDE': 0, 'NOOP': 0, 'EXPIRED_SKIPPED': pre_expired}
    for bk, bc in staged_blocks.items():
        if bc.strip():
            tables.set_core_block(bk, bc, max_chars=1200)
    for fact in verified_facts:
        ttl_t, exp_t, is_exp = _guess_ttl_and_expiry(f"{fact.get('attr','')} {fact.get('value','')}", now_ts)
        if is_exp and not fact.get('protected'):
            counts['EXPIRED_SKIPPED'] += 1
            continue
        ev_q = fact.get('evidence_quotes') if isinstance(fact.get('evidence_quotes'), list) else ([str(fact.get('evidence_quote'))] if fact.get('evidence_quote') else [])
        res = tables.upsert_fact_reconciled(
            entity=str(fact.get('entity') or ''),
            aliases=fact.get('aliases') if isinstance(fact.get('aliases'), list) else [],
            attr=str(fact.get('attr') or ''),
            value=str(fact.get('value') or ''),
            ttl_type=str(fact.get('ttl_type') or ttl_t),
            fact_kind=str(fact.get('fact_kind') or ''),
            evidence_quotes=ev_q,
            expires_at=fact.get('expires_at') or exp_t,
            source_ref=str(fact.get('source_ref') or 'deep_reorganize'),
            search_cues=str(fact.get('search_cues') or ''),
            bot_id=str(fact.get('bot_id') or 'all'),
            embed=False,
        )
        act = str(res.get('action') or '')
        if act in counts:
            counts[act] += 1

    rels_written = 0
    for rl in staged_rels:
        r_res = tables.upsert_relation(src_entity=rl['src'], relation=rl['relation'], dst_entity=rl['dst'])
        if r_res.get('action') == 'INSERT':
            rels_written += 1

    # Migrate legacy `notes/compact_*.md` into 4-tier `episodes` + archive legacy tracks cleanly
    legacy_arch_root = vault_root / '_legacy_archive' / backup_id
    episodes_migrated = 0
    for cf in compact_files:
        try:
            raw = cf.read_text('utf-8', errors='replace').strip()
            if not raw:
                continue
            first_line = next((ln.strip().lstrip('#').strip() for ln in raw.splitlines() if ln.strip()), cf.stem)
            ents = extract_entities(raw[:4000])
            mtime = cf.stat().st_mtime
            ep_res = tables.add_episode(
                title=f'{cf.stem}: {first_line[:90]}',
                summary=raw[:2600],
                entities=ents,
                bot_id='companion',
                source_ref=f'_legacy_archive/{backup_id}/notes/{cf.name}',
                tier=_episode_tier_for_age(mtime, now_ts),
                occurred_at=mtime,
                embed=False,
            )
            if ep_res.get('action') == 'INSERT':
                episodes_migrated += 1
            dst_cf = legacy_arch_root / 'notes' / cf.name
            dst_cf.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(cf), str(dst_cf))
        except Exception as exc:
            logger.warning('Failed migrating compact note %s: %s', cf.name, exc)

    # Retire migrated legacy experiences/*.md into _legacy_archive so new & old recall never collide
    exps_archived = 0
    exp_dir = vault_root / 'experiences'
    if exp_dir.is_dir():
        for md in sorted(exp_dir.glob('*.md')):
            try:
                dst_md = legacy_arch_root / 'experiences' / md.name
                dst_md.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(md), str(dst_md))
                exps_archived += 1
            except Exception:
                pass
    ltm = getattr(getattr(brain, 'memory_engine', None), 'long_term_memory', None)
    if ltm is not None and hasattr(ltm, '_conn'):
        try:
            ltm._conn.execute('UPDATE memories_v2 SET valid_until=? WHERE valid_until IS NULL', (now_ts,))
            ltm._conn.commit()
        except Exception:
            pass

    await _emit(
        7, '原子切换与旧轨归档', 1, 1, 92,
        step={'tool': 'stage_7_commit', 'stage': '原子切换与旧轨归档', 'args': f'归档至 _legacy_archive/{backup_id}', 'ok': True,
              'result': f'入库 {counts["INSERT"]+counts["UPDATE"]+counts["SUPERSEDE"]} 事实 / {rels_written} 关系 / {episodes_migrated} 情景 · 归档 {exps_archived} 旧经验'},
    )

    # Stage 8: Local ONNX Vector Reindex + Ledger & Report Generation
    vec_res = tables.reindex_vectors(batch_size=24)
    ledger_md = tables.render_memory_ledger_markdown()
    ledger_path = vault_root / 'profile' / 'memory_ledger.md'
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(ledger_path, ledger_md)

    rep_name = f"memory_reorg_{task_id or backup_id}.md"
    rep_rel = f'logs/{rep_name}'
    stats_now = tables.stats()
    rep_md = (
        f'# 记忆深度整理审计报告 ({backup_id})\n\n'
        f'- 完成时间：{now_iso()}\n'
        f'- 扫描源：旧经验 `{len(legacy_exps)}` 条 + 压缩笔记 `{len(compact_files)}` 篇 + 置顶 `{len(pins)}` 条\n'
        f'- 聚类与归一：聚为 `{len(clusters)}` 个主题簇，合并实体别名 `{len(alias_map)}` 组，调用辅助模型 `{llm_calls}` 次\n'
        f'- 事实写归并：新增 `{counts["INSERT"]}` | 更新 `{counts["UPDATE"]}` | 新替旧 `{counts["SUPERSEDE"]}` | 无变化 `{counts["NOOP"]}` | 清理过期 `{counts["EXPIRED_SKIPPED"]}`\n'
        f'- 实体关系与情景：新增 1-hop 关系 `{rels_written}` 条 | 迁入情景 `{episodes_migrated}` 段\n'
        f'- Critic 审计：白名单捞回 `{critic_rescued}` 条 | 拦截无依据 `{critic_rejected}` 条 | 降级兜底簇 `{fallback_batches}` 组\n'
        f'- 当前新库规模：核心块 `{stats_now["core_blocks"]}` | 活跃事实 `{stats_now["active_facts"]}` | 活跃关系 `{stats_now["active_relations"]}` | 活跃情景 `{stats_now["active_episodes"]}` | 向量 `{stats_now["active_vectors"]}`\n'
        f'- 旧轨归档：`memory_vault/_legacy_archive/{backup_id}/`\n'
        f'- 回滚命令：`/memory rollback {backup_id}`\n'
    )
    rep_path = vault_root / rep_rel
    rep_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(rep_path, rep_md)
    if vault is not None and hasattr(vault, 'reindex'):
        try:
            vault.reindex()
        except Exception:
            pass

    await _emit(
        8, '本地重建向量与报告生成', 1, 1, 100,
        step={'tool': 'stage_8_finalize', 'stage': '向量索引与审计报告', 'args': rep_rel, 'ok': True,
              'result': f'已索引 {vec_res.get("indexed", 0)} 条向量并刷新 memory_ledger.md'},
    )

    return {
        'ok': True,
        'backup_id': backup_id,
        'backup_dir': backup_dir,
        'report_path': rep_rel,
        'ledger_path': 'profile/memory_ledger.md',
        'legacy_experiences_scanned': len(legacy_exps),
        'legacy_compacts_migrated': episodes_migrated,
        'legacy_experiences_archived': exps_archived,
        'clusters_total': len(clusters),
        'aliases_merged': len(alias_map),
        'relations_added': rels_written,
        'critic_rescued': critic_rescued,
        'critic_rejected': critic_rejected,
        'fallback_batches': fallback_batches,
        'llm_calls': llm_calls,
        'reconciliation': counts,
        'vectors': vec_res,
        'tables': stats_now,
    }


def run_memory_gardener(brain: Any, *, now_ts: float | None = None) -> dict[str, Any]:
    """Weekly anti-rot maintenance: expire ephemeral facts, dedup >0.92 facts, roll up 4-tier episode pyramid."""
    tables = brain.memory_engine.get_memory_tables()
    now = float(now_ts if now_ts is not None else time.time())
    expired_n = 0
    merged_n = 0
    rolled_up_n = 0

    with tables._lock:
        exp_rows = tables._conn.execute(
            "SELECT id FROM entity_facts WHERE status='active' AND expires_at IS NOT NULL AND expires_at <= ?",
            (now,),
        ).fetchall()
        for r in exp_rows:
            tables._conn.execute(
                "UPDATE entity_facts SET status='expired', valid_until=?, updated_at=? WHERE id=?",
                (now, now, r['id']),
            )
            tables._deactivate_vector_locked('fact', r['id'])
            expired_n += 1

        eng = tables.embedding_engine
        mid = eng.active_model_id() if eng is not None else 'off'
        if mid and mid != 'off':
            vrows = tables._conn.execute(
                """SELECT f.id, f.canonical_entity, f.attr, f.updated_at, v.vec_blob
                   FROM entity_facts f
                   JOIN memory_vectors v ON v.item_type='fact' AND v.item_id=f.id AND v.model_id=? AND v.active=1
                   WHERE f.status='active' ORDER BY f.updated_at DESC LIMIT 400""",
                (mid,),
            ).fetchall()
            retired_ids: set[str] = set()
            for i in range(len(vrows)):
                a = vrows[i]
                if a['id'] in retired_ids:
                    continue
                for j in range(i + 1, len(vrows)):
                    b = vrows[j]
                    if b['id'] in retired_ids:
                        continue
                    if cosine_similarity_blobs(bytes(a['vec_blob']), bytes(b['vec_blob'])) >= 0.92:
                        tables._conn.execute(
                            "UPDATE entity_facts SET status='superseded', valid_until=?, superseded_by=?, updated_at=? WHERE id=?",
                            (now, a['id'], now, b['id']),
                        )
                        tables._deactivate_vector_locked('fact', b['id'])
                        retired_ids.add(b['id'])
                        merged_n += 1

        # 4-tier time pyramid: 30d -> L1_summary, 90d -> L2_monthly rollup, 365d -> L3_yearly rollup
        tables._conn.execute(
            "UPDATE episodes SET tier='L1_summary' WHERE rolled_up_into='' AND status='active' AND tier='L0_detail' AND occurred_at < ?",
            (now - 30 * 86400,),
        )
        for cutoff_days, target_tier, fmt, src_prefix, min_cnt in (
            (90, 'L2_monthly', '%Y-%m', 'monthly_rollup', 3),
            (365, 'L3_yearly', '%Y', 'yearly_rollup', 2),
        ):
            cutoff_ts = now - cutoff_days * 86400
            old_eps = tables._conn.execute(
                "SELECT * FROM episodes WHERE rolled_up_into='' AND status='active' AND tier!=? AND occurred_at < ? ORDER BY occurred_at ASC",
                (target_tier, cutoff_ts),
            ).fetchall()
            buckets: dict[str, list[Any]] = {}
            for ep in old_eps:
                key = datetime.fromtimestamp(float(ep['occurred_at']), TZ).strftime(fmt)
                buckets.setdefault(key, []).append(ep)
            for key, eps in buckets.items():
                if len(eps) < min_cnt:
                    continue
                bullets = [f"- {e['title']}: {str(e['summary'])[:160]}" for e in eps[:24]]
                all_ents = list(dict.fromkeys(ent for e in eps for ent in json.loads(e['entities_json'] or '[]')))[:16]
                label = '月度归并记忆' if target_tier == 'L2_monthly' else '年度里程碑归并'
                digest = tables.add_episode(
                    title=f'{key} {label} ({len(eps)} 段)',
                    summary='\n'.join(bullets)[:2600],
                    entities=all_ents,
                    bot_id='all',
                    source_ref=f'{src_prefix}:{key}',
                    tier=target_tier,
                    occurred_at=float(eps[-1]['occurred_at']),
                    embed=False,
                )
                dig_id = str(digest.get('id') or '')
                if dig_id:
                    for e in eps:
                        tables._conn.execute('UPDATE episodes SET rolled_up_into=? WHERE id=?', (dig_id, e['id']))
                        tables._deactivate_vector_locked('episode', e['id'])
                        rolled_up_n += 1
        tables._conn.commit()

    vault = getattr(brain, 'vault', None)
    if vault is not None:
        ledger_path = Path(vault.root) / 'profile' / 'memory_ledger.md'
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(ledger_path, tables.render_memory_ledger_markdown())
    if tables.embedding_engine is not None:
        tables.embedding_engine.maybe_unload_idle(now)
    return {
        'ok': True,
        'expired_facts': expired_n,
        'merged_duplicates': merged_n,
        'rolled_up_episodes': rolled_up_n,
        'tables': tables.stats(),
    }


def ingest_compaction_to_tables(
    memory_engine: Any,
    *,
    bot_id: str,
    source: str,
    summary_text: str,
    experiences: list[dict[str, Any]] | None = None,
    ctx_id: str = '',
) -> dict[str, Any]:
    tables = memory_engine.get_memory_tables() if hasattr(memory_engine, 'get_memory_tables') else None
    if tables is None:
        return {'ok': False, 'reason': 'tables_unavailable'}
    now_ts = time.time()
    ents = extract_entities(summary_text[:4000])
    first_ln = next((ln.strip().lstrip('#').strip() for ln in (summary_text or '').splitlines() if ln.strip() and '核心进展' not in ln), '会话压缩检查点')
    ep = tables.add_episode(
        title=f"{datetime.now(TZ).strftime('%m-%d %H:%M')} {first_ln[:80]}",
        summary=(summary_text or '')[:2600],
        entities=ents,
        bot_id=bot_id,
        source=source,
        ctx_id=ctx_id,
        tier='L0_detail',
        occurred_at=now_ts,
        embed=True,
    )
    facts_written = 0
    for ex in (experiences or []):
        if not isinstance(ex, dict):
            continue
        l0 = str(ex.get('l0_summary') or ex.get('title') or '').strip()
        l1 = str(ex.get('l1_overview') or ex.get('value') or '').strip()
        if not (l0 or l1):
            continue
        ttl_t, exp_t, is_exp = _guess_ttl_and_expiry(f'{l0} {l1}', now_ts)
        if is_exp:
            continue
        ex_ents = extract_entities(f'{l0} {l1}')
        ent_name = str(ex.get('entity') or (ex_ents[0] if ex_ents else l0[:24])).strip()
        tables.upsert_fact_reconciled(
            entity=ent_name,
            aliases=ex_ents[1:5],
            attr=str(ex.get('attr') or l0[:48]).strip(),
            value=(l1 or l0)[:600],
            ttl_type=str(ex.get('ttl_type') or ttl_t),
            expires_at=exp_t,
            source_ref=f'compaction:{ctx_id}' if ctx_id else 'compaction',
            search_cues=' '.join(ex_ents[:6]),
            bot_id='all' if ex.get('kind') == 'fact' else bot_id,
            embed=True,
        )
        facts_written += 1
    return {'ok': True, 'episode_id': ep.get('id', ''), 'facts_written': facts_written}
