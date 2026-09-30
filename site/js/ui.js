// Plurarch shared UI helpers: DOM building (no innerHTML with data), icons, CDN loading,
// runtime config loading, safe storage, and widgets shared by the participant app and console.

import { fmtParamValue, sameValue, toNum, sentenceCase } from './format.js';

/* ---------------------------------------------------------------- CDN */
// Exact, verified versions (jsDelivr). Subresource integrity hashes computed from these files.
export const CDN = {
  lucide: {
    src: 'https://cdn.jsdelivr.net/npm/lucide@1.49.0/dist/umd/lucide.min.js',
    integrity: 'sha384-7mWwAGEOOlxvEBR+Mpy0eVhGL+9abGHkZtVoTs+dvrWkI5g00lyMaAxRIK+rqXGt',
  },
  d3path: {
    src: 'https://cdn.jsdelivr.net/npm/d3-path@3.1.0/dist/d3-path.min.js',
    integrity: 'sha384-6OLefW2YM92GNdyDHuNLXurfMxkbKBdNCBrbc9U57gThqvcmrFrR6V4h46q9RXH6',
  },
  d3shape: {
    src: 'https://cdn.jsdelivr.net/npm/d3-shape@3.2.0/dist/d3-shape.min.js',
    integrity: 'sha384-Pf8DSN0/EBE+hYfdD/WOvxYZPh8+AUL27jVmgRJ2az/EQxJWjgJsZrKFEuFT7DYq',
  },
  supabase: {
    src: 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.117.2/dist/umd/supabase.js',
    integrity: 'sha384-Rj26LVGvoeRVR6+mwQmFfcR3QOBEwT+ZmuCWpuiqeTzJpCs0ER4ITAWGb4Hiy3Ok',
  },
};

const scriptPromises = new Map();

/** Load a classic script once. Rejects on error or after `timeout` ms (0 = no timeout). */
export function loadScript(lib, { timeout = 0 } = {}) {
  const src = typeof lib === 'string' ? lib : lib.src;
  if (scriptPromises.has(src)) return scriptPromises.get(src);
  const p = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
    s.async = true;
    if (typeof lib === 'object' && lib.integrity) {
      s.integrity = lib.integrity;
      s.crossOrigin = 'anonymous';
    }
    let timer = null;
    if (timeout > 0) timer = setTimeout(() => reject(new Error('Timed out loading ' + src)), timeout);
    s.onload = () => { if (timer) clearTimeout(timer); resolve(); };
    s.onerror = () => { if (timer) clearTimeout(timer); s.remove(); reject(new Error('Could not load ' + src)); };
    document.head.appendChild(s);
  });
  scriptPromises.set(src, p);
  p.catch(() => scriptPromises.delete(src));
  return p;
}

/** Load lucide in the background; icons render as placeholders until it arrives. */
export function loadIcons() {
  return loadScript(CDN.lucide).then(() => { refreshIcons(document); return true; }).catch(() => false);
}

/** Load d3-path then d3-shape (UMD, window.d3). Resolves true when d3.arc is available. */
export function loadD3() {
  return loadScript(CDN.d3path)
    .then(() => loadScript(CDN.d3shape))
    .then(() => !!(window.d3 && typeof window.d3.arc === 'function'))
    .catch(() => false);
}

/* ---------------------------------------------------------------- DOM */
function appendChildren(node, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false || c === true) continue;
    node.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

function applyProps(node, props, isSvg) {
  if (!props) return;
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === 'class') {
      if (isSvg) node.setAttribute('class', v); else node.className = v;
    } else if (k === 'text') {
      node.textContent = String(v);
    } else if (k === 'style' && typeof v === 'object') {
      for (const [sk, sv] of Object.entries(v)) {
        if (sv == null) continue;
        if (sk.startsWith('--')) node.style.setProperty(sk, String(sv));
        else node.style[sk] = String(sv);
      }
    } else if (k === 'dataset' && typeof v === 'object') {
      for (const [dk, dv] of Object.entries(v)) if (dv != null) node.dataset[dk] = String(dv);
    } else if (k.startsWith('on') && typeof v === 'function') {
      node.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (k === 'value' && !isSvg && 'value' in node) {
      node.value = String(v);
    } else if (k === 'checked' && !isSvg) {
      node.checked = !!v;
      if (v) node.setAttribute('checked', '');
    } else if (v === true) {
      node.setAttribute(k, '');
    } else {
      node.setAttribute(k, String(v));
    }
  }
}

/** Create an HTML element: el('div', {class, text, onclick, ...}, ...children). */
export function el(tag, props, ...children) {
  const node = document.createElement(tag);
  applyProps(node, props, false);
  appendChildren(node, children);
  return node;
}

