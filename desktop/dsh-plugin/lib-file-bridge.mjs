/**
 * [Module Responsibility]
 * DSH PC 端与云端/云电脑跨端文件同步与上传桥接 (`lib-file-bridge.mjs`)。
 * 统一三端文件路径解析、单次/分片无强制 SHA256 上传、VPS 推送镜像、本机工作区/全局目录遍历。
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";

export function getDefaultPcWorkspaceDir() {
  const envWs = String(process.env.DSH_PC_WORKSPACE || "").trim();
  if (envWs) return envWs;
  const dshWs = path.join(os.homedir(), "Documents", "deepseek-harness", "default-workspace");
  try {
    if (fs.existsSync(dshWs)) return dshWs;
  } catch {}
  return dshWs;
}

export function getPcUploadsDir() {
  const dir = path.join(getDefaultPcWorkspaceDir(), "uploads");
  try {
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
  } catch {}
  return dir;
}

export function stripUploadPrefix(name) {
  const base = path.basename(String(name || "").replace(/\\/g, "/"));
  const clean = base.replace(/^[0-9a-f]{10}-/, "");
  return clean || base;
}

export function resolveSmartPcPath(rawPath, workspaceRoot) {
  const raw = String(rawPath || "").trim();
  const root = path.resolve(workspaceRoot || getDefaultPcWorkspaceDir());
  if (!raw || raw === ".") return root;
  const expanded = raw.startsWith("~/") || raw.startsWith("~\\") ? path.join(os.homedir(), raw.slice(2)) : raw;
  if (path.isAbsolute(expanded)) return path.resolve(expanded);
  const unixRel = expanded.replace(/\\/g, "/");
  const candidates = [];
  if (unixRel.startsWith("workspace/")) {
    candidates.push(path.join(root, unixRel.slice("workspace/".length)));
  }
  candidates.push(path.join(root, unixRel));
  if (!unixRel.includes("/")) {
    candidates.push(path.join(root, "uploads", unixRel));
  }
  const uploadsTail = unixRel.startsWith("workspace/uploads/")
    ? unixRel.slice("workspace/uploads/".length)
    : unixRel.startsWith("uploads/")
    ? unixRel.slice("uploads/".length)
    : (!unixRel.includes("/") ? unixRel : "");
  if (uploadsTail && !uploadsTail.includes("/")) {
    const clean = stripUploadPrefix(uploadsTail);
    if (clean) candidates.push(path.join(root, "uploads", clean));
    const uploadsDir = path.join(root, "uploads");
    if (fs.existsSync(uploadsDir)) {
      try {
        const rx = new RegExp("^[0-9a-f]{10}-" + uploadsTail.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "$");
        const legacy = fs.readdirSync(uploadsDir)
          .filter((n) => rx.test(n))
          .map((n) => ({ name: n, mtime: fs.statSync(path.join(uploadsDir, n)).mtimeMs }))
          .sort((a, b) => b.mtime - a.mtime);
        if (legacy.length > 0) candidates.push(path.join(uploadsDir, legacy[0].name));
      } catch {}
    }
  }
  for (const cand of candidates) {
    try {
      if (fs.existsSync(cand)) return path.resolve(cand);
    } catch {}
  }
  return path.resolve(candidates[0]);
}

export function walkPcDir(rootDir, maxDepth = 3, maxEntries = 250) {
  const results = [];
  function visit(currDir, depth) {
    if (results.length >= maxEntries || depth > maxDepth) return;
    let entries = [];
    try { entries = fs.readdirSync(currDir, { withFileTypes: true }); } catch { return; }
    for (const ent of entries) {
      if (results.length >= maxEntries) break;
      if (ent.name === ".git" || ent.name === "node_modules" || ent.name === "__pycache__") continue;
      const full = path.join(currDir, ent.name);
      const rel = path.relative(rootDir, full).replace(/\\/g, "/");
      results.push({ name: rel || ent.name, path: full, type: ent.isDirectory() ? "dir" : "file" });
      if (ent.isDirectory() && depth < maxDepth) visit(full, depth + 1);
    }
  }
  visit(rootDir, 1);
  return results;
}

export function globToRegExp(globPat) {
  const norm = String(globPat || "*").replace(/\\/g, "/");
  let reStr = "^";
  for (let i = 0; i < norm.length; i++) {
    const c = norm[i];
    if (c === "*") {
      if (norm[i + 1] === "*") {
        if (norm[i + 2] === "/") { reStr += "(?:.*/)?"; i += 2; }
        else { reStr += ".*"; i += 1; }
      } else { reStr += "[^/]*"; }
    } else if (c === "?") reStr += "[^/]";
    else if ("+()^$.{}[]|\\".includes(c)) reStr += "\\" + c;
    else reStr += c;
  }
  return new RegExp(reStr + "$", "i");
}

