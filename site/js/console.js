// Plurarch facilitator console: login gate, live heatmap, reviewer-agent status, decision,
// metrics, participation, trace, decision history, brief, projection mode.
// Local mode polls the console state every second; Supabase mode uses one realtime channel.

import { createBackend, isRetryable } from './backend.js';
import {
  el, mount, clear, icon, loadIcons, loadD3, loadAppConfig, store, sleep, asArray, asObject,
  verdictBadge, verdictOf, decisionRows, sliderValues, toast, wordmark, refreshIcons, reducedMotion,
} from './ui.js';
import {
  fmtInt, fmtNum, fmtSigned, fmtPct, fmtTime, fmtTimeS, fmtClock, fmtDuration, parseTime, joinDot,
  sentenceCase, fmtParamValue, toNum, plural,
} from './format.js';
import { renderHeatmap, renderPie, pieLegend, renderStrip } from './charts.js';

const $ = (id) => document.getElementById(id);

const STEP_LABELS = {
  1: 'Reading the brief',
  2: 'Evaluating the proposal',
  3: 'Evaluating alternatives',
  4: 'Deciding',
  5: 'Applying to the model',
  6: 'Verifying',
};
const TOTAL_STEPS = 6;
const PROJECTION_KEY = 'plurarch:projection';

const C = {
  cfg: null,
  backend: null,
  user: null,
  state: null,
  selectedRoundId: null, // null = follow the newest round
  selectedQuestion: null,
  projection: false,
  conn: null,
  clockOffset: 0,
  busy: false,
  sigs: Object.create(null),
  unsub: null,
  fetchSeq: 0,
  appliedSeq: 0,
  traceRoundId: undefined,
  traceSeen: new Set(),
  traceLastSeq: -Infinity,
  d3Ready: false,
  animatedDecisionId: null,
  shellReady: false,
  ticker: null,
};

/* ================================================================== state */
function num(v, d = 0) { const n = toNum(v); return Number.isFinite(n) ? n : d; }

function normDecision(d) {
  if (!d || typeof d !== 'object') return null;
  return {
    ...d,
    id: d.id != null ? String(d.id) : '',
    round_id: d.round_id != null ? String(d.round_id) : '',
    proposal: asObject(d.proposal),
    applied_parameters: asObject(d.applied_parameters),
    evidence: asObject(d.evidence),
    alternatives_considered: asArray(d.alternatives_considered),
    changes: asArray(d.changes),
    metrics: asObject(d.metrics),
  };
}

function normRound(r) {
  if (!r || typeof r !== 'object' || r.id == null) return null;
  return { ...r, id: String(r.id), status: r.status === 'open' ? 'open' : 'closed' };
}

function normState(raw) {
  const s = raw && typeof raw === 'object' ? raw : {};
  const rounds = asArray(s.rounds).map(normRound).filter(Boolean).sort((a, b) => num(a.number) - num(b.number));
  const round = normRound(s.round);
  return {
    session: s.session && typeof s.session === 'object' ? s.session : null,
    questions: asArray(s.questions).filter((q) => q && typeof q === 'object'),
    rounds,
    round: round ? (rounds.find((r) => r.id === round.id) || round) : null,
    votes: asArray(s.votes).filter((v) => v && typeof v === 'object'),
    agent_events: asArray(s.agent_events).filter((e) => e && typeof e === 'object').sort((a, b) => num(a.seq) - num(b.seq)),
    decisions: asArray(s.decisions).map(normDecision).filter(Boolean),
    participants_total: Number.isFinite(toNum(s.participants_total)) ? toNum(s.participants_total) : null,
    server_time: s.server_time || null,
    join_url: typeof s.join_url === 'string' && s.join_url.trim() ? s.join_url.trim() : null,
    failed_submissions: Number.isFinite(toNum(s.failed_submissions)) ? toNum(s.failed_submissions) : null,
  };
}

function mergeQuestion(p, q) {
  const type = q && (q.type === 'slider' || q.type === 'choice') ? q.type : (p ? p.type : 'choice');
  const key = p ? p.key : String(q.key);
  const m = { key, type, label: p ? p.label : sentenceCase(key), unit: p ? p.unit : '' };
  if (type === 'choice') {
    const cfgOpts = p && p.options ? p.options : [];
    const dbOpts = asArray(q && q.options).map((o) => (o && typeof o === 'object' ? String(o.value) : String(o)));
    const values = dbOpts.length ? dbOpts : cfgOpts.map((o) => o.value);
    m.options = values.map((v) => cfgOpts.find((o) => o.value === v) || { value: v, label: sentenceCase(v), icon: 'circle' });
  } else {
    const pick = (a, b, d) => (Number.isFinite(toNum(a)) ? toNum(a) : Number.isFinite(toNum(b)) ? toNum(b) : d);
    m.min = pick(q && q.min, p && p.min, 0);
    m.max = pick(q && q.max, p && p.max, 100);
    m.step = pick(q && q.step, p && p.step, 1);
    if (!(m.step > 0)) m.step = 1;
    if (m.max < m.min) [m.min, m.max] = [m.max, m.min];
  }
  return m;
}

function questionList() {
  const dbq = C.state ? C.state.questions : [];
  const byKey = new Map();
  for (const q of dbq) if (typeof q.key === 'string') byKey.set(q.key, q);
  const out = [];
  for (const p of C.cfg.params) { out.push(mergeQuestion(p, byKey.get(p.key))); byKey.delete(p.key); }
  for (const q of [...byKey.values()].sort((a, b) => num(a.position) - num(b.position))) out.push(mergeQuestion(null, q));
  return out;
}

function model() {
  const st = C.state || normState(null);
  const rounds = st.rounds;
  const newest = rounds.length ? rounds[rounds.length - 1] : null;
  const openRound = rounds.find((r) => r.status === 'open') || null;
  const round = st.round;
  const decision = round ? st.decisions.find((d) => d.round_id === round.id) || null : null;
  const latestDecision = st.decisions.length ? st.decisions[st.decisions.length - 1] : null;
  const questions = questionList();
  const q = questions.find((x) => x.key === C.selectedQuestion) || questions[0] || null;
  const roundParticipants = new Set(st.votes.map((v) => v.participant_id)).size;
  return { st, rounds, newest, openRound, round, decision, latestDecision, questions, q, roundParticipants };
}

function now() { return Date.now() + C.clockOffset; }
function roundName(n) { return n != null && n !== '' ? `Round ${n}` : 'Round'; }
function changed(name, sig) {
  const s = typeof sig === 'string' ? sig : JSON.stringify(sig);
  if (C.sigs[name] === s) return false;
  C.sigs[name] = s;
  return true;
}
function shortTool(tool) {
  return String(tool || '').replace(/^mcp__.+?__/, '').replace(/^design[-_]mcp[.:]/, '') || '–';
}

/* ================================================================== fetch */
async function refresh() {
  const seq = ++C.fetchSeq;
  const want = C.selectedRoundId;
  const t0 = Date.now();
  let raw;
  try {
    raw = await C.backend.loadConsoleState(want || undefined);
  } catch (e) {
    if (e && (e.code === 'unauthorized' || e.code === 'forbidden')) {
      authLost('Your facilitator access was not accepted. Please sign in again.');
      return;
    }
    throw e;
  }
  if (seq < C.appliedSeq || want !== C.selectedRoundId) return; // stale response
  C.appliedSeq = seq;
  const t1 = Date.now();
  const server = parseTime(raw && raw.server_time);
  if (Number.isFinite(server)) C.clockOffset = server - (t0 + t1) / 2;
  C.state = normState(raw);
  if (C.selectedRoundId && !C.state.rounds.some((r) => r.id === C.selectedRoundId)) C.selectedRoundId = null;
  render();
}

