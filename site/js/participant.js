// Plurarch participant app: anonymous, mobile-first. Polls the tiny status endpoint with jitter,
// submits all answers in one call, and shows the decision. No realtime connection is ever opened here.
//
// Two layouts share one state and one draft:
//   - 3D (default, made for a 5-minute live demo): the building (Langford A) fills the whole screen
//     (js/viewer3d.js, three.js loaded lazily) and the page never scrolls. On top: a small wordmark,
//     ONE status pill (live / "37 voting · 0:42" / voted / AI reviewing / the verdict chip) and (i);
//     at the bottom: Orbit/Tour and one guided button ("Next question · 3/4", then "Submit vote").
//     Every question is a pin on its element; a pin opens a bottom sheet with the stepper's controls
//     and the model rebuilds live from the draft. Moments: a "Vote now" splash when a round opens, a
//     check pop on submit, and a full-screen decision reveal that collapses into the verdict chip.
//     A one-paragraph "About", the question list and "List view" live in the (i) sheet.
//   - List view: the original page with the one-question-per-screen stepper and the design drawing.
//     It is also the fallback whenever WebGL2 or the CDN is unavailable.
// URL flags for testing: ?v3d=0 forces the fallback, ?q=0|1|2 pins the 3D quality tier, ?debug=1.

import { createBackend, isRetryable } from './backend.js';
import {
  el, mount, icon, loadIcons, loadAppConfig, store, sessionStore, randomId, sleep, asObject,
  verdictBadge, verdictOf, decisionRows, appliedValue, snapToStep, wordmark, reducedMotion, toast,
} from './ui.js';
import { fmtParamValue, fmtSliderNumber, fmtTime, fmtClock, parseTime, joinDot, fmtNum, toNum } from './format.js';

const URLQ = new URLSearchParams(window.location.search);
const VIEW_KEY = 'plurarch:view';

const S = {
  cfg: null,
  backend: null,
  pid: null,
  status: undefined, // undefined = not loaded yet; null = no active session
  decision: null,
  decisionId: null,
  draft: null, // { roundId, roundNumber, step, answers }
  closedNotice: null, // { roundId, number } when a draft was dropped because the round closed
  pollProblem: false,
  lastPollAt: 0, // last successful status check
  polledOnce: false,
  revealId: null, // decision id whose reveal should play (new since the page opened)
  submitProblem: false,
  submitting: false,
  submitError: '',
  hint: '',
  viewKey: '',
  lastView: '',
  curView: '',
  configError: '',
  v3d: {
    status: 'idle', // idle | loading | ready | failed
    viewer: null,
    mod: null, // the model module (js/model/langford.js: buildLangford, QUESTION_TAGS)
    pref: sessionStore.get(VIEW_KEY) === 'list' ? 'list' : '3d',
    mode: 'orbit', // orbit | tour
    sheetKey: null, // question open in the sheet
    highlight: null, // decision screen: highlighted question
    modelKey: '',
    pinsKey: '',
    stageView: '',
    cache: new Map(), // params JSON -> Model (a few, so toggles are instant)
  },
};

const dom = {
  view: document.getElementById('view'),
  info: document.getElementById('info'),
  main: document.getElementById('main'),
  bar: document.getElementById('bar'),
  barBtn: document.getElementById('bar-btn'),
  roundPill: document.getElementById('round-pill'),
  header: document.querySelector('.p-header'),
  infoBtn: document.getElementById('info-btn'),
  statusPill: document.getElementById('status-pill'),
  netPill: document.getElementById('net-pill'),
  announce: document.getElementById('announce'),
  brand: document.getElementById('brand'),
};

const DRAFT_KEY = 'plurarch:draft';
const PID_KEY = 'plurarch:participant_id';
const DESIGN_IMG = 'img/langford-a.jpg'; // a render of the 3D model (the initial design)

/* ------------------------------------------------------------------ helpers */
function participantId() {
  let id = store.get(PID_KEY);
  if (!id || !/^[A-Za-z0-9_-]{8,64}$/.test(id)) {
    id = 'p-' + randomId(12);
    store.set(PID_KEY, id);
  }
  return id;
}

function currentRound() {
  const st = S.status;
  if (!st || !st.round_id) return null;
  return {
    id: String(st.round_id),
    number: st.round_number != null ? st.round_number : null,
    status: st.round_status === 'open' ? 'open' : 'closed',
  };
}

function roundName(n) { return n != null && n !== '' ? `Round ${n}` : 'This round'; }

function hasVoted(roundId) { return store.get('voted:' + roundId) != null; }

function votedAnswers(roundId) {
  const raw = store.get('voted:' + roundId);
  if (!raw) return null;
  try { return asObject(JSON.parse(raw).answers); } catch (_) { return null; }
}

function markVoted(roundId, answers) {
  store.set('voted:' + roundId, JSON.stringify({ at: Date.now(), answers }));
}

function saveDraft() {
  if (S.draft) sessionStore.set(DRAFT_KEY, JSON.stringify(S.draft));
}

function dropDraft() {
  S.draft = null;
  sessionStore.remove(DRAFT_KEY);
}

function ensureDraft(round) {
  if (S.draft && S.draft.roundId === round.id) return S.draft;
  let d = null;
  try {
    const raw = sessionStore.get(DRAFT_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    if (parsed && parsed.roundId === round.id) d = parsed;
  } catch (_) { d = null; }
  if (!d) {
    d = { roundId: round.id, roundNumber: round.number, step: 0, answers: {} };
  }
  d.answers = asObject(d.answers);
  d.step = Math.max(0, Math.min(S.cfg.params.length - 1, Number(d.step) || 0));
  // Sliders start at the value now in the model and count as set; choices must be picked.
  for (const p of S.cfg.params) {
    if (p.type === 'slider' && (d.answers[p.key] == null || d.answers[p.key] === '')) d.answers[p.key] = appliedValue(S.decision, p);
  }
  S.draft = d;
  saveDraft();
  return d;
}

function announce(text) {
  dom.announce.textContent = '';
  setTimeout(() => { dom.announce.textContent = text; }, 50);
}

function designTile(caption) {
  return el('figure', { class: 'design-tile', style: { margin: '0' } },
    el('img', {
      src: DESIGN_IMG, width: 1064, height: 524, decoding: 'async',
      alt: 'Langford Architecture Center, Building A, from the south-east quad: a long concrete building with a glazed south-east façade and a row of roof lanterns.',
    }),
    el('figcaption', { class: 'design-caption', text: caption || 'Langford A · initial design' }),
  );
}

/* ------------------------------------------------------------------ polling */
let pollTimer = null;
let polling = false;

function nextDelay() {
  const p = S.cfg.profile;
  const jitter = (Math.random() * 2 - 1) * p.poll_jitter_s;
  return Math.max(1, p.poll_interval_s + jitter) * 1000;
}

function schedulePoll(ms) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(poll, Math.max(0, ms));
}

async function poll() {
  pollTimer = null;
  if (document.hidden) return; // resumes on visibilitychange
  if (polling) return;
  polling = true;
  let ok = true;
  try {
    const st = await S.backend.getStatus();
    applyStatus(st);
    const did = S.status && S.status.latest_decision_id ? String(S.status.latest_decision_id) : null;
    if (!did) {
      S.decision = null;
      S.decisionId = null;
    } else if (did !== S.decisionId) {
      try {
        const d = await S.backend.getDecision(did);
        if (d && typeof d === 'object') {
          S.decision = d;
          S.decisionId = did;
          if (S.polledOnce) S.revealId = did; // arrived while the page was open: play the reveal
        }
      } catch (e) {
        if (isRetryable(e)) ok = false; // try again on the next poll
      }
    }
  } catch (e) {
    ok = false;
  } finally {
    polling = false;
  }
  S.pollProblem = !ok;
  if (ok) { S.lastPollAt = Date.now(); S.polledOnce = true; }
  render();
  if (!document.hidden) schedulePoll(ok ? nextDelay() : Math.min(nextDelay() * 1.5, 12000));
}

function applyStatus(st) {
  S.status = st && typeof st === 'object' ? st : null;
  const round = currentRound();
  if (S.draft) {
    const stillOpen = round && round.id === S.draft.roundId && round.status === 'open';
    if (!stillOpen) {
      if (!hasVoted(S.draft.roundId)) S.closedNotice = { roundId: S.draft.roundId, number: S.draft.roundNumber };
      dropDraft();
      S.hint = '';
      S.submitError = '';
    }
  }
  if (S.closedNotice && round && round.status === 'open' && round.id !== S.closedNotice.roundId) S.closedNotice = null;
  if (!round) S.closedNotice = null;
}

