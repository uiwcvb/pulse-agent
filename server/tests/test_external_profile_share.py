from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.compaction_service import build_compaction_replay_messages
from core.turn_history import expand_stored_turn_steps
from main import create_app


def test_external_ai_profile_db_sync_token_share_and_zero_context_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_TOKEN', 'admin-secret')
    app = create_app(tmp_path, scheduler=False)

    # Populate vault profile files including an accidental credential line that MUST be stripped
    vault = app.state.vault
    vault.write(
        'profile/personal_impression.md',
        '# 个人印象档案\n\n## 1. 基本背景\n- **所在城市**：滨海市 / 软件工程师\n- **偏好**：直接高效、少说套话\n- **API_KEY**：sk-shouldNEVERleak1234567890\n\n## 2. 待补充\n- **爱好**：(待了解)\n',
    )
    vault.write(
        'profile/career.md',
        '# 职业与技术主线\n\n## 技术栈\n- 熟悉 Python FastAPI、Kotlin Jetpack Compose、Node DSH 架构\n',
    )

    with TestClient(app, headers={'Authorization': 'Bearer admin-secret'}) as client:
        # 1. Admin reads synced external profile cards
        ext_res = client.get('/api/profile/external')
        assert ext_res.status_code == 200
        ext_data = ext_res.json()
        assert ext_data['ok'] is True
        titles = [c['title'] for c in ext_data['cards']]
        assert '1. 基本背景' in titles
        assert '技术栈' in titles
        joined_summaries = '\n'.join(c['summary'] for c in ext_data['cards'])
        assert '滨海市' in joined_summaries
        assert 'sk-shouldNEVERleak1234567890' not in joined_summaries
        assert '(待了解)' not in joined_summaries

        # 2. Create scoped read-only share token + bootstrap prompt
        tok_res = client.post(
            '/api/profile/external/token',
            json={
                'label': '给外部网页 AI 用',
                'scopes': ['general', 'coding'],
                'scene': 'coding',
                'public_base_url': 'https://demo.example.com/proactive-agent',
            },
        )
        assert tok_res.status_code == 200
        tok_body = tok_res.json()
        raw_tok = tok_body['token']
        assert raw_tok.startswith('ext_')
        assert 'https://demo.example.com/proactive-agent/api/share/profile?token=' in tok_body['bootstrap_prompt']
        assert '滨海市' in tok_body['bootstrap_prompt']

        # 3. Unauthenticated or device-token access on /api/share/profile without share token fails with 401
        unauth = client.get('/api/share/profile', headers={'Authorization': ''})
        assert unauth.status_code == 401

        # 4. External AI reads Markdown & JSON profile via read-only share token
        md_resp = client.get(f'/api/share/profile?token={raw_tok}&scene=coding&format=md', headers={'Authorization': ''})
        assert md_resp.status_code == 200
        assert 'FastAPI' in md_resp.text
        assert '滨海市' in md_resp.text
        assert 'sk-shouldNEVERleak1234567890' not in md_resp.text

        # 5. Keyword search route
        srch = client.get(f'/api/share/search?token={raw_tok}&q=Kotlin&format=json', headers={'Authorization': ''})
        assert srch.status_code == 200
        assert srch.json()['cards_count'] >= 1

        # 6. Revoking token immediately blocks further external reads
        rev = client.delete(f"/api/profile/external/token/{tok_body['token_id']}")
        assert rev.status_code == 200 and rev.json()['ok'] is True
        after_rev = client.get(f'/api/share/profile?token={raw_tok}', headers={'Authorization': ''})
        assert after_rev.status_code == 401


def test_intermediate_credentials_preserved_through_compaction_replay():
    # Simulate an account registration turn where credentials were spoken before/between tool calls
    raw_steps = [
        {'type': 'step_text', 'text': '余先把注册账号定为 baikefox2026，密码设成 P@ss987654，现在提交表单。'},
        {
            'role': 'assistant',
            'content': '余先把注册账号定为 baikefox2026，密码设成 P@ss987654，现在提交表单。',
            'tool_calls': [{'id': 'tc1', 'type': 'function', 'function': {'name': 'cloud', 'arguments': '{"op":"click"}'}}],
        },
        {'role': 'tool', 'tool_call_id': 'tc1', 'content': '{"ok": true, "url": "https://porkbun.com/checkout"}'},
    ]
    final_msg = '余先把注册账号定为 baikefox2026，密码设成 P@ss987654，现在提交表单。\n\n注册已进入支付页面，差二维码付款。'

    expanded = expand_stored_turn_steps(raw_steps, final_msg)
    serialized = json.dumps(expanded, ensure_ascii=False)
    assert 'baikefox2026' in serialized
    assert 'P@ss987654' in serialized

    replay = build_compaction_replay_messages(
        [
            {'role': 'user', 'text': '帮我注册一个 Porkbun 账号'},
            {'role': 'assistant', 'text': final_msg, 'turn_steps': raw_steps},
        ]
    )
    replay_blob = json.dumps(replay, ensure_ascii=False)
    assert 'baikefox2026' in replay_blob
    assert 'P@ss987654' in replay_blob
