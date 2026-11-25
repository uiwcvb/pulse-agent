"""Unified 4-Source Cross-Compaction Hybrid Recall Engine.

Combines:
1. LongTermMemory (`memories_v2`, full active scan — no 200-row cap)
2. Memory Vault Markdown notes (`notes/*.md`, `profile/*.md`, excluding noisy raw logs)
3. Archived historical messages & prior compact summaries (`messages_store`, excluding active window)
4. Entity-aware boosting (card digits, domains, task/post IDs, capitalized/proper nouns)
5. Context-aware query expansion for short / anaphoric follow-ups ("继续刚才那个", "之前那张卡呢")
6. RRF (Reciprocal Rank Fusion) across lexical BM25/2-gram + entity hits + optional cached embeddings.
"""
from __future__ import annotations

import math
import re
import time
from typing import Any, Iterable

from core.long_term_memory import _extract_terms

_ANAPHORA_RE = re.compile(
    r'(?:刚才|之前|上次|前面|上面|那个|那张|那条|那份|这个|继续|接着|还记得|提过|说过|约好|定的|怎么说|进展)'
)
_ENTITY_RE = re.compile(
    r'(?:bb-\d+|ctx-[a-z0-9]+|[a-zA-Z0-9-]+\.(?:com|cn|org|net|io|ai|cc|dev|md|py|kt)\b|\b\d{4,6}\b|[A-Z][a-zA-Z0-9_+-]{2,24})'
)


