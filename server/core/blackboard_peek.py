"""Read-only Blackboard peer & subtask snapshot helper (`peek_bots_snapshot`)."""
from __future__ import annotations

import json
from typing import Any

from assistants import resolve_bot_for_source
from storage import node_display_name, normalize_node_source


def _format_trace_step(step: dict[str, Any]) -> dict[str, Any]:
    tool_name = str(step.get('tool') or '').strip()
    raw_args = step.get('args')
    args_s = json.dumps(raw_args, ensure_ascii=False)[:160] if isinstance(raw_args, (dict, list)) else str(raw_args or '')[:160]
    raw_res = step.get('result')
    err = ''
    res_s = ''
    ok = True
    if isinstance(raw_res, dict):
        err = str(raw_res.get('error') or '').strip()[:260]
        ok = bool(raw_res.get('ok', not bool(err)))
        if not err:
            res_s = json.dumps(raw_res, ensure_ascii=False)[:200]
    elif raw_res is not None:
        res_s = str(raw_res)[:200]
    if str(step.get('status') or '').lower() == 'error':
        ok = False
        if not err:
            err = res_s or 'tool_error'
    return {
        'tool': tool_name,
        'args': args_s,
        'ok': ok,
        'result': res_s,
        'error': err,
    }


def peek_bots_snapshot(engine, args: dict | None = None) -> dict:
    """Stateless, lock-free read-only snapshot of Bots AND async Subtasks (latest 3 steps + output + errors)."""
    args = args or {}
    q = str(args.get('target') or args.get('bot_id') or 'all').strip().lstrip('@')
    limit = max(1, min(15, int(args.get('limit') or 5)))
    all_msgs = engine.store.state.get('messages') or []
    posts = engine._posts()
    assistants = [b for b in engine.store.state.get('assistants', []) if isinstance(b, dict) and b.get('id')]

    if q and q.lower() not in ('all', 'everyone', '*', '全部', '全体'):
        q_low = q.lower()
        assistants = [
            b for b in assistants
            if str(b.get('id') or '').lower() == q_low
            or str(b.get('name') or '').strip().lower() == q_low
            or q_low in str(b.get('name') or '').strip().lower()
        ]

    snapshots: list[dict] = []
    for b in assistants:
        bid = str(b['id'])
        bname = str(b.get('name') or bid)
        home_node = str(b.get('home_node') or 'phone')
        running_nodes: list[str] = []
        cur_tool = ''
        cur_steps = 0
        stream_snippet = ''
        thought_snippet = ''
        live_trace: list[dict[str, Any]] = []
        for nk in ('phone', 'pc', 'phone2', 'pc2'):
            nb = f'{nk}:{bid}'
            ex = engine.brain.executions_by_node_bot.get(nb)
            st = engine.brain.streams_by_node_bot.get(nb)
            if ex or st or engine.brain.is_node_bot_busy(bid, nk):
                running_nodes.append(nk)
                if isinstance(ex, dict):
                    cur_tool = cur_tool or str(ex.get('tool') or '')
                    tr = ex.get('trace') or []
                    cur_steps = max(cur_steps, len(tr))
                    if tr and not live_trace:
                        live_trace = [t for t in tr if isinstance(t, dict)]
                if isinstance(st, dict):
                    stream_snippet = stream_snippet or str(st.get('text') or '')[:260]
                    thought_snippet = thought_snippet or str(st.get('thought') or '')[-220:]

        bot_msgs = [
            m for m in reversed(all_msgs)
            if isinstance(m, dict) and m.get('bot_id', 'companion') == bid and m.get('role') in ('user', 'assistant')
        ][:limit]
        bot_msgs.reverse()
        if not live_trace:
            for m in reversed(bot_msgs):
                if m.get('role') == 'assistant' and isinstance(m.get('trace'), list) and m['trace']:
                    live_trace = [t for t in m['trace'] if isinstance(t, dict)]
                    break
        recent_steps = [_format_trace_step(t) for t in live_trace[-3:]]
        last_err = next((s['error'] for s in reversed(recent_steps) if s.get('error')), '')

        recent_summary = [{
            'role': m.get('role'),
            'source': normalize_node_source(m.get('source')),
            'text': str(m.get('text') or '').strip()[:240],
            'tools_used': [t.get('tool') for t in (m.get('trace') or []) if isinstance(t, dict) and t.get('tool')][:6],
            'stopped_early': bool(m.get('stopped_early', False)),
            'created_at': m.get('created_at') or m.get('created'),
        } for m in bot_msgs]
        open_items = [{
            'post_id': p.get('post_id'),
            'title': p.get('title'),
            'status': p.get('status'),
            'claimed_by': p.get('claimed_by'),
            'artifact_path': p.get('artifact_path', ''),
        } for p in reversed(posts) if isinstance(p, dict)
            and p.get('status') in ('open', 'claimed', 'review_required', 'scheduled')
            and (p.get('claimed_by') == bid or bid in (p.get('target_bot_ids') or []) or p.get('author_bot_id') == bid)
        ][:8]
        eff_bot = resolve_bot_for_source(b, home_node if home_node in ('phone', 'pc') else 'phone')
        snapshots.append({
            'id': bid,
            'bot_id': bid,
            'name': bname,
            'home_node': home_node,
            'home_node_label': node_display_name(home_node if home_node in ('phone', 'pc', 'phone2', 'pc2') else 'phone'),
            'status': 'running' if running_nodes else 'idle',
            'running_nodes': running_nodes,
            'current_tool': cur_tool or None,
            'steps_executed': cur_steps,
            'recent_steps': recent_steps,
            'last_error': last_err,
            'live_stream_snippet': stream_snippet,
            'live_thought_snippet': thought_snippet,
            'model': eff_bot.get('model') or engine.store.config.effective_model(big=b.get('engine') == 'big'),
            'recent_messages': recent_summary,
            'open_blackboard_items': open_items,
        })

    # Unified same-source subtask snapshots (latest 3 steps + output + thought + errors)
    subtask_snaps: list[dict[str, Any]] = []
    sub_store = getattr(engine.brain, 'subtask_store', None)
    if sub_store is not None:
        try:
            runs = sub_store.list_runs(limit=12, recent_steps_limit=3)
            if q and q.lower() not in ('all', 'everyone', '*', '全部', '全体'):
                q_low = q.lower()
                runs = [
                    r for r in runs
                    if q_low in str(r.get('task_id') or '').lower()
                    or q_low in str(r.get('title') or '').lower()
                    or q_low == str(r.get('parent_bot_id') or '').lower()
                ]
            for r in runs:
                subtask_snaps.append({
                    'task_id': r['task_id'],
                    'title': r['title'],
                    'preset': r['preset'],
                    'parent_bot_id': r['parent_bot_id'],
                    'parent_source': r['parent_source'],
                    'status': r['status'],
                    'elapsed_sec': r['elapsed_sec'],
                    'progress_pct': r['progress_pct'],
                    'stage_idx': r['stage_idx'],
                    'stage_total': r['stage_total'],
                    'stage_label': r['stage_label'],
                    'sub_done': r['sub_done'],
                    'sub_total': r['sub_total'],
                    'steps_executed': r['steps_executed'],
                    'max_steps': r['max_steps'],
                    'recent_steps': r['recent_steps'][-3:],
                    'last_output': r['last_output'][:2000],
                    'live_thought': r['live_thought'][-1000:],
                    'last_error': r['last_error'][:1000],
                    'backup_id': r['backup_id'],
                })
        except Exception:
            pass

    return {
        'ok': True,
        'count': len(snapshots),
        'bots': snapshots,
        'subtasks_count': len(subtask_snaps),
        'subtasks': subtask_snaps,
        'pc_dsh_online': bool(engine.brain.is_dsh_online()) if hasattr(engine.brain, 'is_dsh_online') else False,
        'pc_dsh_sessions': engine.brain._get_dsh_sessions_list()[:8] if hasattr(engine.brain, '_get_dsh_sessions_list') else [],
    }
