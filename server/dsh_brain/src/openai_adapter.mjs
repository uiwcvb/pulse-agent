import http from 'node:http';
import https from 'node:https';

const _httpKeepAliveAgent = new http.Agent({ keepAlive: true, keepAliveMsecs: 30000, maxSockets: 16 });
const _httpsKeepAliveAgent = new https.Agent({ keepAlive: true, keepAliveMsecs: 30000, maxSockets: 16 });
import {
  CONTEXT_WINDOW_EXCEEDED_CODE,
  INVALID_CREDENTIAL_CODE,
  LlmAdapter,
  LlmError,
  QUOTA_EXCEEDED_CODE,
  attributionHeaders,
  errorChain,
  fileHandleText,
  isContextWindowExceededError,
  isQuotaExceededError,
  offloadedImageText,
  textOnlyImageText,
} from '@deepseek-ai/dsh-llm';

function _isTimeoutError(err) {
  if (!err || typeof err !== 'object') return false;
  const name = String(err.name || '');
  const code = String(err.code || err.cause?.code || '');
  const msg = String(err.message || '');
  return (
    name === 'TimeoutError'
    || code === 'ETIMEDOUT'
    || code === 'UND_ERR_CONNECT_TIMEOUT'
    || code === 'UND_ERR_HEADERS_TIMEOUT'
    || code === 'UND_ERR_BODY_TIMEOUT'
    || /\btimed?\s*out\b/i.test(msg)
  );
}

function _parseRetryAfterMs(headerVal) {
  if (!headerVal) return undefined;
  const raw = String(headerVal).trim();
  if (!raw) return undefined;
  const secs = Number(raw);
  if (Number.isFinite(secs) && secs > 0) return Math.round(secs * 1000);
  const ts = Date.parse(raw);
  if (Number.isFinite(ts) && ts > Date.now()) return Math.round(ts - Date.now());
  return undefined;
}

function _classifyHttpError(status, errText, retryAfterHeader) {
  const detail = `HTTP ${status}: ${String(errText || '').slice(0, 400)}`;
  if (isContextWindowExceededError(detail)) {
    return new LlmError(detail, CONTEXT_WINDOW_EXCEEDED_CODE, { status });
  }
  if (isQuotaExceededError(detail)) {
    return new LlmError(detail, QUOTA_EXCEEDED_CODE, { status });
  }
  if (status === 429) {
    const providerRetryAfterMs = _parseRetryAfterMs(retryAfterHeader);
    return new LlmError(detail, 'RATE_LIMIT', {
      status,
      ...(providerRetryAfterMs ? { providerRetryAfterMs } : {}),
    });
  }
  if (status === 401 || status === 403) {
    return new LlmError(detail, INVALID_CREDENTIAL_CODE, { status });
  }
  if (status === 408) {
    return new LlmError(detail, 'TIMEOUT', { status });
  }
  if (status >= 500 || status === 409 || status === 425) {
    return new LlmError(detail, 'SERVER', { status });
  }
  return new LlmError(detail, 'BAD_REQUEST', { status });
}

function _classifyTransportError(err) {
  if (err instanceof LlmError) return err;
  const detail = errorChain(err) || (err instanceof Error ? err.message : String(err));
  const code = _isTimeoutError(err) ? 'TIMEOUT' : 'TRANSPORT';
  return new LlmError(detail, code, err instanceof Error ? { cause: err } : undefined);
}

/**
 * Node native `http`/`https` streaming fetch adapter that completely avoids Node 22
 * built-in undici WebStream `Parser.finish: assert(!this.paused)` process crashes
 * on upstream chunked SSE interruptions.
 */
