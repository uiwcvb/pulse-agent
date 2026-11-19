"""
[Module Responsibility]
原生 Agent 多步工具轨迹 (`turn_steps`) 序列化、配对校验与原子预算裁剪模块 (`server/core/turn_history.py`)。
将每轮工具调用按 OpenAI / DeepSeek 训练分布原生格式 (`assistant.tool_calls` + `role: "tool"`) 持久保存，
并在后续回合构造上下文时无损展开，避免伪造文本摘要导致工具调用退化，同时保证 98%~99% 前缀 KV 缓存命中。
"""
from __future__ import annotations

import json
import hashlib
from typing import Any, Callable

MAX_HIST_TOOL_CHARS = 4000
TURN_TRACE_ARCHIVE_LIMIT = 256
_SCRUB_TOOLS = frozenset({'write_file', 'edit_file', 'custom_tool'})
_SCRUB_FIELDS = ('content', 'old', 'new', 'code', 'script', 'patch')


def _scrub_large_write_payloads(tool_name: str, parsed: dict[str, Any]) -> dict[str, Any]:
    if tool_name not in _SCRUB_TOOLS or not isinstance(parsed, dict):
        return parsed
    out = dict(parsed)
    for fk in _SCRUB_FIELDS:
        val = out.get(fk)
        if isinstance(val, str) and len(val) > 12000:
            out[fk] = val[:4000] + f'\n... (已保留前后共 6000 字符，中间省略 {len(val) - 6000} 字符) ...\n' + val[-2000:]
    return out


def cap_historical_tool_output(raw_content: Any, limit: int = MAX_HIST_TOOL_CHARS) -> str:
    """Deterministically cap a historical `role: "tool"` output string so it never mutates across future turns."""
    s = str(raw_content or '').strip()
    if len(s) <= limit:
        return s or '{}'
    head = int(limit * 0.68)
    tail = max(200, limit - head - 64)
    omitted = len(s) - head - tail
    return f'{s[:head]}\n...[历史工具输出已定长省略 {omitted} 字符，可用 ctx_search 检索]...\n{s[-tail:]}'


def split_concatenated_json_objects(raw: Any) -> list[dict[str, Any]]:
    """Split single or concatenated JSON objects (e.g. `{"a":1}{"b":2}`) into a list of dicts."""
    if isinstance(raw, dict):
        return [raw]
    s = str(raw or '').strip()
    if not s:
        return []
    try:
        parsed = json.loads(s)
        if isinstance(parsed, dict):
            return [parsed]
        return []
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    idx = 0
    n = len(s)
    objs: list[dict[str, Any]] = []
    while idx < n:
        while idx < n and s[idx].isspace():
            idx += 1
        if idx >= n:
            break
        try:
            item, end_idx = decoder.raw_decode(s, idx)
        except ValueError:
            return []
        if not isinstance(item, dict):
            return []
        objs.append(item)
        idx = end_idx
    return objs


def expand_concatenated_tool_calls(raw_calls: list[dict] | None) -> list[dict]:
    """Expand any tool call whose `arguments` holds concatenated JSON objects `{...}{...}` into distinct tool calls."""
    if not raw_calls:
        return []
    expanded: list[dict] = []
    for idx, c in enumerate(raw_calls):
        if not isinstance(c, dict):
            continue
        fn = c.get('function') if isinstance(c.get('function'), dict) else {}
        name = str(fn.get('name') or c.get('name') or '').strip()
        if not name:
            expanded.append(c)
            continue
        orig_id = str(c.get('id') or f'call_exp_{idx}').strip()
        raw_args = fn.get('arguments')
        if isinstance(raw_args, str):
            split_objs = split_concatenated_json_objects(raw_args)
            if len(split_objs) >= 2:
                for sub_i, obj in enumerate(split_objs, start=1):
                    sub_id = orig_id if sub_i == 1 else f'{orig_id}_p{sub_i}'
                    expanded.append({
                        **c,
                        'id': sub_id,
                        'type': c.get('type') or 'function',
                        'function': {
                            **fn,
                            'name': name,
                            'arguments': json.dumps(obj, ensure_ascii=False),
                        },
                    })
                continue
        expanded.append(c)
    return expanded


def normalize_tool_calls_for_history(raw_calls: list[dict], scrub_large_payloads: bool = False) -> list[dict]:
    """Normalize `tool_calls` into strict OpenAI schema (`id`, `type`, `function.name`, `function.arguments` JSON string)."""
    out: list[dict] = []
    for idx, c in enumerate(expand_concatenated_tool_calls(raw_calls)):
        if not isinstance(c, dict):
            continue
        fn = c.get('function') if isinstance(c.get('function'), dict) else {}
        name = str(fn.get('name') or c.get('name') or '').strip()
        if not name:
            continue
        cid = str(c.get('id') or f'call_hist_{idx}').strip()
        raw_args = fn.get('arguments')
        if isinstance(raw_args, dict):
            obj = _scrub_large_write_payloads(name, raw_args) if scrub_large_payloads else raw_args
            args_str = json.dumps(obj, ensure_ascii=False, sort_keys=True)
        elif isinstance(raw_args, str):
            s = raw_args.strip()
            try:
                parsed = json.loads(s) if s else {}
                if isinstance(parsed, dict):
                    obj = _scrub_large_write_payloads(name, parsed) if scrub_large_payloads else parsed
                    args_str = json.dumps(obj, ensure_ascii=False, sort_keys=True)
                else:
                    args_str = s or '{}'
            except Exception:
                args_str = s or '{}'
        else:
            args_str = '{}'
        max_len = 12000 if scrub_large_payloads else 16000
        out.append({
            'id': cid,
            'type': 'function',
            'function': {
                'name': name,
                'arguments': args_str[:max_len],
            },
        })
    return out


