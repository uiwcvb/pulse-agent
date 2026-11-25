"""Canonical v4.0.0 Unified Tool Handlers Mixin (`bash`, `memory`, `reply`, `subtask`, `admin`)."""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Any

from assistants import get_assistant
from core.vfs_router import VirtualPathRouter
from storage import normalize_node_source
from vault_manager import TZ, now_iso


def build_pc_fs_rpc_payload(c_name: str, c_args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Normalize search_files/edit_file args into a concrete `pc_fs` RPC operation + payload."""
    c_op_low = str(c_args.get('op') or c_args.get('action') or '').strip().lower()
    pc_fs_op = c_name
    pc_fs_args = dict(c_args)
    if c_name == 'search_files':
        if c_op_low in ('list', 'list_dir', 'ls', 'dir') or (not c_op_low and not pc_fs_args.get('query') and not pc_fs_args.get('pattern')):
            pc_fs_op = 'list_dir'
            if 'depth' not in pc_fs_args and 'max_depth' in pc_fs_args:
                pc_fs_args['depth'] = pc_fs_args['max_depth']
        elif c_op_low in ('glob', 'glob_files', 'find') or (not c_op_low and not pc_fs_args.get('query') and pc_fs_args.get('pattern')):
            pc_fs_op = 'glob_files'
        else:
            pc_fs_op = 'grep_search'
            if not pc_fs_args.get('query') and pc_fs_args.get('pattern'):
                pc_fs_args['query'] = pc_fs_args['pattern']
    elif c_name == 'edit_file':
        if c_op_low in ('delete', 'rm', 'remove') or pc_fs_args.get('delete') is True:
            pc_fs_op = 'delete_file'
        elif c_op_low in ('move', 'mv', 'rename') or (pc_fs_args.get('src') and pc_fs_args.get('dst')):
            pc_fs_op = 'move_file'
        else:
            if 'old' not in pc_fs_args and 'old_text' in pc_fs_args:
                pc_fs_args['old'] = pc_fs_args['old_text']
            if 'new' not in pc_fs_args and 'new_text' in pc_fs_args:
                pc_fs_args['new'] = pc_fs_args['new_text']
    return pc_fs_op, pc_fs_args


async def stage_and_execute_send_image(brain: Any, src: str, eff_args: dict[str, Any]) -> dict[str, Any]:
    """Stage PC image paths before sending; fail explicitly instead of leaking raw Windows paths."""
    eff = dict(eff_args or {})
    raw_path = str(eff.get('path') or eff.get('url') or '').strip()
    if raw_path and 'path' not in eff:
        eff['path'] = raw_path
    is_http = raw_path.lower().startswith(('http://', 'https://'))
    if not is_http:
        # Prefer already-existing cloud workspace file unless caller explicitly specifies a PC path
        cloud_exists = False
        if not VirtualPathRouter.is_pc_explicit_path(eff, raw_path):
            try:
                cand = brain.harness.path(raw_path.removeprefix('workspace/').lstrip('/'))
                cloud_exists = cand.is_file()
            except Exception:
                cloud_exists = False
        if not cloud_exists:
            if src in ('pc', 'pc2') and brain.is_dsh_online():
                staged = await brain._stage_pc_file_to_cloud_workspace(raw_path)
                if staged is not None:
                    eff['path'] = f'uploads/{staged.name}'
                elif VirtualPathRouter.is_pc_explicit_path(eff, raw_path):
                    return {'ok': False, 'op': 'send_image', 'source_path': raw_path, 'error': f'无法从 PC 节点拉取图片到云端暂存区：{raw_path}'}
            elif VirtualPathRouter.is_pc_explicit_path(eff, raw_path):
                return {'ok': False, 'op': 'send_image', 'source_path': raw_path, 'error': f'PC 节点未在线，无法读取本地图片路径：{raw_path}'}
    res = await brain.harness.execute('send_image', eff)
    if isinstance(res, dict) and res.get('ok') and res.get('chat_image_url'):
        if hasattr(brain, '_queue_turn_image'):
            brain._queue_turn_image(str(res['chat_image_url']))
        else:
            u = str(res['chat_image_url'])
            cur = list(brain.pending_stickers_var.get() or ())
            if u not in cur:
                cur.append(u)
            brain.pending_stickers_var.set(tuple(cur))
            brain.pending_sticker_var.set(u)
    return res


async def stage_and_execute_send_file(brain: Any, src: str, eff_args: dict[str, Any]) -> dict[str, Any]:
    """Send a workspace (or staged PC) file/PDF as a structured attachment card in chat."""
    import mimetypes as _mimetypes
    import shutil as _shutil

    eff = dict(eff_args or {})
    raw_path = str(eff.get('path') or '').strip()
    if not raw_path:
        return {'ok': False, 'op': 'send_file', 'error': 'path 不能为空'}
    cloud_p = None
    if not VirtualPathRouter.is_pc_explicit_path(eff, raw_path):
        try:
            cand = brain.harness.path(raw_path.removeprefix('workspace/').lstrip('/'))
            if cand.is_file():
                cloud_p = cand
        except Exception:
            cloud_p = None
    if cloud_p is None:
        if src in ('pc', 'pc2') and brain.is_dsh_online():
            staged = await brain._stage_pc_file_to_cloud_workspace(raw_path)
            if staged is not None and staged.is_file():
                cloud_p = staged
                eff['path'] = f'uploads/{staged.name}'
            elif VirtualPathRouter.is_pc_explicit_path(eff, raw_path):
                return {'ok': False, 'op': 'send_file', 'source_path': raw_path, 'error': f'无法从 PC 节点拉取文件到云端暂存区：{raw_path}'}
        elif VirtualPathRouter.is_pc_explicit_path(eff, raw_path):
            return {'ok': False, 'op': 'send_file', 'source_path': raw_path, 'error': f'PC 节点未在线，无法读取本地文件路径：{raw_path}'}
    if cloud_p is None:
        return {'ok': False, 'op': 'send_file', 'source_path': raw_path, 'error': f'文件不存在: {raw_path}'}
    size_bytes = int(cloud_p.stat().st_size)
    if size_bytes <= 0:
        return {'ok': False, 'op': 'send_file', 'source_path': raw_path, 'error': f'文件为空 (0 字节)，禁止发送空文件: {raw_path}'}
    if size_bytes > 100 * 1024 * 1024:
        return {'ok': False, 'op': 'send_file', 'source_path': raw_path, 'error': f'文件过大 ({size_bytes} bytes > 100MB)，请先压缩或拆分'}

    root_res = brain.harness.root.resolve()
    try:
        resolved_cp = cloud_p.resolve()
        rel_ws = resolved_cp.relative_to(root_res).as_posix() if resolved_cp.is_relative_to(root_res) else ''
    except Exception:
        rel_ws = ''
    # If not inside workspace (e.g. vault) or outside uploads/, keep safe workspace rel path if inside workspace,
    # otherwise copy into uploads/ so both Android and Desktop can download cleanly.
    if not rel_ws:
        uploads_dir = brain.harness.root / 'uploads'
        uploads_dir.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time() * 1000) % 100000000
        safe_name = f'{cloud_p.stem[:40]}-{stamp}{cloud_p.suffix}'
        dst_p = uploads_dir / safe_name
        _shutil.copy2(cloud_p, dst_p)
        rel_ws = f'uploads/{safe_name}'

    display_name = str(eff.get('name') or cloud_p.name).strip() or cloud_p.name
    ext = cloud_p.suffix.lstrip('.').upper()[:8]
    mime, _ = _mimetypes.guess_type(cloud_p.name)
    file_meta = {
        'path': rel_ws,
        'name': display_name,
        'size': size_bytes,
        'ext': ext,
        'mime': mime or ('application/pdf' if ext == 'PDF' else 'application/octet-stream'),
    }
    if hasattr(brain, 'pending_files_var'):
        cur_f = list(brain.pending_files_var.get() or ())
        if not any(isinstance(x, dict) and x.get('path') == rel_ws for x in cur_f):
            cur_f.append(file_meta)
        brain.pending_files_var.set(tuple(cur_f))
    return {
        'ok': True,
        'op': 'send_file',
        'source_path': raw_path,
        'chat_file': file_meta,
    }


async def dispatch_custom_run(fn: Any, args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Invoke a dynamic plugin `run(...)` function with flexible signature binding and honest return status."""
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    dict_names = ('args', 'params', 'payload', 'data', 'input', 'req', 'request')
    ctx_names = ('context', 'ctx', 'env')
    if len(params) == 0:
        res = fn()
    elif any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params):
        res = fn(args, context)
    elif len(params) == 1 and params[0].kind != inspect.Parameter.VAR_KEYWORD:
        p0 = params[0].name
        if p0 in dict_names or not isinstance(args, dict):
            res = fn(args)
        elif p0 in ctx_names:
            res = fn(context)
        elif p0 in args:
            res = fn(args[p0])
        elif len(args) == 1 and params[0].default is inspect.Parameter.empty:
            res = fn(next(iter(args.values())))
        else:
            res = fn(args)
    elif len(params) == 2 and params[0].name in dict_names and params[1].name in ctx_names:
        res = fn(args, context)
    elif isinstance(args, dict):
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params):
            kw = dict(args)
            for cn in ctx_names:
                if cn in sig.parameters and cn not in kw:
                    kw[cn] = context
            res = fn(**kw)
        else:
            kw = {k: v for k, v in args.items() if k in sig.parameters}
            for cn in ctx_names:
                if cn in sig.parameters and cn not in kw:
                    kw[cn] = context
            for an in dict_names:
                if an in sig.parameters and an not in kw:
                    kw[an] = args
            missing = [p.name for p in params if p.default is inspect.Parameter.empty and p.name not in kw]
            res = fn(**kw) if not missing else fn(args, context)
    else:
        res = fn(args, context)
    out = (await res) if asyncio.iscoroutine(res) else res
    if isinstance(out, dict):
        if out.get('error') and 'ok' not in out:
            out['ok'] = False
        elif 'ok' not in out:
            out['ok'] = True
        return out
    return {'ok': True, 'result': out}


