"""
EWMH X11 Window Manager & Active Focus / UTF-8 Clipboard (`server/cloud_os/window_manager.py`).

Provides true Agent-Computer Interface (ACI) window awareness:
1. Lists all open X11 windows ordered by actual stacking Z-order (`_NET_CLIENT_LIST_STACKING`),
   marking which window is currently focused (`_NET_ACTIVE_WINDOW`), its `WM_CLASS`, title,
   and exact `(x, y, width, height)` geometry.
2. Accurately queries the single currently focused X11 window (`get_active_window_info`,
   `is_active_window_terminal`) so paste shortcuts never misclassify background windows.
3. Supports deterministic window operations (`focus_window`, `close_window`, `tile_windows`)
   and lossless UTF-8 clipboard read/write (`xclip -target UTF8_STRING` with `xsel` fallback).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Dict, List, Optional


_TERMINAL_CLASSES = (
    'xterm', 'lxterminal', 'xfce4-terminal', 'gnome-terminal',
    'konsole', 'alacritty', 'kitty', 'urxvt', 'rxvt', 'st-256color',
)


def _utf8_env(env: Optional[Dict[str, str]]) -> Dict[str, str]:
    out = dict(os.environ)
    if env:
        out.update(env)
    out['LANG'] = 'C.UTF-8'
    out['LC_ALL'] = 'C.UTF-8'
    return out


def list_x11_windows(env: Dict[str, str]) -> List[Dict[str, object]]:
    """
    Return all open X11 windows in stacking order (top-most first) with:
    `id`, `title`, `wm_class`, `x`, `y`, `width`, `height`, `focused`, `z_order`.
    Uses `xprop` + `xdotool` directly so it never segfaults on 64-bit Debian/Trixie.
    """
    run_env = _utf8_env(env)
    if os.name == 'nt' or not run_env.get('DISPLAY'):
        return []

    active_id = _get_active_window_id(run_env)
    stacking_order = _get_stacking_order_ids(run_env)
    if not stacking_order and shutil.which('xdotool'):
        try:
            out = subprocess.check_output(
                ['xdotool', 'search', '--onlyvisible', '--name', '.'],
                env=run_env, stderr=subprocess.DEVNULL, timeout=2,
            ).decode('utf-8', errors='replace')
            stacking_order = [_norm_hex_id(hex(int(w.strip()))) for w in out.splitlines() if w.strip().isdigit()]
        except Exception:
            stacking_order = []

    raw_windows: List[Dict[str, object]] = []
    for idx, norm_wid in enumerate(stacking_order):
        try:
            int_wid = int(norm_wid, 16)
        except ValueError:
            continue
        title = ''
        wm_class = ''
        x, y, w, h = 0, 0, 0, 0
        if shutil.which('xdotool'):
            try:
                wname = subprocess.check_output(
                    ['xdotool', 'getwindowname', str(int_wid)],
                    env=run_env, stderr=subprocess.DEVNULL, timeout=2,
                ).decode('utf-8', errors='replace').strip()
                if wname:
                    title = wname
            except Exception:
                pass
        is_desktop_type = False
        if shutil.which('xprop'):
            try:
                xp = subprocess.check_output(
                    ['xprop', '-id', norm_wid, '_NET_WM_NAME', 'WM_NAME', 'WM_CLASS', '_NET_WM_WINDOW_TYPE'],
                    env=run_env, stderr=subprocess.DEVNULL, timeout=2,
                ).decode('utf-8', errors='replace')
                for line in xp.splitlines():
                    if ('_NET_WM_NAME' in line or line.startswith('WM_NAME')) and '=' in line and not title:
                        m = re.search(r'"(.*)"', line)
                        if m:
                            title = m.group(1).strip()
                    elif 'WM_CLASS' in line and '=' in line:
                        classes = re.findall(r'"([^"]+)"', line)
                        if classes:
                            wm_class = '.'.join(classes)
                    elif '_NET_WM_WINDOW_TYPE' in line and ('_NET_WM_WINDOW_TYPE_DESKTOP' in line or '_NET_WM_WINDOW_TYPE_DOCK' in line):
                        is_desktop_type = True
            except Exception:
                pass
        if is_desktop_type:
            continue
        if shutil.which('xdotool'):
            try:
                geom = subprocess.check_output(
                    ['xdotool', 'getwindowgeometry', '--shell', str(int_wid)],
                    env=run_env, stderr=subprocess.DEVNULL, timeout=2,
                ).decode('utf-8', errors='replace')
                gmap = dict(ln.split('=', 1) for ln in geom.splitlines() if '=' in ln)
                x = int(gmap.get('X', 0))
                y = int(gmap.get('Y', 0))
                w = int(gmap.get('WIDTH', 0))
                h = int(gmap.get('HEIGHT', 0))
            except Exception:
                pass
        if (title or wm_class) in ('Tint2', 'tint2', 'panel', 'pcmanfm-desktop') or 'tint2' in wm_class.lower():
            continue
        if 'pcmanfm' in wm_class.lower() and w >= 1260 and h >= 740:
            continue
        is_focused = bool(active_id and norm_wid == active_id)
        raw_windows.append({
            'id': norm_wid,
            'norm_id': norm_wid,
            'title': title or wm_class or norm_wid,
            'wm_class': wm_class or 'X11Window',
            'x': x,
            'y': y,
            'width': w,
            'height': h,
            'focused': is_focused,
            '_z_rank': idx,
        })

    raw_windows.sort(key=lambda item: (1 if item['focused'] else 0, int(item['_z_rank'])), reverse=True)
    for idx, win in enumerate(raw_windows):
        win.pop('_z_rank', None)
        win.pop('norm_id', None)
        win['z_order'] = idx
    return raw_windows


def get_active_window_info(env: Dict[str, str]) -> Dict[str, str]:
    """Query the single currently focused X11 window's `id`, `title`, and `wm_class`."""
    run_env = _utf8_env(env)
    if os.name == 'nt' or not run_env.get('DISPLAY'):
        return {'id': '', 'title': '', 'wm_class': ''}

    int_wid = ''
    hex_wid = ''
    if shutil.which('xdotool'):
        try:
            raw_id = subprocess.check_output(
                ['xdotool', 'getactivewindow'],
                env=run_env, stderr=subprocess.DEVNULL, timeout=2,
            ).decode('utf-8', errors='replace').strip()
            if raw_id.isdigit():
                int_wid = raw_id
                hex_wid = _norm_hex_id(hex(int(raw_id)))
        except Exception:
            pass

    if not hex_wid:
        hex_wid = _get_active_window_id(run_env)
        if hex_wid:
            try:
                int_wid = str(int(hex_wid, 16))
            except ValueError:
                pass

    title = ''
    wm_class = ''
    if int_wid and shutil.which('xdotool'):
        try:
            title = subprocess.check_output(
                ['xdotool', 'getwindowname', int_wid],
                env=run_env, stderr=subprocess.DEVNULL, timeout=2,
            ).decode('utf-8', errors='replace').strip()
        except Exception:
            pass

    if hex_wid and shutil.which('xprop'):
        try:
            xp = subprocess.check_output(
                ['xprop', '-id', hex_wid, 'WM_CLASS', '_NET_WM_NAME', 'WM_NAME'],
                env=run_env, stderr=subprocess.DEVNULL, timeout=2,
            ).decode('utf-8', errors='replace')
            for line in xp.splitlines():
                if 'WM_CLASS' in line and '=' in line:
                    classes = re.findall(r'"([^"]+)"', line)
                    if classes:
                        wm_class = '.'.join(classes)
                elif ('_NET_WM_NAME' in line or line.startswith('WM_NAME')) and '=' in line and not title:
                    m = re.search(r'"(.*)"', line)
                    if m:
                        title = m.group(1).strip()
        except Exception:
            pass

    return {'id': hex_wid, 'title': title, 'wm_class': wm_class}