def build_compact_turn_step(step_content: str, batch_calls: list[dict], tool_outputs: list[tuple[str, str]]) -> list[dict]:
    """
    Build one atomic OpenAI ReAct step (`[assistant(tool_calls), tool, ...]`) for persistence in `msg['turn_steps']`.
    Omits `reasoning_content` on completed past turns per DeepSeek Chat/Reasoner multi-turn specification.
    """
    norm_calls = normalize_tool_calls_for_history(batch_calls, scrub_large_payloads=True)
    if not norm_calls:
        return []
    out_by_id = {str(cid): cap_historical_tool_output(txt) for cid, txt in (tool_outputs or []) if cid}
    step_msgs: list[dict] = [
        {
            'role': 'assistant',
            'content': (step_content or '').strip()[:6000],
            'tool_calls': norm_calls,
        }
    ]
    for tc in norm_calls:
        cid = tc['id']
        content = out_by_id.get(cid)
        if content is None:
            content = json.dumps({'ok': False, 'interrupted': True, 'note': '该工具调用被中途打断'}, ensure_ascii=False)
        step_msgs.append({
            'role': 'tool',
            'tool_call_id': cid,
            'content': content,
        })
    return step_msgs


def append_turn_step(turn_steps: list[dict], step_msgs: list[dict]) -> None:
    """Append `step_msgs` into `turn_steps` while respecting `TURN_TRACE_ARCHIVE_LIMIT`."""
    if not step_msgs:
        return
    assistant_steps = sum(1 for m in turn_steps if isinstance(m, dict) and m.get('role') == 'assistant')
    if assistant_steps >= TURN_TRACE_ARCHIVE_LIMIT:
        return
    turn_steps.extend(step_msgs)


def expand_stored_turn_steps(raw_steps: Any, final_text: str) -> list[dict]:
    """
    Validate and expand stored `turn_steps` + `final_text` into a valid OpenAI message sequence.
    Guarantees every `assistant.tool_calls[i].id` is immediately followed by its matching `role: "tool"` message.
    """
    if not isinstance(raw_steps, list) or not raw_steps:
        return [{'role': 'assistant', 'content': final_text}] if final_text else []
    flat_steps: list[dict] = []
    for entry in raw_steps:
        if isinstance(entry, dict):
            flat_steps.append(entry)
        elif isinstance(entry, list):
            flat_steps.extend(x for x in entry if isinstance(x, dict))
    expanded: list[dict] = []
    i = 0
    n = len(flat_steps)
    while i < n:
        item = flat_steps[i]
        if item.get('type') == 'step_text' and item.get('text'):
            st = str(item.get('text') or '').strip()
            if st and st not in (final_text or ''):
                expanded.append({'role': 'assistant', 'content': st})
            i += 1
            continue
        if item.get('role') != 'assistant':
            i += 1
            continue
        norm_calls = normalize_tool_calls_for_history(item.get('tool_calls') or [])
        if not norm_calls:
            i += 1
            continue
        tool_map: dict[str, str] = {}
        j = i + 1
        while j < n and flat_steps[j].get('role') == 'tool':
            tc_id = str(flat_steps[j].get('tool_call_id') or '').strip()
            if tc_id:
                tool_map[tc_id] = str(flat_steps[j].get('content') or '{}')
            j += 1
        expanded.append({
            'role': 'assistant',
            'content': str(item.get('content') or ''),
            'tool_calls': norm_calls,
        })
        for tc in norm_calls:
            cid = tc['id']
            expanded.append({
                'role': 'tool',
                'tool_call_id': cid,
                'content': tool_map.get(cid) or '{"ok":false,"interrupted":true}',
            })
        i = j
    if final_text or not expanded:
        expanded.append({'role': 'assistant', 'content': final_text or '已执行完毕。'})
    return expanded


def slice_atomic_units_to_budget(
    units: list[list[dict]],
    budget_tokens: int,
    estimate_fn: Callable[[Any], int],
) -> list[dict]:
    """
    Select newest atomic message units within `budget_tokens`, never splitting an `assistant(tool_calls) -> tool` chain
    and ensuring the sequence starts cleanly on a `user` (or standalone `assistant` summary) message.
    """
    if not units:
        return []

    def _unit_tokens(u: list[dict]) -> int:
        tot = 0
        for m in u:
            tot += estimate_fn(m.get('content', '')) + 12
            if m.get('tool_calls'):
                tot += estimate_fn(m['tool_calls']) + 8
        return tot

    selected: list[list[dict]] = []
    used = 0
    for idx in range(len(units) - 1, -1, -1):
        u = units[idx]
        if not u:
            continue
        utok = _unit_tokens(u)
        if selected and (used + utok > budget_tokens):
            break
        selected.append(u)
        used += utok
    selected.reverse()
    # Ensure first unit doesn't begin with an assistant tool_calls block without preceding user context if avoidable
    while len(selected) > 1 and selected[0] and selected[0][0].get('role') == 'assistant' and selected[0][0].get('tool_calls'):
        selected.pop(0)
    flat: list[dict] = []
    for u in selected:
        flat.extend(u)
    return flat
