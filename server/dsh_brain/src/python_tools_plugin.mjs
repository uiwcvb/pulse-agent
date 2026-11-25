import http from 'node:http';
import { errorChain } from '@deepseek-ai/dsh-llm';

function nodeHttpJsonPost(urlStr, headers, bodyStr, signal) {
  return new Promise((resolve, reject) => {
    const parsed = new URL(urlStr);
    const bodyBuf = Buffer.from(bodyStr, 'utf8');
    const reqHeaders = {
      ...headers,
      'Content-Length': bodyBuf.length,
      Connection: 'close',
    };
    const req = http.request(
      {
        hostname: parsed.hostname,
        port: Number(parsed.port || 80),
        path: parsed.pathname + parsed.search,
        method: 'POST',
        headers: reqHeaders,
        agent: false,
      },
      (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          cleanupAbort();
          const text = Buffer.concat(chunks).toString('utf8');
          try {
            resolve(JSON.parse(text || '{}'));
          } catch (e) {
            reject(new Error(`Invalid JSON from ${urlStr} (${res.statusCode}): ${text.slice(0, 180)}`));
          }
        });
        res.on('error', (err) => {
          cleanupAbort();
          reject(err);
        });
      },
    );
    let onAbort = null;
    const cleanupAbort = () => {
      if (onAbort && signal) {
        try { signal.removeEventListener('abort', onAbort); } catch {}
        onAbort = null;
      }
    };
    if (signal) {
      onAbort = () => {
        const reason = signal.reason || new Error('Aborted');
        try { req.destroy(reason); } catch {}
        reject(reason);
      };
      if (signal.aborted) {
        onAbort();
        return;
      }
      signal.addEventListener('abort', onAbort, { once: true });
    }
    req.on('error', (err) => {
      cleanupAbort();
      reject(err);
    });
    req.end(bodyBuf);
  });
}

/**
 * Registers the 14 canonical proactive-agent tools on Cordis `ToolsService`
 * and dispatches execution to the Python gateway loopback endpoint `/internal/dsh/tool`.
 */

const READ_ONLY_SAFE_TOOLS = new Set([
  'read_file',
  'search_files',
  'subtask',
]);

/**
 * Built-in fallback tool definitions matching `server/core/tool_schemas.py`
 * (Python caller may also pass dynamic schemas including custom plugin tools).
 */
