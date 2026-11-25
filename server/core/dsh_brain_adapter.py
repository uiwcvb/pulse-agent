"""
[Module Responsibility]
HTTP NDJSON Bridge & Session Coordinator between Python FastAPI Gateway (`server/brain.py`)
and the official `@deepseek-ai/*` (`0.2.2-rc.1`) Node DSH Sidecar (`server/dsh_brain/`).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import json
import logging
import os
import re
import time
from typing import Any

import httpx
from core.canonical_tool_handlers import build_pc_fs_rpc_payload, stage_and_execute_send_file, stage_and_execute_send_image
from core.cloud_stuck_detector import CloudStuckDetector
from core.observation_masker import GUI_UI_TOOLS, extract_ephemeral_ui_from_result
from core.vision_engine import detect_image_mime
from core.task_continuity import TaskContinuityManager
from core.tool_schemas import is_tool_allowed, normalize_tool_name
from core.turn_history import append_turn_step, build_compact_turn_step
from core.turn_ledger import TurnLedgerManager
from storage import compact_trace_list, normalize_node_source
from vault_manager import now_iso

logger = logging.getLogger(__name__)


class DshBrainAdapter:
    """Bridges chat turns, tool execution, cancellation, and session resets to the Node DSH kernel."""

    def __init__(self, brain: Any):
        self.brain = brain
        self.base_url = os.getenv('DSH_BRAIN_URL', 'http://127.0.0.1:18199').strip().rstrip('/')
        self.secret = os.getenv('DSH_BRAIN_SECRET', '').strip()
        self.mode = (os.getenv('BRAIN_ENGINE', 'auto') or 'auto').strip().lower()
        self._health_cache_ts: float = 0.0
        self._health_ok: bool = False
        self._health_info: dict[str, Any] = {'ok': False, 'engine': 'legacy'}
        self._turn_acc: dict[str, dict[str, Any]] = {}
        self._pending_reset_keys: set[str] = set()
        self._warm_boot_id: str = ''
        self._warm_sessions: set[str] = set()

    def _has_monkeypatched_llm(self) -> bool:
        b = self.brain
        if 'complete' in getattr(b, '__dict__', {}):
            return True
        llm = getattr(b, 'llm', None)
        if llm is not None and any(k in getattr(llm, '__dict__', {}) for k in ('complete_stream', 'complete_silent')):
            return True
        return False

    def active_engine_label(self) -> str:
        if self.mode == 'legacy' or self._has_monkeypatched_llm():
            return 'legacy'
        if self.mode == 'dsh':
            return 'dsh'
        return 'dsh' if self._health_ok else 'legacy'

    async def health_status(self, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not force and (now - self._health_cache_ts) < 5.0:
            return dict(self._health_info)
        try:
            headers = {'X-DSH-Secret': self.secret} if self.secret else {}
            async with httpx.AsyncClient(timeout=1.5, trust_env=False) as client:
                r = await client.get(f'{self.base_url}/health', headers=headers)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get('ok'):
                    self._health_ok = True
                    self._health_info = data
                    self._health_cache_ts = now
                    b_id = str(data.get('boot_id') or '')
                    if b_id and b_id != self._warm_boot_id:
                        self._warm_boot_id = b_id
                        self._warm_sessions = set(data.get('warm_sessions') or [])
                    elif isinstance(data.get('warm_sessions'), list):
                        self._warm_sessions.update(str(s) for s in data['warm_sessions'] if s)
                    return dict(data)
        except Exception as exc:
            self._health_ok = False
            self._health_info = {'ok': False, 'error': str(exc)[:180]}
            self._health_cache_ts = now
            return dict(self._health_info)
        self._health_ok = False
        self._health_info = {'ok': False, 'engine': 'unavailable'}
        self._health_cache_ts = now
        return dict(self._health_info)

    async def is_healthy(self, force: bool = False) -> bool:
        st = await self.health_status(force=force)
        return bool(st.get('ok'))

    async def should_use_dsh(self) -> bool:
        if self.mode == 'legacy':
            return False
        if self._has_monkeypatched_llm():
            return False
        if self.mode == 'dsh':
            return True
        return await self.is_healthy()

    def _session_key(self, bot_id: str, source: str) -> str:
        return f'{normalize_node_source(source)}__{bot_id or "companion"}'

    def _get_or_init_turn_acc(
        self,
        turn_id: str,
        allowed: set[str] | None = None,
        *,
        turn_origin: str | None = None,
        hop_depth: int = 0,
    ) -> dict[str, Any]:
        rec = self._turn_acc.get(turn_id)
        if rec is None:
            rec = {
                'allowed': set(allowed) if allowed else None,
                'turn_origin': turn_origin,
                'hop_depth': hop_depth,
                'stickers': [],
                'files': [],
                'emitted_stickers': [],
                'emitted_files': [],
                'choices': None,
                'choices_prompt': '',
                'goal_completed': False,
                'goal_summary': '',
                'silent_requested': False,
                'silent_reason': '',
                'stuck_detector': CloudStuckDetector(),
            }
            self._turn_acc[turn_id] = rec
        return rec

    async def execute_tool_from_sidecar(
        self,
        *,
        name: str,
        args: dict[str, Any],
        bot_id: str,
        source: str,
        turn_id: str,
    ) -> dict[str, Any]:
        """Invoke a Python canonical/custom tool with isolated per-request ContextVars and return JSON + image."""
        b = self.brain
        src = normalize_node_source(source or 'phone')
        eff_bot = str(bot_id or 'companion').strip()
        eff_args = dict(args or {})

        acc = self._turn_acc.get(turn_id)
        allowed = acc.get('allowed') if isinstance(acc, dict) else None
        rec_origin = acc.get('turn_origin') if isinstance(acc, dict) else None
        rec_hop = int(acc.get('hop_depth') or 0) if isinstance(acc, dict) else 0

        m_bot = b.bot_context.set(eff_bot)
        m_room = b.room_context.set(eff_bot)
        m_src = b.source_context.set(src)
        m_turn = b.turn_id_var.set(turn_id or '')
        m_hop = b.hop_depth.set(rec_hop) if hasattr(b, 'hop_depth') and rec_hop else None
        m_origin = b.turn_origin_context.set(rec_origin) if (rec_origin and hasattr(b, 'turn_origin_context')) else None
        m_choices = b.choices_var.set(None)
        m_cprompt = b.choices_prompt_var.set('')
        m_sticker = b.pending_sticker_var.set('')
        m_stickers = b.pending_stickers_var.set(())
        m_files = b.pending_files_var.set(()) if hasattr(b, 'pending_files_var') else None
        try:
            c_op_low = str(eff_args.get('op') or eff_args.get('action') or '').strip().lower()
            req_node_val = str(eff_args.get('node') or eff_args.get('target') or '').strip().lower()
            is_pc_plain_bash = (
                req_node_val in ('pc', 'pc2', 'pc_windows', 'windows')
                or (src in ('pc', 'pc2') and req_node_val not in ('server', 'hub', 'cloud_sandbox'))
            ) and (
                name in ('run_bash', 'run_command') or (name == 'bash' and c_op_low in ('', 'run', 'exec'))
            )
            if allowed is not None and not is_tool_allowed(name, allowed):
                result: Any = {'ok': False, 'error': '此助手未启用该工具'}
            elif is_pc_plain_bash:
                if b.is_dsh_online():
                    result = await b._raw_dsh_rpc('pc_shell', eff_args, timeout=float(eff_args.get('timeout') or 25.0))
                    if isinstance(result, dict):
                        result.setdefault('node', 'pc_windows')
                else:
                    result = {
                        'ok': False,
                        'pc_online': False,
                        'node': 'pc_offline',
                        'error': 'Windows 电脑端代理当前未在线，已阻止将本机 bash 静默回退到云端 Linux 沙箱。请等待电脑端重连，或改用 cloud(op="shell") 操作云电脑，或显式传 target="cloud_sandbox" 在中枢沙箱执行。',
                    }
            elif src in ('pc', 'pc2') and name in ('read_file', 'write_file', 'edit_file', 'delete_file', 'move_file', 'search_files', 'list_dir', 'glob_files', 'grep_search') and b._should_use_pc_fs(name, eff_args) and c_op_low not in ('locate', 'find_anywhere', 'cross_locate'):
                pc_fs_op, pc_fs_args = build_pc_fs_rpc_payload(name, eff_args)
                result = await b._raw_dsh_rpc('pc_fs', {'fs_op': pc_fs_op, **pc_fs_args}, timeout=25.0)
            elif name == 'send_image' or (name == 'reply' and c_op_low in ('send_image', 'image')):
                result = await stage_and_execute_send_image(b, src, eff_args)
            elif name == 'reply' and c_op_low in ('send_file', 'file', 'send_pdf', 'send_doc'):
                result = await stage_and_execute_send_file(b, src, eff_args)
            else:
                result = await b.harness.execute(name, eff_args)

            # Attach CloudStuckDetector loop pivot nudge on DSH primary turns
            stuck_nudge_msg = ''
            if isinstance(acc, dict) and normalize_tool_name(name) in ('cloud', 'phone', 'pc'):
                sd = acc.get('stuck_detector')
                if isinstance(sd, CloudStuckDetector):
                    verdict = sd.observe(name, eff_args, result)
                    if verdict.stage == 'nudge' and verdict.message:
                        stuck_nudge_msg = verdict.message

            # Capture ContextVar side effects & IMMEDIATELY push media/screenshots/files to chat
            emitted_now_note = ''
            if isinstance(acc, dict):
                new_imgs: list[str] = []
                q_stickers = list(b.pending_stickers_var.get() or ())
                if b.pending_sticker_var.get():
                    q_stickers.append(b.pending_sticker_var.get())
                for s_url in q_stickers:
                    if s_url and s_url not in acc['emitted_stickers'] and s_url not in acc['stickers']:
                        new_imgs.append(s_url)
                if isinstance(result, dict):
                    for key_img in ('chat_image_url', 'sticker'):
                        val_img = str(result.get(key_img) or '').strip()
                        if val_img and val_img not in acc['emitted_stickers'] and val_img not in new_imgs:
                            new_imgs.append(val_img)
                new_files: list[dict[str, Any]] = []
                q_files = list(b.pending_files_var.get() or ()) if hasattr(b, 'pending_files_var') else []
                if isinstance(result, dict) and isinstance(result.get('chat_file'), dict):
                    q_files.append(result['chat_file'])
                for f_item in q_files:
                    if isinstance(f_item, dict) and f_item.get('path'):
                        if not any(isinstance(x, dict) and x.get('path') == f_item['path'] for x in (acc['emitted_files'] + new_files)):
                            new_files.append(f_item)

                # Keep stickers (/stickers/* or reply(op="sticker")) attached to the final reply bubble so they never split thinking into two cards
                is_sticker_op = normalize_tool_name(name) == 'reply' and str(eff_args.get('op') or eff_args.get('action') or '').strip().lower() in ('sticker', 'send_sticker', 'emoji')
                real_media_imgs = [u for u in new_imgs if not is_sticker_op and not u.startswith('/stickers/') and (u.startswith('/') or u.startswith(('http://', 'https://')))]
                plain_stickers = [u for u in new_imgs if u not in real_media_imgs]
                for ps in plain_stickers:
                    if ps not in acc['stickers']:
                        acc['stickers'].append(ps)
                if (real_media_imgs or new_files) and hasattr(b, 'emit'):
                    try:
                        # Clear context vars so emit() doesn't double-attach unrelated state
                        b.pending_sticker_var.set('')
                        b.pending_stickers_var.set(())
                        if hasattr(b, 'pending_files_var'):
                            b.pending_files_var.set(())
                        push_extra: dict[str, Any] = {
                            'non_terminal': True,
                            'from_bot_id': eff_bot,
                            'from_node': src,
                            'turn_id': turn_id or '',
                        }
                        if real_media_imgs:
                            push_extra['stickers'] = list(real_media_imgs)
                            push_extra['sticker'] = real_media_imgs[-1]
                        if new_files:
                            push_extra['attachments'] = list(new_files)
                        await b.emit('', 'chat', track=False, **push_extra)
                        acc['emitted_stickers'].extend(real_media_imgs)
                        acc['emitted_files'].extend(new_files)
                        emitted_now_note = (
                            '【已实时送达用户】图片/文件已立刻推送到用户聊天界面（无需等待本轮结束）。'
                            '如果正在等用户扫码、付款、输入或人工确认，请立刻用简短文字说明并结束本轮对话，严禁调用 shell sleep 原地干等！'
                        )
                    except Exception:
                        for u in real_media_imgs:
                            if u not in acc['stickers']:
                                acc['stickers'].append(u)
                        acc['files'].extend(new_files)
                else:
                    for u in real_media_imgs:
                        if u not in acc['stickers']:
                            acc['stickers'].append(u)
                    acc['files'].extend(new_files)
                if b.choices_var.get():
                    acc['choices'] = list(b.choices_var.get())
                if b.choices_prompt_var.get():
                    acc['choices_prompt'] = str(b.choices_prompt_var.get())
                if isinstance(result, dict):
                    if result.get('goal_completed'):
                        acc['goal_completed'] = True
                        acc['goal_summary'] = str(result.get('summary') or '').strip()
                    if normalize_tool_name(name) in ('stay_silent', 'do_not_disturb') or result.get('_stay_silent'):
                        if not acc.get('emitted_stickers') and not acc.get('emitted_files'):
                            acc['silent_requested'] = True
                            acc['silent_reason'] = str(result.get('reason') or 'quiet').strip()

            mm_b64 = ''
            mm_mime = 'image/jpeg'
            if isinstance(result, dict):
                clean_res: Any = dict(result)
                b1 = str(clean_res.pop('_multimodal_b64', '') or '')
                b2 = str(clean_res.pop('_image_b64', '') or '')
                b3_raw = clean_res.get('image')
                b3 = str(clean_res.pop('image', '') or '').split(',')[-1] if isinstance(b3_raw, str) and len(b3_raw) > 256 else ''
                mm_b64 = b1 or b2 or b3
                mm_mime = str(clean_res.pop('_multimodal_mime', 'image/jpeg') or 'image/jpeg')
                clean_res.pop('_multimodal_caption', None)
                clean_res.pop('_keep_in_turn', None)
                clean_res.pop('_already_emitted_turn_bubbles', None)
                clean_res.pop('_final_emitted_msg', None)
                if mm_b64:
                    clean_res['_image_attached'] = True
                if emitted_now_note:
                    clean_res['delivered_immediately'] = True
                    clean_res['note'] = emitted_now_note
            else:
                clean_res = result

            # Context protection: apply GUI ephemeral UI compression and FTS5 offloading identically to native loop
            cs = getattr(b, 'context_search', None)
            if name in GUI_UI_TOOLS:
                raw_res_str = clean_res if isinstance(clean_res, str) else json.dumps(clean_res, ensure_ascii=False)
                compact_str, eph_ui = extract_ephemeral_ui_from_result(name, clean_res, raw_res_str)
                if cs is not None and eph_ui is None and len(compact_str) > 8000:
                    compact_str = cs.maybe_offload_tool_output(name, eff_args, clean_res, bot_id=eff_bot, turn_id=turn_id)
                if compact_str != raw_res_str or eph_ui:
                    try:
                        parsed_c = json.loads(compact_str)
                        if isinstance(parsed_c, dict):
                            if eph_ui:
                                parsed_c['_ephemeral_ui'] = eph_ui
                            clean_res = parsed_c
                        elif eph_ui and isinstance(clean_res, dict):
                            clean_res['_ephemeral_ui'] = eph_ui
                    except Exception:
                        if isinstance(clean_res, dict) and eph_ui:
                            clean_res['_ephemeral_ui'] = eph_ui
            elif cs is not None:
                raw_res_str = clean_res if isinstance(clean_res, str) else json.dumps(clean_res, ensure_ascii=False)
                offloaded_str = cs.maybe_offload_tool_output(name, eff_args, clean_res, bot_id=eff_bot, turn_id=turn_id)
                if offloaded_str != raw_res_str:
                    with contextlib.suppress(Exception):
                        parsed_o = json.loads(offloaded_str)
                        if isinstance(parsed_o, dict):
                            if isinstance(clean_res, dict):
                                for keep_k in ('ok', 'error', 'chat_image_url', 'sticker', 'chat_file', 'goal_completed', '_stay_silent'):
                                    if keep_k in clean_res and keep_k not in parsed_o:
                                        parsed_o[keep_k] = clean_res[keep_k]
                            clean_res = parsed_o

            return {
                'ok': True,
                'result': (
                    {**clean_res, '_stuck_nudge': stuck_nudge_msg}
                    if (stuck_nudge_msg and isinstance(clean_res, dict))
                    else clean_res
                ),
                'image_b64': mm_b64,
                'image_mime': mm_mime,
            }
        except Exception as exc:
            return {'ok': False, 'result': {'ok': False, 'error': str(exc)[:280]}, 'image_b64': '', 'image_mime': 'image/jpeg'}
        finally:
            if m_origin is not None and hasattr(b, 'turn_origin_context'):
                b.turn_origin_context.reset(m_origin)
            if m_hop is not None and hasattr(b, 'hop_depth'):
                b.hop_depth.reset(m_hop)
            if m_files is not None and hasattr(b, 'pending_files_var'):
                b.pending_files_var.reset(m_files)
            b.pending_stickers_var.reset(m_stickers)
            b.pending_sticker_var.reset(m_sticker)
            b.choices_prompt_var.reset(m_cprompt)
            b.choices_var.reset(m_choices)
            b.turn_id_var.reset(m_turn)
            b.source_context.reset(m_src)
            b.room_context.reset(m_room)
            b.bot_context.reset(m_bot)

    async def cancel_turn(self, bot_id: str, source: str = 'phone') -> None:
        if self.mode == 'legacy' or (self.mode != 'dsh' and not self._health_ok):
            return
        try:
            headers = {'X-DSH-Secret': self.secret} if self.secret else {}
            async with httpx.AsyncClient(timeout=0.8, trust_env=False) as client:
                await client.post(
                    f'{self.base_url}/turn/cancel',
                    headers=headers,
                    json={'session_key': self._session_key(bot_id, source), 'bot_id': bot_id, 'source': normalize_node_source(source)},
                )
        except Exception as exc:
            logger.warning('DSH cancel_turn failed (%s:%s): %s', source, bot_id, exc)

    async def reset_session(self, bot_id: str, source: str = 'phone') -> None:
        if self.mode == 'legacy':
            return
        skey = self._session_key(bot_id, source)
        self._pending_reset_keys.add(skey)
        self._warm_sessions.discard(skey)
        try:
            headers = {'X-DSH-Secret': self.secret} if self.secret else {}
            async with httpx.AsyncClient(timeout=1.5, trust_env=False) as client:
                r = await client.post(
                    f'{self.base_url}/session/reset',
                    headers=headers,
                    json={'session_key': skey, 'bot_id': bot_id, 'source': normalize_node_source(source)},
                )
                if r.status_code == 200:
                    self._health_ok = True
                    self._pending_reset_keys.discard(skey)
        except Exception:
            pass

    async def keepalive_session(self, bot_id: str, source: str = 'phone') -> dict[str, Any]:
        if self.mode == 'legacy' or (self.mode != 'dsh' and not self._health_ok):
            return {'ok': True, 'skipped': True, 'reason': 'dsh_disabled'}
        skey = self._session_key(bot_id, source)
        try:
            headers = {'X-DSH-Secret': self.secret} if self.secret else {}
            async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
                r = await client.post(f'{self.base_url}/session/keepalive', headers=headers, json={'session_key': skey, 'bot_id': bot_id, 'source': normalize_node_source(source)})
                if r.status_code == 200:
                    return r.json()
        except Exception as exc:
            return {'ok': False, 'error': str(exc)[:160]}
        return {'ok': False, 'error': 'keepalive_failed'}

    async def _publish_live_state(
        self,
        *,
        bot_id: str,
        bot_name: str,
        src: str,
        turn_id: str,
        text: str,
        thought: str,
        tool: str | None,
        step_text: str,
        my_exec: dict[str, Any],
    ) -> None:
        b = self.brain
        nb_key = f'{src}:{bot_id}'
        b.stream_seq += 1
        clean_stream_txt, _, _, _ = b._extract_all_tags(str(text or ''), queue_media=False)
        clean_stream_txt = b.strip_trailing_partial_control_tag(clean_stream_txt)
        clean_timeline = b._sanitize_timeline_entries(
            list(my_exec.get('timeline') or [])[-48:],
            queue_media=False,
            strip_partial=True,
        )
        stream_obj = {
            'seq': b.stream_seq,
            'turn_id': turn_id,
            'bot_id': bot_id,
            'source': src,
            'speaker_id': bot_id,
            'speaker_name': bot_name,
            'speaker_node': src,
            'text': clean_stream_txt,
            'delta': '',
            'thought': thought,
            'step_text': step_text,
            'tool': tool,
            'trace': compact_trace_list(list(my_exec.get('trace') or [])[-24:]),
            'turn_steps': list(my_exec.get('turn_steps') or [])[-32:],
            'timeline': clean_timeline,
            'context_stats': b._get_light_stream_context_stats(bot_id, src),
            'updated_at': time.time(),
        }
        b.live_stream = stream_obj
        b.streams_by_node_bot[nb_key] = stream_obj
        b.streams_by_bot[bot_id] = stream_obj
        b.execution = my_exec
        await b.publish({'type': 'stream', 'source': src, 'stream': stream_obj})

    async def run_chat_turn(
        self,
        *,
        bot_id: str,
        src: str,
        bot: dict[str, Any],
        eff_turn_id: str,
        raw_user_text: str,
        messages: list[dict[str, Any]],
        available_tools: list[dict[str, Any]],
        allowed: set[str],
        my_exec: dict[str, Any],
        is_fg_user_turn: bool,
        deferred_incoming_spec: dict | None,
        goal_mode: bool = False,
        enqueued_stop_epoch: int = 0,
    ) -> dict[str, Any]:
        """Run one full multi-step conversation turn on the DSH Node Sidecar and commit final message."""
        b = self.brain
        cfg = b.store.config
        use_big = bot.get('engine') == 'big'
        if hasattr(cfg, 'resolve_route'):
            base_url, api_key, model_id, _channel_id = cfg.resolve_route(bot_model=bot.get('model', ''), big=use_big)
        else:
            api_key = cfg.effective_api_key(big=use_big) or cfg.effective_api_key(big=not use_big)
            base_url = cfg.effective_base_url()
            model_id = cfg.effective_model(bot_model=bot.get('model', ''), big=use_big) or cfg.effective_model(bot_model=bot.get('model', ''), big=not use_big)
        nb_key = f'{src}:{bot_id}'
        turn_started_perf = time.perf_counter()

        skey = self._session_key(bot_id, src)
        force_reset = skey in self._pending_reset_keys
        if force_reset:
            self._pending_reset_keys.discard(skey)
            self._warm_sessions.discard(skey)
        skip_seed = bool(not force_reset and self._warm_boot_id and skey in self._warm_sessions)

        sys_prompt = str((messages[0] if messages and messages[0].get('role') == 'system' else {}).get('content') or '')
        seed_history = []
        if not skip_seed:
            tc_names_by_id: dict[str, tuple[str, str]] = {}
            seed_chars_total = 0
            raw_seed_items: list[dict[str, str]] = []
            for hm in messages[1:-1]:
                h_role = str(hm.get('role') or '')
                h_content = hm.get('content')
                if isinstance(h_content, list):
                    txt_parts = [str(p.get('text') or '') for p in h_content if isinstance(p, dict) and p.get('type') == 'text']
                    txt_val = '\n'.join(p for p in txt_parts if p).strip()
                else:
                    txt_val = str(h_content or '').strip()

                if h_role == 'assistant' and isinstance(hm.get('tool_calls'), list) and hm['tool_calls']:
                    call_descs: list[str] = []
                    for tc in hm['tool_calls']:
                        if not isinstance(tc, dict):
                            continue
                        cid = str(tc.get('id') or '')
                        fn = tc.get('function') if isinstance(tc.get('function'), dict) else {}
                        tname = str(fn.get('name') or 'tool')
                        targs = str(fn.get('arguments') or '')[:400]
                        if cid:
                            tc_names_by_id[cid] = (tname, targs)
                        call_descs.append(f'{tname}({targs})')
                    note_prefix = f'{txt_val}\n' if txt_val else ''
                    raw_seed_items.append({'role': 'assistant', 'text': f"{note_prefix}[调用工具]: {'; '.join(call_descs)}"[:3000]})
                elif h_role == 'tool':
                    cid = str(hm.get('tool_call_id') or '')
                    tname, _ = tc_names_by_id.get(cid, ('tool', ''))
                    if txt_val:
                        raw_seed_items.append({'role': 'tool', 'text': f"[{tname} 返回]: {txt_val[:3200]}"})
                elif h_role in ('user', 'assistant') and txt_val:
                    # Skip compact_summary inside seed_history because it is now delivered
                    # unconditionally via the dedicated `compaction_checkpoint` section!
                    if h_role == 'assistant' and ('核心进展与上下文摘要' in txt_val or '交接检查点' in txt_val):
                        continue
                    raw_seed_items.append({'role': h_role, 'text': txt_val[:6000]})

            kept_seed_rev: list[dict[str, str]] = []
            for item in reversed(raw_seed_items[-64:]):
                item_len = len(item.get('text') or '') + 24
                if kept_seed_rev and seed_chars_total + item_len > 75_000:
                    break
                kept_seed_rev.append(item)
                seed_chars_total += item_len
            seed_history = list(reversed(kept_seed_rev))

        last_u = messages[-1] if messages and messages[-1].get('role') == 'user' else {'role': 'user', 'content': raw_user_text}
        u_content = last_u.get('content')
        eff_user_text = ''
        images: list[dict[str, str]] = []
        if isinstance(u_content, list):
            for part in u_content:
                if isinstance(part, dict) and part.get('type') == 'text':
                    eff_user_text += str(part.get('text') or '')
                elif isinstance(part, dict) and part.get('type') == 'image_url':
                    url = str((part.get('image_url') or {}).get('url') or '')
                    if url.startswith('data:') and ';base64,' in url:
                        header, b64_data = url.split(';base64,', 1)
                        decl_mime = header[5:] or 'image/jpeg'
                        try:
                            head_bytes = base64.b64decode(b64_data[:32] + '====', validate=False)
                            real_mime = detect_image_mime(head_bytes, fallback=decl_mime)
                        except Exception:
                            real_mime = decl_mime
                        images.append({'media_type': real_mime, 'data_b64': b64_data})
        else:
            eff_user_text = str(u_content or raw_user_text or '')

        cur_origin = b.turn_origin_context.get() if (hasattr(b, 'turn_origin_context') and b.turn_origin_context in contextvars.copy_context()) else None
        cur_hop = int(b.hop_depth.get() or 0) if hasattr(b, 'hop_depth') else 0
        self._get_or_init_turn_acc(eff_turn_id, allowed=allowed, turn_origin=cur_origin, hop_depth=cur_hop)
        my_exec.setdefault('trace', [])
        my_exec.setdefault('turn_steps', [])
        live_timeline: list[dict[str, Any]] = my_exec.setdefault('timeline', [])
        emitted_final_msg: dict[str, Any] | None = None
        stream_text = ''
        stream_thought = ''
        live_step_text = ''
        pending_step_note = ''
        running_calls: dict[str, tuple[str, dict[str, Any], str]] = {}
        api_calls = 0
        total_out_tokens = 0
        first_token_perf: float | None = None
        last_in_tokens = 0
        last_cached_tokens_step = 0
        enqueued_abort_epoch = int(getattr(b, '_abort_epoch_by_node_bot', {}).get(nb_key, 0) or 0)
        enqueued_bot_abort_epoch = int(getattr(b, '_abort_epoch_by_bot', {}).get(bot_id, 0) or 0)

        def _is_aborted() -> bool:
            return (
                (eff_turn_id in b._stopped_turn_ids)
                or (enqueued_stop_epoch != getattr(b, '_user_stop_epoch_by_node_bot', {}).get(nb_key, 0))
                or (enqueued_abort_epoch != getattr(b, '_abort_epoch_by_node_bot', {}).get(nb_key, 0))
                or (enqueued_bot_abort_epoch != getattr(b, '_abort_epoch_by_bot', {}).get(bot_id, 0))
            )

        mem_env = (getattr(b, '_last_turn_memory_envelope', None) or {}).get(nb_key) or {}
        payload = {
            'session_key': skey,
            'bot_id': bot_id,
            'source': src,
            'turn_id': eff_turn_id,
            'reset_session': force_reset,
            'system_prompt': sys_prompt,
            'compaction_checkpoint': str(mem_env.get('compaction_checkpoint') or ''),
            'ephemeral_recall': str(mem_env.get('ephemeral_recall') or ''),
            'user_text': eff_user_text,
            'images': images,
            'seed_history': seed_history,
            'allowed_tools': sorted(allowed),
            'tool_schemas': available_tools,
            'py_port': int(getattr(b, 'py_gateway_port', 0) or os.getenv('PORT', 18190)),
            'py_secret': getattr(b, 'dsh_internal_secret', ''),
            'llm': {
                'base_url': base_url,
                'api_key': api_key,
                'model': model_id,
                'max_tokens': 8192,
            },
        }

        terminal: dict[str, Any] | None = None
        last_pub = 0.0
        headers = {'Content-Type': 'application/json'}
        if self.secret:
            headers['X-DSH-Secret'] = self.secret

        try:
            stream_client = getattr(self, '_stream_client', None)
            if stream_client is None or getattr(stream_client, 'is_closed', False):
                stream_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(600.0, connect=5.0),
                    limits=httpx.Limits(max_keepalive_connections=8, max_connections=16, keepalive_expiry=120.0),
                    trust_env=False,
                )
                self._stream_client = stream_client
            for stream_attempt in range(2):
                try:
                    async with stream_client.stream('POST', f'{self.base_url}/turn/stream', headers=headers, json=payload) as resp:
                            if resp.status_code >= 400:
                                err_body = (await resp.aread()).decode('utf-8', 'replace')[:260]
                                raise RuntimeError(f'DSH Sidecar HTTP {resp.status_code}: {err_body}')
                            async for line in resp.aiter_lines():
                                if _is_aborted():
                                    await self.cancel_turn(bot_id, src)
                                    raise asyncio.CancelledError()
                                raw_ln = (line or '').strip()
                                if not raw_ln:
                                    continue
                                try:
                                    evt = json.loads(raw_ln)
                                except ValueError:
                                    continue
                                et = evt.get('type')
                                if et == 'step_start':
                                    api_calls += 1
                                    # Avoid duplicate stacked thinking blocks when step 1 was only a lightweight sticker/reply call
                                    if api_calls > 1 and stream_thought.strip():
                                        only_sticker_steps = bool(my_exec['trace']) and all(
                                            str(tr.get('tool') or '') in ('reply', 'send_sticker', 'sticker')
                                            and str((tr.get('arguments') or {}).get('op') or 'sticker').lower() in ('sticker', 'send_sticker', 'emoji', '')
                                            for tr in my_exec['trace']
                                        )
                                        if only_sticker_steps:
                                            stream_thought = ''
                                        elif not stream_thought.endswith('\n'):
                                            stream_thought += '\n'
                                elif et == 'thought_delta':
                                    th_d = str(evt.get('delta') or '')
                                    first_th = first_token_perf is None and bool(th_d)
                                    if first_th:
                                        first_token_perf = time.perf_counter()
                                    stream_thought += th_d
                                    now_m = time.monotonic()
                                    if first_th or (now_m - last_pub >= 0.025):
                                        last_pub = now_m
                                        await self._publish_live_state(
                                            bot_id=bot_id, bot_name=bot['name'], src=src, turn_id=eff_turn_id,
                                            text=stream_text, thought=stream_thought, tool=my_exec.get('tool'),
                                            step_text=live_step_text, my_exec=my_exec,
                                        )
                                elif et == 'text_delta':
                                    tx_d = str(evt.get('delta') or '')
                                    first_tx = (not stream_text) and bool(tx_d)
                                    if first_token_perf is None and tx_d:
                                        first_token_perf = time.perf_counter()
                                    stream_text += tx_d
                                    if tx_d:
                                        if live_timeline and live_timeline[-1].get('type') == 'text':
                                            live_timeline[-1]['text'] = str(live_timeline[-1].get('text') or '') + tx_d
                                        else:
                                            clean_chunk = tx_d.lstrip('\n')
                                            if clean_chunk:
                                                live_timeline.append({'type': 'text', 'text': clean_chunk})
                                    now_m = time.monotonic()
                                    if first_tx or (now_m - last_pub >= 0.025):
                                        last_pub = now_m
                                        await self._publish_live_state(
                                            bot_id=bot_id, bot_name=bot['name'], src=src, turn_id=eff_turn_id,
                                            text=stream_text, thought=stream_thought, tool=my_exec.get('tool'),
                                            step_text=live_step_text, my_exec=my_exec,
                                        )
                                elif et in ('step_segment', 'demote_stream_to_step', 'step_text'):
                                    # Unified DSH timeline: NEVER clear `stream_text`! Preserve all pre-tool/mid-tool prose.
                                    st_txt = str(evt.get('step_text') or evt.get('text') or '').strip()
                                    if st_txt:
                                        live_step_text = st_txt
                                        pending_step_note = st_txt
                                        my_exec['last_step_text'] = st_txt
                                        my_exec['turn_steps'].append({'type': 'step_text', 'text': st_txt})
                                    await self._publish_live_state(
                                        bot_id=bot_id, bot_name=bot['name'], src=src, turn_id=eff_turn_id,
                                        text=stream_text, thought=stream_thought, tool=my_exec.get('tool'),
                                        step_text=live_step_text, my_exec=my_exec,
                                    )
                                elif et == 'tool_start':
                                    tname = str(evt.get('tool') or '')
                                    call_id = str(evt.get('call_id') or f'call_dsh_{len(my_exec["trace"])}')
                                    raw_args = evt.get('arguments') if isinstance(evt.get('arguments'), dict) else {}
                                    step_note_here, pending_step_note = pending_step_note, ''
                                    running_calls[call_id] = (tname, raw_args, step_note_here)
                                    if tname == 'reply' and str(raw_args.get('op') or raw_args.get('action') or '').strip().lower() in ('sticker', 'send_sticker', 'emoji', 'choices', 'present_choices'):
                                        continue
                                    my_exec['tool'] = tname
                                    r_ent = compact_trace_list([{
                                        'tool': tname,
                                        'arguments': raw_args,
                                        'duration_ms': 0,
                                        'status': 'running',
                                        **({'step_note': step_note_here} if step_note_here else {}),
                                    }])[0]
                                    r_ent['call_id'] = call_id
                                    my_exec['trace'].append(r_ent)
                                    live_timeline.append({
                                        'type': 'tool',
                                        'call_id': call_id,
                                        'tool': tname,
                                        'arguments': raw_args,
                                        'status': 'running',
                                        'duration_ms': 0,
                                    })
                                    await self._publish_live_state(
                                        bot_id=bot_id, bot_name=bot['name'], src=src, turn_id=eff_turn_id,
                                        text=stream_text, thought=stream_thought, tool=tname,
                                        step_text=live_step_text, my_exec=my_exec,
                                    )
                                    b.stream_seq += 1
                                    await b.publish({'type': 'execution', 'source': src, 'bot_id': bot_id, 'turn_id': eff_turn_id, 'execution': my_exec, 'seq': b.stream_seq})
                                elif et == 'tool_end':
                                    tname = str(evt.get('tool') or '')
                                    call_id = str(evt.get('call_id') or '')
                                    dur = int(evt.get('duration_ms') or 1)
                                    matched_info = running_calls.pop(call_id, None) if call_id else None
                                    saved_args = matched_info[1] if matched_info else {}
                                    step_note_here = matched_info[2] if matched_info else ''
                                    res_payload = evt.get('result') or ({'ok': evt.get('ok')} if 'ok' in evt else {})
                                    st_flag = 'error' if evt.get('ok') is False else 'done'
                                    if tname == 'reply' and st_flag != 'error' and str(saved_args.get('op') or saved_args.get('action') or '').strip().lower() in ('sticker', 'send_sticker', 'emoji', 'choices', 'present_choices'):
                                        continue
                                    done_ent = compact_trace_list([{
                                        'tool': tname,
                                        'arguments': saved_args,
                                        'result': res_payload,
                                        'duration_ms': dur,
                                        'status': st_flag,
                                        **({'step_note': step_note_here} if step_note_here else {}),
                                    }])[0]
                                    replaced = False
                                    for idx in range(len(my_exec['trace']) - 1, -1, -1):
                                        tr_it = my_exec['trace'][idx]
                                        if tr_it.get('status') == 'running' and ((call_id and tr_it.get('call_id') == call_id) or tr_it.get('tool') == tname):
                                            if not saved_args and isinstance(tr_it.get('arguments'), dict):
                                                done_ent['arguments'] = tr_it['arguments']
                                            if not step_note_here and tr_it.get('step_note'):
                                                done_ent['step_note'] = tr_it['step_note']
                                            my_exec['trace'][idx] = done_ent
                                            replaced = True
                                            break
                                    if not replaced:
                                        my_exec['trace'].append(done_ent)
                                    for tl_it in reversed(live_timeline):
                                        if tl_it.get('type') == 'tool' and tl_it.get('status') == 'running' and (
                                            (call_id and tl_it.get('call_id') == call_id) or tl_it.get('tool') == tname
                                        ):
                                            tl_it['status'] = st_flag
                                            tl_it['duration_ms'] = dur
                                            tl_it['arguments'] = done_ent.get('arguments') or {}
                                            tl_it['result'] = done_ent.get('result') or {}
                                            break
                                    eff_cid = call_id or f'call_dsh_{len(my_exec["trace"])}'
                                    step_msgs = build_compact_turn_step(
                                        str(done_ent.get('step_note') or ''),
                                        [{'id': eff_cid, 'function': {'name': tname, 'arguments': done_ent.get('arguments') or {}}}],
                                        [(eff_cid, json.dumps(res_payload, ensure_ascii=False))],
                                    )
                                    append_turn_step(my_exec['turn_steps'], step_msgs)
                                    await self._publish_live_state(
                                        bot_id=bot_id, bot_name=bot['name'], src=src, turn_id=eff_turn_id,
                                        text=stream_text, thought=stream_thought, tool=f'已执行 {len(my_exec["trace"])} 步',
                                        step_text=live_step_text, my_exec=my_exec,
                                    )
                                elif et == 'usage' and isinstance(evt.get('usage'), dict):
                                    u = evt['usage']
                                    in_t = int(u.get('inputTokens') or 0) + int(u.get('cacheReadTokens') or 0)
                                    c_t = int(u.get('cacheReadTokens') or 0)
                                    out_t = int(u.get('outputTokens') or 0)
                                    if out_t > 0:
                                        total_out_tokens += out_t
                                    if in_t > 0:
                                        last_in_tokens = in_t
                                        last_cached_tokens_step = min(c_t, in_t)
                                        b.last_prompt_tokens[nb_key] = in_t
                                        b.last_cached_tokens[nb_key] = last_cached_tokens_step
                                        b.store.state.setdefault('last_prompt_tokens', {})[nb_key] = in_t
                                        b.store.state.setdefault('last_cached_tokens', {})[nb_key] = last_cached_tokens_step
                                        if src == 'phone':
                                            b.last_prompt_tokens[bot_id] = in_t
                                            b.last_cached_tokens[bot_id] = last_cached_tokens_step
                                            b.store.state.setdefault('last_prompt_tokens', {})[bot_id] = in_t
                                            b.store.state.setdefault('last_cached_tokens', {})[bot_id] = last_cached_tokens_step
                                elif et in ('done', 'aborted', 'error'):
                                    terminal = evt
                                    break
                    break
                except (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError) as stream_exc:
                    if stream_attempt == 0 and not my_exec['trace'] and not running_calls and not _is_aborted():
                        stream_text = ''
                        stream_thought = ''
                        live_step_text = ''
                        pending_step_note = ''
                        await asyncio.sleep(0.5)
                        await self.health_status(force=True)
                        continue
                    if stream_text.strip() or my_exec['trace']:
                        partial_body = stream_text.strip() or live_step_text.strip()
                        terminal = {
                            'type': 'done',
                            'text': f"{partial_body}\n\n（注：流传输中途意外断开：{str(stream_exc)[:160]}，以上为已执行与已输出进展）" if partial_body else '',
                            'error': str(stream_exc)[:240],
                        }
                        break
                    raise

            acc = self._turn_acc.pop(eff_turn_id, {})
            if terminal is None:
                terminal = {'type': 'error', 'error': 'DSH Sidecar closed stream without terminal event'}

            t_type = terminal.get('type')
            if t_type == 'aborted' or _is_aborted():
                partial = (
                    str(terminal.get('partial_text') or stream_text or '').strip()
                    or str(terminal.get('last_step_text') or live_step_text or '').strip()
                )
                if b.hop_depth.get() == 0:
                    TurnLedgerManager.finish_turn(b.store, src, bot_id, eff_turn_id, status='interrupted', reason='aborted', api_calls=api_calls, steps_count=len(my_exec['trace']))
                if partial and eff_turn_id not in b._stopped_turn_ids:
                    emitted_final_msg = await b.emit(
                        f'{partial}\n\n（已停止）',
                        'chat',
                        engine='dsh',
                        track=False,
                        trace=list(my_exec['trace']),
                        turn_steps=list(my_exec.get('turn_steps') or []),
                        thought=stream_thought.strip(),
                        from_bot_id=bot_id,
                        from_bot_name=bot['name'],
                    )
                    return emitted_final_msg
                return {'stopped': True}

            if t_type == 'error' and not str(terminal.get('text') or '').strip():
                raise RuntimeError(str(terminal.get('error') or 'DSH execution failed'))

            if acc.get('silent_requested'):
                silent_reason = str(acc.get('silent_reason') or 'quiet')
                if b.hop_depth.get() == 0:
                    duration_ms = max(1, int(round((time.perf_counter() - turn_started_perf) * 1000)))
                    TurnLedgerManager.finish_turn(
                        b.store, src, bot_id, eff_turn_id,
                        status='silent', reason=silent_reason,
                        api_calls=max(1, api_calls), steps_count=len(my_exec['trace']), duration_ms=duration_ms,
                    )
                return {'silent': True, 'reason': silent_reason}

            # Apply stickers, files & choices accumulated during DSH tool calls
            if acc.get('stickers'):
                b.pending_stickers_var.set(tuple(acc['stickers']))
            if acc.get('files') and hasattr(b, 'pending_files_var'):
                b.pending_files_var.set(tuple(acc['files']))
            if acc.get('choices'):
                b.choices_var.set(acc['choices'])
            if acc.get('choices_prompt'):
                b.choices_prompt_var.set(acc['choices_prompt'])

            final_txt = (
                str(terminal.get('text') or '').strip()
                or str(terminal.get('last_step_text') or live_step_text or '').strip()
                or '收到，已处理完毕。'
            )
            final_thought = str(terminal.get('thought') or stream_thought or '').strip()
            duration_ms = max(1, int(round((time.perf_counter() - turn_started_perf) * 1000)))
            ttft_ms = max(1, int(round(((first_token_perf - turn_started_perf) if first_token_perf else (duration_ms / 1000.0)) * 1000)))
            t_boot = str(terminal.get('boot_id') or '')
            if t_boot:
                if t_boot != self._warm_boot_id:
                    self._warm_boot_id = t_boot
                    self._warm_sessions = {skey}
                else:
                    self._warm_sessions.add(skey)
            eff_in_t = last_in_tokens or int(b.last_prompt_tokens.get(nb_key, 0) or 0)
            eff_c_t = min(last_cached_tokens_step or int(b.last_cached_tokens.get(nb_key, 0) or 0), eff_in_t)
            cache_hit_pct = round(eff_c_t * 100.0 / max(1, eff_in_t), 1) if eff_in_t > 0 else 0.0
            eff_out_t = total_out_tokens or max(1, b.memory_engine.estimate_tokens(final_txt))
            tps_val = round(eff_out_t / max(0.2, duration_ms / 1000.0), 1)
            turn_metrics_obj = {
                'ttft_ms': ttft_ms,
                'tps': tps_val,
                'prompt_tokens': eff_in_t,
                'cached_tokens': eff_c_t,
                'cache_hit_pct': cache_hit_pct,
                'completion_tokens': eff_out_t,
                'api_calls': max(1, api_calls),
                'total_ms': duration_ms,
                'engine': 'dsh',
            }
            b.store.state.setdefault('last_metrics', {})[nb_key] = turn_metrics_obj
            if src == 'phone':
                b.store.state.setdefault('last_metrics', {})[bot_id] = turn_metrics_obj
            if b.hop_depth.get() == 0:
                b.vault.log('助理：' + final_txt, bot_id=bot_id)
                TurnLedgerManager.finish_turn(
                    b.store, src, bot_id, eff_turn_id,
                    status='completed', reason='', api_calls=max(1, api_calls),
                    steps_count=len(my_exec['trace']), duration_ms=duration_ms,
                )
            b.vault.reindex()
            b.store.save()
            b.wake.set()

            extra: dict[str, Any] = {
                'api_calls': max(1, api_calls),
                'ttft_ms': ttft_ms,
                'tps': tps_val,
                'cache_hit_pct': cache_hit_pct,
                'metrics': turn_metrics_obj,
                'from_bot_id': bot_id,
                'from_bot_name': bot['name'],
                'from_node': src,
                'llm_text': final_txt,
            }
            if final_thought:
                extra['thought'] = final_thought
            if my_exec['trace']:
                extra['trace'] = list(my_exec['trace'])
            raw_tl = terminal.get('timeline')
            if isinstance(raw_tl, list) and raw_tl:
                raw_tools = sum(1 for x in raw_tl if isinstance(x, dict) and x.get('type') == 'tool')
                live_tools = sum(1 for x in live_timeline if isinstance(x, dict) and x.get('type') == 'tool')
                final_timeline = list(raw_tl) if raw_tools >= live_tools else list(live_timeline)
            else:
                final_timeline = list(live_timeline)
            if final_timeline:
                sanitized_tl = b._sanitize_timeline_entries(final_timeline, queue_media=False)
                if sanitized_tl:
                    extra['timeline'] = sanitized_tl
            if my_exec.get('turn_steps'):
                steps_list = list(my_exec['turn_steps'])
                extra['turn_steps'] = steps_list[-3:] if b.hop_depth.get() > 0 else steps_list
            emit_kind = 'chat'
            if b.hop_depth.get() > 0 and isinstance(deferred_incoming_spec, dict):
                emit_kind = 'bot_reply'
                extra.update({
                    'to_bot_id': deferred_incoming_spec.get('from_bot_id', ''),
                    'to_bot_name': deferred_incoming_spec.get('from_bot_name', ''),
                    'to_node': deferred_incoming_spec.get('from_node', ''),
                    'direction': 'outbound',
                })

            if is_fg_user_turn and hasattr(b, '_touch_cache_keepalive'):
                b._touch_cache_keepalive(src, bot_id)
            emitted_final_msg = await b.emit(final_txt, emit_kind, engine='dsh', track=False, **extra)
            return emitted_final_msg
        except asyncio.CancelledError:
            await self.cancel_turn(bot_id, src)
            return {'stopped': True}
        finally:
            self._turn_acc.pop(eff_turn_id, None)