function onVisibility() {
  if (document.hidden) {
    clearTimeout(pollTimer);
    pollTimer = null;
  } else {
    schedulePoll(150 + Math.random() * 700);
  }
}

/* ------------------------------------------------------------------ submit */
let submitToken = 0;

async function submit() {
  const round = currentRound();
  if (S.submitting || !S.draft || !round || round.id !== S.draft.roundId) return;
  if (round.status !== 'open') { applyStatus(S.status); render(); return; }
  const params = S.cfg.params;
  const missing = params.findIndex((p) => S.draft.answers[p.key] == null || S.draft.answers[p.key] === '');
  if (missing >= 0) {
    S.draft.step = missing;
    S.hint = 'Pick one option to continue.';
    saveDraft();
    render();
    const hint = document.getElementById('q-hint');
    if (hint) { hint.textContent = S.hint; hint.classList.add('alert'); }
    return;
  }
  const answers = { ...S.draft.answers };
  const votes = params.map((p) => ({ question_key: p.key, value: String(answers[p.key]) }));
  const token = ++submitToken;
  S.submitting = true;
  S.submitError = '';
  S.hint = '';
  render();
  let attempt = 0;
  for (;;) {
    try {
      await S.backend.submitVotes(round.id, S.pid, votes); // duplicates count as success
      if (token !== submitToken) return;
      markVoted(round.id, answers);
      dropDraft();
      if (S.closedNotice && S.closedNotice.roundId === round.id) S.closedNotice = null;
      S.submitting = false;
      S.submitProblem = false;
      render();
      announce('Vote recorded');
      window.scrollTo(0, 0);
      return;
    } catch (e) {
      if (token !== submitToken) return;
      if (e && e.code === 'round_closed') {
        S.closedNotice = { roundId: round.id, number: round.number };
        dropDraft();
        S.submitting = false;
        S.submitProblem = false;
        render();
        announce('This round has closed');
        return;
      }
      if (!isRetryable(e)) {
        S.submitting = false;
        S.submitProblem = false;
        S.submitError = 'One of your answers was not accepted. Please check your answers and submit again.';
        render();
        return;
      }
      // Network trouble: keep the draft and retry with backoff while the round is still open.
      S.submitProblem = true;
      renderPills();
      const delay = Math.min(1000 * 2 ** attempt, 10000);
      attempt += 1;
      await sleep(delay);
      if (token !== submitToken) return;
      const cur = currentRound();
      if (!S.draft || !cur || cur.id !== round.id || cur.status !== 'open') {
        S.submitting = false;
        S.submitProblem = false;
        if (cur && cur.id === round.id && cur.status !== 'open') {
          S.closedNotice = { roundId: round.id, number: round.number };
          dropDraft();
        }
        render();
        return;
      }
    }
  }
}

/* ------------------------------------------------------------------ stepper */
function currentParam() {
  const params = S.cfg.params;
  return params[Math.min(S.draft.step, params.length - 1)];
}

function isAnswered(p) {
  const v = S.draft && S.draft.answers[p.key];
  return v != null && v !== '';
}

function goNext() {
  if (!S.draft || S.submitting) return;
  if (S.curView === 'questions' && questionsMode() === '3d') { submit3d(); return; }
  const p = currentParam();
  if (p.type === 'choice' && !isAnswered(p)) {
    S.hint = 'Pick one option to continue.';
    const hint = document.getElementById('q-hint');
    if (hint) { hint.textContent = S.hint; hint.classList.add('alert'); }
    const first = dom.view.querySelector('.tile-input');
    if (first) first.focus();
    return;
  }
  if (S.draft.step < S.cfg.params.length - 1) {
    S.draft.step += 1;
    S.hint = '';
    saveDraft();
    render();
    window.scrollTo(0, 0);
    focusTitle();
  } else {
    submit();
  }
}

function goBack() {
  if (!S.draft || S.draft.step <= 0 || S.submitting) return;
  S.draft.step -= 1;
  S.hint = '';
  saveDraft();
  render();
  window.scrollTo(0, 0);
  focusTitle();
}

function focusTitle() {
  const t = document.getElementById('q-title');
  if (t) { try { t.focus({ preventScroll: true }); } catch (_) { t.focus(); } }
}

function updateBar() {
  if (!S.draft) return;
  const p = currentParam();
  const last = S.draft.step >= S.cfg.params.length - 1;
  const btn = dom.barBtn;
  btn.textContent = '';
  if (S.submitting) {
    btn.append(icon('loader-circle', 20, 'spin'), 'Sending…');
    btn.disabled = true;
    btn.removeAttribute('aria-disabled');
  } else {
    btn.disabled = false;
    btn.append(last ? 'Submit vote' : 'Next');
    if (p.type === 'choice' && !isAnswered(p)) btn.setAttribute('aria-disabled', 'true');
    else btn.removeAttribute('aria-disabled');
  }
}

function stepDots(i, n) {
  const dots = el('span', { class: 'dots', 'aria-hidden': 'true' });
  for (let k = 0; k < n; k++) dots.appendChild(el('i', { class: k === i ? 'on' : k < i ? 'done' : '' }));
  return el('div', { class: 'step-dots' }, dots, el('span', { text: `${i + 1} of ${n}` }));
}

// Question controls, shared by the stepper and the 3D view's sheet.
// opts: { titleId, explId, hintId, idPrefix, onChange } (defaults = the stepper's ids).
function choiceControl(p, opts = {}) {
  const d = S.draft;
  const prefix = opts.idPrefix || 'opt';
  const group = el('div', { class: 'tiles', role: 'radiogroup', 'aria-labelledby': opts.titleId || 'q-title' });
  p.options.forEach((o, idx) => {
    const id = `${prefix}-${p.key}-${idx}`.replace(/[^A-Za-z0-9_-]/g, '_');
    const input = el('input', { type: 'radio', class: 'tile-input', name: `${prefix}-q-${p.key}`, id, value: o.value, checked: d.answers[p.key] === o.value });
    input.addEventListener('change', () => {
      if (!input.checked) return;
      d.answers[p.key] = o.value;
      S.hint = '';
      const hint = document.getElementById(opts.hintId || 'q-hint');
      if (hint) { hint.textContent = ''; hint.classList.remove('alert'); }
      saveDraft();
      if (opts.onChange) opts.onChange(); else updateBar();
    });
    group.appendChild(el('label', { class: 'tile', for: id },
      input,
      el('span', { class: 'tile-body' },
        el('span', { class: 'tile-icon' }, icon(o.icon, 24)),
        el('span', { class: 'tile-label', text: o.label }),
        el('span', { class: 'tile-check' }, icon('check', 24)),
      ),
    ));
  });
  return group;
}

function sliderControl(p, opts = {}) {
  const d = S.draft;
  let v = toNum(d.answers[p.key]);
  if (!Number.isFinite(v)) v = appliedValue(S.decision, p);
  const num = el('span', { class: 'v' });
  const display = el('div', { class: 'slider-value', 'aria-hidden': 'true' }, num, p.unit ? el('span', { class: 'u', text: p.unit }) : null);
  const range = el('input', {
    type: 'range', class: 'range', min: p.min, max: p.max, step: p.step, value: v,
    'aria-labelledby': opts.titleId || 'q-title', 'aria-describedby': p.explainer ? (opts.explId || 'q-expl') : null,
  });
  const minus = el('button', { class: 'round-btn', type: 'button', 'aria-label': `Less (${p.label})` }, icon('minus', 20));
  const plus = el('button', { class: 'round-btn', type: 'button', 'aria-label': `More (${p.label})` }, icon('plus', 20));
  const span = p.max - p.min || 1;
  let last = null;
  const setValue = (raw, silent) => {
    const nv = snapToStep(p, Number(raw));
    d.answers[p.key] = nv;
    if (range.value !== String(nv)) range.value = String(nv);
    num.textContent = fmtSliderNumber(p, nv);
    range.style.setProperty('--p', String((nv - p.min) / span));
    range.setAttribute('aria-valuetext', fmtParamValue(p, nv));
    minus.disabled = nv <= p.min;
    plus.disabled = nv >= p.max;
    saveDraft();
    if (!silent && nv !== last && opts.onChange) opts.onChange();
    last = nv;
  };
  range.addEventListener('input', () => setValue(range.value));
  range.addEventListener('change', () => setValue(range.value));
  minus.addEventListener('click', () => setValue(toNum(d.answers[p.key]) - p.step));
  plus.addEventListener('click', () => setValue(toNum(d.answers[p.key]) + p.step));
  setValue(v, true);
  const now = appliedValue(S.decision, p);
  return el('div', null,
    display,
    el('div', { class: 'slider-row' }, minus, range, plus),
    el('div', { class: 'slider-scale', 'aria-hidden': 'true' },
      el('span', { text: fmtParamValue(p, p.min) }), el('span', { text: fmtParamValue(p, p.max) })),
    el('p', { class: 'slider-current', text: `Now in the model: ${fmtParamValue(p, now)}` }),
  );
}

