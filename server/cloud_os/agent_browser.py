"""
SOTA Hybrid CDP Browser & Live Semantic DOM + Bounding-Box Engine (`server/cloud_os/agent_browser.py`).

1. **Persistent Hot-Reusable Chromium with CDP (`127.0.0.1:9222`)**:
   - Unified executable discovery (`COMPUTER_BROWSER_EXECUTABLE` -> Playwright cache -> system Chromium).
   - Automatic stale profile lock cleanup (`SingletonLock` / `SingletonCookie` / `SingletonSocket`).
   - Proper proxy split (`--proxy-server` + `--proxy-bypass-list`) with 30s-cached live HTTP proxy probe.
2. **CDP Session Guards & Live Rendered DOM (`@b1..@b60`)**:
   - Filters `devtools://` & extension targets; picks the active `http(s)/file` tab.
   - Enables `Page.enable` auto-accepting native JS dialogs (`alert`/`confirm`/`prompt`) without deadlock.
   - Enables `Browser.setDownloadBehavior` so file downloads save directly without GTK "Save As" dialogs.
   - Extracts live SPA elements, cross-origin iframe boxes, and `auth_gate` (QR login / CAPTCHA).
"""

from __future__ import annotations

import asyncio
import base64
import html as html_mod
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from cloud_os.browser_dom_scripts import LIVE_DOM_EXTRACT_JS, build_ref_action_js
from cloud_os.window_manager import list_x11_windows

CDP_PORT = 9222
_WARP_HOST = '127.0.0.1'
_WARP_PORT = 40001
_WARP_PROBE_CACHE: Optional[Tuple[float, Optional[str]]] = None
_RECENT_CDP_DIALOGS: List[Dict[str, object]] = []

PROXY_BYPASS_LIST = (
    '<-loopback>;localhost;127.0.0.1;'
    '*.qq.com;*.tencent.com;*.weixin.qq.com;*.myqcloud.com;*.gtimg.com;'
    '*.baidu.com;*.bilibili.com;*.zhihu.com;*.taobao.com;*.jd.com;'
    '*.163.com;*.feishu.cn;*.cn'
)

_DOMESTIC_DOMAINS = (
    'qq.com', 'tencent.com', 'weixin.qq.com', 'myqcloud.com', 'gtimg.com', 'qpic.cn',
    'baidu.com', 'bdstatic.com', 'bilibili.com', 'bilivideo.com', 'zhihu.com',
    'taobao.com', 'tmall.com', 'alicdn.com', 'jd.com', '360buyimg.com',
    '163.com', '126.net', 'feishu.cn', 'bytedance.com', 'douyin.com',
    'weibo.com', 'csdn.net', 'aliyun.com', 'cn',
)


def is_domestic_direct(url_or_host: str) -> bool:
    """Return True if the URL or hostname belongs to domestic CN services that should bypass WARP."""
    raw = (url_or_host or '').strip()
    if not raw:
        return False
    host = raw.lower()
    if '://' in raw:
        host = (urllib.parse.urlsplit(raw).hostname or '').lower()
    host = host.rstrip('.')
    if not host:
        return False
    return any(host == d or host.endswith('.' + d) for d in _DOMESTIC_DOMAINS)


def get_recent_cdp_dialogs() -> List[Dict[str, object]]:
    """Return a copy of recent auto-handled browser JS dialog events."""
    return list(_RECENT_CDP_DIALOGS[-10:])


def _record_dialog_event(params: Dict[str, object]) -> None:
    _RECENT_CDP_DIALOGS.append({
        'ts': time.time(),
        'type': str(params.get('type') or 'alert'),
        'message': str(params.get('message') or '')[:300],
        'url': str(params.get('url') or '')[:200],
    })
    if len(_RECENT_CDP_DIALOGS) > 20:
        del _RECENT_CDP_DIALOGS[:-20]