function refreshSoon() {
  refresh().catch(() => { /* the subscription loop reports connection problems */ });
}

/* ================================================================== render */
function render() {
  if (!C.state || !C.cfg) return;
  const M = model();
  renderTop(M);
  renderSidebar(M);
  renderHeader(M);
  renderToolbar(M);
  renderViz(M);
  renderAgent(M);
  renderDecision(M);
  renderMetrics(M);
  renderParticipation(M);
  renderTrace(M);
  renderHistory(M);
  tick();
}

function setConn(state) {
  C.conn = state;
  const node = $('conn');
  node.dataset.state = state;
  $('conn-text').textContent = state === 'connected' ? 'Live' : state === 'offline' ? 'Offline' : 'Reconnecting';
}

function renderTop(M) {
  const title = (M.st.session && M.st.session.title) || C.cfg.profile.session_title || 'Session';
  if (!changed('top', [title, M.st.session ? 1 : 0])) return;
  $('top-session').textContent = M.st.session ? title : 'No active session';
  $('top-session').title = $('top-session').textContent;
}

function renderSidebar(M) {
  const s = M.st.session;
  const title = (s && s.title) || C.cfg.profile.session_title || 'Session';
  const started = s ? fmtTime(s.created_at, '') : '';
  const meta = s ? joinDot([C.cfg.profile.label, started ? `started ${started}` : '']) : 'No active session';
  const closedCount = M.rounds.filter((r) => r.status === 'closed').length;
  const events = M.st.agent_events.length;
  const sig = [title, meta, M.st.participants_total, M.rounds.length, closedCount, M.openRound ? 1 : 0, M.st.decisions.length, events];
  if (!changed('side', sig)) return;
  $('side-title').textContent = s ? title : 'No active session';
  $('side-meta').textContent = meta;
  $('side-participants').textContent = fmtInt(M.st.participants_total, '0');
  $('side-rounds').textContent = fmtInt(M.rounds.length, '0');
  $('side-rounds-sub').textContent = M.rounds.length ? (M.openRound ? '1 open' : `${closedCount} closed`) : 'none yet';
  const dc = $('nav-count-decisions');
  dc.hidden = !M.st.decisions.length;
  dc.textContent = fmtInt(M.st.decisions.length);
  const tc = $('nav-count-trace');
  tc.hidden = !events;
  tc.textContent = fmtInt(events);
}

function b(text) { return el('b', { text: String(text) }); }

function renderHeader(M) {
  const total = M.st.participants_total;
  const nw = M.newest;
  const ld = M.latestDecision;
  const join = M.st.join_url;
  const sig = [M.st.session ? 1 : 0, total, nw && nw.id, nw && nw.status, ld && ld.created_at, join];
  if (!changed('header', sig)) return;
  const line = $('meta-line');
  clear(line);
  const parts = [];
  if (!M.st.session) {
    parts.push(['No active session. Start one from the orchestrator.']);
  } else {
    parts.push([b(fmtInt(total, '0')), total === 1 ? ' participant' : ' participants']);
    if (nw) parts.push(['Round ', b(nw.number != null ? nw.number : '–'), nw.status === 'open' ? ' open' : ' closed']);
    else parts.push(['No rounds yet']);
    if (ld) parts.push(['Last decision at ', b(fmtTime(ld.created_at))]);
    if (join) parts.push(['Join at ', b(join)]);
  }
  parts.forEach((p, i) => {
    if (i) line.append(' · ');
    line.append(...p);
  });

  const joinCard = $('join');
  if (join) {
    // qr_plain.png: the code only (no label inside the image), so it scans well off a screen
    const img = el('img', { src: 'qr_plain.png', alt: 'QR code for the join link', width: 132, height: 132 });
    img.addEventListener('error', () => { img.hidden = true; });
    const big = el('button', { class: 'btn btn-gray join-big', type: 'button', text: 'Show big QR' });
    big.addEventListener('click', () => showBigQr(join));
    img.addEventListener('click', () => showBigQr(join));
    mount(joinCard, img, el('div', null,
      el('div', { class: 't-label', text: 'Join' }),
      el('div', { class: 'url', text: join }),
      el('div', { class: 't-small t-secondary', text: 'Scan the code or type the address. No account needed.' }),
      big));
    joinCard.hidden = false;
  } else {
    joinCard.hidden = true;
  }
}

