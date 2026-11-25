from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import storage
from core import channel_router
from core.onboarding_service import handle_system_config_tool
from main import create_app


def _make_store(tmp_path: Path) -> storage.Store:
    return storage.Store(tmp_path)


def test_config_resolve_route_explicit_priority_and_legacy_fallback(tmp_path: Path):
    store = _make_store(tmp_path)
    store.update_config({
        'api_base_url': 'https://legacy.example.com/v1',
        'api_key': 'sk-legacy-11112222',
        'default_model': 'legacy-default',
        'channels': [
            {
                'id': 'deepseek_official',
                'name': 'DeepSeek 官方',
                'base_url': 'https://api.deepseek.com/v1',
                'api_key': 'sk-ds-aaaa1111',
                'enabled': True,
                'priority': 20,
                'models': ['deepseek-chat', 'shared-model'],
            },
            {
                'id': 'openrouter',
                'name': 'OpenRouter',
                'base_url': 'https://openrouter.ai/api/v1',
                'api_key': 'sk-or-bbbb2222',
                'enabled': True,
                'priority': 10,
                'models': ['claude-3.7-sonnet', 'shared-model'],
            },
            {
                'id': 'disabled_ch',
                'name': '已停用渠道',
                'base_url': 'https://disabled.example.com/v1',
                'api_key': 'sk-dis-cccc3333',
                'enabled': False,
                'priority': 1,
                'models': ['shared-model', 'only-disabled'],
            },
        ],
    })

    cfg = store.config
    # 1. Explicit ch:<id>/<model> prefix strips prefix and routes directly
    url, key, model, cid = cfg.resolve_route(bot_model='ch:deepseek_official/shared-model')
    assert (url, key, model, cid) == ('https://api.deepseek.com/v1', 'sk-ds-aaaa1111', 'shared-model', 'deepseek_official')

    # 2. Explicit <id>::<model> syntax also works
    url, key, model, cid = cfg.resolve_route(bot_model='openrouter::claude-3.7-sonnet')
    assert (url, key, model, cid) == ('https://openrouter.ai/api/v1', 'sk-or-bbbb2222', 'claude-3.7-sonnet', 'openrouter')

    # 3. Bare model name picks highest-priority enabled channel (openrouter prio=10 over deepseek prio=20, skipping disabled prio=1)
    url, key, model, cid = cfg.resolve_route(bot_model='shared-model')
    assert (url, key, model, cid) == ('https://openrouter.ai/api/v1', 'sk-or-bbbb2222', 'shared-model', 'openrouter')

    # 4. Same-model automatic failover chain includes both enabled channels in priority order
    chain = cfg.effective_fallback_chain(bot_model='shared-model')
    assert [(c[0], c[2], c[3]) for c in chain] == [
        ('https://openrouter.ai/api/v1', 'shared-model', 'openrouter'),
        ('https://api.deepseek.com/v1', 'shared-model', 'deepseek_official'),
    ]

    # 5. Model not in any channel falls back to legacy primary endpoint
    url, key, model, cid = cfg.resolve_route(bot_model='legacy-only-model')
    assert (url, key, model, cid) == ('https://legacy.example.com/v1', 'sk-legacy-11112222', 'legacy-only-model', '')

    # 6. Public config masks channel API keys and never leaks plaintext secrets
    pub = cfg.public()
    assert 'api_key' not in pub
    assert len(pub['channels']) == 3
    for ch in pub['channels']:
        assert 'api_key' not in ch
        assert ch['has_api_key'] is True
        assert '...' in ch['api_key_masked']

    # 7. Partial update with blank api_key preserves stored secret
    store.update_config({
        'channels': [
            {
                'id': 'deepseek_official',
                'name': 'DeepSeek 官方(改名)',
                'base_url': 'https://api.deepseek.com/v1',
                'api_key': '',
                'enabled': True,
                'priority': 15,
                'models': ['deepseek-chat'],
            }
        ]
    })
    assert store.config.channels[0].api_key == 'sk-ds-aaaa1111'
    assert store.config.channels[0].name == 'DeepSeek 官方(改名)'


def test_channels_only_config_without_legacy_key(tmp_path: Path):
    store = _make_store(tmp_path)
    store.update_config({
        'channels': [
            {
                'id': 'siliconflow',
                'name': 'SiliconFlow',
                'base_url': 'https://api.siliconflow.cn/v1',
                'api_key': 'sk-sf-99998888',
                'enabled': True,
                'priority': 5,
                'models': ['deepseek-ai/DeepSeek-V3'],
            }
        ]
    })
    assert store.config.effective_api_key() == 'sk-sf-99998888'
    assert store.config.effective_base_url() == 'https://api.siliconflow.cn/v1'
    assert store.config.effective_model() == 'deepseek-ai/DeepSeek-V3'
    assert store.config.public()['has_api_key'] is True