def _warp_proxy_url(force_refresh: bool = False) -> Optional[str]:
    """
    Probe local WARP HTTP proxy (`127.0.0.1:40001`) with a 30s-cached live HTTP CONNECT test.
    Returns `None` if WARP is down or stalled so Chromium smoothly falls back to direct egress.
    """
    global _WARP_PROBE_CACHE
    now = time.monotonic()
    if not force_refresh and _WARP_PROBE_CACHE is not None and (now - _WARP_PROBE_CACHE[0]) < 30.0:
        return _WARP_PROBE_CACHE[1]

    proxy_url: Optional[str] = None
    sock: Optional[socket.socket] = None
    try:
        sock = socket.create_connection((_WARP_HOST, _WARP_PORT), timeout=0.25)
        sock.settimeout(1.8)
        probe_req = (
            'CONNECT cp.cloudflare.com:443 HTTP/1.1\r\n'
            'Host: cp.cloudflare.com:443\r\n'
            'Proxy-Connection: keep-alive\r\n\r\n'
        ).encode('ascii')
        sock.sendall(probe_req)
        buf = b''
        while b'\r\n' not in buf and len(buf) < 4096:
            chunk = sock.recv(1024)
            if not chunk:
                break
            buf += chunk
        first_line = buf.split(b'\r\n', 1)[0].decode('ascii', errors='replace')
        if ' 200 ' in first_line:
            proxy_url = f'http://{_WARP_HOST}:{_WARP_PORT}'
    except OSError:
        proxy_url = None
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    _WARP_PROBE_CACHE = (now, proxy_url)
    return proxy_url


def find_chromium_executable() -> Optional[str]:
    """Locate a single consistent Chromium binary shared across `agent_browser` and `computer_browser`."""
    env_bin = os.environ.get('COMPUTER_BROWSER_EXECUTABLE', '').strip()
    if env_bin and Path(env_bin).is_file() and os.access(env_bin, os.X_OK):
        return env_bin
    search_roots = (
        Path('/opt/proactive-agent/desktop-venv'),
        Path('/opt/proactive-agent/browser-runtime'),
        Path('/root/.cache/ms-playwright'),
        Path.home() / '.cache' / 'ms-playwright',
    )
    candidates: List[Path] = []
    for root in search_roots:
        if not root.is_dir():
            continue
        for pat in ('chromium-*/chrome-linux/chrome', 'chromium-*/chrome-linux64/chrome', '**/chromium-*/chrome-linux/chrome', '**/chromium-*/chrome-linux64/chrome'):
            try:
                candidates.extend(p for p in root.glob(pat) if p.is_file() and os.access(p, os.X_OK))
            except OSError:
                continue
    if candidates:
        candidates.sort(key=str, reverse=True)
        return str(candidates[0])
    for fb in ('/usr/lib/chromium/chromium', shutil.which('chromium'), shutil.which('chromium-browser')):
        if fb and Path(fb).is_file() and os.access(fb, os.X_OK):
            return str(fb)
    return None


def _clean_chromium_profile_locks(profile_dir: Path) -> None:
    """Remove stale Chromium profile lock files and mark previous session clean."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    for name in ('SingletonLock', 'SingletonCookie', 'SingletonSocket', 'Lockfile'):
        try:
            (profile_dir / name).unlink(missing_ok=True)
        except Exception:
            pass
    pref_file = profile_dir / 'Default' / 'Preferences'
    if pref_file.exists():
        try:
            ptxt = pref_file.read_text(encoding='utf-8', errors='ignore')
            ptxt = ptxt.replace('"exit_type":"Crashed"', '"exit_type":"Normal"').replace('"exited_cleanly":false', '"exited_cleanly":true')
            pref_file.write_text(ptxt, encoding='utf-8')
        except Exception:
            pass
    for sub in ('component_crx_cache', 'WasmTtsEngine', 'BrowserMetrics-spare.pma', 'Default/Service Worker/CacheStorage', 'Default/Service Worker/ScriptCache'):
        p = profile_dir / sub
        try:
            if p.is_file() or p.is_symlink():
                p.unlink(missing_ok=True)
            elif p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass


def _cdp_get_targets(timeout: float = 3.0) -> List[Dict[str, object]]:
    """Return active CDP page targets if Chromium is listening on `127.0.0.1:9222`."""
    try:
        req = urllib.request.Request(f'http://127.0.0.1:{CDP_PORT}/json/list')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='replace'))
            if isinstance(data, list):
                return [t for t in data if isinstance(t, dict) and t.get('type') == 'page' and t.get('webSocketDebuggerUrl')]
    except Exception:
        pass
    return []


def _validate_http_url(url: str) -> str:
    """Enforce http/https URL scheme allowlist, blocking file://, javascript:, data:, etc."""
    raw = (url or '').strip()
    if not raw:
        raise ValueError('URL 不能为空')
    low = raw.lower()
    m = re.match(r'^([a-z][a-z0-9+.-]*):', low)
    if m and m.group(1) not in ('http', 'https') and not re.match(r'^[^/:]+:\d+(?:[/?#]|$)', low):
        raise ValueError(f'仅允许访问 http:// 或 https:// 网址，禁止协议: {m.group(1)}')
    if '://' in low and not low.startswith(('http://', 'https://')):
        raise ValueError(f'仅允许访问 http:// 或 https:// 网址，禁止协议: {low.split("://", 1)[0]}')
    target = raw if low.startswith(('http://', 'https://')) else 'https://' + raw
    parsed = urllib.parse.urlsplit(target)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('无效的 HTTP(S) 网址')
    return target


