import { createServer } from 'node:http';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { errorChain } from '@deepseek-ai/dsh-llm';
import { createDshSidecarKernel } from './kernel.mjs';

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (c) => {
      size += c.length;
      if (size > 20 * 1024 * 1024) {
        reject(new Error('Body too large'));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8').trim();
      if (!raw) {
        resolve({});
        return;
      }
      try {
        resolve(JSON.parse(raw));
      } catch (err) {
        reject(err);
      }
    });
    req.on('error', reject);
  });
}

/**
 * Start the HTTP NDJSON DSH Brain Sidecar server bound strictly to `127.0.0.1`.
 */
export async function startDshSidecarServer(opts = {}) {
  const host = opts.host || '127.0.0.1';
  const port = Number(opts.port ?? process.env.DSH_BRAIN_PORT ?? 18199);
  const dataDir = path.resolve(opts.dataDir || process.env.DSH_BRAIN_DATA_DIR || path.join(process.cwd(), 'dsh_sessions'));
  const secret = String(opts.secret ?? process.env.DSH_BRAIN_SECRET ?? '');

  const kernel = await createDshSidecarKernel({
    dataDir,
    pyPort: opts.pyPort ?? Number(process.env.PY_GATEWAY_PORT || 18190),
    pySecret: opts.pySecret ?? process.env.DSH_INTERNAL_SECRET ?? '',
    fetchImpl: opts.fetchImpl,
    customAdapter: opts.customAdapter,
  });

  /** Per-session queue to serialize concurrent HTTP turn calls on the same session_key cleanly */
  /** @type {Map<string, Promise<any>>} */
  const turnChains = new Map();

  const server = createServer(async (req, res) => {
    const url = new URL(req.url || '/', `http://${host}:${port}`);
    if (secret) {
      const got = req.headers['x-dsh-secret'] || req.headers['x-internal-secret'] || '';
      if (got !== secret) {
        res.writeHead(401, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: 'Unauthorized' }));
        return;
      }
    }

    if (req.method === 'GET' && url.pathname === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(kernel.statusSummary()));
      return;
    }

    if (req.method === 'POST' && url.pathname === '/turn/cancel') {
      try {
        const body = await readJsonBody(req);
        const key = String(body.session_key || `${body.source || 'phone'}__${body.bot_id || 'companion'}`);
        const out = kernel.cancelTurn(key);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(out));
      } catch (err) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: err instanceof Error ? err.message : String(err) }));
      }
      return;
    }

    if (req.method === 'POST' && url.pathname === '/session/reset') {
      try {
        const body = await readJsonBody(req);
        const key = String(body.session_key || `${body.source || 'phone'}__${body.bot_id || 'companion'}`);
        const out = await kernel.resetSession(key);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(out));
      } catch (err) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: err instanceof Error ? err.message : String(err) }));
      }
      return;
    }

    if (req.method === 'POST' && url.pathname === '/turn/stream') {
      let body;
      try {
        body = await readJsonBody(req);
      } catch (err) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: err instanceof Error ? err.message : String(err) }));
        return;
      }

      const sessionKey = String(body.session_key || `${body.source || 'phone'}__${body.bot_id || 'companion'}`);
      res.writeHead(200, {
        'Content-Type': 'application/x-ndjson; charset=utf-8',
        'Cache-Control': 'no-cache',
        Connection: 'keep-alive',
      });

      const writeLine = (obj) => {
        if (res.writableEnded || res.destroyed) return;
        try {
          res.write(`${JSON.stringify(obj)}\n`);
        } catch {
          // ignore closed socket
        }
      };

      const onClientClose = () => {
        if (!res.writableEnded) {
          kernel.cancelTurn(sessionKey);
        }
      };
      req.on('close', onClientClose);

      const prev = turnChains.get(sessionKey) || Promise.resolve();
      const next = prev.catch(() => {}).then(async () => {
        try {
          const result = await kernel.runTurn(body, (evt) => writeLine(evt));
          writeLine(result);
        } catch (err) {
          writeLine({
            type: 'error',
            session_key: sessionKey,
            error: errorChain(err) || (err instanceof Error ? err.message : String(err)),
          });
        } finally {
          req.off('close', onClientClose);
          if (!res.writableEnded) res.end();
        }
      });
      turnChains.set(sessionKey, next);
      await next;
      if (turnChains.get(sessionKey) === next) {
        turnChains.delete(sessionKey);
      }
      return;
    }

    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: false, error: 'Not Found' }));
  });

  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, host, () => {
      server.off('error', reject);
      resolve();
    });
  });

  const addr = server.address();
  const actualPort = typeof addr === 'object' && addr ? addr.port : port;

  return {
    host,
    port: actualPort,
    kernel,
    async close() {
      await new Promise((resolve) => server.close(() => resolve()));
      await kernel.close();
    },
  };
}

if (import.meta.url === pathToFileURL(process.argv[1] || '').href) {
  const srv = await startDshSidecarServer();
  console.log(`[dsh-brain] listening on http://${srv.host}:${srv.port}`);
  const shutdown = async () => {
    await srv.close();
    process.exit(0);
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}