def extract_entities(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in _ENTITY_RE.findall(text or ''):
        low = m.lower()
        if low in seen or low in ('http', 'https', 'true', 'false', 'none', 'null'):
            continue
        seen.add(low)
        out.append(m)
        if len(out) >= 12:
            break
    return out


def expand_recall_query(current_query: str, recent_turns: list[dict[str, Any]] | None = None) -> tuple[str, list[str]]:
    """Expand short or anaphoric user messages with terms/entities from the last 2-3 turns."""
    q = (current_query or '').strip()
    entities = extract_entities(q)
    if not q:
        return '', []
    need_expand = len(q) < 36 or bool(_ANAPHORA_RE.search(q))
    if not need_expand or not recent_turns:
        return q, entities

    ctx_snippets: list[str] = []
    for m in reversed(recent_turns[-6:]):
        if not isinstance(m, dict):
            continue
        role = str(m.get('role') or '')
        if role not in ('user', 'assistant'):
            continue
        raw_c = m.get('content')
        if isinstance(raw_c, list):
            txt = ' '.join(str(p.get('text') or '') for p in raw_c if isinstance(p, dict) and p.get('type') == 'text')
        else:
            txt = str(raw_c or m.get('text') or '')
        txt = txt.strip()
        if not txt or txt == q or '核心进展与上下文摘要' in txt or '<recalled_' in txt:
            continue
        ctx_snippets.append(txt[:260])
        if len(ctx_snippets) >= 3:
            break

    if not ctx_snippets:
        return q, entities
    joined_ctx = ' '.join(ctx_snippets)
    ctx_terms = _extract_terms(joined_ctx)[:8]
    ctx_ents = extract_entities(joined_ctx)
    merged_ents = list(dict.fromkeys([*entities, *ctx_ents]))[:12]
    extra = ' '.join(t for t in [*ctx_ents[:4], *ctx_terms] if t.lower() not in q.lower())
    expanded = f'{q} {extra}'.strip() if extra else q
    return expanded[:500], merged_ents


def _lexical_score(hay_lower: str, terms: list[str], entities: list[str], raw_query_lower: str) -> float:
    if not hay_lower:
        return 0.0
    score = 0.0
    if raw_query_lower and len(raw_query_lower) >= 3 and raw_query_lower in hay_lower:
        score += 6.0
    hit_terms = sum(1 for t in terms if t in hay_lower)
    if hit_terms:
        coverage = hit_terms / max(1.0, min(len(terms), 6))
        score += hit_terms * 1.3 + coverage * 2.4
    hit_ents = sum(1 for e in entities if e.lower() in hay_lower)
    if hit_ents:
        score += hit_ents * 3.8
    return score


def _search_vault_notes(
    vault: Any,
    query: str,
    terms: list[str],
    entities: list[str],
    *,
    limit: int = 4,
) -> list[dict[str, Any]]:
    if vault is None or not (terms or entities):
        return []
    q_low = query.strip().lower()
    scored: list[tuple[float, dict[str, Any]]] = []
    try:
        files = vault.files()
    except Exception:
        return []
    for f in files:
        p = str(f.get('path') or '')
        if not p or p == 'INDEX.md' or p.startswith(('experiences/', 'timeline/', 'conversations/')):
            continue
        if p.startswith('notes/compact_'):
            continue
        try:
            content = vault.read(p)
        except Exception:
            continue
        if not content:
            continue
        hay = (p + '\n' + content[:12000]).lower()
        sc = _lexical_score(hay, terms, entities, q_low)
        if sc < 2.2:
            continue
        matched_lines: list[str] = []
        for ln in content.splitlines():
            ln_s = ln.strip()
            if not ln_s or ln_s.startswith('---'):
                continue
            ln_low = ln_s.lower()
            if any(e.lower() in ln_low for e in entities) or any(t in ln_low for t in terms[:8]):
                matched_lines.append(ln_s[:180])
                if len(matched_lines) >= 4:
                    break
        excerpt = ' | '.join(matched_lines) if matched_lines else content.strip()[:260]
        scored.append((sc, {
            'source_type': 'vault_note',
            'key': f'vault:{p}',
            'badge': f'笔记:{p}',
            'title': p,
            'body': excerpt[:380],
            'score': sc,
        }))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [x[1] for x in scored[:limit]]


def _search_archived_messages(
    context_search: Any,
    query: str,
    terms: list[str],
    entities: list[str],
    *,
    bot_id: str,
    exclude_msg_ids: Iterable[str] | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    if context_search is None or not query.strip():
        return []
    try:
        res = context_search.conversation_search(
            query=query,
            bot_id=bot_id,
            limit=max(limit + 2, 5),
            exclude_msg_ids=set(exclude_msg_ids or ()),
            vault=None,
        )
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    q_low = query.strip().lower()
    for r in (res.get('results') or []):
        txt = str(r.get('text') or '').strip()
        if not txt or len(txt) < 12:
            continue
        sc = float(r.get('score') or 0.0) + _lexical_score(txt.lower(), terms, entities, q_low) * 0.5
        if sc < 3.5:
            continue
        from vault_manager import fmt_compact_ts
        t_str = fmt_compact_ts(r.get('created_at') or r.get('time'), include_time=True) or str(r.get('time') or '')[:16]
        speaker = str(r.get('speaker') or r.get('role') or '')
        out.append({
            'source_type': 'archived_msg',
            'key': f"msg:{r.get('msg_id') or len(out)}",
            'badge': f'历史:{t_str} {speaker}'.strip(),
            'title': txt.splitlines()[0][:90],
            'body': txt[:360],
            'score': sc,
        })
        if len(out) >= limit:
            break
    return out


_TRIVIAL_ACK_RE = re.compile(
    r'^(?:好|好的|好呀|好哒|嗯|嗯嗯|行|行吧|可以|收到|明白|了解|知道了|谢谢|感谢|快点|继续|接着|ok|okay|yes|yep|no|nope|thx|thanks|1|2|3|[。，！？、!?.~～\s]+)+$',
    re.IGNORECASE,
)


def unified_cross_compaction_recall(
    *,
    query: str,
    recent_turns: list[dict[str, Any]] | None = None,
    ltm: Any = None,
    vault: Any = None,
    context_search: Any = None,
    memory_tables: Any = None,
    bot_id: str = 'companion',
    exclude_msg_ids: Iterable[str] | None = None,
    limit: int = 5,
    max_chars: int = 2400,
) -> list[dict[str, Any]]:
    """Run multi-source hybrid recall with query expansion, 0ms cold-start guard, and RRF fusion."""
    raw_q = (query or '').strip()
    if not raw_q or (len(raw_q) <= 10 and _TRIVIAL_ACK_RE.fullmatch(raw_q) and not _ANAPHORA_RE.search(raw_q)):
        return []
    expanded_q, entities = expand_recall_query(raw_q, recent_turns)
    if not expanded_q or len(expanded_q) < 2:
        return []
    terms = _extract_terms(expanded_q)
    if not terms and not entities:
        return []

    lists_for_rrf: list[list[dict[str, Any]]] = []

    # 0. Durable 4-table structured entity_facts + episodes (single shared query vector + non-blocking warmup!)
    if memory_tables is not None:
        try:
            shared_q_vec = (
                memory_tables.embed_query_fast(expanded_q, non_blocking_warmup=True)
                if hasattr(memory_tables, 'embed_query_fast')
                else None
            )
            fact_hits = memory_tables.query_facts(
                query=expanded_q, limit=max(limit, 6), query_vec=shared_q_vec, non_blocking_warmup=True,
            )
            from vault_manager import fmt_compact_ts
            norm_facts: list[dict[str, Any]] = []
            for f in fact_hits:
                ft = fmt_compact_ts(f.get('updated_at') or f.get('created_at'), include_time=False)
                ft_s = f' {ft}' if ft else ''
                norm_facts.append({
                    'source_type': 'entity_fact',
                    'key': f"ef:{f.get('id')}",
                    'badge': f"事实:{f.get('entity', '')}{ft_s}",
                    'title': f"{f.get('entity', '')} · {f.get('attr', '')}",
                    'body': str(f.get('value') or '')[:380],
                    'score': float(f.get('score') or 0.0),
                })
            if norm_facts:
                lists_for_rrf.append(norm_facts)
                if hasattr(memory_tables, 'query_relations'):
                    rel_seeds = list(dict.fromkeys([
                        *(str(f.get('entity') or '') for f in fact_hits[:5]),
                        *entities[:5],
                    ]))
                    rels = memory_tables.query_relations(rel_seeds, limit=5)
                    if rels:
                        rel_lines = [f"{r['src']} -[{r['relation']}]-> {r['dst']}" for r in rels]
                        lists_for_rrf.append([{
                            'source_type': 'entity_relation',
                            'key': 'er:graph',
                            'badge': '实体关联',
                            'title': '关联关系 (1-hop)',
                            'body': '；'.join(rel_lines)[:340],
                            'score': 4.0,
                        }])
            ep_hits = memory_tables.query_episodes(
                expanded_q, bot_id=bot_id, limit=3, query_vec=shared_q_vec, non_blocking_warmup=True,
            )
            norm_eps: list[dict[str, Any]] = []
            for ep in ep_hits:
                ctx_n = f' (ctx_id="{ep["ctx_id"]}")' if ep.get('ctx_id') else ''
                ept = fmt_compact_ts(ep.get('occurred_at') or ep.get('created_at'), include_time=False)
                norm_eps.append({
                    'source_type': 'episode',
                    'key': f"ep:{ep.get('id')}",
                    'badge': f'情景:{ept}' if ept else '情景',
                    'title': f"{ep.get('title', '')}{ctx_n}",
                    'body': str(ep.get('summary') or '')[:340],
                    'score': float(ep.get('score') or 0.0),
                })
            if norm_eps:
                lists_for_rrf.append(norm_eps)
        except Exception:
            pass

    # 1. Long-term memory (facts, preferences, experiences)
    if ltm is not None:
        try:
            ltm_hits = ltm.recall(expanded_q, bot_id=bot_id, limit=max(limit, 5), max_chars=max_chars)
            norm_ltm: list[dict[str, Any]] = []
            for it in ltm_hits:
                ctx_note = f' (原文 ctx_id="{it["l2_ctx_id"]}")' if it.get('l2_ctx_id') else ''
                url_note = (' | URL: ' + ', '.join(it.get('source_urls', [])[:2])) if it.get('source_urls') else ''
                norm_ltm.append({
                    'source_type': 'ltm',
                    'key': f"ltm:{it.get('id')}",
                    'badge': f"{it.get('kind', 'experience')}:{it.get('outcome', 'success')}",
                    'title': f"{it.get('l0_summary', '')}{ctx_note}{url_note}",
                    'body': str(it.get('l1_overview') or '')[:380],
                    'score': float(it.get('score') or 0.0),
                    'ctx_id': str(it.get('l2_ctx_id') or ''),
                })
            if norm_ltm:
                lists_for_rrf.append(norm_ltm)
        except Exception:
            pass

    # 2. Memory vault notes & profile files
    note_hits = _search_vault_notes(vault, expanded_q, terms, entities, limit=4)
    if note_hits:
        lists_for_rrf.append(note_hits)

    # 3. Archived pre-compaction messages
    msg_hits = _search_archived_messages(
        context_search,
        expanded_q,
        terms,
        entities,
        bot_id=bot_id,
        exclude_msg_ids=exclude_msg_ids,
        limit=3,
    )
    if msg_hits:
        lists_for_rrf.append(msg_hits)

    if not lists_for_rrf:
        return []

    # RRF fusion with light source-priority weighting
    k_rrf = 30.0
    src_weight = {'entity_fact': 1.45, 'entity_relation': 1.28, 'ltm': 1.25, 'episode': 1.20, 'vault_note': 1.15, 'archived_msg': 0.95}
    fused: dict[str, tuple[float, dict[str, Any]]] = {}
    for rank_list in lists_for_rrf:
        for rank_idx, item in enumerate(rank_list, 1):
            w = src_weight.get(item['source_type'], 1.0)
            rrf_sc = w / (k_rrf + rank_idx) + min(0.05, float(item.get('score') or 0.0) * 0.002)
            key = item['key']
            prev = fused.get(key)
            if prev is None:
                fused[key] = (rrf_sc, item)
            else:
                fused[key] = (prev[0] + rrf_sc, prev[1])

    ranked = sorted(fused.values(), key=lambda x: x[0], reverse=True)
    picked: list[dict[str, Any]] = []
    used = 0
    for rrf_sc, item in ranked:
        if len(picked) >= limit:
            break
        est = len(item['title']) + len(item['body']) + 60
        if picked and used + est > max_chars:
            break
        used += est
        picked.append({**item, 'rrf_score': round(rrf_sc, 5)})
    return picked


def format_unified_recall_block(items: list[dict[str, Any]]) -> str:
    if not items:
        return ''
    lines = [
        '<recalled_memory hint="本轮自动从长期经验、Vault 笔记与已压缩历史对话中召回的高相关记忆；如需完整原文可调 memory(op=\'ctx\', ctx_id=...) 或 read_file">'
    ]
    for idx, it in enumerate(items, 1):
        lines.append(f"{idx}. [{it['badge']}] **{it['title']}**")
        if it.get('body'):
            lines.append(f"   {it['body']}")
    lines.append('</recalled_memory>')
    return '\n'.join(lines)
