import { describe, it } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..", "..", "..");

function createMiniFakeDocument() {
  class MiniClassList {
    constructor(el) { this.el = el; }
    _set() { return new Set(String(this.el.className || "").split(/\s+/).filter(Boolean)); }
    toggle(cls, force) {
      const s = this._set();
      const want = force === undefined ? !s.has(cls) : Boolean(force);
      if (want) s.add(cls); else s.delete(cls);
      this.el.className = Array.from(s).join(" ");
      return want;
    }
    contains(cls) { return this._set().has(cls); }
  }
  class MiniEl {
    constructor(tag = "div") {
      this.tagName = tag.toUpperCase();
      this.children = [];
      this.parentNode = null;
      this.attrs = {};
      this.className = "";
      this.classList = new MiniClassList(this);
      this._innerHTML = "";
    }
    set innerHTML(val) {
      this._innerHTML = String(val || "");
      this.children = [];
      const rowMatch = this._innerHTML.match(/^<div class="([^"]*)"([^>]*)>/);
      if (rowMatch && this.tagName === "DIV") {
        const child = new MiniEl("div");
        child.className = rowMatch[1];
        const attrRe = /([a-z0-9-]+)="([^"]*)"/gi;
        let m;
        while ((m = attrRe.exec(rowMatch[2])) !== null) {
          child.setAttribute(m[1], m[2]);
        }
        child.parentNode = this;
        this.children.push(child);
      }
    }
    get innerHTML() { return this._innerHTML; }
    get firstElementChild() { return this.children[0] || null; }
    get lastElementChild() { return this.children[this.children.length - 1] || null; }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
    querySelector(sel) {
      if (sel === ".dsb-empty-state") {
        return this.children.find((c) => c.classList.contains("dsb-empty-state")) || null;
      }
      return null;
    }
    insertBefore(node, ref) {
      if (node.parentNode) {
        const oldIdx = node.parentNode.children.indexOf(node);
        if (oldIdx !== -1) node.parentNode.children.splice(oldIdx, 1);
      }
      node.parentNode = this;
      if (!ref) {
        this.children.push(node);
        return node;
      }
      const idx = this.children.indexOf(ref);
      if (idx === -1) this.children.push(node);
      else this.children.splice(idx, 0, node);
      return node;
    }
    replaceChild(nextEl, oldEl) {
      const idx = this.children.indexOf(oldEl);
      if (idx === -1) return oldEl;
      nextEl.parentNode = this;
      oldEl.parentNode = null;
      this.children[idx] = nextEl;
      return oldEl;
    }
    removeChild(child) {
      const idx = this.children.indexOf(child);
      if (idx !== -1) this.children.splice(idx, 1);
      child.parentNode = null;
      return child;
    }
  }
  return {
    MiniEl,
    doc: {
      createElement: (tag) => new MiniEl(tag),
      getElementById: () => null,
      querySelectorAll: () => [],
    },
  };
}

function loadDesktopUiSandbox() {
  const { MiniEl, doc } = createMiniFakeDocument();
  const win = {
    CSS: { supports: () => true },
    location: { protocol: "http:" },
    addEventListener: () => {},
  };
  const sandbox = {
    window: win,
    document: doc,
    CSS: win.CSS,
    navigator: {},
    WeakMap,
    Map,
    Set,
    URL,
    HTMLElement: function () {},
    requestAnimationFrame: (fn) => fn(),
    setTimeout,
    clearTimeout,
    console,
  };
  win.window = win;
  const renderCode = fs.readFileSync(path.join(REPO_ROOT, "desktop/dsh-plugin/assets/dsb-render.js"), "utf8");
  const uiCode = fs.readFileSync(path.join(REPO_ROOT, "desktop/dsh-plugin/assets/client-ui.js"), "utf8");
  vm.runInNewContext(renderCode, sandbox, { filename: "dsb-render.js" });
  vm.runInNewContext(uiCode, sandbox, { filename: "client-ui.js" });
  return { R: win.__DSB_RENDER__, U: win.__DSB_CLIENT_UI__, MiniEl };
}