const SVG_NS = 'http://www.w3.org/2000/svg';
/** Create an SVG element. */
export function svgEl(tag, props, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  applyProps(node, props, true);
  appendChildren(node, children);
  return node;
}

export function clear(node) {
  if (node) while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/** Replace a node's children. */
export function mount(node, ...children) {
  clear(node);
  appendChildren(node, children);
  return node;
}

export const $ = (sel, root = document) => root.querySelector(sel);

/* ---------------------------------------------------------------- icons */
const FALLBACK_GLYPH = {
  'refresh-cw': '↻', projector: '▣', 'scroll-text': '≡', info: 'i', check: '✓', x: '×',
  'arrow-left': '‹', 'chevron-right': '›', 'chevron-down': '⌄', plus: '+', minus: '−',
  play: '▶', square: '■', 'log-out': '⇥', 'loader-circle': '○', 'wifi-off': '×',
};

function toPascal(name) {
  return name.split('-').filter(Boolean).map((p) => p.charAt(0).toUpperCase() + p.slice(1)).join('');
}

/**
 * Lucide outline icon (1.5px rendered stroke). Returns an <svg> if lucide is loaded,
 * else a sized placeholder <i data-lucide> that refreshIcons() upgrades later.
 */
export function icon(name, size = 18, extraClass = '') {
  const safe = typeof name === 'string' && /^[a-z0-9-]{1,64}$/.test(name) ? name : 'circle';
  const cls = `icon icon-${size}${extraClass ? ' ' + extraClass : ''}`;
  const stroke = (1.5 * 24 / size).toFixed(2);
  const L = window.lucide;
  if (L && L.icons && typeof L.createElement === 'function') {
    const node = L.icons[toPascal(safe)];
    if (node) {
      try {
        return L.createElement(node, { width: size, height: size, 'stroke-width': stroke, class: cls, 'aria-hidden': 'true', focusable: 'false' });
      } catch (_) { /* fall through to placeholder */ }
    }
  }
  return el('i', {
    class: cls + ' icon-ph', 'data-lucide': safe, width: size, height: size, 'stroke-width': stroke,
    'aria-hidden': 'true', 'data-fallback': FALLBACK_GLYPH[safe] || '',
  });
}

/** Replace any <i data-lucide> placeholders under root (uses lucide.createIcons). */
export function refreshIcons(root = document) {
  const L = window.lucide;
  if (!L || typeof L.createIcons !== 'function') return;
  try {
    L.createIcons({ root, nameAttr: 'data-lucide', attrs: {} });
  } catch (_) { /* never let icons break the page */ }
}

/* ---------------------------------------------------------------- storage */
function makeStore(getBackend) {
  const mem = new Map();
  return {
    get(k) {
      try { const s = getBackend(); if (s) { const v = s.getItem(k); if (v !== null) return v; } } catch (_) { /* blocked */ }
      return mem.has(k) ? mem.get(k) : null;
    },
    set(k, v) {
      mem.set(k, String(v));
      try { const s = getBackend(); if (s) s.setItem(k, String(v)); } catch (_) { /* full or blocked */ }
    },
    remove(k) {
      mem.delete(k);
      try { const s = getBackend(); if (s) s.removeItem(k); } catch (_) { /* blocked */ }
    },
  };
}
export const store = makeStore(() => window.localStorage);
export const sessionStore = makeStore(() => window.sessionStorage);

/* ---------------------------------------------------------------- misc */
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function reducedMotion() {
  try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (_) { return false; }
}

export function asArray(v) {
  if (Array.isArray(v)) return v;
  if (typeof v === 'string') { const p = parseJSON(v); return Array.isArray(p) ? p : []; }
  return [];
}
export function asObject(v) {
  if (v && typeof v === 'object' && !Array.isArray(v)) return v;
  if (typeof v === 'string') { const p = parseJSON(v); return p && typeof p === 'object' && !Array.isArray(p) ? p : {}; }
  return {};
}
function parseJSON(s) {
  const t = s.trim();
  if (!(t.startsWith('{') || t.startsWith('['))) return null;
  try { return JSON.parse(t); } catch (_) { return null; }
}

/** Short random id from crypto.getRandomValues (works on plain http, unlike randomUUID). */
export function randomId(bytes = 16) {
  const a = new Uint8Array(bytes);
  try { window.crypto.getRandomValues(a); } catch (_) { for (let i = 0; i < bytes; i++) a[i] = Math.floor(Math.random() * 256); }
  return Array.from(a, (b) => b.toString(16).padStart(2, '0')).join('');
}

/* ---------------------------------------------------------------- config */
async function fetchJSON(url) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 10000);
  try {
    const res = await fetch(url, { cache: 'no-cache', signal: ctrl.signal });
    if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
    return await res.json();
  } finally {
    clearTimeout(t);
  }
}

