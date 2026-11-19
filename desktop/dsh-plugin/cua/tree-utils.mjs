const EMPTY_WRAPPER_RE = /^\t*\d+\s+(区域|组|窗格|列表|列表项目|文本|自定义|补充|部分|图形|图像|graphic|Group|Pane|Region|List|ListItem|Text|Custom|Section|Image)(\s+\(disabled\))?(\s+ID:\s+\S+)?\s*$/i;
const ID_NOISE_RE = /\s+ID:\s+(view_\d+|_r_[^\s]+|_R_[^\s]+|radix-[^\s]+|root)/gi;
const LONG_URL_RE = /\s+Value:\s+(https?|app):\/\/[^\s]{30,}/gi;

export function compactTree(rawTree, filterPattern, full = false) {
  if (!rawTree) return "";
  const withoutDocDump = full ? rawTree : rawTree.replace(/\n\nDocument text:\s*```[\s\S]*$/, "");
  let lines = withoutDocDump.split("\n");
  if (!full) {
    const cleaned = [];
    for (const l of lines) {
      if (EMPTY_WRAPPER_RE.test(l)) continue;
      let s = l.replace(ID_NOISE_RE, "").replace(LONG_URL_RE, "").replace(/^\t+/, "");
      if (s.length > 160) s = s.slice(0, 160) + "…";
      cleaned.push(s);
    }
    lines = [];
    for (let i = 0; i < cleaned.length; i++) {
      const m = /^(\d+)\s+(列表项目|组)\s+(.+)$/.exec(cleaned[i]);
      if (m && i + 1 < cleaned.length && cleaned[i + 1].endsWith(" " + m[3])) continue;
      lines.push(cleaned[i]);
    }
  }
  if (filterPattern) {
    const re = new RegExp(filterPattern, "i");
    lines = lines.filter((l) => re.test(l));
  }
  return lines.join("\n");
}

export function parseElementIndex(a = {}) {
  if (typeof a.element_index === "number" && Number.isFinite(a.element_index)) return a.element_index;
  if (typeof a.element_index === "string" && /^\d+$/.test(a.element_index.trim())) return Number(a.element_index.trim());
  const refStr = String(a.ref || a.element || a.target_ref || "").trim();
  const m = /^@?e?(\d+)$/i.exec(refStr);
  if (m) return Number(m[1]);
  return null;
}
