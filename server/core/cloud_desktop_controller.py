"""Cloud Desktop Controller (`cloud_desktop` / `cloud`) Mixin."""
from __future__ import annotations

import asyncio
import base64
import json
import re
from pathlib import Path

from core.vision_engine import enrich_cloud_desktop_vision, zoom_region_from_args

_OP_ALIASES = {'open_url': 'navigate', 'browser_snapshot': 'screenshot', 'tap': 'click', 'input': 'type', 'press': 'key', 'close': 'close_window', 'inspect': 'ui_tree', 'delay': 'wait', 'sleep': 'wait', 'upload': 'push_file', 'upload_file': 'push_file', 'send_file': 'push_file'}


def _parse_box_4(val) -> list[float] | None:
    if isinstance(val, (list, tuple)) and len(val) == 4:
        try:
            return [float(x) for x in val]
        except (TypeError, ValueError):
            return None
    if isinstance(val, str) and val.strip():
        s = val.strip()
        try:
            parsed = json.loads(s) if s.startswith('[') else [float(x) for x in re.split(r'[\s,]+', s) if x]
            if isinstance(parsed, list) and len(parsed) == 4:
                return [float(x) for x in parsed]
        except Exception:
            return None
    return None


def _validate_cloud_coords(args: dict, op: str, *, require_coords: bool = True) -> tuple[dict, str | None]:
    """Validate pure-vision 0..1000 `nx, ny` (or pixel `x, y` / `grid`) strictly; never default missing coords to (0,0)."""
    nx, ny = args.get('nx'), args.get('ny')
    x, y = args.get('x'), args.get('y')
    grid = str(args.get('grid') or '').strip()
    if nx is not None or ny is not None:
        if nx is None or ny is None:
            return {}, f'{op} 必须同时提供 nx 和 ny (0..1000 归一化视觉坐标)'
        try:
            nxf, nyf = float(nx), float(ny)
        except (TypeError, ValueError):
            return {}, f'{op} 坐标格式错误: nx={nx}, ny={ny} 必须为 0..1000 数值'
        if not (0.0 <= nxf <= 1000.0 and 0.0 <= nyf <= 1000.0):
            return {}, f'{op} 坐标越界: nx={nx}, ny={ny} 必须在 0..1000 范围内'
        return {'nx': int(round(nxf)), 'ny': int(round(nyf)), 'x': int(x) if x is not None else 0, 'y': int(y) if y is not None else 0, 'grid': grid}, None
    if x is not None or y is not None:
        if x is None or y is None:
            return {}, f'{op} 必须同时提供 x 和 y'
        try:
            xf, yf = int(round(float(x))), int(round(float(y)))
        except (TypeError, ValueError):
            return {}, f'{op} 像素坐标格式错误: x={x}, y={y}'
        if xf < 0 or yf < 0:
            return {}, f'{op} 像素坐标不可为负: x={x}, y={y}'
        return {'x': xf, 'y': yf, 'nx': -1, 'ny': -1, 'grid': grid}, None
    if grid:
        return {'x': 0, 'y': 0, 'nx': -1, 'ny': -1, 'grid': grid}, None
    if require_coords:
        return {}, f'{op} 必须提供 nx, ny (0..1000 视觉坐标)'
    return {'x': 640, 'y': 400, 'nx': -1, 'ny': -1, 'grid': ''}, None


