// Plurarch backend adapter: one promise-based API, two implementations.
//   window.PLURARCH_CONFIG.backend === "local"    -> the local server (orchestrator.py run), fetch /api/...
//   window.PLURARCH_CONFIG.backend === "supabase" -> supabase-js (loaded lazily from the CDN)
// Contract: docs/ARCHITECTURE.md. Participants never open a realtime connection; only the
// console's subscribe() does (Supabase mode).

import { CDN, loadScript, sessionStore } from './ui.js';

/* ------------------------------------------------------------------ errors */
const RETRYABLE = new Set(['network', 'timeout', 'server']);

/**
 * Typed backend error. `code` is one of: network, timeout, server (retryable), round_closed,
 * invalid_value, invalid_participant, unauthorized, forbidden, invalid_credentials, not_facilitator, round_already_open,
 * no_open_round, no_session, not_found, not_configured, or another short server code.
 */
export class BackendError extends Error {
  constructor(code, message, extra = {}) {
    super(message || code);
    this.name = 'BackendError';
    this.code = code;
    this.status = extra.status == null ? null : extra.status;
    this.detail = extra.detail || '';
  }
  get retryable() { return RETRYABLE.has(this.code); }
}

export function isRetryable(err) {
  return !!(err && (err.retryable === true || err.name === 'TypeError'));
}

async function fetchWithTimeout(input, init = {}, ms = 10000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ms);
  const outer = init && init.signal;
  if (outer) {
    if (outer.aborted) ctrl.abort();
    else outer.addEventListener('abort', () => ctrl.abort(), { once: true });
  }
  try {
    return await fetch(input, { ...init, signal: ctrl.signal });
  } finally {
    clearTimeout(timer);
  }
}

/* ------------------------------------------------------------------ local */
const KEY_STORAGE = 'plurarch:facilitator_key';

class LocalBackend {
  constructor(cfg, role) {
    this.kind = 'local';
    this.role = role;
    this.authKind = 'key';
    const base = typeof cfg.apiBase === 'string' ? cfg.apiBase.trim() : '';
    this.base = base.replace(/\/+$/, '');
    this.key = null;
  }

  // Relative paths only ("api/status"), so the site works under any base path.
  url(path) { return this.base ? `${this.base}/${path}` : path; }

