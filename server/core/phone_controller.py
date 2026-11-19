"""Android Virtual Display Workspace Controller (`phone`).
Pure-vision 0..1000 normalized grounding + clean unoccluded screenshots via `image_url`.
"""
from __future__ import annotations

import asyncio
import base64
import re
import secrets
import time

from storage import normalize_node_source
from core.phone_ui_formatter import (
    exec_phone_pull_file,
    exec_phone_shell,
    exec_phone_tap_and_type,
    format_available_nodes_hint,
    match_phone_node,
    parse_phone_nodes,
    resolve_phone_grid_or_norm,
)
from core.phone_vision_helpers import (
    build_phone_swipe_args,
    finalize_phone_vision_and_diff,
    run_phone_macro_action,
    validate_norm_box_xyxy,
)
from core.vfs_router import (
    apply_authoritative_tree,
    normalize_single_tree_frame,
    reconcile_device_root_status,
)
from core.vision_engine import annotate_tap_crosshair, draw_vision_ruler_overlay, zoom_region_from_args


def _node_prop(attr: str, cast=None):
    return property(
        lambda self: getattr(self._phone_node_state(), attr),
        lambda self, val: setattr(self._phone_node_state(), attr, cast(val) if cast else val),
    )


class PhoneControllerMixin:
    def _phone_node_state(self, source: str | None = None):
        """Return isolated per-Android-device state (`phone` vs `phone2`) via PerNodePhoneRegistry."""
        src = normalize_node_source(source or self.source_context.get() or 'phone')
        if src not in ('phone', 'phone2'):
            src = 'phone'
        return self.phone_state_registry.get(src)

    _last_phone_tap = _node_prop('last_tap')
    _last_phone_size = _node_prop('last_size')
    _last_phone_pkg = _node_prop('last_pkg')
    _last_phone_node_count = _node_prop('last_node_count')
    _last_phone_tree_hash = _node_prop('last_tree_hash')
    _last_phone_nodes = _node_prop('last_nodes')
    _last_phone_display_id = _node_prop('display_id', int)
    _used_phone_in_turn = _node_prop('used_in_turn', bool)
    _vdisplay_currently_open = _node_prop('vdisplay_open', bool)
    _phone_to_foreground_in_turn = _node_prop('to_foreground_in_turn', bool)

    def resolve_phone_command(self, event: dict):
        req_id = str(event.get('request_id') or '')
        if not req_id:
            return
        res = event.get('result')
        ev_dev = str(
            event.get('device_id')
            or event.get('_client_device_id')
            or (res.get('device_id') if isinstance(res, dict) else '')
            or (res.get('model') if isinstance(res, dict) else '')
            or ''
        ).strip()
        trusted_dev = str((getattr(self, 'store', None) and self.store.state.get('trusted_phone_device_id')) or '').strip()
        if trusted_dev and ev_dev and ev_dev != trusted_dev:
            return
        fut = self.phone_pending.pop(req_id, None)
        if fut and not fut.done():
            if isinstance(res, dict) and res:
                fut.set_result(res)
            elif event.get('error'):
                fut.set_result({'ok': False, 'error': str(event['error'])})
            elif isinstance(res, dict):
                fut.set_result({'ok': False, 'error': '手机端返回空结果对象'})
            else:
                fut.set_result({'ok': False, 'error': '手机端返回空响应'})

    async def _raw_phone_rpc(self, op: str, args: dict, timeout: float = 32.0) -> dict:
        bot_id = self.bot_context.get()
        cur_src = normalize_node_source(
            args.get('source') if args.get('source') in ('phone', 'phone2', '安卓一', '安卓二') else (self.source_context.get() or 'phone')
        )
        if cur_src not in ('phone', 'phone2'):
            cur_src = 'phone'
        req_id = 'ph-' + secrets.token_hex(6)
        fut = asyncio.get_running_loop().create_future()
        self.phone_pending[req_id] = fut
        await self.publish({
            'type': 'phone_workspace_command',
            'request_id': req_id,
            'bot_id': bot_id,
            'source': cur_src,
            'op': op,
            'args': args,
            'timestamp': time.time(),
        })
        try:
            res = await asyncio.wait_for(fut, timeout=timeout)
            return res if isinstance(res, dict) else {'ok': False, 'op': op, 'error': '手机端返回无效响应格式'}
        except asyncio.TimeoutError:
            self.phone_pending.pop(req_id, None)
            return {
                'ok': False,
                'timed_out': True,
                'op': op,
                'error': '手机端在超时时间内未返回执行结果或界面内容（操作未确认完成，严禁假设成功；请重试或确认手机端 App 在线）',
            }

    @staticmethod
    def _annotate_tap_crosshair(img_bytes: bytes, tap_x: int, tap_y: int, width: int, height: int) -> bytes:
        return annotate_tap_crosshair(img_bytes, tap_x, tap_y, width, height)

    @staticmethod
    def _draw_vision_ruler_overlay(
        img_bytes: bytes,
        a11y_summary: str = '',
        screen_w: int = 0,
        screen_h: int = 0,
        pad_px: int = 28,
        draw_badges: bool = False,
    ) -> tuple[bytes, int, int]:
        return draw_vision_ruler_overlay(
            img_bytes,
            a11y_summary=a11y_summary,
            screen_w=screen_w,
            screen_h=screen_h,
            pad_px=pad_px,
            draw_badges=draw_badges,
        )

    @staticmethod
    def _resolve_phone_grid_or_norm(args: dict, width: int, height: int) -> tuple[int, int] | None:
        return resolve_phone_grid_or_norm(args, width, height)

    @staticmethod
    def _is_user_holding_or_refusing_phone(user_text: str, was_blocked: bool = False) -> bool:
        return False

    def _parse_and_cache_phone_nodes(self, res_dict: dict, node_st=None) -> list[dict]:
        st = node_st or self._phone_node_state()
        if not isinstance(res_dict, dict):
            return st.last_nodes or []
        parsed = parse_phone_nodes(res_dict, st.last_size)
        if parsed:
            st.last_nodes = parsed
        return parsed

    def _match_phone_node(self, target_q: str = '', ref_q: str = '', id_q: str = '', for_type: bool = False, node_st=None) -> dict | None:
        st = node_st or self._phone_node_state()
        return match_phone_node(st.last_nodes or [], target_q, ref_q, id_q, for_type=for_type)

    def _format_available_nodes_hint(self, max_items: int = 10, node_st=None) -> str:
        st = node_st or self._phone_node_state()
        return format_available_nodes_hint(st.last_nodes or [], max_items=max_items)

    async def _poll_and_converge_notifications(self, app_filter: str = '', limit: int = 50, timeout: float = 5.5, source: str | None = None) -> dict:
        self._last_notif_bg_poll = time.time()
        try:
            payload = {'op': 'notifications', 'app': app_filter, 'limit': max(20, min(80, limit))}
            if source:
                payload['source'] = source
            raw_res = await self._raw_phone_rpc('notifications', payload, timeout=timeout)
            if isinstance(raw_res, dict) and raw_res.get('ok') is not False and not raw_res.get('error') and isinstance(raw_res.get('notifications'), list):
                ingested = self.notification_hub.ingest_raw_notifications(raw_res['notifications'])
                return {'ok': True, 'listener_connected': raw_res.get('listener_connected', True), **ingested}
            err_s = str(raw_res.get('error') or '手机端未返回有效通知列表') if isinstance(raw_res, dict) else '手机端未返回有效响应'
            return {'ok': False, 'error': err_s, 'inbox_total': len(self.store.state.get('notification_inbox', []))}
        except Exception as exc:
            return {'ok': False, 'error': str(exc), 'inbox_total': len(self.store.state.get('notification_inbox', []))}

    async def _tool_phone_macro(self, args: dict):
        return await run_phone_macro_action(args, self.harness.root, self._tool_phone_workspace)

    async def _tool_phone(self, args: dict):
        eff = dict(args or {})
        op = str(eff.get('op') or eff.get('action') or 'status').strip().lower()
        if op in ('macro', 'save_macro', 'run_macro', 'list_macros', 'delete_macro'):
            act_map = {'save_macro': 'save', 'run_macro': 'run', 'list_macros': 'list', 'delete_macro': 'delete'}
            return await self._tool_phone_macro({**eff, 'action': act_map.get(op, eff.get('action') or 'list')})
        return await self._tool_phone_workspace(eff)

    async def _tool_phone_workspace(self, args: dict):
        cur_turn = self.turn_id_var.get()
        if cur_turn and cur_turn in self._stopped_turn_ids:
            raise asyncio.CancelledError()
        if (self.source_context.get() or 'phone') in ('pc', 'pc2'):
            return {
                'ok': False,
                'error': '你当前位于电脑端，不能直接调用安卓端的 phone 工具！请调用 blackboard(action="post", mentions=["@companion"], text="...") 通知手机端助理代为执行。',
            }

        node_st = self._phone_node_state(args.get('source'))
        rpc_src = {'source': node_st.source} if args.get('source') in ('phone', 'phone2', '安卓一', '安卓二') else {}
        op = str(args.get('op') or 'status').strip().lower()
        orig_requested_op = op
        scroll_dir = ''

        if op in ('promote', 'expand_to_foreground', 'to_foreground', 'fullscreen'):
            res = await self._raw_phone_rpc('promote', {'op': 'promote', **rpc_src})
            if not isinstance(res, dict) or res.get('error') or res.get('ok') is False:
                return {**(res if isinstance(res, dict) else {}), 'ok': False, 'op': 'promote', 'error': str((res or {}).get('error') or '切换前台失败')}
            node_st.used_in_turn = False
            node_st.to_foreground_in_turn = True
            return {**res, 'ok': True, 'op': 'promote', 'note': '已将虚拟屏当前页面切至主屏前台（零重启），请告知用户完成验证或支付'}

        if op in ('pull_back', 'to_virtual', 'pull_to_virtual'):
            pb = await self._raw_phone_rpc('pull_back', {'op': 'pull_back', 'package': str(args.get('package') or args.get('target') or '').strip(), **rpc_src})
            if isinstance(pb, dict) and (pb.get('error') or pb.get('ok') is False):
                return {**pb, 'ok': False, 'op': orig_requested_op}
            await asyncio.sleep(0.35)
            return await self._tool_phone_workspace({'op': 'ui_tree', 'refresh': True, **rpc_src})

        if op in ('release', 'close', 'destroy', 'stop_display'):
            keep_alive = bool(args.get('keep_alive', False))
            destroy_flag = (not keep_alive) if 'destroy' not in args else bool(args.get('destroy'))
            rel_res = await self._raw_phone_rpc(
                'release',
                {'op': 'release', 'destroy': destroy_flag, 'user_close': destroy_flag, 'keep_alive': keep_alive, 'to_foreground': False, **rpc_src},
            )
            if not isinstance(rel_res, dict) or rel_res.get('error') or rel_res.get('ok') is False:
                return {
                    **(rel_res if isinstance(rel_res, dict) else {}),
                    'ok': False,
                    'op': orig_requested_op,
                    'released': False,
                    'error': str((rel_res or {}).get('error') or '释放手机虚拟屏失败'),
                }
            node_st.used_in_turn = False
            node_st.to_foreground_in_turn = False
            node_st.vdisplay_open = not destroy_flag
            return {
                **rel_res,
                'ok': True,
                'op': orig_requested_op,
                'released': destroy_flag,
                'destroyed': destroy_flag,
                'display_active': not destroy_flag,
                'created': not destroy_flag,
                'note': '已销毁并关闭手机独立虚拟屏（主屏应用不受影响）。' if destroy_flag else '虚拟屏已保留在后台驻留。',
            }

        if op == 'ime_policy':
            policy = str(args.get('policy') or args.get('target') or args.get('text') or '').strip().lower()
            if policy not in ('local', 'fallback', 'hide'):
                return {'ok': False, 'op': 'ime_policy', 'error': 'ime_policy 需要提供 policy="local" | "fallback" | "hide"'}
            ime_res = await self._raw_phone_rpc('ime_policy', {'op': 'ime_policy', 'policy': policy, **rpc_src}, timeout=12.0)
            if not isinstance(ime_res, dict) or ime_res.get('error') or ime_res.get('ok') is False:
                return {**(ime_res if isinstance(ime_res, dict) else {}), 'ok': False, 'op': 'ime_policy', 'policy': policy}
            return {**ime_res, 'ok': True, 'op': 'ime_policy', 'policy': policy}

        notif_ops = ('notifications', 'get_notifications', 'recent_notifications', 'notifs', 'notification', 'add_filter', 'remove_filter', 'list_filters', 'clear_inbox')
        otp_ops = ('read_otp', 'otp', 'sms_code')
        non_gui_ops = ('status', 'device_info', 'phone_info', 'battery', 'shell', 'bash', 'cmd', 'exec', 'pull_file', 'push_file', 'list_files', 'files', 'apps', 'connect', 'delay', 'wait', 'sleep', 'release', 'auto_release', 'promote', 'pull_back', 'ime_policy', *otp_ops, *notif_ops)
        if self.activity == 'curiosity' and op not in notif_ops:
            return {'ok': False, 'error': '后台主动关心期间仅允许操作通知栏摘要与过滤 (op=notifications)，禁止操控虚拟屏界面'}
        if op not in non_gui_ops:
            node_st.used_in_turn = True

        if op in otp_ops:
            return await self._raw_phone_rpc('read_otp', {'op': 'read_otp', 'limit': max(1, min(30, int(args.get('limit') or 10))), **rpc_src}, timeout=15.0)

        if op in ('shell', 'bash', 'cmd', 'exec'):
            return await exec_phone_shell(args, node_st, self._raw_phone_rpc)
        if op in ('device_info', 'phone_info', 'battery'):
            from vault_manager import now_iso as _now_iso
            dev_r, sh_r = await asyncio.gather(
                self._raw_phone_rpc('device_info', {'op': 'device_info', **rpc_src}, timeout=10.0),
                self._raw_phone_rpc('shell', {'op': 'shell', 'command': 'id -u', **rpc_src}, timeout=4.5),
            )
            return reconcile_device_root_status(dev_r if isinstance(dev_r, dict) else {}, sh_r if isinstance(sh_r, dict) else None, _now_iso())
        if op in ('list_files', 'files'):
            dir_p = str(args.get('path') or args.get('target') or args.get('text') or '/sdcard/Download').strip()
            lim_f = max(1, min(100, int(args.get('limit') or 25)))
            return await self._raw_phone_rpc('list_files', {'op': 'list_files', 'path': dir_p, 'limit': lim_f, **rpc_src}, timeout=15.0)
        if op == 'pull_file':
            return await exec_phone_pull_file(args, node_st, self._raw_phone_rpc)
        if op == 'push_file':
            file_p = str(args.get('path') or args.get('target') or args.get('text') or '').strip()
            if not file_p:
                return {'ok': False, 'error': 'push_file 需要提供工作区文件路径 path（如 "uploads/xxx.pdf"）'}
            src_origin = str(args.get('source') or 'workspace').strip().lower()
            if src_origin == 'workspace' and not file_p.startswith('workspace/'):
                file_p = 'workspace/' + file_p.lstrip('/')
            return await self._raw_phone_rpc('push_file', {'op': 'push_file', 'path': file_p, 'source': src_origin}, timeout=45.0)

        if op in notif_ops:
            action = str(args.get('action') or (op if op in ('add_filter', 'remove_filter', 'list_filters', 'clear_inbox') else 'list')).strip().lower()
            if action == 'add_filter':
                return self.notification_hub.add_filter(
                    package=str(args.get('filter_package') or args.get('package') or ''),
                    app=str(args.get('filter_app') or args.get('app') or ''),
                    keyword=str(args.get('filter_keyword') or args.get('keyword') or args.get('text') or args.get('target') or ''),
                    reason=str(args.get('reason') or args.get('goal') or ''),
                    created_by='ai',
                )
            if action == 'remove_filter':
                return self.notification_hub.remove_filter(str(args.get('filter_id') or args.get('id') or args.get('filter_keyword') or args.get('keyword') or args.get('target') or args.get('app') or ''))
            if action == 'list_filters':
                return {'ok': True, 'action': 'list_filters', 'filters': self.notification_hub.get_filters()}
            if action == 'clear_inbox':
                return self.notification_hub.clear_inbox(str(args.get('app') or args.get('target') or ''))
            app_q = str(args.get('app') or args.get('package') or args.get('target') or args.get('text') or '').strip()
            lim = int(args.get('limit') or 30)
            poll_info = await self._poll_and_converge_notifications(app_filter='', limit=max(40, lim), timeout=8.0, source=node_st.source if rpc_src else None)
            inbox_res = self.notification_hub.query_inbox(app_query=app_q, limit=lim, mark_seen=True)
            if not poll_info.get('ok'):
                return {
                    'ok': False,
                    'op': 'notifications',
                    'live_polled': False,
                    'error': str(poll_info.get('error') or '手机端未在线或通知拉取失败'),
                    **inbox_res,
                }
            return {'ok': True, 'op': 'notifications', 'live_polled': True, **inbox_res}

        if op in ('send_screenshot', 'share_image', 'send_qr', 'send_image'):
            box_xyxy, box_err = validate_norm_box_xyxy(args)
            if box_err:
                return {'ok': False, 'op': 'send_screenshot', 'error': box_err}
            snap = await self._raw_phone_rpc('preview', {'op': 'preview', 'since': -1, **rpc_src})
            b64_s = str((snap or {}).get('image') or '').strip()
            if not b64_s:
                return {'ok': False, 'op': 'send_screenshot', 'error': '未能实时获取手机虚拟屏画面（拒绝发送历史缓存或带标尺截图）'}
            raw_clean = base64.b64decode(b64_s.split(',')[-1])
            (self.harness.root / 'phone-clean.jpg').write_bytes(raw_clean)
            media_url = self._prepare_chat_screenshot(raw_clean, crop_box=box_xyxy, prefix='phone-shot')
            if not media_url:
                return {'ok': False, 'op': 'send_screenshot', 'error': '手机截图保存或挂载聊天图片失败'}
            rel_shot = media_url.removeprefix('/workspace-media/').lstrip('/')
            shot_path = self.harness.root / rel_shot
            verify_b64 = base64.b64encode(shot_path.read_bytes()).decode('ascii') if shot_path.is_file() else ''
            return {
                'ok': True,
                'op': 'send_screenshot',
                'chat_image_url': media_url,
                'cropped': bool(box_xyxy),
                'crop_box': box_xyxy,
                **({'_multimodal_b64': verify_b64, '_multimodal_mime': 'image/jpeg', '_multimodal_caption': '【已发出给用户的手机实拍截图预览】请核对目标内容是否完整居中。'} if verify_b64 else {}),
                'note': f'已将手机虚拟屏干净无标尺实时截图直接挂载到本轮聊天回复气泡上（{media_url}）！',
            }

        if op in ('zoom', 'crop', 'magnify'):
            box_xyxy, box_err = validate_norm_box_xyxy(args)
            if box_err:
                return {'ok': False, 'op': 'zoom', 'error': box_err}
            snap = await self._raw_phone_rpc('preview', {'op': 'preview', 'since': -1, **rpc_src})
            b64_z = str((snap or {}).get('image') or '').strip()
            if not b64_z:
                return {'ok': False, 'op': 'zoom', 'error': '未能实时获取手机虚拟屏画面用于局部放大'}
            raw_z = base64.b64decode(b64_z.split(',')[-1])
            (self.harness.root / 'phone-clean.jpg').write_bytes(raw_z)
            zoomed_bytes, z_meta = zoom_region_from_args(raw_z, args)
            (self.harness.root / 'phone-zoom.jpg').write_bytes(zoomed_bytes)
            return {
                'ok': True,
                'op': 'zoom',
                'zoom_meta': z_meta,
                'saved_screenshot': 'phone-zoom.jpg',
                '_multimodal_mime': 'image/jpeg',
                '_multimodal_b64': base64.b64encode(zoomed_bytes).decode('ascii'),
                '_multimodal_caption': f'【手机虚拟屏局部放大 ({z_meta.get("norm_box")})】边缘刻度已映射回整屏 0..1000 坐标系，请直接用整屏 nx, ny 坐标调用 phone(op="tap", nx=..., ny=...)。',
            }

        if op in ('run', 'task', 'inspect', 'screenshot', 'capture'):
            op = 'ui_tree'
            args = {**args, 'op': 'ui_tree', 'refresh': True}

        cur_w, cur_h = node_st.last_size
        if op == 'open':
            comp_cand = str(args.get('component') or '').strip()
            u_cand = str(args.get('uri') or args.get('url') or args.get('target') or args.get('app') or args.get('package') or args.get('text') or '').strip()
            is_bare_domain = (
                bool(u_cand) and not comp_cand and ' ' not in u_cand
                and (u_cand.startswith('www.') or '/' in u_cand) and '.' in u_cand
                and not re.match(r'^(?:com|cn|org|net|io|dev|me|tv|app|gov|edu)\.[a-zA-Z0-9_.]+$', u_cand)
            )
            if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', u_cand) or is_bare_domain:
                op = 'open_uri'
                args = {**args, 'op': 'open_uri', 'uri': u_cand if '://' in u_cand else ('https://' + u_cand)}
            elif u_cand or comp_cand:
                op = 'launch'
                args = {**args, 'op': 'launch', 'app': u_cand, 'component': comp_cand}
            else:
                return {'ok': False, 'error': 'phone(op="open") 需要提供 app 名称/包名、component 或 uri 网址'}
        elif op == 'press':
            op = 'key'
            args = {**args, 'op': 'key', 'key': str(args.get('key') or args.get('target') or args.get('text') or 'back').strip().lower()}
        elif op in ('scroll', 'swipe'):
            op, args, scroll_dir, swipe_err = build_phone_swipe_args(args, cur_w, cur_h, op)
            if swipe_err:
                return {'ok': False, 'op': orig_requested_op, 'error': swipe_err}
        elif op == 'type':
            op = 'tap_and_type'
            args = {**args, 'op': 'tap_and_type'}
        elif op == 'tap':
            t_in = str(args.get('target') or args.get('label') or '').strip()
            r_in = str(args.get('ref') or '').strip()
            if not r_in and t_in.startswith(('@e', '@v', 'e', 'v')) and re.fullmatch(r'@?[ev]\d+', t_in, flags=re.IGNORECASE):
                r_in = t_in if t_in.startswith('@') else ('@' + t_in)
                t_in = ''
            if (r_in or t_in or args.get('text') or args.get('view_id') or args.get('id')) and args.get('x') is None and args.get('nx') is None and not args.get('grid'):
                op = 'tap_node'
                args = {**args, 'op': 'tap_node', 'ref': r_in, 'target': t_in, 'label': '', 'text': str(args.get('text') or t_in).strip()}

        resolved_pt = self._resolve_phone_grid_or_norm(args, cur_w, cur_h)
        if args.get('_coord_error'):
            return {'ok': False, 'op': orig_requested_op, 'error': str(args['_coord_error'])}
        if resolved_pt is not None and op in ('tap', 'tap_node', 'click_node', 'tap_and_type', 'long_press'):
            args = {**args, 'x': resolved_pt[0], 'y': resolved_pt[1]}
            if op in ('tap_node', 'click_node') and not args.get('ref') and not args.get('text') and not args.get('target'):
                op = 'tap'
                args['op'] = 'tap'
        if op == 'tap' and (args.get('x') is None or args.get('y') is None):
            return {'ok': False, 'op': orig_requested_op, 'error': 'tap 需要提供 nx, ny (0..1000 视觉坐标) 或有效 target'}

        wait_meta: dict | None = None
        if op in ('wait', 'delay', 'sleep'):
            raw_sec = args.get('seconds') or args.get('sec')
            raw_ms = args.get('duration_ms') or args.get('delay_ms') or args.get('ms')
            ms = int(float(raw_ms)) if raw_ms is not None else (int(float(raw_sec) * 1000) if raw_sec is not None else 1500)
            ms = max(200, min(10000, ms))
            await asyncio.sleep(ms / 1000.0)
            wait_meta = {'ok': True, 'op': 'wait', 'waited_ms': ms, 'duration_ms': ms, 'delay_ms': ms}
            op = 'ui_tree'
            args = {**args, 'op': 'ui_tree', 'refresh': True}

        open_uri_meta: dict | None = None
        if op in ('open_uri', 'open_url'):
            uri_str = str(args.get('uri') or args.get('url') or args.get('text') or args.get('target') or '').strip()
            if not uri_str or not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', uri_str):
                return {'ok': False, 'op': orig_requested_op, 'error': 'open_uri 需要提供完整的 URI/URL（例如 https://...）'}
            uri_res = await self._raw_phone_rpc('open_uri', {'op': 'open_uri', 'uri': uri_str, **rpc_src})
            if isinstance(uri_res, dict) and '未知虚拟屏操作' in str(uri_res.get('error') or ''):
                st = await self._raw_phone_rpc('status', {'op': 'status', **rpc_src})
                disp_id = int(((st.get('display') or {}) if isinstance(st, dict) else {}).get('displayId') or st.get('displayId') or 0)
                if disp_id <= 0:
                    create_res = await self._raw_phone_rpc('create', {'op': 'create', **rpc_src})
                    if isinstance(create_res, dict) and not create_res.get('error'):
                        disp_id = int(create_res.get('displayId') or create_res.get('display_id') or (create_res.get('display') or {}).get('displayId') or 0)
                        if disp_id > 0:
                            node_st.vdisplay_open = True
                if disp_id <= 0:
                    return {'ok': False, 'op': orig_requested_op, 'error': '虚拟屏尚未就绪 (displayId <= 0)，已阻止在主屏 (Display 0) 打开链接'}
                safe_u = uri_str.replace("'", "'\\''")
                uri_res = await self._raw_phone_rpc('shell', {'op': 'shell', 'command': f"am start --display {disp_id} -a android.intent.action.VIEW -d '{safe_u}'", **rpc_src})
                await asyncio.sleep(0.85)
            if not isinstance(uri_res, dict) or uri_res.get('error') or uri_res.get('ok') is False:
                return {**(uri_res if isinstance(uri_res, dict) else {}), 'ok': False, 'op': orig_requested_op, 'opened_uri': uri_str}
            open_uri_meta = {**uri_res, 'opened_uri': uri_str}
            op = 'ui_tree'
            args = {'op': 'ui_tree', 'refresh': True, **rpc_src}

        if op == 'batch' and isinstance(args.get('steps'), list):
            steps_list = [s for s in args['steps'][:30] if isinstance(s, dict) and s.get('op') != 'batch']
            screenshot_mode = str(args.get('screenshot') or 'final').strip().lower()
            batch_results = []
            last_res: dict = {'ok': True}
            for idx_s, step_item in enumerate(steps_list):
                is_last_step = (idx_s + 1 == len(steps_list))
                skip_vis = True if screenshot_mode in ('none', 'false', '0') else (False if screenshot_mode in ('each', 'all', 'every') else not is_last_step)
                step_payload = {**rpc_src, **step_item, '_skip_vision': skip_vis}
                last_res = await self._tool_phone_workspace(step_payload)
                step_ok = isinstance(last_res, dict) and last_res.get('ok') is not False and not last_res.get('error')
                s_tree = str((last_res or {}).get('ui_tree') or '') if isinstance(last_res, dict) else ''
                m_sh = re.search(r'treeHash=([0-9a-zA-Z_-]+)', s_tree)
                batch_results.append({
                    'step': idx_s + 1,
                    'op': step_item.get('op'),
                    'ok': step_ok,
                    'package': str((last_res or {}).get('package') or '') if isinstance(last_res, dict) else '',
                    'node_count': int((last_res or {}).get('node_count') or 0) if isinstance(last_res, dict) else 0,
                    'treeHash': m_sh.group(1) if m_sh else '',
                })
                if not step_ok:
                    break
                if not is_last_step:
                    await asyncio.sleep(0.22)
            if isinstance(last_res, dict):
                last_res['batch_steps_executed'] = batch_results
                last_res['steps_trace'] = batch_results
                last_res['screenshot_mode'] = screenshot_mode
                if screenshot_mode in ('none', 'false', '0'):
                    last_res.pop('saved_screenshot', None)
                    last_res.pop('_multimodal_b64', None)
            return last_res

        ref_arg = str(args.get('ref') or '').strip()
        target_only_arg = str(args.get('target') or args.get('label') or '').strip()
        id_arg = str(args.get('view_id') or args.get('id') or '').strip()
        for cand_ref in (ref_arg, target_only_arg, id_arg):
            if cand_ref and re.fullmatch(r'@?[ev]\d+', cand_ref, flags=re.IGNORECASE):
                ref_arg = cand_ref if cand_ref.startswith('@') else ('@' + cand_ref)
                if target_only_arg == cand_ref:
                    target_only_arg = ''
                if id_arg == cand_ref:
                    id_arg = ''
                break

        before_tree_hash = str(node_st.last_tree_hash or '')
        before_pkg_state = str(node_st.last_pkg or '')
        before_nodes_state = node_st.last_node_count
        if op == 'key' and not before_tree_hash:
            pre_t = await self._raw_phone_rpc('ui_tree', {'op': 'ui_tree', 'refresh': False, **rpc_src}, timeout=6.0)
            if isinstance(pre_t, dict):
                self._parse_and_cache_phone_nodes(pre_t, node_st=node_st)
                m_bh = re.search(r'treeHash=([0-9a-zA-Z_-]+)', str(pre_t.get('ui_tree') or pre_t.get('tree') or pre_t.get('summary') or ''))
                if m_bh:
                    before_tree_hash = f"{pre_t.get('package') or ''}:{m_bh.group(1)}"

        if op == 'long_press':
            lx = int(args['x']) if args.get('x') is not None else None
            ly = int(args['y']) if args.get('y') is not None else None
            if (lx is None or ly is None) and (ref_arg or target_only_arg or id_arg):
                matched_lp = self._match_phone_node(target_q=target_only_arg, ref_q=ref_arg, id_q=id_arg, for_type=False, node_st=node_st)
                if matched_lp and matched_lp.get('x') is not None and matched_lp.get('y') is not None:
                    lx, ly = int(matched_lp['x']), int(matched_lp['y'])
            if lx is None or ly is None:
                return {'ok': False, 'op': orig_requested_op, 'error': 'long_press 需要提供 nx, ny (0..1000 归一化坐标) 或有效的 target'}
            dur = max(450, min(2000, int(args.get('duration_ms') or 650)))
            res = await self._raw_phone_rpc('swipe', {'op': 'swipe', 'x1': lx, 'y1': ly, 'x2': lx, 'y2': ly, 'duration_ms': dur, **rpc_src})
            if isinstance(res, dict):
                res['long_pressed'] = True
                res['x'], res['y'] = lx, ly
            args = {**args, 'x': lx, 'y': ly}
        elif op == 'scroll_to':
            target_q = str(args.get('target') or args.get('text') or args.get('label') or '').strip()
            if not target_q:
                return {'ok': False, 'error': 'scroll_to 需要提供要查找的 target 文字'}
            direction = str(args.get('direction') or 'down').strip().lower()
            max_scroll_attempts = max(4, min(20, int(args.get('max_attempts') or 12)))
            last_tree: dict = {}
            for attempt in range(max_scroll_attempts):
                last_tree = await self._tool_phone_workspace({'op': 'ui_tree', 'refresh': True, **rpc_src})
                if target_q.lower() in str(last_tree.get('ui_tree') or '').lower():
                    last_tree['found_target'] = target_q
                    last_tree['scroll_attempts'] = attempt
                    return last_tree
                sx = cur_w // 2
                sy1, sy2 = (int(cur_h * 0.20), int(cur_h * 0.82)) if direction == 'up' else (int(cur_h * 0.82), int(cur_h * 0.20))
                await self._raw_phone_rpc('swipe', {'op': 'swipe', 'x1': sx, 'y1': sy1, 'x2': sx, 'y2': sy2, 'duration_ms': 300, **rpc_src})
                await asyncio.sleep(0.42)
            last_tree['found_target'] = False
            last_tree['note'] = f'已连续滚动 {max_scroll_attempts} 次，请查看最新画面截图定位「{target_q}」'
            return last_tree
        elif op == 'tap_and_type':
            err_res, res, tx, ty = await exec_phone_tap_and_type(
                args, node_st, orig_requested_op, ref_arg, target_only_arg, id_arg, before_pkg_state,
                self._raw_phone_rpc, self._parse_and_cache_phone_nodes,
            )
            if err_res is not None:
                return err_res
            if tx is not None and ty is not None:
                args = {**args, 'x': tx, 'y': ty}
        else:
            reused_existing_app = False
            if op == 'launch' and not str(args.get('component') or '').strip() and not args.get('force'):
                req_app = str(args.get('app') or args.get('target') or args.get('package') or args.get('text') or '').strip()
                if req_app and node_st.vdisplay_open:
                    probe_tree = await self._raw_phone_rpc('ui_tree', {'op': 'ui_tree', 'refresh': True, **rpc_src})
                    if isinstance(probe_tree, dict) and not probe_tree.get('error'):
                        probe_pkg = str(probe_tree.get('package') or '')
                        if probe_pkg and self._matches_requested_app_package(req_app, probe_pkg):
                            res = {**probe_tree, 'reused_active_session': True}
                            op = 'ui_tree'
                            reused_existing_app = True
            if not reused_existing_app:
                res = await self._raw_phone_rpc(op, {**args, **rpc_src})
                if isinstance(res, dict) and op == 'tap_node' and (res.get('error') or res.get('ok') is False) and (ref_arg or target_only_arg or id_arg):
                    matched_tap = self._match_phone_node(target_q=target_only_arg, ref_q=ref_arg, id_q=id_arg, for_type=False, node_st=node_st)
                    if matched_tap and matched_tap.get('x') is not None and matched_tap.get('y') is not None:
                        fallback_tap = await self._raw_phone_rpc('tap', {'op': 'tap', 'x': int(matched_tap['x']), 'y': int(matched_tap['y']), **rpc_src})
                        if isinstance(fallback_tap, dict) and not fallback_tap.get('error') and fallback_tap.get('ok') is not False:
                            res = {**fallback_tap, 'ok': True, 'ref': matched_tap.get('ref') or ref_arg, 'x': int(matched_tap['x']), 'y': int(matched_tap['y']), 'fallback_by_coord': True}
                has_disp_field = isinstance(res, dict) and (
                    'displayId' in res or 'display_id' in res
                    or (isinstance(res.get('display'), dict) and ('displayId' in res['display'] or 'display_id' in res['display']))
                )
                if (
                    isinstance(res, dict)
                    and op not in ('create', 'release', 'auto_release', 'status', 'apps', 'shell', 'device_info', 'notifications', 'pull_file', 'push_file', 'list_files', 'ime_policy')
                    and (any(k in str(res.get('error') or '') for k in ('虚拟屏未创建', '虚拟屏不存在', '未分配虚拟屏', 'displayId <= 0')) or (not res.get('error') and has_disp_field and int(res.get('displayId') or res.get('display_id') or (res.get('display') or {}).get('displayId') or -1) <= 0))
                ):
                    wake_r = await self._raw_phone_rpc('create', {'op': 'create', **rpc_src})
                    if isinstance(wake_r, dict) and not wake_r.get('error') and int(wake_r.get('displayId') or -1) > 0:
                        node_st.vdisplay_open = True
                        res = await self._raw_phone_rpc(op, {**args, **rpc_src})

        if not isinstance(res, dict):
            return {'ok': False, 'op': orig_requested_op, 'error': '手机端返回无效响应'}
        if wait_meta and (res.get('error') or res.get('ok') is False):
            return {**res, 'ok': False, 'op': orig_requested_op, 'waited_ms': wait_meta.get('waited_ms')}
        if open_uri_meta:
            res = {**open_uri_meta, **res}
        if wait_meta:
            res = {**res, **wait_meta}

        disp_info = res.get('display') if isinstance(res.get('display'), dict) else {}
        has_disp_key = ('displayId' in res or 'display_id' in res or 'displayId' in disp_info or 'display_id' in disp_info)
        eff_disp_id = int(res.get('displayId') or res.get('display_id') or disp_info.get('displayId') or disp_info.get('display_id') or -1)
        if eff_disp_id > 0:
            node_st.display_id = eff_disp_id
        if (
            op in ('launch', 'open_uri', 'tap', 'tap_node', 'click_node', 'tap_and_type', 'swipe', 'text', 'type_node', 'key', 'long_press', 'ui_tree', 'capture')
            and not wait_meta and not res.get('error') and has_disp_key and eff_disp_id <= 0 and not node_st.vdisplay_open
        ):
            return {
                'ok': False,
                'op': op,
                'displayId': eff_disp_id,
                'error': 'virtual_display_not_ready: 虚拟屏尚未就绪 (displayId <= 0)，禁止落回主屏幕 (Display 0)！请先调用 phone(op="open", target="应用名") 启动目标应用。',
            }
        if (op in ('launch', 'create', 'open_uri') or bool(disp_info.get('created') and not disp_info.get('released'))) and not res.get('error') and res.get('ok') is not False:
            node_st.vdisplay_open = True

        if res.get('error') or res.get('ok') is False:
            res['ok'] = False
            res.pop('image', None)
            res.pop('_image_b64', None)
            res.pop('_multimodal_b64', None)
            res.pop('summary', None)
            if '未找到' in str(res.get('error') or '') and '当前可用节点' not in str(res.get('error') or ''):
                res['error'] = f"{res['error']} ({self._format_available_nodes_hint(node_st=node_st)})"
            return res

        pkg, node_count, cur_hash_now = normalize_single_tree_frame(res)
        if op == 'key' and not res.get('error'):
            if not cur_hash_now or (before_tree_hash and cur_hash_now == before_tree_hash):
                await asyncio.sleep(0.24)
                probe_k = await self._raw_phone_rpc('ui_tree', {'op': 'ui_tree', 'refresh': True, **rpc_src})
                if isinstance(probe_k, dict) and not probe_k.get('error') and probe_k.get('ok') is not False:
                    apply_authoritative_tree(res, probe_k)
                    pkg, node_count, cur_hash_now = normalize_single_tree_frame(res)
            res['tree_hash_before'] = before_tree_hash
            res['tree_hash_after'] = cur_hash_now
            res['key_consumed'] = bool(before_tree_hash != cur_hash_now and (before_tree_hash or cur_hash_now))

        if (
            op in ('launch', 'open_uri', 'tap', 'tap_node', 'click_node', 'tap_and_type', 'swipe', 'text', 'type_node', 'key', 'long_press', 'ui_tree')
            and not res.get('error')
            and (int(node_count or 0) < 3 or pkg in ('', 'unknown') or (op == 'launch' and pkg == 'com.android.launcher'))
        ):
            await asyncio.sleep(0.50)
            retry_tree = await self._raw_phone_rpc('ui_tree', {'op': 'ui_tree', 'refresh': True, **rpc_src})
            if isinstance(retry_tree, dict) and not retry_tree.get('error') and retry_tree.get('ok') is not False:
                apply_authoritative_tree(res, retry_tree)
                res['transition_retried'] = True
                if wait_meta:
                    res.update(wait_meta)
                pkg, node_count, cur_hash_now = normalize_single_tree_frame(res)

        if op == 'launch':
            req_app_chk = str(args.get('app') or args.get('target') or args.get('package') or '').strip()
            res['active_package'] = pkg
            res['launched'] = bool(pkg and pkg != 'unknown' and int(node_count or 0) > 0 and (not req_app_chk or pkg != 'com.android.launcher' or self._matches_requested_app_package(req_app_chk, pkg)))
        res['settled'] = False if (int(node_count or 0) == 0 and pkg in ('', 'unknown') and op in ('launch', 'open_uri', 'ui_tree', 'tap', 'tap_node')) else int(node_count or 0) >= 3
        if int(node_count or 0) == 0 and pkg in ('', 'unknown') and op in ('launch', 'open_uri', 'ui_tree', 'tap', 'tap_node'):
            res['likely_blank'] = True

        if op in ('tap', 'tap_node', 'click_node', 'swipe') and not res.get('error') and before_tree_hash and cur_hash_now and cur_hash_now != before_tree_hash:
            await asyncio.sleep(0.26)
            fresh_t = await self._raw_phone_rpc('ui_tree', {'op': 'ui_tree', 'refresh': True, **rpc_src})
            if isinstance(fresh_t, dict) and not fresh_t.get('error') and fresh_t.get('ok') is not False:
                apply_authoritative_tree(res, fresh_t)
                pkg, node_count, cur_hash_now = normalize_single_tree_frame(res)

        res['layoutStable'] = bool(res.get('settled', True))
        res['densityDpi'] = int(res.get('dpi') or res.get('densityDpi') or 320)
        res['screenWidthDp'] = int(res.get('swDp') or res.get('screenWidthDp') or 360)
        res.setdefault('fontScale', 1.0)
        res.setdefault('settle_ms', 0)

        tapped_xy: tuple[int, int] | None = None
        if op in ('tap', 'tap_node', 'click_node', 'tap_and_type', 'type_node', 'input_node', 'long_press'):
            tx_val = res.get('x') if res.get('x') is not None else args.get('x')
            ty_val = res.get('y') if res.get('y') is not None else args.get('y')
            if tx_val is not None and ty_val is not None:
                try:
                    tapped_xy = (int(float(tx_val)), int(float(ty_val)))
                except (ValueError, TypeError):
                    tapped_xy = None

        return await finalize_phone_vision_and_diff(
            res=res,
            node_st=node_st,
            orig_requested_op=orig_requested_op,
            op=op,
            args=args,
            pkg=pkg,
            node_count=node_count,
            cur_hash_now=cur_hash_now,
            eff_disp_id=eff_disp_id,
            tapped_xy=tapped_xy,
            before_tree_hash=before_tree_hash,
            before_pkg_state=before_pkg_state,
            before_nodes_state=before_nodes_state,
            scroll_dir=scroll_dir,
            workspace_root=self.harness.root,
            raw_phone_rpc_fn=lambda rpc_op, rpc_args: self._raw_phone_rpc(rpc_op, {**rpc_args, **rpc_src}),
            prepare_chat_screenshot_fn=self._prepare_chat_screenshot,
        )
