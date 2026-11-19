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
  'web',
  'memory',
  'subtask',
]);

/**
 * Built-in fallback tool definitions matching `server/core/tool_schemas.py`
 * (Python caller may also pass dynamic schemas including custom plugin tools).
 */
export const DEFAULT_TOOL_SCHEMAS = [
  {
    name: 'read_file',
    description: '读取工作区文本文件、列目录，或直接视觉识别本地图片 (png/jpg/webp/bmp/gif)',
    parameters: { type: 'object', properties: { path: { type: 'string' }, offset: { type: 'integer' }, limit: { type: 'integer' }, max_chars: { type: 'integer' } }, additionalProperties: true },
  },
  {
    name: 'write_file',
    description: '创建或完整覆写工作区文件（支持 uploads/、notes/、memory_vault/、workspace/）',
    parameters: { type: 'object', properties: { path: { type: 'string' }, content: { type: 'string' } }, required: ['path', 'content'], additionalProperties: true },
  },
  {
    name: 'edit_file',
    description: '编辑、重命名或删除工作区文件 (op="replace"|"move"|"delete")',
    parameters: { type: 'object', properties: { op: { type: 'string' }, path: { type: 'string' }, old: { type: 'string' }, new: { type: 'string' }, dst: { type: 'string' } }, required: ['path'], additionalProperties: true },
  },
  {
    name: 'search_files',
    description: '工作区检索工具 (op="grep"|"glob"|"list")，默认查找当前目录及记忆库',
    parameters: { type: 'object', properties: { op: { type: 'string' }, path: { type: 'string' }, query: { type: 'string' }, pattern: { type: 'string' }, max_results: { type: 'integer' } }, additionalProperties: true },
  },
  {
    name: 'bash',
    description: '执行命令、后台任务与 0-Token 定时哨兵 (op="run"|"bg_start"|"bg_logs"|"bg_stdin"|"bg_kill"|"bg_list"|"watchdog_start"|"watchdog_stop"|"watchdog_logs"|"watchdog_run_now"|"watchdog_list")',
    parameters: { type: 'object', properties: { op: { type: 'string' }, command: { type: 'string' }, timeout: { type: 'integer' }, cwd: { type: 'string' } }, additionalProperties: true },
  },
  {
    name: 'web',
    description: '联网搜索公开资料与抓取网页正文 (op="search"|"fetch")',
    parameters: { type: 'object', properties: { op: { type: 'string' }, query: { type: 'string' }, url: { type: 'string' } }, additionalProperties: true },
  },
  {
    name: 'memory',
    description: '长期记忆与置顶规则 (op="search"|"conversation"|"ctx"|"pin"|"unpin"|"list_pins")',
    parameters: { type: 'object', properties: { op: { type: 'string' }, query: { type: 'string' }, text: { type: 'string' }, pin_id: { type: 'string' } }, additionalProperties: true },
  },
  {
    name: 'blackboard',
    description: '事件驱动共享黑板、跨端 @同事 协作与定时闹钟编排 (action="post"|"read"|"peek"|"claim"|"complete"|"review"|"deliver"|"approve"|"reject"|"schedule"|"alarm"|"cancel"|"list"|"clear_done")',
    parameters: { type: 'object', properties: { action: { type: 'string' }, text: { type: 'string' }, target: { type: 'string' }, post_id: { type: 'string' }, mentions: { type: 'array', items: { type: 'string' } }, cron_expr: { type: 'string' } }, additionalProperties: true },
  },
  {
    name: 'phone',
    description: '手机隔离虚拟屏 VirtualDisplay 与通知/验证码操控；纯视觉 0..1000 归一化坐标 (nx, ny) 与区域框 box=[nx1,ny1,nx2,ny2]',
    parameters: { type: 'object', properties: { op: { type: 'string' }, nx: { type: 'integer' }, ny: { type: 'integer' }, nx2: { type: 'integer' }, ny2: { type: 'integer' }, box: { type: 'array', items: { type: 'integer' } }, text: { type: 'string' }, target: { type: 'string' }, app: { type: 'string' }, direction: { type: 'string' }, command: { type: 'string' } }, required: ['op'], additionalProperties: true },
  },
  {
    name: 'pc',
    description: 'Windows 电脑端 DSH 协同（桌面状态、0..1000 坐标 CUA 键鼠截图、本机 Shell 与 Session 派单）',
    parameters: { type: 'object', properties: { op: { type: 'string' }, command: { type: 'string' }, nx: { type: 'integer' }, ny: { type: 'integer' }, box: { type: 'array', items: { type: 'integer' } }, text: { type: 'string' }, target: { type: 'string' } }, required: ['op'], additionalProperties: true },
  },
  {
    name: 'cloud',
    description: '24/7 共享 Linux 云电脑（桌面、浏览器、Shell、zoom 局部放大、send_screenshot、文件传输）；统一采用 0..1000 坐标 (nx, ny) 与 box=[nx1,ny1,nx2,ny2]',
    parameters: { type: 'object', properties: { op: { type: 'string' }, nx: { type: 'integer' }, ny: { type: 'integer' }, nx2: { type: 'integer' }, ny2: { type: 'integer' }, box: { type: 'array', items: { type: 'integer' } }, ref: { type: 'string' }, command: { type: 'string' }, url: { type: 'string' }, text: { type: 'string' }, direction: { type: 'string' }, full_page: { type: 'boolean' } }, required: ['op'], additionalProperties: true },
  },
  {
    name: 'reply',
    description: '交互控制与分级通知 (op="notify"|"alarm"|"send_image"|"send_file"|"choices"|"silent"|"dnd"|"complete_goal")；需提供快捷按钮时调用 op="choices" 传入 2~3 个用户第一人称点击短句，并在本轮输出完整正文',
    parameters: { type: 'object', properties: { op: { type: 'string' }, path: { type: 'string' }, name: { type: 'string' }, caption: { type: 'string' }, choices: { type: 'array', items: { type: 'string' } }, title: { type: 'string' }, body: { type: 'string' }, level: { type: 'string' }, urgent: { type: 'boolean' }, overlay: { type: 'boolean' }, summary: { type: 'string' } }, required: ['op'], additionalProperties: true },
  },
  {
    name: 'subtask',
    description: '在独立隔离上下文运行聚焦子任务（查源码/长日志/网页核对）并折叠返回精简结论',
    parameters: { type: 'object', properties: { goal: { type: 'string' }, mode: { type: 'string' }, context_paths: { type: 'array', items: { type: 'string' } }, max_steps: { type: 'integer' } }, required: ['goal'], additionalProperties: true },
  },
  {
    name: 'admin',
    description: '系统运维、配置、Bot管理、Skill/插件、模型列表与云电脑部署 (op="status"|"doctor"|"context"|"config"|"bot"|"skill"|"custom_tool"|"models"|"deploy_cloud")',
    parameters: { type: 'object', properties: { op: { type: 'string' }, action: { type: 'string' }, key: { type: 'string' }, value: { type: 'string' } }, required: ['op'], additionalProperties: true },
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
          const { _imageAttachment, ...cleanMeta } = /** @type {any} */ (value);
          const blocks = [{ type: 'text', text: JSON.stringify(cleanMeta) }];
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
