"""Browser Live DOM Extraction & Reference Action JS Payloads (`server/cloud_os/browser_dom_scripts.py`)."""

from __future__ import annotations

import json
from typing import Optional

LIVE_DOM_EXTRACT_JS = r"""
(() => {
  const title = (document.title || '').trim();
  const url = location.href || '';
  const chromeTop = Math.max(0, (window.outerHeight - window.innerHeight) || 0);
  const chromeLeft = Math.max(0, Math.round(((window.outerWidth - window.innerWidth) || 0) / 2));
  const winX = (typeof window.screenX === 'number' ? window.screenX : 14) + chromeLeft;
  const winY = (typeof window.screenY === 'number' ? window.screenY : 10) + chromeTop;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const toBBox = (l, t, w, h) => [
    clamp(Math.round(((winX + l) * 1000) / 1280), 0, 1000), clamp(Math.round(((winY + t) * 1000) / 800), 0, 1000),
    clamp(Math.round(((winX + l + w) * 1000) / 1280), 0, 1000), clamp(Math.round(((winY + t + h) * 1000) / 800), 0, 1000),
  ];

  const baseSel = 'a[href], button, input:not([type="hidden"]), textarea, select, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [role="option"], [data-tab], [tabindex]:not([tabindex="-1"]), [onclick], [contenteditable="true"]';
  const entries = [], iframesMeta = [], docPool = [];

  const isVis = (el, r, minW = 8, minH = 8) => {
    if (!r || r.width < minW || r.height < minH) return false;
    const vw = window.innerWidth || 1280, vh = window.innerHeight || 800;
    if (r.right < 0 || r.bottom < 0 || r.left > vw || r.top > vh) return false;
    const st = (el.ownerDocument.defaultView || window).getComputedStyle(el);
    return st.display !== 'none' && st.visibility !== 'hidden' && parseFloat(st.opacity || '1') >= 0.05;
  };

  const queryAllDeep = (root, sel, out = [], depth = 0) => {
    if (!root || depth > 4) return out;
    try { for (const n of root.querySelectorAll(sel)) out.push(n); } catch (e) {}
    try {
      for (const host of root.querySelectorAll('*')) {
        if (host.shadowRoot) queryAllDeep(host.shadowRoot, sel, out, depth + 1);
      }
    } catch (e) {}
    return out;
  };

  const scanDoc = (doc, ox, oy, depth) => {
    if (!doc || depth > 2) return;
    docPool.push({ doc, ox, oy });
    for (const el of queryAllDeep(doc, baseSel)) {
      if (el.tagName && el.tagName.toLowerCase() === 'a' && el.getAttribute('target') === '_blank') el.setAttribute('target', '_self');
      entries.push({ el, ox, oy, isIframe: false });
    }
    let spaCount = 0;
    for (const el of queryAllDeep(doc, 'div, span, li, td, img, svg, h1, h2, h3, h4')) {
      if (spaCount >= 40) break;
      if (el.matches && el.matches(baseSel)) continue;
      const r = el.getBoundingClientRect();
      if (!r || r.width < 12 || r.height < 12 || r.width > 640 || r.height > 240) continue;
      if ((doc.defaultView || window).getComputedStyle(el).cursor !== 'pointer' || el.querySelector(baseSel)) continue;
      const txt = (el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('alt') || el.innerText || '').replace(/\s+/g, ' ').trim();
      if (!txt || txt.length > 60) continue;
      entries.push({ el, ox, oy, isIframe: false });
      spaCount += 1;
    }
    for (const ifr of queryAllDeep(doc, 'iframe')) {
      const r = ifr.getBoundingClientRect();
      const absL = r.left + ox, absT = r.top + oy;
      if (!isVis(ifr, { left: absL, top: absT, right: absL + r.width, bottom: absT + r.height, width: r.width, height: r.height }, 40, 30)) continue;
      const src = ifr.getAttribute('src') || ifr.src || '';
      const iTitle = (ifr.getAttribute('title') || ifr.getAttribute('aria-label') || '').trim();
      let host = '';
      try { host = src ? (new URL(src, location.href).hostname || '') : ''; } catch (e) { host = src.slice(0, 40); }
      let sameOriginOk = false;
      try {
        const idoc = ifr.contentDocument;
        if (idoc && idoc.body) { sameOriginOk = true; scanDoc(idoc, absL, absT, depth + 1); }
      } catch (e) {}
      iframesMeta.push({ el: ifr, ox, oy, r, absL, absT, src, iTitle, host, sameOriginOk });
      if (r.width >= 80 && r.height >= 80 && (!sameOriginOk || (host && host !== location.hostname))) {
        entries.push({ el: ifr, ox, oy, isIframe: true, iLabel: `[跨域内嵌框/二维码框] ${iTitle || host || 'iframe'}`, iHref: src });
      }
    }
  };
  scanDoc(document, 0, 0, 0);

  let authGate = null;
  const captchaRe = /challenges\.cloudflare\.com|turnstile|hcaptcha\.com|recaptcha|geetest|tcaptcha|captcha\.qq\.com|dun\.163\.com|防水墙/i;
  const qrIframeRe = /open\.weixin\.qq\.com|ptlogin2\.qq\.com|graph\.qq\.com|qrconnect|wxlogin|ptlogin|qrcode|alipay|passport\.jd\.com\/uc\/qr/i;
  const qrHintRe = /qr|qrcode|alipay|支付宝|wechat|微信|wxlogin|ptlogin|cashier|checkout|scan|pay|收银|二.?维.?码|扫码|login|登录/i;

  for (const m of iframesMeta) {
    const sig = `${m.src} ${m.iTitle} ${m.el.id || ''} ${m.el.className || ''}`;
    if (captchaRe.test(sig)) {
      authGate = { kind: 'captcha', reason: `检测到验证码内嵌框 (${m.host || m.iTitle || 'captcha'})`, bbox: toBBox(m.absL, m.absT, m.r.width, m.r.height), src: m.src.slice(0, 240) };
      break;
    }
    if (m.r.width >= 80 && m.r.height >= 80 && qrIframeRe.test(sig)) {
      authGate = { kind: 'qr_login', reason: `检测到第三方扫码登录框 (${m.host || m.iTitle || 'qr_login'})`, bbox: toBBox(m.absL, m.absT, m.r.width, m.r.height), src: m.src.slice(0, 240) };
      break;
    }
  }
  if (!authGate) {
    for (const d of docPool) {
      const capEl = d.doc.querySelector('.cf-turnstile, .h-captcha, .g-recaptcha, [id*="tcaptcha"], [class*="geetest"]');
      if (capEl) {
        const r = capEl.getBoundingClientRect();
        if (isVis(capEl, { left: d.ox + r.left, top: d.oy + r.top, right: d.ox + r.right, bottom: d.oy + r.bottom, width: r.width, height: r.height }, 60, 40)) {
          authGate = { kind: 'captcha', reason: '检测到页面验证码组件', bbox: toBBox(d.ox + r.left, d.oy + r.top, r.width, r.height), src: url.slice(0, 240) };
          break;
        }
      }
      for (const el of Array.from(d.doc.querySelectorAll('img, canvas'))) {
        const r = el.getBoundingClientRect();
        if (r.width < 90 || r.height < 90 || r.width > 420 || r.height > 420) continue;
        const ratio = r.width / r.height;
        if (ratio < 0.7 || ratio > 1.4) continue;
        if (!isVis(el, { left: d.ox + r.left, top: d.oy + r.top, right: d.ox + r.right, bottom: d.oy + r.bottom, width: r.width, height: r.height }, 90, 90)) continue;
        const pTxt = ((el.parentElement && el.parentElement.innerText) || '').slice(0, 120);
        const hint = `${el.getAttribute('src') || ''} ${el.getAttribute('alt') || ''} ${el.id || ''} ${el.className || ''} ${pTxt}`;
        if (qrHintRe.test(hint)) {
          authGate = { kind: 'qr_login', reason: '检测到登录二维码区域', bbox: toBBox(d.ox + r.left, d.oy + r.top, r.width, r.height), src: (el.getAttribute('src') || url).slice(0, 240) };
          break;
        }
      }
      if (authGate) break;
    }
  }

  const visibleCandidates = [], seen = new Set();
  for (const item of entries) {
    const el = item.el, rect = el.getBoundingClientRect();
    const absLeft = rect.left + item.ox, absTop = rect.top + item.oy;
    if (!isVis(el, { left: absLeft, top: absTop, right: absLeft + rect.width, bottom: absTop + rect.height, width: rect.width, height: rect.height })) continue;
    const tag = (el.tagName || '').toLowerCase();
    let label = item.isIframe ? item.iLabel : (el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('alt') || el.innerText || el.value || el.getAttribute('name') || '');
    label = String(label).replace(/\s+/g, ' ').trim().slice(0, 85);
    const href = item.isIframe ? (item.iHref || '') : ((el.href && typeof el.href === 'string') ? el.href : '');
    if (!label && !href && tag !== 'input' && tag !== 'textarea') continue;
    if (!label) label = href ? href.slice(0, 60) : tag;
    const cx = Math.round(winX + absLeft + rect.width / 2), cy = Math.round(winY + absTop + rect.height / 2);
    const key = `${tag}:${label.slice(0, 24)}:${Math.round(cx / 12)}:${Math.round(cy / 12)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    visibleCandidates.push({
      el, tag, label, href, cx, cy, w: Math.round(rect.width), h: Math.round(rect.height),
      inViewport: rect.bottom > 0 && rect.top < window.innerHeight && rect.right > 0 && rect.left < window.innerWidth,
      rowBand: Math.round(absTop / 24), absLeft, isIframe: item.isIframe,
    });
  }
  visibleCandidates.sort((a, b) => (a.isIframe !== b.isIframe ? (a.isIframe ? -1 : 1) : (a.inViewport !== b.inViewport ? (a.inViewport ? -1 : 1) : (a.rowBand !== b.rowBand ? a.rowBand - b.rowBand : a.absLeft - b.absLeft))));

  const elements = [];
  for (const c of visibleCandidates.slice(0, 60)) {
    const ref = `@b${elements.length + 1}`;
    c.el.setAttribute('data-agent-ref', ref);
    elements.push({
      ref, tag: c.tag, type: c.el.getAttribute('type') || c.tag, label: c.label, href: c.href,
      x: c.cx, y: c.cy, nx: clamp(Math.round((c.cx * 1000) / 1280), 0, 1000), ny: clamp(Math.round((c.cy * 1000) / 800), 0, 1000), w: c.w, h: c.h,
    });
  }
  let bodyText = '';
  try { bodyText = (document.body ? document.body.innerText : '') || ''; } catch (e) {}
  bodyText = bodyText.replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim().slice(0, 3600);
  return JSON.stringify({ title, url, page_text: bodyText, elements, auth_gate: authGate });
})()
"""

