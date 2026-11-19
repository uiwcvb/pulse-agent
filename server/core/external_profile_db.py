"""Standalone SQLite profile database & prompt pack builder for external AI consumption.

Maintains an isolated, desensitized read-only projection (`data/external_ai_profile.db`)
automatically synchronized from `memory_vault/profile/*.md` and explicit user overrides,
allowing zero-context external AIs (web chat or tool-enabled agents) to read user background,
communication preferences, and scene-specific prompt presets via scoped read-only tokens.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_DEFAULT_SCENES: tuple[tuple[str, str, str, str], ...] = (
    (
        'preset_general',
        'general',
        '通用搭档模式',
        (
            '你正在协助一位长期个人助手系统的主人。请直接进入主题，少说空话、不端架子、不做无关道德说教。'
            '先给可执行结论再补充必要依据；遇到拿不准的细节先核查事实或直接确认那一个参数，不要靠猜测编造。'
        ),
    ),
    (
        'preset_coding',
        'coding',
        '工程架构协作模式',
        (
            '进行开发/排障时：先核查真实文件与运行状态再下判断，优先做精准减法与根因修复而非堆叠临时补丁；'
            '改动后必须经过测试或端到端日志验证再报告完成，不谎报未验证的结论。'
        ),
    ),
    (
        'preset_concise',
        'concise',
        '极简高效模式',
        '用最精炼的中文分点回应，保留关键数字、路径与命令，避免冗余寒暄和复述问题。',
    ),
    (
        'preset_advisor',
        'advisor',
        '规划参谋模式',
        '结合主人当前的学业/职业主线、精力与既定安排给出现实可落地的建议，多角度权衡利弊但保持立场明确。',
    ),
)

_VAULT_SOURCE_MAP: tuple[tuple[str, str, str, float], ...] = (
    ('profile/personal_impression.md', 'general', '个人印象核心档案', 1.0),
    ('profile/user_traits.md', 'general', '长期偏好与行为特征', 0.92),
    ('profile/pinned_memories.md', 'general', '置顶长期准则', 0.98),
    ('profile/annoyance_valve.md', 'general', '沟通雷区与防打扰偏好', 0.95),
    ('profile/how_to_cool_down.md', 'general', '情绪与减压偏好', 0.80),
    ('profile/career.md', 'coding', '职业与技术主线', 0.90),
    ('profile/career_plan.md', 'coding', '职业发展规划', 0.85),
    ('profile/academic-record.md', 'schedule', '学业背景与成绩概况', 0.78),
    ('profile/course_schedule.md', 'schedule', '常规作息与课表', 0.75),
)

_SENSITIVE_LINE_RE = re.compile(
    r'(?:api[_-]?key|device[_-]?token|secret|password|passwd|private[_-]?key|sk-[A-Za-z0-9]{12,})',
    re.IGNORECASE,
)


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256((raw_token or '').encode('utf-8')).hexdigest()


def _sanitize_profile_markdown(raw: str, max_chars: int = 2400) -> str:
    """Drop empty placeholders (`待了解`) and strip any accidental credential-looking lines."""
    clean_lines: list[str] = []
    for ln in (raw or '').splitlines():
        s = ln.strip()
        if not s:
            continue
        if '(待了解)' in s or '（待了解）' in s:
            continue
        if _SENSITIVE_LINE_RE.search(s):
            continue
        clean_lines.append(ln.rstrip())
    text = '\n'.join(clean_lines).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 16].rstrip() + '\n...(已节选核心内容)'
    return text


def _split_markdown_sections(raw: str, fallback_title: str) -> list[tuple[str, str]]:
    """Split markdown by `## ` headings into `(section_title, section_body)` pairs."""
    sections: list[tuple[str, str]] = []
    cur_title = fallback_title
    buf: list[str] = []
    for ln in (raw or '').splitlines():
        m = re.match(r'^#{1,3}\s+(.+)$', ln.strip())
        if m:
            body = _sanitize_profile_markdown('\n'.join(buf), max_chars=1800)
            if body:
                sections.append((cur_title, body))
            cur_title = m.group(1).strip()[:80]
            buf = []
        else:
            buf.append(ln)
    tail = _sanitize_profile_markdown('\n'.join(buf), max_chars=1800)
    if tail:
        sections.append((cur_title, tail))
    return sections


class ExternalProfileDB:
    """Dedicated SQLite store for external AI user context & prompt presets."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._wal_ready = False
        self._last_vault_sig: tuple[tuple[str, int, int], ...] = ()
        self._init_schema()

    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=20.0, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA busy_timeout=4000;')
            if not self._wal_ready:
                conn.execute('PRAGMA journal_mode=WAL;')
                self._wal_ready = True
            conn.execute('PRAGMA synchronous=NORMAL;')
            yield conn
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS profile_cards (
                    card_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL DEFAULT 'general',
                    category TEXT NOT NULL DEFAULT '通用',
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    keywords TEXT NOT NULL DEFAULT '',
                    priority REAL NOT NULL DEFAULT 0.5,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    manual_override INTEGER NOT NULL DEFAULT 0,
                    source_path TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL DEFAULT 0.0
                );
                CREATE INDEX IF NOT EXISTS idx_ext_cards_scope ON profile_cards(scope, enabled, priority DESC);
                CREATE TABLE IF NOT EXISTS prompt_presets (
                    preset_id TEXT PRIMARY KEY,
                    scene TEXT NOT NULL,
                    title TEXT NOT NULL,
                    system_prompt TEXT NOT NULL,
                    updated_at REAL NOT NULL DEFAULT 0.0
                );
                CREATE TABLE IF NOT EXISTS share_tokens (
                    token_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    token_prefix TEXT NOT NULL DEFAULT '',
                    label TEXT NOT NULL DEFAULT '外部AI只读令牌',
                    scopes_json TEXT NOT NULL DEFAULT '["general","coding","schedule"]',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL DEFAULT 0.0,
                    last_used_at REAL NOT NULL DEFAULT 0.0
                );
                """
            )
            now = time.time()
            for pid, scene, title, sys_prompt in _DEFAULT_SCENES:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO prompt_presets(preset_id, scene, title, system_prompt, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (pid, scene, title, sys_prompt, now),
                )

    def _vault_signature(self, vault: Any) -> tuple[tuple[str, int, int], ...]:
        root = Path(getattr(vault, 'root', '.'))
        sigs: list[tuple[str, int, int]] = []
        for rel, _, _, _ in _VAULT_SOURCE_MAP:
            p = root / rel
            if p.is_file():
                try:
                    st = p.stat()
                    sigs.append((rel, int(st.st_mtime_ns), int(st.st_size)))
                except OSError:
                    pass
        return tuple(sigs)

    def ensure_synced_from_vault(self, vault: Any) -> int:
        """Incremental re-sync only when files under `memory_vault/profile/` changed."""
        if vault is None:
            return 0
        sig = self._vault_signature(vault)
        if sig and sig == self._last_vault_sig:
            return 0
        count = self.sync_from_vault(vault)
        self._last_vault_sig = sig
        return count

    def sync_from_vault(self, vault: Any) -> int:
        """Parse `memory_vault/profile/*.md` sections into desensitized `profile_cards`."""
        if vault is None:
            return 0
        now = time.time()
        synced = 0
        seen_auto_ids: list[str] = []
        with self._lock, self._conn() as conn:
            for rel, scope, base_cat, prio in _VAULT_SOURCE_MAP:
                raw = ''
                with contextlib.suppress(Exception):
                    raw = vault.read(rel) or ''
                if not raw.strip():
                    continue
                sections = _split_markdown_sections(raw, base_cat)
                if not sections:
                    continue
                # Keep profile compact: up to 8 sections per file, prioritizing non-empty dense blocks
                for idx, (sec_title, sec_body) in enumerate(sections[:8]):
                    cid = f"vault:{rel}:{hashlib.sha1(sec_title.encode('utf-8')).hexdigest()[:10]}"
                    seen_auto_ids.append(cid)
                    existing = conn.execute(
                        'SELECT manual_override FROM profile_cards WHERE card_id = ?',
                        (cid,),
                    ).fetchone()
                    if existing and int(existing['manual_override'] or 0) == 1:
                        continue
                    kw = ' '.join({sec_title, base_cat, scope, rel.rsplit('/', 1)[-1].replace('.md', '')})
                    conn.execute(
                        """
                        INSERT INTO profile_cards(card_id, scope, category, title, summary, keywords, priority, enabled, manual_override, source_path, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
                        ON CONFLICT(card_id) DO UPDATE SET
                            scope=excluded.scope,
                            category=excluded.category,
                            title=excluded.title,
                            summary=excluded.summary,
                            keywords=excluded.keywords,
                            priority=excluded.priority,
                            source_path=excluded.source_path,
                            updated_at=excluded.updated_at
                        """,
                        (cid, scope, base_cat, sec_title, sec_body, kw, max(0.2, prio - idx * 0.02), rel, now),
                    )
                    synced += 1
            if seen_auto_ids:
                placeholders = ','.join('?' for _ in seen_auto_ids)
                conn.execute(
                    f"DELETE FROM profile_cards WHERE manual_override = 0 AND source_path LIKE 'profile/%' AND card_id NOT IN ({placeholders})",
                    seen_auto_ids,
                )
        return synced

    def upsert_card(
        self,
        *,
        card_id: str | None = None,
        scope: str = 'general',
        category: str = '自定义',
        title: str,
        summary: str,
        keywords: str = '',
        priority: float = 0.95,
        enabled: bool = True,
    ) -> dict[str, Any]:
        cid = (card_id or '').strip() or ('custom:' + secrets.token_hex(6))
        clean_summary = _sanitize_profile_markdown(summary, max_chars=3000)
        now = time.time()
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO profile_cards(card_id, scope, category, title, summary, keywords, priority, enabled, manual_override, source_path, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'manual', ?)
                ON CONFLICT(card_id) DO UPDATE SET
                    scope=excluded.scope,
                    category=excluded.category,
                    title=excluded.title,
                    summary=excluded.summary,
                    keywords=excluded.keywords,
                    priority=excluded.priority,
                    enabled=excluded.enabled,
                    manual_override=1,
                    updated_at=excluded.updated_at
                """,
                (cid, scope.strip() or 'general', category.strip() or '自定义', title.strip()[:120], clean_summary, keywords.strip()[:240], float(priority), 1 if enabled else 0, now),
            )
            row = conn.execute('SELECT * FROM profile_cards WHERE card_id = ?', (cid,)).fetchone()
        return dict(row) if row else {'card_id': cid}

    def list_cards(
        self,
        *,
        scopes: list[str] | tuple[str, ...] | None = None,
        q: str = '',
        include_disabled: bool = False,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if not include_disabled:
            where.append('enabled = 1')
        if scopes:
            sc = [s.strip() for s in scopes if s and s.strip() and s.strip() != 'all']
            if sc:
                where.append(f"scope IN ({','.join('?' for _ in sc)})")
                params.extend(sc)
        if q.strip():
            like = f"%{q.strip()}%"
            where.append('(title LIKE ? OR summary LIKE ? OR category LIKE ? OR keywords LIKE ?)')
            params.extend([like, like, like, like])
        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                f'SELECT * FROM profile_cards {clause} ORDER BY priority DESC, updated_at DESC LIMIT ?',
                (*params, max(1, min(100, int(limit or 30)))),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_presets(self) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute('SELECT * FROM prompt_presets ORDER BY preset_id ASC').fetchall()
        return [dict(r) for r in rows]

    def upsert_preset(self, scene: str, title: str, system_prompt: str) -> dict[str, Any]:
        sc = (scene or 'general').strip().lower()[:32]
        pid = f'preset_{sc}'
        now = time.time()
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO prompt_presets(preset_id, scene, title, system_prompt, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(preset_id) DO UPDATE SET
                    scene=excluded.scene,
                    title=excluded.title,
                    system_prompt=excluded.system_prompt,
                    updated_at=excluded.updated_at
                """,
                (pid, sc, title.strip()[:80], system_prompt.strip()[:4000], now),
            )
            row = conn.execute('SELECT * FROM prompt_presets WHERE preset_id = ?', (pid,)).fetchone()
        return dict(row) if row else {'preset_id': pid, 'scene': sc}

    def create_or_rotate_token(
        self,
        *,
        label: str = '外部AI通用只读令牌',
        scopes: list[str] | None = None,
        revoke_previous: bool = False,
    ) -> dict[str, Any]:
        raw = 'ext_' + secrets.token_urlsafe(24)
        tid = 'tok_' + secrets.token_hex(6)
        sc = [s for s in (scopes or ['general', 'coding', 'schedule']) if s in ('general', 'coding', 'schedule')]
        if not sc:
            sc = ['general', 'coding']
        now = time.time()
        with self._lock, self._conn() as conn:
            if revoke_previous:
                conn.execute('UPDATE share_tokens SET enabled = 0')
            conn.execute(
                """
                INSERT INTO share_tokens(token_id, token_hash, token_prefix, label, scopes_json, enabled, created_at, last_used_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, 0.0)
                """,
                (tid, _hash_token(raw), raw[:10], (label or '外部AI通用只读令牌')[:80], json.dumps(sc), now),
            )
        return {'token_id': tid, 'token': raw, 'token_prefix': raw[:10], 'scopes': sc, 'created_at': now}

    def verify_token(self, raw_token: str) -> dict[str, Any] | None:
        if not raw_token or not raw_token.strip():
            return None
        th = _hash_token(raw_token.strip())
        with self._lock, self._conn() as conn:
            row = conn.execute(
                'SELECT * FROM share_tokens WHERE token_hash = ? AND enabled = 1',
                (th,),
            ).fetchone()
            if not row:
                return None
            conn.execute('UPDATE share_tokens SET last_used_at = ? WHERE token_id = ?', (time.time(), row['token_id']))
            res = dict(row)
        try:
            res['scopes'] = json.loads(res.get('scopes_json') or '["general","coding"]')
        except Exception:
            res['scopes'] = ['general', 'coding']
        return res

    def list_tokens(self) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                'SELECT token_id, token_prefix, label, scopes_json, enabled, created_at, last_used_at FROM share_tokens ORDER BY created_at DESC'
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            with contextlib.suppress(Exception):
                d['scopes'] = json.loads(d.pop('scopes_json', '[]'))
            out.append(d)
        return out

    def revoke_token(self, token_id: str) -> bool:
        with self._lock, self._conn() as conn:
            cur = conn.execute('UPDATE share_tokens SET enabled = 0 WHERE token_id = ?', (token_id,))
            return cur.rowcount > 0

    def render_external_payload(
        self,
        *,
        scene: str = 'general',
        q: str = '',
        allowed_scopes: list[str] | None = None,
        limit: int = 16,
    ) -> dict[str, Any]:
        req_scene = (scene or 'general').strip().lower()
        presets = {p['scene']: p for p in self.list_presets()}
        preset = presets.get(req_scene) or presets.get('general') or {
            'scene': 'general',
            'title': '通用模式',
            'system_prompt': _DEFAULT_SCENES[0][3],
        }
        if req_scene == 'all':
            wanted_scopes = allowed_scopes or ['general', 'coding', 'schedule']
        elif req_scene in ('general', 'coding', 'schedule'):
            wanted_scopes = [req_scene] if req_scene == 'general' else ['general', req_scene]
            if allowed_scopes is not None:
                wanted_scopes = [s for s in wanted_scopes if s in allowed_scopes]
        else:
            wanted_scopes = allowed_scopes or ['general', 'coding']
        cards = self.list_cards(scopes=wanted_scopes, q=q, limit=limit)
        return {
            'ok': True,
            'scene': req_scene,
            'query': q,
            'system_prompt': preset['system_prompt'],
            'preset_title': preset['title'],
            'cards_count': len(cards),
            'cards': [
                {
                    'card_id': c['card_id'],
                    'scope': c['scope'],
                    'category': c['category'],
                    'title': c['title'],
                    'summary': c['summary'],
                }
                for c in cards
            ],
        }

    def render_markdown_for_ai(
        self,
        *,
        scene: str = 'general',
        q: str = '',
        allowed_scopes: list[str] | None = None,
        share_url: str = '',
        search_url: str = '',
    ) -> str:
        payload = self.render_external_payload(scene=scene, q=q, allowed_scopes=allowed_scopes)
        lines = [
            f"# 用户外部 AI 认知上下文（场景：{payload['preset_title']}）",
            '',
            '## 1. 对你的协作准则（System Prompt）',
            payload['system_prompt'],
            '',
            '## 2. 用户画像与关键记忆事实',
        ]
        if not payload['cards']:
            lines.append('- （当前条件下暂无公开记忆卡片）')
        else:
            for c in payload['cards']:
                lines.append(f"### [{c['category']}] {c['title']} (`scope={c['scope']}`)")
                lines.append(c['summary'])
                lines.append('')
        if share_url or search_url:
            lines.append('## 3. 若需要按场景或关键词进一步检索（给支持工具的 AI）')
            if share_url:
                lines.append(f'- 切换场景（`general` / `coding` / `schedule` / `all`）：请求 `{share_url}&scene=coding`')
            if search_url:
                lines.append(f'- 按关键词检索：请求 `{search_url}&q=关键词`')
        return '\n'.join(lines).strip()

    def build_zero_context_bootstrap_prompt(
        self,
        *,
        base_url: str,
        raw_token: str,
        scene: str = 'general',
        inline_fallback: bool = True,
    ) -> str:
        """Build a self-contained prompt that any brand-new zero-context AI can use directly."""
        clean_base = (base_url or '').rstrip('/')
        profile_url = f"{clean_base}/api/share/profile?token={raw_token}&scene={scene}&format=md"
        search_url = f"{clean_base}/api/share/search?token={raw_token}"
        parts = [
            '你现在是第一次与我对话的全新 AI，此前对我没有任何了解。请按以下方式快速建立对我的认知并调整回答风格：',
            '',
            '### 方式 A（如果你支持网页访问 / HTTP 请求 / curl 工具，优先执行）',
            f'请立即读取我的实时只读个人上下文库：`{profile_url}`',
            f'（若后续对话涉及具体技术栈、课表或过往习惯，可随时按关键词请求 `{search_url}&q=关键词` 或把 `scene` 改为 `coding` / `all`）。',
            '读取后无需把整份档案背诵给我，直接在后续对话里自然遵循里面的偏好、习惯与语气规则即可。',
        ]
        if inline_fallback:
            compact_md = self.render_markdown_for_ai(
                scene=scene, allowed_scopes=['general', 'coding']
            )
            # Keep inline fallback concise (~2200 chars) so any chat box accepts it cleanly.
            if len(compact_md) > 2200:
                compact_md = compact_md[:2180].rstrip() + '\n...(完整版可通过上方方式 A 实时获取)'
            parts.extend([
                '',
                '### 方式 B（如果你当前环境无法联网，直接阅读下方内联核心档案与提示词）',
                compact_md,
            ])
        return '\n'.join(parts)
