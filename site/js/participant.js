// Plurarch participant app: anonymous, mobile-first. Polls the tiny status endpoint with jitter,
// submits all answers in one call, and shows the decision. No realtime connection is ever opened here.
//
// Voting has two views that share one draft:
//   - 3D (default): the pavilion model (js/viewer3d.js, three.js loaded lazily) with one pin per
//     question on its element; a pin or a question row opens a bottom sheet with the same controls
//     as the stepper, and the model rebuilds live from the draft ("Your version").
//   - List view: the original one-question-per-screen stepper. It is also the fallback whenever
//     WebGL2 or the CDN is unavailable (then the static design image is shown instead of the model).
// URL flags for testing: ?v3d=0 forces the fallback, ?q=0|1|2 pins the 3D quality tier.

import { createBackend, isRetryable } from './backend.js';
import {
  el, mount, icon, loadIcons, loadAppConfig, store, sessionStore, randomId, sleep, asObject,
  verdictBadge, verdictOf, decisionRows, appliedValue, snapToStep, wordmark, reducedMotion,
} from './ui.js';
import { fmtParamValue, fmtSliderNumber, fmtTime, joinDot, fmtNum, toNum } from './format.js';

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
    mod: null, // the pavilion model module (buildPavilion, QUESTION_TAGS)
    pref: sessionStore.get(VIEW_KEY) === 'list' ? 'list' : '3d',
    mode: 'orbit', // orbit | tour
    version: 'yours', // yours | current (voting and voted screens)
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
  netPill: document.getElementById('net-pill'),
  announce: document.getElementById('announce'),
  brand: document.getElementById('brand'),
};

const DRAFT_KEY = 'plurarch:draft';
const PID_KEY = 'plurarch:participant_id';
const DESIGN_IMG = 'img/initial-design.svg';

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
    S.v3d.version = 'yours';
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
      alt: 'Drawing of the pavilion: a one-room box with a mono-pitch roof, vertical façade fins, a glazing band and an entrance canopy.',
    }),
    el('figcaption', { class: 'design-caption', text: caption || 'The pavilion · initial design' }),
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
        if (d && typeof d === 'object') { S.decision = d; S.decisionId = did; }
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

/* ------------------------------------------------------------------ views */
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

function viewWaiting() {
  const noSession = !S.status;
  return el('div', { class: 'p-stack' },
    el('div', { class: 'p-hero anim-rise' },
      el('h1', { class: 'p-state-title', text: noSession ? 'Waiting for the session to start' : 'Waiting for the next round' }),
      el('p', { class: 'p-state-text', text: 'Keep this page open. The questions appear here as soon as voting opens.' }),
    ),
    stageOrTile(),
  );
}

function viewQuestions(round) {
  if (questionsMode() === '3d') return viewVote3d(round);
  const d = ensureDraft(round);
  const params = S.cfg.params;
  const i = Math.min(d.step, params.length - 1);
  const p = params[i];
  const back = i > 0
    ? el('button', { class: 'text-btn', type: 'button', onclick: goBack }, icon('arrow-left', 18), 'Back')
    : el('span', { 'aria-hidden': 'true' });
  const hintText = S.submitError || S.hint || '';
  return el('div', { class: 'q-screen anim-rise' },
    S.v3d.status !== 'failed' ? el('div', { class: 'view-switch' },
      el('button', { class: 'text-btn', type: 'button', onclick: () => setQuestionsMode('3d') }, icon('box', 18), '3D model')) : null,
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
    el('div', { class: 'p-card anim-slide-up' },
      el('div', { class: 'p-icon-circle' }, icon('check', 24)),
      el('h1', { class: 'p-state-title', text: 'Vote recorded' }),
      el('p', { class: 'p-state-text', text: `${roundName(round.number)} · When the round closes, the reviewer agent judges the room's proposal against the brief.` }),
      rows.length ? el('ul', { class: 'answers', 'aria-label': 'Your answers' }, rows) : null,
    ),
    stageOrTile(),
  );
}

