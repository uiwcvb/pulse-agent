/**
 * [Module Responsibility]
 * DSB PC 守护端智能双路云端传输与环形诊断日志模块 (`lib-sentinel-net.mjs`)。
 * 负责：
 * 1. 自动读取 `~/.dsh/.env` 代理配置，提供「直连 (Direct) ↔ 本地代理 (Proxy 7890)」毫秒级双路自动切换与故障自愈，
 *    彻底解决开/关代理软件或跨国链路抖动导致的 PC 失联问题。
 * 2. 提供有界环形日志文件 (`~/.dsh/sentinel.log`, 256KB 自动轮转) 与内存最近日志队列，告别静默吞错。
 * 3. 管理长轮询控制器 (`activeInboxAbortCtrl`)，支持托盘小程序一键「立即重连」0ms 打断挂起连接并重握手。
 */
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import net from "node:net";
import http from "node:http";
import https from "node:https";
import tls from "node:tls";
import zlib from "node:zlib";

const keepAliveHttpAgent = new http.Agent({ keepAlive: true, keepAliveMsecs: 15000, maxSockets: 16 });
const keepAliveHttpsAgent = new https.Agent({ keepAlive: true, keepAliveMsecs: 15000, maxSockets: 16 });

function decodeHttpBody(res, chunks) {
  const buf = Buffer.concat(chunks);
  const enc = String(res?.headers?.["content-encoding"] || "").toLowerCase();
  try {
    if (enc.includes("gzip")) return zlib.gunzipSync(buf).toString("utf8");
    if (enc.includes("deflate")) return zlib.inflateSync(buf).toString("utf8");
  } catch {}
  return buf.toString("utf8");
}

const DSH_HOME = path.join(os.homedir(), ".dsh");
const LOG_FILE = path.join(DSH_HOME, "sentinel.log");
const MAX_LOG_BYTES = 256 * 1024;
const recentLogs = [];

export const netTelemetry = {
  activeRoute: "direct",
  proxyCandidate: "http://127.0.0.1:7890",
  proxyAlive: false,
  proxyCheckedAt: 0,
  lastRttMs: 0,
  lastOkAtMs: 0,
  lastError: "",
  lastErrorAtMs: 0,
  consecutiveFailures: 0,
  reconnectSeq: 0,
};

let activeInboxAbortCtrl = null;

export function logSentinel(level, msg, extra = null) {
  const ts = new Date().toLocaleString("zh-CN", { hour12: false });
  const suffix = extra ? ` | ${typeof extra === "string" ? extra : JSON.stringify(extra)}` : "";
  const line = `[${ts}] [${String(level || "INFO").toUpperCase()}] ${msg}${suffix}`;
  recentLogs.push(line);
  if (recentLogs.length > 60) recentLogs.shift();
  try {
    fs.mkdirSync(DSH_HOME, { recursive: true });
    try {
      const st = fs.existsSync(LOG_FILE) ? fs.statSync(LOG_FILE) : null;
      if (st && st.size > MAX_LOG_BYTES) {
        const tail = fs.readFileSync(LOG_FILE, "utf8").slice(-Math.floor(MAX_LOG_BYTES * 0.5));
        fs.writeFileSync(LOG_FILE, tail, "utf8");
      }
    } catch {}
    fs.appendFileSync(LOG_FILE, line + "\n", "utf8");
  } catch {}
}

export function getRecentSentinelLogs(limit = 25) {
  return recentLogs.slice(-Math.max(1, Math.min(60, limit)));
}

export function getSentinelLogPath() {
  return LOG_FILE;
}

export function registerInboxAbortController(ctrl) {
  activeInboxAbortCtrl = ctrl;
}

export function triggerInstantReconnect(reason = "manual") {
  netTelemetry.reconnectSeq += 1;
  netTelemetry.proxyCheckedAt = 0;
  logSentinel("INFO", `触发即时重连 (${reason}) #${netTelemetry.reconnectSeq}`);
  if (activeInboxAbortCtrl) {
    try {
      activeInboxAbortCtrl.abort(new Error(`instant_reconnect:${reason}`));
    } catch {}
    activeInboxAbortCtrl = null;
  }
  return netTelemetry.reconnectSeq;
}

