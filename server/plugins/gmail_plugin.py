"""Gmail & Email Connector Plugin (`server/plugins/gmail_plugin.py`).

Registered automatically via `ExtensionRegistry`.
Supports:
- Google Gmail OAuth2 (`access_token` / `refresh_token`) AND standard IMAP/SMTP (`imap.gmail.com:993`)
- Searching billing notifications (e.g., Bybit, OpenAI, Stripe), unread emails, and extracting OTP verification codes
- Direct Gmail web links (`https://mail.google.com/mail/u/0/#all/<id>`)
"""
from __future__ import annotations

import asyncio
import base64
import email
import imaplib
import json
import os
import re
import smtplib
import threading
import time
from email.header import decode_header
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlencode

import httpx


class GmailToolPlugin:
    name = 'gmail_connector'
    description = (
        'Gmail 与通用邮箱插件工具（action: status|configure|search|read|latest_codes|send；'
        '支持 Gmail OAuth2 Token 或 IMAP 授权，可自动搜索账单通知如 Bybit/OpenAI、提取最新验证码、阅读或发送邮件）'
    )
    parameters = {
        'type': 'object',
        'properties': {
            'action': {'type': 'string'},
            'query': {'type': 'string'},
            'message_id': {'type': 'string'},
            'to': {'type': 'string'},
            'subject': {'type': 'string'},
            'body': {'type': 'string'},
            'email_address': {'type': 'string'},
            'app_password': {'type': 'string'},
            'oauth_token': {'type': 'string'},
            'refresh_token': {'type': 'string'},
            'client_id': {'type': 'string'},
            'client_secret': {'type': 'string'},
            'imap_host': {'type': 'string'},
            'limit': {'type': 'integer'},
        },
        'required': ['action'],
        'additionalProperties': False,
    }

    @staticmethod
    def _config_path(root: Path) -> Path:
        cfg_dir = root / '.config'
        cfg_dir.mkdir(parents=True, exist_ok=True)
        return cfg_dir / 'gmail_auth.json'

    @classmethod
    def _load_cfg(cls, root: Path, data_dir: Path | None = None) -> dict:
        p = cls._config_path(root)
        cfg: dict = {}
        if p.exists():
            try:
                cfg = json.loads(p.read_text('utf-8'))
            except Exception:
                cfg = {}
        # Fallback to integration_hub gmail config for IMAP when gmail_auth lacks password
        if not (cfg.get('email_address') and cfg.get('app_password')):
            hub_candidates = [root / '.config' / 'integrations.json']
            if data_dir is not None:
                hub_candidates.insert(0, Path(data_dir) / 'integrations.json')
            for hp in hub_candidates:
                if hp.is_file():
                    try:
                        hub_all = json.loads(hp.read_text('utf-8'))
                        em = hub_all.get('email') or hub_all.get('gmail') or {}
                        addr = str(em.get('address') or '')
                        pw = str(em.get('password') or '')
                        prov = str(em.get('provider') or '').lower()
                        if addr and pw and (prov == 'gmail' or '@gmail.' in addr.lower()):
                            cfg.setdefault('email_address', addr)
                            cfg.setdefault('app_password', pw)
                            break
                    except Exception:
                        continue
        return cfg

    @staticmethod
    def _extract_gmail_payload_body(payload: dict | None, *, max_chars: int = 8000) -> str:
        """Recursively extract and decode base64url text/plain or text/html body from Gmail message payload."""
        if not isinstance(payload, dict):
            return ''
        plain_chunks: list[str] = []
        html_chunks: list[str] = []

        def _walk(node: dict) -> None:
            mime = str(node.get('mimeType') or '').lower()
            body_data = (node.get('body') or {}).get('data')
            if isinstance(body_data, str) and body_data:
                padded = body_data + '=' * (-len(body_data) % 4)
                try:
                    raw = base64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8', errors='replace')
                except Exception:
                    raw = ''
                if raw:
                    if mime == 'text/html':
                        cleaned = re.sub(r'<style[\s\S]*?</style>|<script[\s\S]*?</script>', ' ', raw, flags=re.I)
                        cleaned = re.sub(r'<[^>]+>', ' ', cleaned)
                        html_chunks.append(re.sub(r'[ \t]+', ' ', cleaned).strip())
                    else:
                        plain_chunks.append(raw.strip())
            for part in node.get('parts') or []:
                if isinstance(part, dict):
                    _walk(part)

        _walk(payload)
        body = '\n\n'.join(c for c in plain_chunks if c) or '\n\n'.join(c for c in html_chunks if c)
        return body[:max_chars]

    @staticmethod
    def _build_oauth_reauth_url(cfg: dict) -> str:
        cid = str(cfg.get('client_id') or '').strip()
        if cid:
            qs = urlencode({
                'client_id': cid,
                'redirect_uri': str(cfg.get('redirect_uri') or 'urn:ietf:wg:oauth:2.0:oob'),
                'response_type': 'code',
                'scope': 'https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send',
                'access_type': 'offline',
                'prompt': 'consent',
            })
            return f'https://accounts.google.com/o/oauth2/v2/auth?{qs}'
        return 'https://myaccount.google.com/apppasswords'

    @classmethod
    def _build_reauth_error(
        cls,
        cfg: dict,
        status_code: int,
        raw_text: str,
        refresh_err: str = '',
    ) -> dict:
        auth_url = cls._build_oauth_reauth_url(cfg)
        return {
            'ok': False,
            'needs_reauth': True,
            'status_code': status_code,
            'error': f'Gmail OAuth2 凭证已过期或失效 (HTTP {status_code}): {raw_text[:200]}',
            'refresh_error': refresh_err,
            'auth_url': auth_url,
            'reauth_options': [
                '选项1（推荐长期免过期）：到 https://myaccount.google.com/apppasswords 生成 16 位应用专用密码后调用 gmail_connector(action="configure", email_address="你的邮箱", app_password="16位密码")。',
                '选项2（OAuth2 重授权）：打开 auth_url 完成授权并更新 oauth_token / refresh_token，或调 integration_hub(op="connect_oauth", app="gmail")。',
                '选项3（临时查看）：若急需读取验证码或确认链接，可调用 phone(op="launch", app="com.google.android.gm") 在手机虚拟屏直接打开 Gmail。',
            ],
        }

    _cfg_lock = threading.Lock()
    _oauth_locks: dict[tuple[int, str], asyncio.Lock] = {}

    @classmethod
    def _get_oauth_lock(cls, root: Path) -> asyncio.Lock:
        loop_id = id(asyncio.get_running_loop())
        key = (loop_id, str(root.resolve()))
        lock = cls._oauth_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            cls._oauth_locks[key] = lock
        return lock

    @classmethod
    def _save_cfg(cls, root: Path, data: dict) -> None:
        p = cls._config_path(root)
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        with cls._cfg_lock:
            tmp = p.with_suffix(p.suffix + '.tmp')
            tmp.write_text(payload, encoding='utf-8')
            os.replace(tmp, p)

    @staticmethod
    def _decode_mime_str(val: str | None) -> str:
        if not val:
            return ''
        parts = decode_header(val)
        out = []
        for content, enc in parts:
            if isinstance(content, bytes):
                out.append(content.decode(enc or 'utf-8', errors='replace'))
            else:
                out.append(str(content))
        return ''.join(out)

    async def _refresh_oauth_if_needed(self, root: Path, cfg: dict) -> tuple[str, str]:
        token = str(cfg.get('oauth_token') or '')
        refresh_err = ''
        force_refresh = cfg.get('_force_refresh') is True
        try:
            exp_ts = float(cfg.get('expires_at') or 0)
        except Exception:
            exp_ts = 0.0
        token_fresh = bool(token) and (exp_ts <= 0 or exp_ts > time.time() + 60.0)
        if token_fresh and not force_refresh:
            return token, ''
        if cfg.get('refresh_token') and cfg.get('client_id') and cfg.get('client_secret'):
            async with self._get_oauth_lock(root):
                latest = self._load_cfg(root)
                latest_tok = str(latest.get('oauth_token') or '')
                if latest_tok and latest_tok != token and not force_refresh:
                    cfg['oauth_token'] = latest_tok
                    return latest_tok, ''
                try:
                    async with httpx.AsyncClient(timeout=15) as client:
                        r = await client.post(
                            'https://oauth2.googleapis.com/token',
                            data={
                                'client_id': cfg['client_id'],
                                'client_secret': cfg['client_secret'],
                                'refresh_token': cfg['refresh_token'],
                                'grant_type': 'refresh_token',
                            },
                        )
                    if r.status_code == 200:
                        resp_json = r.json()
                        new_tok = resp_json.get('access_token')
                        if new_tok:
                            merged = dict(latest) if isinstance(latest, dict) else {}
                            merged.update({k: v for k, v in cfg.items() if k != '_force_refresh'})
                            merged['oauth_token'] = new_tok
                            if resp_json.get('expires_in'):
                                try:
                                    merged['expires_at'] = time.time() + float(resp_json['expires_in'])
                                except Exception:
                                    pass
                            if resp_json.get('refresh_token'):
                                merged['refresh_token'] = resp_json['refresh_token']
                            cfg['oauth_token'] = new_tok
                            self._save_cfg(root, merged)
                            return new_tok, ''
                    else:
                        refresh_err = f'HTTP {r.status_code}: {r.text[:160]}'
                except Exception as exc:
                    refresh_err = str(exc)[:160]
        elif cfg.get('oauth_token') and not (cfg.get('refresh_token') and cfg.get('client_id') and cfg.get('client_secret')):
            refresh_err = '缺少 refresh_token/client_id/client_secret，无法自动续期'
        return token, refresh_err

    async def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        root = Path(context.get('root') or context.get('workspace_root') or '.')
        data_dir_raw = context.get('data_dir')
        data_dir = Path(data_dir_raw) if data_dir_raw else None
        action = str(args.get('action') or 'status').strip().lower()
        cfg = self._load_cfg(root, data_dir=data_dir)

        if action == 'status':
            has_oauth = bool(cfg.get('oauth_token') or cfg.get('refresh_token'))
            has_imap = bool(cfg.get('email_address') and cfg.get('app_password'))
            can_auto_refresh = bool(cfg.get('refresh_token') and cfg.get('client_id') and cfg.get('client_secret'))
            return {
                'configured': has_oauth or has_imap,
                'mode': 'gmail_oauth' if has_oauth else ('imap' if has_imap else 'unconfigured'),
                'oauth_can_auto_refresh': can_auto_refresh,
                'has_imap_fallback': has_imap,
                'email_address': cfg.get('email_address', ''),
                'setup_hint': (
                    '可通过 gmail_connector(action="configure", oauth_token="...") 配置 Google OAuth2 授权，'
                    '或传入 email_address="xxx@gmail.com", app_password="16位应用专用密码" 启用 IMAP/SMTP。'
                ),
            }

        if action == 'configure':
            for k in ('email_address', 'app_password', 'oauth_token', 'refresh_token', 'client_id', 'client_secret', 'imap_host'):
                if args.get(k):
                    cfg[k] = str(args[k]).strip()
            self._save_cfg(root, cfg)
            return {'configured': True, 'email_address': cfg.get('email_address', ''), 'mode': 'gmail_oauth' if cfg.get('oauth_token') else 'imap'}

        limit = max(1, min(20, int(args.get('limit') or 8)))
        query = str(args.get('query') or '').strip()

        # Mode A: Gmail Official REST API (OAuth2)
        if cfg.get('oauth_token') or cfg.get('refresh_token'):
            token, last_refresh_err = await self._refresh_oauth_if_needed(root, cfg)
            async with httpx.AsyncClient(timeout=20) as client:
                async def _gmail_req(method: str, url: str, **kw):
                    nonlocal token, last_refresh_err
                    hdrs = {'Authorization': f'Bearer {token}'}
                    resp = await client.request(method, url, headers=hdrs, **kw)
                    if resp.status_code in (401, 403):
                        cfg['_force_refresh'] = True
                        new_tok, ref_err = await self._refresh_oauth_if_needed(root, cfg)
                        cfg.pop('_force_refresh', None)
                        if ref_err:
                            last_refresh_err = ref_err
                        if new_tok and new_tok != token:
                            token = new_tok
                            resp = await client.request(method, url, headers={'Authorization': f'Bearer {token}'}, **kw)
                    return resp

                if action in ('search', 'list', 'latest_codes'):
                    q_str = query or ('verification OR code OR 验证码 OR 账单 OR Bybit OR OpenAI' if action == 'latest_codes' else 'in:inbox')
                    r = await _gmail_req(
                        'GET',
                        'https://gmail.googleapis.com/gmail/v1/users/me/messages',
                        params={'q': q_str, 'maxResults': limit},
                    )
                    if r.status_code in (401, 403):
                        if cfg.get('email_address') and cfg.get('app_password'):
                            pass  # smooth fallback to Mode B IMAP below
                        else:
                            return self._build_reauth_error(cfg, r.status_code, r.text, last_refresh_err)
                    elif r.status_code >= 400:
                        return {'ok': False, 'error': f'Gmail API 返回 HTTP {r.status_code}: {r.text[:240]}'}
                    else:
                        msgs = r.json().get('messages') or []
                        results = []
                        for m_meta in msgs[:limit]:
                            mid = m_meta['id']
                            mr = await _gmail_req(
                                'GET',
                                f'https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}',
                                params={'format': 'full'},
                            )
                            if mr.status_code != 200:
                                continue
                            m_data = mr.json()
                            hdrs = {h['name'].lower(): h['value'] for h in (m_data.get('payload', {}).get('headers') or [])}
                            snippet = m_data.get('snippet', '')
                            codes = re.findall(r'\b\d{4,8}\b', snippet)
                            results.append({
                                'id': mid,
                                'subject': hdrs.get('subject', ''),
                                'from': hdrs.get('from', ''),
                                'date': hdrs.get('date', ''),
                                'snippet': snippet,
                                'extracted_codes': codes[:3],
                                'web_link': f'https://mail.google.com/mail/u/0/#all/{mid}',
                            })
                        return {'ok': True, 'count': len(results), 'query': q_str, 'emails': results}
                if action == 'read':
                    mid = str(args.get('message_id') or query or '').strip()
                    if not mid:
                        return {'ok': False, 'error': 'read 动作需要提供 message_id'}
                    mr = await _gmail_req(
                        'GET',
                        f'https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}',
                        params={'format': 'full'},
                    )
                    if mr.status_code in (401, 403):
                        if cfg.get('email_address') and cfg.get('app_password'):
                            pass
                        else:
                            return self._build_reauth_error(cfg, mr.status_code, mr.text, last_refresh_err)
                    elif mr.status_code >= 400:
                        return {'ok': False, 'error': f'Gmail API 返回 HTTP {mr.status_code}: {mr.text[:240]}'}
                    else:
                        m_data = mr.json()
                        payload = m_data.get('payload', {}) or {}
                        hdrs = {h['name'].lower(): h['value'] for h in (payload.get('headers') or [])}
                        snippet = m_data.get('snippet', '')
                        body_txt = self._extract_gmail_payload_body(payload)
                        return {
                            'ok': True,
                            'id': mid,
                            'subject': hdrs.get('subject', ''),
                            'from': hdrs.get('from', ''),
                            'date': hdrs.get('date', ''),
                            'snippet': snippet,
                            'body': body_txt or snippet,
                            'web_link': f'https://mail.google.com/mail/u/0/#all/{mid}',
                        }
                if action == 'send':
                    to_addr = str(args.get('to') or '').strip()
                    subj = str(args.get('subject') or '').strip()
                    body = str(args.get('body') or '').strip()
                    if not to_addr or not body:
                        return {'ok': False, 'error': '发送邮件需提供 to 和 body'}
                    msg = EmailMessage()
                    msg['To'] = to_addr
                    msg['Subject'] = subj
                    msg.set_content(body)
                    raw_b64 = base64.urlsafe_b64encode(msg.as_bytes()).decode('ascii')
                    sr = await _gmail_req(
                        'POST',
                        'https://gmail.googleapis.com/gmail/v1/users/me/messages/send',
                        json={'raw': raw_b64},
                    )
                    if sr.status_code in (401, 403):
                        if cfg.get('email_address') and cfg.get('app_password'):
                            pass
                        else:
                            return self._build_reauth_error(cfg, sr.status_code, sr.text, last_refresh_err)
                    elif sr.status_code >= 400:
                        return {'ok': False, 'error': f'Gmail 发送失败 HTTP {sr.status_code}: {sr.text[:240]}'}
                    else:
                        return {'ok': True, 'sent': True, 'to': to_addr, 'subject': subj, 'id': sr.json().get('id', '')}

        # Mode B: Standard IMAP / SMTP
        if cfg.get('email_address') and cfg.get('app_password'):
            imap_host = str(cfg.get('imap_host') or 'imap.gmail.com')
            email_addr = str(cfg['email_address'])
            app_pw = str(cfg['app_password'])

            if action == 'send':
                to_addr = str(args.get('to') or '').strip()
                subj = str(args.get('subject') or '').strip()
                body = str(args.get('body') or '').strip()
                if not to_addr or not body:
                    return {'error': '发送邮件需提供 to 和 body'}

                def _smtp_send():
                    msg = EmailMessage()
                    msg['Subject'] = subj
                    msg['From'] = email_addr
                    msg['To'] = to_addr
                    msg.set_content(body)
                    smtp_host = str(cfg.get('smtp_host') or '').strip() or (
                        re.sub(r'^imap\.', 'smtp.', imap_host, flags=re.I)
                        if imap_host.lower().startswith('imap.')
                        else imap_host
                    )
                    with smtplib.SMTP_SSL(smtp_host, 465, timeout=15) as s:
                        s.login(email_addr, app_pw)
                        s.send_message(msg)
                    return {'sent': True, 'to': to_addr, 'subject': subj}

                return await asyncio.to_thread(_smtp_send)

            def _imap_fetch():
                with imaplib.IMAP4_SSL(imap_host, 993) as M:
                    M.login(email_addr, app_pw)
                    M.select('INBOX', readonly=True)
                    target_mid = str(args.get('message_id') or '').strip()
                    if action == 'read' and target_mid:
                        ids = [target_mid.encode('ascii', errors='ignore')]
                    else:
                        typ, data = M.search(None, 'ALL')
                        scan_cap = max(limit, min(60, limit * 5)) if query else limit
                        ids = (data[0] or b'').split()[-scan_cap:]
                    items = []
                    for num in reversed(ids):
                        _, msg_data = M.fetch(num, '(RFC822)')
                        if not msg_data or not isinstance(msg_data[0], tuple):
                            continue
                        em = email.message_from_bytes(msg_data[0][1])
                        subj = self._decode_mime_str(em.get('Subject'))
                        sender = self._decode_mime_str(em.get('From'))
                        date_s = str(em.get('Date') or '')
                        body_txt = ''
                        if em.is_multipart():
                            for part in em.walk():
                                if part.get_content_type() == 'text/plain':
                                    payload = part.get_payload(decode=True)
                                    if payload:
                                        body_txt = payload.decode(part.get_content_charset() or 'utf-8', errors='replace')
                                        break
                        else:
                            payload = em.get_payload(decode=True)
                            if payload:
                                body_txt = payload.decode(em.get_content_charset() or 'utf-8', errors='replace')
                        hay = f'{subj}\n{sender}\n{body_txt}'
                        if query and action != 'read' and query.lower() not in hay.lower():
                            continue
                        codes = re.findall(r'\b\d{4,8}\b', f'{subj} {body_txt[:600]}')
                        items.append({
                            'id': num.decode('ascii', errors='ignore'),
                            'subject': subj,
                            'from': sender,
                            'date': date_s,
                            'snippet': body_txt.strip()[:2000 if action == 'read' else 400],
                            'extracted_codes': codes[:3],
                        })
                        if len(items) >= limit:
                            break
                    return {'ok': True, 'count': len(items), 'query': query, 'emails': items}

            return await asyncio.to_thread(_imap_fetch)

        return {
            'ok': False,
            'configured': False,
            'error': '邮箱插件尚未配置授权凭证。请先调用 gmail_connector(action="configure", ...) 配置 Google OAuth2 Token 或 IMAP 应用专用密码。',
        }


def register(registry) -> None:
    registry.register_tool(GmailToolPlugin())
