"""Local PC Workspace Controller (`dsh_workspace` / `pc`)."""
from __future__ import annotations

import asyncio
import base64
import time

from assistants import get_assistant
from core.phone_vision_helpers import validate_norm_box_xyxy
from core.vision_engine import (
    compute_frame_diff_pct,
    compute_perceptual_frame_hash,
    draw_vision_ruler_overlay,
    zoom_region_from_args,
)
from storage import node_display_name, normalize_node_source


def _validate_pc_norm_coords(merged: dict, cua_action: str, *, require_if_no_target: bool = False) -> tuple[int | None, int | None, str | None]:
    nx, ny = merged.get('nx'), merged.get('ny')
    if nx is not None or ny is not None:
        if nx is None or ny is None:
            return None, None, f'{cua_action} 必须同时提供 nx 和 ny (0..1000 归一化坐标)'
        try:
            nxf, nyf = float(nx), float(ny)
        except (TypeError, ValueError):
            return None, None, f'{cua_action} 坐标格式错误: nx={nx}, ny={ny} 必须为 0..1000 数值'
        if not (0.0 <= nxf <= 1000.0 and 0.0 <= nyf <= 1000.0):
            return None, None, f'{cua_action} 坐标越界: nx={nx}, ny={ny} 必须在 0..1000 范围内'
        return int(round(nxf)), int(round(nyf)), None
    has_ref_or_px = any(merged.get(k) is not None for k in ('element_index', 'ref', 'target', 'x', 'y'))
    if require_if_no_target and not has_ref_or_px:
        return None, None, f'{cua_action} 必须提供 nx, ny (0..1000 视觉坐标)'
    return None, None, None


