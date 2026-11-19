"""KV-Cache-Preserving Observation Masking & Ephemeral Tail UI State (Layers 1 & 2).

Implements:
1. Ephemeral Tail UI State (`extract_ephemeral_ui_from_result`):
   GUI / DOM snapshots (`ui_tree`, `dom_elements`, `page_text`, etc.) are moved out of persistent
   in-turn `role: "tool"` messages into an ephemeral per-step tail attachment (`call_messages[-1]`)
   so prior turn messages NEVER mutate and multi-turn GUI screenshots/trees do not bloat history.
2. Dual-Mode Action-Preserving Observation Masking (`maybe_mask_room_observations`):
   - Cold Cache (`idle >= 600s`): upstream KV cache already evicted -> mask older non-error tool
     outputs (> 350 chars) while preserving all `assistant.tool_calls` and the latest 2 turns.
   - Hot Cache (`usage >= 78%` threshold): amortized batch mask of oldest 65% of eligible tool
     outputs (preserving the most recent 4 tool steps) to prevent prefix cache thrashing.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from typing import Any

from storage import normalize_node_source

GUI_UI_TOOLS = frozenset({'cloud', 'phone', 'pc', 'phone_workspace', 'cloud_desktop', 'dsh_workspace', 'phone_macro'})
BULKY_UI_KEYS = (
    'dom_elements',
    'ui_tree',
    'elements',
    'nodes',
    'page_text',
    'accessibility_tree',
    'window_list',
    'windows',
    'x11_windows',
)
_ERROR_MARKERS = ('"ok": false', '"ok":false', 'traceback', 'exception', 'errno', 'permission denied', 'suppressed', '失败', '报错', '异常')
_MASK_PREFIX = '[已归档观察结果'


def _is_error_observation(content: str) -> bool:
    low = (content or '').lower()
    if len(low) <= 350:
        return True
    return any(tok in low for tok in _ERROR_MARKERS)


def _trim_receipt_json(receipt: dict[str, Any], limit: int = 8000) -> str:
    """Serialize structured tool receipt cleanly within generous `limit` chars without hollowing it out."""
    s = json.dumps(receipt, ensure_ascii=False)
    if len(s) <= limit:
        return s
    trimmed = dict(receipt)
    for shrink_k, max_len in (('page_text', 1200), ('stdout', 1500), ('output', 1500), ('note', 600), ('error', 800)):
        if len(s) <= limit:
            break
        if isinstance(trimmed.get(shrink_k), str) and len(trimmed[shrink_k]) > max_len:
            trimmed[shrink_k] = trimmed[shrink_k][:max_len] + '…'
            s = json.dumps(trimmed, ensure_ascii=False)
    return s if len(s) <= limit else s[: max(10, limit - 4)] + '...}'


def extract_ephemeral_ui_from_result(
    tool_name: str,
    result: Any,
    serialized_tool_content: str | None = None,
    max_tail_chars: int = 8000,
) -> tuple[str, str | None]:
    """
    If `tool_name` returned heavy GUI UI state (`dom_elements`, `ui_tree`, `page_text`, ...),
    keep a complete, structured receipt (up to 8000 chars, omitting only bulky DOM arrays)
    and return an ephemeral `<ephemeral_ui_state>` tail preserving `@bN`/`@eN` elements and coords.
    """
    parsed_res = result
    if isinstance(result, str):
        if serialized_tool_content is None:
            serialized_tool_content = result
        try:
            parsed_res = json.loads(result)
        except Exception:
            parsed_res = None
    if serialized_tool_content is None:
        try:
            serialized_tool_content = json.dumps(parsed_res, ensure_ascii=False) if parsed_res is not None else ''
        except Exception:
            serialized_tool_content = str(result or '')

    if tool_name not in GUI_UI_TOOLS or not isinstance(parsed_res, dict):
        return serialized_tool_content, None

    extracted: dict[str, Any] = {}
    total_bulky = 0
    for k in BULKY_UI_KEYS:
        if k in parsed_res and parsed_res[k]:
            try:
                raw_k = parsed_res[k] if isinstance(parsed_res[k], str) else json.dumps(parsed_res[k], ensure_ascii=False)
            except Exception:
                raw_k = str(parsed_res[k])
            total_bulky += len(raw_k)
            extracted[k] = raw_k

    has_meta_state = any(parsed_res.get(k) is not None for k in ('auth_gate', 'active_window'))
    if not extracted or (total_bulky <= 700 and len(serialized_tool_content) <= 1200 and not has_meta_state):
        return serialized_tool_content, None

    parts: list[str] = []
    meta_hdr = ' / '.join(
        f'{k}={parsed_res.get(k)}'
        for k in (
            'op', 'action', 'app', 'package', 'activity', 'window', 'active_window',
            'title', 'url', 'ui_changed', 'frame_diff_pct', 'clicked', 'control', 'user_takeover_locked',
        )
        if parsed_res.get(k) is not None and parsed_res.get(k) != ''
    )
    parts.append(f'<ephemeral_ui_state tool="{tool_name}" {meta_hdr}>')
    if parsed_res.get('auth_gate') is not None:
        parts.append(f"[auth_gate]: {json.dumps(parsed_res['auth_gate'], ensure_ascii=False)}")
    if parsed_res.get('active_window') is not None:
        parts.append(f"[active_window]: {json.dumps(parsed_res['active_window'], ensure_ascii=False) if not isinstance(parsed_res['active_window'], str) else parsed_res['active_window']}")

    budget = max(1200, int(max_tail_chars))
    # Prioritize DOM/UI elements first so 100% of @bN elements are preserved without truncation by page_text/windows
    priority_keys = [k for k in ('dom_elements', 'ui_tree', 'elements', 'nodes', 'accessibility_tree', 'page_text', 'x11_windows', 'windows', 'window_list') if k in extracted]
    for idx_k, k in enumerate(priority_keys):
        raw_k = extracted[k]
        if budget <= 120:
            break
        remaining = max(1, len(priority_keys) - idx_k)
        if k in ('dom_elements', 'ui_tree', 'elements', 'nodes'):
            take = min(len(raw_k), max(1200, int(budget * 0.72)))
        else:
            take = min(len(raw_k), max(250, budget // remaining))
        snippet = raw_k[:take]
        if len(raw_k) > take:
            snippet += f'... (+{len(raw_k) - take} chars)'
        parts.append(f'[{k}]:\n{snippet}')
        budget -= len(snippet)
    parts.append('</ephemeral_ui_state>')
    ephemeral_tail = '\n'.join(parts)

    receipt: dict[str, Any] = {}
    for k in (
        'ok', 'op', 'action', 'error', 'note', 'app', 'package', 'activity', 'window', 'active_window',
        'title', 'url', 'ui_changed', 'frame_diff_pct', 'clicked', 'typed', 'scrolled',
        'control', 'user_takeover_locked', 'running', 'timed_out', 'exit_code',
        'auth_gate', 'display_id', 'ctx_offloaded', 'ctx_id', '_image_attached',
        'saved_screenshot', 'chat_image_url', 'stdout', 'stderr', 'output', 'page_text',
    ):
        if k in parsed_res and parsed_res[k] not in (None, '', []):
            val = parsed_res[k]
            if isinstance(val, str):
                max_f = 2500 if k in ('page_text', 'stdout', 'stderr', 'output') else 800
                receipt[k] = val[:max_f]
            else:
                receipt[k] = val
    for k in BULKY_UI_KEYS:
        if k == 'page_text':
            continue
        v = parsed_res.get(k)
        if isinstance(v, list):
            receipt[f'{k}_count'] = len(v)
        elif isinstance(v, str) and v:
            receipt[f'{k}_chars'] = len(v)
    receipt['ui_state'] = '[实时UI树/DOM已完整挂载至当步末尾]'
    compact_str = _trim_receipt_json(receipt, limit=8000)
    return compact_str, ephemeral_tail


def maybe_mask_room_observations(
    store,
    memory_engine,
    bot_id: str,
    source: str = 'phone',
    *,
    now_ts: float | None = None,
    cold_idle_seconds: float = 600.0,
    hot_usage_ratio: float = 0.78,
) -> int:
    """
    Dual-mode observation masking on stored `msg['turn_steps']` before loading turn history.
    NEVER runs inside an active tool loop; returns count of masked tool observation entries.
    """
    now = float(now_ts if now_ts is not None else time.time())
    src = normalize_node_source(source or 'phone')
    msgs = store.state.get('messages') or []
    bot_msgs = [
        m for m in msgs
        if isinstance(m, dict) and m.get('bot_id', 'companion') == bot_id and normalize_node_source(m.get('source')) == src
    ]
    if len(bot_msgs) < 3:
        return 0

    last_ts = 0.0
    for m in reversed(bot_msgs):
        ts_val = float(m.get('created_at') or m.get('created') or m.get('ts') or 0)
        if ts_val > 0:
            last_ts = ts_val
            break
    idle_elapsed = max(0.0, now - last_ts) if last_ts > 0 else 0.0

    threshold = max(1, int(memory_engine.get_threshold_tokens() or 100_000))
    nb_key = f'{src}:{bot_id}'
    rep_pt = int(memory_engine.last_prompt_tokens.get(nb_key, memory_engine.last_prompt_tokens.get(bot_id, 0) if src == 'phone' else 0) or 0)
    usage_ratio = rep_pt / threshold

    is_cold = idle_elapsed >= cold_idle_seconds
    is_hot = usage_ratio >= hot_usage_ratio
    if not (is_cold or is_hot):
        return 0

    # Collect all maskable `(step_tool_msg, tool_name)` candidates except recent preserved tail
    # Keep the latest 2 messages' turn_steps completely untouched in cold mode,
    # or keep at least the latest 4 tool-call steps untouched in hot mode.
    eligible_entries: list[tuple[list, int, dict, str]] = []
    steps_seen: list[list[tuple[list, int, dict]]] = []
    for m in bot_msgs:
        ts = m.get('turn_steps')
        if m.get('role') != 'assistant' or not isinstance(ts, list) or not ts:
            continue
        if all(isinstance(step, list) for step in ts):
            for step in ts:
                grp = [(step, idx_s, it) for idx_s, it in enumerate(step) if isinstance(it, dict)]
                if grp:
                    steps_seen.append(grp)
        else:
            cur_grp: list[tuple[list, int, dict]] = []
            for idx_s, it in enumerate(ts):
                if not isinstance(it, dict):
                    continue
                if it.get('role') == 'assistant' and cur_grp:
                    steps_seen.append(cur_grp)
                    cur_grp = []
                cur_grp.append((ts, idx_s, it))
            if cur_grp:
                steps_seen.append(cur_grp)

    if len(steps_seen) <= 2:
        return 0

    keep_tail_steps = 2 if is_cold else min(len(steps_seen), 4)
    candidate_steps = steps_seen[:-keep_tail_steps] if keep_tail_steps > 0 else steps_seen

    for step_grp in candidate_steps:
        tc_names: dict[str, str] = {}
        for _, _, item in step_grp:
            if item.get('role') == 'assistant':
                for tc in item.get('tool_calls') or []:
                    if isinstance(tc, dict) and tc.get('id'):
                        tc_names[str(tc['id'])] = str((tc.get('function') or {}).get('name') or 'tool')
        for container, idx_s, item in step_grp:
            if item.get('role') == 'tool':
                raw_c = str(item.get('content') or '')
                if raw_c.startswith(_MASK_PREFIX) or _is_error_observation(raw_c):
                    continue
                tname = tc_names.get(str(item.get('tool_call_id') or ''), 'tool')
                eligible_entries.append((container, idx_s, item, tname))

    if not eligible_entries:
        return 0

    to_mask_count = len(eligible_entries) if is_cold else max(1, int(len(eligible_entries) * 0.65))
    chars_freed = 0
    masked = 0
    for container, idx_s, item, tname in eligible_entries[:to_mask_count]:
        old_c = str(item.get('content') or '')
        if len(old_c) <= 350:
            continue
        ctx_id = ''
        m_exist_cid = re.search(r'"ctx_id"\s*:\s*"([^"]+)"', old_c)
        if m_exist_cid:
            ctx_id = m_exist_cid.group(1).strip()
        cs = getattr(memory_engine, 'context_search', None)
        if not ctx_id and cs is not None:
            try:
                uniq_cid = str(item.get('tool_call_id') or secrets.token_hex(4))
                ctx_id = cs.store_tool_context(
                    f'masked_{tname}',
                    {'bot_id': bot_id, 'source': src, 'masked_from_tool': tname, 'call_id': uniq_cid, 'chars': len(old_c)},
                    old_c,
                    bot_id=bot_id,
                )
            except Exception:
                ctx_id = ''
        urls = re.findall(r'https?://[^\s<>"\')\]}]+', old_c)[:3]
        url_part = f" | URLs: {', '.join(urls)}" if urls else ''
        preview = ' '.join(old_c.splitlines())[:160].strip()
        ctx_part = f'ctx_id="{ctx_id}", ' if ctx_id else ''
        new_c = (
            f'{_MASK_PREFIX} · tool={tname}, {ctx_part}原长度={len(old_c)} chars{url_part} | '
            f'摘要={preview}…（参数与报错记录保留；详情可用 ctx_search 检索）]'
        )
        chars_freed += max(0, len(old_c) - len(new_c))
        copied_item = dict(item)
        copied_item['content'] = new_c
        container[idx_s] = copied_item
        masked += 1

    if masked > 0:
        tokens_freed = max(0, int(chars_freed // 3))
        if rep_pt > tokens_freed and tokens_freed > 0:
            new_pt = max(0, rep_pt - tokens_freed)
            memory_engine.last_prompt_tokens[nb_key] = new_pt
            st_pt = store.state.setdefault('last_prompt_tokens', {})
            st_pt[nb_key] = new_pt
            if src == 'phone':
                memory_engine.last_prompt_tokens[bot_id] = new_pt
                st_pt[bot_id] = new_pt
        store.save()
    return masked