def _select_cdp_target(targets: List[Dict[str, object]]) -> Optional[Dict[str, object]]:
    """Pick the active content `http(s)` page tab, filtering `devtools://` and extensions."""
    if not targets:
        return None
    pool = [
        t for t in targets
        if not str(t.get('url') or '').strip().lower().startswith(('devtools://', 'chrome-extension://', 'chrome://extensions', 'chrome://settings'))
    ] or targets
    for t in pool:
        if str(t.get('url') or '').strip().lower().startswith(('http://', 'https://')):
            return t
    return pool[0]


def _ws_cdp_call(
    ws_url: str,
    commands: List[Tuple[str, Dict[str, object]]],
    timeout: float = 4.5,
) -> List[Dict[str, object]]:
    """
    Minimal RFC 6455 WebSocket client for local Chromium CDP RPC calls.
    Auto-accepts `Page.javascriptDialogOpening`, responds to WebSocket pings, and always closes cleanly.
    """
    parsed = urllib.parse.urlsplit(ws_url)
    host = parsed.hostname or '127.0.0.1'
    port = parsed.port or CDP_PORT
    path = parsed.path or '/'
    if parsed.query:
        path += '?' + parsed.query

    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        handshake = (
            f'GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n'
            f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n'
        )
        sock.sendall(handshake.encode('ascii'))
        resp_buf = b''
        while b'\r\n\r\n' not in resp_buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError('CDP WebSocket handshake closed early')
            resp_buf += chunk
        header_end = resp_buf.index(b'\r\n\r\n') + 4
        leftover = bytearray(resp_buf[header_end:])

        def _send_ws(opcode: int, raw: bytes) -> None:
            mask_key = os.urandom(4)
            header = bytearray([0x80 | (opcode & 0x0F)])
            length = len(raw)
            if length < 126:
                header.append(0x80 | length)
            elif length < 65536:
                header.append(0x80 | 126)
                header.extend(struct.pack('!H', length))
            else:
                header.append(0x80 | 127)
                header.extend(struct.pack('!Q', length))
            header.extend(mask_key)
            masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(raw))
            sock.sendall(bytes(header) + masked)

        def _recv_exact(n: int) -> bytes:
            while len(leftover) < n:
                chunk = sock.recv(65536)
                if not chunk:
                    raise RuntimeError('CDP WebSocket EOF')
                leftover.extend(chunk)
            out = bytes(leftover[:n])
            del leftover[:n]
            return out

        def _recv_frame() -> str:
            chunks: List[bytes] = []
            while True:
                b0, b1 = _recv_exact(2)
                fin = bool(b0 & 0x80)
                opcode = b0 & 0x0F
                masked = bool(b1 & 0x80)
                length = b1 & 0x7F
                if length == 126:
                    length = struct.unpack('!H', _recv_exact(2))[0]
                elif length == 127:
                    length = struct.unpack('!Q', _recv_exact(8))[0]
                mask_k = _recv_exact(4) if masked else b''
                payload = _recv_exact(length)
                if masked:
                    payload = bytes(b ^ mask_k[i % 4] for i, b in enumerate(payload))
                if opcode == 0x8:
                    raise RuntimeError('CDP WebSocket closed')
                if opcode == 0x9:
                    _send_ws(0xA, payload)
                    continue
                if opcode == 0xA:
                    continue
                if opcode in (0x0, 0x1, 0x2):
                    chunks.append(payload)
                    if fin:
                        return b''.join(chunks).decode('utf-8', errors='replace')

        next_id = 1
        results: List[Dict[str, object]] = []
        for method, params in commands:
            req_id = next_id
            next_id += 1
            _send_ws(0x1, json.dumps({'id': req_id, 'method': method, 'params': params or {}}).encode('utf-8'))
            deadline = time.time() + timeout
            got_reply = False
            while time.time() < deadline:
                msg = json.loads(_recv_frame())
                if not isinstance(msg, dict):
                    continue
                if msg.get('method') == 'Page.javascriptDialogOpening':
                    d_params = msg.get('params') if isinstance(msg.get('params'), dict) else {}
                    _record_dialog_event(d_params)
                    dlg_id = next_id
                    next_id += 1
                    _send_ws(0x1, json.dumps({'id': dlg_id, 'method': 'Page.handleJavaScriptDialog', 'params': {'accept': True}}).encode('utf-8'))
                    continue
                if msg.get('id') == req_id:
                    if msg.get('error'):
                        results.append({'_cdp_error': msg['error']})
                    else:
                        results.append(msg.get('result') or {})
                    got_reply = True
                    break
            if not got_reply:
                results.append({'_cdp_error': {'message': f'CDP timeout waiting for {method}'}})
        try:
            _send_ws(0x8, b'')
        except Exception:
            pass
        return results
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _ensure_cdp_session_guards(ws_url: str, downloads_dir: Optional[Path] = None) -> None:
    """Configure `Page.enable` (for JS dialog auto-handling) and non-blocking download directory."""
    dl_path = downloads_dir or (Path.home() / 'Downloads')
    try:
        dl_path.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    cmds: List[Tuple[str, Dict[str, object]]] = [
        ('Page.enable', {}),
        ('Browser.setDownloadBehavior', {'behavior': 'allowAndName', 'downloadPath': str(dl_path), 'eventsEnabled': True}),
        ('Page.setDownloadBehavior', {'behavior': 'allow', 'downloadPath': str(dl_path)}),
    ]
    try:
        _ws_cdp_call(ws_url, cmds, timeout=2.5)
    except Exception:
        pass


