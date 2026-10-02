// Plurarch stage view: a projector screen that shows only the one thing that matters now,
// driven by the session data (same console state and sign-in as console.html).
//   join      no round open (before the first round, or after "next round"): QR + short URL
//   voting    round open: live count, countdown, one slim live row per question, Close round
//   reviewing round closed, no decision yet: 4 checkpoints driven by agent_events.step
//   decision  verdict word, one change line, one sentence, at most 2 metrics, optional 3D
// Keys: Space (or a clicker's PageDown) = open / close round, and "next round" on a decision;
// F = full screen; Esc = back to the console.
// URL flags: ?demo=1 plays canned data (rehearsals, tests; with &state=join|voting|reviewing|
// decision|lobby to start there), ?v3d=0 turns the 3D view off.

import { createBackend, isRetryable, BackendError } from './backend.js';
import {
  el, svgEl, mount, icon, loadIcons, loadScript, loadAppConfig, sessionStore, sleep, asArray, asObject,
  verdictOf, verdictBadge, decisionRows, snapToStep, reducedMotion, wordmark,
} from './ui.js';
import { fmtInt, fmtNum, fmtPct, fmtClock, parseTime, fmtParamValue, fmtSliderNumber, toNum, sameValue } from './format.js';

// qrcode-generator (MIT), exact version checked on jsDelivr; SRI hash of that file.
const QR_LIB = {
  src: 'https://cdn.jsdelivr.net/npm/qrcode-generator@2.0.4/dist/qrcode.js',
  integrity: 'sha384-e9EFD6BGC90bkW9aDV5xbbBfzwN7G8YImHao2lfLVKV/hPB0E0go+H3I64h7oHtA',
};
const Q = new URLSearchParams(window.location.search);
const DEMO = Q.get('demo') === '1';
const DISMISS_KEY = 'plurarch:stage-dismissed';

// Reviewer workflow (docs/ARCHITECTURE.md): 1 brief · 2 proposal · 3 alternatives · 4 decide ·
// 5 set_parameters · 6 verify. A checkpoint ticks once a later step has started.
const CHECKPOINTS = [
  { label: 'Reading the brief', until: 1 },
  { label: 'Testing your proposal', until: 2 },
  { label: 'Trying alternatives', until: 3 },
  { label: 'Applying to the model', until: 6, last: true },
];

const S = {
  cfg: null,
  backend: null,
  live: false,
  state: null,
  M: null,
  conn: 'reconnecting',
  screen: null,
  screenKey: '',
  view: '',
  busy: false,
  dismissed: sessionStore.get(DISMISS_KEY) || null,
  clockOffset: 0,
  unsub: null,
  fetchSeq: 0,
  appliedSeq: 0,
  shownCount: 0,
  countTarget: 0,
  countAnim: 0,
  hintTimer: null,
  idleTimer: null,
  wakeLock: null,
  v3d: { status: 'idle', viewer: null, mod: null, key: '', idleKey: '' },
};

const dom = {
  stage: document.getElementById('stage'),
  panel3d: document.getElementById('stage-3d'),
  conn: document.getElementById('conn'),
  demoChip: document.getElementById('demo-chip'),
  hints: document.getElementById('hints'),
  hintPrimary: document.getElementById('hint-primary'),
  hintFs: document.getElementById('hint-fs'),
  toast: document.getElementById('toast'),
  announce: document.getElementById('announce'),
};

/* ================================================================== helpers */
const num = (v, d = 0) => { const n = toNum(v); return Number.isFinite(n) ? n : d; };
const now = () => Date.now() + S.clockOffset;
const shortTool = (t) => String(t || '').replace(/^mcp__.+?__/, '');

