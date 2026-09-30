# Plurarch progress log

New session? Read `SPEC.md`, then this file, then `docs/ARCHITECTURE.md` (the data contract).

## 2026-09-30: fast first version (all milestones built in one pass, at the user's request)

The user asked for an early version quickly rather than stopping after each milestone. Defaults used
where the user had not answered yet (all changeable):
- about 100 participants, designed for 300
- a hot, sunny climate
- facilitator `YOUR-EMAIL@example.edu`
- repo `Faroshad/plurarch` (public, for free GitHub Pages)

### Decisions that deviate from or add to SPEC.md (and why)
- **Local mode** (`config/local.json` `"backend": "local"`): `orchestrator.py run` serves the site
  and a small local API from the laptop (SQLite in `state/plurarch.db`). The whole loop then works with
  no accounts, phones on the same Wi-Fi. Supabase mode is the internet path (and stays the default for
  GitHub Pages). Same data model in both (docs/ARCHITECTURE.md).
- A `questions` table (seeded from `config/parameters.json` per session) so the database can
  validate votes without duplicating config.
- `decisions` has extra columns: `session_id`, `round_number`, `changes`, `consistency_note`,
  `message`, `metrics` (before/proposal/after, recomputed by the orchestrator), `duration_s`.
- The evaluate cap is 5 exploratory calls. One extra verification evaluate is allowed after
  `set_parameters`, or (REJECTED) of the unchanged design after `get_parameters`.
- REJECTED never calls `set_parameters`.
- "Reasonable modification" is defined in the brief: at most 2 changed parameters, 3 slider steps
  each, and the material changes only to a leading option under weak consensus. Nothing valid within
  these limits means REJECTED.
- Strong-consensus values change only when a hard rule forces it. Goal problems are fixed through
  the other parameters.
- Slider consensus is the share of votes within one step of the median.
- `agent/mcp.json`, `.mcp.json` and `config/local.json` are generated with absolute paths by
  `tools/setup_local.py` and git-ignored. They are machine-specific.
- `mcp` 2.x renamed FastMCP to `MCPServer` (`from mcp.server.mcpserver import MCPServer`); same
  decorator style.

