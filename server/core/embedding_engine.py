"""
[Module Responsibility]
Lightweight Multi-Tier Embedding Engine (`server/core/embedding_engine.py`).

Supports 1C1G-friendly local INT8 ONNX Transformer inference (`onnxruntime` + `tokenizers`,
zero PyTorch dependency, `intra_op_num_threads=1`, lazy-load + 5-minute idle session unload)
as well as optional OpenAI-compatible `/v1/embeddings` API override and graceful FTS5 fallback.
"""
from __future__ import annotations

import gc
import hashlib
import logging
import math
import os
import struct
import threading
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

IDLE_UNLOAD_SECONDS = 300.0

EMBEDDING_TIERS: dict[str, dict[str, Any]] = {
    'off': {
        'id': 'off',
        'label': '关闭向量 (仅查表 + FTS5)',
        'mode': 'off',
        'dim': 0,
        'max_len': 0,
        'ram_mb': 0,
    },
    'local_gte_small_zh': {
        'id': 'local_gte_small_zh',
        'label': '轻量推荐 · gte-small-zh INT8 (~100MB, 检索 65.5)',
        'mode': 'local_onnx',
        'hf_repo': 'towing/gte-small-zh',
        'fallback_hf_repo': 'thenlper/gte-small-zh',
        'onnx_files': ('onnx/model_quantized.onnx', 'onnx/model.onnx', 'model_quantized.onnx'),
        'dim': 512,
        'max_len': 512,
        'pooling': 'mean',
        'query_prefix': '',
        'ram_mb': 105,
    },
    'local_bge_small_zh': {
        'id': 'local_bge_small_zh',
        'label': '超轻兼容 · bge-small-zh-v1.5 INT8 (~90MB, 检索 61.8)',
        'mode': 'local_onnx',
        'hf_repo': 'Xenova/bge-small-zh-v1.5',
        'fallback_hf_repo': 'BAAI/bge-small-zh-v1.5',
        'onnx_files': ('onnx/model_quantized.onnx', 'model_quantized.onnx', 'model_int8.onnx', 'model.onnx'),
        'dim': 512,
        'max_len': 512,
        'pooling': 'cls',
        'query_prefix': '为这个句子生成表示以用于检索相关文章：',
        'ram_mb': 90,
    },
    'local_dmeta_zh_small': {
        'id': 'local_dmeta_zh_small',
        'label': '1C1G高配 · Dmeta-embedding-zh-small INT8 (~170MB, 1024长文, 检索 67.7)',
        'mode': 'local_onnx',
        'hf_repo': 'baby2008/Dmeta-embedding-zh-onnx',
        'fallback_hf_repo': 'DMetaSoul/Dmeta-embedding-zh-small',
        'onnx_files': ('model_int8.onnx', 'onnx/model_quantized.onnx', 'model_quantized.onnx', 'model.onnx'),
        'dim': 768,
        'max_len': 1024,
        'pooling': 'mean',
        'query_prefix': '',
        'ram_mb': 170,
    },
    'local_gte_base_zh': {
        'id': 'local_gte_base_zh',
        'label': '2G+进阶 · bge-base-zh-v1.5 INT8 (~240MB, 检索 69.5)',
        'mode': 'local_onnx',
        'hf_repo': 'Xenova/bge-base-zh-v1.5',
        'fallback_hf_repo': 'BAAI/bge-base-zh-v1.5',
        'onnx_files': ('onnx/model_quantized.onnx', 'onnx/model_int8.onnx', 'model_quantized.onnx'),
        'dim': 768,
        'max_len': 512,
        'pooling': 'cls',
        'query_prefix': '为这个句子生成表示以用于检索相关文章：',
        'ram_mb': 240,
    },
    'api': {
        'id': 'api',
        'label': '外接 API (/v1/embeddings, 本地 0MB)',
        'mode': 'api',
        'dim': 0,
        'max_len': 8192,
        'ram_mb': 0,
    },
}


def pack_int8_vector(vec: list[float]) -> bytes:
    """L2-normalize and quantize a float vector into compact signed int8 blob (`float32 scale` + `int8[dim]`)."""
    if not vec:
        return b''
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    unit = [v / norm for v in vec]
    max_abs = max((abs(v) for v in unit), default=1.0) or 1.0
    scale = max_abs / 127.0
    q = bytes(max(-127, min(127, int(round(v / scale)))) & 0xFF for v in unit)
    return struct.pack('<f', scale) + q


def unpack_int8_vector(blob: bytes) -> list[float]:
    if not blob or len(blob) <= 4:
        return []
    scale = struct.unpack('<f', blob[:4])[0]
    raw = struct.unpack(f'<{len(blob) - 4}b', blob[4:])
    return [float(x) * scale for x in raw]


