"""Sub-Trajectory Folding Runner (`run_subtask`, Layer 3 Context Anti-Bloat).

Runs an isolated research / verification / log-digging loop inside a disposable
`sub_messages` buffer (1 system + 1 user goal), discarding all intermediate verbose
tool outputs upon completion while indexing the raw trace into `context_search`
and returning only a clean structured summary (< 1800 chars) to the parent turn.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any

_DISALLOWED_SUBTASK_TOOLS = frozenset({
    'run_subtask',
    'notify_user',
    'emergency_alarm',
    'present_choices',
    'complete_goal',
    'finish_goal',
    'blackboard',
    'system_config',
    'deploy_cloud_computer',
})


async def execute_run_subtask(brain, args: dict[str, Any]) -> dict[str, Any]:
    goal = str((args or {}).get('goal') or '').strip()
    if not goal:
        return {'ok': False, 'error': 'run_subtask 需要明确的子任务目标 goal'}
    if int(getattr(brain, 'subtask_depth', 0) or 0) >= 1:
        return {'ok': False, 'error': '禁止在 run_subtask 内部递归调用 run_subtask'}

    max_steps = max(1, min(60, int((args or {}).get('max_steps') or 20)))
    raw_focus = (args or {}).get('focus_files')
    focus_list: list[str] = []
    if isinstance(raw_focus, list):
        focus_list = [str(x).strip() for x in raw_focus if str(x).strip()][:12]
    elif isinstance(raw_focus, str) and raw_focus.strip():
        focus_list = [raw_focus.strip()]

    bot_id = brain.bot_context.get() or 'companion'
    try:
        from assistants import get_assistant
        bot = get_assistant(brain.store, bot_id)
    except Exception:
        bot = {'id': bot_id, 'name': bot_id, 'tools': None}

    all_schemas = brain.harness.all_tools()
    plugin_names = set(brain.harness.extension_registry.tool_plugins.keys())
    caller_allowed = (
        set(bot.get('tools') or [t['function']['name'] for t in all_schemas])
        | plugin_names
        | {'system_status', 'ctx_search', 'memory_search', 'read_file', 'grep_search', 'glob_files', 'list_dir', 'run_bash'}
    )
    req_tools = (args or {}).get('allowed_tools')
    if isinstance(req_tools, list) and req_tools:
        caller_allowed &= {str(t).strip() for t in req_tools if str(t).strip()}
    sub_allowed = caller_allowed - _DISALLOWED_SUBTASK_TOOLS
    sub_tools = [t for t in all_schemas if t.get('function', {}).get('name') in sub_allowed]

    focus_hint = f'\n优先关注文件/目录：{", ".join(focus_list)}' if focus_list else ''
    sub_system = (
        '你是父 Agent 派出的独立子任务执行单元 (`run_subtask`)，运行在完全隔离的一次性上下文中。\n'
        '你的目标是以最少步骤精准查证、梳理代码或核验事实；完成后请直接用简洁结构化语言给出结论与关键文件/行号证据。\n'
        '不要向用户客套，只输出扎实的发现与证据。'
    )
    sub_messages: list[dict[str, Any]] = [
        {'role': 'system', 'content': sub_system},
        {'role': 'user', 'content': f'子任务目标：{goal}{focus_hint}'},
    ]

    sub_turn_id = 'sub-' + secrets.token_hex(5)
    t0 = time.perf_counter()
    tools_called: list[str] = []
    touched_files: list[str] = []
    transcript_log: list[str] = [f'# run_subtask: {goal}']
    final_summary = ''
    steps_used = 0
    latest_frames: list[dict[str, str]] = []

    from storage import normalize_node_source
    src = normalize_node_source(brain.source_context.get() if hasattr(brain, 'source_context') else 'phone')
    nb_key = f'{src}:{bot_id}'
    _sentinel = object()
    snap_pt_nb = brain.last_prompt_tokens.get(nb_key, _sentinel)
    snap_ct_nb = brain.last_cached_tokens.get(nb_key, _sentinel)
    snap_pt_b = brain.last_prompt_tokens.get(bot_id, _sentinel) if src == 'phone' else _sentinel
    snap_ct_b = brain.last_cached_tokens.get(bot_id, _sentinel) if src == 'phone' else _sentinel
    st_pt_map = brain.store.state.get('last_prompt_tokens') if isinstance(brain.store.state, dict) else None
    st_ct_map = brain.store.state.get('last_cached_tokens') if isinstance(brain.store.state, dict) else None
    snap_st_pt_nb = st_pt_map.get(nb_key, _sentinel) if isinstance(st_pt_map, dict) else _sentinel
    snap_st_ct_nb = st_ct_map.get(nb_key, _sentinel) if isinstance(st_ct_map, dict) else _sentinel
    snap_st_pt_b = st_pt_map.get(bot_id, _sentinel) if (src == 'phone' and isinstance(st_pt_map, dict)) else _sentinel
    snap_st_ct_b = st_ct_map.get(bot_id, _sentinel) if (src == 'phone' and isinstance(st_ct_map, dict)) else _sentinel

    brain.subtask_depth = int(getattr(brain, 'subtask_depth', 0) or 0) + 1
    exhausted = False
    run_error: str | None = None
    try:
        completed_cleanly = False
        for _ in range(max_steps):
            call_msgs = sub_messages
            if latest_frames:
                mm_parts: list[dict[str, Any]] = []
                for fr in latest_frames[-2:]:
                    mm_parts.append({'type': 'text', 'text': fr['caption']})
                    mm_parts.append({'type': 'image_url', 'image_url': {'url': f"data:{fr['mime']};base64,{fr['b64']}"}})
                call_msgs = sub_messages + [{'role': 'user', 'content': mm_parts}]
                latest_frames = []

            steps_used += 1
            msg = await brain.complete(call_msgs, tools=sub_tools or None, big=bool((args or {}).get('big', False)))
            msg.pop('_metrics', None)
            msg.pop('_finish_reason', None)
            msg.pop('_stream_incomplete', None)

            raw_calls = msg.get('tool_calls') or []
            step_txt = str(msg.get('content') or '').strip()
            if not raw_calls:
                final_summary = step_txt or final_summary
                completed_cleanly = True
                break

            sub_messages.append({'role': 'assistant', 'content': step_txt, 'tool_calls': raw_calls})
            for c_item in raw_calls:
                fn = c_item.get('function') or {}
                cname = str(fn.get('name') or '').strip()
                raw_args = fn.get('arguments')
                try:
                    cargs = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except Exception:
                    cargs = {}
                if cname not in sub_allowed:
                    res: Any = {'ok': False, 'error': f'子任务中未允许调用工具 {cname}'}
                else:
                    res = await brain.harness.execute(cname, cargs)
                tools_called.append(cname)
                if isinstance(cargs, dict) and cargs.get('path'):
                    p_str = str(cargs['path']).strip()
                    if p_str and p_str not in touched_files:
                        touched_files.append(p_str)
                if isinstance(res, dict) and (res.get('_multimodal_b64') or res.get('_image_b64')):
                    b64_v = str(res.pop('_multimodal_b64', '') or res.pop('_image_b64', '') or '')
                    mime_v = str(res.pop('_multimodal_mime', 'image/jpeg') or 'image/jpeg')
                    if b64_v:
                        latest_frames.append({'b64': b64_v, 'mime': mime_v, 'caption': f'【子任务 {cname} 附图】'})
                offloaded = brain.context_search.maybe_offload_tool_output(cname, cargs, res, bot_id=bot_id, turn_id=sub_turn_id)
                sub_messages.append({'role': 'tool', 'tool_call_id': c_item['id'], 'content': offloaded})
                transcript_log.append(f'## {cname}({json.dumps(cargs, ensure_ascii=False)[:180]})\n{offloaded[:600]}')
                if step_txt:
                    final_summary = step_txt

        if not completed_cleanly:
            exhausted = True
        if not final_summary:
            brain._next_tool_choice = 'none'
            tail_msg = await brain.complete(
                sub_messages + [{'role': 'user', 'content': '请立即基于以上子任务收集到的证据输出简明最终结论（<= 800字），不再调工具。'}],
                tools=None,
                big=False,
            )
            final_summary = str(tail_msg.get('content') or '').strip() or '子任务已完成（无文本结论）'
    except Exception as exc:
        run_error = str(exc)[:300] or type(exc).__name__
    finally:
        brain.subtask_depth = max(0, int(getattr(brain, 'subtask_depth', 1) or 1) - 1)
        def _restore_key(mapping: dict | None, key: str, old_val: Any) -> None:
            if not isinstance(mapping, dict):
                return
            if old_val is _sentinel:
                mapping.pop(key, None)
            else:
                mapping[key] = old_val
        _restore_key(brain.last_prompt_tokens, nb_key, snap_pt_nb)
        _restore_key(brain.last_cached_tokens, nb_key, snap_ct_nb)
        _restore_key(st_pt_map, nb_key, snap_st_pt_nb)
        _restore_key(st_ct_map, nb_key, snap_st_ct_nb)
        if src == 'phone':
            _restore_key(brain.last_prompt_tokens, bot_id, snap_pt_b)
            _restore_key(brain.last_cached_tokens, bot_id, snap_ct_b)
            _restore_key(st_pt_map, bot_id, snap_st_pt_b)
            _restore_key(st_ct_map, bot_id, snap_st_ct_b)

    ctx_id = None
    cs = getattr(brain, 'context_search', None)
    if cs is not None:
        try:
            ctx_id = cs.store_tool_context(
                'run_subtask',
                {'goal': goal[:200], 'steps': steps_used},
                '\n\n'.join(transcript_log) + f'\n\n# 最终总结\n{final_summary or run_error or ""}',
                bot_id=bot_id,
            )
        except Exception:
            ctx_id = None
    elapsed_ms = max(1, int(round((time.perf_counter() - t0) * 1000)))
    if run_error is not None:
        return {
            'ok': False,
            'error': run_error,
            'goal': goal[:140],
            'steps_used': steps_used,
            'tools_called': tools_called[:30],
            'artifacts': touched_files[:12],
            'ctx_id': ctx_id,
            'elapsed_ms': elapsed_ms,
            'result_summary': final_summary[:1400],
        }
    if exhausted:
        return {
            'ok': False,
            'error': f'子任务达到最大步数限制 ({max_steps}) 未完成',
            'goal': goal[:140],
            'steps_used': steps_used,
            'tools_called': tools_called[:30],
            'artifacts': touched_files[:12],
            'ctx_id': ctx_id,
            'elapsed_ms': elapsed_ms,
            'result_summary': final_summary[:1400],
        }
    return {
        'ok': True,
        'goal': goal[:140],
        'steps_used': steps_used,
        'tools_called': tools_called[:30],
        'artifacts': touched_files[:12],
        'ctx_id': ctx_id,
        'elapsed_ms': elapsed_ms,
        'result_summary': final_summary[:1400],
    }