/* ------------------------------------------------------------------ views: list layout */
// The classic page: used for "List view" and whenever 3D is unavailable (then without the switch).
function viewLoading() {
  return el('div', { class: 'p-hero' },
    el('h1', { class: 'p-state-title', text: 'Connecting…' }),
    el('p', { class: 'p-state-text', text: 'Getting the session ready. This page keeps trying on its own.' }),
  );
}

function viewConfigError() {
  return el('div', { class: 'p-hero' },
    el('h1', { class: 'p-state-title', text: 'Not set up yet' }),
    el('p', { class: 'p-state-text', text: S.configError }),
  );
}

function closedNoticeCard(round) {
  if (!S.closedNotice) return null;
  if (round && S.closedNotice.roundId !== round.id) return null;
  return el('div', { class: 'p-card anim-rise' },
    el('div', { class: 'notice' },
      el('span', { class: 'notice-icon' }, icon('info', 20)),
      el('p', { class: 'notice-title t-title', text: 'This round has closed' }),
      el('p', { class: 't-secondary', text: 'Your answers were not sent before voting closed. You can vote in the next round.' }),
    ),
  );
}

// "3D model" switch shown at the top of the list layout while 3D can still be used.
function view3dSwitch() {
  if (S.v3d.status === 'failed' || URLQ.get('v3d') === '0') return null;
  return el('div', { class: 'view-switch' },
    el('button', { class: 'text-btn', type: 'button', onclick: () => setLayoutPref('3d') }, icon('box', 18), '3D model'));
}

function viewWaiting() {
  const noSession = !S.status;
  return el('div', { class: 'p-stack' },
    view3dSwitch(),
    el('div', { class: 'p-hero anim-rise' },
      el('h1', { class: 'p-state-title', text: noSession ? 'Waiting for the session to start' : 'Waiting for the next round' }),
      el('p', { class: 'p-state-text', text: 'Keep this page open. The questions appear here as soon as voting opens.' }),
    ),
    designTile(),
  );
}

function viewQuestions(round) {
  const d = ensureDraft(round);
  const params = S.cfg.params;
  const i = Math.min(d.step, params.length - 1);
  const p = params[i];
  const back = i > 0
    ? el('button', { class: 'text-btn', type: 'button', onclick: goBack }, icon('arrow-left', 18), 'Back')
    : el('span', { 'aria-hidden': 'true' });
  const hintText = S.submitError || S.hint || '';
  return el('div', { class: 'q-screen anim-rise' },
    view3dSwitch(),
    el('div', { class: 'q-top' }, back, stepDots(i, params.length)),
    el('h1', { class: 'q-title', id: 'q-title', tabindex: '-1', text: p.question }),
    p.explainer ? el('p', { class: 'q-explainer', id: 'q-expl', text: p.explainer }) : null,
    p.type === 'choice' ? choiceControl(p) : sliderControl(p),
    el('p', { class: 'q-hint' + (hintText ? ' alert' : ''), id: 'q-hint', role: 'status', text: hintText }),
  );
}

function viewVoted(round) {
  const answers = votedAnswers(round.id) || {};
  const rows = S.cfg.params
    .filter((p) => answers[p.key] != null)
    .map((p) => el('li', null, el('span', { text: p.label }), el('b', { text: fmtParamValue(p, answers[p.key]) })));
  return el('div', { class: 'p-stack' },
    view3dSwitch(),
    el('div', { class: 'p-card anim-slide-up' },
      el('div', { class: 'p-icon-circle' }, icon('check', 24)),
      el('h1', { class: 'p-state-title', text: 'Vote recorded' }),
      el('p', { class: 'p-state-text', text: `${roundName(round.number)} · When the round closes, the reviewer agent judges the room's proposal against the brief.` }),
      rows.length ? el('ul', { class: 'answers', 'aria-label': 'Your answers' }, rows) : null,
    ),
    designTile(),
  );
}

function viewReviewing(round) {
  const voted = hasVoted(round.id);
  return el('div', { class: 'p-stack' },
    view3dSwitch(),
    closedNoticeCard(round),
    el('div', { class: 'p-decision anim-slide-up' },
      el('div', { class: 'p-icon-circle soft' }, icon('loader-circle', 24, 'spin')),
      el('h1', { class: 'p-state-title', text: voted ? 'The reviewer agent is reviewing your proposal…' : "The reviewer agent is reviewing the room's proposal…" }),
      el('p', { class: 'p-state-text', text: `${roundName(round.number)} is closed. The AI reviewer is checking the room's choice.` }),
    ),
    designTile(),
  );
}

function decisionCard(d) {
  const v = verdictOf(d);
  const params = S.cfg.params;
  const rows = decisionRows(d, params).filter((r) => r.voted !== undefined || r.applied !== undefined);
  const tagText = v && v !== 'REJECTED' ? 'Changed' : 'Not applied';
  const titles = {
    ACCEPTED: 'The room’s proposal was accepted',
    MODIFIED: 'The proposal was adjusted',
    REJECTED: 'The design stays as it was',
  };
  const cmp = rows.length
    ? el('div', { class: 'cmp', role: 'table', 'aria-label': 'Room voted versus applied' },
      el('div', { class: 'cmp-row head', role: 'row' },
        el('span', { role: 'columnheader', text: 'Parameter' }),
        el('span', { role: 'columnheader', text: 'Room voted' }),
        el('span', { role: 'columnheader', text: 'Applied' })),
      rows.map((r) => el('div', { class: 'cmp-row' + (r.changed ? ' changed' : ''), role: 'row' },
        el('span', { class: 'name', role: 'cell' }, r.param.label, r.changed ? el('span', { class: 'changed-tag', text: tagText }) : null),
        el('span', { class: 'val voted', role: 'cell', text: r.votedText }),
        el('span', { class: 'val applied', role: 'cell', text: r.appliedText }),
        r.changed && r.reason ? el('span', { class: 'reason', text: r.reason }) : null,
      )))
    : null;
  const meta = joinDot([d.round_number != null ? `Round ${d.round_number}` : '', fmtTime(d.created_at, '')]);
  return el('article', { class: 'p-decision anim-slide-up', 'aria-label': 'Decision' },
    el('div', { class: 'head' }, el('span', { class: 't-label', text: meta || 'Decision' }), verdictBadge(d)),
    el('h2', { text: v ? titles[v] : 'No change this round' }),
    !v && d.message ? el('p', { class: 'p-rationale', style: { marginTop: '0', marginBottom: '14px' }, text: String(d.message) }) : null,
    cmp,
    d.rationale ? el('p', { class: 'p-rationale', text: String(d.rationale) }) : null,
    v === 'REJECTED' && d.message
      ? el('p', { class: 'p-rationale' }, el('strong', { text: 'What to vote for instead: ' }), String(d.message)) : null,
    el('p', { class: 'p-indicative', text: 'Metrics are indicative' }),
  );
}

function viewDecision(round) {
  const waiting = !round || round.status !== 'open';
  return el('div', { class: 'p-stack' },
    view3dSwitch(),
    closedNoticeCard(round),
    decisionCard(S.decision),
    waiting ? el('div', { class: 'p-hero' },
      el('p', { class: 'p-state-text', style: { marginTop: '0' } }, el('b', { text: 'Waiting for the next round.' }), ' Keep this page open.'),
    ) : null,
  );
}

function resolveView() {
  if (S.configError) return 'config_error';
  if (!S.cfg || S.status === undefined) return 'loading';
  const round = currentRound();
  if (!round) return S.decision ? 'decision' : 'waiting';
  if (round.status === 'open') return hasVoted(round.id) ? 'voted' : 'questions';
  if (S.decision && String(S.decision.round_id) === round.id) return 'decision';
  return 'reviewing';
}

