-- =====================================================================================
-- Plurarch migration 003: a live participant count per round, for phones and the stage.
-- Run once in the SQL Editor (after 002). Safe to re-run. schema.sql already contains it.
--
-- Phones cannot read votes (RLS). While a round is open, the orchestrator (service role)
-- writes the round's number of distinct participants into rounds.participants every few
-- seconds; anonymous devices read it through session_status.round_participants.
-- It is an aggregate number only: it reveals nothing about any vote.
-- =====================================================================================
begin;

alter table public.rounds add column if not exists participants integer not null default 0;
grant select (participants) on table public.rounds to anon;

drop view if exists public.session_status;
create view public.session_status
  with (security_invoker = true)
as
select
  s.id             as session_id,
  s.title          as session_title,
  s.use_case       as use_case,
  cr.id            as round_id,
  cr.number        as round_number,
  cr.status        as round_status,
  cr.opened_at     as round_opened_at,
  cr.closed_at     as round_closed_at,
  cr.participants  as round_participants,
  ld.id            as latest_decision_id,
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
  and s.use_case <> 'health_check'
order by s.created_at desc;

comment on view public.session_status is
  'Plurarch: one row per active session (newest round, its live participant count, latest decision id). Security invoker.';

revoke all on table public.session_status from anon, authenticated;
grant select on table public.session_status to anon, authenticated, service_role;

commit;