def is_active_window_terminal(env: Dict[str, str]) -> bool:
    """Return True strictly when the single currently focused X11 window is a terminal emulator."""
    info = get_active_window_info(env)
    wclass = info.get('wm_class', '').lower()
    wtitle = info.get('title', '').lower()
    if wclass:
        return any(tc in wclass for tc in _TERMINAL_CLASSES)
    return any(tc in wtitle for tc in ('xterm', 'lxterminal', 'xfce4-terminal', 'windows terminal'))


def focus_window(env: Dict[str, str], target: str) -> Optional[Dict[str, object]]:
    """Focus a window by hex ID (`0x...`), `wm_class`, or title substring."""
    run_env = _utf8_env(env)
    target_q = (target or '').strip().lower()
    if not target_q or os.name == 'nt':
        return None
    windows = list_x11_windows(run_env)
    matched = _match_window(windows, target_q)
    if not matched:
        return None
    wid = str(matched['id'])
    if shutil.which('xdotool'):
        try:
            int_id = str(int(wid, 16))
            subprocess.run(
                ['xdotool', 'windowactivate', '--sync', int_id],
                env=run_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
            )
            subprocess.run(
                ['xdotool', 'windowraise', int_id],
                env=run_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
            )
            return matched
        except Exception:
            pass
    return matched


def close_window(env: Dict[str, str], target: str) -> Optional[Dict[str, object]]:
    """Gracefully close a window by hex ID (`0x...`), `wm_class`, or title substring."""
    run_env = _utf8_env(env)
    target_q = (target or '').strip().lower()
    if not target_q or os.name == 'nt':
        return None
    windows = list_x11_windows(run_env)
    matched = _match_window(windows, target_q)
    if not matched:
        return None
    wid = str(matched['id'])
    if shutil.which('xdotool'):
        try:
            subprocess.run(
                ['xdotool', 'windowclose', str(int(wid, 16))],
                env=run_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
            )
            return matched
        except Exception:
            pass
    return matched