/* ------------------------------------------------------------------ 3D layout */
// One persistent stage (one WebGL context for the whole visit): a fixed, full-screen scene. On top:
// the header (small wordmark, ONE status pill, (i)) and one bottom row (Orbit/Tour + one guided
// button while voting). Everything else lives in sheets. Views re-parent the stage; moving the
// element keeps the canvas and its WebGL context.
const stage = { root: null, host: null, poster: null, modeSeg: null };
const STAGE_VIEWS = ['waiting', 'questions', 'voted', 'reviewing', 'decision'];

function use3d() {
  return S.v3d.status !== 'failed' && S.v3d.pref === '3d' && URLQ.get('v3d') !== '0';
}
function questionsMode() { return use3d() ? '3d' : 'list'; }
function wantsStage(view) { return use3d() && STAGE_VIEWS.includes(view); }

function setLayoutPref(pref) {
  S.v3d.pref = pref === 'list' ? 'list' : '3d';
  sessionStore.set(VIEW_KEY, S.v3d.pref);
  closeSheet(false);
  S.hint = '';
  S.viewKey = '';
  render();
  window.scrollTo(0, 0);
  if (S.v3d.pref === 'list' && S.curView === 'questions') focusTitle();
}

function segControl(label, items, get, onPick) {
  const node = el('div', { class: 'seg seg-ov', role: 'group', 'aria-label': label });
  const btns = items.map(([value, text, aria]) => {
    const b = el('button', { class: 'seg-btn', type: 'button', 'aria-pressed': 'false', 'aria-label': aria || null, text });
    b.addEventListener('click', () => onPick(value));
    node.appendChild(b);
    return [value, b];
  });
  const sync = () => { const v = get(); for (const [value, b] of btns) b.setAttribute('aria-pressed', String(v === value)); };
  const setDisabled = (d) => { for (const [, b] of btns) b.disabled = d; };
  sync();
  return { node, sync, setDisabled };
}

function buildStage() {
  if (stage.root) return stage.root;
  stage.host = el('div', { class: 'v3d-host' });
  stage.poster = el('div', { class: 'p-stage-poster', 'aria-hidden': 'true' },
    el('img', { src: DESIGN_IMG, alt: '', width: 1064, height: 524, decoding: 'async' }),
    el('span', { class: 'p-stage-loading' }, icon('loader-circle', 16, 'spin'), 'Loading 3D…'));
  stage.modeSeg = segControl('View', [['orbit', 'Orbit', 'Orbit: turn the model'], ['tour', 'Tour', 'Tour: walk around the building']], () => S.v3d.mode, onModeSeg);
  stage.modeSeg.setDisabled(S.v3d.status !== 'ready');
  stage.root = el('section', { class: 'p-stage', 'aria-label': 'Langford A in 3D' }, stage.poster, stage.host);
  return stage.root;
}

function ensureViewer() {
  const v = S.v3d;
  if (v.status !== 'idle') return;
  if (URLQ.get('v3d') === '0') { v.status = 'failed'; return; }
  buildStage();
  v.status = 'loading';
  // The real building (Langford A, js/model/langford.js): its base geometry is fetched and decoded
  // once; a failure here (network, decode) falls back to the list view like a WebGL failure.
  Promise.all([import('./viewer3d.js'), import('./model/langford.js').then((pm) => pm.loadLangford().then(() => pm))]).then(([vm, pm]) => {
    if (v.status !== 'loading') return;
    v.mod = pm;
    v.build = pm.buildLangford;
    try {
      v.pinLabels = {};
      for (const p of pm.buildLangford({}).pins || []) if (p && p.label && !v.pinLabels[p.question]) v.pinLabels[p.question] = String(p.label);
    } catch (_) { v.pinLabels = null; }
    const q = URLQ.get('q');
    v.viewer = vm.createViewer(stage.host, {
      questionTags: pm.QUESTION_TAGS,
      quality: q != null && /^[0-2]$/.test(q) ? Number(q) : 'auto',
      tourHint: false,
      canvasLabel: '3D model of Langford Architecture Center, Building A. Drag to turn it, pinch to zoom, drag with two fingers to move. The pins mark the questions.',
      onPinTap,
      onReady: onViewerReady,
      onFallback: viewerFailed,
      onModeChange: (m) => {
        v.mode = m;
        stage.modeSeg.sync();
        syncStage();
        requestAnimationFrame(applyInsets);
      },
    });
    if (URLQ.get('debug') === '1') window.__plurarch = { S, viewer: v.viewer, render };
    // orbit only: in the tour a tap walks, and resetView would leave the tour
    watchDoubleTap(stage.host, () => {
      if (v.viewer && v.status === 'ready' && v.viewer.getMode() === 'orbit') v.viewer.resetView();
    });
    v.modelKey = '';
    v.pinsKey = '';
    syncStage();
    applyInsets();
  }).catch((e) => {
    console.warn('[plurarch] 3D view unavailable:', e);
    viewerFailed('load');
  });
}

function onViewerReady() {
  if (S.v3d.status !== 'loading') return;
  S.v3d.status = 'ready';
  stage.root.classList.add('is-ready');
  stage.modeSeg.setDisabled(false);
  syncStage();
  applyInsets();
}

function viewerFailed(reason) {
  const v = S.v3d;
  if (v.status === 'failed') return;
  console.warn('[plurarch] 3D view off:', reason);
  v.status = 'failed';
  closeSheet(false);
  if (v.viewer) { try { v.viewer.dispose(); } catch (_) { /* gone */ } }
  v.viewer = null;
  S.viewKey = ''; // re-mount the current screen in the list layout
  render();
}

function onModeSeg(m) {
  const viewer = S.v3d.viewer;
  if (!viewer || S.v3d.status !== 'ready') return;
  if (m === 'orbit' && viewer.getMode() === 'orbit') { viewer.resetView(); return; }
  viewer.setMode(m);
}

function paramsFrom(source) {
  const out = {};
  for (const p of S.cfg.params) {
    let v = source && source[p.key];
    if (v == null || v === '') v = appliedValue(S.decision, p);
    if (v == null) v = p.default;
    out[p.key] = p.type === 'slider' ? toNum(v) : String(v);
  }
  return out;
}

// Voting: the participant's draft. Voted: their vote. Otherwise: the applied design.
function stageParams(view) {
  const round = currentRound();
  if (view === 'questions' && S.draft) return paramsFrom(S.draft.answers);
  if (view === 'voted' && round) return paramsFrom(votedAnswers(round.id));
  return paramsFrom(null);
}

function votePins() {
  const d = S.draft;
  if (!d) return [];
  const n = S.cfg.params.length;
  return S.cfg.params.map((p, i) => {
    const set = isAnswered(p);
    const value = set ? fmtParamValue(p, d.answers[p.key]) : 'Pick one';
    return {
      question: p.key, value, badge: set ? 'check' : 'ring',
      state: S.v3d.sheetKey === p.key ? 'active' : set ? 'answered' : 'todo',
      aria: `Question ${i + 1} of ${n}, ${p.label}: ${set ? value : 'not set yet'}. Opens the question.`,
    };
  });
}

function decisionPins() {
  const v = verdictOf(S.decision);
  const tag = v && v !== 'REJECTED' ? 'Changed' : 'Not applied';
  return decisionRows(S.decision, S.cfg.params).map((r) => ({
    question: r.param.key, value: r.appliedText, badge: 'none', tag,
    state: S.v3d.sheetKey === r.param.key ? 'active' : r.changed ? 'changed' : 'info',
    aria: `${r.param.label}: ${r.appliedText}${r.changed ? ` (${tag.toLowerCase()}, the room voted ${r.votedText})` : ''}. Shows the details.`,
  }));
}

// Read-only pins (waiting, voted, reviewing): the values the model shows.
function valuePins(view) {
  const vals = stageParams(view);
  return S.cfg.params.map((p) => ({
    question: p.key, value: fmtParamValue(p, vals[p.key]), badge: 'none',
    state: S.v3d.sheetKey === p.key ? 'active' : 'info',
    aria: `${p.label}: ${fmtParamValue(p, vals[p.key])}. Shows the question.`,
  }));
}

function stagePins(view) {
  if (view === 'questions') return votePins();
  if (view === 'decision' && S.decision) return decisionPins();
  return valuePins(view);
}

function modelFor(params) {
  const v = S.v3d;
  const key = JSON.stringify(params);
  let m = v.cache.get(key);
  if (!m) {
    m = v.build(params);
    v.cache.set(key, m);
    if (v.cache.size > 6) v.cache.delete(v.cache.keys().next().value);
  }
  return { key, model: m };
}

