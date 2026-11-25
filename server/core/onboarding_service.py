"""
[Module Responsibility]
Zero-friction onboarding & conversational system configuration service
(`server/core/onboarding_service.py`).
"""
from __future__ import annotations
import copy, os, time
from pathlib import Path
from typing import Any
from storage import normalize_node_source

_PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    'deepseek': {
        'base_url': 'https://api.deepseek.com/v1',
        'small_model': 'deepseek-chat',
        'big_model': 'deepseek-reasoner',
    },
    'openrouter': {
        'base_url': 'https://openrouter.ai/api/v1',
        'small_model': 'deepseek/deepseek-chat-v3-0324',
        'big_model': 'deepseek/deepseek-r1',
    },
    'siliconflow': {
        'base_url': 'https://api.siliconflow.cn/v1',
        'small_model': 'deepseek-ai/DeepSeek-V3',
        'big_model': 'deepseek-ai/DeepSeek-R1',
    },
    'gemini': {
        'base_url': 'https://generativelanguage.googleapis.com/v1beta/openai',
        'small_model': 'gemini-2.5-flash',
        'big_model': 'gemini-2.5-pro',
    },
    'openai': {
        'base_url': 'https://api.openai.com/v1',
        'small_model': 'gpt-4.1-mini',
        'big_model': 'gpt-4.1',
    },
}


def _detect_provider(provider_hint: str, url: str, key: str) -> str:
    p = str(provider_hint or '').strip().lower()
    if p in _PROVIDER_PRESETS:
        return p
    u = str(url or '').strip().lower()
    k = str(key or '').strip()
    if 'deepseek.com' in u:
        return 'deepseek'
    if 'openrouter.ai' in u or k.startswith('sk-or-'):
        return 'openrouter'
    if 'siliconflow.cn' in u:
        return 'siliconflow'
    if 'generativelanguage.googleapis.com' in u or k.startswith('AIza'):
        return 'gemini'
    if u == 'https://api.openai.com/v1' or (not u and k.startswith('sk-')):
        return 'openai'
    return p or 'custom'


def infer_provider_defaults(payload: dict[str, Any], current_cfg: Any = None) -> dict[str, Any]:
    """Fill sensible `api_base_url`, `default_model`, `small_brain`, and `big_brain` from a provider preset or key."""
    out = copy.deepcopy(dict(payload or {}))
    provider_raw = str(out.pop('provider', '') or '').strip().lower()
    cur_url = str(getattr(current_cfg, 'api_base_url', '') or '').strip()
    cur_model = str(getattr(current_cfg, 'default_model', '') or '').strip()
    cur_big_model = str(getattr(getattr(current_cfg, 'big_brain', None), 'model', '') or '').strip()

    req_url = str(out.get('api_base_url') or '').strip()
    req_key = str(out.get('api_key') or '').strip()
    req_model = str(out.get('default_model') or '').strip()

    has_api_intent = bool(provider_raw or 'api_key' in out or 'api_base_url' in out or 'default_model' in out)
    if not has_api_intent:
        return out

    effective_url = req_url or cur_url
    detected = _detect_provider(provider_raw, effective_url, req_key)
    preset = _PROVIDER_PRESETS.get(detected)

    if preset:
        if (not req_url) and (provider_raw in _PROVIDER_PRESETS or not cur_url or cur_url == 'https://api.openai.com/v1'):
            # Prefer preset base URL when explicit provider preset or key prefix matched on stock OpenAI URL
            if provider_raw in _PROVIDER_PRESETS or detected != 'openai':
                out['api_base_url'] = preset['base_url']
        if not req_model and (not cur_model or provider_raw in _PROVIDER_PRESETS):
            out['default_model'] = preset['small_model']

        # Also wire big_brain model intelligently on fresh 1-step setup unless explicitly passed
        explicit_big = out.get('big_brain')
        if not isinstance(explicit_big, dict):
            if (not cur_big_model) or (provider_raw in _PROVIDER_PRESETS and cur_big_model in ('', 'gpt-4.1-mini', 'deepseek-chat')):
                big_patch: dict[str, Any] = {'model': preset['big_model']}
                if 'api_base_url' in out:
                    big_patch['base_url'] = out['api_base_url']
                if req_key:
                    big_patch['api_key'] = req_key
                out['big_brain'] = big_patch
    elif req_url and not req_model and not cur_model:
        out['default_model'] = 'gpt-4.1-mini'

    return out