function participantUrl() {
  if (DEMO && /^https?:\/\//.test(Q.get('url') || '')) return Q.get('url'); // demo only: rehearse with the real address
  const u = new URL(window.location.href);
  u.hash = '';
  u.search = '';
  u.pathname = u.pathname.replace(/stage\.html$/, '');
  return u.href;
}

function shortUrl(url) {
  return String(url || '').replace(/^https?:\/\//, '').replace(/^www\./, '').replace(/\/+$/, '');
}

/** Text with a break opportunity after each slash (long URLs wrap at a slash, not mid-word). */
function breakable(text) {
  const parts = String(text).split('/');
  const out = [];
  parts.forEach((p, i) => {
    out.push(i < parts.length - 1 ? p + '/' : p);
    if (i < parts.length - 1) out.push(el('wbr'));
  });
  return out;
}

/** First sentence; a full stop only ends a sentence when a space and a capital (or the end) follow. */
function firstSentence(text) {
  const t = String(text || '').trim().replace(/\s+/g, ' ');
  if (!t) return '';
  const m = t.match(/^(.+?[.!?])(?=\s+[A-Z0-9“"(]|$)/);
  return m ? m[1] : t;
}

function announce(text) {
  dom.announce.textContent = '';
  setTimeout(() => { dom.announce.textContent = text; }, 40);
}

let toastTimer = null;
function toast(text, ms = 2600) {
  dom.toast.textContent = text;
  dom.toast.hidden = false;
  dom.toast.style.animation = 'none';
  void dom.toast.offsetWidth;
  dom.toast.style.animation = '';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { dom.toast.hidden = true; }, ms);
}

function errText(e) {
  const code = e && e.code;
  if (code === 'round_already_open') return 'A round is already open';
  if (code === 'no_open_round') return 'No round is open';
  if (code === 'no_session') return 'No active session yet';
  if (code === 'unauthorized' || code === 'forbidden') return 'Facilitator sign-in needed';
  if (isRetryable(e)) return 'Could not reach the server. Press Space again.';
  return `Something went wrong (${code || 'error'})`;
}

/* ================================================================== state */
function normRound(r) {
  if (!r || typeof r !== 'object' || r.id == null) return null;
  return { ...r, id: String(r.id), status: r.status === 'open' ? 'open' : 'closed' };
}

function normDecision(d) {
  if (!d || typeof d !== 'object') return null;
  return {
    ...d,
    id: d.id != null ? String(d.id) : '',
    round_id: d.round_id != null ? String(d.round_id) : '',
    proposal: asObject(d.proposal),
    applied_parameters: asObject(d.applied_parameters),
    evidence: asObject(d.evidence),
    changes: asArray(d.changes),
    metrics: asObject(d.metrics),
  };
}

function norm(raw) {
  const s = raw && typeof raw === 'object' ? raw : {};
  const rounds = asArray(s.rounds).map(normRound).filter(Boolean).sort((a, b) => num(a.number) - num(b.number));
  const round = normRound(s.round);
  return {
    session: s.session && typeof s.session === 'object' ? s.session : null,
    rounds,
    round: round ? (rounds.find((r) => r.id === round.id) || round) : null,
    votes: asArray(s.votes).filter((v) => v && typeof v === 'object'),
    agent_events: asArray(s.agent_events).filter((e) => e && typeof e === 'object').sort((a, b) => num(a.seq) - num(b.seq)),
    decisions: asArray(s.decisions).map(normDecision).filter(Boolean),
    join_url: typeof s.join_url === 'string' && s.join_url.trim() ? s.join_url.trim() : null,
    server_time: s.server_time || null,
  };
}

function model() {
  const st = S.state || norm(null);
  const newest = st.rounds.length ? st.rounds[st.rounds.length - 1] : null;
  const decision = newest ? st.decisions.find((d) => d.round_id === newest.id) || null : null;
  const lastDecision = st.decisions.length ? st.decisions[st.decisions.length - 1] : null;
  let view;
  if (!st.session || !newest) view = 'join';
  else if (newest.status === 'open') view = 'voting';
  else if (!decision) view = 'reviewing';
  else if (decision.id === S.dismissed) view = 'join';
  else view = 'decision';
  const isNewest = st.round && newest && st.round.id === newest.id;
  const votes = isNewest ? st.votes : [];
  const events = isNewest ? st.agent_events : [];
  // Live count: the orchestrator's aggregate (rounds.participants / session_status.round_participants,
  // migration 003) or, if missing or behind, the distinct participants in the round's votes.
  const people = Math.max(num(newest && (newest.participants != null ? newest.participants : newest.round_participants)),
    new Set(votes.map((v) => v.participant_id)).size);
  const nextNumber = newest && Number.isFinite(toNum(newest.number)) ? toNum(newest.number) + 1 : 1;
  return {
    st, newest, decision, lastDecision, view, votes, events, people, nextNumber,
    between: view === 'join' && !!lastDecision && !!newest,
    joinUrl: st.join_url || participantUrl(),
  };
}

/* ================================================================== screens */
function screenKey(M) {
  if (M.view === 'join') return ['join', M.joinUrl, M.between ? M.lastDecision.id : '', M.st.session ? 1 : 0].join('|');
  if (M.view === 'decision') return ['decision', M.decision.id, M.decision.status || ''].join('|');
  return `${M.view}|${M.newest.id}`;
}

function swap(next) {
  const prev = S.screen;
  S.screen = next;
  const boot = dom.stage.querySelector('.screen-boot');
  if (boot) boot.remove();
  dom.stage.appendChild(next.root);
  if (prev) {
    if (reducedMotion()) prev.root.remove();
    else { prev.root.classList.add('is-leaving'); setTimeout(() => prev.root.remove(), 240); }
  }
}

function render() {
  if (!S.cfg) return;
  const M = model();
  S.M = M;
  const key = screenKey(M);
  if (key !== S.screenKey) {
    S.screenKey = key;
    const build = { join: buildJoin, voting: buildVoting, reviewing: buildReviewing, decision: buildDecision }[M.view];
    swap(build(M));
    if (M.view !== S.view) {
      if (M.view === 'voting') announce(`Round ${M.newest.number} is open`);
      else if (M.view === 'reviewing') announce('Voting closed. The AI reviewer is judging.');
      else if (M.view === 'decision') announce(`Decision: ${verdictOf(M.decision) || 'no change'}`);
      else announce('Scan to join');
    }
    S.view = M.view;
  }
  S.screen.update(M);
  sync3d(M);
  renderHints(M);
}

function tick() {
  if (S.screen && S.screen.tick && S.M) S.screen.tick(S.M);
}

/* ---- 1. join */
function buildJoin(M) {
  const url = M.joinUrl;
  const short = shortUrl(url);
  const qr = el('div', { class: 'qr', role: 'img', 'aria-label': `QR code for ${short}` });
  drawQr(qr, url, short);
  const note = el('p', { class: 'join-note' });
  const corner = M.between ? el('div', { class: 'corner-verdict' },
    el('span', { text: M.lastDecision.round_number != null ? `Round ${M.lastDecision.round_number}` : 'Last round' }),
    verdictBadge(M.lastDecision)) : null;
  const root = el('section', { class: 'screen join' },
    el('div', { class: 'join-text' },
      el('h1', { class: 'join-title', text: 'Scan to reshape Langford A' }),
      el('p', { class: 'join-url' }, breakable(short)),
      note),
    qr,
    corner);
  return {
    root,
    update(m) { note.textContent = m.st.session ? 'Anonymous · no app · no account' : 'Waiting for the session to start'; },
  };
}

function drawQr(host, text, short) {
  const paint = () => {
    const make = window.qrcode;
    if (typeof make !== 'function') return false;
    try {
      const qr = make(0, 'M');
      qr.addData(text);
      qr.make();
      const n = qr.getModuleCount();
      const quiet = 2;
      const size = n + quiet * 2;
      let d = '';
      for (let r = 0; r < n; r++) {
        let c = 0;
        while (c < n) {
          if (!qr.isDark(r, c)) { c++; continue; }
          let len = 1;
          while (c + len < n && qr.isDark(r, c + len)) len++;
          d += `M${c + quiet} ${r + quiet}h${len}v1h-${len}z`;
          c += len;
        }
      }
      mount(host, svgEl('svg', { viewBox: `0 0 ${size} ${size}`, 'shape-rendering': 'crispEdges', 'aria-hidden': 'true', focusable: 'false' },
        svgEl('rect', { width: size, height: size, fill: '#FFFFFF' }),
        svgEl('path', { d, fill: '#111111' })));
      return true;
    } catch (_) {
      return false;
    }
  };
  if (paint()) return;
  // Plain-text fallback until (or unless) the QR library loads.
  mount(host, el('div', { class: 'qr-fallback' }, el('span', { text: 'Open on your phone' }), short));
  loadScript(QR_LIB, { timeout: 15000 }).then(() => { if (host.isConnected) paint(); }).catch(() => { /* keep the text */ });
}

/* ---- 2. voting */
function tallyQuestion(p, votes) {
  const vs = votes.filter((v) => v.question_key === p.key);
  if (p.type === 'choice') {
    const counts = new Map(p.options.map((o) => [o.value, 0]));
    let n = 0;
    for (const v of vs) {
      const k = String(v.value);
      if (counts.has(k)) { counts.set(k, counts.get(k) + 1); n++; }
    }
    let best = null;
    let bc = -1;
    for (const o of p.options) { const c = counts.get(o.value); if (c > bc) { bc = c; best = o; } }
    return { n, text: n ? best.label : '–', caption: '', share: n ? bc / n : 0 };
  }
  const nums = vs.map((v) => toNum(v.value)).filter(Number.isFinite).map((x) => snapToStep(p, x)).sort((a, b) => a - b);
  const n = nums.length;
  if (!n) return { n: 0, text: '–', caption: '', share: 0 };
  const mid = Math.floor(n / 2);
  const median = snapToStep(p, n % 2 ? nums[mid] : (nums[mid - 1] + nums[mid]) / 2);
  const band = Math.max(0, num(S.cfg.brief.consensus.slider_band_steps, 1)) * p.step;
  const share = nums.filter((x) => Math.abs(x - median) <= band + 1e-9).length / n;
  return { n, text: fmtParamValue(p, median), caption: 'median', share };
}

function setCount(node, target) {
  if (target === S.countTarget && node.textContent) return;
  const from = S.shownCount;
  S.countTarget = target;
  cancelAnimationFrame(S.countAnim);
  if (reducedMotion() || Math.abs(target - from) < 1) {
    S.shownCount = target;
    node.textContent = fmtInt(target, '0');
    return;
  }
  const t0 = performance.now();
  const dur = 480;
  const step = (t) => {
    const k = Math.min(1, (t - t0) / dur);
    const e = 1 - Math.pow(1 - k, 3);
    S.shownCount = Math.round(from + (target - from) * e);
    node.textContent = fmtInt(S.shownCount, '0');
    if (k < 1) S.countAnim = requestAnimationFrame(step);
  };
  S.countAnim = requestAnimationFrame(step);
}

function buildVoting(M) {
  S.shownCount = 0;
  S.countTarget = -1;
  const count = el('div', { class: 'count', text: '0' });
  const countLabel = el('div', { class: 'count-label', text: 'people voting' });
  const clockT = el('div', { class: 't' });
  const clockL = el('div', { class: 'l' });
  const clock = el('div', { class: 'clock', 'aria-hidden': 'true' }, clockT, clockL);
  const rows = S.cfg.params.map((p) => {
    const val = el('div', { class: 'val' });
    const bar = el('span');
    const share = el('div', { class: 'share' });
    const row = el('div', { class: 'qrow' }, el('div', { class: 'lbl', text: p.label }), val, el('div', { class: 'bar' }, bar), share);
    return { p, row, val, bar, share, last: null };
  });
  const btn = el('button', { class: 'btn btn-ring stage-btn', type: 'button' }, icon('square', 28), 'Close round', el('kbd', { text: 'Space' }));
  btn.addEventListener('click', () => { btn.blur(); primary(); });
  const root = el('section', { class: 'screen vote' },
    el('div', { class: 'vote-top' },
      el('div', null,
        el('p', { class: 'eyebrow' }, el('b', { text: `Round ${M.newest.number != null ? M.newest.number : ''}` }), ' · vote on your phone'),
        count, countLabel),
      clock),
    el('div', { class: 'rows' }, rows.map((r) => r.row)),
    el('div', { class: 'vote-bottom' },
      el('p', { class: 'eyebrow' }, 'Join at ', el('b', { text: shortUrl(M.joinUrl) })),
      btn));
  const tickClock = (m) => {
    const opened = parseTime(m.newest && m.newest.opened_at);
    if (!Number.isFinite(opened)) { clockT.textContent = ''; clockL.textContent = ''; return; }
    const left = S.cfg.profile.round_duration_s - (now() - opened) / 1000;
    const t = left >= 0 ? fmtClock(Math.ceil(left)) : `+${fmtClock(Math.floor(-left))}`;
    if (clockT.textContent !== t) clockT.textContent = t;
    const l = left >= 0 ? 'left' : 'over time';
    if (clockL.textContent !== l) clockL.textContent = l;
    clock.classList.toggle('is-over', left < 0);
  };
  return {
    root,
    tick: tickClock,
    update(m) {
      setCount(count, m.people);
      countLabel.textContent = m.people === 1 ? 'person voting' : 'people voting';
      for (const r of rows) {
        const t = tallyQuestion(r.p, m.votes);
        const sig = `${t.text}|${t.caption}`;
        if (sig !== r.last) {
          mount(r.val, t.text, t.caption ? el('small', { text: t.caption }) : null);
          if (r.last !== null && !reducedMotion()) { r.val.classList.remove('flash'); void r.val.offsetWidth; r.val.classList.add('flash'); }
          r.last = sig;
        }
        r.bar.style.width = `${(t.share * 100).toFixed(1)}%`;
        r.share.textContent = t.n ? fmtPct(t.share * 100) : '';
      }
      btn.disabled = S.busy;
      tickClock(m);
    },
  };
}

/* ---- 3. reviewing */
function checkIcon() {
  return svgEl('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 3, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' },
    svgEl('path', { d: 'M20 6 9 17l-5-5' }));
}

function buildReviewing(M) {
  const items = CHECKPOINTS.map((cp) => ({ cp, li: el('li', { class: 'check' }, el('span', { class: 'dot' }, checkIcon()), el('span', { text: cp.label })) }));
  const sub = el('p', { class: 'review-sub' });
  const eyebrow = el('p', { class: 'eyebrow' });
  const root = el('section', { class: 'screen tone-agent review' },
    eyebrow,
    el('h1', { class: 'review-title', text: 'The AI reviewer is judging…' }),
    el('ol', { class: 'checks' }, items.map((i) => i.li)),
    sub);
  return {
    root,
    update(m) {
      mount(eyebrow, el('b', { text: `Round ${m.newest.number != null ? m.newest.number : ''} closed` }), ` · ${fmtInt(m.people, '0')} ${m.people === 1 ? 'person' : 'people'} voted`);
      const maxStep = m.events.reduce((a, e) => Math.max(a, Math.min(6, num(e.step))), 0);
      const stored = m.events.some((e) => shortTool(e.tool) === 'decision');
      let activeSet = false;
      for (const { cp, li } of items) {
        const done = maxStep > cp.until || (!!cp.last && stored); // boolean: toggle(x, undefined) would flip
        const active = !done && !activeSet;
        if (active) activeSet = true;
        li.classList.toggle('is-done', done);
        li.classList.toggle('is-active', active);
        if (active) li.setAttribute('aria-current', 'step'); else li.removeAttribute('aria-current');
      }
      const last = m.events.length ? m.events[m.events.length - 1] : null;
      const text = last && last.summary ? String(last.summary) : 'Starting the review…';
      if (sub.textContent !== text) sub.textContent = text;
    },
  };
}

/* ---- 4. decision */
function shortLabel(p) { return String(p.label || p.key).split(/\s+/)[0]; }

/** The change as short parts (one per changed parameter, at most 2), each shown on its own line. */
function changeLine(d) {
  const v = verdictOf(d);
  if (!v || v === 'REJECTED') return ['Design kept'];
  if (v === 'ACCEPTED') return ['Applied as voted'];
  const params = S.cfg.params;
  const changes = d.changes.filter((c) => c && params.some((p) => p.key === c.parameter));
  let pairs = changes.map((c) => ({ p: params.find((p) => p.key === c.parameter), from: c.from, to: c.to }));
  if (!pairs.length) pairs = decisionRows(d, params).filter((r) => r.changed).map((r) => ({ p: r.param, from: r.voted, to: r.applied }));
  pairs = pairs.filter((x) => x.to !== undefined && !(x.from !== undefined && sameValue(x.p, x.from, x.to)));
  if (!pairs.length) return ['Adjusted to meet the brief'];
  return pairs.slice(0, 2).map(({ p, from, to }) => {
    if (from === undefined) return `${shortLabel(p)} → ${fmtParamValue(p, to)}`;
    const a = p.type === 'slider' ? fmtSliderNumber(p, from) : fmtParamValue(p, from);
    return `${shortLabel(p)} ${a} → ${fmtParamValue(p, to)}`;
  });
}

function goalState(g, v) {
  const t = toNum(g.threshold);
  if (!Number.isFinite(v) || !Number.isFinite(t)) return null;
  const m = Math.max(0, num(S.cfg.brief.close_tradeoff_margin, 0));
  if (g.direction === 'min') return v >= t ? 'pass' : v >= t - m ? 'marginal' : 'fail';
  return v <= t ? 'pass' : v <= t + m ? 'marginal' : 'fail';
}

/** At most 2 metrics that matter: a failed goal, then a goal whose pass/fail changed, else the first two. */
function pickMetrics(d) {
  const v = verdictOf(d);
  if (!v) return [];
  const before = asObject(d.metrics.before);
  const proposal = asObject(d.metrics.proposal);
  let after = asObject(d.metrics.after);
  if (!Object.keys(after).length) after = asObject(d.evidence.applied_metrics);
  const useProposal = v !== 'ACCEPTED' && Object.keys(proposal).length > 0;
  const from = useProposal ? proposal : before;
  const failed = asArray(d.evidence.failed_goals).map(String);
  const goals = S.cfg.brief.goals
    .map((g, i) => ({ g, i, key: g.metric || g.id }))
    .filter((x) => Number.isFinite(toNum(after[x.key])) && Number.isFinite(toNum(from[x.key])));
  const score = (x) => {
    if (failed.includes(String(x.g.id)) || failed.includes(String(x.key))) return 0;
    if (goalState(x.g, toNum(from[x.key])) !== goalState(x.g, toNum(after[x.key]))) return 1;
    return 2;
  };
  const words = { pass: 'Pass', marginal: 'Marginal', fail: 'Fail' };
  const ranked = goals.sort((a, b) => score(a) - score(b) || a.i - b.i);
  // Only the goals that matter (failed or changed pass/fail); if none, the brief's first two goals.
  const mattering = ranked.filter((x) => score(x) < 2);
  return (mattering.length ? mattering : ranked).slice(0, 2).map(({ g, key }) => {
    const a = toNum(after[key]);
    const state = goalState(g, a);
    const t = toNum(g.threshold);
    return {
      label: g.label || key,
      from: toNum(from[key]),
      to: a,
      caption: useProposal ? 'Voted → applied' : 'Before → after',
      state,
      word: state ? words[state] : '',
      target: Number.isFinite(t) ? `${g.direction === 'min' ? 'target' : 'limit'} ${fmtNum(t)}` : '',
    };
  });
}

function want3d() {
  return Q.get('v3d') !== '0' && S.v3d.status !== 'failed';
}

function buildDecision(M) {
  const d = M.decision;
  const v = verdictOf(d);
  const reason = firstSentence(v ? d.rationale : (d.message || d.rationale)) || (v ? '' : 'The design stays as it was.');
  const metrics = pickMetrics(d);
  const body = el('div', { class: 'dec-body' + (want3d() ? ' with-3d' : '') },
    el('div', { class: 'dec-text' },
      el('p', { class: 'change' }, changeLine(d).map((t) => el('span', { class: 'change-part', text: t }))),
      reason ? el('p', { class: 'reason', text: reason }) : null,
      metrics.length ? el('div', { class: 'metrics' }, metrics.map((m) => el('div', { class: 'metric' },
        el('div', { class: 'lbl', text: m.label }),
        el('div', { class: 'val' }, fmtNum(m.from), el('span', { class: 'arrow', text: ' → ' }), fmtNum(m.to)),
        el('div', { class: 'sub' }, m.caption,
          m.word ? ' · ' : '', m.word ? el('span', { class: `word-${m.state}`, text: m.word }) : null,
          m.target ? ` · ${m.target}` : '')))) : null));
  const root = el('section', { class: 'screen tone-agent dec' },
    el('p', { class: 'eyebrow' }, el('b', { text: `Round ${d.round_number != null ? d.round_number : ''}` }), ' · the reviewer decided'),
    el('h1', { class: `verdict-word ${v ? 'v-' + v.toLowerCase() : 'v-none'}`, text: v || 'NO CHANGE' }),
    body);
  return { root, body, update() { body.classList.toggle('with-3d', want3d()); } };
}

/* ---- optional 3D view of the applied design (read-only use of viewer3d.js) */
function appliedParams(d) {
  const applied = asObject(d && d.applied_parameters);
  const current = asObject(asObject(d && d.proposal).current_parameters);
  const out = {};
  for (const p of S.cfg.params) {
    let v = applied[p.key];
    if (v === undefined) v = current[p.key];
    if (v === undefined) v = p.default;
    out[p.key] = p.type === 'slider' ? num(v, num(p.default, p.min)) : String(v);
  }
  return out;
}

function fail3d(reason) {
  if (S.v3d.status === 'failed') return;
  console.warn('[stage] 3D view off:', reason);
  S.v3d.status = 'failed';
  dom.panel3d.classList.remove('is-on');
  if (S.v3d.viewer) { try { S.v3d.viewer.dispose(); } catch (_) { /* gone */ } }
  S.v3d.viewer = null;
  if (S.screen && S.screen.body) S.screen.body.classList.remove('with-3d');
}

function ensureViewer() {
  const v = S.v3d;
  if (v.status !== 'idle') return;
  v.status = 'loading';
  const host = el('div', { class: 'v3d-host' });
  dom.panel3d.appendChild(host);
  // The real building (Langford A): the base geometry is fetched and decoded once, then each
  // decision only re-assigns materials and adds the fins (js/model/langford.js).
  Promise.all([import('./viewer3d.js'), import('./model/langford.js').then((pm) => pm.loadLangford().then(() => pm))]).then(([vm, pm]) => {
    if (v.status !== 'loading') return;
    if (typeof vm.createViewer !== 'function' || typeof pm.buildLangford !== 'function') throw new Error('viewer API');
    v.mod = pm;
    v.viewer = vm.createViewer(host, {
      questionTags: pm.QUESTION_TAGS,
      quality: 'auto',
      canvasLabel: '3D model of the applied design',
      onReady: () => { if (v.status === 'loading') { v.status = 'ready'; render(); } },
      onFallback: (r) => fail3d(r),
    });
    if (typeof v.viewer.setPins === 'function') v.viewer.setPins([]);
    render();
  }).catch((e) => fail3d(e && e.message ? e.message : 'load'));
}

function sync3d(M) {
  const v = S.v3d;
  const on = M.view === 'decision' && want3d();
  if (!on) {
    dom.panel3d.classList.remove('is-on');
    if (v.viewer && v.idleKey) { v.viewer.setIdleRotation(false); v.idleKey = ''; }
    return;
  }
  ensureViewer();
  if (!v.viewer || !v.mod) return;
  const params = appliedParams(M.decision);
  const key = JSON.stringify(params);
  if (key !== v.key) {
    try {
      v.viewer.setModel(v.mod.buildLangford(params), { keepCamera: true });
      v.key = key;
    } catch (e) {
      fail3d('model');
      return;
    }
  }
  if (v.idleKey !== S.screenKey) {
    // restart the slow turn for each new decision (the viewer stops it by itself after a while)
    v.viewer.setIdleRotation(false);
    v.viewer.setIdleRotation(true);
    v.idleKey = S.screenKey;
  }
  dom.panel3d.classList.toggle('is-on', v.status === 'ready');
}

/* ================================================================== controls */
function renderHints(M) {
  let label = '';
  if (M.view === 'join' && M.st.session) label = `Open round ${M.nextNumber}`;
  else if (M.view === 'voting') label = 'Close round';
  else if (M.view === 'decision') label = 'Next round';
  const sig = label;
  if (dom.hintPrimary.dataset.sig === sig) return;
  dom.hintPrimary.dataset.sig = sig;
  mount(dom.hintPrimary, label ? [el('kbd', { text: 'Space' }), label] : []);
  dom.hintPrimary.hidden = !label;
}

function showHints(ms = 2800) {
  dom.hints.classList.add('is-on');
  document.body.classList.remove('is-idle');
  clearTimeout(S.hintTimer);
  S.hintTimer = setTimeout(() => {
    dom.hints.classList.remove('is-on');
    document.body.classList.add('is-idle');
  }, ms);
}

async function primary() {
  if (S.busy || !S.cfg || !S.live) return;
  const M = model();
  if (M.view === 'decision') {
    // "Next round": back to the join screen (QR for latecomers); Space again opens the round.
    S.dismissed = M.decision.id;
    sessionStore.set(DISMISS_KEY, S.dismissed);
    render();
    return;
  }
  if (M.view === 'reviewing') { toast('The reviewer is still working'); return; }
  if (!M.st.session) { toast('No active session yet'); return; }
  S.busy = true;
  render();
  try {
    if (M.view === 'voting') await S.backend.closeRound();
    else await S.backend.openRound();
  } catch (e) {
    toast(errText(e));
    if (e && (e.code === 'unauthorized' || e.code === 'forbidden')) authLost('Please sign in again.');
  } finally {
    S.busy = false;
    try { await refresh(); } catch (_) { render(); }
  }
}

function toggleFullscreen() {
  try {
    if (document.fullscreenElement) document.exitFullscreen();
    else if (document.documentElement.requestFullscreen) document.documentElement.requestFullscreen({ navigationUI: 'hide' });
  } catch (_) { /* not allowed */ }
}

function onKey(e) {
  if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
  if (e.code === 'Space' || e.key === ' ' || e.key === 'PageDown') {
    e.preventDefault();
    if (!e.repeat) primary();
  } else if (e.key === 'f' || e.key === 'F') {
    e.preventDefault();
    toggleFullscreen();
  } else if (e.key === 'Escape') {
    // In full screen the browser uses Esc to leave full screen; a second Esc goes to the console.
    if (!document.fullscreenElement) window.location.href = 'console.html';
  } else {
    showHints();
  }
}

async function keepAwake() {
  try {
    if ('wakeLock' in navigator && !document.hidden) S.wakeLock = await navigator.wakeLock.request('screen');
  } catch (_) { /* not supported or not allowed */ }
}

/* ================================================================== data */
async function refresh() {
  const seq = ++S.fetchSeq;
  const t0 = Date.now();
  let raw;
  try {
    raw = await S.backend.loadConsoleState();
  } catch (e) {
    if (e && (e.code === 'unauthorized' || e.code === 'forbidden')) { authLost('Your facilitator access was not accepted. Please sign in again.'); return; }
    throw e;
  }
  if (seq < S.appliedSeq) return;
  S.appliedSeq = seq;
  const server = parseTime(raw && raw.server_time);
  if (Number.isFinite(server)) S.clockOffset = server - (t0 + Date.now()) / 2;
  S.state = norm(raw);
  render();
}

function setConn(state) {
  S.conn = state;
  dom.conn.dataset.state = state;
  const txt = state === 'connected' ? 'Live' : state === 'offline' ? 'Offline' : 'Reconnecting';
  dom.conn.querySelector('.txt').textContent = txt;
  dom.conn.title = txt;
}

function startLive() {
  stopLive();
  S.live = true;
  S.unsub = S.backend.subscribe(refresh, setConn);
  keepAwake();
  showHints(5000);
}

function stopLive() {
  if (S.unsub) { try { S.unsub(); } catch (_) { /* ignore */ } }
  S.unsub = null;
  S.live = false;
}

function authLost(message) {
  if (!S.live) return;
  stopLive();
  showLogin(message);
}

/* ---- login gate (same backend sign-in as the console; the session is shared) */
function showLogin(message) {
  let gate = document.getElementById('stage-login');
  if (gate) gate.remove();
  const kind = S.backend.authKind;
  const field = (id, label, attrs) => el('div', { class: 'field' }, el('label', { for: id, text: label }), el('input', { id, class: 'input', ...attrs }));
  const fields = kind === 'password'
    ? [field('s-email', 'Email', { type: 'email', autocomplete: 'username', required: true, autocapitalize: 'none', spellcheck: 'false' }),
      el('div', { style: { height: '12px' } }),
      field('s-password', 'Password', { type: 'password', autocomplete: 'current-password', required: true })]
    : [field('s-key', 'Facilitator key', { type: 'password', autocomplete: 'off', required: true, value: (typeof S.backend.storedKey === 'function' && S.backend.storedKey()) || '' })];
  const err = el('p', { class: 'form-error', role: 'alert', text: message || '' });
  const btn = el('button', { class: 'btn btn-ink', type: 'submit', text: 'Sign in' });
  const form = el('form', { class: 'login-card', novalidate: true },
    wordmark(),
    el('div', null,
      el('h1', { class: 't-display', text: 'Stage view' }),
      el('p', { class: 'meta-line', style: { marginTop: '10px' }, text: 'Sign in as the facilitator to run the projector screen.' })),
    el('div', null, fields),
    err, btn,
    el('p', { class: 'login-help' }, el('a', { class: 'link', href: 'console.html', text: 'Back to the console' })));
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    err.textContent = '';
    btn.disabled = true;
    btn.textContent = 'Signing in…';
    const creds = kind === 'password'
      ? { email: (document.getElementById('s-email') || {}).value || '', password: (document.getElementById('s-password') || {}).value || '' }
      : { key: (document.getElementById('s-key') || {}).value || '' };
    try {
      await S.backend.signIn(creds);
      gate.remove();
      startLive();
    } catch (ex) {
      const code = ex && ex.code;
      if (code === 'not_facilitator') err.textContent = 'This account is not a facilitator';
      else if (code === 'invalid_credentials') err.textContent = ex.message;
      else if (isRetryable(ex)) err.textContent = 'Could not reach the server. Try again.';
      else err.textContent = `Sign-in failed (${code || 'error'})`;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Sign in';
    }
  });
  gate = el('div', { id: 'stage-login', class: 'login stage-login' }, form);
  document.body.appendChild(gate);
  const first = form.querySelector('input');
  if (first) setTimeout(() => first.focus(), 30);
}

/* ================================================================== demo backend */
// Canned, fully interactive data for rehearsals and tests (?demo=1). Everything is built from the
// config files, so it follows parameters.json and the brief. It is clearly labelled "Demo data".
function seeded(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function createDemoBackend(cfg) {
  const rnd = seeded(20260930);
  const iso = (ms) => new Date(ms).toISOString();
  const D = { session: { id: 'demo', title: cfg.profile.session_title || 'Demo session', status: 'active', created_at: iso(Date.now()) }, rounds: [], votes: [], events: [], decisions: [], timers: new Set(), voteId: 1, eventId: 1, pid: 1 };
  const later = (fn, ms) => { const t = setTimeout(() => { D.timers.delete(t); fn(); }, ms); D.timers.add(t); };
  const params = cfg.params;
  const goals = cfg.brief.goals;
  const currentParams = () => {
    const last = D.decisions[D.decisions.length - 1];
    const out = {};
    for (const p of params) out[p.key] = last && last.applied_parameters[p.key] !== undefined ? last.applied_parameters[p.key] : p.default;
    return out;
  };

  function castVotes(r, at) {
    const pid = 'demo-p' + (D.pid++);
    const cur = currentParams();
    for (const p of params) {
      let value;
      if (p.type === 'choice') {
        const lead = (num(r.number) + 1) % p.options.length;
        const w = p.options.map((o, i) => (i === lead ? 3.2 : 1 + rnd()));
        let x = rnd() * w.reduce((s, y) => s + y, 0);
        let i = 0;
        while (i < w.length - 1 && x > w[i]) { x -= w[i]; i++; }
        value = p.options[i].value;
      } else {
        const center = num(cur[p.key], p.min) + (num(r.number) % 2 ? 1 : -1) * p.step;
        const g = (rnd() + rnd() + rnd() - 1.5) * 3.2;
        value = String(snapToStep(p, center + g * p.step));
      }
      D.votes.push({ id: D.voteId++, round_id: r.id, participant_id: pid, question_key: p.key, value, is_simulated: true, created_at: iso(at) });
    }
    r.participants = new Set(D.votes.filter((v) => v.round_id === r.id).map((v) => v.participant_id)).size;
  }

  function voters(r, target) {
    const next = () => {
      if (r.status !== 'open' || r.participants >= target) return;
      const k = 1 + Math.floor(rnd() * 3);
      for (let i = 0; i < k && r.participants < target; i++) castVotes(r, Date.now());
      later(next, 350 + rnd() * 650);
    };
    later(next, 600);
  }

  function tallyAll(r) {
    const votes = D.votes.filter((v) => v.round_id === r.id);
    const voted = {};
    const tally = {};
    for (const p of params) {
      const vs = votes.filter((v) => v.question_key === p.key);
      if (p.type === 'choice') {
        const counts = {};
        for (const o of p.options) counts[o.value] = 0;
        for (const v of vs) if (counts[v.value] != null) counts[v.value] += 1;
        const winner = p.options.reduce((a, o) => (counts[o.value] > counts[a.value] ? o : a), p.options[0]).value;
        voted[p.key] = winner;
        tally[p.key] = { type: 'choice', n: vs.length, winner, counts };
      } else {
        const nums = vs.map((v) => toNum(v.value)).sort((a, b) => a - b);
        const median = nums.length ? snapToStep(p, nums[Math.floor(nums.length / 2)]) : num(currentParams()[p.key], p.min);
        voted[p.key] = median;
        tally[p.key] = { type: 'slider', n: nums.length, median };
      }
    }
    return { voted, tally };
  }

  function makeDecision(r) {
    const { voted, tally } = tallyAll(r);
    const before = currentParams();
    const modify = num(r.number) % 2 === 1;
    const sliders = params.filter((p) => p.type === 'slider');
    const target = sliders[sliders.length - 1];
    const applied = { ...voted };
    const changes = [];
    const fg = goals.find((g) => g.direction === 'max') || goals[0];
    if (modify && target) {
      const from = voted[target.key];
      let to = snapToStep(target, from - target.step);
      if (to === from) to = snapToStep(target, from + target.step);
      applied[target.key] = to;
      changes.push({ parameter: target.key, from, to, reason: fg ? `One step keeps ${String(fg.label).toLowerCase()} within its limit.` : '' });
    }
    const mb = {}; const mp = {}; const ma = {};
    for (const g of goals) {
      const key = g.metric || g.id;
      const t = num(g.threshold, 50);
      const s = g.direction === 'min' ? 1 : -1;
      mb[key] = Math.round((t + s * 8.4) * 10) / 10;
      mp[key] = Math.round((g === fg && modify ? t - s * 3.6 : t + s * 5.2) * 10) / 10;
      ma[key] = Math.round((g === fg && modify ? t + s * 2.3 : mp[key]) * 10) / 10;
    }
    const fk = fg ? fg.metric || fg.id : '';
    const rationale = modify && fg && target
      ? `The room's proposal would bring ${String(fg.label).toLowerCase()} to ${fmtNum(mp[fk])}, past the limit of ${fmtNum(fg.threshold)}. Moving ${String(target.label).toLowerCase()} one step brings it back to ${fmtNum(ma[fk])} and keeps every other vote as cast.`
      : `The room's proposal passes every hard rule and meets all ${goals.length} goals of the brief. It is applied exactly as voted.`;
    return {
      id: `demo-d${r.number}`, session_id: 'demo', round_id: r.id, round_number: r.number, status: 'ok',
      verdict: modify ? 'MODIFIED' : 'ACCEPTED',
      proposal: { parameters: voted, current_parameters: before, tally },
      applied_parameters: applied, changes,
      evidence: { failed_goals: modify && fg ? [fg.id] : [], failed_rules: [] },
      alternatives_considered: [], metrics: { before: mb, proposal: mp, after: ma },
      rationale, created_at: iso(Date.now()), duration_s: 14,
    };
  }

  function review(r, speed = 1500) {
    const steps = [
      [1, 'get_project_brief', `Read ${goals.length} goals and ${cfg.brief.hard_rules.length} hard rules`],
      [2, 'evaluate', 'Proposal: checking every hard rule and goal'],
      [3, 'evaluate', 'Alternative A: one value one step away'],
      [3, 'evaluate', 'Alternative B: keep the votes, adjust another value'],
      [4, 'decide', 'Choosing the closest design that meets the brief'],
      [5, 'set_parameters', 'Applying the decision to the model'],
      [6, 'get_parameters', 'The model state matches the decision'],
      [6, 'evaluate', 'Verified the applied design'],
      [6, 'decision', 'Decision stored'],
    ];
    steps.forEach(([step, tool, summary], i) => later(() => {
      D.events.push({ id: D.eventId++, round_id: r.id, seq: i + 1, step, tool, summary, created_at: iso(Date.now()) });
      if (i === steps.length - 1) later(() => D.decisions.push(makeDecision(r)), 700);
    }, 900 + i * speed));
  }

  const api = {
    kind: 'demo',
    authKind: 'none',
    async restore() { return { label: 'Demo', initial: 'D' }; },
    async signIn() { return { label: 'Demo', initial: 'D' }; },
    async signOut() {},
    onAuthLost() {},
    async loadConsoleState() {
      const round = D.rounds.length ? D.rounds[D.rounds.length - 1] : null;
      return {
        session: D.session, questions: [], rounds: D.rounds.map((r) => ({ ...r })), round: round ? { ...round } : null,
        votes: round ? D.votes.filter((v) => v.round_id === round.id) : [],
        agent_events: round ? D.events.filter((e) => e.round_id === round.id) : [],
        decisions: D.decisions.slice(), participants_total: D.pid - 1, server_time: iso(Date.now()),
      };
    },
    async openRound() {
      if (D.rounds.some((r) => r.status === 'open')) throw new BackendError('round_already_open', 'A round is already open');
      const r = { id: `demo-r${D.rounds.length + 1}`, session_id: 'demo', number: D.rounds.length + 1, status: 'open', opened_at: iso(Date.now()), closed_at: null, participants: 0 };
      D.rounds.push(r);
      voters(r, 34 + Math.floor(rnd() * 16));
      return { round: r };
    },
    async closeRound() {
      const r = D.rounds.find((x) => x.status === 'open');
      if (!r) throw new BackendError('no_open_round', 'No round is open');
      r.status = 'closed';
      r.closed_at = iso(Date.now());
      review(r);
      return { round: r };
    },
    subscribe(onChange, onConnection) {
      onConnection('connected');
      const run = () => { onChange().catch(() => {}); };
      run();
      const t = setInterval(run, 400);
      return () => clearInterval(t);
    },
    /** Jump straight to a state for tests and screenshots. */
    jump(state) {
      if (!state || state === 'join') return;
      const t = Date.now();
      const r = { id: 'demo-r1', session_id: 'demo', number: 1, status: 'open', opened_at: iso(t - 38000), closed_at: null, participants: 0 };
      D.rounds.push(r);
      const pre = state === 'voting' ? 29 : 43;
      for (let i = 0; i < pre; i++) castVotes(r, t - 36000 + (i / pre) * 34000);
      if (state === 'voting') { voters(r, 47); return; }
      r.status = 'closed';
      r.closed_at = iso(t - 1500);
      if (state === 'reviewing') { review(r, 2600); return; }
      review(r, 0);
      return state === 'lobby' ? 'lobby' : 'decision';
    },
  };
  return api;
}

/* ================================================================== boot */
async function boot() {
  loadIcons();
  let attempt = 0;
  while (!S.cfg) {
    try {
      const cfg = window.PLURARCH_CONFIG || {};
      S.cfg = await loadAppConfig(cfg.useCase);
    } catch (e) {
      const t = dom.stage.querySelector('.boot-text');
      if (t) t.textContent = 'Could not load the configuration. Retrying…';
      await sleep(Math.min(1000 * 2 ** attempt, 10000));
      attempt += 1;
    }
  }

  document.addEventListener('keydown', onKey);
  document.addEventListener('mousemove', () => showHints(), { passive: true });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && S.live) keepAwake(); });
  dom.hintFs.addEventListener('click', () => { dom.hintFs.blur(); toggleFullscreen(); });
  setInterval(tick, 250);
  if (Q.get('debug') === '1') window.__plurarchStage = { S, model, render }; // test aid

  if (DEMO) {
    S.backend = createDemoBackend(S.cfg);
    dom.demoChip.hidden = false;
    sessionStore.remove(DISMISS_KEY);
    S.dismissed = null;
    const start = S.backend.jump(Q.get('state'));
    startLive();
    if (start === 'lobby' || start === 'decision') {
      // wait for the canned decision, then (for the lobby) press "next round" once
      for (let i = 0; i < 60 && !(S.state && S.state.decisions.length); i++) await sleep(100);
      if (start === 'lobby' && S.state && S.state.decisions.length) {
        S.dismissed = S.state.decisions[S.state.decisions.length - 1].id;
        render();
      }
    }
    return;
  }

  S.backend = createBackend('console');
  S.backend.onAuthLost(() => authLost('You were signed out.'));
  if (S.backend.kind === 'supabase' && !S.backend.configured()) {
    showLogin('Supabase is not configured in config.js yet.');
    return;
  }
  try {
    const user = await S.backend.restore();
    if (user) { startLive(); return; }
    showLogin('');
  } catch (e) {
    showLogin(isRetryable(e) ? 'Could not reach the server yet. Check the connection and sign in.' : '');
  }
}

boot().catch((e) => {
  console.error(e);
  mount(dom.stage, el('section', { class: 'screen screen-boot' }, el('p', { class: 'boot-text', text: 'The stage could not start. Please reload the page.' })));
});
