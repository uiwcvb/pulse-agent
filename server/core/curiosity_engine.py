"""Curiosity Engine v5: Priority-waterfall proactive intelligence with zero toxic fallbacks."""
from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
from datetime import datetime
from typing import Any

from assistants import get_assistant
from core.task_continuity import TaskContinuityManager
from core.tool_schemas import is_tool_allowed, normalize_bot_tools
from core.xml_tool_parser import is_silent_proactive_output
from harness import TOOLS
from storage import normalize_node_source
from vault_manager import TZ, now_iso


CURIOSITY_SAFE_TOOLS: tuple[str, ...] = (
    'read_file',
    'write_file',
    'edit_file',
    'search_files',
    'web',
    'memory',
    'blackboard',
    'schedule_task',
    'reply',
)

_STRESS_HINTS = ('好累', '烦死', '崩溃', '别烦', '等会儿', '忙死', '赶deadline', '通宵', '头疼', '焦虑', '吵架')


def _day_start_ts(now_ts: float) -> float:
    dt = datetime.fromtimestamp(now_ts, TZ)
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


def _has_real_notifications(notif_digest_md: str) -> bool:
    txt = str(notif_digest_md or '').strip()
    if not txt:
        return False
    empty_markers = ('暂无新', '暂无重要通知', '暂无积攒', '（无）', '暂无未读')
    return not any(m in txt for m in empty_markers) and len(txt) > 28


from core.curiosity_coalesce_helpers import (
    _ALARM_BLOCK_HINTS,
    _NOTIF_INSPECTION_HINTS,
    curiosity_will_coalesce_soon,
    detect_periodic_coalesce_opportunities,
    is_coalesceable_blackboard_for_curiosity,
    is_coalesceable_routine_for_curiosity,
)