function numOr(v, d) { const n = toNum(v); return Number.isFinite(n) ? n : d; }

function normalizeParam(p) {
  if (!p || typeof p !== 'object' || typeof p.key !== 'string') return null;
  const type = p.type === 'slider' ? 'slider' : 'choice';
  const base = {
    key: p.key,
    type,
    label: String(p.label || sentenceCase(p.key)),
    question: String(p.question || p.label || sentenceCase(p.key)),
    explainer: p.explainer ? String(p.explainer) : '',
    unit: p.unit ? String(p.unit) : '',
    default: p.default,
  };
  if (type === 'choice') {
    base.options = asArray(p.options)
      .map((o) => (o && typeof o === 'object')
        ? { value: String(o.value), label: String(o.label || sentenceCase(o.value)), icon: o.icon ? String(o.icon) : 'circle' }
        : { value: String(o), label: sentenceCase(String(o)), icon: 'circle' })
      .filter((o) => o.value !== 'undefined');
    if (!base.options.length) return null;
  } else {
    base.min = numOr(p.min, 0);
    base.max = numOr(p.max, 100);
    base.step = numOr(p.step, 1) > 0 ? numOr(p.step, 1) : 1;
    if (base.max < base.min) [base.min, base.max] = [base.max, base.min];
  }
  return base;
}

/**
 * Fetch config/parameters.json, config/project_brief.json and config/use_cases/<useCase>.json
 * (relative to the page) and normalise them. Throws on network/parse failure.
 */
export async function loadAppConfig(useCase) {
  const uc = typeof useCase === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(useCase) ? useCase : 'live_presentation';
  const [parameters, brief, profile] = await Promise.all([
    fetchJSON('config/parameters.json'),
    fetchJSON('config/project_brief.json'),
    fetchJSON(`config/use_cases/${uc}.json`),
  ]);
  const params = asArray(parameters && parameters.parameters).map(normalizeParam).filter(Boolean);
  const b = asObject(brief);
  const pr = asObject(profile);
  return {
    params,
    brief: {
      title: String(b.title || ''),
      narrative: String(b.narrative || ''),
      climate: String(b.climate || ''),
      metrics_note: String(b.metrics_note || ''),
      goals: asArray(b.goals).filter((g) => g && typeof g === 'object'),
      hard_rules: asArray(b.hard_rules).filter((r) => r && typeof r === 'object'),
      close_tradeoff_margin: numOr(b.close_tradeoff_margin, 0),
      close_tradeoff_note: String(b.close_tradeoff_note || ''),
      consensus: asObject(b.consensus),
      modification_limits: asObject(b.modification_limits),
      agent_limits: asObject(b.agent_limits),
    },
    profile: {
      id: String(pr.id || uc),
      label: String(pr.label || sentenceCase(uc)),
      session_title: String(pr.session_title || ''),
      poll_interval_s: Math.max(1, numOr(pr.poll_interval_s, 4)),
      poll_jitter_s: Math.max(0, numOr(pr.poll_jitter_s, 1)),
      round_duration_s: Math.max(5, numOr(pr.round_duration_s, 60)),
      expected_participants: numOr(pr.expected_participants, null),
      projection_scale: Math.min(3, Math.max(1, numOr(pr.projection_scale, 1.4))),
      privacy_notice: String(pr.privacy_notice || ''),
      facilitator_label: String(pr.facilitator_label || 'Facilitator'),
      participant_label: String(pr.participant_label || 'Participant'),
    },
  };
}

/* ---------------------------------------------------------------- decisions (shared) */
const VERDICTS = {
  ACCEPTED: { cls: 'verdict-accepted', icon: 'check' },
  MODIFIED: { cls: 'verdict-modified', icon: 'pencil-ruler' },
  REJECTED: { cls: 'verdict-rejected', icon: 'x' },
};

/** Normalised verdict word or null. */
export function verdictOf(decision) {
  const v = decision && typeof decision.verdict === 'string' ? decision.verdict.trim().toUpperCase() : '';
  return VERDICTS[v] && (!decision.status || decision.status === 'ok') ? v : null;
}