def extract_live_cdp_dom_snapshot(wait_ms: int = 350, downloads_dir: Optional[Path] = None) -> Optional[Dict[str, object]]:
    """If Chromium CDP is active, extract live DOM, `@b1..@b60` coordinates, and `auth_gate`."""
    target = _select_cdp_target(_cdp_get_targets(timeout=0.5))
    if not target:
        return None
    ws_url = str(target.get('webSocketDebuggerUrl') or '')
    if not ws_url:
        return None
    if wait_ms > 0:
        time.sleep(wait_ms / 1000.0)
    try:
        cmds: List[Tuple[str, Dict[str, object]]] = [('Page.enable', {})]
        if downloads_dir is not None:
            downloads_dir.mkdir(parents=True, exist_ok=True)
            cmds.append(('Browser.setDownloadBehavior', {
                'behavior': 'allowAndName',
                'downloadPath': str(downloads_dir),
                'eventsEnabled': True,
            }))
        cmds.append(('Runtime.evaluate', {'expression': LIVE_DOM_EXTRACT_JS, 'returnByValue': True}))
        res_list = _ws_cdp_call(ws_url, cmds, timeout=3.5)
        if not res_list or res_list[-1].get('_cdp_error'):
            return None
        val = ((res_list[-1].get('result') or {}).get('value'))
        if isinstance(val, str) and val:
            parsed = json.loads(val)
            if isinstance(parsed, dict):
                if _RECENT_CDP_DIALOGS:
                    parsed['dialog_events'] = get_recent_cdp_dialogs()
                return parsed
    except Exception:
        pass
    return None


