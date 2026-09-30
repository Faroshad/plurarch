-- =====================================================================================
-- Plurarch: Supabase schema
-- Tables, vote validation, status view, facilitator RPCs, grants, RLS, realtime.
--
-- How to apply: Supabase Dashboard -> SQL Editor -> New query -> paste this whole file
-- -> Run. It runs as the `postgres` role in one transaction (all or nothing).
-- Re-running is safe: tables/indexes use IF NOT EXISTS, functions/triggers use
-- CREATE OR REPLACE, policies and the view are dropped and re-created, grants are
-- idempotent, and the realtime publication is changed only when a table is missing.
-- (The editor may warn about "destructive operations" because of DROP POLICY / DROP VIEW
-- IF EXISTS. That is expected; confirm and run.)
-- Note: CREATE TABLE IF NOT EXISTS never alters an existing table. If you change a
-- column or constraint here after the first run, write an ALTER TABLE for it.
--
-- Data contract: docs/ARCHITECTURE.md (column names must match it exactly).
-- Postgres 15+ (Supabase projects run 15 or 17).
--
-- Error convention (what clients see through PostgREST / supabase-js):
--   RAISE ... USING ERRCODE = 'PTxyz' makes PostgREST answer with HTTP status xyz, and
--   the error body is { "code": "PTxyz", "message": "<code word>", "details": "...", "hint": null }.
--   Code words (error.message): round_closed (409), round_not_found (400),
--   unknown_question (400), invalid_value (400), invalid_participant (400),
--   round_already_open (409), no_open_round (409), session_not_found (404),
--   not_facilitator (403, SQLSTATE 42501).
--   With a plain INSERT, two concurrent submissions of the same vote can still surface as
--   SQLSTATE 23505 (HTTP 409): treat it as "already recorded". (upsert with
--   ignoreDuplicates never returns 23505.)
-- =====================================================================================

begin;

-- gen_random_uuid() is built into Postgres 13+; no extension needed.

-- -------------------------------------------------------------------------------------
-- 1. Tables
-- -------------------------------------------------------------------------------------

-- One live event / workshop / review. Only the service role (orchestrator) writes it.
create table if not exists public.sessions (
  id          uuid        primary key default gen_random_uuid(),
  title       text        not null default 'Plurarch session',
  use_case    text        not null default 'live_presentation',
  status      text        not null default 'active',
  created_at  timestamptz not null default now(),
  constraint sessions_status_check    check (status in ('active', 'ended')),
  constraint sessions_title_length    check (char_length(title) between 1 and 200),
  constraint sessions_use_case_length check (char_length(use_case) between 1 and 64)
);
create index if not exists sessions_status_created_idx
  on public.sessions (status, created_at desc);

-- The questions of a session, seeded from config/parameters.json when the session is
-- created. The vote trigger validates every vote against these rows.
--   choice: options = JSON array of option values (strings), min/max/step null
--   slider: min, max, step (step > 0), options null
create table if not exists public.questions (
  session_id  uuid    not null references public.sessions (id) on delete cascade,
  key         text    not null,
  type        text    not null,
  options     jsonb,
  min         numeric,
  max         numeric,
  step        numeric,
  position    integer not null default 0,
  primary key (session_id, key),
  constraint questions_key_format check (key ~ '^[a-z][a-z0-9_]{0,63}$'),
  constraint questions_type_check check (type in ('choice', 'slider')),
  -- CASE (not AND) so that jsonb_array_length never sees a non-array, and COALESCE so a
  -- NULL never slips through (a CHECK that evaluates to NULL counts as passed).
  constraint questions_shape_check check (
    case
      when type = 'choice' then
        coalesce(jsonb_typeof(options) = 'array', false)
        and (case when jsonb_typeof(options) = 'array'
                  then jsonb_array_length(options) > 0 else false end)
        and not jsonb_path_exists(coalesce(options, '[]'::jsonb), '$[*] ? (@.type() != "string")')
      when type = 'slider' then
        min is not null and max is not null and step is not null
        and step > 0 and max >= min
      else false
    end
  )
);

