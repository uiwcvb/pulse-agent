"""Multi-provider API channel aggregation, concurrent model discovery, and CRUD routes."""
from __future__ import annotations

import asyncio
import re
import secrets
import time
from typing import Any

import httpx
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

BUILTIN_FALLBACK_MODELS: tuple[str, ...] = (
    'commandcode/deepseek/deepseek-v4.1-flash',
    'commandcode/deepseek/deepseek-v4-flash',
    'commandcode/deepseek/deepseek-v4-pro',
    'deepseek-v4.1-flash-eco',
)


class ChannelUpsertInput(BaseModel):
    id: str = Field('', max_length=64)
    name: str = Field('', max_length=80)
    base_url: str = Field('', max_length=500)
    api_key: str = Field('', max_length=4096)
    enabled: bool = True
    priority: int = Field(100, ge=0, le=1000)
    models: list[str] | None = None
    auto_fetch: bool = False


def _clean_error_text(raw: str, limit: int = 180) -> str:
    s = re.sub(r'<[^>]+>', ' ', str(raw or ''))
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:limit]


async def fetch_channel_models_once(
    base_url: str,
    api_key: str,
    timeout: float = 12.0,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch `/models` from a single OpenAI-compatible provider endpoint."""
    url = str(base_url or '').strip().rstrip('/')
    key = str(api_key or '').strip()
    if not url or not key:
        return {'ok': False, 'models': [], 'latency_ms': 0, 'status_code': 0, 'error': '缺少 Base URL 或 API Key'}

    t0 = time.perf_counter()
    owns_client = client is None
    hc = client or httpx.AsyncClient(timeout=timeout, trust_env=False)
    try:
        r = await hc.get(url + '/models', headers={'Authorization': 'Bearer ' + key}, timeout=timeout)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        if r.status_code in (401, 403):
            detail = _clean_error_text(getattr(r, 'text', ''))
            return {
                'ok': False,
                'models': [],
                'latency_ms': latency_ms,
                'status_code': int(r.status_code),
                'error': f'HTTP {r.status_code}: {detail or "API Key 无效或无权限"}',
            }
        if r.status_code >= 400:
            detail = _clean_error_text(getattr(r, 'text', ''))
            return {
                'ok': False,
                'models': [],
                'latency_ms': latency_ms,
                'status_code': int(r.status_code),
                'error': f'HTTP {r.status_code}: {detail or "上游返回错误"}',
            }
        body = r.json()
        raw_list = body.get('data') if isinstance(body, dict) else (body if isinstance(body, list) else [])
        models: list[str] = []
        for item in (raw_list or []):
            mid = str(item['id']).strip() if isinstance(item, dict) and item.get('id') else str(item or '').strip()
            if mid and mid not in models:
                models.append(mid)
        return {
            'ok': True,
            'models': models,
            'latency_ms': latency_ms,
            'status_code': int(r.status_code),
            'error': '',
        }
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return {
            'ok': False,
            'models': [],
            'latency_ms': latency_ms,
            'status_code': 0,
            'error': _clean_error_text(str(exc)) or type(exc).__name__,
        }
    finally:
        if owns_client:
            await hc.aclose()


async def fetch_aggregated_models(
    store: Any,
    override_base_url: str = '',
    override_api_key: str = '',
    persist_channel_models: bool = True,
    raise_on_auth_error: bool = False,
) -> dict[str, Any]:
    """Concurrently aggregate `/models` across all enabled channels + legacy primary & fallback."""
    cfg = store.config
    targets: list[dict[str, Any]] = []
    seen_endpoints: set[tuple[str, str]] = set()

    def _add_target(tid: str, name: str, url: str, key: str, is_channel: bool, priority: int):
        u = str(url or '').strip().rstrip('/')
        k = str(key or '').strip()
        if not (u and k):
            return
        sig = (u, k[-12:])
        if not is_channel and sig in seen_endpoints:
            return
        seen_endpoints.add(sig)
        targets.append({
            'id': tid,
            'name': name,
            'base_url': u,
            'api_key': k,
            'is_channel': is_channel,
            'priority': priority,
        })

    # 1. Enabled multi-provider channels (sorted by priority)
    enabled_channels = sorted(
        [ch for ch in (getattr(cfg, 'channels', None) or []) if ch.enabled and (ch.base_url or '').strip() and (ch.api_key or '').strip()],
        key=lambda c: (int(c.priority), c.id),
    )
    for ch in enabled_channels:
        _add_target(ch.id, ch.name or ch.id, ch.base_url, ch.api_key, True, int(ch.priority))

    # 2. Legacy primary endpoint (or explicit override from UI form)
    ov_url = str(override_base_url or '').strip()
    ov_key = str(override_api_key or '').strip()
    legacy_url = ov_url or (cfg._legacy_base_url() if hasattr(cfg, '_legacy_base_url') else cfg.effective_base_url())
    legacy_key = ov_key or (
        (cfg._legacy_api_key(big=True) or cfg._legacy_api_key(big=False))
        if hasattr(cfg, '_legacy_api_key')
        else (cfg.effective_api_key(big=True) or cfg.effective_api_key(big=False))
    )
    if legacy_url and legacy_key:
        _add_target('primary', '主接口', legacy_url, legacy_key, False, 500)

    # 3. Legacy fallback endpoint
    fb_url, fb_key, fb_model = cfg.effective_fallback(big=True) if hasattr(cfg, 'effective_fallback') else ('', '', '')
    if fb_url and fb_key:
        _add_target('fallback', '备用接口', fb_url, fb_key, False, 900)

    async with httpx.AsyncClient(timeout=12.0, trust_env=False) as client:
        results = await asyncio.gather(
            *[fetch_channel_models_once(t['base_url'], t['api_key'], timeout=12.0, client=client) for t in targets],
            return_exceptions=False,
        )

    channel_models_diag: list[dict[str, Any]] = []
    model_owners: dict[str, list[str]] = {}
    channel_updates: dict[str, list[str]] = {}
    first_auth_err: str | None = None

    for tgt, res in zip(targets, results):
        m_list = list(res.get('models') or [])
        if not res.get('ok') and res.get('status_code') in (401, 403) and first_auth_err is None:
            first_auth_err = str(res.get('error') or 'HTTP 401')
        if tgt['is_channel'] and res.get('ok') and m_list:
            channel_updates[tgt['id']] = m_list
        channel_models_diag.append({
            'id': tgt['id'],
            'name': tgt['name'],
            'base_url': tgt['base_url'],
            'is_channel': tgt['is_channel'],
            'ok': bool(res.get('ok')),
            'latency_ms': int(res.get('latency_ms') or 0),
            'count': len(m_list),
            'models': m_list,
            'error': str(res.get('error') or ''),
        })
        for mid in m_list:
            owners = model_owners.setdefault(mid, [])
            if tgt['id'] not in owners:
                owners.append(tgt['id'])

    # Persist freshly fetched channel model lists so automatic model->channel routing works immediately
    if persist_channel_models and channel_updates and getattr(cfg, 'channels', None):
        raw_channels = [ch.model_dump() for ch in cfg.channels]
        dirty = False
        for rc in raw_channels:
            cid = str(rc.get('id') or '')
            if cid in channel_updates and rc.get('models') != channel_updates[cid]:
                rc['models'] = channel_updates[cid]
                dirty = True
        if dirty:
            try:
                store.update_config({'channels': raw_channels}, source_tag='channel_models_refresh')
            except Exception:
                pass

    # Build aggregated model list + structured model_entries with explicit channel attribution
    aggregated: list[str] = []
    model_entries: list[dict[str, Any]] = []
    seen_entry_values: set[str] = set()

    for diag in channel_models_diag:
        ch_id = str(diag['id'] or '')
        ch_name = str(diag['name'] or ch_id or '主接口')
        is_ch = bool(diag['is_channel'])
        for mid in diag['models']:
            if mid not in aggregated:
                aggregated.append(mid)
            multi_owner = len(model_owners.get(mid, [])) > 1
            if is_ch:
                prefixed = f'ch:{ch_id}/{mid}'
                if multi_owner and prefixed not in aggregated:
                    aggregated.append(prefixed)
                entry_val = prefixed if multi_owner else mid
            else:
                entry_val = mid
            if entry_val not in seen_entry_values:
                seen_entry_values.add(entry_val)
                model_entries.append({
                    'value': entry_val,
                    'model': mid,
                    'channel_id': ch_id,
                    'channel_name': ch_name,
                    'is_channel': is_ch,
                })

    if raise_on_auth_error and first_auth_err and not aggregated:
        raise ValueError(first_auth_err)

    for builtin_m in (fb_model, *BUILTIN_FALLBACK_MODELS):
        if builtin_m and builtin_m not in aggregated:
            aggregated.append(builtin_m)
        if builtin_m and builtin_m not in seen_entry_values:
            seen_entry_values.add(builtin_m)
            model_entries.append({
                'value': builtin_m,
                'model': builtin_m,
                'channel_id': 'fallback' if builtin_m == fb_model and fb_model else 'builtin',
                'channel_name': '备用接口' if builtin_m == fb_model and fb_model else '内置预设',
                'is_channel': False,
            })

    return {
        'models': aggregated,
        'model_entries': model_entries,
        'base_url': (legacy_url or store.config.effective_base_url()).rstrip('/'),
        'channel_models': channel_models_diag,
        'channels': store.config.public().get('channels', []),
    }


def upsert_channel_in_store(store: Any, data: dict[str, Any], source_tag: str = 'api_channel') -> tuple[dict[str, Any], dict[str, Any]]:
    """Create or update a provider channel in Store, preserving api_key when blank."""
    existing = [ch.model_dump() for ch in (getattr(store.config, 'channels', None) or [])]
    cid = re.sub(r'[^a-zA-Z0-9_-]', '', str(data.get('id') or '').strip())[:64]
    if not cid:
        slug = re.sub(r'[^a-zA-Z0-9_-]', '', str(data.get('name') or '').strip().lower().replace(' ', '_'))[:24]
        cid = f'{slug}_{secrets.token_hex(2)}' if slug else f'ch_{secrets.token_hex(3)}'

    found_idx = next((i for i, c in enumerate(existing) if str(c.get('id') or '') == cid), -1)
    old_ch = existing[found_idx] if found_idx >= 0 else {}
    new_ch = {
        'id': cid,
        'name': str(data.get('name') or '').strip() or str(old_ch.get('name') or cid),
        'base_url': str(data.get('base_url') if data.get('base_url') is not None else old_ch.get('base_url', '')).strip(),
        'api_key': str(data.get('api_key') or '').strip() or str(old_ch.get('api_key') or ''),
        'enabled': bool(data.get('enabled', old_ch.get('enabled', True))),
        'priority': int(data.get('priority', old_ch.get('priority', 100))),
        'models': list(data['models']) if isinstance(data.get('models'), list) else list(old_ch.get('models') or []),
    }
    if not new_ch['base_url']:
        raise ValueError('渠道 base_url 不能为空')
    if found_idx >= 0:
        existing[found_idx] = new_ch
    else:
        existing.append(new_ch)

    pub_cfg = store.update_config({'channels': existing}, source_tag=source_tag)
    pub_ch = next((c for c in pub_cfg.get('channels', []) if c.get('id') == cid), {})
    return pub_ch, pub_cfg


def delete_channel_in_store(store: Any, channel_id: str, source_tag: str = 'api_channel') -> tuple[bool, dict[str, Any]]:
    cid = str(channel_id or '').strip()
    existing = [ch.model_dump() for ch in (getattr(store.config, 'channels', None) or [])]
    kept = [c for c in existing if str(c.get('id') or '') != cid]
    if len(kept) == len(existing):
        return False, store.config.public()
    pub_cfg = store.update_config({'channels': kept}, source_tag=source_tag)
    return True, pub_cfg


def install_channel_routes(app: Any, auth: Any, store: Any, brain: Any, sync_engine: Any, publish: Any) -> None:
    """Register `/api/channels` CRUD and live test endpoints."""

    @app.get('/api/channels', dependencies=[Depends(auth)])
    async def list_channels():
        pub = store.config.public()
        return {
            'channels': pub.get('channels', []),
            'default_model': pub.get('default_model', ''),
            'api_base_url': pub.get('api_base_url', ''),
        }

    @app.post('/api/channels', dependencies=[Depends(auth)])
    async def upsert_channel(data: ChannelUpsertInput):
        try:
            pub_ch, pub_cfg = upsert_channel_in_store(store, data.model_dump(exclude_none=True), source_tag='api_channel')
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

        test_res: dict[str, Any] | None = None
        if data.auto_fetch and pub_ch.get('id'):
            raw_ch = next((c for c in store.config.channels if c.id == pub_ch['id']), None)
            if raw_ch and raw_ch.base_url and raw_ch.api_key:
                test_res = await fetch_channel_models_once(raw_ch.base_url, raw_ch.api_key)
                if test_res.get('ok') and test_res.get('models'):
                    pub_ch, pub_cfg = upsert_channel_in_store(
                        store,
                        {'id': raw_ch.id, 'models': test_res['models']},
                        source_tag='api_channel_autofetch',
                    )

        brain.activity = brain.ready_state()
        await publish({'type': 'config_updated', 'activity': brain.activity, 'channels': pub_cfg.get('channels', [])})
        return {
            'saved': True,
            'channel': pub_ch,
            'channels': pub_cfg.get('channels', []),
            'test': test_res,
        }

    @app.delete('/api/channels/{channel_id}', dependencies=[Depends(auth)])
    async def remove_channel(channel_id: str):
        deleted, pub_cfg = delete_channel_in_store(store, channel_id, source_tag='api_channel')
        if not deleted:
            raise HTTPException(404, f'未找到渠道: {channel_id}')
        brain.activity = brain.ready_state()
        await publish({'type': 'config_updated', 'activity': brain.activity, 'channels': pub_cfg.get('channels', [])})
        return {'deleted': True, 'id': channel_id, 'channels': pub_cfg.get('channels', [])}

    @app.post('/api/channels/{channel_id}/test', dependencies=[Depends(auth)])
    async def test_channel(channel_id: str):
        raw_ch = next((c for c in (store.config.channels or []) if c.id == channel_id), None)
        if raw_ch is None:
            raise HTTPException(404, f'未找到渠道: {channel_id}')
        res = await fetch_channel_models_once(raw_ch.base_url, raw_ch.api_key, timeout=15.0)
        pub_cfg = store.config.public()
        if res.get('ok') and res.get('models'):
            _, pub_cfg = upsert_channel_in_store(
                store,
                {'id': raw_ch.id, 'models': res['models']},
                source_tag='api_channel_test',
            )
            await publish({'type': 'config_updated', 'channels': pub_cfg.get('channels', [])})
        return {
            'id': raw_ch.id,
            'name': raw_ch.name or raw_ch.id,
            'ok': bool(res.get('ok')),
            'latency_ms': int(res.get('latency_ms') or 0),
            'count': len(res.get('models') or []),
            'models': list(res.get('models') or []),
            'error': str(res.get('error') or ''),
            'channels': pub_cfg.get('channels', []),
        }