/** Full-screen join overlay for the projector: a huge QR code and the address. Click or Escape closes. */
function showBigQr(url) {
  const old = document.getElementById('qr-overlay');
  if (old) old.remove();
  const close = () => { ov.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  const ov = el('div', { id: 'qr-overlay', class: 'qr-overlay', role: 'dialog', 'aria-label': 'Join this session' },
    el('img', { src: 'qr_plain.png', alt: 'QR code for the join link' }),
    el('div', { class: 'qr-overlay-url', text: url }),
    el('div', { class: 'qr-overlay-hint', text: 'Scan with your phone camera · same Wi-Fi · click anywhere to close' }));
  ov.addEventListener('click', close);
  document.addEventListener('keydown', onKey);
  document.body.append(ov);
}

function roundSegLabel(r, selected) {
  if (!selected) return roundName(r.number);
  const from = fmtTime(r.opened_at, '');
  const to = r.status === 'open' ? 'now' : fmtTime(r.closed_at, '');
  return from ? `${roundName(r.number)} · ${from}–${to}` : roundName(r.number);
}

function isReviewing(M) {
  return !!(M.round && M.round.status === 'closed' && !M.decision);
}

function renderToolbar(M) {
  const sel = M.round ? M.round.id : null;
  const roundsSig = M.rounds.map((r) => [r.id, r.status, r.opened_at, r.closed_at, r.number]);
  if (changed('segRounds', [roundsSig, sel])) {
    const seg = $('seg-rounds');
    clear(seg);
    if (!M.rounds.length) {
      seg.appendChild(el('button', { class: 'seg-btn', type: 'button', disabled: true, 'aria-pressed': 'true', text: 'No rounds yet' }));
    } else {
      for (const r of M.rounds) {
        const selected = r.id === sel;
        seg.appendChild(el('button', {
          class: 'seg-btn', type: 'button', 'aria-pressed': selected ? 'true' : 'false', dataset: { roundId: r.id },
        }, r.status === 'open' ? el('span', { class: 'live-dot', 'aria-hidden': 'true' }) : null, roundSegLabel(r, selected)));
      }
      const active = seg.querySelector('[aria-pressed="true"]');
      if (active && active.scrollIntoView) active.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }
  }
  const qKey = M.q ? M.q.key : null;
  if (changed('segQuestions', [M.questions.map((q) => [q.key, q.label]), qKey])) {
    const seg = $('seg-questions');
    clear(seg);
    for (const q of M.questions) {
      seg.appendChild(el('button', {
        class: 'seg-btn', type: 'button', 'aria-pressed': q.key === qKey ? 'true' : 'false', dataset: { q: q.key }, text: q.label,
      }));
    }
  }
  const btnSig = [M.st.session ? 1 : 0, M.openRound && M.openRound.id, M.newest && M.newest.number, C.busy];
  if (changed('roundBtn', btnSig)) {
    const btn = $('round-btn');
    clear(btn);
    if (!M.st.session) {
      btn.disabled = true;
      btn.append('No active session');
    } else if (M.openRound) {
      btn.disabled = C.busy;
      btn.append(C.busy ? icon('loader-circle', 18, 'spin') : icon('square', 18), `Close round ${M.openRound.number != null ? M.openRound.number : ''}`.trim());
    } else {
      const next = M.newest && Number.isFinite(toNum(M.newest.number)) ? toNum(M.newest.number) + 1 : 1;
      btn.disabled = C.busy;
      btn.append(C.busy ? icon('loader-circle', 18, 'spin') : icon('play', 18), `Open round ${next}`);
    }
  }
  const events = M.st.agent_events.length;
  const reviewing = isReviewing(M);
  if (changed('traceBtn', [events, reviewing])) {
    const cnt = $('tb-trace-count');
    cnt.hidden = !events;
    cnt.textContent = events > 99 ? '99+' : String(events);
    $('tb-trace-dot').hidden = !reviewing || !!events;
  }
}

function heatmapModel(M) {
  const q = M.q;
  const round = M.round;
  const dur = C.cfg.profile.round_duration_s;
  let rows = [];
  let rowIndex = () => -1;
  if (q && q.type === 'choice') {
    rows = q.options.map((o) => ({ label: o.label }));
    const idx = new Map(q.options.map((o, i) => [o.value, i]));
    rowIndex = (v) => (idx.has(String(v)) ? idx.get(String(v)) : -1);
  } else if (q) {
    const values = sliderValues(q).slice().reverse(); // highest at the top
    rows = values.map((v) => ({ label: fmtParamValue(q, v) }));
    rowIndex = (v) => {
      const n = toNum(v);
      if (!Number.isFinite(n)) return -1;
      let best = -1;
      let bestD = Infinity;
      values.forEach((x, i) => { const d = Math.abs(x - n); if (d < bestD) { bestD = d; best = i; } });
      return bestD <= q.step / 2 + 1e-9 ? best : -1;
    };
  }
  let bucketS = Math.max(1, Math.round(dur / 12));
  if (!round || !q) {
    const cols = Array.from({ length: 12 }, (_, c) => ({ label: c % 2 === 0 ? fmtClock(c * bucketS) : '', range: '' }));
    return {
      key: ['none', q && q.key, rows.length].join('|'), rows, cols,
      cells: rows.map(() => cols.map(() => ({ count: 0, future: true }))), max: 0,
      ariaLabel: 'No round yet', overlay: M.st.session ? 'Open a round to see votes arrive' : 'No active session',
    };
  }
  const votes = M.st.votes.filter((v) => v.question_key === q.key);
  const times = votes.map((v) => parseTime(v.created_at)).filter(Number.isFinite);
  let start = parseTime(round.opened_at);
  if (!Number.isFinite(start)) start = times.length ? Math.min(...times) : now();
  let end;
  if (round.status === 'open') end = Math.max(now(), times.length ? Math.max(...times) : -Infinity);
  else {
    const closed = parseTime(round.closed_at);
    end = Number.isFinite(closed) ? closed : (times.length ? Math.max(...times) : start + dur * 1000);
  }
  const elapsedS = Math.max(0, (end - start) / 1000);
  let n = Math.max(12, Math.ceil(elapsedS / bucketS));
  if (n > 24) {
    bucketS = Math.ceil(elapsedS / 24 / 5) * 5;
    n = Math.max(12, Math.ceil(elapsedS / bucketS));
  }
  const labelEvery = Math.max(1, Math.ceil(n / 6));
  const cols = Array.from({ length: n }, (_, c) => ({
    label: c % labelEvery === 0 ? fmtClock(c * bucketS) : '',
    range: `${fmtClock(c * bucketS)}–${fmtClock((c + 1) * bucketS)}`,
  }));
  const cells = rows.map(() => cols.map((_, c) => ({ count: 0, future: start + c * bucketS * 1000 > end })));
  let max = 0;
  for (const v of votes) {
    const r = rowIndex(v.value);
    if (r < 0) continue;
    const t = parseTime(v.created_at);
    let c = Number.isFinite(t) ? Math.floor((t - start) / (bucketS * 1000)) : n - 1;
    c = Math.min(n - 1, Math.max(0, c));
    const cell = cells[r][c];
    cell.count += 1;
    cell.future = false;
    if (cell.count > max) max = cell.count;
  }
  let overlay = null;
  if (!votes.length) overlay = round.status === 'open' ? 'Waiting for the first votes' : 'No votes for this question';
  return {
    key: [round.id, q.key, rows.length, n, bucketS].join('|'),
    rows, cols, cells, max, overlay,
    ariaLabel: `Votes over time for ${q.label}, ${roundName(round.number)}: ${votes.length} votes`,
  };
}

function legendNode() {
  const ramp = [1, 2, 3, 4, 5, 6].map((i) => el('span', { class: 'sw', style: { background: `var(--accent-${i})` } }));
  return [el('span', { text: 'Fewer' }), ...ramp, el('span', { text: 'More' }), el('span', { class: 'gap' }),
    el('span', { class: 'sw', style: { background: 'var(--neutral-1)' } }), el('span', { text: 'Not yet' })];
}

function renderViz(M) {
  const q = M.q;
  const votesQ = q ? M.st.votes.filter((v) => v.question_key === q.key) : [];
  if (changed('vizHead', [q && q.key, q && q.label, M.round && M.round.id, votesQ.length])) {
    const title = $('viz-title');
    clear(title);
    if (q) {
      title.append(`Vote activity · ${q.label}`);
      if (M.round) title.append(el('span', { class: 't-secondary', style: { fontWeight: '500' }, text: ` · ${plural(votesQ.length, 'vote')}` }));
    } else {
      title.append('Vote activity');
    }
  }
  if (changed('vizLegend', 'static')) mount($('viz-legend'), legendNode());

  renderHeatmap($('heatmap'), heatmapModel(M));

  const under = $('under');
  const decisionTally = M.decision ? asObject(asObject(M.decision.proposal).tally) : {};
  if (!changed('under', [q && q.key, M.round && M.round.id, votesQ.length, M.decision && M.decision.id])) return;
  if (!q) { clear(under); return; }
  if (q.type === 'slider') {
    const values = sliderValues(q);
    const counts = values.map(() => 0);
    const nums = [];
    for (const v of votesQ) {
      const n = toNum(v.value);
      if (!Number.isFinite(n)) continue;
      const i = Math.round((n - q.min) / q.step);
      if (i >= 0 && i < values.length) { counts[i] += 1; nums.push(values[i]); }
    }
    let median = null;
    const t = asObject(decisionTally[q.key]);
    if (Number.isFinite(toNum(t.median))) median = toNum(t.median);
    else if (nums.length) {
      nums.sort((a, c) => a - c);
      const mid = Math.floor(nums.length / 2);
      const raw = nums.length % 2 ? nums[mid] : (nums[mid - 1] + nums[mid]) / 2;
      median = q.min + Math.round((raw - q.min) / q.step) * q.step;
    }
    const labelEvery = values.length > 14 ? Math.ceil(values.length / 10) : 1;
    const max = Math.max(0, ...counts);
    const medianIdx = median == null ? null : (median - q.min) / q.step;
    renderStrip(under, {
      key: [q.key, values.length].join('|'),
      bins: values.map((v, i) => ({ label: i % labelEvery === 0 ? fmtParamValue(q, v) : '', count: counts[i], full: fmtParamValue(q, v) })),
      max,
      medianPos: medianIdx == null ? null : (medianIdx + 0.5) / values.length,
      medianLabel: median == null ? '' : fmtParamValue(q, median),
    });
  } else {
    under._strip = null;
    const total = votesQ.length;
    const counts = new Map(q.options.map((o) => [o.value, 0]));
    for (const v of votesQ) if (counts.has(String(v.value))) counts.set(String(v.value), counts.get(String(v.value)) + 1);
    mount(under, el('div', { class: 'choice-counts' }, q.options.map((o) => {
      const c = counts.get(o.value) || 0;
      return el('div', { class: 'choice-count' },
        icon(o.icon, 20),
        el('div', null,
          el('div', { class: 'lbl', text: o.label }),
          el('div', null, el('span', { class: 'cnt', text: fmtInt(c) }), el('span', { class: 'pct', text: ` · ${total ? fmtPct((c / total) * 100) : '0%'}` }))));
    })));
  }
}

function countEvaluations(events) {
  const exempt = C.cfg.brief.agent_limits.verify_evaluate_exempt === true;
  return events.filter((e) => /evaluate/i.test(String(e.tool || '')) && !(exempt && num(e.step) >= TOTAL_STEPS)).length;
}

function renderAgent(M) {
  const events = M.st.agent_events;
  const d = M.decision;
  const round = M.round;
  const last = events.length ? events[events.length - 1] : null;
  const sig = [round && round.id, round && round.status, d && d.id, d && d.status, events.length, last && (last.id || last.seq), C.cfg ? 1 : 0];
  if (!changed('agent', sig)) return;

  let status = 'Idle';
  if (d) status = d.status === 'failed' ? 'Failed' : 'Done';
  else if (round && round.status === 'closed') status = 'Reviewing';
  const chipCls = { Idle: 'chip-idle', Reviewing: 'chip-reviewing', Done: 'chip-done', Failed: 'chip-failed' }[status];
  const chip = el('span', { class: `chip ${chipCls}` },
    status === 'Reviewing' ? el('span', { class: 'live-dot', 'aria-hidden': 'true' }) : null,
    status === 'Done' ? icon('check', 14) : null,
    status === 'Failed' ? icon('x', 14) : null,
    status);

  const maxStep = events.reduce((m, e) => Math.max(m, Math.min(TOTAL_STEPS, num(e.step))), 0);
  const progress = d && d.status !== 'failed' ? 1 : maxStep / TOTAL_STEPS;

  let line;
  let sub = '';
  if (last) {
    const tool = shortTool(last.tool);
    const label = tool === 'decision' ? 'Decision stored' : (STEP_LABELS[num(last.step)] || sentenceCase(tool));
    line = joinDot([label, fmtTime(last.created_at, '')]);
    sub = last.summary && String(last.summary).trim() !== label ? String(last.summary) : '';
  } else if (status === 'Reviewing') {
    line = joinDot(['Starting the review', fmtTime(round.closed_at, '')]);
  } else if (d) {
    line = joinDot([d.status === 'ok' ? 'Decision stored' : sentenceCase(d.status || 'done'), fmtTime(d.created_at, '')]);
  } else if (round && round.status === 'open') {
    line = 'Collecting votes';
    sub = 'The review starts when you close the round.';
  } else {
    line = 'Waiting for a round';
  }
  if (d && d.status !== 'ok' && d.message) sub = String(d.message);

  const limit = num(C.cfg.brief.agent_limits.max_evaluate_calls, 0);
  const evals = countEvaluations(events);
  const rulesTotal = C.cfg.brief.hard_rules.length;
  const failedRules = d ? asArray(d.evidence.failed_rules).length : null;
  const alts = d ? d.alternatives_considered.length : null;

  const stat = (ic, value, label, title) => el('div', { class: 'agent-stat', title },
    el('span', { class: 'row' }, icon(ic, 18), value),
    el('span', { class: 't-label', text: label }));

  mount($('agent'),
    el('div', { class: 'head' }, el('h2', { class: 't-title', text: 'Reviewer agent' }), chip),
    el('div', null,
      el('p', { class: 'status-line', text: line }),
      sub ? el('p', { class: 'status-sub', text: sub }) : null),
    el('div', null,
      el('div', { class: 'progress', role: 'progressbar', 'aria-label': 'Review progress', 'aria-valuemin': 0, 'aria-valuemax': TOTAL_STEPS, 'aria-valuenow': Math.round(progress * TOTAL_STEPS) },
        el('span', { style: { width: `${(progress * 100).toFixed(1)}%` } })),
      el('p', { class: 'step-label', style: { marginTop: '6px' }, text: `Step ${Math.round(progress * TOTAL_STEPS)} of ${TOTAL_STEPS}${maxStep && !(d && d.status === 'ok') ? ' · ' + (STEP_LABELS[maxStep] || '') : ''}` })),
    el('div', { class: 'agent-stats' },
      stat('flask-conical', limit ? `${evals}/${limit}` : fmtInt(evals), 'Evaluations', 'Evaluate calls used'),
      stat('shield-check', failedRules == null ? '–' : `${Math.max(0, rulesTotal - failedRules)}/${rulesTotal}`, 'Hard rules', 'Hard rules the proposal passed'),
      stat('git-compare-arrows', alts == null ? '–' : fmtInt(alts), 'Alternatives', 'Alternatives considered')),
  );
}

function renderBriefNotice() {
  const b = C.cfg.brief;
  const narrative = b.narrative || '';
  const firstMatch = narrative.match(/^[^.!?]*[.!?]/);
  const first = firstMatch ? firstMatch[0] : narrative;
  const summary = joinDot([
    b.goals.length ? plural(b.goals.length, 'goal') : '',
    b.hard_rules.length ? plural(b.hard_rules.length, 'hard rule') : '',
  ]);
  const link = el('a', { class: 'link', href: '#brief' }, 'Read the project brief', icon('chevron-right', 16));
  link.addEventListener('click', (e) => { e.preventDefault(); goToSection('brief'); });
  mount($('brief-notice'),
    el('span', { class: 'notice-icon' }, icon('info', 20)),
    el('p', { text: first }),
    summary ? el('p', { class: 't-secondary', text: `${summary} guide the reviewer. Hard rules always win, then goals, then votes.` }) : null,
    el('p', null, link),
  );
}

function emptyState(ic, title, text) {
  return el('div', { class: 'empty' }, icon(ic, 28), el('p', { class: 't-title', text: title }), text ? el('p', { text }) : null);
}

function altList(d) {
  const alts = d.alternatives_considered;
  if (!alts.length) return null;
  const goals = C.cfg.brief.goals;
  return el('div', { class: 'alts' },
    el('p', { class: 'alts-title', text: 'Alternatives considered' }),
    alts.map((a) => {
      const alt = asObject(a);
      const params = asObject(alt.parameters);
      const metrics = asObject(alt.metrics);
      const values = C.cfg.params.filter((p) => params[p.key] !== undefined).map((p) => fmtParamValue(p, params[p.key]));
      const mline = goals.filter((g) => metrics[g.metric || g.id] !== undefined).map((g) => `${g.label} ${fmtNum(metrics[g.metric || g.id])}`);
      const rules = alt.hard_rules_pass === true ? 'Hard rules pass' : alt.hard_rules_pass === false ? 'Hard rules fail' : '';
      return el('div', { class: 'alt' },
        el('div', { class: 'params', text: joinDot(values) || '–' }),
        mline.length || rules ? el('div', { class: 'metrics', text: joinDot([...mline, rules]) }) : null,
        alt.note ? el('div', { class: 'note', text: String(alt.note) }) : null);
    }));
}

function choicePies(d) {
  const tally = asObject(d.proposal.tally);
  if (!Object.keys(tally).length) return [];
  const blocks = [];
  for (const p of C.cfg.params) {
    if (p.type !== 'choice') continue;
    const t = asObject(tally[p.key]);
    const counts = asObject(t.counts);
    const items = p.options.map((o) => ({ label: o.label, icon: o.icon, count: Math.max(0, num(counts[o.value])) }));
    for (const k of Object.keys(counts)) {
      if (!p.options.some((o) => o.value === k)) items.push({ label: sentenceCase(k), icon: 'circle', count: Math.max(0, num(counts[k])) });
    }
    const total = items.reduce((s, i) => s + i.count, 0);
    const host = el('div', { class: 'pie' });
    renderPie(host, items, { animate: C.animatedDecisionId !== d.id });
    blocks.push(el('div', { class: 'pie-block' },
      el('p', { class: 'pie-title' }, `${p.label} · `, el('span', { class: 't-secondary', style: { fontWeight: '500' }, text: plural(total, 'vote') })),
      host,
      total ? pieLegend(items) : null));
  }
  return blocks;
}

function renderDecision(M) {
  const host = $('decision-card');
  const d = M.decision;
  const round = M.round;
  const sig = [d && d.id, d && d.status, d && d.verdict, d && d.rationale, round && round.id, round && round.status, C.d3Ready, M.st.session ? 1 : 0];
  if (!changed('decision', sig)) return;
  const head = (right) => el('div', { class: 'decision-head' },
    el('h2', { class: 't-title', id: 'decision-title', text: d && d.round_number != null ? `Decision · Round ${d.round_number}` : 'Decision' }), right);
  if (!d) {
    let content;
    if (!round) content = emptyState('scale', M.st.session ? 'No rounds yet' : 'No active session', M.st.session ? 'Open a round to start.' : 'Start a session from the orchestrator.');
    else if (round.status === 'open') content = emptyState('vote', `${roundName(round.number)} is open`, 'The reviewer agent starts when you close the round.');
    else content = emptyState('loader-circle', `Reviewing ${roundName(round.number).toLowerCase()}…`, 'The decision appears here as soon as the agent is done.');
    mount(host, head(null), content);
    return;
  }
  const rows = decisionRows(d, C.cfg.params).filter((r) => r.voted !== undefined || r.applied !== undefined);
  const vd = verdictOf(d);
  const tagText = vd && vd !== 'REJECTED' ? 'Changed' : 'Not applied';
  const table = rows.length ? el('table', { class: 'vt' },
    el('thead', null, el('tr', null, el('th', { text: 'Parameter' }), el('th', { class: 'r', text: 'Voted' }), el('th', { class: 'r', text: 'Applied' }))),
    el('tbody', null, rows.map((r) => el('tr', { class: r.changed ? 'changed' : '' },
      el('td', null, el('span', { style: { fontWeight: '500' }, text: r.param.label }),
        r.changed ? el('span', { class: 'changed-tag', text: tagText }) : null,
        r.changed && r.reason ? el('span', { class: 'reason', text: r.reason }) : null),
      el('td', { class: 'r voted', text: r.votedText }),
      el('td', { class: 'r val', text: r.appliedText }))))) : null;
  const when = joinDot([fmtTime(d.created_at, ''), d.duration_s != null ? fmtDuration(d.duration_s) : '']);
  const pies = choicePies(d);
  const left = el('div', { style: { minWidth: '0' } },
    table,
    !verdictOf(d) ? el('p', { class: 'decision-message', text: d.message ? String(d.message) : 'The design was kept as it was.' }) : null,
    d.rationale ? el('p', { class: 'rationale', text: String(d.rationale) }) : null,
    verdictOf(d) === 'REJECTED' && d.message
      ? el('p', { class: 'consistency' }, el('strong', { text: 'What to vote for instead: ' }), String(d.message)) : null,
    d.consistency_note ? el('p', { class: 'consistency', text: String(d.consistency_note) }) : null,
    altList(d));
  const body = pies.length ? el('div', { class: 'decision-grid' }, left, el('div', { style: { display: 'flex', flexDirection: 'column', gap: '24px', minWidth: '0' } }, pies)) : left;
  const animate = C.animatedDecisionId !== d.id;
  C.animatedDecisionId = d.id;
  mount(host, head(el('div', { style: { display: 'flex', alignItems: 'center', gap: '12px' } },
    when ? el('span', { class: 'when', text: when }) : null, verdictBadge(d, { large: true }))), el('div', { class: animate ? 'anim-slide-up' : '' }, body));
}

function goalState(g, v) {
  const t = toNum(g.threshold);
  if (!Number.isFinite(v) || !Number.isFinite(t)) return null;
  const m = Math.max(0, num(C.cfg.brief.close_tradeoff_margin, 0));
  if (g.direction === 'min') return v >= t ? 'pass' : v >= t - m ? 'marginal' : 'fail';
  return v <= t ? 'pass' : v <= t + m ? 'marginal' : 'fail';
}

function renderMetrics(M) {
  const d = M.decision;
  if (!changed('metrics', [d && d.id, d && d.status])) return;
  const head = el('div', { class: 'card-head' },
    el('h2', { class: 't-title', id: 'metrics-title', text: 'Metrics' }),
    el('span', { class: 'chip chip-neutral', text: 'Indicative' }));
  const host = $('metrics-card');
  if (!d) { mount(host, head, el('p', { class: 't-secondary', text: 'Before and after values appear with the decision.' })); return; }
  const before = asObject(d.metrics.before);
  let after = asObject(d.metrics.after);
  if (!Object.keys(after).length) after = asObject(d.evidence.applied_metrics);
  const goals = C.cfg.brief.goals.filter((g) => after[g.metric || g.id] !== undefined || before[g.metric || g.id] !== undefined);
  if (!goals.length) { mount(host, head, el('p', { class: 't-secondary', text: 'This decision has no metrics.' })); return; }
  const words = { pass: 'Pass', marginal: 'Marginal', fail: 'Fail' };
  mount(host, head, el('div', { class: 'metrics-grid' }, goals.map((g) => {
    const key = g.metric || g.id;
    const a = toNum(after[key]);
    const bf = toNum(before[key]);
    const delta = Number.isFinite(a) && Number.isFinite(bf) ? a - bf : NaN;
    let deltaText = '';
    if (Number.isFinite(delta)) {
      const r = Math.round(delta * 10) / 10;
      const better = g.direction === 'min' ? r > 0 : r < 0;
      deltaText = r === 0 ? 'no change' : `${fmtSigned(delta)} ${better ? 'better' : 'worse'}`;
    }
    const state = goalState(g, a);
    const target = Number.isFinite(toNum(g.threshold)) ? `${g.direction === 'min' ? 'Target ≥' : 'Target ≤'} ${fmtNum(g.threshold)}` : '';
    return el('div', { class: 'metric' },
      el('div', { class: 't-label', text: g.label || sentenceCase(key) }),
      el('div', { class: 'after', text: fmtNum(a) }),
      el('div', { class: 'before', text: joinDot([Number.isFinite(bf) ? `Before ${fmtNum(bf)}` : '', deltaText]) }),
      el('div', { class: 'target' }, target, target && state ? ' · ' : '', state ? el('span', { class: `word-${state}`, text: words[state] }) : null));
  })));
}

function renderParticipation(M) {
  const votes = M.st.votes;
  const simulated = votes.filter((v) => v.is_simulated === true || v.is_simulated === 1 || v.is_simulated === 'true').length;
  const closedCount = M.rounds.filter((r) => r.status === 'closed').length;
  const sig = [M.round && M.round.id, M.roundParticipants, votes.length, simulated, closedCount, M.st.participants_total, M.st.failed_submissions];
  if (!changed('participation', sig)) return;
  const mini = (label, value) => el('div', { class: 'mini-stat' }, el('span', { class: 't-label', text: label }), el('span', { class: 'value', text: value }));
  mount($('participation-card'),
    el('div', { class: 'card-head' }, el('h2', { class: 't-title', id: 'participation-title', text: 'Participation' })),
    el('div', { class: 'stat-big', text: fmtInt(M.roundParticipants, '0') }),
    el('div', { class: 'stat-big-label', text: M.round ? `Participants in ${roundName(M.round.number).toLowerCase()}` : 'Participants' }),
    el('div', { class: 'mini-stats' },
      mini('Votes cast', fmtInt(votes.length, '0')),
      mini('Rounds completed', fmtInt(closedCount, '0')),
      mini('Session total', fmtInt(M.st.participants_total, '0')),
      M.st.failed_submissions != null ? mini('Failed submissions', fmtInt(M.st.failed_submissions)) : null,
      simulated ? mini('Simulated votes', fmtInt(simulated)) : null));
}

function renderTrace(M) {
  const list = $('trace-list');
  const empty = $('trace-empty');
  const round = M.round;
  const rid = round ? round.id : null;
  const events = M.st.agent_events;
  let initial = false;
  if (C.traceRoundId !== rid) {
    clear(list);
    C.traceSeen = new Set();
    C.traceLastSeq = -Infinity;
    C.traceRoundId = rid;
    initial = true;
  }
  $('trace-meta').textContent = round ? joinDot([roundName(round.number), plural(events.length, 'tool call')]) : '';
  if (!events.length) {
    if (list.childElementCount) { clear(list); C.traceSeen = new Set(); C.traceLastSeq = -Infinity; }
    empty.hidden = false;
    empty.textContent = round
      ? (round.status === 'open' ? 'The agent starts when the round closes. Its tool calls appear here, one line each.' : 'Waiting for the first tool call…')
      : 'No round selected.';
    list.hidden = true;
    return;
  }
  empty.hidden = true;
  list.hidden = false;
  const fresh = events.filter((e) => !C.traceSeen.has(e.id != null ? String(e.id) : `${e.seq}|${e.tool}|${e.created_at}`));
  if (!fresh.length) return;
  if (fresh.some((e) => num(e.seq) < C.traceLastSeq)) {
    // Out-of-order arrival: rebuild in order.
    clear(list);
    C.traceSeen = new Set();
    C.traceLastSeq = -Infinity;
    initial = true;
  }
  const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  const animate = !initial && !reducedMotion();
  let k = 0;
  for (const e of events) {
    const id = e.id != null ? String(e.id) : `${e.seq}|${e.tool}|${e.created_at}`;
    if (C.traceSeen.has(id)) continue;
    C.traceSeen.add(id);
    C.traceLastSeq = Math.max(C.traceLastSeq, num(e.seq));
    const li = el('li', { class: animate ? 'new' : '', style: animate ? { animationDelay: `${k * 160}ms` } : null },
      el('span', { class: 'seq', text: e.seq != null ? String(e.seq) : '' }),
      el('span', { class: 'time', text: fmtTimeS(e.created_at) }),
      el('span', { class: 'tool', text: shortTool(e.tool), title: String(e.tool || '') }),
      el('span', { class: 'sum', text: e.summary != null ? String(e.summary) : '' }));
    list.appendChild(li);
    k += 1;
  }
  if (nearBottom || initial) list.scrollTop = list.scrollHeight;
}

function renderHistory(M) {
  const ds = M.st.decisions;
  if (!changed('history', ds.map((d) => [d.id, d.status, d.verdict, d.rationale]))) return;
  $('history-meta').textContent = ds.length ? plural(ds.length, 'decision') : '';
  const host = $('history');
  if (!ds.length) { mount(host, el('div', { class: 'card' }, emptyState('gavel', 'No decisions yet', 'Each closed round adds one here.'))); return; }
  mount(host, ds.slice().reverse().map((d) => {
    const applied = d.applied_parameters;
    const chips = C.cfg.params.filter((p) => applied[p.key] !== undefined)
      .map((p) => el('span', { class: 'pill' }, p.label, el('b', { text: fmtParamValue(p, applied[p.key]) })));
    return el('article', { class: 'card history-item' },
      el('div', null,
        el('div', { class: 'round', text: d.round_number != null ? `Round ${d.round_number}` : 'Round' }),
        el('div', { class: 'when', text: joinDot([fmtTime(d.created_at, ''), d.duration_s != null ? fmtDuration(d.duration_s) : '']) })),
      el('div', { style: { minWidth: '0' } },
        verdictBadge(d),
        chips.length ? el('div', { class: 'chips' }, chips) : null,
        d.rationale ? el('p', { class: 't-lead', text: String(d.rationale) }) : null,
        !verdictOf(d) && d.message ? el('p', { class: 't-secondary', text: String(d.message) }) : null));
  }));
}

function renderBrief() {
  const b = C.cfg.brief;
  const pct = (v) => (Number.isFinite(toNum(v)) ? fmtPct(toNum(v) * 100) : null);
  const goals = el('section', { class: 'card' },
    el('div', { class: 'card-head' }, el('h3', { class: 't-title', text: 'Goals' }), el('span', { class: 't-label', text: 'Thresholds' })),
    b.goals.length ? el('ul', { class: 'rule-list' }, b.goals.map((g) => el('li', null,
      el('div', { class: 'rl-head' }, el('span', { text: String(g.label || g.id || '') }),
        el('span', { text: Number.isFinite(toNum(g.threshold)) ? `${g.direction === 'min' ? '≥' : '≤'} ${fmtNum(g.threshold)}${g.unit && g.unit !== 'index' ? ' ' + g.unit : ''}` : '' })),
      g.plain ? el('div', { class: 'rl-text', text: String(g.plain) }) : null))) : el('p', { class: 't-secondary', text: 'No goals in the brief.' }));
  const rules = el('section', { class: 'card' },
    el('div', { class: 'card-head' }, el('h3', { class: 't-title', text: 'Hard rules' }), el('span', { class: 't-label', text: 'Never overridable' })),
    b.hard_rules.length ? el('ul', { class: 'rule-list' }, b.hard_rules.map((r) => el('li', null,
      el('div', { class: 'rl-head' }, el('span', { text: String(r.label || r.id || '') })),
      r.plain ? el('div', { class: 'rl-text', text: String(r.plain) }) : null))) : el('p', { class: 't-secondary', text: 'No hard rules in the brief.' }));
  const c = b.consensus;
  const ml = b.modification_limits;
  const al = b.agent_limits;
  const items = [
    pct(c.strong) ? ['Strong consensus', `≥ ${pct(c.strong)}`, 'Kept unless a hard rule forces a change.'] : null,
    pct(c.weak) ? ['Weak consensus', `< ${pct(c.weak)}`, 'The agent may compromise between the leading options.'] : null,
    pct(c.leading_option_gap) ? ['Leading options', `within ${pct(c.leading_option_gap)}`, 'Options this close to the winner count as leading.'] : null,
    Number.isFinite(toNum(b.close_tradeoff_margin)) && b.close_tradeoff_margin > 0 ? ['Close trade-off', `${fmtNum(b.close_tradeoff_margin)} points`, b.close_tradeoff_note || 'Close calls go to the participants.'] : null,
    Number.isFinite(toNum(ml.max_changed_parameters)) ? ['Changes per modification', `≤ ${fmtNum(ml.max_changed_parameters)} parameters`, Number.isFinite(toNum(ml.max_slider_steps)) ? `Sliders move at most ${fmtNum(ml.max_slider_steps)} steps.` : ''] : null,
    Number.isFinite(toNum(al.max_evaluate_calls)) ? ['Evaluate calls', `≤ ${fmtNum(al.max_evaluate_calls)} per round`, al.note || ''] : null,
  ].filter(Boolean);
  const consensus = el('section', { class: 'card' },
    el('div', { class: 'card-head' }, el('h3', { class: 't-title', text: 'Consensus and judgment' })),
    el('p', { class: 't-secondary', style: { marginBottom: '12px' }, text: 'Decision hierarchy, in strict order: hard rules, then the brief’s goals, then participant preference.' }),
    el('ul', { class: 'rule-list' }, items.map(([label, value, text]) => el('li', null,
      el('div', { class: 'rl-head' }, el('span', { text: label }), el('span', { text: value })),
      text ? el('div', { class: 'rl-text', text }) : null))),
    c.note ? el('p', { class: 't-secondary t-small', style: { marginTop: '12px' }, text: String(c.note) }) : null);
  const narrative = el('section', { class: 'card' },
    el('div', { class: 'card-head' }, el('h3', { class: 't-title', text: b.title || 'Project brief' }), b.climate ? el('span', { class: 'pill', text: sentenceCase(b.climate) }) : null),
    b.narrative ? el('p', { class: 'brief-narr', text: b.narrative }) : null,
    b.metrics_note ? el('p', { class: 't-secondary', style: { marginTop: '12px' }, text: b.metrics_note }) : null);
  mount($('brief-body'), el('div', { style: { display: 'flex', flexDirection: 'column', gap: 'var(--gap-card)' } },
    narrative, el('div', { class: 'brief-grid' }, goals, rules), consensus));
}

/* ================================================================== tick */
function tick() {
  if (!C.state) return;
  const M = model();
  const timer = $('timer');
  if (M.openRound) {
    const opened = parseTime(M.openRound.opened_at);
    if (Number.isFinite(opened)) {
      const left = C.cfg.profile.round_duration_s - (now() - opened) / 1000;
      const text = left >= 0 ? `${fmtClock(Math.ceil(left))} left` : `${fmtClock(Math.floor(-left))} over`;
      if (timer.textContent !== text) timer.textContent = text;
      timer.classList.toggle('over', left < 0);
      timer.hidden = false;
    } else {
      timer.hidden = true;
    }
  } else {
    timer.hidden = true;
  }
  if (M.round && M.round.status === 'open') renderHeatmap($('heatmap'), heatmapModel(M));
}

/* ================================================================== actions */
function errorText(e) {
  const code = e && e.code;
  if (code === 'round_already_open') return 'A round is already open.';
  if (code === 'no_open_round') return 'No round is open.';
  if (code === 'no_session') return 'No active session. Start one from the orchestrator.';
  if (code === 'unauthorized' || code === 'forbidden') return 'Your facilitator access was not accepted.';
  if (isRetryable(e)) return 'Could not reach the server. Try again.';
  return `Something went wrong (${code || 'error'}).`;
}

async function onRoundButton() {
  if (C.busy || !C.state) return;
  const M = model();
  if (!M.st.session) return;
  C.busy = true;
  renderToolbar(model());
  try {
    if (M.openRound) {
      const res = await C.backend.closeRound();
      const n = res && res.round && res.round.number != null ? res.round.number : M.openRound.number;
      toast(`${roundName(n)} closed · the reviewer agent takes over`);
    } else {
      const res = await C.backend.openRound();
      C.selectedRoundId = null;
      const n = res && res.round && res.round.number != null ? res.round.number : '';
      toast(`${roundName(n)} is open`);
    }
  } catch (e) {
    toast(errorText(e));
    if (e && (e.code === 'unauthorized' || e.code === 'forbidden')) authLost('Please sign in again.');
  } finally {
    C.busy = false;
    try { await refresh(); } catch (_) { render(); }
  }
}

function setProjection(on, persist = true) {
  C.projection = !!on;
  const root = document.documentElement;
  root.style.setProperty('--scale', on ? String(C.cfg.profile.projection_scale) : '1');
  root.classList.toggle('projection', C.projection);
  if (persist) store.set(PROJECTION_KEY, C.projection ? '1' : '0');
  $('side-projection').setAttribute('aria-checked', String(C.projection));
  $('tb-projection').setAttribute('aria-pressed', String(C.projection));
}

function goToSection(id) {
  const sec = $(id);
  if (!sec) return;
  sec.scrollIntoView({ behavior: reducedMotion() ? 'auto' : 'smooth', block: 'start' });
  setActiveNav(id);
}

function setActiveNav(id) {
  document.querySelectorAll('.nav-item').forEach((a) => {
    if (a.dataset.section === id) a.setAttribute('aria-current', 'true');
    else a.removeAttribute('aria-current');
  });
}

let spyQueued = false;
function onScrollSpy() {
  if (spyQueued) return;
  spyQueued = true;
  requestAnimationFrame(() => {
    spyQueued = false;
    const ids = ['live', 'trace', 'decisions', 'brief'];
    const offset = 120 * (C.projection ? C.cfg.profile.projection_scale : 1);
    let active = ids[0];
    for (const id of ids) {
      const sec = $(id);
      if (sec && sec.getBoundingClientRect().top <= offset) active = id;
    }
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) active = ids[ids.length - 1];
    setActiveNav(active);
  });
}