// Push the current screen's design, pins, idle turn and highlight to the viewer (cheap when unchanged).
let modelRaf = 0;
let modelParams = null;
function syncStage() {
  const v = S.v3d;
  if (!stage.root || !v.viewer) return;
  const view = S.curView;
  if (!wantsStage(view)) return;
  if (v.stageView !== view) {
    // A new screen after the idle turn (or a tour): bring the orbit camera home so the pins show.
    const prev = v.stageView;
    v.stageView = view;
    if (prev && (view === 'questions' || view === 'decision') && v.status === 'ready' && v.viewer.getMode() === 'orbit') v.viewer.resetView();
  }
  modelParams = stageParams(view);
  if (!modelRaf) {
    modelRaf = requestAnimationFrame(() => {
      modelRaf = 0;
      if (!v.viewer || !modelParams || !v.build) return;
      try {
        const { key, model } = modelFor(modelParams);
        if (key !== v.modelKey) {
          v.modelKey = key;
          v.viewer.setModel(model, { keepCamera: true });
        }
      } catch (e) {
        console.error('[plurarch] model build failed', e);
        viewerFailed('model');
      }
    });
  }
  const pins = stagePins(view);
  const pk = JSON.stringify(pins);
  if (pk !== v.pinsKey) { v.pinsKey = pk; v.viewer.setPins(pins); }
  v.viewer.setIdleRotation((view === 'waiting' || view === 'voted' || view === 'reviewing') && !v.sheetKey);
  v.viewer.highlight(v.sheetKey || null);
}

function onPinTap(key) {
  const btn = stage.host && stage.host.querySelector(`.v3d-pin[data-q="${CSS.escape(key)}"]`);
  if (S.curView === 'questions' && S.draft && use3d()) openQuestionSheet(key, btn);
  else openPinCard(key, btn);
}

/* ---- the status pill (the one line of text on screen) */
function participantsCount() {
  const v = S.status ? S.status.round_participants : null;
  if (v == null || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) && n >= 0 ? Math.round(n) : null;
}

function countdownText() {
  const opened = parseTime(S.status && S.status.round_opened_at);
  const dur = Number(S.cfg.profile.round_duration_s);
  if (!Number.isFinite(opened) || !(dur > 0)) return '';
  const left = Math.ceil((opened + dur * 1000 - Date.now()) / 1000);
  return left > 0 ? fmtClock(Math.min(left, dur)) : 'closing…';
}

// short names for the verdict chip and the reveal: the model's pin label (Glass, Fins, ...) when the 3D
// model is loaded, else the first word of the question label
const shortLabel = (p) => (S.v3d.pinLabels && S.v3d.pinLabels[p.key]) || String(p.label).split(' ')[0];
const chipWord = (w) => (w.length > 1 && w === w.toUpperCase() ? w : w.toLowerCase()); // keep 'SE'
const bareValue = (p, v) => (p.type === 'slider' ? fmtSliderNumber(p, v) : fmtParamValue(p, v));

function firstChange(d) {
  return decisionRows(d, S.cfg.params).find((r) => r.changed) || null;
}

// Short change for the pill chip: "canopy 2.5 m", "as voted", "kept".
function decisionShort(d) {
  const v = verdictOf(d);
  if (!v) return 'no change';
  if (v === 'REJECTED') return 'kept';
  const ch = firstChange(d);
  return v === 'MODIFIED' && ch ? `${chipWord(shortLabel(ch.param))} ${ch.appliedText}` : 'as voted';
}

function statusParts(view, problem) {
  if (problem) return { icon: 'spin', text: 'Reconnecting…' };
  const n = participantsCount();
  switch (view) {
    case 'questions': {
      const cd = countdownText();
      return { icon: 'live', text: joinDot([n != null ? `${n} voting` : 'Voting', cd]) };
    }
    case 'voted': return { icon: 'check', text: n != null ? `Voted · ${n} voting` : 'Voted' };
    case 'reviewing': return { icon: 'spin', text: 'AI reviewing…' };
    default: return { icon: 'live', text: 'Live · next round soon' };
  }
}

function renderStatusPill() {
  const pill = dom.statusPill;
  if (!pill) return;
  const view = S.curView;
  const problem = S.pollProblem || S.submitProblem;
  if (!problem && view === 'decision' && S.decision) {
    const d = S.decision;
    const v = verdictOf(d);
    const short = decisionShort(d);
    const key = `d|${S.decisionId}|${short}`;
    if (pill.dataset.key === key) return;
    pill.dataset.key = key;
    pill.dataset.kind = 'chip';
    mount(pill, el('button', {
      class: 'pill-chip', type: 'button', 'aria-haspopup': 'dialog',
      'aria-label': `Decision: ${v || 'no change'}, ${short}. Show details.`,
      onclick: (e) => openDetailsSheet(e.currentTarget),
    }, verdictBadge(d), el('span', { class: 'pill-text', text: short })));
    return;
  }
  const parts = statusParts(view, problem);
  const key = JSON.stringify(parts);
  if (pill.dataset.key === key) return;
  pill.dataset.key = key;
  pill.dataset.kind = 'text';
  const ic = parts.icon === 'live' ? el('span', { class: 'live-dot', 'aria-hidden': 'true' })
    : parts.icon === 'spin' ? icon('loader-circle', 16, 'spin') : icon('check', 16);
  mount(pill, ic, el('span', { class: 'pill-text', text: parts.text }));
}

/* ---- moments: "Vote now", the vote check, the decision reveal */
function showSplash(text, then) {
  const n = el('div', { class: 'splash', 'aria-hidden': 'true' }, el('span', { class: 'splash-text', text }));
  document.body.appendChild(n);
  requestAnimationFrame(() => requestAnimationFrame(() => n.classList.add('in')));
  const fast = reducedMotion();
  setTimeout(() => {
    n.classList.remove('in');
    setTimeout(() => { n.remove(); if (then) then(); }, fast ? 0 : 320);
  }, fast ? 900 : 1200);
}

function pulsePrimary() {
  const b = document.getElementById('go-3d');
  if (!b || reducedMotion()) return;
  b.classList.remove('pulse-once');
  void b.offsetWidth; // restart the animation
  b.classList.add('pulse-once');
}

function popCheck() {
  const n = el('div', { class: 'pop-check', 'aria-hidden': 'true' }, el('span', { class: 'pop-disc' }, icon('check', 28)));
  document.body.appendChild(n);
  setTimeout(() => n.remove(), reducedMotion() ? 700 : 1100);
}

function revealLine(d) {
  const v = verdictOf(d);
  if (!v) return 'No change this round';
  if (v === 'REJECTED') return 'Kept the design';
  const ch = firstChange(d);
  if (v === 'MODIFIED' && ch) return `${shortLabel(ch.param)} ${bareValue(ch.param, ch.voted)} → ${ch.appliedText}`;
  return 'Applied as voted';
}

// The element to fly to: what the reviewer changed, else what the room changed in the design.
function revealTarget(d) {
  const v = verdictOf(d);
  if (!v || v === 'REJECTED') return null;
  const ch = firstChange(d);
  if (ch) return ch.param.key;
  const cur = asObject(asObject(d.proposal).current_parameters);
  const moved = decisionRows(d, S.cfg.params).find((r) => r.applied !== undefined && cur[r.param.key] !== undefined && String(cur[r.param.key]) !== String(r.applied));
  return moved ? moved.param.key : null;
}

let reveal = null;
function showReveal(d) {
  if (reveal) reveal.collapse(true);
  const v = verdictOf(d);
  const word = v || 'No change';
  const line = revealLine(d);
  const node = el('div', { class: `reveal reveal-${v ? v.toLowerCase() : 'none'}`, role: 'status' },
    el('div', { class: 'reveal-card' },
      el('div', { class: 'reveal-word', text: word }),
      el('div', { class: 'reveal-line', text: line })));
  document.body.appendChild(node);
  announce(`Decision: ${word}. ${line}`);
  requestAnimationFrame(() => requestAnimationFrame(() => node.classList.add('in')));
  const target = revealTarget(d);
  const viewer = S.v3d.viewer;
  if (viewer && target && S.v3d.status === 'ready' && !reducedMotion()) {
    viewer.focus(target);
    viewer.pulse(target, 3300);
  }
  let done = false;
  const collapse = (instant) => {
    if (done) return;
    done = true;
    clearTimeout(timer);
    reveal = null;
    const card = node.querySelector('.reveal-card');
    const pill = dom.statusPill;
    if (!instant && pill && card && !reducedMotion()) {
      // shrink into the status pill, where the verdict chip lives from now on
      const a = card.getBoundingClientRect(); const b = pill.getBoundingClientRect();
      const sc = Math.max(0.08, Math.min(1, b.width / a.width));
      card.style.transform = `translate(${Math.round(b.left + b.width / 2 - (a.left + a.width / 2))}px, ${Math.round(b.top + b.height / 2 - (a.top + a.height / 2))}px) scale(${sc.toFixed(3)})`;
    }
    node.classList.remove('in');
    node.classList.add('out');
    setTimeout(() => {
      node.remove();
      if (pill && !instant) { pill.classList.remove('pop'); void pill.offsetWidth; pill.classList.add('pop'); }
    }, instant || reducedMotion() ? 0 : 480);
  };
  node.addEventListener('click', () => collapse(false));
  const timer = setTimeout(() => collapse(false), 3500);
  reveal = { collapse };
}