def cdp_click_or_type_ref(
    ref_id: str,
    text: Optional[str] = None,
    *,
    clear: bool = False,
    submit: bool = False,
) -> Optional[Dict[str, object]]:
    """Click or type into `[data-agent-ref="@bN"]` inside live DOM/ShadowDOM/iframe via CDP."""
    target = _select_cdp_target(_cdp_get_targets(timeout=0.5))
    if not target:
        return None
    ws_url = str(target.get('webSocketDebuggerUrl') or '')
    if not ws_url:
        return None
    js = build_ref_action_js(ref_id, text=text, clear=clear, submit=submit)
    try:
        res_list = _ws_cdp_call(
            ws_url,
            [('Page.enable', {}), ('Runtime.evaluate', {'expression': js, 'returnByValue': True})],
            timeout=3.2,
        )
        if not res_list:
            return None
        last = res_list[-1]
        if last.get('_cdp_error'):
            return {'ok': False, 'error': f"CDP 执行失败: {last['_cdp_error']}"}
        if last.get('exceptionDetails'):
            desc = (last['exceptionDetails'].get('exception') or {}).get('description') or last['exceptionDetails'].get('text') or 'JS exception'
            return {'ok': False, 'error': f'页面脚本执行失败: {str(desc)[:180]}'}
        val = ((last.get('result') or {}).get('value'))
        if isinstance(val, str) and val:
            return json.loads(val)
    except Exception:
        pass
    return None


class _SemanticDOMParser(HTMLParser):
    """Lightweight static HTML fallback parser extracting clean readable text and link elements."""

    def __init__(self, base_url: str = '') -> None:
        super().__init__()
        self.base_url = base_url
        self._skip_depth = 0
        self._in_title = False
        self.title = ''
        self.text_chunks: List[str] = []
        self.elements: List[Dict[str, object]] = []
        self._active_elem: Optional[Dict[str, object]] = None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t in ('script', 'style', 'noscript', 'svg', 'path', 'head'):
            if t != 'head':
                self._skip_depth += 1
            return
        if t == 'title':
            self._in_title = True
            return
        if self._skip_depth > 0 or t != 'a':
            return
        attr_map = {k.lower(): (v or '') for k, v in attrs}
        href = attr_map.get('href', '').strip()
        if not href or href.startswith(('javascript:', '#')):
            return
        if self.base_url and not href.startswith(('http://', 'https://')):
            href = urllib.parse.urljoin(self.base_url, href)
        label = (attr_map.get('aria-label') or attr_map.get('title') or '').strip()
        self._active_elem = {'tag': 'a', 'type': 'a', 'label': label, 'href': href, '_text': []}

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ('script', 'style', 'noscript', 'svg', 'path'):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if t == 'title':
            self._in_title = False
            return
        if self._active_elem and t == 'a':
            inner = ' '.join(self._active_elem.pop('_text', [])).strip()
            lbl = self._active_elem.get('label') or inner or self._active_elem.get('href', '')[:60]
            self._active_elem['label'] = re.sub(r'\s+', ' ', str(lbl)).strip()[:90]
            if self._active_elem['label'] and len(self.elements) < 60:
                self._active_elem['ref'] = f'@b{len(self.elements) + 1}'
                self.elements.append(self._active_elem)
            self._active_elem = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip_depth > 0:
            return
        cleaned = re.sub(r'\s+', ' ', data).strip()
        if cleaned:
            self.text_chunks.append(cleaned)
            if self._active_elem is not None:
                self._active_elem['_text'].append(cleaned)


def _extract_search_query_from_url(url: str) -> Optional[str]:
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or '').lower()
        qs = urllib.parse.parse_qs(parsed.query)
        param_map = (
            (('bing.com', 'google.', 'duckduckgo.com', 'so.com', 'ecosia.org', 'brave.com'), ('q', 'query', 'wd', 'p')),
            (('baidu.com',), ('wd', 'word', 'q')),
            (('sogou.com',), ('query', 'keyword', 'q')),
            (('yahoo.',), ('p', 'q')),
        )
        for hosts, keys in param_map:
            if any(h in host for h in hosts):
                for k in keys:
                    if qs.get(k) and qs[k][0].strip():
                        return qs[k][0].strip()
    except Exception:
        pass
    return None


def _is_poisoned_or_blocked_search(query: str, title: str, page_text: str) -> bool:
    combined = f'{title} {page_text}'.lower()
    if len(page_text.strip()) < 120:
        return True
    if any(bad in combined for bad in ('anomaly-modal', 'unusual traffic', '安全验证', '百度安全验证', 'there are no results for')):
        return True
    q_tokens = [t.lower() for t in re.split(r'\s+', query.strip()) if len(t) >= 2] or [query.strip().lower()]
    cjk_chars = [ch for ch in query if '\u4e00' <= ch <= '\u9fff']
    if len(cjk_chars) >= 3:
        if ''.join(cjk_chars[:2]) not in combined:
            return True
    elif q_tokens and not any(tok in combined for tok in q_tokens):
        return True
    return False


