"""Tests for cross-compaction memory continuity & 4-source hybrid recall."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from core.compaction_service import (
    _build_fallback_summary,
    _retain_experiences_precompact,
    build_compaction_replay_messages,
    OPENCODE_SUMMARY_TEMPLATE,
)
from core.context_search import ContextSearchEngine
from core.long_term_memory import LongTermMemoryEngine
from core.unified_recall import (
    expand_recall_query,
    format_unified_recall_block,
    unified_cross_compaction_recall,
)
from vault_manager import Vault


def test_compaction_replay_flattens_tool_calls_and_dual_track_template():
    assert 'User Facts, Preferences & Commitments' in OPENCODE_SUMMARY_TEMPLATE
    assert 'Key Resolved Conclusions' in OPENCODE_SUMMARY_TEMPLATE

    older = [
        {'role': 'user', 'text': '帮我查一下土耳其卡的续费方式'},
        {
            'role': 'assistant',
            'text': '查到了，公用 Mastercard 5335 已经绑进苹果小号。',
            'turn_steps': [
                {'role': 'assistant', 'content': '查笔记', 'tool_calls': [{'id': 'c1', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{"path":"notes/tr.md"}'}}]},
                {'role': 'tool', 'tool_call_id': 'c1', 'content': 'Mastercard 5335 bound to Apple ID'},
            ],
        },
    ]
    msgs = build_compaction_replay_messages(older, previous_ledger='### 📋 核心进展与上下文摘要\n- 旧目标：保留 5335 纪律')
    assert all(m.get('role') != 'tool' for m in msgs)
    assert all('tool_calls' not in m for m in msgs)
    joined = '\n'.join(str(m.get('content') or '') for m in msgs)
    assert '5335' in joined
    assert '[Tool Result]: Mastercard 5335' in joined


def test_fallback_summary_preserves_previous_ledger():
    older = [{'role': 'user', 'text': '最新一句闲聊'}]
    fb = _build_fallback_summary(older, previous_ledger='## Objective\n- 关键长期目标：Bybit Card 办理')
    assert 'Bybit Card' in fb
    assert '继承上一轮锚定工作簿' in fb


def test_precompact_inline_experiences_json_and_all_bot_fact_sharing(tmp_path: Path):
    vault = Vault(tmp_path / 'memory_vault')
    ltm = LongTermMemoryEngine(tmp_path / 'ltm.db', vault=vault)
    summary = (
        '### 📋 核心进展与上下文摘要\n## Objective\n- 办理土区卡\n'
        '<experiences_json>[{"topic":"tr-card-5335","kind":"fact","outcome":"success",'
        '"l0":"Google One 土区续费用公用 Mastercard 5335","l1":"已绑定苹果小号，12月1日提醒续费"}]</experiences_json>'
    )
    asyncio.run(
        _retain_experiences_precompact(
            ltm,
            summary_text=summary,
            raw_transcript='dummy',
            bot_id='companion',
            source='phone',
            ctx_id='ctx-1234',
            silent_complete_fn=None,
        )
    )
    # Must be recallable by ANOTHER bot (e.g. research) because kind=fact -> bot_id="all"
    hits = ltm.recall('Mastercard 5335 土区续费', bot_id='research', limit=3)
    assert hits and any('5335' in h['l0_summary'] for h in hits)


def test_unified_4_source_recall_and_short_anaphora_expansion(tmp_path: Path):
    vault = Vault(tmp_path / 'memory_vault')
    vault.write('notes/google-apple-tr-region-status.md', '# 土区订阅状态\n- 公用 Mastercard 5335 已绑定苹果小号\n- 12-01 强提醒续费\n')
    ltm = LongTermMemoryEngine(tmp_path / 'ltm.db', vault=vault)
    ltm.retain(
        l0_summary='Kliq 独享卡优先走接码平台路线',
        l1_overview='避坑清单已确认，明早 7-10 点执行 5sim 接码',
        topic='kliq-card',
        kind='experience',
        bot_id='companion',
    )
    cs = ContextSearchEngine(tmp_path / 'ctx.db')
    cs.index_message({
        'id': 'old-msg-1',
        'bot_id': 'companion',
        'role': 'user',
        'kind': 'chat',
        'text': '我之前把 fubuki.com.cn 的域名解析改完了，记得检查移动端字号',
        'created_at': 1700000000.0,
    })

    # 1. Short anaphoric query expands using recent turns and hits vault note!
    recent = [
        {'role': 'user', 'content': '帮我看看 Google One 土区那个 5335 卡'},
        {'role': 'assistant', 'content': '好的，余记着那张 5335 卡绑在苹果小号上。'},
    ]
    exp_q, ents = expand_recall_query('之前那个怎么说的？', recent)
    assert '5335' in exp_q and '5335' in ents

    items = unified_cross_compaction_recall(
        query='之前那个怎么说的？',
        recent_turns=recent,
        ltm=ltm,
        vault=vault,
        context_search=cs,
        bot_id='companion',
        exclude_msg_ids={'active-msg-9'},
        limit=5,
    )
    assert any(it['source_type'] == 'vault_note' and 'google-apple-tr-region-status.md' in it['title'] for it in items)

    # 2. Archived pre-compaction message recall works
    items2 = unified_cross_compaction_recall(
        query='fubuki.com.cn 移动端字号',
        ltm=ltm,
        vault=vault,
        context_search=cs,
        bot_id='companion',
        limit=5,
    )
    assert any(it['source_type'] == 'archived_msg' and 'fubuki.com.cn' in it['body'] for it in items2)
    block = format_unified_recall_block(items2)
    assert '<recalled_memory' in block and "memory(op='ctx'" in block
