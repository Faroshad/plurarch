-- =====================================================================================
-- Plurarch migration 002: make session_status a normal (security invoker) view.
-- Run once in the SQL Editor if you already ran schema.sql before 2026-09-30 15:00.
-- (schema.sql already contains this end state for new projects.) Safe to re-run.
--
-- Before: a SECURITY DEFINER view, flagged by the Security Advisor and shown as
-- "Unrestricted" in the Table Editor.
-- After:  the view runs with the reader's own rights. Anonymous devices may read ONLY the
-- non-sensitive status columns of ACTIVE sessions and their rounds (title, round number,
-- open/closed, times), exactly what the status view shows anyway. Votes, agent events and
-- the facilitator list stay private.
-- =====================================================================================
begin;

-- column-level read access for anonymous devices (the status columns only)
grant select (id, title, use_case, status, created_at) on table public.sessions to anon;
grant select (id, session_id, number, status, opened_at, closed_at) on table public.rounds to anon;

-- rows: only active sessions and the rounds of active sessions
drop policy if exists sessions_select_active on public.sessions;
create policy sessions_select_active on public.sessions
  for select to anon, authenticated
  using (status = 'active');

drop policy if exists rounds_select_active on public.rounds;
create policy rounds_select_active on public.rounds
  for select to anon, authenticated
  using (exists (select 1 from public.sessions s where s.id = rounds.session_id and s.status = 'active'));

-- the view, now security invoker
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
  ld.id          as latest_decision_id,
  greatest(s.created_at, cr.opened_at, cr.closed_at, ld.created_at) as updated_at
from public.sessions s
left join lateral (
  select r.id, r.number, r.status, r.opened_at, r.closed_at
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
  'Plurarch: one row per active session (newest round + latest decision id). Security invoker; polled by participant devices.';

revoke all on table public.session_status from anon, authenticated;
grant select on table public.session_status to anon, authenticated, service_role;

commit;