function nodeHttpStreamFetch(rawUrl, init = {}) {
  return new Promise((resolve, reject) => {
    let parsed;
    try {
      parsed = new URL(rawUrl);
    } catch (e) {
      reject(e);
      return;
    }
    const isHttps = parsed.protocol === 'https:';
    const mod = isHttps ? https : http;
    const method = String(init.method || 'GET').toUpperCase();
    const headers = { 'Accept-Encoding': 'identity', ...(init.headers || {}) };
    const bodyText = init.body == null ? '' : String(init.body);
    if (bodyText && !headers['Content-Length'] && !headers['content-length']) {
      headers['Content-Length'] = Buffer.byteLength(bodyText, 'utf8');
    }

    let settled = false;
    const req = mod.request(
      {
        protocol: parsed.protocol,
        hostname: parsed.hostname,
        port: parsed.port || (isHttps ? 443 : 80),
        path: parsed.pathname + parsed.search,
        method,
        headers,
        agent: isHttps ? _httpsKeepAliveAgent : _httpKeepAliveAgent,
      },
      (res) => {
        if (settled) return;
        settled = true;
        const status = Number(res.statusCode || 500);
        const ok = status >= 200 && status < 300;
        const resHeaders = {
          get(name) {
            const v = res.headers[String(name || '').toLowerCase()];
            return Array.isArray(v) ? v.join(', ') : (v ?? null);
          },
        };

        let closed = false;
        let onAbortListener = null;
        const cleanupAbort = () => {
          if (onAbortListener && init.signal) {
            try { init.signal.removeEventListener('abort', onAbortListener); } catch {}
            onAbortListener = null;
          }
        };

        const bodyStream = new ReadableStream({
          start(controller) {
            res.on('data', (chunk) => {
              if (closed) return;
              try {
                controller.enqueue(new Uint8Array(chunk));
              } catch {
                closed = true;
                try { res.destroy(); } catch {}
              }
            });
            res.on('end', () => {
              cleanupAbort();
              if (closed) return;
              closed = true;
              try { controller.close(); } catch {}
            });
            res.on('error', (err) => {
              cleanupAbort();
              if (closed) return;
              closed = true;
              try { controller.error(err); } catch {}
            });
            if (init.signal) {
              onAbortListener = () => {
                if (closed) return;
                closed = true;
                const reason = init.signal.reason || new Error('Aborted');
                try { res.destroy(reason); } catch {}
                try { controller.error(reason); } catch {}
              };
              if (init.signal.aborted) {
                onAbortListener();
              } else {
                init.signal.addEventListener('abort', onAbortListener, { once: true });
              }
            }
          },
          cancel() {
            closed = true;
            cleanupAbort();
            try { res.destroy(); } catch {}
          },
        });

        resolve({
          ok,
          status,
          headers: resHeaders,
          body: bodyStream,
          async text() {
            const chunks = [];
            const r = bodyStream.getReader();
            const dec = new TextDecoder('utf-8');
            while (true) {
              const { value, done } = await r.read();
              if (done) break;
              if (value) chunks.push(dec.decode(value, { stream: true }));
            }
            chunks.push(dec.decode());
            return chunks.join('');
          },
          async json() {
            return JSON.parse(await this.text());
          },
        });
      },
    );

    req.on('error', (err) => {
      if (!settled) {
        settled = true;
        reject(err);
      }
    });

    if (init.signal) {
      if (init.signal.aborted) {
        settled = true;
        const reason = init.signal.reason || new Error('Aborted');
        req.destroy(reason);
        reject(reason);
        return;
      }
      init.signal.addEventListener('abort', () => {
        const reason = init.signal.reason || new Error('Aborted');
        req.destroy(reason);
        if (!settled) {
          settled = true;
          reject(reason);
        }
      }, { once: true });
    }

    if (bodyText) {
      req.write(bodyText, 'utf8');
    }
    req.end();
  });
}

/**
 * Dynamic OpenAI-compatible LLM adapter supporting arbitrary per-turn or per-agent
 * `{ baseUrl, apiKey, model, maxTokens }` routes (e.g. `https://api.example.com/v1`).
 */
export class OpenAiCompatibleDshAdapter extends LlmAdapter {
  /**
   * @param {import('@deepseek-ai/cordis').Context} ctx
   * @param {{ defaultBaseUrl?: string, defaultApiKey?: string, defaultModel?: string, fetchImpl?: typeof fetch }} [defaults]
   */
  constructor(ctx, defaults = {}) {
    super();
    this.ctx = ctx;
    this.fetchImpl = defaults.fetchImpl || nodeHttpStreamFetch;
    /** @type {Map<string, { baseUrl: string, apiKey: string, model: string, maxTokens?: number, timeoutMs?: number }>} */
    this.sessionRoutes = new Map();
    this.defaultRoute = {
      baseUrl: (defaults.defaultBaseUrl || process.env.OPENAI_BASE_URL || 'https://api.deepseek.com/v1').replace(/\/+$/, ''),
      apiKey: defaults.defaultApiKey || process.env.DEEPSEEK_API_KEY || '',
      model: defaults.defaultModel || 'deepseek-chat',
      maxTokens: 8192,
      timeoutMs: 120000,
    };
  }

