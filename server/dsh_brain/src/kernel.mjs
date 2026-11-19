import { randomUUID } from 'node:crypto';
import { mkdirSync, readdirSync, rmSync } from 'node:fs';
import path from 'node:path';
import { Context } from '@deepseek-ai/cordis';
import TimerService from '@deepseek-ai/cordis-plugin-timer';
import AgentRegistry from '@deepseek-ai/dsh-agent';
import AgentLoop from '@deepseek-ai/dsh-agent-loop';
import LocalAttachmentStore from '@deepseek-ai/dsh-attachment-local';
import BasicCompactionEngine from '@deepseek-ai/dsh-compaction-basic';
import * as CompactionImageOffload from '@deepseek-ai/dsh-compaction-image-offload';
import LlmRuntime, { createUserMessage } from '@deepseek-ai/dsh-llm';
import * as LlmRetry from '@deepseek-ai/dsh-llm-retry';
import SessionStore, { SessionId } from '@deepseek-ai/dsh-session';
import { SessionPersistenceNotFoundError } from '@deepseek-ai/dsh-session-persistence';
import * as JsonlPersistence from '@deepseek-ai/dsh-session-persistence-jsonl';
import SessionProjectionRegistry from '@deepseek-ai/dsh-session-projection';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import TokenMeter from '@deepseek-ai/dsh-token-meter';
import * as NativeToolExecutor from '@deepseek-ai/dsh-tool-executor-native';
import ToolsService from '@deepseek-ai/dsh-tools';

import { OpenAiCompatibleDshAdapter } from './openai_adapter.mjs';
import * as PythonToolsPlugin from './python_tools_plugin.mjs';

const DEFAULT_SYSTEM_TEXT = '你是跨端智能主助理，严格依据工具实际返回结果客观作答。';

/**
 * Boot an official `@deepseek-ai/*` Cordis DSH Sidecar kernel backed by
 * `OpenAiCompatibleDshAdapter` and Python gateway tool bridging.
 *
 * @param {{
 *   dataDir: string,
 *   pyPort?: number,
 *   pySecret?: string,
 *   provider?: string,
 *   defaultBaseUrl?: string,
 *   defaultApiKey?: string,
 *   defaultModel?: string,
 *   fetchImpl?: typeof fetch,
 *   customAdapter?: any,
 * }} options
 */