/* ---- bottom row: Orbit/Tour + one guided button */
function view3d(view, round) {
  if (view === 'questions') ensureDraft(round);
  const row = el('div', { class: 'ov-row' }, stage.modeSeg.node,
    view === 'questions' ? el('button', { class: 'ov-go', id: 'go-3d', type: 'button', onclick: onPrimary }) : null);
  const bottom = el('div', { class: 'ov ov-bottom', id: 'ov-bottom' }, row);
  return [buildStage(), bottom];
}

function updatePrimary() {
  const btn = document.getElementById('go-3d');
  if (!btn || !S.draft) return;
  const params = S.cfg.params;
  const n = params.filter(isAnswered).length;
  const all = n === params.length;
  const key = JSON.stringify([n, all, S.submitting]);
  if (btn.dataset.key === key) return;
  btn.dataset.key = key;
  btn.textContent = '';
  btn.removeAttribute('aria-label');
  if (S.submitting) {
    btn.append(icon('loader-circle', 20, 'spin'), 'Sending…');
    btn.disabled = true;
    return;
  }
  btn.disabled = false;
  btn.dataset.mode = all ? 'submit' : 'next';
  if (all) btn.append('Submit vote');
  else {
    btn.append('Next question', el('span', { class: 'go-count', 'aria-hidden': 'true', text: ` · ${n}/${params.length}` }));
    btn.setAttribute('aria-label', `Next question, ${n} of ${params.length} set`);
  }
  if (all && S.v3d.wasComplete === false) pulsePrimary();
  S.v3d.wasComplete = all;
}

function onPrimary(e) {
  if (!S.draft || S.submitting) return;
  const next = S.cfg.params.find((p) => !isAnswered(p));
  if (next) openQuestionSheet(next.key, e && e.currentTarget);
  else submit3d();
}

function submit3d() {
  if (!S.draft || S.submitting) return;
  const missing = S.cfg.params.find((p) => !isAnswered(p));
  if (missing) {
    S.hint = 'Pick one option to continue.';
    openQuestionSheet(missing.key, document.getElementById('go-3d'));
    return;
  }
  closeSheet(false);
  submit();
}

// Refresh the pill, the guided button, an open question list and the pins in place (no re-mount).
function refreshOverlays() {
  if (!use3d() || !STAGE_VIEWS.includes(S.curView)) return;
  renderStatusPill();
  if (S.curView === 'questions' && S.draft) {
    updatePrimary();
    if (sheet.kind === 'list') renderQuestionRows();
  }
  syncStage();
}

function refreshVoteUI() {
  if (S.curView !== 'questions' || !S.draft) return;
  if (use3d()) refreshOverlays();
}

// Covered bands: pins avoid the header and the bottom row, tour chips sit above the bottom row,
// and the camera centres the model between them (or between the header and an open sheet).
let ovRO = null;
let insets = { top: 0, bottom: 0 };
function watchOverlays() {
  if (!('ResizeObserver' in window)) { requestAnimationFrame(applyInsets); return; }
  if (!ovRO) ovRO = new ResizeObserver(() => applyInsets());
  ovRO.disconnect();
  const bottom = document.getElementById('ov-bottom');
  if (bottom) ovRO.observe(bottom);
  if (dom.header) ovRO.observe(dom.header);
  requestAnimationFrame(applyInsets);
}

function applyInsets() {
  const viewer = S.v3d.viewer;
  const bottom = document.getElementById('ov-bottom');
  if (!viewer || !bottom || !dom.header) return;
  const topPx = dom.header.getBoundingClientRect().bottom;
  const botPx = Math.max(0, window.innerHeight - bottom.getBoundingClientRect().top);
  insets = { top: topPx, bottom: botPx };
  viewer.setOverlayInsets(insets);
  if (!sheet.root || !sheet.focusModel) viewer.setViewInset(botPx, topPx);
}

/* ------------------------------------------------------------------ sheets */
// One bottom sheet at a time: a question, the question list, the (i) info, the decision details or a
// read-only pin card. Modal: focus is trapped, Escape / Done / the backdrop close it, the page behind
// is inert, and it scrolls inside itself only.
const sheet = { root: null, backdrop: null, kind: null, key: null, opener: null, onKey: null, focusModel: false };

function setInert(on) {
  for (const n of [dom.header, dom.main, dom.bar]) {
    if (!n) continue;
    if (on) { n.inert = true; n.setAttribute('aria-hidden', 'true'); } else { n.inert = false; n.removeAttribute('aria-hidden'); }
  }
}

function sheetFocusables() {
  if (!sheet.root) return [];
  return [...sheet.root.querySelectorAll('button, input, [href], [tabindex]:not([tabindex="-1"])')]
    .filter((n) => !n.disabled && !(n.type === 'radio' && !n.checked && sheet.root.querySelector(`input[name="${CSS.escape(n.name)}"]:checked`)));
}

function sheetHead(label) {
  return el('div', { class: 'sheet-head' },
    el('span', { class: 't-label', text: label || '' }),
    el('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Close', onclick: () => closeSheet(true) }, icon('x', 20)));
}

function doneButton(text = 'Done') {
  const b = el('button', { class: 'btn btn-ink sheet-done', type: 'button', text });
  b.addEventListener('click', () => closeSheet(true));
  return b;
}

/**
 * Open a sheet. { kind, key (question it is about, or null), content: [nodes], opener,
 * focusModel (keep the element in view above the sheet), compact }
 * The content must contain an element with id "sheet-title".
 */
function showSheet({ kind, key = null, content, opener = null, focusModel = false, compact = false, descId = null }) {
  const replacing = !!sheet.root;
  const keepOpener = replacing ? sheet.opener : null;
  if (replacing) closeSheet(false, true);
  if (reveal) reveal.collapse(true);
  S.v3d.sheetKey = key;
  const dlg = el('div', {
    class: 'sheet' + (compact ? ' compact' : ''), role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'sheet-title',
    'aria-describedby': descId, dataset: { kind },
  }, el('div', { class: 'sheet-grab', 'aria-hidden': 'true' }), content);
  const backdrop = el('div', { class: 'sheet-backdrop', 'aria-hidden': 'true' });
  backdrop.addEventListener('click', () => closeSheet(true));
  document.body.append(backdrop, dlg);
  Object.assign(sheet, { root: dlg, backdrop, kind, key, opener: keepOpener || opener || null, focusModel });
  sheet.onKey = (e) => {
    if (!sheet.root) return;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeSheet(true); return; }
    if (e.key === 'Enter' && e.target && e.target.classList && e.target.classList.contains('tile-input')) {
      e.preventDefault(); e.stopPropagation(); closeSheet(true); return;
    }
    if (e.key !== 'Tab') return;
    const f = sheetFocusables();
    if (!f.length) return;
    const first = f[0]; const last = f[f.length - 1];
    const inside = sheet.root.contains(document.activeElement);
    if (e.shiftKey && (!inside || document.activeElement === first || document.activeElement.id === 'sheet-title')) { e.preventDefault(); last.focus(); } else if (!e.shiftKey && (!inside || document.activeElement === last)) { e.preventDefault(); first.focus(); }
  };
  document.addEventListener('keydown', sheet.onKey, true);
  setInert(true);
  if (replacing) { dlg.classList.add('open'); backdrop.classList.add('open'); } else {
    requestAnimationFrame(() => requestAnimationFrame(() => { dlg.classList.add('open'); backdrop.classList.add('open'); }));
  }
  const title = document.getElementById('sheet-title');
  if (title) { try { title.focus({ preventScroll: true }); } catch (_) { title.focus(); } }
  if (!use3d() && window.scrollY > 0) window.scrollTo({ top: 0, behavior: reducedMotion() ? 'auto' : 'smooth' });
  const viewer = S.v3d.viewer;
  if (viewer && use3d() && focusModel) {
    const overlap = Math.max(0, dlg.offsetHeight);
    viewer.setViewInset(Math.min(window.innerHeight * 0.62, overlap), insets.top);
    if (key) viewer.focus(key);
  }
  refreshOverlays();
  return dlg;
}

