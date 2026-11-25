from __future__ import annotations
import contextlib, copy, json, re, secrets, threading, time
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, field_validator
from vault_manager import TZ, atomic_write, now_iso

class BrainConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base_url: str = 'https://api.openai.com/v1'
    api_key: str = Field('', max_length=4096)
    model: str = Field('', max_length=200)

    @field_validator('base_url')
    @classmethod
    def url(cls, v):
        from urllib.parse import urlsplit
        u = urlsplit(v)
        if u.scheme not in ('https', 'http') or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError('填写 OpenAI 兼容 API 的 HTTP(S) Base URL')
        return v.rstrip('/')

class SMTPConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    host: str = Field('', max_length=253)
    port: int = Field(587, ge=1, le=65535)
    username: str = Field('', max_length=320)
    password: str = Field('', max_length=4096)
    recipient: str = Field('', max_length=320)
    sender: str = Field('', max_length=320)
    security: str = 'starttls'

    @field_validator('security')
    @classmethod
    def mode(cls, v):
        if v not in ('starttls','ssl'): raise ValueError('SMTP 必须使用 STARTTLS 或 SSL')
        return v

    @field_validator('host','username','recipient','sender')
    @classmethod
    def no_headers(cls, v):
        if '\r' in v or '\n' in v: raise ValueError('字段包含非法换行')
        return v.strip()


def parse_channel_model(spec: str) -> tuple[str, str]:
    """Parse explicit channel-prefixed model spec: 'ch:<id>/<model>' or '<id>::<model>'."""
    raw = str(spec or '').strip()
    if not raw:
        return '', ''
    if raw.startswith('ch:') and '/' in raw[3:]:
        cid, mid = raw[3:].split('/', 1)
        cid, mid = cid.strip(), mid.strip()
        if cid and mid:
            return cid, mid
    if '::' in raw:
        cid, mid = raw.split('::', 1)
        cid, mid = cid.strip(), mid.strip()
        if cid and mid:
            return cid, mid
    return '', raw


def _mask_api_key(key: str) -> str:
    s = str(key or '').strip()
    if not s:
        return ''
    if len(s) <= 8:
        return '*' * len(s)
    return f'{s[:4]}...{s[-4:]}'


class ProviderChannel(BaseModel):
    model_config = ConfigDict(extra='ignore')
    id: str = Field('', max_length=64)
    name: str = Field('', max_length=80)
    base_url: str = Field('', max_length=500)
    api_key: str = Field('', max_length=4096)
    enabled: bool = True
    priority: int = Field(100, ge=0, le=1000)
    models: list[str] = Field(default_factory=list)

    @field_validator('id')
    @classmethod
    def clean_id(cls, v):
        s = re.sub(r'[^a-zA-Z0-9_-]', '', str(v or '').strip())
        return s[:64]

    @field_validator('base_url')
    @classmethod
    def validate_channel_url(cls, v):
        s = str(v or '').strip()
        return BrainConfig.url(s) if s else ''

    @field_validator('models', mode='before')
    @classmethod
    def clean_models(cls, v):
        if not isinstance(v, list):
            return []
        out: list[str] = []
        for item in v:
            s = str(item or '').strip()
            if s and s not in out:
                out.append(s)
        return out[:500]


