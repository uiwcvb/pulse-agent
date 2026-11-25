"""Scheduler, due-time parsing, mention routing, and due-post firing helpers for `BlackboardEngine`."""
from __future__ import annotations

import datetime as dt
import re
import secrets
from typing import Any

from assistants import get_assistant
from storage import normalize_node_source
from vault_manager import TZ, now_iso

MENTION_TOKEN_RE = re.compile(r'(?<![A-Za-z0-9_.\-/|>])@([A-Za-z0-9_\-\u4e00-\u9fa5:：]{1,32})')


def parse_weekdays(spec: str) -> set[int]:
    days: set[int] = set()
    for part in re.split(r'[,，\s]+', spec.strip()):
        if '-' in part:
            a, b = part.split('-', 1)
            if a.isdigit() and b.isdigit():
                for d in range(int(a), int(b) + 1):
                    if 1 <= d <= 7:
                        days.add(d)
        elif part.isdigit() and 1 <= int(part) <= 7:
            days.add(int(part))
    return days or {1, 2, 3, 4, 5, 6, 7}


def _parse_cron_field(field: str, min_v: int, max_v: int, *, dow: bool = False) -> set[int]:
    vals: set[int] = set()
    for part in (field or '').strip().split(','):
        part = part.strip()
        if not part:
            raise ValueError(f'Cron 字段不能为空: {field}')
        step = 1
        if '/' in part:
            base, step_s = part.split('/', 1)
            if not step_s.isdigit() or int(step_s) <= 0:
                raise ValueError(f'Cron 步长非法: {part}')
            step = int(step_s)
        else:
            base = part
        if base == '*':
            lo, hi = min_v, max_v
        elif '-' in base:
            a_s, b_s = base.split('-', 1)
            if not (a_s.isdigit() and b_s.isdigit()):
                raise ValueError(f'Cron 区间非法: {part}')
            lo, hi = int(a_s), int(b_s)
        elif base.isdigit():
            lo = hi = int(base)
        else:
            raise ValueError(f'Cron 值非法: {part}')
        for v in range(lo, hi + 1, step):
            norm_v = 0 if (dow and v == 7) else v
            if min_v <= norm_v <= max_v:
                vals.add(norm_v)
    if not vals:
        raise ValueError(f'Cron 字段无匹配值: {field}')
    return vals