function writeAtomicBuffer(targetPath, buf) {
  fs.mkdirSync(path.dirname(targetPath), { recursive: true });
  const tmp = targetPath + "." + crypto.randomBytes(6).toString("hex") + ".tmp";
  fs.writeFileSync(tmp, buf);
  fs.renameSync(tmp, targetPath);
}

export async function pullWorkspaceFileToPc({ cloudFetch, remotePath, localTarget }) {
  if (typeof cloudFetch !== "function") return { ok: false, error: "cloudFetch 不可用" };
  const raw = String(remotePath || "").replace(/\\/g, "/").replace(/^uploads\//, "workspace/uploads/");
  const normRemote = raw.startsWith("workspace/") ? raw : "workspace/uploads/" + path.basename(raw);
  const chunks = [];
  let offset = 0;
  let version = null;
  for (let i = 0; i < 200; i++) {
    const qs = new URLSearchParams({ path: normRemote, offset: String(offset), size: "524288" });
    if (version) qs.set("version", version);
    const j = await cloudFetch(`/api/files/file?${qs.toString()}`, "GET", null, 25000).catch((e) => ({ _err: String(e?.message || e) }));
    if (!j || j._err || j.detail || typeof j.data !== "string") {
      return { ok: false, error: j?.detail || j?._err || "云端文件读取失败" };
    }
    if (!version && j.version) version = String(j.version);
    const piece = Buffer.from(String(j.data || ""), "base64");
    chunks.push(piece);
    offset += piece.length;
    if (j.eof || offset >= Number(j.bytes || 0) || piece.length === 0) break;
  }
  const fullBuf = Buffer.concat(chunks);
  const target = localTarget || path.join(getPcUploadsDir(), stripUploadPrefix(normRemote));
  writeAtomicBuffer(target, fullBuf);
  return { ok: true, localPath: target, workspacePath: "uploads/" + path.basename(target), bytes: fullBuf.length };
}

export async function syncWorkspaceFileToPc({ payload, cloudFetch, logger }) {
  const cleanName = stripUploadPrefix(payload?.name || payload?.workspace_path || payload?.path || "shared.bin");
  if (!cleanName || cleanName === "." || cleanName === "..") return { ok: false, error: "文件名无效" };
  const target = path.join(getPcUploadsDir(), cleanName);
  if (typeof payload?.data_b64 === "string" && payload.data_b64.length > 0) {
    try {
      const buf = Buffer.from(payload.data_b64, "base64");
      writeAtomicBuffer(target, buf);
      return { ok: true, localPath: target, workspacePath: "uploads/" + cleanName, bytes: buf.length };
    } catch (e) {
      logger?.warn?.(`[dsh] inline sync_uploaded_file write error: ${String(e)}`);
    }
  }
  const remotePath = String(payload?.path || payload?.workspace_path || ("workspace/uploads/" + cleanName));
  return await pullWorkspaceFileToPc({ cloudFetch, remotePath, localTarget: target });
}

const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

export async function uploadBufferToCloudAndLocal({ cloudFetch, name, bytesBuf, logger = null }) {
  const buf = Buffer.isBuffer(bytesBuf) ? bytesBuf : Buffer.from(bytesBuf || "");
  if (buf.length > MAX_UPLOAD_BYTES) throw new Error("单文件不能超过 50 MB");
  const cleanName = stripUploadPrefix(name || "upload.bin").replace(/[^a-zA-Z0-9._\u4e00-\u9fa5-]/g, "_").replace(/^\.+/, "") || "file.bin";
  const localPath = path.join(getPcUploadsDir(), cleanName);
  writeAtomicBuffer(localPath, buf);

  let cloudRes = null;
  let singleShotErr = null;
  if (typeof cloudFetch === "function") {
    if (buf.length <= 15_000_000) {
      try {
        const r1 = await cloudFetch("/api/files/upload", "POST", { name: cleanName, data: buf.toString("base64"), sha256: "" }, 30000);
        if (r1?.path) cloudRes = r1;
      } catch (e) {
        singleShotErr = e;
        logger?.warn?.(`[dsh] single-shot upload failed, falling back to chunked upload: ${String(e?.message || e)}`);
      }
    }
    if (!cloudRes) {
      let startRes;
      try {
        startRes = await cloudFetch("/api/files/uploads", "POST", { name: cleanName, size: buf.length, sha256: "" }, 20000);
      } catch (chunkErr) {
        const baseMsg = String(chunkErr?.message || chunkErr);
        const extra = singleShotErr ? ` (single-shot: ${String(singleShotErr?.message || singleShotErr)})` : "";
        throw new Error(`${baseMsg}${extra}`);
      }
      const uploadId = startRes?.id || startRes?.key;
      if (!uploadId) throw new Error(startRes?.detail || startRes?.error || "创建上传会话失败");
      const chunkSize = 360 * 1024;
      for (let off = 0; off < buf.length; off += chunkSize) {
        const slice = buf.subarray(off, Math.min(buf.length, off + chunkSize));
        const cRes = await cloudFetch(`/api/files/uploads/${encodeURIComponent(uploadId)}`, "PUT", { offset: off, data: slice.toString("base64") }, 30000);
        if (typeof cRes?.offset !== "number") {
          await cloudFetch(`/api/files/uploads/${encodeURIComponent(uploadId)}`, "DELETE", null, 8000).catch(() => {});
          throw new Error(cRes?.detail || cRes?.error || "上传分块失败");
        }
      }
      const finRes = await cloudFetch(`/api/files/uploads/${encodeURIComponent(uploadId)}/finish`, "POST", {}, 20000);
      if (!finRes?.path) throw new Error(finRes?.detail || finRes?.error || "合并上传文件失败");
      cloudRes = finRes;
    }
    if (cloudRes?.bytes !== undefined && Number(cloudRes.bytes) !== buf.length) {
      throw new Error(`上传文件字节大小不一致: expected=${buf.length}, actual=${cloudRes.bytes}`);
    }
  }

  return {
    ok: true,
    name: cleanName,
    path: cloudRes?.path || ("workspace/uploads/" + cleanName),
    workspace_path: "uploads/" + cleanName,
    local_path: localPath,
    bytes: buf.length,
    sha256: "",
  };
}

export async function handleLocalUploadRpc(body, cloudFetch, logger = null) {
  try {
    const rawName = String(body?.name || "attachment.bin");
    const rawB64 = String(body?.data_b64 || body?.data || "").replace(/^data:[^;]+;base64,/, "").trim();
    if (!rawB64) return { ok: false, error: "上传内容为空" };
    const buf = Buffer.from(rawB64, "base64");
    if (!buf.length || buf.length > MAX_UPLOAD_BYTES) return { ok: false, error: "文件大小需在 1B ~ 50MB 之间" };
    return await uploadBufferToCloudAndLocal({ cloudFetch, name: rawName, bytesBuf: buf, logger });
  } catch (err) {
    return { ok: false, error: String(err?.message || err) };
  }
}

export async function handleLocalFilesRpc(clean, body, cloudFetch) {
  if (clean === "/local/files/sync") {
    const direction = String(body?.direction || "to_cloud");
    const targetPath = String(body?.path || "").trim();
    if (!targetPath) return { ok: false, error: "missing path" };
    if (direction === "to_pc") {
      return await pullWorkspaceFileToPc({ cloudFetch, remotePath: targetPath });
    }
    const resolved = resolveSmartPcPath(targetPath, getDefaultPcWorkspaceDir());
    if (!fs.existsSync(resolved) || !fs.statSync(resolved).isFile()) {
      return { ok: false, error: `本机文件不存在: ${targetPath}` };
    }
    const up = await uploadBufferToCloudAndLocal({ cloudFetch, name: path.basename(resolved), bytesBuf: fs.readFileSync(resolved) });
    if (direction === "to_desktop" && typeof cloudFetch === "function") {
      const pushJson = await cloudFetch("/api/computer/command", "POST", { op: "push_file", path: up.path }, 30000).catch((e) => ({ ok: false, error: String(e?.message || e) }));
      return { ...up, ok: Boolean(pushJson?.ok !== false), desktop_push: pushJson };
    }
    return up;
  }

  const uploadsDir = getPcUploadsDir();
  let localFiles = [];
  try {
    localFiles = fs.readdirSync(uploadsDir)
      .map((n) => {
        const full = path.join(uploadsDir, n);
        const st = fs.statSync(full);
        return st.isFile() ? { name: n, path: "uploads/" + n, full_path: full, bytes: st.size, modified_at: st.mtimeMs / 1000 } : null;
      })
      .filter(Boolean)
      .sort((a, b) => b.modified_at - a.modified_at);
  } catch {}
  let cloudFiles = [];
  let desktopFiles = [];
  if (typeof cloudFetch === "function") {
    const cf = await cloudFetch("/api/files", "GET", null, 10000).catch(() => null);
    if (Array.isArray(cf?.files)) cloudFiles = cf.files;
    const df = await cloudFetch("/api/computer/files", "GET", null, 10000).catch(() => null);
    if (Array.isArray(df?.files)) desktopFiles = df.files;
  }
  return { ok: true, local: localFiles, cloud: cloudFiles, desktop: desktopFiles };
}

export async function executePcCloudDesktopTool(params, cloudFetch) {
  const args = { ...(params || {}) };
  const effOp = String(args.op || args.action || "status").trim();
  if (effOp === "push_file") {
    if (args.content !== undefined || args.data_b64 !== undefined) {
      const buf = args.data_b64 !== undefined ? Buffer.from(String(args.data_b64), "base64") : Buffer.from(String(args.content), "utf-8");
      const up = await uploadBufferToCloudAndLocal({ cloudFetch, name: args.name || args.path || "upload.txt", bytesBuf: buf });
      args.path = up.path;
      delete args.content;
      delete args.data_b64;
    } else {
      const rawPath = String(args.path || args.target || args.text || "").trim();
      const resolved = resolveSmartPcPath(rawPath, getDefaultPcWorkspaceDir());
      if (fs.existsSync(resolved) && fs.statSync(resolved).isFile()) {
        const up = await uploadBufferToCloudAndLocal({ cloudFetch, name: path.basename(resolved), bytesBuf: fs.readFileSync(resolved) });
        args.path = up.path;
      }
    }
  }
  const res = await cloudFetch("/api/computer/command", "POST", { ...args, op: effOp }, 85000);
  const ok = Boolean(res?.ok !== false && !res?.error);
  const txt = res?.stdout || res?.output || res?.last_bash_output || res?.snapshot || res?.result || res?.page_text || res?.message || JSON.stringify(res || {}).slice(0, 2400);
  return { ok, summary: `[云电脑 · ${effOp}]\n${String(txt).slice(0, 2800)}`, data: res || {} };
}
