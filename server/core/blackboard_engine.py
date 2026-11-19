"""
[Module Responsibility]
事件驱动共享黑板 (`BlackboardEngine`)、零算力条件监视点 (`watch`)、黑板闹钟/定时器及全端 Bot 状态窥视 (`peek_bots`) 模块 (`server/core/blackboard_engine.py`)。
取代阻塞式点对点 `send_message`：
- 发帖/回帖/认领/交付/评审 `< 50ms` 瞬时返回，后台异步唤醒被 `@` 的目标端 Bot（支持 `@Bot名`、`@self`、`@everyone` 及 `steer=True` 抢占插话）；
- 支持可选的 Maker-Checker 审校门禁（仅当显式指定 `review_by` 或 `require_review=True` 时启用）；
- 统一承载黑板闹钟/定时器 (`at_time`/`delay_minutes`/`daily_time`/`exec_action`) 与 0-Token 条件监视器 (`watch`)；
- 提供无锁只读 `peek_bots` 供任意端随时查看其他 Bot 干到哪了。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

import httpx

from assistants import get_assistant, resolve_bot_for_source
from core.blackboard_scheduler_helpers import (
    MENTION_TOKEN_RE,
    expire_stale_leases,
    extract_mentions,
    fire_due_post,
    parse_due_timestamp,
    parse_weekdays,
    resolve_targets,
)
from core.task_continuity import TaskContinuityManager
from storage import node_display_name, normalize_node_source
from vault_manager import TZ, now_iso


class BlackboardEngine:
    MAX_POSTS = 200
    DEFAULT_LEASE_SEC = 900
    MAX_BLACKBOARD_HOP_DEPTH = 2

    def __init__(self, brain: Any):
        self.brain = brain
        self.store = brain.store
        self.vault = brain.vault
        self._watch_last_check: dict[str, float] = {}
        self._recent_wakeups: dict[tuple[str, str, str], float] = {}
        self._post_lock = asyncio.Lock()

    def _posts(self) -> list[dict]:
        posts = self.store.state.setdefault('blackboard_posts', [])
        if not isinstance(posts, list):
            posts = []
            self.store.state['blackboard_posts'] = posts
        return posts

    def _next_post_id(self) -> str:
        seq = int(self.store.state.get('blackboard_seq') or 0) + 1
        self.store.state['blackboard_seq'] = seq
        return f'bb-{seq}'

    @staticmethod
    def _norm_post_id(raw: Any) -> str:
        s = str(raw or '').strip().lstrip('#')
        if not s:
            return ''
        if s.isdigit():
            return f'bb-{s}'
        return s

    def _find_post(self, post_id: Any) -> dict | None:
        pid = self._norm_post_id(post_id)
        if not pid:
            return None
        for p in reversed(self._posts()):
            if isinstance(p, dict) and str(p.get('post_id') or '') == pid:
                return p
        return None

    def _expire_stale_leases(self, now: float) -> bool:
        return expire_stale_leases(self._posts(), now)

    def _extract_mentions(self, args: dict, text: str) -> list[str]:
        return extract_mentions(args, text)

    def _resolve_targets(self, mentions: list[str], author_bot_id: str, author_src: str) -> list[tuple[str, str, str]]:
        """Resolve `@mention` tokens into `[(bot_id, bot_name, target_source)]`."""
        return resolve_targets(self.store, mentions, author_bot_id, author_src)

    def _parse_due_timestamp(self, args: dict, now: float) -> tuple[float, dict]:
        """Parse optional timer/alarm scheduling fields."""
        return parse_due_timestamp(args, now)

    @staticmethod
    def _parse_weekdays(spec: str) -> set[int]:
        return parse_weekdays(spec)

    async def _fire_due_post(self, post: dict, matched_watch_evidence: str, now: float) -> None:
        await fire_due_post(self, post, matched_watch_evidence, now)

    def _schedule_wakeup(
        self,
        post: dict,
        targets: list[tuple[str, str, str]],
        steer: bool = False,
        reason_label: str = '黑板提醒',
        sender_bot_id: str | None = None,
        sender_name: str | None = None,
        snippet_text: str | None = None,
    ) -> list[str]:
        if bool(self.store.state.get('blackboard_muted', False)):
            return []
        cur_hop = max(
            int(self.brain.hop_depth.get() or 0) if hasattr(self.brain, 'hop_depth') else 0,
            int(self.brain.wake_hop_context.get() or 0) if hasattr(self.brain, 'wake_hop_context') else 0,
        )
        if cur_hop >= self.MAX_BLACKBOARD_HOP_DEPTH:
            return []
        woken: list[str] = []
        post_id = str(post.get('post_id') or '')
        eff_sender_id = str(sender_bot_id if sender_bot_id is not None else (post.get('author_bot_id') or ''))
        eff_sender_name = str(sender_name if sender_name is not None else (post.get('author_name') or eff_sender_id or '同事'))
        raw_hint = str(snippet_text if snippet_text is not None else (post.get('title') or post.get('text') or '')).strip()
        clean_hint = re.sub(r'^(?:@[A-Za-z0-9_\-\u4e00-\u9fa5:：]+\s*)+', '', raw_hint).replace('\n', ' ').strip()[:56]
        if not clean_hint:
            clean_hint = raw_hint.replace('\n', ' ')[:56]
        now_ts = time.time()
        for tid, tname, tsrc in targets:
            dedup_key = (post_id, tid, normalize_node_source(tsrc))
            last_w = self._recent_wakeups.get(dedup_key, 0.0)
            if not steer and (now_ts - last_w) < 6.0:
                continue
            self._recent_wakeups[dedup_key] = now_ts
            woken.append(f'{node_display_name(tsrc)}:{tname}({tid})')
            asyncio.create_task(
                self._async_wake_bot(
                    target_bot_id=tid,
                    target_name=tname,
                    target_src=tsrc,
                    post_id=post_id,
                    author_bot_id=eff_sender_id,
                    author_name=eff_sender_name,
                    title_hint=clean_hint,
                    steer=steer,
                    reason_label=reason_label,
                )
            )
        return woken

    async def _async_wake_bot(
        self,
        target_bot_id: str,
        target_name: str,
        target_src: str,
        post_id: str,
        author_bot_id: str = '',
        author_name: str = '同事',
        title_hint: str = '',
        steer: bool = False,
        reason_label: str = '黑板提醒',
    ) -> None:
        norm_src = normalize_node_source(target_src)
        nb_key = f'{norm_src}:{target_bot_id}'
        enqueued_stop_epoch = self.brain._user_stop_epoch_by_node_bot.get(nb_key, 0)
        cur_wake_task = asyncio.current_task()
        if cur_wake_task is not None:
            setattr(cur_wake_task, '_is_peer_wakeup', True)
            self.brain.queued_tasks_by_bot.setdefault(target_bot_id, set()).add(cur_wake_task)
            self.brain.queued_tasks_by_node_bot.setdefault(nb_key, set()).add(cur_wake_task)
        try:
            if steer and self.brain.is_node_bot_busy(target_bot_id, norm_src):
                TaskContinuityManager.snapshot_for_system_preempt(self.brain, target_bot_id, norm_src, 'peer_steer')
                await self.brain.stop_immediately(
                    bot_id=target_bot_id,
                    reason='peer_steer',
                    commit_partial=True,
                    source=norm_src,
                )
                for _ in range(15):
                    if not self.brain._get_bot_lock(target_bot_id, norm_src).locked():
                        break
                    await asyncio.sleep(0.01)

            has_custom_complete = 'complete' in getattr(self.brain, '__dict__', {}) or 'chat' in getattr(self.brain, '__dict__', {})
            has_api = has_custom_complete or bool(
                (self.store.config.effective_api_key(big=False) or self.store.config.effective_api_key(big=True))
                and (self.store.config.effective_model(big=False) or self.store.config.effective_model(big=True))
            )
            await self.brain.publish({
                'type': 'blackboard_wake',
                'post_id': post_id,
                'target_bot_id': target_bot_id,
                'target_bot_name': target_name,
                'source': norm_src,
                'steer': steer,
            })
            if not has_api:
                return
            from core.clock_wheel import build_inline_wakeup_digest
            async with self.brain._get_bot_lock(target_bot_id, norm_src):
                if enqueued_stop_epoch != self.brain._user_stop_epoch_by_node_bot.get(nb_key, 0):
                    return
                p_cur = self._find_post(post_id)
                if isinstance(p_cur, dict) and p_cur.get('status') == 'cancelled':
                    return
                wake_prompt = build_inline_wakeup_digest(
                    p_cur if isinstance(p_cur, dict) else {'post_id': post_id, 'author_name': author_name, 'author_bot_id': author_bot_id, 'title': title_hint, 'text': title_hint},
                    reason_label=reason_label,
                    author_name=author_name,
                    title_hint=title_hint,
                )
                turn_id = 'turn-' + secrets.token_hex(6)
                m = self.store.message(
                    'user',
                    wake_prompt,
                    kind='bot_message',
                    bot_id=target_bot_id,
                    from_bot_id=author_bot_id,
                    from_bot_name=author_name,
                    to_bot_id=target_bot_id,
                    direction='inbound',
                    source=norm_src,
                )
                self.brain.stream_seq += 1
                m['seq'] = self.brain.stream_seq
                m['turn_id'] = turn_id
                self.store.save()
                await self.brain.publish({'type': 'message', 'source': norm_src, 'message': m, 'seq': self.brain.stream_seq})
                next_hop = max(1, int(self.brain.hop_depth.get() or 0) + 1)
                hop_tok = self.brain.hop_depth.set(next_hop)
                origin_tok = self.brain.turn_origin_context.set('autonomous') if hasattr(self.brain, 'turn_origin_context') else None
                whop_tok = self.brain.wake_hop_context.set(next_hop) if hasattr(self.brain, 'wake_hop_context') else None
                try:
                    await self.brain.chat(
                        wake_prompt,
                        bot_id=target_bot_id,
                        mode='auto',
                        pre_recorded_user_msg=m,
                        turn_id=turn_id,
                        source=norm_src,
                        _lock_already_held=True,
                    )
                finally:
                    if whop_tok is not None:
                        self.brain.wake_hop_context.reset(whop_tok)
                    if origin_tok is not None:
                        self.brain.turn_origin_context.reset(origin_tok)
                    self.brain.hop_depth.reset(hop_tok)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            err_txt = str(exc)[:300]
            self.store.state['last_error'] = f'blackboard_wake({target_bot_id}): {err_txt[:180]}'
            try:
                self.store.message('assistant', f'执行出错：{err_txt}', kind='error', bot_id=target_bot_id, from_bot_id=target_bot_id, source=norm_src)
                async with self._post_lock:
                    p = self._find_post(post_id)
                    if p is not None:
                        p.setdefault('replies', []).append({'reply_id': 'r-' + secrets.token_hex(3), 'author_bot_id': target_bot_id, 'author_name': target_name, 'author_node': norm_src, 'text': f'⚠️ 唤醒执行异常：{err_txt}', 'artifact_path': '', 'mentions': [], 'created_at': time.time(), 'created_iso': now_iso()})
                    self.store.save()
            except Exception:
                pass
        finally:
            if cur_wake_task is not None:
                self.brain.queued_tasks_by_bot.get(target_bot_id, set()).discard(cur_wake_task)
                self.brain.queued_tasks_by_node_bot.get(nb_key, set()).discard(cur_wake_task)
            TaskContinuityManager.schedule_auto_resume_if_needed(self.brain, target_bot_id, norm_src)

    async def execute(self, args: dict) -> dict:
        t0 = time.perf_counter()
        action = str(args.get('action') or 'list').strip().lower()
        caller_bot_id = str(args.get('_caller_bot_id') or self.brain.bot_context.get() or 'companion').strip()
        caller_room_id = str(self.brain.room_context.get() or caller_bot_id).strip()
        caller_src = normalize_node_source(args.get('_caller_source') or self.brain.source_context.get())
        try:
            caller_name = str(get_assistant(self.store, caller_bot_id).get('name') or caller_bot_id)
        except Exception:
            caller_name = caller_bot_id

        if action == 'peek':
            return self.peek_bots(args)
        if action == 'write':
            return self.brain.context_search.blackboard_write(key=str(args.get('key') or ''), value=str(args.get('value') or args.get('text') or ''), author_bot=caller_bot_id, summary=str(args.get('summary') or args.get('title') or ''))

        async with self._post_lock:
            return await self._execute_locked(args, action, caller_bot_id, caller_room_id, caller_src, caller_name, t0)

    async def _execute_locked(self, args: dict, action: str, caller_bot_id: str, caller_room_id: str, caller_src: str, caller_name: str, t0: float) -> dict:
        now = time.time()
        if self._expire_stale_leases(now):
            self.store.save()
        if action in ('mute', 'unmute'):
            muted = action == 'mute'
            self.store.state['blackboard_muted'] = muted
            self.store.save()
            await self.brain.publish({'type': 'blackboard_updated', 'op': action, 'blackboard_muted': muted})
            return {'ok': True, 'blackboard_muted': muted}
        if action == 'delete':
            pid = self._norm_post_id(args.get('post_id') or args.get('topic_id') or '')
            if pid:
                if not self._find_post(pid):
                    return {'ok': False, 'error': f'未找到黑板条目 {pid}'}
                self.store.state['blackboard_posts'] = [p for p in self._posts() if p.get('post_id') != pid]
                self.store.save()
                return {'ok': True, 'deleted_post_id': pid}
            return self.brain.context_search.blackboard_delete(str(args.get('key') or ''))
        if action == 'set_todos':
            raw_steps = args.get('steps') if isinstance(args.get('steps'), list) else []
            if not raw_steps:
                return {'ok': False, 'error': 'steps 不能为空（请提供包含步骤标题的列表）'}
            todos = []
            for idx, st in enumerate(raw_steps[:12], 1):
                if isinstance(st, dict):
                    todos.append({
                        'title': str(st.get('title') or st.get('text') or f'步骤 {idx}')[:120],
                        'status': str(st.get('status') or ('done' if st.get('done') else 'pending')),
                    })
                elif isinstance(st, str) and st.strip():
                    todos.append({'title': st.strip()[:120], 'status': 'pending'})
            if not todos:
                return {'ok': False, 'error': 'steps 不能为空'}
            todo_key = str(args.get('key') or 'todos').strip() or 'todos'
            self.brain.context_search.blackboard_write(
                key=todo_key,
                value=json.dumps(todos, ensure_ascii=False),
                author_bot=caller_bot_id,
                summary='; '.join(f"{t['title']}({t['status']})" for t in todos[:5]),
            )
            cur_exec = self.brain.executions_by_bot.get(caller_room_id) or self.brain.execution
            if isinstance(cur_exec, dict):
                cur_exec['todos'] = todos
                self.brain.stream_seq += 1
                await self.brain.publish({
                    'type': 'execution',
                    'source': normalize_node_source(cur_exec.get('source') or caller_src),
                    'bot_id': cur_exec.get('bot_id') or caller_room_id,
                    'turn_id': cur_exec.get('turn_id', ''),
                    'execution': cur_exec,
                    'seq': self.brain.stream_seq,
                })
            return {'ok': True, 'key': todo_key, 'todos': todos}

        # 2. Event Blackboard `post` (tasks, @mentions, timers/alarms, zero-token watchpoints)
        if action in ('post', 'publish', 'schedule', 'watch'):
            text = str(args.get('text') or args.get('value') or args.get('content') or args.get('goal') or '').strip()
            title = str(args.get('title') or args.get('summary') or '').strip()[:140]
            if not text and not title:
                return {'ok': False, 'error': 'post 需要提供 text（或 title/value）'}
            if not text:
                text = title
            mentions = self._extract_mentions(args, f'{title} {text}')
            try:
                due_at, sched_meta = self._parse_due_timestamp(args, now)
            except ValueError as exc:
                return {'ok': False, 'error': str(exc)}
            watch_cfg = args.get('watch') if isinstance(args.get('watch'), dict) else None
            if action == 'watch' and not watch_cfg and args.get('target'):
                watch_cfg = {
                    'kind': str(args.get('watch_kind') or 'file_exists'),
                    'target': str(args.get('target') or ''),
                    'expect': str(args.get('expect') or ''),
                    'interval_sec': max(3, int(args.get('interval_sec') or 10)),
                }
            exec_action = str(args.get('exec_action') or '').strip().lower()
            if not exec_action and (args.get('ring_alarm') or args.get('alarm')):
                exec_action = 'ring_alarm'
            elif not exec_action and args.get('notify_user'):
                exec_action = 'notify_user'

            is_deferred = bool(due_at > now or watch_cfg)
            if is_deferred and not mentions:
                mentions = ['@self']

            # Opt-In ONLY Maker-Checker Gate: active ONLY when caller explicitly sets `review_by` or `require_review=True`
            review_by = str(args.get('review_by') or '').strip()
            if not review_by and bool(args.get('require_review', False)):
                review_by = '@contrarian' if caller_src in ('phone', 'phone2') else '@pc_reviewer'

            targets = self._resolve_targets(mentions, caller_bot_id, caller_src)
            priority = str(args.get('priority') or 'normal').strip().lower()
            deadline_sec = max(0, int(args.get('deadline_seconds') or 0))
            on_late = str(args.get('on_late') or 'run_with_notice').strip().lower()
            if on_late not in ('run_with_notice', 'notify_only', 'drop'):
                on_late = 'run_with_notice'
            cw = getattr(self.brain, 'clock_wheel', None)
            if is_deferred and due_at > now and not watch_cfg and cw is not None and not args.get('force_duplicate'):
                dup = cw.find_cross_pool_duplicate(caller_bot_id, due_at, f'{title} {text}', kind='blackboard')
                if dup is not None:
                    ex = dup['item']
                    if exec_action == 'ring_alarm':
                        ex['exec_action'] = 'ring_alarm'
                    elif exec_action == 'notify_user' and not ex.get('exec_action'):
                        ex['exec_action'] = 'notify_user'
                    self.store.save()
                    return {
                        'ok': True,
                        'deduplicated': True,
                        'duplicate_pool': dup['pool'],
                        'duplicate_id': dup['id'],
                        'post_id': dup['id'] if dup['pool'] == 'blackboard' else None,
                        'topic_id': dup['id'] if dup['pool'] == 'blackboard' else None,
                        'post': ex if dup['pool'] == 'blackboard' else None,
                        'timer': ex if dup['pool'] == 'timer' else None,
                    }
            post_id = self._next_post_id()
            post: dict[str, Any] = {
                'post_id': post_id, 'title': title or text.splitlines()[0][:72], 'text': text[:8000],
                'author_bot_id': caller_bot_id, 'author_name': caller_name, 'author_node': caller_src,
                'mentions': mentions, 'target_bot_ids': [t[0] for t in targets], 'unread_by': [t[0] for t in targets],
                'status': 'scheduled' if is_deferred else 'open',
                'claimed_by': '', 'claimed_name': '', 'claimed_node': '', 'lease_until': 0.0,
                'artifact_path': str(args.get('artifact_path') or '').strip(), 'next_action': str(args.get('next_action') or '').strip()[:400],
                'review_by': review_by, 'exec_action': exec_action, 'priority': priority,
                'direct_only': bool(args.get('direct_only', False)), 'deadline_seconds': deadline_sec, 'on_late': on_late,
                'merge_with_curiosity': args.get('merge_with_curiosity') if isinstance(args.get('merge_with_curiosity'), bool) else None,
                'due_at': round(due_at, 3) if due_at > 0 else 0.0,
                'due_iso': dt.datetime.fromtimestamp(due_at, TZ).isoformat(timespec='seconds') if due_at > 0 else '',
                'schedule': sched_meta, 'watch': watch_cfg, 'replies': [],
                'created_at': now, 'created_iso': now_iso(), 'updated_at': now,
            }
            posts = self._posts()
            posts.append(post)
            if len(posts) > self.MAX_POSTS:
                self.store.state['blackboard_posts'] = posts[-self.MAX_POSTS:]
            self.store.save()
            await self.brain.publish({'type': 'blackboard_updated', 'op': 'post', 'post': post})
            if is_deferred:
                se = getattr(self.brain, 'sync_engine', None)
                if se is not None:
                    await se.notify_schedules_changed(kind='blackboard', task_id=post_id, force=True)

            woken: list[str] = []
            steer_flag = bool(args.get('steer') or str(args.get('priority') or '').lower() in ('steer', 'interrupt', 'urgent'))
            if not is_deferred and targets:
                woken = self._schedule_wakeup(post, targets, steer=steer_flag, reason_label='黑板提醒')
            else:
                self.brain.wake.set()

            coalesce_hint = None
            if is_deferred:
                from core.curiosity_engine import detect_periodic_coalesce_opportunities
                coalesce_hint = detect_periodic_coalesce_opportunities(
                    self.store,
                    text,
                    interval_hours=sched_meta.get('interval_hours') if isinstance(sched_meta, dict) else None,
                    exclude_id=post_id,
                )

            res_payload = {
                'ok': True, 'post_id': post_id, 'topic_id': post_id, 'post': post, 'topic': post,
                'status': post['status'], 'mentions': mentions, 'woken': woken,
                'woken_bots': [t[0] for t in targets] if not is_deferred else [],
                'scheduled_due_iso': post['due_iso'] or None, 'watch_active': bool(watch_cfg),
                'review_by': review_by or None, 'elapsed_ms': max(1, int(round((time.perf_counter() - t0) * 1000))),
            }
            if coalesce_hint:
                res_payload['coalesce_hint'] = coalesce_hint
            return res_payload

        # 3. `reply`: append progress/reply + optional `@mention` wakeup
        if action == 'reply':
            post = self._find_post(args.get('post_id') or args.get('topic_id') or args.get('key'))
            if not post:
                return {'ok': False, 'error': 'reply 需要有效的 post_id（如 "bb-1"）'}
            text = str(args.get('text') or args.get('value') or args.get('content') or '').strip()
            if not text:
                return {'ok': False, 'error': 'reply 内容 text 不能为空'}
            mentions = self._extract_mentions(args, text)
            targets = self._resolve_targets(mentions, caller_bot_id, caller_src)
            art = str(args.get('artifact_path') or '').strip()
            if art:
                post['artifact_path'] = art
            if args.get('next_action'):
                post['next_action'] = str(args.get('next_action')).strip()[:400]
            rep = {
                'reply_id': 'r-' + secrets.token_hex(3),
                'author_bot_id': caller_bot_id,
                'author_name': caller_name,
                'author_node': caller_src,
                'text': text[:6000],
                'artifact_path': art,
                'mentions': mentions,
                'created_at': now,
                'created_iso': now_iso(),
            }
            post.setdefault('replies', []).append(rep)
            unread = set(post.get('unread_by') or [])
            unread.discard(caller_bot_id)
            for tid, _, _ in targets:
                unread.add(tid)
            post['unread_by'] = list(unread)
            post['updated_at'] = now
            self.store.save()
            await self.brain.publish({'type': 'blackboard_updated', 'op': 'reply', 'post': post})
            steer_flag = bool(args.get('steer'))
            woken = (
                self._schedule_wakeup(
                    post,
                    targets,
                    steer=steer_flag,
                    reason_label='黑板回帖提醒',
                    sender_bot_id=caller_bot_id,
                    sender_name=caller_name,
                    snippet_text=text,
                )
                if targets else []
            )
            return {'ok': True, 'post_id': post['post_id'], 'reply': rep, 'woken': woken}

        # 4. `claim`: atomic Kanban claim with 15-min lease so `@everyone` tasks are never duplicated
        if action == 'claim':
            post = self._find_post(args.get('post_id') or args.get('topic_id') or args.get('key'))
            if not post:
                return {'ok': False, 'error': 'claim 需要有效的 post_id'}
            cur_claimed = str(post.get('claimed_by') or '')
            lease_until = float(post.get('lease_until') or 0)
            if post.get('status') == 'claimed' and cur_claimed and cur_claimed != caller_bot_id and lease_until > now:
                return {
                    'ok': False,
                    'already_claimed': True,
                    'post_id': post['post_id'],
                    'claimed_by': cur_claimed,
                    'claimed_name': post.get('claimed_name', ''),
                    'lease_remaining_sec': int(lease_until - now),
                }
            lease_sec = max(60, min(7200, int(args.get('lease_seconds') or self.DEFAULT_LEASE_SEC)))
            post['status'] = 'claimed'
            post['claimed_by'] = caller_bot_id
            post['claimed_name'] = caller_name
            post['claimed_node'] = caller_src
            post['lease_until'] = now + lease_sec
            post['unread_by'] = [u for u in (post.get('unread_by') or []) if u != caller_bot_id]
            post['updated_at'] = now
            self.store.save()
            await self.brain.publish({'type': 'blackboard_updated', 'op': 'claim', 'post': post})
            return {'ok': True, 'post_id': post['post_id'], 'post': post, 'topic': post, 'status': 'claimed', 'claimed_by': caller_bot_id, 'lease_seconds': lease_sec}

        # 5. `complete` / `deliver`: mark done OR enter opt-in Maker-Checker review if `review_by` was explicitly set
        if action in ('complete', 'deliver', 'done', 'resolve'):
            post = self._find_post(args.get('post_id') or args.get('topic_id') or args.get('key'))
            if not post:
                return {'ok': False, 'error': 'complete 需要有效的 post_id'}
            summary = str(args.get('summary') or args.get('text') or args.get('content') or args.get('value') or '').strip()
            art = str(args.get('artifact_path') or '').strip()
            if art:
                post['artifact_path'] = art
            if args.get('next_action'):
                post['next_action'] = str(args.get('next_action')).strip()[:400]
            if summary:
                post.setdefault('replies', []).append({
                    'reply_id': 'r-' + secrets.token_hex(3),
                    'author_bot_id': caller_bot_id,
                    'author_name': caller_name,
                    'author_node': caller_src,
                    'text': f'✅ 交付总结：{summary[:4000]}',
                    'artifact_path': art,
                    'mentions': [],
                    'created_at': now,
                    'created_iso': now_iso(),
                })
            review_by = str(post.get('review_by') or '').strip()
            woken: list[str] = []
            if review_by and caller_bot_id not in review_by:
                post['status'] = 'review_required'
                rev_targets = self._resolve_targets([review_by], caller_bot_id, caller_src)
                post['unread_by'] = [t[0] for t in rev_targets]
                woken = self._schedule_wakeup(
                    post,
                    rev_targets,
                    reason_label='黑板审校请求',
                    sender_bot_id=caller_bot_id,
                    sender_name=caller_name,
                    snippet_text=summary or post.get('title', ''),
                )
            else:
                post['status'] = 'done'
                post['completed_at'] = now
                post['unread_by'] = []
                # Optionally notify original author if another Bot completed their delegated post
                orig_author = str(post.get('author_bot_id') or '')
                orig_node = normalize_node_source(post.get('author_node') or caller_src)
                if orig_author and orig_author != caller_bot_id and bool(args.get('notify_author', True)):
                    try:
                        ob = get_assistant(self.store, orig_author)
                        woken = self._schedule_wakeup(
                            post,
                            [(orig_author, str(ob.get('name') or orig_author), orig_node)],
                            reason_label='黑板任务结项回执',
                            sender_bot_id=caller_bot_id,
                            sender_name=caller_name,
                            snippet_text=summary or '任务已结项',
                        )
                    except Exception:
                        pass
            post['lease_until'] = 0.0
            post['updated_at'] = now
            self.store.save()
            await self.brain.publish({'type': 'blackboard_updated', 'op': 'complete', 'post': post})
            return {'ok': True, 'post_id': post['post_id'], 'post': post, 'topic': post, 'status': post['status'], 'artifact_path': post.get('artifact_path', ''), 'woken': woken}

        # 6. `review` / `approve` / `reject`: Maker-Checker verdict (`approved` | `rejected`)
        if action in ('review', 'approve', 'reject'):
            post = self._find_post(args.get('post_id') or args.get('topic_id') or args.get('key'))
            if not post:
                return {'ok': False, 'error': 'review 需要有效的 post_id'}
            verdict = str(args.get('verdict') or ('rejected' if action == 'reject' else 'approved')).strip().lower()
            feedback = str(args.get('text') or args.get('content') or args.get('summary') or args.get('feedback') or '').strip()
            approved = verdict in ('approved', 'approve', 'pass', 'ok', '通过')
            post.setdefault('replies', []).append({
                'reply_id': 'r-' + secrets.token_hex(3),
                'author_bot_id': caller_bot_id,
                'author_name': caller_name,
                'author_node': caller_src,
                'text': f"{'🟢 审校通过' if approved else '🟠 审校退回修改'}：{feedback or verdict}",
                'artifact_path': '',
                'mentions': [],
                'created_at': now,
                'created_iso': now_iso(),
            })
            woken: list[str] = []
            if approved:
                post['status'] = 'done'
                post['completed_at'] = now
                post['unread_by'] = []
            else:
                post['status'] = 'claimed' if post.get('claimed_by') else 'open'
                assignee = str(post.get('claimed_by') or post.get('author_bot_id') or '')
                assignee_node = normalize_node_source(post.get('claimed_node') or post.get('author_node') or caller_src)
                if assignee and assignee != caller_bot_id:
                    post['unread_by'] = [assignee]
                    try:
                        ab = get_assistant(self.store, assignee)
                        woken = self._schedule_wakeup(
                            post,
                            [(assignee, str(ab.get('name') or assignee), assignee_node)],
                            reason_label='黑板审校退回提醒',
                            sender_bot_id=caller_bot_id,
                            sender_name=caller_name,
                            snippet_text=feedback or verdict,
                        )
                    except Exception:
                        pass
            post['updated_at'] = now
            self.store.save()
            await self.brain.publish({'type': 'blackboard_updated', 'op': 'review', 'post': post})
            return {'ok': True, 'post_id': post['post_id'], 'post': post, 'topic': post, 'status': post['status'], 'verdict': 'approved' if approved else 'rejected', 'woken': woken}

        if action == 'cancel':
            was_sched = False
            post = self._find_post(args.get('post_id') or args.get('topic_id') or args.get('key'))
            if not post:
                q_txt = str(args.get('title') or args.get('query') or args.get('goal') or args.get('text') or '').strip().lower()
                if q_txt:
                    for cand in reversed(self._posts()):
                        if not isinstance(cand, dict):
                            continue
                        if cand.get('status') not in ('scheduled', 'open', 'claimed', 'review_required'):
                            continue
                        hay = f"{cand.get('title') or ''} {cand.get('text') or ''}".lower()
                        if q_txt in hay:
                            post = cand
                            break
            if not post:
                return {'ok': False, 'error': 'cancel 需要有效的 post_id 或匹配的 title/query'}
            was_sched = post.get('status') == 'scheduled'
            post['status'] = 'cancelled'
            post['unread_by'] = []
            post['updated_at'] = now
            self.store.save()
            await self.brain.publish({'type': 'blackboard_updated', 'op': 'cancel', 'post': post})
            if was_sched:
                se = getattr(self.brain, 'sync_engine', None)
                if se is not None:
                    await se.notify_schedules_changed(kind='blackboard', task_id=post['post_id'], force=True)
            return {'ok': True, 'post_id': post['post_id'], 'post': post, 'topic': post, 'status': 'cancelled'}

        # 7. `read`: read single post, legacy KV key, OR smart inbox digest for calling Bot when called with no args!
        if action == 'read':
            raw_pid = str(args.get('post_id') or args.get('topic_id') or '').strip()
            raw_key = str(args.get('key') or '').strip()
            lookup = raw_pid or (raw_key if raw_key.lstrip('#').startswith('bb-') or raw_key.lstrip('#').isdigit() else '')
            if lookup:
                post = self._find_post(lookup)
                if not post:
                    return {'ok': False, 'error': f'未找到黑板帖子 {lookup}'}
                if caller_bot_id in (post.get('unread_by') or []):
                    post['unread_by'] = [u for u in post['unread_by'] if u != caller_bot_id]
                    self.store.save()
                return {'ok': True, 'post': post, 'topic': post}
            if raw_key:
                return self.brain.context_search.blackboard_read(raw_key)
            # Smart scan: unread mentions for caller_bot_id + active open/claimed/scheduled posts
            posts = self._posts()
            unread_posts = []
            open_posts = []
            dirty = False
            for p in reversed(posts):
                if not isinstance(p, dict):
                    continue
                if caller_bot_id in (p.get('unread_by') or []):
                    unread_posts.append(dict(p))
                    p['unread_by'] = [u for u in p['unread_by'] if u != caller_bot_id]
                    dirty = True
                elif p.get('status') in ('open', 'claimed', 'review_required', 'scheduled') and len(open_posts) < 15:
                    open_posts.append(dict(p))
            if dirty:
                self.store.save()
            kv_res = self.brain.context_search.blackboard_list()
            return {
                'ok': True,
                'caller_bot_id': caller_bot_id,
                'unread_mentions': unread_posts,
                'active_posts': open_posts,
                'kv_entries': kv_res.get('entries', []),
            }

        # 8. Default `list`
        status_filter = str(args.get('status') or '').strip().lower()
        posts = [
            p for p in reversed(self._posts())
            if isinstance(p, dict) and (not status_filter or p.get('status') == status_filter)
        ][:30]
        kv_res = self.brain.context_search.blackboard_list()
        return {
            'ok': True,
            'posts': posts,
            'total_posts': len(self._posts()),
            'entries': kv_res.get('entries', []),
        }

    async def tick(self) -> int:
        """Zero-LLM-token background tick for Blackboard timers, alarms, and watchpoints."""
        now = time.time()
        fired_count = 0
        async with self._post_lock:
            dirty = self._expire_stale_leases(now)
            posts = list(self._posts())
            due_batch: list[tuple[dict, str]] = []
            for post in posts:
                if not isinstance(post, dict) or post.get('status') != 'scheduled':
                    continue
                pid = str(post.get('post_id') or '')
                due_at = float(post.get('due_at') or post.get('trigger_at') or 0.0)
                watch = post.get('watch') if isinstance(post.get('watch'), dict) else None
                matched_watch_evidence = ''
                is_ready = False
                if watch:
                    interval = max(3.0, float(watch.get('interval_sec') or 10.0))
                    if now - self._watch_last_check.get(pid, 0.0) < interval:
                        continue
                    self._watch_last_check[pid] = now
                    ok_w, ev_w = await self._check_watchpoint(watch)
                    if ok_w:
                        is_ready, matched_watch_evidence = True, ev_w
                elif due_at > 0 and now >= due_at:
                    from core.curiosity_engine import (
                        curiosity_will_coalesce_soon,
                        is_coalesceable_blackboard_for_curiosity,
                    )
                    p_bot = str(post.get('target_bot_id') or post.get('author_bot_id') or 'companion')
                    if (
                        is_coalesceable_blackboard_for_curiosity(post)
                        and (now - due_at) <= 180.0
                        and curiosity_will_coalesce_soon(
                            self.store,
                            getattr(self.brain, 'proactive_gate', None),
                            getattr(self.brain, 'scheduler_engine', None),
                            p_bot,
                            now,
                        )
                    ):
                        continue
                    is_ready = True
                if is_ready:
                    due_batch.append((post, matched_watch_evidence))
            for post, matched_ev in due_batch:
                fired_count += 1
                dirty = True
                await self._fire_due_post(post, matched_ev, now)
            if dirty:
                self.store.save()
        if fired_count > 0:
            se = getattr(self.brain, 'sync_engine', None)
            if se is not None and hasattr(se, 'trigger_schedules_changed'):
                se.trigger_schedules_changed(kind='blackboard', force=False)
        return fired_count

    async def _check_watchpoint(self, watch: dict) -> tuple[bool, str]:
        cw = getattr(self.brain, 'clock_wheel', None)
        return await cw.check_watchpoint(watch) if cw is not None else (False, '')

    def peek_bots(self, args: dict | None = None) -> dict:
        """Stateless, lock-free read-only snapshot of all Bots' live progress, recent messages, and active Blackboard items."""
        from core.blackboard_peek import peek_bots_snapshot
        return peek_bots_snapshot(self, args)