def next_cron_timestamp(cron_expr: str, now: float) -> float:
    """Compute the next Asia/Shanghai timestamp for a standard 5-field cron expression."""
    parts = (cron_expr or '').strip().split()
    if len(parts) != 5:
        raise ValueError(f'cron_expr 必须为 5 段 (分 时 日 月 周)，收到: {cron_expr!r}')
    mins = _parse_cron_field(parts[0], 0, 59)
    hrs = _parse_cron_field(parts[1], 0, 23)
    doms = _parse_cron_field(parts[2], 1, 31)
    mons = _parse_cron_field(parts[3], 1, 12)
    dows = _parse_cron_field(parts[4], 0, 7, dow=True)
    dom_star = parts[2].strip() == '*'
    dow_star = parts[4].strip() == '*'

    cur = dt.datetime.fromtimestamp(now, TZ).replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    end_limit = cur + dt.timedelta(days=366)
    while cur <= end_limit:
        if cur.month not in mons:
            # Advance to 1st of next month 00:00
            if cur.month == 12:
                cur = cur.replace(year=cur.year + 1, month=1, day=1, hour=0, minute=0)
            else:
                cur = cur.replace(month=cur.month + 1, day=1, hour=0, minute=0)
            continue
        py_dow_sun0 = (cur.weekday() + 1) % 7
        dom_match = cur.day in doms
        dow_match = py_dow_sun0 in dows
        day_ok = (dom_match and dow_match) if (dom_star or dow_star) else (dom_match or dow_match)
        if not day_ok:
            cur = (cur + dt.timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if cur.hour not in hrs:
            cur = (cur + dt.timedelta(hours=1)).replace(minute=0)
            continue
        if cur.minute in mins:
            return cur.timestamp()
        cur += dt.timedelta(minutes=1)
    raise ValueError(f'cron_expr 在一年内无可用触发时间: {cron_expr!r}')


def parse_due_timestamp(args: dict, now: float) -> tuple[float, dict]:
    """Parse optional timer/alarm scheduling fields (`when`, `run_at`, `delay_seconds`, `delay_minutes`, `at_time`, `daily_time`, `interval_hours`, `cron_expr`)."""
    sched_meta: dict[str, Any] = {}
    cron_raw = str(args.get('cron_expr') or '').strip()
    if cron_raw:
        due_cron = next_cron_timestamp(cron_raw, now)
        sched_meta['kind'] = 'routine'
        sched_meta['cron_expr'] = cron_raw
        return due_cron, sched_meta

    when_field = next((k for k in ('when', 'run_at', 'at') if args.get(k) not in (None, '')), None)
    when_raw = str(args.get(when_field) or '').strip() if when_field else ''
    eff_args = dict(args)
    has_other_sched = any(args.get(k) not in (None, '', 0) for k in ('delay_seconds', 'delay_minutes', 'at_time', 'daily_time', 'interval_hours'))
    if when_raw and not has_other_sched:
        wl = when_raw.lower().strip()
        m_rel = re.fullmatch(
            r'(?:in\s+)?(\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds|秒|m|min|mins|minute|minutes|分|分钟|h|hr|hrs|hour|hours|小时)(?:后)?',
            wl,
        )
        m_daily = re.fullmatch(r'(?:daily|everyday|每天|每日)\s*(\d{1,2}:\d{2})', wl)
        if m_rel:
            val, unit = float(m_rel.group(1)), m_rel.group(2)
            if unit.startswith(('s', '秒')):
                eff_args['delay_seconds'] = val
            elif unit.startswith(('h', '小')):
                eff_args['delay_minutes'] = val * 60.0
            else:
                eff_args['delay_minutes'] = val
        elif m_daily:
            eff_args['daily_time'] = m_daily.group(1)
        elif re.fullmatch(r'\d{1,2}:\d{2}', wl):
            eff_args['at_time'] = wl
        else:
            s_iso = when_raw.replace('/', '-')
            if re.match(r'^\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}', s_iso):
                s_iso = re.sub(r'\s+', 'T', s_iso, count=1)
            if s_iso.endswith('Z'):
                s_iso = s_iso[:-1] + '+00:00'
            try:
                dt_parsed = dt.datetime.fromisoformat(s_iso)
            except Exception as exc:
                raise ValueError(
                    f"无法解析定时时间 {when_field}={when_raw!r}（支持 ISO8601、'YYYY-MM-DD HH:MM'、'HH:MM'、'30m' 或 delay_minutes）: {exc}"
                ) from exc
            if dt_parsed.tzinfo is None:
                dt_parsed = dt_parsed.replace(tzinfo=TZ)
            ts = dt_parsed.timestamp()
            if ts <= now:
                raise ValueError(f"定时时间 {when_field}={when_raw!r} 早于或等于当前时间，请提供未来时间")
            sched_meta['kind'] = 'alarm'
            sched_meta['when'] = when_raw
            return ts, sched_meta

    for num_k in ('delay_seconds', 'delay_minutes', 'interval_hours'):
        raw_num = eff_args.get(num_k)
        if raw_num not in (None, ''):
            try:
                val_f = float(raw_num)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"定时参数 {num_k}={raw_num!r} 必须为正数") from exc
            if val_f < 0:
                raise ValueError(f"定时参数 {num_k}={raw_num!r} 不能为负数")

    delay_sec = float(eff_args.get('delay_seconds') or 0)
    delay_min = float(eff_args.get('delay_minutes') or 0)
    interval_h = float(eff_args.get('interval_hours') or 0)
    at_time = str(eff_args.get('at_time') or '').strip()
    daily_time = str(eff_args.get('daily_time') or '').strip()
    weekdays = str(eff_args.get('weekdays') or '').strip()

    if delay_sec > 0 or delay_min > 0:
        sched_meta['kind'] = 'timer'
        return now + max(0.01, delay_sec + delay_min * 60.0), sched_meta

    clock_field = 'daily_time' if daily_time else ('at_time' if at_time else '')
    clock_str = daily_time or at_time
    if clock_str:
        m_hm = re.match(r'^(\d{1,2}):(\d{2})(?::(\d{2}))?$', clock_str)
        if not m_hm:
            raise ValueError(f"无法解析时间格式 {clock_field}={clock_str!r}（需要 'HH:MM'）")
        hh, mm, ss = int(m_hm.group(1)), int(m_hm.group(2)), int(m_hm.group(3) or 0)
        if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
            raise ValueError(f"时间超出范围 {clock_field}={clock_str!r}")
        cand = dt.datetime.fromtimestamp(now, TZ).replace(hour=hh, minute=mm, second=ss, microsecond=0)
        if cand.timestamp() <= now + 1.0:
            cand += dt.timedelta(days=1)
        if weekdays:
            allowed_days = parse_weekdays(weekdays)
            for _ in range(8):
                if cand.isoweekday() in allowed_days:
                    break
                cand += dt.timedelta(days=1)
        sched_meta['kind'] = 'routine' if daily_time else 'alarm'
        if daily_time:
            sched_meta['daily_time'] = f'{hh:02d}:{mm:02d}'
        if weekdays:
            sched_meta['weekdays'] = weekdays
        return cand.timestamp(), sched_meta

    if interval_h > 0:
        sched_meta['kind'] = 'routine'
        sched_meta['interval_hours'] = interval_h
        return now + interval_h * 3600.0, sched_meta

    if any(eff_args.get(k) not in (None, '') for k in ('delay_seconds', 'delay_minutes', 'interval_hours')):
        raise ValueError("定时延迟参数必须大于 0")

    return 0.0, sched_meta


