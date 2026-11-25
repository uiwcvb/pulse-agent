"""Comprehensive tests for Phase 0-3:
- 2+1 config routing (worker_model, embedding_tier, keepalive/idle_compact, key redaction)
- CacheKeepaliveManager bounded keepalive + 9m idle hot compaction + 0 overnight burn
- subtask presets + parent custom tools + worker_model dispatch
- EmbeddingEngine INT8 quantization + idle unload + tier registry
- MemoryTablesStore 4-table schema + 4-way reconciliation + vector search + reindex
- One-click legacy memory reorganization (backup, migration, compact archive, memory_ledger.md)
- Weekly Memory Gardener (expiry, >0.92 dedup, >90d monthly episode rollup)
- Unified recall & slash commands integration
"""
from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.cache_keepalive import CacheKeepaliveManager
from core.embedding_engine import (
    EMBEDDING_TIERS,
    EmbeddingEngine,
    cosine_similarity_blobs,
    pack_int8_vector,
    unpack_int8_vector,
)
from core.memory_migrator import reorganize_legacy_memory, run_memory_gardener
from core.memory_tables import MemoryTablesStore
from core.slash_commands import try_handle_slash_command
from core.subtask_runner import SUBTASK_PRESETS, execute_run_subtask
from core.unified_recall import unified_cross_compaction_recall
from storage import Config, Store