export const DEFAULT_TOOL_SCHEMAS = [
  {
    name: 'read_file',
    description: '读取工作区或 memory_vault/ 文本文件（grep 关键字过滤、tail 读末尾、start_line/end_line 分段）、列目录，或视觉识别图片',
    parameters: { type: 'object', properties: { path: { type: 'string' }, start_line: { type: 'integer' }, end_line: { type: 'integer' }, tail: { type: 'integer' }, grep: { type: 'string' }, context: { type: 'integer' }, literal: { type: 'boolean' }, max_chars: { type: 'integer' } }, additionalProperties: true },
  },
  {
    name: 'write_file',
    description: '创建或完整覆写工作区文件（支持 uploads/、notes/、memory_vault/、workspace/）',
    parameters: { type: 'object', properties: { path: { type: 'string' }, content: { type: 'string' } }, required: ['path', 'content'], additionalProperties: true },
  },
  {
    name: 'edit_file',
    description: '精确替换、移动重命名或删除工作区与 memory_vault/ 文件 (op="replace"|"move"|"delete")',
    parameters: { type: 'object', properties: { op: { type: 'string' }, path: { type: 'string' }, old_text: { type: 'string' }, new_text: { type: 'string' }, replace_all: { type: 'boolean' }, src: { type: 'string' }, dst: { type: 'string' } }, additionalProperties: true },
  },
  {
    name: 'search_files',
    description: '工作区检索工具 (op="grep"|"glob"|"list"|"locate")；locate 可跨 hub_workspace / memory_vault / cloud_desktop / pc_windows 定位文件',
    parameters: { type: 'object', properties: { op: { type: 'string' }, path: { type: 'string' }, query: { type: 'string' }, pattern: { type: 'string' }, literal: { type: 'boolean' }, max_depth: { type: 'integer' }, limit: { type: 'integer' } }, additionalProperties: true },
  },
  {
    name: 'bash',
    description: 'VPS 本地 Linux 沙箱命令行（算日期/周次/Python脚本/后台任务/0-Token哨兵，最长 300s）；Windows 请用 pc，公网请用 cloud',
    parameters: { type: 'object', properties: { op: { type: 'string' }, command: { type: 'string' }, timeout: { type: 'integer' }, cwd: { type: 'string' }, task_id: { type: 'string' } }, additionalProperties: true },
  },
  {
    name: 'memory',
    description: '日常长期记忆与跨压缩检索 (op="search"|"conversation"|"query_facts"|"upsert_fact"|"delete_fact"|"pin"|"unpin"|"list_pins")',
    parameters: { type: 'object', properties: { op: { type: 'string' }, query: { type: 'string' }, ctx_id: { type: 'string' }, entity: { type: 'string' }, attr: { type: 'string' }, value: { type: 'string' }, text: { type: 'string' }, pin_id: { type: 'string' } }, additionalProperties: true },
  },
  {
    name: 'blackboard',
    description: '多 Bot 共享黑板协作（高阶协作见 memory_vault/skills/blackboard_collab.md；定时请用 schedule）',
    parameters: { type: 'object', properties: { action: { type: 'string' }, text: { type: 'string' }, title: { type: 'string' }, post_id: { type: 'string' }, mentions: { type: 'array', items: { type: 'string' } }, key: { type: 'string' }, value: { type: 'string' }, params: { type: 'object' } }, additionalProperties: true },
  },
  {
    name: 'schedule',
    description: '唯一周期/定时提醒与闹钟管理 (op="schedule"|"alarm"|"cancel"|"list"|"add_routine"|"update_routine"|"remove_routine"|"list_routines")',
    parameters: { type: 'object', properties: { op: { type: 'string' }, task_id: { type: 'string' }, title: { type: 'string' }, text: { type: 'string' }, when: { type: 'string' }, daily_time: { type: 'string' }, weekdays: { type: 'string' }, cron_expr: { type: 'string' }, overwrite: { type: 'boolean' } }, required: ['op'], additionalProperties: true },
  },
  {
    name: 'phone',
    description: '手机隔离虚拟屏 VirtualDisplay、通知与验证码读取（@eN 优先，自绘页用 0..1000 nx,ny 与 zoom；过滤用 query）',
    parameters: { type: 'object', properties: { op: { type: 'string' }, nx: { type: 'integer' }, ny: { type: 'integer' }, nx2: { type: 'integer' }, ny2: { type: 'integer' }, box: { type: 'array', items: { type: 'integer' } }, text: { type: 'string' }, target: { type: 'string' }, query: { type: 'string' }, app: { type: 'string' }, direction: { type: 'string' }, command: { type: 'string' }, destroy: { type: 'boolean' } }, required: ['op'], additionalProperties: true },
  },
  {
    name: 'pc',
    description: '唯一 Windows 本机入口（pc_shell、扁平 0..1000 桌面键鼠截图与 DSH 会话协同）',
    parameters: { type: 'object', properties: { op: { type: 'string' }, command: { type: 'string' }, nx: { type: 'integer' }, ny: { type: 'integer' }, box: { type: 'array', items: { type: 'integer' } }, text: { type: 'string' }, target: { type: 'string' } }, required: ['op'], additionalProperties: true },
  },
  {
    name: 'cloud',
    description: '24/7 云电脑与唯一公网搜索/抓网页入口（search/navigate 默认无图 CDP 快读且用完自动释放浏览器；需画面传 screenshot=true）',
    parameters: { type: 'object', properties: { op: { type: 'string' }, query: { type: 'string' }, url: { type: 'string' }, nx: { type: 'integer' }, ny: { type: 'integer' }, nx2: { type: 'integer' }, ny2: { type: 'integer' }, box: { type: 'array', items: { type: 'integer' } }, ref: { type: 'string' }, command: { type: 'string' }, script: { type: 'string' }, text: { type: 'string' }, direction: { type: 'string' }, screenshot: { type: 'boolean' }, keep_open: { type: 'boolean' }, path: { type: 'string' }, name: { type: 'string' } }, required: ['op'], additionalProperties: true },
  },
  {
    name: 'reply',
    description: '交互控制与分级通知 (op="notify"|"alarm"|"send_image"|"send_file"|"sticker"|"choices"|"silent"|"dnd"|"complete_goal")；alarm 仅立刻响',
    parameters: { type: 'object', properties: { op: { type: 'string' }, path: { type: 'string' }, name: { type: 'string' }, code: { type: 'string' }, caption: { type: 'string' }, crop_box: { type: 'array', items: { type: 'integer' } }, choices: { type: 'array', items: { type: 'string' } }, title: { type: 'string' }, body: { type: 'string' }, level: { type: 'string' }, urgent: { type: 'boolean' }, overlay: { type: 'boolean' }, summary: { type: 'string' } }, required: ['op'], additionalProperties: true },
  },
  {
    name: 'subtask',
    description: '瞬发异步子任务（research 预设走 cloud 无图快读自动释放浏览器；支持 spawn/status/list/cancel/resume/steer）',
    parameters: { type: 'object', properties: { op: { type: 'string' }, task_id: { type: 'string' }, title: { type: 'string' }, goal: { type: 'string' }, preset: { type: 'string' }, tools: { type: 'array', items: { type: 'string' } }, context_paths: { type: 'array', items: { type: 'string' } }, max_steps: { type: 'integer' }, wait: { type: 'boolean' }, use_main_model: { type: 'boolean' } }, additionalProperties: true },
  },
  {
    name: 'admin',
    description: '系统运维与记忆库整理（非主人要求勿调，调前必读 memory_vault/skills/tool_admin.md）',
    parameters: { type: 'object', properties: { op: { type: 'string' }, action: { type: 'string' }, params: { type: 'object' } }, required: ['op'], additionalProperties: true },
  },
];