-- Voting rounds. Numbered 1, 2, 3... per session; at most one open round per session.
create table if not exists public.rounds (
  id            uuid        primary key default gen_random_uuid(),
  session_id    uuid        not null references public.sessions (id) on delete cascade,
  number        integer     not null,
  status        text        not null default 'open',
  opened_at     timestamptz not null default now(),
  closed_at     timestamptz,
  processed_at  timestamptz,           -- set by the orchestrator once the decision is written
  participants  integer     not null default 0,  -- live count written by the orchestrator (aggregate only)
  constraint rounds_number_positive    check (number >= 1),
  constraint rounds_status_check       check (status in ('open', 'closed')),
  constraint rounds_session_number_key unique (session_id, number)
);
-- Partial unique index: only ONE open round per session (the orchestrator's
-- backend maps a violation of this index to 'round_already_open').
create unique index if not exists rounds_one_open_per_session
  on public.rounds (session_id) where status = 'open';

-- Votes. Browsers insert them (anon key); nobody but facilitators and the service role
-- can read them. One vote per participant per question per round; the first one counts.
create table if not exists public.votes (
  id              bigint      generated always as identity primary key,
  round_id        uuid        not null references public.rounds (id) on delete cascade,
  participant_id  text        not null,   -- random id from the device (localStorage)
  question_key    text        not null,
  value           text        not null,   -- option value ("glass") or number as text ("45", "1.5")
  is_simulated    boolean     not null default false,  -- only the service role may set true
  created_at      timestamptz not null default now(),
  constraint votes_one_per_question      unique (round_id, participant_id, question_key),
  constraint votes_participant_id_length check (char_length(participant_id) between 1 and 64),
  constraint votes_participant_id_chars  check (participant_id !~ '[[:space:][:cntrl:]]'),
  constraint votes_question_key_length   check (char_length(question_key) between 1 and 64),
  constraint votes_value_length          check (char_length(value) between 1 and 32)
);
-- (round_id lookups use the leading column of votes_one_per_question.)

-- The reviewer agent's decision per round. Only the service role writes it.
create table if not exists public.decisions (
  id                       uuid        primary key default gen_random_uuid(),
  session_id               uuid        not null references public.sessions (id) on delete cascade,
  round_id                 uuid        not null references public.rounds (id) on delete cascade,
  round_number             integer,
  status                   text        not null default 'ok',
  verdict                  text,       -- null when status is failed / skipped
  proposal                 jsonb,
  applied_parameters       jsonb,
  evidence                 jsonb,
  alternatives_considered  jsonb       default '[]'::jsonb,
  changes                  jsonb       default '[]'::jsonb,
  rationale                text,
  consistency_note         text,
  message                  text,       -- for failed or skipped decisions
  metrics                  jsonb,      -- { before, proposal, after }
  duration_s               double precision,
  created_at               timestamptz not null default now(),
  constraint decisions_round_unique   unique (round_id),
  constraint decisions_status_check   check (status in ('ok', 'failed', 'skipped')),
  constraint decisions_verdict_check  check (verdict is null or verdict in ('ACCEPTED', 'MODIFIED', 'REJECTED')),
  constraint decisions_ok_has_verdict check (status <> 'ok' or verdict is not null)
);
create index if not exists decisions_session_created_idx
  on public.decisions (session_id, created_at desc);

-- One row per reviewer-agent tool call (live progress + trace). Only the service role writes it.
create table if not exists public.agent_events (
  id          bigint      generated always as identity primary key,
  round_id    uuid        not null references public.rounds (id) on delete cascade,
  seq         integer     not null,     -- 1, 2, 3... within the round
  step        integer,                  -- workflow step 1-6 (0 allowed for "starting")
  tool        text        not null,
  summary     text,
  created_at  timestamptz not null default now(),
  constraint agent_events_round_seq_key      unique (round_id, seq),
  constraint agent_events_seq_nonnegative    check (seq >= 0),
  constraint agent_events_step_range         check (step is null or step between 0 and 6),
  constraint agent_events_tool_length        check (char_length(tool) between 1 and 200),
  constraint agent_events_summary_length     check (summary is null or char_length(summary) <= 4000)
);