def _read_server_url_and_token(brain: Any) -> tuple[str, str]:
    store = brain.store
    data_dir = Path(getattr(store, 'data_dir', None) or (store.config_path.parent))
    tok = ''
    getter = getattr(brain, 'token_getter', None)
    if callable(getter):
        try:
            tok = str(getter() or '').strip()
        except Exception:
            tok = ''
    if not tok:
        for cand in (data_dir / 'device-token', store.config_path.parent / 'device-token'):
            if cand.exists():
                try:
                    tok = cand.read_text('utf-8').strip()
                    if tok:
                        break
                except Exception:
                    pass
    base_url = (
        os.getenv('DS_CLAW_PUBLIC_URL', '').strip()
        or str(getattr(brain, 'public_base_url', '') or '').strip()
        or 'https://<YOUR_SERVER_DOMAIN>/proactive-agent'
    ).rstrip('/')
    return base_url, (tok or '<DEVICE_TOKEN>')


def _status_payload(brain: Any) -> dict[str, Any]:
    store = brain.store
    cfg = store.config
    cc = getattr(brain, 'computer_coordinator', None) or getattr(brain, 'computer', None)
    cc_configured = bool(
        (cc and getattr(cc, 'client', None) and getattr(cc.client, 'configured', False))
        or (cfg.cloud_computer_host or '').strip()
        or (cfg.cloud_computer_url or '').strip()
    )
    dsh_info = getattr(brain, 'dsh_bridge_info', {}) or {}
    dsh_online = False
    try:
        dsh_online = bool(dsh_info.get('online')) or (time.time() - float(dsh_info.get('last_seen') or 0.0) < 90.0)
    except Exception:
        dsh_online = False

    base_url, _ = _read_server_url_and_token(brain)
    return {
        'ok': True,
        'config': cfg.public(),
        'onboarding_completed': bool(cfg.onboarding_completed),
        'auto_compact_tokens': int(cfg.auto_compact_tokens),
        'curiosity_interval_minutes': int(cfg.curiosity_interval_minutes),
        'idle_research_minutes': int(cfg.idle_research_minutes),
        'quiet_hours': f'{cfg.quiet_start_hour:02d}:00-{cfg.quiet_end_hour:02d}:00',
        'phone_online': bool(brain.is_phone_online()),
        'dsh_online': dsh_online,
        'cloud_computer': {
            'configured': cc_configured,
            'mode': cfg.cloud_computer_mode or ('local_or_http' if cfg.cloud_computer_url else ''),
            'host': cfg.cloud_computer_host,
            'status': cfg.cloud_computer_status or ('configured' if cc_configured else 'not_configured'),
        },
        'pc_connect_summary': {
            'server_url': base_url,
            'dsh_online': dsh_online,
            'agent_script': 'desktop/desktop_agent.py',
        },
    }


