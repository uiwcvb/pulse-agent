from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import os
import re
import secrets
import signal
import sys
import time
from pathlib import Path
from typing import Any

from core.canonical_tool_handlers import CanonicalToolHandlersMixin, build_custom_plugin_source
from core.scheduler_engine import SchedulerEngine
from core.tool_schemas import scrub_subprocess_env
from storage import normalize_node_source


class ToolHandlersMixin(CanonicalToolHandlersMixin):
    """Domain Controller for Scheduling, Background Tasks, Watchdogs, Memory Search, Skills, Blackboard & Notifications."""

    async def _tool_list_models(self, args: dict):
        from core.channel_router import fetch_aggregated_models
        agg = await fetch_aggregated_models(self.store, persist_channel_models=True)
        models = list(agg.get('models') or [])
        entries = list(agg.get('model_entries') or [])
        return {
            'models': models[:100],
            'model_entries': entries[:100],
            'channel_models': [
                {
                    'id': d.get('id'),
                    'name': d.get('name'),
                    'ok': d.get('ok'),
                    'count': d.get('count'),
                    'latency_ms': d.get('latency_ms'),
                }
                for d in (agg.get('channel_models') or [])
            ],
            'total': len(models),
            'default_model': self.store.config.effective_model(),
            'api_base_url': self.store.config.effective_base_url(),
        }

    def _notify_sync_schedules(self, kind: str = 'all', task_id: str = '', force: bool = True) -> None:
        se = getattr(self, 'sync_engine', None)
        if se:
            se.trigger_schedules_changed(kind=kind, task_id=task_id, force=force)

    @staticmethod
    def _validate_daily_time_slots(daily_raw: str) -> list[tuple[int, int, int]]:
        return SchedulerEngine.validate_daily_time_slots(daily_raw)

    @classmethod
    def _extract_clock_from_goal(cls, goal: str) -> str:
        return ''

    def _compute_next_routine_run(self, r: dict, now_ts: float | None = None) -> float:
        eng = getattr(self, 'scheduler_engine', None)
        if eng is not None:
            return eng.compute_next_routine_run(r, now_ts)
        return SchedulerEngine._compute_next_routine_ts(r, now_ts)

    def _decorate_schedule_view(self) -> dict:
        return self.scheduler_engine.decorate_schedule_view()

    async def _tool_schedule_task(self, args: dict):
        action = str(args.get('action') or 'list').strip().lower()
        timers = self.store.state.setdefault('timers', [])
        routines = self.store.state.setdefault('routines', [])
        if action == 'list':
            return self._decorate_schedule_view()
        if action in ('snooze', 'dnd', 'quiet'):
            mins = max(0, min(1440, int(args.get('minutes') if args.get('minutes') is not None else 60)))
            await self.escalation.snooze(mins)
            return {'snoozed': mins > 0, 'minutes': mins, 'snooze_until': self.store.state.get('snooze_until', 0)}
        if action == 'add_timer':
            goal = str(args.get('goal') or '').strip()
            if not goal:
                return {'error': 'goal 不能为空'}
            bot_id = str(args.get('bot_id') or self.bot_context.get()).strip()
            at_time = str(args.get('at_time') or args.get('daily_time') or '').strip()
            if at_time:
                try:
                    due_at = self._parse_wall_time(at_time)
                except Exception as e:
                    return {'error': f'无法解析 at_time "{at_time}"（支持 "20:35" 或 "2026-11-05 07:30"）：{e}'}
            elif args.get('delay_seconds') is not None:
                due_at = time.time() + max(5, min(604800, int(args['delay_seconds'])))
            else:
                delay_min = max(1, min(10080, int(args.get('delay_minutes') or 5)))
                due_at = time.time() + delay_min * 60
            priority = str(args.get('priority') or 'normal').strip().lower()
            exec_action = str(args.get('exec_action') or ('ring_alarm' if priority == 'direct_alarm' else ('notify_user' if priority == 'direct_notify' else ''))).strip().lower()
            deadline_seconds = max(0, int(args.get('deadline_seconds') or 0))
            on_late = str(args.get('on_late') or 'run_with_notice').strip().lower()
            if on_late not in ('run_with_notice', 'notify_only', 'drop'):
                on_late = 'run_with_notice'
            cw = getattr(self, 'clock_wheel', None)
            if cw is not None and not args.get('force_duplicate'):
                dup = cw.find_cross_pool_duplicate(bot_id, due_at, goal, kind='timer')
                if dup is not None:
                    existing = dup['item']
                    if exec_action == 'ring_alarm':
                        existing['exec_action'] = 'ring_alarm'
                    elif exec_action == 'notify_user' and not existing.get('exec_action'):
                        existing['exec_action'] = 'notify_user'
                    if priority in ('direct_alarm', 'direct_notify', 'interrupt') and existing.get('priority') in (None, '', 'normal'):
                        existing['priority'] = priority
                    self.store.save()
                    return {
                        'scheduled': True,
                        'deduplicated': True,
                        'duplicate_pool': dup['pool'],
                        'duplicate_id': dup['id'],
                        'timer': existing if dup['pool'] == 'timer' else None,
                        'post': existing if dup['pool'] == 'blackboard' else None,
                    }
            item = {
                'id': 'timer-' + secrets.token_hex(6),
                'goal': goal[:2000],
                'bot_id': bot_id,
                'at_time': at_time,
                'due_at': due_at,
                'due_at_bj': self._fmt_bj(due_at),
                'priority': priority,
                'exec_action': exec_action,
                'direct_only': bool(args.get('direct_only', False)),
                'deadline_seconds': deadline_seconds,
                'on_late': on_late,
                'done': False,
                'created_at': time.time(),
            }
            timers.append(item)
            self.store.save()
            self.wake.set()
            self._notify_sync_schedules('timer', item['id'])
            return {'scheduled': True, 'timer': item}
        if action == 'add_routine':
            goal = str(args.get('goal') or args.get('text') or '').strip()
            if not goal:
                return {'ok': False, 'error': 'goal 不能为空'}
            try:
                raw_h = float(args.get('interval_hours') or 24.0)
            except (TypeError, ValueError):
                raw_h = 24.0
            hours = round(max(0.25, min(720.0, raw_h)), 2)
            if hours.is_integer():
                hours = int(hours)
            bot_id = str(args.get('bot_id') or self.bot_context.get()).strip()
            daily_time = str(args.get('daily_time') or args.get('at_time') or '').strip()
            if not daily_time:
                daily_time = self._extract_clock_from_goal(goal)
            if daily_time:
                try:
                    self._validate_daily_time_slots(daily_time)
                except ValueError as e:
                    return {'ok': False, 'error': str(e)}
            weekdays = str(args.get('weekdays') or '').strip()
            item = {
                'id': secrets.token_hex(8),
                'goal': goal[:2000],
                'bot_id': bot_id,
                'daily_time': daily_time,
                'weekdays': weekdays,
                'interval_hours': hours,
                'enabled': True,
                'last_run': time.time(),
                'created_at': time.time(),
            }
            if isinstance(args.get('merge_with_curiosity'), bool):
                item['merge_with_curiosity'] = args['merge_with_curiosity']
            item['next_run'] = self._compute_next_routine_run(item)
            item['next_run_bj'] = self._fmt_bj(item['next_run'])
            routines.append(item)
            self.store.save()
            self.wake.set()
            self._notify_sync_schedules('routine', item['id'])
            from core.curiosity_engine import detect_periodic_coalesce_opportunities
            hint = detect_periodic_coalesce_opportunities(self.store, goal, interval_hours=float(hours), exclude_id=item['id'])
            out = {'ok': True, 'scheduled': True, 'routine': item}
            if hint:
                out['coalesce_hint'] = hint
            return out
        if action == 'update':
            tid = str(args.get('task_id') or args.get('post_id') or args.get('id') or args.get('title') or args.get('name') or args.get('label') or '').strip()
            for r in routines:
                if r['id'] == tid or (tid and tid.lower() in str(r.get('goal') or '').lower()) or (not tid and len(routines) == 1):
                    if ('goal' in args or 'text' in args) and str(args.get('goal') or args.get('text') or '').strip():
                        r['goal'] = str(args.get('goal') or args.get('text') or '').strip()[:2000]
                    if 'bot_id' in args and str(args['bot_id']).strip():
                        r['bot_id'] = str(args['bot_id']).strip()
                    if 'daily_time' in args or 'at_time' in args:
                        new_dt = str(args.get('daily_time') or args.get('at_time') or '').strip()
                        if new_dt:
                            try:
                                self._validate_daily_time_slots(new_dt)
                            except ValueError as e:
                                return {'ok': False, 'error': str(e)}
                        r['daily_time'] = new_dt
                    if 'weekdays' in args:
                        r['weekdays'] = str(args['weekdays'] or '').strip()
                    if 'interval_hours' in args and args['interval_hours'] is not None:
                        uh = round(max(0.25, min(720.0, float(args['interval_hours']))), 2)
                        r['interval_hours'] = int(uh) if uh.is_integer() else uh
                    if isinstance(args.get('merge_with_curiosity'), bool):
                        r['merge_with_curiosity'] = args['merge_with_curiosity']
                    if 'enabled' in args and args['enabled'] is not None:
                        r['enabled'] = bool(args['enabled'])
                    r['next_run'] = self._compute_next_routine_run(r)
                    r['next_run_bj'] = self._fmt_bj(r['next_run'])
                    self.store.save()
                    self.wake.set()
                    self._notify_sync_schedules('routine', r['id'])
                    return {'ok': True, 'updated': True, 'routine': r}
            for t in timers:
                if t['id'] == tid:
                    if 'goal' in args and str(args['goal']).strip():
                        t['goal'] = str(args['goal']).strip()[:2000]
                    if 'at_time' in args or 'daily_time' in args:
                        raw_at = str(args.get('at_time') or args.get('daily_time') or '').strip()
                        try:
                            due_val = self._parse_wall_time(raw_at)
                        except ValueError as e:
                            return {'ok': False, 'error': str(e)}
                        t['at_time'] = raw_at
                        t['due_at'] = due_val
                        t['due_at_bj'] = self._fmt_bj(t['due_at'])
                    elif 'delay_minutes' in args and args['delay_minutes'] is not None:
                        t['due_at'] = time.time() + max(1, int(args['delay_minutes'])) * 60
                        t['due_at_bj'] = self._fmt_bj(t['due_at'])
                    t['done'] = False
                    self.store.save()
                    self.wake.set()
                    self._notify_sync_schedules('timer', t['id'])
                    return {'updated': True, 'timer': t}
            avail_ids = [f"{r['id']}:{str(r.get('goal') or '')[:30]}" for r in routines[:10]] + [f"{t['id']}:{str(t.get('goal') or '')[:30]}" for t in timers[:10]]
            return {'ok': False, 'error': f'未找到定时任务 "{tid}"（支持传 task_id / post_id / title 关键词）', 'available_tasks': avail_ids}
        if action == 'remove':
            tid = str(args.get('task_id') or args.get('post_id') or args.get('id') or args.get('title') or args.get('name') or args.get('label') or '').strip()
            before_n = len(timers) + len(routines)
            self.store.state['timers'] = [t for t in timers if t['id'] != tid]
            self.store.state['routines'] = [r for r in routines if r['id'] != tid]
            existed = (len(self.store.state['timers']) + len(self.store.state['routines'])) < before_n
            if existed:
                self.store.save()
                self._notify_sync_schedules('all', tid)
            avail_ids = [f"{r['id']}:{str(r.get('goal') or '')[:30]}" for r in self.store.state['routines'][:10]]
            return {'ok': existed, 'removed': existed, 'existed': existed, 'task_id': tid, **({'error': f'未找到定时任务 "{tid}"', 'available_tasks': avail_ids} if not existed else {})}
        return {'error': f'未知 schedule_task action: {action}'}

    async def _tool_bg_task(self, args: dict):
        action = str(args.get('action') or 'list').strip().lower()
        if action == 'list':
            return {
                'tasks': [
                    {k: v for k, v in info.items() if k != 'proc'}
                    for info in self.bg_tasks.values()
                ],
                'watchdogs': [
                    {k: v for k, v in w.items() if k != 'task'}
                    for w in self.watchdogs.values()
                ],
            }
        if action == 'logs':
            tid = str(args.get('task_id') or '').strip()
            info = self.bg_tasks.get(tid)
            if not info:
                return {'error': f'未找到后台任务 {tid}'}
            log_path = Path(info['log_file'])
            return {
                'task_id': tid,
                'status': info['status'],
                'exit_code': info.get('exit_code'),
                'logs': log_path.read_text('utf-8', errors='replace')[-12000:] if log_path.exists() else '',
            }
        if action == 'send_stdin':
            tid = str(args.get('task_id') or '').strip()
            input_text = str(args.get('input_text') or args.get('command') or '')
            info = self.bg_tasks.get(tid)
            if not info or info.get('status') != 'running':
                return {'error': f'未找到正在运行的后台任务 {tid}'}
            proc = info.get('proc')
            if not proc or proc.stdin is None:
                return {'error': f'任务 {tid} 不支持 stdin 输入'}
            try:
                line_bytes = (input_text if input_text.endswith('\n') else input_text + '\n').encode('utf-8')
                proc.stdin.write(line_bytes)
                await proc.stdin.drain()
                return {'sent_stdin': True, 'task_id': tid, 'bytes': len(line_bytes)}
            except Exception as e:
                return {'error': f'写入 stdin 失败: {e}'}
        if action == 'wait':
            tid = str(args.get('task_id') or '').strip()
            info = self.bg_tasks.get(tid)
            if not info:
                return {'ok': False, 'error': f'未找到后台任务 {tid}'}
            wait_sec = max(1, min(180, int(args.get('timeout') or 60)))
            proc = info.get('proc')
            timed_out = False
            if info.get('status') == 'running' and proc is not None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=wait_sec)
                    await asyncio.sleep(0.05)
                except asyncio.TimeoutError:
                    timed_out = True
            log_path = Path(info['log_file'])
            tail = log_path.read_text('utf-8', errors='replace')[-8000:] if log_path.exists() else ''
            return {
                'ok': True,
                'task_id': tid,
                'status': info.get('status'),
                'exit_code': info.get('exit_code'),
                'timed_out': timed_out,
                'output': tail,
            }
        if action == 'kill':
            tid = str(args.get('task_id') or '').strip()
            info = self.bg_tasks.get(tid)
            if not info:
                return {'ok': False, 'killed': False, 'error': f'未找到后台任务 {tid}'}
            if info.get('status') != 'running':
                return {'ok': False, 'killed': False, 'task_id': tid, 'status': info.get('status'), 'error': f'后台任务 {tid} 当前状态为 {info.get("status")}，无法终止'}
            proc = info.get('proc')
            if not proc:
                return {'ok': False, 'killed': False, 'task_id': tid, 'error': f'后台任务 {tid} 缺少可控进程句柄'}
            try:
                if getattr(proc, 'returncode', None) is None:
                    if sys.platform == 'linux':
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.wait(), timeout=3.0)
            except Exception as e:
                return {'ok': False, 'killed': False, 'error': f'终止后台任务失败: {e}'}
            info['status'] = 'killed'
            return {'ok': True, 'killed': True, 'task_id': tid}
        if action == 'start':
            cmd = str(args.get('command') or '').strip()
            if not cmd:
                return {'error': 'command 不能为空'}
            if normalize_node_source(self.source_context.get()) in ('pc', 'pc2') and self.is_dsh_online():
                pc_bg = await self._raw_dsh_rpc('pc_shell', {'command': cmd, 'background': True, 'cwd': args.get('cwd')}, timeout=15.0)
                if isinstance(pc_bg, dict) and pc_bg.get('ok'):
                    return {'started': True, 'node': 'pc', **pc_bg}
                return {'ok': False, 'error': str((pc_bg or {}).get('error') or 'Windows 后台任务启动失败')}
            tid = 'bg-' + secrets.token_hex(4)
            log_dir = self.harness.root / '.bg_logs'
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / f'{tid}.log'
            owner_bot = self.bot_context.get()
            owner_source = normalize_node_source(self.source_context.get() or 'phone')
            notify_exit = bool(args.get('notify_on_exit', True))
            env = scrub_subprocess_env(extra={
                'HOME': str(self.harness.root),
                'TZ': 'Asia/Shanghai',
                'API_BASE_URL': self.store.config.effective_base_url(),
                'DEFAULT_MODEL': self.store.config.effective_model(),
            })
            out_f = log_file.open('w', encoding='utf-8')
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd, cwd=str(self.harness.root), stdin=asyncio.subprocess.PIPE, stdout=out_f, stderr=asyncio.subprocess.STDOUT,
                    start_new_session=(sys.platform == 'linux'), env=env
                )
            except Exception as e:
                out_f.close()
                return {'ok': False, 'error': f'启动后台任务失败: {e}'}
            info = {
                'task_id': tid,
                'command': cmd[:300],
                'bot_id': owner_bot,
                'status': 'running',
                'started_at': time.time(),
                'log_file': str(log_file),
                'proc': proc,
            }
            self.bg_tasks[tid] = info
            self._prune_bg_tasks()

            async def watch():
                try:
                    rc = await proc.wait()
                    info['status'] = 'done' if rc == 0 else 'failed'
                    info['exit_code'] = rc
                    info['finished_at'] = time.time()
                finally:
                    out_f.close()
                    self._prune_bg_tasks()
                if notify_exit:
                    full_tail = log_file.read_text('utf-8', errors='replace')[-12000:] if log_file.exists() else ''
                    short_tail = full_tail[-500:]
                    ctx_id = self.context_search.store_tool_context(
                        'bg_task', {'task_id': tid, 'cmd': cmd[:120]}, full_tail or '(空输出)', bot_id=owner_bot
                    ) if full_tail else ''
                    ctx_hint = f'\n完整日志已归档 (ctx_id="{ctx_id}")，需要细节可用 `memory(op="ctx", ctx_id="{ctx_id}")` 检索。' if ctx_id else ''
                    msg_text = f'⚙️ [后台任务 `{tid}` 已结束 (exit={info.get("exit_code")})]\n命令：`{cmd[:120]}`\n尾部摘要：\n```\n{short_tail}\n```{ctx_hint}'
                    m = self.store.message(
                        'assistant',
                        msg_text,
                        kind='result',
                        bot_id=owner_bot,
                        source=owner_source,
                        is_bg_result=True,
                        non_terminal=True,
                        turn_id=f'bg-{tid}',
                        notify=False,
                    )
                    oseq = int(m.get('order_seq') or m.get('seq') or 0)
                    self.stream_seq = max(self.stream_seq + 1, oseq)
                    m['seq'] = oseq
                    self.store.sql.upsert_message(m)
                    self.context_search.index_message(m)
                    await self.publish({
                        'type': 'message',
                        'message': m,
                        'seq': self.stream_seq,
                        'order_seq': oseq,
                        'state_seq': getattr(getattr(self, 'sync_engine', None), 'state_seq', oseq),
                    })
                    if self.is_node_bot_busy(owner_bot, owner_source):
                        if hasattr(self, 'clock_wheel'):
                            self.clock_wheel.enqueue_urgent_interrupt(
                                owner_bot,
                                owner_source,
                                f'⚙️ [后台任务 `{tid}` 已结束 (exit={info.get("exit_code")})]\n命令：`{cmd[:120]}`\n尾部摘要：\n```\n{short_tail[:400]}\n```{ctx_hint}',
                                item_id=tid,
                                kind='bg_task',
                            )
                        cur_stream = (
                            getattr(self, 'streams_by_node_bot', {}).get(f'{owner_source}:{owner_bot}')
                            or getattr(self, 'streams_by_bot', {}).get(owner_bot)
                        )
                        if isinstance(cur_stream, dict) and cur_stream.get('bot_id') == owner_bot:
                            self.stream_seq += 1
                            snap = dict(cur_stream)
                            snap['seq'] = self.stream_seq
                            await self.publish({'type': 'stream', 'stream': snap})
                    elif info.get('status') != 'killed' and hasattr(self, 'run_scheduled'):
                        wake_goal = (
                            f'【后台任务 `{tid}` 已结束 (exit={info.get("exit_code")})】\n'
                            f'命令：`{cmd[:200]}`\n'
                            f'尾部输出：\n```\n{short_tail or "(空输出)"}\n```{ctx_hint}\n'
                            '请立即查看并核对此次后台任务结果，必要时继续后续步骤或清晰向用户汇报。'
                        )
                        asyncio.create_task(
                            self.run_scheduled(
                                wake_goal,
                                bot_id=owner_bot,
                                label=f'后台任务完成 ({tid})',
                            )
                        )

            asyncio.create_task(watch())
            return {'started': True, 'task_id': tid, 'log_file': f'.bg_logs/{tid}.log'}
        return {'error': f'未知 bg_task action: {action}'}

    def _resolve_watchdog(self, args: dict) -> tuple[str, dict | None]:
        tid = str(args.get('task_id') or args.get('watchdog_id') or args.get('id') or args.get('name') or '').strip()
        w = self.watchdogs.get(tid)
        if not w:
            for k, cand in list(self.watchdogs.items()):
                if cand.get('name') == tid:
                    return k, cand
        return tid, w

    async def _tool_watchdog_task(self, args: dict):
        """Zero-token self-written watchdog scripts that silently monitor targets and wake the agent ONLY on TRIGGER:"""
        action = str(args.get('action') or 'list').strip().lower()
        if action == 'list':
            return {'watchdogs': [{k: v for k, v in w.items() if k != 'task'} for w in self.watchdogs.values()]}
        if action in ('stop', 'kill', 'remove'):
            tid, w = self._resolve_watchdog(args)
            if not w:
                return {'ok': False, 'error': f'未找到盯盘哨兵 {tid}'}
            w['status'] = 'stopped'
            t = w.get('task')
            if t and not t.done():
                t.cancel()
            self.watchdogs.pop(tid, None)
            self._notify_sync_schedules('watchdog', tid)
            return {'stopped': True, 'task_id': tid, 'watchdog_id': tid}
        if action == 'logs':
            tid, w = self._resolve_watchdog(args)
            if not w:
                return {'ok': False, 'error': f'未找到盯盘哨兵 {tid}'}
            return {k: v for k, v in w.items() if k != 'task'}
        if action == 'run_now':
            tid, w = self._resolve_watchdog(args)
            if not w:
                return {'ok': False, 'error': f'未找到盯盘哨兵 {tid}'}
            out = await self._exec_watchdog_once(w)
            return {'ran': True, 'task_id': tid, 'watchdog_id': tid, 'output': out}

        if action == 'start':
            name = str(args.get('name') or '监控哨兵').strip()[:80]
            script = str(args.get('script') or '').strip()
            if not script:
                return {'error': 'script 不能为空（请提供 Python 或 Bash 探针脚本，当检测到目标变化时打印 "TRIGGER: 详情"）'}
            interval_sec = max(15, min(86400, int(args.get('interval_sec') or 120)))
            owner_bot = str(args.get('bot_id') or self.bot_context.get() or 'companion').strip()
            tid = 'wd-' + secrets.token_hex(4)
            wd_dir = self.harness.root / '.watchdogs'
            wd_dir.mkdir(parents=True, exist_ok=True)
            is_py = ('import ' in script or 'print(' in script or script.startswith('#!/usr/bin/env python'))
            ext = '.py' if is_py else '.sh'
            script_path = wd_dir / f'{tid}{ext}'
            script_path.write_text(script, encoding='utf-8')
            w_info: dict = {
                'task_id': tid,
                'name': name,
                'bot_id': owner_bot,
                'interval_sec': interval_sec,
                'script_path': str(script_path),
                'status': 'running',
                'runs': 0,
                'triggers': 0,
                'last_run_at': 0.0,
                'last_output': '',
                'created_at': time.time(),
            }

            async def _loop():
                while w_info.get('status') == 'running':
                    try:
                        await self._exec_watchdog_once(w_info)
                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        w_info['last_output'] = f'Error: {e}'
                    await asyncio.sleep(interval_sec)

            w_info['task'] = asyncio.create_task(_loop())
            self.watchdogs[tid] = w_info
            self._notify_sync_schedules('watchdog', tid)
            return {
                'started': True,
                'task_id': tid,
                'name': name,
                'interval_sec': interval_sec,
                'note': '哨兵已在后台以 0 Token 模式静默运行；当脚本输出包含 "TRIGGER:" 时将自动唤醒你向用户汇报。',
            }
        return {'error': f'未知 watchdog_task action: {action}'}

    def _prune_bg_tasks(self, keep_finished: int = 40) -> None:
        finished = [
            (tid, info.get('finished_at') or info.get('started_at') or 0.0)
            for tid, info in self.bg_tasks.items()
            if isinstance(info, dict) and info.get('status') != 'running'
        ]
        if len(finished) <= keep_finished:
            return
        finished.sort(key=lambda it: it[1])
        for tid, _ in finished[:len(finished) - keep_finished]:
            self.bg_tasks.pop(tid, None)

    async def shutdown_background_tasks(self) -> None:
        for wid, w_info in list(getattr(self, 'watchdogs', {}).items()):
            w_info['status'] = 'stopped'
            t = w_info.get('task')
            if t and not t.done():
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        for tid, info in list(getattr(self, 'bg_tasks', {}).items()):
            proc = info.get('proc') if isinstance(info, dict) else None
            if proc is not None and getattr(proc, 'returncode', None) is None:
                with contextlib.suppress(Exception):
                    if sys.platform == 'linux':
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                if isinstance(info, dict):
                    info['status'] = 'killed'

    async def _exec_watchdog_once(self, w_info: dict) -> str:
        script_path = Path(w_info['script_path'])
        cmd = f'{sys.executable} "{script_path}"' if script_path.suffix == '.py' else f'bash "{script_path}"'
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(self.harness.root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=scrub_subprocess_env(),
        )
        try:
            raw_out, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            raw_out = b'Timeout after 30s'
        out_str = raw_out.decode('utf-8', errors='replace').strip()
        w_info['runs'] = int(w_info.get('runs') or 0) + 1
        w_info['last_run_at'] = time.time()
        w_info['last_output'] = out_str[-800:]
        if 'TRIGGER:' in out_str:
            w_info['triggers'] = int(w_info.get('triggers') or 0) + 1
            trig_detail = out_str.split('TRIGGER:', 1)[1].strip()[:1500]
            asyncio.create_task(
                self.run_scheduled(
                    f'【盯盘哨兵「{w_info["name"]}」命中触发条件】\n探针输出：{trig_detail}\n请立即向用户汇报此变化并给出处理建议。',
                    bot_id=w_info.get('bot_id', 'companion'),
                    label=f'哨兵触发 ({w_info["name"]})',
                )
            )
        return out_str[-800:]

    async def _tool_conversation_search(self, args: dict):
        query = str(args.get('query') or '').strip()
        cur_bot = self.room_context.get() or self.bot_context.get()
        raw_bot_arg = str(args.get('bot_id') or '').strip()
        bot_id = raw_bot_arg if raw_bot_arg else cur_bot
        limit = int(args.get('limit') or 10)
        try:
            self.context_search.sync_store_messages(self.store.state.get('messages', []))
        except Exception:
            pass
        active_ids = {
            str(m.get('id'))
            for m in self.store.state.get('messages', [])
            if m.get('bot_id', 'companion') == cur_bot and m.get('kind') != 'compact_summary'
        }
        return self.context_search.conversation_search(
            query=query,
            bot_id=bot_id,
            limit=limit,
            exclude_msg_ids=active_ids if bot_id in ('all', cur_bot, '') else set(),
            vault=self.vault,
        )

    async def _tool_ctx_search(self, args: dict):
        query = str(args.get('query') or '').strip()
        ctx_id = str(args.get('ctx_id') or '').strip()
        limit = int(args.get('limit') or 8)
        cur_bot = self.room_context.get() or self.bot_context.get()
        bot_filter = str(args.get('bot_id') or cur_bot or '').strip()
        return self.context_search.ctx_search(query=query, ctx_id=ctx_id, limit=limit, bot_id=bot_filter)

    def _search_vault_files(self, query: str, limit: int = 8, bot_id: str = '') -> list[dict]:
        if not query or self.vault is None or limit <= 0:
            return []
        tokens = [t for t in re.split(r'\s+', query.strip()) if t]
        rx_full = re.compile(re.escape(query), re.IGNORECASE)
        hits = []
        eff_bot = str(bot_id or '').strip()
        for f in self.vault.files():
            path_str = f['path']
            if eff_bot and eff_bot != 'all':
                if path_str.startswith('timeline/bots/') and not path_str.startswith(f'timeline/bots/{eff_bot}/'):
                    continue
                if path_str.startswith('conversations/') and '_' in path_str[14:] and not path_str[14:].startswith(f'{eff_bot}_'):
                    continue
            content = self.vault.read(path_str)
            if rx_full.search(content) or rx_full.search(path_str) or (tokens and any(t.lower() in content.lower() or t.lower() in path_str.lower() for t in tokens)):
                matched_lines = [ln.strip() for ln in content.splitlines() if any(t.lower() in ln.lower() for t in (tokens or [query]))]
                snippet = ('\n'.join(matched_lines[:4])[:360] if matched_lines else content[:360])
                hits.append({'path': 'memory_vault/' + path_str, 'snippet': snippet})
                if len(hits) >= limit:
                    break
        return hits[:limit]

    async def _tool_memory_search(self, args: dict):
        query = str(args.get('query') or '').strip()
        cur_bot = self.room_context.get() or self.bot_context.get()
        bot_id = str(args.get('bot_id') or cur_bot or 'all').strip()
        ctx_id = str(args.get('ctx_id') or '').strip()
        raw_lim = args.get('limit')
        if raw_lim is not None and int(raw_lim) <= 0:
            return {'ok': False, 'query': query, 'hits': [], 'conversation_hits': [], 'ctx_hits': [], 'error': 'limit 必须大于 0'}
        if not query and not ctx_id:
            return {'ok': False, 'query': '', 'hits': [], 'conversation_hits': [], 'ctx_hits': [], 'error': '检索词 query 不能为空'}
        limit = max(1, min(20, int(raw_lim) if raw_lim is not None else 6))
        if ctx_id:
            ctx_res = self.context_search.ctx_search(query=query, ctx_id=ctx_id, limit=limit, bot_id=bot_id)
            if isinstance(ctx_res, dict) and ctx_res.get('ok') is False:
                return ctx_res
            vault_hits = self._search_vault_files(query, min(limit, max(1, limit // 2)), bot_id=bot_id) if query else []
            raw_ctx_hits = (ctx_res.get('chunks') or ctx_res.get('hits') or [])[:limit] if isinstance(ctx_res, dict) else []
            return {
                'ok': True,
                'query': query,
                'hits': vault_hits[:limit],
                'ctx_hits': [
                    {**c, 'content': str(c.get('content') or '')[:400]} if isinstance(c, dict) else c
                    for c in raw_ctx_hits
                ],
                'ctx_id': ctx_id,
            }
        vault_hits = self._search_vault_files(query, limit, bot_id=bot_id)
        conv_res = await self._tool_conversation_search({'query': query, 'bot_id': bot_id, 'limit': limit})
        ctx_res = self.context_search.ctx_search(query=query, ctx_id='', limit=limit, bot_id=bot_id)
        ltm = getattr(getattr(self, 'memory_engine', None), 'long_term_memory', None)
        exp_hits = ltm.recall(query, bot_id='' if bot_id == 'all' else bot_id, limit=limit, max_chars=2400) if ltm is not None else []
        raw_conv_hits = (conv_res.get('results') or conv_res.get('hits') or [])[:limit] if isinstance(conv_res, dict) else []
        raw_ctx_hits = (ctx_res.get('chunks') or ctx_res.get('hits') or [])[:limit] if isinstance(ctx_res, dict) else []
        total_found = len(vault_hits) + len(raw_conv_hits) + len(raw_ctx_hits) + len(exp_hits)
        out_ms: dict = {
            'ok': True,
            'query': query,
            'hits': vault_hits[:limit],
            'experience_hits': exp_hits[:limit],
            'conversation_hits': [
                {**c, 'text': str(c.get('text') or '')[:400]} if isinstance(c, dict) else c
                for c in raw_conv_hits
            ],
            'ctx_hits': [
                {**c, 'content': str(c.get('content') or '')[:400]} if isinstance(c, dict) else c
                for c in raw_ctx_hits
            ],
        }
        if total_found > 0:
            out_ms['note'] = '已一次性检索长期经验库 (experiences L0/L1)、memory_vault、历史对话归档与工具大输出缓存，无需再重复调用其他搜索工具。'
        return out_ms

    async def _tool_present_choices(self, args: dict):
        raw_choices = args.get('choices') or args.get('options') or args.get('items')
        prompt_txt = str(args.get('prompt') or args.get('question') or args.get('title') or '').strip()
        if isinstance(raw_choices, str):
            raw_choices = [ln.strip() for ln in re.split(r'[\n;]+', raw_choices) if ln.strip()]
        if not isinstance(raw_choices, list):
            return {'error': 'choices 必须是列表（如 ["直接帮我填好 | 顺手把我的信息填进去提交", "先发截图看看 | 先截张图给我确认一下"]）'}
        cleaned = self._sanitize_choices_list(raw_choices)
        if not cleaned:
            return {'error': 'choices 不能为空，且必须是站在用户第一人称视角的简短可执行选项（禁止传入总结汇报条目）'}
        self.choices_var.set(cleaned)
        self._explicit_human_gate_in_turn = True
        if prompt_txt:
            self.choices_prompt_var.set(prompt_txt)
        bot_id = self.room_context.get() or self.bot_context.get()
        cur_exec = self.executions_by_bot.get(bot_id) or self.execution
        if isinstance(cur_exec, dict):
            cur_exec['choices'] = cleaned
        return {
            'ok': True,
            'choices_attached': [item['label'] for item in cleaned],
            'note': '快捷选项已就绪，将在你输出最终正文回复时自动挂载到正文底部。请直接撰写并输出完整正文回复（不要再重复调用 choices）。',
        }

    async def _tool_memory_pin(self, args: dict):
        action = str(args.get('action') or 'list').strip().lower()
        if action == 'list':
            return {'ok': True, 'pins': self.memory_engine.list_pins()}
        if action in ('pin', 'add', 'remember'):
            src = str(args.get('source') or self.source_context.get() or 'phone').strip().lower()
            return self.memory_engine.add_pin(str(args.get('text') or ''), source=('pc' if src == 'pc' else 'phone'))
        if action in ('unpin', 'remove', 'delete', 'forget'):
            return self.memory_engine.remove_pin(str(args.get('pin_id') or args.get('text') or ''))
        return {'error': f'未知 memory_pin action: {action}'}

    async def _tool_blackboard(self, args: dict):
        eff = dict(args or {})
        act = str(eff.get('action') or eff.get('op') or 'read').strip().lower()
        eff['action'] = act
        if act in ('add_routine', 'update_routine', 'remove_routine', 'list_routines'):
            mapped = {'add_routine': 'add_routine', 'update_routine': 'update', 'remove_routine': 'remove', 'list_routines': 'list'}[act]
            eff['action'] = mapped
            return await self._tool_schedule_task(eff)
        if not eff.get('text') and not eff.get('content'):
            fallback_txt = str(eff.get('prompt') or eff.get('label') or '').strip()
            if fallback_txt:
                eff['text'] = fallback_txt
        if not eff.get('title') and eff.get('label'):
            eff['title'] = str(eff['label']).strip()[:140]
        if act == 'alarm':
            has_when = any(
                eff.get(k) not in (None, '', 0)
                for k in ('when', 'run_at', 'at', 'delay_minutes', 'delay_seconds', 'at_time', 'daily_time', 'interval_hours', 'cron_expr')
            )
            if not has_when:
                return await self._tool_emergency_alarm({
                    'title': eff.get('title') or eff.get('label') or '紧急呼叫',
                    'reason': eff.get('reason') or eff.get('text') or eff.get('content') or eff.get('prompt') or '紧急提醒',
                    'duration_sec': eff.get('duration_sec') or 18,
                })
            eff['alarm'] = True
            eff['action'] = 'schedule'
        return await self.blackboard_engine.execute(eff)

    async def _tool_peek_bots(self, args: dict):
        return self.blackboard_engine.peek_bots(args)

    async def _tool_custom_tool(self, args: dict):
        action = str(args.get('action') or 'list').strip().lower()
        plugins_dir = self.harness.root / 'plugins'
        plugins_dir.mkdir(parents=True, exist_ok=True)
        skills_dir = self.vault.root / 'skills'
        skills_dir.mkdir(parents=True, exist_ok=True)

        if action in ('list_skills', 'skills'):
            skills = []
            for sf in sorted(skills_dir.glob('*.md')):
                raw_txt = sf.read_text('utf-8', errors='replace')
                fm_desc = ''
                fm_name = ''
                body_txt = raw_txt
                if raw_txt.lstrip().startswith('---'):
                    fm_match = re.match(r'^\s*---\s*\r?\n([\s\S]*?)\r?\n---\s*(?:\r?\n|$)', raw_txt)
                    if fm_match:
                        fm_block = fm_match.group(1)
                        body_txt = raw_txt[fm_match.end():]
                        for fm_ln in fm_block.splitlines():
                            if ':' in fm_ln:
                                fk, fv = fm_ln.split(':', 1)
                                fk_clean = fk.strip().lower()
                                fv_clean = fv.strip().strip('"\'')
                                if fk_clean == 'description' and fv_clean:
                                    fm_desc = fv_clean
                                elif fk_clean == 'name' and fv_clean:
                                    fm_name = fv_clean
                body_lines = [ln.strip() for ln in body_txt.splitlines() if ln.strip()]
                heading = next((ln.lstrip('#').strip() for ln in body_lines if ln.startswith('#')), '')
                title = heading or (body_lines[0].lstrip('#').strip() if body_lines else (fm_name or sf.stem))
                desc = fm_desc or (body_lines[1] if len(body_lines) > 1 else '')
                skills.append({'name': sf.stem, 'path': f'memory_vault/skills/{sf.name}', 'title': title[:80], 'summary': desc[:180]})
            return {'ok': True, 'skills': skills, 'note': '按需调用 admin(op="skill", action="open", name="...") 或 read_file("memory_vault/skills/...") 打开指定 Skill，日常不占额外 Token'}
        if action in ('open_skill', 'read_skill', 'load_skill'):
            s_name = re.sub(r'[^a-zA-Z0-9_\-\u4e00-\u9fa5]', '-', str(args.get('name') or '')).strip('-').lower()
            if not s_name:
                return {'error': '请提供要打开的 skill name'}
            sf = skills_dir / f'{s_name}.md'
            if not sf.exists():
                return {'error': f'未找到技能 "{s_name}"，可先用 action="list_skills" 查看可用技能列表'}
            return {'ok': True, 'name': s_name, 'path': f'memory_vault/skills/{sf.name}', 'content': sf.read_text('utf-8', errors='replace')}
        if action in ('save_skill', 'create_skill', 'write_skill'):
            s_name = re.sub(r'[^a-zA-Z0-9_\-\u4e00-\u9fa5]', '-', str(args.get('name') or '')).strip('-').lower()
            desc = str(args.get('description') or '').strip()
            body = str(args.get('content') or args.get('code') or '').strip()
            if not s_name or not body:
                return {'error': 'save_skill 需要提供 name 与 content（技能步骤/经验总结 Markdown）'}
            if body.startswith('---') or body.startswith('#'):
                md_text = body if body.endswith('\n') else body + '\n'
            else:
                safe_desc = (desc or s_name).replace('\n', ' ').strip()
                md_text = f'---\nname: {s_name}\ndescription: {safe_desc}\n---\n\n# {s_name}\n\n{body}\n'
            rel_p = f'skills/{s_name}.md'
            self.vault.write(rel_p, md_text)
            return {'ok': True, 'saved_skill': s_name, 'path': f'memory_vault/{rel_p}'}
        if action in ('delete_skill', 'remove_skill'):
            s_name = re.sub(r'[^a-zA-Z0-9_\-\u4e00-\u9fa5]', '-', str(args.get('name') or '')).strip('-').lower()
            if not s_name:
                return {'ok': False, 'deleted': False, 'existed': False, 'error': '请提供要删除的 skill 名称 name'}
            sf = skills_dir / f'{s_name}.md'
            existed = sf.exists()
            if existed:
                sf.unlink()
            return {'ok': existed, 'deleted': existed, 'existed': existed, 'deleted_skill': s_name, **({'error': f'未找到技能 "{s_name}"'} if not existed else {})}

        if action == 'list':
            summary = self.harness.extension_registry.summary()
            skills_list = [sf.stem for sf in sorted(skills_dir.glob('*.md'))]
            return {**summary, 'available_skills': skills_list}
        if action == 'delete':
            raw_name = str(args.get('name') or '').strip()
            t_name = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fa5]', '_', raw_name).strip('_').lower()
            if not t_name:
                return {'ok': False, 'deleted': False, 'existed': False, 'error': '请提供要删除的自定义工具名称 name'}
            safe_t_name = self.harness.extension_registry._safe_openai_tool_name(t_name)
            p_file = plugins_dir / f'{t_name}.py'
            existed = p_file.exists() or (t_name in self.harness.extension_registry.tool_plugins) or (safe_t_name in self.harness.extension_registry.tool_plugins)
            if p_file.exists():
                p_file.unlink()
            self.harness.extension_registry.tool_plugins.pop(t_name, None)
            self.harness.extension_registry.tool_plugins.pop(safe_t_name, None)
            return {'ok': existed, 'deleted': existed, 'existed': existed, 'name': t_name, **({'error': f'未找到自定义工具 "{raw_name}"'} if not existed else {})}
        if action == 'create':
            if hasattr(self, '_is_non_user_execution_context') and self._is_non_user_execution_context():
                return {'ok': False, 'error': '权限锁定：仅允许用户在直接对话中创建自定义可执行工具插件'}
            raw_name = str(args.get('name') or '').strip()
            t_name = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fa5]', '_', raw_name).strip('_').lower()
            code = str(args.get('code') or '').strip()
            if not t_name or not code:
                return {
                    'ok': False,
                    'error': 'create 需要提供合法的 name（支持中英文、数字与下划线）和 code (支持 def run(args, context) / def run(args) / def run(param_name) / async def run(...))',
                }
            code_lines = code.splitlines()
            try:
                ast.parse(code)
            except SyntaxError as se:
                return {
                    'ok': False,
                    'total_lines': len(code_lines),
                    'error': f'Python 语法校验未通过 (line {se.lineno} / 共 {len(code_lines)} 行): {se.msg}',
                }
            desc = str(args.get('description') or f'Custom tool {t_name}').strip()
            cls_id = re.sub(r'[^a-zA-Z0-9_]', '_', t_name).strip('_')
            if not cls_id or not cls_id.isidentifier():
                import hashlib as _hl
                cls_id = 'u_' + _hl.md5(t_name.encode('utf-8')).hexdigest()[:10]
            try:
                params_schema = json.loads(str(args.get('parameters_json') or '{"type":"object","properties":{}}'))
            except Exception:
                params_schema = {'type': 'object', 'properties': {}}
            plugin_src, header_lines = build_custom_plugin_source(t_name, cls_id, desc, params_schema, code_lines)
            try:
                ast.parse(plugin_src)
            except SyntaxError as se:
                adj_line = max(1, (se.lineno or header_lines) - header_lines)
                return {'ok': False, 'total_lines': len(code_lines), 'error': f'Python 语法校验未通过 (line {adj_line} / 共 {len(code_lines)} 行): {se.msg}'}
            p_file = plugins_dir / f'{t_name}.py'
            p_file.write_text(plugin_src, encoding='utf-8')
            loaded = self.harness.extension_registry.discover_plugins_dir(plugins_dir)
            return {
                'ok': True,
                'created': True,
                'name': t_name,
                'total_lines': len(code_lines),
                'loaded_plugins': loaded,
                'supported_signatures': [
                    'def run(args, context):',
                    'def run(args):',
                    'def run(named_param, ...):',
                    'def run(*args, **kwargs):',
                ],
            }
        return {'ok': False, 'error': f'未知 custom_tool action: {action}'}