def cosine_similarity_blobs(blob_a: bytes, blob_b: bytes) -> float:
    """Fast cosine similarity between two int8-packed normalized vectors (uses numpy when available)."""
    if not blob_a or not blob_b or len(blob_a) != len(blob_b) or len(blob_a) <= 4:
        return 0.0
    sa = struct.unpack('<f', blob_a[:4])[0]
    sb = struct.unpack('<f', blob_b[:4])[0]
    try:
        import numpy as np  # type: ignore
        va = np.frombuffer(blob_a, dtype=np.int8, offset=4).astype(np.float32)
        vb = np.frombuffer(blob_b, dtype=np.int8, offset=4).astype(np.float32)
        return float(np.dot(va, vb) * sa * sb)
    except Exception:
        n = len(blob_a) - 4
        va = struct.unpack(f'<{n}b', blob_a[4:])
        vb = struct.unpack(f'<{n}b', blob_b[4:])
        return float(sum(x * y for x, y in zip(va, vb)) * sa * sb)


def text_sha1(text: str) -> str:
    return hashlib.sha1((text or '').strip().encode('utf-8')).hexdigest()


class EmbeddingEngine:
    """Lazy-loading, idle-unloading embedding engine for local ONNX INT8 or external API."""

    def __init__(self, models_dir: Path, config_getter=None, idle_unload_sec: float = IDLE_UNLOAD_SECONDS):
        self.models_dir = Path(models_dir)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self._config_getter = config_getter
        self.idle_unload_sec = max(30.0, float(idle_unload_sec))
        self._lock = threading.RLock()
        self._loaded_tier: str = ''
        self._session: Any = None
        self._tokenizer: Any = None
        self._last_used_at: float = 0.0
        self._last_error: str = ''
        self._warmup_inflight: bool = False
        self._query_cache: dict[tuple[str, str], tuple[float, bytes]] = {}

    def _cfg(self) -> Any:
        if callable(self._config_getter):
            try:
                return self._config_getter()
            except Exception:
                return None
        return None

    def active_tier(self) -> str:
        cfg = self._cfg()
        tier = str(getattr(cfg, 'embedding_tier', 'local_gte_small_zh') or 'local_gte_small_zh').strip().lower()
        return tier if tier in EMBEDDING_TIERS else 'local_gte_small_zh'

    def active_model_id(self) -> str:
        tier = self.active_tier()
        if tier == 'off':
            return 'off'
        if tier == 'api':
            cfg = self._cfg()
            m = str(getattr(cfg, 'embedding_api_model', '') or 'text-embedding-3-small').strip()
            return f'api:{m}'
        return tier

    def tier_dir(self, tier: str) -> Path:
        return self.models_dir / tier

    def _find_local_model_files(self, tier: str) -> tuple[Path | None, Path | None]:
        spec = EMBEDDING_TIERS.get(tier) or {}
        base = self.tier_dir(tier)
        if not base.exists():
            return None, None
        onnx_path = None
        for rel in spec.get('onnx_files', ('model_quantized.onnx', 'model_int8.onnx', 'model.onnx')):
            cand = base / rel
            if cand.is_file() and cand.stat().st_size > 100_000:
                onnx_path = cand
                break
        if onnx_path is None:
            for p in base.rglob('*.onnx'):
                if p.is_file() and p.stat().st_size > 100_000:
                    onnx_path = p
                    break
        tok_path = base / 'tokenizer.json'
        if not tok_path.is_file():
            tok_path = None
        return onnx_path, tok_path

    def is_local_model_installed(self, tier: str | None = None) -> bool:
        t = tier or self.active_tier()
        if t in ('off', 'api'):
            return True
        onnx_p, tok_p = self._find_local_model_files(t)
        return bool(onnx_p and tok_p)

    def maybe_unload_idle(self, now_ts: float | None = None) -> bool:
        now = float(now_ts if now_ts is not None else time.time())
        with self._lock:
            if self._session is None:
                return False
            if (now - self._last_used_at) < self.idle_unload_sec:
                return False
            self._session = None
            self._tokenizer = None
            self._loaded_tier = ''
            gc.collect()
            return True

    def unload(self) -> None:
        with self._lock:
            self._session = None
            self._tokenizer = None
            self._loaded_tier = ''
            gc.collect()

    def _load_local_session_locked(self, tier: str) -> bool:
        if self._session is not None and self._loaded_tier == tier:
            return True
        onnx_p, tok_p = self._find_local_model_files(tier)
        if not onnx_p or not tok_p:
            self._last_error = f'model_not_downloaded:{tier}'
            return False
        try:
            import onnxruntime as ort  # type: ignore
            from tokenizers import Tokenizer  # type: ignore
        except Exception as exc:
            self._last_error = f'runtime_missing:{exc}'
            return False
        try:
            os.environ.setdefault('MALLOC_ARENA_MAX', '2')
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 1
            opts.inter_op_num_threads = 1
            opts.enable_mem_pattern = False
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            sess = ort.InferenceSession(str(onnx_p), sess_options=opts, providers=['CPUExecutionProvider'])
            tok = Tokenizer.from_file(str(tok_p))
            max_len = int((EMBEDDING_TIERS.get(tier) or {}).get('max_len') or 512)
            tok.enable_truncation(max_length=max_len)
            tok.enable_padding(pad_id=0, pad_token='[PAD]')
            self._session = sess
            self._tokenizer = tok
            self._loaded_tier = tier
            self._last_used_at = time.time()
            self._last_error = ''
            return True
        except Exception as exc:
            self._last_error = str(exc)[:200]
            logger.warning('Failed to load ONNX embedding model %s: %s', tier, exc)
            return False

    def embed_texts(self, texts: list[str], *, is_query: bool = False) -> list[list[float]] | None:
        """Return float vectors for `texts`, or None if embeddings are disabled/unavailable."""
        clean = [str(t or '').strip() for t in (texts or [])]
        if not clean:
            return []
        tier = self.active_tier()
        if tier == 'off':
            return None
        if tier == 'api':
            return self._embed_via_api(clean)
        spec = EMBEDDING_TIERS.get(tier) or {}
        prefix = str(spec.get('query_prefix') or '') if is_query else ''
        inputs_txt = [f'{prefix}{t}' if prefix else t for t in clean]
        with self._lock:
            if not self._load_local_session_locked(tier):
                return None
            try:
                import numpy as np  # type: ignore
                encs = self._tokenizer.encode_batch(inputs_txt)
                input_ids = np.array([e.ids for e in encs], dtype=np.int64)
                attn_mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
                ort_inputs: dict[str, Any] = {'input_ids': input_ids, 'attention_mask': attn_mask}
                in_names = {inp.name for inp in self._session.get_inputs()}
                if 'token_type_ids' in in_names:
                    ort_inputs['token_type_ids'] = np.array([e.type_ids for e in encs], dtype=np.int64)
                outputs = self._session.run(None, ort_inputs)
                token_emb = outputs[0]
                if token_emb.ndim == 2:
                    pooled = token_emb
                elif spec.get('pooling') == 'mean':
                    mask_f = attn_mask[:, :, None].astype(np.float32)
                    pooled = (token_emb * mask_f).sum(axis=1) / np.clip(mask_f.sum(axis=1), 1e-9, None)
                else:
                    pooled = token_emb[:, 0, :]
                norms = np.linalg.norm(pooled, axis=1, keepdims=True)
                pooled = pooled / np.clip(norms, 1e-9, None)
                self._last_used_at = time.time()
                return [row.astype(float).tolist() for row in pooled]
            except Exception as exc:
                self._last_error = str(exc)[:200]
                logger.warning('ONNX embedding inference error (%s): %s', tier, exc)
                return None

    def is_loaded_for_active_tier(self) -> bool:
        tier = self.active_tier()
        if tier in ('off', 'api'):
            return tier == 'api'
        return bool(self._session is not None and self._loaded_tier == tier)

    def warmup_async(self) -> None:
        """Load the local ONNX session in a background daemon thread so TTFT is NEVER blocked on cold start."""
        tier = self.active_tier()
        if tier in ('off', 'api') or self.is_loaded_for_active_tier() or not self.is_local_model_installed(tier):
            return
        with self._lock:
            if self._warmup_inflight or self.is_loaded_for_active_tier():
                return
            self._warmup_inflight = True

        def _bg():
            try:
                with self._lock:
                    self._load_local_session_locked(tier)
            finally:
                with self._lock:
                    self._warmup_inflight = False

        threading.Thread(target=_bg, name='onnx-embed-warmup', daemon=True).start()

    def embed_one(self, text: str, *, is_query: bool = False, non_blocking_warmup: bool = False) -> bytes | None:
        clean = str(text or '').strip()
        if not clean:
            return None
        tier = self.active_tier()
        if tier == 'off':
            return None
        if is_query:
            ck = (self.active_model_id(), clean[:256])
            cached = self._query_cache.get(ck)
            if cached and (time.time() - cached[0]) < 30.0:
                return cached[1]
        if non_blocking_warmup and tier != 'api' and not self.is_loaded_for_active_tier():
            self.warmup_async()
            return None
        vecs = self.embed_texts([clean], is_query=is_query)
        if not vecs or not vecs[0]:
            return None
        blob = pack_int8_vector(vecs[0])
        if is_query and blob:
            if len(self._query_cache) >= 64:
                self._query_cache.clear()
            self._query_cache[(self.active_model_id(), clean[:256])] = (time.time(), blob)
        return blob

    def _embed_via_api(self, texts: list[str]) -> list[list[float]] | None:
        cfg = self._cfg()
        if cfg is None:
            return None
        base_url = (str(getattr(cfg, 'embedding_api_base_url', '') or '').strip() or cfg.effective_base_url()).rstrip('/')
        api_key = str(getattr(cfg, 'embedding_api_key', '') or '').strip() or cfg.effective_api_key(big=False)
        model = str(getattr(cfg, 'embedding_api_model', '') or 'text-embedding-3-small').strip()
        if not (base_url and api_key and model):
            self._last_error = 'missing_embedding_api_config'
            return None
        url = base_url if base_url.endswith('/embeddings') else f'{base_url}/embeddings'
        try:
            with httpx.Client(timeout=20.0, trust_env=False) as client:
                r = client.post(
                    url,
                    headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                    json={'model': model, 'input': texts},
                )
            if r.status_code != 200:
                self._last_error = f'api_http_{r.status_code}'
                return None
            data = r.json().get('data') or []
            data.sort(key=lambda x: int(x.get('index') or 0))
            return [list(map(float, item['embedding'])) for item in data if isinstance(item, dict) and item.get('embedding')]
        except Exception as exc:
            self._last_error = str(exc)[:180]
            return None

    def download_tier_model(self, tier: str | None = None, *, hf_endpoint: str = '') -> dict[str, Any]:
        """Download ONNX INT8 model + tokenizer.json for a local tier (supports HF mirror)."""
        t = (tier or self.active_tier()).strip().lower()
        spec = EMBEDDING_TIERS.get(t)
        if not spec or spec.get('mode') != 'local_onnx':
            return {'ok': False, 'error': f'非本地 ONNX 档位: {t}'}
        dest = self.tier_dir(t)
        dest.mkdir(parents=True, exist_ok=True)
        endpoint = (hf_endpoint or os.getenv('HF_ENDPOINT') or 'https://huggingface.co').rstrip('/')
        repo = spec['hf_repo']
        downloaded: list[str] = []
        with httpx.Client(timeout=180.0, follow_redirects=True, trust_env=False) as client:
            tok_url = f'{endpoint}/{repo}/resolve/main/tokenizer.json'
            r_tok = client.get(tok_url)
            if r_tok.status_code != 200 and spec.get('fallback_hf_repo'):
                r_tok = client.get(f"{endpoint}/{spec['fallback_hf_repo']}/resolve/main/tokenizer.json")
            if r_tok.status_code != 200:
                return {'ok': False, 'error': f'tokenizer.json 下载失败 (HTTP {r_tok.status_code})'}
            (dest / 'tokenizer.json').write_bytes(r_tok.content)
            downloaded.append('tokenizer.json')

            onnx_ok = False
            for rel in spec.get('onnx_files', ('onnx/model_quantized.onnx', 'model_quantized.onnx')):
                url = f'{endpoint}/{repo}/resolve/main/{rel}'
                with client.stream('GET', url) as r_m:
                    if r_m.status_code != 200:
                        continue
                    out_p = dest / Path(rel).name
                    with out_p.open('wb') as f:
                        for chunk in r_m.iter_bytes(65536):
                            f.write(chunk)
                    if out_p.stat().st_size > 100_000:
                        downloaded.append(out_p.name)
                        onnx_ok = True
                        break
            if not onnx_ok:
                return {'ok': False, 'error': f'未能在 {repo} 找到可用 ONNX INT8 权重'}
        return {'ok': True, 'tier': t, 'dir': str(dest), 'files': downloaded}

    def status(self) -> dict[str, Any]:
        tier = self.active_tier()
        spec = EMBEDDING_TIERS.get(tier) or {}
        return {
            'tier': tier,
            'model_id': self.active_model_id(),
            'label': spec.get('label', tier),
            'mode': spec.get('mode', 'off'),
            'installed': self.is_local_model_installed(tier),
            'loaded': bool(self._session is not None and self._loaded_tier == tier),
            'last_error': self._last_error,
            'available_tiers': [
                {'id': k, 'label': v['label'], 'ram_mb': v['ram_mb'], 'installed': self.is_local_model_installed(k)}
                for k, v in EMBEDDING_TIERS.items()
            ],
        }
