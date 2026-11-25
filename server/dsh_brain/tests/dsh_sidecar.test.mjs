import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { startDshSidecarServer } from '../src/server.mjs';

const TINY_PNG_B64 =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=';

async function startMockPythonToolServer(handler) {
  const srv = createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', async () => {
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
      const out = await handler(body, req.headers);
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(out));
    });
  });
  await new Promise((r) => srv.listen(0, '127.0.0.1', r));
  const addr = srv.address();
  return {
    port: addr.port,
    close: () => new Promise((r) => srv.close(() => r())),
  };
}

function makeSseResponse(lines) {
  const enc = new TextEncoder();
  const stream = new ReadableStream({
    start(ctrl) {
      for (const ln of lines) {
        ctrl.enqueue(enc.encode(`${ln}\n`));
      }
      ctrl.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

describe('DSH Sidecar HTTP & AgentLoop Kernel', () => {
  it('runs multi-step tool loop, bridges multimodal image back to LLM, and demotes intermediate prose cleanly', async () => {
    const dataDir = mkdtempSync(path.join(tmpdir(), 'dsh-sidecar-test-'));
    const toolCallsSeen = [];
    const llmRequests = [];

    const pySrv = await startMockPythonToolServer(async (reqBody, headers) => {
      toolCallsSeen.push({ ...reqBody, secret: headers['x-internal-secret'] });
      return {
        ok: true,
        result: { ok: true, clicked: true, nx: 500, ny: 300 },
        image_b64: TINY_PNG_B64,
        image_mime: 'image/png',
      };
    });

    const mockFetch = async (url, init) => {
      if (String(url).includes('/internal/dsh/tool')) {
        return globalThis.fetch(url, init);
      }
      const parsed = JSON.parse(String(init.body || '{}'));
      llmRequests.push(parsed);
      if (llmRequests.length === 1) {
        return makeSseResponse([
          'data: {"choices":[{"delta":{"reasoning_content":"先点击目标按钮查看界面"}}]}',
          'data: {"choices":[{"delta":{"content":"先打开目标画面。"}}]}',
          'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_phone_1","function":{"name":"phone","arguments":"{\\"op\\":\\"tap\\",\\"nx\\":500,\\"ny\\":300}"}}]}}]}',
          'data: {"usage":{"prompt_tokens":120,"completion_tokens":18,"total_tokens":138},"choices":[]}',
          'data: [DONE]',
        ]);
      }
      return makeSseResponse([
        'data: {"choices":[{"delta":{"content":"已经点开并确认界面正常。"}}]}',
        'data: {"usage":{"prompt_tokens":220,"completion_tokens":12,"total_tokens":232},"choices":[]}',
        'data: [DONE]',
      ]);
    };

    const sidecar = await startDshSidecarServer({
      port: 0,
      dataDir,
      pyPort: pySrv.port,
      pySecret: 'sec-123',
      fetchImpl: mockFetch,
    });

    try {
      const health = await (await globalThis.fetch(`http://127.0.0.1:${sidecar.port}/health`)).json();
      assert.equal(health.ok, true);
      assert.equal(health.engine, 'dsh');
      assert.ok(health.rss_mb > 0);
      assert.ok(typeof health.boot_id === 'string' && health.boot_id.length >= 6);
      assert.deepEqual(health.warm_sessions, []);

      const resp = await globalThis.fetch(`http://127.0.0.1:${sidecar.port}/turn/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_key: 'phone__companion',
          bot_id: 'companion',
          source: 'phone',
          turn_id: 'turn-abc',
          system_prompt: '你是智能主助理。',
          user_text: '点一下屏幕中间',
          allowed_tools: ['phone', 'reply'],
          llm: { base_url: 'https://mock.llm/v1', api_key: 'sk-test', model: 'deepseek-v4.1-flash' },
        }),
      });
      const raw = await resp.text();
      const events = raw.trim().split('\n').map((l) => JSON.parse(l));

      assert.equal(toolCallsSeen.length, 1);
      assert.equal(toolCallsSeen[0].name, 'phone');
      assert.equal(toolCallsSeen[0].secret, 'sec-123');
      assert.equal(toolCallsSeen[0].turn_id, 'turn-abc');

      // Second LLM request must carry tool result AND trailing synthetic multimodal image message
      assert.equal(llmRequests.length, 2);
      const secondMsgs = llmRequests[1].messages;
      assert.ok(secondMsgs.some((m) => m.role === 'tool'));
      assert.ok(secondMsgs.some((m) => Array.isArray(m.content) && m.content.some((p) => p.type === 'image_url')));

      // Unified DSH timeline: BOTH pre-tool prose AND final summary must be preserved chronologically in doneEvt.text
      assert.ok(events.some((e) => (e.type === 'step_segment' || e.type === 'step_text') && e.step_text === '先打开目标画面。'));
      const doneEvt = events.find((e) => e.type === 'done');
      assert.ok(doneEvt);
      assert.ok(doneEvt.text.includes('先打开目标画面。'));
      assert.ok(doneEvt.text.includes('已经点开并确认界面正常。'));
      assert.ok(doneEvt.text.indexOf('先打开目标画面。') < doneEvt.text.indexOf('已经点开并确认界面正常。'));
      assert.equal(doneEvt.thought, '先点击目标按钮查看界面');
      assert.equal(doneEvt.trace.length, 1);
      assert.equal(doneEvt.trace[0].tool, 'phone');
      assert.equal(doneEvt.trace[0].has_image, true);
      assert.equal(doneEvt.boot_id, health.boot_id);
      assert.ok(Array.isArray(doneEvt.timeline) && doneEvt.timeline.length === 3);
      assert.equal(doneEvt.timeline[0].type, 'text');
      assert.ok(doneEvt.timeline[0].text.includes('先打开目标画面。'));
      assert.equal(doneEvt.timeline[1].type, 'tool');
      assert.equal(doneEvt.timeline[1].tool, 'phone');
      assert.equal(doneEvt.timeline[1].status, 'done');
      assert.equal(doneEvt.timeline[2].type, 'text');
      assert.ok(doneEvt.timeline[2].text.includes('已经点开并确认界面正常。'));

      const warmHealth = await (await globalThis.fetch(`http://127.0.0.1:${sidecar.port}/health`)).json();
      assert.ok(Array.isArray(warmHealth.warm_sessions) && warmHealth.warm_sessions.includes('phone__companion'));

      // Session reset cleans up state
      const resetRes = await (await globalThis.fetch(`http://127.0.0.1:${sidecar.port}/session/reset`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_key: 'phone__companion' }),
      })).json();
      assert.equal(resetRes.ok, true);
      const afterResetHealth = await (await globalThis.fetch(`http://127.0.0.1:${sidecar.port}/health`)).json();
      assert.ok(!afterResetHealth.warm_sessions.includes('phone__companion'));
    } finally {
      await sidecar.close();
      await pySrv.close();
      rmSync(dataDir, { recursive: true, force: true });
    }
  });

  it('cancels an active streaming turn cleanly on POST /turn/cancel', async () => {
    const dataDir = mkdtempSync(path.join(tmpdir(), 'dsh-sidecar-cancel-'));
    let llmStartedResolve;
    const llmStarted = new Promise((r) => { llmStartedResolve = r; });

    const slowFetch = async (_url, init) => {
      llmStartedResolve();
      const enc = new TextEncoder();
      const stream = new ReadableStream({
        start(ctrl) {
          ctrl.enqueue(enc.encode('data: {"choices":[{"delta":{"content":"正在查第一步"}}]}\n'));
          const timer = setTimeout(() => {
            try {
              ctrl.enqueue(enc.encode('data: {"choices":[{"delta":{"content":"永不到达"}}]}\n'));
              ctrl.enqueue(enc.encode('data: [DONE]\n'));
              ctrl.close();
            } catch {
              // ignore
            }
          }, 5000);
          init.signal?.addEventListener('abort', () => {
            clearTimeout(timer);
            try { ctrl.error(new Error('Aborted')); } catch {}
          });
        },
      });
      return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
    };

    const sidecar = await startDshSidecarServer({
      port: 0,
      dataDir,
      fetchImpl: slowFetch,
    });

    try {
      const turnPromise = globalThis.fetch(`http://127.0.0.1:${sidecar.port}/turn/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_key: 'phone__companion',
          bot_id: 'companion',
          source: 'phone',
          turn_id: 'turn-cancel-1',
          user_text: '慢速任务',
          llm: { base_url: 'https://mock.llm/v1', api_key: 'sk-test', model: 'deepseek-v4.1-flash' },
        }),
      }).then((r) => r.text());

      await llmStarted;
      await new Promise((r) => setTimeout(r, 80));

      const cancelRes = await (await globalThis.fetch(`http://127.0.0.1:${sidecar.port}/turn/cancel`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_key: 'phone__companion' }),
      })).json();
      assert.equal(cancelRes.cancelled, true);

      const ndjson = await turnPromise;
      const events = ndjson.trim().split('\n').map((l) => JSON.parse(l));
      const terminal = events[events.length - 1];
      assert.equal(terminal.type, 'aborted');
    } finally {
      await sidecar.close();
      rmSync(dataDir, { recursive: true, force: true });
    }
  });

  it('rewinds persisted session.v4.jsonl cleanly on resetSession so rerolled turns do not retain discarded prompts', async () => {
    const dataDir = mkdtempSync(path.join(tmpdir(), 'dsh-sidecar-rewind-'));
    const llmRequests = [];
    const mockFetch = async (_url, init) => {
      const parsed = JSON.parse(String(init.body || '{}'));
      llmRequests.push(parsed);
      return makeSseResponse([
        `data: {"choices":[{"delta":{"content":"收到第${llmRequests.length}次"}}]}`,
        'data: {"usage":{"prompt_tokens":80,"completion_tokens":8,"total_tokens":88},"choices":[]}',
        'data: [DONE]',
      ]);
    };

    const sidecar = await startDshSidecarServer({
      port: 0,
      dataDir,
      fetchImpl: mockFetch,
    });

    try {
      const postTurn = async (turnId, userText) => {
        const resp = await globalThis.fetch(`http://127.0.0.1:${sidecar.port}/turn/stream`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            session_key: 'phone__companion',
            bot_id: 'companion',
            source: 'phone',
            turn_id: turnId,
            system_prompt: '你是主助理。',
            user_text: userText,
            llm: { base_url: 'https://mock.llm/v1', api_key: 'sk-test', model: 'deepseek-v4.1-flash' },
          }),
        });
        return resp.text();
      };

      // Turn 1 & Turn 2 (simulating two prior tries that get rerolled away)
      await postTurn('turn-1', '测试重复词_MARKER');
      await postTurn('turn-2', '测试重复词_MARKER');
      assert.equal(llmRequests.length, 2);
      // Confirm turn 2 saw turn 1 in DSH persisted state before reset
      assert.ok(JSON.stringify(llmRequests[1].messages).includes('收到第1次'));

      // Now simulate rewind/reroll resetSession
      const resetRes = await (await globalThis.fetch(`http://127.0.0.1:${sidecar.port}/session/reset`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_key: 'phone__companion' }),
      })).json();
      assert.equal(resetRes.ok, true);
      assert.ok(resetRes.removed >= 1, `Expected persisted session artifacts removed, got ${JSON.stringify(resetRes)}`);

      // Turn 3 (reroll of Turn 1 with empty seed_history) MUST ONLY contain the single current prompt
      await postTurn('turn-3', '测试重复词_MARKER');
      assert.equal(llmRequests.length, 3);
      const msgsAfterRewind = llmRequests[2].messages;
      const userMsgs = msgsAfterRewind.filter((m) => m.role === 'user');
      assert.equal(userMsgs.length, 1);
      assert.ok(!JSON.stringify(msgsAfterRewind).includes('收到第1次'));
      assert.ok(!JSON.stringify(msgsAfterRewind).includes('收到第2次'));
    } finally {
      await sidecar.close();
      rmSync(dataDir, { recursive: true, force: true });
    }
  });

  it('retries transient fetch failed (TRANSPORT) automatically and preserves errorChain cause on permanent failure', async () => {
    const dataDir = mkdtempSync(path.join(tmpdir(), 'dsh-sidecar-retry-'));
    let attempt = 0;
    const mockFetch = async () => {
      attempt++;
      if (attempt === 1) {
        const cause = Object.assign(new Error('other side closed'), { code: 'UND_ERR_SOCKET' });
        throw new TypeError('fetch failed', { cause });
      }
      return makeSseResponse([
        'data: {"choices":[{"delta":{"content":"重连成功喵"}}]}',
        'data: {"usage":{"prompt_tokens":30,"completion_tokens":6,"total_tokens":36},"choices":[]}',
        'data: [DONE]',
      ]);
    };

    const sidecar = await startDshSidecarServer({
      port: 0,
      dataDir,
      fetchImpl: mockFetch,
    });

    try {
      const resp = await globalThis.fetch(`http://127.0.0.1:${sidecar.port}/turn/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_key: 'pc__pc_engineer',
          bot_id: 'pc_engineer',
          source: 'pc',
          turn_id: 'retry-turn-1',
          system_prompt: '你是桌面总控工程师。',
          user_text: '你好',
          llm: { base_url: 'https://mock.llm/v1', api_key: 'sk-test', model: 'deepseek-v4.1-flash' },
        }),
      });
      const events = (await resp.text()).trim().split('\n').map((l) => JSON.parse(l));
      const terminal = events[events.length - 1];
      assert.equal(terminal.type, 'done');
      assert.equal(terminal.text, '重连成功喵');
      assert.equal(attempt, 2);
    } finally {
      await sidecar.close();
      rmSync(dataDir, { recursive: true, force: true });
    }
  });

  it('prunes historical tool screenshots beyond the latest 3 images to prevent V8 OOM on long visual loops', async () => {
    const dataDir = mkdtempSync(path.join(tmpdir(), 'dsh-sidecar-imgprune-'));
    const llmRequests = [];
    const pySrv = await startMockPythonToolServer(async (reqBody) => ({
      ok: true,
      result: { ok: true, step: reqBody.args?.step || 0 },
      image_b64: TINY_PNG_B64,
      image_mime: 'image/png',
    }));

    const mockFetch = async (url, init) => {
      if (String(url).includes('/internal/dsh/tool')) return globalThis.fetch(url, init);
      const parsed = JSON.parse(String(init.body || '{}'));
      llmRequests.push(parsed);
      const idx = llmRequests.length;
      if (idx <= 6) {
        return makeSseResponse([
          `data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_p_${idx}","function":{"name":"phone","arguments":"{\\"op\\":\\"inspect\\",\\"step\\":${idx}}"}}]}}]}`,
          'data: [DONE]',
        ]);
      }
      return makeSseResponse([
        'data: {"choices":[{"delta":{"content":"六步视觉操作全部完成喵。"}}]}',
        'data: [DONE]',
      ]);
    };

    const sidecar = await startDshSidecarServer({
      port: 0,
      dataDir,
      pyPort: pySrv.port,
      pySecret: 'sec-prune',
      fetchImpl: mockFetch,
    });

    try {
      const resp = await globalThis.fetch(`http://127.0.0.1:${sidecar.port}/turn/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_key: 'phone__companion',
          bot_id: 'companion',
          source: 'phone',
          turn_id: 'turn-img-prune',
          user_text: '连续检查六步画面',
          allowed_tools: ['phone'],
          llm: { base_url: 'https://mock.llm/v1', api_key: 'sk-test', model: 'deepseek-v4.1-flash' },
        }),
      });
      const events = (await resp.text()).trim().split('\n').map((l) => JSON.parse(l));
      const doneEvt = events.find((e) => e.type === 'done');
      assert.ok(doneEvt);
      assert.equal(doneEvt.trace.length, 6);
      assert.equal(doneEvt.timeline.filter((x) => x.type === 'tool').length, 6);
      const lastReqMsgs = llmRequests[llmRequests.length - 1].messages;
      let imgCount = 0;
      for (const m of lastReqMsgs) {
        if (Array.isArray(m.content)) {
          imgCount += m.content.filter((p) => p.type === 'image_url').length;
        }
      }
      assert.equal(imgCount, 3, `Expected at most 3 image_url blocks in final LLM request, got ${imgCount}`);
    } finally {
      await sidecar.close();
      await pySrv.close();
      rmSync(dataDir, { recursive: true, force: true });
    }
  });

  it('keeps systemPrompt 100% byte-identical across turns while appending ephemeral_recall to user_text tail', async () => {
    const dataDir = mkdtempSync(path.join(tmpdir(), 'dsh-sidecar-mem-'));
    const llmRequests = [];
    const pySrv = await startMockPythonToolServer(async () => ({ ok: true, result: { ok: true } }));

    const mockFetch = async (url, init) => {
      if (String(url).includes('/internal/dsh/tool')) return globalThis.fetch(url, init);
      llmRequests.push(JSON.parse(String(init.body || '{}')));
      const n = llmRequests.length;
      return makeSseResponse([
        `data: {"choices":[{"delta":{"content":"第${n}轮收到，余记得 5335 续费规则和土区笔记。"}}]}`,
        'data: {"usage":{"prompt_tokens":180,"completion_tokens":14,"total_tokens":194},"choices":[]}',
        'data: [DONE]',
      ]);
    };

    const sidecar = await startDshSidecarServer({
      port: 0,
      dataDir,
      pyPort: pySrv.port,
      pySecret: 'sec-mem',
      fetchImpl: mockFetch,
    });

    try {
      const sendTurn = async (turnId, userText, recall) => {
        const resp = await globalThis.fetch(`http://127.0.0.1:${sidecar.port}/turn/stream`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            session_key: 'phone__companion',
            bot_id: 'companion',
            source: 'phone',
            turn_id: turnId,
            system_prompt: '你是智能主助理。',
            compaction_checkpoint: '### 📋 核心进展与上下文摘要\n- 土区 Mastercard 5335 续费纪律',
            ephemeral_recall: recall,
            user_text: userText,
            allowed_tools: ['read_file', 'memory'],
            llm: { base_url: 'https://mock.llm/v1', api_key: 'sk-test', model: 'deepseek-v4.1-flash' },
          }),
        });
        return (await resp.text()).trim().split('\n').map((l) => JSON.parse(l));
      };

      const events1 = await sendTurn('turn-mem-1', '之前那个卡怎么说的？', '<recalled_memory>1. [笔记:notes/tr.md] 5335 绑在苹果小号</recalled_memory>');
      assert.equal(events1.find((e) => e.type === 'agent_error' || e.type === 'error'), undefined);
      assert.ok(events1.find((e) => e.type === 'done')?.text.includes('5335'));

      const events2 = await sendTurn('turn-mem-2', '那另外一件事呢？', '<recalled_memory>2. [经验] 主人偏好晚间静音</recalled_memory>');
      assert.equal(events2.find((e) => e.type === 'agent_error' || e.type === 'error'), undefined);

      assert.equal(llmRequests.length, 2);
      const msgs1 = llmRequests[0].messages;
      const msgs2 = llmRequests[1].messages;
      const sys1 = msgs1.find((m) => m.role === 'system');
      const sys2 = msgs2.find((m) => m.role === 'system');
      assert.ok(sys1 && sys2);
      // Byte-identical system prompt across turns => 0 prefix cache invalidation!
      assert.strictEqual(sys1.content, sys2.content);
      assert.ok(sys1.content.includes('你是智能主助理。'));
      assert.ok(sys1.content.includes('土区 Mastercard 5335 续费纪律'));
      assert.ok(!sys1.content.includes('5335 绑在苹果小号'), 'ephemeral_recall must NOT pollute static systemPrompt');
      assert.ok(!sys2.content.includes('主人偏好晚间静音'), 'ephemeral_recall must NOT pollute static systemPrompt');

      // Append-only guarantee: Turn 1 messages must be exact prefix of Turn 2 messages
      assert.deepStrictEqual(msgs2.slice(0, msgs1.length), msgs1);
      // Each turn's ephemeral_recall is appended cleanly to that turn's user message tail
      const user1 = msgs1[msgs1.length - 1];
      const user2 = msgs2[msgs2.length - 1];
      assert.equal(user1.role, 'user');
      assert.equal(user2.role, 'user');
      assert.ok(String(user1.content).includes('5335 绑在苹果小号'));
      assert.ok(String(user2.content).includes('主人偏好晚间静音'));
    } finally {
      await sidecar.close();
      await pySrv.close();
      rmSync(dataDir, { recursive: true, force: true });
    }
  });

  it('pings /session/keepalive with max_tokens=1 reusing exact prefix without polluting session history', async () => {
    const dataDir = mkdtempSync(path.join(tmpdir(), 'dsh-sidecar-keepalive-'));
    const llmRequests = [];
    const mockFetch = async (_url, init) => {
      const parsed = JSON.parse(String(init.body || '{}'));
      llmRequests.push(parsed);
      if (parsed.stream === false) {
        return new Response(JSON.stringify({
          id: 'ka-1',
          choices: [{ message: { role: 'assistant', content: '.' }, finish_reason: 'length' }],
          usage: { prompt_tokens: 180, prompt_tokens_details: { cached_tokens: 180 }, completion_tokens: 1 },
        }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return makeSseResponse([
        `data: {"choices":[{"delta":{"content":"回复${llmRequests.length}"}}]}`,
        'data: {"usage":{"prompt_tokens":180,"completion_tokens":8,"total_tokens":188},"choices":[]}',
        'data: [DONE]',
      ]);
    };

    const sidecar = await startDshSidecarServer({ port: 0, dataDir, fetchImpl: mockFetch });
    try {
      await globalThis.fetch(`http://127.0.0.1:${sidecar.port}/turn/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_key: 'phone__companion',
          bot_id: 'companion',
          source: 'phone',
          turn_id: 'ka-t1',
          system_prompt: '静态系统提示词。',
          user_text: '第一句对话',
          llm: { base_url: 'https://mock.llm/v1', api_key: 'sk-test', model: 'deepseek-v4.1-flash' },
        }),
      }).then((r) => r.text());

      const kaRes = await (await globalThis.fetch(`http://127.0.0.1:${sidecar.port}/session/keepalive`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_key: 'phone__companion' }),
      })).json();
      assert.equal(kaRes.ok, true);
      assert.equal(kaRes.kept_alive, true);
      assert.equal(kaRes.cached_tokens, 180);

      // Next real turn must NOT contain any keepalive artifact
      await globalThis.fetch(`http://127.0.0.1:${sidecar.port}/turn/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_key: 'phone__companion',
          bot_id: 'companion',
          source: 'phone',
          turn_id: 'ka-t2',
          system_prompt: '静态系统提示词。',
          user_text: '第二句对话',
          llm: { base_url: 'https://mock.llm/v1', api_key: 'sk-test', model: 'deepseek-v4.1-flash' },
        }),
      }).then((r) => r.text());

      assert.equal(llmRequests.length, 3);
      // Keepalive request (index 1) has max_tokens=1 and identical messages to turn 1
      assert.equal(llmRequests[1].max_tokens, 1);
      assert.deepStrictEqual(llmRequests[1].messages, llmRequests[0].messages);
      // Turn 2 (index 2) has only Turn 1 user + Turn 1 assistant + Turn 2 user (zero keepalive noise)
      const turn2Users = llmRequests[2].messages.filter((m) => m.role === 'user');
      assert.equal(turn2Users.length, 2);
      assert.equal(turn2Users[0].content, '第一句对话');
      assert.equal(turn2Users[1].content, '第二句对话');
    } finally {
      await sidecar.close();
      rmSync(dataDir, { recursive: true, force: true });
    }
  });
});