  /**
   * Bind dynamic endpoint/credentials for an active DSH session key (or 'default').
   * @param {string} sessionId
   * @param {{ baseUrl?: string, apiKey?: string, model?: string, maxTokens?: number, timeoutMs?: number }} cfg
   */
  setRoute(sessionId, cfg) {
    const clean = {
      baseUrl: String(cfg?.baseUrl || this.defaultRoute.baseUrl).trim().replace(/\/+$/, ''),
      apiKey: String(cfg?.apiKey || this.defaultRoute.apiKey).trim(),
      model: String(cfg?.model || this.defaultRoute.model).trim(),
      maxTokens: Number(cfg?.maxTokens || this.defaultRoute.maxTokens || 8192),
      timeoutMs: Number(cfg?.timeoutMs || this.defaultRoute.timeoutMs || 120000),
    };
    if (!sessionId || sessionId === 'default') {
      this.defaultRoute = clean;
    } else {
      this.sessionRoutes.set(String(sessionId), clean);
    }
  }

  /** @param {string | undefined} sessionId */
  getRoute(sessionId) {
    if (sessionId && this.sessionRoutes.has(String(sessionId))) {
      return this.sessionRoutes.get(String(sessionId));
    }
    return this.defaultRoute;
  }

  providerInfo(provider) {
    return { id: provider, name: 'OpenAI Compatible' };
  }

  /**
   * @param {string} provider
   * @param {string} model
   */
  async resolveModel(provider, model) {
    const modelId = model || this.defaultRoute.model || 'deepseek-chat';
    return {
      provider,
      id: modelId,
      name: modelId,
      context: { contextWindow: 200000 },
      inputModalities: ['text', 'image'],
    };
  }