export async function createDshSidecarKernel(options) {
  const dataDir = path.resolve(options.dataDir);
  const sessionsDir = path.join(dataDir, 'sessions');
  mkdirSync(sessionsDir, { recursive: true });

  let pyPort = Number(options.pyPort || process.env.PY_GATEWAY_PORT || 18190);
  let pySecret = String(options.pySecret || process.env.DSH_INTERNAL_SECRET || '');

  const ctx = new Context();
  const bootId = randomUUID().slice(0, 12);
  const providerName = options.provider || 'openai-compat';
  const defaultModel = options.defaultModel || 'deepseek-chat';

  await ctx.plugin(TimerService);
  await ctx.plugin(LlmRuntime);
  await ctx.plugin(LlmRetry, {});

  await ctx.plugin(SessionStore);
  await ctx.plugin(SessionProjectionRegistry);
  await ctx.plugin(JsonlPersistence, {
    root: sessionsDir,
    compression: 'none',
  });
  await ctx.plugin(LocalAttachmentStore, { dshHome: dataDir });
  await ctx.plugin(TokenMeter);
  await ctx.plugin(BasicCompactionEngine, { auto: false });
  await ctx.plugin(SystemPrompt, {
    includeHarnessIdentity: false,
    includeRuntimeContext: false,
    personaPrefix: '',
  });
  await ctx.plugin(CompactionImageOffload);
  await ctx.plugin(ToolsService);
  await ctx.plugin(NativeToolExecutor);
  await ctx.plugin(AgentRegistry);
  await ctx.plugin(AgentLoop, { agents: [] });

  const adapter = options.customAdapter || new OpenAiCompatibleDshAdapter(ctx, {
    defaultBaseUrl: options.defaultBaseUrl,
    defaultApiKey: options.defaultApiKey,
    defaultModel,
    fetchImpl: options.fetchImpl,
  });
  ctx.llm.registerAdapter([providerName], adapter);

  /** Per-session mutable state for scoped prompt, allowed tools, turn metadata, and active stream callback */
  /** @type {Map<string, { handle: any, systemText: string, agentCtx: any, restrictDisposer: (() => void) | null, meta: { botId: string, source: string, turnId: string }, emitEvent: ((evt: any) => void) | null }>} */
  const sessionStates = new Map();
  /** @type {Map<string, Promise<any>>} */
  const inflightCreates = new Map();
  let closingPromise = null;

  const toolsBridge = PythonToolsPlugin.apply(ctx, {
    getPyPort: () => pyPort,
    getPySecret: () => pySecret,
    getTurnMeta: (sid) => sessionStates.get(sid)?.meta,
    onToolEvent: (sid, evt) => {
      sessionStates.get(sid)?.emitEvent?.(evt);
    },
    fetchImpl: options.fetchImpl,
  });

  // Subscribe once to forward DSH agent stream frames cleanly per session
  /** @type {Map<string, { step: number, thoughtBuf: string, textBuf: string, sawToolCall: boolean, demoted: boolean }>} */
  const attemptState = new Map();

  ctx.on('agent/assistant-stream', ({ agent, frame }) => {
    const sid = String(agent.id);
    const st = sessionStates.get(sid);
    if (!st?.emitEvent) return;

    if (frame.type === 'start') {
      attemptState.set(`${sid}:${frame.attemptId}`, {
        step: frame.step,
        thoughtBuf: '',
        textBuf: '',
        sawToolCall: false,
        demoted: false,
      });
      st.emitEvent({ type: 'step_start', step: frame.step, turn: frame.turn });
      return;
    }

    if (frame.type === 'chunk') {
      const key = `${sid}:${frame.attemptId}`;
      const acc = attemptState.get(key) || {
        step: 1,
        thoughtBuf: '',
        textBuf: '',
        sawToolCall: false,
        demoted: false,
      };
      const ch = frame.chunk;
      if (ch.type === 'reasoning-delta' && ch.text) {
        acc.thoughtBuf += ch.text;
        st.emitEvent({ type: 'thought_delta', delta: ch.text, step: acc.step });
      } else if (ch.type === 'text-delta' && ch.text) {
        acc.textBuf += ch.text;
        // Preserve every spoken text block in stream order (DSH/Codex unified timeline: never erase pre-tool prose).
        st.emitEvent({ type: 'text_delta', delta: ch.text, step: acc.step });
      } else if (ch.type === 'tool-call-delta') {
        if (!acc.sawToolCall) {
          acc.sawToolCall = true;
          if (acc.textBuf.trim() && !acc.demoted) {
            acc.demoted = true;
            st.emitEvent({
              type: 'step_segment',
              step_text: acc.textBuf.trim(),
              step: acc.step,
            });
          }
        }
      } else if (ch.type === 'usage' && ch.usage) {
        st.emitEvent({ type: 'usage', usage: ch.usage, step: acc.step });
      }
      return;
    }

    if (frame.type === 'end') {
      const key = `${sid}:${frame.attemptId}`;
      const acc = attemptState.get(key);
      attemptState.delete(key);
      st.emitEvent({
        type: 'step_end',
        step: acc?.step || 1,
        saw_tool_calls: Boolean(acc?.sawToolCall),
        step_text: acc?.sawToolCall ? acc.textBuf.trim() : '',
        outcome: frame.outcome?.kind || 'committed',
      });
    }
  });

  ctx.on('agent/error', ({ agent, step, error }) => {
    const sid = String(agent.id);
    const st = sessionStates.get(sid);
    if (!st?.emitEvent) return;
    const msg = error instanceof Error ? error.message : String(error);
    st.emitEvent({ type: 'agent_error', step, error: msg });
  });

  /**
   * Ensure an active Cordis Agent exists for `sessionKey`.
   * @param {string} sessionKey
   * @param {{ systemText?: string, model?: string }} [initOpts]
   */
  async function ensureSession(sessionKey, initOpts = {}) {
    if (closingPromise) throw new Error('DSH sidecar kernel is closing');
    const sid = SessionId(sessionKey);
    const existing = sessionStates.get(sessionKey);
    if (existing) {
      if (initOpts.systemText !== undefined) {
        existing.systemText = initOpts.systemText;
      }
      return existing;
    }
    if (inflightCreates.has(sessionKey)) {
      return inflightCreates.get(sessionKey);
    }

    const createPromise = (async () => {
      const stateRec = {
        handle: null,
        systemText: initOpts.systemText || DEFAULT_SYSTEM_TEXT,
        agentCtx: null,
        restrictDisposer: null,
        meta: { botId: 'companion', source: 'phone', turnId: '' },
        cancelled: false,
        emitEvent: null,
      };
      const agentOptions = {
        provider: providerName,
        model: initOpts.model || defaultModel,
      };
      const setup = (agentCtx) => {
        stateRec.agentCtx = agentCtx;
        agentCtx.systemPrompt.section({
          name: 'proactive:system',
          order: 100,
          complete: true,
          interpolate: false,
          text: () => stateRec.systemText || DEFAULT_SYSTEM_TEXT,
        });
      };

      let handle;
      let wasCreatedNew = false;
      try {
        handle = await ctx.agents.resume({
          resumeSessionId: sid,
          agentOptions,
          setup,
        });
      } catch (err) {
        if (!(err instanceof SessionPersistenceNotFoundError)) throw err;
        wasCreatedNew = true;
        handle = await ctx.agents.create({
          sessionId: sid,
          meta: { cwd: dataDir },
          agentOptions,
          setup,
        });
      }
      stateRec.handle = handle;
      stateRec.wasCreatedNew = wasCreatedNew;
      sessionStates.set(sessionKey, stateRec);
      return stateRec;
    })().finally(() => {
      inflightCreates.delete(sessionKey);
    });

    inflightCreates.set(sessionKey, createPromise);
    return createPromise;
  }

  function removePersistedSessionArtifacts(dir, targetKeys, depth = 0) {
    if (depth > 4) return 0;
    let removed = 0;
    let entries = [];
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      return 0;
    }
    for (const ent of entries) {
      const fullPath = path.join(dir, ent.name);
      if (ent.isDirectory()) {
        if (targetKeys.has(ent.name)) {
          try {
            rmSync(fullPath, { recursive: true, force: true });
            removed++;
          } catch {
            // ignore
          }
        } else {
          removed += removePersistedSessionArtifacts(fullPath, targetKeys, depth + 1);
        }
      } else if (ent.isFile()) {
        const stem = ent.name.replace(/\.jsonl$/i, '');
        if (targetKeys.has(ent.name) || (ent.name.endsWith('.jsonl') && targetKeys.has(stem))) {
          try {
            rmSync(fullPath, { force: true });
            removed++;
          } catch {
            // ignore
          }
        }
      }
    }
    return removed;
  }

  /**
   * Dispose agent handle and remove persisted JSONL session for `sessionKey` (including `<workDir>/<sessionId>/session.v4.jsonl`).
   * @param {string} sessionKey
   */
  async function resetSession(sessionKey) {
    const rec = sessionStates.get(sessionKey);
    if (rec) {
      if (rec.handle && !rec.cancelled) {
        rec.cancelled = true;
        try {
          rec.handle.abort?.();
        } catch {
          // ignore
        }
      }
      sessionStates.delete(sessionKey);
      try {
        rec.restrictDisposer?.();
      } catch {
        // ignore
      }
      try {
        await rec.handle?.dispose();
      } catch {
        // ignore
      }
    }
    const targetKeys = new Set([sessionKey, encodeURIComponent(sessionKey)]);
    const removedCount = removePersistedSessionArtifacts(sessionsDir, targetKeys, 0);
    return { ok: true, reset: sessionKey, removed: removedCount };
  }

  /**
   * Execute one conversational turn inside DSH AgentLoop and emit incremental NDJSON-friendly events.
   * @param {{
   *   session_key: string,
   *   bot_id?: string,
   *   source?: string,
   *   turn_id?: string,
   *   system_prompt?: string,
   *   user_text?: string,
   *   images?: Array<{ data_b64: string, media_type?: string }>,
   *   allowed_tools?: string[],
   *   tool_schemas?: any[],
   *   llm?: { base_url?: string, api_key?: string, model?: string, max_tokens?: number },
   *   py_port?: number,
   *   py_secret?: string,
   *   reset_session?: boolean,
   * }} payload
   * @param {(evt: any) => void} onEvent
   */
  async function runTurn(payload, onEvent) {
    const sessionKey = String(payload.session_key || `${payload.source || 'phone'}__${payload.bot_id || 'companion'}`);
    if (payload.py_port) pyPort = Number(payload.py_port);
    if (payload.py_secret) pySecret = String(payload.py_secret);
    if (Array.isArray(payload.tool_schemas) && payload.tool_schemas.length > 0) {
      toolsBridge.syncTools(payload.tool_schemas);
    }
    if (payload.reset_session) {
      await resetSession(sessionKey);
    }

    if (payload.llm && typeof payload.llm === 'object') {
      adapter.setRoute(sessionKey, {
        baseUrl: payload.llm.base_url,
        apiKey: payload.llm.api_key,
        model: payload.llm.model,
        maxTokens: payload.llm.max_tokens,
      });
    }

    const rec = await ensureSession(sessionKey, {
      systemText: payload.system_prompt,
      model: payload.llm?.model,
    });
    const agent = rec.handle.agent;

    if (payload.llm?.model) {
      const curSel = agent.model.current();
      if (!curSel || curSel.model !== payload.llm.model || curSel.provider !== providerName) {
        agent.model.select({ provider: providerName, model: String(payload.llm.model) });
      }
    }

    // Apply per-turn allowed tool filtering dynamically via scoped restrict
    if (rec.restrictDisposer) {
      try {
        rec.restrictDisposer();
      } catch {
        // ignore
      }
      rec.restrictDisposer = null;
    }
    if (Array.isArray(payload.allowed_tools) && rec.agentCtx) {
      const known = new Set(ctx.tools.schemas().map((s) => s.name));
      const allowList = payload.allowed_tools.filter((n) => known.has(n));
      if (allowList.length > 0) {
        rec.restrictDisposer = rec.agentCtx.tools.restrict({ allow: allowList });
      }
    }

    rec.meta = {
      botId: String(payload.bot_id || 'companion'),
      source: String(payload.source || 'phone'),
      turnId: String(payload.turn_id || ''),
    };
    rec.cancelled = false;

    const collectedThoughts = [];
    const collectedStepTexts = [];
    const collectedTrace = [];
    const collectedTurnSteps = [];
    let currentFinalText = '';
    let abortedFlag = false;
    let errorMsg = '';
    let lastUsage = null;
    let lastToolTextFallback = '';

    let lastTextStep = -1;
    rec.emitEvent = (evt) => {
      if (evt.type === 'thought_delta') {
        collectedThoughts.push(evt.delta);
      } else if (evt.type === 'text_delta') {
        const sIdx = Number(evt.step || 1);
        if (lastTextStep > 0 && sIdx !== lastTextStep && currentFinalText && !currentFinalText.endsWith('\n\n')) {
          const sep = currentFinalText.endsWith('\n') ? '\n' : '\n\n';
          currentFinalText += sep;
          onEvent({ type: 'text_delta', delta: sep, step: sIdx });
        }
        lastTextStep = sIdx;
        currentFinalText += evt.delta;
      } else if (evt.type === 'step_segment' || evt.type === 'demote_stream_to_step') {
        // Keep previously streamed prose visible in `currentFinalText`; also tag the upcoming tool step.
        if (evt.step_text) {
          collectedStepTexts.push(evt.step_text);
          collectedTurnSteps.push({ type: 'step_text', text: evt.step_text, step: evt.step });
          onEvent({ type: 'step_text', text: evt.step_text, step: evt.step });
        }
      } else if (evt.type === 'tool_end') {
        collectedTrace.push({
          tool: evt.tool,
          ok: evt.ok,
          result: evt.result,
          has_image: evt.has_image,
          duration_ms: evt.duration_ms,
        });
        collectedTurnSteps.push({
          type: 'tool',
          tool: evt.tool,
          ok: evt.ok,
          step_note: lastToolTextFallback || '',
        });
      } else if (evt.type === 'step_end') {
        if (evt.step_text) {
          lastToolTextFallback = evt.step_text;
        }
        if (evt.outcome === 'abandoned' || rec.cancelled) {
          abortedFlag = true;
        }
      } else if (evt.type === 'usage') {
        lastUsage = evt.usage;
      } else if (evt.type === 'agent_error') {
        errorMsg = String(evt.error || 'Unknown DSH agent error');
      }
      onEvent(evt);
    };

    // Build UserMessage content (text + optional images)
    const content = [];
    if (Array.isArray(payload.images)) {
      for (const img of payload.images) {
        if (!img?.data_b64) continue;
        const buf = Buffer.from(img.data_b64, 'base64');
        let mime = img.media_type || 'image/jpeg';
        if (buf.length >= 4) {
          if (buf[0] === 0xff && buf[1] === 0xd8 && buf[2] === 0xff) mime = 'image/jpeg';
          else if (buf[0] === 0x89 && buf[1] === 0x50 && buf[2] === 0x4e && buf[3] === 0x47) mime = 'image/png';
          else if (buf[0] === 0x47 && buf[1] === 0x49 && buf[2] === 0x46 && buf[3] === 0x38) mime = 'image/gif';
          else if (buf.length >= 12 && buf[0] === 0x52 && buf[1] === 0x49 && buf[2] === 0x46 && buf[3] === 0x46 && buf[8] === 0x57 && buf[9] === 0x45 && buf[10] === 0x42 && buf[11] === 0x50) mime = 'image/webp';
        }
        const ref = await ctx.attachments.saveImage({ data: new Uint8Array(buf), mediaType: mime });
        content.push({ type: 'image', attachment: ref });
      }
    }
    let textBody = String(payload.user_text || '').trim();
    if (rec.wasCreatedNew && Array.isArray(payload.seed_history) && payload.seed_history.length > 0) {
      rec.wasCreatedNew = false;
      const keptLines = [];
      let usedChars = 0;
      const maxSeedChars = 65000;
      for (let i = payload.seed_history.length - 1; i >= 0 && keptLines.length < 48; i--) {
        const h = payload.seed_history[i];
        const roleTag = h.role === 'user' ? '用户' : (h.role === 'tool' ? '工具输出' : '助手');
        const txt = String(h.text || '').slice(0, 6000);
        if (!txt) continue;
        const line = `[${roleTag}]: ${txt}`;
        if (keptLines.length > 0 && usedChars + line.length > maxSeedChars) break;
        keptLines.push(line);
        usedChars += line.length + 2;
      }
      keptLines.reverse();
      const lines = keptLines.join('\n');
      textBody = `[近期完整对话与工具链轨迹]\n${lines}\n\n[当前新消息]\n${textBody}`;
    }
    if (textBody || content.length === 0) {
      content.push({ type: 'text', text: textBody || ' ' });
    }

    const idlePromise = new Promise((resolve) => {
      let sawRunning = agent.status === 'running';
      const dispose = ctx.on('agent/status', ({ agent: a, status }) => {
        if (String(a.id) !== sessionKey) return;
        if (status === 'running') sawRunning = true;
        if (status === 'idle' && sawRunning) {
          dispose();
          resolve();
        }
      });
      // Safety fallback if idle immediately without transition
      queueMicrotask(() => {
        if (!sawRunning && agent.status === 'idle') {
          // Will transition synchronously on agent.followup below unless immediately rejected
        }
      });
    });

    const turnStartEventIdx = agent.session.snapshotEvents().length;

    agent.followup(
      createUserMessage({
        content,
        source: { kind: 'user' },
      }),
    );

    await idlePromise;
    rec.emitEvent = null;

    // Extract ALL chronological assistant prose blocks AND interleaved tool items into `timeline`
    const committedSegments = [];
    const timeline = [];
    const evts = agent.session.snapshotEvents();
    let turnStartIdx = Math.min(turnStartEventIdx, evts.length);
    for (let i = turnStartIdx; i < evts.length; i++) {
      if (evts[i].type === 'user/message') {
        turnStartIdx = i + 1;
        break;
      }
    }
    const traceByIdx = Array.isArray(collectedTrace) ? collectedTrace : [];
    let traceCursor = 0;
    const toolBlocksById = new Map();
    for (let i = turnStartIdx; i < evts.length; i++) {
      const ev = evts[i];
      if (ev.type === 'assistant/message') {
        const msgObj = ev.data?.message || ev.message || {};
        const blocks = Array.isArray(msgObj.content) ? msgObj.content : [];
        const txtParts = [];
        for (const blk of blocks) {
          if (blk?.type === 'text' && blk.text) {
            txtParts.push(blk.text);
          } else if (blk?.type === 'tool-call') {
            if (txtParts.length) {
              const seg = txtParts.join('').trim();
              txtParts.length = 0;
              if (seg) {
                if (committedSegments[committedSegments.length - 1] !== seg) committedSegments.push(seg);
                timeline.push({ type: 'text', text: seg });
              }
            }
            const tMeta = traceByIdx[traceCursor++] || {};
            const rawArgs = blk.arguments ?? blk.args ?? blk.input;
            const parsedArgs = typeof rawArgs === 'string'
              ? (() => { try { return JSON.parse(rawArgs); } catch { return { raw: rawArgs }; } })()
              : (rawArgs || {});
            const tItem = {
              type: 'tool',
              call_id: String(blk.id || ''),
              tool: String(blk.name || tMeta.tool || 'tool'),
              arguments: parsedArgs,
              result: tMeta.result ?? {},
              status: tMeta.ok === false ? 'error' : 'done',
              duration_ms: Number(tMeta.duration_ms || 0),
            };
            if (tItem.call_id) toolBlocksById.set(tItem.call_id, tItem);
            timeline.push(tItem);
          }
        }
        const tailSeg = txtParts.join('').trim();
        if (tailSeg) {
          if (committedSegments[committedSegments.length - 1] !== tailSeg) committedSegments.push(tailSeg);
          timeline.push({ type: 'text', text: tailSeg });
        }
      } else if (ev.type === 'tool/result') {
        const rData = ev.data || ev;
        const cid = String(rData.toolCallId || rData.callId || rData.id || '');
        const matched = cid ? toolBlocksById.get(cid) : null;
        if (matched && (!matched.result || Object.keys(matched.result).length === 0)) {
          const rContent = rData.content ?? rData.output ?? rData.result;
          const rTxt = Array.isArray(rContent)
            ? rContent.filter((c) => c?.type === 'text').map((c) => c.text).join('\n')
            : String(rContent || '');
          if (rTxt) matched.result = { text: rTxt.slice(0, 1200) };
        }
      }
    }
    const committedText = (committedSegments.length > 0 ? committedSegments.join('\n\n') : currentFinalText.trim());

    // If interrupted/aborted with no final text, surface last executed step sentence cleanly
    const lastStepSentence = collectedStepTexts.length > 0
      ? collectedStepTexts[collectedStepTexts.length - 1]
      : lastToolTextFallback;

    return {
      type: (abortedFlag || rec.cancelled) ? 'aborted' : (errorMsg && !committedText ? 'error' : 'done'),
      boot_id: bootId,
      session_key: sessionKey,
      text: committedText,
      partial_text: currentFinalText.trim(),
      thought: collectedThoughts.join('').trim(),
      last_step_text: lastStepSentence || '',
      trace: collectedTrace,
      turn_steps: collectedTurnSteps,
      timeline,
      usage: lastUsage,
      error: errorMsg || undefined,
    };
  }

  /** @param {string} sessionKey */
  function cancelTurn(sessionKey) {
    const rec = sessionStates.get(sessionKey);
    if (!rec) return { ok: true, cancelled: false };
    rec.cancelled = true;
    rec.handle.agent.cancel({ kind: 'user' });
    return { ok: true, cancelled: true };
  }

  function statusSummary() {
    return {
      ok: true,
      engine: 'dsh',
      boot_id: bootId,
      sdk: '@deepseek-ai/dsh-agent-loop@0.2.2-rc.1',
      sessions: sessionStates.size,
      warm_sessions: Array.from(sessionStates.keys()),
      rss_mb: Math.round((process.memoryUsage().rss / (1024 * 1024)) * 10) / 10,
      heap_used_mb: Math.round((process.memoryUsage().heapUsed / (1024 * 1024)) * 10) / 10,
    };
  }

  async function close() {
    if (closingPromise) return closingPromise;
    closingPromise = (async () => {
      for (const rec of Array.from(sessionStates.values())) {
        try {
          rec.restrictDisposer?.();
        } catch {
          // ignore
        }
        try {
          await rec.handle?.dispose();
        } catch {
          // ignore
        }
      }
      sessionStates.clear();
      await ctx.fiber.dispose();
    })();
    return closingPromise;
  }

  return {
    ctx,
    adapter,
    runTurn,
    cancelTurn,
    resetSession,
    statusSummary,
    close,
  };
}