-- Allowlist of facilitator accounts (match by auth user id or by e-mail, case-insensitive).
create table if not exists public.facilitators (
  id          bigint      generated always as identity primary key,
  email       text,
  user_id     uuid        references auth.users (id) on delete cascade,
  created_at  timestamptz not null default now(),
  constraint facilitators_identity check (email is not null or user_id is not null)
);
create unique index if not exists facilitators_email_key
  on public.facilitators (lower(email)) where email is not null;
create unique index if not exists facilitators_user_id_key
  on public.facilitators (user_id) where user_id is not null;

comment on table public.sessions     is 'Plurarch: one participatory session. Written by the service role only.';
comment on table public.questions    is 'Plurarch: questions of a session (seeded from config/parameters.json). Votes are validated against it.';
comment on table public.rounds       is 'Plurarch: voting rounds. Opened/closed by facilitators (RPC) or the orchestrator (service role).';
comment on table public.votes        is 'Plurarch: participant votes. Insert-only for browsers; readable by facilitators only.';
comment on table public.decisions    is 'Plurarch: reviewer-agent decision per round. Public read; service role writes.';
comment on table public.agent_events is 'Plurarch: reviewer-agent tool calls. Facilitator read; service role writes.';
comment on table public.facilitators is 'Plurarch: facilitator allowlist (email and/or auth user id).';

-- -------------------------------------------------------------------------------------
-- 2. Helper functions
-- -------------------------------------------------------------------------------------

-- True when the signed-in user is on the facilitators allowlist. SECURITY DEFINER so it
-- can read public.facilitators (which nobody else can read). The e-mail comes from the
-- signed Supabase access token (auth.jwt() ->> 'email'); sign-ups are disabled, so only
-- accounts created by the admin can hold such a token.
create or replace function public.is_facilitator()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.facilitators f
    -- compare as text: never raises, even if a token carries an unexpected "sub"
    where (nullif(auth.jwt() ->> 'sub', '') is not null
           and f.user_id is not null
           and f.user_id::text = auth.jwt() ->> 'sub')
       or (
            nullif(auth.jwt() ->> 'email', '') is not null
            and (auth.jwt() ->> 'is_anonymous') is distinct from 'true'
            and f.email is not null
            and lower(f.email) = lower(auth.jwt() ->> 'email')
          )
  );
$$;
comment on function public.is_facilitator() is
  'Plurarch: true if the current auth user (JWT sub or email) is in public.facilitators.';

-- True when the round exists, is open, and belongs to an active session. SECURITY DEFINER
-- so the votes INSERT policy works without giving anon any read access to rounds.
create or replace function public.round_is_open(p_round_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.rounds r
    join public.sessions s on s.id = r.session_id
    where r.id = p_round_id
      and r.status = 'open'
      and s.status = 'active'
  );
$$;
comment on function public.round_is_open(uuid) is
  'Plurarch: true if the round is open and its session active (used by the votes INSERT policy).';

-- -------------------------------------------------------------------------------------
-- 3. Vote validation (BEFORE INSERT trigger on votes)
-- -------------------------------------------------------------------------------------
-- Runs for every inserted vote, whoever inserts it (anon, authenticated, service role):
--   (a) browser callers (a JWT whose role is not service_role) cannot set is_simulated or
--       created_at: they are forced to false / now();
--   (b) the round must exist, be open and belong to an active session  -> 'round_closed';
--       FOR SHARE on the round row makes a concurrent close wait for in-flight votes, so
--       every accepted vote is committed before the orchestrator sees the round closed;
--   (c) the question must exist in the round's session                -> 'unknown_question';
--   (d) choice: value must be one of the options; slider: a plain decimal number within
--       [min, max] on the step grid (tolerance 1e-6 of a step, for float error). The
--       stored value is normalised to the exact grid value ("1.50000001" -> "1.5",
--       "45.0" -> "45")                                               -> 'invalid_value';
--   (e) a repeated vote (same round, participant, question) is skipped silently (the
--       trigger returns NULL): the first vote counts, and a plain INSERT from the browser
--       behaves like INSERT ... ON CONFLICT DO NOTHING without any read access.
--       The browser therefore uses a plain insert(rows) (never upsert, never .select()).
create or replace function public.votes_before_insert()
returns trigger
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  v_role            text := coalesce(auth.jwt() ->> 'role', '');
  v_round_status    text;
  v_session_id      uuid;
  v_session_status  text;
  v_type            text;
  v_options         jsonb;
  v_min             numeric;
  v_max             numeric;
  v_step            numeric;
  v_num             numeric;
  v_k               numeric;
  v_kr              numeric;
  v_canon           numeric;
