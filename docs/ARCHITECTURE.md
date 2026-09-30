# Plurarch architecture and data contract

This file is the contract between the web apps (`site/`), the backends (local server and
Supabase) and the orchestrator. If you change a shape here, change every side.

## Two backends, one data model

| | Local mode (`backend: "local"`) | Supabase mode (`backend: "supabase"`) |
|---|---|---|
| Who serves the site | `orchestrator.py run` (http://<laptop-ip>:8787) | GitHub Pages |
| Data store | SQLite at `<state_dir>/plurarch.db` | Supabase Postgres |
| Participant → backend | `fetch` to `/api/...` (same origin) | supabase-js (REST only, never realtime) |
| Console live updates | polls `/api/console/state` every 1 s | supabase-js realtime + a slow safety refetch |
| Facilitator auth | facilitator key (printed by `run`) | Supabase email + password, `facilitators` allowlist |

Local mode needs no accounts: laptop and phones on the same Wi-Fi. Supabase mode works over the
internet. The site picks the mode from `window.PLURARCH_CONFIG.backend`. The local server serves a
generated `/config.js` with `backend: "local"`; GitHub Pages serves the static `site/config.js`
with `backend: "supabase"`.

Config files are fetched by the site at runtime from `config/…` relative to the page:
- local server: `/config/*` maps to the repo `config/` folder
- GitHub Pages: the deploy workflow copies `config/` into the published site as `config/`

## Tables (same columns in SQLite and Postgres)

`sessions`: `id` (uuid text), `title`, `use_case`, `status` (`active` | `ended`), `created_at`

`questions`: `session_id`, `key`, `type` (`choice` | `slider`), `options` (json array of option
values, choice only), `min`, `max`, `step` (slider only), `position`. Seeded from
`config/parameters.json` when a session is created. The database validates votes against it.

`rounds`: `id` (uuid text), `session_id`, `number` (1, 2, 3…), `status` (`open` | `closed`),
`opened_at`, `closed_at`, `processed_at` (set by the orchestrator once the round's decision is written)

`votes`: `id`, `round_id`, `participant_id` (random id from the device), `question_key`, `value`
(text: an option value like `"glass"`, or a number as text like `"45"` / `"1.5"`), `is_simulated`
(bool), `created_at`. Unique on (`round_id`, `participant_id`, `question_key`). First vote counts.

`decisions`: `id` (uuid text), `session_id`, `round_id`, `round_number`, `status` (`ok` | `failed` |
`skipped`), `verdict` (`ACCEPTED` | `MODIFIED` | `REJECTED` | null when failed/skipped), `proposal`
(json), `applied_parameters` (json), `evidence` (json), `alternatives_considered` (json array),
`changes` (json array), `rationale` (text), `consistency_note` (text), `message` (text, for failed or
skipped decisions), `metrics` (json), `duration_s` (number), `created_at`

`agent_events`: `id`, `round_id`, `seq` (1, 2, 3…), `step` (1–6), `tool`, `summary`, `created_at`

`facilitators` (Supabase only): `email`, `user_id`

`session_status` (view, one row per active session): `session_id`, `session_title`, `use_case`,
`round_id`, `round_number`, `round_status`, `round_opened_at`, `round_closed_at`,
`round_participants`, `latest_decision_id`, `updated_at`. The newest round of the session is the
current round.

`round_participants` / `rounds.participants` is the live number of distinct participants of the
round. It is written by the orchestrator every ~2.5 s while the round is open, and finally when it
closes. It is an aggregate number only; phones can never read votes.

## JSON shapes

Parameters object (keys from `config/parameters.json`):
```json
{ "facade_material": "timber", "window_ratio": 40, "roof_angle": 15, "canopy_depth": 1.5 }
```

Metrics object (from `design_mcp/metrics.py`; all indicative):
```json
{ "daylight": 67.5, "cooling": 52.3, "cost": 90.9, "carbon": 58.2, "shading": 0.349 }
```

`decisions.metrics`:
```json
{ "before": {metrics}, "proposal": {metrics}, "after": {metrics} }
```
`before` = the design before this round, `proposal` = what participants voted for, `after` = what is
applied now (equals `before` when REJECTED or failed).

`decisions.proposal` (built by the orchestrator's tally):
```json
{
  "session_id": "…", "round_id": "…", "round_number": 2,
  "parameters": { "facade_material": "glass", "window_ratio": 45, "roof_angle": 15, "canopy_depth": 1.5 },
  "current_parameters": { … },
  "tally": {
    "facade_material": { "type": "choice", "n": 38, "winner": "glass",
      "counts": { "timber": 10, "concrete": 5, "glass": 23 },
      "percentages": { "timber": 26.3, "concrete": 13.2, "glass": 60.5 },
      "consensus": 0.605, "consensus_level": "moderate", "leading_options": ["glass"] },
    "window_ratio": { "type": "slider", "n": 37, "median": 45,
      "counts": { "20": 1, "25": 0, "30": 2, "35": 4, "40": 9, "45": 12, "50": 6, "55": 2, "60": 1 },
      "min": 20, "max": 60, "q1": 40, "q3": 50,
      "consensus": 0.73, "consensus_level": "strong" }
  },
  "participation": { "participants": 40, "votes": 150, "by_question": { "facade_material": 38, … } },
  "previous_decisions": [
    { "round_number": 1, "status": "ok", "verdict": "ACCEPTED",
      "applied_parameters": { … }, "rationale": "…" }
  ]
}
```
`consensus_level` is `strong` (≥ brief.consensus.strong), `weak` (< brief.consensus.weak) or `moderate`.
A question with no votes has `"n": 0` and the proposal keeps the current value.

`alternatives_considered`: `[{ "parameters": {…}, "metrics": {…}, "hard_rules_pass": true, "note": "…" }]`

`changes`: `[{ "parameter": "window_ratio", "from": 20, "to": 25, "reason": "…" }]`

`evidence`: `{ "proposal_metrics": {…}, "applied_metrics": {…}, "failed_rules": ["…"], "failed_goals": ["…"] }`

## Local API (served by `orchestrator.py run`, default port 8787)

Static:
- `GET /config.js` → `window.PLURARCH_CONFIG = { backend: "local", useCase: "live_presentation", apiBase: "" }`
- `GET /config/<file>` → repo `config/` files
- everything else → `site/` files (`/` = `index.html`)

Public (participants):
- `GET /api/status` → the `session_status` row, or `null` if there is no active session
- `GET /api/decisions/<id>` → one decision row
- `POST /api/votes` with body
  `{ "round_id": "…", "participant_id": "…", "votes": [ { "question_key": "window_ratio", "value": "45" }, … ] }`
  → `200 { "inserted": 4, "duplicates": 0 }`, `400 { "error": "invalid_value", "detail": "…" }`,
  `409 { "error": "round_closed" }`

Facilitator (header `Authorization: Bearer <facilitator key>`; 401 otherwise):
- `GET /api/console/state?round_id=<optional>` →
  ```json
  { "session": {…}, "questions": […], "rounds": […],
    "round": {the selected round, default the newest},
    "votes": [ votes of the selected round ],
    "agent_events": [ events of the selected round ],
    "decisions": [ all decisions of the session, oldest first ],
    "participants_total": 57, "server_time": "…" }
  ```
- `POST /api/rounds/open` → `{ "round": {…} }` (409 `round_already_open`)
- `POST /api/rounds/close` → `{ "round": {…} }` (409 `no_open_round`)

## Supabase access (supabase-js)

Participants (anon key):
- status: `from('session_status').select('*').limit(1).maybeSingle()`
- decision: `from('decisions').select('*').eq('id', id).single()`
- vote: `from('votes').insert(rows)` (plain insert: never upsert, never `.select()`; with RLS,
  ON CONFLICT would need a SELECT policy). Each row is `{ round_id, participant_id, question_key, value }`.
  The database trigger skips repeated votes silently; treat error 23505 (HTTP 409, a race) as
  "already recorded". Errors carry a code word in `message`: `round_closed` (409),
  `invalid_value` / `unknown_question` / `round_not_found` / `invalid_participant` (400).
- anon cannot read `rounds` or `sessions`: participants use only `session_status` and `decisions`.

Facilitator (signed in with `auth.signInWithPassword`):
- `rpc('is_facilitator')` → boolean
- `rpc('open_round', { p_session_id })`, `rpc('close_round', { p_session_id })` → the round row
- reads: `sessions`, `questions`, `rounds`, `votes`, `agent_events`, `decisions`
- realtime: `postgres_changes` on `votes`, `agent_events`, `decisions`, `rounds`

Orchestrator: service role key over PostgREST (never in the browser).

## Reviewer-agent workflow steps (drive the 6-step progress bar)

1. `get_project_brief` · 2. evaluate the proposal · 3. evaluate alternatives · 4. decide ·
5. `set_parameters` · 6. verify (`get_parameters` / `evaluate` after applying)

`agent_events.step` holds the step number. The orchestrator writes a first event
(`tool: "orchestrator"`, step 0: the tally) before the agent starts, and a final event
(`tool: "decision"`, step 6) when the decision is stored.

For a REJECTED decision, `decisions.message` holds the agent's "what to vote for instead" advice.
For failed or skipped decisions, `message` explains why and `verdict` is null.