  /**
   * Detect real MIME strictly from magic bytes (never trust declared extensions/headers).
   */
  _detectImageMimeFromBytes(bytes, fallback = 'image/jpeg') {
    if (!bytes || bytes.length < 4) return fallback;
    if (bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return 'image/jpeg';
    if (bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47) return 'image/png';
    if (bytes[0] === 0x47 && bytes[1] === 0x49 && bytes[2] === 0x46 && bytes[3] === 0x38) return 'image/gif';
    if (bytes.length >= 12 && bytes[0] === 0x52 && bytes[1] === 0x49 && bytes[2] === 0x46 && bytes[3] === 0x46 && bytes[8] === 0x57 && bytes[9] === 0x45 && bytes[10] === 0x42 && bytes[11] === 0x50) return 'image/webp';
    if (bytes[0] === 0x42 && bytes[1] === 0x4d) return 'image/bmp';
    return fallback;
  }

  /**
   * Read image bytes from AttachmentStore and build OpenAI `image_url` part.
   * @param {any} block
   * @param {AbortSignal | undefined} signal
   */
  async _imageBlockToOpenAi(block, signal) {
    if (!block?.attachment || block.offloaded) {
      return { type: 'text', text: offloadedImageText(block?.attachment) };
    }
    const attachments = this.ctx?.attachments;
    if (!attachments) {
      return { type: 'text', text: textOnlyImageText(block.attachment) };
    }
    try {
      const stored = await attachments.readImage(block.attachment, signal);
      const rawBuf = Buffer.from(stored.data);
      const declMime = stored?.ref?.mediaType || block.attachment.mediaType || 'image/jpeg';
      const mime = this._detectImageMimeFromBytes(rawBuf, declMime);
      const b64 = rawBuf.toString('base64');
      return {
        type: 'image_url',
        image_url: { url: `data:${mime};base64,${b64}` },
      };
    } catch {
      return { type: 'text', text: textOnlyImageText(block.attachment) };
    }
  }

  /**
   * Convert DSH conversation messages into OpenAI Chat Completions wire messages.
   * Tool images are deferred to a synthetic trailing user message right after each tool batch.
   * @param {any[]} dshMessages
   * @param {string | undefined} systemOverride
   * @param {AbortSignal | undefined} signal
   */
  async _serializeMessages(dshMessages, systemOverride, signal) {
    const MAX_IMAGES_IN_CONTEXT = 4;
    const allImageCoords = [];
    const msgList = dshMessages || [];
    for (let mi = 0; mi < msgList.length; mi++) {
      const m = msgList[mi];
      const blks = Array.isArray(m?.content) ? m.content : [];
      for (let bi = 0; bi < blks.length; bi++) {
        if (blks[bi]?.type === 'image') {
          allImageCoords.push(`${mi}:${bi}`);
        }
      }
    }
    const keptImageKeys = new Set(allImageCoords.slice(-MAX_IMAGES_IN_CONTEXT));

    const out = [];
    if (systemOverride && String(systemOverride).trim()) {
      out.push({ role: 'system', content: String(systemOverride) });
    }
    /** @type {any[]} */
    let deferredToolImages = [];
    const flushToolImages = () => {
      if (deferredToolImages.length > 0) {
        out.push({
          role: 'user',
          content: [
            { type: 'text', text: '[Tool Result Visual Screenshot]' },
            ...deferredToolImages,
          ],
        });
        deferredToolImages = [];
      }
    };

    for (let mi = 0; mi < msgList.length; mi++) {
      const msg = msgList[mi];
      const blocks = Array.isArray(msg?.content) ? msg.content : [];
      if (msg.role === 'tool') {
        const textParts = [];
        for (let bi = 0; bi < blocks.length; bi++) {
          const b = blocks[bi];
          if (b?.type === 'text') {
            textParts.push(String(b.text || ''));
          } else if (b?.type === 'image') {
            if (!keptImageKeys.has(`${mi}:${bi}`)) {
              textParts.push('[历史步骤截图已折叠]');
              continue;
            }
            const imgPart = await this._imageBlockToOpenAi(b, signal);
            if (imgPart.type === 'image_url') {
              deferredToolImages.push(imgPart);
            } else {
              textParts.push(imgPart.text);
            }
          } else if (b?.type === 'file' && b.attachment) {
            textParts.push(fileHandleText(b.attachment));
          }
        }
        out.push({
          role: 'tool',
          tool_call_id: String(msg.toolCallId || msg.source?.callId || ''),
          content: textParts.join('\n') || '{}',
        });
        continue;
      }

      flushToolImages();

      if (msg.role === 'system') {
        const txt = blocks.filter((b) => b?.type === 'text').map((b) => b.text).join('\n\n').trim();
        if (txt) {
          // Only keep system at leading slot or fold subsequent dynamic system/developer context cleanly
          if (out.length === 0) {
            out.push({ role: 'system', content: txt });
          } else {
            out.push({ role: 'user', content: `[System Context]\n${txt}` });
          }
        }
        continue;
      }

      if (msg.role === 'developer') {
        const txt = blocks
          .map((b) => (b?.type === 'text' ? String(b.text || '') : ''))
          .filter(Boolean)
          .join('\n')
          .trim();
        if (txt) {
          out.push({ role: 'user', content: txt });
        }
        continue;
      }

      if (msg.role === 'user') {
        const parts = [];
        let hasImage = false;
        const isLastUserMsg = mi === msgList.length - 1;
        for (let bi = 0; bi < blocks.length; bi++) {
          const b = blocks[bi];
          if (b?.type === 'text') {
            parts.push({ type: 'text', text: String(b.text || '') });
          } else if (b?.type === 'image') {
            if (!isLastUserMsg && !keptImageKeys.has(`${mi}:${bi}`)) {
              parts.push({ type: 'text', text: '[历史图片已省略]' });
              continue;
            }
            const imgPart = await this._imageBlockToOpenAi(b, signal);
            if (imgPart.type === 'image_url') hasImage = true;
            parts.push(imgPart);
          } else if (b?.type === 'file' && b.attachment) {
            parts.push({ type: 'text', text: fileHandleText(b.attachment) });
          }
        }
        if (!hasImage) {
          out.push({ role: 'user', content: parts.map((p) => p.text).join('\n') || '' });
        } else {
          out.push({ role: 'user', content: parts });
        }
        continue;
      }

      if (msg.role === 'assistant') {
        let reasoning = '';
        let text = '';
        const toolCalls = [];
        for (const b of blocks) {
          if (b?.type === 'reasoning') reasoning += String(b.text || '');
          else if (b?.type === 'text') text += String(b.text || '');
          else if (b?.type === 'tool-call') {
            toolCalls.push({
              id: String(b.id || ''),
              type: 'function',
              function: {
                name: String(b.name || ''),
                arguments: typeof b.arguments === 'string' ? b.arguments : JSON.stringify(b.arguments || {}),
              },
            });
          }
        }
        /** @type {any} */
        const item = { role: 'assistant', content: text };
        if (reasoning) item.reasoning_content = reasoning;
        if (toolCalls.length > 0) item.tool_calls = toolCalls;
        out.push(item);
      }
    }

    flushToolImages();
    return out;
  }

  /**
   * Stream OpenAI Chat Completions response into canonical DSH StreamChunks.
   * @param {import('@deepseek-ai/dsh-llm').GenerateOptions} options
   * @returns {AsyncIterable<import('@deepseek-ai/dsh-llm').StreamChunk>}
   */
  async *stream(options) {
    const route = this.getRoute(options.sessionId);
    const modelId = options.model || route.model || 'deepseek-chat';
    if (!route.baseUrl) {
      throw new Error('OpenAiCompatibleDshAdapter: missing baseUrl');
    }
    const url = route.baseUrl.endsWith('/chat/completions')
      ? route.baseUrl
      : `${route.baseUrl}/chat/completions`;

    const messages = await this._serializeMessages(options.messages, options.system, options.signal);
    const tools = Array.isArray(options.tools) && options.tools.length > 0
      ? options.tools.map((t) => ({
          type: 'function',
          function: {
            name: t.name,
            description: t.description || '',
            parameters: t.parameters || { type: 'object', properties: {} },
          },
        }))
      : undefined;

    /** @type {any} */
    const body = {
      model: modelId,
      messages,
      stream: true,
      stream_options: { include_usage: true },
    };
    if (options.maxTokens || route.maxTokens) body.max_tokens = options.maxTokens || route.maxTokens;
    if (typeof options.temperature === 'number') body.temperature = options.temperature;
    if (Array.isArray(options.stop) && options.stop.length > 0) body.stop = options.stop;
    if (tools) body.tools = tools;

    const headers = {
      'Content-Type': 'application/json',
      ...(route.apiKey ? { Authorization: `Bearer ${route.apiKey}` } : {}),
      ...attributionHeaders(),
    };

    let res;
    try {
      res = await this.fetchImpl(url, {
        method: 'POST',
        headers,
        body: JSON.stringify(body),
        signal: options.signal,
      });
    } catch (err) {
      if (options.signal?.aborted) throw err;
      throw _classifyTransportError(err);
    }

    if (!res.ok) {
      const errText = await res.text().catch(() => '');
      throw _classifyHttpError(res.status, errText, res.headers?.get?.('retry-after'));
    }

    if (!res.body) {
      throw new LlmError('Empty HTTP response body from LLM stream', 'TRANSPORT');
    }

    let nextBlockIndex = 0;
    let reasoningIdx = -1;
    let reasoningBuf = '';
    let textIdx = -1;
    let textBuf = '';
    /** @type {Map<number, { blockIndex: number, id: string, name: string, arguments: string }>} */
    const toolAcc = new Map();
    /** @type {any} */
    let lastUsage = null;

    const closeReasoning = async function* () {
      if (reasoningIdx >= 0) {
        yield { type: 'block-end', index: reasoningIdx, block: { type: 'reasoning', text: reasoningBuf } };
        reasoningIdx = -1;
      }
    };
    const closeText = async function* () {
      if (textIdx >= 0) {
        yield { type: 'block-end', index: textIdx, block: { type: 'text', text: textBuf } };
        textIdx = -1;
      }
    };

    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let lineBuf = '';
    let sawDone = false;

    try {
      while (!sawDone) {
        if (options.signal?.aborted) {
          throw options.signal.reason || new Error('Aborted');
        }
        let readChunk;
        try {
          readChunk = await reader.read();
        } catch (err) {
          if (options.signal?.aborted) throw err;
          throw _classifyTransportError(err);
        }
        const { done, value } = readChunk;
        if (done) break;
        lineBuf += decoder.decode(value, { stream: true });
        const lines = lineBuf.split(/\r?\n/);
        lineBuf = lines.pop() ?? '';

        for (const rawLine of lines) {
          const line = rawLine.trim();
          if (!line || !line.startsWith('data:')) continue;
          const payloadStr = line.slice(5).trim();
          if (!payloadStr) continue;
          if (payloadStr === '[DONE]') {
            sawDone = true;
            break;
          }

          let parsed;
          try {
            parsed = JSON.parse(payloadStr);
          } catch {
            continue;
          }

          if (parsed.usage && typeof parsed.usage === 'object') {
            lastUsage = parsed.usage;
          }

          const choice = Array.isArray(parsed.choices) ? parsed.choices[0] : null;
          if (!choice) continue;
          const delta = choice.delta || {};

          const rChunk = typeof delta.reasoning_content === 'string'
            ? delta.reasoning_content
            : (typeof delta.reasoning === 'string' ? delta.reasoning : '');
          if (rChunk) {
            if (reasoningIdx < 0) {
              reasoningIdx = nextBlockIndex++;
              yield { type: 'block-start', index: reasoningIdx, blockType: 'reasoning' };
            }
            reasoningBuf += rChunk;
            yield { type: 'reasoning-delta', index: reasoningIdx, text: rChunk };
          }

          const tChunk = typeof delta.content === 'string' ? delta.content : '';
          if (tChunk) {
            yield* closeReasoning();
            if (textIdx < 0) {
              textIdx = nextBlockIndex++;
              yield { type: 'block-start', index: textIdx, blockType: 'text' };
            }
            textBuf += tChunk;
            yield { type: 'text-delta', index: textIdx, text: tChunk };
          }

          if (Array.isArray(delta.tool_calls)) {
            yield* closeReasoning();
            yield* closeText();
            for (const tc of delta.tool_calls) {
              const slotIdx = Number(tc.index ?? 0);
              let entry = toolAcc.get(slotIdx);
              if (!entry) {
                const blockIndex = nextBlockIndex++;
                entry = {
                  blockIndex,
                  id: String(tc.id || `call_${slotIdx}_${Date.now()}`),
                  name: String(tc.function?.name || ''),
                  arguments: '',
                };
                toolAcc.set(slotIdx, entry);
                yield { type: 'block-start', index: blockIndex, blockType: 'tool-call' };
              } else {
                if (tc.id) entry.id = String(tc.id);
                if (tc.function?.name) entry.name += String(tc.function.name);
              }
              const argDelta = typeof tc.function?.arguments === 'string' ? tc.function.arguments : '';
              if (argDelta || tc.function?.name) {
                entry.arguments += argDelta;
                yield {
                  type: 'tool-call-delta',
                  index: entry.blockIndex,
                  id: /** @type {any} */ (entry.id),
                  ...(tc.function?.name ? { name: tc.function.name } : {}),
                  argumentsDelta: argDelta,
                };
              }
            }
          }
        }
      }
    } finally {
      try {
        reader.releaseLock();
      } catch {
        // ignore
      }
    }

    yield* closeReasoning();
    yield* closeText();

    const orderedTools = Array.from(toolAcc.entries())
      .sort((a, b) => a[0] - b[0])
      .map(([, v]) => v);
    for (const entry of orderedTools) {
      yield {
        type: 'block-end',
        index: entry.blockIndex,
        block: {
          type: 'tool-call',
          id: /** @type {any} */ (entry.id),
          name: entry.name,
          arguments: entry.arguments || '{}',
        },
      };
    }

    if (lastUsage) {
      const totalPrompt = Number(lastUsage.prompt_tokens || 0);
      const cachedTokens = Number(
        lastUsage.prompt_tokens_details?.cached_tokens ?? lastUsage.prompt_cache_hit_tokens ?? 0,
      );
      const outputTokens = Number(lastUsage.completion_tokens || 0);
      const reasoningTokens = Number(lastUsage.completion_tokens_details?.reasoning_tokens ?? 0);
      yield {
        type: 'usage',
        usage: {
          inputTokens: Math.max(0, totalPrompt - cachedTokens),
          outputTokens,
          ...(lastUsage.total_tokens != null ? { totalTokens: Number(lastUsage.total_tokens) } : {}),
          ...(cachedTokens > 0 ? { cacheReadTokens: cachedTokens } : {}),
          ...(reasoningTokens > 0 ? { reasoningTokens } : {}),
        },
      };
    }

    yield {
      type: 'finish',
      reason: { kind: orderedTools.length > 0 ? 'tool-calls' : 'stop' },
    };
  }
}
