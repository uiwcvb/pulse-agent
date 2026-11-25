"""
[Module Responsibility]
轻量级 1C 1G 10GB 云端 Linux X11 虚拟桌面管理器、GUI/Bash 动作执行与 FastAPI 服务 (`server/computer_desktop.py`)。
负责 Xvfb/Openbox/WebProxy 生命周期 (`start`, `close`, `stop`, `takeover`, `handback`)、
高帧率按需画面合成 (`frame`, `status`)、持久化 Bash 会话委托 (`exec_bash`)、
GUI 鼠键与 `@bN` 元素动作分发 (`_exec_single_input`, `input`, `run`) 及独立守护进程 HTTP 路由 (`create_app`)。
"""
from __future__ import annotations

import asyncio, base64, contextlib, hashlib, hmac, io, os, re, secrets, shutil, subprocess, sys, tempfile, threading, time, zlib
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from cloud_os.agent_browser import cdp_click_or_type_ref, cdp_eval_js, extract_live_cdp_dom_snapshot, fetch_semantic_dom_snapshot, launch_modern_browser
from cloud_os.bash_session import CloudBashSession
from cloud_os.subsystem import ensure_subsystem_environment, get_subsystem_telemetry
from cloud_os.tmux_terminal import ensure_single_tmux_terminal
from cloud_os.window_manager import close_window, focus_window, get_clipboard, is_active_window_terminal, list_x11_windows, set_clipboard, tile_windows
from computer_browser import (
    DesktopBrowserMixin, HOME_HTML, _draw_papirus_icon, _generate_win11_wallpaper,
    _load_cjk_font, _set_x11_root_wallpaper, _trim_process_memory, resolve_grid_or_norm_xy,
)
from computer_files import WorkspaceFiles, install_file_routes
from computer_models import HEIGHT, WIDTH, InputAction, NavigateInput, RunInput, SteerInput
from computer_security import PublicWebProxy, public_addresses