def _run_fallback_multi_search_sync(query: str) -> Dict[str, object]:
    from core.search_engine import multi_web_search
    try:
        return asyncio.run(multi_web_search(query, max_results=12))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(multi_web_search(query, max_results=12))
        finally:
            loop.close()


def _render_search_results_html(query: str, search_data: Dict[str, object], cwd: Path) -> Path:
    items = search_data.get('results') or []
    rows_html = []
    for idx, item in enumerate(items, 1):
        t = html_mod.escape(str(item.get('title') or ''))
        u = html_mod.escape(str(item.get('url') or ''))
        s = html_mod.escape(str(item.get('snippet') or ''))
        eng = html_mod.escape(str(item.get('engine') or 'web'))
        rows_html.append(
            f'<div class="res"><div class="meta">[{idx}] {eng} · <span class="url">{u[:95]}</span></div>'
            f'<h3><a href="{u}">{t}</a></h3><p>{s}</p></div>'
        )
    body_rows = '\n'.join(rows_html) if rows_html else '<p>未找到相关结果</p>'
    q_esc = html_mod.escape(query)
    html_doc = (
        f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>{q_esc} - 搜索结果</title>'
        '<style>body{margin:0;font-family:"Noto Sans CJK SC",system-ui,sans-serif;background:#0b1120;color:#f8fafc;padding:20px 28px}'
        '.bar{display:flex;align-items:center;gap:12px;padding-bottom:14px;border-bottom:1px solid #1e293b;margin-bottom:16px}.qbox{background:#111827;border:1px solid #334155;border-radius:10px;padding:9px 16px;color:#fff;font-size:15px;flex:1}'
        '.badge{font-size:12px;color:#38bdf8;background:rgba(56,189,248,0.14);padding:5px 12px;border-radius:8px;border:1px solid rgba(56,189,248,0.25)}.res{background:#111827;border:1px solid #1e293b;border-radius:12px;padding:13px 18px;margin-bottom:10px}'
        '.meta{font-size:11.5px;color:#94a3b8;margin-bottom:4px}.url{color:#64748b}h3{margin:0 0 6px 0;font-size:15.5px}h3 a{color:#38bdf8;text-decoration:none}p{margin:0;font-size:13px;color:#cbd5e1;line-height:1.5}</style></head><body>'
        f'<div class="bar"><div class="qbox">🔍 {q_esc}</div><div class="badge">WARP 多源搜索结果 ({len(items)} 条)</div></div>{body_rows}</body></html>'
    )
    out_file = cwd / '.last_search_results.html'
    out_file.write_text(html_doc, encoding='utf-8')
    return out_file