/** Verdict badge with the word (never colour alone); "No change" for failed/skipped. */
export function verdictBadge(decision, { large = false } = {}) {
  const v = verdictOf(decision);
  const size = large ? ' verdict-lg' : '';
  if (v) {
    return el('span', { class: `verdict ${VERDICTS[v].cls}${size}` }, icon(VERDICTS[v].icon, large ? 18 : 16), v);
  }
  return el('span', { class: `verdict verdict-none${size}` }, icon('minus', large ? 18 : 16), 'No change');
}

/**
 * Rows comparing what was voted with what was applied, one per parameter:
 * [{param, voted, applied, changed, reason}]
 */
export function decisionRows(decision, params) {
  const proposal = asObject(decision && decision.proposal);
  const voted = asObject(proposal.parameters);
  const current = asObject(proposal.current_parameters);
  let applied = asObject(decision && decision.applied_parameters);
  if (!Object.keys(applied).length) applied = current;
  const changes = asArray(decision && decision.changes);
  return params.map((p) => {
    const v = voted[p.key];
    const a = applied[p.key] !== undefined ? applied[p.key] : current[p.key];
    const ch = changes.find((c) => c && c.parameter === p.key);
    const changed = v !== undefined && a !== undefined && !sameValue(p, v, a);
    return {
      param: p,
      voted: v,
      applied: a,
      votedText: fmtParamValue(p, v),
      appliedText: fmtParamValue(p, a),
      changed,
      reason: ch && ch.reason ? String(ch.reason) : '',
    };
  });
}

/** The currently applied value for a parameter (latest decision), else the config default. */
export function appliedValue(decision, param) {
  const applied = asObject(decision && decision.applied_parameters);
  let v = applied[param.key];
  if (v === undefined) v = asObject(asObject(decision && decision.proposal).current_parameters)[param.key];
  if (v === undefined) v = param.default;
  if (param.type === 'slider') {
    let n = toNum(v);
    if (!Number.isFinite(n)) n = toNum(param.default);
    if (!Number.isFinite(n)) n = param.min;
    return snapToStep(param, n);
  }
  const ok = (param.options || []).some((o) => o.value === String(v));
  return ok ? String(v) : null;
}

export function snapToStep(param, n) {
  const steps = Math.round((n - param.min) / param.step);
  const v = param.min + steps * param.step;
  const clamped = Math.min(param.max, Math.max(param.min, v));
  return Math.round(clamped * 1e6) / 1e6;
}

/** All step values of a slider, ascending. */
export function sliderValues(param) {
  const out = [];
  const n = Math.round((param.max - param.min) / param.step);
  for (let i = 0; i <= n && i <= 400; i++) out.push(Math.round((param.min + i * param.step) * 1e6) / 1e6);
  return out;
}

/* ---------------------------------------------------------------- toast + tooltip */
let toastHost = null;
export function toast(message, ms = 3200) {
  if (!toastHost) {
    toastHost = el('div', { class: 'toast-host', role: 'status', 'aria-live': 'polite' });
    document.body.appendChild(toastHost);
  }
  const t = el('div', { class: 'toast', text: message });
  toastHost.appendChild(t);
  setTimeout(() => t.remove(), ms);
}

let tip = null;
export function showTip(x, y, bold, sub) {
  if (!tip) {
    tip = el('div', { class: 'tooltip', role: 'tooltip', hidden: true });
    document.body.appendChild(tip);
  }
  mount(tip, el('b', { text: bold }), sub ? el('span', { text: sub }) : null);
  tip.hidden = false;
  const w = tip.offsetWidth || 120;
  const left = Math.min(window.innerWidth - w / 2 - 8, Math.max(w / 2 + 8, x));
  tip.style.left = left + 'px';
  tip.style.top = Math.max((tip.offsetHeight || 40) + 20, y) + 'px';
}
export function hideTip() {
  if (tip) tip.hidden = true;
}

/** Plurarch wordmark: ink tile with a mono-pitch roof over fins of rising height, plus "plurarch". */
export function wordmark() {
  const mark = svgEl('svg', { class: 'wordmark-mark', viewBox: '0 0 28 28', 'aria-hidden': 'true', focusable: 'false' },
    svgEl('rect', { width: 28, height: 28, rx: 8, fill: '#1A1A1A' }),
    svgEl('path', { d: 'M6 13.5L22 8.5', stroke: '#fff', 'stroke-width': 2.4, 'stroke-linecap': 'round', fill: 'none' }),
    svgEl('path', { d: 'M8.5 15.4V21.5M12.5 14.2V21.5M16.5 12.9V21.5M20.5 11.7V21.5', stroke: '#fff', 'stroke-width': 2.4, 'stroke-linecap': 'round', fill: 'none' }),
  );
  return el('span', { class: 'wordmark' }, mark, el('span', { text: 'plurarch' }));
}