function closeSheet(returnFocus = true, replacing = false) {
  if (!sheet.root) return;
  const { root, backdrop, opener } = sheet;
  document.removeEventListener('keydown', sheet.onKey, true);
  Object.assign(sheet, { root: null, backdrop: null, kind: null, key: null, onKey: null, focusModel: false });
  if (replacing) { root.remove(); backdrop.remove(); return; }
  sheet.opener = null;
  root.classList.remove('open');
  backdrop.classList.remove('open');
  setTimeout(() => { root.remove(); backdrop.remove(); }, reducedMotion() ? 0 : 300);
  S.v3d.sheetKey = null;
  S.hint = '';
  setInert(false);
  if (S.v3d.viewer) S.v3d.viewer.setViewInset(insets.bottom, insets.top);
  refreshOverlays();
  if (returnFocus && opener && document.contains(opener)) {
    try { opener.focus({ preventScroll: true }); } catch (_) { opener.focus(); }
  }
}

function openQuestionSheet(key, opener) {
  const params = S.cfg.params;
  const p = params.find((x) => x.key === key);
  if (!p || !S.draft || S.submitting) return;
  const i = params.indexOf(p);
  const opts = { titleId: 'sheet-title', explId: 'sheet-expl', hintId: 'sheet-hint', idPrefix: 'sheet', onChange: refreshVoteUI };
  const hintText = S.hint || '';
  showSheet({
    kind: 'question', key, opener, focusModel: true, descId: p.explainer ? 'sheet-expl' : null,
    content: [
      sheetHead(`Question ${i + 1} of ${params.length}`),
      el('h2', { class: 'q-title', id: 'sheet-title', tabindex: '-1', text: p.question }),
      p.explainer ? el('p', { class: 'q-explainer', id: 'sheet-expl', text: p.explainer }) : null,
      p.type === 'choice' ? choiceControl(p, opts) : sliderControl(p, opts),
      el('p', { class: 'q-hint' + (hintText ? ' alert' : ''), id: 'sheet-hint', role: 'status', text: hintText }),
      doneButton(),
    ],
  });
}

// All questions as rows (reachable from (i); a pin can be hidden at some angles).
function renderQuestionRows() {
  const list = document.getElementById('sheet-qrows');
  if (!list || !S.draft) return;
  const params = S.cfg.params;
  mount(list, params.map((p, i) => {
    const set = isAnswered(p);
    const value = set ? fmtParamValue(p, S.draft.answers[p.key]) : 'Not set yet';
    return el('li', null, el('button', {
      class: 'q-row', type: 'button', dataset: { q: p.key, state: set ? 'answered' : 'todo' }, 'aria-haspopup': 'dialog',
      'aria-label': `Question ${i + 1}, ${p.label}: ${set ? value : 'not set yet'}`,
      onclick: () => openQuestionSheet(p.key, dom.infoBtn),
    },
    el('span', { class: 'q-row-badge' }, set ? icon('check', 18) : null),
    el('span', { class: 'q-row-text' }, el('span', { class: 'q-row-label', text: p.label }), el('span', { class: 'q-row-value', text: value })),
    icon('chevron-right', 20)));
  }));
}

function openQuestionsList(opener) {
  if (!S.draft) return;
  const params = S.cfg.params;
  const n = params.filter(isAnswered).length;
  showSheet({
    kind: 'list', opener,
    content: [
      sheetHead(`${n} of ${params.length} set`),
      el('h2', { class: 'sheet-h2', id: 'sheet-title', tabindex: '-1', text: 'Questions' }),
      el('ul', { class: 'q-rows', id: 'sheet-qrows', 'aria-label': 'Questions' }),
    ],
  });
  renderQuestionRows();
}

// One short paragraph about the whole purpose (config: use-case profile "about"). The detailed
// rules and goals are deliberately not shown on phones: the talk is short, and the reviewer
// explains every decision anyway.
function infoSections(prefix) {
  const p = S.cfg.profile;
  const about = p.about || p.privacy_notice || '';
  return [
    el('section', { class: 'notice', 'aria-labelledby': `${prefix}-about-title` },
      el('span', { class: 'notice-icon' }, icon('info', 20)),
      el('h2', { class: 'notice-title t-title', id: `${prefix}-about-title`, text: 'About Plurarch' }),
      el('p', { text: about }),
    ),
  ];
}

function openInfoSheet(opener) {
  const voting = S.curView === 'questions' && S.draft && use3d();
  const n = voting ? S.cfg.params.filter(isAnswered).length : 0;
  showSheet({
    kind: 'info', opener,
    content: [
      sheetHead(S.cfg.profile.session_title || 'Plurarch'),
      el('h2', { class: 'sr-only', id: 'sheet-title', tabindex: '-1', text: 'About Plurarch' }),
      el('div', { class: 'sheet-actions' },
        voting ? el('button', { class: 'btn btn-gray', type: 'button', onclick: () => openQuestionsList(dom.infoBtn) },
          icon('list-checks', 18), `All questions · ${n}/${S.cfg.params.length}`) : null,
        el('button', { class: 'btn btn-gray', type: 'button', onclick: () => setLayoutPref('list') }, icon('list', 18), 'List view')),
      el('div', { class: 'p-info sheet-info' }, infoSections('sh')),
      doneButton('Close'),
    ],
  });
}

function openDetailsSheet(opener) {
  if (!S.decision) return;
  showSheet({
    kind: 'details', opener,
    content: [
      sheetHead('Decision'),
      el('h2', { class: 'sr-only', id: 'sheet-title', tabindex: '-1', text: 'Decision details' }),
      decisionCard(S.decision),
      doneButton('Close'),
    ],
  });
}

// Read-only card for a pin outside voting: the question and the value the model shows.
function openPinCard(key, opener) {
  const params = S.cfg.params;
  const p = params.find((x) => x.key === key);
  if (!p) return;
  const view = S.curView;
  const round = currentRound();
  const rows = [];
  let note = null;
  if (view === 'decision' && S.decision) {
    const r = decisionRows(S.decision, params).find((x) => x.param.key === key);
    rows.push(['Applied', r ? r.appliedText : fmtParamValue(p, appliedValue(S.decision, p))]);
    if (r && r.voted !== undefined) rows.push(['Room voted', r.votedText]);
    if (r && r.changed && r.reason) note = r.reason;
  } else {
    if (view === 'voted' && round) {
      const mine = votedAnswers(round.id) || {};
      if (mine[key] != null) rows.push(['Your vote', fmtParamValue(p, mine[key])]);
    }
    rows.push(['Now', fmtParamValue(p, appliedValue(S.decision, p))]);
  }
  showSheet({
    kind: 'card', key, opener, focusModel: true, compact: true,
    content: [
      sheetHead(p.label),
      el('h2', { class: 'sheet-h2', id: 'sheet-title', tabindex: '-1', text: p.question }),
      el('dl', { class: 'card-values' }, rows.map(([k, v]) => el('div', null, el('dt', { text: `${k}:` }), el('dd', { text: v })))),
      note ? el('p', { class: 'q-explainer card-note', text: note }) : null,
      doneButton(),
    ],
  });
}

/* ------------------------------------------------------------------ render */
function renderPills() {
  const round = currentRound();
  const pill = dom.roundPill;
  pill.textContent = '';
  if (S.status === undefined) pill.append('Connecting…');
  else if (!round) pill.append('Waiting');
  else if (round.status === 'open') pill.append(el('span', { class: 'live-dot', 'aria-hidden': 'true' }), `${roundName(round.number)} · open`);
  else pill.append(`${roundName(round.number)} · closed`);

  const problem = S.pollProblem || S.submitProblem;
  if (problem) {
    if (dom.netPill.hidden) {
      mount(dom.netPill, icon('loader-circle', 16, 'spin'), 'Reconnecting…');
      dom.netPill.hidden = false;
    }
  } else {
    dom.netPill.hidden = true;
  }
  if (document.documentElement.classList.contains('is-3d')) renderStatusPill();
}

function setLayoutClass(threeD) {
  document.documentElement.classList.toggle('is-3d', threeD);
}