class Config(BaseModel):
    model_config = ConfigDict(extra='ignore')
    api_base_url: str = 'https://api.openai.com/v1'
    api_key: str = Field('', max_length=4096)
    default_model: str = Field('', max_length=200)
    worker_model: str = Field('', max_length=200)
    embedding_tier: str = Field('local_gte_small_zh', max_length=64)
    embedding_api_base_url: str = Field('', max_length=500)
    embedding_api_key: str = Field('', max_length=4096)
    embedding_api_model: str = Field('', max_length=200)
    cache_keepalive_enabled: bool = True
    cache_keepalive_interval_sec: int = Field(260, ge=60, le=600)
    cache_keepalive_max_pings: int = Field(1, ge=0, le=5)
    idle_compact_minutes: int = Field(9, ge=0, le=120)
    sleep_consolidate_idle_seconds: int = Field(480, ge=60, le=3600)
    channels: list[ProviderChannel] = Field(default_factory=list)
    fallback_enabled: bool = False
    fallback_base_url: str = Field('', max_length=500)
    fallback_api_key: str = Field('', max_length=4096)
    fallback_model: str = Field('', max_length=200)
    small_brain: BrainConfig = Field(default_factory=BrainConfig)
    big_brain: BrainConfig = Field(default_factory=BrainConfig)
    smtp: SMTPConfig = Field(default_factory=SMTPConfig)
    enabled: bool = True
    curiosity_interval_minutes: int = Field(90, ge=5, le=1440)
    idle_research_minutes: int = Field(30, ge=5, le=1440)
    email_after_minutes: int = Field(15, ge=1, le=1440)
    overlay_after_minutes: int = Field(30, ge=2, le=2880)
    quiet_start_hour: int = Field(0, ge=0, le=23)
    quiet_end_hour: int = Field(8, ge=0, le=23)
    auto_compact_tokens: int = Field(100000, ge=8000, le=1000000)
    onboarding_completed: bool = False
    cloud_computer_mode: str = Field('', max_length=32)
    cloud_computer_host: str = Field('', max_length=255)
    cloud_computer_port: int = Field(22, ge=1, le=65535)
    cloud_computer_user: str = Field('', max_length=128)
    cloud_computer_password: str = Field('', max_length=1024)
    cloud_computer_ssh_key: str = Field('', max_length=16384)
    cloud_computer_url: str = Field('', max_length=500)
    cloud_computer_token: str = Field('', max_length=512)
    cloud_computer_status: str = Field('', max_length=64)

    @field_validator('api_base_url')
    @classmethod
    def validate_api_base_url(cls, v):
        return BrainConfig.url(v)

    @field_validator('fallback_base_url', 'embedding_api_base_url')
    @classmethod
    def validate_fallback_base_url(cls, v):
        s = (v or '').strip()
        return BrainConfig.url(s) if s else ''

    @field_validator('embedding_tier')
    @classmethod
    def validate_embedding_tier(cls, v):
        s = str(v or 'local_gte_small_zh').strip().lower()
        allowed = {'off', 'local_gte_small_zh', 'local_bge_small_zh', 'local_dmeta_zh_small', 'local_gte_base_zh', 'api'}
        return s if s in allowed else 'local_gte_small_zh'

    def _enabled_channels(self) -> list[ProviderChannel]:
        items = [ch for ch in (self.channels or []) if ch.enabled and (ch.base_url or '').strip() and (ch.api_key or '').strip()]
        return sorted(items, key=lambda c: (int(c.priority), c.id))

    def _legacy_base_url(self) -> str:
        if self.api_base_url and self.api_base_url != 'https://api.openai.com/v1':
            return self.api_base_url
        if self.big_brain.base_url and self.big_brain.base_url != 'https://api.openai.com/v1':
            return self.big_brain.base_url
        return self.small_brain.base_url or self.api_base_url or 'https://api.openai.com/v1'

    def _legacy_api_key(self, big: bool = False) -> str:
        primary = self.big_brain.api_key if big else self.small_brain.api_key
        return primary or self.api_key

    def effective_base_url(self) -> str:
        if self._legacy_api_key(big=True) or self._legacy_api_key(big=False):
            return self._legacy_base_url()
        enabled = self._enabled_channels()
        if enabled:
            return enabled[0].base_url
        return self._legacy_base_url()

    def effective_api_key(self, big: bool = False) -> str:
        legacy = self._legacy_api_key(big=big)
        if legacy:
            return legacy
        enabled = self._enabled_channels()
        if enabled:
            return enabled[0].api_key
        return ''

    def effective_model(self, bot_model: str = '', big: bool = False) -> str:
        if bot_model and bot_model.strip():
            return bot_model.strip()
        primary = self.big_brain.model if big else self.small_brain.model
        if primary:
            return primary
        if self.default_model:
            return self.default_model
        enabled = self._enabled_channels()
        for ch in enabled:
            if ch.models:
                return ch.models[0]
        return ''

    def effective_worker_model(self, bot_model: str = '') -> str:
        wm = str(self.worker_model or '').strip()
        return wm if wm else self.effective_model(bot_model=bot_model, big=False)

    def resolve_worker_route(self, bot_model: str = '') -> tuple[str, str, str, str]:
        wm = str(self.worker_model or '').strip()
        return self.resolve_route(bot_model=wm or bot_model, big=False)

    def resolve_route(self, bot_model: str = '', big: bool = False) -> tuple[str, str, str, str]:
        """Resolve (base_url, api_key, real_model_id, channel_id) with multi-channel routing."""
        raw_model = self.effective_model(bot_model=bot_model, big=big)
        if not raw_model:
            raw_model = self.effective_model(bot_model=bot_model, big=not big)
        explicit_cid, real_model = parse_channel_model(raw_model)
        enabled = self._enabled_channels()

        if explicit_cid:
            for ch in enabled:
                if ch.id == explicit_cid:
                    return ch.base_url.rstrip('/'), ch.api_key, real_model, ch.id
            for ch in (self.channels or []):
                if ch.id == explicit_cid and (ch.base_url or '').strip() and (ch.api_key or '').strip():
                    return ch.base_url.rstrip('/'), ch.api_key, real_model, ch.id

        if real_model:
            for ch in enabled:
                if real_model in (ch.models or []):
                    return ch.base_url.rstrip('/'), ch.api_key, real_model, ch.id

        legacy_key = self._legacy_api_key(big=big) or self._legacy_api_key(big=not big)
        if legacy_key:
            return self._legacy_base_url().rstrip('/'), legacy_key, real_model, ''

        if enabled:
            top = enabled[0]
            fallback_m = real_model or (top.models[0] if top.models else '')
            return top.base_url.rstrip('/'), top.api_key, fallback_m, top.id

        return self._legacy_base_url().rstrip('/'), '', real_model, ''

    def effective_fallback(self, big: bool = False) -> tuple[str, str, str]:
        if not self.fallback_enabled:
            return '', '', ''
        fb_raw_model = (self.fallback_model or '').strip()
        explicit_cid, real_fb_model = parse_channel_model(fb_raw_model)
        if explicit_cid:
            for ch in (self.channels or []):
                if ch.id == explicit_cid and (ch.base_url or '').strip() and (ch.api_key or '').strip():
                    return ch.base_url.rstrip('/'), ch.api_key, real_fb_model
        fb_url = (self.fallback_base_url or '').strip() or self.effective_base_url()
        fb_key = (self.fallback_api_key or '').strip() or self.effective_api_key(big=big)
        return fb_url.rstrip('/'), fb_key, real_fb_model

    def effective_fallback_chain(self, bot_model: str = '', big: bool = False) -> list[tuple[str, str, str, str]]:
        """Build ordered deduplicated [(base_url, api_key, real_model, channel_id), ...] candidates."""
        candidates: list[tuple[str, str, str, str]] = []
        seen: set[tuple[str, str]] = set()

        def _add(url: str, key: str, model: str, cid: str):
            u, k, m = (url or '').rstrip('/'), (key or '').strip(), (model or '').strip()
            if not (u and k and m):
                return
            sig = (u, m)
            if sig not in seen:
                seen.add(sig)
                candidates.append((u, k, m, cid))

        p_url, p_key, p_model, p_cid = self.resolve_route(bot_model=bot_model, big=big)
        _add(p_url, p_key, p_model, p_cid)

        # Same-model automatic failover across other enabled channels
        if p_model:
            for ch in self._enabled_channels():
                if ch.id != p_cid and p_model in (ch.models or []):
                    _add(ch.base_url, ch.api_key, p_model, ch.id)

        # Explicit fallback endpoint/model if enabled
        fb_url, fb_key, fb_model = self.effective_fallback(big=big)
        _add(fb_url, fb_key, fb_model, 'fallback')
        return candidates

    def public(self):
        data = self.model_dump()
        enabled_ch = self._enabled_channels()
        data['has_api_key'] = bool(data.pop('api_key', '') or self.small_brain.api_key or self.big_brain.api_key or enabled_ch)
        data['has_fallback_api_key'] = bool(data.pop('fallback_api_key', ''))
        data['has_embedding_api_key'] = bool(data.pop('embedding_api_key', ''))
        pub_channels = []
        for ch in (self.channels or []):
            pub_channels.append({
                'id': ch.id,
                'name': ch.name or ch.id,
                'base_url': ch.base_url,
                'enabled': bool(ch.enabled),
                'priority': int(ch.priority),
                'models': list(ch.models or []),
                'has_api_key': bool((ch.api_key or '').strip()),
                'api_key_masked': _mask_api_key(ch.api_key),
            })
        data['channels'] = pub_channels
        data.pop('cloud_computer_password', None)
        data.pop('cloud_computer_ssh_key', None)
        data.pop('cloud_computer_token', None)
        data['cloud_computer_configured'] = bool(
            (self.cloud_computer_host or '').strip() or (self.cloud_computer_url or '').strip()
        )
        data['api_base_url'] = self.effective_base_url()
        data['default_model'] = self.default_model or self.big_brain.model or self.small_brain.model
        data['fallback_enabled'] = bool(self.fallback_enabled)
        data['fallback_base_url'] = (self.fallback_base_url or '').strip()
        data['fallback_model'] = (self.fallback_model or '').strip()
        for key in ('small_brain','big_brain'):
            data[key]['has_api_key'] = bool(data[key].pop('api_key'))
        data['smtp']['has_password'] = bool(data['smtp'].pop('password'))
        return data

