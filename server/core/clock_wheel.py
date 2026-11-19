"""Unified 0.5s Clock Wheel & Cross-Pool Alarm/Blackboard Arbiter.

Resolves Bugbox Section 44:
1. Independent 0.5s clock wheel decoupled from long LLM turns (no serial `await` blocking).
2. Fast-lane physical delivery (`direct_alarm` / `direct_notify`) executes within <= 500ms even
   when the target bot is busy running a long task.
3. Cross-pool deduplication between `timers` (`schedule_task`) and scheduled `blackboard_posts`
   using `(bot_id, 5min_bucket, normalized_goal_sig)` + text overlap check.
4. `deadline_seconds` + `on_late` grading (`run_with_notice` / `notify_only` / `drop`).
5. Safe ReAct step-boundary interrupt queue (`brain.urgent_interrupts_by_node_bot`) with
   append-only injection and automatic fallback execution if the active turn ends before
   consuming the interrupt.
6. Also hosts compact inline blackboard wakeup digests (L4 context anti-bloat helper).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

from core.task_continuity import TaskContinuityManager
from core.turn_ledger import TurnLedgerManager
from storage import now_iso

_MENTIONS_RE = re.compile(r'@[A-Za-z0-9_\-\u4e00-\u9fa5:：]+')
_DEDUPE_STOPWORDS = re.compile(
    r'(?:请|帮我|麻烦|记得|提醒我|提醒一下|提醒|叫我|一下|设个闹钟|定个闹钟|定闹钟|设闹钟|闹钟|定时任务|日程|待办|到时候|届时|务必|一定要|去|吧|了)'
)
_PUNCT_WS = re.compile(r'[\s\W_]+', flags=re.UNICODE)


def _clean_core_text(text: str) -> str:
    raw = _MENTIONS_RE.sub('', str(text or '').strip().lower())
    raw = _DEDUPE_STOPWORDS.sub('', raw)
    cleaned = _PUNCT_WS.sub('', raw)
    if not cleaned:
        cleaned = _PUNCT_WS.sub('', _MENTIONS_RE.sub('', str(text or '').strip().lower()))
    return cleaned


def normalize_schedule_signature(text: str) -> str:
    """Return a stable short signature for cross-pool timer/blackboard deduplication."""
    return hashlib.sha1(_clean_core_text(text).encode('utf-8', errors='ignore')).hexdigest()[:12]


def _char_bigrams(s: str) -> set[str]:
    cleaned = _clean_core_text(s)
    if len(cleaned) <= 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def text_similarity(a: str, b: str) -> float:
    ca, cb = _clean_core_text(a), _clean_core_text(b)
    if not ca or not cb:
        return 0.0
    if ca == cb or ca in cb or cb in ca:
        return 1.0
    sa, sb = _char_bigrams(a), _char_bigrams(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    jaccard = inter / max(1, len(sa | sb))
    overlap = inter / max(1, min(len(sa), len(sb))) if min(len(sa), len(sb)) >= 2 else 0.0
    return max(jaccard, overlap * 0.92)


def build_inline_wakeup_digest(
    post: dict[str, Any],
    reason_label: str = '黑板提醒',
    *,
    author_name: str = '',
    title_hint: str = '',
) -> str:
    """Build a self-contained compact wakeup prompt (<300 tokens) so bots do not waste an extra `blackboard(read)` step."""
    post_id = str(post.get('post_id') or '')
    eff_author = str(author_name or post.get('author_name') or post.get('author_bot_id') or '系统').strip()
    raw_title = str(title_hint or post.get('title') or '')
    title = re.sub(r'^(?:@[A-Za-z0-9_\-\u4e00-\u9fa5:：]+\s*)+', '', raw_title).strip()[:90]
    raw_body = re.sub(r'\s+', ' ', str(post.get('text') or '')).strip()
    body = re.sub(r'^(?:@[A-Za-z0-9_\-\u4e00-\u9fa5:：]+\s*)+', '', raw_body).strip()[:360]
    art = str(post.get('artifact_path') or '').strip()
    nxt = str(post.get('next_action') or '').strip()[:180]
    replies = post.get('replies') if isinstance(post.get('replies'), list) else []
    rep_tail = ''
    if replies:
        last_r = replies[-1]
        r_author = str(last_r.get('author_name') or last_r.get('author_bot_id') or '')
        r_txt = re.sub(r'\s+', ' ', str(last_r.get('text') or '')).strip()[:160]
        rep_tail = f'\n最近回复（{r_author}）：{r_txt}'
    art_line = f'\n关联产物：`{art}`' if art else ''
    nxt_line = f'\n建议下一步：{nxt}' if nxt else ''
    return (
        f'[{reason_label} · #{post_id} · 来自「{eff_author}」] {title}\n'
        f'摘要：{body}{rep_tail}{art_line}{nxt_line}\n'
        '（以上已是该黑板帖子的完整摘要与最新进展，通常无需再调 `blackboard(read)`，仅在确实需要更早完整回复历史时再调；处理后若需回复结果直接 `blackboard(reply/done)`。）'
    )


class UnifiedClockWheel:
    def __init__(self, brain):
        self.brain = brain
        self._tick_lock = asyncio.Lock()
        self._dispatch_tasks: dict[str, asyncio.Task] = {}
        self._fired_dedupe: dict[tuple[str, int, str], float] = {}
        self.last_tick_at: float = 0.0
        self.tick_count: int = 0
        self._running: bool = False
        self.last_error: str = ''

    def find_cross_pool_duplicate(
        self,
        bot_id: str,
        due_at: float,
        text: str,
        *,
        kind: str = 'timer',
        window_sec: float = 180.0,
    ) -> dict[str, Any] | None:
        """Check both `timers` and scheduled `blackboard_posts` for an overlapping reminder."""
        if due_at <= 0 or not str(text or '').strip():
            return None
        sig = normalize_schedule_signature(text)
        state = getattr(self.brain.store, 'state', {}) or {}
        # 1. Scan active timers
        for t in state.get('timers', []):
            if t.get('done'):
                continue
            if str(t.get('bot_id') or 'companion') != bot_id:
                continue
            t_due = float(t.get('due_at') or 0)
            if abs(t_due - due_at) <= window_sec:
                t_goal = str(t.get('goal') or '')
                if normalize_schedule_signature(t_goal) == sig or text_similarity(text, t_goal) >= 0.65:
                    return {'pool': 'timer', 'id': t.get('id', ''), 'item': t, 'due_at': t_due}
        # 2. Scan scheduled blackboard posts
        for p in state.get('blackboard_posts', []):
            if p.get('status') != 'scheduled':
                continue
            targets = list(p.get('target_bot_ids') or [])
            if targets and bot_id not in targets and str(p.get('author_bot_id') or '') != bot_id:
                continue
            p_due = float(p.get('due_at') or 0)
            if p_due > 0 and abs(p_due - due_at) <= window_sec:
                p_txt = f"{p.get('title') or ''} {p.get('text') or ''}".strip()
                if normalize_schedule_signature(p_txt) == sig or text_similarity(text, p_txt) >= 0.65:
                    return {'pool': 'blackboard', 'id': p.get('post_id', ''), 'item': p, 'due_at': p_due}
        return None

    def _mark_runtime_fired_or_is_dup(self, bot_id: str, due_at: float, text: str) -> bool:
        """Return True if this `(bot_id, 5min_bucket, sig)` already fired within the last 5 minutes."""
        now = time.time()
        # Prune entries older than 600s
        for k, ts in list(self._fired_dedupe.items()):
            if now - ts > 600.0:
                self._fired_dedupe.pop(k, None)
        bucket = int((due_at or now) // 300)
        sig = normalize_schedule_signature(text)
        key = (bot_id, bucket, sig)
        if key in self._fired_dedupe:
            return True
        self._fired_dedupe[key] = now
        return False

    async def execute_fast_lane(
        self,
        exec_action: str,
        bot_id: str,
        title: str,
        reason: str,
        collapse_key: str = '',
    ) -> dict[str, Any]:
        """Deliver physical notification / emergency alarm synchronously on the clock wheel (never blocked by busy LLM)."""
        act = str(exec_action or '').strip().lower()
        marker = self.brain.bot_context.set(bot_id)
        try:
            if act in ('ring_alarm', 'direct_alarm'):
                return await self.brain._tool_emergency_alarm({
                    'title': (title or '定时闹钟')[:120],
                    'reason': (reason or '预定时间到')[:1000],
                })
            if act in ('notify_user', 'direct_notify'):
                return await self.brain._tool_notify_user({
                    'title': (title or '定时提醒')[:120],
                    'body': (reason or '预定时间到')[:1000],
                    'urgent': True,
                    'collapse_key': collapse_key or f'bot:{bot_id}:scheduled',
                })
            return {'ok': False, 'skipped': True}
        finally:
            self.brain.bot_context.reset(marker)

    def enqueue_urgent_interrupt(
        self,
        bot_id: str,
        source: str,
        prompt: str,
        *,
        item_id: str = '',
        kind: str = 'timer',
        fallback_coro_factory=None,
    ) -> bool:
        """Queue a step-boundary interrupt for a currently-busy bot; attaches safety watchdog fallback."""
        src = str(source or 'phone')
        key = f'{src}:{bot_id}'
        q = self.brain.urgent_interrupts_by_node_bot.setdefault(key, [])
        entry = {
            'id': item_id or f'int-{int(time.time() * 1000)}',
            'kind': kind,
            'prompt': prompt,
            'queued_at': time.time(),
            'consumed': False,
        }
        q.append(entry)

        if callable(fallback_coro_factory):
            async def _watchdog_fallback():
                # Never fire concurrent fallback while the foreground turn/chain is still actively running!
                # Poll cleanly until consumed at step boundary OR the active foreground task finishes.
                poll_interval = max(0.01, float(getattr(self, 'watchdog_poll_interval', 1.0) or 1.0))
                max_wait_sec = max(10.0, float(getattr(self, 'watchdog_max_wait_sec', 3600.0) or 3600.0))
                max_polls = max(5, int(max_wait_sec / poll_interval))
                await asyncio.sleep(min(2.5, poll_interval * 2.5))
                for _ in range(max_polls):
                    if entry.get('consumed'):
                        return
                    if not TaskContinuityManager.is_foreground_busy(self.brain, bot_id, src):
                        break
                    await asyncio.sleep(poll_interval)
                await asyncio.sleep(min(0.05, poll_interval))
                if entry.get('consumed'):
                    return
                with contextlib.suppress(ValueError):
                    if key in self.brain.urgent_interrupts_by_node_bot and entry in self.brain.urgent_interrupts_by_node_bot[key]:
                        self.brain.urgent_interrupts_by_node_bot[key].remove(entry)
                await fallback_coro_factory()

            t = asyncio.create_task(_watchdog_fallback())
            self._dispatch_tasks[f'fb:{entry["id"]}'] = t
            t.add_done_callback(lambda _t, k=f'fb:{entry["id"]}': self._dispatch_tasks.pop(k, None))
        return True

    def _spawn_bot_job(self, job_key: str, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._dispatch_tasks[job_key] = t
        t.add_done_callback(lambda _t, k=job_key: self._dispatch_tasks.pop(k, None))
        return t

    async def _run_timer_job(self, t_item: dict[str, Any], now: float) -> None:
        t_bot = str(t_item.get('bot_id') or self.brain.store.state.get('primary_bot_id', 'companion'))
        goal = str(t_item.get('goal') or '')
        due_at = float(t_item.get('due_at') or now)
        overdue_sec = max(0.0, now - due_at)
        deadline_sec = float(t_item.get('deadline_seconds') or 0)
        on_late = str(t_item.get('on_late') or 'run_with_notice').strip().lower()
        priority = str(t_item.get('priority') or '').strip().lower()
        exec_action = str(t_item.get('exec_action') or '').strip().lower()

        if exec_action in ('ring_alarm', 'notify_user') or priority in ('direct_alarm', 'direct_notify'):
            act = exec_action or ('ring_alarm' if priority == 'direct_alarm' else 'notify_user')
            await self.execute_fast_lane(act, t_bot, title=goal[:60] or '定时闹钟', reason=goal)
            if t_item.get('direct_only'):
                TurnLedgerManager.append_run_log(
                    self.brain.store,
                    {'kind': 'timer', 'task_id': t_item.get('id', ''), 'bot_id': t_bot, 'label': '快车道闹钟', 'goal': goal[:120], 'status': 'fast_lane_ok', 'ts': now_iso()},
                )
                return

        if deadline_sec > 0 and overdue_sec > deadline_sec:
            if on_late == 'drop':
                self.brain.vault.append_work_log(f'[定时闹钟超时放弃] {t_item.get("id")} ({goal[:60]}) 已超时 {int(overdue_sec)}s > deadline {int(deadline_sec)}s')
                TurnLedgerManager.append_run_log(
                    self.brain.store,
                    {'kind': 'timer', 'task_id': t_item.get('id', ''), 'bot_id': t_bot, 'goal': goal[:120], 'status': 'expired_drop', 'late_seconds': int(overdue_sec), 'ts': now_iso()},
                )
                self.brain.store.save()
                return
            if on_late == 'notify_only':
                await self.execute_fast_lane(
                    'notify_user',
                    t_bot,
                    title=f'⏰ 迟到提醒（延迟 {max(1, int(overdue_sec // 60))} 分钟）',
                    reason=f'原定定时任务已错过最佳时机：{goal[:200]}',
                    collapse_key=f'bot:{t_bot}:late_timer',
                )
                TurnLedgerManager.append_run_log(
                    self.brain.store,
                    {'kind': 'timer', 'task_id': t_item.get('id', ''), 'bot_id': t_bot, 'goal': goal[:120], 'status': 'late_notify_only', 'late_seconds': int(overdue_sec), 'ts': now_iso()},
                )
                self.brain.store.save()
                return

        async def _do_llm_timer():
            eff_goal = (
                f'[⏰ 延迟触发说明：原定时间已过 {int(overdue_sec // 60)} 分钟 {int(overdue_sec % 60)} 秒]\n{goal}'
                if overdue_sec >= 90.0
                else goal
            )
            t0_tm = time.perf_counter()
            try:
                tm_res = await self.brain.run_scheduled(eff_goal, bot_id=t_bot, label='定时闹钟')
                TurnLedgerManager.append_run_log(
                    self.brain.store,
                    {
                        'kind': 'timer', 'task_id': t_item.get('id', ''), 'bot_id': t_bot, 'label': '定时闹钟',
                        'goal': goal[:120], 'status': 'ok',
                        'steps': len((tm_res or {}).get('trace') or []) if isinstance(tm_res, dict) else 0,
                        'duration_ms': int(round((time.perf_counter() - t0_tm) * 1000)), 'ts': now_iso(),
                    },
                )
            except Exception as tm_err:
                t_item['last_error'] = str(tm_err)[:240]
                TurnLedgerManager.append_run_log(
                    self.brain.store,
                    {'kind': 'timer', 'task_id': t_item.get('id', ''), 'bot_id': t_bot, 'status': 'error', 'error': str(tm_err)[:200], 'ts': now_iso()},
                )
                await self.brain.emit(f'⏰ [定时闹钟执行异常告警] 任务「{goal[:60]}」触发报错：{str(tm_err)[:180]}', 'error', track=False, bot_id=t_bot, source='phone')
            self.brain.store.save()
            se = getattr(self.brain, 'sync_engine', None)
            if se:
                se.trigger_schedules_changed(kind='timer', task_id=str(t_item.get('id') or ''), force=True)

        if priority in ('interrupt', 'steer', 'urgent') and self.brain.is_node_bot_busy(t_bot, 'phone'):
            self.enqueue_urgent_interrupt(
                t_bot,
                'phone',
                f'⏰ 到点定时加急事件（timer:{t_item.get("id", "")}）：{goal}\n请立即按需处理（若需向用户提醒可直接调 `notify_user` 或 `emergency_alarm`）。',
                item_id=str(t_item.get('id') or ''),
                kind='timer',
                fallback_coro_factory=_do_llm_timer,
            )
            return

        await _do_llm_timer()

    async def check_watchpoint(self, watch: dict) -> tuple[bool, str]:
        import httpx, os
        from pathlib import Path
        kind = str(watch.get('kind') or 'file_exists').strip().lower()
        target = str(watch.get('target') or '').strip()
        expect = str(watch.get('expect') or '').strip()
        if not target:
            return False, ''
        try:
            if kind in ('file_exists', 'file_contains'):
                try:
                    p = self.brain.harness.path(target)
                except Exception:
                    p = Path(target)
                if not p.exists():
                    return False, ''
                if kind == 'file_exists':
                    return True, f'文件已就绪: {target} ({p.stat().st_size} bytes)'
                body = p.read_text('utf-8', errors='replace')[:20000]
                return (True, f'文件 {target} 已包含 "{expect}"') if (expect and expect in body) else (False, '')
            if kind in ('cmd_exit', 'cmd_contains'):
                proc = await asyncio.create_subprocess_shell(
                    target, cwd=str(self.brain.harness.root),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    env={**os.environ, 'LANG': 'C.UTF-8'},
                )
                out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=8.0)
                out_s = (out_b or b'').decode('utf-8', errors='replace').strip()
                if kind == 'cmd_exit':
                    exp_code = int(expect) if expect.lstrip('-').isdigit() else 0
                    return (True, f'命令退出码 {proc.returncode}: {out_s[:240]}') if proc.returncode == exp_code else (False, '')
                return (True, f'命令输出命中 "{expect}": {out_s[:240]}') if (expect and expect in out_s) else (False, '')
            if kind == 'http_status':
                async with httpx.AsyncClient(timeout=6.0, trust_env=False) as client:
                    r = await client.get(target)
                exp_st = int(expect) if expect.isdigit() else 200
                return (True, f'HTTP {target} 返回状态码 {r.status_code}') if r.status_code == exp_st else (False, '')
        except Exception:
            return False, ''
        return False, ''

    async def deliver_blackboard_fire(
        self,
        engine,
        post: dict[str, Any],
        targets: list[tuple[str, str, str]],
        matched_watch_evidence: str,
        now: float,
    ) -> None:
        """Handle a newly-due scheduled/watchpoint Blackboard post with fast-lane + deadline + step-boundary interrupt."""
        pid = str(post.get('post_id') or '')
        due_at = float(post.get('due_at') or now)
        overdue_sec = max(0.0, now - due_at) if due_at > 0 else 0.0
        deadline_sec = float(post.get('deadline_seconds') or 0)
        on_late = str(post.get('on_late') or 'run_with_notice').strip().lower()
        priority = str(post.get('priority') or '').strip().lower()
        exec_act = str(post.get('exec_action') or '').strip().lower()
        author_id = str(post.get('author_bot_id') or 'companion')
        primary_target_bot = targets[0][0] if targets else author_id

        if exec_act in ('ring_alarm', 'notify_user') or priority in ('direct_alarm', 'direct_notify'):
            act = exec_act or ('ring_alarm' if priority == 'direct_alarm' else 'notify_user')
            await self.execute_fast_lane(
                act,
                primary_target_bot,
                title=str(post.get('title') or '黑板闹钟'),
                reason=str(post.get('text') or '黑板预定闹钟时间到'),
                collapse_key=f'bb:{pid}',
            )
            if post.get('direct_only'):
                return

        if not matched_watch_evidence and self._mark_runtime_fired_or_is_dup(primary_target_bot, due_at, f"{post.get('title') or ''} {post.get('text') or ''}"):
            post['dedup_skipped'] = True
            return

        if deadline_sec > 0 and overdue_sec > deadline_sec and not matched_watch_evidence:
            if on_late == 'drop':
                self.brain.vault.append_work_log(f'[黑板日程超时放弃] #{pid} ({str(post.get("title") or "")[:50]}) 已超截止窗口 {int(overdue_sec)}s')
                return
            if on_late == 'notify_only':
                await self.execute_fast_lane(
                    'notify_user',
                    primary_target_bot,
                    title=f'⏰ 黑板迟到提醒（#{pid} 延迟 {max(1, int(overdue_sec // 60))} 分钟）',
                    reason=str(post.get('text') or post.get('title') or '')[:240],
                    collapse_key=f'bb:{pid}',
                )
                return

        label = '黑板监视命中' if matched_watch_evidence else '黑板闹钟到期'
        digest = build_inline_wakeup_digest(post, reason_label=label)
        if overdue_sec >= 90.0 and not matched_watch_evidence:
            digest = f'[⏰ 延迟触发说明：原定时间已过 {int(overdue_sec // 60)} 分钟]\n{digest}'

        normal_targets = []
        for tid, tname, tsrc in targets:
            if priority in ('interrupt', 'steer', 'urgent') and self.brain.is_node_bot_busy(tid, tsrc):
                def _mk_fb(tb=tid, tn=tname, ts=tsrc):
                    return lambda: engine._async_wake_bot(
                        target_bot_id=tb, target_name=tn, target_src=ts, post_id=pid,
                        author_bot_id=author_id, author_name=str(post.get('author_name') or author_id),
                        title_hint=str(post.get('title') or '')[:56], steer=False, reason_label=label,
                    )
                self.enqueue_urgent_interrupt(tid, tsrc, digest, item_id=pid, kind='blackboard', fallback_coro_factory=_mk_fb())
            else:
                normal_targets.append((tid, tname, tsrc))
        if normal_targets:
            engine._schedule_wakeup(post, normal_targets, reason_label=label)

    async def tick_once(self) -> None:
        if self._tick_lock.locked():
            return
        async with self._tick_lock:
            now = time.time()
            self.last_tick_at = now
            self.tick_count += 1
            s, c = self.brain.store.state, self.brain.store.config
            if not c.enabled:
                return
            if 0 < s.get('snooze_until', 0) <= now:
                s['snooze_until'] = 0
                self.brain.store.save()
                self.brain.vault.write('profile/annoyance_valve.md', '# 烦人度阀门\n\n状态：好奇心开启（免打扰已到期）\n\n更新时间：' + now_iso() + '\n')

            try:
                await self.brain.escalation.tick()
            except Exception as exc:
                self.last_error = f'escalation.tick: {type(exc).__name__}: {exc}'
                logger.warning('clock_wheel escalation.tick failed: %s', exc, exc_info=True)
            if hasattr(self.brain, 'blackboard_engine'):
                try:
                    await self.brain.blackboard_engine.tick()
                except Exception as exc:
                    self.last_error = f'blackboard_engine.tick: {type(exc).__name__}: {exc}'
                    logger.warning('clock_wheel blackboard_engine.tick failed: %s', exc, exc_info=True)

            if s.get('snooze_until', 0) > now:
                return

            # Non-blocking deadlines
            for d in s.get('deadlines', []):
                if not d.get('notified') and float(d.get('due_at') or 0) <= now:
                    d['notified'] = True
                    self.brain.store.save()
                    se = getattr(self.brain, 'sync_engine', None)
                    if se:
                        se.trigger_schedules_changed(kind='deadline', task_id=str(d.get('id') or ''), force=True)
                    marker = self.brain.bot_context.set(s.get('primary_bot_id', 'companion'))
                    try:
                        await self.brain.emit('约好的截止时间到了：' + d['title'] + '。现在需要你拍板。', 'deadline')
                    finally:
                        self.brain.bot_context.reset(marker)

            has_api = bool((c.effective_api_key(big=True) or c.effective_api_key(big=False)) and (c.effective_model(big=True) or c.effective_model(big=False)))

            # Non-blocking due timers dispatch
            for t in s.get('timers', []):
                if t.get('done') or float(t.get('due_at') or 0) > now:
                    continue
                t_bot = str(t.get('bot_id') or s.get('primary_bot_id', 'companion'))
                priority = str(t.get('priority') or '').strip().lower()
                exec_action = str(t.get('exec_action') or '').strip().lower()
                is_urgent_or_fast = priority in ('direct_alarm', 'direct_notify', 'interrupt', 'steer', 'urgent') or exec_action in ('ring_alarm', 'notify_user')
                # Non-urgent normal timers still wait until target bot becomes idle (unless deadline already expired!)
                overdue_sec = max(0.0, now - float(t.get('due_at') or now))
                deadline_sec = float(t.get('deadline_seconds') or 0)
                on_late = str(t.get('on_late') or 'run_with_notice').strip().lower()
                is_late_lightweight = deadline_sec > 0 and overdue_sec > deadline_sec and on_late in ('drop', 'notify_only')
                if not is_urgent_or_fast and not is_late_lightweight:
                    if TaskContinuityManager.is_foreground_busy(self.brain, t_bot, 'phone'):
                        continue
                if self._mark_runtime_fired_or_is_dup(t_bot, float(t.get('due_at') or now), str(t.get('goal') or '')):
                    t['done'] = True
                    t['fired_at'] = now
                    t['dedup_skipped'] = True
                    self.brain.store.save()
                    continue
                t['done'] = True
                t['fired_at'] = now
                self.brain.store.save()
                se = getattr(self.brain, 'sync_engine', None)
                if se:
                    se.trigger_schedules_changed(kind='timer', task_id=str(t.get('id') or ''), force=True)
                if has_api or exec_action in ('ring_alarm', 'notify_user') or priority in ('direct_alarm', 'direct_notify'):
                    self._spawn_bot_job(f"timer:{t.get('id')}", self._run_timer_job(t, now))
                break  # dispatch one newly-fired timer per 500ms tick cleanly

    async def run_forever(self) -> None:
        self._running = True
        try:
            while True:
                try:
                    await self.tick_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = f'tick_once: {type(exc).__name__}: {exc}'
                    logger.exception('clock_wheel tick_once crashed')
                await asyncio.sleep(0.5)
        finally:
            self._running = False