@pytest.mark.asyncio
async def test_concurrent_models_aggregation_partial_failure_and_disambiguation(tmp_path: Path, monkeypatch):
    store = _make_store(tmp_path)
    store.update_config({
        'channels': [
            {
                'id': 'ch_fast',
                'name': 'Fast Provider',
                'base_url': 'https://fast.example.com/v1',
                'api_key': 'sk-fast-12345678',
                'enabled': True,
                'priority': 10,
            },
            {
                'id': 'ch_backup',
                'name': 'Backup Provider',
                'base_url': 'https://backup.example.com/v1',
                'api_key': 'sk-back-12345678',
                'enabled': True,
                'priority': 20,
            },
            {
                'id': 'ch_broken',
                'name': 'Broken Provider',
                'base_url': 'https://broken.example.com/v1',
                'api_key': 'sk-fail-12345678',
                'enabled': True,
                'priority': 30,
            },
        ]
    })

    async def fake_fetch_once(base_url: str, api_key: str, timeout: float = 12.0, client=None):
        if 'fast.example.com' in base_url:
            return {'ok': True, 'models': ['gpt-4o', 'deepseek-r1'], 'latency_ms': 42, 'status_code': 200, 'error': ''}
        if 'backup.example.com' in base_url:
            return {'ok': True, 'models': ['deepseek-r1', 'claude-sonnet-4'], 'latency_ms': 88, 'status_code': 200, 'error': ''}
        return {'ok': False, 'models': [], 'latency_ms': 500, 'status_code': 502, 'error': 'HTTP 502: bad gateway'}

    monkeypatch.setattr(channel_router, 'fetch_channel_models_once', fake_fetch_once)
    agg = await channel_router.fetch_aggregated_models(store)

    # All healthy models aggregated; broken channel did not block healthy ones
    assert 'gpt-4o' in agg['models']
    assert 'claude-sonnet-4' in agg['models']
    assert 'deepseek-r1' in agg['models']
    # Shared model gets explicit ch:<id>/<model> disambiguation entries
    assert 'ch:ch_fast/deepseek-r1' in agg['models']
    assert 'ch:ch_backup/deepseek-r1' in agg['models']
    # Structured model_entries carry readable channel_name attribution
    entries_by_val = {e['value']: e for e in agg['model_entries']}
    assert entries_by_val['gpt-4o']['channel_name'] == 'Fast Provider'
    assert entries_by_val['claude-sonnet-4']['channel_name'] == 'Backup Provider'
    assert entries_by_val['ch:ch_fast/deepseek-r1']['channel_name'] == 'Fast Provider'
    assert entries_by_val['ch:ch_backup/deepseek-r1']['channel_name'] == 'Backup Provider'
    # Per-channel diagnostics preserved
    diag_by_id = {d['id']: d for d in agg['channel_models']}
    assert diag_by_id['ch_fast']['ok'] is True
    assert diag_by_id['ch_broken']['ok'] is False
    assert '502' in diag_by_id['ch_broken']['error']
    # Persisted channel models enable automatic routing immediately
    url, _, model, cid = store.config.resolve_route(bot_model='claude-sonnet-4')
    assert (url, model, cid) == ('https://backup.example.com/v1', 'claude-sonnet-4', 'ch_backup')


def test_channels_http_crud_and_test_endpoint(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('AGENT_TOKEN', 'test-channel-token-12345')
    app = create_app(tmp_path, scheduler=False)
    headers = {'Authorization': 'Bearer test-channel-token-12345'}

    async def fake_fetch_once(base_url: str, api_key: str, timeout: float = 12.0, client=None):
        return {'ok': True, 'models': ['qwen3-max', 'deepseek-v4'], 'latency_ms': 35, 'status_code': 200, 'error': ''}

    monkeypatch.setattr(channel_router, 'fetch_channel_models_once', fake_fetch_once)

    with TestClient(app) as client:
        r0 = client.get('/api/channels', headers=headers)
        assert r0.status_code == 200
        assert r0.json()['channels'] == []

        r1 = client.post('/api/channels', headers=headers, json={
            'id': 'qwen_hub',
            'name': '通义聚合',
            'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
            'api_key': 'sk-qwen-secret-9999',
            'priority': 15,
            'auto_fetch': True,
        })
        assert r1.status_code == 200
        body1 = r1.json()
        assert body1['saved'] is True
        assert body1['channel']['id'] == 'qwen_hub'
        assert body1['channel']['has_api_key'] is True
        assert 'api_key' not in body1['channel']
        assert body1['channel']['models'] == ['qwen3-max', 'deepseek-v4']

        # Test endpoint refreshes models & returns latency
        r2 = client.post('/api/channels/qwen_hub/test', headers=headers)
        assert r2.status_code == 200
        assert r2.json()['ok'] is True
        assert r2.json()['count'] == 2

        # Delete channel
        r3 = client.delete('/api/channels/qwen_hub', headers=headers)
        assert r3.status_code == 200
        assert r3.json()['deleted'] is True
        assert r3.json()['channels'] == []


@pytest.mark.asyncio
async def test_admin_config_tool_channel_lifecycle(tmp_path: Path):
    store = _make_store(tmp_path)
    events: list[dict] = []

    async def publish(ev: dict):
        events.append(ev)

    brain = SimpleNamespace(store=store, publish=publish, _is_non_user_execution_context=lambda: False)
    r_add = await handle_system_config_tool(brain, {
        'action': 'add_channel',
        'id': 'ai_hub',
        'name': 'AI Hub',
        'base_url': 'https://hub.example.com/v1',
        'api_key': 'sk-hub-12345678',
        'priority': 50,
    })
    assert r_add['ok'] is True
    assert r_add['channel']['id'] == 'ai_hub'
    assert r_add['channel']['has_api_key'] is True

    r_list = await handle_system_config_tool(brain, {'action': 'list_channels'})
    assert r_list['ok'] is True
    assert len(r_list['channels']) == 1

    r_del = await handle_system_config_tool(brain, {'action': 'delete_channel', 'channel_id': 'ai_hub'})
    assert r_del['ok'] is True
    assert r_del['channels'] == []
