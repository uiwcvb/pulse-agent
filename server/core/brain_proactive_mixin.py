"""主动关怀、空闲研究、定时巡检与睡眠记忆整合模块 (`server/core/brain_proactive_mixin.py`)."""
from __future__ import annotations
import asyncio, contextlib, json, logging, secrets, time
from datetime import datetime
from assistants import get_assistant, resolve_bot_for_source
from core.task_continuity import TaskContinuityManager
from core.xml_tool_parser import is_silent_proactive_output
from core.tool_schemas import is_tool_allowed, normalize_bot_tools
from harness import TOOLS
from storage import normalize_node_source
from vault_manager import TZ, now_iso

logger = logging.getLogger(__name__)


class BrainProactiveMixin:
    def _is_current_bot_vision_capable(self) -> bool:
        return True

    async def _tool_stay_silent(self, args: dict) -> dict:
        reason = str((args or {}).get('reason') or (args or {}).get('summary') or (args or {}).get('text') or '当前无重要新情况，保持沉默不打扰用户').strip()[:300]
        try:
            snooze_min = max(0, min(1440, int((args or {}).get('snooze_minutes') or 0)))
        except (ValueError, TypeError):
            snooze_min = 0
        now_ts = time.time()
        if 'minutes' in (args or {}):
            try:
                dnd_min = max(0, int((args or {}).get('minutes') or 0))
            except (ValueError, TypeError):
                dnd_min = 0
            if dnd_min > 0:
                self.store.state['snooze_until'] = now_ts + dnd_min * 60
                snooze_min = snooze_min or dnd_min
            else:
                self.store.state['snooze_until'] = 0
        if self.activity == 'curiosity':
            cur_min = max(1, int(getattr(self.store.config, 'curiosity_interval_minutes', 30) or 30))
            delay_min = snooze_min if snooze_min > 0 else cur_min
            self.store.state['last_curiosity'] = now_ts
            self.store.state['curiosity_next_run'] = now_ts + delay_min * 60
        self.store.save()
        if self.vault is not None:
            try:
                self.vault.append_work_log(f'[静默不打扰] {reason}' + (f' (延后 {snooze_min} 分钟)' if snooze_min > 0 else ''))
            except Exception:
                pass
        return {
            'ok': True,
            'status': 'silent',
            '_stay_silent': True,
            'reason': reason,
            'snooze_minutes': snooze_min,
        }

    def _finish_curiosity_cycle(self, status: str, bot_id: str, detail: str = '', snooze_min: int | None = None) -> None:
        self.scheduler_engine.record_curiosity_cycle(status, bot_id, detail=detail, snooze_min=snooze_min)

    async def curiosity(self, manual=False, bot_id=None, source: str = 'phone'):
        t = asyncio.create_task(self._curiosity_impl(manual=manual, bot_id=bot_id, source=source))
        try:
            return await t
        except asyncio.CancelledError:
            cur = asyncio.current_task()
            outer_cancelling = bool(cur and hasattr(cur, 'cancelling') and cur.cancelling())
            if getattr(t, '_dsb_preempted', False) and not outer_cancelling:
                return {'suppressed': True, 'reason': 'preempted'}
            raise

    def _get_cache_keepalive(self):
        mgr = getattr(self, 'cache_keepalive', None)
        if mgr is None:
            from core.cache_keepalive import CacheKeepaliveManager
            mgr = CacheKeepaliveManager()
            self.cache_keepalive = mgr
        return mgr

    def _touch_cache_keepalive(self, source: str, bot_id: str) -> None:
        self._get_cache_keepalive().touch(source, bot_id)

    def _get_curiosity_engine(self):
        eng = getattr(self, 'curiosity_engine', None)
        if eng is None:
            from core.curiosity_engine import CuriosityEngine
            eng = CuriosityEngine(self)
            self.curiosity_engine = eng
        return eng

    async def _curiosity_impl(self, manual=False, bot_id=None, source: str = 'phone'):
        return await self._get_curiosity_engine().run_cycle(manual=manual, bot_id=bot_id, source=source)

    async def research(self, goal=None, manual=False, bot_id=None, record_user=False):
        bot_id = bot_id or self.store.state['primary_bot_id']
        get_assistant(self.store, bot_id)
        marker = self.bot_context.set(bot_id)
        room_marker = self.room_context.set(bot_id)
        origin_marker = self.turn_origin_context.set('autonomous') if hasattr(self, 'turn_origin_context') else None
        try:
            return await self._research(goal, manual, record_user)
        finally:
            if origin_marker is not None:
                self.turn_origin_context.reset(origin_marker)
            self.room_context.reset(room_marker)
            self.bot_context.reset(marker)

    async def _research(self, goal=None, manual=False, record_user=False):
        bot_id = self.bot_context.get()
        async with self.lock, self._get_bot_lock(bot_id, 'phone'):
            bot = get_assistant(self.store, bot_id)
            turn_token = self.turn_id_var.set('turn-' + secrets.token_hex(6))
            prefix_token = self.stream_prefix_var.set('')
            self.activity = 'deep_research'
            cur_task = asyncio.current_task()
            self.active_task = cur_task
            if cur_task:
                self.active_tasks_by_bot[bot_id] = cur_task
            my_exec = {'bot_id':bot_id, 'source':'phone', 'goal':goal or '整理任务池', 'trace':[], 'tool':None, 'started_at':time.time()}
            self.executions_by_node_bot[f'phone:{bot_id}'] = my_exec
            self.executions_by_bot[bot_id] = my_exec
            self.execution = my_exec
            try:
                if record_user:
                    TaskContinuityManager.touch_foreground_activity(self.store)
                    message = self.store.message('user', goal, bot_id=bot_id)
                    self.stream_seq += 1
                    self.vault.log('用户：' + goal)
                    await self.publish({'type':'message', 'message':message, 'seq':self.stream_seq})
                    visible = {m['id'] for m in self.store.state['messages'] if m.get('bot_id','companion') == bot_id}
                    await self.escalation.ack([mid for mid in self.store.state['pending'] if mid in visible])
                ready = next((x for x in reversed(self.store.state['opportunities']) if x['ready'] and not x['done']), None)
                goal = goal or (ready['goal'] if ready else None)
                api_key = self.store.config.effective_api_key(big=True) or self.store.config.effective_api_key(big=False)
                model = self.store.config.effective_model(bot_model=bot.get('model',''), big=True) or self.store.config.effective_model(bot_model=bot.get('model',''), big=False)
                if api_key and model and goal:
                    await self._set_stream(text='正在思考与执行任务…', speaker_id=bot_id, speaker_name=bot['name'])
                    progress = self._make_harness_event_bridge(bot_id, bot['name'], my_exec, publish_exec=True)
                    result = await self.harness.run(
                        goal,
                        lambda messages, tools=None: self.complete(messages, tools, big=True),
                        self.context() + '\n助手的角色设定：' + self.persona(),
                        allowed_tools=bot['tools'],
                        on_event=progress
                    )
                else:
                    reason = 'no_goal' if not goal else 'no_api_key_configured'
                    self.store.state['last_research'] = time.time()
                    self.store.save()
                    if record_user:
                        msg_txt = '还没有明确的研究目标，请在消息里说明你想让我帮忙查的内容。' if not goal else '尚未配置可用的大模型 API Key，请先在设置中配置后再试。'
                        return await self.emit(msg_txt, 'chat', trace=[], goal=goal or '', from_bot_id=bot_id, from_bot_name=bot['name'])
                    self.vault.append_work_log(f'[静默研究跳过] {reason}')
                    return {
                        'status': 'silent',
                        'ok': True,
                        'silent': True,
                        'bot_id': bot_id,
                        'reason': reason,
                        'trace': [],
                    }
                if ready:
                    ready['done'] = True
                self.store.state['last_research'] = time.time()
                self.store.save()
                res_txt = str(result.get('text') or '').strip()
                if result.get('silent') or is_silent_proactive_output(res_txt):
                    self.vault.append_work_log(f'[静默任务] {(goal or "任务池")[:80]} · {res_txt[:160]}')
                    return {
                        'status': 'silent',
                        'ok': True,
                        'silent': True,
                        'bot_id': bot_id,
                        'goal': goal or '整理任务池快照',
                        'summary': res_txt or 'SILENT_OK',
                        'trace': result.get('trace', []),
                    }
                self.vault.log('完成一次任务执行：' + (goal or '本地任务池快照'))
                return await self.emit(result['text'], 'chat', trace=result['trace'], goal=goal or '整理任务池快照', from_bot_id=bot_id, from_bot_name=bot['name'])
            except asyncio.CancelledError:
                partial = (self.stream_prefix_var.get() or '').strip()
                if partial:
                    return await self.emit(partial, 'chat', track=False, trace=list(my_exec['trace']), from_bot_id=bot_id, from_bot_name=bot['name'])
                return {'stopped': True}
            finally:
                if cur_task:
                    self.queued_tasks_by_bot.get(bot_id, set()).discard(cur_task)
                fin_res_turn = self.turn_id_var.get() or ''
                cur_res_src = normalize_node_source(self.source_context.get())
                self._clear_bot_stream(bot_id, source=cur_res_src)
                with contextlib.suppress(Exception):
                    await self.publish({'type': 'stream', 'source': cur_res_src, 'bot_id': bot_id, 'turn_id': fin_res_turn, 'stream': None, 'seq': self.stream_seq})
                    await self.publish({'type': 'execution', 'source': cur_res_src, 'bot_id': bot_id, 'execution': None, 'seq': self.stream_seq})
                self.stream_prefix_var.reset(prefix_token)
                self.turn_id_var.reset(turn_token)

    async def confirm(self, mid, accepted):
        m = next((m for m in self.store.state['messages'] if m['id'] == mid and m['kind'] == 'result'), None)
        if not m: raise ValueError('成果不存在')
        if m.get('decision'):
            return {'already_decided':True, 'decision':m['decision']}
        m['decision'] = 'accepted' if accepted else 'rejected'
        if accepted:
            routine = {'id':mid, 'goal':m.get('goal', m['text'][:1000]), 'bot_id':m.get('bot_id','companion'), 'interval_hours':24, 'last_run':time.time(), 'enabled':True}
            self.store.state['routines'].append(routine)
            self.vault.append('playbooks/routines.md', f'## 用户确认 · {now_iso()}\n\n- ID: {mid}\n- 职责: {routine["goal"]}\n- 触发: 每 24 小时（免打扰/夜间顺延）\n- 每次完成后仍需用户验收\n')
        else:
            self.vault.log('用户否决成果 ' + mid + '，不固化职责。')
        self.store.save()
        await self.escalation.ack([mid])
        await self.publish({'type':'decision', 'id':mid, 'accepted':accepted})
        return {'saved':True, 'routine_created':accepted}

    def add_deadline(self, title, due_at):
        dt = datetime.fromisoformat(due_at)
        if dt.tzinfo is None: raise ValueError('截止时间必须含时区')
        item = {'id':str(time.time_ns()), 'title':title[:300], 'due_at':dt.timestamp(), 'notified':False}
        self.store.state['deadlines'].append(item)
        self.store.save()
        self.vault.append('tasks/active_deadlines.md', f'- [ ] {title} · {dt.astimezone(TZ).isoformat()} · ID {item["id"]}')
        return item

    async def run_scheduled(self, goal: str, bot_id: str | None = None, label: str = '定时任务'):
        t = asyncio.create_task(self._run_scheduled_impl(goal=goal, bot_id=bot_id, label=label))
        try:
            return await t
        except asyncio.CancelledError:
            cur = asyncio.current_task()
            outer_cancelling = bool(cur and hasattr(cur, 'cancelling') and cur.cancelling())
            if getattr(t, '_dsb_preempted', False) and not outer_cancelling:
                return {'silent': True, 'stopped': True, 'reason': 'preempted'}
            raise

    async def _run_scheduled_impl(self, goal: str, bot_id: str | None = None, label: str = '定时任务'):
        eng = self._get_curiosity_engine()
        target_id = bot_id or self.store.state.get('primary_bot_id', 'companion')
        marker, room_marker = self.bot_context.set(target_id), self.room_context.set(target_id)
        origin_marker = self.turn_origin_context.set('autonomous') if hasattr(self, 'turn_origin_context') else None
        try:
            async with self._get_bot_lock(target_id, 'phone'):
                bot = get_assistant(self.store, target_id)
                now_ts = time.time()
                self._last_scheduled_fire_by_bot[target_id] = now_ts
                self.activity = 'deep_research'
                cur_task = asyncio.current_task()
                nb_key = f'phone:{target_id}'
                self.active_task = cur_task
                if cur_task:
                    self.active_tasks_by_bot[target_id] = cur_task
                    self.active_tasks_by_node_bot[nb_key] = cur_task
                my_exec = {'bot_id': target_id, 'source': 'phone', 'goal': goal[:200], 'trace': [], 'tool': None, 'started_at': now_ts}
                self.executions_by_bot[target_id] = my_exec
                self.executions_by_node_bot[nb_key] = my_exec
                self.execution = my_exec
                turn_tok = self.turn_id_var.set('turn-' + secrets.token_hex(6))
                pref_tok = self.stream_prefix_var.set('')
                pre_pt = int(self.last_prompt_tokens.get(nb_key, self.last_prompt_tokens.get(target_id, 0)) or 0)
                pre_ct = int(self.last_cached_tokens.get(nb_key, self.last_cached_tokens.get(target_id, 0)) or 0)
                emitted_text = ''
                try:
                    recent = [
                        {'role': m['role'], 'content': (self._get_llm_history_text(m) if m['role'] == 'assistant' else (m.get('text') or '').strip())[:1500]}
                        for m in self.store.state['messages']
                        if m.get('bot_id', 'companion') == target_id and m['role'] in ('user', 'assistant')
                        and not (m['role'] == 'assistant' and self._get_llm_history_text(m) in ('', '已完成操作。', '收到，已处理完毕。'))
                    ][-24:]
                    today_s = datetime.now(TZ).strftime('%Y-%m-%d')
                    yest_s = datetime.fromtimestamp(time.time() - 86400, TZ).strftime('%Y-%m-%d')
                    pre_blocks = []
                    for rp, lp in (('tasks/active_deadlines.md', '待办与截止'), ('profile/course_schedule.md', '课表与作息'), (f'timeline/{today_s}.md', '今日时间线'), (f'timeline/{yest_s}.md', '昨日时间线')):
                        with contextlib.suppress(Exception):
                            rv = (self.vault.read(rp) or '').strip()
                            if rv:
                                pre_blocks.append(f'### {lp}\n{rv[:2500]}')
                    preloaded_md = '\n\n'.join(pre_blocks) if pre_blocks else '（暂无预载档案）'
                    is_silent_rt = any(kw in goal for kw in ('全程静默', '静默巡检', '静默整理', '不打扰用户', '无新消费或已记过则静默', '不要主动发消息打扰', '不推提醒'))
                    rule_txt = f'[{label}触发（后台静默任务）]：按目标静默执行工具。若无重大异常，请调 `reply(op="silent", reason="...")` 或输出 `SILENT_OK`。' if is_silent_rt else f'[{label}触发]：无需提醒请立即调 `reply(op="silent", reason="...")` 或输出 `SILENT_OK`；多步任务请闭环办完；确需提醒用户直接输出正文。'
                    system = self.harness.static_system_instructions(self.memory_engine.build_static_pinned_block()) + '\n\n你的设定：' + self.persona(target_id) + '\n\n' + rule_txt
                    user_p = '当前时间：' + self._fmt_bj(time.time()) + ' (' + now_iso() + ')\n【预载档案】：\n' + preloaded_md + f'\n\n⏰ [{label}到点触发]：{goal}'
                    allowed = set(normalize_bot_tools(bot.get('tools'), default_all=[t['function']['name'] for t in TOOLS])) | {'reply', 'stay_silent'}
                    avail_tools = [t for t in TOOLS if is_tool_allowed(t['function']['name'], allowed)]
                    messages = [{'role': 'system', 'content': system}, *recent, {'role': 'user', 'content': user_p}]
                    trace, acc_texts, final_txt, mm_frame, retries, api_calls = [], [], '', None, 0, 0
                    await self._set_stream(text=f'⏰ 正在执行{label}…', speaker_id=target_id, speaker_name=bot['name'])
                    use_big = (bot.get('engine') == 'big')
                    msg = {'content': ''}
                    for _ in range(60):
                        api_calls += 1
                        call_msgs, mm_frame = eng._build_vision_msgs(messages, mm_frame, self._is_current_bot_vision_capable())
                        msg = await self.complete(call_msgs, tools=avail_tools or None, big=use_big)
                        turn_txt = self._clean_and_filter_dot_text(msg.get('content') or '')
                        calls = msg.get('tool_calls') or []
                        if not calls:
                            thought_s = str(msg.get('thought') or msg.get('reasoning_content') or '').strip()
                            if avail_tools and retries < 3 and ((not turn_txt and thought_s and self._is_unfulfilled_action_promise('', user_text=goal, trace=trace, thought=thought_s)) or (turn_txt and self._is_unfulfilled_action_promise(turn_txt, user_text=goal, trace=trace, thought=thought_s))):
                                retries += 1
                                messages.extend([{'role': 'assistant', 'content': turn_txt or '（准备继续执行）'}, {'role': 'user', 'content': '[系统纠偏]：请继续发起 tool_calls 完成任务；若无需提醒请调 `reply(op="silent", reason="...")`。'}])
                                continue
                            if turn_txt:
                                final_txt = turn_txt
                                if not acc_texts or acc_texts[-1] != turn_txt:
                                    acc_texts.append(turn_txt)
                                self.stream_prefix_var.set(final_txt)
                            break
                        final_txt = ''
                        if turn_txt:
                            self.stream_prefix_var.set(turn_txt)
                        messages.append(msg)
                        def _parse_call_args(raw_a: Any) -> dict:
                            if isinstance(raw_a, dict):
                                return raw_a
                            if not raw_a:
                                return {}
                            try:
                                parsed = json.loads(raw_a)
                                return parsed if isinstance(parsed, dict) else {}
                            except (ValueError, TypeError, KeyError) as exc:
                                logger.warning('proactive tool arguments json parse failed: %s', exc)
                                return {}

                        running = [(ci, ci['function']['name'], _parse_call_args(ci['function'].get('arguments')), {'tool': ci['function']['name'], 'arguments': {}, 'duration_ms': 0, 'status': 'running'}) for ci in calls[:16]]
                        for _, _, ca, ent in running:
                            ent['arguments'] = ca
                            my_exec['trace'].append(ent)
                        my_exec['tool'] = f"并行执行 ({', '.join(x[1] for x in running[:3])})" if len(running) > 1 else running[0][1]
                        self.execution = my_exec
                        await self._set_stream(text=turn_txt or f"正在调用 {my_exec['tool']}…", tool=my_exec['tool'], speaker_id=target_id, speaker_name=bot['name'])
                        await self.publish({'type': 'execution', 'execution': my_exec})
                        silent_res, new_mm = await eng._execute_tool_batch(batch=calls[:16], allowed=allowed, target_id=target_id, trace=trace, messages=messages, offload_output=True, running_entries=running)
                        if new_mm is not None:
                            mm_frame = new_mm
                        if silent_res is not None:
                            return {'status': 'silent', 'ok': True, 'silent': True, 'bot_id': target_id, 'goal': goal, 'summary': str(silent_res.get('reason') or 'SILENT_OK'), 'trace': trace, 'api_calls': api_calls}
                        my_exec['tool'] = f'已执行 {len(trace)} 步'
                        self.execution = my_exec
                        await self._set_stream(text=turn_txt or '正在思考…', tool=my_exec['tool'], speaker_id=target_id, speaker_name=bot['name'])
                        await self.publish({'type': 'execution', 'execution': my_exec})
                    raw_txt = self._clean_and_filter_dot_text(final_txt) or self._clean_and_filter_dot_text('\n\n'.join(acc_texts)) or self._clean_and_filter_dot_text(msg.get('content') or '')
                    if not raw_txt and trace:
                        api_calls += 1
                        self._next_tool_choice = 'none'
                        sm = await self.complete(messages + [{'role': 'user', 'content': '请基于以上结果输出给用户的简练汇报（若无需打扰直接输出 SILENT_OK），不要再调用工具。'}], tools=avail_tools or None, big=use_big)
                        raw_txt = self._clean_and_filter_dot_text(sm.get('content') or '') or raw_txt
                    short_goal = goal.split('：')[0].split(':')[0][:60].strip()
                    if is_silent_rt or not raw_txt or is_silent_proactive_output(raw_txt):
                        clean_s = (raw_txt or '').replace('SILENT_OK', '').strip() or 'empty_output_silent'
                        self.vault.append_work_log(f'[静默巡检] {short_goal} · {clean_s[:180]}')
                        return {'status': 'silent', 'ok': True, 'silent': True, 'bot_id': target_id, 'goal': goal, 'summary': clean_s, 'trace': trace, 'api_calls': api_calls}
                    self.store.save()
                    self.vault.log(f'完成{label}：{short_goal}', bot_id=target_id)
                    already_notif = any(isinstance(tr, dict) and tr.get('tool') in ('notify_user', 'emergency_alarm', 'reply') for tr in trace)
                    emitted = await self.emit(raw_txt, 'routine', track=False, notify=False, trace=trace, api_calls=api_calls, goal=goal, from_bot_id=target_id, from_bot_name=bot['name'])
                    emitted_text = raw_txt
                    if bot.get('notify', True) and not already_notif:
                        await self.publish({'type': 'user_notification', 'id': emitted['id'], 'collapse_key': f'bot:{target_id}:scheduled', 'bot_id': target_id, 'bot_name': bot['name'], 'title': f"{bot['name']} · {label}", 'body': raw_txt[:300], 'urgent': False})
                    return emitted
                finally:
                    eng._restore_sched_tokens(target_id, nb_key, pre_pt, pre_ct, emitted_text)
                    await eng._cleanup_turn(target_id=target_id, src=normalize_node_source(self.source_context.get()), cur_task=cur_task, my_exec=my_exec)
                    with contextlib.suppress(Exception):
                        self.stream_prefix_var.reset(pref_tok)
                        self.turn_id_var.reset(turn_tok)
        finally:
            if origin_marker is not None:
                self.turn_origin_context.reset(origin_marker)
            self.room_context.reset(room_marker)
            self.bot_context.reset(marker)
            TaskContinuityManager.schedule_auto_resume_if_needed(self, target_id, 'phone')

    def _make_harness_event_bridge(self, target_id: str, speaker_name: str, exec_dict: dict | None, sub_goal: str | None = None, publish_exec: bool = False):
        async def _on_event(event):
            ev_type = event.get('type')
            if isinstance(exec_dict, dict):
                subs = exec_dict.setdefault('sub_agents', {}) if sub_goal is not None else None
                if ev_type == 'tool_start':
                    if subs is not None:
                        subs[target_id] = {'bot_id': target_id, 'name': speaker_name, 'status': f"调用 {event['tool']}", 'goal': sub_goal, 'updated_at': time.time()}
                    if isinstance(exec_dict.get('trace'), list):
                        exec_dict['trace'].append({'tool': event['tool'], 'arguments': event.get('arguments') or {}, 'duration_ms': 0, 'status': 'running'})
                    exec_dict['tool'] = event['tool']
                elif ev_type == 'tool_end':
                    entry_done = {**dict(event['entry']), 'status': 'done'}
                    if isinstance(exec_dict.get('trace'), list):
                        if exec_dict['trace'] and exec_dict['trace'][-1].get('status') == 'running' and exec_dict['trace'][-1].get('tool') == entry_done.get('tool'):
                            exec_dict['trace'][-1] = entry_done
                        else:
                            exec_dict['trace'].append(entry_done)
                    exec_dict['tool'] = f"正在分析 {entry_done.get('tool', '工具')} 结果…"
                elif ev_type == 'turn_text' and subs is not None:
                    subs[target_id] = {'bot_id': target_id, 'name': speaker_name, 'status': '生成回复中', 'goal': sub_goal, 'updated_at': time.time()}
                if publish_exec:
                    self.execution = exec_dict
            if ev_type == 'turn_text':
                self.stream_prefix_var.set(event['text'])
                await self._set_stream(text=event['text'], speaker_id=target_id, speaker_name=speaker_name)
            elif ev_type == 'tool_start':
                await self._set_stream(text=self.stream_prefix_var.get() or f"正在调用 {event['tool']}…", tool=event['tool'], speaker_id=target_id, speaker_name=speaker_name)
                if publish_exec and isinstance(exec_dict, dict):
                    await self.publish({'type': 'execution', 'source': normalize_node_source(exec_dict.get('source')), 'execution': exec_dict})
            elif ev_type == 'tool_end':
                await self._set_stream(text=self.stream_prefix_var.get() or '正在思考…', tool=f"正在分析 {event['entry'].get('tool', '工具')} 结果…", speaker_id=target_id, speaker_name=speaker_name)
                if publish_exec and isinstance(exec_dict, dict):
                    await self.publish({'type': 'execution', 'source': normalize_node_source(exec_dict.get('source')), 'execution': exec_dict})
        return _on_event

    def quiet(self):
        return self.scheduler_engine.is_quiet_hour()

    async def _sleep_time_consolidate(self):
        """Silent background Sleep-Time Compute via MemoryEngine (never touches live_stream)."""
        async with self.lock:
            await self.memory_engine.sleep_time_consolidate(
                silent_complete_fn=lambda msgs: self.llm.complete_silent(msgs, tools=None, big=False)
            )
            saved_vc = str(self.store.state.get('cached_vault_context') or '').strip()
            if saved_vc:
                self._cached_vault_context = saved_vc

    async def keepalive_loop(self):
        """Dedicated lightweight daemon for KV cache keepalive, 9m idle hot-compaction & 8m sleep memory consolidation.

        Strictly decoupled from `scheduler()` so slow routines / curiosity / research can NEVER starve idle compaction!
        """
        self._keepalive_loop_active = True
        try:
            while True:
                try:
                    await asyncio.sleep(5)
                    s, c = self.store.state, self.store.config
                    if not c.enabled:
                        continue
                    now = time.time()
                    has_api = bool(
                        (c.effective_api_key(big=True) or c.effective_api_key(big=False))
                        and (c.effective_model(big=True) or c.effective_model(big=False))
                    )
                    if has_api:
                        try:
                            await asyncio.wait_for(self._get_cache_keepalive().tick(self, now_ts=now), timeout=240.0)
                        except Exception as ke:
                            logger.warning('keepalive_loop tick error: %s', ke)
                    with contextlib.suppress(Exception):
                        if hasattr(self.memory_engine, 'get_embedding_engine'):
                            self.memory_engine.get_embedding_engine().maybe_unload_idle(now)
                    if (
                        has_api
                        and not TaskContinuityManager.is_any_foreground_busy(self)
                        and (now - float(s.get('last_user') or 0)) >= self.memory_engine.get_sleep_consolidate_idle_seconds()
                        and float(s.get('last_user') or 0) > float(s.get('last_sleep_consolidate') or 0)
                    ):
                        try:
                            await asyncio.wait_for(self._sleep_time_consolidate(), timeout=180.0)
                        except Exception as se:
                            logger.warning('keepalive_loop sleep_consolidate error: %s', se)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning('keepalive_loop unexpected error: %s', exc)
        finally:
            self._keepalive_loop_active = False

    async def scheduler(self):
        while True:
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            try:
                s, c = self.store.state, self.store.config
                if not c.enabled: continue
                now = time.time()
                if not getattr(self.clock_wheel, '_running', False):
                    await self.clock_wheel.tick_once()
                if s['snooze_until'] > now: continue
                has_api = bool((c.effective_api_key(big=True) or c.effective_api_key(big=False)) and (c.effective_model(big=True) or c.effective_model(big=False)))
                from core.turn_ledger import TurnLedgerManager
                # 2. Fire due routines per-bot with 4h missed-run catch-up, quiet-hour deferral, exponential skip backoff & circuit breaker!
                if not self.lock.locked():
                    from core.curiosity_engine import (
                        curiosity_will_coalesce_soon,
                        is_coalesceable_routine_for_curiosity,
                    )
                    now = time.time()
                    for r in s['routines']:
                        if not r.get('enabled', True):
                            continue
                        if not r.get('next_run'):
                            r['next_run'] = self._compute_next_routine_run(r, now)
                            r['next_run_bj'] = self._fmt_bj(r['next_run'])
                            self.store.save()
                        due_ts = float(r['next_run'])
                        if now < due_ts:
                            continue
                        is_clock_routine = bool(r.get('daily_time'))
                        r_bot = r.get('bot_id', 'companion')
                        if (
                            is_coalesceable_routine_for_curiosity(r)
                            and (now - due_ts) <= 180.0
                            and curiosity_will_coalesce_soon(self.store, getattr(self, 'proactive_gate', None), self.scheduler_engine, r_bot, now)
                        ):
                            continue
                        if not is_clock_routine and not r.get('allow_quiet') and not r.get('quiet_ok') and self.quiet():
                            deferred_ts, _ = self.scheduler_engine.defer_out_of_quiet_hours(now)
                            prev_next = float(r.get('next_run') or 0)
                            if abs(prev_next - deferred_ts) > 1.0:
                                r['next_run'] = deferred_ts
                                r['next_run_bj'] = self._fmt_bj(deferred_ts)
                                r['deferred_reason'] = 'quiet_hours'
                                r['last_status'] = 'quiet_deferred'
                                TurnLedgerManager.append_run_log(self.store, {'kind': 'routine', 'task_id': r.get('id', ''), 'bot_id': r_bot, 'goal': str(r.get('goal') or '')[:120], 'status': 'quiet_deferred', 'next_run_bj': r['next_run_bj'], 'ts': now_iso()})
                                self.store.save()
                            continue
                        overdue_sec = max(0.0, now - due_ts)
                        is_wakeup_goal = any(k in str(r.get('goal') or '') for k in ('叫醒', '起床'))
                        max_catchup_sec = 1500.0 if is_wakeup_goal else 14400.0
                        if overdue_sec <= max_catchup_sec and TaskContinuityManager.is_foreground_busy(self, r_bot, 'phone'):
                            continue
                        r.pop('deferred_reason', None)
                        orig_due_bj = self._fmt_bj(due_ts)
                        if overdue_sec > max_catchup_sec:
                            skips = int(r.get('consecutive_skips') or 0) + 1
                            r['consecutive_skips'] = skips
                            backoff_s = min(3600.0, 30.0 * (2 ** min(6, skips - 1)))
                            cand_next = max(self._compute_next_routine_run(r, now + 2), now + backoff_s)
                            r['next_run'] = cand_next
                            r['next_run_bj'] = self._fmt_bj(cand_next)
                            r['last_status'] = 'expired_skip'
                            r['last_late_seconds'] = int(overdue_sec)
                            TurnLedgerManager.append_run_log(self.store, {'kind': 'routine', 'task_id': r.get('id', ''), 'bot_id': r_bot, 'goal': str(r.get('goal') or '')[:120], 'status': 'expired_skip', 'late_seconds': int(overdue_sec), 'next_run_bj': r['next_run_bj'], 'ts': now_iso()})
                            if skips <= 2 or skips % 5 == 0:
                                self.vault.append_work_log(f"[定时例行过期顺延 x{skips}] {r.get('id')} 原定 {orig_due_bj}（已超窗 {int(overdue_sec // 60)} 分钟），已推进 next_run -> {r['next_run_bj']}")
                            self.store.save()
                            continue
                        is_late = overdue_sec >= 120.0
                        r['last_run'] = now
                        r['consecutive_skips'] = 0
                        r['next_run'] = self._compute_next_routine_run(r, now + 2)
                        r['next_run_bj'] = self._fmt_bj(r['next_run'])
                        r['last_late_seconds'] = int(overdue_sec)
                        self.store.save()
                        if has_api:
                            r_label = f"定点例行 ({r['daily_time']})" if is_clock_routine else f"例行巡检 ({r.get('interval_hours', 24)}h)"
                            eff_goal = (
                                f"[⏰ 延迟补跑说明：本任务原定 {orig_due_bj} 触发，因前序任务占用延迟 {int(overdue_sec // 60)} 分钟补跑]\n{r['goal']}"
                                if is_late
                                else r['goal']
                            )
                            t0_rt = time.perf_counter()
                            try:
                                rt_res = await asyncio.wait_for(
                                    self.run_scheduled(eff_goal, bot_id=r_bot, label=r_label + ('·补跑' if is_late else '')),
                                    timeout=600.0,
                                )
                                rt_silent = bool(isinstance(rt_res, dict) and rt_res.get('silent'))
                                r['last_status'] = 'silent' if rt_silent else ('late_ok' if is_late else 'ok')
                                r['last_error'] = ''
                                TurnLedgerManager.append_run_log(self.store, {
                                    'kind': 'routine', 'task_id': r.get('id', ''), 'bot_id': r_bot, 'label': r_label,
                                    'goal': str(r.get('goal') or '')[:120], 'status': r['last_status'], 'late_seconds': int(overdue_sec),
                                    'steps': len((rt_res or {}).get('trace') or []) if isinstance(rt_res, dict) else 0,
                                    'duration_ms': int(round((time.perf_counter() - t0_rt) * 1000)), 'ts': now_iso(),
                                })
                            except Exception as rt_err:
                                r['last_status'] = 'error'
                                r['last_error'] = str(rt_err)[:240]
                                self.vault.append_work_log(f"[定时例行异常] {r.get('id')} ({r_label}): {str(rt_err)[:200]}")
                                TurnLedgerManager.append_run_log(self.store, {
                                    'kind': 'routine', 'task_id': r.get('id', ''), 'bot_id': r_bot, 'label': r_label,
                                    'goal': str(r.get('goal') or '')[:120], 'status': 'error', 'error': str(rt_err)[:200], 'ts': now_iso(),
                                })
                                await self.emit(
                                    f'⏰ [定时例行执行异常告警] 「{r_label}」（{str(r.get("goal") or "")[:50]}…）执行报错：{str(rt_err)[:180]}（已自动顺延下次触发至 {r["next_run_bj"]}）',
                                    'error', track=False, bot_id=r_bot, source='phone',
                                )
                            self.store.save()
                        self.wake.set()
                        break
                # 2.5 Passive background notification convergence every 75s when phone is online and idle
                if not TaskContinuityManager.is_any_foreground_busy(self) and self.is_phone_online() and now - self._last_notif_bg_poll >= 75.0 and not self.executions_by_bot:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(self._poll_and_converge_notifications(timeout=4.5), timeout=8.0)
                # 2.6 Fallback when keepalive_loop is not running (e.g. unit tests calling scheduler directly)
                if not getattr(self, '_keepalive_loop_active', False):
                    if has_api:
                        try:
                            await asyncio.wait_for(self._get_cache_keepalive().tick(self, now_ts=now), timeout=240.0)
                        except Exception as ke:
                            logger.warning('scheduler cache_keepalive error: %s', ke)
                    with contextlib.suppress(Exception):
                        if hasattr(self.memory_engine, 'get_embedding_engine'):
                            self.memory_engine.get_embedding_engine().maybe_unload_idle(now)
                    if (
                        has_api
                        and not TaskContinuityManager.is_any_foreground_busy(self)
                        and (now - s.get('last_user', 0)) >= self.memory_engine.get_sleep_consolidate_idle_seconds()
                        and s.get('last_user', 0) > s.get('last_sleep_consolidate', 0)
                    ):
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(self._sleep_time_consolidate(), timeout=180.0)
                # 2.7 Weekly Memory Gardener
                if not TaskContinuityManager.is_any_foreground_busy(self) and (now - float(s.get('last_memory_gardener') or 0)) >= 7 * 86400:
                    with contextlib.suppress(Exception):
                        from core.memory_migrator import run_memory_gardener
                        run_memory_gardener(self, now_ts=now)
                        s['last_memory_gardener'] = now
                        self.store.save()
                # 4. Background curiosity & idle research respect quiet hours (00:00 - 08:00) and active chat sessions
                if self.quiet():
                    continue
                cur_interval_s = max(1, int(getattr(c, 'curiosity_interval_minutes', 30) or 30)) * 60
                cur_due = float(s.get('curiosity_next_run') or 0) or (float(s.get('last_curiosity') or 0) + cur_interval_s)
                if len(s['pending']) < 5 and now >= cur_due:
                    curiosity_targets = []
                    seen_pairs = set()
                    for node_k in ('phone', 'phone2', 'pc', 'pc2'):
                        for b in s.get('assistants', []):
                            if not isinstance(b, dict) or not b.get('enabled', True):
                                continue
                            if resolve_bot_for_source(b, node_k).get('curiosity_enabled'):
                                pair = (b['id'], node_k)
                                if pair not in seen_pairs:
                                    seen_pairs.add(pair)
                                    curiosity_targets.append(pair)
                    if curiosity_targets:
                        last_active_src = normalize_node_source(s.get('last_active_source') or '')
                        if last_active_src:
                            curiosity_targets.sort(key=lambda p: 0 if p[1] == last_active_src else 1)
                        rr_idx = int(getattr(self, '_curiosity_rr_idx', 0) or 0) % len(curiosity_targets)
                        target_bot_id, target_src = curiosity_targets[rr_idx]
                        self._curiosity_rr_idx = (rr_idx + 1) % len(curiosity_targets)
                        cur_res = await asyncio.wait_for(self.curiosity(bot_id=target_bot_id, source=target_src), timeout=600.0)
                        if isinstance(cur_res, dict) and cur_res.get('suppressed'):
                            delay_s = float(cur_res.get('next_delay_s') or cur_interval_s)
                            s['curiosity_next_run'] = max(float(s.get('curiosity_next_run') or 0), now + delay_s)
                            self.store.save()
                    else:
                        s['curiosity_next_run'] = now + cur_interval_s
                        self.store.save()
                ready = next((x for x in s['opportunities'] if x['ready'] and not x['done']), None)
                if ready and has_api and now - s['last_user'] >= c.idle_research_minutes * 60:
                    await self.research()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.store.state['last_error'] = str(e)[:250]
                self.store.save()
                await self.publish({'type':'engine_error', 'message':str(e)[:250]})

    async def _tool_system_status(self, args: dict) -> dict:
        import shutil
        req_op = str(args.get('op') or args.get('action') or 'status').strip().lower()
        if req_op not in ('status', 'doctor', 'context', 'all', 'check', ''):
            return {
                'ok': False,
                'error': f'未知的 system_status 操作: {req_op}（支持 status / doctor / context / all）',
            }
        bot_id = self.bot_context.get() or 'companion'
        cur_src = normalize_node_source(self.source_context.get())
        last_pt = int(self.last_prompt_tokens.get(bot_id, 0) or 0)
        last_ct = int(self.last_cached_tokens.get(bot_id, 0) or 0)
        hit_pct = int(round((last_ct * 100.0) / max(1, last_pt))) if last_pt > 0 else 0
        limit_tok = self.memory_engine.get_threshold_tokens()
        rem_tok = max(0, limit_tok - last_pt)
        cur_trace_steps = len((self.execution or {}).get('trace') or [])
        vault_files = self.vault.files() if self.vault is not None else []
        idx_lines = 0
        if self.vault is not None and (self.vault.root / 'INDEX.md').exists():
            idx_lines = sum(1 for ln in (self.vault.root / 'INDEX.md').read_text('utf-8', errors='ignore').splitlines() if ln.strip().startswith('- [['))
        tess_ok = bool(shutil.which('tesseract'))
        return {
            'ok': True, 'op': req_op or 'status', 'node': cur_src, 'bot_id': bot_id,
            'context_budget': {
                'last_prompt_tokens': last_pt, 'last_cached_tokens': last_ct, 'cache_hit_pct': hit_pct,
                'auto_compact_limit_tokens': limit_tok, 'remaining_tokens_before_compact': rem_tok,
                'usage_pct': round((last_pt * 100.0) / max(1, limit_tok), 1),
                'current_turn_tool_steps': cur_trace_steps, 'soft_step_budget_per_turn': 15,
                'vault_markdown_files_count': len(vault_files), 'vault_index_entries_count': idx_lines,
            },
            'channels_doctor': {
                'vision_read_file': {'ok': True, 'active_mode': 'multimodal_vision_direct', 'multimodal_passthrough': True, 'pil_metadata': True, 'ocr_fallback_used': False, 'local_tesseract_binary_installed': tess_ok},
                'send_image': {'ok': True, 'route': '/workspace-media/uploads/'},
                'memory_vault': {'ok': self.vault is not None, 'allowed_tools': ['read_file', 'write_file', 'edit_file', 'delete_file', 'move_file', 'list_dir', 'grep_search', 'glob_files', 'memory_search'], 'bash_access': 'isolated_by_landlock_by_design (use file tools instead of run_bash for memory_vault)', 'allowed_extensions': ['.md']},
                'phone_online': bool(self.is_phone_online()),
                'pc_online': bool(self.is_dsh_online()) if hasattr(self, 'is_dsh_online') else False,
            },
            **self.scheduler_engine.build_system_status_extras(cur_src, bot_id, last_pt, last_ct, hit_pct),
        }