def _wait_for_browser_window(run_env: Dict[str, str], timeout_s: float = 10.0) -> bool:
    """Wait until an actual Chromium/Chrome window maps on X11 or CDP responds."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        wins = list_x11_windows(run_env)
        for w in wins:
            wclass = str(w.get('wm_class', '')).lower()
            wtitle = str(w.get('title', '')).lower()
            if any(k in wclass for k in ('chromium', 'chrome')):
                return True
            if any(k in wtitle for k in ('chromium', 'google chrome')):
                return True
        if wins and _cdp_get_targets(timeout=0.2):
            return True
        time.sleep(0.18)
    return False


def launch_modern_browser(
    url: str,
    env: Dict[str, str],
    cwd: Path,
    width: int = 1280,
    height: int = 720,
    downloads_dir: Optional[Path] = None,
) -> Tuple[str, Dict[str, object]]:
    """Open or navigate `url` in persistent CDP-enabled Chromium."""
    target_url = _validate_http_url(url)

    run_env = dict(os.environ)
    run_env.update(env)
    home_dir = Path(run_env.get('HOME') or str(cwd.parent))
    run_env['HOME'] = str(home_dir)
    run_env.setdefault('LANG', 'C.UTF-8')
    run_env.setdefault('LC_ALL', 'C.UTF-8')
    run_env.update({'DBUS_SESSION_BUS_ADDRESS': 'disabled:', 'DBUS_SYSTEM_BUS_ADDRESS': 'disabled:', 'WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS': '1', 'WEBKIT_DISABLE_COMPOSITING_MODE': '1', 'WEBKIT_DISABLE_DMABUF_RENDERER': '1'})

    dl_dir = downloads_dir or (home_dir / 'Downloads')
    warp_url = _warp_proxy_url()
    domestic_direct = is_domestic_direct(target_url)
    for k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'all_proxy'):
        run_env.pop(k, None)
    if warp_url and not domestic_direct:
        for k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY'):
            run_env[k] = warp_url
        run_env['NO_PROXY'] = 'localhost,127.0.0.1'

    search_q = _extract_search_query_from_url(target_url)
    display_url = target_url
    pre_snapshot: Optional[Dict[str, object]] = None
    if search_q:
        pre_snapshot = fetch_semantic_dom_snapshot(target_url, width=width, height=height, cwd=cwd)
        display_url = str(pre_snapshot.get('_local_render_uri') or target_url)
        pre_snapshot.pop('_local_render_uri', None)

    browser_used = 'chromium-cdp'
    chrom_bin = find_chromium_executable()

    if os.name != 'nt' and run_env.get('DISPLAY'):
        target = _select_cdp_target(_cdp_get_targets(timeout=3.0))
        if not target and chrom_bin and _wait_for_browser_window(run_env, timeout_s=1.2):
            for _ in range(3):
                target = _select_cdp_target(_cdp_get_targets(timeout=2.0))
                if target:
                    break
                time.sleep(0.3)
        if target and chrom_bin:
            ws_url = str(target.get('webSocketDebuggerUrl') or '')
            try:
                _ensure_cdp_session_guards(ws_url, dl_dir)
                _ws_cdp_call(ws_url, [('Page.navigate', {'url': display_url})], timeout=8.0)
                time.sleep(1.5 if domestic_direct or not display_url.startswith('file://') else 0.35)
                live_snap = extract_live_cdp_dom_snapshot(wait_ms=250, downloads_dir=dl_dir)
                if live_snap:
                    txt = str(live_snap.get('page_text') or '')
                    if 'ERR_PROXY_CONNECTION_FAILED' in txt or ('ERR_EMPTY_RESPONSE' in txt and domestic_direct):
                        live_snap = None
                if live_snap and (live_snap.get('page_text') or live_snap.get('elements') or live_snap.get('auth_gate') or live_snap.get('title')):
                    live_snap['browser'] = 'chromium-cdp'
                    return 'chromium-cdp', live_snap
            except Exception:
                pass

        if chrom_bin and not _wait_for_browser_window(run_env, timeout_s=0.35):
            profile_dir = home_dir / '.local' / 'chromium_profile'
            _clean_chromium_profile_locks(profile_dir)
            cmd = [
                str(chrom_bin), '--no-sandbox', '--test-type', '--disable-infobars', '--hide-crash-restore-bubble', '--disable-session-crashed-bubble',
                '--disable-gpu', '--disable-dev-shm-usage', '--disable-software-rasterizer', '--disable-blink-features=AutomationControlled', '--no-first-run',
                '--no-default-browser-check', '--disable-background-networking', '--disable-component-update', '--disable-sync', '--disable-translate', '--hide-scrollbars',
                '--disk-cache-size=33554432', '--media-cache-size=16777216', '--renderer-process-limit=1', '--js-flags=--max-old-space-size=256',
                f'--remote-debugging-port={CDP_PORT}', '--remote-allow-origins=*', f'--user-data-dir={profile_dir}', f'--window-size={width - 28},{height - 64}', '--window-position=14,10', display_url,
            ]
            if warp_url and not domestic_direct:
                cmd.insert(-1, f'--proxy-server={warp_url}')
                cmd.insert(-1, f'--proxy-bypass-list={PROXY_BYPASS_LIST}')
            subprocess.Popen(cmd, env=run_env, cwd=str(cwd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            browser_used = 'chromium-cdp'
            _wait_for_browser_window(run_env, timeout_s=10.0)
            new_target = None
            for _ in range(24):
                new_target = _select_cdp_target(_cdp_get_targets(timeout=0.25))
                if new_target:
                    break
                time.sleep(0.25)
            if new_target and new_target.get('webSocketDebuggerUrl'):
                _ensure_cdp_session_guards(str(new_target['webSocketDebuggerUrl']), dl_dir)
            time.sleep(1.2 if not display_url.startswith('file://') else 0.35)
            live_snap = extract_live_cdp_dom_snapshot(wait_ms=250, downloads_dir=dl_dir)
            if live_snap and (live_snap.get('page_text') or live_snap.get('elements') or live_snap.get('auth_gate') or live_snap.get('title') or display_url.startswith('file://')):
                live_snap['browser'] = browser_used
                return browser_used, live_snap

    snapshot = pre_snapshot or fetch_semantic_dom_snapshot(target_url, width=width, height=height, cwd=cwd)
    snapshot.pop('_local_render_uri', None)
    snapshot['browser'] = browser_used
    return browser_used, snapshot


def fetch_semantic_dom_snapshot(url: str, width: int = 1280, height: int = 720, cwd: Optional[Path] = None) -> Dict[str, object]:
    """Fetch live CDP DOM snapshot or static HTTP(S) fallback without fabricating missing interactive refs."""
    target_url = _validate_http_url(url)
    live = extract_live_cdp_dom_snapshot(wait_ms=100)
    if live and str(live.get('url') or '').rstrip('/') == target_url.rstrip('/'):
        if live.get('elements') or live.get('page_text') or live.get('title') or live.get('auth_gate'):
            return live

    html_text = ''
    final_url = target_url
    fetch_error = ''
    warp_url = _warp_proxy_url()
    domestic_direct = is_domestic_direct(target_url)
    ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'

    handlers = [urllib.request.ProxyHandler({'http': warp_url, 'https': warp_url})] if (warp_url and not domestic_direct) else []
    opener = urllib.request.build_opener(*handlers)
    opener.addheaders = [
        ('User-Agent', ua),
        ('Accept', 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'),
        ('Accept-Language', 'zh-CN,zh;q=0.9,en;q=0.8'),
    ]
    try:
        with opener.open(target_url, timeout=8) as resp:
            raw_bytes = resp.read(450_000)
            final_url = resp.geturl() or target_url
            html_text = raw_bytes.decode(resp.headers.get_content_charset() or 'utf-8', errors='replace')
    except Exception as exc:
        fetch_error = str(exc)[:180]
        if handlers:
            try:
                req = urllib.request.Request(target_url, headers={'User-Agent': ua, 'Accept-Language': 'zh-CN,zh;q=0.9'})
                with urllib.request.urlopen(req, timeout=7) as resp:
                    raw_bytes = resp.read(450_000)
                    final_url = resp.geturl() or target_url
                    html_text = raw_bytes.decode(resp.headers.get_content_charset() or 'utf-8', errors='replace')
                    fetch_error = ''
            except Exception as exc2:
                fetch_error = str(exc2)[:180]

    parser = _SemanticDOMParser(base_url=final_url)
    if html_text:
        try:
            parser.feed(html_text)
        except Exception:
            pass

    title_str = re.sub(r'\s+', ' ', parser.title).strip()[:140]
    page_text = re.sub(r'\s+', ' ', ' '.join(parser.text_chunks)).strip()[:3200]
    # Static HTML fallback elements only retain links with `href`; non-live <input>/<button> lack CDP refs/coords
    elements = [el for el in parser.elements if el.get('href')][:60]

    search_q = _extract_search_query_from_url(target_url)
    local_render_uri = None
    if search_q and _is_poisoned_or_blocked_search(search_q, title_str, page_text):
        s_data = _run_fallback_multi_search_sync(search_q)
        s_items = s_data.get('results') or []
        if s_items:
            title_str = f'{search_q} - 搜索结果'
            lines, new_elements = [], []
            for idx, it in enumerate(s_items[:12], 1):
                it_title, it_url, it_snip = str(it.get('title') or ''), str(it.get('url') or ''), str(it.get('snippet') or '')
                lines.append(f'[{idx}] {it_title} ({it_url}) — {it_snip}')
                new_elements.append({'ref': f'@b{idx}', 'tag': 'a', 'type': 'a', 'label': it_title[:90], 'href': it_url})
            page_text = '\n'.join(lines)[:3200]
            elements = new_elements
            if cwd is not None:
                try:
                    local_render_uri = f'file://{_render_search_results_html(search_q, s_data, cwd)}'
                except Exception:
                    pass

    res: Dict[str, object] = {'url': final_url, 'title': title_str, 'page_text': page_text, 'elements': elements, 'auth_gate': None}
    if fetch_error and not html_text:
        res['fetch_error'] = fetch_error
    if local_render_uri:
        res['_local_render_uri'] = local_render_uri
    return res