class CuriosityEngine:
    """Unified proactive curiosity engine replacing canned prompts with intent-driven decisions."""

    MAX_CURIOSITY_QUESTIONS_PER_DAY = 12

    def __init__(self, brain: Any):
        self.brain = brain
        self.store = brain.store

    def _is_user_stressed_or_busy(self, now_ts: float, recent_msgs: list[dict]) -> bool:
        last_u = float(self.store.state.get('last_user') or 0.0)
        recent_user_texts = [
            str(m.get('text') or '')
            for m in recent_msgs[-6:]
            if isinstance(m, dict) and m.get('role') == 'user'
        ]
        if any(h in txt for txt in recent_user_texts[-2:] for h in _STRESS_HINTS):
            return True
        if last_u > 0 and (now_ts - last_u) < 600.0 and len(recent_user_texts) >= 4:
            return True
        return False

    def _collect_coalesced_periodic_work(
        self,
        *,
        now_ts: float,
        target_id: str,
        merge_window_sec: float = 120.0,
    ) -> list[dict[str, Any]]:
        """Gather due/nearly-due periodic inspection tasks so they execute in one unified curiosity cycle."""
        items: list[dict[str, Any]] = []
        seen_sigs: set[str] = set()
        for r in (self.store.state.get('routines') or []):
            if not is_coalesceable_routine_for_curiosity(r):
                continue
            r_bot = str(r.get('bot_id') or 'companion')
            if r_bot != target_id:
                continue
            nxt = float(r.get('next_run') or 0.0)
            if nxt > 0 and nxt > (now_ts + merge_window_sec):
                continue
            goal = str(r.get('goal') or '').strip()
            sig = 'notif_check' if any(h in goal for h in _NOTIF_INSPECTION_HINTS) else goal[:64]
            items.append({
                'pool': 'routine',
                'id': str(r.get('id') or ''),
                'goal': goal,
                'interval_hours': float(r.get('interval_hours') or 0.5),
                'duplicate_of_merged': sig in seen_sigs,
                'ref': r,
            })
            seen_sigs.add(sig)

        for p in (self.store.state.get('blackboard_posts') or []):
            if not is_coalesceable_blackboard_for_curiosity(p):
                continue
            p_target = str(p.get('target_bot_id') or p.get('author_bot_id') or target_id)
            if p_target != target_id and '@everyone' not in (p.get('mentions') or []):
                continue
            due_at = float(p.get('due_at') or p.get('trigger_at') or 0.0)
            if due_at <= 0 or due_at > (now_ts + merge_window_sec):
                continue
            text = str(p.get('text') or p.get('content') or '').strip()
            sig = 'notif_check' if any(h in text for h in _NOTIF_INSPECTION_HINTS) else text[:64]
            items.append({
                'pool': 'blackboard',
                'id': str(p.get('post_id') or p.get('id') or ''),
                'goal': text,
                'duplicate_of_merged': sig in seen_sigs,
                'ref': p,
            })
            seen_sigs.add(sig)
            if len(items) >= 6:
                break
        return items

    def _build_periodic_awareness_summary(self, *, target_id: str, coalesced: list[dict[str, Any]]) -> str:
        """Let the AI itself perceive active periodic rhythms and duplicate inspection items so it can self-merge."""
        cur_min = max(1, int(getattr(self.store.config, 'curiosity_interval_minutes', 30) or 30))
        short_or_dup: list[str] = []
        notif_ids: list[str] = []
        for r in (self.store.state.get('routines') or []):
            if not isinstance(r, dict) or not r.get('enabled', True) or r.get('bot_id', 'companion') != target_id:
                continue
            rid = str(r.get('id') or '')
            goal = str(r.get('goal') or '').replace('\n', ' ')[:50]
            if any(h in goal for h in _NOTIF_INSPECTION_HINTS):
                notif_ids.append(f"routine:{rid}")
            if is_coalesceable_routine_for_curiosity(r) or float(r.get('interval_hours') or 24) <= 2.0:
                tag = '已并入好奇心' if r.get('merge_with_curiosity') or is_coalesceable_routine_for_curiosity(r) else '独立周期'
                short_or_dup.append(f"- routine `{rid}` ({r.get('interval_hours', 24)}h · {tag}): {goal}")
        for p in (self.store.state.get('blackboard_posts') or []):
            if not isinstance(p, dict) or p.get('status') != 'scheduled':
                continue
            pid = str(p.get('post_id') or '')
            ptxt = str(p.get('text') or p.get('content') or '').replace('\n', ' ')[:50]
            if any(h in ptxt for h in _NOTIF_INSPECTION_HINTS):
                notif_ids.append(f"blackboard:{pid}")
            if is_coalesceable_blackboard_for_curiosity(p):
                tag = '已并入好奇心' if p.get('merge_with_curiosity') else '可自整理'
                short_or_dup.append(f"- blackboard `{pid}` ({tag}): {ptxt}")
        if not short_or_dup and not coalesced:
            return ''
        lines = [f'【周期任务自省视图（当前好奇心节拍：每 {cur_min} 分钟）】']
        lines.extend(short_or_dup[:8])
        if len(notif_ids) >= 2:
            lines.append(
                f'⚠️ 注意：当前场上同时存在 {len(notif_ids)} 个通知栏检查周期项（{", ".join(notif_ids[:6])}）。'
                '如果你察觉它们属于同类重复巡检，或可完全交由好奇心顺带检查并汇报，请主动调用 '
                '`blackboard(action="cancel", post_id="...")` 或 `blackboard(action="update_routine", task_id="...", merge_with_curiosity=True)` 合并去重，下次在好奇心回复里一起说即可。'
            )
        return '\n'.join(lines)

    def _advance_coalesced_periodic_work(
        self,
        coalesced: list[dict[str, Any]],
        *,
        outcome: str,
        now_ts: float,
    ) -> None:
        """Advance next_run/due_at and record run ledger for all periodic items handled by this curiosity cycle."""
        if not coalesced:
            return
        from core.blackboard_scheduler_helpers import parse_due_timestamp
        from core.turn_ledger import TurnLedgerManager
        dirty = False
        cur_iv_hours = max(0.05, float(getattr(self.store.config, 'curiosity_interval_minutes', 30) or 30) / 60.0)
        for entry in coalesced:
            pool = entry.get('pool')
            ref = entry.get('ref')
            if not isinstance(ref, dict):
                continue
            if pool == 'routine':
                dirty = True
                ref['last_run'] = now_ts
                ref['consecutive_skips'] = 0
                ref.pop('deferred_reason', None)
                ref['last_status'] = 'coalesced_silent' if outcome == 'silent' else 'coalesced_ok'
                ref['last_error'] = ''
                nxt = self.brain._compute_next_routine_run(ref, now_ts + 2.0)
                ref['next_run'] = nxt
                ref['next_run_bj'] = self.brain._fmt_bj(nxt)
                TurnLedgerManager.append_run_log(self.store, {
                    'kind': 'routine_coalesced',
                    'task_id': str(ref.get('id') or ''),
                    'bot_id': str(ref.get('bot_id') or 'companion'),
                    'goal': str(ref.get('goal') or '')[:120],
                    'status': ref['last_status'],
                    'next_run_bj': ref['next_run_bj'],
                    'ts': now_iso(),
                })
            elif pool == 'blackboard':
                dirty = True
                ref['last_fired_at'] = now_ts
                sched = ref.get('schedule') if isinstance(ref.get('schedule'), dict) else {}
                if sched.get('cron_expr') or sched.get('daily_time') or sched.get('interval_hours') or sched.get('interval_minutes'):
                    next_due, _ = parse_due_timestamp(sched, now_ts + 2.0)
                else:
                    # Periodic notification inspection posts without structured schedule follow curiosity's cycle
                    next_due = now_ts + max(1800.0, cur_iv_hours * 3600.0)
                if next_due > 0:
                    ref['due_at'] = round(next_due, 3)
                    ref['due_iso'] = datetime.fromtimestamp(next_due, TZ).isoformat(timespec='seconds')
        if dirty:
            self.store.save()

    def _pick_question_topic(
        self,
        *,
        now_ts: float,
        manual: bool,
        recent_msgs: list[dict],
    ) -> dict[str, Any] | None:
        sql = getattr(self.store, 'sql', None)
        if sql is None or not hasattr(sql, 'pick_candidate_curiosity_topics'):
            return None
        if not manual and self._is_user_stressed_or_busy(now_ts, recent_msgs):
            return None
        asked_today = 0
        if hasattr(sql, 'count_today_curiosity_questions'):
            asked_today = sql.count_today_curiosity_questions(day_start_ts=_day_start_ts(now_ts))
        if not manual and asked_today >= self.MAX_CURIOSITY_QUESTIONS_PER_DAY:
            return None
        cands = sql.pick_candidate_curiosity_topics(now_ts=now_ts, limit=3, max_depth=3)
        if cands:
            return cands[0]
        if hasattr(sql, 'list_curiosity_topics'):
            fallback_open = [
                t for t in sql.list_curiosity_topics(status='open')
                if isinstance(t, dict) and int(t.get('depth') or 0) < 3
            ]
            if fallback_open:
                fallback_open.sort(
                    key=lambda t: (
                        int(t.get('ask_count') or 0),
                        float(t.get('last_asked_at') or 0.0),
                        int(t.get('depth') or 0),
                    )
                )
                return fallback_open[0]
        return None

    def _select_intent(
        self,
        *,
        now_ts: float,
        manual: bool,
        target_id: str,
        raw_imp: str,
        notif_digest_md: str,
        recent_msgs: list[dict],
        coalesced: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Priority waterfall: P1 followup -> P2 prep/assist/periodic -> P3 curiosity -> P4 care -> P5 noop.
        Whenever open curiosity topics exist (or manual=True), attach `topic` + `force_ask_curiosity=True`
        so P1/P2/periodic checks never starve curiosity questioning.
        """
        coalesced_list = list(coalesced or [])
        sql = getattr(self.store, 'sql', None)
        if sql is not None and hasattr(sql, 'seed_topics_from_impression_markdown') and raw_imp:
            with contextlib.suppress(Exception):
                sql.seed_topics_from_impression_markdown(raw_imp, now_ts=now_ts)

        topic_cand = self._pick_question_topic(now_ts=now_ts, manual=manual, recent_msgs=recent_msgs)
        force_ask = bool(topic_cand or manual)

        # P1: Due follow-up promises (still piggybacking any coalesced periodic checks + mandatory curiosity question!)
        if sql is not None and hasattr(sql, 'list_due_followups'):
            due_fus = sql.list_due_followups(now_ts=now_ts, limit=4)
            preferred = next((f for f in due_fus if f.get('bot_id') == target_id), None) or (due_fus[0] if due_fus else None)
            if preferred and int(preferred.get('attempts') or 0) < 3:
                return {
                    'intent': 'followup_check_in',
                    'followup': preferred,
                    'coalesced': coalesced_list,
                    'has_notifs': _has_real_notifications(notif_digest_md),
                    'topic': topic_cand,
                    'topic_id': str((topic_cand or {}).get('topic_id') or ''),
                    'force_ask_curiosity': force_ask,
                    'reason': f"存在待跟进承诺：{preferred.get('topic')}",
                }

        # P2: Prep & assist (imminent deadlines <= 4h, ready opportunities, unread notifs, or coalesced periodic tasks)
        urgent_deadlines = [
            d for d in (self.store.state.get('deadlines') or [])
            if isinstance(d, dict) and not d.get('notified')
            and 0 < (float(d.get('due_at') or 0) - now_ts) <= 4 * 3600.0
        ]
        ready_opps = [
            o for o in (self.store.state.get('opportunities') or [])
            if isinstance(o, dict) and o.get('ready') and not o.get('done')
        ]
        has_notifs = _has_real_notifications(notif_digest_md)
        if urgent_deadlines or ready_opps or has_notifs or coalesced_list:
            reason_parts = []
            if urgent_deadlines or ready_opps:
                reason_parts.append('临近截止/待办机会')
            if has_notifs:
                reason_parts.append('未读重要通知')
            if coalesced_list:
                reason_parts.append(f'合并 {len(coalesced_list)} 项周期巡检')
            return {
                'intent': 'prep_and_assist',
                'deadlines': urgent_deadlines[:3],
                'opportunities': ready_opps[:2],
                'has_notifs': has_notifs,
                'coalesced': coalesced_list,
                'topic': topic_cand,
                'topic_id': str((topic_cand or {}).get('topic_id') or ''),
                'force_ask_curiosity': force_ask,
                'reason': '命中' + ' / '.join(reason_parts or ['协助窗口']),
            }

        # P4: Genuine curiosity (auto-seeded slots, depth 0..3)
        if topic_cand:
            return {
                'intent': 'genuine_curiosity',
                'topic': topic_cand,
                'coalesced': coalesced_list,
                'topic_id': str(topic_cand.get('topic_id') or ''),
                'force_ask_curiosity': True,
                'reason': f"待深化了解维度：{topic_cand.get('label')} (depth={topic_cand.get('depth', 0)})",
            }

        # P5: Care nudge (explicit manual trigger or genuinely long silence >= 4h while awake)
        last_u = float(self.store.state.get('last_user') or 0.0)
        last_cur = float(self.store.state.get('last_curiosity') or 0.0)
        if manual or (last_u > 0 and (now_ts - last_u) >= 4 * 3600.0 and (now_ts - last_cur) >= 4 * 3600.0):
            return {
                'intent': 'care_nudge',
                'coalesced': coalesced_list,
                'topic_id': '',
                'force_ask_curiosity': True,
                'reason': '手动触发或长时间静默后的强制关心提问窗口' if manual else '长时间未互动的主动关怀提问窗口',
            }

        # P6: Silent no-op (0 LLM tokens!)
        return {
            'intent': 'silent_noop',
            'coalesced': [],
            'topic_id': '',
            'force_ask_curiosity': False,
            'reason': '当前无到期跟进、无需协助且无待了解话题，零开销保持沉默',
        }

    def _format_intent_directive(self, plan: dict[str, Any], recent_episodes: list[dict]) -> str:
        intent = plan.get('intent', 'care_nudge')
        force_ask = bool(plan.get('force_ask_curiosity'))
        lines = [f"【本轮自主意图判定：{intent}】", f"判定依据：{plan.get('reason', '')}"]
        if intent == 'followup_check_in' and plan.get('followup'):
            fu = plan['followup']
            lines.append(f"- 待跟进主题：{fu.get('topic')}")
            if fu.get('context'):
                lines.append(f"- 背景上下文：{fu.get('context')}")
            if plan.get('coalesced') or plan.get('has_notifs') or force_ask:
                lines.append('- 重要提醒：若上述旧跟进已过时或无需复读，千万不要因此静默退出，直接略过旧事并完成下方的强制好奇心提问！')
        if plan.get('topic'):
            tp = plan['topic']
            lines.append('【本轮强制好奇心提问（必问 · 禁止静默跳过）】')
            lines.append(
                f"- 目标印象维度：[{tp.get('category')}] {tp.get('label')} (depth={tp.get('depth', 0)}/3)"
            )
            if tp.get('known_summary'):
                lines.append(f"- 已知事实：{tp['known_summary']}")
            if tp.get('open_question'):
                lines.append(f"- 建议追问方向：{tp['open_question']}")
            lines.append('- 强制要求：先简要交待上方有价值的动态（若无可报则直接省略巡检废话），然后在同一条回复里用熟稔自然的口吻向主人提出这个明确具体的问题（**必须带有问号 ？**），绝不允许调用 `reply(op="silent")` 或输出 `SILENT_OK`；可顺手调用 `reply(op="choices", choices=[...])` 提供 2~4 个接地气选项。')
        elif force_ask:
            lines.append('【本轮强制好奇心提问（必问 · 禁止静默跳过）】')
            lines.append('- 强制要求：请结合个人印象缺失维度或主人近况，向主人提出一个真诚、具体、不查户口的好奇小问题（**必须带有明确问号 ？**），禁止调用 `reply(op="silent")` 或输出 `SILENT_OK`；可顺带提供 `reply(op="choices", choices=[...])`。')
        coalesced = [c for c in (plan.get('coalesced') or []) if not c.get('duplicate_of_merged')]
        if coalesced:
            tail_rule = (
                '（若巡检全无异常可直接忽略巡检结果，**紧接着必须完成上方的好奇心提问，禁止静默**）'
                if force_ask
                else '（与主动搭话同轮一次完成，有重要发现就合并在一条回复里说明；确实全无可报再调 `reply(op="silent")`）'
            )
            lines.append(f'【本轮已并入好奇心的周期巡检任务{tail_rule}】')
            for idx, item in enumerate(coalesced, 1):
                pool_lbl = '周期例行' if item.get('pool') == 'routine' else '周期黑板'
                lines.append(f"{idx}. [{pool_lbl} #{item.get('id')}] {str(item.get('goal') or '')[:500]}")
        if recent_episodes:
            lines.append('【近期主动触达去重台账（严禁重复相同主题与开场白）】')
            for ep in recent_episodes[:6]:
                lines.append(
                    f"- [{ep.get('intent')}/{ep.get('outcome')}] replied={ep.get('user_replied')} :: {str(ep.get('message_text') or '')[:80]}"
                )
        return '\n'.join(lines)

    def _post_send_bookkeeping(
        self,
        *,
        plan: dict[str, Any],
        target_id: str,
        text: str,
        trace: list[dict],
        now_ts: float,
    ) -> None:
        intent = str(plan.get('intent') or 'care_nudge')
        sql = getattr(self.store, 'sql', None)
        if sql is not None and hasattr(sql, 'record_curiosity_episode'):
            with contextlib.suppress(Exception):
                sql.record_curiosity_episode(
                    intent=intent,
                    topic_id=str(plan.get('topic_id') or ''),
                    bot_id=target_id,
                    message_text=text,
                    tool_calls_count=len(trace),
                    outcome='sent',
                    created_at=now_ts,
                )
        asked_question = ('？' in text) or ('?' in text)
        if plan.get('topic') and sql is not None and (intent == 'genuine_curiosity' or asked_question):
            tp = plan['topic']
            new_depth = min(3, int(tp.get('depth') or 0) + 1)
            new_status = 'saturated' if new_depth >= 3 or int(tp.get('ask_count') or 0) >= 2 else 'open'
            with contextlib.suppress(Exception):
                sql.upsert_curiosity_topic(
                    tp['topic_id'],
                    category=str(tp.get('category') or 'general'),
                    label=str(tp.get('label') or ''),
                    depth=new_depth,
                    status=new_status,
                    increment_ask=True,
                    cooldown_until=now_ts + 12 * 3600.0,
                    now_ts=now_ts,
                )
        if intent == 'followup_check_in' and plan.get('followup') and sql is not None:
            fu = plan['followup']
            next_attempts = int(fu.get('attempts') or 0) + 1
            new_st = 'expired' if next_attempts >= 3 else 'open'
            with contextlib.suppress(Exception):
                sql.resolve_followup(
                    str(fu['followup_id']),
                    status=new_st,
                    increment_attempts=True,
                    next_due_at=now_ts + 6 * 3600.0,
                    now_ts=now_ts,
                )

    def _finalize_silent(
        self,
        *,
        plan: dict[str, Any],
        target_id: str,
        reason: str,
        outcome: str,
        coalesced: list[dict[str, Any]],
        trace: list[dict],
        now_ts: float,
        snooze_min: int | None = None,
        mark_notifs_seen: bool = True,
    ) -> dict[str, Any]:
        if mark_notifs_seen:
            self.brain.notification_hub.mark_curiosity_digest_seen()
        self._advance_coalesced_periodic_work(coalesced, outcome='silent', now_ts=now_ts)
        self._record_silent_episode(plan, target_id, reason[:200], outcome, trace, now_ts)
        self.brain._finish_curiosity_cycle('silent', target_id, reason[:200], snooze_min=snooze_min)
        return {
            'status': 'silent',
            'ok': True,
            'silent': True,
            'bot_id': target_id,
            'reason': reason,
            'coalesced_count': len(coalesced),
        }

    def _record_silent_episode(self, plan: dict[str, Any], target_id: str, reason: str, outcome: str, trace: list[dict], now_ts: float) -> None:
        sql = getattr(self.store, 'sql', None)
        if sql is not None and hasattr(sql, 'record_curiosity_episode'):
            with contextlib.suppress(Exception):
                sql.record_curiosity_episode(
                    intent=str(plan.get('intent') or 'silent_noop'),
                    topic_id=str(plan.get('topic_id') or ''),
                    bot_id=target_id,
                    message_text=reason[:200],
                    tool_calls_count=len(trace),
                    outcome=outcome,
                    created_at=now_ts,
                )
        # Prevent stale follow-up promises from trapping curiosity in infinite silent loops every 30 min
        if outcome == 'silent' and plan.get('intent') == 'followup_check_in' and plan.get('followup') and sql is not None:
            fu = plan['followup']
            next_attempts = int(fu.get('attempts') or 0) + 1
            new_st = 'expired' if next_attempts >= 3 else 'open'
            with contextlib.suppress(Exception):
                sql.resolve_followup(
                    str(fu['followup_id']),
                    status=new_st,
                    increment_attempts=True,
                    next_due_at=now_ts + 6 * 3600.0,
                    now_ts=now_ts,
                )

    async def _execute_tool_batch(
        self,
        *,
        batch: list[dict],
        allowed: Any,
        target_id: str,
        trace: list[dict],
        messages: list[dict],
        offload_output: bool = False,
        running_entries: list[tuple] | None = None,
    ) -> tuple[dict | None, dict | None]:
        b = self.brain

        async def _run_item(item):
            if running_entries is not None:
                c_item, c_name, c_args, r_ent = item
            else:
                c_item, c_name, r_ent = item, item['function']['name'], None
                try:
                    c_args = json.loads(c_item['function']['arguments'])
                except (ValueError, KeyError):
                    c_args = {}
            t0 = asyncio.get_running_loop().time()
            res = await b.harness.execute(c_name, c_args) if is_tool_allowed(c_name, allowed) else {'error': '此助手未启用该工具'}
            dur = max(1, round((asyncio.get_running_loop().time() - t0) * 1000))
            return c_item, c_name, c_args, res, dur, r_ent

        items = running_entries if running_entries is not None else batch
        results = await asyncio.gather(*[_run_item(it) for it in items])
        silent_res: dict | None = None
        mm_frame: dict | None = None
        for c_item, name, args, result, dur, r_ent in results:
            if name in ('stay_silent', 'do_not_disturb') or (isinstance(result, dict) and result.get('_stay_silent')):
                silent_res = result if isinstance(result, dict) else {'reason': 'stay_silent'}
            if isinstance(result, dict) and result.get('_multimodal_b64'):
                mb = str(result.pop('_multimodal_b64') or '')
                mm = str(result.pop('_multimodal_mime', 'image/jpeg') or 'image/jpeg')
                mc = str(result.pop('_multimodal_caption', '') or '【工具返回的最新实时画面】')
                if mb:
                    mm_frame = {'b64': mb, 'mime': mm, 'caption': mc}
            entry = {'tool': name, 'arguments': args, 'result': result, 'duration_ms': dur, 'status': 'done'}
            trace.append(entry)
            if r_ent is not None:
                r_ent.update(entry)
            content_str = (
                b.context_search.maybe_offload_tool_output(name, args, result, bot_id=target_id, turn_id=b.turn_id_var.get())
                if offload_output
                else json.dumps(result, ensure_ascii=False)
            )
            messages.append({'role': 'tool', 'tool_call_id': c_item['id'], 'content': content_str})
        return silent_res, mm_frame

    @staticmethod
    def _build_vision_msgs(messages: list[dict], mm_frame: dict | None, vision_ok: bool) -> tuple[list[dict], dict | None]:
        if mm_frame and vision_ok:
            return messages + [{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': mm_frame['caption']},
                    {'type': 'image_url', 'image_url': {'url': f"data:{mm_frame['mime']};base64,{mm_frame['b64']}"}},
                ],
            }], None
        return messages, mm_frame

    async def run_cycle(self, *, manual: bool = False, bot_id: str | None = None, source: str = 'phone') -> dict[str, Any]:
        b = self.brain
        if not manual and not self.store.config.enabled:
            return {'suppressed': True, 'reason': 'disabled'}
        gate = getattr(b, 'proactive_gate', None)
        if gate is not None:
            allowed_g, reason_g, next_delay_g = gate.can_emit_proactive(manual=manual, cooldown_sec=300.0)
            if not allowed_g:
                if reason_g == 'paused_after_unanswered_nudges' or reason_g.startswith('exponential_backoff'):
                    b._finish_curiosity_cycle('unanswered_backoff', bot_id or self.store.state['primary_bot_id'], f'主动触达自动退避 ({reason_g})')
                if next_delay_g > 0 and not manual:
                    self.store.state['curiosity_next_run'] = max(float(self.store.state.get('curiosity_next_run') or 0), time.time() + next_delay_g)
                    self.store.save()
                return {'suppressed': True, 'reason': reason_g, 'next_delay_s': next_delay_g}
        elif not manual and (self.store.state['snooze_until'] > time.time() or TaskContinuityManager.is_any_foreground_busy(b) or b.is_any_bot_busy()):
            return {'suppressed': True, 'reason': 'bot_busy', 'next_delay_s': 120.0}

        src = normalize_node_source(source)
        target_id = bot_id or self.store.state['primary_bot_id']
        origin_tok = b.turn_origin_context.set('autonomous') if hasattr(b, 'turn_origin_context') else None
        async with b.lock, b._get_bot_lock(target_id, src):
            pre_push_guard = gate.capture_guard() if gate is not None else None
            if not manual:
                if b.executions_by_bot or any(bool(q) for q in b.queued_tasks_by_bot.values()):
                    return {'suppressed': True, 'reason': 'bot_busy', 'next_delay_s': 120.0}
                bot_for_src = get_assistant(self.store, target_id, source=src)
                if not bot_for_src.get('curiosity_enabled', False):
                    return {'suppressed': True, 'reason': 'curiosity_disabled_on_node'}

            marker = b.bot_context.set(target_id)
            room_marker = b.room_context.set(target_id)
            src_marker = b.source_context.set(src)
            cur_turn_token = b.turn_id_var.set('turn-' + secrets.token_hex(6))
            cur_prefix_token = b.stream_prefix_var.set('')
            cur_choices_token = b.choices_var.set(None)
            cur_choices_prompt_token = b.choices_prompt_var.set('')
            cur_task_c = asyncio.current_task()
            nb_cur = f'{src}:{target_id}'
            if cur_task_c:
                b.active_tasks_by_node_bot[nb_cur] = cur_task_c
                b.active_tasks_by_bot[target_id] = cur_task_c
            b.activity = 'curiosity'
            try:
                now_ts = time.time()
                bot = get_assistant(self.store, target_id, source=src)
                use_big = (bot.get('engine') == 'big')
                cfg = self.store.config
                api_key = cfg.effective_api_key(big=use_big) or cfg.effective_api_key(big=not use_big)
                model = cfg.effective_model(bot_model=bot.get('model', ''), big=use_big) or cfg.effective_model(bot_model=bot.get('model', ''), big=not use_big)

                # BUG-TOX-01/02 purge: zero canned fallback when no API key/model is configured.
                if not api_key or not model:
                    b._finish_curiosity_cycle('silent', target_id, '未配置可用模型 API Key，静默跳过')
                    return {'status': 'silent', 'ok': True, 'silent': True, 'bot_id': target_id, 'reason': 'no_api_key_configured'}

                if api_key != 'fixture' and b.is_phone_online():
                    await b._poll_and_converge_notifications(timeout=2.5)
                notif_digest_md = b.notification_hub.build_curiosity_digest(mark_seen=False)
                raw_imp, missing_imp = b.memory_engine.get_personal_impression()
                recent_items = [m for m in self.store.state['messages'] if m.get('bot_id', 'companion') == target_id and normalize_node_source(m.get('source')) == src][-8:]
                coalesced = self._collect_coalesced_periodic_work(now_ts=now_ts, target_id=target_id)

                plan = self._select_intent(
                    now_ts=now_ts,
                    manual=manual,
                    target_id=target_id,
                    raw_imp=raw_imp,
                    notif_digest_md=notif_digest_md,
                    recent_msgs=recent_items,
                    coalesced=coalesced,
                )
                if plan.get('intent') == 'silent_noop':
                    self._record_silent_episode(plan, target_id, plan.get('reason', ''), 'zero_token_noop', [], now_ts)
                    b._finish_curiosity_cycle('silent', target_id, str(plan.get('reason') or 'silent_noop'))
                    return {'status': 'silent', 'ok': True, 'silent': True, 'bot_id': target_id, 'intent': 'silent_noop', 'reason': str(plan.get('reason') or '')}

                bot_allowed = normalize_bot_tools(bot.get('tools'))
                allowed = [name for name in CURIOSITY_SAFE_TOOLS if is_tool_allowed(name, bot_allowed) or name == 'reply']
                available_tools = [t for t in TOOLS if is_tool_allowed(t['function']['name'], allowed)]

                opps = b.vault.read('tasks/sniffed_opportunities.md')[:2400]
                deadlines = b.vault.read('tasks/active_deadlines.md')[:2400]
                routines_md = b.vault.read('playbooks/routines.md')[:2000]
                schedule_md = ''
                with contextlib.suppress(Exception):
                    schedule_md = b.vault.read('profile/course_schedule.md')[:3000]

                deltas_block = b.memory_engine.peek_recent_memory_deltas(limit=8)
                sql = getattr(self.store, 'sql', None)
                recent_episodes = sql.list_recent_curiosity_episodes(limit=6) if (sql is not None and hasattr(sql, 'list_recent_curiosity_episodes')) else []

                recent_digest = [
                    {
                        'role': m.get('role', 'user'),
                        'speaker': '用户' if m.get('role') == 'user' else (m.get('from_bot_name') or bot['name']),
                        'kind': m.get('kind', 'chat'),
                        'text': (b._get_llm_history_text(m) if m.get('role') == 'assistant' else m.get('text', ''))[:240],
                    }
                    for m in recent_items
                ]
                missing_hint = ('\n- ' + '\n- '.join(missing_imp[:8])) if missing_imp else '（主要维度已初步记录，可深入细节）'
                intent_directive = self._format_intent_directive(plan, recent_episodes)
                periodic_awareness = self._build_periodic_awareness_summary(target_id=target_id, coalesced=coalesced)

                force_ask = bool(plan.get('force_ask_curiosity'))
                mode_rules = (
                    '【好奇心主动提问模式 · 本轮强制发问】：\n'
                    '- 主人已把好奇心设为主动提问模式：本轮**必须**用 2~4 句自然熟稔的口吻向主人提出**一个明确、具体且带问号（？/?）的问题**，**严禁**调用 `reply(op="silent")` 或输出 `SILENT_OK` 静默跳过。\n'
                    '- 若周期巡检/通知栏无异常或旧待办已过时，直接略过汇报废话，切入下方的【本轮强制好奇心提问】方向自然聊天并提问；可按需调用 `reply(op="choices", choices=[...])` 提供贴心快捷选项。\n'
                    if force_ask
                    else
                    '【好奇心与主动陪伴原则 · 没事就调用 `reply(op="silent")` 沉默】：\n'
                    '- 根据下方选定的意图、今日状态、记忆增量、待办与最近触达台账做一次轻量判断。\n'
                    '- 若没有真正值得告诉用户或温和关心的内容，请立即调用 `reply(op="silent", reason="...", snooze_minutes=0)` 静默结束（或仅输出 `SILENT_OK`），绝不为了凑字数硬聊。\n'
                    '- 若决定开口：控制在 2~4 句，语气真诚自然、不查户口、不复读报错，可按需调用 `reply(op="choices", choices=[...])` 提供贴心选项。\n'
                )
                sys_prompt = (
                    b.harness.static_system_instructions(b.memory_engine.build_static_pinned_block()) + '\n\n'
                    '你的设定：' + b.persona() + '\n\n' + mode_rules
                )
                user_trigger = (
                    '当前时间：' + b._fmt_bj(now_ts) + ' (' + now_iso() + ')\n'
                    + intent_directive + '\n\n'
                    + (periodic_awareness + '\n\n' if periodic_awareness else '')
                    + (deltas_block + '\n\n' if deltas_block else '')
                    + '【手机通知栏积攒摘要】：\n' + notif_digest_md + '\n\n'
                    + '个人印象档案：\n' + raw_imp[:1800] + '\n\n'
                    + '尚未了解的个人印象维度：' + missing_hint + '\n\n'
                    + '课表：\n' + (schedule_md or '（未记录）') + '\n\n'
                    + '待确认截止事项：\n' + deadlines + '\n\n'
                    + '常驻例行：\n' + routines_md + '\n\n'
                    + '待办机会池：\n' + opps + '\n\n'
                    + '最近对话摘要（仅用于防重复，严禁顺着里面的闲聊或报错继续尬聊）：\n' + json.dumps(recent_digest, ensure_ascii=False)
                )

                messages: list[dict[str, Any]] = [
                    {'role': 'system', 'content': sys_prompt},
                    {'role': 'user', 'content': user_trigger},
                ]
                trace: list[dict[str, Any]] = []
                cur_accumulated: list[str] = []
                cur_mm_frame: dict[str, Any] | None = None
                api_calls = 0
                forced_ask_retried = False
                silent_requested_in_tools = False
                silent_reason_saved = 'stay_silent'
                silent_snooze_saved: int | None = None

                for _ in range(6):
                    api_calls += 1
                    call_msgs, cur_mm_frame = self._build_vision_msgs(messages, cur_mm_frame, b._is_current_bot_vision_capable())
                    msg = await b.llm.complete_silent(call_msgs, tools=available_tools or None, big=use_big, bot_id=target_id)
                    msg.pop('_metrics', None)
                    step_txt = b._clean_and_filter_dot_text(msg.get('content') or '')
                    if step_txt and (not cur_accumulated or (step_txt != cur_accumulated[-1] and step_txt not in cur_accumulated[-1])):
                        cur_accumulated.append(step_txt)
                    calls = msg.get('tool_calls') or []
                    if not calls:
                        break
                    messages.append(msg)
                    batch_c = calls[:6]
                    silent_res, new_mm = await self._execute_tool_batch(
                        batch=batch_c, allowed=allowed, target_id=target_id, trace=trace, messages=messages,
                    )
                    if new_mm is not None:
                        cur_mm_frame = new_mm
                    if silent_res is not None:
                        silent_reason_saved = str(silent_res.get('reason') or 'stay_silent')
                        silent_snooze_saved = int(silent_res.get('snooze_minutes') or 0) or None
                        if force_ask and not forced_ask_retried:
                            silent_requested_in_tools = True
                            cur_accumulated.clear()
                            break
                        return self._finalize_silent(
                            plan=plan, target_id=target_id, reason=silent_reason_saved,
                            outcome='silent', coalesced=coalesced, trace=trace, now_ts=now_ts,
                            snooze_min=silent_snooze_saved,
                        )
                    if all(c['function']['name'] in ('present_choices', 'reply') for c in batch_c) and cur_accumulated:
                        break

                choice_prompt = b._clean_and_filter_dot_text(b.choices_prompt_var.get() or '')
                if choice_prompt and all(choice_prompt not in p for p in cur_accumulated):
                    cur_accumulated.append(choice_prompt)
                text = b._clean_and_filter_dot_text('\n\n'.join(cur_accumulated))

                if force_ask and not forced_ask_retried and (silent_requested_in_tools or not text or is_silent_proactive_output(text)):
                    forced_ask_retried = True
                    api_calls += 1
                    retry_prompt = (
                        '禁止静默！巡检无需汇报也没关系，但本轮好奇心必须向主人提出一个明确问题。'
                        '请不要调工具也不要输出 SILENT_OK，立刻用 2~3 句熟稔自然的语气围绕本轮【本轮强制好奇心提问】直接对主人开口，并在结尾带一个明确问号（？）。'
                    )
                    retry_msg = await b.llm.complete_silent(
                        messages + [{'role': 'user', 'content': retry_prompt}],
                        tools=available_tools or None,
                        big=use_big,
                        bot_id=target_id,
                        tool_choice='none',
                    )
                    text = b._clean_and_filter_dot_text(retry_msg.get('content') or '')

                if (text and is_silent_proactive_output(text)) or (silent_requested_in_tools and not text):
                    return self._finalize_silent(
                        plan=plan, target_id=target_id, reason=(text or silent_reason_saved)[:120],
                        outcome='silent', coalesced=coalesced, trace=trace, now_ts=now_ts,
                        snooze_min=silent_snooze_saved,
                    )

                if not text and (trace or b.choices_var.get()):
                    api_calls += 1
                    nudge_msg = (
                        '请基于以上结果直接用自然简练的语气对用户说一句主动的话并提出本轮【强制好奇心提问】的明确问题（必须带问号 ？），不要再调用工具。'
                        if force_ask
                        else '请基于以上结果直接用自然简练的语气对用户说一句主动的话（若无需打扰请直接输出 SILENT_OK），不要再调用工具。'
                    )
                    summary = await b.llm.complete_silent(
                        messages + [{'role': 'user', 'content': nudge_msg}],
                        tools=available_tools or None,
                        big=use_big,
                        bot_id=target_id,
                        tool_choice='none',
                    )
                    text = b._clean_and_filter_dot_text(summary.get('content') or '')
                    if text and is_silent_proactive_output(text):
                        return self._finalize_silent(
                            plan=plan, target_id=target_id, reason=text[:120],
                            outcome='silent', coalesced=coalesced, trace=trace, now_ts=now_ts,
                        )

                # Zero canned fallback on empty model output!
                if not text:
                    return self._finalize_silent(
                        plan=plan, target_id=target_id, reason='empty_model_output',
                        outcome='empty_silent', coalesced=coalesced, trace=trace, now_ts=now_ts,
                        mark_notifs_seen=False,
                    )

                if gate is not None:
                    ok_guard, guard_reason = gate.verify_guard(pre_push_guard, manual=manual)
                    if not ok_guard:
                        self._record_silent_episode(plan, target_id, f'guard:{guard_reason}', 'guard_suppressed', trace, now_ts)
                        b._finish_curiosity_cycle('silent', target_id, f'推送前拦截: {guard_reason}')
                        return {'status': 'silent', 'ok': True, 'silent': True, 'suppressed': True, 'reason': guard_reason}

                cur_min = max(1, int(getattr(self.store.config, 'curiosity_interval_minutes', 30) or 30))
                if gate is not None and not manual:
                    gate.record_proactive_sent()
                    next_gap_min = max(cur_min, int(round(cur_min * gate.multiplier())))
                else:
                    next_gap_min = cur_min

                b.notification_hub.mark_curiosity_digest_seen()
                self._advance_coalesced_periodic_work(coalesced, outcome='sent', now_ts=now_ts)
                b._finish_curiosity_cycle('emitted', target_id, text[:120], snooze_min=next_gap_min)
                self._post_send_bookkeeping(plan=plan, target_id=target_id, text=text, trace=trace, now_ts=now_ts)
                mode = 'big_brain' if use_big else 'small_brain'
                extra: dict[str, Any] = {'api_calls': api_calls, 'from_bot_id': target_id, 'from_bot_name': bot['name'], 'intent': plan.get('intent', ''), 'coalesced_count': len(coalesced)}
                if trace:
                    extra['trace'] = trace
                emitted = await b.emit(text, 'curiosity', engine=mode, **extra)
                if bot.get('notify', True):
                    await b.publish({
                        'type': 'user_notification',
                        'id': emitted['id'],
                        'bot_id': target_id,
                        'bot_name': bot['name'],
                        'title': f"{bot['name']} · 主动搭话",
                        'body': text[:300],
                        'urgent': False,
                    })
                return emitted
            finally:
                if cur_task_c and b.active_tasks_by_node_bot.get(nb_cur) is cur_task_c:
                    b.active_tasks_by_node_bot.pop(nb_cur, None)
                if cur_task_c and b.active_tasks_by_bot.get(target_id) is cur_task_c:
                    b.active_tasks_by_bot.pop(target_id, None)
                fin_cur_turn = b.turn_id_var.get() or ''
                if not b.executions_by_bot.get(target_id):
                    b._clear_bot_stream(target_id, source=src)
                    with contextlib.suppress(Exception):
                        await b.publish({'type': 'stream', 'source': src, 'bot_id': target_id, 'turn_id': fin_cur_turn, 'stream': None, 'seq': b.stream_seq})
                        await b.publish({'type': 'execution', 'source': src, 'bot_id': target_id, 'execution': None, 'seq': b.stream_seq})
                b.choices_var.reset(cur_choices_token)
                b.choices_prompt_var.reset(cur_choices_prompt_token)
                b.stream_prefix_var.reset(cur_prefix_token)
                b.turn_id_var.reset(cur_turn_token)
                b.source_context.reset(src_marker)
                b.room_context.reset(room_marker)
                b.bot_context.reset(marker)
                if origin_tok is not None:
                    b.turn_origin_context.reset(origin_tok)
                b.activity = 'thinking' if b.executions_by_bot else b.ready_state()

    async def _cleanup_turn(
        self,
        *,
        target_id: str,
        src: str,
        cur_task: Any,
        my_exec: dict | None = None,
    ) -> None:
        b = self.brain
        nb_key = f'{src}:{target_id}'
        if my_exec is not None:
            if b.executions_by_node_bot.get(nb_key) is my_exec:
                b.executions_by_node_bot.pop(nb_key, None)
            if b.executions_by_bot.get(target_id) is my_exec:
                b.executions_by_bot.pop(target_id, None)
            if b.execution is my_exec:
                b.execution = None
        if cur_task and b.active_tasks_by_node_bot.get(nb_key) is cur_task:
            b.active_tasks_by_node_bot.pop(nb_key, None)
        if cur_task and b.active_tasks_by_bot.get(target_id) is cur_task:
            b.active_tasks_by_bot.pop(target_id, None)
        fin_t = b.turn_id_var.get() or ''
        if not b.executions_by_bot.get(target_id):
            b._clear_bot_stream(target_id, source=src)
            with contextlib.suppress(Exception):
                await b.publish({'type': 'stream', 'source': src, 'bot_id': target_id, 'turn_id': fin_t, 'stream': None, 'seq': b.stream_seq})
                await b.publish({'type': 'execution', 'source': src, 'bot_id': target_id, 'execution': None, 'seq': b.stream_seq})
        b.activity = 'thinking' if b.executions_by_bot else b.ready_state()

    def _restore_sched_tokens(self, target_id: str, nb_key: str, pre_pt: int, pre_ct: int, emitted_text: str) -> None:
        if pre_pt <= 0:
            return
        b = self.brain
        delta = b.memory_engine.estimate_tokens(emitted_text) if emitted_text else 0
        restored = pre_pt + delta
        if int(b.last_prompt_tokens.get(nb_key, 0) or 0) <= restored:
            return
        for key in (nb_key, target_id):
            b.last_prompt_tokens[key] = restored
            self.store.state.setdefault('last_prompt_tokens', {})[key] = restored
            capped = min(pre_ct, restored)
            b.last_cached_tokens[key] = capped
            self.store.state.setdefault('last_cached_tokens', {})[key] = capped
        self.store.save()