### Headless agent isolation (verified 2026-09-30, Claude Code 2.1.285)
Flags in `orchestrator/agent_runner.py`:
- `-p --output-format stream-json --verbose`
- `--system-prompt <SYSTEM_PROMPT.md>`
- `--settings {"claudeMdExcludes": [...]}` (removes the user's global CLAUDE.md and AGENTS.md)
- `--mcp-config <per-run> --strict-mcp-config`
- `--tools ""` (no built-in tools)
- `--allowedTools mcp__design__*` (five names)
- `--permission-mode dontAsk`
- `--json-schema <decision_schema>` (draft-07; the CLI's validator rejects 2020-12)
- `--no-session-persistence`

The working folder is `state/agent_work`. Probes confirmed that the agent sees only the 5 design
tools and no CLAUDE.md. `--safe-mode` is NOT usable: it also drops `--mcp-config` servers. `--bare`
needs an API key. The structured record arrives as `structured_output` in the final `result` event.
Typical run: 10–22 s on Sonnet, about $0.08 per round.

### Status by milestone
| Milestone | Status |
|---|---|
| 0 · config, metrics, design-mcp, Grasshopper, headless smoke test | **Done.** `grasshopper/plurarch.gh` is wired in Rhino 8 (a loader component execs `model_builder.py`, a Timer at 500 ms, Custom Preview). The model changed on its own when the agent applied a decision. |
| 1 · Supabase schema, RLS, auth | Written (`supabase/schema.sql`, `SETUP.md`, `tests/test_rls.py`). **Not run yet: needs the user's Supabase project.** |
| 2 · Participant app, console, Pages, QR | **Done in local mode.** Headless Edge vote → local API → DB verified; the console renders real decisions; projection mode has no text under 14 px (checked). Pages workflow written but not pushed. Real phones not tested yet. |
| 3 · Orchestrator, simulate, health-check, test-agent | **Done in local mode.** Rounds of every profile gave the expected verdicts. A restart did not reprocess rounds (the processed_at + unique decision per round guard). |
| 4 · Judgment suite, load test | **Done.** Judgment: 30/30 runs as expected, 100% consistency, every rationale cites real tool numbers (`tests/judgment/report.md`). Load: 300 devices, 100% success, p95 < 10 ms (localhost; not over Wi-Fi). |
| 5 · README and checklists | **Done** (README.md), apart from the Supabase-mode steps being verified. |

### Notes from integration (2026-09-30)
- Supabase votes use a **plain insert**. The trigger skips repeat votes silently, and anon has no SELECT
  on votes at all. An upsert with ignoreDuplicates would need a SELECT policy under RLS.
- Test sessions (`use_case` `rls_test` / `health_check`) are hidden from the status view, the console
  and the orchestrator.
- Headless-Edge screenshots with `--screenshot --timeout` freeze CSS animations (the cards look faded
  or empty). A CDP probe confirmed that nothing re-mounts in a real browser. Use CDP scripts
  (scratchpad `cdp_probe.py`, `cdp_vote.py`) for UI checks, not screenshot mode.
- The web builder's test cleanup ran `taskkill msedge.exe`, which may have closed the user's Edge
  windows once.

### How to run (local mode)
```
.venv\Scripts\python.exe tools\setup_local.py            # once per machine
.venv\Scripts\python.exe orchestrator\orchestrator.py run
```
It prints the participant URL (LAN IP) and the console URL (with the facilitator key).

## 2026-09-30 (later): v2, a detailed model, a phone 3D view, a walkthrough and question pins

The user tested v1 and asked for more:
- a much more detailed model, inside and out;
- on the phone, a textured 3D model to rotate and a virtual walkthrough of the interior;
- every question as a pin on the element it is about, opening the same question UI.

This is an explicit feature request after Milestone 5.

Design (the contract is `docs/MODEL.md`):
- **One geometry generator in JavaScript**, `site/js/model/pavilion.js`, used by both the phone and
  Rhino. Rhino calls `node tools/pavilion_cli.mjs` and falls back to the built-in Python generator
  if Node fails. The projector and the phones therefore show the same model.
- **No backend change:** phones build the model from `applied_parameters` (or from their own draft,
  for a live preview of what they vote for). This works the same in local and Supabase mode.
- The phone viewer is `site/js/viewer3d.js`: three.js WebGL2, loaded lazily. It has:
  - procedural canvas textures, with no asset downloads;
  - orbit mode and a tour mode (stops, look around, tap to walk);
  - HTML pins with occlusion;
  - render on demand;
  - the existing stepper kept as "List view" and as the fallback.

**Status: done and tested (2026-09-30)**, except on real phones.
- **The model:** 815–1,125 parts and 32 materials, detailed inside and out. It builds in 0.85 ms
  mean in Node. It has 27 pins (7 exterior, 20 interior), and every tour stop sees at least 2–4
  questions (tested across all 1,512 designs).
- **Rhino** rebuilds from the same generator in 210–350 ms. If Node fails, it falls back to the
  simpler Python model with a warning.
  **Grasshopper must stay open (minimised is fine)**, or Rhino shows no preview.
- **The phone** uses three.js 0.186.1 from jsDelivr (about 205 KB brotli). The model is 11–14k
  triangles in 30 draw calls, and a rebuild takes 2–12 ms. It draws 0 frames when idle.
  - It falls back to the list view without WebGL2 or an import map, or if the CDN fails.
  - Tested in headless Edge (SwiftShader) against the real server: voting through pins, the live
    preview, the tour, decisions, and votes stored in the DB.
- **Tests:** `node tests/model/test_pavilion.mjs` (28), `python -m unittest discover -s tests` (35),
  `python grasshopper/model_builder.py --selftest` (67).
- **Phones need internet** for the three.js CDN, even in local mode. Without it, they fall back to
  the list view.

## 2026-09-30 (evening): online. Supabase and GitHub Pages are live.

- **Site:** https://faroshad.github.io/plurarch/ (participants) and `…/console.html` (facilitator
  login: the Supabase user whose email is in `public.facilitators`).
- **Supabase project** `ifrhsamzievkbebxbqtv`. Schema plus migration 002 applied (the status view is
  now security invoker).
- **Tests:** `tests/test_rls.py` 48 passed / 0 failed / 2 skipped (they need the test accounts'
  passwords). `health-check` (Supabase) all passed, realtime included.
- **End-to-end over the internet:** a vote from the live site landed in Supabase; the orchestrator
  (local PC, `backend: supabase`) reviewed it (ACCEPTED, 10 s) and Rhino updated.
- **Rhino:** `model_builder.py` now reopens the Grasshopper editor (minimised) if someone closes it,
  because Grasshopper draws no preview with the editor closed.
- The public QR code is `state/qr_online.png`.

### Open issues / next steps
1. **The user tests on real phones in local mode** (same Wi-Fi): scan `state/qr.png`, vote, close
   the round from the console.
2. **Supabase:** the user creates the project (`supabase/SETUP.md`, about 10 min). Then run
   `tests/test_rls.py` and `health-check --backend supabase`, and put the keys in `.env` and
   `site/config.js`.
3. **GitHub:** create `Faroshad/plurarch` (public), `git init`, first commit (check `.gitignore`
   first), push, Settings → Pages → Source: GitHub Actions.
4. Rehearse on the projector: projection mode, and the Rhino named view "Plurarch".
5. Still unanswered by the user: session date, participant count, climate and the facilitator email
   (defaults in use).