function initShell() {
  if (C.shellReady) return;
  C.shellReady = true;
  mount($('top-brand'), wordmark());
  renderBriefNotice();
  renderBrief();

  $('seg-rounds').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-round-id]');
    if (!btn || !C.state) return;
    const id = btn.dataset.roundId;
    const newest = C.state.rounds[C.state.rounds.length - 1];
    C.selectedRoundId = newest && newest.id === id ? null : id;
    // Show the selection immediately, then fetch that round's data.
    const r = C.state.rounds.find((x) => x.id === id);
    if (r && (!C.state.round || C.state.round.id !== id)) {
      C.state = { ...C.state, round: r, votes: [], agent_events: [] };
    }
    render();
    refreshSoon();
  });
  $('seg-questions').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-q]');
    if (!btn) return;
    C.selectedQuestion = btn.dataset.q;
    render();
  });
  $('round-btn').addEventListener('click', onRoundButton);
  $('tb-projection').addEventListener('click', () => setProjection(!C.projection));
  $('side-projection').addEventListener('click', () => setProjection(!C.projection));
  $('tb-refresh').addEventListener('click', () => {
    const btn = $('tb-refresh');
    const svg = btn.querySelector('svg, i');
    if (svg && !reducedMotion()) { svg.classList.add('spin'); setTimeout(() => svg.classList.remove('spin'), 700); }
    refreshSoon();
  });
  $('tb-trace').addEventListener('click', () => goToSection('trace'));
  document.querySelectorAll('.nav-item').forEach((a) => {
    a.addEventListener('click', (e) => { e.preventDefault(); goToSection(a.dataset.section); });
  });
  window.addEventListener('scroll', onScrollSpy, { passive: true });

  const avatar = $('avatar');
  const menu = $('menu');
  const closeMenu = () => { menu.hidden = true; avatar.setAttribute('aria-expanded', 'false'); };
  avatar.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = menu.hidden;
    menu.hidden = !open;
    avatar.setAttribute('aria-expanded', String(open));
    if (open) $('sign-out').focus();
  });
  document.addEventListener('click', (e) => { if (!menu.hidden && !menu.contains(e.target)) closeMenu(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !menu.hidden) { closeMenu(); avatar.focus(); } });
  $('sign-out').addEventListener('click', async () => {
    closeMenu();
    stopLive();
    await C.backend.signOut().catch(() => {});
    showLogin('You are signed out.');
  });

  window.addEventListener('resize', () => { C.sigs.segRounds = null; });
  refreshIcons(document);
}