begin
  -- (a) trusted columns: only the service role (orchestrator) or a direct database
  --     session without a JWT (SQL editor) may mark a vote as simulated or set created_at.
  if v_role <> '' and v_role <> 'service_role' then
    new.is_simulated := false;
    new.created_at   := now();
  end if;
  new.is_simulated := coalesce(new.is_simulated, false);
  new.created_at   := coalesce(new.created_at, now());

  -- basic shape, with clear messages (the CHECK constraints are the final guard)
  if new.participant_id is null or char_length(new.participant_id) not between 1 and 64
     or new.participant_id ~ '[[:space:][:cntrl:]]' then
    raise exception 'invalid_participant' using errcode = 'PT400',
      detail = 'participant_id must be 1 to 64 characters without spaces.';
  end if;
  new.value := btrim(coalesce(new.value, ''));
  if char_length(new.value) not between 1 and 32 then
    raise exception 'invalid_value' using errcode = 'PT400',
      detail = 'value must be 1 to 32 characters.';
  end if;

  -- (b) round open, session active
  select r.status, r.session_id, s.status
    into v_round_status, v_session_id, v_session_status
    from public.rounds r
    join public.sessions s on s.id = r.session_id
   where r.id = new.round_id
     for share of r;
  if not found then
    raise exception 'round_not_found' using errcode = 'PT400',
      detail = 'No round with this id.';
  end if;
  if v_round_status <> 'open' or v_session_status <> 'active' then
    raise exception 'round_closed' using errcode = 'PT409',
      detail = 'Voting for this round is closed.';
  end if;

  -- (c) question of this session
  select q.type, q.options, q.min, q.max, q.step
    into v_type, v_options, v_min, v_max, v_step
    from public.questions q
   where q.session_id = v_session_id
     and q.key = new.question_key;
  if not found then
    raise exception 'unknown_question' using errcode = 'PT400',
      detail = format('Question "%s" is not part of this session.', left(coalesce(new.question_key, ''), 64));
  end if;

  -- (d) value
  if v_type = 'choice' then
    if not exists (
      select 1 from jsonb_array_elements_text(v_options) as o(val) where o.val = new.value
    ) then
      raise exception 'invalid_value' using errcode = 'PT400',
        detail = format('Not one of the options of "%s".', new.question_key);
    end if;
  elsif v_type = 'slider' then
    -- plain decimal only (no exponent, NaN, Infinity); at most 32 characters in total
    if new.value !~ '^-?[0-9]{1,12}([.][0-9]{1,18})?$' then
      raise exception 'invalid_value' using errcode = 'PT400',
        detail = format('"%s" expects a number.', new.question_key);
    end if;
    v_num := new.value::numeric;
    v_k   := (v_num - v_min) / v_step;      -- grid index, ideally an integer
    v_kr  := round(v_k);
    if abs(v_k - v_kr) > 0.000001 then
      raise exception 'invalid_value' using errcode = 'PT400',
        detail = format('"%s" must be a multiple of %s from %s.', new.question_key, v_step, v_min);
    end if;
    v_canon := v_min + v_kr * v_step;
    if v_kr < 0 or v_canon > v_max then
      raise exception 'invalid_value' using errcode = 'PT400',
        detail = format('"%s" must be between %s and %s.', new.question_key, v_min, v_max);
    end if;
    new.value := trim_scale(v_canon)::text;   -- canonical text, e.g. '1.5', '45'
  else
    raise exception 'invalid_question' using errcode = 'PT400',
      detail = 'Unsupported question type.';
  end if;

  -- (e) first vote counts: skip a repeated vote silently
  if exists (
    select 1 from public.votes v
     where v.round_id = new.round_id
       and v.participant_id = new.participant_id
       and v.question_key = new.question_key
  ) then
    return null;
  end if;

  return new;