def tile_windows(env: Dict[str, str], width: int = 1280, height: int = 720, mode: str = 'auto') -> List[str]:
    """Arrange open windows cleanly on the desktop."""
    run_env = _utf8_env(env)
    if os.name == 'nt':
        return []
    windows = list_x11_windows(run_env)
    if not windows:
        return []

    usable_h = max(400, height - 52)
    tiled: List[str] = []
    if len(windows) == 1 or mode != 'split':
        w0 = windows[0]
        int_wid = str(int(str(w0['id']), 16))
        if shutil.which('xdotool'):
            subprocess.run(['xdotool', 'windowmove', int_wid, '14', '10'], env=run_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['xdotool', 'windowsize', int_wid, str(width - 28), str(usable_h - 14)], env=run_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['xdotool', 'windowactivate', int_wid], env=run_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tiled.append(f"{w0['title']} (full)")
    else:
        half_w = (width - 36) // 2
        for idx, (win, x_pos) in enumerate(((windows[0], 12), (windows[1], 24 + half_w))):
            int_wid = str(int(str(win['id']), 16))
            if shutil.which('xdotool'):
                subprocess.run(['xdotool', 'windowmove', int_wid, str(x_pos), '12'], env=run_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(['xdotool', 'windowsize', int_wid, str(half_w), str(usable_h - 16)], env=run_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            tiled.append(f"{win['title']} ({'left' if idx == 0 else 'right'})")
    return tiled


def get_clipboard(env: Dict[str, str]) -> str:
    """Read X11 clipboard text losslessly in UTF-8 via `xclip` or `xsel`."""
    run_env = _utf8_env(env)
    if os.name == 'nt':
        return ''
    if shutil.which('xclip'):
        for sel in ('clipboard', 'primary'):
            for cmd in (
                ['xclip', '-selection', sel, '-t', 'UTF8_STRING', '-o'],
                ['xclip', '-selection', sel, '-o'],
            ):
                try:
                    out = subprocess.check_output(cmd, env=run_env, stderr=subprocess.DEVNULL, timeout=2)
                    txt = out.decode('utf-8', errors='replace')
                    if txt:
                        return txt[:4000]
                except Exception:
                    continue
    if shutil.which('xsel'):
        for flag in ('-b', '-p'):
            try:
                out = subprocess.check_output(['xsel', flag, '-o'], env=run_env, stderr=subprocess.DEVNULL, timeout=2)
                txt = out.decode('utf-8', errors='replace')
                if txt:
                    return txt[:4000]
            except Exception:
                continue
    return ''


def set_clipboard(env: Dict[str, str], text: str) -> bool:
    """Write UTF-8 text into X11 clipboard via `xclip -target UTF8_STRING` (fallback `xsel`)."""
    run_env = _utf8_env(env)
    if os.name == 'nt':
        return False
    payload = (text or '').encode('utf-8')
    if shutil.which('xclip'):
        ok_any = False
        for sel in ('clipboard', 'primary'):
            try:
                proc = subprocess.Popen(
                    ['xclip', '-selection', sel, '-target', 'UTF8_STRING', '-i'],
                    env=run_env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                proc.communicate(input=payload, timeout=2)
                if proc.returncode == 0:
                    ok_any = True
            except Exception:
                continue
        if ok_any:
            return True
    if shutil.which('xsel'):
        ok_any = False
        for flag in ('-b', '-p'):
            try:
                proc = subprocess.Popen(
                    ['xsel', flag, '--input'],
                    env=run_env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                proc.communicate(input=payload, timeout=2)
                if proc.returncode == 0:
                    ok_any = True
            except Exception:
                continue
        if ok_any:
            return True
    return False


def _get_active_window_id(env: Dict[str, str]) -> str:
    if not shutil.which('xprop'):
        return ''
    try:
        out = subprocess.check_output(
            ['xprop', '-root', '_NET_ACTIVE_WINDOW'],
            env=env, stderr=subprocess.DEVNULL, timeout=2,
        ).decode('utf-8', errors='replace')
        m = re.search(r'0x[0-9a-fA-F]+', out)
        return _norm_hex_id(m.group(0)) if m else ''
    except Exception:
        return ''


def _get_stacking_order_ids(env: Dict[str, str]) -> List[str]:
    if not shutil.which('xprop'):
        return []
    try:
        out = subprocess.check_output(
            ['xprop', '-root', '_NET_CLIENT_LIST_STACKING'],
            env=env, stderr=subprocess.DEVNULL, timeout=2,
        ).decode('utf-8', errors='replace')
        return [_norm_hex_id(m) for m in re.findall(r'0x[0-9a-fA-F]+', out)]
    except Exception:
        return []


def _norm_hex_id(raw: str) -> str:
    try:
        return hex(int(raw, 16)).lower()
    except Exception:
        return raw.lower()


def _match_window(windows: List[Dict[str, object]], query: str) -> Optional[Dict[str, object]]:
    for w in windows:
        if _norm_hex_id(str(w.get('id', ''))) == _norm_hex_id(query):
            return w
    for w in windows:
        title = str(w.get('title', '')).lower()
        wm_class = str(w.get('wm_class', '')).lower()
        if query in title or query in wm_class:
            return w
    return None
