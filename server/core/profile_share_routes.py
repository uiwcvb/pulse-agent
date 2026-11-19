"""FastAPI routes for external AI profile sharing, search, and zero-context bootstrap prompts."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Header, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from core.external_profile_db import ExternalProfileDB


class ExternalCardInput(BaseModel):
    card_id: str | None = None
    scope: str = 'general'
    category: str = '自定义'
    title: str
    summary: str
    keywords: str = ''
    priority: float = 0.95
    enabled: bool = True


class ExternalPresetInput(BaseModel):
    scene: str = 'general'
    title: str
    system_prompt: str


class ExternalTokenInput(BaseModel):
    label: str = '外部AI通用只读令牌'
    scopes: list[str] | None = None
    revoke_previous: bool = False
    scene: str = 'general'
    public_base_url: str | None = None


def _extract_share_token(authorization: str | None, query_token: str | None) -> str:
    if query_token and query_token.strip():
        return query_token.strip()
    if authorization:
        raw = authorization.strip()
        if raw.lower().startswith('bearer '):
            return raw[7:].strip()
        return raw
    return ''


def _resolve_public_base(request: Request, explicit_base: str | None = None) -> str:
    if explicit_base and explicit_base.strip():
        return explicit_base.strip().rstrip('/')
    root_path = str(request.scope.get('root_path') or '').rstrip('/')
    host = (request.headers.get('x-forwarded-host') or request.headers.get('host') or '').strip()
    proto = (request.headers.get('x-forwarded-proto') or request.url.scheme or 'http').split(',')[0].strip()
    if host:
        return f'{proto}://{host}{root_path}'
    return str(request.base_url).rstrip('/') + root_path


def register_profile_share_routes(app: FastAPI, auth: Any, store: Any, vault: Any) -> ExternalProfileDB:
    """Attach isolated external-AI profile DB management and read-only share routes."""
    db_path = Path(getattr(store, 'root', None) or Path(store.db_path).parent) / 'external_ai_profile.db'
    profile_db = ExternalProfileDB(db_path)
    profile_db.ensure_synced_from_vault(vault)

    def _require_share_token(
        authorization: str | None = Header(default=None),
        token: str | None = Query(default=None),
    ) -> dict[str, Any]:
        raw = _extract_share_token(authorization, token)
        verified = profile_db.verify_token(raw)
        if verified is None:
            raise HTTPException(401, '无效或已停用的外部只读分享令牌')
        return verified

    @app.get('/api/share/profile')
    async def share_profile(
        request: Request,
        scene: str = Query(default='general'),
        format: str = Query(default='md'),
        q: str = Query(default=''),
        token: str | None = Query(default=None),
        tok: dict[str, Any] = Depends(_require_share_token),
    ):
        profile_db.ensure_synced_from_vault(vault)
        scopes = tok.get('scopes') or ['general', 'coding']
        fmt = (format or 'md').strip().lower()
        if fmt == 'json':
            return profile_db.render_external_payload(scene=scene, q=q, allowed_scopes=scopes)
        base = _resolve_public_base(request)
        raw_tok = _extract_share_token(request.headers.get('authorization'), token)
        share_url = f'{base}/api/share/profile?token={raw_tok}&format=md' if raw_tok else ''
        search_url = f'{base}/api/share/search?token={raw_tok}' if raw_tok else ''
        md = profile_db.render_markdown_for_ai(
            scene=scene, q=q, allowed_scopes=scopes, share_url=share_url, search_url=search_url
        )
        return PlainTextResponse(md, media_type='text/markdown; charset=utf-8')

    @app.get('/api/share/search')
    async def share_search(
        q: str = Query(default=''),
        format: str = Query(default='md'),
        tok: dict[str, Any] = Depends(_require_share_token),
    ):
        profile_db.ensure_synced_from_vault(vault)
        scopes = tok.get('scopes') or ['general', 'coding']
        if (format or 'md').strip().lower() == 'json':
            return profile_db.render_external_payload(scene='all', q=q, allowed_scopes=scopes)
        md = profile_db.render_markdown_for_ai(scene='all', q=q, allowed_scopes=scopes)
        return PlainTextResponse(md, media_type='text/markdown; charset=utf-8')

    @app.get('/api/profile/external', dependencies=[Depends(auth)])
    async def admin_get_external_profile():
        profile_db.ensure_synced_from_vault(vault)
        return {
            'ok': True,
            'db_path': str(db_path),
            'cards': profile_db.list_cards(include_disabled=True, limit=100),
            'presets': profile_db.list_presets(),
            'tokens': profile_db.list_tokens(),
        }

    @app.post('/api/profile/external/sync', dependencies=[Depends(auth)])
    async def admin_sync_external_profile():
        synced = profile_db.sync_from_vault(vault)
        return {'ok': True, 'synced_cards': synced}

    @app.put('/api/profile/external/card', dependencies=[Depends(auth)])
    async def admin_upsert_card(data: ExternalCardInput):
        card = profile_db.upsert_card(
            card_id=data.card_id,
            scope=data.scope,
            category=data.category,
            title=data.title,
            summary=data.summary,
            keywords=data.keywords,
            priority=data.priority,
            enabled=data.enabled,
        )
        return {'ok': True, 'card': card}

    @app.put('/api/profile/external/preset', dependencies=[Depends(auth)])
    async def admin_upsert_preset(data: ExternalPresetInput):
        preset = profile_db.upsert_preset(data.scene, data.title, data.system_prompt)
        return {'ok': True, 'preset': preset}

    @app.post('/api/profile/external/token', dependencies=[Depends(auth)])
    async def admin_create_share_token(request: Request, data: ExternalTokenInput):
        profile_db.ensure_synced_from_vault(vault)
        created = profile_db.create_or_rotate_token(
            label=data.label,
            scopes=data.scopes,
            revoke_previous=data.revoke_previous,
        )
        base = _resolve_public_base(request, data.public_base_url)
        prompt = profile_db.build_zero_context_bootstrap_prompt(
            base_url=base,
            raw_token=created['token'],
            scene=data.scene,
            inline_fallback=True,
        )
        return {
            'ok': True,
            **created,
            'share_url': f"{base}/api/share/profile?token={created['token']}&scene={data.scene}&format=md",
            'search_url': f"{base}/api/share/search?token={created['token']}",
            'bootstrap_prompt': prompt,
        }

    @app.delete('/api/profile/external/token/{token_id}', dependencies=[Depends(auth)])
    async def admin_revoke_share_token(token_id: str):
        return {'ok': profile_db.revoke_token(token_id)}

    return profile_db