def build_custom_plugin_source(
    t_name: str, cls_id: str, desc: str, params_schema: dict[str, Any], code_lines: list[str]
) -> tuple[str, int]:
    """Generate dynamic plugin module source and header line count for syntax-error line adjustment."""
    indented_code = '\n'.join('        ' + ln for ln in code_lines)
    header = (
        f'"""Dynamic Custom Tool: {t_name}"""\n'
        f'import asyncio, inspect, json, os, re, time, httpx\n'
        f'from pathlib import Path\n'
        f'from core.canonical_tool_handlers import dispatch_custom_run\n\n'
        f'class DynamicPlugin_{cls_id}:\n'
        f'    name = {json.dumps(t_name, ensure_ascii=False)}\n'
        f'    description = {json.dumps(desc, ensure_ascii=False)}\n'
        f'    parameters = {repr(params_schema)}\n\n'
        f'    async def execute(self, args: dict, context: dict) -> dict:\n'
    )
    footer = (
        f'\n        if "run" in locals() and callable(locals()["run"]):\n'
        f'            return await dispatch_custom_run(locals()["run"], args, context)\n'
        f'        return {{"ok": False, "error": "自定义工具未定义可调用的 run(...) 入口函数"}}\n\n'
        f'def register(registry):\n'
        f'    registry.register_tool(DynamicPlugin_{cls_id}())\n'
    )
    return header + indented_code + footer, len(header.splitlines())