  async request(method, path, { body, auth = false, timeout = 10000 } = {}) {
    const headers = { Accept: 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (auth) {
      if (!this.key) throw new BackendError('unauthorized', 'Facilitator key required', { status: 401 });
      headers.Authorization = `Bearer ${this.key}`;
    }
    let res;
    try {
      res = await fetchWithTimeout(this.url(path), {
        method, headers, cache: 'no-store', credentials: 'same-origin',
        body: body !== undefined ? JSON.stringify(body) : undefined,
      }, timeout);
    } catch (e) {
      const aborted = e && e.name === 'AbortError';
      throw new BackendError(aborted ? 'timeout' : 'network', aborted ? 'Request timed out' : 'Network error', { detail: String(e && e.message || e) });
    }
    let text = '';
    try { text = await res.text(); } catch (e) {
      throw new BackendError('network', 'Network error while reading the response', { status: res.status });
    }
    let data = null;
    if (text) { try { data = JSON.parse(text); } catch (_) { data = null; } }
    if (res.ok) return data;
    const serverCode = data && typeof data.error === 'string' ? data.error : '';
    let code;
    if (res.status >= 500) code = 'server';
    else if (res.status === 401) code = 'unauthorized';
    else if (res.status === 403) code = serverCode || 'forbidden';
    else if (serverCode) code = serverCode;
    else if (res.status === 404) code = 'not_found';
    else if (res.status === 408 || res.status === 429) code = 'server';
    else code = 'http_' + res.status;
    const detail = data && data.detail ? String(data.detail) : serverCode;
    throw new BackendError(code, detail || code, { status: res.status, detail });
  }

  /* participant */
  async getStatus() {
    const data = await this.request('GET', 'api/status', { timeout: 8000 });
    return data && typeof data === 'object' ? data : null;
  }

  async getDecision(id) {
    return this.request('GET', 'api/decisions/' + encodeURIComponent(id), { timeout: 8000 });
  }

  async submitVotes(roundId, participantId, votes) {
    const body = {
      round_id: roundId,
      participant_id: participantId,
      votes: votes.map((v) => ({ question_key: String(v.question_key), value: String(v.value) })),
    };
    const data = await this.request('POST', 'api/votes', { body, timeout: 10000 });
    return {
      inserted: Number(data && data.inserted) || 0,
      duplicates: Number(data && data.duplicates) || 0,
    };
  }

  /* console: facilitator key auth */
  readKeyFromHash() {
    try {
      const m = /(?:^#|[#&])key=([^&]+)/.exec(window.location.hash || '');
      if (m) {
        const key = decodeURIComponent(m[1]);
        window.history.replaceState(null, '', window.location.pathname + window.location.search);
        if (key) sessionStore.set(KEY_STORAGE, key);
        return key;
      }
    } catch (_) { /* ignore malformed hash */ }
    return null;
  }

  storedKey() { return sessionStore.get(KEY_STORAGE); }

  /** Try the key from the URL hash or sessionStorage. Returns a user object or null. */
  async restore() {
    const key = this.readKeyFromHash() || this.storedKey();
    if (!key) return null;
    this.key = key;
    try {
      await this.loadConsoleState();
      return { label: 'Facilitator', initial: 'F' };
    } catch (e) {
      if (e.code === 'unauthorized' || e.code === 'forbidden') {
        this.key = null;
        sessionStore.remove(KEY_STORAGE);
        return null;
      }
      throw e;
    }
  }

  async signIn({ key } = {}) {
    const k = String(key || '').trim();
    if (!k) throw new BackendError('invalid_credentials', 'Enter the facilitator key');
    this.key = k;
    try {
      await this.loadConsoleState();
    } catch (e) {
      if (e.code === 'unauthorized' || e.code === 'forbidden') {
        this.key = null;
        throw new BackendError('invalid_credentials', 'That facilitator key was not accepted');
      }
      this.key = null;
      throw e;
    }
    sessionStore.set(KEY_STORAGE, k);
    return { label: 'Facilitator', initial: 'F' };
  }

  async signOut() {
    this.key = null;
    sessionStore.remove(KEY_STORAGE);
  }

  onAuthLost() { /* local mode: 401s surface through loadConsoleState */ }

  async loadConsoleState(roundId) {
    const q = roundId ? '?round_id=' + encodeURIComponent(roundId) : '';
    return this.request('GET', 'api/console/state' + q, { auth: true, timeout: 8000 });
  }

  async openRound() {
    const data = await this.request('POST', 'api/rounds/open', { auth: true });
    return { round: data && data.round ? data.round : null };
  }

  async closeRound() {
    const data = await this.request('POST', 'api/rounds/close', { auth: true });
    return { round: data && data.round ? data.round : null };
  }

  /**
   * Poll: calls onChange() (which refetches the console state) every 1 s, one at a time.
   * onConnection('connected' | 'reconnecting' | 'offline') reflects whether polls succeed.
   */
  subscribe(onChange, onConnection) {
    let stopped = false;
    let timer = null;
    let running = false;
    let lastOk = Date.now();
    let failures = 0;
    let state = null;
    const setState = (s) => { if (s !== state) { state = s; if (onConnection) onConnection(s); } };

    const loop = async () => {
      if (stopped || running) return;
      running = true;
      clearTimeout(timer);
      try {
        await onChange();
        failures = 0;
        lastOk = Date.now();
        setState('connected');
      } catch (e) {
        if (isRetryable(e)) {
          failures += 1;
          const offline = (typeof navigator !== 'undefined' && navigator.onLine === false) || Date.now() - lastOk > 15000;
          setState(offline ? 'offline' : 'reconnecting');
        } else {
          setState('connected'); // the server answered; the caller handles the error
        }
      } finally {
        running = false;
        if (!stopped) timer = setTimeout(loop, failures ? Math.min(1000 + failures * 500, 4000) : 1000);
      }
    };
    const onOnline = () => { clearTimeout(timer); loop(); };
    const onOffline = () => setState('offline');
    window.addEventListener('online', onOnline);
    window.addEventListener('offline', onOffline);
    loop();
    return () => {
      stopped = true;
      clearTimeout(timer);
      window.removeEventListener('online', onOnline);
      window.removeEventListener('offline', onOffline);
    };
  }
}

/* ------------------------------------------------------------------ supabase */
function sbError(error, status) {
  if (!error) return null;
  if (error instanceof BackendError) return error;
  const msg = String(error.message || error.error_description || error.msg || error.error || '');
  const code = String(error.code || '');
  const st = typeof status === 'number' ? status : (typeof error.status === 'number' ? error.status : 0);
  const blob = `${msg} ${code} ${error.details || ''} ${error.hint || ''}`;
  if (!st || /failed to fetch|networkerror|network request failed|load failed|fetch failed|aborted|abort|timed? ?out|internet connection|ERR_/i.test(msg)) {
    if (!/round_closed|invalid_value|round_already_open|no_open_round/i.test(blob)) {
      return new BackendError(/abort|timed? ?out/i.test(msg) ? 'timeout' : 'network', 'Network error', { status: st, detail: msg });
    }
  }
  if (st >= 500 || st === 408 || st === 429) return new BackendError('server', msg || 'Server error', { status: st, detail: msg });
  for (const c of ['round_closed', 'invalid_value', 'round_already_open', 'no_open_round', 'no_session', 'not_facilitator']) {
    if (blob.toLowerCase().includes(c)) return new BackendError(c, msg || c, { status: st, detail: msg });
  }
  if (st === 401 || st === 403 || code === '42501' || /row-level security|permission denied|JWT/i.test(msg)) {
    return new BackendError('forbidden', msg || 'Not allowed', { status: st, detail: msg });
  }
  return new BackendError(code || 'error', msg || 'Request failed', { status: st, detail: msg });
}

async function run(builder) {
  const { data, error, status, count } = await builder;
  if (error) throw sbError(error, status);
  return { data, count };
}

class SupabaseBackend {
  constructor(cfg, role) {
    this.kind = 'supabase';
    this.role = role;
    this.authKind = 'password';
    this.cfg = cfg;
    this._clientP = null;
    this._sessionId = null;
    this._closedVotes = new Map(); // round_id -> votes of a closed round (immutable once closed)
    this._authLost = null;
  }

  configured() {
    const u = String(this.cfg.supabaseUrl || '');
    const k = String(this.cfg.supabaseAnonKey || '');
    return /^https:\/\/[^/]+/.test(u) && !/YOUR-PROJECT/i.test(u) && k.length > 20 && !/YOUR-ANON-KEY/i.test(k);
  }

  client() {
    if (!this.configured()) {
      return Promise.reject(new BackendError('not_configured', 'Supabase is not configured in config.js'));
    }
    if (!this._clientP) {
      this._clientP = loadScript(CDN.supabase, { timeout: 20000 })
        .then(() => {
          const sb = window.supabase;
          if (!sb || typeof sb.createClient !== 'function') throw new Error('supabase-js did not initialise');
          const isConsole = this.role === 'console';
          const client = sb.createClient(this.cfg.supabaseUrl, this.cfg.supabaseAnonKey, {
            auth: isConsole
              ? { persistSession: true, autoRefreshToken: true, detectSessionInUrl: false, storageKey: 'plurarch-console-auth' }
              // Participants are anonymous: no stored session, no token refresh.
              : { persistSession: false, autoRefreshToken: false, detectSessionInUrl: false, storageKey: 'plurarch-participant' },
            global: { fetch: (input, init) => fetchWithTimeout(input, init, 12000) },
          });
          if (isConsole) {
            client.auth.onAuthStateChange((event) => {
              if (event === 'SIGNED_OUT' && this._authLost) this._authLost();
            });
          }
          // Note: participants never call client.channel(), so no realtime socket is opened.
          return client;
        })
        .catch((e) => {
          this._clientP = null;
          throw e instanceof BackendError ? e : new BackendError('network', 'Could not load supabase-js', { detail: String(e && e.message) });
        });
    }
    return this._clientP;
  }

  /* participant */
  async getStatus() {
    const c = await this.client();
    const { data } = await run(c.from('session_status').select('*').limit(1).maybeSingle());
    return data || null;
  }

  async getDecision(id) {
    const c = await this.client();
    const { data } = await run(c.from('decisions').select('*').eq('id', id).single());
    return data;
  }

  /**
   * Plain insert (no upsert, no .select()): with RLS, ON CONFLICT would need a SELECT policy,
   * which anon does not have. A database trigger silently skips repeat votes, so any success
   * means "recorded"; inserted rows cannot be told apart from duplicates.
   */
  async submitVotes(roundId, participantId, votes) {
    const pid = String(participantId || '');
    if (!pid || pid.length > 64 || /\s/.test(pid)) {
      throw new BackendError('invalid_participant', 'The participant id is not valid');
    }
    const rows = votes.map((v) => ({
      round_id: roundId, participant_id: pid,
      question_key: String(v.question_key), value: String(v.value),
    }));
    if (rows.some((r) => r.value.length > 32)) throw new BackendError('invalid_value', 'A vote value is too long');
    const c = await this.client();
    const { error, status } = await c.from('votes').insert(rows);
    if (error) {
      const pgCode = String(error.code || '');
      // Unique violation from a race: the vote is already recorded.
      if (pgCode === '23505') return { inserted: rows.length, duplicates: 0 };
      const e = sbError(error, status);
      if (e.retryable) throw e; // network, timeout, 5xx, 408, 429
      const text = `${error.message || ''} ${error.details || ''} ${error.hint || ''}`.toLowerCase();
      if (text.includes('round_closed') || text.includes('round_not_found')) {
        throw new BackendError('round_closed', 'This round has closed', { status: e.status, detail: e.detail });
      }
      for (const code of ['invalid_value', 'unknown_question', 'invalid_participant']) {
        if (text.includes(code)) throw new BackendError(code === 'invalid_participant' ? 'invalid_participant' : 'invalid_value', e.message, { status: e.status, detail: code });
      }
      // RLS refuses anonymous inserts outside an open round.
      if (e.code === 'forbidden') throw new BackendError('round_closed', 'This round has closed', { status: e.status, detail: e.detail });
      throw new BackendError('invalid_value', e.message, { status: e.status, detail: e.detail });
    }
    return { inserted: rows.length, duplicates: 0 };
  }

  /* console */
  async isFacilitator(c) {
    const { data } = await run(c.rpc('is_facilitator'));
    return data === true || (Array.isArray(data) && data[0] === true);
  }

  async restore() {
    const c = await this.client();
    const { data } = await c.auth.getSession();
    const session = data && data.session;
    if (!session) return null;
    let ok = false;
    try { ok = await this.isFacilitator(c); } catch (e) {
      if (e.retryable) throw e;
      ok = false;
    }
    if (!ok) { await c.auth.signOut().catch(() => {}); return null; }
    const email = session.user && session.user.email ? session.user.email : 'Facilitator';
    return { label: email, initial: email.charAt(0).toUpperCase() };
  }

  async signIn({ email, password } = {}) {
    const c = await this.client();
    const em = String(email || '').trim();
    if (!em || !password) throw new BackendError('invalid_credentials', 'Enter your email and password');
    const { data, error } = await c.auth.signInWithPassword({ email: em, password: String(password) });
    if (error) {
      const e = sbError(error, error.status);
      if (e.retryable) throw e;
      throw new BackendError('invalid_credentials', 'Email or password is not correct');
    }
    let ok = false;
    try {
      ok = await this.isFacilitator(c);
    } catch (e) {
      await c.auth.signOut().catch(() => {});
      throw e;
    }
    if (!ok) {
      await c.auth.signOut().catch(() => {});
      throw new BackendError('not_facilitator', 'This account is not a facilitator');
    }
    const label = (data && data.user && data.user.email) || em;
    return { label, initial: label.charAt(0).toUpperCase() };
  }

  async signOut() {
    const c = await this.client().catch(() => null);
    if (c) await c.auth.signOut().catch(() => {});
  }

  onAuthLost(cb) { this._authLost = cb; }

  async _pagedVotes(c, roundId) {
    const out = [];
    const page = 1000;
    for (let from = 0; from < 200000; from += page) {
      const { data } = await run(
        c.from('votes')
          .select('id,round_id,participant_id,question_key,value,is_simulated,created_at')
          .eq('round_id', roundId)
          .order('created_at', { ascending: true })
          .order('id', { ascending: true })
          .range(from, from + page - 1),
      );
      const rows = data || [];
      out.push(...rows);
      if (rows.length < page) break;
    }
    return out;
  }

  async _roundVotes(c, round) {
    if (round.status === 'closed' && this._closedVotes.has(round.id)) return this._closedVotes.get(round.id);
    const votes = await this._pagedVotes(c, round.id);
    if (round.status === 'closed') this._closedVotes.set(round.id, votes);
    return votes;
  }

  /** Same shape as the local GET /api/console/state. */
  async loadConsoleState(roundId) {
    const c = await this.client();
    const now = new Date().toISOString();
    const { data: session } = await run(
      // temporary sessions of tests/test_rls.py and health-check are never the live session
      c.from('sessions').select('*').eq('status', 'active').not('use_case', 'in', '(rls_test,health_check)')
        .order('created_at', { ascending: false }).limit(1).maybeSingle(),
    );
    if (!session) {
      this._sessionId = null;
      return { session: null, questions: [], rounds: [], round: null, votes: [], agent_events: [], decisions: [], participants_total: 0, server_time: now };
    }
    if (session.id !== this._sessionId) { this._sessionId = session.id; this._closedVotes.clear(); }

    const [q, r, d] = await Promise.all([
      run(c.from('questions').select('*').eq('session_id', session.id).order('position', { ascending: true })),
      run(c.from('rounds').select('*').eq('session_id', session.id).order('number', { ascending: true })),
      run(c.from('decisions').select('*').eq('session_id', session.id).order('created_at', { ascending: true })),
    ]);
    const rounds = r.data || [];
    const round = (roundId && rounds.find((x) => x.id === roundId)) || rounds[rounds.length - 1] || null;
    let votes = [];
    let events = [];
    if (round) {
      const [v, e] = await Promise.all([
        this._roundVotes(c, round),
        run(c.from('agent_events').select('*').eq('round_id', round.id).order('seq', { ascending: true })),
      ]);
      votes = v;
      events = e.data || [];
    }
    // Distinct participants across the session (closed rounds are fetched once, then cached).
    const everyone = new Set();
    for (const rr of rounds) {
      const list = round && rr.id === round.id ? votes : await this._roundVotes(c, rr);
      for (const vote of list) everyone.add(vote.participant_id);
    }
    return {
      session, questions: q.data || [], rounds, round, votes, agent_events: events,
      decisions: d.data || [], participants_total: everyone.size, server_time: now,
    };
  }

  async _sessionIdOrLoad() {
    if (this._sessionId) return this._sessionId;
    const st = await this.loadConsoleState();
    if (!st.session) throw new BackendError('no_session', 'No active session');
    return st.session.id;
  }

  async openRound() {
    const c = await this.client();
    const sid = await this._sessionIdOrLoad();
    const { data } = await run(c.rpc('open_round', { p_session_id: sid }));
    return { round: Array.isArray(data) ? data[0] || null : data || null };
  }

  async closeRound() {
    const c = await this.client();
    const sid = await this._sessionIdOrLoad();
    const { data } = await run(c.rpc('close_round', { p_session_id: sid }));
    return { round: Array.isArray(data) ? data[0] || null : data || null };
  }

  /**
   * One realtime channel with postgres_changes on votes, agent_events, decisions and rounds.
   * Each change calls onChange() (refetch), throttled to at most 2 per second; plus a safety
   * refetch every 10 s. Rebuilds the channel with backoff if it errors or closes.
   */
  subscribe(onChange, onConnection) {
    if (this.role !== 'console') throw new Error('Only the console subscribes to realtime');
    let stopped = false;
    let channel = null;
    let subscribed = false;
    let fetchOk = true;
    let lastGood = Date.now();
    let conn = null;
    let attempt = 0;
    let rebuildTimer = null;
    let throttleTimer = null;
    let inflight = false;
    let pending = false;
    let lastRun = 0;
    let client = null;

    const report = () => {
      let s;
      if (typeof navigator !== 'undefined' && navigator.onLine === false) s = 'offline';
      else if (subscribed && fetchOk) s = 'connected';
      else s = Date.now() - lastGood > 30000 ? 'offline' : 'reconnecting';
      if (s === 'connected') lastGood = Date.now();
      if (s !== conn) { conn = s; if (onConnection) onConnection(s); }
    };

    const pump = async () => {
      if (stopped || inflight || !pending) return;
      const wait = 500 - (Date.now() - lastRun);
      if (wait > 0) {
        if (!throttleTimer) throttleTimer = setTimeout(() => { throttleTimer = null; pump(); }, wait);
        return;
      }
      pending = false;
      inflight = true;
      lastRun = Date.now();
      try {
        await onChange();
        fetchOk = true;
      } catch (e) {
        fetchOk = !isRetryable(e);
      } finally {
        inflight = false;
        report();
        if (pending) pump();
      }
    };
    const request = () => { pending = true; pump(); };

    const onVotes = (payload) => {
      const rid = (payload && ((payload.new && payload.new.round_id) || (payload.old && payload.old.round_id))) || null;
      if (payload && payload.eventType === 'DELETE') this._closedVotes.clear();
      else if (rid) this._closedVotes.delete(rid);
      request();
    };

    const scheduleRebuild = () => {
      if (stopped || rebuildTimer) return;
      const delay = Math.min(1000 * 2 ** attempt, 10000);
      attempt += 1;
      rebuildTimer = setTimeout(() => {
        rebuildTimer = null;
        if (stopped || subscribed) return;
        build();
      }, delay);
    };

    const build = () => {
      if (stopped || !client) return;
      const old = channel;
      channel = null;
      if (old) { try { client.removeChannel(old); } catch (_) { /* ignore */ } }
      const ch = client.channel('plurarch-console-' + Math.random().toString(36).slice(2, 10));
      channel = ch;
      ch.on('postgres_changes', { event: '*', schema: 'public', table: 'votes' }, onVotes)
        .on('postgres_changes', { event: '*', schema: 'public', table: 'agent_events' }, request)
        .on('postgres_changes', { event: '*', schema: 'public', table: 'decisions' }, request)
        .on('postgres_changes', { event: '*', schema: 'public', table: 'rounds' }, request)
        .subscribe((status) => {
          if (ch !== channel || stopped) return; // callback from an old channel
          if (status === 'SUBSCRIBED') {
            subscribed = true;
            attempt = 0;
            report();
            request(); // catch up on anything missed while disconnected
          } else if (status === 'CHANNEL_ERROR' || status === 'TIMED_OUT' || status === 'CLOSED') {
            subscribed = false;
            report();
            scheduleRebuild();
          }
        });
    };

    const safety = setInterval(() => request(), 10000);
    const onOnline = () => { report(); request(); if (!subscribed) { attempt = 0; clearTimeout(rebuildTimer); rebuildTimer = null; build(); } };
    const onOffline = () => report();
    window.addEventListener('online', onOnline);
    window.addEventListener('offline', onOffline);

    report();
    request();
    this.client().then((c) => { client = c; if (!stopped) build(); }).catch(() => { fetchOk = false; report(); scheduleRebuildClient(); });
    const scheduleRebuildClient = () => {
      if (stopped) return;
      setTimeout(() => {
        this.client().then((c) => { client = c; if (!stopped) build(); }).catch(() => scheduleRebuildClient());
      }, 5000);
    };

    return () => {
      stopped = true;
      clearInterval(safety);
      clearTimeout(rebuildTimer);
      clearTimeout(throttleTimer);
      window.removeEventListener('online', onOnline);
      window.removeEventListener('offline', onOffline);
      if (client && channel) { try { client.removeChannel(channel); } catch (_) { /* ignore */ } }
      channel = null;
    };
  }
}

/* ------------------------------------------------------------------ factory */
/**
 * createBackend(role) with role 'participant' | 'console'. Mode from window.PLURARCH_CONFIG.backend.
 */
export function createBackend(role = 'participant') {
  const cfg = (typeof window !== 'undefined' && window.PLURARCH_CONFIG && typeof window.PLURARCH_CONFIG === 'object')
    ? window.PLURARCH_CONFIG : { backend: 'local' };
  return cfg.backend === 'supabase' ? new SupabaseBackend(cfg, role) : new LocalBackend(cfg, role);
}

export function backendConfig() {
  return (typeof window !== 'undefined' && window.PLURARCH_CONFIG) || { backend: 'local' };
}
