"""Read-only Blackboard peer snapshot helper (`peek_bots_snapshot`)."""
from __future__ import annotations

from assistants import resolve_bot_for_source
from storage import node_display_name, normalize_node_source


def peek_bots_snapshot(engine, args: dict | None = None) -> dict:
    """Stateless, lock-free read-only snapshot of all Bots' live progress, recent messages, and active Blackboard items."""
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
        for nk in ('phone', 'pc', 'phone2', 'pc2'):
            nb = f'{nk}:{bid}'
            ex = engine.brain.executions_by_node_bot.get(nb)
            st = engine.brain.streams_by_node_bot.get(nb)
            if ex or st or engine.brain.is_node_bot_busy(bid, nk):
                running_nodes.append(nk)
                if isinstance(ex, dict):
                    cur_tool = cur_tool or str(ex.get('tool') or '')
                    cur_steps = max(cur_steps, len(ex.get('trace') or []))
                if isinstance(st, dict):
                    stream_snippet = stream_snippet or str(st.get('text') or '')[:260]
                    thought_snippet = thought_snippet or str(st.get('thought') or '')[-220:]

        bot_msgs = [
            m for m in reversed(all_msgs)
            if isinstance(m, dict) and m.get('bot_id', 'companion') == bid and m.get('role') in ('user', 'assistant')
        ][:limit]
        bot_msgs.reverse()
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
            'live_stream_snippet': stream_snippet,
            'live_thought_snippet': thought_snippet,
            'model': eff_bot.get('model') or engine.store.config.effective_model(big=b.get('engine') == 'big'),
            'recent_messages': recent_summary,
            'open_blackboard_items': open_items,
        })
    return {
        'ok': True,
        'count': len(snapshots),
        'bots': snapshots,
        'pc_dsh_online': bool(engine.brain.is_dsh_online()) if hasattr(engine.brain, 'is_dsh_online') else False,
        'pc_dsh_sessions': engine.brain._get_dsh_sessions_list()[:8] if hasattr(engine.brain, '_get_dsh_sessions_list') else [],
    }
