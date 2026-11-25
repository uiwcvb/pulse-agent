from __future__ import annotations
import asyncio, contextlib, json, re, secrets, time
from typing import Any
from assistants import get_assistant
from core.canonical_tool_handlers import build_pc_fs_rpc_payload, stage_and_execute_send_file, stage_and_execute_send_image
from core.prompt_guards import (
    BAIKE_CALIBRATION_TAIL,
    build_preachy_refusal_retry_prompt,
    is_preachy_refusal as guard_is_preachy_refusal,
)
from core.stream_publisher_mixin import StreamPublisherMixin
from core.task_continuity import TaskContinuityManager
from core.tool_schemas import is_tool_allowed, normalize_bot_tools, normalize_tool_name
from core.turn_history import append_turn_step, build_compact_turn_step, expand_concatenated_tool_calls
from core.turn_ledger import TurnLedgerManager
from storage import compact_trace_list, normalize_node_source
from vault_manager import now_iso


class BrainToolsMixin(StreamPublisherMixin):
    async def _chat(self, text, pre_recorded_user_msg=None, turn_id=None, source: str = 'phone', client_msg_id: str | None = None, deferred_incoming_spec: dict | None = None, _lock_already_held: bool = False, _auto_resume_spec: dict | None = None):
        bot_id = self.bot_context.get()
        src = normalize_node_source(source or self.source_context.get())
        nb_turn_key = f'{src}:{bot_id}'
        enqueued_epoch = self._abort_epoch_by_node_bot.get(nb_turn_key, 0)
        stop_ep_map = getattr(self, '_user_stop_epoch_by_node_bot', {})
        enqueued_stop_epoch = stop_ep_map.get(nb_turn_key, 0)
        cur_task_outer = asyncio.current_task()
        if cur_task_outer and self.hop_depth.get() == 0:
            self.queued_tasks_by_bot.setdefault(bot_id, set()).add(cur_task_outer)
            self.queued_tasks_by_node_bot.setdefault(nb_turn_key, set()).add(cur_task_outer)

        def _is_aborted() -> bool:
            cur_tid = self.turn_id_var.get() or turn_id
            if cur_tid and cur_tid in self._stopped_turn_ids:
                return True
            if enqueued_stop_epoch != getattr(self, '_user_stop_epoch_by_node_bot', {}).get(nb_turn_key, 0):
                return True
            if self.hop_depth.get() == 0 and enqueued_epoch != self._abort_epoch_by_node_bot.get(nb_turn_key, 0):
                return True
            return False

        lock_ctx = contextlib.nullcontext() if _lock_already_held else self._get_bot_lock(bot_id, src)
        async with lock_ctx:
            if _is_aborted():
                return {'stopped': True}
            bot = get_assistant(self.store, bot_id, source=src)
            eff_turn_id = turn_id or ('turn-' + secrets.token_hex(6))
            turn_token = self.turn_id_var.set(eff_turn_id)
            prefix_token = self.stream_prefix_var.set('')
            thought_prefix_token = self.thought_prefix_var.set('') if hasattr(self, 'thought_prefix_var') else None
            self._current_user_text_by_bot[bot_id] = text
            self._phone_blocked_by_user_by_bot[bot_id] = self._is_user_holding_or_refusing_phone(text, was_blocked=False)
            if isinstance(deferred_incoming_spec, dict) and pre_recorded_user_msg is None:
                spec_copy = dict(deferred_incoming_spec)
                out_box = spec_copy.pop('out_box', None)
                inc_role = spec_copy.pop('role', 'assistant')
                inc_text = spec_copy.pop('text', text)
                inc_kind = spec_copy.pop('kind', 'bot_message')
                inc_turn = spec_copy.pop('turn_id', '')
                inc_sub = spec_copy.pop('is_sub_turn', True)
                if inc_turn:
                    spec_copy['turn_id'] = inc_turn
                spec_copy['is_sub_turn'] = bool(inc_sub)
                m = self.store.message(inc_role, inc_text, kind=inc_kind, **spec_copy)
                self.stream_seq += 1
                if isinstance(out_box, dict):
                    out_box['msg'] = m
                await self.publish({'type': 'message', 'source': src, 'message': m, 'seq': self.stream_seq})
            elif pre_recorded_user_msg is None:
                self.store.state['last_user'] = time.time()
                extra_u = {'turn_id': eff_turn_id}
                if client_msg_id:
                    extra_u['client_msg_id'] = client_msg_id
                m = self.store.message('user', text, bot_id=bot_id, source=src, **extra_u)
                self.stream_seq += 1
                self.vault.log('用户：' + text, bot_id=bot_id)
                await self.publish({'type': 'message', 'source': src, 'message': m, 'seq': self.stream_seq})
                visible = {m['id'] for m in self.store.state['messages'] if m.get('bot_id', 'companion') == bot_id}
                await self.escalation.ack([mid for mid in self.store.state['pending'] if mid in visible])
            else:
                m = pre_recorded_user_msg
            self.activity = 'thinking'
            cur_task = asyncio.current_task()
            if self.hop_depth.get() == 0:
                self.active_task = cur_task
            if cur_task:
                self.active_tasks_by_bot[bot_id] = cur_task
                self.active_tasks_by_node_bot[nb_turn_key] = cur_task
            is_fg_user_turn = bool(
                self.hop_depth.get() == 0
                and deferred_incoming_spec is None
                and (not isinstance(pre_recorded_user_msg, dict) or pre_recorded_user_msg.get('kind') != 'bot_message')
            )
            if is_fg_user_turn:
                u_seq = int(m.get('order_seq') or m.get('seq') or 0) if isinstance(m, dict) else 0
                res_cnt = int((_auto_resume_spec or {}).get('resume_count') or 0)
                TaskContinuityManager.touch_foreground_activity(self.store)
                TaskContinuityManager.begin_foreground_chain(self, bot_id, src, eff_turn_id, text, user_msg_seq=u_seq, resume_count=res_cnt)
                if hasattr(self, '_touch_cache_keepalive'):
                    self._touch_cache_keepalive(src, bot_id)
            prev_interrupted_ledger = TurnLedgerManager.begin_turn(self.store, src, bot_id, eff_turn_id, text) if self.hop_depth.get() == 0 else None
            my_exec = {'bot_id': bot_id, 'source': src, 'turn_id': eff_turn_id, 'goal': text[:200], 'trace': [], 'turn_steps': [], 'tool': None, 'started_at': time.time()}
            self.executions_by_node_bot[f'{src}:{bot_id}'] = my_exec
            self.executions_by_bot[bot_id] = my_exec
            self.execution = my_exec
            final_msg_for_handoff: dict | None = None
            try:
                if self.hop_depth.get() == 0 and str(text or '').strip().startswith('/'):
                    from core.slash_commands import try_handle_slash_command
                    slash_md = await try_handle_slash_command(self, str(text or '').strip(), bot_id, src, bot, current_user_msg=m)
                    if slash_md is not None:
                        if not str(slash_md).strip():
                            return {'ok': True, 'silent_slash': True}
                        final_msg_for_handoff = await self.emit(
                            slash_md,
                            'chat',
                            bot_id=bot_id,
                            from_bot_id=bot_id,
                            from_bot_name=bot['name'],
                            track=False,
                            source=src,
                        )
                        return final_msg_for_handoff
                api_key = self.store.config.effective_api_key(big=False) or self.store.config.effective_api_key(big=True)
                model = self.store.config.effective_model(bot_model=bot.get('model', ''), big=False) or self.store.config.effective_model(bot_model=bot.get('model', ''), big=True)
                if not (api_key and model):
                    final_msg_for_handoff = await self.emit('记下了。具体最卡的是哪一步，理想结果又是什么？\n\n目前使用本地引导；在右上角「设置」中填入 API Key 并选择模型后，就能直接读写工作区、调工具和安排其它 Bot 干活啦。', 'curiosity', engine='local_prompt', source=src)
                    return final_msg_for_handoff

                from core.observation_masker import GUI_UI_TOOLS, extract_ephemeral_ui_from_result
                comp_res = await self._auto_compact_if_needed(bot_id, source=src)
                if isinstance(comp_res, dict) and comp_res.get('compacted') and hasattr(self, 'dsh_adapter'):
                    with contextlib.suppress(Exception):
                        await self.dsh_adapter.reset_session(bot_id, src)
                active_msg_id = str(m.get('id') or '') if isinstance(m, dict) else ''
                messages, available_tools, allowed, calibration_tail = await self._build_turn_messages_and_tools(bot_id, src, bot, active_msg_id=active_msg_id)
                raw_trim_u = str(text or '').strip()
                nb_goal_key = f'{src}:{bot_id}'
                active_goals_map = self.store.state.setdefault('active_goals', {})
                m_goal_cmd = re.match(r'^/(?:goal|goals|loop)\s+([\s\S]+)$', raw_trim_u, flags=re.IGNORECASE)
                if m_goal_cmd:
                    goal_spec = m_goal_cmd.group(1).strip()
                    active_goals_map[nb_goal_key] = {
                        'id': 'goal-' + secrets.token_hex(4), 'goal': goal_spec, 'bot_id': bot_id, 'source': src,
                        'status': 'active', 'wakeups': 0, 'total_steps': 0, 'started_at': time.time(), 'updated_at': now_iso(),
                    }
                    self.store.save()
                    await self.publish({'type': 'goal_status', 'source': src, 'bot_id': bot_id, 'active': True, 'status': 'active', 'goal': goal_spec[:300]})
                existing_goal = active_goals_map.get(nb_goal_key)
                goal_mode = bool(self.hop_depth.get() == 0 and isinstance(existing_goal, dict) and existing_goal.get('status') == 'active')
                goal_spec = str(existing_goal.get('goal') or raw_trim_u).strip() if goal_mode else raw_trim_u
                if goal_mode:
                    allowed.update({'reply', 'complete_goal', 'finish_goal'})
                    if not any(is_tool_allowed(t.get('function', {}).get('name'), {'reply'}) for t in available_tools):
                        for t_schema in self.harness.all_tools():
                            if t_schema.get('function', {}).get('name') in ('reply', 'complete_goal'):
                                available_tools.append(t_schema)
                                break
                is_pure_question_interrupt = bool(
                    self.hop_depth.get() == 0 and not m_goal_cmd and len(raw_trim_u) <= 56
                    and (raw_trim_u in ('?', '？', '??', '？？', '???') or re.search(r'^(?:为什么|为啥|怎么回事|怎么又|你干嘛|你在干嘛|干嘛|谁让你)', raw_trim_u))
                    and not re.search(r'(?:继续|帮我|打开|点击|去查|查一下|重试|再试)', raw_trim_u)
                )
                if messages and messages[-1].get('role') == 'user':
                    turn_Contract_Note = TaskContinuityManager.build_goal_mode_contract(goal_spec) if goal_mode else ''
                    if is_pure_question_interrupt:
                        turn_Contract_Note += '\n\n<turn_safety_hint>用户本条消息是在向你发问/质询原因：请先诚实、直接回答用户的问题；除非用户明确要求继续操作，否则本轮严禁盲目对旧画面坐标发起手机点击或修改设置。</turn_safety_hint>'
                    elif isinstance(_auto_resume_spec, dict):
                        turn_Contract_Note += '\n\n' + TaskContinuityManager.build_auto_resume_prompt(_auto_resume_spec)
                    elif prev_interrupted_ledger is not None:
                        turn_Contract_Note += TurnLedgerManager.format_interrupted_turn_hint(prev_interrupted_ledger)
                    if turn_Contract_Note:
                        last_c = messages[-1].get('content')
                        if isinstance(last_c, str):
                            messages[-1] = {**messages[-1], 'content': last_c + turn_Contract_Note}
                        elif isinstance(last_c, list):
                            messages[-1] = {**messages[-1], 'content': list(last_c) + [{'type': 'text', 'text': turn_Contract_Note}]}
                elif isinstance(_auto_resume_spec, dict):
                    messages.append({'role': 'user', 'content': TaskContinuityManager.build_auto_resume_prompt(_auto_resume_spec)})
                if hasattr(self, 'dsh_adapter') and await self.dsh_adapter.should_use_dsh():
                    res_dsh = await self.dsh_adapter.run_chat_turn(
                        bot_id=bot_id, src=src, bot=bot, eff_turn_id=eff_turn_id, raw_user_text=text,
                        messages=messages, available_tools=available_tools, allowed=allowed, my_exec=my_exec,
                        is_fg_user_turn=is_fg_user_turn, deferred_incoming_spec=deferred_incoming_spec,
                        goal_mode=goal_mode, enqueued_stop_epoch=enqueued_stop_epoch,
                    )
                    if isinstance(res_dsh, dict) and res_dsh.get('id'):
                        final_msg_for_handoff = res_dsh
                    return res_dsh
                trace = []
                last_substantive_step_txt: str = ''
                pre_cleanup_summary: str = ''
                spoken_segments_in_turn: list[str] = []
                process_log_steps: list[str] = []
                last_step_text: str = ''
                last_nonempty_narration: str = ''
                final_turn_text: str = ''
                silent_requested: bool = False
                silent_reason: str = ''
                has_user_visible_reply: bool = False
                self._explicit_human_gate_in_turn = False
                parsed_meta: dict = {}
                choices_reset_token = self.choices_var.set(None)
                choices_prompt_token = self.choices_prompt_var.set('')
                cloud_used_in_turn = False
                sent_auth_screenshot_in_turn = False
                await self._set_stream(text='🎯 正在持久目标模式下推进…' if goal_mode else '', tool='正在思考…', speaker_id=bot_id, speaker_name=bot['name'])
                msg = {'content': '收到。'}
                use_big = True if goal_mode else (bot.get('engine') == 'big')
                rolling_vision_frames: list[dict[str, Any]] = []
                persistent_turn_mm_frames: list[dict] = []
                ephemeral_tail_notes: list[str] = []
                self.pending_sticker_var.set('')
                self.pending_stickers_var.set(())
                api_calls = 0
                step_dropout_retries = 0
                total_dropout_retries = 0
                goal_wakeup_count = 0
                goal_completed_in_turn = False
                goal_completed_summary = ''
                max_loop_steps = TaskContinuityManager.get_max_loop_steps(self, goal_mode=goal_mode)
                max_segments = TaskContinuityManager.get_max_segments(self, is_foreground=is_fg_user_turn, goal_mode=goal_mode)
                segment_idx = 0
                seg_step = 0
                segment_cap_truncated = False
                turn_started_perf = time.perf_counter()
                step_metrics_list: list[dict] = []
                def _is_waiting_human_gate() -> bool:
                    return bool(
                        getattr(self, '_phone_to_foreground_in_turn', False)
                        or getattr(self, '_explicit_human_gate_in_turn', False)
                    )
                while True:
                    if _is_aborted():
                        raise asyncio.CancelledError()
                    if seg_step >= max_loop_steps:
                        if _is_waiting_human_gate() or not available_tools:
                            break
                        if segment_idx + 1 >= max_segments:
                            segment_cap_truncated = True
                            break
                        segment_idx += 1
                        seg_step = 0
                        if is_fg_user_turn:
                            TaskContinuityManager.touch_foreground_activity(self.store)
                        messages = TaskContinuityManager.compact_messages_for_next_segment(
                            messages, goal_spec, segment_idx + 1, len(trace), [str(t.get('tool') or '') for t in trace]
                        )
                        last_step_text = ''
                        await self._set_stream(
                            text=f'🔄 任务步骤较多，正在无缝推进第 {segment_idx + 1} 段（已完成 {len(trace)} 步）…',
                            tool=f'无缝续跑 #{segment_idx + 1}',
                            speaker_id=bot_id,
                            speaker_name=bot['name'],
                        )
                    seg_step += 1
                    # Step-boundary urgent scheduled interrupt injection (preserves 100% prefix KV cache via append-only!)
                    pending_ints = self.urgent_interrupts_by_node_bot.pop(nb_turn_key, []) if hasattr(self, 'urgent_interrupts_by_node_bot') else []
                    if pending_ints:
                        int_chunks = []
                        for p_int in pending_ints:
                            p_int['consumed'] = True
                            int_chunks.append(str(p_int.get('prompt') or '').strip())
                        combined_int = '\n---\n'.join(c for c in int_chunks if c)
                        if combined_int:
                            messages.append({
                                'role': 'user',
                                'content': TaskContinuityManager.wrap_urgent_interrupt_prompt(combined_int),
                            })
                    # Keep persistent image reads + a 3-frame rolling visual window (initial anchor + previous + latest)
                    self._prune_tool_loop_messages(messages)
                    if len(persistent_turn_mm_frames) > 8:
                        del persistent_turn_mm_frames[:4]
                    if len(rolling_vision_frames) <= 3:
                        window_frames = list(rolling_vision_frames)
                    else:
                        window_frames = [rolling_vision_frames[0], rolling_vision_frames[-2], rolling_vision_frames[-1]]
                    if persistent_turn_mm_frames or window_frames or ephemeral_tail_notes:
                        mm_parts: list[dict] = []
                        if ephemeral_tail_notes:
                            mm_parts.append({'type': 'text', 'text': '\n\n'.join(ephemeral_tail_notes[-2:])})
                            ephemeral_tail_notes = []
                        for mf in persistent_turn_mm_frames:
                            mm_parts.append({'type': 'text', 'text': mf['caption']})
                            mm_parts.append({'type': 'image_url', 'image_url': {'url': f"data:{mf['mime']};base64,{mf['b64']}"}})
                        n_win = len(window_frames)
                        for w_idx, mf in enumerate(window_frames):
                            if n_win == 1:
                                role_tag = '[当前最新画面]'
                            elif n_win == 2:
                                role_tag = '[上一步画面]' if w_idx == 0 else '[当前最新画面]'
                            else:
                                role_tag = '[首帧基准]' if w_idx == 0 else ('[上一步画面]' if w_idx == 1 else '[当前最新画面]')
                            mm_parts.append({'type': 'text', 'text': f"{role_tag} {mf['caption']}"})
                            mm_parts.append({'type': 'image_url', 'image_url': {'url': f"data:{mf['mime']};base64,{mf['b64']}"}})
                        call_messages = messages + [{'role': 'user', 'content': mm_parts}]
                    else:
                        call_messages = messages
                    api_calls += 1
                    msg = await self.complete(call_messages, tools=available_tools or None, big=use_big)
                    m_step = msg.pop('_metrics', None)
                    finish_reason_step = str(msg.pop('_finish_reason', '') or '')
                    stream_incomplete_step = bool(msg.pop('_stream_incomplete', False))
                    if isinstance(m_step, dict):
                        step_metrics_list.append(m_step)
                    if _is_aborted():
                        raise asyncio.CancelledError()
                    turn_content = self._clean_and_filter_dot_text(msg.get('content') or '')
                    msg['content'] = turn_content
                    thought_str = str(msg.get('thought') or msg.get('reasoning_content') or '').strip()
                    calls = expand_concatenated_tool_calls(msg.get('tool_calls') or [])
                    if calls:
                        msg['tool_calls'] = calls
                    if not calls and turn_content and available_tools:
                        xml_calls, xml_cleaned = self._extract_inline_xml_tool_calls(turn_content, allowed)
                        if xml_calls:
                            calls = xml_calls
                            msg['tool_calls'] = calls
                            turn_content = self._clean_and_filter_dot_text(xml_cleaned)
                            msg['content'] = turn_content
                    if not calls and self.hop_depth.get() == 0 and not trace and 'blackboard' in allowed and turn_content:
                        auto_at = self._extract_plain_at_mention_call(turn_content, user_text=text, cur_src=src)
                        if auto_at is not None:
                            at_target, at_body = auto_at
                            synth_call = {
                                'id': 'call_at_' + secrets.token_hex(4),
                                'type': 'function',
                                'function': {
                                    'name': 'blackboard',
                                    'arguments': json.dumps({'action': 'post', 'mentions': [f'@{at_target}'], 'text': at_body}, ensure_ascii=False),
                                },
                            }
                            calls = [synth_call]
                            msg['tool_calls'] = calls
                            turn_content = ''
                            msg['content'] = ''
                    if not calls and available_tools:
                        is_stream_truncated = bool(stream_incomplete_step or finish_reason_step == 'length')
                        is_fake_action = self._is_unfulfilled_action_promise(turn_content, user_text=text, trace=trace, thought=thought_str)
                        is_empty_dropout = not turn_content
                        is_fake_write = self._is_unfulfilled_write_claim(turn_content, trace=trace) if turn_content else False
                        is_preachy_refusal = bool(
                            turn_content and calibration_tail and guard_is_preachy_refusal(turn_content, user_text=text, trace=trace, thought=thought_str)
                        )
                        if turn_content:
                            turn_content = self._strip_fake_action_stage_directions(turn_content)
                            if self._is_placeholder_text(turn_content):
                                turn_content = ''
                            msg['content'] = turn_content
                        if (is_stream_truncated or is_fake_action or is_empty_dropout or is_fake_write or is_preachy_refusal) and step_dropout_retries < 2 and total_dropout_retries < 5:
                            step_dropout_retries += 1
                            total_dropout_retries += 1
                            messages.append({'role': 'assistant', 'content': turn_content or '（继续执行下一步操作）'})
                            if is_preachy_refusal:
                                is_baike_bot = bool(calibration_tail and calibration_tail == BAIKE_CALIBRATION_TAIL)
                                messages.append({
                                    'role': 'user',
                                    'content': build_preachy_refusal_retry_prompt(bot_name=bot.get('name', ''), is_baike=is_baike_bot),
                                })
                            elif is_fake_write:
                                messages.append({'role': 'user', 'content': '【系统实时校验拦截】：你刚才声称“已经记进/已写入”文件，但本轮 `trace` 中没有任何 `write_file` 或 `edit_file` 调用！严禁口头虚构写入。请立即真实调用 `write_file` 或 `edit_file` 落盘后再回复用户。'})
                            elif trace and (is_fake_action or is_empty_dropout):
                                messages.append({'role': 'user', 'content': '【系统校验提示】：你刚才输出了空内容或停在未执行的动作说明上。若仍有明确可执行工具步骤，请立即发起对应 `tool_calls`；若遇到真实报错、卡死或需用户授权确认，请直接如实说明卡点与现状；若任务已完成，请直接输出完整总结。'})
                            else:
                                messages.append({'role': 'user', 'content': '【系统校验提示】：你刚才只输出了空内容或未发出的动作意图。若仍需调工具请立即发起 `tool_calls`，若受阻或已完成请直接如实向用户汇报。'})
                            continue
                    elif turn_content:
                        turn_content = self._strip_fake_action_stage_directions(turn_content)
                        msg['content'] = turn_content
                    piece_to_add = ''
                    if turn_content:
                        clean_piece, piece_meta = self._extract_reply_piece(turn_content)
                        if piece_meta:
                            parsed_meta.update(piece_meta)
                        piece_to_add = self._clean_and_filter_dot_text(clean_piece or turn_content)
                        piece_to_add, step_choices, step_th, _ = self._extract_all_tags(piece_to_add)
                        if step_choices and not self.choices_var.get():
                            self.choices_var.set(step_choices)
                        if step_th:
                            msg['reasoning_content'] = ((msg.get('reasoning_content') or '') + '\n' + step_th).strip()
                            if step_th not in thought_str:
                                thought_str = (thought_str + '\n' + step_th).strip() if thought_str else step_th
                        piece_to_add = self._clean_and_filter_dot_text(piece_to_add)
                    if calls:
                        step_dropout_retries = 0
                        if thought_str:
                            process_log_steps.append(f'💭 [第{api_calls}步思考] {thought_str}')
                        if piece_to_add:
                            process_log_steps.append(f'💬 [第{api_calls}步进展] {piece_to_add}')
                            if not spoken_segments_in_turn or spoken_segments_in_turn[-1] != piece_to_add:
                                spoken_segments_in_turn.append(piece_to_add)
                                my_exec.setdefault('timeline', []).append({'type': 'text', 'text': piece_to_add})
                            last_step_text = piece_to_add
                            last_nonempty_narration = piece_to_add
                            if len(piece_to_add) >= 120:
                                pre_cleanup_summary = piece_to_add
                            final_turn_text = '\n\n'.join(spoken_segments_in_turn)
                            self.stream_prefix_var.set(final_turn_text)
                        if hasattr(self, 'thought_prefix_var'):
                            self.thought_prefix_var.set('\n\n'.join(process_log_steps[-14:]))
                        first_t = calls[0]['function'].get('name') or 'tool'
                        await self._set_stream(
                            text=final_turn_text or piece_to_add or f'正在调用 {first_t}…',
                            tool=first_t,
                            speaker_id=bot_id,
                            speaker_name=bot['name'],
                            thought=None,
                        )
                    else:
                        if thought_str:
                            process_log_steps.append(f'💭 [总结思考] {thought_str}' if trace else thought_str)
                            if hasattr(self, 'thought_prefix_var'):
                                self.thought_prefix_var.set('\n\n'.join(process_log_steps[-14:]))
                        if piece_to_add:
                            if not spoken_segments_in_turn or spoken_segments_in_turn[-1] != piece_to_add:
                                spoken_segments_in_turn.append(piece_to_add)
                            last_step_text = piece_to_add
                            last_nonempty_narration = piece_to_add
                            last_substantive_step_txt = piece_to_add
                            final_turn_text = '\n\n'.join(spoken_segments_in_turn)
                            self.stream_prefix_var.set(final_turn_text)
                    if not calls:
                        cur_active_goal = (self.store.state.get('active_goals') or {}).get(nb_goal_key)
                        still_in_goal = bool(goal_mode and not goal_completed_in_turn and isinstance(cur_active_goal, dict) and cur_active_goal.get('status') == 'active')
                        if still_in_goal and not _is_waiting_human_gate() and available_tools and goal_wakeup_count < 200:
                            goal_wakeup_count += 1
                            cur_active_goal['wakeups'] = int(cur_active_goal.get('wakeups') or 0) + 1
                            cur_active_goal['total_steps'] = int(cur_active_goal.get('total_steps') or 0) + len(trace)
                            cur_active_goal['updated_at'] = now_iso()
                            await self._set_stream(
                                text=f'🎯 目标模式持续推进中（第 {goal_wakeup_count} 次自动拉起）…',
                                tool=f'🎯 目标模式自动拉起 #{goal_wakeup_count}',
                                speaker_id=bot_id,
                                speaker_name=bot['name'],
                            )
                            messages.append({'role': 'assistant', 'content': last_nonempty_narration or last_step_text or '（检查目标进度并继续执行）'})
                            messages.append({
                                'role': 'user',
                                'content': (
                                    f'【🎯 目标模式引擎自动拉起 (第 {goal_wakeup_count} 次)】：\n'
                                    f'当前持久目标为：「{goal_spec}」。\n'
                                    f'尚未调用 `reply(op="complete_goal", ...)` 结束目标模式：\n'
                                    f'- 若仍有可执行子步骤，请立即发起下一步 `tool_calls` 继续推进。\n'
                                    f'- 若确实已 100% 完成并拿到核实结果，请立即调用 `reply(op="complete_goal", summary="...", verified_evidence="...")` 结束。'
                                ),
                            })
                            continue
                        break
                    norm_append = dict(msg)
                    if norm_append.get('tool_calls'):
                        from core.turn_history import normalize_tool_calls_for_history
                        norm_append['tool_calls'] = normalize_tool_calls_for_history(norm_append['tool_calls'])
                    if msg.get('reasoning_content'):
                        norm_append['reasoning_content'] = str(msg['reasoning_content'])
                    messages.append(norm_append)
                    batch_calls = calls[:48]
                    step_narration_for_batch = piece_to_add
                    from core.tool_schemas import normalize_tool_name
                    def _is_choices_call(c_item: dict) -> bool:
                        fn_n = c_item['function']['name']
                        if fn_n == 'present_choices':
                            return True
                        if fn_n == 'reply':
                            try:
                                import json as _json
                                _op = (_json.loads(c_item['function'].get('arguments') or '{}') or {}).get('op', 'choices')
                                return _op == 'choices'
                            except Exception:
                                return True
                        return False
                    if not all(_is_choices_call(c) for c in batch_calls) and len(piece_to_add) < 120:
                        last_step_text = ''
                    # Universal Resource-Domain Concurrency Scheduler:
                    # - Stateless / read-only calls (`domain is None`) each get their own concurrent lane.
                    # - Stateful / mutating calls sharing the same `domain` are queued sequentially in that domain's lane
                    #   with automatic fail-fast causality, while distinct domains execute concurrently via `asyncio.gather`!
                    batch_norm_names = [normalize_tool_name(c_chk['function']['name']) for c_chk in batch_calls]
                    has_bash_and_fs_mix = (
                        'bash' in batch_norm_names
                        and any(fn in ('read_file', 'write_file', 'edit_file', 'search_files', 'reply') for fn in batch_norm_names)
                    )
                    parsed_batch: list[tuple[int, dict, str, dict, str | None, dict]] = []
                    domain_lanes: dict[str, list[tuple[int, dict, str, dict, str | None, dict]]] = {}
                    independent_lanes: list[list[tuple[int, dict, str, dict, str | None, dict]]] = []
                    for idx_b, c_item in enumerate(batch_calls):
                        try:
                            c_args_init = json.loads(c_item['function'].get('arguments') or '{}')
                            if not isinstance(c_args_init, dict):
                                c_args_init = {}
                        except (ValueError, KeyError, TypeError):
                            c_args_init = {}
                        c_name = c_item['function']['name']
                        domain_key = self._resolve_tool_concurrency_domain(c_name, c_args_init, src, has_bash_and_fs_mix)
                        raw_tr_init: dict[str, Any] = {'tool': c_name, 'arguments': c_args_init, 'duration_ms': 0, 'status': 'running'}
                        if idx_b == 0:
                            if step_narration_for_batch:
                                raw_tr_init['step_note'] = step_narration_for_batch[:280]
                            if thought_str:
                                raw_tr_init['step_thought'] = thought_str[:280]
                        r_ent = compact_trace_list([raw_tr_init])[0]
                        my_exec['trace'].append(r_ent)
                        my_exec.setdefault('timeline', []).append({
                            'type': 'tool',
                            'call_id': str(c_item.get('id') or ''),
                            'tool': c_name,
                            'arguments': r_ent.get('arguments') or {},
                            'status': 'running',
                            'duration_ms': 0,
                        })
                        item_tuple = (idx_b, c_item, c_name, c_args_init, domain_key, r_ent)
                        parsed_batch.append(item_tuple)
                        if domain_key is None:
                            independent_lanes.append([item_tuple])
                        else:
                            domain_lanes.setdefault(domain_key, []).append(item_tuple)

                    all_lanes = independent_lanes + list(domain_lanes.values())
                    names = [x[2] for x in parsed_batch]
                    my_exec['tool'] = names[0] if len(parsed_batch) == 1 else f"并行调度 ({', '.join(names[:3])})"
                    if self.hop_depth.get() == 0:
                        TurnLedgerManager.record_pending_step(self.store, src, bot_id, eff_turn_id, my_exec['tool'])
                    self.execution = my_exec
                    await self._set_stream(
                        text=step_narration_for_batch or (f'正在调用 {names[0]}…' if len(parsed_batch) == 1 else f'正在执行 {len(parsed_batch)} 项任务…'),
                        tool=my_exec['tool'],
                        speaker_id=bot_id,
                        speaker_name=bot['name'],
                        thought=None,
                    )
                    self.stream_seq += 1
                    await self.publish({'type': 'execution', 'source': src, 'bot_id': bot_id, 'turn_id': eff_turn_id, 'execution': my_exec, 'seq': self.stream_seq})

                    ordered_results: list[tuple[dict, str, dict, dict, int, dict] | None] = [None] * len(parsed_batch)

                    async def _run_domain_lane(lane_items: list[tuple[int, dict, str, dict, str | None, dict]]):
                        nonlocal cloud_used_in_turn
                        for idx_b, c_item, c_name, c_args, domain_key, r_ent in lane_items:
                            if c_name in ('cloud', 'cloud_desktop', 'cua_desktop'):
                                cloud_used_in_turn = True
                            t0 = asyncio.get_running_loop().time()
                            try:
                                c_op_low = str(c_args.get('op') or c_args.get('action') or '').strip().lower()
                                if not is_tool_allowed(c_name, allowed):
                                    res = {'ok': False, 'error': '此助手未启用该工具'}
                                elif src in ('pc', 'pc2') and (c_name in ('run_bash', 'run_command') or (c_name == 'bash' and c_op_low in ('', 'run', 'exec'))) and self.is_dsh_online():
                                    res = await self._raw_dsh_rpc('pc_shell', c_args, timeout=float(c_args.get('timeout') or 25.0))
                                elif src in ('pc', 'pc2') and c_name in ('read_file', 'write_file', 'edit_file', 'delete_file', 'move_file', 'search_files', 'list_dir', 'glob_files', 'grep_search') and self._should_use_pc_fs(c_name, c_args):
                                    pc_fs_op, pc_fs_args = build_pc_fs_rpc_payload(c_name, c_args)
                                    res = await self._raw_dsh_rpc('pc_fs', {'fs_op': pc_fs_op, **pc_fs_args}, timeout=25.0)
                                elif c_name == 'send_image' or (c_name == 'reply' and c_op_low in ('send_image', 'image')):
                                    res = await stage_and_execute_send_image(self, src, c_args)
                                elif c_name == 'reply' and c_op_low in ('send_file', 'file', 'send_pdf', 'send_doc'):
                                    res = await stage_and_execute_send_file(self, src, c_args)
                                else:
                                    res = await self.harness.execute(c_name, c_args)
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc:
                                res = {'ok': False, 'error': f'工具执行异常: {str(exc)[:240]}'}
                            dur = max(1, round((asyncio.get_running_loop().time() - t0) * 1000))
                            ordered_results[idx_b] = (c_item, c_name, c_args, res, dur, r_ent)

                    await asyncio.gather(*[_run_domain_lane(lane) for lane in all_lanes])

                    delivered_replies_in_batch: list[str] = []
                    completed_tuples = [r for r in ordered_results if r is not None]
                    for c_item, c_name, c_args, result, dur, r_ent in completed_tuples:
                        if isinstance(result, dict):
                            if result.get('goal_completed'):
                                goal_completed_in_turn = True
                                goal_completed_summary = str(result.get('summary') or '').strip()
                            if result.get('chat_image_url'):
                                img_idx, img_tot, img_list = self._queue_turn_image(str(result['chat_image_url']))
                                result['turn_image_index'] = img_idx
                                result['turn_image_total'] = img_tot
                                result['turn_queued_images'] = img_list
                            if result.get('status') == 'delivered_reply' and result.get('delivered_text'):
                                delivered_replies_in_batch.append(str(result['delivered_text']).strip())
                            result.pop('_already_emitted_turn_bubbles', None)
                            result.pop('_final_emitted_msg', None)
                    all_queued_now = list(self.pending_stickers_var.get() or ())
                    if len(all_queued_now) > 1:
                        for _, _, _, r_dict, _, _ in completed_tuples:
                            if isinstance(r_dict, dict) and 'turn_image_index' in r_dict:
                                r_dict['turn_image_total'] = len(all_queued_now)
                                r_dict['turn_queued_images'] = list(all_queued_now)
                    step_tool_outputs: list[tuple[str, str]] = []
                    for c_item, c_name, c_args, result, dur, r_ent in completed_tuples:
                        if c_name in ('stay_silent', 'do_not_disturb') or (isinstance(result, dict) and result.get('_stay_silent')):
                            silent_requested = True
                            if isinstance(result, dict):
                                silent_reason = str(result.get('reason') or silent_reason or 'quiet').strip()
                        elif c_name in ('notify_user', 'emergency_alarm', 'present_choices', 'send_image'):
                            has_user_visible_reply = True
                        elif isinstance(result, dict) and (result.get('status') == 'delivered_reply' or result.get('chat_image_url') or result.get('chat_file') or result.get('choices_attached')):
                            has_user_visible_reply = True

                        if isinstance(result, dict) and (result.get('_multimodal_b64') or result.get('_image_b64') or result.get('image')):
                            b1 = str(result.pop('_multimodal_b64', '') or '')
                            b2 = str(result.pop('_image_b64', '') or '')
                            b3_raw = result.get('image')
                            b3 = str(result.pop('image', '') or '').split(',')[-1] if isinstance(b3_raw, str) and len(b3_raw) > 256 else ''
                            mm_b64 = b1 or b2 or b3
                            mm_mime = str(result.pop('_multimodal_mime', 'image/jpeg') or 'image/jpeg')
                            mm_cap = str(result.pop('_multimodal_caption', '') or '【工具返回的最新实时画面】')
                            keep_turn = bool(result.pop('_keep_in_turn', False))
                            f_hash = str(result.get('frame_hash') or '')
                            if mm_b64:
                                result['_image_attached'] = True
                                frame_item = {'b64': mm_b64, 'mime': mm_mime, 'caption': mm_cap, 'frame_hash': f_hash, 'step': len(trace) + 1}
                                if keep_turn:
                                    persistent_turn_mm_frames.append(frame_item)
                                else:
                                    if rolling_vision_frames and f_hash and rolling_vision_frames[-1].get('frame_hash') == f_hash:
                                        rolling_vision_frames[-1] = frame_item
                                    else:
                                        rolling_vision_frames.append(frame_item)
                                        if len(rolling_vision_frames) > 6:
                                            rolling_vision_frames[:] = [rolling_vision_frames[0]] + rolling_vision_frames[-4:]
                        raw_done_entry: dict[str, Any] = {'tool': c_name, 'arguments': c_args, 'result': result, 'duration_ms': dur, 'status': 'done'}
                        if r_ent.get('step_note'):
                            raw_done_entry['step_note'] = r_ent['step_note']
                        if r_ent.get('step_thought'):
                            raw_done_entry['step_thought'] = r_ent['step_thought']
                        entry = compact_trace_list([raw_done_entry])[0]
                        trace.append(entry)
                        r_ent.clear()
                        r_ent.update(entry)
                        for tl_it in reversed(my_exec.get('timeline') or []):
                            if tl_it.get('type') == 'tool' and tl_it.get('status') == 'running' and (
                                (c_item.get('id') and tl_it.get('call_id') == c_item.get('id')) or tl_it.get('tool') == c_name
                            ):
                                tl_it['status'] = entry.get('status', 'done')
                                tl_it['duration_ms'] = entry.get('duration_ms', dur)
                                tl_it['result'] = entry.get('result') or {}
                                break
                        if c_name in GUI_UI_TOOLS:
                            raw_res_str = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                            tool_content_str, eph_ui = extract_ephemeral_ui_from_result(c_name, result, raw_res_str)
                            if eph_ui is None and len(tool_content_str) > 8000:
                                tool_content_str = self.context_search.maybe_offload_tool_output(
                                    c_name, c_args, result, bot_id=bot_id, turn_id=self.turn_id_var.get()
                                )
                        else:
                            tool_content_str = self.context_search.maybe_offload_tool_output(
                                c_name, c_args, result, bot_id=bot_id, turn_id=self.turn_id_var.get()
                            )
                            eph_ui = None
                        if eph_ui:
                            ephemeral_tail_notes.append(eph_ui)
                        messages.append({'role': 'tool', 'tool_call_id': c_item['id'], 'content': tool_content_str})
                        step_tool_outputs.append((c_item['id'], tool_content_str))
                    _, had_batch_progress, batch_sent_shot = TaskContinuityManager.evaluate_batch_progress(completed_tuples, 0)
                    if batch_sent_shot:
                        sent_auth_screenshot_in_turn = True
                    append_turn_step(my_exec['turn_steps'], build_compact_turn_step(step_narration_for_batch, batch_calls, step_tool_outputs))
                    my_exec['tool'] = f'已执行 {len(trace)} 步'
                    if self.hop_depth.get() == 0:
                        TurnLedgerManager.record_completed_steps(self.store, src, bot_id, eff_turn_id, [x[1] for x in completed_tuples])
                    if is_fg_user_turn and had_batch_progress:
                        TaskContinuityManager.touch_foreground_activity(self.store)
                    self.execution = my_exec
                    if step_narration_for_batch:
                        my_exec['last_step_text'] = step_narration_for_batch
                    await self._set_stream(
                        text='',
                        tool=my_exec['tool'],
                        speaker_id=bot_id,
                        speaker_name=bot['name'],
                        thought=None,
                    )
                    self.stream_seq += 1
                    await self.publish({'type': 'execution', 'source': src, 'bot_id': bot_id, 'turn_id': eff_turn_id, 'execution': my_exec, 'seq': self.stream_seq})
                    if any(
                        (c['function']['name'] in ('custom_tool', 'admin'))
                        and any(k in (c['function'].get('arguments') or '') for k in ('"create"', '"delete"', '"custom_tool"'))
                        for c in batch_calls
                    ):
                        all_schemas = self.harness.all_tools()
                        plugin_names = set(self.harness.extension_registry.tool_plugins.keys())
                        allowed = set(normalize_bot_tools(bot.get('tools'), default_all=[t['function']['name'] for t in all_schemas])) | plugin_names | {'blackboard', 'cloud', 'admin', 'reply', 'subtask'}
                        if src in ('pc', 'pc2'):
                            allowed -= {'phone', 'phone_workspace', 'phone_macro', 'notify_user', 'emergency_alarm'}
                        else:
                            allowed -= {'pc', 'dsh_workspace', 'message_phone_bot'}
                        available_tools = [t for t in all_schemas if is_tool_allowed(t['function']['name'], allowed)]
                    if all(_is_choices_call(c) for c in batch_calls) and len(final_turn_text.strip()) >= 12:
                        last_step_text = final_turn_text
                        break
                    if self.hop_depth.get() > 0 and delivered_replies_in_batch:
                        rep_body = '\n\n'.join(d for d in delivered_replies_in_batch if d)
                        last_step_text = rep_body
                        last_substantive_step_txt = rep_body
                        final_turn_text = rep_body
                        self.stream_prefix_var.set(final_turn_text)
                        break
                    if silent_requested and not has_user_visible_reply:
                        self.stream_prefix_var.set('')
                        break
                    self.stream_prefix_var.set('')

                if silent_requested and not has_user_visible_reply:
                    self.stream_prefix_var.set('')
                    if self.hop_depth.get() == 0:
                        TurnLedgerManager.finish_turn(
                            self.store, src, bot_id, eff_turn_id,
                            status='silent', reason=silent_reason or 'stay_silent',
                            api_calls=api_calls, steps_count=len(trace),
                            duration_ms=max(1, int(round((time.perf_counter() - turn_started_perf) * 1000))),
                        )
                    self.store.save()
                    self.wake.set()
                    try:
                        return {'status': 'silent', 'ok': True, 'silent': True, 'reason': silent_reason, 'trace': trace}
                    finally:
                        self.choices_var.reset(choices_reset_token)
                        self.choices_prompt_var.reset(choices_prompt_token)

                choice_prompt_str = (self.choices_prompt_var.get() or '').strip()
                if choice_prompt_str and not last_substantive_step_txt:
                    last_substantive_step_txt = choice_prompt_str
                    last_step_text = choice_prompt_str
                    final_turn_text = choice_prompt_str
                    self.stream_prefix_var.set(final_turn_text)

                if goal_mode:
                    cur_g_end = (self.store.state.get('active_goals') or {}).get(nb_goal_key)
                    if isinstance(cur_g_end, dict):
                        cur_g_end['total_steps'] = int(cur_g_end.get('total_steps') or 0) + len(trace)
                        cur_g_end['updated_at'] = now_iso()

                # Pure text summary fallback ONLY when zero substantive text was produced after tool calls.
                # Strictly pass `tools=None` so the model NEVER emits unexecuted post-loop tool_calls!
                if not last_substantive_step_txt and not self._clean_and_filter_dot_text(last_step_text) and trace:
                    self.stream_prefix_var.set('')
                    await self._set_stream(text='正在整理回复…', speaker_id=bot_id, speaker_name=bot['name'])
                    api_calls += 1
                    final_msg = await self.complete(
                        messages + [{'role': 'user', 'content': '请基于以上工具执行结果，直接用自然、清晰的话向用户输出干净完整的最终总结汇报（禁止继续调用工具，禁止只输出句号 `.` 或空白）。'}],
                        tools=None,
                        big=use_big,
                    )
                    if _is_aborted():
                        raise asyncio.CancelledError()
                    m_step_f = final_msg.pop('_metrics', None)
                    if isinstance(m_step_f, dict):
                        step_metrics_list.append(m_step_f)
                    f_th = str(final_msg.get('thought') or final_msg.get('reasoning_content') or '').strip()
                    if f_th:
                        process_log_steps.append(f'💭 [总结思考] {f_th}')
                    content_str = self._clean_and_filter_dot_text(final_msg.get('content') or '')
                    if content_str:
                        clean_piece, piece_meta = self._extract_reply_piece(content_str)
                        if piece_meta:
                            parsed_meta.update(piece_meta)
                        piece_to_add = self._clean_and_filter_dot_text(clean_piece or content_str)
                        piece_to_add, f_choices, f_tag_th, _ = self._extract_all_tags(piece_to_add)
                        if f_choices and not self.choices_var.get():
                            self.choices_var.set(f_choices)
                        if f_tag_th and f_tag_th not in f_th:
                            process_log_steps.append(f'💭 [总结思考] {f_tag_th}')
                        piece_to_add = self._clean_and_filter_dot_text(piece_to_add)
                        if piece_to_add:
                            if not spoken_segments_in_turn or spoken_segments_in_turn[-1] != piece_to_add:
                                spoken_segments_in_turn.append(piece_to_add)
                            last_step_text = piece_to_add
                            last_substantive_step_txt = piece_to_add
                            final_turn_text = '\n\n'.join(spoken_segments_in_turn)
                            self.stream_prefix_var.set(final_turn_text)
                if spoken_segments_in_turn:
                    joined_reply = '\n\n'.join(
                        p for p in (self._clean_and_filter_dot_text(seg) for seg in spoken_segments_in_turn) if p
                    )
                elif last_substantive_step_txt:
                    joined_reply = self._clean_and_filter_dot_text(last_substantive_step_txt)
                elif trace and self._clean_and_filter_dot_text(last_step_text):
                    joined_reply = self._clean_and_filter_dot_text(last_step_text)
                else:
                    joined_reply = self._clean_and_filter_dot_text(final_turn_text) or self._clean_and_filter_dot_text(last_nonempty_narration)
                if pre_cleanup_summary and joined_reply and len(joined_reply) < 100 and pre_cleanup_summary not in joined_reply:
                    joined_reply = f'{pre_cleanup_summary}\n\n{joined_reply}'
                if not joined_reply and goal_completed_summary:
                    joined_reply = goal_completed_summary
                if not joined_reply and trace:
                    last_tool = trace[-1]
                    joined_reply = f"已执行 `{last_tool['tool']}`，结果：{json.dumps(last_tool['result'], ensure_ascii=False)[:400]}"
                llm_only_reply = (joined_reply or last_step_text or last_nonempty_narration).strip()
                data = dict(parsed_meta)
                data['reply'] = joined_reply or '收到，已处理完毕。'
                if self.hop_depth.get() == 0:
                    TaskContinuityManager.persist_turn_metadata(self, data)
                reply_out = str(data.get('reply', '收到。'))
                turn_metrics = TaskContinuityManager.compute_turn_metrics_and_ghost_reset(
                    self, src, bot_id, step_metrics_list, my_exec, reply_out, api_calls, turn_started_perf
                )
                turn_total_ms = turn_metrics['total_ms']
                if self.hop_depth.get() == 0:
                    self.vault.log('助理：' + reply_out, bot_id=bot_id)
                    TurnLedgerManager.finish_turn(
                        self.store, src, bot_id, eff_turn_id,
                        status='truncated' if segment_cap_truncated else 'completed',
                        reason='max_segments_exhausted' if segment_cap_truncated else '',
                        api_calls=api_calls, steps_count=len(trace), duration_ms=turn_total_ms,
                    )
                self.vault.reindex()
                self.store.save()
                self.wake.set()
                extra = {'api_calls': api_calls, 'metrics': turn_metrics, 'from_bot_id': bot_id, 'from_bot_name': bot['name'], 'from_node': src, 'llm_text': llm_only_reply or reply_out}
                if process_log_steps:
                    extra['thought'] = '\n\n'.join(process_log_steps)
                emit_kind = 'chat'
                if self.hop_depth.get() > 0 and isinstance(deferred_incoming_spec, dict):
                    emit_kind = 'bot_reply'
                    extra.update({'to_bot_id': deferred_incoming_spec.get('from_bot_id', ''), 'to_bot_name': deferred_incoming_spec.get('from_bot_name', ''), 'to_node': deferred_incoming_spec.get('from_node', ''), 'direction': 'outbound'})
                if trace:
                    extra['trace'] = trace
                if my_exec.get('timeline'):
                    extra['timeline'] = list(my_exec['timeline'])
                if my_exec.get('turn_steps'):
                    steps_list = list(my_exec['turn_steps'])
                    extra['turn_steps'] = steps_list[-3:] if self.hop_depth.get() > 0 else steps_list
                try:
                    final_msg_for_handoff = await self.emit(reply_out, emit_kind, engine='small_brain', track=False, **extra)
                    return final_msg_for_handoff
                finally:
                    self.choices_var.reset(choices_reset_token)
                    self.choices_prompt_var.reset(choices_prompt_token)
            except asyncio.CancelledError:
                cur_turn = self.turn_id_var.get()
                if (cur_turn and cur_turn in self._stopped_turn_ids) or enqueued_stop_epoch != getattr(self, '_user_stop_epoch_by_node_bot', {}).get(nb_turn_key, 0):
                    return {'stopped': True}
                if self.hop_depth.get() == 0:
                    TurnLedgerManager.finish_turn(self.store, src, bot_id, eff_turn_id, status='interrupted', reason='cancelled', api_calls=api_calls, steps_count=len(trace))
                partial = (last_step_text or last_nonempty_narration or final_turn_text or last_substantive_step_txt).strip()
                if partial and not self._is_placeholder_text(partial):
                    fin_partial = partial if partial.endswith('（已停止）') else f'{partial}\n\n（已停止）'
                    final_msg_for_handoff = await self.emit(fin_partial, 'chat', engine='small_brain', track=False, trace=list(my_exec['trace']), turn_steps=list(my_exec.get('turn_steps') or []), thought='\n\n'.join(process_log_steps) if process_log_steps else '', from_bot_id=bot_id, from_bot_name=bot['name'])
                    return final_msg_for_handoff
                return {'stopped': True}
            finally:
                if is_fg_user_turn:
                    TaskContinuityManager.end_foreground_chain(self, bot_id, src, eff_turn_id)
                    TaskContinuityManager.touch_foreground_activity(self.store)
                    with contextlib.suppress(Exception):
                        self.store.save()
                if cur_task_outer and self.hop_depth.get() == 0:
                    self.queued_tasks_by_bot.get(bot_id, set()).discard(cur_task_outer)
                    self.queued_tasks_by_node_bot.get(nb_turn_key, set()).discard(cur_task_outer)
                self._clear_bot_stream(bot_id, source=src)
                fin_turn = self.turn_id_var.get() or ''
                self.stream_seq += 1
                with contextlib.suppress(Exception):
                    await self.publish({'type': 'stream', 'source': src, 'bot_id': bot_id, 'turn_id': fin_turn, 'stream': None, 'final_message': final_msg_for_handoff, 'seq': self.stream_seq})
                    await self.publish({'type': 'execution', 'source': src, 'bot_id': bot_id, 'turn_id': fin_turn, 'execution': None, 'final_message': final_msg_for_handoff, 'seq': self.stream_seq})
                if thought_prefix_token is not None and hasattr(self, 'thought_prefix_var'):
                    self.thought_prefix_var.reset(thought_prefix_token)
                self.stream_prefix_var.reset(prefix_token)
                self.turn_id_var.reset(turn_token)