REF_ACTION_JS = r"""
(function(ref, textVal, doClear, doSubmit) {
  const sel = `[data-agent-ref=${JSON.stringify(ref)}]`;
  const findInRoot = (root, ox, oy, depth) => {
    if (!root || depth > 4) return null;
    try {
      const direct = root.querySelector(sel);
      if (direct) return { el: direct, ox, oy };
    } catch (e) {}
    try {
      for (const host of root.querySelectorAll('*')) {
        if (host.shadowRoot) {
          const hit = findInRoot(host.shadowRoot, ox, oy, depth + 1);
          if (hit) return hit;
        }
        if (host.tagName && host.tagName.toLowerCase() === 'iframe') {
          try {
            const idoc = host.contentDocument;
            if (idoc && idoc.body) {
              const ir = host.getBoundingClientRect();
              const hit = findInRoot(idoc, ox + ir.left, oy + ir.top, depth + 1);
              if (hit) return hit;
            }
          } catch (e) {}
        }
      }
    } catch (e) {}
    return null;
  };
  const found = findInRoot(document, 0, 0, 0);
  if (!found || !found.el) return null;
  const el = found.el;
  el.scrollIntoView({block: 'center', inline: 'center'});
  const rect = el.getBoundingClientRect();
  const chromeTop = Math.max(0, (window.outerHeight - window.innerHeight) || 0);
  const chromeLeft = Math.max(0, Math.round(((window.outerWidth - window.innerWidth) || 0) / 2));
  const cx = Math.round((window.screenX || 14) + chromeLeft + found.ox + rect.left + rect.width / 2);
  const cy = Math.round((window.screenY || 10) + chromeTop + found.oy + rect.top + rect.height / 2);
  const tag = (el.tagName || '').toLowerCase();
  const deepActive = (doc) => {
    let a = doc.activeElement;
    while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
    return a;
  };
  if (textVal !== null && textVal !== undefined) {
    const itype = (el.getAttribute('type') || 'text').toLowerCase();
    const nonText = new Set(['button', 'submit', 'reset', 'checkbox', 'radio', 'file', 'image', 'hidden', 'range', 'color']);
    const isInput = (tag === 'input' && !nonText.has(itype)) || tag === 'textarea';
    const isEditable = (isInput && !el.disabled && !el.readOnly) || !!el.isContentEditable;
    if (!isEditable) {
      return JSON.stringify({ok: false, reason: 'not_editable', error: `目标元素 ${ref} (${tag}) 不可编辑`, ref, tag});
    }
    el.focus({preventScroll: true});
    const doc = el.ownerDocument || document;
    let act = deepActive(doc);
    if (act !== el && !el.contains(act)) {
      try { el.click(); el.focus({preventScroll: true}); } catch (e) {}
      act = deepActive(doc);
    }
    if (act !== el && !el.contains(act)) {
      return JSON.stringify({ok: false, reason: 'focus_failed', error: `目标输入框 ${ref} 未获得焦点`, ref, tag});
    }
    if (isInput && 'value' in el) {
      const win = doc.defaultView || window;
      const setter = Object.getOwnPropertyDescriptor(win.HTMLInputElement.prototype, 'value')?.set
        || Object.getOwnPropertyDescriptor(win.HTMLTextAreaElement.prototype, 'value')?.set;
      if (doClear) {
        if (setter) setter.call(el, ''); else el.value = '';
        el.dispatchEvent(new Event('input', {bubbles: true}));
      }
      const nextVal = doClear ? textVal : ((el.value || '') + textVal);
      if (setter) setter.call(el, nextVal); else el.value = nextVal;
      el.dispatchEvent(new Event('input', {bubbles: true}));
      el.dispatchEvent(new Event('change', {bubbles: true}));
    } else if (el.isContentEditable) {
      if (doClear) el.innerText = '';
      el.innerText = doClear ? textVal : ((el.innerText || '') + textVal);
      el.dispatchEvent(new Event('input', {bubbles: true}));
    }
    if (doSubmit) {
      if (el.form && typeof el.form.requestSubmit === 'function') { try { el.form.requestSubmit(); } catch (e) {} }
      else {
        el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true}));
        el.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true}));
      }
    }
  } else if (tag !== 'iframe') {
    if (tag === 'a' && el.getAttribute('target') === '_blank') el.setAttribute('target', '_self');
    el.focus({preventScroll: true});
    el.click();
  }
  return JSON.stringify({ok: true, ref, x: cx, y: cy, tag, href: el.href || el.src || ''});
})
"""


def build_ref_action_js(ref_id: str, text: Optional[str] = None, *, clear: bool = False, submit: bool = False) -> str:
    """Build a self-contained CDP evaluation expression for clicking or typing into `@bN`."""
    return f'({REF_ACTION_JS.strip()})({json.dumps(ref_id)}, {json.dumps(text)}, {json.dumps(bool(clear))}, {json.dumps(bool(submit))})'