def expire_stale_leases(posts: list[dict], now: float) -> bool:
    changed = False
    for p in posts:
        if not isinstance(p, dict):
            continue
        if p.get('status') == 'claimed' and float(p.get('lease_until') or 0) > 0 and now > float(p['lease_until']):
            p['status'] = 'open'
            p['expired_claim_by'] = p.get('claimed_by', '')
            p['claimed_by'] = ''
            p['claimed_name'] = ''
            p['lease_until'] = 0.0
            changed = True
    return changed


def extract_mentions(args: dict, text: str) -> list[str]:
    raw_list: list[str] = []
    m_arg = args.get('mentions') or args.get('at') or args.get('to')
    if isinstance(m_arg, list):
        raw_list.extend(str(x).strip() for x in m_arg if str(x).strip())
    elif isinstance(m_arg, str) and m_arg.strip():
        for part in re.split(r'[,，;\s]+', m_arg.strip()):
            if part.strip():
                raw_list.append(part.strip())
    for m in MENTION_TOKEN_RE.finditer(text or ''):
        tok = m.group(1).strip()
        if not tok or tok.isdigit() or re.match(r'^[be]\d+$', tok, re.IGNORECASE) or tok in ('你', '您', '他', '她', '它', '大家'):
            continue
        raw_list.append(f'@{tok}')
    normed: list[str] = []
    for item in raw_list:
        clean = item if item.startswith('@') else f'@{item}'
        if clean not in normed:
            normed.append(clean)
    return normed


