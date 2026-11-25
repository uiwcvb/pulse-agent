"""
[Module Responsibility]
Stream publisher, streaming tool summary, repeat-call signature, and resource-domain concurrency resolver
extracted from `brain_tools_mixin.py` (`server/core/stream_publisher_mixin.py`).
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import httpx
from assistants import get_assistant
from storage import compact_trace_list, normalize_node_source


class StreamPublisherMixin:
    def _should_use_pc_fs(self, fs_op: str, args: dict) -> bool:
        if not self.is_dsh_online():
            return False
        eff_op = fs_op
        if fs_op == 'search_files':
            sf_op = str(args.get('op') or args.get('action') or '').strip().lower()
            if sf_op in ('list', 'list_dir', 'ls', 'dir') or (not sf_op and not args.get('query') and not args.get('pattern')):
                eff_op = 'list_dir'
            elif sf_op in ('glob', 'glob_files', 'find') or (not sf_op and not args.get('query') and args.get('pattern')):
                eff_op = 'glob_files'
            else:
                eff_op = 'grep_search'
        if eff_op == 'grep_search':
            raw_p = str(args.get('path') or args.get('file_path') or '.').strip()
        elif eff_op == 'glob_files':
            raw_p = str(args.get('pattern') or args.get('path') or '**/*').strip()
        elif eff_op == 'list_dir':
            raw_p = str(args.get('path') or '.').strip() or '.'
        else:
            raw_p = str(args.get('path') or args.get('src') or args.get('file_path') or args.get('target') or '').strip()
        if not raw_p:
            return False
        norm_p = raw_p.replace('\\', '/').lstrip('./')
        if re.match(r'^[A-Za-z]:[/\\]', raw_p) or raw_p.startswith(('\\\\', '~')):
            return True
        if norm_p.lower().startswith('uploads/') and (self.harness.root / norm_p).exists():
            return False
        cloud_prefixes = ('memory_vault', 'memory_vault/', 'notes/', 'skills/', '.config/')
        cloud_files = {'settings.json', 'bots.json', 'routines.json', 'memory.md', 'user.md', 'soul.md', 'system.md', 'heartbeat.md', 'index.md'}
        if norm_p.lower() in cloud_files or any(norm_p.lower() == pref.rstrip('/') or norm_p.lower().startswith(pref) for pref in cloud_prefixes):
            return False
        return True

    @staticmethod
    def _summarize_streaming_tool(t_name: str, raw_args: str) -> str:
        if not raw_args:
            return f'正在准备调用 {t_name}…'
        path_m = re.search(r'"(?:path|url|query|app|package)"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', raw_args)
        target = path_m.group(1) if path_m else ''
        content_m = re.search(r'"(?:content|new_text|command|text)"\s*:\s*"((?:[^"\\]|\\.)*)', raw_args, re.DOTALL)
        snippet = ''
        if content_m:
            raw_s = content_m.group(1).replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')
            snippet = raw_s[-240:].strip()
        head = f'{t_name} ({target})' if target else t_name
        return f'{head}\n{snippet}' if snippet else f'正在准备 {head}…'

    def _get_light_stream_context_stats(self, bot_id: str, cur_src: str) -> dict:
        """O(1) cached stats snapshot during 28Hz token streaming (avoids scanning every message on every delta)."""
        nb_key = f'{cur_src}:{bot_id}'
        cache = getattr(self, '_stream_stats_cache', None)
        if not isinstance(cache, dict):
            cache = {}
            self._stream_stats_cache = cache
        now = time.monotonic()
        cached = cache.get(nb_key)
        if cached and (now - cached[0]) < 2.0:
            return cached[1]
        stats = self.memory_engine.get_context_stats(bot_id, source=cur_src)
        cache[nb_key] = (now, stats)
        return stats

    async def _set_stream(self, text='', tool=None, speaker_id=None, speaker_name=None, thought=None):
        bot_id = self.room_context.get() or self.bot_context.get()
        cur_turn_check = self.turn_id_var.get()
        if cur_turn_check and cur_turn_check in self._stopped_turn_ids:
            raise asyncio.CancelledError()
        sp_id = speaker_id or self.bot_context.get()
        sp_name = speaker_name
        if not sp_name:
            try:
                sp_name = get_assistant(self.store, sp_id)['name']
            except ValueError:
                sp_name = sp_id
        cur_src = normalize_node_source(self.source_context.get())
        nb_key = f'{cur_src}:{bot_id}'
        prev = self.streams_by_node_bot.get(nb_key) or {}
        prev_text = str(prev.get('text') or '')
        prefix = (self.stream_prefix_var.get() or '').strip()
        turn_id = self.turn_id_var.get() or str(prev.get('turn_id') or '')
        if self._is_placeholder_text(text):
            if prefix:
                effective_text = prefix
            elif prev.get('bot_id') == bot_id and prev.get('speaker_id') == sp_id and prev.get('turn_id') == turn_id and not self._is_placeholder_text(prev_text):
                effective_text = prev_text
            else:
                effective_text = text if str(text or '').startswith('🎯') else ''
        else:
            clean_piece, _ = self._extract_reply_piece(text)
            piece = self._clean_and_filter_dot_text(clean_piece or text)
            if not piece:
                effective_text = prefix or ''
            elif prefix and piece != prefix and not piece.startswith(prefix):
                effective_text = prefix + '\n\n' + piece
            else:
                effective_text = piece
        trace_tail = []
        sub_agents = []
        todos = []
        cur_exec = self.executions_by_node_bot.get(nb_key)
        if cur_exec:
            if isinstance(cur_exec.get('trace'), list):
                trace_tail = compact_trace_list(cur_exec['trace'][-24:])
            if isinstance(cur_exec.get('sub_agents'), list):
                sub_agents = list(cur_exec['sub_agents'])
            elif isinstance(cur_exec.get('sub_agents'), dict):
                sub_agents = list(cur_exec['sub_agents'].values())
            if isinstance(cur_exec.get('todos'), list):
                todos = list(cur_exec['todos'])
        same_stream = (prev.get('bot_id') == bot_id and prev.get('turn_id') == turn_id and prev.get('speaker_id') == sp_id)
        is_real_tool_name = bool(
            tool and isinstance(tool, str)
            and not tool.startswith(('正在', '已执行', '并行执行', '并行调度', '🎯'))
        )
        if not trace_tail and same_stream and isinstance(prev.get('trace'), list):
            trace_tail = [
                {**t, 'status': 'done'} if (not is_real_tool_name and isinstance(t, dict) and t.get('status') == 'running') else t
                for t in prev['trace']
            ]
        if is_real_tool_name:
            if not trace_tail or trace_tail[-1].get('tool') != tool or trace_tail[-1].get('status') != 'running':
                trace_tail.append({
                    'tool': tool,
                    'arguments': {},
                    'duration_ms': 0,
                    'status': 'running',
                })
        effective_tool = tool
        if not effective_tool:
            if isinstance(cur_exec, dict) and cur_exec.get('tool'):
                effective_tool = str(cur_exec['tool'])
            elif same_stream and prev.get('tool'):
                effective_tool = str(prev['tool'])
            elif trace_tail:
                effective_tool = f'已执行 {len(trace_tail)} 步'
        thought_prefix = (getattr(self, 'thought_prefix_var', None) and self.thought_prefix_var.get() or '').strip()
        if thought is None:
            raw_thought = str(prev.get('thought') or '') if same_stream else thought_prefix
            if thought_prefix and raw_thought and not raw_thought.startswith(thought_prefix):
                raw_thought = f'{thought_prefix}\n\n{raw_thought}'
            elif thought_prefix and not raw_thought:
                raw_thought = thought_prefix
        else:
            clean_th = str(thought).strip()
            if thought_prefix and clean_th and not clean_th.startswith(thought_prefix):
                raw_thought = f'{thought_prefix}\n\n💭 {clean_th}'
            elif thought_prefix and not clean_th:
                raw_thought = thought_prefix
            else:
                raw_thought = clean_th
        effective_text, tag_choices, tag_thought, tag_sticker = self._extract_all_tags(effective_text, queue_media=False)
        effective_text = self.strip_trailing_partial_control_tag(effective_text)
        if tag_choices:
            self.choices_var.set(tag_choices)
        if tag_thought:
            raw_thought = (raw_thought + '\n' + tag_thought).strip() if raw_thought and tag_thought not in raw_thought else tag_thought
        effective_thought = raw_thought
        delta_text = ''
        if same_stream and prev_text and not self._is_placeholder_text(prev_text) and effective_text.startswith(prev_text):
            delta_text = effective_text[len(prev_text):]
        # Never expose quick-choice pills prematurely before body prose streams!
        has_body_prose = bool(effective_text.strip()) and not self._is_placeholder_text(effective_text)
        live_choices: list = []
        if has_body_prose and not is_real_tool_name:
            live_choices = list(self.choices_var.get() or [])
            if not live_choices and same_stream and isinstance(prev.get('choices'), list):
                live_choices = list(prev['choices'])
        self.stream_seq += 1
        self.live_stream = {
            'seq': self.stream_seq, 'turn_id': turn_id, 'bot_id': bot_id, 'source': cur_src,
            'speaker_id': sp_id, 'speaker_name': sp_name, 'speaker_node': cur_src,
            'text': effective_text, 'delta': delta_text, 'thought': effective_thought,
            'step_text': str((cur_exec or {}).get('last_step_text') or (prev.get('step_text') if same_stream else '') or ''),
            'tool': effective_tool, 'trace': trace_tail,
            'turn_steps': list((cur_exec or {}).get('turn_steps') or [])[-32:],
            'timeline': self._sanitize_timeline_entries(list((cur_exec or {}).get('timeline') or [])[-48:], queue_media=False, strip_partial=True),
            'sub_agents': sub_agents, 'todos': todos,
            'choices': live_choices,
            'context_stats': self._get_light_stream_context_stats(bot_id, cur_src),
            'updated_at': time.time(),
        }
        self.streams_by_node_bot[nb_key] = self.live_stream
        self.streams_by_bot[bot_id] = self.live_stream
        await self.publish({'type': 'stream', 'source': cur_src, 'stream': self.live_stream})

    @property
    def _next_tool_choice(self):
        var = getattr(self, '_next_tool_choice_var', None)
        if var is not None:
            return var.get()
        return self.__dict__.get('_next_tool_choice_fallback')

    @_next_tool_choice.setter
    def _next_tool_choice(self, val):
        var = getattr(self, '_next_tool_choice_var', None)
        if var is not None:
            var.set(val)
        else:
            self.__dict__['_next_tool_choice_fallback'] = val

    async def complete(self, messages, tools=None, big=False, tool_choice=None):
        ntc_val = self._next_tool_choice
        self._next_tool_choice = None
        eff_tc = tool_choice or ntc_val
        bot_id = self.bot_context.get()
        cur_src = normalize_node_source(self.source_context.get())
        try:
            bot = get_assistant(self.store, bot_id, source=cur_src)
        except ValueError:
            bot = {}
        return await self.llm.complete_stream(
            messages=messages, bot_id=bot_id, bot_model=str(bot.get('model') or ''), tools=tools, big=big,
            stream_prefix=self.stream_prefix_var.get() or '',
            on_stream=lambda text='', tool=None, thought=None: self._set_stream(text=text, tool=tool, thought=thought),
            tool_choice=eff_tc, source=cur_src,
        )

    async def _fetch_available_models(self) -> list[str]:
        if hasattr(self, 'llm') and self.llm is not None:
            try:
                return await self.llm.fetch_available_models()
            except Exception:
                pass
        from core.channel_router import fetch_aggregated_models
        try:
            agg = await fetch_aggregated_models(self.store, persist_channel_models=False)
            return list(agg.get('models') or [])
        except Exception:
            return []

    def _build_tool_call_sig(self, name: str, args: dict) -> tuple[str, bool]:
        op_val = str(args.get('op') or args.get('action') or '').strip().lower()
        is_scroll_op = (
            name in ('phone', 'cloud', 'pc', 'phone_workspace', 'cloud_desktop', 'dsh_workspace')
            and op_val in ('scroll', 'swipe', 'scroll_to', 'key', 'press', 'press_key')
        )
        if is_scroll_op:
            norm_args = {k: v for k, v in args.items() if k not in ('amount', 'pages', 'duration_ms')}
            state_fp = self._last_phone_tree_hash if name in ('phone', 'phone_workspace') else ''
            return f"{name}:scroll:{json.dumps(norm_args, sort_keys=True, ensure_ascii=False)}:fp={state_fp}", True
        return f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}", False

    @staticmethod
    def _resolve_tool_concurrency_domain(name: str, args: dict, src: str, has_bash_and_fs_mix: bool = False) -> str | None:
        if name in (
            'web', 'subtask', 'reply',
            'memory_search', 'context_search', 'web_search', 'web_fetch', 'peek_bots',
            'system_status', 'present_choices', 'notify_user', 'stay_silent', 'do_not_disturb', 'send_image',
            'complete_goal', 'finish_goal', 'run_subtask',
        ):
            return None
        if name == 'memory':
            op_v = str(args.get('op') or args.get('action') or 'search').strip().lower()
            return None if op_v in ('', 'search', 'conversation', 'conv', 'ctx', 'context', 'list', 'pins', 'list_pins') else 'domain:memory_pin'
        if name == 'blackboard':
            act_v = str(args.get('action') or args.get('op') or 'list').strip().lower()
            return None if act_v in ('read', 'list', 'peek') else 'domain:blackboard'
        if name == 'admin':
            op_v = str(args.get('op') or 'status').strip().lower()
            act_v = str(args.get('action') or '').strip().lower()
            if op_v in ('status', 'doctor', 'context', 'all', 'check', 'models', 'list_models', ''):
                return None
            if op_v in ('config', 'system_config') and act_v in ('', 'get', 'status', 'pc_connect_guide', 'phone_wireless_guide', 'test_api'):
                return None
            if op_v in ('skill', 'custom_tool', 'plugin') and act_v in ('', 'list', 'list_skills', 'open_skill', 'read_skill', 'open', 'read', 'load'):
                return None
            return f'domain:admin:{op_v}'
        if name == 'system_config':
            act_v = str(args.get('action') or 'status').strip().lower()
            return None if act_v in ('get', 'status', 'pc_connect_guide', 'phone_wireless_guide', 'test_api') else 'domain:system_config'
        if name == 'deploy_cloud_computer':
            return 'domain:deploy_cloud_computer'
        if name in ('read_file', 'search_files', 'list_dir', 'glob_files', 'grep_search'):
            return f"domain:fs_workspace:{src}" if has_bash_and_fs_mix else None
        if name in ('write_file', 'edit_file', 'delete_file', 'move_file'):
            if has_bash_and_fs_mix:
                return f"domain:fs_workspace:{src}"
            p_norm = str(args.get('path') or args.get('src') or '').strip().replace('\\', '/').lower()
            while p_norm.startswith('./'):
                p_norm = p_norm[2:]
            return f"domain:file:{src}:{p_norm or 'default'}"
        if name in ('bash', 'run_bash', 'run_command'):
            return f"domain:fs_workspace:{src}" if has_bash_and_fs_mix else f"domain:shell:{src}"
        if name in ('phone', 'phone_workspace', 'phone_macro'):
            op_v = str(args.get('op') or '').strip().lower()
            act_v = str(args.get('action') or 'list').strip().lower()
            if op_v in ('status', 'apps', 'device_info', 'list_files', 'zoom'):
                return None
            if op_v == 'notifications' and act_v in ('', 'list', 'list_filters'):
                return None
            if op_v == 'shell':
                return 'domain:phone_shell'
            if op_v in ('pull_file', 'push_file'):
                return 'domain:phone_transfer'
            return 'domain:phone_gui'
        if name in ('pc', 'dsh_workspace'):
            op_v = str(args.get('op') or '').strip().lower()
            act_v = str(args.get('action') or '').strip().lower()
            if op_v in ('status', 'list_sessions', 'inspect_session', 'zoom'):
                return None
            if op_v == 'cua_desktop' and act_v in ('list_windows', 'get_window_state'):
                return None
            if op_v == 'pc_shell':
                return 'domain:pc_shell'
            if op_v == 'dispatch_session':
                sid = str(args.get('session_id') or 'default').strip()
                return f"domain:dsh_session:{sid}"
            return 'domain:pc_gui'
        if name in ('cloud', 'cloud_desktop'):
            op_v = str(args.get('op') or '').strip().lower()
            return None if op_v == 'status' else 'domain:cloud_desktop'
        if name == 'custom_tool':
            act_v = str(args.get('action') or '').strip().lower()
            return None if act_v in ('list', 'list_skills', 'open_skill', 'read_skill') else 'domain:custom_tool'
        if name in ('send_message', 'message_phone_bot'):
            tgt = str(args.get('to_bot_id') or args.get('target') or args.get('bot_id') or name).strip().lower()
            return f"domain:interconnect:{tgt}"
        return f"domain:ext:{name}"