class CloudDesktop(DesktopBrowserMixin):
    def __init__(self):
        self.home = Path(os.getenv('HOME', '/nonexistent'))
        self.files = self.home / 'Downloads'
        self.workspace = self.home / 'workspace'
        self.venv_dir = self.home / 'venv'
        self.live_log = self.workspace / '.agent_terminal.log'
        self.children = []
        self.browser_proc = None
        self.term_proc = None
        self.proxy = None
        self.task = None
        self.control = 'agent'
        self.user_takeover_locked = False
        self.step_running = False
        self.job = None
        self.error = ''
        self.clipboard = None
        self.input_lock = asyncio.Lock()
        self._frame_lock = threading.Lock()
        self.started = False
        self.current_url = ''
        self.current_title = 'Cloud Computer (1C 1G 10GB)'
        self.last_bash_cmd = ''
        self.last_bash_output = ''
        self.last_dom_snapshot: dict = {}
        self.last_frame_hash = ''
        self.last_frame_b64 = ''
        self.last_frame_at = 0.0
        self.last_browser_active_at = 0.0
        self.last_pointer_xy: tuple[int, int] | None = None
        self._scrot_supports_p: bool | None = None
        self._bash_session: CloudBashSession | None = None
        self._bash_session_home: Path | None = None
        self.reaper_task: asyncio.Task | None = None
        self.subsystem_env: dict[str, str] = {}

    def _get_bash_session(self) -> CloudBashSession:
        if self._bash_session is None or self._bash_session_home != self.home:
            self._bash_session = CloudBashSession(self.home, self.workspace, self._proxy_env)
            self._bash_session_home = self.home
        return self._bash_session

    def _proxy_env(self) -> dict[str, str]:
        venv_bin = str(self.venv_dir / 'bin')
        sub_path = self.subsystem_env.get('PATH', os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'))
        return {
            'HOME': str(self.home), 'USER': os.environ.get('USER', 'proactive-desktop'), 'SHELL': '/bin/bash',
            'XDG_RUNTIME_DIR': os.environ.get('XDG_RUNTIME_DIR', '/run/proactive-desktop'),
            'XDG_CONFIG_HOME': str(self.home / '.config'), 'XDG_CACHE_HOME': str(self.home / '.cache'),
            'DISPLAY': ':91', 'XAUTHORITY': str(self.home / '.Xauthority'), 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
            'DBUS_SESSION_BUS_ADDRESS': 'disabled:', 'DBUS_SYSTEM_BUS_ADDRESS': 'disabled:',
            'WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS': '1', 'WEBKIT_DISABLE_COMPOSITING_MODE': '1', 'WEBKIT_DISABLE_DMABUF_RENDERER': '1',
            'PATH': f'{venv_bin}:{sub_path}', 'LD_LIBRARY_PATH': self.subsystem_env.get('LD_LIBRARY_PATH', ''),
            'VIRTUAL_ENV': str(self.venv_dir), 'PYTHONUSERBASE': str(self.home / '.local'),
            'PREFIX': str(self.home / '.local'), 'CMAKE_INSTALL_PREFIX': str(self.home / '.local'),
            'npm_config_prefix': str(self.home / '.local'), 'PIP_CACHE_DIR': str(self.home / '.cache' / 'pip'),
            'TMPDIR': str(self.home / 'tmp'), 'SUBSYSTEM_PROFILE': '1 vCPU · 1 GB RAM · 10 GB SSD', 'TZ': 'Asia/Shanghai',
        }

    def _quota_info(self) -> dict:
        telem = get_subsystem_telemetry(self.home)
        return {
            'profile': telem['profile'], 'cpus': 1, 'memory_limit_mb': 1024, 'memory_used_mb': telem['ram_used_mb'],
            'memory_free_mb': max(0, 1024 - telem['ram_used_mb']), 'disk_limit_gb': 10.0,
            'disk_used_gb': round(telem['disk_used_mb'] / 1024.0, 2),
            'disk_free_gb': round(max(0, telem['disk_total_mb'] - telem['disk_used_mb']) / 1024.0, 2),
        }

    async def start(self):
        if sys.platform != 'linux' or os.geteuid() == 0:
            raise RuntimeError('Cloud computer needs its dedicated Linux account')
        self.files.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._setup_win11_environment)

        os.environ['DISPLAY'] = ':91'
        os.environ['XAUTHORITY'] = str(self.home / '.Xauthority')
        os.environ['LANG'] = 'C.UTF-8'
        os.environ['LC_ALL'] = 'C.UTF-8'
        cookie = secrets.token_hex(16)
        xauth_bin = os.environ.get('COMPUTER_XAUTH_EXECUTABLE') or shutil.which('xauth') or '/usr/bin/xauth'
        subprocess.run(
            [xauth_bin, '-f', os.environ['XAUTHORITY']],
            input=('add :91 . ' + cookie + '\n').encode(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=5,
        )
        self.spawn(['/usr/bin/Xvfb', ':91', '-screen', '0', f'{WIDTH}x{HEIGHT}x24', '-nolisten', 'tcp', '-noreset', '-auth', os.environ['XAUTHORITY']])
        for _ in range(80):
            if Path('/tmp/.X11-unix/X91').exists():
                break
            if self.children[0].poll() is not None:
                raise RuntimeError('Dedicated X display failed to start')
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError('Dedicated X display timed out')

        if shutil.which('xsetroot'):
            with contextlib.suppress(Exception):
                subprocess.run(['/usr/bin/xsetroot', '-solid', '#090E1A'], env=os.environ.copy(), timeout=3)

        self.spawn(['/usr/bin/openbox', '--config-file', str(Path(__file__).with_name('computer-openbox.xml'))])
        await asyncio.sleep(0.18)
        await asyncio.to_thread(_set_x11_root_wallpaper, self.home / '.config' / 'wallpaper.png', ':91')

        self.proxy = PublicWebProxy(18194)
        await self.proxy.start()
        (self.workspace / '.welcome.html').write_text(HOME_HTML, encoding='utf-8')
        self.current_title = 'Windows 11 Desktop · 1C 1G (+2G Swap) 10GB'
        self.reaper_task = None
        self.started = True
        _trim_process_memory()

    def spawn(self, args, cwd=None, extra_env=None):
        self.children = [c for c in self.children if c.poll() is None]
        env = {
            k: v for k, v in os.environ.items()
            if not any(s in k.upper() for s in ('TOKEN', 'SECRET', 'API_KEY', 'PASSWORD'))
        }
        env.update(self._proxy_env())
        if extra_env:
            env.update(extra_env)
        child = subprocess.Popen(
            args, cwd=cwd or str(self.workspace),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        )
        self.children.append(child)
        return child

    async def close(self):
        if self.reaper_task and not self.reaper_task.done():
            self.reaper_task.cancel()
        await self.stop()
        if self._bash_session is not None:
            with contextlib.suppress(Exception):
                await self._bash_session.close()
        if self.clipboard:
            self.clipboard.terminate()
        if self.proxy:
            await self.proxy.close()
        for child in reversed(self.children):
            if child.poll() is None:
                child.terminate()
        for child in self.children:
            with contextlib.suppress(subprocess.TimeoutExpired):
                await asyncio.to_thread(child.wait, 2)
        with contextlib.suppress(Exception):
            from cloud_os.agent_browser import close_tracked_browsers
            await asyncio.to_thread(close_tracked_browsers, 2.0)

    @staticmethod
    def _dedup_x11_windows(wins: list[dict], max_items: int = 16) -> list[dict]:
        """Collapse duplicate unfocused popup windows (e.g. repeated Chromium 'Profile error occurred')."""
        if not isinstance(wins, list) or not wins:
            return []
        deduped: list[dict] = []
        seen_unfocused: dict[tuple[str, str], int] = {}
        for w in wins:
            if not isinstance(w, dict):
                continue
            item = dict(w)
            focused = bool(item.get('focused'))
            title = str(item.get('title') or '').strip()
            wm_class = str(item.get('wm_class') or '').strip().lower()
            key = (title.lower(), wm_class)
            if not focused and title and key in seen_unfocused:
                idx = seen_unfocused[key]
                deduped[idx]['duplicate_count'] = int(deduped[idx].get('duplicate_count') or 1) + 1
                continue
            if not focused and title:
                seen_unfocused[key] = len(deduped)
            deduped.append(item)
            if len(deduped) >= max_items:
                break
        return deduped

    def _list_windows(self, force: bool = False) -> list[dict]:
        now_m = time.monotonic()
        cached_w = getattr(self, '_wins_cache', None)
        if not force and cached_w is not None and (now_m - cached_w[0]) < 0.6:
            return list(cached_w[1])
        wins = self._dedup_x11_windows(list_x11_windows(self._proxy_env()))
        self._wins_cache = (now_m, wins)
        return list(wins)

    def _active_windows_summary(self, wins: list[dict] | None = None) -> str:
        if wins is None:
            wins = self._list_windows()
        if wins:
            top_titles = [str(w.get('title') or '') for w in wins[:2] if w.get('title')]
            if top_titles:
                return ' · '.join(top_titles)
        return self.current_title

    def status(self):
        wins, dom_elems, page_txt = self._sync_active_dom_state()
        title = self._active_windows_summary(wins)
        focused_win = next((w for w in wins if w.get('focused')), wins[0] if wins else None)
        snap = self.last_dom_snapshot or {}
        return {
            'available': self.started,
            'width': WIDTH,
            'height': HEIGHT,
            'control': self.control or 'agent',
            'user_takeover_locked': bool(self.user_takeover_locked),
            'control_ready': self.started,
            'quota': self._quota_info(),
            'url': self.current_url,
            'title': title,
            'active_window': focused_win,
            'windows': wins,
            'x11_windows': wins,
            'window_count': len(wins),
            'dom_elements': dom_elems,
            'page_text': page_txt,
            'auth_gate': snap.get('auth_gate'),
            'browser_dialogs': snap.get('dialog_events', []),
            'venv_ready': (self.venv_dir / 'bin' / 'python3').exists(),
            'last_bash_cmd': self.last_bash_cmd,
            'last_bash_output': self.last_bash_output[-1500:] if self.last_bash_output else '',
            'job': self.job,
            'engine': 'Cloud Computer · 1C 1G (+2G Swap) 10GB Isolated Linux Subsystem',
            'error': self.error,
        }

    def _query_pointer_xy(self) -> tuple[int, int] | None:
        if sys.platform == 'linux' and shutil.which('xdotool'):
            try:
                out = subprocess.check_output(
                    ['xdotool', 'getmouselocation', '--shell'],
                    env=self._proxy_env(), stderr=subprocess.DEVNULL, timeout=1,
                ).decode('utf-8', errors='replace')
                m_map = dict(ln.split('=', 1) for ln in out.splitlines() if '=' in ln)
                return int(m_map['X']), int(m_map['Y'])
            except Exception:
                pass
        return self.last_pointer_xy

    def _capture_desktop_with_cursor(self, shot):
        from PIL import Image, ImageDraw

        image = Image.frombytes('RGB', shot.size, shot.bgra, 'raw', 'BGRX') if shot is not None else None
        if image is None:
            scrot_bin = shutil.which('scrot') if sys.platform == 'linux' else None
            if scrot_bin:
                tmp_fd, tmp_name = tempfile.mkstemp(prefix='frame_', suffix='.png')
                os.close(tmp_fd)
                tmp_path = Path(tmp_name)
                try:
                    subprocess.run([scrot_bin, str(tmp_path)], env=self._proxy_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5.0)
                    if tmp_path.exists() and tmp_path.stat().st_size > 0:
                        with Image.open(tmp_path) as im:
                            image = im.convert('RGB')
                except Exception:
                    image = None
                finally:
                    tmp_path.unlink(missing_ok=True)
        if image is not None:
            px_xy = self._query_pointer_xy()
            if px_xy is not None:
                mx, my = max(0, min(WIDTH - 1, px_xy[0])), max(0, min(HEIGHT - 1, px_xy[1]))
                draw_cur = ImageDraw.Draw(image)
                poly = [(mx, my), (mx, my + 16), (mx + 4, my + 12), (mx + 9, my + 18), (mx + 11, my + 16), (mx + 6, my + 10), (mx + 12, my + 10)]
                draw_cur.polygon(poly, fill=(255, 255, 255), outline=(15, 23, 42))
        return image

    def frame(self, quality: int = 88, since_hash: str = ''):
        import mss

        with self._frame_lock:
            with mss.mss(display=':91') as capture:
                shot = capture.grab({'left': 0, 'top': 0, 'width': WIDTH, 'height': HEIGHT})
                raw_bytes = bytes(shot.raw)
                pixel_sig = zlib.adler32(raw_bytes[::97]) & 0xFFFFFFFF
                h = hashlib.md5(f'{pixel_sig:08x}|{len(raw_bytes)}'.encode('ascii')).hexdigest()[:16]
                title = self._active_windows_summary()
                if since_hash and since_hash == h:
                    return {
                        'unchanged': True, 'hash': h, 'width': WIDTH, 'height': HEIGHT,
                        'url': self.current_url, 'title': title, 'control': self.control,
                        'user_takeover_locked': bool(self.user_takeover_locked), 'captured_at': time.time(),
                    }
                if self.current_url or self.last_browser_active_at > 0:
                    self.last_browser_active_at = time.time()
                if h == self.last_frame_hash and self.last_frame_b64 and quality in (62, 88):
                    b64 = self.last_frame_b64
                else:
                    image = self._capture_desktop_with_cursor(shot)
                    output = io.BytesIO()
                    image.save(output, 'JPEG', quality=max(30, min(90, quality)), optimize=False)
                    b64 = base64.b64encode(output.getvalue()).decode()
                    self.last_frame_hash = h
                    self.last_frame_b64 = b64
                    self.last_frame_at = time.time()
        return {
            'image': b64, 'hash': h, 'unchanged': False, 'mime': 'image/jpeg',
            'width': WIDTH, 'height': HEIGHT, 'url': self.current_url, 'title': title,
            'control': self.control, 'user_takeover_locked': bool(self.user_takeover_locked),
            'captured_at': time.time(),
        }

    async def takeover(self):
        self.control = 'user'
        self.user_takeover_locked = True
        self.last_browser_active_at = time.time()
        return self.status()

    async def handback(self):
        self.user_takeover_locked = False
        self.control = 'agent'
        self.last_browser_active_at = time.time()
        return self.status()

    async def stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        with contextlib.suppress(Exception):
            await self._get_bash_session().interrupt()
        self.step_running = False
        self.control = 'user' if self.user_takeover_locked else 'agent'
        self.job = None
        return self.status()

    async def exec_bash(self, command: str, timeout: int = 35, is_input: bool = False) -> dict:
        """Execute command via persistent `CloudBashSession` with middle truncation & soft-timeout."""
        cmd = (command or '').strip()
        if not cmd and not is_input:
            return {'exit_code': 0, 'output': '', 'running': False, 'timed_out': False}
        self.last_bash_cmd = (cmd or '<is_input>')[:400]
        res = await self._get_bash_session().run(command, timeout=timeout, is_input=is_input)
        out_str = str(res.get('output') or '')
        self.last_bash_output = out_str[-4000:]
        with contextlib.suppress(Exception):
            with self.live_log.open('a', encoding='utf-8') as f:
                f.write(f'\n$ {self.last_bash_cmd}\n{out_str[:4000]}\n')
        res.setdefault('venv', str(self.venv_dir))
        res.setdefault('quota', self._quota_info())
        return res

    async def _exec_single_input(
        self, kind: str, x: int = 0, y: int = 0, x2: int = 0, y2: int = 0,
        nx: int = -1, ny: int = -1, nx2: int = -1, ny2: int = -1,
        grid: str = '', grid2: str = '', ref: str = '', target: str = '', text: str = '',
        command: str = '', script: str = '', key: str = '', direction: str = 'down', amount: int = 3, app: str = '', timeout: int = 35,
        click: bool = False, clear: bool = False, submit: bool = False, dx: int = 0, dy: int = 0, is_input: bool = False,
        skip_dom_sync: bool = False,
    ) -> dict | None:
        env = self._proxy_env()

        def run(*args) -> tuple[bool, str]:
            xdotool_bin = shutil.which('xdotool') or '/usr/bin/xdotool'
            if not Path(xdotool_bin).exists():
                return False, 'xdotool 未安装'
            try:
                p = subprocess.run([xdotool_bin, *args], env=env, check=False, timeout=8, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                if p.returncode != 0:
                    err_s = (p.stderr or b'').decode('utf-8', errors='replace').strip()[:200]
                    return False, err_s or f'xdotool 退出码 {p.returncode}'
                return True, ''
            except subprocess.TimeoutExpired:
                return False, 'xdotool 执行超时'
            except OSError as exc:
                return False, f'xdotool 执行失败: {exc}'

        def _xtest_pointer(px_t: int, py_t: int, btn: int | None = None, repeat: int = 1) -> tuple[bool, str] | None:
            if sys.platform != 'linux' or not env.get('DISPLAY'):
                return None
            try:
                from Xlib import X, display as xdisplay
                from Xlib.ext import xtest
                d = xdisplay.Display(env.get('DISPLAY'))
                try:
                    xtest.fake_input(d, X.MotionNotify, x=int(px_t), y=int(py_t))
                    d.sync()
                    if btn is not None:
                        for idx_c in range(max(1, repeat)):
                            xtest.fake_input(d, X.ButtonPress, btn)
                            d.sync()
                            xtest.fake_input(d, X.ButtonRelease, btn)
                            d.sync()
                            if idx_c + 1 < repeat:
                                time.sleep(0.06)
                    return True, ''
                finally:
                    d.close()
            except Exception:
                return None

        if kind == 'wait':
            wait_sec = min(10.0, max(0.05, float(amount or 1)))
            await asyncio.sleep(wait_sec)
            return {'waited_sec': wait_sec}
        if kind == 'find_text':
            q = (text or target or ref or '').strip().lower()
            _, dom_elems_now, page_txt_now = await asyncio.to_thread(self._sync_active_dom_state, None, True)
            matches = [
                el for el in dom_elems_now
                if q and (q in str(el.get('label', '')).lower() or q in str(el.get('ref', '')).lower())
            ]
            out_ft: dict = {'query': q, 'matches': matches, 'found_in_page_text': bool(q and q in page_txt_now.lower()), 'clicked': False}
            if click:
                hit = next((m for m in matches if m.get('x') is not None and m.get('y') is not None), None)
                if hit is not None:
                    cx, cy = int(hit['x']), int(hit['y'])
                    self.last_pointer_xy = (cx, cy)
                    xok, xerr = await asyncio.to_thread(run, 'mousemove', '--sync', str(cx), str(cy), 'click', '1')
                    if not xok:
                        out_ft.update({'ok': False, 'error': f'点击文本 "{q}" 失败: {xerr}'})
                        return out_ft
                    await asyncio.sleep(0.3)
                    wins_c, elems_c, ptxt_c = await asyncio.to_thread(self._sync_active_dom_state, None, True)
                    out_ft.update({'ok': True, 'clicked': True, 'x': cx, 'y': cy, 'element': hit, 'windows': wins_c, 'dom_elements': elems_c, 'page_text': ptxt_c})
                else:
                    out_ft.update({'ok': False, 'error': f'未找到可点击文本 "{q}"，请改用 nx, ny 视觉坐标点击'})
            return out_ft
        if kind == 'bash':
            b_res = await self.exec_bash(command or text, timeout=timeout, is_input=is_input)
            ec = int(b_res.get('exit_code') or 0)
            if ec != 0 or b_res.get('timed_out'):
                b_res['ok'] = False
                b_res.setdefault('error', f'bash 命令{"超时" if b_res.get("timed_out") else f"失败 (exit_code={ec})"}')
            return b_res
        if kind == 'eval_js':
            return await asyncio.to_thread(cdp_eval_js, script or command or text, timeout=float(timeout or 8))
        if kind == 'launch_app':
            try:
                launched = await asyncio.to_thread(self._launch_or_focus_desktop_app, app or target or text, text if app else '')
            except ValueError as exc:
                return {'ok': False, 'error': str(exc)}
            await asyncio.sleep(0.22)
            if not launched or str(launched).startswith(('unknown_app', 'launch_failed')):
                return {'ok': False, 'launched': launched, 'error': f'无法启动应用: {launched}'}
            return {'ok': True, 'launched': launched}
        if kind == 'focus_window':
            fw = await asyncio.to_thread(focus_window, env, target or app or text)
            if not fw:
                return {'ok': False, 'focused_window': fw, 'error': f'未找到窗口: {target or app or text}'}
            return {'ok': True, 'focused_window': fw}
        if kind == 'close_window':
            q_close = (target or app or text or 'active').strip().lower()
            if q_close in ('all', 'desktop', '*', '全部', '所有'):
                wins_before = self._list_windows()
                for w_it in wins_before:
                    await asyncio.to_thread(close_window, env, str(w_it.get('id') or ''))
                await asyncio.sleep(0.25)
                self.last_dom_snapshot = {}
                self.current_url = ''
                self.current_title = 'Windows 11 Desktop · 1C 1G (+2G Swap) 10GB'
                return {'ok': True, 'closed_all': len(wins_before)}
            if q_close in ('active', 'current', ''):
                wins_before = self._list_windows()
                q_close = str(wins_before[0].get('id') or '') if wins_before else ''
            closed = await asyncio.to_thread(close_window, env, q_close) if q_close else None
            await asyncio.sleep(0.22)
            if not closed:
                return {'ok': False, 'closed_window': None, 'error': f'未找到可关闭的窗口: {q_close}'}
            return {'ok': True, 'closed_window': closed}
        if kind == 'tile_windows':
            return {'tiled_windows': await asyncio.to_thread(tile_windows, env, WIDTH, HEIGHT)}
        if kind == 'clipboard':
            if text:
                ok_clip = await asyncio.to_thread(set_clipboard, env, text)
                out_clip: dict = {'ok': bool(ok_clip), 'clipboard_set': bool(ok_clip), 'characters': len(text)}
                if not ok_clip:
                    out_clip['error'] = '写入 X11 剪贴板失败'
                return out_clip
            return {'clipboard_text': await asyncio.to_thread(get_clipboard, env)}
        if kind in ('click_ref', 'type_ref'):
            ref_id = (ref or target or '').strip()
            has_b_win = any(
                any(k in str(w.get('wm_class', '')).lower() for k in ('chromium', 'chrome', 'luakit'))
                for w in self._list_windows()
            )
            elems = (self.last_dom_snapshot or {}).get('elements', []) if has_b_win else self._desktop_icon_elements()
            matched_el = next((el for el in elems if str(el.get('ref', '')).lower() == ref_id.lower()), None)
            if matched_el and matched_el.get('app'):
                if kind == 'type_ref':
                    return {'ok': False, 'clicked_ref': matched_el.get('ref'), 'error': f'{ref_id} 是桌面图标，不支持文本输入'}
                launched = await asyncio.to_thread(self._launch_or_focus_desktop_app, str(matched_el['app']))
                if not launched or str(launched).startswith(('unknown_app', 'launch_failed')):
                    return {'ok': False, 'clicked_ref': matched_el.get('ref'), 'launched': launched, 'error': f'无法启动应用: {launched}'}
                return {'ok': True, 'clicked_ref': matched_el.get('ref'), 'launched': launched, 'x': int(matched_el.get('x') or 60), 'y': int(matched_el.get('y') or 66), 'element': matched_el}
            target_ref = str(matched_el.get('ref') if matched_el else ref_id)
            cdp_res = await asyncio.to_thread(cdp_click_or_type_ref, target_ref, text if kind == 'type_ref' else None, clear=clear, submit=submit)
            if cdp_res and isinstance(cdp_res, dict):
                if cdp_res.get('ok') is False:
                    return {'ok': False, 'clicked_ref': target_ref, 'error': str(cdp_res.get('error') or f'元素 {target_ref} 操作失败'), 'element': matched_el or cdp_res}
                cx, cy = int(cdp_res.get('x') or (matched_el or {}).get('x') or 0), int(cdp_res.get('y') or (matched_el or {}).get('y') or 0)
                if cx > 0 and cy > 0:
                    self.last_pointer_xy = (cx, cy)
                live_after = await asyncio.to_thread(extract_live_cdp_dom_snapshot, 280, getattr(self, 'files', None))
                if live_after:
                    self.last_dom_snapshot = live_after
                    if live_after.get('url'):
                        self.current_url = str(live_after['url'])
                    if live_after.get('title'):
                        self.current_title = f"{live_after['title']} · chromium-cdp"
                return {'ok': True, 'clicked_ref': target_ref, 'x': cx, 'y': cy, 'element': matched_el or cdp_res, 'dom_elements': (self.last_dom_snapshot or {}).get('elements', [])[:60], 'page_text': (self.last_dom_snapshot or {}).get('page_text', '')[:1200]}
            if matched_el and matched_el.get('x') and matched_el.get('y'):
                x, y, nx, ny, grid = int(matched_el['x']), int(matched_el['y']), -1, -1, ''
                kind = 'type' if kind == 'type_ref' else 'click'
            elif matched_el and matched_el.get('href') and kind == 'click_ref':
                nav_res = await self.navigate(str(matched_el['href']), include_frame=False)
                if isinstance(nav_res, dict) and nav_res.get('ok') is False:
                    return nav_res
                return {'ok': True, 'clicked_ref': matched_el.get('ref'), 'navigated_to': matched_el.get('href'), 'title': nav_res.get('title')}
            else:
                return {'ok': False, 'clicked_ref': ref_id, 'element': matched_el, 'error': f'未找到云桌面元素 {ref_id}，请直接使用画面 0..1000 坐标 nx, ny 操作'}

        try:
            px, py = resolve_grid_or_norm_xy(x, y, nx=nx, ny=ny, grid=grid, width=WIDTH, height=HEIGHT)
        except ValueError as exc:
            return {'ok': False, 'error': str(exc)}
        if kind in ('click', 'double_click', 'right_click', 'move', 'hover', 'scroll', 'drag'):
            self.last_pointer_xy = (px, py)
        x_ok, x_err = True, ''
        if kind == 'click':
            x_pair = await asyncio.to_thread(_xtest_pointer, px, py, 1, 1)
            x_ok, x_err = x_pair if x_pair is not None else await asyncio.to_thread(run, 'mousemove', '--sync', str(px), str(py), 'click', '1')
            if x_ok:
                await self._check_desktop_taskbar_launch(px, py)
        elif kind == 'double_click':
            x_pair = await asyncio.to_thread(_xtest_pointer, px, py, 1, 2)
            x_ok, x_err = x_pair if x_pair is not None else await asyncio.to_thread(run, 'mousemove', '--sync', str(px), str(py), 'click', '--repeat', '2', '--delay', '80', '1')
            if x_ok:
                await self._check_desktop_taskbar_launch(px, py)
        elif kind == 'right_click':
            x_pair = await asyncio.to_thread(_xtest_pointer, px, py, 3, 1)
            x_ok, x_err = x_pair if x_pair is not None else await asyncio.to_thread(run, 'mousemove', '--sync', str(px), str(py), 'click', '3')
        elif kind in ('move', 'hover'):
            x_pair = await asyncio.to_thread(_xtest_pointer, px, py, None, 0)
            x_ok, x_err = x_pair if x_pair is not None else await asyncio.to_thread(run, 'mousemove', '--sync', str(px), str(py))
        elif kind == 'drag':
            if x2 == 0 and y2 == 0 and nx2 < 0 and ny2 < 0 and not grid2 and (dx != 0 or dy != 0):
                px2, py2 = max(0, min(WIDTH - 1, px + dx)), max(0, min(HEIGHT - 1, py + dy))
            else:
                try:
                    px2, py2 = resolve_grid_or_norm_xy(x2, y2, nx=nx2, ny=ny2, grid=grid2, width=WIDTH, height=HEIGHT)
                except ValueError as exc:
                    return {'ok': False, 'error': str(exc)}
            x_ok, x_err = await asyncio.to_thread(run, 'mousemove', '--sync', str(px), str(py), 'mousedown', '1')
            if x_ok:
                await asyncio.sleep(0.05)
                x_ok, x_err = await asyncio.to_thread(run, 'mousemove', '--sync', str(px2), str(py2))
            if x_ok:
                await asyncio.sleep(0.05)
                x_ok, x_err = await asyncio.to_thread(run, 'mouseup', '1')
            if x_ok:
                self.last_pointer_xy = (px2, py2)
        elif kind == 'scroll':
            scroll_btn = {'up': '4', 'down': '5', 'left': '6', 'right': '7'}.get(direction, '5')
            x_ok, x_err = await asyncio.to_thread(run, 'mousemove', '--sync', str(px), str(py), 'click', '--repeat', str(max(1, min(20, amount))), scroll_btn)
        elif kind in ('key', 'hotkey'):
            mapped = self._normalize_xdotool_key(key or text or 'Return')
            x_ok, x_err = await asyncio.to_thread(run, 'key', '--clearmodifiers', mapped)
        elif kind == 'type':
            if px > 0 or py > 0:
                self.last_pointer_xy = (px, py)
                x_ok, x_err = await asyncio.to_thread(run, 'mousemove', '--sync', str(px), str(py), 'click', '1')
                if x_ok:
                    await asyncio.sleep(0.05)
            if x_ok and clear:
                x_ok, x_err = await asyncio.to_thread(run, 'key', '--clearmodifiers', 'ctrl+a')
                if x_ok:
                    await asyncio.sleep(0.03)
                    x_ok, x_err = await asyncio.to_thread(run, 'key', '--clearmodifiers', 'BackSpace')
                    await asyncio.sleep(0.04)
            if x_ok and text:
                if not await asyncio.to_thread(set_clipboard, env, text):
                    return {'ok': False, 'error': '写入 X11 剪贴板失败'}
                await asyncio.sleep(0.04)
                paste_key = 'ctrl+shift+v' if await asyncio.to_thread(is_active_window_terminal, env) else 'ctrl+v'
                x_ok, x_err = await asyncio.to_thread(run, 'key', '--clearmodifiers', paste_key)
            if x_ok and submit:
                await asyncio.sleep(0.05)
                x_ok, x_err = await asyncio.to_thread(run, 'key', '--clearmodifiers', 'Return')

        if not x_ok:
            return {'ok': False, 'error': f'输入执行失败 ({kind}): {x_err}'}

        if skip_dom_sync:
            await asyncio.sleep(0.05)
            out_meta: dict = {
                'ok': True, 'title': self._active_windows_summary(), 'url': self.current_url,
            }
            if kind in ('click', 'double_click', 'right_click', 'move', 'hover', 'type'):
                out_meta['x'], out_meta['y'] = px, py
            return out_meta

        await asyncio.sleep(0.12)
        wins_after, dom_elems_after, page_txt_after = await asyncio.to_thread(
            self._sync_active_dom_state, None, kind in ('click', 'double_click', 'scroll', 'key', 'hotkey', 'type'),
        )
        out_meta = {
            'ok': True, 'windows': wins_after, 'window_count': len(wins_after),
            'dom_elements': dom_elems_after, 'page_text': page_txt_after,
            'title': self._active_windows_summary(), 'url': self.current_url,
        }
        if kind in ('click', 'double_click', 'right_click', 'move', 'hover', 'type'):
            out_meta['x'], out_meta['y'] = px, py
        return out_meta

    @staticmethod
    def _point_covered_by_window(wins: list[dict], px: int, py: int) -> bool:
        for w in wins:
            wx, wy = int(w.get('x') or 0), int(w.get('y') or 0)
            ww, wh = int(w.get('width') or 0), int(w.get('height') or 0)
            if ww > 0 and wh > 0 and wx <= px < wx + ww and wy <= py < wy + wh:
                return True
        return False

    async def _check_desktop_taskbar_launch(self, px: int, py: int) -> None:
        wins = self._list_windows()
        if self._point_covered_by_window(wins, px, py):
            return
        if py >= HEIGHT - 45:
            taskbar_apps = ['chrome', 'terminal', 'files', 'code', 'excel', 'notepad', 'taskmgr', 'calculator']
            for idx, tb_app in enumerate(taskbar_apps):
                if abs(px - (38 + idx * 34)) <= 16:
                    await asyncio.to_thread(self._launch_or_focus_desktop_app, tb_app)
                    break
        elif not wins:
            for d_icon in self._desktop_icon_elements():
                if abs(px - int(d_icon['x'])) <= 44 and abs(py - int(d_icon['y'])) <= 46:
                    await asyncio.to_thread(self._launch_or_focus_desktop_app, str(d_icon['app']))
                    break

    @staticmethod
    def _normalize_xdotool_key(raw_key: str) -> str:
        raw_k = (raw_key or 'Return').strip()
        kmap = {'ctrl': 'ctrl', 'control': 'ctrl', 'alt': 'alt', 'shift': 'shift', 'super': 'super', 'enter': 'Return', 'return': 'Return', 'esc': 'Escape', 'escape': 'Escape'}
        if '+' in raw_k:
            return '+'.join(kmap.get(p.lower(), p.lower() if len(p) == 1 else p) for p in (x.strip() for x in raw_k.split('+')) if p)
        return {'Enter': 'Return', 'enter': 'Return', 'Backspace': 'BackSpace', 'PageDown': 'Page_Down', 'PageUp': 'Page_Up', 'Esc': 'Escape', 'esc': 'Escape'}.get(raw_k, raw_k)

    async def _wait_for_visual_settle(self, max_wait_s: float = 0.42):
        prev_h = None
        deadline = time.monotonic() + max_wait_s
        while time.monotonic() < deadline:
            try:
                cur_h = (await asyncio.to_thread(self.frame, 88)).get('hash')
            except Exception:
                return
            if prev_h is not None and cur_h == prev_h:
                return
            prev_h = cur_h
            await asyncio.sleep(0.11)

    async def input(self, action: InputAction):
        is_user = getattr(action, 'actor', 'agent') == 'user'
        if not is_user:
            self._auto_takeover_for_interactive()
        if self.user_takeover_locked and not is_user:
            return {'ok': False, 'error': '当前云电脑正由用户人工接管中（user_takeover_locked=True），操作已被锁定', 'control': 'user', 'user_takeover_locked': True, 'title': self._active_windows_summary(), 'url': self.current_url}
        _STEP_INT_DEFS = (('x', 0), ('y', 0), ('x2', 0), ('y2', 0), ('nx', -1), ('ny', -1), ('nx2', -1), ('ny2', -1), ('amount', 3), ('timeout', 35), ('dx', 0), ('dy', 0))
        _STEP_STR_DEFS = (('grid', ''), ('grid2', ''), ('ref', ''), ('target', ''), ('text', ''), ('command', ''), ('script', ''), ('key', ''), ('direction', 'down'), ('app', ''))
        _STEP_BOOL_DEFS = ('click', 'clear', 'submit', 'is_input')
        async with self.input_lock:
            extra_res = None
            if action.kind == 'batch' and action.steps:
                for step in action.steps[:25]:
                    kw = {k: int(step[k] if step.get(k) is not None else d) for k, d in _STEP_INT_DEFS}
                    kw.update({k: str(step.get(k) or d) for k, d in _STEP_STR_DEFS})
                    kw.update({k: bool(step.get(k, False)) for k in _STEP_BOOL_DEFS})
                    r = await self._exec_single_input(kind=str(step.get('kind') or step.get('op') or 'click'), skip_dom_sync=is_user, **kw)
                    if r is not None:
                        extra_res = r
                        if isinstance(r, dict) and r.get('ok') is False:
                            break
                    await asyncio.sleep(0.04 if is_user else 0.06)
            else:
                kw = {k: getattr(action, k) for k, _ in _STEP_INT_DEFS + _STEP_STR_DEFS}
                kw.update({k: getattr(action, k) for k in _STEP_BOOL_DEFS})
                extra_res = await self._exec_single_input(kind=action.kind, skip_dom_sync=is_user, **kw)
            if action.kind in ('bash', 'eval_js') and not action.include_frame:
                lean_out = {
                    'ok': bool(extra_res.get('ok', True)) if isinstance(extra_res, dict) else True,
                    'op': action.kind,
                    'control': self.control,
                    'user_takeover_locked': bool(self.user_takeover_locked),
                }
                if isinstance(extra_res, dict):
                    lean_out.update(extra_res)
                return lean_out
            if is_user:
                res: dict = {
                    'ok': True, 'control': self.control, 'user_takeover_locked': bool(self.user_takeover_locked),
                    'title': self._active_windows_summary(), 'url': self.current_url, 'quota': self._quota_info(),
                }
                if isinstance(extra_res, dict):
                    res.update(extra_res)
                if action.include_frame:
                    await asyncio.sleep(0.08)
                    res.update(await asyncio.to_thread(self.frame, 62))
                return res
            if isinstance(extra_res, dict) and 'windows' in extra_res and 'dom_elements' in extra_res:
                wins_now, dom_elems_now, page_txt_now = extra_res['windows'], extra_res['dom_elements'], extra_res.get('page_text', '')
            else:
                wins_now, dom_elems_now, page_txt_now = await asyncio.to_thread(self._sync_active_dom_state)
            focused_win = next((w for w in wins_now if w.get('focused')), wins_now[0] if wins_now else None)
            snap = self.last_dom_snapshot or {}
            res = {
                'ok': True, 'control': self.control, 'user_takeover_locked': bool(self.user_takeover_locked),
                'title': self._active_windows_summary(), 'url': self.current_url, 'active_window': focused_win,
                'windows': wins_now, 'x11_windows': wins_now, 'window_count': len(wins_now),
                'dom_elements': dom_elems_now, 'page_text': page_txt_now, 'auth_gate': snap.get('auth_gate'), 'quota': self._quota_info(),
            }
            if extra_res is not None:
                res.update(extra_res)
            if action.include_frame:
                await self._wait_for_visual_settle()
                res.update(await asyncio.to_thread(self.frame, 88))
            return res

    async def run(self, request: RunInput):
        run_id = secrets.token_hex(8)
        goal = (request.goal or '').strip()
        m_url = re.search(r'https?://[^\s"\'<>]+', goal)
        res_summary = '已就绪'
        if m_url and not any(k in goal for k in (';', '&&', '|', 'python', 'pip', 'gcc', 'apt', 'sudo')):
            await self.navigate(m_url.group(0), include_frame=False)
            res_summary = f'已在现代浏览器打开 {m_url.group(0)}'
        elif goal:
            b_res = await self.exec_bash(goal, timeout=35)
            res_summary = (b_res.get('output') or '').strip()[:1500] or f'exit={b_res.get("exit_code", 0)}'
        self.job = None
        st = self.status()
        st['job'] = {'id': run_id, 'state': 'done', 'goal': goal, 'step': 1, 'max_steps': 1, 'result': res_summary, 'trace': []}
        return st

    async def steer(self, text):
        return {'ok': False, 'error': 'steer_not_supported'}

    def file_path(self, name):
        if not name or '\\' in name or ':' in name:
            raise ValueError('文件路径无效')
        namespace, _, relative = name.partition('/')
        root = {'downloads': self.files, 'workspace': self.workspace}.get(namespace)
        if root is None or not relative:
            raise ValueError('文件路径无效')
        path = root / relative
        if '..' in Path(relative).parts or any(item.is_symlink() for item in [path, *path.parents] if item != root.parent):
            raise ValueError('文件路径无效')
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('文件不在云电脑工作区内')
        return path

    def list_files(self):
        files = []
        for namespace, root in [('downloads', self.files), ('workspace', self.workspace)]:
            for path in root.rglob('*'):
                if path.is_symlink() or not path.is_file():
                    continue
                relative = path.relative_to(root)
                if any(part.startswith('.') for part in relative.parts):
                    continue
                stat = path.stat()
                files.append({'path': namespace + '/' + relative.as_posix(), 'name': path.name, 'size': stat.st_size, 'bytes': stat.st_size, 'modified_at': stat.st_mtime})
                if len(files) >= 250:
                    return files
        return sorted(files, key=lambda item: item['modified_at'], reverse=True)


def _resolve_control_token(explicit: str | None = None) -> str:
    tok = (explicit or os.environ.get('COMPUTER_CONTROL_TOKEN') or '').strip()
    if tok:
        return tok
    tok_file = (os.environ.get('COMPUTER_TOKEN_FILE') or '').strip()
    if tok_file and Path(tok_file).is_file():
        with contextlib.suppress(Exception):
            return Path(tok_file).read_text(encoding='utf-8').strip()
    return ''


def create_app(desktop=None, token=None):
    desktop = desktop or CloudDesktop()
    resolved_token = _resolve_control_token(token)

    @asynccontextmanager
    async def lifespan(app):
        nonlocal resolved_token
        resolved_token = resolved_token or _resolve_control_token()
        if not resolved_token:
            raise RuntimeError('Private desktop control token is missing')
        try:
            await desktop.start()
            yield
        finally:
            await desktop.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.desktop = desktop

    async def auth(authorization: str = Header('')):
        if not resolved_token or not hmac.compare_digest(authorization, 'Bearer ' + resolved_token):
            raise HTTPException(401, 'Private computer endpoint')

    install_file_routes(app, auth, WorkspaceFiles(desktop.workspace, desktop.home / '.transfers'), prefix='/library')

    async def operation(coroutine):
        try:
            return await coroutine
        except (ValueError, subprocess.TimeoutExpired) as error:
            raise HTTPException(409, str(error)) from None

    @app.get('/home', response_class=HTMLResponse)
    async def home():
        return HOME_HTML

    @app.get('/status', dependencies=[Depends(auth)])
    async def status():
        return await asyncio.to_thread(desktop.status)

    @app.get('/frame', dependencies=[Depends(auth)])
    async def frame(since: str = '', quality: int = 88):
        return await asyncio.to_thread(desktop.frame, quality, since)

    @app.post('/takeover', dependencies=[Depends(auth)])
    async def takeover():
        return await desktop.takeover()

    @app.post('/handback', dependencies=[Depends(auth)])
    async def handback():
        return await desktop.handback()

    @app.post('/stop', dependencies=[Depends(auth)])
    async def stop():
        return await desktop.stop()

    @app.post('/navigate', dependencies=[Depends(auth)])
    async def navigate(body: NavigateInput):
        return await operation(desktop.navigate(body.url, body.include_frame, actor=body.actor))

    class SearchInput(BaseModel):
        query: str = ''
        engine: str = 'auto'
        include_frame: bool = True
        actor: str = 'agent'

    @app.post('/search', dependencies=[Depends(auth)])
    async def search_route(body: SearchInput):
        return await operation(desktop.search(body.query, body.engine, body.include_frame, actor=body.actor))

    @app.post('/input', dependencies=[Depends(auth)])
    async def input_action(body: InputAction):
        return await operation(desktop.input(body))

    @app.post('/run', dependencies=[Depends(auth)])
    async def run(body: RunInput):
        return await operation(desktop.run(body))

    @app.post('/steer', dependencies=[Depends(auth)])
    async def steer(body: SteerInput):
        return await operation(desktop.steer(body.text))

    @app.get('/files', dependencies=[Depends(auth)])
    async def files():
        return {'files': await asyncio.to_thread(desktop.list_files)}

    @app.get('/file', dependencies=[Depends(auth)])
    async def read_file(path: str, offset: int = 0, size: int = 262144, version: str | None = None):
        if offset < 0 or not 1 <= size <= 524288:
            raise HTTPException(400, '文件分块参数无效')
        try:
            target = desktop.file_path(path)
            stat = target.stat()
            total = stat.st_size
            fingerprint = f'{stat.st_mtime_ns}-{total}'
            if version and version != fingerprint:
                raise ValueError('文件在下载期间发生变化')
            if not target.is_file() or total > 50_000_000:
                raise ValueError('文件不可读取或超过 50 MB')
            with target.open('rb') as stream:
                stream.seek(offset)
                data = stream.read(size)
            return {'data': base64.b64encode(data).decode(), 'size': total, 'offset': offset, 'next_offset': offset + len(data), 'eof': offset + len(data) >= total, 'version': fingerprint}
        except (ValueError, OSError) as error:
            raise HTTPException(400, '云电脑文件不可读取') from error

    return app


app = create_app()
