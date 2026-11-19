"""Windows PC DeepSeek Harness (`DSH`) Bridge & Multi-Device Peer Management."""
from __future__ import annotations

import asyncio
import os
import secrets
import time
from pathlib import Path


class DshBridgeMixin:
    def is_dsh_online(self, ws_clients_count: int = 0) -> bool:
        if ws_clients_count > 0:
            return True
        if not self.dsh_bridge_info.get('online'):
            return False
        last_seen = float(self.dsh_bridge_info.get('last_seen') or 0.0)
        return (time.time() - last_seen) < 90.0

    def update_dsh_peer(self, payload: dict | None = None) -> str:
        now_t = time.time()
        self.dsh_bridge_info['online'] = True
        self.dsh_bridge_info['last_seen'] = now_t
        if not isinstance(payload, dict):
            return str(self.dsh_bridge_info.get('active_device_id') or 'pc_dsh')

        st_in = payload.get('status') if isinstance(payload.get('status'), dict) else {}
        if not st_in and any(k in payload for k in ('dsh_running', 'sessions', 'workspaces', 'last_3_dialogs')):
            st_in = {
                k: v for k, v in payload.items()
                if k in ('dsh_running', 'sessions', 'workspaces', 'active_session_id', 'version', 'last_3_dialogs', 'last_active_time', 'sessions_count', 'running_count')
            }

        raw_dev_id = str(
            payload.get('device_id')
            or payload.get('peer_id')
            or payload.get('id')
            or st_in.get('device_id')
            or 'pc_dsh'
        ).strip() or 'pc_dsh'

        dev_name = str(
            payload.get('device_name')
            or payload.get('name')
            or st_in.get('device_name')
            or (f'{raw_dev_id} (电脑端 DSH)' if raw_dev_id != 'pc_dsh' else 'PC DeepSeek Harness (DSB)')
        ).strip()

        peers = self.dsh_bridge_info.setdefault('peers', {})
        existing = peers.get(raw_dev_id) if isinstance(peers.get(raw_dev_id), dict) else {}
        prev_status = existing.get('status') if isinstance(existing.get('status'), dict) else {}
        merged_status = {**prev_status, **st_in} if st_in else dict(prev_status)

        raw_sessions = merged_status.get('sessions')
        if isinstance(raw_sessions, list):
            tagged_sessions = []
            for s in raw_sessions:
                if isinstance(s, dict):
                    tagged_sessions.append({
                        **s,
                        'device_id': str(s.get('device_id') or raw_dev_id),
                        'device_name': str(s.get('device_name') or dev_name),
                        'device_type': 'pc',
                        'source': 'pc',
                    })
            merged_status['sessions'] = tagged_sessions
        else:
            tagged_sessions = list(existing.get('sessions') or [])

        if st_in:
            self.dsh_bridge_info['status'] = dict(merged_status)

        peer_entry = {
            **existing,
            'id': raw_dev_id,
            'peer_id': raw_dev_id,
            'device_id': raw_dev_id,
            'name': dev_name,
            'device_name': dev_name,
            'kind': 'pc',
            'device_type': 'pc',
            'source': 'pc',
            'online': True,
            'last_seen': now_t,
            'sessions': tagged_sessions,
            'sessions_count': int(merged_status.get('sessions_count') or len(tagged_sessions)),
            'running_count': int(merged_status.get('running_count') or sum(1 for s in tagged_sessions if s.get('status') == 'running')),
            'status': merged_status,
        }
        peers[raw_dev_id] = peer_entry
        if raw_dev_id != 'pc_dsh' or not self.dsh_bridge_info.get('active_device_id'):
            self.dsh_bridge_info['active_device_id'] = raw_dev_id
        return raw_dev_id

    def resolve_target_dsh_peer(self, device_id: str | None = None) -> tuple[str, dict | None]:
        peers_map = self.dsh_bridge_info.get('peers') or {}
        req = str(device_id or '').strip()
        if req and req.lower() not in ('auto', 'default', 'active', 'latest', 'pc', 'pc_dsh'):
            if req in peers_map and isinstance(peers_map[req], dict):
                return req, peers_map[req]
            low = req.lower()
            prefixed = low if low.startswith('pc-') else f'pc-{low}'
            for k, v in peers_map.items():
                if not isinstance(v, dict):
                    continue
                kl = str(k).lower()
                nm = str(v.get('device_name') or v.get('name') or '').lower()
                if kl == low or kl == prefixed or low in nm:
                    return str(k), v
            return req, None

        active_id = str(self.dsh_bridge_info.get('active_device_id') or '').strip()
        if active_id and active_id in peers_map and isinstance(peers_map[active_id], dict):
            return active_id, peers_map[active_id]

        specific_peers = [
            (k, v) for k, v in peers_map.items()
            if k not in ('pc_dsh', 'phone_dsb') and isinstance(v, dict)
        ]
        if specific_peers:
            specific_peers.sort(key=lambda item: float(item[1].get('last_seen') or 0.0), reverse=True)
            return specific_peers[0][0], specific_peers[0][1]

        if 'pc_dsh' in peers_map and isinstance(peers_map['pc_dsh'], dict):
            return 'pc_dsh', peers_map['pc_dsh']
        return 'pc_dsh', None

    def get_dsh_peers(self, ws_clients_count: int = 0, phone_clients_count: int = 0) -> list[dict]:
        dsh_on = self.is_dsh_online(ws_clients_count)
        last_seen = float(self.dsh_bridge_info.get('last_seen') or 0.0)
        peers_map = dict(self.dsh_bridge_info.get('peers') or {})
        pc_peer = {
            'id': 'pc_dsh',
            'peer_id': 'pc_dsh',
            'device_id': 'pc_dsh',
            'name': 'PC DeepSeek Harness (DSB)',
            'device_name': 'PC DeepSeek Harness (DSB)',
            'kind': 'pc',
            'device_type': 'pc',
            'source': 'pc',
            'online': dsh_on,
            'ws_connected': ws_clients_count > 0,
            'last_seen': last_seen,
            'status': self.dsh_bridge_info.get('status') or {},
        }
        if 'pc_dsh' in peers_map and isinstance(peers_map['pc_dsh'], dict):
            pc_peer = {**peers_map['pc_dsh'], **pc_peer}
        out = [
            pc_peer,
            {
                'id': 'phone_dsb',
                'peer_id': 'phone_dsb',
                'device_id': 'phone_dsb',
                'name': 'Phone DSB App',
                'device_name': 'Phone DSB App',
                'kind': 'phone',
                'device_type': 'phone',
                'source': 'phone',
                'online': phone_clients_count > 0,
                'clients': phone_clients_count,
            },
        ]
        now_t = time.time()
        for k, v in peers_map.items():
            if k not in ('pc_dsh', 'phone_dsb') and isinstance(v, dict):
                p_seen = float(v.get('last_seen') or 0.0)
                p_online = bool(v.get('online', True) and (now_t - p_seen) < 90.0)
                out.append({
                    **v,
                    'id': k,
                    'peer_id': k,
                    'device_id': k,
                    'kind': 'pc',
                    'device_type': 'pc',
                    'source': 'pc',
                    'online': p_online,
                })
        return out

    def resolve_dsh_command(self, event: dict):
        req_id = str(event.get('request_id') or '')
        self.update_dsh_peer(event)
        if req_id and isinstance(self.dsh_bridge_info.get('inbox'), list):
            self.dsh_bridge_info['inbox'] = [
                item for item in self.dsh_bridge_info['inbox']
                if str(item.get('request_id') or '') != req_id
            ]
        fut = self.dsh_pending.pop(req_id, None)
        if fut and not fut.done():
            fut.set_result(event.get('result') if isinstance(event.get('result'), dict) else {'ok': True, 'data': event.get('result')})

    async def _raw_dsh_rpc(self, op: str, args: dict, timeout: float = 30.0) -> dict:
        req_device_id = str(args.get('device_id') or '').strip()
        target_id, target_peer = self.resolve_target_dsh_peer(req_device_id)
        if req_device_id and req_device_id.lower() not in ('auto', 'default', 'active', 'latest', 'pc', 'pc_dsh') and target_peer is None:
            avail = [k for k in (self.dsh_bridge_info.get('peers') or {}).keys() if k != 'phone_dsb']
            return {
                'ok': False,
                'pc_online': False,
                'device_id': req_device_id,
                'available_devices': avail,
                'op': op,
                'error': f'未找到指定电脑设备: {req_device_id}',
            }

        peer_status = (target_peer.get('status') if isinstance(target_peer, dict) and isinstance(target_peer.get('status'), dict) else None) or self.dsh_bridge_info.get('status') or {}
        if self.dsh_dispatch and not self.is_dsh_online() and op not in ('status', 'list_sessions'):
            for _ in range(4):
                await asyncio.sleep(0.5)
                if self.is_dsh_online():
                    break
        if self.dsh_dispatch and self.is_dsh_online():
            if op == 'status' and not self.dsh_bridge_info.get('ws_connected') and peer_status:
                return {'ok': True, 'pc_online': True, 'device_id': target_id, **peer_status}
            if op == 'list_sessions' and not self.dsh_bridge_info.get('ws_connected') and isinstance(peer_status.get('sessions'), list):
                lim = max(1, min(200, int(args.get('limit') or 25)))
                sess_list = list(peer_status.get('sessions') or [])
                return {
                    'ok': True,
                    'pc_online': True,
                    'device_id': target_id,
                    'device_name': (target_peer or {}).get('device_name', target_id),
                    'total_sessions': len(sess_list),
                    'sessions': sess_list[:lim],
                }
            req_id = 'dsh-' + secrets.token_hex(6)
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            self.dsh_pending[req_id] = fut
            cmd_pkt = {
                'type': 'dsh_workspace_command',
                'request_id': req_id,
                'device_id': target_id,
                'peer_id': target_id,
                'bot_id': self.bot_context.get(),
                'op': op,
                'args': {**args, 'device_id': target_id},
                'timestamp': time.time(),
            }
            inbox = self.dsh_bridge_info.setdefault('inbox', [])
            inbox.append(cmd_pkt)
            if len(inbox) > 50:
                del inbox[:-50]
            try:
                await self.dsh_dispatch(cmd_pkt)
                res = await asyncio.wait_for(fut, timeout=timeout)
                return res if isinstance(res, dict) else {'ok': True}
            except asyncio.TimeoutError:
                self.dsh_pending.pop(req_id, None)
                if op == 'status' and self.is_dsh_online():
                    return {'ok': True, 'pc_online': True, 'device_id': target_id, **peer_status}
                if op == 'list_sessions' and isinstance(peer_status.get('sessions'), list):
                    lim = max(1, min(200, int(args.get('limit') or 25)))
                    sess_list = list(peer_status.get('sessions') or [])
                    return {
                        'ok': True,
                        'pc_online': True,
                        'device_id': target_id,
                        'total_sessions': len(sess_list),
                        'sessions': sess_list[:lim],
                    }
                return {'ok': False, 'timed_out': True, 'device_id': target_id, 'op': op, 'error': f'电脑端 DeepSeek Harness ({target_id}) 在 {int(timeout)}s 内未返回响应'}
            except Exception as e:
                self.dsh_pending.pop(req_id, None)
                return {'ok': False, 'device_id': target_id, 'op': op, 'error': f'发送指令至电脑端失败: {str(e)[:200]}'}

        plugin_dir = Path(__file__).resolve().parent.parent.parent / 'dsh-plugin'
        if not (plugin_dir / 'dsh_core.py').is_file() and Path(r'D:\dsh-baike-plugin\dsh_core.py').is_file():
            plugin_dir = Path(r'D:\dsh-baike-plugin')
        if (plugin_dir / 'dsh_core.py').is_file() and (
            (Path.home() / '.dsh').is_dir() or Path(r'D:\DeepSeek-Harness\data').is_dir() or os.getenv('DSH_DESKTOP_HOME')
        ):
            import sys as _sys
            if str(plugin_dir) not in _sys.path:
                _sys.path.insert(0, str(plugin_dir))
            from dsh_core import DSHController
            ctrl = DSHController()
            if op == 'status':
                return {'ok': True, 'pc_online': True, 'local_direct': True, **ctrl.status()}
            if op == 'list_sessions':
                return await asyncio.to_thread(
                    ctrl.list_sessions,
                    int(args.get('limit') or 25),
                    args.get('workspace'),
                    bool(args.get('include_blank', False)),
                    args.get('query'),
                )
            if op == 'inspect_session':
                return await asyncio.to_thread(
                    ctrl.inspect_session,
                    str(args.get('session_id') or 'latest'),
                    int(args.get('max_messages') or 12),
                    int(args.get('max_tools') or 15),
                    bool(args.get('include_reasoning', False)),
                )
            if op == 'wake_gui':
                return await asyncio.to_thread(ctrl.wake_gui)
            if op in ('send_message', 'new_session'):
                return await ctrl.control_session(
                    op=op,
                    message=str(args.get('message') or args.get('text') or ''),
                    session_id=args.get('session_id'),
                    cwd=args.get('cwd'),
                    timeout_sec=float(args.get('timeout_sec') or timeout),
                    wake_gui=bool(args.get('wake_gui', False)),
                )
        return {
            'ok': False,
            'pc_online': False,
            'device_id': target_id,
            'op': op,
            'error': '电脑端 DeepSeek Claw Sentinel 当前未在线连接（请确认电脑已开机并启动 DeepSeek-Claw-Sentinel）',
        }
