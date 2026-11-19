"""
[Module Responsibility]
Task continuity manager for foreground user turns (`server/core/task_continuity.py`).
Prevents background scheduled routines, curiosity beats, or peer steers from interrupting
and permanently abandoning in-progress foreground tasks:
1. Seamless multi-segment continuation when a single ReAct loop hits `max_loop_steps`.
2. Activity timestamp refresh (`last_user` / `last_foreground_activity_at`) during & after
   long-running foreground turns so curiosity/sleep-time consolidators never misfire right
   after completion.
3. Foreground chain & pause/auto-resume bookkeeping so non-user preemptions automatically
   resume the interrupted foreground goal once the preemptor finishes.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from storage import normalize_node_source
from vault_manager import now_iso

DEFAULT_MAX_FOREGROUND_SEGMENTS = 4
DEFAULT_MAX_AUTO_RESUMES = 2
PAUSE_TTL_SECONDS = 1800.0
SYSTEM_OR_PEER_NO_PREEMPT_REASONS = {'user_stop', 'user_preempt', 'user_edit', 'user_reroll'}
CLOUD_GUI_TOOLS = frozenset({'cloud', 'cloud_desktop', 'cua_desktop'})
CONTINUITY_GUI_TOOLS = frozenset({'phone', 'phone2', 'pc', 'pc2', 'cloud', 'cloud_desktop', 'cua_desktop'})


class TaskContinuityManager:
    """Stateless helper operating cleanly on `Brain` and `Store` continuity attributes."""

    @staticmethod
    def _nb_key(source: str | None, bot_id: str | None) -> str:
        return f"{normalize_node_source(source)}:{(bot_id or 'companion').strip()}"

    @staticmethod
    def _chain_map(brain: Any) -> dict[str, dict[str, Any]]:
        m = getattr(brain, '_foreground_chain_by_node_bot', None)
        if not isinstance(m, dict):
            m = {}
            setattr(brain, '_foreground_chain_by_node_bot', m)
        return m

    @staticmethod
    def _paused_map(brain: Any) -> dict[str, dict[str, Any]]:
        m = getattr(brain, '_paused_foreground_by_node_bot', None)
        if not isinstance(m, dict):
            m = {}
            setattr(brain, '_paused_foreground_by_node_bot', m)
        return m

    @staticmethod
    def touch_foreground_activity(store: Any, now_ts: float | None = None) -> float:
        ts = float(now_ts if now_ts is not None else time.time())
        state = getattr(store, 'state', None)
        if isinstance(state, dict):
            prev = float(state.get('last_user') or 0.0)
            if ts > prev:
                state['last_user'] = ts
            state['last_foreground_activity_at'] = ts
        return ts

    @staticmethod
    def get_max_segments(brain: Any, *, is_foreground: bool = True, goal_mode: bool = False) -> int:
        if not is_foreground:
            return 1
        override = int(getattr(brain, 'max_foreground_segments', 0) or 0)
        if override > 0:
            return max(1, override)
        return DEFAULT_MAX_FOREGROUND_SEGMENTS if goal_mode else 1

    @staticmethod
    def get_max_loop_steps(brain: Any, *, goal_mode: bool) -> int:
        override = int(getattr(brain, 'max_loop_steps_override', 0) or 0)
        if override > 0:
            return override
        return 240 if goal_mode else 200

    @classmethod
    def begin_foreground_chain(
        cls,
        brain: Any,
        bot_id: str,
        source: str,
        turn_id: str,
        goal: str,
        *,
        user_msg_seq: int = 0,
        resume_count: int = 0,
    ) -> None:
        key = cls._nb_key(source, bot_id)
        cls._chain_map(brain)[key] = {
            'bot_id': (bot_id or 'companion').strip(),
            'source': normalize_node_source(source),
            'turn_id': str(turn_id or ''),
            'goal': str(goal or '')[:300],
            'user_msg_seq': int(user_msg_seq or 0),
            'resume_count': int(resume_count or 0),
            'started_at': time.time(),
        }

    @classmethod
    def end_foreground_chain(cls, brain: Any, bot_id: str, source: str, turn_id: str = '') -> None:
        key = cls._nb_key(source, bot_id)
        chains = cls._chain_map(brain)
        cur = chains.get(key)
        if not isinstance(cur, dict):
            return
        if not turn_id or not cur.get('turn_id') or cur.get('turn_id') == turn_id:
            chains.pop(key, None)

    @classmethod
    def clear_foreground_state(cls, brain: Any, bot_id: str | None, source: str | None = None) -> None:
        chains = cls._chain_map(brain)
        paused = cls._paused_map(brain)
        if not bot_id:
            chains.clear()
            paused.clear()
            return
        b = (bot_id or 'companion').strip()
        if source:
            key = cls._nb_key(source, b)
            chains.pop(key, None)
            paused.pop(key, None)
            return
        for k in [k for k in chains if k.endswith(f':{b}')]:
            chains.pop(k, None)
        for k in [k for k in paused if k.endswith(f':{b}')]:
            paused.pop(k, None)

    @classmethod
    def has_pending_resume(cls, brain: Any, bot_id: str, source: str = 'phone') -> bool:
        key = cls._nb_key(source, bot_id)
        paused = cls._paused_map(brain)
        spec = paused.get(key)
        if not isinstance(spec, dict):
            return False
        if (time.time() - float(spec.get('paused_at') or 0.0)) > PAUSE_TTL_SECONDS:
            paused.pop(key, None)
            return False
        return True

    @classmethod
    def is_foreground_busy(cls, brain: Any, bot_id: str, source: str = 'phone') -> bool:
        key = cls._nb_key(source, bot_id)
        if brain.is_node_bot_busy(bot_id, source):
            return True
        if key in cls._chain_map(brain):
            return True
        return cls.has_pending_resume(brain, bot_id, source)

    @classmethod
    def is_any_foreground_busy(cls, brain: Any) -> bool:
        if brain.is_any_bot_busy():
            return True
        if cls._chain_map(brain):
            return True
        now = time.time()
        paused = cls._paused_map(brain)
        for k in list(paused.keys()):
            spec = paused.get(k)
            if not isinstance(spec, dict) or (now - float(spec.get('paused_at') or 0.0)) > PAUSE_TTL_SECONDS:
                paused.pop(k, None)
        return bool(paused)

    @staticmethod
    def wrap_urgent_interrupt_prompt(combined_int: str) -> str:
        return (
            f'<urgent_scheduled_interrupt>\n{combined_int}\n</urgent_scheduled_interrupt>\n'
            '【系统并行调度契约】：请按需用 1 步轻量工具（如 `notify_user` / `emergency_alarm`）快速处理上方到点提醒；'
            '**处理后严禁丢弃或终止用户当前正在进行的任务**，请立即无缝接着执行未完成的前台任务并推进到最终结果！'
        )

    @staticmethod
    def compact_messages_for_next_segment(
        messages: list[dict[str, Any]],
        goal: str,
        next_segment_num: int,
        total_steps: int,
        completed_steps: list[str],
    ) -> list[dict[str, Any]]:
        """Compact tool loop history cleanly at segment boundary without breaking tool_call/tool pairs or system prefix."""
        if not messages:
            return messages
        head: list[dict[str, Any]] = [messages[0]]
        start_body = 1
        if len(messages) > 1 and messages[1].get('role') == 'user':
            head.append(messages[1])
            start_body = 2

        tail_raw = messages[start_body:]
        keep_tail = 16
        if len(tail_raw) <= keep_tail:
            kept_tail = list(tail_raw)
            dropped_count = 0
        else:
            cand = len(tail_raw) - keep_tail
            while cand < len(tail_raw) and tail_raw[cand].get('role') not in ('user', 'assistant'):
                cand += 1
            if cand >= len(tail_raw):
                cand = len(tail_raw) - keep_tail
                while cand > 0 and tail_raw[cand].get('role') not in ('user', 'assistant'):
                    cand -= 1
            dropped_count = max(0, cand)
            kept_tail = list(tail_raw[cand:])

        recent_tools = ', '.join(str(x) for x in completed_steps[-8:] if x) or '已完成前序工具探索'
        directive = (
            f'<auto_continuation_segment index="{next_segment_num}">\n'
            f'【引擎无缝续跑第 {next_segment_num} 段】：前序已执行 {total_steps} 步工具操作'
            + (f'（折叠早先 {dropped_count} 条过程消息；最近工具：{recent_tools}）。\n' if dropped_count else f'（最近工具：{recent_tools}）。\n')
            + f'用户的原始目标：「{str(goal or "")[:200]}」尚未完成。\n'
            '请紧接上方最新工具结果继续推进；严禁重复已成功完成的步骤。'
            '若遇到连续失败、权限受限或需要用户扫码/输密码/确认付款，请立即诚实说明具体卡点并停止操作。\n'
            '</auto_continuation_segment>'
        )

        compacted = head + kept_tail
        if compacted and compacted[-1].get('role') == 'user':
            last_c = compacted[-1].get('content')
            if isinstance(last_c, str):
                compacted[-1] = {**compacted[-1], 'content': last_c + '\n\n' + directive}
            elif isinstance(last_c, list):
                compacted[-1] = {**compacted[-1], 'content': list(last_c) + [{'type': 'text', 'text': directive}]}
            else:
                compacted.append({'role': 'user', 'content': directive})
        else:
            compacted.append({'role': 'user', 'content': directive})
        return compacted

    @classmethod
    def snapshot_for_system_preempt(
        cls,
        brain: Any,
        bot_id: str,
        source: str,
        reason: str,
    ) -> dict[str, Any] | None:
        norm_reason = str(reason or '').strip().lower()
        if norm_reason in SYSTEM_OR_PEER_NO_PREEMPT_REASONS:
            return None
        norm_src = normalize_node_source(source)
        b_id = (bot_id or 'companion').strip()
        key = cls._nb_key(norm_src, b_id)

        chain = cls._chain_map(brain).get(key)
        cur_exec = brain.executions_by_node_bot.get(key)
        ledger = (getattr(brain.store, 'state', {}) or {}).get('turn_ledgers', {}).get(key)

        goal = ''
        resume_count = 0
        user_msg_seq = 0
        if isinstance(chain, dict):
            goal = str(chain.get('goal') or '').strip()
            resume_count = int(chain.get('resume_count') or 0)
            user_msg_seq = int(chain.get('user_msg_seq') or 0)
        elif isinstance(cur_exec, dict) and str(cur_exec.get('turn_id') or ''):
            # Only snapshot real foreground turns that ran inside `_chat`, never scheduled/curiosity turns.
            if getattr(brain, 'activity', '') in ('thinking',):
                goal = str(cur_exec.get('goal') or '').strip()
        if not goal and isinstance(ledger, dict) and ledger.get('status') == 'running':
            goal = str(ledger.get('goal') or '').strip()
        if not goal or resume_count >= DEFAULT_MAX_AUTO_RESUMES:
            return None

        stop_ep = int(getattr(brain, '_user_stop_epoch_by_node_bot', {}).get(key, 0) or 0)
        if not user_msg_seq:
            for m in reversed(getattr(brain.store, 'state', {}).get('messages') or []):
                if (
                    m.get('bot_id', 'companion') == b_id
                    and normalize_node_source(m.get('source')) == norm_src
                    and m.get('role') == 'user'
                    and m.get('kind') != 'bot_message'
                ):
                    user_msg_seq = int(m.get('order_seq') or m.get('seq') or 0)
                    break

        done_steps = list((ledger or {}).get('completed_steps') or [])
        pending_step = str((ledger or {}).get('pending_step') or '')
        spec = {
            'bot_id': b_id,
            'source': norm_src,
            'goal': goal,
            'completed_steps': done_steps[-8:],
            'pending_step': pending_step,
            'stop_epoch': stop_ep,
            'user_msg_seq': user_msg_seq,
            'resume_count': resume_count + 1,
            'reason': norm_reason or 'system_preempt',
            'paused_at': time.time(),
            'paused_iso': now_iso(),
        }
        cls._paused_map(brain)[key] = spec
        return spec

    @staticmethod
    def build_auto_resume_prompt(spec: dict[str, Any]) -> str:
        goal = str(spec.get('goal') or '').strip()
        done = ', '.join(str(x) for x in (spec.get('completed_steps') or [])[-6:] if x) or '已有前序步骤'
        pending = str(spec.get('pending_step') or '').strip()
        pend_line = f'\n- 中断前正准备执行：`{pending}`' if pending else ''
        return (
            f'<auto_resume_interrupted_task reason="{spec.get("reason", "preempt")}" attempt="{spec.get("resume_count", 1)}">\n'
            f'【系统自动恢复中断任务】：刚才在执行用户的前台任务时被系统/同事唤醒打断（原因：`{spec.get("reason", "preempt")}`）。\n'
            f'- 用户的原定目标：「{goal}」\n'
            f'- 中断前已完成步骤：{done}{pend_line}\n'
            '现在插曲已处理完毕，请你**立即自动接着继续完成用户的原定目标**，直接调用所需工具核实最新状态并推进到底，直到彻底交付成果后再回复用户！\n'
            '</auto_resume_interrupted_task>'
        )

    @classmethod
    def schedule_auto_resume_if_needed(
        cls,
        brain: Any,
        bot_id: str,
        source: str = 'phone',
    ) -> asyncio.Task | None:
        norm_src = normalize_node_source(source)
        b_id = (bot_id or 'companion').strip()
        key = cls._nb_key(norm_src, b_id)
        spec = cls._paused_map(brain).pop(key, None)
        if not isinstance(spec, dict):
            return None
        if (time.time() - float(spec.get('paused_at') or 0.0)) > PAUSE_TTL_SECONDS:
            return None
        cur_stop_ep = int(getattr(brain, '_user_stop_epoch_by_node_bot', {}).get(key, 0) or 0)
        if cur_stop_ep != int(spec.get('stop_epoch') or 0):
            return None
        if int(spec.get('resume_count') or 1) > DEFAULT_MAX_AUTO_RESUMES:
            return None

        last_u_seq = 0
        for m in reversed(getattr(brain.store, 'state', {}).get('messages') or []):
            if (
                m.get('bot_id', 'companion') == b_id
                and normalize_node_source(m.get('source')) == norm_src
                and m.get('role') == 'user'
                and m.get('kind') != 'bot_message'
            ):
                last_u_seq = int(m.get('order_seq') or m.get('seq') or 0)
                break
        if spec.get('user_msg_seq') and last_u_seq > int(spec['user_msg_seq']):
            return None

        async def _auto_resume_coro():
            for _ in range(50):
                if not brain.is_node_bot_busy(b_id, norm_src):
                    break
                await asyncio.sleep(0.02)
            now_stop_ep = int(getattr(brain, '_user_stop_epoch_by_node_bot', {}).get(key, 0) or 0)
            if now_stop_ep != int(spec.get('stop_epoch') or 0):
                return
            sentinel = {'id': '', 'role': 'user', 'text': str(spec.get('goal') or ''), '_synthetic_resume': True, 'source': norm_src}
            with contextlib.suppress(asyncio.CancelledError):
                await brain.chat(
                    str(spec.get('goal') or ''),
                    bot_id=b_id,
                    mode='auto',
                    pre_recorded_user_msg=sentinel,
                    source=norm_src,
                    _auto_resume_spec=spec,
                )

        task = asyncio.create_task(_auto_resume_coro())
        brain.queued_tasks_by_bot.setdefault(b_id, set()).add(task)
        brain.queued_tasks_by_node_bot.setdefault(key, set()).add(task)

        def _cleanup(t: asyncio.Task) -> None:
            brain.queued_tasks_by_bot.get(b_id, set()).discard(t)
            brain.queued_tasks_by_node_bot.get(key, set()).discard(t)

        task.add_done_callback(_cleanup)
        return task

    @classmethod
    def evaluate_batch_progress(
        cls,
        completed_tuples: list[tuple[Any, str, dict, Any, int, dict]],
        prev_error_streak: int,
    ) -> tuple[int, bool, bool]:
        """Track consecutive tool failures (including `cloud`) and whether real GUI/task progress occurred."""
        if not completed_tuples:
            return prev_error_streak, False, False
        all_failed = True
        had_real_progress = False
        sent_screenshot = False
        for _, c_name, c_args, res, _, _ in completed_tuples:
            op = str((c_args or {}).get('op') or (c_args or {}).get('action') or '').strip().lower()
            if c_name in CONTINUITY_GUI_TOOLS and op in ('send_screenshot', 'screenshot'):
                sent_screenshot = True
            if c_name in ('send_image', 'reply') and op in ('', 'send_image', 'image'):
                if isinstance(res, dict) and res.get('chat_image_url'):
                    sent_screenshot = True
            is_err = isinstance(res, dict) and (bool(res.get('error')) or res.get('ok') is False)
            if not is_err:
                all_failed = False
                if isinstance(res, dict) and c_name in CONTINUITY_GUI_TOOLS and res.get('ui_changed') is False and op not in ('status', 'inspect', 'screenshot', 'send_screenshot', 'zoom'):
                    continue
                had_real_progress = True
        new_error_streak = (prev_error_streak + 1) if all_failed else 0
        return new_error_streak, had_real_progress, sent_screenshot

    @staticmethod
    def compute_turn_metrics_and_ghost_reset(
        brain: Any,
        src: str,
        bot_id: str,
        step_metrics_list: list[dict],
        my_exec: dict[str, Any],
        reply_out: str,
        api_calls: int,
        turn_started_perf: float,
    ) -> dict[str, Any]:
        """Aggregate per-turn latency/token metrics and reset transient mid-loop ghost token peaks."""
        turn_total_ms = max(1, int(round((time.perf_counter() - turn_started_perf) * 1000)))
        last_m = step_metrics_list[-1] if step_metrics_list else {}
        first_m = step_metrics_list[0] if step_metrics_list else {}
        total_comp_tok = sum(int(x.get('completion_tokens') or 0) for x in step_metrics_list)
        avg_tps = round(sum(float(x.get('tps') or 0.0) for x in step_metrics_list) / max(1, len(step_metrics_list)), 1) if step_metrics_list else 0.0
        nb_key = f'{src}:{bot_id}'
        fb_pt = brain.last_prompt_tokens.get(nb_key, brain.last_prompt_tokens.get(bot_id, 0) if src == 'phone' else 0)
        fb_ct = brain.last_cached_tokens.get(nb_key, brain.last_cached_tokens.get(bot_id, 0) if src == 'phone' else 0)
        first_step_pt = int(first_m.get('prompt_tokens') or 0) if step_metrics_list else 0
        if api_calls > 1 and first_step_pt > 0:
            persisted_steps_cnt = min(len(my_exec.get('turn_steps') or []), 3 if brain.hop_depth.get() > 0 else 24)
            persisted_delta = brain.memory_engine.estimate_tokens(reply_out) + min(1800, persisted_steps_cnt * 220) + 200
            post_turn_base = first_step_pt + persisted_delta
            cur_rep_pt = int(brain.last_prompt_tokens.get(nb_key, 0) or 0)
            if cur_rep_pt > post_turn_base:
                brain.last_prompt_tokens[nb_key] = post_turn_base
                st_pt_map = brain.store.state.setdefault('last_prompt_tokens', {})
                st_pt_map[nb_key] = post_turn_base
                capped_ct = min(int(brain.last_cached_tokens.get(nb_key, 0) or 0), post_turn_base)
                brain.last_cached_tokens[nb_key] = capped_ct
                st_ct_map = brain.store.state.setdefault('last_cached_tokens', {})
                st_ct_map[nb_key] = capped_ct
                if src == 'phone':
                    brain.last_prompt_tokens[bot_id] = post_turn_base
                    st_pt_map[bot_id] = post_turn_base
                    brain.last_cached_tokens[bot_id] = capped_ct
                    st_ct_map[bot_id] = capped_ct
                fb_pt = post_turn_base
                fb_ct = capped_ct
        return {
            'ttft_ms': int(first_m.get('ttft_ms') or last_m.get('ttft_ms') or 0),
            'tps': float(last_m.get('tps') or avg_tps or 0.0),
            'prompt_tokens': int(last_m.get('prompt_tokens') or fb_pt or 0),
            'cached_tokens': int(last_m.get('cached_tokens') or fb_ct or 0),
            'cache_hit_pct': int(last_m.get('cache_hit_pct') or 0),
            'completion_tokens': total_comp_tok,
            'api_calls': api_calls,
            'total_ms': turn_total_ms,
        }

    @staticmethod
    def persist_turn_metadata(brain: Any, data: dict[str, Any]) -> None:
        """Persist extracted traits, sniffed opportunities, and deadlines for top-level turns."""
        for trait in data.get('traits', [])[:5]:
            if isinstance(trait, str) and trait.strip():
                ltm = getattr(getattr(brain, 'memory_engine', None), 'long_term_memory', None)
                if ltm is not None:
                    ltm.retain(l0_summary=trait.strip()[:180], l1_overview=trait.strip(), kind='fact', outcome='success', supersede_topic=False)
                else:
                    brain.vault.append('profile/user_traits.md', f'- {now_iso()}（用户陈述）{trait}')
        op = data.get('opportunity')
        if isinstance(op, dict) and op.get('title') and op.get('goal'):
            op_rec = {
                'title': str(op['title'])[:200],
                'goal': str(op['goal'])[:3000],
                'ready': op.get('ready') is True,
                'done': False,
                'created_at': time.time(),
            }
            brain.store.state['opportunities'].append(op_rec)
            status_note = '上下文已对齐，可试做一步' if op_rec['ready'] else '仍需追问'
            brain.vault.append('tasks/sniffed_opportunities.md', f'## {op_rec["title"]}\n\n{op_rec["goal"]}\n\n状态：{status_note}')
        deadline = data.get('deadline')
        if isinstance(deadline, dict):
            try:
                brain.add_deadline(deadline['title'], deadline['due_at'])
            except (ValueError, KeyError):
                pass

    @staticmethod
    def build_goal_mode_contract(goal_spec: str) -> str:
        """Return system contract hint injected when /goal persistent mode is active."""
        return (
            f'\n\n<goal_mode_contract>\n'
            f'【🎯 引擎级持久目标模式 (/goal) 正在运行】\n'
            f'- 终局目标：{goal_spec}\n'
            f'- 持久拉起与结束契约：\n'
            f'  1. 本模式由底层引擎持续驱动：未调用 `reply(op="complete_goal", ...)` 前，结束输出会按步数上限与卡死断路规则拉起继续推进。\n'
            f'  2. 请一步步发起 `tool_calls` 推进并验证；遇到连续报错或卡死时结合实时画面换方案，不可解时如实说明。\n'
            f'  3. 全部子目标完成并拿到核实结果后，立即调用 `reply(op="complete_goal", summary="...", verified_evidence="...")` 结束目标模式！\n'
            f'</goal_mode_contract>'
        )