end;
$$;

create or replace trigger votes_before_insert
  before insert on public.votes
  for each row execute function public.votes_before_insert();

-- -------------------------------------------------------------------------------------
-- 4. Small row triggers for rounds and decisions
-- -------------------------------------------------------------------------------------

-- Closing a round without closed_at fills it with the database clock (the orchestrator's
-- backend sends only {status: 'closed'} so the server time is used).
create or replace function public.rounds_before_update()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.status = 'closed' and old.status = 'open' and new.closed_at is null then
    new.closed_at := now();
  end if;
  return new;
end;
$$;

create or replace trigger rounds_before_update
  before update on public.rounds
  for each row execute function public.rounds_before_update();

-- A decision row may omit session_id / round_number: they are copied from the round.
-- NULL alternatives_considered / changes become empty arrays.
create or replace function public.decisions_before_insert()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_session_id uuid;
  v_number     integer;
begin
  if new.session_id is null or new.round_number is null then
    select r.session_id, r.number
      into v_session_id, v_number
      from public.rounds r
     where r.id = new.round_id;
    new.session_id   := coalesce(new.session_id, v_session_id);
    new.round_number := coalesce(new.round_number, v_number);
  end if;
  new.alternatives_considered := coalesce(new.alternatives_considered, '[]'::jsonb);
  new.changes                 := coalesce(new.changes, '[]'::jsonb);
  return new;
end;
$$;

create or replace trigger decisions_before_insert
  before insert on public.decisions
  for each row execute function public.decisions_before_insert();

-- -------------------------------------------------------------------------------------
-- 5. Facilitator RPCs: open_round / close_round
-- -------------------------------------------------------------------------------------
-- supabase-js (signed in):  rpc('open_round', { p_session_id })  -> the new round row
--                           rpc('close_round', { p_session_id }) -> the closed round row
-- The orchestrator (service role) does NOT use these (they require a facilitator JWT);
-- it writes the rounds table directly (orchestrator/backend_supabase.py).

create or replace function public.open_round(p_session_id uuid)
returns public.rounds
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  v_session_id uuid;
  v_next       integer;
  v_round      public.rounds;
begin
  if not public.is_facilitator() then
    raise exception 'not_facilitator' using errcode = '42501',
      detail = 'Only facilitators can open rounds.';
  end if;

  -- lock the session row: serialises concurrent open/close calls for this session
  select s.id into v_session_id
    from public.sessions s
   where s.id = p_session_id and s.status = 'active'
     for no key update;
  if not found then
    raise exception 'session_not_found' using errcode = 'PT404',
      detail = 'No active session with this id.';
  end if;

  if exists (select 1 from public.rounds r where r.session_id = p_session_id and r.status = 'open') then
    raise exception 'round_already_open' using errcode = 'PT409',
      detail = 'Close the open round first.';
  end if;

  select coalesce(max(r.number), 0) + 1 into v_next
    from public.rounds r
   where r.session_id = p_session_id;

  begin
    insert into public.rounds (session_id, number, status, opened_at)
    values (p_session_id, v_next, 'open', now())
    returning * into v_round;
  exception when unique_violation then
    -- someone (e.g. the orchestrator) opened a round at the same moment
    raise exception 'round_already_open' using errcode = 'PT409',
      detail = 'Another round was opened at the same time.';
  end;

  return v_round;
end;
$$;

create or replace function public.close_round(p_session_id uuid)
returns public.rounds
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  v_session_id uuid;
  v_round      public.rounds;
begin
  if not public.is_facilitator() then
    raise exception 'not_facilitator' using errcode = '42501',
      detail = 'Only facilitators can close rounds.';
  end if;

  select s.id into v_session_id
    from public.sessions s
   where s.id = p_session_id
     for no key update;
  if not found then
    raise exception 'session_not_found' using errcode = 'PT404',
      detail = 'No session with this id.';
  end if;

  update public.rounds r
     set status = 'closed',
         closed_at = now()
   where r.session_id = p_session_id
     and r.status = 'open'
  returning r.* into v_round;
  if not found then
    raise exception 'no_open_round' using errcode = 'PT409',
      detail = 'There is no open round to close.';
  end if;

  return v_round;
