"""Private loopback / dynamic SSH-tunnel RPC to this project's dedicated desktop service."""
from __future__ import annotations
import asyncio, os, re, time
from pathlib import Path
from typing import Any, Callable
import httpx

class ComputerUnavailable(ValueError):pass


class ComputerClient:
    def __init__(
        self,
        token: str | None = None,
        transport: Any = None,
        *,
        base_url: str | None = None,
        config_getter: Callable[[], Any] | None = None,
        data_dir: Path | str | None = None,
    ):
        self._explicit_token = token
        self._explicit_base = (base_url or '').strip().rstrip('/') or None
        self.config_getter = config_getter
        self.data_dir = Path(data_dir) if data_dir else None
        self.transport = transport
        self.cached: dict[str, Any] | None = None
        self.cached_at = 0.0
        self.lock = asyncio.Lock()
        self._tunnel_lock = asyncio.Lock()
        self._ssh_conn: Any = None
        self._ssh_listener: Any = None
        self._tunnel_sig: tuple[Any, ...] | None = None
        self._tunnel_port: int | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._client_base: str = ''

    def _env_token(self) -> str:
        tok = (os.getenv('COMPUTER_CONTROL_TOKEN') or os.getenv('COMPUTER_SERVICE_TOKEN') or '').strip()
        if tok:
            return tok
        tok_file = (os.getenv('COMPUTER_TOKEN_FILE') or '').strip()
        if tok_file:
            p = Path(tok_file)
            if p.is_file():
                try:
                    return p.read_text('utf-8').strip()
                except Exception:
                    pass
        return ''

    def _env_base(self) -> str:
        return (os.getenv('COMPUTER_SERVICE_URL') or 'http://127.0.0.1:18193').strip().rstrip('/')

    def _cfg(self) -> Any:
        if callable(self.config_getter):
            try:
                return self.config_getter()
            except Exception:
                return None
        return None

    def _mode_and_token(self) -> tuple[str, str, Any]:
        if self._explicit_token is not None:
            return ('explicit', self._explicit_token, None)
        cfg = self._cfg()
        if cfg is not None:
            mode = str(getattr(cfg, 'cloud_computer_mode', '') or '').strip().lower()
            host = str(getattr(cfg, 'cloud_computer_host', '') or '').strip()
            url = str(getattr(cfg, 'cloud_computer_url', '') or '').strip().rstrip('/')
            tok = str(getattr(cfg, 'cloud_computer_token', '') or '').strip() or self._env_token()
            if mode == 'ssh_tunnel' and host and tok:
                return ('ssh_tunnel', tok, cfg)
            if url and tok:
                return ('http', tok, cfg)
        return ('local', self._env_token(), cfg)

    @property
    def token(self) -> str:
        return self._mode_and_token()[1]

    @property
    def base(self) -> str:
        mode, _, cfg = self._mode_and_token()
        if mode == 'explicit':
            return self._explicit_base or self._env_base()
        if mode == 'ssh_tunnel' and self._tunnel_port:
            return f'http://127.0.0.1:{self._tunnel_port}'
        if mode == 'http' and cfg is not None:
            return str(getattr(cfg, 'cloud_computer_url', '') or '').strip().rstrip('/') or self._env_base()
        return self._explicit_base or self._env_base()

    @property
    def configured(self) -> bool:
        mode, tok, cfg = self._mode_and_token()
        if mode == 'ssh_tunnel':
            return bool(tok and cfg is not None and str(getattr(cfg, 'cloud_computer_host', '') or '').strip())
        return bool(tok)

    async def _close_tunnel(self) -> None:
        if self._ssh_listener is not None:
            try:
                self._ssh_listener.close()
                await self._ssh_listener.wait_closed()
            except Exception:
                pass
            self._ssh_listener = None
        if self._ssh_conn is not None:
            try:
                self._ssh_conn.close()
                await self._ssh_conn.wait_closed()
            except Exception:
                pass
            self._ssh_conn = None
        self._tunnel_port = None
        self._tunnel_sig = None

    async def _ensure_ssh_tunnel(self, cfg: Any) -> str:
        import asyncssh

        host = str(getattr(cfg, 'cloud_computer_host', '') or '').strip()
        port = int(getattr(cfg, 'cloud_computer_port', 22) or 22)
        user = str(getattr(cfg, 'cloud_computer_user', '') or 'root').strip()
        password = str(getattr(cfg, 'cloud_computer_password', '') or '').strip()
        key_pem = str(getattr(cfg, 'cloud_computer_ssh_key', '') or '').strip()
        if not key_pem and self.data_dir:
            key_p = self.data_dir / 'cloud_computer_ed25519'
            if key_p.is_file():
                try:
                    key_pem = key_p.read_text('utf-8').strip()
                except Exception:
                    pass
        sig = (host, port, user, bool(password), hash(key_pem))
        async with self._tunnel_lock:
            conn_alive = self._ssh_conn is not None and not getattr(self._ssh_conn, '_transport', None) is None
            try:
                if self._ssh_conn is not None and getattr(self._ssh_conn, 'is_closed', lambda: True)():
                    conn_alive = False
            except Exception:
                pass
            if conn_alive and self._ssh_listener is not None and self._tunnel_port and self._tunnel_sig == sig:
                return f'http://127.0.0.1:{self._tunnel_port}'

            await self._close_tunnel()
            client_keys: list[Any] = []
            if key_pem:
                try:
                    client_keys.append(asyncssh.import_private_key(key_pem))
                except Exception:
                    pass
            try:
                conn = await asyncssh.connect(
                    host,
                    port=port,
                    username=user,
                    password=password or None,
                    client_keys=client_keys or None,
                    known_hosts=None,
                    connect_timeout=15,
                    keepalive_interval=30,
                )
                listener = await conn.forward_local_port('127.0.0.1', 0, '127.0.0.1', 18193)
                local_port = int(listener.get_port())
            except Exception as exc:
                raise ComputerUnavailable(f'云电脑 SSH 隧道连接失败 ({user}@{host}:{port}): {exc}') from None
            self._ssh_conn = conn
            self._ssh_listener = listener
            self._tunnel_port = local_port
            self._tunnel_sig = sig
            return f'http://127.0.0.1:{local_port}'

    def _get_client(self, target_base: str) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed or self._client_base != target_base:
            if self._http_client is not None and not self._http_client.is_closed:
                try:
                    asyncio.get_running_loop().create_task(self._http_client.aclose())
                except Exception:
                    pass
            self._client_base = target_base
            self._http_client = httpx.AsyncClient(
                base_url=target_base,
                timeout=httpx.Timeout(45.0, connect=3.0),
                trust_env=False,
                transport=self.transport,
            )
        return self._http_client

    async def reload_config(self) -> dict[str, Any]:
        if self._http_client is not None and not self._http_client.is_closed:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
        self._http_client = None
        self._client_base = ''
        await self._close_tunnel()
        self.cached = None
        self.cached_at = 0.0
        if self.configured:
            return await self.status(force=True)
        return {'configured': False, 'available': False}

    async def aclose(self) -> None:
        if self._http_client is not None and not self._http_client.is_closed:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
        self._http_client = None
        await self._close_tunnel()

    async def call(self, method, path, body=None, query=None):
        if not self.configured:
            raise ComputerUnavailable('云电脑尚未启用')
        if path not in ('/status', '/frame', '/takeover', '/handback', '/stop', '/navigate', '/search', '/input', '/run', '/steer', '/files', '/file', '/library/upload', '/library/uploads') and not re.fullmatch(r'/library/uploads/[a-f0-9]{32}(/finish)?', path):
            raise ValueError('云电脑操作无效')
        req_timeout = 6.0 if path == '/status' else (12.0 if path == '/frame' else 45.0)
        if isinstance(body, dict) and body.get('timeout'):
            try:
                req_timeout = max(req_timeout, float(body['timeout']) + 15.0)
            except (ValueError, TypeError):
                pass
        mode, tok, cfg = self._mode_and_token()
        response = None
        for attempt in range(2):
            try:
                target_base = await self._ensure_ssh_tunnel(cfg) if mode == 'ssh_tunnel' else self.base
                client = self._get_client(target_base)
                response = await client.request(method, path, json=body, params=query, headers={'Authorization': 'Bearer ' + tok}, timeout=req_timeout)
                break
            except httpx.TimeoutException:
                raise ComputerUnavailable(f'云电脑操作响应超时（{path} > {int(req_timeout)}s），请缩短单次命令耗时或稍后重试') from None
            except httpx.RequestError:
                if self._http_client and not self._http_client.is_closed:
                    try:
                        await self._http_client.aclose()
                    except Exception:
                        pass
                self._http_client = None
                if mode == 'ssh_tunnel':
                    await self._close_tunnel()
                if attempt == 0:
                    await asyncio.sleep(0.25)
                    continue
                raise ComputerUnavailable('暂时连不上云电脑服务，请稍后重试') from None
        if response.status_code == 409:
            detail = self._extract_http_error_detail(response, default='当前操作暂不可用', limit=400)
            raise ValueError(detail)
        if response.status_code >= 400:
            detail = self._extract_http_error_detail(response, default='', limit=400)
            raise ComputerUnavailable(f'云电脑操作失败（HTTP {response.status_code}{": " + detail if detail else ""}）')
        self.cached_at = 0.0 if method != 'GET' else self.cached_at
        return response.json()

    @staticmethod
    def _extract_http_error_detail(response: httpx.Response, *, default: str = '', limit: int = 400) -> str:
        """Preserve upstream HTTP error body (JSON detail/error/message or raw text)."""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                for key in ('detail', 'error', 'message'):
                    val = payload.get(key)
                    if val is not None and str(val).strip():
                        return str(val).strip()[:limit]
                return str(payload)[:limit]
        except Exception:
            pass
        raw_txt = (response.text or '').strip()
        return raw_txt[:limit] if raw_txt else default

    async def status(self,force=False):
        if not force and self.cached and time.monotonic()-self.cached_at<2.0:
            return dict(self.cached)
        async with self.lock:
            if not force and self.cached and time.monotonic()-self.cached_at<2.0:
                return dict(self.cached)
            try:
                result=await self.call('GET','/status');result['configured']=True
            except ComputerUnavailable as error:
                result={'configured':self.configured,'available':False,'error':str(error)}
            self.cached=result;self.cached_at=time.monotonic();return dict(result)

    async def get_file(self,path,target:Path):
        offset=0;temporary=target.with_suffix(target.suffix+'.partial')
        target.parent.mkdir(parents=True,exist_ok=True)
        try:
            import base64
            with temporary.open('wb') as output:
                while True:
                    packet=await self.call('GET','/file',query={'path':path,'offset':offset,'size':262144})
                    if packet['size']>50_000_000 or packet['offset']!=offset:raise ValueError('云电脑文件传输参数无效')
                    data=base64.b64decode(packet['data'],validate=True)
                    output.write(data)
                    if packet['next_offset']!=offset+len(data):raise ValueError('云电脑文件分块不连续')
                    offset+=len(data)
                    if packet['eof']:
                        if offset!=packet['size']:raise ValueError('云电脑文件大小不完整')
                        break
                    if not data:raise ValueError('云电脑文件传输未完成')
            temporary.replace(target)
            return {'path':path,'bytes':offset}
        finally:
            if temporary.exists():temporary.unlink()

    async def upload_file(self, src_path: Path, remote_name: str | None = None):
        import base64
        p = Path(src_path)
        if not p.is_file():
            raise ValueError(f'待上传文件不存在: {p}')
        raw_bytes = p.read_bytes()
        total_size = len(raw_bytes)
        if total_size > 20_000_000:
            raise ValueError('上传到云电脑的文件不能超过 20 MB')
        fname = re.sub(r'^[0-9a-f]{10}-', '', Path(remote_name or p.name).name) or p.name
        start_res = await self.call('POST', '/library/uploads', {'name': fname, 'size': total_size, 'sha256': ''})
        key = start_res.get('id') or start_res.get('key')
        if not key:
            raise ValueError('云电脑未返回上传句柄')
        offset = 0
        chunk_sz = 262_144
        try:
            while offset < total_size:
                piece = raw_bytes[offset:offset + chunk_sz]
                b64_data = base64.b64encode(piece).decode('ascii')
                await self.call('PUT', f'/library/uploads/{key}', {'offset': offset, 'data': b64_data})
                offset += len(piece)
            fin = await self.call('POST', f'/library/uploads/{key}/finish')
            if isinstance(fin, dict) and fin.get('bytes') is not None and int(fin['bytes']) != total_size:
                raise ValueError('云电脑上传字节大小不一致')
            return fin
        except Exception:
            try:
                await self.call('DELETE', f'/library/uploads/{key}')
            except Exception:
                pass
            raise