async def handle_system_config_tool(brain: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Conversational configuration tool invoked by Whale-chan during onboarding or later adjustments."""
    store = brain.store
    action = str(args.get('action') or args.get('op') or 'status').strip().lower()
    non_user_ctx = bool(hasattr(brain, '_is_non_user_execution_context') and brain._is_non_user_execution_context())

    if action in ('get', 'status', 'inspect'):
        return _status_payload(brain)

    if action == 'mark_onboarded':
        updated = store.update_config({'onboarding_completed': True}, source_tag='whalechan_onboarding')
        if callable(getattr(brain, 'publish', None)):
            await brain.publish({'type': 'config_updated', 'onboarding_completed': True, 'config': updated})
        return {'ok': True, 'onboarding_completed': True, 'config': updated}

    if action == 'pc_connect_guide':
        if non_user_ctx:
            return {'ok': False, 'error': '权限锁定：仅允许用户在直接对话中查看包含配对凭证的电脑连接指令'}
        base_url, tok = _read_server_url_and_token(brain)
        pair_code = f'DSCLAW1:{base_url}|{tok}'
        skill_url = f'{base_url}/bootstrap/pc-skill.md'
        ai_agent_prompt = (
            f'请读取 {skill_url}（或仓库内的 .agents/skills/pulse-pc-bootstrap/SKILL.md），'
            f'严格按其中的步骤把这台电脑接入我的 Pulse Agent，配对码：{pair_code}'
        )
        return {
            'ok': True,
            'server_url': base_url,
            'pair_code': pair_code,
            'skill_url': skill_url,
            'ai_agent_prompt': ai_agent_prompt,
            'setup_mjs_cmd': f'node desktop/dsh-plugin/setup.mjs --pair-code "{pair_code}"',
            'python_agent_cmd': f'python desktop/desktop_agent.py --pair-code "{pair_code}" --save',
            'python_check_cmd': f'python desktop/desktop_agent.py --pair-code "{pair_code}" --save --check',
            'powershell_cmd': f'python desktop/desktop_agent.py --pair-code "{pair_code}" --save',
            'bash_cmd': f'python3 desktop/desktop_agent.py --pair-code "{pair_code}" --save',
            'steps': [
                '1. 最省心（把下面 ai_agent_prompt 发给你电脑上的 Claude Code / Codex / Antigravity / Cursor）：AI 会自动读取启动 Skill 识别系统环境并把电脑接入配对！',
                '2. 或手工一行搞定：有 DSH 桌面端运行 setup_mjs_cmd；跨平台通用直接运行 python_check_cmd 校验并启动 python_agent_cmd。',
                '3. 连接成功后即可在手机端直接指挥本机终端与桌面 GUI。',
            ],
        }

    if action == 'phone_wireless_guide':
        return {
            'ok': True,
            'phone_online': bool(brain.is_phone_online()),
            'supported_modes': ['root (uid=0)', 'shizuku (uid=2000)', 'wireless_adb (uid=2000)'],
            'steps': [
                '1. 推荐方式（Shizuku 无线启动，关掉 Wi-Fi 切 5G 流量不断连）：先打开系统【无线调试】，在 Shizuku App 点击「通过无线调试启动」，再到本 App【设置 → 手机工作区 → Shizuku 无线授权】点「一键授权 Shizuku」即可自动连接 uid=2000 后台虚拟屏。',
                '2. 备用方式（免装额外 App · 内置无线 ADB）：在本 App【设置 → 手机工作区】点击「开始无线配对通知」，去系统【使用配对码配对设备】直接下拉通知栏填 6 位码配对。',
                '3. 若设备已 Root 则全自动直连 uid=0；未授权前系统绝不会回退操控主屏 Display 0。',
            ],
        }

    if action == 'test_api':
        base_url = store.config.effective_base_url().rstrip('/')
        api_key = store.config.effective_api_key(big=True) or store.config.effective_api_key(big=False)
        if not base_url or not api_key:
            return {'ok': False, 'error': '尚未配置 API Base URL 或 API Key'}
        try:
            models = await brain.llm.fetch_available_models(raise_on_auth_error=True)
            return {
                'ok': True,
                'base_url': store.config.effective_base_url(),
                'default_model': store.config.effective_model(big=False),
                'models_sample': list(models[:20]),
            }
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}

    if action == 'list_channels':
        return {'ok': True, 'channels': store.config.public().get('channels', [])}

    if action in ('add_channel', 'update_channel', 'upsert_channel'):
        if non_user_ctx:
            return {'ok': False, 'error': '权限锁定：仅允许用户在直接对话中增改 API 渠道'}
        from core.channel_router import fetch_channel_models_once, upsert_channel_in_store
        ch_payload = dict(args.get('channel') if isinstance(args.get('channel'), dict) else args)
        try:
            pub_ch, pub_cfg = upsert_channel_in_store(store, ch_payload, source_tag='admin_tool_channel')
        except ValueError as exc:
            return {'ok': False, 'error': str(exc)}
        test_res = None
        if ch_payload.get('auto_fetch') and pub_ch.get('id'):
            raw_ch = next((c for c in store.config.channels if c.id == pub_ch['id']), None)
            if raw_ch and raw_ch.base_url and raw_ch.api_key:
                test_res = await fetch_channel_models_once(raw_ch.base_url, raw_ch.api_key)
                if test_res.get('ok') and test_res.get('models'):
                    pub_ch, pub_cfg = upsert_channel_in_store(store, {'id': raw_ch.id, 'models': test_res['models']}, source_tag='admin_tool_channel')
        if callable(getattr(brain, 'publish', None)):
            await brain.publish({'type': 'config_updated', 'channels': pub_cfg.get('channels', [])})
        return {'ok': True, 'channel': pub_ch, 'channels': pub_cfg.get('channels', []), 'test': test_res}

    if action == 'delete_channel':
        if non_user_ctx:
            return {'ok': False, 'error': '权限锁定：仅允许用户在直接对话中删除 API 渠道'}
        from core.channel_router import delete_channel_in_store
        cid = str(args.get('channel_id') or args.get('id') or '').strip()
        deleted, pub_cfg = delete_channel_in_store(store, cid, source_tag='admin_tool_channel')
        if not deleted:
            return {'ok': False, 'error': f'未找到渠道: {cid}'}
        if callable(getattr(brain, 'publish', None)):
            await brain.publish({'type': 'config_updated', 'channels': pub_cfg.get('channels', [])})
        return {'ok': True, 'deleted': True, 'id': cid, 'channels': pub_cfg.get('channels', [])}

    if action == 'test_channel':
        from core.channel_router import fetch_channel_models_once, upsert_channel_in_store
        cid = str(args.get('channel_id') or args.get('id') or '').strip()
        raw_ch = next((c for c in (store.config.channels or []) if c.id == cid), None)
        if raw_ch is None:
            return {'ok': False, 'error': f'未找到渠道: {cid}'}
        res = await fetch_channel_models_once(raw_ch.base_url, raw_ch.api_key, timeout=15.0)
        if res.get('ok') and res.get('models'):
            upsert_channel_in_store(store, {'id': raw_ch.id, 'models': res['models']}, source_tag='admin_tool_channel_test')
        return {'ok': bool(res.get('ok')), 'id': raw_ch.id, 'name': raw_ch.name or raw_ch.id, 'latency_ms': res.get('latency_ms', 0), 'count': len(res.get('models') or []), 'models_sample': list((res.get('models') or [])[:20]), 'error': res.get('error', '')}

    if action == 'update':
        if non_user_ctx:
            return {'ok': False, 'error': '权限锁定：仅允许用户在直接对话中修改系统配置与凭证（禁止后台/子任务/同伴越权改配）'}
        allowed_keys = {
            'api_base_url', 'api_key', 'default_model', 'worker_model',
            'embedding_tier', 'embedding_api_base_url', 'embedding_api_key', 'embedding_api_model',
            'cache_keepalive_enabled', 'cache_keepalive_interval_sec', 'cache_keepalive_max_pings', 'idle_compact_minutes',
            'provider', 'channels',
            'fallback_enabled', 'fallback_base_url', 'fallback_api_key', 'fallback_model',
            'small_brain', 'big_brain', 'smtp', 'enabled',
            'curiosity_interval_minutes', 'idle_research_minutes',
            'email_after_minutes', 'overlay_after_minutes',
            'quiet_start_hour', 'quiet_end_hour', 'auto_compact_tokens',
            'onboarding_completed', 'cloud_computer_mode', 'cloud_computer_host',
            'cloud_computer_port', 'cloud_computer_user', 'cloud_computer_password',
            'cloud_computer_ssh_key', 'cloud_computer_url', 'cloud_computer_status',
        }
        patch: dict[str, Any] = {}
        alias_map = {'base_url': 'api_base_url', 'model': 'default_model'}
        raw_settings = args.get('settings')
        if isinstance(raw_settings, dict):
            for k, v in raw_settings.items():
                mk = alias_map.get(k, k)
                if mk in allowed_keys and v is not None:
                    patch[mk] = v
        for k, v in args.items():
            mk = alias_map.get(k, k)
            if mk in allowed_keys and v is not None:
                patch[mk] = v
        kv_key = alias_map.get(str(args.get('key') or '').strip(), str(args.get('key') or '').strip())
        if kv_key in allowed_keys and args.get('value') is not None:
            patch[kv_key] = args.get('value')
        if not patch:
            return {'ok': False, 'error': f'未提供任何可修改的系统配置字段（支持：{", ".join(sorted(allowed_keys)[:12])} 等）'}

        inferred = infer_provider_defaults(patch, store.config)
        updated = store.update_config(inferred, source_tag='tool:system_config')
        cc = getattr(brain, 'computer_coordinator', None) or getattr(brain, 'computer', None)
        if cc and getattr(cc, 'client', None) and any(k.startswith('cloud_computer_') for k in inferred):
            try:
                await cc.client.reload_config()
            except Exception:
                pass
        if callable(getattr(brain, 'publish', None)):
            await brain.publish({
                'type': 'config_updated',
                'activity': brain.ready_state(),
                'auto_compact_tokens': store.config.auto_compact_tokens,
                'curiosity_interval_minutes': store.config.curiosity_interval_minutes,
                'idle_research_minutes': store.config.idle_research_minutes,
                'quiet_start_hour': store.config.quiet_start_hour,
                'quiet_end_hour': store.config.quiet_end_hour,
                'onboarding_completed': store.config.onboarding_completed,
                'context_by_bot': brain.memory_engine.get_all_bots_context_stats(source='phone'),
            })
        return {
            'ok': True,
            'saved': True,
            'changed_keys': sorted(inferred.keys()),
            'config': updated,
        }

    return {'ok': False, 'error': f'不支持的 system_config action: {action}'}


async def maybe_trigger_whalechan_onboarding(
    store: Any,
    brain: Any,
    publish: Any,
    source: str = 'phone',
) -> dict[str, Any] | None:
    """Post an instant zero-latency Whale-chan welcome & setup checklist on first API Key save."""
    if getattr(store.config, 'onboarding_completed', False):
        return None
    has_key = bool(store.config.effective_api_key(big=False) or store.config.effective_api_key(big=True))
    if not has_key:
        return None

    has_companion_msgs = any(
        m.get('bot_id') == 'companion' and normalize_node_source(m.get('source')) == 'phone'
        for m in store.state.get('messages', [])
    )
    if has_companion_msgs:
        return None

    cur_compact = int(getattr(store.config, 'auto_compact_tokens', 100000) or 100000)
    cur_curiosity = int(getattr(store.config, 'curiosity_interval_minutes', 90) or 90)
    cur_model = store.config.effective_model(big=False) or '默认模型'

    welcome_text = (
        f'API 已连通就绪（当前模型：`{cur_model}`），现在为你核对基础配置：\n\n'
        '接下来我直接陪你用聊天把剩下几项全部配齐，**不用去翻复杂的设置菜单**（我先给你标好了最省心的推荐默认值）：\n'
        f'1. **上下文分段局部压缩阈值**：当前 `{cur_compact // 1000}K` Tokens（推荐 `100K`：低于阈值不压缩，前缀缓存命中 98%+，满额只压缩已完结旧话题）。\n'
        f'2. **主动陪伴与夜间免打扰**：推荐每 `{cur_curiosity}` 分钟轻声关照一次，`00:00–08:00` 夜间静音。\n'
        '3. **手机后台隔离虚拟屏**：已 Root 自动直连；无 Root 推荐使用 **Shizuku 无线启动（切 5G 流量不断线）**，或用 App 内置无线调试 (`uid=2000`) 实现后台隔离控屏。\n'
        '4. **连接电脑端 & 闲置 VPS 云电脑**：如果有空闲 Linux VPS，可直接走加密云端部署一键搭出 24/7 云桌面；电脑端则能直接复制启动 Skill 指令发给电脑上的 AI 自动接入。\n\n'
        '你想先从哪一项开始，还是先一键套用推荐设置？'
    )
    choices = [
        {
            'label': '一键套用推荐配置（100K压缩+90m节奏）',
            'value': '请帮我套用推荐配置（100K自动压缩、90分钟主动陪伴、0-8点免打扰），然后继续引导我连接手机虚拟屏、电脑或VPS云电脑！',
        },
        {
            'label': '开启手机免Root无线虚拟屏',
            'value': '我的手机没有Root，教我用无线调试开启后台隔离虚拟屏吧！',
        },
        {
            'label': '我有闲置VPS，帮我搭云电脑',
            'value': '我有一台闲置Linux VPS，想让你帮我自动部署24小时云电脑！',
        },
        {
            'label': '连接我的Windows电脑',
            'value': '我想把我的Windows电脑连上来协同干活，怎么连？',
        },
    ]
    msg = store.message(
        'assistant',
        welcome_text,
        kind='chat',
        bot_id='companion',
        source='phone',
        choices=choices,
    )
    if callable(publish):
        await publish({'type': 'message', 'message': msg, 'source': 'phone'})
    return msg