end;
$$;

comment on function public.open_round(uuid)  is 'Plurarch: facilitator opens the next round (1, 2, 3...). Raises round_already_open.';
comment on function public.close_round(uuid) is 'Plurarch: facilitator closes the open round. Raises no_open_round.';

-- -------------------------------------------------------------------------------------
-- 6. Public status view
-- -------------------------------------------------------------------------------------
-- Participant devices poll ONLY this view (every few seconds, with jitter), and fetch a
-- full decision only when latest_decision_id changes.
--
-- Security choice: a SECURITY INVOKER view (Supabase's recommendation; no Security Advisor
-- warning). It runs with the reader's own rights, so anon gets column-level SELECT on the
-- non-sensitive status columns of sessions and rounds, limited by RLS to ACTIVE sessions
-- (section 7). It never touches votes, agent_events or facilitators.
-- If you add a column, keep it non-sensitive (never counts per participant, never votes),
-- and grant that column to anon in section 7.
drop view if exists public.session_status;
create view public.session_status
  with (security_invoker = true)
as
select
  s.id           as session_id,
  s.title        as session_title,
  s.use_case     as use_case,
  cr.id          as round_id,
  cr.number      as round_number,
  cr.status      as round_status,
  cr.opened_at   as round_opened_at,
  cr.closed_at   as round_closed_at,
  cr.participants as round_participants,
  ld.id          as latest_decision_id,
  greatest(s.created_at, cr.opened_at, cr.closed_at, ld.created_at) as updated_at
from public.sessions s
left join lateral (
  select r.id, r.number, r.status, r.opened_at, r.closed_at, r.participants
    from public.rounds r
   where r.session_id = s.id
   order by r.number desc
   limit 1
) cr on true
left join lateral (
  select d.id, d.created_at
    from public.decisions d
   where d.session_id = s.id
   order by d.created_at desc, d.round_number desc nulls last
   limit 1
) ld on true
where s.status = 'active'
  and s.use_case <> 'health_check'   -- health-check's temporary session is never shown to devices
order by s.created_at desc;

comment on view public.session_status is
  'Plurarch: one row per active session (newest round + latest decision id). Public, polled by participant devices.';

-- -------------------------------------------------------------------------------------
-- 7. Grants (minimal) and row level security
-- -------------------------------------------------------------------------------------
-- Supabase's default privileges give anon/authenticated ALL on new tables in public, and
-- EXECUTE on new functions. Start from nothing and grant back only what is needed.
-- (TRUNCATE is not subject to RLS, so removing it matters.)
revoke all on table
  public.sessions, public.questions, public.rounds, public.votes,
  public.decisions, public.agent_events, public.facilitators
  from anon, authenticated;
revoke all on table public.session_status from anon, authenticated;

-- Public (participant devices, anon key; also signed-in browsers).
grant select on table public.session_status to anon, authenticated;
grant select on table public.decisions      to anon, authenticated;
grant select on table public.questions      to anon, authenticated;   -- question definitions are public anyway
grant insert on table public.votes          to anon, authenticated;
-- The status columns read by the (security invoker) session_status view. RLS limits them to
-- active sessions. No processed_at, no votes.
grant select (id, title, use_case, status, created_at) on table public.sessions to anon;
grant select (id, session_id, number, status, opened_at, closed_at, participants) on table public.rounds to anon;
-- Browsers use a plain insert (the trigger skips repeated votes), so anon needs NO select
-- privilege on votes at all. Never use upsert/ON CONFLICT or .select() from the browser.

-- Facilitator console (authenticated; RLS narrows to is_facilitator()).
grant select on table public.sessions, public.rounds, public.votes, public.agent_events to authenticated;

-- Orchestrator (service role bypasses RLS; keep full table privileges explicit).
grant all on table
  public.sessions, public.questions, public.rounds, public.votes,
  public.decisions, public.agent_events, public.facilitators
  to service_role;
grant select on table public.session_status to service_role;