class _FakeEmbeddingEngine:
    """Deterministic lightweight semantic embedder for unit tests (zero network/weights)."""

    def __init__(self, model_id: str = 'local_gte_small_zh'):
        self.model_id = model_id
        self.unloaded = False

    def active_model_id(self) -> str:
        return self.model_id

    def _vec(self, text: str) -> list[float]:
        s = (text or '').lower()
        dims = [0.0] * 16
        # Hand-crafted semantic buckets so synonyms ("土区卡" vs "Mastercard 5335") match strongly
        buckets = [
            ('5335', '土区', 'mastercard', '苹果', '续费', 'card'),
            ('静音', '夜间', '免打扰', '睡觉', 'quiet'),
            ('vps', '1c1g', '服务器', '部署', '内存'),
            ('白珂', '主人', '狐狸', '称呼'),
            ('微信', '手机', '虚拟屏', 'android'),
            ('python', 'node', 'pytest', 'sidecar'),
        ]
        for idx, kws in enumerate(buckets):
            for kw in kws:
                if kw in s:
                    dims[idx] += 1.0
        for ch in s:
            dims[6 + (ord(ch) % 10)] += 0.08
        norm = math.sqrt(sum(v * v for v in dims)) or 1.0
        return [v / norm for v in dims]

    def embed_texts(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_one(self, text: str, *, is_query: bool = False) -> bytes:
        return pack_int8_vector(self._vec(text))

    def maybe_unload_idle(self, now_ts: float | None = None) -> bool:
        self.unloaded = True
        return True

    def status(self) -> dict:
        return {'tier': self.model_id, 'model_id': self.model_id, 'installed': True, 'loaded': not self.unloaded}


def test_config_2plus1_routing_and_key_redaction(tmp_path: Path):
    store = Store(tmp_path)
    pub = store.update_config({
        'api_base_url': 'https://api.example.com/v1',
        'api_key': 'sk-main-secret-1234',
        'default_model': 'claude-opus-4',
        'worker_model': 'deepseek-chat',
        'embedding_tier': 'local_gte_small_zh',
        'embedding_api_key': 'sk-embed-secret-9999',
        'cache_keepalive_enabled': True,
        'cache_keepalive_interval_sec': 260,
        'idle_compact_minutes': 9,
    })
    assert pub['worker_model'] == 'deepseek-chat'
    assert pub['embedding_tier'] == 'local_gte_small_zh'
    assert pub['has_embedding_api_key'] is True
    assert 'embedding_api_key' not in pub
    assert pub['cache_keepalive_interval_sec'] == 260
    assert pub['idle_compact_minutes'] == 9
    assert 'compaction_model' not in pub

    assert store.config.effective_worker_model() == 'deepseek-chat'
    _, _, w_model, _ = store.config.resolve_worker_route()
    assert w_model == 'deepseek-chat'

    # Empty worker_model cleanly falls back to main model
    store.update_config({'worker_model': ''})
    assert store.config.effective_worker_model() == 'claude-opus-4'
    store.close()


def test_embedding_int8_pack_cosine_and_idle_unload(tmp_path: Path):
    v1 = [0.6, 0.8, 0.0, -0.2, 0.4, -0.5]
    v2 = [0.6, 0.8, 0.0, -0.2, 0.4, -0.5]
    v3 = [-0.6, -0.8, 0.0, 0.2, -0.4, 0.5]
    b1, b2, b3 = pack_int8_vector(v1), pack_int8_vector(v2), pack_int8_vector(v3)
    assert len(b1) == 4 + len(v1)
    assert cosine_similarity_blobs(b1, b2) > 0.99
    assert cosine_similarity_blobs(b1, b3) < -0.99
    assert len(unpack_int8_vector(b1)) == len(v1)
    assert 'local_gte_small_zh' in EMBEDDING_TIERS
    assert 'local_dmeta_zh_small' in EMBEDDING_TIERS

    eng = EmbeddingEngine(tmp_path / 'models', config_getter=lambda: Config(), idle_unload_sec=60.0)
    eng._session = object()
    eng._loaded_tier = 'local_gte_small_zh'
    eng._last_used_at = 1000.0
    assert eng.maybe_unload_idle(1030.0) is False
    assert eng.maybe_unload_idle(1061.0) is True
    assert eng._session is None


def test_cache_keepalive_bounded_pings_and_9m_idle_sleep():
    mgr = CacheKeepaliveManager()
    pings: list[tuple[str, str]] = []
    compacts: list[tuple[str, str]] = []
    resets: list[tuple[str, str]] = []

    class _FakeDsh:
        async def keepalive_session(self, bid, src):
            pings.append((src, bid))
            return {'ok': True, 'kept_alive': True}

        async def reset_session(self, bid, src):
            resets.append((src, bid))

    class _FakeBrain:
        def __init__(self):
            self.store = SimpleNamespace(
                config=Config(
                    cache_keepalive_enabled=True,
                    cache_keepalive_interval_sec=260,
                    idle_compact_minutes=9,
                    auto_compact_tokens=100_000,
                ),
                state={},
            )
            self.last_prompt_tokens = {'phone:companion': 80_000}
            self.dsh_adapter = _FakeDsh()
            self.active_tasks_by_node_bot = {}
            self.executions_by_node_bot = {}

        def is_node_bot_busy(self, bid, src):
            return False

        async def _auto_compact_if_needed(self, bid, force=False, source='phone'):
            compacts.append((source, bid))
            return {'compacted': True}

    brain = _FakeBrain()
    t0 = 10_000.0
    mgr.touch('phone', 'companion', now_ts=t0)

    # At +100s: too early
    assert asyncio.run(mgr.tick(brain, now_ts=t0 + 100)) == []
    assert len(pings) == 0

    # At +265s: exactly 1 keepalive ping
    a1 = asyncio.run(mgr.tick(brain, now_ts=t0 + 265))
    assert a1[0]['action'] == 'keepalive_ping'
    assert len(pings) == 1

    # At +525s (8m45s, 15s before 9m compact): MUST NOT send a wasteful 2nd keepalive ping!
    assert asyncio.run(mgr.tick(brain, now_ts=t0 + 525)) == []
    assert len(pings) == 1

    # At +545s (9m5s): hot compaction triggers once and session enters idle_sleep
    a2 = asyncio.run(mgr.tick(brain, now_ts=t0 + 545))
    assert a2[0]['action'] == 'idle_hot_compact'
    assert len(compacts) == 1 and len(resets) == 1

    # Overnight (+20000s): 0 pings, 0 extra compactions!
    assert asyncio.run(mgr.tick(brain, now_ts=t0 + 20_000)) == []
    assert len(pings) == 1 and len(compacts) == 1
    assert brain.last_prompt_tokens['phone:companion'] == 0

    # Manual/auto compaction immediately marks idle_slept => 0 pings and 0 re-compactions even after 260s/540s!
    mgr.touch('phone', 'companion', now_ts=t0 + 30_000)
    brain.last_prompt_tokens['phone:companion'] = 90_000
    mgr.mark_compacted('phone', 'companion', now_ts=t0 + 30_010)
    assert asyncio.run(mgr.tick(brain, now_ts=t0 + 30_300)) == []
    assert asyncio.run(mgr.tick(brain, now_ts=t0 + 30_600)) == []
    assert len(pings) == 1 and len(compacts) == 1


def test_cache_keepalive_session_not_warm_and_restart_hydration():
    mgr = CacheKeepaliveManager()
    pings: list[tuple[str, str]] = []
    compacts: list[tuple[str, str]] = []
    resets: list[tuple[str, str]] = []

    class _ColdDsh:
        async def keepalive_session(self, bid, src):
            pings.append((src, bid))
            return {'ok': True, 'skipped': True, 'reason': 'session_not_warm'}

        async def reset_session(self, bid, src):
            resets.append((src, bid))

    t0 = 50_000.0

    class _ColdBrain:
        def __init__(self):
            self.store = SimpleNamespace(
                config=Config(
                    cache_keepalive_enabled=True,
                    cache_keepalive_interval_sec=260,
                    cache_keepalive_max_pings=2,
                    idle_compact_minutes=9,
                    auto_compact_tokens=100_000,
                ),
                state={
                    'messages': [
                        {'role': 'user', 'bot_id': 'companion', 'source': 'phone', 'text': 'hi', 'created_at': t0 - 30},
                        {'role': 'assistant', 'bot_id': 'companion', 'source': 'phone', 'text': 'hello', 'created_at': t0},
                    ],
                },
            )
            self.last_prompt_tokens = {'phone:companion': 92_000}
            self.last_cached_tokens = {'phone:companion': 70_000}
            self.dsh_adapter = _ColdDsh()
            self.active_tasks_by_node_bot = {}
            self.executions_by_node_bot = {}

        def is_node_bot_busy(self, bid, src):
            return False

        async def _auto_compact_if_needed(self, bid, force=False, source='phone'):
            compacts.append((source, bid))
            return {'compacted': True, 'after_tokens': 11_000}

    brain = _ColdBrain()
    # 1. After restart (_sessions empty), tick() hydrates phone:companion from state + last_prompt_tokens!
    a1 = asyncio.run(mgr.tick(brain, now_ts=t0 + 265))
    assert a1 and a1[0]['action'] == 'keepalive_ping'
    assert len(pings) == 1
    # 2. session_not_warm caps further pings, but MUST NOT set idle_slept=True!
    st = mgr.get_state('phone', 'companion')
    assert st is not None and st['idle_slept'] is False
    assert asyncio.run(mgr.tick(brain, now_ts=t0 + 470)) == []
    assert len(pings) == 1
    # 3. At 9m+5s, idle_hot_compact MUST still fire!
    a2 = asyncio.run(mgr.tick(brain, now_ts=t0 + 545))
    assert a2 and a2[0]['action'] == 'idle_hot_compact'
    assert len(compacts) == 1 and len(resets) == 1
    assert brain.last_prompt_tokens['phone:companion'] == 11_000


def test_v4046_compact_ts_idle_cancel_14tools_and_cloud_auto_release():
    from core.cloud_desktop_controller import CloudDesktopControllerMixin
    from core.subtask_runner import SUBTASK_PRESETS
    from core.tool_schemas import CANONICAL_TOOL_NAMES, normalize_tool_name
    from vault_manager import fmt_compact_ts

    # 1. Ultra-compact lossless timestamp (year+month+day+weekday+time)
    assert fmt_compact_ts('2026-11-25T22:35:00+08:00', include_time=True) == '26-11-25三22:35'
    assert fmt_compact_ts('2026-11-25T22:35:00+08:00', include_time=False) == '26-11-25三'

    # 2. 14 canonical tools, web retired -> cloud, subtask research -> cloud
    assert len(CANONICAL_TOOL_NAMES) == 14
    assert 'web' not in CANONICAL_TOOL_NAMES
    assert normalize_tool_name('web') == 'cloud'
    assert normalize_tool_name('web_search') == 'cloud'
    assert 'cloud' in SUBTASK_PRESETS['research'][0]
    assert 'web' not in SUBTASK_PRESETS['research'][0]

    # 3. Idle compaction cancels immediately on incoming user turn (zero commit, zero reset, stays active)
    async def _run_cancel():
        mgr = CacheKeepaliveManager()
        started = asyncio.Event()
        resets = []

        class _SlowBrain:
            def __init__(self):
                self.store = SimpleNamespace(
                    config=Config(cache_keepalive_enabled=True, idle_compact_minutes=9, auto_compact_tokens=100_000),
                    state={},
                )
                self.last_prompt_tokens = {'phone:companion': 88_000}
                self.last_cached_tokens = {}
                self.dsh_adapter = SimpleNamespace(reset_session=lambda b, s: resets.append((b, s)))
                self.active_tasks_by_node_bot = {}
                self.executions_by_node_bot = {}

            def is_node_bot_busy(self, bid, src):
                return False

            async def _auto_compact_if_needed(self, bid, force=False, source='phone', silent=True, cancel_check=None):
                started.set()
                await asyncio.sleep(5.0)
                return {'compacted': True}

        b = _SlowBrain()
        mgr.touch('phone', 'companion', now_ts=1000.0)
        tick_task = asyncio.create_task(mgr.tick(b, now_ts=1600.0))
        await started.wait()
        cancelled = await mgr.cancel_idle_compaction('phone', 'companion')
        assert cancelled is True
        actions = await tick_task
        assert actions and actions[0]['action'] == 'idle_compact_cancelled'
        assert resets == []
        assert mgr.get_state('phone', 'companion')['idle_slept'] is False

    asyncio.run(_run_cancel())

    # 4. Cloud search/navigate default fast-text CDP read + auto browser release when browser was closed
    calls: list[tuple[str, str, dict | None]] = []

    class _FakeClient:
        async def status(self, force=False):
            return {'windows': [], 'url': ''}

        async def call(self, method, path, body=None):
            calls.append((method, path, body))
            if path == '/search':
                return {'ok': True, 'title': 'Search', 'url': 'https://example.com', 'page_text': 'x' * 120, 'links': [{'t': 'a', 'u': 'https://example.com'}]}
            return {'ok': True}

    class _DummyCloud(CloudDesktopControllerMixin):
        def __init__(self):
            self.computer = SimpleNamespace(client=_FakeClient())
            self.harness = SimpleNamespace(root=Path('.'))

    res = asyncio.run(_DummyCloud()._tool_cloud_desktop({'op': 'search', 'query': 'hello'}))
    assert res['ok'] is True
    assert res['mode'] == 'fast_text_cdp'
    assert res['browser_released'] is True
    assert any(p == '/input' and (b or {}).get('kind') == 'close_window' for _, p, b in calls)


def test_subtask_presets_custom_tools_and_worker_model_routing(tmp_path: Path):
    import contextvars
    captured: list[dict] = []

    class _FakeLLM:
        async def complete_stream(self, **kwargs):
            captured.append(kwargs)
            return {'content': '子任务核查完毕：已确认目标文件。', 'tool_calls': []}

    class _FakeHarness:
        def all_tools(self):
            return [
                {'type': 'function', 'function': {'name': n, 'description': n, 'parameters': {'type': 'object', 'properties': {}}}}
                for n in ('read_file', 'search_files', 'web', 'memory', 'bash', 'phone', 'pc', 'cloud', 'reply')
            ]

    store = Store(tmp_path)
    store.update_config({'default_model': 'main-expensive', 'worker_model': 'cheap-worker'})
    brain = SimpleNamespace(
        store=store,
        llm=_FakeLLM(),
        harness=_FakeHarness(),
        context_search=None,
        bot_context=contextvars.ContextVar('b', default='companion'),
        source_context=contextvars.ContextVar('s', default='phone'),
        last_prompt_tokens={},
        last_cached_tokens={},
    )
    # 1. Preset `research` routes to cheap worker_model with lean toolset
    r1 = asyncio.run(execute_run_subtask(brain, {'goal': '查一下文档', 'preset': 'research'}))
    assert r1['ok'] is True
    assert captured[-1]['bot_model'] == 'cheap-worker'
    tool_names_1 = {t['function']['name'] for t in (captured[-1]['tools'] or [])}
    assert tool_names_1 == set(SUBTASK_PRESETS['research'][0])

    # 2. Parent main model assigns custom `tools=['bash', 'read_file']` + `use_main_model=True`
    r2 = asyncio.run(execute_run_subtask(brain, {'goal': '跑测试看日志', 'tools': ['bash', 'read_file'], 'use_main_model': True}))
    assert r2['ok'] is True
    assert captured[-1]['bot_model'] != 'cheap-worker'
    tool_names_2 = {t['function']['name'] for t in (captured[-1]['tools'] or [])}
    assert tool_names_2 == {'bash', 'read_file'}
    store.close()


def test_memory_tables_4way_reconciliation_vectors_and_unified_recall(tmp_path: Path):
    emb = _FakeEmbeddingEngine('local_gte_small_zh')
    tables = MemoryTablesStore(tmp_path / 'context_index.db', embedding_engine=emb)

    tables.set_core_block('identity_rules', '- 默认中文，自称余，称呼主人')
    assert '自称余' in tables.build_static_core_blocks_text()

    # 1. INSERT
    r1 = tables.upsert_fact_reconciled(
        entity='土区 Mastercard 5335',
        aliases=['5335', '土区卡'],
        attr='绑定账号',
        value='绑在苹果小号',
        ttl_type='permanent',
        search_cues='苹果 土区 续费',
    )
    assert r1['action'] == 'INSERT'

    # 2. NOOP on same value
    r2 = tables.upsert_fact_reconciled(entity='5335', attr='绑定账号', value='绑在苹果小号')
    assert r2['action'] == 'NOOP'

    # 3. UPDATE on superset
    r3 = tables.upsert_fact_reconciled(entity='土区卡', attr='绑定账号', value='绑在苹果小号（每月15日检查余额）')
    assert r3['action'] == 'UPDATE'

    # 4. SUPERSEDE on changed truth
    r4 = tables.upsert_fact_reconciled(entity='土区 Mastercard 5335', attr='绑定账号', value='已换绑到新美区主号')
    assert r4['action'] == 'SUPERSEDE'
    assert r4['superseded_id'] == r1['id']

    active_hits = tables.query_facts(entity='5335', include_history=False)
    assert len(active_hits) == 1
    assert active_hits[0]['value'] == '已换绑到新美区主号'

    hist_hits = tables.query_facts(entity='5335', include_history=True)
    assert len(hist_hits) == 2
    assert any(h['status'] == 'superseded' for h in hist_hits)

    # 5. Model switch reindex
    emb.model_id = 'local_dmeta_zh_small'
    reidx = tables.reindex_vectors()
    assert reidx['ok'] is True and reidx['indexed'] >= 1

    # 6. Unified recall prioritizes structured entity_facts
    recalled = unified_cross_compaction_recall(query='之前那个土区卡绑在哪个号？', memory_tables=tables)
    assert recalled and recalled[0]['source_type'] == 'entity_fact'
    assert '新美区主号' in recalled[0]['body']

    # 7. Explicit hard delete removes fact & vectors cleanly (both active and history)
    del_res = tables.delete_fact(entity='5335', attr='绑定账号', hard=True)
    assert del_res['ok'] is True and del_res['deleted'] == 2
    assert tables.query_facts(entity='5335', include_history=True) == []

    # 8. Explicit episode delete removes episode & vector
    ep_add = tables.add_episode(title='测试删除情景', summary='用于验证 delete_episode')
    assert ep_add['ok'] is True
    ep_del = tables.delete_episode(episode_id=ep_add['id'])
    assert ep_del['ok'] is True and ep_del['deleted'] == 1
    assert tables.query_episodes('测试删除情景') == []
    tables.close()


def test_reorganize_legacy_memory_and_gardener_and_slash_commands(tmp_path: Path):
    from core.context_search import ContextSearchEngine
    from core.memory_engine import MemoryEngine
    from vault_manager import Vault

    store = Store(tmp_path)
    vault = Vault(tmp_path / 'memory_vault')
    vault.write('profile/user_traits.md', '# 用户画像\n- 偏好简洁高效，讨厌废话\n')
    vault.write('experiences/exp_old_expired.md', '# 2024-01-05 临时验证码提醒\n今天临时收到验证码 123456\n')
    vault.write('experiences/exp_vps.md', '# VPS 部署规则\n生产 VPS 为 1C1G，向量内存需控制在 100MB 左右。\n')
    vault.write('notes/compact_20261120_01.md', '# 2026-11-20 压缩摘要\n完成云电脑接管修复与手机端画布优化。\n')

    cs = ContextSearchEngine(tmp_path / 'context_index.db')
    me = MemoryEngine(store, vault, context_search=cs)
    me.add_pin('绝不阉割 15 个正交工具', source='phone')
    fake_emb = _FakeEmbeddingEngine('local_gte_small_zh')
    me._embedding_engine = fake_emb
    me._memory_tables = MemoryTablesStore(me.unified_db_path, embedding_engine=fake_emb)

    brain = SimpleNamespace(store=store, vault=vault, memory_engine=me, llm=None)

    # 1. Dry-run preview via slash command
    dry_md = asyncio.run(try_handle_slash_command(brain, '/memory reorganize --dry-run', 'companion', 'phone', {'id': 'companion', 'name': '白珂'}))
    assert dry_md and '预演统计' in dry_md and '待归档压缩笔记' in dry_md

    # 2. Full reorganization (backup + migrate + archive legacy tracks + ledger + rollback)
    res = asyncio.run(reorganize_legacy_memory(brain, dry_run=False, use_llm=False))
    assert res['ok'] is True
    assert Path(res['backup_dir']).is_dir()
    assert res['legacy_compacts_migrated'] == 1
    assert (vault.root / '_legacy_archive' / res['backup_id'] / 'notes' / 'compact_20261120_01.md').is_file()
    assert (vault.root / '_legacy_archive' / res['backup_id'] / 'experiences' / 'exp_vps.md').is_file()
    assert (vault.root / 'profile' / 'memory_ledger.md').is_file()
    assert (vault.root / res['report_path']).is_file()
    assert res['reconciliation']['EXPIRED_SKIPPED'] >= 1
    assert res['tables']['active_facts'] >= 1
    assert res['tables']['active_episodes'] >= 1

    # 2.5 Test 1-hop entity relations + access_count reinforcement + one-click rollback
    from core.memory_migrator import rollback_memory_backup
    tables = me.get_memory_tables()
    tables.upsert_relation(src_entity='VPS 部署规则', relation='deployed_on', dst_entity='Tokyo-1C1G')
    rels = tables.query_relations(['VPS'])
    assert len(rels) == 1 and rels[0]['dst'] == 'Tokyo-1C1G'
    hits_before = tables.query_facts(query='VPS')
    assert hits_before and hits_before[0]['access_count'] == 0
    hits_after = tables.query_facts(query='VPS')
    assert hits_after and hits_after[0]['access_count'] >= 1

    rb = rollback_memory_backup(brain, backup_id=res['backup_id'])
    assert rb['ok'] is True and rb['restored_backup'] == res['backup_id']
    assert (vault.root / 'experiences' / 'exp_vps.md').is_file()
    # Re-run reorganize to continue gardener test cleanly
    res2 = asyncio.run(reorganize_legacy_memory(brain, dry_run=False, use_llm=False))
    assert res2['ok'] is True

    # 3. Gardener: retire expired ephemeral + dedup >0.92 + 4-tier episode pyramid (90d monthly & 365d yearly)
    tables = me.get_memory_tables()
    now = time.time()
    tables.upsert_fact_reconciled(entity='临时日程', attr='提醒', value='今晚取快递', ttl_type='ephemeral', expires_at=now - 10)
    tables.upsert_fact_reconciled(entity='VPS主机', attr='规格', value='1C1G内存控制100MB', ttl_type='project')
    tables.upsert_fact_reconciled(entity='云服务器VPS', attr='配置', value='1C1G内存控制100MB', ttl_type='project')
    old_ts = now - 100 * 86400
    for k in range(3):
        tables.add_episode(title=f'老情景{k}', summary=f'历史任务细节 {k}', occurred_at=old_ts + k * 60)
    yr_ts = now - 400 * 86400
    for k in range(2):
        tables.add_episode(title=f'去年里程碑{k}', summary=f'年度关键决定 {k}', occurred_at=yr_ts + k * 60)

    g = run_memory_gardener(brain, now_ts=now)
    assert g['ok'] is True
    assert g['expired_facts'] >= 1
    assert g['merged_duplicates'] >= 1
    assert g['rolled_up_episodes'] >= 5
    assert fake_emb.unloaded is True

    tables.close()
    cs.close()
    store.close()


def test_ttft_recall_single_embed_trivial_ack_and_nonblocking_cold_start(tmp_path: Path):
    calls: list[str] = []

    class _CountingEmbed(_FakeEmbeddingEngine):
        def embed_one(self, text: str, *, is_query: bool = False, non_blocking_warmup: bool = False) -> bytes:
            calls.append(text)
            return super().embed_one(text, is_query=is_query)

    emb = _CountingEmbed('local_gte_small_zh')
    tables = MemoryTablesStore(tmp_path / 'ttft.db', embedding_engine=emb)
    tables.upsert_fact_reconciled(entity='土区卡5335', attr='规则', value='每月15日检查余额')
    tables.add_episode(title='土区卡讨论', summary='确认5335绑在苹果小号')
    calls.clear()

    # 1. Trivial ack short-circuits with 0 embed calls
    assert unified_cross_compaction_recall(query='好的', memory_tables=tables) == []
    assert unified_cross_compaction_recall(query='ok', memory_tables=tables) == []
    assert len(calls) == 0

    # 2. Real query computes embedding EXACTLY ONCE shared across facts + episodes
    hits = unified_cross_compaction_recall(query='土区卡5335怎么续费？', memory_tables=tables)
    assert hits and len(calls) == 1

    # 3. Cold EmbeddingEngine with non_blocking_warmup=True returns immediately (0ms)
    real_eng = EmbeddingEngine(tmp_path / 'm', config_getter=lambda: Config())
    t0 = time.perf_counter()
    assert real_eng.embed_one('查询测试', is_query=True, non_blocking_warmup=True) is None
    assert (time.perf_counter() - t0) < 0.05
    tables.close()


def test_async_subtask_fire_and_forget_gc_and_unified_peek(tmp_path: Path):
    from core.blackboard_peek import peek_bots_snapshot
    from core.subtask_store import SubtaskStore

    sub_store = SubtaskStore(tmp_path / 'sub.db')
    # Create 15 finished runs for companion + 1 running run -> GC keeps running + caps finished at 12
    for i in range(15):
        tid = f'sub-old-{i:02d}'
        sub_store.create_run(task_id=tid, title=f'旧任务{i}', preset='research', goal='g', parent_bot_id='companion')
        sub_store.finish_run(tid, status='completed', result_summary=f'done {i}')
    sub_store.create_run(task_id='sub-live-1', title='排查登录报错', preset='code_audit', goal='检查 auth.py', parent_bot_id='companion')
    sub_store.append_step('sub-live-1', tool='read_file', stage='步骤 1', args='{"path":"auth.py"}', ok=True, result='read 120 lines')
    sub_store.append_step('sub-live-1', tool='bash', stage='步骤 2', args='{"command":"pytest"}', ok=False, error='AssertionError: token expired')

    removed = sub_store.gc_runs(max_per_bot=10, max_total=20)
    assert len(removed) == 5
    live = sub_store.get_run('sub-live-1')
    assert live is not None and live['status'] == 'running'
    assert live['last_error'] == 'AssertionError: token expired'

    # Unified peek exposes both bots AND subtasks with latest 3 steps + full errors
    store = Store(tmp_path)
    store.state['assistants'] = [{'id': 'companion', 'name': '白珂', 'home_node': 'phone'}]
    fake_brain = SimpleNamespace(
        store=store,
        subtask_store=sub_store,
        executions_by_node_bot={
            'phone:companion': {
                'tool': 'bash',
                'trace': [
                    {'tool': 'read_file', 'args': {'path': 'a.py'}, 'result': {'ok': True}},
                    {'tool': 'bash', 'args': {'command': 'bad'}, 'result': {'ok': False, 'error': 'exit code 2'}, 'status': 'error'},
                ],
            }
        },
        streams_by_node_bot={'phone:companion': {'text': '正在检查', 'thought': '看日志'}},
        is_node_bot_busy=lambda bid, nk: bid == 'companion' and nk == 'phone',
    )
    fake_engine = SimpleNamespace(store=store, brain=fake_brain, _posts=lambda: [])
    snap = peek_bots_snapshot(fake_engine)
    assert snap['ok'] is True
    assert snap['subtasks_count'] >= 1
    s_live = next(s for s in snap['subtasks'] if s['task_id'] == 'sub-live-1')
    assert s_live['title'] == '排查登录报错'
    assert s_live['parent_bot_id'] == 'companion'
    assert s_live['last_error'] == 'AssertionError: token expired'
    assert len(s_live['recent_steps']) == 2 and s_live['recent_steps'][-1]['error'] == 'AssertionError: token expired'
    bot_comp = next(b for b in snap['bots'] if b['id'] == 'companion')
    assert bot_comp['last_error'] == 'exit code 2'
    assert len(bot_comp['recent_steps']) == 2

    # Verify true fire-and-forget async spawn (<100ms return) + parent binding + cancel
    async def _test_async_spawn():
        import contextvars
        from core.subtask_runner import cancel_subtask, execute_run_subtask

        async def _slow_complete(msgs, tools=None, big=False, **kw):
            await asyncio.sleep(0.25)
            return {'content': '异步调研结论 OK', 'tool_calls': []}

        b_ctx = contextvars.ContextVar('b_ctx', default='companion')
        s_ctx = contextvars.ContextVar('s_ctx', default='phone')
        fake_harness = SimpleNamespace(all_tools=lambda: [], execute=None)
        async_brain = SimpleNamespace(
            store=store,
            subtask_store=sub_store,
            harness=fake_harness,
            bot_context=b_ctx,
            source_context=s_ctx,
            last_prompt_tokens={},
            last_cached_tokens={},
            complete=_slow_complete,
        )
        t0 = time.perf_counter()
        sp = await execute_run_subtask(async_brain, {'goal': '并发检查线上日志', 'title': '线上日志排查'})
        dt = time.perf_counter() - t0
        assert sp['ok'] is True and sp['async'] is True
        assert sp['title'] == '线上日志排查'
        assert sp['parent_bot_id'] == 'companion'
        assert dt < 0.20

        # Live steer injects user message immediately without losing task
        from core.subtask_runner import steer_subtask
        st_r = await steer_subtask(async_brain, sp['task_id'], '重点只看 5xx 错误')
        assert st_r['ok'] is True and st_r['steered'] is True
        msgs_now = st_r['subtask']['messages']
        assert any(m['role'] == 'user' and '5xx' in m['text'] for m in msgs_now)

        c_res = await cancel_subtask(async_brain, sp['task_id'])
        assert c_res['ok'] is True and c_res['subtask']['status'] == 'cancelled'

        # Verify long conclusions (>3000 chars) are NEVER truncated at 1400 chars
        long_text = '完整调研结论段落：' + ('A' * 3500)
        sub_store.finish_run(sp['task_id'], status='completed', result_summary=long_text)
        got = sub_store.get_run(sp['task_id'])
        assert got is not None and len(got['result_summary']) == len(long_text)

    asyncio.run(_test_async_spawn())
    sub_store.close()
    store.close()


def test_memory_vector_space_projection_probe_and_sleep_consolidate_sync(tmp_path: Path):
    import json
    from core.context_search import ContextSearchEngine
    from core.memory_engine import MemoryEngine
    from core.memory_vector_space import build_vector_space_payload
    from vault_manager import Vault

    store = Store(tmp_path / 'vec_space_state')
    vault = Vault(tmp_path / 'vec_space_state' / 'memory_vault')
    cs = ContextSearchEngine(tmp_path / 'vec_space_state' / 'context_index.db')
    me = MemoryEngine(store=store, vault=vault, context_search=cs)
    fake_emb = _FakeEmbeddingEngine('local_gte_small_zh')
    me._embedding_engine = fake_emb

    assert me.get_sleep_consolidate_idle_seconds() == 480
    store.config.sleep_consolidate_idle_seconds = 600
    assert me.get_sleep_consolidate_idle_seconds() == 600
    assert '45 秒' not in me.build_prompt_context()

    tables = me.get_memory_tables()
    f1 = tables.upsert_fact_reconciled(entity='VirtualDisplay', attr='修复方案', value='修复 VirtualDisplay displayId 177 叠层泄漏', ttl_type='project', search_cues='RootDisplayServer Shizuku')
    f2 = tables.upsert_fact_reconciled(entity='VirtualDisplay', attr='无Root支持', value='Shizuku 和无线 ADB uid 2000 驱动后台虚拟屏', ttl_type='project', search_cues='Shizuku VirtualDisplay')
    f3 = tables.upsert_fact_reconciled(entity='记忆压缩', attr='时序配置', value='8分钟闲置整理错开9分钟空闲热压缩', ttl_type='permanent', search_cues='KV Cache 向量')
    e1 = tables.add_episode(title='虚拟屏排障实录', summary='定位了 PrivilegeManager 重连导致 VirtualDisplay 叠层问题')
    assert f1['ok'] and f2['ok'] and f3['ok'] and e1['ok']

    # 1. Base vector space returns normalized 2D coordinates, real vector edges and keyword centroids
    payload = build_vector_space_payload(me, query='', limit=100)
    assert payload['ok'] is True
    assert len(payload['nodes']) == 4
    assert payload['stats']['vectorized_nodes'] == 4
    assert all(0.0 <= nd['x'] <= 1.0 and 0.0 <= nd['y'] <= 1.0 for nd in payload['nodes'])
    assert len(payload['edges']) >= 1
    assert any(e['kind'] == 'vector' and e['weight'] > 0.3 for e in payload['edges'])
    assert any(k['word'] == 'VirtualDisplay' for k in payload['keywords'])

    # 2. Live query projects probe coordinates and ranks matching memories
    probed = build_vector_space_payload(me, query='VirtualDisplay 虚拟屏 Shizuku', limit=100)
    assert probed['probe'] is not None
    assert probed['probe']['mode'] == 'vector+keyword'
    assert probed['probe']['matches'] and probed['probe']['matches'][0]['score'] > 0.3

    # 3. sleep_time_consolidate incrementally syncs new traits/experiences into entity_facts + vectors
    now_ts = time.time()
    store.state['messages'].extend([
        {'id': 'u1', 'role': 'user', 'text': '以后发版前一定要先查脱敏再推公开仓', 'created_at': now_ts - 700},
        {'id': 'a1', 'role': 'assistant', 'text': '收到主人，记住了', 'created_at': now_ts - 690},
    ])
    store.state['last_user'] = now_ts - 650
    store.state['last_sleep_consolidate'] = 0.0

    async def _fake_consolidate_llm(_msgs):
        return {'content': json.dumps({
            'new_traits': ['发版前必须先做全仓脱敏检查再推送公开仓库'],
            'experiences': [{'topic': '公开发版流程', 'kind': 'experience', 'l0': '脱敏后单提交同步公开仓', 'l1': '创建公开 GitHub Release 附带 APK'}],
        }, ensure_ascii=False)}

    asyncio.run(me.sleep_time_consolidate(_fake_consolidate_llm))
    synced = tables.query_facts(query='脱敏')
    assert synced and any('脱敏' in r['value'] for r in synced)

    # 4. Token stats never cross-wire pc_* bots to phone:0 and sanitize merges sticker-split thoughts
    me.last_prompt_tokens['pc:pc_engineer'] = 196454
    me.last_cached_tokens['pc:pc_engineer'] = 195000
    pc_st = me.get_context_stats('pc_engineer')
    assert pc_st['current_tokens'] == 196454 and pc_st['source'] == 'pc'

    from core.brain_prompt_mixin import BrainPromptMixin
    dummy = SimpleNamespace(_extract_all_tags=lambda s, queue_media=False: (s, [], '', ''), strip_trailing_partial_control_tag=lambda s: s)
    sanitized = BrainPromptMixin._sanitize_timeline_entries(dummy, [
        {'type': 'thought', 'text': '先发个贴纸卖个萌'},
        {'type': 'tool', 'tool': 'reply', 'arguments': {'op': 'sticker', 'code': 'cat_hi'}, 'status': 'done'},
        {'type': 'thought', 'text': '再回答主人的问题'},
        {'type': 'text', 'text': '主人好呀喵'},
    ])
    thoughts = [x for x in sanitized if x.get('type') == 'thought']
    assert len(thoughts) == 1 and '先发个贴纸卖个萌' in thoughts[0]['text'] and '再回答主人的问题' in thoughts[0]['text']
    assert not any(x.get('type') == 'tool' for x in sanitized)

    tables.close()
    cs.close()
    store.close()


def test_v4046_prompt_time_anchor_and_silent_manual_compaction(tmp_path):
    """Verify v4.0.46 user message compact timestamp anchor, silent compaction, and run_manual_compaction."""
    import inspect
    from contextvars import ContextVar
    from core.brain_prompt_mixin import BrainPromptMixin
    from vault_manager import fmt_compact_ts

    sig = inspect.signature(BrainPromptMixin._auto_compact_if_needed)
    assert 'cancel_check' in sig.parameters
    assert sig.parameters['silent'].default is True

    published = []
    cancelled_idle = []
    reset_calls = []

    class _FakeKeepalive:
        async def cancel_idle_compaction(self, src, bid):
            cancelled_idle.append((src, bid))
        def mark_compacted(self, src, bid):
            pass

    class _FakeDsh:
        async def reset_session(self, bid, src):
            reset_calls.append((bid, src))

    class _FakeMemEngine:
        async def auto_compact_if_needed(self, **kwargs):
            assert kwargs.get('on_stream') is None, 'silent compaction must NOT emit streaming bubble'
            assert kwargs.get('force') is True
            return {
                'compacted': True,
                'already_compacted': False,
                'after_tokens': 1234,
                'marker': {'id': 'm-compact', 'role': 'assistant', 'kind': 'compact_summary', 'text': 'hidden'},
            }
        def build_static_pinned_block(self):
            return ''
        def get_all_bots_context_stats(self, source='phone'):
            return {'companion': {'current_tokens': 1234}}

    class _DummyBrain(BrainPromptMixin):
        def __init__(self):
            self.source_context = ContextVar('src', default='phone')
            self.bot_context = ContextVar('bot', default='companion')
            self._active_manual_compact_tasks = {}
            self._bot_locks = {}
            self.last_prompt_tokens = {'phone:companion': 88000}
            self.last_cached_tokens = {'phone:companion': 80000}
            self.streams_by_node_bot = {}
            self.streams_by_bot = {}
            self.stream_seq = 0
            self.store = SimpleNamespace(
                state={'assistants': [], 'last_prompt_tokens': {}, 'last_cached_tokens': {}},
                save=lambda: None,
            )
            self.harness = SimpleNamespace(system_instructions=lambda _c: 'sys')
            self.llm = SimpleNamespace()
            self.memory_engine = _FakeMemEngine()
            self.dsh_adapter = _FakeDsh()
            self._ka = _FakeKeepalive()

        def context(self):
            return ''

        def _get_cache_keepalive(self):
            return self._ka

        def _get_bot_lock(self, bid, src):
            k = f'{src}:{bid}'
            if k not in self._bot_locks:
                self._bot_locks[k] = asyncio.Lock()
            return self._bot_locks[k]

        async def publish(self, ev):
            published.append(ev)

    b = _DummyBrain()
    res = asyncio.run(b.run_manual_compaction('companion', source='phone'))
    assert res and res['compacted'] is True
    assert cancelled_idle == [('phone', 'companion')]
    assert reset_calls == [('companion', 'phone')]
    assert published and all(e['type'] == 'context_compacted' for e in published)
    assert not any(e['type'] == 'message' for e in published)
    assert 'phone:companion' not in b._active_manual_compact_tasks

    # Verify compact timestamp format used in user message prefix
    ts_str = fmt_compact_ts('2026-11-25T22:35:00+08:00', include_time=True)
    assert ts_str == '26-11-25三22:35'