def resolve_targets(
    store: Any,
    mentions: list[str],
    author_bot_id: str,
    author_src: str,
    *,
    allow_self_explicit: bool = False,
) -> list[tuple[str, str, str]]:
    """Resolve `@mention` tokens into `[(bot_id, bot_name, target_source)]`."""
    assistants = [b for b in store.state.get('assistants', []) if isinstance(b, dict) and b.get('id')]
    targets: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    norm_author_src = normalize_node_source(author_src)

    def _home_src(b: dict, explicit_src: str | None = None) -> str:
        if explicit_src:
            return normalize_node_source(explicit_src)
        hn = str(b.get('home_node') or '').strip().lower()
        return hn if hn in ('pc', 'pc2', 'phone', 'phone2') else norm_author_src

    def _add_bot(b: dict, exp_src: str | None = None, allow_self: bool = False):
        tsrc = _home_src(b, exp_src)
        bid = str(b['id'])
        if not allow_self and bid == author_bot_id and tsrc == norm_author_src:
            return
        if (bid, tsrc) not in seen:
            seen.add((bid, tsrc))
            targets.append((bid, str(b.get('name') or bid), tsrc))

    for raw in mentions:
        tok = raw.lstrip('@').strip()
        if not tok:
            continue
        low = tok.lower()
        if low in ('everyone', 'all', '全体', '所有人', '全员'):
            for b in assistants:
                if b['id'] != author_bot_id and not b.get('hidden'):
                    _add_bot(b)
            continue
        if low in ('self', 'me', '自己', '我'):
            try:
                _add_bot(get_assistant(store, author_bot_id), norm_author_src, allow_self=True)
            except Exception:
                pass
            continue
        exp_src: str | None = None
        bot_tok = tok
        if ':' in tok or '：' in tok:
            left, right = re.split(r'[:：]', tok, maxsplit=1)
            exp_src = normalize_node_source(left)
            bot_tok = right.strip()
        elif low in ('电脑端', '电脑', '桌面端', 'windows一', 'windows1', 'pc'):
            exp_src, bot_tok = 'pc', 'pc_engineer'
        elif low in ('手机端', '手机', '安卓一', '安卓1', 'phone'):
            exp_src, bot_tok = 'phone', str(store.state.get('primary_bot_id') or 'companion')
        matched = next(
            (b for b in assistants if str(b.get('id') or '').lower() == bot_tok.lower() or str(b.get('name') or '').strip().lower() == bot_tok.lower()),
            None,
        )
        if matched is None and exp_src == 'pc':
            matched = next((b for b in assistants if str(b.get('home_node') or '') == 'pc'), None) or (assistants[0] if assistants else None)
        elif matched is None and exp_src == 'phone':
            matched = next((b for b in assistants if str(b.get('home_node') or 'phone') == 'phone'), None)
        if matched is not None:
            _add_bot(matched, exp_src, allow_self=allow_self_explicit)
    return targets


async def fire_due_post(engine: Any, post: dict, matched_watch_evidence: str, now: float) -> None:
    """Update a due/matched post (including legacy `trigger_at`/`content` fallbacks) and dispatch wakeup."""
    watch = post.get('watch') if isinstance(post.get('watch'), dict) else None
    if not post.get('text') and post.get('content'):
        post['text'] = str(post.get('content') or '')
    if not post.get('due_at') and post.get('trigger_at'):
        post['due_at'] = float(post.get('trigger_at') or 0.0)
    if matched_watch_evidence:
        if isinstance(watch, dict):
            watch['triggered'] = True
        post['watch_evidence'] = matched_watch_evidence[:600]
        post.setdefault('replies', []).append({
            'reply_id': 'r-' + secrets.token_hex(3),
            'author_bot_id': 'system',
            'author_name': '黑板监视器',
            'author_node': post.get('author_node', 'phone'),
            'text': f'🎯 零算力条件监视命中：{matched_watch_evidence[:500]}',
            'artifact_path': '',
            'mentions': post.get('mentions', []),
            'created_at': now,
            'created_iso': now_iso(),
        })

    sched = post.get('schedule') if isinstance(post.get('schedule'), dict) else {}
    if sched.get('cron_expr') or sched.get('daily_time') or sched.get('interval_hours'):
        next_due, _ = parse_due_timestamp(sched, now + 2.0)
        post['due_at'] = round(next_due, 3)
        post['due_iso'] = dt.datetime.fromtimestamp(next_due, TZ).isoformat(timespec='seconds')
        post['last_fired_at'] = now
    else:
        post['status'] = 'open'
        post['fired_at'] = now

    mentions = post.get('mentions') or ['@self']
    author_id = str(post.get('author_bot_id') or 'companion')
    author_src = normalize_node_source(post.get('author_node') or 'phone')
    targets = resolve_targets(engine.store, mentions, author_id, author_src, allow_self_explicit=True)
    if not targets:
        targets = resolve_targets(engine.store, ['@self'], author_id, author_src, allow_self_explicit=True)
    post['unread_by'] = [t[0] for t in targets]
    cw = getattr(engine.brain, 'clock_wheel', None)
    if cw is not None:
        await cw.deliver_blackboard_fire(engine, post, targets, matched_watch_evidence, now)
    else:
        label = '黑板监视命中' if matched_watch_evidence else '黑板闹钟到期'
        engine._schedule_wakeup(post, targets, reason_label=label, bypass_hop_limit=True)