class PcDesktopControllerMixin:
    def extract_dialogs_by_source(self, source: str = 'phone', limit: int = 3) -> tuple[list[dict], str]:
        target_src = normalize_node_source(source)
        cap = max(1, min(100, int(limit or 3)))
        all_msgs = self.store.state.get('messages') or []
        bot_names = {b['id']: b['name'] for b in self.store.state.get('assistants', [])}
        dialogs: list[dict] = []
        cur_u = ''
        cur_t = ''
        cur_b = '主助理'
        scan_window = max(60, cap * 4)
        filtered_msgs = [m for m in all_msgs if normalize_node_source(m.get('source')) == target_src][-scan_window:]
        for m in filtered_msgs:
            role = m.get('role')
            txt = str(m.get('text') or '').strip()
            if not txt:
                continue
            ts = float(m.get('created_at') or m.get('time') or 0)
            t_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts)) if ts > 0 else ''
            b_id = str(m.get('bot_id') or 'companion')
            b_name = str(m.get('from_bot_name') or bot_names.get(b_id) or '主助理')
            if role == 'user':
                if cur_u:
                    dialogs.append({
                        'time': cur_t,
                        'bot_name': cur_b,
                        'source': target_src,
                        'user': cur_u[:240],
                        'assistant': '(处理中...)',
                    })
                cur_u = txt
                cur_t = t_str
                cur_b = bot_names.get(b_id, '主助理')
            elif role == 'assistant':
                dialogs.append({
                    'time': t_str or cur_t,
                    'bot_name': b_name,
                    'source': target_src,
                    'user': cur_u[:240] if cur_u else '(主动通知)',
                    'assistant': txt[:320],
                })
                cur_u = ''
        if cur_u:
            dialogs.append({
                'time': cur_t,
                'bot_name': cur_b,
                'source': target_src,
                'user': cur_u[:240],
                'assistant': '(处理中...)',
            })
        sliced = dialogs[-cap:]
        last_time = sliced[-1]['time'] if sliced else ''
        return sliced, last_time

    async def _tool_dsh_workspace(self, args: dict) -> dict:
        op = str(args.get('op') or args.get('action') or 'status').strip().lower()
        if op == 'open_url':
            op = 'navigate'
        elif op == 'browser_snapshot':
            op = 'screenshot'
        if op in ('list', 'sessions'):
            op = 'list_sessions'
        elif op in ('inspect', 'read', 'context', 'tools'):
            op = 'inspect_session'
        elif op in ('send', 'steer', 'chat', 'reply', 'ask', 'send_message'):
            op = 'send_message'
        elif op in ('new', 'create', 'spawn', 'start'):
            op = 'new_session'
        elif op in ('wake', 'open_gui', 'launch_gui'):
            op = 'wake_gui'
        elif op in ('peek', 'last_3', 'peek_dialogs', 'dialogs'):
            op = 'status'
        elif op in ('shell', 'exec', 'run_bash', 'powershell', 'cmd', 'pc_shell'):
            op = 'pc_shell'
        elif op in ('desktop', 'cua', 'cua_desktop', 'screen', 'screenshot', 'zoom', 'crop', 'magnify', 'tap', 'click', 'double_click', 'right_click', 'type', 'scroll', 'key', 'hotkey', 'move', 'hover', 'drag', 'send_screenshot'):
            if op != 'cua_desktop' and not args.get('action'):
                args = {**args, 'action': 'inspect' if op in ('desktop', 'cua', 'screen', 'screenshot') else op}
            op = 'cua_desktop'

        if op == 'send_message':
            if self.hop_depth.get() > 0:
                return {'error': '当前处于被跨端/同事协作唤醒的子轮次中，请勿再次嵌套调用 send_message，请直接用自然语言正文输出你的处理结果与回复（系统会自动将你的正文送回发件方）。'}
            target_bot_arg = str(args.get('to_bot_id') or args.get('bot_id') or args.get('target') or '').strip()
            msg_text = str(args.get('message') or args.get('text') or '').strip()
            cur_src = normalize_node_source(self.source_context.get())
            default_node = '安卓一' if cur_src == 'pc' else 'Windows一'
            to_spec = f'{default_node}:{target_bot_arg}' if target_bot_arg and ':' not in target_bot_arg else (target_bot_arg or default_node)
            return await self._tool_send_message({'to_bot_id': to_spec, 'text': msg_text})

        # Enforce symmetric cross-device isolation (P0-6): phone nodes cannot directly execute PC tools
        cur_node_src = normalize_node_source(self.source_context.get() or 'phone')
        if cur_node_src in ('phone', 'phone2') and op not in ('status', 'send_message'):
            return {
                'ok': False,
                'error': '你当前位于「安卓一」，不能直接调用电脑端的 pc 工具操控电脑本机！请调用 blackboard(action="post", content="@Windows一 ...", mentions=["Windows一"]) 发布任务让「Windows一」接手执行。',
            }

        if op == 'cua_desktop':
            nested = args.get('args') if isinstance(args.get('args'), dict) else {}
            cua_action = str(args.get('action') or args.get('tool') or nested.get('action') or nested.get('tool') or 'inspect').strip().lower()
            merged_in = {**nested, **{k: v for k, v in args.items() if k != 'args'}}
            try:
                cur_bot_name = get_assistant(self.store, self.bot_context.get()).get('name') or '主助理'
            except Exception:
                cur_bot_name = '主助理'

            if cua_action in ('send_screenshot', 'share_image', 'send_qr', 'send_image'):
                box_xyxy, box_err = validate_norm_box_xyxy(merged_in)
                if box_err:
                    return {'ok': False, 'op': 'cua_desktop', 'action': 'send_screenshot', 'error': box_err}
                fresh_res = await self._raw_dsh_rpc('cua_desktop', {'action': 'inspect', 'tool': 'inspect', 'bot_name': cur_bot_name}, timeout=float(args.get('timeout_sec') or 35.0))
                imgs = (fresh_res or {}).get('images') if isinstance(fresh_res, dict) else None
                b64_live = str((imgs[0] if isinstance(imgs, list) and imgs and isinstance(imgs[0], dict) else {}).get('data') or '').strip()
                if not isinstance(fresh_res, dict) or fresh_res.get('error') or fresh_res.get('ok') is False or not b64_live:
                    return {'ok': False, 'op': 'cua_desktop', 'action': 'send_screenshot', 'error': str((fresh_res or {}).get('error') or '未能实时获取电脑真机画面（拒绝发送历史缓存截图）')}
                raw_clean = base64.b64decode(b64_live.split(',')[-1])
                (self.harness.root / 'cua-clean.jpg').write_bytes(raw_clean)
                media_url = self._prepare_chat_screenshot(raw_clean, crop_box=box_xyxy, prefix='pc-cua-shot')
                if not media_url:
                    return {'ok': False, 'op': 'cua_desktop', 'action': 'send_screenshot', 'error': '电脑真机截图保存或挂载失败'}
                return {
                    'ok': True,
                    'op': 'cua_desktop',
                    'action': 'send_screenshot',
                    'chat_image_url': media_url,
                    'cropped': bool(crop_b),
                    'note': f'已将电脑真机干净无标尺实时截图直接挂载到本轮聊天回复气泡上（{media_url}）！',
                }

            if cua_action in ('zoom', 'crop', 'magnify'):
                box_xyxy, box_err = validate_norm_box_xyxy(merged_in)
                if box_err:
                    return {'ok': False, 'op': 'cua_desktop', 'action': cua_action, 'error': box_err}
                fresh_res = await self._raw_dsh_rpc('cua_desktop', {'action': 'inspect', 'tool': 'inspect', 'bot_name': cur_bot_name}, timeout=float(args.get('timeout_sec') or 35.0))
                imgs = (fresh_res or {}).get('images') if isinstance(fresh_res, dict) else None
                b64_live = str((imgs[0] if isinstance(imgs, list) and imgs and isinstance(imgs[0], dict) else {}).get('data') or '').strip()
                if not isinstance(fresh_res, dict) or fresh_res.get('error') or fresh_res.get('ok') is False or not b64_live:
                    return {'ok': False, 'op': 'cua_desktop', 'action': cua_action, 'error': str((fresh_res or {}).get('error') or '未能实时获取电脑真机画面用于局部放大')}
                raw_z = base64.b64decode(b64_live.split(',')[-1])
                (self.harness.root / 'cua-clean.jpg').write_bytes(raw_z)
                zoomed_bytes, z_meta = zoom_region_from_args(raw_z, merged_in)
                (self.harness.root / 'cua-zoom.jpg').write_bytes(zoomed_bytes)
                return {
                    'ok': True,
                    'op': 'cua_desktop',
                    'action': 'zoom',
                    'zoom_meta': z_meta,
                    'saved_screenshot': 'cua-zoom.jpg',
                    '_multimodal_mime': 'image/jpeg',
                    '_multimodal_b64': base64.b64encode(zoomed_bytes).decode('ascii'),
                    '_multimodal_caption': f'【Windows 电脑真机局部放大 ({z_meta.get("norm_box")})】边缘标尺已映射回整屏 0..1000 坐标系，请直接调用 pc(op="cua_desktop", action="tap", nx=..., ny=...)！',
                }

            if cua_action in ('tap', 'click', 'double_click', 'right_click', 'move', 'hover', 'long_press', 'type', 'drag'):
                need_req = cua_action in ('tap', 'click', 'double_click', 'right_click', 'long_press', 'drag')
                v_nx, v_ny, coord_err = _validate_pc_norm_coords(merged_in, cua_action, require_if_no_target=need_req)
                if coord_err:
                    return {'ok': False, 'op': 'cua_desktop', 'action': cua_action, 'error': coord_err}
                if v_nx is not None and v_ny is not None:
                    merged_in['nx'], merged_in['ny'] = v_nx, v_ny
                if cua_action == 'type' and v_nx is not None and v_ny is not None:
                    tap_payload = {**merged_in, 'action': 'tap', 'tool': 'tap', 'nx': v_nx, 'ny': v_ny, 'bot_name': cur_bot_name}
                    tap_res = await self._raw_dsh_rpc('cua_desktop', tap_payload, timeout=float(args.get('timeout_sec') or 35.0))
                    if not isinstance(tap_res, dict) or tap_res.get('error') or tap_res.get('ok') is False:
                        return {**(tap_res if isinstance(tap_res, dict) else {}), 'ok': False, 'op': 'cua_desktop', 'action': 'type', 'error': str((tap_res or {}).get('error') or '点击聚焦目标输入框失败')}
                    await asyncio.sleep(0.15)

            merged_cua_args = {**merged_in, 'action': cua_action, 'tool': cua_action, 'bot_name': cur_bot_name}
            res = await self._raw_dsh_rpc('cua_desktop', merged_cua_args, timeout=float(args.get('timeout_sec') or 35.0))
            if isinstance(res, dict):
                res.pop('raw', None)
                imgs = res.pop('images', None)
                if isinstance(imgs, list) and imgs and res.get('ok') is not False and not res.get('error'):
                    first_img = imgs[0] if isinstance(imgs[0], dict) else {}
                    b64_data = str(first_img.get('data') or '').strip()
                    if b64_data:
                        try:
                            raw_img = base64.b64decode(b64_data.split(',')[-1])
                            (self.harness.root / 'cua-clean.jpg').write_bytes(raw_img)
                            prev_pc = getattr(self, '_pc_prev_raw_jpg', None)
                            diff_pct = compute_frame_diff_pct(prev_pc, raw_img)
                            self._pc_prev_raw_jpg = raw_img
                            res['frame_hash'] = compute_perceptual_frame_hash(raw_img)
                            res['frame_diff_pct'] = diff_pct
                            res['ui_changed'] = True if prev_pc is None else bool(diff_pct >= 0.35)
                            annotated_bytes, iw, ih = draw_vision_ruler_overlay(
                                raw_img,
                                a11y_summary='',
                                screen_w=0,
                                screen_h=0,
                                pad_px=28,
                                draw_badges=False,
                            )
                            (self.harness.root / 'cua-latest.jpg').write_bytes(annotated_bytes)
                            res['saved_screenshot'] = 'cua-latest.jpg'
                            res['_multimodal_b64'] = base64.b64encode(annotated_bytes).decode('ascii')
                            res['_multimodal_mime'] = 'image/jpeg'
                            res['_multimodal_caption'] = (
                                f'【Windows 电脑真机窗口实时画面 (action={cua_action}, 尺寸={iw}x{ih}, Δ={diff_pct}%)】\n'
                                '1. 画面外框带有无遮挡 `0..1000` 归一化标尺，请直接依据视觉坐标调用 `pc(op="cua_desktop", action="tap", nx=..., ny=...)` 或 `action="type"`。\n'
                                '2. 小字或拥挤控件请先调用 `pc(op="cua_desktop", action="zoom", box=[xmin,ymin,xmax,ymax])` 放大核对；绝不猜测未核实坐标。'
                            )
                        except Exception:
                            res['_multimodal_b64'] = b64_data
                            res['_multimodal_mime'] = str(first_img.get('mimeType') or 'image/png')
                            res['_multimodal_caption'] = f'【Windows 电脑真机窗口实时画面 (action={cua_action})】'
            return res

        limit = max(1, min(100, int(args.get('limit') or 3)))
        req_source = str(args.get('source') or '').strip().lower()
        req_device_id = str(args.get('device_id') or '').strip()
        phone_online = bool(self.phone_online_checker()) if callable(self.phone_online_checker) else False

        if op == 'status':
            phone_dialogs, phone_last_time = self.extract_dialogs_by_source('phone', limit=limit)
            bot_names = [b.get('name', b.get('id', '')) for b in self.store.state.get('assistants', [])]
            if req_source in ('phone', 'phone2', '安卓一', '安卓二') or req_device_id.lower() in ('phone', 'phone2', 'phone_dsb', '安卓一', '安卓二'):
                norm_ph = normalize_node_source(req_source or req_device_id)
                ph_label = node_display_name(norm_ph)
                ph_dialogs, ph_last_time = self.extract_dialogs_by_source(norm_ph, limit=limit)
                lines = [
                    f"{ph_label}在线状态: {'🟢 在线' if phone_online else '⚪ 后台待命'} (最后活跃: {ph_last_time or '未知'}, 常驻Bot: {', '.join(bot_names)})",
                    f"{ph_label}最近 {len(ph_dialogs)} 条对话 (limit={limit}):",
                ]
                for idx, d in enumerate(ph_dialogs, 1):
                    lines.append(f"{idx}. [{d.get('time', '')}] ({d.get('bot_name', '主助理')}) 用户: {d.get('user', '')} -> AI: {d.get('assistant', '')}")
                return {
                    'ok': True,
                    'source': norm_ph,
                    'limit': limit,
                    'phone_online': phone_online,
                    'online_bots': bot_names,
                    'last_active_time': ph_last_time,
                    'dialogs': ph_dialogs,
                    'phone_dialogs': ph_dialogs,
                    'last_3_dialogs': ph_dialogs,
                    'summary': '\n'.join(lines),
                }

            res = await self._raw_dsh_rpc('status', {**args, 'limit': limit}, timeout=10.0)
            if req_device_id and not res.get('ok') and '未找到指定电脑设备' in str(res.get('error') or ''):
                return res
            target_id, target_peer = self.resolve_target_dsh_peer(req_device_id)
            pc_on = bool(isinstance(res, dict) and res.get('ok'))
            rpc_err = str(res.get('error') or '') if isinstance(res, dict) and not pc_on else ''
            pc_chat_dialogs, pc_chat_time = self.extract_dialogs_by_source('pc', limit=limit)
            dsh_dialogs = list(res.get('last_3_dialogs') or []) if isinstance(res, dict) else []
            peer_sessions = ((target_peer or {}).get('status') or {}).get('sessions') if isinstance(target_peer, dict) else None
            sessions = list(peer_sessions) if isinstance(peer_sessions, list) else self._get_dsh_sessions_list()
            combined_dialogs = (pc_chat_dialogs + dsh_dialogs)[-limit:]
            last_time = str(res.get('last_active_time') or pc_chat_time or '') if isinstance(res, dict) else pc_chat_time
            pc_status_str = '🟢 在线' if pc_on else (f'⚪ 离线/未响应 ({rpc_err})' if rpc_err else '⚪ 离线')
            lines = [
                f"手机端 (安卓一) 在线状态: {'🟢 在线' if phone_online else '⚪ 后台待命'} (最后活跃: {phone_last_time or '未知'}, 常驻Bot: {', '.join(bot_names)})",
                f"电脑端 (Windows一 / {target_id}) 在线状态: {pc_status_str} (最后活跃: {last_time or '未知'}, 常驻Bot: {', '.join(bot_names)})",
                f"手机端最近 {len(phone_dialogs)} 条对话:",
            ]
            if phone_dialogs:
                for idx, d in enumerate(phone_dialogs, 1):
                    lines.append(f"  {idx}. [{d.get('time', '')}] ({d.get('bot_name', '主助理')}) 用户: {str(d.get('user', ''))[:120]} -> AI: {str(d.get('assistant', ''))[:160]}")
            else:
                lines.append("  (暂无最近对话)")
            lines.append(f"电脑端最近 {len(combined_dialogs)} 条对话:")
            if combined_dialogs:
                for idx, d in enumerate(combined_dialogs, 1):
                    lines.append(f"  {idx}. [{d.get('time', '')}] ({d.get('session_title') or d.get('bot_name') or 'PC'}) 用户: {str(d.get('user', ''))[:120]} -> AI: {str(d.get('assistant', ''))[:160]}")
            else:
                lines.append("  (暂无最近对话)")
            lines.append("提示: 如需跨端协同或提醒其他 Bot 接手，请调用 blackboard(action='post', content='...', mentions=['Windows一']) 或 peek_bots(target='all')。")
            compact_sessions = [
                {'title': s.get('title'), 'short_id': s.get('short_id'), 'status': s.get('status', 'idle')}
                for s in sessions[:10]
            ]
            out_st = {
                'ok': True,
                'device_id': target_id,
                'source': normalize_node_source(req_source) if req_source else 'pc',
                'limit': limit,
                'sessions': sessions,
                'dialogs': combined_dialogs,
                'phone_online': phone_online,
                'phone_last_active_time': phone_last_time,
                'phone_dialogs': phone_dialogs,
                'pc_online': pc_on,
                'pc_last_active_time': last_time,
                'pc_dialogs': combined_dialogs,
                'online_bots': bot_names,
                'workspace_sessions': compact_sessions,
                'summary': '\n'.join(lines),
            }
            if rpc_err:
                out_st['rpc_error'] = rpc_err
            return out_st

        timeout = float(args.get('timeout_sec') or (180.0 if op == 'new_session' else 25.0))
        res = await self._raw_dsh_rpc(op, args, timeout=timeout)
        if isinstance(res, dict):
            wline = str(res.get('work_log_line') or '').strip()
            if wline:
                self.vault.append_work_log(wline)
        return res