/* ================================================================== auth */
function stopLive() {
  if (C.unsub) { try { C.unsub(); } catch (_) { /* ignore */ } }
  C.unsub = null;
  if (C.ticker) { clearInterval(C.ticker); C.ticker = null; }
}

function authLost(message) {
  if ($('app').hidden) return;
  stopLive();
  showLogin(message || 'Please sign in again.');
}

function startApp(user) {
  C.user = user || { label: 'Facilitator', initial: 'F' };
  $('boot').hidden = true;
  $('login').hidden = true;
  $('app').hidden = false;
  initShell();
  $('avatar').textContent = (C.user.initial || 'F').slice(0, 2);
  $('menu-who').textContent = C.user.label ? `Signed in · ${C.user.label}` : 'Signed in';
  setConn('reconnecting');
  stopLive();
  C.sigs = Object.create(null);
  C.unsub = C.backend.subscribe(refresh, setConn);
  C.ticker = setInterval(tick, 500);
}

function showLogin(message) {
  $('boot').hidden = true;
  $('app').hidden = true;
  $('login').hidden = false;
  const fields = $('login-fields');
  const kind = C.backend ? C.backend.authKind : 'key';
  const field = (id, label, attrs) => el('div', { class: 'field' }, el('label', { for: id, text: label }), el('input', { id, class: 'input', ...attrs }));
  if (kind === 'password') {
    mount(fields,
      field('f-email', 'Email', { type: 'email', name: 'email', autocomplete: 'username', inputmode: 'email', required: true, autocapitalize: 'none', spellcheck: 'false' }),
      el('div', { style: { height: '12px' } }),
      field('f-password', 'Password', { type: 'password', name: 'password', autocomplete: 'current-password', required: true }));
    $('login-help').textContent = 'Only accounts on the facilitator list can sign in.';
  } else {
    const stored = C.backend && typeof C.backend.storedKey === 'function' ? C.backend.storedKey() : '';
    mount(fields, field('f-key', 'Facilitator key', {
      type: 'password', name: 'key', autocomplete: 'off', required: true, autocapitalize: 'none', spellcheck: 'false', value: stored || '',
    }));
    $('login-help').textContent = 'The key is printed in the terminal when the session starts (orchestrator.py run). Opening the console link from that terminal signs you in directly.';
  }
  $('login-error').textContent = message || '';
  const first = fields.querySelector('input');
  if (first) setTimeout(() => first.focus(), 30);
}