/**
 * Strip unsupported keywords from OpenAI JSON schema so it satisfies DSH's strict JsonSchemaNode subset.
 * @param {any} node
 */
export function sanitizeSchemaForDsh(node) {
  if (!node || typeof node !== 'object' || Array.isArray(node)) {
    return { type: 'object', properties: {}, additionalProperties: true };
  }
  const out = {};
  if (node.type && ['object', 'array', 'string', 'number', 'integer', 'boolean', 'null'].includes(node.type)) {
    out.type = node.type;
  }
  if (typeof node.description === 'string') out.description = node.description;
  if (Array.isArray(node.enum)) out.enum = node.enum;
  if (out.type === 'object' || node.properties) {
    out.type = 'object';
    const props = {};
    if (node.properties && typeof node.properties === 'object') {
      for (const [k, v] of Object.entries(node.properties)) {
        props[k] = sanitizeSchemaForDsh(v);
      }
    }
    out.properties = props;
    if (Array.isArray(node.required)) {
      out.required = node.required.filter((k) => k in props);
    }
    out.additionalProperties = typeof node.additionalProperties === 'boolean' ? node.additionalProperties : true;
  } else if (out.type === 'array') {
    if (node.items && typeof node.items === 'object') {
      out.items = sanitizeSchemaForDsh(node.items);
    }
  }
  return out;
}

/**
 * Create Cordis plugin registering Python-bridged tools on `ctx.tools`.
 */
export const name = 'python-tools-bridge';
export const inject = ['tools', 'attachments'];

/**
 * @param {import('@deepseek-ai/cordis').Context} ctx
 * @param {{
 *   getPyPort: () => number,
 *   getPySecret: () => string,
 *   getTurnMeta: (sessionId: string) => { botId: string, source: string, turnId: string } | undefined,
 *   onToolEvent?: (sessionId: string, evt: any) => void,
 *   fetchImpl?: typeof fetch,
 * }} opts
 */
