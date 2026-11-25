"""
[Module Responsibility]
Fire-and-Forget Async Subtask Harness (`server/core/subtask_runner.py`).

Every subtask strictly belongs to its spawning parent bot (`parent_bot_id`, `parent_source`):
- Default `wait=False`: spawns asynchronously and returns `<50ms` with `task_id` & `title` so the
  parent chat turn never blocks (and multiple concurrent subtasks can run side-by-side).
- Hard-disallows `reply`, `blackboard`, `schedule`, `admin`, `subtask`: a subtask NEVER speaks to
  the user directly or contacts other bots — its ONLY reporting outlet is returning its completion
  or error payload back to the spawning parent bot.
- Every tool execution / reorganization stage streams into `subtask_runs` (`SubtaskStore`) and
  broadcasts `subtask_update` so BOTH the user's independent per-subtask secondary panel AND the
  parent AI's `blackboard(action="peek")` see the exact same live steps, outputs, and errors.
- Automatically recycles old finished runs (`gc_runs`) to prevent unbounded stacking.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import secrets
import time
from typing import Any

logger = logging.getLogger(__name__)

_SUBTASK_DEPTH_VAR: contextvars.ContextVar[int] = contextvars.ContextVar('subtask_depth', default=0)

_ACTIVE_TASKS: dict[str, asyncio.Task] = {}
_CANCEL_FLAGS: dict[str, bool] = {}
_STEER_QUEUES: dict[str, list[str]] = {}

MAX_CONCURRENT_SUBTASKS_PER_BOT = 6

_DISALLOWED_SUBTASK_TOOLS = frozenset({
    'run_subtask',
    'subtask',
    'notify_user',
    'emergency_alarm',
    'present_choices',
    'complete_goal',
    'finish_goal',
    'reply',
    'blackboard',
    'schedule',
    'schedule_task',
    'system_config',
    'deploy_cloud_computer',
    'admin',
})

SUBTASK_PRESETS: dict[str, tuple[tuple[str, ...], str]] = {
    'research': (
        ('read_file', 'search_files', 'cloud', 'memory'),
        '你是聚焦调研子单元。公网检索统一用 cloud(op="search"|"navigate") 无图快读（用完自动释放浏览器），核对原文与证据后返回结构化事实与来源链接。',
    ),
    'code_audit': (
        ('read_file', 'search_files', 'bash'),
        '你是代码审查与日志排查子单元。只查关键文件、行号、命令输出与根因，严禁猜测。',
    ),
    'device_ops': (
        ('phone', 'pc', 'cloud', 'bash', 'read_file'),
        '你是设备执行子单元。严格按指令完成聚焦操作并回报真实状态与关键截图/输出证据。',
    ),
    'memory_clerk': (
        ('memory', 'read_file', 'write_file', 'edit_file', 'search_files'),
        '你是记忆整理与档案核对子单元。精准读写结构化事实与笔记，去重消歧并返回变更清单。',
    ),
}


def get_subtask_store(brain: Any):
    store = getattr(brain, 'subtask_store', None)
    if store is not None:
        return store
    from core.subtask_store import SubtaskStore
    db_path = getattr(getattr(brain, 'context_search', None), 'db_path', None)
    if db_path is None:
        db_path = brain.store.root / 'context_index.db'
    store = SubtaskStore(db_path)
    brain.subtask_store = store
    return store


async def _broadcast_subtask(brain: Any, payload: dict[str, Any]) -> None:
    if hasattr(brain, 'publish'):
        try:
            await brain.publish(payload)
            return
        except Exception:
            pass
    hub = getattr(brain, 'hub', None)
    if hub is not None and hasattr(hub, 'broadcast'):
        try:
            await hub.broadcast(payload)
        except Exception:
            pass


def _derive_task_title(args: dict[str, Any], preset_name: str, goal: str) -> str:
    explicit = str((args or {}).get('title') or '').strip()
    if explicit:
        return explicit[:48]
    if preset_name == 'memory_reorganize':
        return '记忆深度整理'
    clean = ' '.join(str(goal or '').split()).strip()
    if not clean:
        return '后台子任务'
    return (clean[:24] + '…') if len(clean) > 25 else clean


def _summarize_tool_result(res: Any) -> tuple[bool, str, str]:
    if isinstance(res, dict):
        err = str(res.get('error') or '').strip()
        ok = bool(res.get('ok', not bool(err)))
        if err:
            return False, '', err[:4000]
        parts = []
        for k in ('summary', 'result_summary', 'message', 'status', 'action', 'path', 'count'):
            if res.get(k) not in (None, '', []):
                parts.append(f'{k}={str(res[k])[:1500]}')
        txt = ' · '.join(parts) if parts else json.dumps(res, ensure_ascii=False)[:4000]
        return ok, txt[:4000], ''
    txt = str(res or '').strip()
    return True, txt[:4000], ''


async def _notify_parent_bot_only(
    brain: Any,
    *,
    task_id: str,
    title: str,
    parent_bot_id: str,
    parent_source: str,
    status: str,
    summary: str,
    error: str = '',
    backup_id: str = '',
    report_path: str = '',
) -> None:
    """Sole reporting outlet for an async subtask: report ONLY to its spawning parent bot."""
    await _broadcast_subtask(brain, {
        'type': 'subtask_completed',
        'task_id': task_id,
        'title': title,
        'parent_bot_id': parent_bot_id,
        'parent_source': parent_source,
        'status': status,
        'summary': summary[:12000],
        'error': error[:4000],
    })
    # Only wake parent bot via chat turn if brain has an active LLM config (skip in bare unit tests)
    cfg = getattr(getattr(brain, 'store', None), 'config', None)
    has_api = bool(
        cfg is not None
        and (cfg.effective_api_key(big=False) or cfg.effective_api_key(big=True))
        and (cfg.effective_model(big=False) or cfg.effective_model(big=True))
    )
    if not has_api or not hasattr(brain, 'chat') or not hasattr(brain, '_get_bot_lock'):
        return
    if status == 'cancelled':
        return
    try:
        header = f'[子任务回执 · {title} ({task_id}) · {"已完成" if status == "completed" else "执行出错"}]'
        body = summary[:24000] if summary else (f'报错：{error}' if error else '已结束')
        extra = []
        if report_path:
            extra.append(f'报告路径：`{report_path}`')
        if backup_id:
            extra.append(f'回滚标识：`/memory rollback {backup_id}`')
        extra_s = ('\n' + ' | '.join(extra)) if extra else ''
        prompt = f'{header}\n{body}{extra_s}\n\n请基于以上子任务完整结果向主人汇报关键结论或报错原因（不要截断遗漏关键数据）。'
        async with brain._get_bot_lock(parent_bot_id, parent_source):
            await brain.chat(
                prompt,
                bot_id=parent_bot_id,
                source=parent_source,
                mode='auto',
                turn_origin='subtask_callback',
            )
    except Exception as exc:
        logger.warning('Parent callback failed for subtask %s -> %s: %s', task_id, parent_bot_id, exc)


async def _run_memory_reorganize_worker(
    brain: Any,
    *,
    task_id: str,
    title: str,
    parent_bot_id: str,
    parent_source: str,
    use_llm: bool = True,
    resume_checkpoint: dict[str, Any] | None = None,
    notify_parent: bool = True,
) -> dict[str, Any]:
    store = get_subtask_store(brain)
    from core.memory_migrator import reorganize_legacy_memory

    async def _on_progress(
        *,
        stage_idx: int,
        stage_total: int,
        stage_label: str,
        sub_done: int,
        sub_total: int,
        progress_pct: int,
        step: dict[str, Any] | None = None,
        checkpoint: dict[str, Any] | None = None,
        backup_id: str = '',
    ) -> None:
        if step:
            store.append_step(
                task_id,
                tool=str(step.get('tool') or f'stage_{stage_idx}'),
                stage=str(step.get('stage') or stage_label),
                args=str(step.get('args') or ''),
                ok=bool(step.get('ok', True)),
                result=str(step.get('result') or ''),
                error=str(step.get('error') or ''),
            )
        updated = store.update_progress(
            task_id,
            stage_idx=stage_idx,
            stage_total=stage_total,
            stage_label=stage_label,
            sub_done=sub_done,
            sub_total=sub_total,
            progress_pct=progress_pct,
            steps_executed=stage_idx,
            last_output=str((step or {}).get('result') or stage_label),
            checkpoint=checkpoint,
            backup_id=backup_id or None,
        )
        if updated:
            await _broadcast_subtask(brain, {'type': 'subtask_update', 'subtask': updated})

    try:
        res = await reorganize_legacy_memory(
            brain,
            dry_run=False,
            use_llm=use_llm,
            resume_checkpoint=resume_checkpoint,
            cancel_check=lambda: bool(_CANCEL_FLAGS.get(task_id)),
            on_progress=_on_progress,
            task_id=task_id,
        )
        if res.get('cancelled') or _CANCEL_FLAGS.get(task_id):
            fin = store.finish_run(task_id, status='cancelled', result_summary='用户已取消记忆整理（线上记忆未受影响）', last_error='')
            if fin:
                await _broadcast_subtask(brain, {'type': 'subtask_update', 'subtask': fin})
            return {'ok': False, 'cancelled': True, 'task_id': task_id, 'title': title}

        rec = res.get('reconciliation') or {}
        tbl = res.get('tables') or {}
        summary = (
            f"扫描 {res.get('legacy_experiences_scanned', 0)} 条旧经验 + {res.get('legacy_compacts_migrated', 0)} 篇压缩笔记，"
            f"聚为 {res.get('clusters_total', 0)} 簇、合并 {res.get('aliases_merged', 0)} 组别名。\n"
            f"事实入库：新增 {rec.get('INSERT', 0)} / 更新 {rec.get('UPDATE', 0)} / 替代 {rec.get('SUPERSEDE', 0)} / 清理过期 {rec.get('EXPIRED_SKIPPED', 0)}；"
            f"1-hop 关系 +{res.get('relations_added', 0)}；Critic 捞回 {res.get('critic_rescued', 0)} 条、拦截幻觉 {res.get('critic_rejected', 0)} 条。\n"
            f"当前活跃：核心块 {tbl.get('core_blocks', 0)} · 事实 {tbl.get('active_facts', 0)} · 关系 {tbl.get('active_relations', 0)} · 情景 {tbl.get('active_episodes', 0)} · 向量 {tbl.get('active_vectors', 0)}。"
        )
        fin = store.finish_run(
            task_id,
            status='completed',
            result_summary=summary,
            metrics={
                'scanned_experiences': res.get('legacy_experiences_scanned', 0),
                'migrated_compacts': res.get('legacy_compacts_migrated', 0),
                'archived_experiences': res.get('legacy_experiences_archived', 0),
                'clusters': res.get('clusters_total', 0),
                'aliases_merged': res.get('aliases_merged', 0),
                'relations_added': res.get('relations_added', 0),
                'critic_rescued': res.get('critic_rescued', 0),
                'critic_rejected': res.get('critic_rejected', 0),
                'llm_calls': res.get('llm_calls', 0),
                'active_facts': tbl.get('active_facts', 0),
                'active_episodes': tbl.get('active_episodes', 0),
                'active_vectors': tbl.get('active_vectors', 0),
            },
            backup_id=str(res.get('backup_id') or ''),
            report_path=str(res.get('report_path') or ''),
        )
        if fin:
            await _broadcast_subtask(brain, {'type': 'subtask_update', 'subtask': fin})
        removed = store.gc_runs()
        if removed:
            await _broadcast_subtask(brain, {'type': 'subtask_gc', 'removed_ids': removed})
        if notify_parent:
            await _notify_parent_bot_only(
                brain,
                task_id=task_id,
                title=title,
                parent_bot_id=parent_bot_id,
                parent_source=parent_source,
                status='completed',
                summary=summary,
                backup_id=str(res.get('backup_id') or ''),
                report_path=str(res.get('report_path') or ''),
            )
        return {**res, 'task_id': task_id, 'title': title, 'result_summary': summary}
    except Exception as exc:
        err_s = str(exc)[:300] or type(exc).__name__
        store.append_step(task_id, tool='memory_reorganize', stage='异常中断', ok=False, error=err_s)
        fin = store.finish_run(task_id, status='failed', last_error=err_s)
        if fin:
            await _broadcast_subtask(brain, {'type': 'subtask_update', 'subtask': fin})
        if notify_parent:
            await _notify_parent_bot_only(
                brain, task_id=task_id, title=title, parent_bot_id=parent_bot_id,
                parent_source=parent_source, status='failed', summary='', error=err_s,
            )
        return {'ok': False, 'error': err_s, 'task_id': task_id, 'title': title}
    finally:
        _ACTIVE_TASKS.pop(task_id, None)
        _CANCEL_FLAGS.pop(task_id, None)


async def _run_generic_subtask_worker(
    brain: Any,
    args: dict[str, Any],
    *,
    task_id: str,
    title: str,
    parent_bot_id: str,
    parent_source: str,
    notify_parent: bool = True,
    prior_messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    store = get_subtask_store(brain)
    goal = str((args or {}).get('goal') or '').strip()
    max_steps = max(1, min(500, int((args or {}).get('max_steps') or 200)))
    raw_focus = (args or {}).get('focus_files') or (args or {}).get('context_paths')
    focus_list: list[str] = []
    if isinstance(raw_focus, list):
        focus_list = [str(x).strip() for x in raw_focus if str(x).strip()][:12]
    elif isinstance(raw_focus, str) and raw_focus.strip():
        focus_list = [raw_focus.strip()]
    sub_mode = str((args or {}).get('mode') or '').strip().lower()

    try:
        from assistants import get_assistant
        bot = get_assistant(brain.store, parent_bot_id)
    except Exception:
        bot = {'id': parent_bot_id, 'name': parent_bot_id, 'tools': None}

    from core.tool_schemas import is_tool_allowed, normalize_bot_tools, normalize_tool_name
    all_schemas = brain.harness.all_tools()
    base_names = normalize_bot_tools(bot.get('tools'), default_all=[t['function']['name'] for t in all_schemas])
    caller_allowed = set(base_names) | {'blackboard', 'schedule', 'cloud', 'admin', 'reply'}
    if parent_source in ('pc', 'pc2'):
        caller_allowed -= {'phone', 'phone_workspace', 'phone_macro', 'notify_user', 'emergency_alarm'}
    else:
        caller_allowed -= {'pc', 'dsh_workspace', 'message_phone_bot'}
    preset_name = str((args or {}).get('preset') or '').strip().lower()
    preset_spec = SUBTASK_PRESETS.get(preset_name)
    req_tools = (args or {}).get('tools') or (args or {}).get('allowed_tools')
    if isinstance(req_tools, list) and req_tools:
        norm_req = {normalize_tool_name(str(t).strip()) for t in req_tools if str(t).strip()}
        caller_allowed &= norm_req
    elif preset_spec is not None:
        caller_allowed &= set(preset_spec[0])
    sub_allowed = caller_allowed - _DISALLOWED_SUBTASK_TOOLS
    sub_tools = [t for t in all_schemas if t.get('function', {}).get('name') in sub_allowed]

    focus_hint = f'\n优先关注文件/目录：{", ".join(focus_list)}' if focus_list else ''
    mode_hint = f'\n执行模式偏好：{sub_mode}' if sub_mode else ''
    preset_role = preset_spec[1] if preset_spec is not None else (
        '你是父 Bot 派出的独立子任务执行单元 (`subtask`)，运行在完全隔离的轻上下文中。'
    )
    sub_system = (
        f'{preset_role}\n'
        '以最少步骤精准查证、梳理代码或执行聚焦任务；完成后直接用简洁结构化语言给出结论与关键文件/行号证据。'
        '你的结果只能返回给派发任务的父 Bot，严禁联系其他 Bot、严禁尝试发弹窗或改定时。'
    )
    use_main_model = bool((args or {}).get('use_main_model') or (args or {}).get('big', False))
    cfg = getattr(getattr(brain, 'store', None), 'config', None)
    worker_model_spec = str(getattr(cfg, 'worker_model', '') or '').strip() if cfg is not None else ''
    effective_sub_model = str(bot.get('model') or '') if (use_main_model or not worker_model_spec) else worker_model_spec
    sub_messages: list[dict[str, Any]] = [
        {'role': 'system', 'content': sub_system},
        {'role': 'user', 'content': f'子任务目标：{goal}{focus_hint}{mode_hint}'},
    ]
    if prior_messages:
        for pm in prior_messages[1:]:
            r_role = 'user' if pm.get('role') == 'user' else 'assistant'
            r_txt = str(pm.get('text') or '').strip()
            if r_txt:
                sub_messages.append({'role': r_role, 'content': r_txt})

    t0 = time.perf_counter()
    tools_called: list[str] = []
    touched_files: list[str] = []
    transcript_log: list[str] = [f'# subtask ({task_id}): {goal}']
    final_summary = ''
    steps_used = 0
    latest_frames: list[dict[str, str]] = []

    nb_key = f'{parent_source}:{parent_bot_id}'
    _sentinel = object()
    snap_pt_nb = brain.last_prompt_tokens.get(nb_key, _sentinel)
    snap_ct_nb = brain.last_cached_tokens.get(nb_key, _sentinel)
    snap_pt_b = brain.last_prompt_tokens.get(parent_bot_id, _sentinel) if parent_source == 'phone' else _sentinel
    snap_ct_b = brain.last_cached_tokens.get(parent_bot_id, _sentinel) if parent_source == 'phone' else _sentinel
    st_pt_map = brain.store.state.get('last_prompt_tokens') if isinstance(brain.store.state, dict) else None
    st_ct_map = brain.store.state.get('last_cached_tokens') if isinstance(brain.store.state, dict) else None
    snap_st_pt_nb = st_pt_map.get(nb_key, _sentinel) if isinstance(st_pt_map, dict) else _sentinel
    snap_st_ct_nb = st_ct_map.get(nb_key, _sentinel) if isinstance(st_ct_map, dict) else _sentinel
    snap_st_pt_b = st_pt_map.get(parent_bot_id, _sentinel) if (parent_source == 'phone' and isinstance(st_pt_map, dict)) else _sentinel
    snap_st_ct_b = st_ct_map.get(parent_bot_id, _sentinel) if (parent_source == 'phone' and isinstance(st_ct_map, dict)) else _sentinel

    depth_tok = _SUBTASK_DEPTH_VAR.set(1)
    bot_tok = brain.bot_context.set(parent_bot_id) if hasattr(brain, 'bot_context') else None
    src_tok = brain.source_context.set(parent_source) if hasattr(brain, 'source_context') else None
    origin_tok = brain.turn_origin_context.set('subtask') if hasattr(brain, 'turn_origin_context') else None
    exhausted = False
    run_error: str | None = None
    cancelled = False

    async def _silent_sub_complete(call_messages: list[dict[str, Any]], tools_arg: list[dict] | None, use_big: bool) -> dict[str, Any]:
        if 'complete' in getattr(brain, '__dict__', {}):
            return await brain.complete(call_messages, tools=tools_arg, big=use_big)
        llm_obj = getattr(brain, 'llm', None)
        if llm_obj is not None and hasattr(llm_obj, 'complete_stream'):
            return await llm_obj.complete_stream(
                messages=call_messages,
                bot_id=parent_bot_id,
                bot_model=effective_sub_model,
                tools=tools_arg,
                big=use_big,
                stream_prefix='',
                on_stream=None,
                tool_choice='none' if tools_arg is None else None,
                source=parent_source,
            )
        return await brain.complete(call_messages, tools=tools_arg, big=use_big)

    try:
        completed_cleanly = False
        for step_idx in range(max_steps):
            if _CANCEL_FLAGS.get(task_id):
                cancelled = True
                break
            pending_steers = _STEER_QUEUES.pop(task_id, [])
            for st_msg in pending_steers:
                sub_messages.append({'role': 'user', 'content': f'【用户实时插话补充指示】\n{st_msg}'})
            call_msgs = list(sub_messages)
            if latest_frames:
                mm_parts: list[dict[str, Any]] = []
                for fr in latest_frames[-2:]:
                    mm_parts.append({'type': 'text', 'text': fr['caption']})
                    mm_parts.append({'type': 'image_url', 'image_url': {'url': f"data:{fr['mime']};base64,{fr['b64']}"}})
                call_msgs.append({'role': 'user', 'content': mm_parts})
                latest_frames = []
            if step_idx == max_steps - 1 and max_steps > 1:
                call_msgs.append({'role': 'user', 'content': '【系统提示】这是子任务的最后一步，请立即停止调用工具，直接基于已有证据完整输出结构化结论。'})

            steps_used += 1
            allow_tools_now = None if (step_idx == max_steps - 1 and max_steps > 1) else (sub_tools or None)
            msg = await _silent_sub_complete(call_msgs, allow_tools_now, bool((args or {}).get('big', False)))
            msg.pop('_metrics', None)
            msg.pop('_finish_reason', None)
            msg.pop('_stream_incomplete', None)

            raw_calls = msg.get('tool_calls') or []
            step_txt = str(msg.get('content') or '').strip()
            thought_txt = str(msg.get('reasoning_content') or msg.get('thought') or '').strip()
            upd = store.update_progress(
                task_id,
                steps_executed=steps_used,
                progress_pct=0,
                stage_label=f'第 {steps_used} 步',
                last_output=step_txt or None,
                live_thought=thought_txt or None,
            )
            if upd:
                await _broadcast_subtask(brain, {'type': 'subtask_update', 'subtask': upd})

            if not raw_calls:
                final_summary = step_txt or final_summary
                # Check if user steered while LLM was generating final text
                late_steers = _STEER_QUEUES.pop(task_id, [])
                if late_steers:
                    sub_messages.append({'role': 'assistant', 'content': final_summary})
                    for st_msg in late_steers:
                        sub_messages.append({'role': 'user', 'content': f'【用户实时插话补充指示】\n{st_msg}'})
                    continue
                completed_cleanly = True
                break

            sub_messages.append({'role': 'assistant', 'content': step_txt, 'tool_calls': raw_calls})
            for c_item in raw_calls:
                if _CANCEL_FLAGS.get(task_id):
                    cancelled = True
                    break
                fn = c_item.get('function') or {}
                cname = str(fn.get('name') or '').strip()
                raw_args = fn.get('arguments')
                try:
                    cargs = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except Exception:
                    cargs = {}
                if not is_tool_allowed(cname, sub_allowed):
                    res: Any = {'ok': False, 'error': f'子任务中未允许调用工具 {cname}'}
                elif (
                    parent_source in ('pc', 'pc2')
                    and cname in ('read_file', 'write_file', 'edit_file', 'search_files')
                    and hasattr(brain, '_maybe_route_pc_fs')
                ):
                    routed = await brain._maybe_route_pc_fs(cname, cargs)
                    res = routed if routed is not None else await brain.harness.execute(cname, cargs)
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

                ok_flag, res_brief, err_brief = _summarize_tool_result(res)
                upd_step = store.append_step(
                    task_id,
                    tool=cname,
                    stage=f'第 {steps_used} 步',
                    args=json.dumps(cargs, ensure_ascii=False)[:3000],
                    ok=ok_flag,
                    result=res_brief,
                    error=err_brief,
                )
                if upd_step:
                    await _broadcast_subtask(brain, {'type': 'subtask_update', 'subtask': upd_step})

                offloaded = brain.context_search.maybe_offload_tool_output(cname, cargs, res, bot_id=parent_bot_id, turn_id=task_id)
                sub_messages.append({'role': 'tool', 'tool_call_id': c_item['id'], 'content': offloaded})
                transcript_log.append(f'## {cname}({json.dumps(cargs, ensure_ascii=False)[:600]})\n{offloaded[:2400]}')
                if step_txt:
                    final_summary = step_txt
            if cancelled:
                break

        if not cancelled and not completed_cleanly:
            exhausted = True
        if not cancelled and not final_summary:
            tail_msg = await _silent_sub_complete(
                sub_messages + [{'role': 'user', 'content': '请立即基于以上子任务收集到的证据完整输出最终结论，不再调工具。'}],
                None,
                False,
            )
            final_summary = str(tail_msg.get('content') or '').strip() or '子任务已完成（无文本结论）'
    except asyncio.CancelledError:
        cancelled = True
    except Exception as exc:
        run_error = f'{type(exc).__name__}: {str(exc)[:1500]}'
        store.append_step(task_id, tool='subtask_runtime', stage='执行异常', ok=False, error=run_error)
    finally:
        _ACTIVE_TASKS.pop(task_id, None)
        _CANCEL_FLAGS.pop(task_id, None)
        _STEER_QUEUES.pop(task_id, None)
        if origin_tok is not None:
            brain.turn_origin_context.reset(origin_tok)
        if bot_tok is not None:
            brain.bot_context.reset(bot_tok)
        if src_tok is not None:
            brain.source_context.reset(src_tok)
        _SUBTASK_DEPTH_VAR.reset(depth_tok)

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
        if parent_source == 'phone':
            _restore_key(brain.last_prompt_tokens, parent_bot_id, snap_pt_b)
            _restore_key(brain.last_cached_tokens, parent_bot_id, snap_ct_b)
            _restore_key(st_pt_map, parent_bot_id, snap_st_pt_b)
            _restore_key(st_ct_map, parent_bot_id, snap_st_ct_b)

    ctx_id = None
    cs = getattr(brain, 'context_search', None)
    if cs is not None:
        try:
            ctx_id = cs.store_tool_context(
                'run_subtask',
                {'task_id': task_id, 'title': title, 'goal': goal[:200], 'steps': steps_used},
                '\n\n'.join(transcript_log) + f'\n\n# 最终总结\n{final_summary or run_error or ""}',
                bot_id=parent_bot_id,
            )
        except Exception:
            ctx_id = None
    elapsed_ms = max(1, int(round((time.perf_counter() - t0) * 1000)))

    final_status = 'cancelled' if cancelled else ('failed' if run_error else 'completed')
    if not cancelled and final_summary:
        store.append_message(task_id, 'assistant', final_summary[:30000])
    fin = store.finish_run(
        task_id,
        status=final_status,
        result_summary=final_summary[:30000] if not cancelled else '子任务已取消',
        last_error=run_error or '',
        metrics={'steps_used': steps_used, 'tools_called': len(tools_called), 'elapsed_ms': elapsed_ms},
    )
    if fin:
        await _broadcast_subtask(brain, {'type': 'subtask_update', 'subtask': fin})
    removed = store.gc_runs()
    if removed:
        await _broadcast_subtask(brain, {'type': 'subtask_gc', 'removed_ids': removed})
    if notify_parent and not cancelled:
        await _notify_parent_bot_only(
            brain,
            task_id=task_id,
            title=title,
            parent_bot_id=parent_bot_id,
            parent_source=parent_source,
            status=final_status,
            summary=final_summary[:24000],
            error=run_error or '',
        )

    if cancelled:
        return {'ok': False, 'cancelled': True, 'task_id': task_id, 'title': title, 'steps_used': steps_used}
    if run_error is not None:
        return {
            'ok': False, 'error': run_error, 'task_id': task_id, 'title': title, 'goal': goal[:500],
            'steps_used': steps_used, 'tools_called': tools_called[:60], 'artifacts': touched_files[:30],
            'ctx_id': ctx_id, 'elapsed_ms': elapsed_ms, 'result_summary': final_summary[:30000],
        }
    if exhausted:
        has_usable = bool(final_summary and final_summary != '子任务已完成（无文本结论）')
        return {
            'ok': has_usable, 'partial': True, 'truncated_at_max_steps': True,
            **({'error': f'子任务达到最大步数兜底上限 ({max_steps}) 且未产出结论'} if not has_usable else {}),
            'task_id': task_id, 'title': title, 'goal': goal[:500],
            'steps_used': steps_used, 'tools_called': tools_called[:60], 'artifacts': touched_files[:30],
            'ctx_id': ctx_id, 'elapsed_ms': elapsed_ms, 'result_summary': final_summary[:30000],
        }
    return {
        'ok': True, 'task_id': task_id, 'title': title, 'goal': goal[:500],
        'steps_used': steps_used, 'tools_called': tools_called[:60], 'artifacts': touched_files[:30],
        'ctx_id': ctx_id, 'elapsed_ms': elapsed_ms, 'result_summary': final_summary[:30000],
    }


async def spawn_memory_reorganize_subtask(
    brain: Any,
    *,
    parent_bot_id: str = 'companion',
    parent_source: str = 'phone',
    use_llm: bool = True,
    resume_from_task_id: str = '',
    title: str = '记忆深度整理',
) -> dict[str, Any]:
    store = get_subtask_store(brain)
    if store.count_running(parent_bot_id) >= MAX_CONCURRENT_SUBTASKS_PER_BOT:
        return {'ok': False, 'error': f'当前并行子任务数已达上限 ({MAX_CONCURRENT_SUBTASKS_PER_BOT})，请稍候或先取消部分子任务'}
    ckpt = store.get_raw_checkpoint(resume_from_task_id) if resume_from_task_id else None
    task_id = resume_from_task_id if (resume_from_task_id and store.get_run(resume_from_task_id)) else ('sub-' + secrets.token_hex(5))
    cfg = getattr(getattr(brain, 'store', None), 'config', None)
    w_model = cfg.effective_worker_model() if (cfg is not None and hasattr(cfg, 'effective_worker_model')) else ''
    if resume_from_task_id and store.get_run(resume_from_task_id):
        run_obj = store.update_progress(task_id, status='running', last_error='', stage_label='断点续传恢复中')
    else:
        run_obj = store.create_run(
            task_id=task_id,
            title=title,
            preset='memory_reorganize',
            goal='8 阶段全自动深度整理长期记忆并归档旧轨',
            parent_bot_id=parent_bot_id,
            parent_source=parent_source,
            model=w_model,
            stage_total=8,
            max_steps=8,
        )
    _CANCEL_FLAGS[task_id] = False
    await _broadcast_subtask(brain, {'type': 'subtask_update', 'subtask': run_obj})
    bg = asyncio.create_task(
        _run_memory_reorganize_worker(
            brain,
            task_id=task_id,
            title=title,
            parent_bot_id=parent_bot_id,
            parent_source=parent_source,
            use_llm=use_llm,
            resume_checkpoint=ckpt,
            notify_parent=True,
        ),
        name=f'subtask:{task_id}',
    )
    _ACTIVE_TASKS[task_id] = bg
    return {
        'ok': True,
        'async': True,
        'task_id': task_id,
        'title': title,
        'preset': 'memory_reorganize',
        'status': 'running',
        'parent_bot_id': parent_bot_id,
        'parent_source': parent_source,
        'subtask': run_obj,
    }


async def cancel_subtask(brain: Any, task_id: str) -> dict[str, Any]:
    store = get_subtask_store(brain)
    tid = str(task_id or '').strip()
    run = store.get_run(tid)
    if not run:
        return {'ok': False, 'error': f'未找到子任务 {tid}'}
    _CANCEL_FLAGS[tid] = True
    t = _ACTIVE_TASKS.get(tid)
    if t is not None and not t.done():
        t.cancel()
    fin = store.finish_run(tid, status='cancelled', result_summary='已手动取消')
    if fin:
        await _broadcast_subtask(brain, {'type': 'subtask_update', 'subtask': fin})
    return {'ok': True, 'task_id': tid, 'subtask': fin}


async def resume_subtask(brain: Any, task_id: str) -> dict[str, Any]:
    store = get_subtask_store(brain)
    tid = str(task_id or '').strip()
    run = store.get_run(tid)
    if not run:
        return {'ok': False, 'error': f'未找到子任务 {tid}'}
    if run.get('status') == 'running':
        return {'ok': True, 'already_running': True, 'task_id': tid, 'subtask': run}
    if run.get('preset') == 'memory_reorganize':
        return await spawn_memory_reorganize_subtask(
            brain,
            parent_bot_id=run['parent_bot_id'],
            parent_source=run['parent_source'],
            resume_from_task_id=tid,
            title=run['title'],
        )
    # Generic subtask: re-spawn cleanly with same goal & parent
    return await execute_run_subtask(
        brain,
        {
            'op': 'spawn',
            'goal': run['goal'],
            'title': run['title'],
            'preset': run['preset'],
            'max_steps': run['max_steps'],
            'wait': False,
        },
        override_parent=(run['parent_bot_id'], run['parent_source']),
    )


async def steer_subtask(brain: Any, task_id: str, message: str) -> dict[str, Any]:
    """Inject a live user/parent steer message into a running subtask, or continue a finished AI subtask in-place."""
    store = get_subtask_store(brain)
    tid = str(task_id or '').strip()
    msg = str(message or '').strip()
    if not msg:
        return {'ok': False, 'error': '插话内容不能为空'}
    run = store.get_run(tid)
    if not run:
        return {'ok': False, 'error': f'未找到子任务 {tid}'}
    if run.get('preset') == 'memory_reorganize':
        return {'ok': False, 'error': '记忆深度整理为固定 8 阶段流水线，不支持中途插话'}

    updated = store.append_message(tid, 'user', msg)
    if run.get('status') == 'running' and tid in _ACTIVE_TASKS and not _ACTIVE_TASKS[tid].done():
        _STEER_QUEUES.setdefault(tid, []).append(msg)
        if updated:
            await _broadcast_subtask(brain, {'type': 'subtask_update', 'subtask': updated})
        return {'ok': True, 'steered': True, 'mode': 'live_inject', 'task_id': tid, 'subtask': updated}

    # Subtask already finished/failed/cancelled -> continue in-place with full prior conversation bubbles!
    run_now = store.update_progress(tid, status='running', last_error='', stage_label='按补充指示继续')
    if run_now:
        await _broadcast_subtask(brain, {'type': 'subtask_update', 'subtask': run_now})
    _CANCEL_FLAGS[tid] = False
    bg = asyncio.create_task(
        _run_generic_subtask_worker(
            brain,
            {
                'goal': run['goal'],
                'title': run['title'],
                'preset': run['preset'],
                'max_steps': max(200, int(run.get('max_steps') or 200)),
            },
            task_id=tid,
            title=run['title'],
            parent_bot_id=run['parent_bot_id'],
            parent_source=run['parent_source'],
            notify_parent=True,
            prior_messages=(run_now or updated or run).get('messages') or [],
        ),
        name=f'subtask:{tid}',
    )
    _ACTIVE_TASKS[tid] = bg
    return {'ok': True, 'steered': True, 'mode': 'continued', 'task_id': tid, 'subtask': run_now}


async def execute_run_subtask(
    brain: Any,
    args: dict[str, Any],
    *,
    override_parent: tuple[str, str] | None = None,
) -> dict[str, Any]:
    args = args or {}
    op = str(args.get('op') or 'spawn').strip().lower()
    store = get_subtask_store(brain)

    if op == 'status':
        tid = str(args.get('task_id') or '').strip()
        run = store.get_run(tid)
        return {'ok': True, 'subtask': run} if run else {'ok': False, 'error': f'未找到子任务 {tid}'}
    if op == 'list':
        bot_id = override_parent[0] if override_parent else (brain.bot_context.get() or 'companion')
        return {'ok': True, 'subtasks': store.list_runs(parent_bot_id=bot_id, limit=int(args.get('limit') or 20))}
    if op == 'cancel':
        return await cancel_subtask(brain, str(args.get('task_id') or ''))
    if op == 'resume':
        return await resume_subtask(brain, str(args.get('task_id') or ''))
    if op == 'steer':
        return await steer_subtask(brain, str(args.get('task_id') or ''), str(args.get('message') or args.get('goal') or ''))

    cur_depth = max(int(_SUBTASK_DEPTH_VAR.get() or 0), int(getattr(brain, 'subtask_depth', 0) or 0))
    if cur_depth >= 1:
        return {'ok': False, 'error': '禁止在 subtask 内部递归调用 subtask'}

    from storage import normalize_node_source
    parent_bot_id = override_parent[0] if override_parent else (brain.bot_context.get() or 'companion')
    parent_source = normalize_node_source(
        override_parent[1] if override_parent else (brain.source_context.get() if hasattr(brain, 'source_context') else 'phone')
    )
    preset_name = str(args.get('preset') or '').strip().lower()
    goal = str(args.get('goal') or '').strip()

    if preset_name == 'memory_reorganize':
        return await spawn_memory_reorganize_subtask(
            brain,
            parent_bot_id=parent_bot_id,
            parent_source=parent_source,
            title=_derive_task_title(args, preset_name, goal),
        )

    if not goal:
        return {'ok': False, 'error': 'subtask 需要明确的子任务目标 goal'}
    if store.count_running(parent_bot_id) >= MAX_CONCURRENT_SUBTASKS_PER_BOT:
        return {'ok': False, 'error': f'当前并行子任务数已达上限 ({MAX_CONCURRENT_SUBTASKS_PER_BOT})，请等待已有子任务完成或取消后再派发'}

    title = _derive_task_title(args, preset_name, goal)
    max_steps = max(1, min(500, int(args.get('max_steps') or 200)))
    task_id = 'sub-' + secrets.token_hex(5)
    cfg = getattr(getattr(brain, 'store', None), 'config', None)
    w_model = str(getattr(cfg, 'worker_model', '') or '') if cfg is not None else ''
    run_obj = store.create_run(
        task_id=task_id,
        title=title,
        preset=preset_name,
        goal=goal,
        parent_bot_id=parent_bot_id,
        parent_source=parent_source,
        model=w_model,
        stage_total=0,
        max_steps=max_steps,
    )
    _CANCEL_FLAGS[task_id] = False
    await _broadcast_subtask(brain, {'type': 'subtask_update', 'subtask': run_obj})

    wait_flag = bool(args.get('wait', False))
    if wait_flag:
        return await _run_generic_subtask_worker(
            brain,
            args,
            task_id=task_id,
            title=title,
            parent_bot_id=parent_bot_id,
            parent_source=parent_source,
            notify_parent=False,
        )

    bg = asyncio.create_task(
        _run_generic_subtask_worker(
            brain,
            args,
            task_id=task_id,
            title=title,
            parent_bot_id=parent_bot_id,
            parent_source=parent_source,
            notify_parent=True,
        ),
        name=f'subtask:{task_id}',
    )
    _ACTIVE_TASKS[task_id] = bg
    return {
        'ok': True,
        'async': True,
        'task_id': task_id,
        'title': title,
        'preset': preset_name,
        'status': 'running',
        'parent_bot_id': parent_bot_id,
        'parent_source': parent_source,
        'hint': f'子任务「{title}」已瞬发启动 ({task_id})，完成后会自动回传给当前对话；如需随时查看最新步骤或报错，可调用 blackboard(action="peek")。',
    }