async function onLoginSubmit(e) {
  e.preventDefault();
  if (!C.backend) return;
  const btn = $('login-btn');
  const err = $('login-error');
  err.textContent = '';
  const creds = C.backend.authKind === 'password'
    ? { email: ($('f-email') || {}).value || '', password: ($('f-password') || {}).value || '' }
    : { key: ($('f-key') || {}).value || '' };
  btn.disabled = true;
  mount(btn, icon('loader-circle', 18, 'spin'), 'Signing in…');
  try {
    const user = await C.backend.signIn(creds);
    startApp(user);
  } catch (ex) {
    const code = ex && ex.code;
    if (code === 'not_facilitator') err.textContent = 'This account is not a facilitator';
    else if (code === 'invalid_credentials') err.textContent = ex.message;
    else if (code === 'not_configured') err.textContent = 'Supabase is not configured in config.js yet.';
    else if (isRetryable(ex)) err.textContent = 'Could not reach the server. Check the connection and try again.';
    else err.textContent = `Sign-in failed (${code || 'error'}).`;
  } finally {
    btn.disabled = false;
    mount(btn, 'Sign in');
  }
}

/* ================================================================== boot */
async function boot() {
  mount($('login-brand'), wordmark());
  loadIcons();
  loadD3().then((ok) => {
    C.d3Ready = ok;
    if (ok && C.state) { C.sigs.decision = null; render(); }
  });
  $('login-form').addEventListener('submit', onLoginSubmit);

  let attempt = 0;
  while (!C.cfg) {
    try {
      const cfg = window.PLURARCH_CONFIG || {};
      C.cfg = await loadAppConfig(cfg.useCase);
    } catch (e) {
      $('boot').textContent = 'Could not load the configuration. Retrying…';
      await sleep(Math.min(1000 * 2 ** attempt, 10000));
      attempt += 1;
    }
  }
  C.backend = createBackend('console');
  C.backend.onAuthLost(() => authLost('You were signed out.'));
  setProjection(store.get(PROJECTION_KEY) === '1', false);

  if (C.backend.kind === 'supabase' && !C.backend.configured()) {
    showLogin('Supabase is not configured in config.js yet.');
    return;
  }
  try {
    const user = await C.backend.restore();
    if (user) { startApp(user); return; }
    showLogin('');
  } catch (e) {
    showLogin(isRetryable(e) ? 'Could not reach the server yet. Check the connection and sign in.' : '');
  }
}

boot().catch((e) => {
  console.error(e);
  const bootEl = $('boot');
  bootEl.hidden = false;
  bootEl.textContent = 'The console could not start. Please reload the page.';
});