function discoverProxyUrl() {
  const envProxy = process.env.HTTPS_PROXY || process.env.HTTP_PROXY || process.env.ALL_PROXY || "";
  if (envProxy && /^https?:\/\//i.test(envProxy.trim())) {
    return envProxy.trim();
  }
  try {
    const envPath = path.join(DSH_HOME, ".env");
    if (fs.existsSync(envPath)) {
      const lines = fs.readFileSync(envPath, "utf8").split(/\r?\n/);
      for (const ln of lines) {
        const m = ln.match(/^\s*(?:HTTPS_PROXY|HTTP_PROXY)\s*=\s*([^\s#]+)/i);
        if (m && /^https?:\/\//i.test(m[1])) return m[1].trim();
      }
    }
  } catch {}
  return "http://127.0.0.1:7890";
}

async function checkProxyPortAlive(proxyUrlStr) {
  const now = Date.now();
  if (now - netTelemetry.proxyCheckedAt < 10000) return netTelemetry.proxyAlive;
  netTelemetry.proxyCheckedAt = now;
  try {
    const u = new URL(proxyUrlStr);
    const port = Number(u.port || (u.protocol === "https:" ? 443 : 80));
    const host = u.hostname || "127.0.0.1";
    const alive = await new Promise((resolve) => {
      const sock = net.connect({ host, port });
      const done = (ok) => {
        try { sock.destroy(); } catch {}
        resolve(ok);
      };
      sock.setTimeout(180);
      sock.once("connect", () => done(true));
      sock.once("timeout", () => done(false));
      sock.once("error", () => done(false));
    });
    netTelemetry.proxyAlive = alive;
    return alive;
  } catch {
    netTelemetry.proxyAlive = false;
    return false;
  }
}

function rawHttpRequestDirect(targetUrlObj, method, headers, bodyBuf, timeoutMs, externalSignal) {
  return new Promise((resolve, reject) => {
    if (externalSignal?.aborted) {
      return reject(externalSignal.reason || new Error("Request aborted"));
    }
    const isHttps = targetUrlObj.protocol === "https:";
    const mod = isHttps ? https : http;
    const reqHeaders = { "Accept-Encoding": "gzip", ...headers, Connection: "keep-alive" };
    if (bodyBuf) reqHeaders["Content-Length"] = bodyBuf.length;

    const req = mod.request(
      {
        protocol: targetUrlObj.protocol,
        hostname: targetUrlObj.hostname,
        port: targetUrlObj.port || (isHttps ? 443 : 80),
        path: `${targetUrlObj.pathname}${targetUrlObj.search}`,
        method,
        headers: reqHeaders,
        agent: isHttps ? keepAliveHttpsAgent : keepAliveHttpAgent,
      },
      (res) => {
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => {
          resolve({ status: res.statusCode || 0, text: decodeHttpBody(res, chunks) });
        });
        res.on("error", reject);
      }
    );

    const timer = setTimeout(() => {
      req.destroy(new Error(`Direct timeout after ${timeoutMs}ms`));
    }, timeoutMs);

    const onAbort = () => {
      clearTimeout(timer);
      req.destroy(externalSignal?.reason || new Error("Aborted"));
    };
    if (externalSignal) externalSignal.addEventListener("abort", onAbort, { once: true });

    req.on("error", (err) => {
      clearTimeout(timer);
      if (externalSignal) externalSignal.removeEventListener("abort", onAbort);
      reject(err);
    });
    req.on("close", () => {
      clearTimeout(timer);
      if (externalSignal) externalSignal.removeEventListener("abort", onAbort);
    });

    if (bodyBuf) req.write(bodyBuf);
    req.end();
  });
}

function rawHttpRequestViaHttpProxy(targetUrlObj, proxyUrlObj, method, headers, bodyBuf, timeoutMs, externalSignal) {
  return new Promise((resolve, reject) => {
    if (externalSignal?.aborted) {
      return reject(externalSignal.reason || new Error("Request aborted"));
    }
    const proxyHost = proxyUrlObj.hostname || "127.0.0.1";
    const proxyPort = Number(proxyUrlObj.port || 7890);
    const isTargetHttps = targetUrlObj.protocol === "https:";

    if (!isTargetHttps) {
      const reqHeaders = {
        "Accept-Encoding": "gzip",
        ...headers,
        Host: targetUrlObj.host,
        Connection: "close",
        "Proxy-Connection": "close",
      };
      if (bodyBuf) reqHeaders["Content-Length"] = bodyBuf.length;
      const req = http.request(
        {
          hostname: proxyHost,
          port: proxyPort,
          path: targetUrlObj.toString(),
          method,
          headers: reqHeaders,
          agent: false,
        },
        (res) => {
          const chunks = [];
          res.on("data", (c) => chunks.push(c));
          res.on("end", () => {
            resolve({ status: res.statusCode || 0, text: decodeHttpBody(res, chunks) });
          });
          res.on("error", reject);
        }
      );
      const timer = setTimeout(() => req.destroy(new Error(`Proxy timeout after ${timeoutMs}ms`)), timeoutMs);
      const onAbort = () => {
        clearTimeout(timer);
        req.destroy(externalSignal?.reason || new Error("Aborted"));
      };
      if (externalSignal) externalSignal.addEventListener("abort", onAbort, { once: true });
      req.on("error", (e) => {
        clearTimeout(timer);
        if (externalSignal) externalSignal.removeEventListener("abort", onAbort);
        reject(e);
      });
      req.on("close", () => {
        clearTimeout(timer);
        if (externalSignal) externalSignal.removeEventListener("abort", onAbort);
      });
      if (bodyBuf) req.write(bodyBuf);
      req.end();
      return;
    }

    // HTTPS over HTTP CONNECT tunnel
    const targetPort = Number(targetUrlObj.port || 443);
    const connectReq = http.request({
      hostname: proxyHost,
      port: proxyPort,
      method: "CONNECT",
      path: `${targetUrlObj.hostname}:${targetPort}`,
      headers: { Host: `${targetUrlObj.hostname}:${targetPort}`, "Proxy-Connection": "close" },
      agent: false,
    });
    const timer = setTimeout(() => connectReq.destroy(new Error(`CONNECT timeout after ${timeoutMs}ms`)), timeoutMs);
    const onAbort = () => {
      clearTimeout(timer);
      connectReq.destroy(externalSignal?.reason || new Error("Aborted"));
    };
    if (externalSignal) externalSignal.addEventListener("abort", onAbort, { once: true });

    connectReq.on("connect", (res, socket) => {
      if (res.statusCode !== 200) {
        clearTimeout(timer);
        socket.destroy();
        return reject(new Error(`Proxy CONNECT HTTP ${res.statusCode}`));
      }
      const tlsSock = tls.connect({ socket, servername: targetUrlObj.hostname }, () => {
        const reqHeaders = { "Accept-Encoding": "gzip", ...headers, Host: targetUrlObj.host, Connection: "close" };
        if (bodyBuf) reqHeaders["Content-Length"] = bodyBuf.length;
        let head = `${method} ${targetUrlObj.pathname}${targetUrlObj.search} HTTP/1.1\r\n`;
        for (const [k, v] of Object.entries(reqHeaders)) head += `${k}: ${v}\r\n`;
        head += "\r\n";
        tlsSock.write(head);
        if (bodyBuf) tlsSock.write(bodyBuf);
      });
      const rawChunks = [];
      let totalLen = 0;
      let headerEndIdx = -1;
      let parsedStatus = 200;
      let parsedHeaders = {};
      let contentLength = null;
      let isChunked = false;
      let settled = false;

      const parseHeadersMap = (hdrStr) => {
        const lines = hdrStr.split("\r\n");
        const m = lines[0]?.match(/^HTTP\/\d\.\d\s+(\d+)/);
        const st = m ? Number(m[1]) : 200;
        const map = {};
        for (let i = 1; i < lines.length; i++) {
          const col = lines[i].indexOf(":");
          if (col > 0) {
            const k = lines[i].slice(0, col).trim().toLowerCase();
            const v = lines[i].slice(col + 1).trim();
            map[k] = v;
          }
        }
        return { st, map };
      };

      const decodeChunked = (buf) => {
        const decoded = [];
        let pos = 0;
        while (pos < buf.length) {
          const crlf = buf.indexOf("\r\n", pos);
          if (crlf < 0) return { complete: false, body: null };
          const sizeLine = buf.subarray(pos, crlf).toString("ascii").split(";")[0].trim();
          const sz = parseInt(sizeLine, 16);
          if (Number.isNaN(sz)) return { complete: false, body: null };
          if (sz === 0) {
            return { complete: true, body: Buffer.concat(decoded) };
          }
          const dataStart = crlf + 2;
          const dataEnd = dataStart + sz;
          if (dataEnd + 2 > buf.length) return { complete: false, body: null };
          decoded.push(buf.subarray(dataStart, dataEnd));
          pos = dataEnd + 2;
        }
        return { complete: false, body: null };
      };

      const finishWithBuffer = (full) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (externalSignal) externalSignal.removeEventListener("abort", onAbort);
        const sep = headerEndIdx >= 0 ? headerEndIdx : full.indexOf("\r\n\r\n");
        if (sep < 0) {
          try { tlsSock.destroy(); } catch {}
          return reject(new Error("Malformed HTTPS response via proxy"));
        }
        if (headerEndIdx < 0) {
          const parsed = parseHeadersMap(full.subarray(0, sep).toString("utf8"));
          parsedStatus = parsed.st;
          parsedHeaders = parsed.map;
          isChunked = /chunked/i.test(parsedHeaders["transfer-encoding"] || "");
        }
        let bodySlice = full.subarray(sep + 4);
        if (isChunked) {
          const dec = decodeChunked(bodySlice);
          if (dec.body) bodySlice = dec.body;
        } else if (contentLength !== null && bodySlice.length > contentLength) {
          bodySlice = bodySlice.subarray(0, contentLength);
        }
        try { tlsSock.destroy(); } catch {}
        resolve({ status: parsedStatus, text: decodeHttpBody({ headers: parsedHeaders }, [bodySlice]) });
      };

      tlsSock.on("data", (c) => {
        if (settled) return;
        rawChunks.push(c);
        totalLen += c.length;
        const full = Buffer.concat(rawChunks, totalLen);
        if (headerEndIdx < 0) {
          const sep = full.indexOf("\r\n\r\n");
          if (sep < 0) return;
          headerEndIdx = sep;
          const parsed = parseHeadersMap(full.subarray(0, sep).toString("utf8"));
          parsedStatus = parsed.st;
          parsedHeaders = parsed.map;
          isChunked = /chunked/i.test(parsedHeaders["transfer-encoding"] || "");
          const clRaw = parsedHeaders["content-length"];
          if (clRaw !== undefined && /^\d+$/.test(clRaw)) contentLength = Number(clRaw);
        }
        const bodyLen = totalLen - (headerEndIdx + 4);
        if (parsedStatus === 204 || parsedStatus === 304 || method === "HEAD") {
          return finishWithBuffer(full);
        }
        if (contentLength !== null && bodyLen >= contentLength) {
          return finishWithBuffer(full);
        }
        if (isChunked) {
          const bodySlice = full.subarray(headerEndIdx + 4);
          if (decodeChunked(bodySlice).complete) {
            return finishWithBuffer(full);
          }
        }
      });
      tlsSock.on("end", () => {
        if (!settled) finishWithBuffer(Buffer.concat(rawChunks, totalLen));
      });
      tlsSock.on("error", (err) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (externalSignal) externalSignal.removeEventListener("abort", onAbort);
        reject(err);
      });
    });
    connectReq.on("error", (err) => {
      clearTimeout(timer);
      if (externalSignal) externalSignal.removeEventListener("abort", onAbort);
      reject(err);
    });
    connectReq.end();
  });
}

export async function smartCloudFetchJson(fullUrl, method = "GET", headers = {}, bodyObj = null, timeoutMs = 30000, externalSignal = null) {
  const targetUrl = new URL(fullUrl);
  const bodyBuf = bodyObj !== null && bodyObj !== undefined && method !== "GET"
    ? Buffer.from(JSON.stringify(bodyObj), "utf8")
    : null;
  const proxyStr = discoverProxyUrl();
  netTelemetry.proxyCandidate = proxyStr;
  const proxyAvailable = await checkProxyPortAlive(proxyStr);
  const proxyUrlObj = proxyAvailable ? new URL(proxyStr) : null;

  const waitSec = Number(bodyObj?.wait_sec || 0);
  const routes = [];
  if (netTelemetry.activeRoute.startsWith("proxy") && proxyUrlObj) {
    routes.push("proxy", "direct");
  } else {
    routes.push("direct");
    if (proxyUrlObj) routes.push("proxy");
  }

  let lastErr = null;
  for (let i = 0; i < routes.length; i++) {
    const rMode = routes[i];
    if (externalSignal?.aborted) throw externalSignal.reason || new Error("Aborted");
    const minWaitTimeout = waitSec > 0 ? (waitSec * 1000 + 8500) : 8500;
    const perAttemptTimeout = routes.length > 1 && i === 0
      ? Math.min(timeoutMs, Math.max(minWaitTimeout, Math.floor(timeoutMs * 0.75)))
      : timeoutMs;
    const t0 = Date.now();
    try {
      const raw = rMode === "proxy" && proxyUrlObj
        ? await rawHttpRequestViaHttpProxy(targetUrl, proxyUrlObj, method, headers, bodyBuf, perAttemptTimeout, externalSignal)
        : await rawHttpRequestDirect(targetUrl, method, headers, bodyBuf, perAttemptTimeout, externalSignal);

      if (raw.status === 502 || raw.status === 504 || raw.status === 0) {
        throw new Error(`Upstream gateway HTTP ${raw.status}`);
      }
      const rawRtt = Math.max(1, Date.now() - t0);
      const rtt = waitSec > 0 && rawRtt >= waitSec * 950
        ? Math.max(12, rawRtt - waitSec * 1000)
        : rawRtt;
      const nextRouteLabel = rMode === "proxy" ? `proxy (${proxyUrlObj.host})` : "direct";
      if (i > 0 || !netTelemetry.activeRoute || (rMode === "direct" && !proxyUrlObj)) {
        const prevRoute = netTelemetry.activeRoute;
        netTelemetry.activeRoute = nextRouteLabel;
        if (prevRoute && prevRoute !== nextRouteLabel) {
          logSentinel("INFO", `云端路由故障切换: ${prevRoute} -> ${nextRouteLabel} (RTT ${rtt}ms)`);
        }
      }
      netTelemetry.lastRttMs = rtt;
      netTelemetry.lastOkAtMs = Date.now();
      netTelemetry.lastError = "";
      netTelemetry.consecutiveFailures = 0;
      let parsed = {};
      try {
        parsed = raw.text ? JSON.parse(raw.text) : {};
      } catch {
        parsed = { raw: raw.text };
      }
      if (raw.status < 200 || raw.status >= 300) {
        return { ok: false, status: raw.status, error: parsed?.detail || parsed?.error || `HTTP ${raw.status}`, ...parsed };
      }
      return { ok: true, ...parsed };
    } catch (err) {
      lastErr = err;
      if (externalSignal?.aborted) break;
    }
  }

  const errMsg = String(lastErr?.message || lastErr || "Network unreachable");
  if (!String(errMsg).includes("instant_reconnect")) {
    netTelemetry.consecutiveFailures += 1;
    netTelemetry.lastError = errMsg;
    netTelemetry.lastErrorAtMs = Date.now();
    if (netTelemetry.consecutiveFailures === 1 || netTelemetry.consecutiveFailures % 5 === 0) {
      logSentinel("WARN", `云端通信失败 (#${netTelemetry.consecutiveFailures}): ${errMsg}`);
    }
  }
  throw lastErr || new Error(errMsg);
}