function viewReviewing(round) {
  const voted = hasVoted(round.id);
  return el('div', { class: 'p-stack' },
    closedNoticeCard(round),
    el('div', { class: 'p-decision anim-slide-up' },
      el('div', { class: 'p-icon-circle soft' }, icon('loader-circle', 24, 'spin')),
      el('h1', { class: 'p-state-title', text: voted ? 'The reviewer agent is reviewing your proposal…' : "The reviewer agent is reviewing the room's proposal…" }),
      el('p', { class: 'p-state-text', text: `${roundName(round.number)} is closed. The agent checks the hard rules and the brief's goals, tries alternatives if needed, and applies the result to the model.` }),
    ),
    stageOrTile(),
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
    closedNoticeCard(round),
    S.v3d.status !== 'failed' ? buildStage() : null,
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

/* ------------------------------------------------------------------ 3D stage */
// One persistent stage (one WebGL context for the whole visit). Views place it in their tree;
// moving the element keeps the canvas and its context alive.
const stage = { root: null, host: null, poster: null, bar: null, modeSeg: null, verSeg: null };

function questionsMode() {
  return S.v3d.status === 'failed' ? 'list' : S.v3d.pref;
}

function wantsStage(view) {
  return view === 'waiting' || view === 'voted' || view === 'reviewing' || view === 'decision'
    || (view === 'questions' && questionsMode() === '3d');
}

function setQuestionsMode(m) {
  S.v3d.pref = m === 'list' ? 'list' : '3d';
  sessionStore.set(VIEW_KEY, S.v3d.pref);
  closeSheet(false);
  S.hint = '';
  render();
  window.scrollTo(0, 0);
  if (S.v3d.pref === 'list') focusTitle();
}

function segControl(label, items, get, onPick) {
  const node = el('div', { class: 'seg seg-float', role: 'group', 'aria-label': label });
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
    el('span', { class: 'p-stage-loading' }, icon('loader-circle', 16, 'spin'), 'Loading the 3D model…'));
  stage.modeSeg = segControl('View', [['orbit', 'Orbit', 'Orbit: turn the model'], ['tour', 'Tour', 'Tour: walk inside']], () => S.v3d.mode, onModeSeg);
  stage.modeSeg.setDisabled(S.v3d.status !== 'ready');
  stage.verSeg = segControl('Which design', [['yours', 'Your version'], ['current', 'Current', 'Current design']], () => S.v3d.version, onVersionSeg);
  stage.bar = el('div', { class: 'p-stage-bar' }, stage.modeSeg.node, stage.verSeg.node);
  stage.root = el('section', { class: 'p-stage', 'aria-label': 'The pavilion in 3D' }, stage.poster, stage.host, stage.bar);
  return stage.root;
}

function stageOrTile(caption) {
  return S.v3d.status === 'failed' ? designTile(caption) : buildStage();
}

function ensureViewer() {
  const v = S.v3d;
  if (v.status !== 'idle') return;
  if (URLQ.get('v3d') === '0') { v.status = 'failed'; return; }
  buildStage();
  v.status = 'loading';
  Promise.all([import('./viewer3d.js'), import('./model/pavilion.js')]).then(([vm, pm]) => {
    if (v.status !== 'loading') return;
    v.mod = pm;
    const q = URLQ.get('q');
    v.viewer = vm.createViewer(stage.host, {
      questionTags: pm.QUESTION_TAGS,
      quality: q != null && /^[0-2]$/.test(q) ? Number(q) : 'auto',
      insetTop: 58,
      canvasLabel: '3D model of the pavilion. Drag to turn it. The pins mark the questions.',
      onPinTap,
      onReady: onViewerReady,
      onFallback: viewerFailed,
      onModeChange: (m) => {
        v.mode = m;
        stage.modeSeg.sync();
        syncStage();
      },
    });
    if (URLQ.get('debug') === '1') window.__plurarch = { S, viewer: v.viewer, render };
    v.modelKey = '';
    v.pinsKey = '';
    syncStage();
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
}

function viewerFailed(reason) {
  const v = S.v3d;
  if (v.status === 'failed') return;
  console.warn('[plurarch] 3D view off:', reason);
  v.status = 'failed';
  closeSheet(false);
  if (v.viewer) { try { v.viewer.dispose(); } catch (_) { /* gone */ } }
  v.viewer = null;
  S.viewKey = ''; // re-mount the current screen with the list view and the design image
  render();
}

function onModeSeg(m) {
  const viewer = S.v3d.viewer;
  if (!viewer || S.v3d.status !== 'ready') return;
  if (m === 'orbit' && viewer.getMode() === 'orbit') { viewer.resetView(); return; }
  viewer.setMode(m);
}

function onVersionSeg(ver) {
  if (S.v3d.version === ver) return;
  S.v3d.version = ver;
  stage.verSeg.sync();
  syncStage();
  announce(ver === 'yours' ? 'Showing your version' : 'Showing the current design');
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

function stageParams(view) {
  const round = currentRound();
  if (view === 'questions' && S.v3d.version === 'yours' && S.draft) return paramsFrom(S.draft.answers);
  if (view === 'voted' && S.v3d.version === 'yours' && round) return paramsFrom(votedAnswers(round.id));
  return paramsFrom(null);
}

function votePins() {
  const d = S.draft;
  if (!d) return [];
  const n = S.cfg.params.length;
  const current = S.v3d.version === 'current'; // the model shows the current design: pins say so too
  return S.cfg.params.map((p, i) => {
    const set = isAnswered(p);
    const mine = set ? fmtParamValue(p, d.answers[p.key]) : 'not set yet';
    const value = current ? `Now ${fmtParamValue(p, appliedValue(S.decision, p))}` : set ? mine : 'Pick one';
    return {
      question: p.key, num: i + 1, value,
      state: S.v3d.sheetKey === p.key ? 'active' : set ? 'answered' : 'todo',
      aria: `Question ${i + 1} of ${n}, ${p.label}: ${current ? value.replace('Now', 'now') + ', your answer ' + mine : mine}. Opens the question.`,
    };
  });
}

function decisionPins() {
  const v = verdictOf(S.decision);
  const tag = v && v !== 'REJECTED' ? 'Changed' : 'Not applied';
  return decisionRows(S.decision, S.cfg.params).map((r, i) => ({
    question: r.param.key, num: i + 1, value: r.appliedText,
    state: r.changed ? 'changed' : 'answered', tag,
    aria: `${r.param.label}: ${r.appliedText} applied${r.voted !== undefined ? `, the room voted ${r.votedText}` : ''}${r.changed ? ` (${tag.toLowerCase()})` : ''}. Highlights it on the model.`,
  }));
}

function modelFor(params) {
  const v = S.v3d;
  const key = JSON.stringify(params);
  let m = v.cache.get(key);
  if (!m) {
    m = v.mod.buildPavilion(params);
    v.cache.set(key, m);
    if (v.cache.size > 6) v.cache.delete(v.cache.keys().next().value);
  }
  return { key, model: m };
}

// Push the current screen's design, pins and highlight to the viewer (cheap when nothing changed).
let modelRaf = 0;
let modelParams = null;
function syncStage() {
  const v = S.v3d;
  if (!stage.root || v.status === 'failed') return;
  const view = S.curView;
  const onStage = wantsStage(view);
  const version = view === 'questions' || view === 'voted';
  stage.verSeg.node.hidden = !version;
  stage.root.classList.toggle('compact', view === 'waiting' || view === 'voted' || view === 'reviewing');
  if (!v.viewer || !onStage) return;
  if (v.stageView !== view) {
    // Entering a screen with pins (voting, decision) after the idle turn: bring the camera home.
    const prev = v.stageView;
    v.stageView = view;
    if (prev && (view === 'questions' || view === 'decision') && v.status === 'ready' && v.viewer.getMode() === 'orbit') v.viewer.resetView();
  }
  modelParams = stageParams(view);
  if (!modelRaf) {
    modelRaf = requestAnimationFrame(() => {
      modelRaf = 0;
      if (!v.viewer || !modelParams || !v.mod) return;
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
  const pins = view === 'questions' ? votePins() : view === 'decision' && S.decision ? decisionPins() : [];
  const pk = JSON.stringify(pins);
  if (pk !== v.pinsKey) { v.pinsKey = pk; v.viewer.setPins(pins); }
  v.viewer.setIdleRotation(view === 'waiting' || view === 'voted' || view === 'reviewing');
  v.viewer.highlight(view === 'questions' ? v.sheetKey : view === 'decision' ? v.highlight : null);
}

function onPinTap(key) {
  if (S.curView === 'questions' && S.draft && questionsMode() === '3d') {
    const btn = stage.host && stage.host.querySelector(`.v3d-pin[data-q="${CSS.escape(key)}"]`);
    openSheet(key, btn);
    return;
  }
  if (S.curView === 'decision') {
    S.v3d.highlight = S.v3d.highlight === key ? null : key;
    syncStage();
    if (S.v3d.highlight && S.v3d.viewer) S.v3d.viewer.focus(key);
    const pin = decisionPins().find((x) => x.question === key);
    if (pin && S.v3d.highlight) announce(pin.aria.replace(' Highlights it on the model.', ''));
  }
}

/* ------------------------------------------------------------------ 3D voting screen */
function viewVote3d(round) {
  ensureDraft(round);
  const params = S.cfg.params;
  const hintText = S.submitError || S.hint || '';
  const rows = params.map((p, i) => el('li', null, el('button', {
    class: 'q-row', type: 'button', dataset: { q: p.key }, 'aria-haspopup': 'dialog',
    onclick: (e) => openSheet(p.key, e.currentTarget),
  },
  el('span', { class: 'q-row-badge' }),
  el('span', { class: 'q-row-text' }, el('span', { class: 'q-row-label', text: p.label }), el('span', { class: 'q-row-value' })),
  icon('chevron-right', 20))));
  return el('div', { class: 'p-stack anim-rise' },
    buildStage(),
    el('div', { class: 'vote-panel' },
      el('div', { class: 'vote-progress' },
        el('div', { class: 'vote-count', id: 'vote-count', role: 'status' }),
        el('button', { class: 'text-btn', type: 'button', onclick: () => setQuestionsMode('list') }, icon('list', 18), 'List view')),
      el('p', { class: 'vote-lead', text: 'Tap a pin on the model to answer its question, or pick one below.' }),
      el('ul', { class: 'q-rows', 'aria-label': 'Questions' }, rows),
      el('p', { class: 'q-hint' + (hintText ? ' alert' : ''), id: 'q-hint', role: 'status', text: hintText }),
    ),
  );
}

// Update progress, rows, pins, the submit button and the model in place (no re-mount).
function refreshVoteUI() {
  if (S.curView !== 'questions' || questionsMode() !== '3d' || !S.draft) return;
  const params = S.cfg.params;
  const n = params.filter(isAnswered).length;
  const count = document.getElementById('vote-count');
  if (count) {
    const dots = el('span', { class: 'dots', 'aria-hidden': 'true' }, params.map((p) => el('i', { class: isAnswered(p) ? 'done' : '' })));
    const text = `${n} of ${params.length}`;
    if (count.dataset.text !== text) {
      count.dataset.text = text;
      mount(count, el('span', { text }), el('span', { class: 'of', text: 'set' }), dots);
    }
  }
  for (const btn of dom.view.querySelectorAll('.q-row')) {
    const p = params.find((x) => x.key === btn.dataset.q);
    if (!p) continue;
    const set = isAnswered(p);
    const i = params.indexOf(p);
    btn.dataset.state = set ? 'answered' : 'todo';
    const badge = btn.querySelector('.q-row-badge');
    mount(badge, set ? icon('check', 18) : String(i + 1));
    btn.querySelector('.q-row-value').textContent = set ? fmtParamValue(p, S.draft.answers[p.key]) : 'Not set yet';
    btn.setAttribute('aria-label', `Question ${i + 1}, ${p.label}: ${set ? fmtParamValue(p, S.draft.answers[p.key]) : 'not set yet'}`);
  }
  updateVoteBar();
  syncStage();
}

function updateVoteBar() {
  const btn = dom.barBtn;
  btn.textContent = '';
  if (S.submitting) {
    btn.append(icon('loader-circle', 20, 'spin'), 'Sending…');
    btn.disabled = true;
    btn.removeAttribute('aria-disabled');
    return;
  }
  btn.disabled = false;
  btn.append('Submit vote');
  if (S.cfg.params.every(isAnswered)) btn.removeAttribute('aria-disabled');
  else btn.setAttribute('aria-disabled', 'true');
}

function submit3d() {
  if (!S.draft || S.submitting) return;
  const missing = S.cfg.params.find((p) => !isAnswered(p));
  if (missing) {
    S.hint = 'Pick one option to continue.';
    openSheet(missing.key, dom.barBtn);
    return;
  }
  closeSheet(false);
  submit();
}

/* ------------------------------------------------------------------ question sheet */
const sheet = { root: null, backdrop: null, key: null, opener: null, onKey: null, inertEls: [] };

function setInert(on) {
  const els = [document.querySelector('.p-header'), dom.main, dom.bar];
  for (const n of els) {
    if (!n) continue;
    if (on) { n.inert = true; n.setAttribute('aria-hidden', 'true'); } else { n.inert = false; n.removeAttribute('aria-hidden'); }
  }
}

function sheetFocusables() {
  if (!sheet.root) return [];
  return [...sheet.root.querySelectorAll('button, input, [href], [tabindex]:not([tabindex="-1"])')]
    .filter((n) => !n.disabled && !(n.type === 'radio' && !n.checked && sheet.root.querySelector(`input[name="${CSS.escape(n.name)}"]:checked`)));
}

function openSheet(key, opener) {
  const params = S.cfg.params;
  const p = params.find((x) => x.key === key);
  if (!p || !S.draft || S.submitting) return;
  const replacing = !!sheet.root;
  const keepOpener = replacing ? sheet.opener : null;
  if (replacing) closeSheet(false, true);
  S.v3d.sheetKey = key;
  if (S.v3d.version !== 'yours') { S.v3d.version = 'yours'; if (stage.verSeg) stage.verSeg.sync(); }
  const i = params.indexOf(p);
  const opts = { titleId: 'sheet-title', explId: 'sheet-expl', hintId: 'sheet-hint', idPrefix: 'sheet', onChange: refreshVoteUI };
  const done = el('button', { class: 'btn btn-ink sheet-done', type: 'button', text: 'Done' });
  done.addEventListener('click', () => closeSheet(true));
  const hintText = S.hint || '';
  const dlg = el('div', {
    class: 'sheet', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'sheet-title',
    'aria-describedby': p.explainer ? 'sheet-expl' : null,
  },
  el('div', { class: 'sheet-grab', 'aria-hidden': 'true' }),
  el('div', { class: 'sheet-head' },
    el('span', { class: 't-label', text: `Question ${i + 1} of ${params.length}` }),
    el('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Close', onclick: () => closeSheet(true) }, icon('x', 20))),
  el('h2', { class: 'q-title', id: 'sheet-title', tabindex: '-1', text: p.question }),
  p.explainer ? el('p', { class: 'q-explainer', id: 'sheet-expl', text: p.explainer }) : null,
  p.type === 'choice' ? choiceControl(p, opts) : sliderControl(p, opts),
  el('p', { class: 'q-hint' + (hintText ? ' alert' : ''), id: 'sheet-hint', role: 'status', text: hintText }),
  done);
  const backdrop = el('div', { class: 'sheet-backdrop', 'aria-hidden': 'true' });
  backdrop.addEventListener('click', () => closeSheet(true));
  document.body.append(backdrop, dlg);
  sheet.root = dlg;
  sheet.backdrop = backdrop;
  sheet.key = key;
  sheet.opener = keepOpener || opener || null;
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
  try { title.focus({ preventScroll: true }); } catch (_) { title.focus(); }
  // Keep the element in view: stage at the top of the page, camera turned to it, model centred above the sheet.
  if (window.scrollY > 0) window.scrollTo({ top: 0, behavior: reducedMotion() ? 'auto' : 'smooth' });
  refreshVoteUI();
  const viewer = S.v3d.viewer;
  if (viewer && stage.root && document.contains(stage.root)) {
    const r = stage.root.getBoundingClientRect();
    const stageBottom = r.bottom + window.scrollY; // page coordinates; the page scrolls to the top
    const overlap = stageBottom - (window.innerHeight - dlg.offsetHeight);
    viewer.setViewInset(Math.max(0, Math.min(r.height * 0.6, overlap)));
    viewer.focus(key);
  }
}

function closeSheet(returnFocus = true, replacing = false) {
  if (!sheet.root) return;
  const { root, backdrop, opener } = sheet;
  document.removeEventListener('keydown', sheet.onKey, true);
  sheet.root = null; sheet.backdrop = null; sheet.key = null; sheet.onKey = null;
  if (replacing) { root.remove(); backdrop.remove(); return; }
  sheet.opener = null;
  root.classList.remove('open');
  backdrop.classList.remove('open');
  setTimeout(() => { root.remove(); backdrop.remove(); }, reducedMotion() ? 0 : 300);
  S.v3d.sheetKey = null;
  S.hint = '';
  setInert(false);
  if (S.v3d.viewer) S.v3d.viewer.setViewInset(0);
  refreshVoteUI();
  syncStage();
  if (returnFocus && opener && document.contains(opener)) {
    try { opener.focus({ preventScroll: true }); } catch (_) { opener.focus(); }
  }
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
}

function render() {
  if (!S.cfg && !S.configError) { renderPills(); return; }
  renderPills();
  const view = resolveView();
  const round = currentRound();
  if (wantsStage(view)) ensureViewer(); // may switch to the fallback at once (?v3d=0)
  const sub = view === 'questions' ? questionsMode() : '';
  if (sub !== '3d') closeSheet(false);
  if (view !== 'decision') S.v3d.highlight = null;
  S.curView = view;
  const key = JSON.stringify([
    view, sub, round && round.id, round && round.status, S.decisionId,
    sub === 'list' && S.draft ? S.draft.step : null,
    S.closedNotice && S.closedNotice.roundId, S.submitting, S.submitError, S.status ? 1 : 0,
    S.v3d.status === 'failed',
  ]);
  if (view === 'questions') updateBarVisibility(true);
  if (key === S.viewKey) {
    if (sub === '3d') refreshVoteUI(); else if (view === 'questions') updateBar();
    syncStage();
    return;
  }
  S.viewKey = key;

  let node;
  switch (view) {
    case 'config_error': node = viewConfigError(); break;
    case 'loading': node = viewLoading(); break;
    case 'questions': node = viewQuestions(round); break;
    case 'voted': node = viewVoted(round); break;
    case 'reviewing': node = viewReviewing(round); break;
    case 'decision': node = viewDecision(round); break;
    default: node = viewWaiting();
  }
  mount(dom.view, node);
  updateBarVisibility(view === 'questions');
  if (sub === '3d') refreshVoteUI(); else if (view === 'questions') updateBar();
  syncStage();

  if (view !== S.lastView) {
    if (view === 'questions' && S.lastView) announce(`${roundName(round && round.number)} is open. ${S.cfg.params.length} questions.`);
    if (view === 'decision' && S.decision) announce(`Decision: ${verdictOf(S.decision) || 'no change'}`);
    if (view === 'reviewing') announce('Voting closed. The reviewer agent is reviewing.');
    S.lastView = view;
  }
}

function updateBarVisibility(show) {
  dom.bar.hidden = !show;
  dom.main.classList.toggle('has-bar', show);
}

function renderInfo() {
  const b = S.cfg.brief;
  const p = S.cfg.profile;
  const rules = b.hard_rules.map((r) => el('li', { text: String(r.plain || r.label || '') }));
  const goals = b.goals.map((g) => el('li', { text: String(g.plain || g.label || '') }));
  const margin = toNum(b.close_tradeoff_margin);
  const closeCall = Number.isFinite(margin) && margin > 0
    ? ` A goal missed by ${fmtNum(margin)} index points or less is a close call, and close calls go to the room.`
    : ' Close calls go to the room.';
  const sections = [
    el('section', { class: 'notice', 'aria-labelledby': 'rules-title' },
      el('span', { class: 'notice-icon' }, icon('info', 20)),
      el('h2', { class: 'notice-title t-title', id: 'rules-title', text: 'Rules of the game' }),
      b.narrative ? el('p', { class: 't-secondary', text: b.narrative }) : null,
      rules.length ? el('p', null, el('b', { text: 'Hard rules, never broken' })) : null,
      rules.length ? el('ul', null, rules) : null,
      goals.length ? el('p', null, el('b', { text: 'Goals of the brief' })) : null,
      goals.length ? el('ul', null, goals) : null,
      el('p', { text: 'The reviewer agent decides in a strict order: hard rules first, then the brief’s goals, then your votes. It overrules the room only for a measurable reason, never on taste.' + closeCall }),
      el('p', { class: 't-secondary', text: 'Metrics are indicative: simple, documented proxies, not simulations.' }),
    ),
  ];
  if (p.privacy_notice) {
    sections.push(el('div', { class: 'p-divider', 'aria-hidden': 'true' }));
    sections.push(el('section', { class: 'notice', 'aria-labelledby': 'privacy-title' },
      el('span', { class: 'notice-icon' }, icon('info', 20)),
      el('h2', { class: 'notice-title t-title', id: 'privacy-title', text: 'Privacy' }),
      el('p', { text: p.privacy_notice }),
    ));
  }
  mount(dom.info, el('div', { class: 'p-info' }, sections));
}

/* ------------------------------------------------------------------ boot */
async function boot() {
  mount(dom.brand, wordmark());
  loadIcons();
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
  poll();
}

boot().catch((e) => {
  // Last-resort guard: never leave a blank screen.
  console.error(e);
  mount(dom.view, el('div', { class: 'p-hero' },
    el('h1', { class: 'p-state-title', text: 'Something went wrong' }),
    el('p', { class: 'p-state-text', text: 'Please reload the page.' })));
});
