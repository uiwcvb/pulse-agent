"""
[Module Responsibility]
Real Vector Semantic Space Projection, Sparse Cosine-Similarity Graph & Keyword Cluster Engine
(`server/core/memory_vector_space.py`).

Projects active `entity_facts` / `episodes` (and legacy `memories_v2` fallback) from `memory_vectors`
into a 2D semantic canvas using lightweight centered PCA + anti-overlap refinement, computes sparse
top-K cosine-similarity edges, extracts keyword anchors with spatial centroids, and projects live
search queries as real-time vector probes into the same semantic space.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from typing import Any

from core.embedding_engine import cosine_similarity_blobs, unpack_int8_vector

_STOPWORDS = frozenset({
    '的', '了', '在', '是', '我', '你', '他', '她', '它', '们', '这', '那', '和', '与', '或', '及',
    '把', '被', '让', '给', '从', '到', '对', '为', '以', '于', '中', '上', '下', '里', '后', '前',
    '有', '无', '不', '没', '也', '都', '就', '还', '会', '能', '要', '将', '可', '已', '已经',
    '进行', '使用', '通过', '支持', '可以', '需要', '相关', '以及', '或者', '如果', '因为', '所以',
    '但是', '不过', '然后', '现在', '目前', '一个', '一种', '这个', '那个', '自己', '用户', '主人',
    '记忆', '记录', '事实', '经验', '状态', '信息', '内容', '问题', '方式', '方法', '情况', '时候',
    'the', 'and', 'for', 'with', 'that', 'this', 'from', 'have', 'are', 'was', 'were', 'not',
    'http', 'https', 'www', 'com', 'org', 'net', 'true', 'false', 'none', 'null', 'all', 'phone',
    'companion', 'auto', 'stateful', 'permanent', 'project', 'ephemeral', 'experience', 'fact',
})

_ASCII_TOKEN_RE = re.compile(r'[A-Za-z][A-Za-z0-9_.\-]{1,28}')
_CJK_SPAN_RE = re.compile(r'[\u4e00-\u9fff]{2,8}')

_SPACE_CACHE: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}


def _extract_keywords(text: str, *, entity: str = '', attr: str = '', cues: str = '', max_k: int = 5) -> list[str]:
    """Extract clean, human-readable bilingual keywords for a memory item."""
    out: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        t = str(tok or '').strip(' ·:：,，;；()（）[]【】"\'`-_/\\')
        if not t or len(t) < 2 or len(t) > 24:
            return
        low = t.lower()
        if low in _STOPWORDS or low in seen or t.isdigit():
            return
        if re.fullmatch(r'特征_[0-9a-f]{4,8}', t):
            return
        seen.add(low)
        out.append(t)

    if entity:
        _add(entity)
    if attr and not re.fullmatch(r'特征_[0-9a-f]{4,8}', attr):
        _add(attr)
    for part in re.split(r'[\s,，;；|/]+', str(cues or '')):
        if part and not part.startswith('http'):
            _add(part)

    raw = f'{entity} {attr} {cues} {text}'
    for m in _ASCII_TOKEN_RE.findall(raw):
        _add(m)
        if len(out) >= max_k + 4:
            break

    for span in _CJK_SPAN_RE.findall(text):
        if len(span) <= 5:
            _add(span)
        else:
            # Split longer CJK phrases into meaningful 2-4 char anchors without flooding
            _add(span[:4])
            if len(span) >= 6:
                _add(span[-4:])
        if len(out) >= max_k + 6:
            break

    return out[:max_k]


def _deterministic_xy(seed: str) -> tuple[float, float]:
    h = hashlib.sha1(seed.encode('utf-8')).digest()
    a = int.from_bytes(h[:4], 'big') / 0xFFFFFFFF
    b = int.from_bytes(h[4:8], 'big') / 0xFFFFFFFF
    return (0.14 + 0.72 * a, 0.14 + 0.72 * b)


def _normalize_coords(coords: list[list[float]]) -> list[list[float]]:
    if not coords:
        return []
    xs = [p[0] for p in coords]
    ys = [p[1] for p in coords]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    rx = max(1e-6, max_x - min_x)
    ry = max(1e-6, max_y - min_y)
    return [
        [
            round(0.09 + 0.82 * ((p[0] - min_x) / rx if rx > 1e-5 else 0.5), 4),
            round(0.11 + 0.78 * ((p[1] - min_y) / ry if ry > 1e-5 else 0.5), 4),
        ]
        for p in coords
    ]


def _relax_positions(coords: list[list[float]], edges: list[dict[str, Any]], id_to_idx: dict[str, int]) -> list[list[float]]:
    """Lightweight repulsion + edge-spring pass so dense clusters stay legible on mobile Canvas."""
    n = len(coords)
    if n <= 1:
        return [[0.5, 0.5]] if n == 1 else []
    pts = [[float(p[0]), float(p[1])] for p in coords]
    min_dist = max(0.055, min(0.11, 0.85 / math.sqrt(max(n, 4))))
    steps = 14 if n <= 140 else 8
    for _ in range(steps):
        disp = [[0.0, 0.0] for _ in range(n)]
        for i in range(n):
            xi, yi = pts[i]
            for j in range(i + 1, n):
                dx = xi - pts[j][0]
                dy = yi - pts[j][1]
                d2 = dx * dx + dy * dy
                if d2 < min_dist * min_dist:
                    d = math.sqrt(d2) if d2 > 1e-9 else 1e-4
                    push = (min_dist - d) * 0.42
                    ux, uy = (dx / d, dy / d) if d2 > 1e-9 else (math.cos(i + j), math.sin(i + j))
                    disp[i][0] += ux * push
                    disp[i][1] += uy * push
                    disp[j][0] -= ux * push
                    disp[j][1] -= uy * push
        for e in edges:
            ia = id_to_idx.get(e['source'])
            ib = id_to_idx.get(e['target'])
            if ia is None or ib is None:
                continue
            dx = pts[ib][0] - pts[ia][0]
            dy = pts[ib][1] - pts[ia][1]
            d = math.sqrt(dx * dx + dy * dy) + 1e-6
            target_d = max(min_dist * 1.15, 0.24 * (1.0 - min(0.85, float(e['weight']))))
            pull = (d - target_d) * 0.06 * float(e['weight'])
            ux, uy = dx / d, dy / d
            disp[ia][0] += ux * pull
            disp[ia][1] += uy * pull
            disp[ib][0] -= ux * pull
            disp[ib][1] -= uy * pull
        for i in range(n):
            pts[i][0] = max(0.07, min(0.93, pts[i][0] + max(-0.04, min(0.04, disp[i][0]))))
            pts[i][1] = max(0.09, min(0.91, pts[i][1] + max(-0.04, min(0.04, disp[i][1]))))
    return _normalize_coords(pts)


def _load_memory_items(memory_engine: Any, limit: int = 160) -> tuple[list[dict[str, Any]], dict[str, bytes], str, float]:
    tables = memory_engine.get_memory_tables()
    eng = getattr(tables, 'embedding_engine', None)
    model_id = eng.active_model_id() if eng is not None else 'off'
    now = time.time()
    items: list[dict[str, Any]] = []
    vec_map: dict[str, bytes] = {}
    max_upd = 0.0

    with tables._lock:
        f_rows = tables._conn.execute(
            """SELECT id, canonical_entity, aliases_json, attr, value, ttl_type, fact_kind,
                      search_cues, access_count, updated_at, created_at, bot_id
               FROM entity_facts
               WHERE status='active' AND (expires_at IS NULL OR expires_at > ?)
               ORDER BY access_count DESC, updated_at DESC LIMIT ?""",
            (now, limit),
        ).fetchall()
        for r in f_rows:
            upd = float(r['updated_at'] or r['created_at'] or 0.0)
            max_upd = max(max_upd, upd)
            ent = str(r['canonical_entity'] or '').strip()
            attr = str(r['attr'] or '').strip()
            val = str(r['value'] or '').strip()
            cues = str(r['search_cues'] or '').strip()
            label = f'{ent} · {attr}' if (ent and attr and not re.fullmatch(r'特征_[0-9a-f]{4,8}', attr)) else (ent or val[:18])
            kws = _extract_keywords(val, entity=ent, attr=attr, cues=cues, max_k=5)
            items.append({
                'id': r['id'],
                'item_type': 'fact',
                'entity': ent,
                'attr': attr,
                'label': label[:34],
                'summary': val[:260],
                'full_text': val,
                'ttl_type': str(r['ttl_type'] or 'stateful'),
                'fact_kind': str(r['fact_kind'] or 'core_fact'),
                'access_count': int(r['access_count'] or 0),
                'updated_at': upd,
                'keywords': kws,
                'search_cues': cues,
            })

        rem = max(24, limit - len(items))
        e_rows = tables._conn.execute(
            """SELECT id, title, summary, entities_json, tier, source, bot_id, occurred_at, created_at
               FROM episodes
               WHERE status='active'
               ORDER BY occurred_at DESC LIMIT ?""",
            (rem,),
        ).fetchall()
        for r in e_rows:
            upd = float(r['occurred_at'] or r['created_at'] or 0.0)
            max_upd = max(max_upd, upd)
            title = str(r['title'] or '').strip()
            summ = str(r['summary'] or '').strip()
            try:
                ents = [str(x).strip() for x in json.loads(r['entities_json'] or '[]') if str(x).strip()]
            except Exception:
                ents = []
            raw_kw = ' '.join(ents)
            kws = _extract_keywords(f'{title} {summ}', cues=raw_kw, max_k=5)
            items.append({
                'id': r['id'],
                'item_type': 'episode',
                'entity': title[:24] or '历史事件',
                'attr': str(r['tier'] or 'L0_detail'),
                'label': (title or summ[:24])[:34],
                'summary': summ[:260],
                'full_text': summ,
                'ttl_type': str(r['tier'] or 'L0_detail'),
                'fact_kind': 'episode',
                'access_count': 0,
                'updated_at': upd,
                'keywords': kws,
                'search_cues': raw_kw,
            })

        if model_id and model_id != 'off':
            v_rows = tables._conn.execute(
                'SELECT item_type, item_id, vec_blob, updated_at FROM memory_vectors WHERE model_id=? AND active=1',
                (model_id,),
            ).fetchall()
            for vr in v_rows:
                vec_map[f"{vr['item_type']}:{vr['item_id']}"] = bytes(vr['vec_blob'])
                max_upd = max(max_upd, float(vr['updated_at'] or 0.0))

    # Fallback: if new 5+1 tables are still sparse, include active legacy `memories_v2` so users see real memories immediately
    if len(items) < 8 and getattr(memory_engine, 'long_term_memory', None) is not None:
        try:
            legacy = memory_engine.long_term_memory.list_active(limit=max(20, min(60, limit - len(items))))
            seen_ids = {it['id'] for it in items}
            for lr in legacy:
                lid = str(lr.get('id') or '')
                if not lid or lid in seen_ids:
                    continue
                upd = float(lr.get('updated_at') or lr.get('created_at') or 0.0)
                max_upd = max(max_upd, upd)
                topic = str(lr.get('topic') or '').strip()
                l0 = str(lr.get('l0_summary') or '').strip()
                l1 = str(lr.get('l1_overview') or '').strip()
                kw_raw = str(lr.get('keywords') or '').strip()
                kws = _extract_keywords(f'{l0} {l1}', entity=topic, cues=kw_raw, max_k=5)
                items.append({
                    'id': lid,
                    'item_type': 'legacy',
                    'entity': topic or '待整理记忆',
                    'attr': str(lr.get('kind') or 'experience'),
                    'label': (topic or l0[:24])[:34],
                    'summary': (l0 or l1)[:260],
                    'full_text': l1 or l0,
                    'ttl_type': 'legacy',
                    'fact_kind': str(lr.get('kind') or 'experience'),
                    'access_count': int(lr.get('recall_count') or 0),
                    'updated_at': upd,
                    'keywords': kws,
                    'search_cues': kw_raw,
                })
        except Exception:
            pass

    return items[:limit], vec_map, model_id, max_upd


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _unit_vec(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _pure_python_pca_2d(unit_vecs: list[list[float]]) -> tuple[list[list[float]], dict[str, Any]]:
    """Zero-dependency 2D PCA via centered power iteration (works even without numpy)."""
    m = len(unit_vecs)
    d = len(unit_vecs[0]) if m else 0
    mean_v = [sum(v[k] for v in unit_vecs) / max(1, m) for k in range(d)]
    centered = [[v[k] - mean_v[k] for k in range(d)] for v in unit_vecs]

    def _top_pc(mat: list[list[float]], seed_idx: int) -> list[float]:
        w = [math.sin((k + 1) * (seed_idx + 1.37)) for k in range(d)]
        w = _unit_vec(w)
        for _ in range(12):
            scores = [_dot(row, w) for row in mat]
            nxt = [0.0] * d
            for row, sc in zip(mat, scores):
                for k in range(d):
                    nxt[k] += row[k] * sc
            norm = math.sqrt(sum(x * x for x in nxt))
            if norm < 1e-9:
                break
            w = [x / norm for x in nxt]
        return w

    pc1 = _top_pc(centered, 1)
    proj1 = [_dot(row, pc1) for row in centered]
    deflated = [[row[k] - p * pc1[k] for k in range(d)] for row, p in zip(centered, proj1)]
    pc2 = _top_pc(deflated, 2)
    proj2 = [_dot(row, pc2) for row in deflated]
    coords = [[proj1[i], proj2[i]] for i in range(m)]
    pmin = [min(proj1), min(proj2)]
    prng = [max(1e-6, max(proj1) - pmin[0]), max(1e-6, max(proj2) - pmin[1])]
    return coords, {'mean': mean_v, 'basis': [pc1, pc2], 'pmin': pmin, 'prng': prng, 'pure': True}


def _build_base_space(memory_engine: Any, limit: int = 160) -> dict[str, Any]:
    tables = memory_engine.get_memory_tables()
    items, vec_map, model_id, max_upd = _load_memory_items(memory_engine, limit=limit)
    cache_key = str(getattr(tables, 'db_path', 'mem'))
    sig = (len(items), len(vec_map), model_id, round(max_upd, 2), limit)
    cached = _SPACE_CACHE.get(cache_key)
    if cached is not None and cached[0] == sig:
        return cached[1]

    n = len(items)
    id_to_idx = {it['id']: idx for idx, it in enumerate(items)}
    unpacked: dict[int, list[float]] = {}
    for idx, it in enumerate(items):
        blob = vec_map.get(f"{it['item_type']}:{it['id']}")
        if blob:
            vec = unpack_int8_vector(blob)
            if vec:
                unpacked[idx] = vec
                it['has_vector'] = True
                continue
        it['has_vector'] = False

    raw_coords: list[list[float]] = [list(_deterministic_xy(it['id'])) for it in items]
    edges_map: dict[tuple[str, str], dict[str, Any]] = {}
    pca_basis: dict[str, Any] | None = None

    vec_indices = sorted(unpacked.keys())
    if len(vec_indices) >= 2:
        try:
            import numpy as np  # type: ignore

            mat = np.array([unpacked[i] for i in vec_indices], dtype=np.float32)
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms < 1e-8] = 1.0
            unit = mat / norms

            # 1) Real 2D PCA semantic projection
            mean_v = unit.mean(axis=0)
            centered = unit - mean_v
            _, _, vt = np.linalg.svd(centered, full_matrices=False)
            basis = vt[:2] if vt.shape[0] >= 2 else np.vstack([vt[0], np.zeros_like(vt[0])])
            proj = centered @ basis.T
            pmin = proj.min(axis=0)
            pmax = proj.max(axis=0)
            prng = np.where((pmax - pmin) > 1e-6, pmax - pmin, 1.0)
            pca_basis = {'mean': mean_v, 'basis': basis, 'pmin': pmin, 'prng': prng, 'pure': False}
            for k, orig_idx in enumerate(vec_indices):
                raw_coords[orig_idx] = [float(proj[k, 0]), float(proj[k, 1])]

            sim_mat = (unit @ unit.T).tolist()
        except Exception:
            unit_list = [_unit_vec(unpacked[i]) for i in vec_indices]
            p_coords, pca_basis = _pure_python_pca_2d(unit_list)
            for k, orig_idx in enumerate(vec_indices):
                raw_coords[orig_idx] = p_coords[k]
            m_len = len(unit_list)
            sim_mat = [[0.0] * m_len for _ in range(m_len)]
            for r_i in range(m_len):
                sim_mat[r_i][r_i] = -1.0
                for c_j in range(r_i + 1, m_len):
                    s_val = _dot(unit_list[r_i], unit_list[c_j])
                    sim_mat[r_i][c_j] = s_val
                    sim_mat[c_j][r_i] = s_val

        upper_vals = [sim_mat[i][j] for i in range(len(vec_indices)) for j in range(i + 1, len(vec_indices))]
        sim_floor = 0.56
        if upper_vals and max(upper_vals) < 0.56:
            s_sorted = sorted(upper_vals)
            p72 = s_sorted[int(0.72 * (len(s_sorted) - 1))]
            sim_floor = max(0.22, p72)
        for row_i, orig_i in enumerate(vec_indices):
            row_pairs = [(sim_mat[row_i][col_j], col_j) for col_j in range(len(vec_indices)) if col_j != row_i]
            row_pairs.sort(key=lambda x: x[0], reverse=True)
            for w, col_j in row_pairs[:3]:
                if w < sim_floor:
                    break
                orig_j = vec_indices[col_j]
                a, b = items[orig_i]['id'], items[orig_j]['id']
                key = (a, b) if a < b else (b, a)
                prev = edges_map.get(key)
                if prev is None or w > prev['weight']:
                    shared = sorted(set(items[orig_i]['keywords']) & set(items[orig_j]['keywords']))[:3]
                    edges_map[key] = {
                        'source': key[0],
                        'target': key[1],
                        'weight': round(float(w), 3),
                        'kind': 'vector',
                        'shared_keywords': shared,
                    }

    # Keyword-overlap edges: complement vector links when nodes share explicit keywords (and fallback when vectors off)
    kw_sets = [set(k.lower() for k in it['keywords']) for it in items]
    for i in range(n):
        if not kw_sets[i]:
            continue
        cands: list[tuple[float, int, list[str]]] = []
        for j in range(n):
            if i == j or not kw_sets[j]:
                continue
            inter = kw_sets[i] & kw_sets[j]
            if not inter:
                continue
            union = kw_sets[i] | kw_sets[j]
            jac = len(inter) / max(1, len(union))
            score = min(0.95, 0.42 + jac * 0.53)
            shared_orig = [k for k in items[i]['keywords'] if k.lower() in inter][:3]
            cands.append((score, j, shared_orig))
        cands.sort(key=lambda t: t[0], reverse=True)
        for score, j, shared_orig in cands[:3]:
            a, b = items[i]['id'], items[j]['id']
            key = (a, b) if a < b else (b, a)
            if key not in edges_map:
                edges_map[key] = {
                    'source': key[0],
                    'target': key[1],
                    'weight': round(score, 3),
                    'kind': 'keyword',
                    'shared_keywords': shared_orig,
                }

    edges = sorted(edges_map.values(), key=lambda e: e['weight'], reverse=True)[: max(80, n * 3)]
    norm_coords = _normalize_coords(raw_coords)
    final_coords = _relax_positions(norm_coords, edges, id_to_idx)

    # Degree + top neighbors per node for instant client highlight
    deg: Counter[str] = Counter()
    for e in edges:
        deg[e['source']] += 1
        deg[e['target']] += 1

    nodes_out: list[dict[str, Any]] = []
    for idx, it in enumerate(items):
        x, y = final_coords[idx]
        nodes_out.append({
            **it,
            'x': x,
            'y': y,
            'degree': int(deg.get(it['id'], 0)),
        })

    # Keyword anchors positioned at the spatial centroid of matching vector nodes
    kw_nodes: dict[str, list[int]] = {}
    kw_display: dict[str, str] = {}
    for idx, it in enumerate(items):
        for kw in it['keywords']:
            low = kw.lower()
            kw_display.setdefault(low, kw)
            kw_nodes.setdefault(low, []).append(idx)

    kw_ranked = sorted(kw_nodes.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    keywords_out: list[dict[str, Any]] = []
    for low, idxs in kw_ranked[:28]:
        if len(idxs) < 2 and len(kw_ranked) > 12:
            continue
        cx = sum(final_coords[i][0] for i in idxs) / len(idxs)
        cy = sum(final_coords[i][1] for i in idxs) / len(idxs)
        keywords_out.append({
            'word': kw_display[low],
            'count': len(idxs),
            'x': round(cx, 4),
            'y': round(cy, 4),
            'node_ids': [items[i]['id'] for i in idxs],
        })
    if not keywords_out:
        for low, idxs in kw_ranked[:16]:
            cx = sum(final_coords[i][0] for i in idxs) / len(idxs)
            cy = sum(final_coords[i][1] for i in idxs) / len(idxs)
            keywords_out.append({
                'word': kw_display[low],
                'count': len(idxs),
                'x': round(cx, 4),
                'y': round(cy, 4),
                'node_ids': [items[i]['id'] for i in idxs],
            })

    t_stats = tables.stats()
    result = {
        'nodes': nodes_out,
        'edges': edges,
        'keywords': keywords_out,
        'vec_map': vec_map,
        'pca_basis': pca_basis,
        'stats': {
            'total_nodes': len(nodes_out),
            'vectorized_nodes': len(vec_indices),
            'edge_count': len(edges),
            'keyword_count': len(keywords_out),
            'embedding_model': model_id,
            'embedding_loaded': bool(t_stats.get('embedding_loaded', False)),
            'active_facts': int(t_stats.get('active_facts', 0)),
            'episodes': int(t_stats.get('episodes', 0)),
            'vectors': int(t_stats.get('vectors', 0)),
            'sleep_consolidate_idle_seconds': int(memory_engine.get_sleep_consolidate_idle_seconds()),
        },
    }
    _SPACE_CACHE[cache_key] = (sig, result)
    return result


def build_vector_space_payload(memory_engine: Any, *, query: str = '', limit: int = 160) -> dict[str, Any]:
    """Public entry for `/api/memory/vector-space`: cached 2D vector space + optional live query probe."""
    lim = max(20, min(300, int(limit or 160)))
    base = _build_base_space(memory_engine, limit=lim)
    q = str(query or '').strip()
    probe: dict[str, Any] | None = None

    if q and base['nodes']:
        tables = memory_engine.get_memory_tables()
        q_blob = tables.embed_query_fast(q, non_blocking_warmup=False)
        q_low = q.lower()
        q_terms = _extract_keywords(q, max_k=8)
        vec_map: dict[str, bytes] = base['vec_map']
        scored: list[dict[str, Any]] = []
        for nd in base['nodes']:
            v_sim = 0.0
            if q_blob:
                nb = vec_map.get(f"{nd['item_type']}:{nd['id']}")
                if nb:
                    v_sim = max(0.0, float(cosine_similarity_blobs(q_blob, nb)))
            hay = f"{nd['label']} {nd['summary']} {' '.join(nd['keywords'])} {nd.get('search_cues', '')}".lower()
            lex = 0.0
            if len(q_low) >= 2 and q_low in hay:
                lex += 0.45
            if q_terms:
                hits = sum(1 for t in q_terms if t.lower() in hay)
                lex += min(0.45, (hits / max(1, len(q_terms))) * 0.45)
            score = min(0.99, (v_sim * 0.78 + lex * 0.42) if v_sim > 0 else lex)
            if score >= 0.22:
                scored.append({
                    'node_id': nd['id'],
                    'score': round(score, 3),
                    'vec_sim': round(v_sim, 3),
                    'lex_score': round(lex, 3),
                })
        scored.sort(key=lambda x: x['score'], reverse=True)
        top_matches = scored[:12]

        # Project query probe onto the exact 2D canvas
        px, py = 0.5, 0.5
        if top_matches:
            by_id = {nd['id']: nd for nd in base['nodes']}
            w_sum = sum(m['score'] for m in top_matches[:5]) or 1.0
            px = sum(by_id[m['node_id']]['x'] * m['score'] for m in top_matches[:5]) / w_sum
            py = sum(by_id[m['node_id']]['y'] * m['score'] for m in top_matches[:5]) / w_sum
            # Slight radial offset so the probe visibly shoots rays into its neighbors instead of sitting on top
            px = round(max(0.10, min(0.90, px + (0.04 if px < 0.5 else -0.04))), 4)
            py = round(max(0.12, min(0.88, py + (0.04 if py < 0.5 else -0.04))), 4)
        elif q_blob and base.get('pca_basis') is not None:
            try:
                pb = base['pca_basis']
                qv_raw = _unit_vec(unpack_int8_vector(q_blob))
                if pb.get('pure'):
                    c_vec = [qv_raw[k] - pb['mean'][k] for k in range(len(qv_raw))]
                    c0 = _dot(c_vec, pb['basis'][0])
                    c1 = _dot(c_vec, pb['basis'][1])
                    px = round(float(max(0.10, min(0.90, 0.09 + 0.82 * ((c0 - pb['pmin'][0]) / pb['prng'][0])))), 4)
                    py = round(float(max(0.12, min(0.88, 0.11 + 0.78 * ((c1 - pb['pmin'][1]) / pb['prng'][1])))), 4)
                else:
                    import numpy as np  # type: ignore

                    qv = np.array(qv_raw, dtype=np.float32)
                    coord = (qv - pb['mean']) @ pb['basis'].T
                    px = round(float(max(0.10, min(0.90, 0.09 + 0.82 * ((coord[0] - pb['pmin'][0]) / pb['prng'][0])))), 4)
                    py = round(float(max(0.12, min(0.88, 0.11 + 0.78 * ((coord[1] - pb['pmin'][1]) / pb['prng'][1])))), 4)
            except Exception:
                pass

        probe = {
            'query': q,
            'x': px,
            'y': py,
            'mode': 'vector+keyword' if q_blob else 'keyword',
            'matches': top_matches,
        }

    return {
        'ok': True,
        'nodes': base['nodes'],
        'edges': base['edges'],
        'keywords': base['keywords'],
        'probe': probe,
        'stats': base['stats'],
    }