describe("DSH Desktop UI Renderer & Client Modules", () => {
  it("renders live and completed tool timelines without humanizeToolName ReferenceError and preserves expanded details", () => {
    const { R } = loadDesktopUiSandbox();
    const timeline = [
      { type: "text", text: "我先看下文件，稍等。" },
      { type: "tool", tool: "read_file", arguments: { path: "README.md" }, result: { ok: true }, status: "done", duration_ms: 42 },
      { type: "tool", tool: "reply", arguments: { op: "sticker", code: "wink" }, status: "running", duration_ms: 0 },
    ];
    const expanded = { "msg-1": true, "msg-1:s1": true };
    const html = R.renderUnifiedTimelineBodyHtml(timeline, "推理过程检查", "最终答复", [], true, "msg-1", expanded);
    assert.ok(html.includes('data-proc-key="msg-1" open'));
    assert.ok(html.includes('data-proc-key="msg-1:s1" open'));
    assert.ok(html.includes("附带表情贴纸 · wink"));
    assert.ok(html.includes("读取文件 · README.md"));
  });

  it("renders user & AI stickers framelessly, strips leaked sticker XML tags, adds copy aria-label, and tags compact cards", () => {
    const { R } = loadDesktopUiSandbox();
    const userRow = R.buildMessageRowHtml(
      { id: "u1", role: "user", kind: "chat", text: "[sticker:pat]", sticker: "/stickers/fubuki/pat.png", time: "15:00" },
      { quote: "", body: "[sticker:pat]", files: [] },
      {}
    );
    assert.ok(userRow.includes("dsb-sticker-img is-user"));
    assert.ok(!userRow.includes("[sticker:pat]"));

    const aiRow = R.buildMessageRowHtml({
      id: "a1",
      role: "assistant",
      kind: "chat",
      text: "给你看代码示例<sticker:wink>\n```js\nconst x = 1;\n```",
      sticker: "/stickers/fubuki/wink.png",
      time: "15:01",
      variants: [{ text: "v1" }, { text: "v2" }],
      active_variant_idx: 1,
    });
    assert.ok(aiRow.includes("dsb-sticker-img"));
    assert.ok(!aiRow.includes("<sticker:wink>"));
    assert.ok(aiRow.includes('aria-label="复制代码"'));
    assert.ok(aiRow.includes("dsb-variant-bar"));

    const compactRow = R.buildMessageRowHtml({ id: "c1", kind: "compact_summary", text: "已浓缩", time: "15:02", decision: "{}" });
    assert.ok(compactRow.includes('data-msg-id="c1"'));
  });

  it("renders sticker pack picker tabs, items, loading and empty states", () => {
    const { R } = loadDesktopUiSandbox();
    assert.ok(R.buildStickerPopoverHtml([], "", true).includes("加载表情包"));
    assert.ok(R.buildStickerPopoverHtml([], "", false).includes("暂无可用贴纸包"));

    const html = R.buildStickerPopoverHtml(
      [
        { id: "fubuki", name: "白吹雪", stickers: [{ code: "wink", label: "眨眼", url: "/stickers/fubuki/wink.png" }] },
        { id: "extra", name: "附加包", stickers: [{ code: "ok", label: "好的", url: "/stickers/extra/ok.png" }] },
      ],
      "fubuki",
      false
    );
    assert.ok(html.includes('role="tablist"'));
    assert.ok(html.includes('data-sticker-pack="extra"'));
    assert.ok(html.includes('data-send-sticker="wink"'));
    assert.ok(html.includes('loading="lazy"'));
  });

  it("filters slash commands cleanly by prefix and description", () => {
    const { U } = loadDesktopUiSandbox();
    const cmds = [
      { cmd: "/goal ", desc: "持久目标" },
      { cmd: "/compact", desc: "浓缩归档" },
      { cmd: "/pin ", desc: "置顶记忆" },
      { cmd: "/status", desc: "状态检查" },
    ];
    assert.equal(U.getFilteredSlashCommands("/", cmds).length, 4);
    assert.deepEqual(U.getFilteredSlashCommands("/co", cmds).map((c) => c.cmd), ["/compact"]);
    assert.deepEqual(U.getFilteredSlashCommands("/目标", cmds).map((c) => c.cmd), ["/goal "]);
    assert.equal(U.getFilteredSlashCommands("hello", cmds).length, 0);
  });

  it("reconciles history DOM by key, reusing unchanged nodes and deferring only messages before the trailing 8", () => {
    const { R, U, MiniEl } = loadDesktopUiSandbox();
    const historyEl = new MiniEl("div");
    const msgs = Array.from({ length: 12 }, (_, idx) => ({
      id: `m-${idx}`,
      role: idx % 2 === 0 ? "user" : "assistant",
      kind: "chat",
      text: `message ${idx}`,
      time: "15:10",
    }));
    const ctx = {
      escapeHtml: R.escapeHtml,
      renderBotAvatarSvg: R.renderBotAvatarSvg,
      renderSingleMessageHtml: (m) => R.buildMessageRowHtml(m, null, {}),
    };
    const r1 = U.reconcileChatHistory(ctx, historyEl, msgs, { id: "pc_engineer", name: "司工" });
    assert.equal(r1.changed, true);
    assert.equal(historyEl.children.length, 12);
    // First 4 (indices 0..3) are deferred; trailing 8 (indices 4..11) are NOT deferred
    assert.equal(historyEl.children[0].classList.contains("is-deferred"), true);
    assert.equal(historyEl.children[3].classList.contains("is-deferred"), true);
    assert.equal(historyEl.children[4].classList.contains("is-deferred"), false);
    assert.equal(historyEl.children[11].classList.contains("is-deferred"), false);

    const preservedFirstNode = historyEl.children[0];
    const preservedTailNode = historyEl.children[10];
    // Append one new message and re-reconcile
    const msgs2 = [...msgs, { id: "m-12", role: "user", kind: "chat", text: "newest", time: "15:11" }];
    const r2 = U.reconcileChatHistory(ctx, historyEl, msgs2, { id: "pc_engineer", name: "司工" });
    assert.equal(r2.addedNew, true);
    assert.equal(historyEl.children.length, 13);
    assert.equal(historyEl.children[0], preservedFirstNode);
    assert.equal(historyEl.children[10], preservedTailNode);
    // Index 4 is now 9th from end -> toggled to is-deferred without losing element identity
    assert.equal(historyEl.children[4].classList.contains("is-deferred"), true);
    assert.equal(historyEl.children[12].classList.contains("is-deferred"), false);
  });
});