def normalize_node_source(raw: str | None) -> str:
    s = str(raw or '').strip().lower()
    if not s or s in ('phone', 'phone1', '安卓一', '安卓1', 'android', 'android1', '手机', '手机端', 'phone_dsb'):
        return 'phone'
    if s in ('phone2', '安卓二', '安卓2', 'android2', 'tablet', 'pad', '平板', '平板端'):
        return 'phone2'
    if s in ('pc', 'pc1', 'pc端', 'windows一', 'windows1', 'windows', 'dsh', 'pc_dsh', 'dsb', '电脑', '电脑端', 'deepseek-harness', 'harness'):
        return 'pc'
    if s in ('pc2', 'windows二', 'windows2'):
        return 'pc2'
    return s


def node_display_name(raw: str | None) -> str:
    norm = normalize_node_source(raw)
    if norm == 'phone':
        return '安卓一'
    if norm == 'phone2':
        return '安卓二'
    if norm == 'pc':
        return 'Windows一'
    if norm == 'pc2':
        return 'Windows二'
    return str(raw or '安卓一').strip()


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.workspace_dir = root / 'workspace'
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.config_path = root / 'config.json'
        self.state_path = root / 'state.json'
        self.db_path = root / 'pulse_state.db'
        self.settings_ws_path = self.workspace_dir / 'settings.json'
        self.bots_ws_path = self.workspace_dir / 'bots.json'
        self.routines_ws_path = self.workspace_dir / 'routines.json'
        self._settings_raw_text = ''
        self._bots_raw_text = ''
        self._routines_raw_text = ''
        self.config = Config.model_validate_json(self.config_path.read_text('utf-8')) if self.config_path.exists() else Config()

        from core.sqlite_state import SQLiteStateBackend
        self.sql = SQLiteStateBackend(self.db_path)

        def _default_state() -> dict:
            return {
                'messages': [],
                'pending': {},
                'routines': [],
                'opportunities': [],
                'deadlines': [],
                'blackboard_posts': [],
                'snooze_until': 0,
                'last_curiosity': 0,
                'last_research': 0,
                'last_user': time.time(),
                'last_error': '',
            }

        self.state = self.sql.load_or_migrate(self.state_path, _default_state)
        self._migrate_legacy_timers_once()
        self._scrub_state_bloat()
        self._sync_to_workspace()

    def _migrate_legacy_timers_once(self) -> None:
        """One-time migration of legacy active timers into unified blackboard_posts."""
        with self.lock:
            if self.state.get('_migrated_legacy_timers'):
                return
            timers = self.state.get('timers')
            if isinstance(timers, list) and timers:
                posts = self.state.setdefault('blackboard_posts', [])
                existing_ids = {str(p.get('post_id') or p.get('id') or '') for p in posts if isinstance(p, dict)}
                migrated_cnt = 0
                now_ts = time.time()
                for t in timers:
                    if not isinstance(t, dict) or t.get('done'):
                        continue
                    due_at = float(t.get('due_at') or t.get('fire_at') or 0.0)
                    if due_at <= now_ts - 3600.0:
                        continue
                    tid = str(t.get('id') or f'migrated-{secrets.token_hex(4)}')
                    pid = f'migrated-timer-{tid}'
                    if pid in existing_ids:
                        continue
                    goal = str(t.get('goal') or t.get('prompt') or t.get('title') or '定时提醒').strip()
                    bot_target = str(t.get('bot_id') or 'companion')
                    src_node = normalize_node_source(t.get('source') or 'phone')
                    exec_act = 'ring_alarm' if t.get('alarm') else 'wake_bot'
                    wake_md = 'silent' if t.get('silent') else 'proactive'
                    posts.append({
                        'post_id': pid,
                        'id': pid,
                        'title': goal[:60],
                        'content': goal,
                        'text': goal,
                        'author_id': bot_target,
                        'author_bot_id': bot_target,
                        'target_bot_id': bot_target,
                        'source': src_node,
                        'mentions': [bot_target],
                        'status': 'scheduled',
                        'trigger_at': due_at,
                        'due_at': due_at,
                        'exec_action': exec_act,
                        'wake_mode': wake_md,
                        'migrated_from_timer': True,
                        'created_at': float(t.get('created_at') or now_ts),
                        'updated_at': now_ts,
                        'replies': [],
                    })
                    existing_ids.add(pid)
                    migrated_cnt += 1
                self.state['timers'] = []
                if migrated_cnt > 0:
                    self.state['_migrated_timers_count'] = migrated_cnt
            # Idempotently backfill canonical keys on already-migrated legacy timer posts
            posts_all = self.state.get('blackboard_posts')
            if isinstance(posts_all, list):
                for p in posts_all:
                    if not isinstance(p, dict) or not p.get('migrated_from_timer'):
                        continue
                    bot_id = str(p.get('target_bot_id') or p.get('author_bot_id') or p.get('author_id') or 'companion')
                    p.setdefault('due_at', float(p.get('trigger_at') or 0.0))
                    p.setdefault('trigger_at', float(p.get('due_at') or 0.0))
                    p.setdefault('text', str(p.get('content') or p.get('title') or '定时提醒'))
                    p.setdefault('author_bot_id', str(p.get('author_id') or bot_id))
                    p.setdefault('target_bot_id', bot_id)
                    p.setdefault('exec_action', 'wake_bot')
                    p.setdefault('wake_mode', 'proactive')
            self.state['_migrated_legacy_timers'] = True
            self.sql.sync_state(self.state)

    def export_json_snapshot(self, target_path: Path | None = None) -> Path:
        """Explicitly export full state JSON snapshot for backup/diagnostics."""
        dst = Path(target_path) if target_path else self.state_path
        with self.lock:
            serialized = json.dumps(self.state, ensure_ascii=False, separators=(',', ':'))
            atomic_write(dst, serialized)
        return dst

    def _scrub_state_bloat(self, drop_caches: bool = False):
        with self.lock:
            changed = False
            now_ts = time.time()
            msgs = self.state.get('messages')
            if isinstance(msgs, list):
                for m in msgs:
                    if isinstance(m, dict):
                        if m.get('trace'):
                            old_tr = m['trace']
                            new_tr = compact_trace_list(old_tr, finalize=True)
                            if old_tr != new_tr:
                                m['trace'] = new_tr
                                changed = True
                        for blob_key in ('_multimodal_b64', 'image_b64', 'base64'):
                            if blob_key in m:
                                m.pop(blob_key, None)
                                changed = True
                # Never use a 1-by-1 sliding window (e.g. msgs[-240:]) because popping 1 message on every turn
                # shifts `messages[1:]` right after `messages[0]` (8,192 tokens) and destroys 100% of the conversation history Prefix Cache!
                # Normal compaction is handled by MemoryEngine.auto_compact_if_needed at `auto_compact_tokens` (120K~200K).
                # Only apply an emergency chunked ceiling if raw messages exceed 2,400 (slicing to 1,800 so prefix stays static for 600 turns).
                if len(msgs) > 2400:
                    self.state['messages'] = msgs[-1800:]
                    changed = True

            # Prune completed one-shot timers older than 1 hour (keep all active timers + at most 5 recent done timers)
            timers = self.state.get('timers')
            if isinstance(timers, list) and timers:
                active_t = [t for t in timers if isinstance(t, dict) and not t.get('done')]
                done_t = [
                    t for t in timers
                    if isinstance(t, dict) and t.get('done')
                    and (now_ts - float(t.get('due_at') or t.get('created_at') or 0)) <= 3600.0
                ][-5:]
                new_timers = active_t + done_t
                if len(new_timers) != len(timers):
                    self.state['timers'] = new_timers
                    changed = True

            # Prune notified deadlines older than 7 days
            deadlines = self.state.get('deadlines')
            if isinstance(deadlines, list) and deadlines:
                new_dl = [
                    d for d in deadlines
                    if isinstance(d, dict) and (not d.get('notified') or (now_ts - float(d.get('due_at') or 0)) <= 7 * 86400.0)
                ][-40:]
                if len(new_dl) != len(deadlines):
                    self.state['deadlines'] = new_dl
                    changed = True

            # Prune old opportunities beyond 30
            opps = self.state.get('opportunities')
            if isinstance(opps, list) and len(opps) > 30:
                self.state['opportunities'] = [o for o in opps if not o.get('done')][-30:]
                changed = True

            # Prune & compact notification_inbox (7-day TTL, max 100 items, slim history/fingerprints)
            inbox = self.state.get('notification_inbox')
            if isinstance(inbox, list) and inbox:
                cutoff_7d = now_ts - 7 * 86400.0
                kept_inbox = []
                for it in inbox:
                    if not isinstance(it, dict):
                        changed = True
                        continue
                    if float(it.get('timestamp') or 0) < cutoff_7d:
                        changed = True
                        continue
                    fps = it.get('fingerprints')
                    if isinstance(fps, list) and len(fps) > 15:
                        it['fingerprints'] = fps[-15:]
                        changed = True
                    hist = it.get('history')
                    if isinstance(hist, list) and len(hist) > 12:
                        it['history'] = hist[-12:]
                        changed = True
                    kept_inbox.append(it)
                if len(kept_inbox) > 100:
                    kept_inbox = kept_inbox[-100:]
                    changed = True
                if len(kept_inbox) != len(inbox):
                    changed = True
                self.state['notification_inbox'] = kept_inbox

            # Prune expired/acked pending escalation & overlay notif-* entries
            pend = self.state.get('pending')
            if isinstance(pend, dict) and pend:
                cutoff_pend = now_ts - 72 * 3600.0
                for pid_k, pv in list(pend.items()):
                    if not isinstance(pv, dict):
                        pend.pop(pid_k, None)
                        changed = True
                        continue
                    p_ts = float(pv.get('updated_at') or pv.get('created_at') or pv.get('ts') or 0.0)
                    st_p = str(pv.get('status') or '').lower()
                    if pv.get('acked') or st_p in ('acked', 'done', 'resolved', 'cancelled', 'expired') or (
                        str(pid_k).startswith('notif-') and p_ts > 0 and p_ts < cutoff_pend
                    ):
                        pend.pop(pid_k, None)
                        changed = True

            # Keep cached_vault_context intact across normal restarts so prefix KV cache stays 98%+ locked;
            # drop it only when explicit storage GC (drop_caches=True) runs.
            if drop_caches and 'cached_vault_context' in self.state:
                self.state.pop('cached_vault_context', None)
                changed = True
            if changed:
                self.sql.sync_state(self.state)

    def _sync_to_workspace(self, _from_sync: bool = False):
        """Write settings.json and bots.json into workspace so any Bot can inspect/edit its own setup."""
        with self.lock:
            if not _from_sync:
                ws_modified = (
                    (self.bots_ws_path.exists() and getattr(self, '_bots_mtime', 0) > 0 and (
                        self.bots_ws_path.stat().st_mtime_ns != getattr(self, '_bots_mtime', 0)
                        or ((time.time() - getattr(self, '_bots_write_ts', 0.0)) < 1.0 and self.bots_ws_path.read_text('utf-8') != getattr(self, '_bots_raw_text', ''))
                    ))
                    or (self.settings_ws_path.exists() and getattr(self, '_settings_mtime', 0) > 0 and (
                        self.settings_ws_path.stat().st_mtime_ns != getattr(self, '_settings_mtime', 0)
                        or ((time.time() - getattr(self, '_settings_write_ts', 0.0)) < 1.0 and self.settings_ws_path.read_text('utf-8') != getattr(self, '_settings_raw_text', ''))
                    ))
                )
                if ws_modified:
                    self.sync_from_workspace(force=True, _skip_ws_write=True)
            settings_payload = {
                'api_base_url': self.config.effective_base_url(),
                'api_key': '',
                'default_model': self.config.effective_model(),
                'small_brain': {
                    'base_url': self.config.small_brain.base_url,
                    'api_key': '',
                    'model': self.config.small_brain.model,
                    'has_api_key': bool(self.config.small_brain.api_key or self.config.api_key),
                },
                'big_brain': {
                    'base_url': self.config.big_brain.base_url,
                    'api_key': '',
                    'model': self.config.big_brain.model,
                    'has_api_key': bool(self.config.big_brain.api_key or self.config.api_key),
                },
                'fallback_enabled': bool(self.config.fallback_enabled),
                'fallback_base_url': self.config.fallback_base_url,
                'fallback_api_key': '',
                'fallback_model': self.config.fallback_model,
                'has_api_key': bool(self.config.effective_api_key()),
                'has_fallback_api_key': bool(self.config.fallback_api_key),
                'enabled': self.config.enabled,
                'curiosity_interval_minutes': self.config.curiosity_interval_minutes,
                'idle_research_minutes': self.config.idle_research_minutes,
                'quiet_start_hour': self.config.quiet_start_hour,
                'quiet_end_hour': self.config.quiet_end_hour,
                'auto_compact_tokens': self.config.auto_compact_tokens,
            }
            settings_json_str = json.dumps(settings_payload, ensure_ascii=False, indent=2)
            if settings_json_str != self._settings_raw_text or not self.settings_ws_path.exists():
                atomic_write(self.settings_ws_path, settings_json_str)
                self._settings_raw_text = settings_json_str
                self._settings_write_ts = time.time()
                self._settings_mtime = self.settings_ws_path.stat().st_mtime_ns if self.settings_ws_path.exists() else 0
            if 'assistants' in self.state:
                ws_bots = [copy.deepcopy(b) for b in self.state['assistants'] if isinstance(b, dict)]
                bots_json_str = json.dumps(ws_bots, ensure_ascii=False, indent=2)
                if bots_json_str != self._bots_raw_text or not self.bots_ws_path.exists():
                    atomic_write(self.bots_ws_path, bots_json_str)
                    self._bots_raw_text = bots_json_str
                    self._bots_write_ts = time.time()
                    self._bots_mtime = self.bots_ws_path.stat().st_mtime_ns if self.bots_ws_path.exists() else 0

    def sync_from_workspace(self, force: bool = False, _skip_ws_write: bool = False) -> bool:
        """If a Bot edited workspace/settings.json or bots.json via tools/bash, apply changes."""
        now_ts = time.time()
        recent_ws_write = (
            (now_ts - getattr(self, '_bots_write_ts', 0.0)) < 1.0
            or (now_ts - getattr(self, '_settings_write_ts', 0.0)) < 1.0
        )
        if not force and not recent_ws_write and (now_ts - getattr(self, '_last_ws_sync_ts', 0.0)) < 0.2:
            return False
        self._last_ws_sync_ts = now_ts
        changed = False
        bots_ws_changed = False
        routines_ws_changed = False
        pending_config_update = None
        bot_curiosity_interval: int | None = None
        with self.lock:
            try:
                if self.bots_ws_path.exists():
                    mtime = self.bots_ws_path.stat().st_mtime_ns
                    if force or recent_ws_write or mtime != getattr(self, '_bots_mtime', 0):
                        raw_text = self.bots_ws_path.read_text('utf-8')
                        if force or raw_text != getattr(self, '_bots_raw_text', ''):
                            self._bots_mtime = mtime
                            self._bots_raw_text = raw_text
                            raw = json.loads(raw_text)
                            if isinstance(raw, list) and raw:
                                from assistants import TOOL_NAMES
                                prev_by_id = {b.get('id'): b for b in (self.state.get('assistants') or []) if isinstance(b, dict)}
                                valid_bots = []
                                for item in raw[:30]:
                                    if isinstance(item, dict) and item.get('id') and item.get('name'):
                                        item.setdefault('title', '')
                                        item.setdefault('description', '')
                                        item.setdefault('instructions', '')
                                        item.setdefault('model', '')
                                        item.setdefault('engine', 'small')
                                        item.setdefault('color', '#7C5CFF')
                                        item.setdefault('shape', 'circle')
                                        item.setdefault('notify', True)
                                        item.setdefault('curiosity_enabled', item.get('id') == 'companion')
                                        item.setdefault('pinned', False)
                                        item.setdefault('hidden', False)
                                        item.setdefault('pinned_at', 0)
                                        item.setdefault('avatar_version', '')
                                        item.setdefault('tools', TOOL_NAMES.copy())
                                        prev_b = prev_by_id.get(item.get('id'))
                                        if isinstance(prev_b, dict):
                                            if not item.get('home_node') and prev_b.get('home_node'):
                                                item['home_node'] = prev_b['home_node']
                                            if isinstance(prev_b.get('node_settings'), dict):
                                                item.setdefault('node_settings', copy.deepcopy(prev_b['node_settings']))
                                        if isinstance(prev_b, dict) and isinstance(item.get('node_settings'), dict):
                                            for root_k in ('model', 'curiosity_enabled', 'notify'):
                                                if item.get(root_k) != prev_b.get(root_k):
                                                    for ns_v in item['node_settings'].values():
                                                        if isinstance(ns_v, dict) and ns_v.get(root_k) == prev_b.get(root_k):
                                                            ns_v[root_k] = item.get(root_k)
                                                else:
                                                    phone_ns = item['node_settings'].get('phone')
                                                    if isinstance(phone_ns, dict) and phone_ns.get(root_k) != prev_b.get(root_k):
                                                        item[root_k] = phone_ns.get(root_k)
                                        if isinstance(item.get('curiosity_interval_minutes'), (int, float)):
                                            c_int = max(1, min(1440, int(item.pop('curiosity_interval_minutes'))))
                                            if c_int != int(getattr(self.config, 'curiosity_interval_minutes', 30) or 30):
                                                bot_curiosity_interval = c_int
                                        valid_bots.append(item)
                                if valid_bots and valid_bots != self.state.get('assistants'):
                                    self.state['assistants'] = valid_bots
                                    changed = True
                                    bots_ws_changed = True
            except Exception:
                pass
            try:
                if self.settings_ws_path.exists():
                    mtime = self.settings_ws_path.stat().st_mtime_ns
                    if force or recent_ws_write or mtime != getattr(self, '_settings_mtime', 0):
                        raw_cfg_text = self.settings_ws_path.read_text('utf-8')
                        if force or raw_cfg_text != getattr(self, '_settings_raw_text', ''):
                            self._settings_mtime = mtime
                            self._settings_raw_text = raw_cfg_text
                            raw = json.loads(raw_cfg_text)
                            if isinstance(raw, dict):
                                update = {}
                                cur_dump = self.config.model_dump()
                                for k in ('api_base_url', 'api_key', 'default_model',
                                          'fallback_enabled', 'fallback_base_url', 'fallback_api_key', 'fallback_model',
                                          'enabled', 'curiosity_interval_minutes', 'idle_research_minutes',
                                          'quiet_start_hour', 'quiet_end_hour', 'auto_compact_tokens'):
                                    if k in raw:
                                        if k in ('api_key', 'fallback_api_key') and not str(raw[k] or '').strip():
                                            continue
                                        if raw[k] != cur_dump.get(k):
                                            update[k] = raw[k]
                                for brain_k in ('small_brain', 'big_brain'):
                                    if isinstance(raw.get(brain_k), dict):
                                        b_in = raw[brain_k]
                                        b_cur = cur_dump.get(brain_k) or {}
                                        b_up = {}
                                        for sub_k in ('base_url', 'model', 'api_key'):
                                            if sub_k in b_in:
                                                if sub_k == 'api_key' and not str(b_in[sub_k] or '').strip():
                                                    continue
                                                if b_in[sub_k] != b_cur.get(sub_k):
                                                    b_up[sub_k] = b_in[sub_k]
                                        if b_up:
                                            update[brain_k] = b_up
                                if update:
                                    pending_config_update = update
            except Exception:
                pass
            if bot_curiosity_interval is not None:
                pending_config_update = dict(pending_config_update or {})
                pending_config_update.setdefault('curiosity_interval_minutes', bot_curiosity_interval)
            if pending_config_update:
                self.update_config(pending_config_update, source_tag='workspace_sync')
                changed = True
            elif changed and not _skip_ws_write:
                self.save()
        if bots_ws_changed or routines_ws_changed:
            cb = getattr(self, 'on_workspace_synced', None)
            if cb:
                try:
                    cb(bots_ws_changed, routines_ws_changed)
                except Exception:
                    pass
        return changed

    def save(self):
        with self.lock:
            self.sql.sync_state(self.state)
            self._sync_to_workspace()

    def close(self) -> None:
        with self.lock:
            self.sql.close()

    def update_config(self, update: dict, source_tag: str = 'api'):
        changed_summary: dict[str, tuple[Any, Any]] = {}
        public_cfg: dict = {}
        with self.lock:
            prev_dump = self.config.model_dump()
            data = copy.deepcopy(prev_dump)
            # Synchronize top-level unified API config with small_brain & big_brain only when not explicitly overridden
            unified_url = update.get('api_base_url')
            unified_key = update.get('api_key')
            unified_model = update.get('default_model')
            has_explicit_small = isinstance(update.get('small_brain'), dict)
            has_explicit_big = isinstance(update.get('big_brain'), dict)
            for k, v in update.items():
                if k == 'channels' and isinstance(v, list):
                    prev_by_id = {
                        str(ch.get('id') or '').strip(): ch
                        for ch in (prev_dump.get('channels') or [])
                        if isinstance(ch, dict) and str(ch.get('id') or '').strip()
                    }
                    merged_channels = []
                    seen_cids: set[str] = set()
                    for item in v:
                        if not isinstance(item, dict):
                            continue
                        cid = re.sub(r'[^a-zA-Z0-9_-]', '', str(item.get('id') or '').strip())[:64]
                        if not cid:
                            cid = f'ch_{secrets.token_hex(3)}'
                        if cid in seen_cids:
                            continue
                        seen_cids.add(cid)
                        old_ch = prev_by_id.get(cid, {})
                        ch_merged = dict(old_ch)
                        ch_merged.update(item)
                        ch_merged['id'] = cid
                        if not str(ch_merged.get('api_key') or '').strip() and old_ch.get('api_key'):
                            ch_merged['api_key'] = old_ch['api_key']
                        if not str(ch_merged.get('name') or '').strip():
                            ch_merged['name'] = old_ch.get('name') or cid
                        merged_channels.append(ch_merged)
                    data['channels'] = merged_channels
                elif isinstance(v, dict) and isinstance(data.get(k), dict):
                    merged = dict(data[k])
                    for sub_k, sub_v in v.items():
                        if k in ('small_brain', 'big_brain'):
                            if sub_k in ('base_url', 'api_key', 'model'):
                                merged[sub_k] = sub_v
                        else:
                            merged[sub_k] = sub_v
                    data[k] = merged
                else:
                    data[k] = v
            if unified_url is not None:
                if not has_explicit_small or 'base_url' not in update['small_brain']:
                    data['small_brain']['base_url'] = unified_url
                if not has_explicit_big or 'base_url' not in update['big_brain']:
                    data['big_brain']['base_url'] = unified_url
            if unified_key is not None and unified_key != '':
                if not has_explicit_small or 'api_key' not in update['small_brain']:
                    data['small_brain']['api_key'] = unified_key
                if not has_explicit_big or 'api_key' not in update['big_brain']:
                    data['big_brain']['api_key'] = unified_key
            if unified_model is not None and unified_model != '':
                if not has_explicit_small or 'model' not in update['small_brain']:
                    data['small_brain']['model'] = unified_model
                if not has_explicit_big or 'model' not in update['big_brain']:
                    data['big_brain']['model'] = unified_model

            config = Config.model_validate(data)
            new_dump = config.model_dump()
            atomic_write(self.config_path, config.model_dump_json(indent=2))
            disk_cfg = json.loads(self.config_path.read_text('utf-8'))
            for vk in ('auto_compact_tokens', 'curiosity_interval_minutes', 'idle_research_minutes', 'quiet_start_hour', 'quiet_end_hour', 'default_model'):
                if disk_cfg.get(vk) != new_dump.get(vk):
                    raise RuntimeError(f'config.json readback verification mismatch on {vk}: expected={new_dump.get(vk)!r}, got={disk_cfg.get(vk)!r}')
            self.config = config
            self._sync_to_workspace(_from_sync=True)
            def _redact_sensitive_cfg(obj):
                if isinstance(obj, dict):
                    out_d = {}
                    for dk, dv in obj.items():
                        dkl = str(dk).lower()
                        if dkl != 'auto_compact_tokens' and ('key' in dkl or 'password' in dkl or 'secret' in dkl or 'token' in dkl):
                            out_d[dk] = '***' if dv not in (None, '') else dv
                        else:
                            out_d[dk] = _redact_sensitive_cfg(dv)
                    return out_d
                if isinstance(obj, list):
                    return [_redact_sensitive_cfg(x) for x in obj]
                return obj

            for k, new_v in new_dump.items():
                old_v = prev_dump.get(k)
                if new_v != old_v:
                    kl = str(k).lower()
                    if kl != 'auto_compact_tokens' and ('key' in kl or 'password' in kl or 'secret' in kl or 'token' in kl):
                        changed_summary[k] = ('***', '***')
                    else:
                        changed_summary[k] = (_redact_sensitive_cfg(old_v), _redact_sensitive_cfg(new_v))
            if changed_summary:
                try:
                    audit_p = self.settings_ws_path.parent / 'logs' / 'config_audit.md'
                    audit_p.parent.mkdir(parents=True, exist_ok=True)
                    ts_s = now_iso() if 'now_iso' in globals() else datetime.now(TZ).isoformat(timespec='seconds')
                    diff_parts = [f'{k}: {old_v!r} -> {new_v!r}' for k, (old_v, new_v) in changed_summary.items()]
                    line = f'- `{ts_s}` [{source_tag}] ' + '; '.join(diff_parts) + '\n'
                    if not audit_p.exists():
                        atomic_write(audit_p, '# 配置变更审计日志 (Config Audit Log)\n\n' + line)
                    else:
                        with audit_p.open('a', encoding='utf-8') as f:
                            f.write(line)
                except Exception:
                    pass
            public_cfg = config.public()
        cb = getattr(self, 'on_config_changed', None)
        if changed_summary and callable(cb):
            try:
                cb(public_cfg, source_tag, list(changed_summary.keys()))
            except Exception:
                pass
        return public_cfg

    def message(self, role, text, kind='chat', **extra):
        now_ts = time.time()
        clean_txt = str(text or '').strip()
        target_bot = extra.get('bot_id', 'companion')
        target_src = normalize_node_source(extra.get('source'))
        extra['source'] = target_src
        if 'trace' in extra:
            extra['trace'] = compact_trace_list(extra['trace'], finalize=True)
        with self.lock:
            if role == 'assistant' and clean_txt and self.state['messages']:
                for prev in reversed(self.state['messages'][-6:]):
                    prev_src = normalize_node_source(prev.get('source'))
                    if prev.get('bot_id', 'companion') == target_bot and prev_src == target_src:
                        if (
                            prev.get('role') == 'assistant'
                            and str(prev.get('text') or '').strip() == clean_txt
                            and abs(now_ts - float(prev.get('created_at') or 0)) < 15.0
                        ):
                            if extra.get('trace') and not prev.get('trace'):
                                prev['trace'] = extra['trace']
                            if extra.get('timeline') and not prev.get('timeline'):
                                prev['timeline'] = extra['timeline']
                            if extra.get('choices') and not prev.get('choices'):
                                prev['choices'] = extra['choices']
                            if extra.get('turn_id') and not prev.get('turn_id'):
                                prev['turn_id'] = extra['turn_id']
                            if extra.get('client_msg_id') and not prev.get('client_msg_id'):
                                prev['client_msg_id'] = extra['client_msg_id']
                            self.sql.upsert_message(prev)
                            self.save()
                            return prev
                        break
            raw_ts = float(extra.pop('created_at', now_ts) or now_ts)
            last_ts = float((self.state['messages'][-1] or {}).get('created_at') or 0) if self.state['messages'] else 0.0
            created_ts = max(raw_ts, last_ts + 0.001) if last_ts > 0 else raw_ts
            oseq = self.sql.alloc_order_seq()
            extra.pop('seq', None)
            m = {
                'id': secrets.token_hex(12),
                'role': role,
                'text': text,
                'kind': kind,
                'created_at': created_ts,
                'order_seq': oseq,
                'seq': oseq,
                **extra,
            }
            self.state['messages'].append(m)
            if len(self.state['messages']) > 2400:
                self.state['messages'] = self.state['messages'][-1800:]
                self.state['messages_epoch'] = int(self.state.get('messages_epoch') or 1) + 1
                self.sql.sync_state(self.state)
            else:
                self.sql.upsert_message(m)
                self.sql.sync_state(self.state)
        return m


from core.storage_hygiene import compact_trace_list, run_storage_hygiene, run_storage_maintenance  # noqa: E402, F401