class CanonicalToolHandlersMixin:
    """Unified routers for the canonical v4.0.0 tool set while preserving legacy compatibility."""

    async def _tool_complete_goal(self, args: dict):
        summary = str(args.get('summary') or args.get('result') or args.get('text') or '').strip()
        verified_evidence = str(args.get('verified_evidence') or args.get('evidence') or '').strip()
        if not summary:
            return {'ok': False, 'error': '调用 complete_goal 必须提供具体的完成总结 summary'}
        norm_src = normalize_node_source(self.source_context.get())
        room_bot_id = self.room_context.get() or self.bot_context.get()
        nb_key = f'{norm_src}:{room_bot_id}'
        with self.store.lock:
            active_goals = self.store.state.setdefault('active_goals', {})
            cur_goal = active_goals.pop(nb_key, None) or {}
            cur_exec = self.executions_by_node_bot.get(nb_key) or self.execution or {}
            cur_trace_steps = len(cur_exec.get('trace') or [])
            goal_title = str(cur_goal.get('goal') or summary)[:200]
            self.store.state['last_goal_run'] = {
                'goal': goal_title,
                'status': 'completed',
                'summary': summary[:600],
                'verified_evidence': verified_evidence[:400],
                'wakeups': int(cur_goal.get('wakeups') or 0),
                'steps': int(cur_goal.get('total_steps') or 0) + max(1, cur_trace_steps),
                'updated_at': now_iso(),
            }
            self.store.save()
        await self.publish({
            'type': 'goal_status',
            'source': norm_src,
            'bot_id': room_bot_id,
            'active': False,
            'status': 'completed',
            'goal': goal_title,
            'summary': summary[:500],
        })
        return {
            'ok': True,
            'goal_completed': True,
            'goal': goal_title,
            'summary': summary,
            'verified_evidence': verified_evidence,
            'note': '🎯 目标已完成状态已由引擎正式登记，自动拉起循环已解除。现在请向用户输出清晰、完整的最终交付总结！',
        }

    async def _tool_emergency_alarm(self, args: dict):
        title = str(args.get('title') or '紧急呼叫').strip()[:120]
        reason = str(args.get('reason') or '').strip()[:1000]
        duration_sec = max(5, min(45, int(args.get('duration_sec') or 18)))
        if not reason:
            return {'error': '必须提供触发紧急响铃的具体原因 reason'}
        now = time.time()
        if self.store.state.get('snooze_until', 0) > now:
            return {'suppressed': True, 'reason': '当前处于设定的免打扰时段，已拦截紧急闹钟'}
        if now - self._last_emergency_alarm_at < 180.0:
            return {'suppressed': True, 'reason': '3 分钟内已触发过一次紧急响铃，避免重复打扰'}
        self._last_emergency_alarm_at = now
        bot_id = self.room_context.get() or self.bot_context.get()
        bot = get_assistant(self.store, self.bot_context.get())
        aid = 'alarm-' + secrets.token_hex(6)
        await self.publish({
            'type': 'emergency_alarm',
            'id': aid,
            'bot_id': bot_id,
            'bot_name': bot['name'],
            'title': f'🚨 {title}',
            'body': reason,
            'duration_sec': duration_sec,
            'urgent': True,
        })
        return {'alarm_triggered': True, 'id': aid, 'duration_sec': duration_sec, 'title': title}

    async def _tool_system_config(self, args: dict):
        from core.onboarding_service import handle_system_config_tool
        return await handle_system_config_tool(self, args or {})

    async def _tool_deploy_cloud_computer(self, args: dict):
        from core.cloud_computer_deployer import deploy_cloud_computer_over_ssh
        cc = getattr(self, 'computer_coordinator', None) or getattr(self, 'computer', None)
        ws_dir = getattr(getattr(self, 'harness', None), 'root', None)
        return await deploy_cloud_computer_over_ssh(self.store, cc, self.publish, args or {}, workspace_dir=ws_dir)

    async def _tool_run_subtask(self, args: dict):
        from core.subtask_runner import execute_run_subtask
        return await execute_run_subtask(self, args or {})

    async def _tool_locate_files(self, args: dict):
        import fnmatch as _fnmatch
        from datetime import datetime as _dt
        query = str(args.get('query') or args.get('pattern') or args.get('name') or args.get('path') or '').strip()
        if not query:
            return {'ok': False, 'op': 'locate', 'matches': [], 'error': 'locate 需要提供文件名/关键词 query 或 glob pattern'}
        limit = max(1, min(100, int(args.get('limit') or 40)))
        q_low = query.lower()
        is_glob = any(ch in query for ch in ('*', '?', '['))
        skip_dirs = {'.history', '.cache', '.config', '.python_packages', '.transfers', '.versions', '__pycache__', 'node_modules', '.git', 'pylibs', '.venv', 'venv', '.local', 'imapwork'}
        matches: list[dict[str, Any]] = []
        nodes_checked: list[str] = []
        nodes_unavailable: list[dict[str, str]] = []

        def _is_match(rel_s: str, fname: str) -> bool:
            if is_glob:
                return _fnmatch.fnmatch(fname.lower(), q_low) or _fnmatch.fnmatch(rel_s.lower(), q_low)
            return q_low in fname.lower() or q_low in rel_s.lower()

        def _scan_local():
            root = self.harness.root
            if root.is_dir():
                nodes_checked.append('hub_workspace')
                for f in sorted(root.rglob('*')):
                    if not f.is_file() or f.is_symlink():
                        continue
                    rel_parts = f.relative_to(root).parts
                    if any(part in skip_dirs or part.startswith('org.chromium.') for part in rel_parts):
                        continue
                    rel_p = f.relative_to(root).as_posix()
                    if _is_match(rel_p, f.name):
                        st = f.stat()
                        matches.append({
                            'node': 'hub_workspace',
                            'path': rel_p,
                            'size_bytes': st.st_size,
                            'mtime_iso': _dt.fromtimestamp(st.st_mtime, TZ).isoformat(timespec='seconds'),
                        })
                        if len(matches) >= limit:
                            return
            vault = getattr(self.harness, 'vault', None)
            if vault is not None and vault.root.is_dir():
                nodes_checked.append('memory_vault')
                for f in sorted(vault.root.rglob('*.md')):
                    if not f.is_file() or f.is_symlink():
                        continue
                    rel_parts = f.relative_to(vault.root).parts
                    if '.history' in rel_parts or self.harness._is_other_bot_private_file(f):
                        continue
                    rel_p = 'memory_vault/' + f.relative_to(vault.root).as_posix()
                    if _is_match(rel_p, f.name):
                        st = f.stat()
                        matches.append({
                            'node': 'memory_vault',
                            'path': rel_p,
                            'size_bytes': st.st_size,
                            'mtime_iso': _dt.fromtimestamp(st.st_mtime, TZ).isoformat(timespec='seconds'),
                        })
                        if len(matches) >= limit:
                            return

        await asyncio.to_thread(_scan_local)

        async def _scan_cloud():
            cc = getattr(self, 'computer', None)
            if cc is None or getattr(cc, 'client', None) is None:
                nodes_unavailable.append({'node': 'cloud_desktop', 'reason': '未部署或未挂载'})
                return
            try:
                res = await asyncio.wait_for(cc.client.call('GET', '/files'), timeout=4.0)
                nodes_checked.append('cloud_desktop')
                for item in (res.get('files') or []) if isinstance(res, dict) else []:
                    p_str = str(item.get('path') or item.get('name') or '')
                    if not p_str:
                        continue
                    if _is_match(p_str, p_str.rsplit('/', 1)[-1]):
                        mt = float(item.get('modified_at') or 0)
                        matches.append({
                            'node': 'cloud_desktop',
                            'path': p_str,
                            'size_bytes': int(item.get('size') or item.get('bytes') or 0),
                            'mtime_iso': _dt.fromtimestamp(mt, TZ).isoformat(timespec='seconds') if mt > 0 else '',
                        })
            except Exception as exc:
                nodes_unavailable.append({'node': 'cloud_desktop', 'reason': str(exc)[:120]})

        async def _scan_pc():
            cur_src = normalize_node_source(self.source_context.get())
            if cur_src in ('pc', 'pc2') and self.is_dsh_online():
                try:
                    pat = query if is_glob else f'*{query}*'
                    pc_res = await self._raw_dsh_rpc('pc_fs', {'fs_op': 'glob_files', 'pattern': pat}, timeout=4.0)
                    if isinstance(pc_res, dict) and pc_res.get('ok'):
                        nodes_checked.append('pc_windows')
                        for item in (pc_res.get('matches') or [])[:limit]:
                            p_s = str(item) if isinstance(item, str) else str((item or {}).get('path') or '')
                            if p_s:
                                matches.append({'node': 'pc_windows', 'path': p_s, 'size_bytes': None, 'mtime_iso': ''})
                except Exception as exc:
                    nodes_unavailable.append({'node': 'pc_windows', 'reason': str(exc)[:120]})
            elif cur_src in ('pc', 'pc2'):
                nodes_unavailable.append({'node': 'pc_windows', 'reason': 'DSH 电脑代理未在线'})

        await asyncio.gather(_scan_cloud(), _scan_pc())
        return {
            'ok': True,
            'op': 'locate',
            'query': query,
            'count': len(matches[:limit]),
            'nodes_checked': nodes_checked,
            'nodes_unavailable': nodes_unavailable,
            'matches': matches[:limit],
            'note': '已跨全部在线节点检索；如需把 hub_workspace 文件送到云电脑请调 cloud(op="push", path="...")，取回请调 cloud(op="pull", path="...")。',
        }

    async def _tool_search_files(self, args: dict):
        eff = dict(args or {})
        op = str(eff.get('op') or eff.get('action') or '').strip().lower()
        if op in ('locate', 'find_anywhere', 'cross_locate'):
            return await self._tool_locate_files(eff)
        # Dispatch directly to concrete harness ops (never re-enter 'search_files' -> avoids RecursionError!)
        if op in ('list', 'list_dir', 'ls', 'dir') or (not op and not eff.get('query') and not eff.get('pattern')):
            target_tool = 'list_dir'
        elif op in ('glob', 'glob_files', 'find') or (not op and not eff.get('query') and eff.get('pattern')):
            target_tool = 'glob_files'
        else:
            target_tool = 'grep_search'
        return await self.harness.execute(target_tool, eff)

    async def _tool_bash(self, args: dict):
        eff = dict(args or {})
        op = str(eff.get('op') or eff.get('action') or 'run').strip().lower()
        if op in ('bg_start', 'bg_wait', 'bg_logs', 'bg_stdin', 'bg_kill', 'bg_list'):
            sub = {'bg_start': 'start', 'bg_wait': 'wait', 'bg_logs': 'logs', 'bg_stdin': 'send_stdin', 'bg_kill': 'kill', 'bg_list': 'list'}[op]
            return await self._tool_bg_task({**eff, 'action': sub})
        if op in ('watchdog_start', 'watchdog_stop', 'watchdog_logs', 'watchdog_run_now', 'watchdog_list'):
            return await self._tool_watchdog_task({**eff, 'action': op[len('watchdog_'):]})
        if op == 'start' and 'notify_on_exit' in eff:
            return await self._tool_bg_task({**eff, 'action': 'start'})
        if op in ('logs', 'send_stdin', 'kill') and eff.get('task_id'):
            return await self._tool_bg_task({**eff, 'action': op})
        cur_src = normalize_node_source(self.source_context.get())
        req_node = str(eff.get('node') or eff.get('target') or '').strip().lower()
        want_pc_shell = (
            req_node in ('pc', 'pc2', 'pc_windows', 'windows')
            or (cur_src in ('pc', 'pc2') and req_node not in ('server', 'hub', 'cloud_sandbox'))
        )
        if want_pc_shell:
            if self.is_dsh_online():
                res_pc = await self._raw_dsh_rpc('pc_shell', eff, timeout=float(eff.get('timeout') or 25.0))
                if isinstance(res_pc, dict):
                    res_pc.setdefault('node', 'pc_windows')
                return res_pc
            return {
                'ok': False,
                'pc_online': False,
                'node': 'pc_offline',
                'error': 'Windows 电脑端代理当前未在线，已阻止将本机 bash 静默回退到云端 Linux 沙箱。请等待电脑端重连，或改用 cloud(op="shell") 操作云电脑，或显式传 target="cloud_sandbox" 在中枢沙箱执行。',
            }
        return await self.harness.execute('run_command', eff)

    async def _tool_memory(self, args: dict):
        eff = dict(args or {})
        op = str(eff.get('op') or eff.get('action') or 'search').strip().lower()
        if op in ('pin', 'add', 'remember'):
            return await self._tool_memory_pin({**eff, 'action': 'pin'})
        if op in ('unpin', 'remove', 'delete', 'forget'):
            return await self._tool_memory_pin({**eff, 'action': 'unpin'})
        if op in ('list_pins', 'pins', 'list'):
            return await self._tool_memory_pin({**eff, 'action': 'list'})
        if op in ('conversation', 'conv'):
            return await self._tool_conversation_search(eff)
        if op in ('ctx', 'context'):
            return await self._tool_ctx_search(eff)
        if op in ('query_facts', 'facts', 'table'):
            tables = self.memory_engine.get_memory_tables()
            hits = tables.query_facts(
                query=str(eff.get('query') or ''),
                entity=str(eff.get('entity') or ''),
                attr=str(eff.get('attr') or ''),
                include_history=bool(eff.get('include_history', False)),
                limit=int(eff.get('limit') or 12),
            )
            return {'ok': True, 'count': len(hits), 'facts': hits}
        if op in ('upsert_fact', 'set_fact', 'record_fact'):
            tables = self.memory_engine.get_memory_tables()
            return tables.upsert_fact_reconciled(
                entity=str(eff.get('entity') or ''),
                attr=str(eff.get('attr') or ''),
                value=str(eff.get('value') or eff.get('text') or ''),
                aliases=eff.get('aliases') if isinstance(eff.get('aliases'), list) else [],
                ttl_type=str(eff.get('ttl_type') or 'stateful'),
                source_ref=f"tool:{self.bot_context.get() or 'companion'}",
                search_cues=str(eff.get('search_cues') or eff.get('query') or ''),
                bot_id=str(eff.get('bot_id') or 'all'),
            )
        if op in ('delete_fact', 'remove_fact', 'forget_fact'):
            tables = self.memory_engine.get_memory_tables()
            res_del = tables.delete_fact(
                fact_id=str(eff.get('fact_id') or eff.get('id') or ''),
                entity=str(eff.get('entity') or ''),
                attr=str(eff.get('attr') or ''),
                hard=bool(eff.get('hard', True)),
            )
            if res_del.get('deleted') and getattr(self, 'vault', None) is not None:
                with contextlib.suppress(Exception):
                    self.vault.write('profile/memory_ledger.md', tables.render_memory_ledger_markdown())
            return res_del
        if op in ('delete_episode', 'remove_episode'):
            tables = self.memory_engine.get_memory_tables()
            res_del = tables.delete_episode(
                episode_id=str(eff.get('episode_id') or eff.get('id') or ''),
                title_keyword=str(eff.get('query') or eff.get('text') or ''),
                hard=bool(eff.get('hard', True)),
            )
            if res_del.get('deleted') and getattr(self, 'vault', None) is not None:
                with contextlib.suppress(Exception):
                    self.vault.write('profile/memory_ledger.md', tables.render_memory_ledger_markdown())
            return res_del
        if op in ('reorganize', 'migrate', 'rebuild'):
            if bool(eff.get('dry_run', False)):
                from core.memory_migrator import reorganize_legacy_memory
                return await reorganize_legacy_memory(self, dry_run=True, use_llm=True)
            from core.subtask_runner import spawn_memory_reorganize_subtask
            return await spawn_memory_reorganize_subtask(
                self,
                parent_bot_id=self.bot_context.get() or 'companion',
                parent_source=normalize_node_source(self.source_context.get()),
            )
        if op in ('rollback', 'restore_backup'):
            from core.memory_migrator import rollback_memory_backup
            return rollback_memory_backup(self, backup_id=str(eff.get('backup_id') or eff.get('id') or ''))
        if op in ('list_backups', 'backups'):
            from core.memory_migrator import list_memory_backups
            from pathlib import Path
            vroot = Path(self.vault.root) if getattr(self, 'vault', None) is not None else (self.store.root / 'memory_vault')
            return {'ok': True, 'backups': list_memory_backups(vroot)}
        if op in ('gardener', 'maintain', 'prune'):
            from core.memory_migrator import run_memory_gardener
            return run_memory_gardener(self)
        if op in ('ledger', 'stats', 'status'):
            tables = self.memory_engine.get_memory_tables()
            return {'ok': True, 'stats': tables.stats(), 'ledger_preview': tables.render_memory_ledger_markdown()[:2400]}
        if op in ('embedding_status', 'embed_status'):
            return {'ok': True, **self.memory_engine.get_embedding_engine().status()}
        if op in ('download_embedding', 'install_embedding'):
            import asyncio as _aio
            eng = self.memory_engine.get_embedding_engine()
            dl = await _aio.to_thread(eng.download_tier_model, eff.get('tier'))
            if dl.get('ok'):
                idx_res = await _aio.to_thread(self.memory_engine.get_memory_tables().reindex_vectors)
                dl['reindex'] = idx_res
            return dl
        return await self._tool_memory_search(eff)

    async def _tool_schedule(self, args: dict):
        eff = dict(args or {})
        op = str(eff.get('op') or eff.get('action') or 'list').strip().lower()
        eff['action'] = op
        return await self._tool_blackboard(eff)

    async def _tool_reply(self, args: dict):
        eff = dict(args or {})
        op = str(eff.get('op') or eff.get('action') or '').strip().lower()
        src = normalize_node_source(self.source_context.get())
        if op in ('send_image', 'image'):
            return await stage_and_execute_send_image(self, src, eff)
        if op in ('send_file', 'file', 'send_pdf', 'send_doc'):
            return await stage_and_execute_send_file(self, src, eff)
        if op in ('sticker', 'send_sticker', 'emoji'):
            return await self._tool_send_sticker(eff)
        if op in ('choices', 'present_choices'):
            return await self._tool_present_choices(eff)
        if op in ('notify', 'notify_user'):
            if src in ('pc', 'pc2'):
                return {'ok': False, 'error': 'PC 节点不支持直接触发手机端通知，请通过 blackboard @主助理 转达'}
            return await self._tool_notify_user(eff)
        if op in ('silent', 'stay_silent'):
            return await self._tool_stay_silent(eff)
        if op in ('dnd', 'do_not_disturb'):
            sn = eff.get('snooze_minutes') if eff.get('snooze_minutes') is not None else eff.get('minutes', 60)
            return await self._tool_stay_silent({**eff, 'snooze_minutes': sn})
        if op in ('alarm', 'emergency_alarm'):
            if src in ('pc', 'pc2'):
                return {'ok': False, 'error': 'PC 节点不支持直接触发手机端强闹钟，请通过 blackboard @主助理 转达'}
            return await self._tool_emergency_alarm(eff)
        if op in ('complete_goal', 'finish_goal'):
            return await self._tool_complete_goal(eff)
        return {'ok': False, 'error': f'未知 reply op: {op}（支持 send_image/send_file/sticker/choices/notify/silent/dnd/alarm/complete_goal）'}

    async def _tool_send_sticker(self, args: dict):
        raw_code = str(
            args.get('code') or args.get('sticker') or args.get('name') or args.get('id') or args.get('label') or ''
        ).strip()
        info = self.harness.extension_registry.resolve_sticker_info(raw_code)
        if not info:
            avail = [f"{s['code']}={s['label']}" for s in self.harness.extension_registry.list_active_stickers()[:16]]
            return {
                'ok': False,
                'error': f'未找到匹配的表情贴纸: {raw_code!r}，请从可用代码中选择',
                'available_codes': avail,
            }
        self._queue_turn_image(info['url'])
        return {
            'ok': True,
            'queued': True,
            'sticker': info['url'],
            'pack_id': info['pack_id'],
            'code': info['code'],
            'label': info['label'],
            'note': f"贴纸「{info['label']}」({info['code']}) 已挂载到本轮回复，直接输出自然回复正文即可，严禁在正文手写 <sticker:...> 标签。",
        }

    async def _tool_subtask(self, args: dict):
        return await self._tool_run_subtask(args)

    async def _tool_admin(self, args: dict):
        eff = dict(args or {})
        op = str(eff.get('op') or 'status').strip().lower()
        if op in ('status', 'doctor', 'context', 'all', 'check', ''):
            return await self._tool_system_status({**eff, 'op': op or 'status'})
        if op in ('config', 'system_config'):
            return await self._tool_system_config({**eff, 'action': eff.get('action') or 'status'})
        if op in ('bot', 'manage_bot'):
            return await self._tool_manage_bot({**eff, 'action': eff.get('action') or 'list'})
        if op == 'skill':
            s_act = str(eff.get('action') or 'list_skills').strip().lower()
            s_map = {
                'list': 'list_skills', 'open': 'open_skill', 'read': 'open_skill',
                'load': 'open_skill', 'save': 'save_skill', 'create': 'save_skill',
                'write': 'save_skill', 'delete': 'delete_skill', 'remove': 'delete_skill',
            }
            return await self._tool_custom_tool({**eff, 'action': s_map.get(s_act, s_act)})
        if op in ('custom_tool', 'plugin'):
            return await self._tool_custom_tool({**eff, 'action': eff.get('action') or 'list'})
        if op in ('models', 'list_models'):
            return await self._tool_list_models(eff)
        if op in ('deploy_cloud', 'deploy_cloud_computer'):
            return await self._tool_deploy_cloud_computer(eff)
        return {'ok': False, 'error': f'未知 admin op: {op}（支持 status/doctor/context/config/bot/skill/custom_tool/models/deploy_cloud）'}

    @staticmethod
    def _fmt_bj(ts: float | None) -> str:
        if not ts:
            return ''
        dt = datetime.fromtimestamp(float(ts), TZ)
        wk = ('周一', '周二', '周三', '周四', '周五', '周六', '周日')[dt.weekday()]
        return f"{dt.strftime('%Y-%m-%d %H:%M:%S')} ({wk})"

    @staticmethod
    def _parse_wall_time(raw: str, now_ts: float | None = None) -> float:
        s = raw.strip()
        base_ts = now_ts if now_ts is not None else time.time()
        now_dt = datetime.fromtimestamp(base_ts, TZ)
        m_hm = re.fullmatch(r'(\d{1,2}):(\d{2})(?::(\d{2}))?', s)
        if m_hm:
            hh, mm, ss = int(m_hm.group(1)), int(m_hm.group(2)), int(m_hm.group(3) or 0)
            if hh == 24 and mm == 0 and ss == 0:
                hh = 0
            if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
                raise ValueError(f'时间格式非法: {raw}（小时须为 00-23，分钟/秒须为 00-59）')
            target = now_dt.replace(hour=hh, minute=mm, second=ss, microsecond=0)
            if target.timestamp() <= base_ts:
                target += timedelta(days=1)
            return target.timestamp()
        s_iso = s.replace(' ', 'T') if re.match(r'^\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}', s) else s
        dt = datetime.fromisoformat(s_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.timestamp()

    async def _tool_notify_user(self, args: dict):
        title = str(args.get('title') or '助理提醒').strip()[:120]
        body = str(args.get('body') or '').strip()[:2000]
        level = str(args.get('level') or '').strip().lower()
        has_urgent = bool(args.get('urgent', False))
        has_overlay = bool(args.get('overlay', False))
        if level in ('strong', 'urgent', 'overlay', 'high', 'float', 'popup'):
            is_strong = True
        elif level in ('weak', 'normal', 'low', 'bar', 'gentle') and not has_overlay:
            is_strong = False
        else:
            is_strong = has_urgent or has_overlay
        if not body:
            return {'ok': False, 'error': '通知内容不能为空'}
        bot_id = self.room_context.get() or self.bot_context.get()
        bot = get_assistant(self.store, self.bot_context.get())
        bot_name = str(bot.get('name') or '主助理')
        nid = 'notif-' + secrets.token_hex(6)
        collapse_key = str(args.get('collapse_key') or f'bot:{bot_id}:notify').strip()
        await self.publish({
            'type': 'user_notification',
            'id': nid,
            'collapse_key': collapse_key,
            'bot_id': bot_id,
            'bot_name': bot_name,
            'title': title,
            'body': body,
            'urgent': is_strong,
            'overlay': is_strong,
            'level': 'strong' if is_strong else 'weak',
        })
        if is_strong:
            overlay_text = f'【{title}】\n{body}' if title and title not in body else body
            with self.store.lock:
                self.store.state.setdefault('pending', {})[nid] = {
                    'id': nid,
                    'text': overlay_text[:3000],
                    'title': title,
                    'body': body[:2000],
                    'bot_id': bot_id,
                    'bot_name': bot_name,
                    'created_at': time.time(),
                    'stage': 3,
                    'last_email': 0,
                    'emails': 0,
                    'demo': False,
                    'email_status': 'overlay_tool',
                }
                self.store.save()
            await self.publish({
                'type': 'escalation',
                'stage': 3,
                'id': nid,
                'bot_id': bot_id,
                'bot_name': bot_name,
                'title': title,
                'text': body[:3000],
                'demo': False,
            })
        return {
            'ok': True,
            'notified': True,
            'level': 'strong' if is_strong else 'weak',
            'delivery': 'overlay_and_notification' if is_strong else 'notification_bar',
            'overlay_popped': is_strong,
            'id': nid,
            'title': title,
            'urgent': is_strong,
            'note': (
                '强通知已发出：手机主屏全局悬浮窗卡片 + 高优通知栏已触发；请直接输出自然回复正文，勿重复声明“已发通知”。'
                if is_strong else
                '弱通知已发出：手机系统通知栏轻提醒已推送；请直接输出自然回复正文，勿重复声明“已发通知”。'
            ),
        }