function render() {
  if (!S.cfg && !S.configError) { renderPills(); return; }
  const view = resolveView();
  const round = currentRound();
  if (use3d() && STAGE_VIEWS.includes(view)) ensureViewer(); // may switch to the fallback at once (?v3d=0)
  const threeD = wantsStage(view);
  const sub = view === 'questions' ? questionsMode() : '';
  if (sheet.root && (!threeD || ((sheet.kind === 'question' || sheet.kind === 'list') && view !== 'questions')
    || (sheet.kind === 'details' && view !== 'decision') || (sheet.kind === 'card' && view !== S.curView))) closeSheet(false);
  if (reveal && view !== 'decision') reveal.collapse(true);
  const prevView = S.lastView;
  S.curView = view;
  renderPills();
  const key = JSON.stringify([
    view, threeD, round && round.id, round && round.status, S.decisionId,
    sub === 'list' && S.draft ? S.draft.step : null,
    S.closedNotice && S.closedNotice.roundId, S.submitting, S.submitError, S.status ? 1 : 0,
    S.v3d.status === 'failed',
  ]);
  if (key === S.viewKey) {
    if (threeD) refreshOverlays(); else if (view === 'questions') updateBar();
    return;
  }
  S.viewKey = key;

  setLayoutClass(threeD);
  let nodes;
  if (threeD) nodes = view3d(view, round);
  else {
    switch (view) {
      case 'config_error': nodes = viewConfigError(); break;
      case 'loading': nodes = viewLoading(); break;
      case 'questions': nodes = viewQuestions(round); break;
      case 'voted': nodes = viewVoted(round); break;
      case 'reviewing': nodes = viewReviewing(round); break;
      case 'decision': nodes = viewDecision(round); break;
      default: nodes = viewWaiting();
    }
  }
  mount(dom.view, nodes);
  updateBarVisibility(view === 'questions' && !threeD);
  if (threeD) {
    S.v3d.wasComplete = null;
    refreshOverlays();
    watchOverlays();
  } else if (view === 'questions') updateBar();

  if (view !== prevView) {
    const live = prevView && prevView !== 'loading'; // a change seen on this page, not the first screen
    if (view === 'questions') {
      announce(`${roundName(round && round.number)} is open. ${S.cfg.params.length} questions.`);
      if (threeD && live) showSplash('Vote now', pulsePrimary);
      else if (threeD) setTimeout(pulsePrimary, 900);
    }
    if (view === 'voted' && prevView === 'questions' && threeD) {
      popCheck();
      toast('Watch the big screen');
      announce('Vote recorded');
    }
    if (view === 'decision' && S.decision) announce(`Decision: ${verdictOf(S.decision) || 'no change'}`);
    if (view === 'reviewing') announce('Voting closed. The reviewer agent is reviewing.');
    S.lastView = view;
  }
  if (view === 'decision' && S.revealId && S.revealId === S.decisionId) {
    S.revealId = null;
    if (threeD) showReveal(S.decision);
  }
  if (threeD && S.closedNotice && S.v3d.closedToast !== S.closedNotice.roundId) {
    S.v3d.closedToast = S.closedNotice.roundId;
    toast('The round closed before your vote was sent');
  }
  if (threeD && S.submitError) toast(S.submitError);
}

function updateBarVisibility(show) {
  dom.bar.hidden = !show;
  dom.main.classList.toggle('has-bar', show);
}

function renderInfo() {
  mount(dom.info, el('div', { class: 'p-info' }, infoSections('pg')));
}

/* ------------------------------------------------------------------ boot */
// In the 3D layout the page never scrolls or zooms: the scene takes every gesture. Safari's own
// pinch gesture events and stray touchmoves outside scrollable sheets are cancelled as a backstop to
// touch-action / overscroll-behavior (older iOS versions ignore overscroll-behavior).
function guardGestures() {
  const in3d = () => document.documentElement.classList.contains('is-3d');
  const scrollable = (t) => t && t.closest && t.closest('.sheet, .v3d-stops');
  const vv = window.visualViewport;
  // If the browser zoomed the page anyway (double-tap on an old iOS, accessibility zoom), never
  // block the gesture that would undo it: the guards only apply while the page is at scale 1.
  const zoomed = () => !!(vv && vv.scale > 1.01);
  for (const type of ['gesturestart', 'gesturechange']) {
    document.addEventListener(type, (e) => { if (in3d() && !scrollable(e.target) && !zoomed()) e.preventDefault(); }, { passive: false });
  }
  document.addEventListener('touchmove', (e) => {
    if (in3d() && !scrollable(e.target) && !zoomed() && e.touches.length <= 2) e.preventDefault();
  }, { passive: false });
  // ...and snap it back: re-applying the viewport meta (maximum-scale=1) makes iOS and Android
  // return to scale 1, so the overlays, pins and the Submit button are never left off-screen.
  if (vv) {
    let timer = 0;
    vv.addEventListener('resize', () => {
      if (!zoomed()) return;
      clearTimeout(timer);
      timer = setTimeout(resetPageZoom, 250);
    });
  }
}

function resetPageZoom() {
  const meta = document.getElementById('vp');
  if (!meta) return;
  const base = 'width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover';
  meta.setAttribute('content', base.replace('initial-scale=1', 'initial-scale=1.0'));
  requestAnimationFrame(() => {
    meta.setAttribute('content', base);
    window.scrollTo(0, 0);
  });
}

// Double-tap on the 3D model = "reset the view" (a useful action instead of a page zoom).
function watchDoubleTap(host, onDouble) {
  let last = 0; let lx = 0; let ly = 0; let downX = 0; let downY = 0;
  // capture phase: the orbit controls handle (and may stop) these events on the canvas itself
  host.addEventListener('pointerdown', (e) => { downX = e.clientX; downY = e.clientY; }, { capture: true });
  host.addEventListener('pointerup', (e) => {
    if (e.pointerType !== 'touch' || !e.isPrimary) return;
    if (Math.hypot(e.clientX - downX, e.clientY - downY) > 12) { last = 0; return; } // a drag, not a tap
    if (e.target && e.target.closest && e.target.closest('button, a, input, .sheet')) return; // pins, controls
    const t = performance.now();
    if (t - last < 350 && Math.hypot(e.clientX - lx, e.clientY - ly) < 40) { last = 0; onDouble(); return; }
    last = t; lx = e.clientX; ly = e.clientY;
  }, { capture: true });
}

async function boot() {
  mount(dom.brand, wordmark());
  if (dom.infoBtn) {
    mount(dom.infoBtn, icon('info', 20));
    dom.infoBtn.addEventListener('click', () => { if (S.cfg) openInfoSheet(dom.infoBtn); });
  }
  loadIcons();
  guardGestures();
  dom.barBtn.addEventListener('click', goNext);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !sheet.root && !dom.bar.hidden && e.target && e.target.classList && e.target.classList.contains('tile-input')) {
      e.preventDefault();
      goNext();
    }
  });

  let attempt = 0;
  while (!S.cfg) {
    try {
      const cfg = window.PLURARCH_CONFIG || {};
      S.cfg = await loadAppConfig(cfg.useCase);
    } catch (e) {
      S.pollProblem = true;
      renderPills();
      await sleep(Math.min(1000 * 2 ** attempt, 10000));
      attempt += 1;
    }
  }
  S.pollProblem = false;
  if (!S.cfg.params.length) {
    S.configError = 'No questions are configured (config/parameters.json).';
    render();
    return;
  }
  S.backend = createBackend('participant');
  if (S.backend.kind === 'supabase' && !S.backend.configured()) {
    S.configError = 'The Supabase project is not set in config.js yet.';
    render();
    return;
  }
  S.pid = participantId();
  renderInfo();
  render();
  document.addEventListener('visibilitychange', onVisibility);
  window.addEventListener('online', () => schedulePoll(200 + Math.random() * 800));
  window.addEventListener('pageshow', (e) => { if (e.persisted) schedulePoll(100 + Math.random() * 500); });
  window.addEventListener('resize', () => requestAnimationFrame(applyInsets));
  // The countdown in the status pill: text only, once a second, only while it can change.
  setInterval(() => {
    if (!document.hidden && use3d() && (S.curView === 'questions' || S.curView === 'voted')) renderStatusPill();
  }, 1000);
  poll();
}

boot().catch((e) => {
  // Last-resort guard: never leave a blank screen.
  console.error(e);
  setLayoutClass(false);
  mount(dom.view, el('div', { class: 'p-hero' },
    el('h1', { class: 'p-state-title', text: 'Something went wrong' }),
    el('p', { class: 'p-state-text', text: 'Please reload the page.' })));
});
