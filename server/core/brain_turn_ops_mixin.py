"""
[Module Responsibility]
Turn re-run, user-message in-place edit, and active-turn preemption helpers
extracted from `server/brain.py` (`server/core/brain_turn_ops_mixin.py`).
"""
from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from assistants import get_assistant
from storage import normalize_node_source
from vault_manager import now_iso


class BrainTurnOpsMixin:
    @staticmethod
    def _build_variant_snapshot(m: dict) -> dict:
        m_metrics = m.get('metrics') if isinstance(m.get('metrics'), dict) else {}
        return {
            'id': str(m.get('id') or ''),
            'text': str(m.get('text') or ''),
            'llm_text': str(m.get('llm_text') or m.get('text') or ''),
            'thought': str(m.get('thought') or ''),
            'trace': list(m.get('trace') or []),
            'timeline': list(m.get('timeline') or []),
            'turn_steps': list(m.get('turn_steps') or []),
            'sticker': str(m.get('sticker') or ''),
            'stickers': list(m.get('stickers') or []),
            'attachments': list(m.get('attachments') or []),
            'choices': list(m.get('choices') or []),
            'metrics': dict(m_metrics),
            'api_calls': int(m.get('api_calls') or m_metrics.get('api_calls') or 0),
            'ttft_ms': int(m.get('ttft_ms') or m_metrics.get('ttft_ms') or 0),
            'tps': float(m.get('tps') or m_metrics.get('tps') or 0.0),
            'cache_hit_pct': float(m.get('cache_hit_pct') or m_metrics.get('cache_hit_pct') or 0.0),
            'created_at': float(m.get('created_at') or 0.0),
            'time': str(m.get('time') or ''),
        }

    @classmethod
    def _extract_variants_from_msg(cls, m: dict | None) -> list[dict]:
        if not isinstance(m, dict):
            return []
        existing = m.get('variants')
        if isinstance(existing, list) and existing:
            out = [dict(v) for v in existing if isinstance(v, dict)]
            active_idx = int(m.get('active_variant_idx') if m.get('active_variant_idx') is not None else len(out) - 1)
            if 0 <= active_idx < len(out):
                out[active_idx] = cls._build_variant_snapshot(m)
            return out
        if str(m.get('text') or '').strip() or m.get('sticker') or m.get('attachments'):
            return [cls._build_variant_snapshot(m)]
        return []

    async def _spawn_turn_for_existing_user_message(
        self,
        user_msg: dict,
        bot_id: str,
        src: str,
        removed_ids: list[str],
        is_edit: bool = False,
        prev_variants: list[dict] | None = None,
    ) -> dict:
        """
        Increment `messages_epoch`, broadcast truncation/edit metadata, and launch an async turn
        for an already-existing `user_msg` in-place (NEVER appending a duplicate user message!).
        """
        src = normalize_node_source(src)
        nb_enq = f'{src}:{bot_id}'
        bot = get_assistant(self.store, bot_id)
        text = str(user_msg.get('text') or '').strip()
        turn_id = 'turn-' + secrets.token_hex(6)
        self.proactive_gate.reset_on_user_turn()
        user_msg['turn_id'] = turn_id
        user_msg['seq'] = int(user_msg.get('order_seq') or user_msg.get('seq') or 0)
        self.store.state['messages_epoch'] = int(self.store.state.get('messages_epoch') or 1) + 1
        self.store.sql.upsert_message(user_msg)
        self.store.save()
        if hasattr(self, 'dsh_adapter'):
            with contextlib.suppress(Exception):
                await self.dsh_adapter.reset_session(bot_id, src)
        self.stream_seq += 1
        await self.publish({
            'type': 'messages_truncated',
            'bot_id': bot_id,
            'source': src,
            'edited_message': user_msg if is_edit else None,
            'user_message': user_msg,
            'removed_message_ids': removed_ids,
            'messages_epoch': self.store.state['messages_epoch'],
            'seq': self.stream_seq,
        })
        visible = {msg['id'] for msg in self.store.state['messages'] if msg.get('bot_id', 'companion') == bot_id}
        await self.escalation.ack([mid for mid in self.store.state['pending'] if mid in visible])

        marker = self.bot_context.set(bot_id)
        room_marker = self.room_context.set(bot_id)
        src_marker = self.source_context.set(src)
        turn_token = self.turn_id_var.set(turn_id)
        prefix_token = self.stream_prefix_var.set('')
        try:
            self.activity = 'thinking'
            exec_obj = {'bot_id': bot_id, 'source': src, 'turn_id': turn_id, 'goal': text[:200], 'trace': [], 'tool': None, 'started_at': time.time()}
            self.executions_by_node_bot[f'{src}:{bot_id}'] = exec_obj
            self.executions_by_bot[bot_id] = exec_obj
            self.execution = exec_obj
            await self._set_stream(text='', tool='正在思考…', speaker_id=bot_id, speaker_name=bot['name'])
        finally:
            self.stream_prefix_var.reset(prefix_token)
            self.turn_id_var.reset(turn_token)
            self.source_context.reset(src_marker)
            self.room_context.reset(room_marker)
            self.bot_context.reset(marker)

        async def _bg():
            rv_tok = self.reroll_variants_var.set(prev_variants) if (prev_variants and hasattr(self, 'reroll_variants_var')) else None
            try:
                await self.chat(text, bot_id=bot_id, mode='chat', pre_recorded_user_msg=user_msg, turn_id=turn_id, source=src)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                err_msg = f'⚠️ [来自 {bot["name"]} ({bot_id}) 的报错]: {str(e)[:260]}'
                await self.emit(err_msg, 'error', track=False, bot_id=bot_id, source=src)
            finally:
                if rv_tok is not None and hasattr(self, 'reroll_variants_var'):
                    self.reroll_variants_var.reset(rv_tok)
                cur_ex = self.executions_by_node_bot.get(nb_enq)
                if isinstance(cur_ex, dict) and cur_ex.get('turn_id') == turn_id:
                    self.executions_by_node_bot.pop(nb_enq, None)
                    if (self.executions_by_bot.get(bot_id) or {}).get('turn_id') == turn_id:
                        self.executions_by_bot.pop(bot_id, None)
                        if (self.execution or {}).get('turn_id') == turn_id:
                            self.execution = None
                    self._clear_bot_stream(bot_id, source=src)
                    self.stream_seq += 1
                    with contextlib.suppress(Exception):
                        await self.publish({'type': 'stream', 'source': src, 'bot_id': bot_id, 'turn_id': turn_id, 'stream': None, 'seq': self.stream_seq})
                        await self.publish({'type': 'execution', 'source': src, 'bot_id': bot_id, 'turn_id': turn_id, 'execution': None, 'seq': self.stream_seq})
                    self.activity = 'thinking' if self.executions_by_bot else self.ready_state()

        bg_task = asyncio.create_task(_bg())
        self.queued_tasks_by_bot.setdefault(bot_id, set()).add(bg_task)
        self.queued_tasks_by_node_bot.setdefault(nb_enq, set()).add(bg_task)
        bg_task.add_done_callback(lambda t, bid=bot_id, nbk=nb_enq: (self.queued_tasks_by_bot.get(bid, set()).discard(t), self.queued_tasks_by_node_bot.get(nbk, set()).discard(t)))
        has_api = bool(
            (self.store.config.effective_api_key(big=False) or self.store.config.effective_api_key(big=True))
            and (self.store.config.effective_model(big=False) or self.store.config.effective_model(big=True))
        )
        if not has_api:
            await bg_task
        else:
            await asyncio.sleep(0)
        tail = [x for x in self.store.state['messages'] if normalize_node_source(x.get('source')) == src][-10:]
        return {
            'ok': True, 'queued': True, 'turn_id': turn_id, 'edited_message': user_msg,
            'user_message': user_msg, 'rerolled_user_message_id': user_msg.get('id'),
            'removed_message_ids': removed_ids, 'messages_epoch': self.store.state['messages_epoch'],
            'context_by_bot': self.memory_engine.get_all_bots_context_stats(source=src),
            'recent_tail': tail, 'seq': self.stream_seq,
        }

    async def _preempt_if_bot_busy(self, bot_id: str, reason: str, source: str | None = None):
        norm_src = normalize_node_source(source or self.source_context.get())
        nb_key = f'{norm_src}:{bot_id}'
        cur_exec_for_bot = self.executions_by_bot.get(bot_id)
        same_src_exec = bool(
            self.executions_by_node_bot.get(nb_key)
            or (cur_exec_for_bot and normalize_node_source(cur_exec_for_bot.get('source')) == norm_src)
        )
        is_bot_busy = (
            same_src_exec
            or self._get_bot_lock(bot_id, norm_src).locked()
        )
        if is_bot_busy:
            await self.stop_immediately(bot_id=bot_id, reason=reason, commit_partial=False, source=norm_src)
            for _ in range(60):
                if not self._get_bot_lock(bot_id, norm_src).locked():
                    break
                await asyncio.sleep(0.01)

    def _truncate_room_messages_after(self, msgs: list[dict], target_idx: int, bot_id: str, src: str) -> list[str]:
        norm_src = normalize_node_source(src)
        kept, removed_ids = [], []
        for idx, m in enumerate(msgs):
            if idx <= target_idx or m.get('bot_id', 'companion') != bot_id or normalize_node_source(m.get('source')) != norm_src:
                kept.append(m)
            else:
                self.context_search.index_message(m)
                if m.get('id'):
                    removed_ids.append(str(m['id']))
        self.store.state['messages'] = kept
        if hasattr(self, 'memory_engine') and hasattr(self.memory_engine, 'recalculate_tokens_after_rewind'):
            self.memory_engine.recalculate_tokens_after_rewind(bot_id=bot_id, source=norm_src)
        return removed_ids

    async def edit_and_rerun(self, message_id: str, new_text: str, bot_id: str = 'companion', source: str | None = None) -> dict:
        """
        Edit an existing user message in-place (preserving its `id` and list index!),
        truncate/archive only subsequent messages in the same `(bot_id, source)` room,
        increment `messages_epoch`, and re-run without creating a duplicate user message.
        """
        new_text = (new_text or '').strip()
        if not new_text:
            raise ValueError('新消息内容不能为空')
        msgs = self.store.state.get('messages', [])
        target_idx = -1
        for idx, m in enumerate(msgs):
            if m.get('id') == message_id and m.get('bot_id', 'companion') == bot_id:
                target_idx = idx
                break
        if target_idx < 0:
            for idx, m in enumerate(msgs):
                if m.get('id') == message_id or (message_id and m.get('client_msg_id') == message_id):
                    target_idx = idx
                    break
        if target_idx < 0 and not message_id and source:
            req_src = normalize_node_source(source)
            for idx in range(len(msgs) - 1, -1, -1):
                m = msgs[idx]
                if m.get('bot_id', 'companion') == bot_id and normalize_node_source(m.get('source')) == req_src and m.get('role') == 'user':
                    target_idx = idx
                    break
        if target_idx < 0:
            raise ValueError('未找到该条消息')
        target_msg = msgs[target_idx]
        bot_id = str(target_msg.get('bot_id') or bot_id or 'companion')
        src = normalize_node_source(source or target_msg.get('source'))

        await self._preempt_if_bot_busy(bot_id, 'user_edit', source=src)

        msgs = self.store.state.get('messages', [])
        target_idx = next((i for i, m in enumerate(msgs) if m.get('id') == target_msg.get('id')), -1)
        if target_idx < 0:
            raise ValueError('未找到该条消息')
        target_msg = msgs[target_idx]
        target_msg['text'] = new_text
        target_msg['edited_at'] = now_iso()
        target_msg.pop('llm_text', None)

        removed_ids = self._truncate_room_messages_after(msgs, target_idx, bot_id, src)
        self.vault.log(f'用户（编辑重发 {target_msg["id"]}）：' + new_text, bot_id=bot_id)
        return await self._spawn_turn_for_existing_user_message(
            target_msg,
            bot_id=bot_id,
            src=src,
            removed_ids=removed_ids,
            is_edit=True,
        )

    async def reroll_last(self, bot_id: str = 'companion', message_id: str | None = None, source: str | None = None) -> dict:
        """
        Re-generate the assistant reply for the target user message (or the user message preceding `message_id`)
        in-place, preserving the user message's `id` and list position without appending a duplicate!
        """
        msgs = self.store.state.get('messages', [])
        explicit_src = normalize_node_source(source) if source else None
        matched_idx = -1
        if message_id:
            for idx, m in enumerate(msgs):
                if m.get('id') == message_id or m.get('client_msg_id') == message_id:
                    matched_idx = idx
                    break
        if matched_idx >= 0:
            bot_id = str(msgs[matched_idx].get('bot_id') or bot_id or 'companion')
            src = explicit_src or normalize_node_source(msgs[matched_idx].get('source'))
        else:
            src = explicit_src or 'phone'

        await self._preempt_if_bot_busy(bot_id, 'user_reroll', source=src)

        msgs = self.store.state.get('messages', [])
        if message_id:
            matched_idx = next((i for i, m in enumerate(msgs) if m.get('id') == message_id or m.get('client_msg_id') == message_id), -1)
        room_indices = [
            i for i, m in enumerate(msgs)
            if m.get('bot_id', 'companion') == bot_id and normalize_node_source(m.get('source')) == src
        ]
        if not room_indices:
            room_indices = [i for i, m in enumerate(msgs) if m.get('bot_id', 'companion') == bot_id]
        if not room_indices:
            raise ValueError('当前房间暂无消息可重答')

        target_user_idx = -1
        if matched_idx >= 0:
            if msgs[matched_idx].get('role') == 'user':
                target_user_idx = matched_idx
            else:
                for i in reversed(room_indices):
                    if i < matched_idx and msgs[i].get('role') == 'user':
                        target_user_idx = i
                        break
        if target_user_idx < 0:
            for i in reversed(room_indices):
                if msgs[i].get('role') == 'user':
                    target_user_idx = i
                    break
        if target_user_idx < 0:
            raise ValueError('未找到上一条用户消息')

        u_msg = msgs[target_user_idx]
        src = normalize_node_source(u_msg.get('source') or src)
        # Preserve prior assistant response variants (`1 / 2 / 3 / 4`) before truncating
        prev_assistant_msg = None
        for idx_a in range(target_user_idx + 1, len(msgs)):
            cand = msgs[idx_a]
            if cand.get('bot_id', 'companion') != bot_id or normalize_node_source(cand.get('source')) != src:
                continue
            if cand.get('role') == 'user':
                break
            if cand.get('role') == 'assistant' and not cand.get('is_sub_turn') and cand.get('kind') not in ('compact_summary', 'bot_message', 'error'):
                prev_assistant_msg = cand
                break
        prev_variants = self._extract_variants_from_msg(prev_assistant_msg)
        removed_ids = self._truncate_room_messages_after(msgs, target_user_idx, bot_id, src)
        return await self._spawn_turn_for_existing_user_message(
            u_msg,
            bot_id=bot_id,
            src=src,
            removed_ids=removed_ids,
            is_edit=False,
            prev_variants=prev_variants,
        )

    async def switch_message_variant(
        self,
        message_id: str,
        variant_idx: int,
        bot_id: str | None = None,
        source: str | None = None,
    ) -> dict:
        """Switch an assistant message's active reroll variant (`0..N-1`) and persist/sync it cleanly."""
        msgs = self.store.state.get('messages', [])
        target_msg = next((m for m in msgs if m.get('id') == message_id), None)
        if not target_msg:
            raise ValueError('未找到该条消息')
        variants = target_msg.get('variants')
        if not isinstance(variants, list) or not variants:
            raise ValueError('该条消息暂无多版本可切换')
        idx = int(variant_idx)
        if idx < 0 or idx >= len(variants):
            raise ValueError(f'版本号超出范围 (0..{len(variants) - 1})')
        cur_active = int(target_msg.get('active_variant_idx') if target_msg.get('active_variant_idx') is not None else len(variants) - 1)
        if 0 <= cur_active < len(variants):
            variants[cur_active] = self._build_variant_snapshot(target_msg)
        sel = dict(variants[idx])
        target_msg['active_variant_idx'] = idx
        target_msg['text'] = str(sel.get('text') or '')
        target_msg['llm_text'] = str(sel.get('llm_text') or target_msg['text'])
        for fld, default_val in (
            ('thought', ''),
            ('trace', []),
            ('timeline', []),
            ('turn_steps', []),
            ('sticker', ''),
            ('stickers', []),
            ('attachments', []),
            ('choices', []),
            ('metrics', {}),
        ):
            val = sel.get(fld)
            if val:
                target_msg[fld] = list(val) if isinstance(val, list) else (dict(val) if isinstance(val, dict) else val)
            else:
                target_msg.pop(fld, None)
        for num_fld in ('api_calls', 'ttft_ms', 'tps', 'cache_hit_pct'):
            if sel.get(num_fld):
                target_msg[num_fld] = sel[num_fld]
            else:
                target_msg.pop(num_fld, None)
        if sel.get('time'):
            target_msg['time'] = str(sel['time'])
        m_bot = str(target_msg.get('bot_id') or bot_id or 'companion')
        m_src = normalize_node_source(source or target_msg.get('source'))
        self.store.state['messages_epoch'] = int(self.store.state.get('messages_epoch') or 1) + 1
        if hasattr(self, 'memory_engine') and hasattr(self.memory_engine, 'recalculate_tokens_after_rewind'):
            self.memory_engine.recalculate_tokens_after_rewind(bot_id=m_bot, source=m_src)
        self.store.sql.upsert_message(target_msg)
        self.store.save()
        if hasattr(self, 'dsh_adapter'):
            with contextlib.suppress(Exception):
                await self.dsh_adapter.reset_session(m_bot, m_src)
        self.stream_seq += 1
        cur_epoch = int(self.store.state.get('messages_epoch') or 1)
        await self.publish({
            'type': 'message_updated',
            'bot_id': m_bot,
            'source': m_src,
            'message': target_msg,
            'active_variant_idx': idx,
            'messages_epoch': cur_epoch,
            'seq': self.stream_seq,
        })
        return {
            'ok': True,
            'message': target_msg,
            'active_variant_idx': idx,
            'variants_count': len(variants),
            'messages_epoch': cur_epoch,
            'context_by_bot': self.memory_engine.get_all_bots_context_stats(source=m_src),
        }