-- Functions: revoke the default EXECUTE, then grant only what is used.
revoke all on function public.is_facilitator()          from public, anon, authenticated;
revoke all on function public.round_is_open(uuid)       from public, anon, authenticated;
revoke all on function public.open_round(uuid)          from public, anon, authenticated;
revoke all on function public.close_round(uuid)         from public, anon, authenticated;
revoke all on function public.votes_before_insert()     from public, anon, authenticated;
revoke all on function public.rounds_before_update()    from public, anon, authenticated;
revoke all on function public.decisions_before_insert() from public, anon, authenticated;

grant execute on function public.is_facilitator()    to anon, authenticated, service_role;  -- false for anon
grant execute on function public.round_is_open(uuid) to anon, authenticated, service_role;  -- used by the votes INSERT policy
grant execute on function public.open_round(uuid)    to authenticated;
grant execute on function public.close_round(uuid)   to authenticated;
-- Trigger functions need no EXECUTE grant: triggers fire regardless.

-- RLS on EVERY table.
alter table public.sessions     enable row level security;
alter table public.questions    enable row level security;
alter table public.rounds       enable row level security;
alter table public.votes        enable row level security;
alter table public.decisions    enable row level security;
alter table public.agent_events enable row level security;
alter table public.facilitators enable row level security;

-- Who may do what (service_role bypasses RLS and is the only writer of sessions,
-- questions, rounds (besides the RPCs), decisions and agent_events):
--
--   table         anon / any browser               facilitator (is_facilitator())
--   ------------  -------------------------------  ------------------------------
--   sessions      SELECT status columns, active     SELECT
--   questions     SELECT                           SELECT
--   rounds        SELECT status columns, active     SELECT (+ open/close via RPC)
--   votes         INSERT while round open          SELECT (+ INSERT like anyone)
--   decisions     SELECT                           SELECT
--   agent_events  -                                SELECT
--   facilitators  -                                - (use rpc is_facilitator)
--   session_status (view)  SELECT                  SELECT
--
-- A signed-in account that is NOT in facilitators gets exactly the anon rights.

-- sessions
drop policy if exists sessions_select_facilitator on public.sessions;
create policy sessions_select_facilitator on public.sessions
  for select to authenticated
  using ((select public.is_facilitator()));

-- questions
drop policy if exists questions_select_public on public.questions;
create policy questions_select_public on public.questions
  for select to anon, authenticated
  using (true);

-- sessions and rounds: everyone may read the status of ACTIVE sessions (the public status
-- view needs it; anon only has the status columns granted)
drop policy if exists sessions_select_active on public.sessions;
create policy sessions_select_active on public.sessions
  for select to anon, authenticated
  using (status = 'active');

-- rounds
drop policy if exists rounds_select_facilitator on public.rounds;
create policy rounds_select_facilitator on public.rounds
  for select to authenticated
  using ((select public.is_facilitator()));

drop policy if exists rounds_select_active on public.rounds;
create policy rounds_select_active on public.rounds
  for select to anon, authenticated
  using (exists (select 1 from public.sessions s where s.id = rounds.session_id and s.status = 'active'));

-- votes: insert only while the round is open (the trigger enforces the same rule with a
-- clear 'round_closed' error, plus value validation).
drop policy if exists votes_insert_open_round on public.votes;
create policy votes_insert_open_round on public.votes
  for insert to anon, authenticated
  with check (public.round_is_open(round_id));

-- votes: facilitators read everything.
drop policy if exists votes_select_facilitator on public.votes;
create policy votes_select_facilitator on public.votes
  for select to authenticated
  using ((select public.is_facilitator()));

-- votes: no SELECT policy for anon at all (an earlier draft had a scoped one for upsert;
-- dropped so re-running this script on an older database removes it).
drop policy if exists votes_select_row_being_inserted on public.votes;

-- decisions: public read (participants see verdicts)
drop policy if exists decisions_select_public on public.decisions;
create policy decisions_select_public on public.decisions
  for select to anon, authenticated
  using (true);

-- agent_events: facilitators only
drop policy if exists agent_events_select_facilitator on public.agent_events;
create policy agent_events_select_facilitator on public.agent_events
  for select to authenticated
  using ((select public.is_facilitator()));