export function apply(ctx, opts) {
  const customFetch = opts.fetchImpl || null;
  /** @type {Map<string, () => void>} */
  const disposers = new Map();

  const registerToolSchema = (toolDef) => {
    const toolName = String(toolDef?.function?.name || toolDef?.name || '').trim();
    if (!toolName) return;
    const prev = disposers.get(toolName);
    if (prev) {
      prev();
      disposers.delete(toolName);
    }
    const desc = String(toolDef?.function?.description || toolDef?.description || toolName);
    const params = sanitizeSchemaForDsh(toolDef?.function?.parameters || toolDef?.parameters);

    const dispose = ctx.tools.register({
      name: toolName,
      description: desc,
      parameters: params,
      output: {
        schema: {},
        render: (_args, value) => {
          if (!value || typeof value !== 'object' || Array.isArray(value)) {
            return [{ type: 'text', text: JSON.stringify(value) }];
          }
          const { _imageAttachment, _ephemeral_ui, _stuck_nudge, ...cleanMeta } = /** @type {any} */ (value);
          const blocks = [{ type: 'text', text: JSON.stringify(cleanMeta) }];
          if (typeof _ephemeral_ui === 'string' && _ephemeral_ui.trim()) {
            blocks.push({ type: 'text', text: _ephemeral_ui.trim() });
          }
          if (typeof _stuck_nudge === 'string' && _stuck_nudge.trim()) {
            blocks.push({ type: 'text', text: _stuck_nudge.trim() });
          }
          if (_imageAttachment && typeof _imageAttachment.attachmentId === 'string') {
            blocks.push({ type: 'image', attachment: _imageAttachment });
          }
          return blocks;
        },
      },
      isConcurrencySafe: () => READ_ONLY_SAFE_TOOLS.has(toolName),
      execute: async (args, exec) => {
        const sessionId = String(exec.agent?.id || '');
        const meta = opts.getTurnMeta(sessionId) || { botId: 'companion', source: 'phone', turnId: '' };
        const pyPort = opts.getPyPort();
        const secret = opts.getPySecret();

        opts.onToolEvent?.(sessionId, {
          type: 'tool_start',
          tool: toolName,
          call_id: String(exec.callId || ''),
          arguments: args,
        });

        const t0 = Date.now();
        let data;
        try {
          const toolUrl = `http://127.0.0.1:${pyPort}/internal/dsh/tool`;
          const reqHeaders = {
            'Content-Type': 'application/json',
            Connection: 'close',
            ...(secret ? { 'X-Internal-Secret': secret } : {}),
          };
          const bodyStr = JSON.stringify({
            name: toolName,
            args: args || {},
            bot_id: meta.botId,
            source: meta.source,
            turn_id: meta.turnId,
            session_id: sessionId,
          });
          if (customFetch) {
            const res = await customFetch(toolUrl, {
              method: 'POST',
              headers: reqHeaders,
              body: bodyStr,
              signal: exec.signal,
            });
            data = await res.json();
          } else {
            data = await nodeHttpJsonPost(toolUrl, reqHeaders, bodyStr, exec.signal);
          }
        } catch (err) {
          const errMsg = errorChain(err) || (err instanceof Error ? err.message : String(err));
          opts.onToolEvent?.(sessionId, {
            type: 'tool_end',
            tool: toolName,
            call_id: String(exec.callId || ''),
            ok: false,
            error: errMsg,
            duration_ms: Date.now() - t0,
          });
          return { ok: false, error: errMsg };
        }

        const resultObj = data?.result !== undefined ? data.result : data;
        let imageAttachment;
        const b64 = data?.image_b64 || data?._multimodal_b64?.data || '';
        const mime = data?.image_mime || data?._multimodal_b64?.mime || 'image/jpeg';
        if (typeof b64 === 'string' && b64.length > 0 && ctx.attachments) {
          try {
            const buf = Buffer.from(b64, 'base64');
            let realMime = mime;
            if (buf.length >= 4) {
              if (buf[0] === 0xff && buf[1] === 0xd8 && buf[2] === 0xff) realMime = 'image/jpeg';
              else if (buf[0] === 0x89 && buf[1] === 0x50 && buf[2] === 0x4e && buf[3] === 0x47) realMime = 'image/png';
              else if (buf[0] === 0x47 && buf[1] === 0x49 && buf[2] === 0x46 && buf[3] === 0x38) realMime = 'image/gif';
              else if (buf.length >= 12 && buf[0] === 0x52 && buf[1] === 0x49 && buf[2] === 0x46 && buf[3] === 0x46 && buf[8] === 0x57 && buf[9] === 0x45 && buf[10] === 0x42 && buf[11] === 0x50) realMime = 'image/webp';
            }
            if (buf.length > 0) {
              imageAttachment = await ctx.attachments.saveImage({
                data: new Uint8Array(buf),
                mediaType: realMime,
              });
            }
          } catch {
            // Non-fatal if image normalization rejects; text JSON still returns
          }
        }

        opts.onToolEvent?.(sessionId, {
          type: 'tool_end',
          tool: toolName,
          call_id: String(exec.callId || ''),
          ok: !Boolean(resultObj?.error || resultObj?.ok === false),
          result: resultObj,
          has_image: Boolean(imageAttachment),
          duration_ms: Date.now() - t0,
        });

        if (resultObj && typeof resultObj === 'object' && !Array.isArray(resultObj)) {
          return {
            ...resultObj,
            ...(imageAttachment ? { _imageAttachment: imageAttachment } : {}),
          };
        }
        return {
          ok: true,
          value: resultObj,
          ...(imageAttachment ? { _imageAttachment: imageAttachment } : {}),
        };
      },
    });
    disposers.set(toolName, dispose);
  };

  for (const t of DEFAULT_TOOL_SCHEMAS) {
    registerToolSchema(t);
  }

  return {
    syncTools(toolsList) {
      if (!Array.isArray(toolsList) || toolsList.length === 0) return;
      for (const t of toolsList) {
        registerToolSchema(t);
      }
    },
  };
}