class CloudDesktopControllerMixin:
    def _write_clean_desktop_shot(self, raw_clean: bytes, write_latest: bool = False) -> None:
        if not raw_clean:
            return
        try:
            uploads_dir = self.harness.root / 'uploads'
            uploads_dir.mkdir(parents=True, exist_ok=True)
            (self.harness.root / 'desktop-clean.jpg').write_bytes(raw_clean)
            (uploads_dir / 'desktop-clean.jpg').write_bytes(raw_clean)
            if write_latest:
                (self.harness.root / 'desktop-latest.jpg').write_bytes(raw_clean)
                (uploads_dir / 'desktop-latest.jpg').write_bytes(raw_clean)
        except Exception:
            pass

    def _read_clean_desktop_shot(self) -> bytes | None:
        for rel in ('uploads/desktop-clean.jpg', 'desktop-clean.jpg', 'uploads/desktop-latest.jpg', 'desktop-latest.jpg'):
            p = self.harness.root / rel
            if p.is_file():
                try:
                    data = p.read_bytes()
                    if data:
                        return data
                except Exception:
                    continue
        return None

    async def _broadcast_desktop_frame(self, res_or_frame: dict, op: str, write_latest: bool = True):
        if not isinstance(res_or_frame, dict):
            return
        b64 = res_or_frame.get('image')
        if b64:
            try:
                self._write_clean_desktop_shot(base64.b64decode(str(b64).split(',')[-1]), write_latest=write_latest)
            except Exception:
                pass
        room_id = self.room_context.get() or self.bot_context.get()
        await self.publish({
            'type': 'cloud_desktop_frame', 'bot_id': room_id, 'speaker_id': self.bot_context.get(), 'op': op,
            'frame': {
                'hash': res_or_frame.get('hash', ''), 'url': res_or_frame.get('url', ''),
                'title': res_or_frame.get('title', ''), 'width': res_or_frame.get('width', 1280), 'height': res_or_frame.get('height', 800),
            },
        })

    async def _finalize_desktop_result(self, res: dict, op: str, tap_xy: tuple[int, int] | None = None) -> dict:
        if not isinstance(res, dict):
            return res
        st_box = getattr(self, '_desktop_vision_state', None)
        if st_box is None:
            st_box = {}
            self._desktop_vision_state = st_box
        if 'auth_gate' in res:
            st_box['last_auth_gate'] = res.get('auth_gate')
        if bool(res.get('user_takeover_locked')):
            res['user_takeover_locked'] = True
            res['control'] = 'user'
            res.setdefault('takeover_notice', '当前云电脑正由用户人工接管中（user_takeover_locked=True），操作已被锁定。请等待用户交还控制权后再继续操作。')

        if not str(res.get('image') or '').strip():
            res.setdefault('x11_windows', list(res.get('x11_windows') or res.get('windows') or []))
            return res
        await self._broadcast_desktop_frame(res, op, write_latest=False)
        clean_res = enrich_cloud_desktop_vision(res, op, self.harness.root, st_box, tap_xy=tap_xy)
        w, h = int(clean_res.get('width') or 1280), int(clean_res.get('height') or 800)
        clean_res['saved_screenshot'] = 'desktop-latest.jpg'
        ui_flag = '画面有变化' if clean_res.get('ui_changed') else '画面基本未变'
        if clean_res.get('_multimodal_b64') and not clean_res.get('_multimodal_caption'):
            prefix = f"⚠️ {clean_res['takeover_notice']}\n" if clean_res.get('takeover_notice') else ''
            gate = clean_res.get('auth_gate')
            if isinstance(gate, dict) and gate.get('kind'):
                prefix += f"🔐 检测到登录/验证门槛 ({gate.get('kind')}, bbox={gate.get('bbox')})：若需用户扫码/过验证，请调 `cloud(op=\"send_screenshot\", box=...)` 发清晰图并交还控制权。\n"
            clean_res['_multimodal_caption'] = (
                f"{prefix}【云电脑最新画面 (op={op}, {ui_flag} Δ={clean_res.get('frame_diff_pct')}%)】\n"
                '画面已零偏移对齐 0..1000 归一化坐标系，直接传 nx, ny (0..1000) 或 @bN 调用 click/type；发二维码请用 send_screenshot(box=[nx1,ny1,nx2,ny2]) 或不带 box 发整屏。'
            )
        return clean_res

    async def _stage_pc_file_to_cloud_workspace(self, raw_p: str) -> Path | None:
        if not raw_p or not self.is_dsh_online():
            return None
        pc_res = await self._raw_dsh_rpc('pc_fs', {'fs_op': 'read_file', 'path': raw_p, 'base64': True}, timeout=25.0)
        if not isinstance(pc_res, dict) or not pc_res.get('ok'):
            return None
        b64 = str(pc_res.get('_multimodal_b64') or pc_res.get('data_b64') or '').strip()
        if b64:
            raw_bytes = base64.b64decode(b64.split(',')[-1])
        elif isinstance(pc_res.get('content'), str):
            raw_bytes = pc_res['content'].encode('utf-8')
        else:
            return None
        safe_name = re.sub(r'[^a-zA-Z0-9._\u4e00-\u9fa5-]', '_', Path(raw_p.replace('\\', '/')).name) or 'pc-file.bin'
        staged = self.harness.root / 'uploads' / safe_name
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(raw_bytes)
        return staged

    def _resolve_local_upload_candidate(self, raw_path: str) -> Path | None:
        raw = str(raw_path or '').strip().replace('\\', '/')
        if not raw or re.match(r'^[A-Za-z]:[/\\]', raw):
            return None
        root = self.harness.root.resolve()
        rel = raw[len('workspace/'):] if raw.startswith('workspace/') else raw
        candidates = [root / rel] + ([root / 'uploads' / rel] if '/' not in rel else [])
        tail = rel[len('uploads/'):] if rel.startswith('uploads/') else (rel if '/' not in rel else '')
        if tail and '/' not in tail:
            clean = re.sub(r'^[0-9a-f]{10}-', '', tail)
            if clean:
                candidates.append(root / 'uploads' / clean)
            uploads_dir = root / 'uploads'
            if uploads_dir.is_dir():
                for match in sorted(uploads_dir.glob('*-' + tail), key=lambda p: p.stat().st_mtime, reverse=True):
                    if re.match(r'^[0-9a-f]{10}-', match.name):
                        candidates.append(match)
                        break
        for cand in candidates:
            try:
                resolved = cand.resolve()
                if resolved.is_file() and resolved.is_relative_to(root):
                    return resolved
            except OSError:
                continue
        return None

    async def _handle_cloud_file_op(self, op: str, args: dict) -> dict | None:
        if op in ('files', 'list_files'):
            res = await self.computer.client.call('GET', '/files')
            if isinstance(res, dict) and 'ok' not in res:
                res['ok'] = True
            return res
        if op == 'pull_file':
            rel_p = str(args.get('path') or args.get('target') or args.get('text') or '').strip()
            if not rel_p:
                files_res = await self.computer.client.call('GET', '/files')
                items = (files_res.get('files') or []) if isinstance(files_res, dict) else []
                if not items:
                    return {'ok': False, 'error': '云电脑下载/工作区目录暂无文件'}
                rel_p = str(items[0].get('path') or '')
            if not rel_p.startswith(('downloads/', 'workspace/')):
                rel_p = 'downloads/' + rel_p.lstrip('/')
            dest_dir = self.harness.root / 'uploads'
            dest_dir.mkdir(parents=True, exist_ok=True)
            clean_name = re.sub(r'^[0-9a-f]{10}-', '', Path(rel_p).name) or Path(rel_p).name
            receipt = await self.computer.client.get_file(rel_p, dest_dir / clean_name)
            return {'ok': True, 'op': 'pull_file', 'source_path': rel_p, 'workspace_path': f'uploads/{clean_name}', 'name': clean_name, 'bytes': receipt.get('bytes', 0)}
        if op == 'push_file':
            inline_content, inline_b64 = args.get('content'), args.get('data_b64')
            orig_p = str(args.get('name') or args.get('path') or args.get('target') or args.get('text') or '').strip()
            if inline_content is not None or inline_b64 is not None:
                raw_name = Path(orig_p.replace('\\', '/')).name if orig_p else 'upload.txt'
                clean_name = re.sub(r'^[0-9a-f]{10}-', '', raw_name) or raw_name
                dest_dir = self.harness.root / 'uploads'
                dest_dir.mkdir(parents=True, exist_ok=True)
                src_file = dest_dir / clean_name
                src_file.write_bytes(base64.b64decode(str(inline_b64))) if inline_b64 is not None else src_file.write_text(str(inline_content), encoding='utf-8')
                rel_p = f'uploads/{clean_name}'
            else:
                src_file = self._resolve_local_upload_candidate(orig_p) or await self._stage_pc_file_to_cloud_workspace(orig_p)
                if src_file is None or not src_file.is_file():
                    return {'ok': False, 'error': f'工作区或本机文件不存在: {orig_p}'}
                clean_name = re.sub(r'^[0-9a-f]{10}-', '', src_file.name) or src_file.name
                try:
                    rel_p = src_file.resolve().relative_to(self.harness.root.resolve()).as_posix()
                except ValueError:
                    rel_p = f'uploads/{clean_name}'
            receipt = await self.computer.client.upload_file(src_file, remote_name=clean_name)
            return {'ok': True, 'op': 'push_file', 'workspace_path': rel_p, 'desktop_path': receipt.get('path', f'workspace/uploads/{clean_name}'), 'name': clean_name, 'bytes': receipt.get('bytes', src_file.stat().st_size), 'sha256': receipt.get('sha256', '')}
        return None

    async def _handle_cloud_send_screenshot(self, args: dict) -> dict:
        raw_box = None
        for bk in ('box', 'zoom_box', 'crop_box'):
            if args.get(bk) is not None:
                raw_box = args.get(bk)
                break
        box_xyxy: list[float] | None = None
        if raw_box is not None:
            box_xyxy = _parse_box_4(raw_box)
            if not box_xyxy or not (0 <= box_xyxy[0] < box_xyxy[2] <= 1000 and 0 <= box_xyxy[1] < box_xyxy[3] <= 1000):
                return {'ok': False, 'op': 'send_screenshot', 'error': f'无效的裁剪框 {raw_box}：必须为 [xmin, ymin, xmax, ymax] 且满足 0..1000 范围'}

        frame: dict = {}
        raw_clean: bytes | None = None
        try:
            fr = await self.computer.client.call('GET', '/frame')
            if isinstance(fr, dict):
                frame = fr
                b64_f = str(frame.get('image') or '').strip()
                if b64_f:
                    raw_clean = base64.b64decode(b64_f.split(',')[-1])
                await self._broadcast_desktop_frame(frame, 'send_screenshot', write_latest=False)
        except Exception as exc:
            return {'ok': False, 'op': 'send_screenshot', 'error': f'获取云电脑实时截图失败: {exc}'}
        if not raw_clean:
            return {'ok': False, 'op': 'send_screenshot', 'error': '云电脑未返回实时画面（拒绝发送历史缓存截图）'}

        st_box = getattr(self, '_desktop_vision_state', None) or {}
        gate = frame.get('auth_gate') or st_box.get('last_auth_gate')
        crop_arg: list[float] | None = None
        if box_xyxy:
            crop_arg = box_xyxy
        elif not args.get('full_page') and isinstance(gate, dict) and gate.get('kind') == 'qr_login':
            gbox = _parse_box_4(gate.get('bbox'))
            if gbox and 0 <= gbox[0] < gbox[2] <= 1000 and 0 <= gbox[1] < gbox[3] <= 1000:
                crop_arg = [
                    max(0.0, gbox[0] - 45.0),
                    max(0.0, gbox[1] - 45.0),
                    min(1000.0, gbox[2] + 45.0),
                    min(1000.0, gbox[3] + 45.0),
                ]

        media_url = self._prepare_chat_screenshot(raw_clean, crop_box=crop_arg, prefix='desktop-shot')
        if not media_url:
            return {'ok': False, 'op': 'send_screenshot', 'error': '云电脑截图保存或挂载失败'}
        rel_shot = media_url.removeprefix('/workspace-media/').lstrip('/')
        shot_path = self.harness.root / rel_shot
        verify_b64 = ''
        if shot_path.is_file():
            verify_b64 = base64.b64encode(shot_path.read_bytes()).decode('ascii')
        return {
            'ok': True, 'op': 'send_screenshot', 'chat_image_url': media_url, 'cropped': bool(crop_arg),
            'crop_box': crop_arg,
            **({'_multimodal_b64': verify_b64, '_multimodal_mime': 'image/jpeg', '_multimodal_caption': '【已发出给用户的实拍截图预览】请确认目标内容/二维码是否完整居中，若有裁偏请不带 box 发整屏重发。'} if verify_b64 else {}),
            'note': f'已将云电脑干净无标尺实时截图直接挂载到本轮聊天回复气泡上（{media_url}），请核对附图确保关键信息完整。',
        }

    async def _tool_cloud_desktop(self, args: dict):
        if self.computer is None:
            return {'error': '云桌面组件未挂载'}
        op = _OP_ALIASES.get(str(args.get('op') or args.get('action') or 'status').strip().lower(), str(args.get('op') or args.get('action') or 'status').strip().lower())
        if op == 'open':
            tv = str(args.get('url') or args.get('uri') or args.get('target') or args.get('app') or args.get('text') or '').strip()
            if tv.startswith(('http://', 'https://')) or ('.' in tv and ' ' not in tv and not tv.startswith('/')):
                args, op = {**args, 'op': 'navigate', 'url': tv if tv.startswith(('http://', 'https://')) else ('https://' + tv)}, 'navigate'
            else:
                args, op = {**args, 'op': 'launch_app', 'app': tv or 'chrome'}, 'launch_app'

        try:
            file_res = await self._handle_cloud_file_op(op, args)
            if file_res is not None:
                return file_res
            if op in ('send_screenshot', 'share_image', 'send_qr', 'send_image'):
                return await self._handle_cloud_send_screenshot(args)
            if op == 'status':
                st = await self.computer.client.status(force=True)
                if not isinstance(st, dict) or st.get('available') is False or st.get('ok') is False or st.get('error'):
                    return {**(st if isinstance(st, dict) else {}), 'ok': False, 'op': 'status'}
                try:
                    fr = await self.computer.client.call('GET', '/frame')
                    fr_ok = isinstance(fr, dict) and not fr.get('error') and fr.get('ok') is not False
                    merged = {**st, **(fr if isinstance(fr, dict) else {}), 'ok': fr_ok, 'x11_windows': list(st.get('x11_windows') or st.get('windows') or [])}
                    return await self._finalize_desktop_result(merged, 'status')
                except Exception as exc:
                    if isinstance(st, dict) and 'x11_windows' not in st:
                        st['x11_windows'] = list(st.get('windows') or [])
                    return {**st, 'frame_error': str(exc)}
            if op in ('zoom', 'crop', 'magnify'):
                fr, st = {}, {}
                try:
                    fr = await self.computer.client.call('GET', '/frame') or {}
                except Exception as exc:
                    return {'ok': False, 'op': op, 'error': f'获取云电脑实时画面失败: {exc}'}
                try:
                    st = await self.computer.client.status(force=True) or {}
                except Exception:
                    pass
                b64_f = str((fr or {}).get('image') or '').strip()
                if not b64_f:
                    return {'ok': False, 'op': op, 'error': '未能实时获取云电脑画面用于局部放大'}
                raw_z = base64.b64decode(b64_f.split(',')[-1])
                self._write_clean_desktop_shot(raw_z, write_latest=False)
                zoomed_bytes, z_meta = zoom_region_from_args(raw_z, args)
                (self.harness.root / 'desktop-zoom.jpg').write_bytes(zoomed_bytes)
                uploads_dir = self.harness.root / 'uploads'
                uploads_dir.mkdir(parents=True, exist_ok=True)
                (uploads_dir / 'desktop-zoom.jpg').write_bytes(zoomed_bytes)
                return {
                    'ok': True, 'op': 'zoom', 'zoom_meta': z_meta, 'x11_windows': list((st or {}).get('x11_windows') or (st or {}).get('windows') or []),
                    'url': (st or {}).get('url', ''), 'title': (fr or {}).get('title') or (st or {}).get('title', ''),
                    'saved_screenshot': 'desktop-zoom.jpg', '_multimodal_mime': 'image/jpeg', '_multimodal_b64': base64.b64encode(zoomed_bytes).decode('ascii'),
                    '_multimodal_caption': f'【云电脑局部放大 ({z_meta.get("scale", 2)}x, norm_box={z_meta.get("norm_box")})】\n边缘标尺已映射回整屏真实全局 `0..1000` 归一化坐标，请直接调用 `cloud(op="click", nx=..., ny=...)`！',
                }
            if op in ('screenshot', 'capture', 'frame', 'ui_tree', 'read_screen', 'nodes'):
                frame = await self.computer.client.call('GET', '/frame')
                st = await self.computer.client.status(force=True)
                if not isinstance(frame, dict) or frame.get('error') or frame.get('ok') is False or not str(frame.get('image') or '').strip():
                    return {'ok': False, 'op': op, 'error': str((frame or {}).get('error') or '云电脑未返回有效实时画面')}
                out = {
                    **frame, 'ok': True, 'captured': True, 'width': frame.get('width', 1280), 'height': frame.get('height', 800),
                    'title': frame.get('title') or st.get('title', ''), 'url': st.get('url', ''), 'windows': st.get('windows', []),
                    'window_count': st.get('window_count', 0), 'dom_elements': st.get('dom_elements', []), 'page_text': st.get('page_text', ''), 'saved_to': 'desktop-latest.jpg',
                }
                return await self._finalize_desktop_result(out, 'inspect')
            if op in ('bash', 'exec', 'shell', 'terminal') or (op == 'run' and (args.get('command') is not None or args.get('goal') or args.get('is_input'))):
                is_input = bool(args.get('is_input', False))
                raw_cmd = args.get('command')
                cmd = '' if raw_cmd is None else str(raw_cmd)
                if not is_input:
                    cmd = cmd.strip()
                    goal = str(args.get('goal') or args.get('text') or args.get('url') or '').strip()
                    if not cmd and goal:
                        m_url = re.search(r'https?://[^\s"\'<>]+', goal)
                        if m_url and not any(k in goal for k in (';', '&&', '|', 'python', 'pip', 'gcc', 'ls ', 'cat ', 'echo ')):
                            return await self._finalize_desktop_result(await self.computer.client.call('POST', '/navigate', body={'url': m_url.group(0), 'include_frame': True}), 'navigate')
                        cmd = goal
                    if not cmd:
                        return {'ok': False, 'error': 'cloud(op="bash") 需要提供 command（向挂起会话发送交互输入请传 is_input=True）'}
                elif raw_cmd is None and args.get('text') is not None:
                    cmd = str(args.get('text'))
                timeout_s = max(5, min(90, int(args.get('timeout') or 25)))
                res = await self.computer.client.call('POST', '/input', body={'kind': 'bash', 'command': cmd, 'is_input': is_input, 'timeout': timeout_s, 'include_frame': True})
                if isinstance(res, dict) and (res.get('timed_out') or (res.get('exit_code') is not None and int(res['exit_code']) != 0)):
                    res['ok'] = False
                    res.setdefault('error', f"云电脑命令执行失败 (exit_code={res.get('exit_code')}, timed_out={bool(res.get('timed_out'))})")
                return await self._finalize_desktop_result(res, 'bash')
            if op == 'navigate':
                return await self._finalize_desktop_result(await self.computer.client.call('POST', '/navigate', body={'url': str(args.get('url') or args.get('target') or args.get('text') or ''), 'include_frame': True}), 'navigate')
            if op in ('search', 'web_search'):
                q = str(args.get('query') or args.get('text') or args.get('goal') or args.get('url') or '').strip()
                return await self._finalize_desktop_result(await self.computer.client.call('POST', '/search', body={'query': q, 'engine': str(args.get('engine') or args.get('target') or 'auto').strip(), 'include_frame': True}), 'search')
            if op in ('launch_app', 'app', 'open_app'):
                app_name = str(args.get('app') or args.get('target') or args.get('text') or 'lite_browser').strip()
                return await self._finalize_desktop_result(await self.computer.client.call('POST', '/input', body={'kind': 'launch_app', 'app': app_name, 'text': str(args.get('url') or args.get('text') or ''), 'include_frame': True}), 'launch_app')
            if op == 'batch':
                steps = args.get('steps') if isinstance(args.get('steps'), list) else []
                return await self._finalize_desktop_result(await self.computer.client.call('POST', '/input', body={'kind': 'batch', 'steps': steps[:20], 'include_frame': True}), 'batch')

            ref_in, target_in = str(args.get('ref') or '').strip(), str(args.get('target') or '').strip()
            if not ref_in and target_in.startswith(('@b', '@v', '@e')):
                ref_in, target_in = target_in, ''
            if ref_in and not ref_in.startswith('@b') and op in ('click', 'type'):
                return {'ok': False, 'op': op, 'error': f'不支持的 ref="{ref_in}"，请直接使用纯视觉 0..1000 坐标 nx, ny 操作'}
            if op == 'click' and ref_in.startswith('@b'):
                op, args = 'click_ref', {**args, 'ref': ref_in}
            elif op == 'type' and ref_in.startswith('@b'):
                op, args = 'type_ref', {**args, 'ref': ref_in}
            if op in ('click_text', 'tap_node', 'click_node') or (op == 'click' and args.get('x') is None and args.get('nx') is None and not args.get('grid') and (args.get('text') or target_in or ref_in)):
                txt_q = str(target_in or args.get('text') or args.get('goal') or ref_in).strip()
                click_flag = bool(args.get('click', True))
                res = await self.computer.client.call('POST', '/input', body={'kind': 'find_text', 'text': txt_q, 'click': click_flag, 'include_frame': True})
                if isinstance(res, dict):
                    res['clicked'] = bool(res.get('clicked', False))
                    if click_flag and not res['clicked']:
                        res['ok'] = False
                        res.setdefault('error', f'未找到可点击文本 "{txt_q}"，请改用 nx, ny (0..1000 视觉坐标) 点击')
                return await self._finalize_desktop_result(res, 'find_text')
            if op in ('click', 'double_click', 'right_click', 'type', 'key', 'hotkey', 'scroll', 'move', 'hover', 'drag', 'wait', 'find_text', 'focus_window', 'close_window', 'tile_windows', 'clipboard', 'click_ref', 'type_ref'):
                tap_xy: tuple[int, int] | None = None
                payload: dict = {'kind': op, 'include_frame': True}
                if op in ('click', 'double_click', 'right_click', 'move', 'hover'):
                    coords, c_err = _validate_cloud_coords(args, op, require_coords=True)
                    if c_err:
                        return {'ok': False, 'op': op, 'error': c_err}
                    payload.update(coords)
                elif op == 'type':
                    has_coords = any(args.get(k) is not None for k in ('nx', 'ny', 'x', 'y')) or bool(args.get('grid'))
                    if has_coords:
                        coords, c_err = _validate_cloud_coords(args, op, require_coords=True)
                        if c_err:
                            return {'ok': False, 'op': op, 'error': c_err}
                        payload.update(coords)
                    elif target_in:
                        ft_res = await self.computer.client.call('POST', '/input', body={'kind': 'find_text', 'text': target_in, 'click': True, 'include_frame': False})
                        if not isinstance(ft_res, dict) or not ft_res.get('clicked') or ft_res.get('error') or ft_res.get('ok') is False:
                            return {'ok': False, 'op': op, 'error': f'未找到目标输入框 "{target_in}"，已阻止盲输入；请传 nx, ny (0..1000) 视觉坐标'}
                        if ft_res.get('x') is not None and ft_res.get('y') is not None:
                            payload.update(x=int(ft_res['x']), y=int(ft_res['y']), nx=-1, ny=-1, grid='')
                    elif args.get('allow_focused') is False:
                        return {'ok': False, 'op': op, 'error': 'cloud(op="type") 已明确禁用 allow_focused=False，必须提供 nx, ny (0..1000 视觉坐标) 定位输入框'}
                    payload.update(text=str(args.get('text') or ''), clear=bool(args.get('clear', False)), submit=bool(args.get('submit', False)))
                elif op == 'drag':
                    coords, c_err = _validate_cloud_coords(args, op, require_coords=True)
                    if c_err:
                        return {'ok': False, 'op': op, 'error': c_err}
                    for k_end in ('nx2', 'ny2'):
                        if args.get(k_end) is not None:
                            try:
                                v_end = float(args[k_end])
                            except (TypeError, ValueError):
                                return {'ok': False, 'op': op, 'error': f'drag 坐标格式错误: {k_end}={args[k_end]}'}
                            if not (0.0 <= v_end <= 1000.0):
                                return {'ok': False, 'op': op, 'error': f'drag 坐标越界: {k_end}={args[k_end]} 必须在 0..1000 范围内'}
                    nx_v, ny_v = coords['nx'], coords['ny']
                    px_s = coords['x'] if nx_v < 0 else round(nx_v * 1280 / 1000.0)
                    py_s = coords['y'] if ny_v < 0 else round(ny_v * 800 / 1000.0)
                    dx_v, dy_v = int(round(float(args.get('dx') or 0))), int(round(float(args.get('dy') or 0)))
                    has_end = any(args.get(k) is not None and str(args.get(k)).strip() != '' for k in ('x2', 'y2', 'nx2', 'ny2', 'grid2'))
                    x2_v = int(args.get('x2') or 0) if has_end else max(0, min(1280, px_s + dx_v))
                    y2_v = int(args.get('y2') or 0) if has_end else max(0, min(800, py_s + dy_v))
                    nx2_v = (int(round(float(args['nx2']))) if args.get('nx2') is not None else -1) if has_end else -1
                    ny2_v = (int(round(float(args['ny2']))) if args.get('ny2') is not None else -1) if has_end else -1
                    payload.update(x=coords['x'], y=coords['y'], x2=x2_v, y2=y2_v, nx=nx_v, ny=ny_v, nx2=nx2_v, ny2=ny2_v, dx=dx_v, dy=dy_v, grid=coords['grid'], grid2=str(args.get('grid2') or ''))
                elif op == 'wait':
                    payload.update(amount=max(1, min(10, int(args.get('seconds') or args.get('amount') or (max(1, int(args.get('duration_ms') or 1000) // 1000)) or 1))))
                elif op == 'find_text':
                    click_flag = bool(args.get('click', True))
                    payload.update(text=str(args.get('text') or args.get('query') or args.get('target') or ''), click=click_flag)
                elif op == 'clipboard':
                    payload.update(text=str(args.get('text') or ''))
                elif op in ('key', 'hotkey'):
                    payload.update(key=str(args.get('key') or args.get('target') or args.get('text') or 'Return'))
                elif op == 'scroll':
                    coords, c_err = _validate_cloud_coords(args, op, require_coords=False)
                    if c_err:
                        return {'ok': False, 'op': op, 'error': c_err}
                    payload.update(**coords, direction=str(args.get('direction') or args.get('text') or 'down').strip().lower(), amount=max(1, min(20, int(args.get('amount') or 4))))
                elif op in ('focus_window', 'close_window'):
                    payload.update(target=str(args.get('target') or args.get('app') or args.get('text') or ''))
                elif op in ('click_ref', 'type_ref'):
                    payload.update(ref=str(args.get('ref') or args.get('target') or ''), text=str(args.get('text') or ''), clear=bool(args.get('clear', False)), submit=bool(args.get('submit', False)))
                res = await self.computer.client.call('POST', '/input', body=payload)
                if isinstance(res, dict) and op == 'find_text':
                    res['clicked'] = bool(res.get('clicked', False))
                    if bool(args.get('click', True)) and not res['clicked']:
                        res['ok'] = False
                        res.setdefault('error', f'未找到可点击文本 "{payload.get("text", "")}"，请改用 nx, ny (0..1000) 视觉坐标点击')
                if isinstance(res, dict) and res.get('x') is not None and res.get('y') is not None and op in ('click', 'double_click', 'right_click', 'type'):
                    try:
                        tap_xy = (int(res['x']), int(res['y']))
                    except (ValueError, TypeError):
                        pass
                return await self._finalize_desktop_result(res, op, tap_xy=tap_xy)
            return {'ok': False, 'error': f'未知 cloud op: {op}'}
        except Exception as e:
            return {'ok': False, 'error': str(e)[:260]}