-- facilitators: RLS enabled, no policies, no grants -> invisible to every API role.

-- -------------------------------------------------------------------------------------
-- 8. Realtime (facilitator console; participants never subscribe)
-- -------------------------------------------------------------------------------------
-- postgres_changes respects RLS: the console receives votes / agent_events / rounds only
-- while signed in as a facilitator (decisions are public anyway).
do $$
declare
  t text;
begin
  if not exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    create publication supabase_realtime;
  end if;
  if (select p.puballtables from pg_publication p where p.pubname = 'supabase_realtime') then
    raise notice 'supabase_realtime already publishes all tables; nothing to add.';
    return;
  end if;
  foreach t in array array['votes', 'agent_events', 'decisions', 'rounds'] loop
    if not exists (
      select 1 from pg_publication_tables
       where pubname = 'supabase_realtime'
         and schemaname = 'public'
         and tablename = t
    ) then
      execute format('alter publication supabase_realtime add table public.%I', t);
    end if;
  end loop;
end
$$;

commit;

-- =====================================================================================
-- After running: add the facilitator (see supabase/SETUP.md), e.g.
--   insert into public.facilitators (email) values ('YOUR-EMAIL@example.edu') on conflict do nothing;
--
-- Quick verification queries (run them separately in the SQL editor):
--   select tablename, rowsecurity from pg_tables where schemaname = 'public' order by 1;
--   select tablename, policyname, cmd, roles from pg_policies where schemaname = 'public' order by 1, 2;
--   select * from pg_publication_tables where pubname = 'supabase_realtime' order by tablename;
--   select grantee, table_name, privilege_type from information_schema.role_table_grants
--    where table_schema = 'public' and grantee in ('anon', 'authenticated') order by 2, 1, 3;
-- End-to-end access tests: .venv\Scripts\python.exe tests\test_rls.py
-- =====================================================================================

-- =====================================================================================
-- Auth settings notes (dashboard, not SQL; step-by-step in supabase/SETUP.md)
-- =====================================================================================
-- * Authentication -> Sign In / Providers:
--     - "Allow new users to sign up": OFF. Only accounts the admin creates can sign in.
--       Nobody can register an account, so nobody can obtain a token for an allowlisted
--       e-mail except the real facilitator.
--     - Email provider: ON (the console signs in with e-mail + password).
--     - "Confirm email": either setting works; admin-created users are created with
--       "Auto Confirm User" ticked.
--     - "Allow anonymous sign-ins": OFF. Participants do not sign in at all; they use the
--       publishable (anon) key, which maps to the Postgres role `anon`.
-- * Authentication -> Users -> Add user -> Create new user: the facilitator's e-mail and
--   password, "Auto Confirm User" ticked. Then add the e-mail to public.facilitators.
--   Any other account (e.g. a test user) has no extra rights unless it is in that table.
-- * Authentication -> URL Configuration: Site URL = https://faroshad.github.io/plurarch/
--   (password sign-in needs no redirect; the Site URL is used in any auth e-mails).
-- * Keys: the publishable key (sb_publishable_..., or the legacy "anon" JWT) goes into
--   site/config.js; it is public by design, since security comes from the grants and RLS
--   above. The secret key (sb_secret_..., or the legacy "service_role" JWT) goes only into
--   .env as SUPABASE_SERVICE_ROLE_KEY, used by the orchestrator. Never put it in the site,
--   never commit it.
-- * The access token lifetime (JWT expiry, default 3600 s) is fine; supabase-js refreshes
--   it automatically while the console is open.
-- * Free plan: the project pauses after about a week of inactivity (the API then answers
--   HTTP 540 or the host stops resolving). Open the dashboard and click "Restore project"
--   a day before the session, and run the orchestrator's health-check on the day.
--   Realtime: max 200 concurrent connections, which is fine because only the console
--   (and optionally the orchestrator) subscribe.
-- * API -> "Max rows" (default 1000) caps every REST response; the orchestrator's backend
--   paginates, and the console should page or filter votes by round.
-- =====================================================================================
