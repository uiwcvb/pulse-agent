"""Pulse · Lightweight Non-Intrusive Desktop Agent (`desktop/desktop_agent.py`).

Three-Layer Non-Intrusive PC Control Architecture (Mirrors Android VirtualDisplay):
- Layer 1 (Zero-UI Fast Path): `shell` (CREATE_NO_WINDOW PowerShell/Bash), `clipboard` (Win32 native),
  `open_url`, `launch` (`SW_SHOWNOACTIVATE` without stealing focus).
- Layer 2 (Background HWND & Semantic UI Tree `@n1..@nN`):
  - `windows`: Enumerates visible top-level windows (`hwnd`, `title`, `process`, `bounds`).
  - `tree` / `ui_tree`: Extracts interactive `@n1..@nN` controls via Windows UI Automation (if available)
    + built-in zero-dependency Win32 `EnumChildWindows` traversal.
  - `screenshot`: Captures a specific background/occluded window via Win32 `PrintWindow(..., PW_RENDERFULLCONTENT)`
    without bringing it to the foreground!
  - `click_node` / `type_node`: Dispatches UIA `InvokePattern`/`ValuePattern` or Win32 `PostMessageW`
    (`WM_LBUTTONDOWN`/`WM_LBUTTONUP`/`WM_CHAR`/`WM_SETTEXT`) directly to the window without moving the
    user's physical mouse or stealing keyboard focus!
- Layer 3 (Physical Fallback with Human Activity Guard & <12ms Cursor Save-and-Restore):
  - Checks `GetLastInputInfo()` before any physical `SendInput`; if the user is actively moving the mouse,
    defers instead of fighting the user's cursor.
- Zero Idle Drain: 100% event-driven over WebSocket (`/ws/device`), 0% idle CPU, 0 background polling.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
import ctypes.wintypes as wt
import io
import json
import os
import platform
import random
import secrets
import shlex
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import contextlib

import math
from desktop_win32_uia import (
    IS_WIN, user32, kernel32, WM_SETTEXT, WM_CLOSE, WM_KEYDOWN, WM_KEYUP, WM_CHAR,
    WM_COMMAND, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONDOWN, WM_RBUTTONUP,
    WM_LBUTTONDBLCLK, WM_MOUSEWHEEL, BM_CLICK, MK_LBUTTON, VK_RETURN, VK_BACK,
    VK_TAB, VK_ESCAPE, SW_SHOWNOACTIVATE, makelparam, get_human_idle_seconds,
    get_screen_size, get_virtual_screen_rect, _get_hwnd_text, _get_hwnd_class,
    _get_hwnd_process_name, list_windows, win32_clipboard_get_checked,
    win32_clipboard_set_checked, capture_window_jpeg, win32_last_error,
)


class DesktopWorkspaceHarness:
    def __init__(self) -> None:
        self.last_hwnd: int = 0
        self.cache_hwnd: int = 0
        self.cache_pid: int = 0
        self.window_generation: int = 0
        self.node_cache: dict[str, dict[str, Any]] = {}

    def resolve_target_hwnd(self, args: dict[str, Any]) -> int:
        if not IS_WIN:
            return 0
        raw_h = args.get('hwnd')
        if raw_h is not None and str(raw_h).strip():
            try:
                explicit = int(str(raw_h).strip(), 0)
            except (ValueError, TypeError):
                return 0
            if explicit and user32.IsWindow(explicit):
                self.last_hwnd = explicit
                return explicit
            return 0
        win_q = str(args.get('window') or args.get('app') or args.get('title') or '').strip()
        if win_q:
            wins = list_windows(win_q)
            if wins:
                self.last_hwnd = int(wins[0]['hwnd'])
                return self.last_hwnd
            return 0
        if self.last_hwnd and user32.IsWindow(self.last_hwnd):
            return self.last_hwnd
        return 0

    def _require_target_hwnd(self, args: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
        hwnd = self.resolve_target_hwnd(args)
        if hwnd and (not IS_WIN or user32.IsWindow(hwnd)):
            return hwnd, None
        q = str(args.get('window') or args.get('app') or args.get('title') or args.get('hwnd') or '').strip()
        return 0, {
            'ok': False,
            'error': 'window_not_found',
            'detail': f'未找到匹配窗口: {q}' if q else '请先指定有效窗口 (hwnd / window / app)',
        }

    def _resolve_coords(
        self, hwnd: int, args: dict[str, Any], allow_center: bool = False
    ) -> tuple[int | None, int | None, dict[str, Any] | None]:
        has_nx, has_ny = args.get('nx') is not None, args.get('ny') is not None
        if has_nx or has_ny:
            if not (has_nx and has_ny):
                return None, None, {'ok': False, 'error': 'invalid_coordinates', 'detail': 'nx 与 ny 必须同时提供'}
            try:
                nx = float(args['nx'])
                ny = float(args['ny'])
            except (TypeError, ValueError):
                return None, None, {'ok': False, 'error': 'invalid_coordinates', 'detail': 'nx 与 ny 必须为数字'}
            if not (math.isfinite(nx) and math.isfinite(ny)) or nx < 0 or nx > 1000 or ny < 0 or ny > 1000:
                return None, None, {'ok': False, 'error': 'invalid_coordinates', 'detail': f'归一化坐标越界 ({nx}, {ny})，必须在 [0, 1000] 内'}
            if IS_WIN and hwnd and user32.IsWindow(hwnd):
                rect = wt.RECT()
                if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    return None, None, {'ok': False, 'error': f'GetWindowRect 失败 (err={win32_last_error()})'}
                w = max(1, int(rect.right - rect.left))
                h = max(1, int(rect.bottom - rect.top))
            else:
                _, _, w, h = get_virtual_screen_rect()
            return round((nx / 1000.0) * w), round((ny / 1000.0) * h), None
        raw_x, raw_y = args.get('x'), args.get('y')
        if raw_x is not None or raw_y is not None:
            if raw_x is None or raw_y is None:
                return None, None, {'ok': False, 'error': 'invalid_coordinates', 'detail': 'x 与 y 必须同时提供'}
            try:
                return int(round(float(raw_x))), int(round(float(raw_y))), None
            except (TypeError, ValueError):
                return None, None, {'ok': False, 'error': 'invalid_coordinates', 'detail': 'x 与 y 必须为数字'}
        if allow_center:
            if IS_WIN and hwnd and user32.IsWindow(hwnd):
                rect = wt.RECT()
                if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    return max(1, (rect.right - rect.left) // 2), max(1, (rect.bottom - rect.top) // 2), None
            _, _, vw, vh = get_virtual_screen_rect()
            return vw // 2, vh // 2, None
        return None, None, {'ok': False, 'error': 'missing_coordinates', 'detail': '必须提供 (nx, ny) 或 (x, y) 坐标'}

    def _node_belongs_to_hwnd(self, node: dict[str, Any], hwnd: int) -> bool:
        if not node or not hwnd:
            return False
        root_hwnd = int(node.get('root_hwnd') or 0)
        if root_hwnd and root_hwnd != int(hwnd):
            return False
        node_hwnd = int(node.get('hwnd') or 0)
        if node_hwnd and node_hwnd != int(hwnd):
            if IS_WIN and not user32.IsChild(hwnd, node_hwnd):
                return False
        return True

    def dump_window_tree(self, hwnd: int) -> dict[str, Any]:
        if not IS_WIN or not hwnd or not user32.IsWindow(hwnd):
            return {'ok': False, 'error': '未找到有效目标窗口'}
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        win_l, win_t = int(rect.left), int(rect.top)
        win_w = max(1, int(rect.right - rect.left))
        win_h = max(1, int(rect.bottom - rect.top))
        title = _get_hwnd_text(hwnd)
        pid, proc = _get_hwnd_process_name(hwnd)

        self.window_generation += 1
        gen_id = self.window_generation
        nodes: list[dict[str, Any]] = []
        seen_centers: list[tuple[int, int, str]] = []

        # 1. Try UIAutomation if installed, otherwise Win32 EnumChildWindows
        try:
            import uiautomation as auto  # type: ignore
            root_ctrl = auto.ControlFromHandle(hwnd)
            if root_ctrl:
                for ctrl, depth in auto.WalkControl(root_ctrl, includeTop=False, maxDepth=10):
                    if len(nodes) >= 90:
                        break
                    try:
                        cname = (ctrl.ControlTypeName or '').replace('Control', '')
                        if cname not in ('Button', 'Edit', 'ListItem', 'TabItem', 'Hyperlink', 'CheckBox', 'MenuItem', 'ComboBox', 'Document', 'Text'):
                            continue
                        name = (ctrl.Name or '').strip()
                        br = ctrl.BoundingRectangle
                        if not br or br.width() < 6 or br.height() < 6:
                            continue
                        sx = int((br.left + br.right) // 2)
                        sy = int((br.top + br.bottom) // 2)
                        rel_x = sx - win_l
                        rel_y = sy - win_t
                        if not (0 <= rel_x < win_w and 0 <= rel_y < win_h):
                            continue
                        if not name and cname == 'Text':
                            continue
                        native_hwnd = int(ctrl.NativeWindowHandle or 0)
                        automation_id = str(getattr(ctrl, 'AutomationId', '') or '').strip()
                        class_name = str(getattr(ctrl, 'ClassName', '') or '').strip()
                        seen_centers.append((rel_x, rel_y, name))
                        ref = f'@n{len(nodes) + 1}'
                        nodes.append({
                            'ref': ref,
                            'role': cname,
                            'text': name[:100],
                            'hwnd': native_hwnd,
                            'root_hwnd': int(hwnd),
                            'has_native_hwnd': bool(native_hwnd),
                            'automation_id': automation_id,
                            'class_name': class_name,
                            'uia_control': ctrl,
                            'pid': int(pid),
                            'generation': gen_id,
                            'center_x': rel_x,
                            'center_y': rel_y,
                            'screen_x': sx,
                            'screen_y': sy,
                            'editable': cname in ('Edit', 'Document', 'ComboBox'),
                        })
                    except Exception:
                        continue
        except Exception:
            pass

        # 2. Win32 EnumChildWindows supplement / fallback
        WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        def _child_cb(ch_hwnd: int, _lparam: int) -> bool:
            if len(nodes) >= 90 or not user32.IsWindowVisible(ch_hwnd):
                return True
            r = wt.RECT()
            user32.GetWindowRect(ch_hwnd, ctypes.byref(r))
            cw, ch = r.right - r.left, r.bottom - r.top
            if cw < 8 or ch < 8:
                return True
            txt = _get_hwnd_text(ch_hwnd)
            cls = _get_hwnd_class(ch_hwnd)
            sx = int((r.left + r.right) // 2)
            sy = int((r.top + r.bottom) // 2)
            rel_x = sx - win_l
            rel_y = sy - win_t
            if not (0 <= rel_x < win_w and 0 <= rel_y < win_h):
                return True
            editable = 'edit' in cls.lower() or 'richedit' in cls.lower()
            if not txt and not editable and 'button' not in cls.lower():
                return True
            if any(abs(px - rel_x) <= 6 and abs(py - rel_y) <= 6 and ptxt == txt for px, py, ptxt in seen_centers):
                return True
            seen_centers.append((rel_x, rel_y, txt))
            ref = f'@n{len(nodes) + 1}'
            nodes.append({
                'ref': ref,
                'role': cls[:32] or 'Control',
                'text': txt[:100],
                'hwnd': int(ch_hwnd),
                'root_hwnd': int(hwnd),
                'has_native_hwnd': True,
                'class_name': cls,
                'pid': int(pid),
                'generation': gen_id,
                'center_x': rel_x,
                'center_y': rel_y,
                'screen_x': sx,
                'screen_y': sy,
                'editable': editable,
            })
            return True

        user32.EnumChildWindows(hwnd, WNDENUMPROC(_child_cb), 0)
        self.cache_hwnd = int(hwnd)
        self.cache_pid = int(pid)
        self.node_cache = {n['ref'].lower(): n for n in nodes}

        lines = [f'hwnd={hex(hwnd)} process={proc} title="{title}" size={win_w}x{win_h} count={len(nodes)}']
        for n in nodes:
            flags = 'click' + (' edit' if n.get('editable') else '')
            lines.append(f"{n['ref']} [{n['role']}] \"{n['text']}\" center=({n['center_x']},{n['center_y']}) {flags}")
        summary = '\n'.join(lines)
        out: dict[str, Any] = {
            'ok': True,
            'hwnd': hwnd,
            'generation': gen_id,
            'title': title,
            'process': proc,
            'width': win_w,
            'height': win_h,
            'node_count': len(nodes),
            'summary': summary,
            'ui_tree': summary,
        }
        if len(nodes) < 2:
            b64, cw, ch = capture_window_jpeg(hwnd=hwnd)
            if b64:
                out['image'] = b64
                out['width'] = cw
                out['height'] = ch
        return out

    def _post_background_click(
        self,
        hwnd: int,
        rel_x: int,
        rel_y: int,
        right: bool = False,
        double: bool = False,
        screen_x: int | None = None,
        screen_y: int | None = None,
        target_hwnd: int = 0,
    ) -> tuple[bool, str]:
        if not IS_WIN or not hwnd:
            return (False, '无效的桌面窗口句柄')
        if screen_x is not None and screen_y is not None:
            sx, sy = int(screen_x), int(screen_y)
        else:
            rect = wt.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return (False, f'GetWindowRect 失败 (err={win32_last_error()})')
            sx = int(rect.left) + int(rel_x)
            sy = int(rect.top) + int(rel_y)

        if target_hwnd and (target_hwnd == hwnd or user32.IsChild(hwnd, target_hwnd)):
            target = target_hwnd
        else:
            cpt = wt.POINT(sx, sy)
            user32.ScreenToClient(hwnd, ctypes.byref(cpt))
            child = user32.ChildWindowFromPointEx(hwnd, cpt, 0x0001 | 0x0002)
            target = int(child) if (child and (child == hwnd or user32.IsChild(hwnd, child))) else hwnd

        pt = wt.POINT(sx, sy)
        user32.ScreenToClient(target, ctypes.byref(pt))
        lparam = makelparam(pt.x, pt.y)
        if right:
            ok1 = bool(user32.PostMessageW(target, WM_RBUTTONDOWN, 0, lparam))
            time.sleep(0.02)
            ok2 = bool(user32.PostMessageW(target, WM_RBUTTONUP, 0, lparam))
            return (ok1 and ok2, '' if (ok1 and ok2) else f'PostMessageW 右键失败 (err={win32_last_error()})')
        ok1 = bool(user32.PostMessageW(target, WM_LBUTTONDOWN, MK_LBUTTON, lparam))
        time.sleep(0.02)
        ok2 = bool(user32.PostMessageW(target, WM_LBUTTONUP, 0, lparam))
        if not (ok1 and ok2):
            return (False, f'PostMessageW 点击失败 (err={win32_last_error()})')
        if double:
            time.sleep(0.04)
            ok3 = bool(user32.PostMessageW(target, WM_LBUTTONDBLCLK, MK_LBUTTON, lparam))
            time.sleep(0.02)
            ok4 = bool(user32.PostMessageW(target, WM_LBUTTONUP, 0, lparam))
            if not (ok3 and ok4):
                return (False, f'PostMessageW 双击失败 (err={win32_last_error()})')
        return (True, '')

    def _post_background_text(
        self,
        hwnd: int,
        text: str,
        clear: bool = True,
        submit: bool = False,
        allow_settext: bool = False,
    ) -> tuple[bool, str]:
        if not IS_WIN or not hwnd:
            return (False, '无效的桌面窗口句柄')
        if clear and allow_settext:
            user32.SendMessageW(hwnd, WM_SETTEXT, 0, ctypes.c_wchar_p(''))
        elif clear:
            for _ in range(32):
                if not user32.PostMessageW(hwnd, WM_KEYDOWN, VK_BACK, 0):
                    return (False, f'PostMessageW 退格失败 (err={win32_last_error()})')
                user32.PostMessageW(hwnd, WM_KEYUP, VK_BACK, 0)
        for ch in text:
            if not user32.PostMessageW(hwnd, WM_CHAR, ord(ch), 0):
                return (False, f'PostMessageW 字符输入失败 (err={win32_last_error()})')
            time.sleep(0.004)
        if submit:
            time.sleep(0.03)
            if not user32.PostMessageW(hwnd, WM_KEYDOWN, VK_RETURN, 0):
                return (False, f'PostMessageW 回车失败 (err={win32_last_error()})')
            user32.PostMessageW(hwnd, WM_KEYUP, VK_RETURN, 0)
        return (True, '')

    def _type_via_uia_or_hwnd(
        self,
        root_hwnd: int,
        node: dict[str, Any],
        text: str,
        clear: bool = True,
        submit: bool = False,
    ) -> tuple[bool, str]:
        uia_ctrl = node.get('uia_control')
        if uia_ctrl is not None:
            try:
                val_pat = uia_ctrl.GetValuePattern()
                if val_pat is not None:
                    new_val = text if clear else (str( getattr(val_pat, 'Value', '') or '' ) + text)
                    val_pat.SetValue(new_val)
                    if submit and IS_WIN and root_hwnd:
                        target_h = int(node.get('hwnd') or root_hwnd)
                        user32.PostMessageW(target_h, WM_KEYDOWN, VK_RETURN, 0)
                        user32.PostMessageW(target_h, WM_KEYUP, VK_RETURN, 0)
                    return (True, 'uia_value_pattern')
            except Exception:
                pass
        native_hwnd = int(node.get('hwnd') or 0)
        if native_hwnd and native_hwnd != root_hwnd:
            cls_lower = str(node.get('class_name') or node.get('role') or '').lower()
            is_native_edit = 'edit' in cls_lower or bool(node.get('editable'))
            ok_t, err_t = self._post_background_text(native_hwnd, text, clear=clear, submit=submit, allow_settext=is_native_edit)
            return (ok_t, 'native_hwnd_wm_char' if ok_t else err_t)
        if root_hwnd and node.get('center_x') is not None and node.get('center_y') is not None:
            ok_c, err_c = self._post_background_click(
                root_hwnd,
                int(node['center_x']),
                int(node['center_y']),
                screen_x=node.get('screen_x'),
                screen_y=node.get('screen_y'),
            )
            if not ok_c:
                return (False, err_c)
            time.sleep(0.05)
            # Never send WM_SETTEXT to root_hwnd (which would overwrite the window title!)
            ok_t, err_t = self._post_background_text(root_hwnd, text, clear=clear, submit=submit, allow_settext=False)
            return (ok_t, 'focused_virtual_wm_char' if ok_t else err_t)
        return (False, 'unsupported_virtual_control')

    async def run_shell(self, cmd: str, timeout_s: int = 30) -> dict[str, Any]:
        clean_cmd = (cmd or '').strip()
        if not clean_cmd:
            return {'ok': False, 'error': 'command 不能为空'}
        eff_timeout = max(3, min(120, int(timeout_s or 30)))
        creationflags = subprocess.CREATE_NO_WINDOW if IS_WIN else 0
        proc = await asyncio.create_subprocess_shell(
            clean_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=(not IS_WIN),
        )

        async def _read_bounded() -> bytes:
            buf = bytearray()
            while True:
                if proc.stdout is None:
                    break
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                if len(buf) < 32000:
                    buf.extend(chunk[:32000 - len(buf)])
            return bytes(buf)

        timed_out = False
        cancelled = False
        out_bytes = b''
        try:
            out_bytes = await asyncio.wait_for(_read_bounded(), timeout=eff_timeout)
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            timed_out = True
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if timed_out or cancelled or getattr(proc, 'returncode', None) is None:
                pid = getattr(proc, 'pid', None)
                if IS_WIN and pid:
                    with contextlib.suppress(Exception):
                        subprocess.run(
                            ['taskkill', '/F', '/T', '/PID', str(pid)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=5,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                elif not IS_WIN and pid:
                    with contextlib.suppress(Exception):
                        import signal
                        os.killpg(pid, signal.SIGKILL)
                if getattr(proc, 'returncode', None) is None and hasattr(proc, 'kill'):
                    with contextlib.suppress(Exception):
                        proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=3.0)

        if timed_out:
            return {'ok': False, 'timed_out': True, 'exit_code': -1, 'error': f'命令执行超时 ({eff_timeout}s)'}
        return {
            'ok': proc.returncode == 0,
            'exit_code': proc.returncode,
            'output': out_bytes.decode('utf-8', errors='replace')[:32000],
        }

    async def execute(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        op = (op or 'status').strip().lower()
        if op == 'status':
            sw, sh = get_screen_size()
            wins = list_windows()
            fg = next((w for w in wins if w.get('active')), None)
            return {
                'ok': True,
                'hostname': socket.gethostname(),
                'platform': platform.platform(),
                'screen': {'width': sw, 'height': sh},
                'idle_seconds': get_human_idle_seconds(),
                'window_count': len(wins),
                'active_window': fg,
            }
        if op == 'windows':
            wins = list_windows(str(args.get('window') or args.get('target') or args.get('text') or ''))
            return {'ok': True, 'count': len(wins), 'windows': wins[:50]}
        if op in ('tree', 'ui_tree', 'dump', 'nodes'):
            hwnd, err = self._require_target_hwnd(args)
            return err or await asyncio.to_thread(self.dump_window_tree, hwnd)
        if op in ('screenshot', 'capture', 'preview'):
            full_scr = bool(args.get('full_screen', False))
            hwnd = 0 if full_scr else self.resolve_target_hwnd(args)
            if not full_scr and not hwnd and (args.get('hwnd') or args.get('window') or args.get('app') or args.get('title')):
                _, err = self._require_target_hwnd(args)
                return err or {'ok': False, 'error': 'window_not_found'}
            b64, w, h = await asyncio.to_thread(capture_window_jpeg, hwnd, full_scr)
            if not b64:
                return {'ok': False, 'error': '截图捕获失败 (PrintWindow/ImageGrab returned empty)', 'hwnd': hwnd}
            return {'ok': True, 'hwnd': hwnd, 'title': _get_hwnd_text(hwnd) if hwnd else 'Screen', 'width': w, 'height': h, 'image': b64}
        if op in ('clipboard', 'clipboard_get', 'clipboard_set'):
            is_set = op == 'clipboard_set' or (op == 'clipboard' and 'text' in args and args.get('action') != 'get')
            if is_set:
                txt = str(args.get('text') or '')
                ok, err_msg = win32_clipboard_set_checked(txt)
                return {'ok': ok, 'length': len(txt), **({'error': err_msg} if not ok else {})}
            ok, txt, err_msg = win32_clipboard_get_checked()
            return {'ok': ok, 'text': txt, **({'error': err_msg} if not ok else {})}
        if op == 'open_url':
            url = str(args.get('url') or args.get('text') or '').strip()
            if not url:
                return {'ok': False, 'error': 'url 不能为空'}
            parsed_url = urlsplit(url)
            if parsed_url.scheme.lower() not in ('http', 'https') or not parsed_url.netloc:
                return {'ok': False, 'error': f'仅允许打开 http:// 或 https:// 链接: {url}'}
            if not webbrowser.open(url, new=2, autoraise=False):
                return {'ok': False, 'error': f'无法打开浏览器 URL: {url}'}
            return {'ok': True, 'opened_url': url}
        if op == 'launch':
            app_cmd = str(args.get('app') or args.get('command') or args.get('text') or '').strip()
            if not app_cmd:
                return {'ok': False, 'error': 'app 不能为空'}
            try:
                raw_tokens = shlex.split(app_cmd, posix=(not IS_WIN))
                argv = [
                    tok[1:-1] if (IS_WIN and len(tok) >= 2 and ((tok[0] == '"' and tok[-1] == '"') or (tok[0] == "'" and tok[-1] == "'"))) else tok
                    for tok in raw_tokens
                ]
                argv = [tok for tok in argv if tok]
                if not argv:
                    return {'ok': False, 'error': 'app 不能为空'}
                if IS_WIN:
                    si = subprocess.STARTUPINFO()
                    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    si.wShowWindow = SW_SHOWNOACTIVATE
                    subprocess.Popen(argv, shell=False, startupinfo=si)
                else:
                    subprocess.Popen(argv, shell=False)
            except Exception as exc:
                return {'ok': False, 'error': f'启动应用失败: {exc}'}
            await asyncio.sleep(0.6)
            return {'ok': True, 'launched': app_cmd, 'windows': list_windows()[:15]}
        if op == 'shell':
            cmd = str(args.get('command') or args.get('text') or '').strip()
            timeout_s = max(3, min(120, int(args.get('timeout') or 30)))
            return await self.run_shell(cmd, timeout_s=timeout_s)
        if op in ('click_node', 'tap_node'):
            hwnd, err = self._require_target_hwnd(args)
            if err:
                return err
            ref = str(args.get('ref') or '').strip().lower()
            target_txt = str(args.get('text') or args.get('target') or '').strip().lower()
            if self.cache_hwnd and self.cache_hwnd != hwnd:
                if ref:
                    return {'ok': False, 'error': f'节点缓存属于窗口 {hex(self.cache_hwnd)}，与当前目标窗口 {hex(hwnd)} 不一致，请先重新读取窗口树'}
                self.node_cache = {}
            if not self.node_cache and hwnd and not ref:
                await asyncio.to_thread(self.dump_window_tree, hwnd)
            node = self.node_cache.get(ref) if ref else next((c for c in self.node_cache.values() if target_txt and target_txt in str(c.get('text') or '').lower()), None)
            if node is None:
                return {'ok': False, 'error': f'未找到桌面节点 (ref={ref}, text={target_txt})'}
            if not self._node_belongs_to_hwnd(node, hwnd):
                return {'ok': False, 'error': f'节点 {ref or node.get("ref")} 不属于目标窗口 {hex(hwnd)}'}
            node_hwnd = int(node.get('hwnd') or 0)
            role_lower = str(node.get('role') or '').lower()
            method_used = 'background_postmessage'
            posted_ok, post_err = False, ''
            if node_hwnd and node_hwnd != hwnd and ('button' in role_lower or 'checkbox' in role_lower) and IS_WIN:
                posted_ok = bool(user32.PostMessageW(node_hwnd, BM_CLICK, 0, 0))
                if posted_ok:
                    method_used = 'bm_click'
            if not posted_ok:
                posted_ok, post_err = await asyncio.to_thread(
                    self._post_background_click, hwnd, int(node['center_x']), int(node['center_y']),
                    False, False, node.get('screen_x'), node.get('screen_y'), node_hwnd or hwnd,
                )
            if not posted_ok:
                return {'ok': False, 'error': post_err or 'PostMessageW 点击失败'}
            await asyncio.sleep(0.22)
            tree = await asyncio.to_thread(self.dump_window_tree, hwnd)
            return {
                'ok': True, 'clicked': True, 'method': method_used, 'ref': node['ref'],
                'label': node['text'], 'x': node['center_x'], 'y': node['center_y'],
                'action_receipt': f"已在后台点击窗口 {hex(hwnd)} 节点 {node['ref']} \"{node['text']}\" ({node['center_x']},{node['center_y']})，未抢占物理鼠标",
                'post_ui_summary': tree.get('summary', ''),
            }
        if op in ('type_node', 'text', 'type'):
            hwnd, err = self._require_target_hwnd(args)
            if err:
                return err
            ref = str(args.get('ref') or '').strip().lower()
            txt = str(args.get('text') or '')
            clear, submit = bool(args.get('clear', True)), bool(args.get('submit', False))
            if op == 'type_node' or ref:
                if not ref:
                    return {'ok': False, 'error': 'type_node 必须提供有效的 ref'}
                if self.cache_hwnd and self.cache_hwnd != hwnd:
                    return {'ok': False, 'error': f'节点缓存属于窗口 {hex(self.cache_hwnd)}，与当前目标窗口 {hex(hwnd)} 不一致'}
                node = self.node_cache.get(ref)
                if node is None:
                    return {'ok': False, 'error': f'未找到输入节点: {ref}'}
                if not self._node_belongs_to_hwnd(node, hwnd):
                    return {'ok': False, 'error': f'输入节点 {ref} 不属于目标窗口 {hex(hwnd)}'}
                ok_typed, method_used = await asyncio.to_thread(self._type_via_uia_or_hwnd, hwnd, node, txt, clear, submit)
                if not ok_typed:
                    return {'ok': False, 'error': f'节点 {ref} 不支持后台输入 ({method_used})'}
                target_hwnd = int(node.get('hwnd') or hwnd)
            else:
                if any(args.get(k) is not None for k in ('nx', 'ny', 'x', 'y')):
                    cx, cy, c_err = self._resolve_coords(hwnd, args, allow_center=False)
                    if c_err:
                        return c_err
                    ok_c, err_c = await asyncio.to_thread(self._post_background_click, hwnd, cx, cy)
                    if not ok_c:
                        return {'ok': False, 'error': err_c}
                    await asyncio.sleep(0.06)
                target_hwnd, method_used = hwnd, 'background_wm_char'
                ok_t, err_t = await asyncio.to_thread(self._post_background_text, target_hwnd, txt, clear, submit, False)
                if not ok_t:
                    return {'ok': False, 'error': err_t}
            await asyncio.sleep(0.22)
            tree = await asyncio.to_thread(self.dump_window_tree, hwnd)
            return {
                'ok': True, 'typed': True, 'method': method_used, 'characters': len(txt), 'submitted': submit,
                'action_receipt': f'已向后台窗口 {hex(target_hwnd)} 注入 {len(txt)} 个字符 (回车={submit})，未占用物理键盘',
                'post_ui_summary': tree.get('summary', ''),
            }
        if op in ('click', 'double_click', 'right_click'):
            hwnd, err = self._require_target_hwnd(args)
            if err:
                return err
            x, y, c_err = self._resolve_coords(hwnd, args, allow_center=False)
            if c_err:
                return c_err
            ok_c, err_c = await asyncio.to_thread(self._post_background_click, hwnd, x, y, op == 'right_click', op == 'double_click')
            if not ok_c:
                return {'ok': False, 'error': err_c}
            await asyncio.sleep(0.22)
            tree = await asyncio.to_thread(self.dump_window_tree, hwnd)
            return {'ok': True, 'clicked': True, 'hwnd': hwnd, 'x': x, 'y': y, 'post_ui_summary': tree.get('summary', '')}
        if op in ('key', 'hotkey'):
            hwnd, err = self._require_target_hwnd(args)
            if err:
                return err
            key_str = str(args.get('key') or args.get('keys') or args.get('text') or '').strip()
            if not key_str:
                return {'ok': False, 'error': 'key 不能为空'}
            vk_map = {
                'enter': VK_RETURN, 'return': VK_RETURN, 'tab': VK_TAB, 'esc': VK_ESCAPE, 'escape': VK_ESCAPE,
                'backspace': VK_BACK, 'back': VK_BACK, 'space': 0x20, 'left': 0x25, 'up': 0x26, 'right': 0x27,
                'down': 0x28, 'delete': 0x2E, 'del': 0x2E, 'home': 0x24, 'end': 0x23, 'pageup': 0x21,
                'pagedown': 0x22, 'ctrl': 0x11, 'control': 0x11, 'alt': 0x12, 'shift': 0x10,
            }
            vks: list[int] = []
            for p in [p.strip().lower() for p in key_str.replace('-', '+').split('+') if p.strip()]:
                if p in vk_map:
                    vks.append(vk_map[p])
                elif p.startswith('f') and p[1:].isdigit() and 1 <= int(p[1:]) <= 12:
                    vks.append(0x70 + int(p[1:]) - 1)
                elif len(p) == 1:
                    vks.append(ord(p.upper()))
            if not vks:
                return {'ok': False, 'error': f'不支持的按键: {key_str}'}
            if IS_WIN:
                for vk in vks:
                    if not user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 0):
                        return {'ok': False, 'error': f'PostMessageW 按键失败 (err={win32_last_error()})'}
                for vk in reversed(vks):
                    user32.PostMessageW(hwnd, WM_KEYUP, vk, 0)
            return {'ok': True, 'key': key_str, 'hwnd': hwnd}
        if op == 'scroll':
            hwnd, err = self._require_target_hwnd(args)
            if err:
                return err
            x, y, c_err = self._resolve_coords(hwnd, args, allow_center=True)
            if c_err:
                return c_err
            direction = str(args.get('direction') or 'down').strip().lower()
            raw_amount = args.get('amount')
            amount = max(1, min(20, 3 if raw_amount is None else int(raw_amount)))
            delta = (120 * amount) if direction in ('up', 'left') else (-120 * amount)
            wparam = (delta & 0xFFFF) << 16
            if IS_WIN:
                rect = wt.RECT()
                if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    return {'ok': False, 'error': f'GetWindowRect 失败 (err={win32_last_error()})'}
                sx, sy = int(rect.left) + x, int(rect.top) + y
                if not user32.PostMessageW(hwnd, WM_MOUSEWHEEL, wparam, makelparam(sx, sy)):
                    return {'ok': False, 'error': f'PostMessageW 滚轮失败 (err={win32_last_error()})'}
            return {'ok': True, 'scrolled': True, 'direction': direction, 'amount': amount, 'x': x, 'y': y, 'hwnd': hwnd}
        if op == 'batch':
            steps = args.get('steps')
            if not isinstance(steps, list) or not steps:
                return {'ok': False, 'error': 'batch steps 必须为非空列表', 'steps_executed': 0, 'results': []}
            results = []
            failed_step = None
            stop_reason = ''
            for idx, step in enumerate(steps[:10]):
                if not isinstance(step, dict):
                    failed_step = idx
                    stop_reason = f'第 {idx + 1} 步不是有效对象'
                    results.append({'ok': False, 'error': stop_reason})
                    break
                step_op = str(step.get('op') or step.get('kind') or '').strip()
                if not step_op or step_op == 'batch':
                    failed_step = idx
                    stop_reason = f'第 {idx + 1} 步缺少有效操作类型'
                    results.append({'ok': False, 'error': stop_reason})
                    break
                r = await self.execute(step_op, step)
                results.append(r)
                if not r.get('ok'):
                    failed_step = idx
                    stop_reason = str(r.get('error') or f'第 {idx + 1} 步 ({step_op}) 执行失败')
                    break
            all_ok = bool(results) and failed_step is None and all(bool(r.get('ok')) for r in results)
            batch_out: dict[str, Any] = {
                'ok': all_ok,
                'steps_executed': len(results),
                'results': results,
            }
            if failed_step is not None:
                batch_out['failed_step'] = failed_step
                batch_out['error'] = stop_reason
            return batch_out
        return {'ok': False, 'error': f'未知桌面端指令: {op}'}


_PC_CONFIG_PATH = Path.home() / '.pulse-agent' / 'pc_config.json'


def parse_pair_code(raw: str) -> tuple[str, str] | None:
    clean = str(raw or '').strip()
    if not clean:
        return None
    if clean.upper().startswith('DSCLAW1:'):
        clean = clean[len('DSCLAW1:'):].strip()
    if '|' not in clean:
        return None
    url_part, tok_part = clean.rsplit('|', 1)
    url_clean = url_part.strip().rstrip('/')
    tok_clean = tok_part.strip()
    if not url_clean or not tok_clean:
        return None
    return url_clean, tok_clean


def load_saved_pc_config() -> dict[str, str]:
    try:
        if _PC_CONFIG_PATH.is_file():
            data = json.loads(_PC_CONFIG_PATH.read_text('utf-8'))
            if isinstance(data, dict):
                return {
                    'server_url': str(data.get('server_url') or '').strip().rstrip('/'),
                    'token': str(data.get('token') or '').strip(),
                    'device_id': str(data.get('device_id') or '').strip(),
                }
    except Exception:
        pass
    return {}


def save_pc_config(server_url: str, token: str, device_id: str) -> Path:
    _PC_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'server_url': server_url.strip().rstrip('/'),
        'token': token.strip(),
        'device_id': device_id.strip(),
        'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    _PC_CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if os.name == 'posix':
        with contextlib.suppress(OSError):
            os.chmod(_PC_CONFIG_PATH, 0o600)
    return _PC_CONFIG_PATH


async def run_agent_loop(server_url: str, token: str, device_id: str, check_only: bool = False) -> None:
    import websockets  # type: ignore

    ws_base = server_url.rstrip('/').replace('https://', 'wss://').replace('http://', 'ws://')
    ws_url = ws_base + '/ws/device'
    harness = DesktopWorkspaceHarness()

    backoff = 1.5
    while True:
        pending_tasks: set[asyncio.Task[Any]] = set()
        try:
            sw, sh = get_screen_size()
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20, max_size=8_000_000) as ws:
                hello = {
                    'type': 'hello',
                    'token': token,
                    'device_type': 'desktop',
                    'device_id': device_id,
                    'device_name': f'{socket.gethostname()} ({platform.system()})',
                    'platform': sys.platform,
                    'capabilities': [
                        'status', 'windows', 'tree', 'screenshot', 'click_node', 'type_node',
                        'click', 'double_click', 'right_click', 'type', 'key', 'hotkey', 'scroll',
                        'launch', 'open_url', 'shell', 'clipboard', 'batch',
                    ],
                    'screen': {'width': sw, 'height': sh, 'scale': 1.0},
                }
                await ws.send(json.dumps(hello))
                if check_only:
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=1.5)
                    except asyncio.TimeoutError:
                        pass
                    if getattr(ws, 'close_code', None) not in (None, 1000):
                        raise RuntimeError(f'Server closed WebSocket during handshake (code={ws.close_code})')
                    print(f'DESKTOP_AGENT_CONNECTED server={server_url} device_id={device_id}')
                    return
                backoff = 1.5
                ws_send_lock = asyncio.Lock()

                async def _dispatch_cmd(ev_req_id: str, ev_op: str, ev_args: dict[str, Any]) -> None:
                    try:
                        res = await harness.execute(ev_op, ev_args)
                    except asyncio.CancelledError:
                        return
                    except Exception as exc:
                        res = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
                    reply = {
                        'type': 'desktop_workspace_result',
                        'request_id': ev_req_id,
                        'result': res,
                    }
                    with contextlib.suppress(Exception):
                        async with ws_send_lock:
                            await ws.send(json.dumps(reply))

                async for raw_msg in ws:
                    event = json.loads(raw_msg)
                    ev_type = event.get('type')
                    if ev_type == 'desktop_workspace_command':
                        req_id = event.get('request_id', '')
                        op = event.get('op', 'status')
                        cmd_args = event.get('args') or {}
                        task = asyncio.create_task(_dispatch_cmd(req_id, op, cmd_args))
                        pending_tasks.add(task)
                        task.add_done_callback(pending_tasks.discard)
        except Exception as exc:
            if check_only:
                raise SystemExit(f'DESKTOP_AGENT_CHECK_FAILED: {exc}')
            wait_s = min(30.0, backoff) + random.uniform(0.0, 0.75)
            print(f'[desktop-agent] Disconnected ({exc}), reconnecting in {wait_s:.1f}s...', file=sys.stderr)
            backoff = min(30.0, backoff * 2.0)
            await asyncio.sleep(wait_s)
        finally:
            for t in list(pending_tasks):
                t.cancel()
            if pending_tasks:
                with contextlib.suppress(Exception):
                    await asyncio.gather(*pending_tasks, return_exceptions=True)


def main() -> None:
    saved = load_saved_pc_config()
    parser = argparse.ArgumentParser(description='Pulse Lightweight Non-Intrusive Desktop Agent')
    parser.add_argument('--pair-code', default=os.getenv('PULSE_PAIR_CODE', os.getenv('DS_CLAW_PAIR_CODE', '')), help='DSCLAW1:<server_url>|<token> pair code')
    parser.add_argument('--server', default=os.getenv('PULSE_SERVER_URL', saved.get('server_url') or 'http://127.0.0.1:18190'))
    parser.add_argument('--token', default=os.getenv('PULSE_DEVICE_TOKEN', saved.get('token') or ''))
    parser.add_argument('--device-id', default=os.getenv('PULSE_DEVICE_ID', saved.get('device_id') or f'desktop-{socket.gethostname().lower()}'))
    parser.add_argument('--save', action='store_true', help='Save server/token/device_id to ~/.pulse-agent/pc_config.json')
    parser.add_argument('--check', action='store_true', help='Perform live WebSocket handshake test and exit')
    args = parser.parse_args()
    server_url = args.server.strip().rstrip('/')
    token = args.token.strip()
    if args.pair_code.strip():
        parsed = parse_pair_code(args.pair_code)
        if not parsed:
            raise SystemExit('无效配对码格式，期望 DSCLAW1:<server_url>|<token>')
        server_url, token = parsed
    if not token:
        tok_file = Path(__file__).parent.parent / 'private' / 'device-token'
        if tok_file.exists():
            token = tok_file.read_text('utf-8').strip()
    if not token:
        raise SystemExit('请提供 --pair-code、--token，或设置 PULSE_DEVICE_TOKEN')
    if args.save:
        cfg_path = save_pc_config(server_url, token, args.device_id)
        print(f'[desktop-agent] Saved config -> {cfg_path}')
    asyncio.run(run_agent_loop(server_url, token, args.device_id, check_only=args.check))


if __name__ == '__main__':
    main()
