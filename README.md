# Plurarch

**An AI-mediated participatory design platform.** *Plurarch* comes from "plural" + "arch", where
*arch* means both architecture and rule: many hands shape the architecture.

Participants vote on design parameters from any device. A reviewer agent checks their collective
proposal against a project brief and tests alternatives. It then decides to **ACCEPT**, **MODIFY** or
**REJECT** it, with a transparent rationale based on evidence. It applies the result through MCP and
verifies the change.

**The building (v3, since 2026-10-01):** the retrofit of the studio façade of the real **Langford
Architecture Center, Building A** (Texas A&M, College Station), an occupied 1970s brutalist
architecture school. **Revit is the source of truth:** the AI changes real Revit elements in
`revit/LangfordA_Plurarch.rvt` (panel types, the solid-panel material, new `PLX-FIN-*` fins). Rhino
renders the same decision, and the phones show light geometry from the same model. The contract is
`docs/LANGFORD.md`. The earlier generated pavilion is archived in `config/archive_pavilion/`.

**The judgment principle:** the agent exercises real, auditable judgment, not blind execution.
- Participants propose.
- The rules constrain: hard rules first, then brief goals, then preference.
- The agent judges and executes.
- Everyone sees why. Every decision is traceable to tool outputs.

**Use cases:**
1. **Live lectures and presentations**: *the first implemented use case.* The audience scans a QR
   code and the model changes on the projector.
2. Design studio reviews and juries.
3. Client workshops.
4. Community consultation.

## Build time vs session time

- **Build time:** Claude Code in VS Code builds and changes this project. With `.mcp.json`, Claude
  in VS Code can also use the design tools directly, for example "evaluate these two options and
  apply one".
- **Session time:** VS Code does not need to be open. You start the orchestrator in a terminal. Each
  time a round closes, it tallies the votes and launches a fresh, **headless** Claude Code process
  as the reviewer agent.
  - The agent sees exactly six tools (design-mcp) and nothing else: no shell, no files, no web, no
    other MCP servers, no CLAUDE.md.
  - It returns a decision record, the orchestrator validates it, and the process exits.

```
phones ──votes──▶ backend (local server or Supabase) ◀──realtime/polling── facilitator console
                        │ round closed
                        ▼
              orchestrator (your laptop) ──tally──▶ headless reviewer agent (Claude Code -p)
                        ▲                                   │ design-mcp tools only
                        │ validate + write decision         ▼ set_parameters
                        └────────────── state/parameters.json ──▶ Rhino render scene, phones, stage
                                                            │
                                                            └──▶ Revit (HTTP add-in, document guard):
                                                                 revit/LangfordA_Plurarch.rvt
```

The four questions (`config/parameters.json`): the finish of the solid façade panels (concrete,
aluminium, fritted glass), the share of the 142 SE studio window panels that stay clear glass
(40–100 %), the depth of 30 new concrete sunshade fins on the SE glass (0–1.2 m), and how many of the
12 north-light roof lanterns stay glazed (0–12). The defaults are the building as it stands.

## The model: one building, one plan rule, three views

- **Data:** `tools\langford_prep.py` (one-off, run with `C:\Python314\python.exe`) reads the P1
  Revit-first source read-only and writes `config/langford/elements.json` (which real elements each
  question controls) and the phone geometry `site/models/langford/base.json` + `base.bin`.
- **The plan rule** (`design_mcp/langford_plan.py`, mirrored in `site/js/model/langford.js`, kept
  equal by a parity test) turns parameters into the target state of those elements.
- **Revit** (`design_mcp/revit_apply.py`): `set_parameters` diffs the plan against the Revit state,
  applies only the changes in one transaction, reads back and verifies. It writes only when the active
  document is `LangfordA_Plurarch` from this repo's `revit/` folder.
- **Rhino** (`grasshopper/langford_builder.py`) applies the same plan to the render scene.
- **Phones** (`site/js/model/langford.js`) re-tag panel materials and add fin boxes on the base
  geometry. The pavilion generator (`site/js/model/pavilion.js`, `docs/MODEL.md`) is the archived v2.

The phone viewer (v2 features, unchanged) lets participants:
  - rotate the textured model, or take the **Tour** through the interior (stops, swipe to look
    around, tap the floor to walk);
  - answer each question by tapping its **pin** on the façade, the windows, the roof or the canopy;
  - see **their own version** rebuild live as they change a value.
  After a decision, the phones show the applied design, and pins mark the values the reviewer
  changed. "List view" keeps the simple step-by-step questions. It is also the automatic
  fallback on phones without WebGL2.

## Two ways to run

| | Local mode (default) | Supabase mode |
|---|---|---|
| Accounts needed | none | Supabase (free) + GitHub (Pages) |
| Participants join | same Wi-Fi as the laptop: `http://<laptop-ip>:8787/` | anywhere: `https://faroshad.github.io/plurarch/` |
| Good for | rehearsal, small rooms, home tests | lecture halls, campus Wi-Fi with client isolation |

Switch with `"backend"` in `config/local.json` (or `--backend supabase` on any command).
**Campus Wi-Fi often blocks phone-to-laptop traffic, so use Supabase mode for the real lecture.**

## Setup (once per machine)

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe tools\setup_local.py      # writes config/local.json, agent/mcp.json, .mcp.json
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

- Claude Code must be installed and logged in (`claude --version`). The reviewer uses the same login.
- **Rhino:** follow `grasshopper/SETUP.md`. It takes about 5 minutes: one Python 3 Script component,
  a Timer and a Custom Preview.
- **Supabase mode:** follow `supabase/SETUP.md`, then put the keys in `.env` and `site/config.js`.
- **GitHub Pages:**
  1. Create the repo `plurarch` on github.com. It must be public for free Pages.
  2. Push this folder.
  3. Go to Settings → Pages → Build and deployment → Source: **GitHub Actions**. Every push to
     `main` deploys `site/` plus `config/` (the workflow never publishes `config/local.json`).

## Run commands

All commands are `.venv\Scripts\python.exe orchestrator\orchestrator.py <command>`:

| Command | What it does |
|---|---|
| `run` | The session loop. It processes every closed round exactly once (safe to restart). Local mode also serves the site and prints the join URL, the console URL (with the facilitator key) and `state/qr.png`. |
| `open-round` / `close-round` | Manual round control (the console has the same buttons). |
| `status` | Session, rounds, votes, decisions, current model. |
| `simulate --n 40 --profile consensus` | Fake votes into the open round. Profiles: `consensus`, `split`, `extreme`, `rule_violating`. Add `--open --close` for a complete round; `--over 15` spreads the votes over 15 s. |
| `health-check` | A pass/fail report on every part of the loop. |
| `test-agent [--profile P \| --proposal file]` | One reviewer run in a temporary state folder, with Revit off; the live model is untouched. |
| `reset-session [--yes]` | Closes rounds, removes simulated votes, restores the default (as-built) design, re-applies it to the Revit copy (best effort, same document guard) and starts a fresh session. |

Other tools:
- `tools\make_qr.py <url>`: a QR code PNG with the URL underneath.
- `tools\load_test.py --devices 300`: simulated phones polling and voting.
- `tests\judgment\run_judgment.py --runs 3`: the judgment suite against the real agent.

## How the agent judges

The hierarchy, in strict order:
1. **Hard rules**, deterministic and never overridable:
   - SE studio glass ≥ 50 % (studio daylight and views);
   - in College Station's climate, SE glass ≥ 80 % needs fins ≥ 0.6 m.
2. **Brief goals** with thresholds:
   - studio daylight ≥ 70, cooling ≤ 68, retrofit cost ≤ 90, embodied carbon ≤ 70, heritage fit ≥ 70;
   - a goal missed by ≤ 3 points is *marginal*: a close trade-off, so the participants win.
3. **Participant preference.**

**The as-built premise:** the starting design is the building as it stands (concrete / 100 % glass /
no fins / 12 lanterns). It fails the fins rule and overheats (cooling 73.0), which is why the retrofit
is needed. A vote for it is MODIFIED with the minimum fins (0.6 m: cooling 67.4). REJECTED keeps the
current design even when it is the non-compliant as-built, but only when nothing valid exists within
the limits; the validator accepts that case with a warning.

- **ACCEPTED:** it passes every rule and no goal fails. Applied as voted.
- **MODIFIED:** the closest valid alternative. It changes at most 2 parameters, at most 3 slider
  steps each; the material changes only to a leading option under weak consensus. The agent must
  test at least 2 alternatives first, with at most 5 evaluations.
- **REJECTED:** nothing valid exists within those limits. The design stays, and the agent says what
  to vote for instead.

Strong consensus (≥ 70%) keeps its value unless a hard rule forces a change. The agent also sees the
session's previous decisions and must stay consistent with them, or explain why it departs. All
numbers live in `config/project_brief.json`, so you can tune them without touching code.

Safety: participant input is data, never instructions. The agent receives only the numeric tally.
design-mcp re-checks every hard rule in `set_parameters` and refuses violations, whatever the agent
says. The orchestrator then validates the record independently: schema, verdict vs what happened,
model file vs record, metrics recomputed. If anything fails, it restores the previous design.

## Metrics (indicative proxies, not simulations)

Defined in `design_mcp/metrics.py`, computed on the **real panel areas** of the Revit model
(`config/langford/elements.json`): 142 SE lites (382.8 m²), 12 lanterns (359.3 m² of glazing), 30 fin
anchors at 3.77 m spacing. Which panels are glass for a given share comes from the plan rule.
Notation (all 0..1):
- g = glazed SE area / 382.8; g_fin = glazed SE area in the finned bays / 382.8; s_se = 1 − g;
- k = open lantern glazing / 359.3; s_sky = 1 − k; n_closed = closed lanterns; d = fin depth (m);
- F = fin shading fraction: the share of the hot-season beam sun on the finned SE glass that the fins
  block. For May–September in College Station (30.6° N), every 30 min while the sun is in front of the
  façade (azimuth 140.65°), the fins shade min(1, d·|tan γ| / 3.77) of the glass width (γ = the
  horizontal sun angle to the façade normal), weighted by clear-sky beam irradiance on the façade.
  F = 0.08 / 0.16 / 0.24 / 0.32 for d = 0.3 / 0.6 / 0.9 / 1.2 m.

| Metric | Formula |
|---|---|
| Studio daylight (higher is better) | 100·(1 − e^(−a)), a = 1.9·(g − 0.30·F·g_fin) + 0.6·k + light_f·(1.9·s_se + 0.6·s_sky) |
| Cooling load (lower is better) | 28 + 36·(g − F·g_fin) + 9·k + heat_f·(s_se + 0.6·s_sky) |
| Retrofit cost (budget 90) | 35 + 26·d/1.2 + cost_f·(40·s_se + 20·s_sky) |
| Embodied carbon | 15 + 30·d/1.2 + carbon_f·(40·s_se + 20·s_sky) |
| Heritage fit (higher is better) | 100 − (base_f + per_f·s_se) − 22·s_se − 2.2·n_closed − 6·d/1.2 |

Finish factors for the solid panels:

| | heat_f | light_f | cost_f | carbon_f | heritage base_f, per_f |
|---|---|---|---|---|---|
| concrete | 3 | 0 | 1.00 | 1.00 | 0, 0 |
| aluminium | 9 | 0 | 1.45 | 1.90 | 18, 25 |
| fritted glass | 16 | 0.40 | 1.25 | 1.15 | 4, 8 |

Reference values: as built 91.8 / 73.0 / 35.0 / 15.0 / 100.0 (daylight / cooling / cost / carbon /
heritage); with the minimum fins (0.6 m) 91.0 / 67.4 / 48.0 / 30.0 / 97.0.
The metrics also report `shading` (F), `se_glass_m2` and `skylight_glass_m2`.

## Rehearsal checklist
1. `health-check` passes.
2. Rhino is open, the Timer is running, and the Custom Preview shows the pavilion.
3. `run` is going in a terminal with a large font, placed next to the console.
4. Log in to the console, switch projection mode on, and read it from the back of the room.
5. Simulate three rounds in a row:
   - `simulate --profile consensus --open --close` (concrete, 90 %, 0.6 m, 12) → expect ACCEPTED;
   - `simulate --profile rule_violating --open --close` (100 % glass, 0.3 m fins) → expect MODIFIED;
   - `simulate --profile split --open --close` (aluminium by a weak plurality) → expect MODIFIED;
   - `simulate --profile extreme --open --close` (aluminium, 40 %, no fins, no lanterns) → expect REJECTED.
   The model should change for the first three, in Revit too (check `PLX-FIN-*` and the panels).
6. Scan the QR code with two or three real phones, vote in a real round, and check that each phone
   shows the decision card.
7. Stop `run` in the middle of a round, restart it, and check that no round is processed twice.
8. `reset-session --yes`.

## Pre-session checklist (the day of the lecture)
1. Supabase mode: open the Supabase dashboard an hour before, so the project is not paused.
2. `health-check` passes; fix anything red first.
3. Rhino is open with `grasshopper/plurarch.gh`, the Timer is on, and the "Plurarch" named view is
   restored. **Keep the Grasshopper window open (minimised is fine).** When it is closed, Grasshopper
   draws no preview in Rhino at all.
4. `reset-session --yes`, then `run` in a terminal with a large font.
5. Console logged in, projection mode on, no rounds open.
6. The QR code (`state/qr.png`, or from `tools\make_qr.py <pages-url>`) is on the slide.
7. Backup: a phone hotspot for the laptop. If few people join, use `simulate`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Phones cannot open the join URL (local mode) | The Wi-Fi isolates devices, or the Windows firewall blocks port 8787. Allow Python in the firewall prompt, use a phone hotspot, or switch to Supabase mode. |
| Decision "failed: timed out" | Claude was slow or offline. The design was kept. Check the internet connection, then open and close another round. Raise `agent_timeout_s` in the use-case profile if runs are slow. |
| Decision "failed: not logged in" | Run `claude` once in a terminal and log in. |
| Model does not change in Rhino | The Timer is stopped, or the `path` panel points somewhere else (`config/local.json` → `grasshopper_file`). Check the `warning` output. |
| Rhino shows nothing | The Grasshopper window was closed: reopen it (minimised is fine). |
| Rhino shows a simpler model with an orange warning | Node failed, so Rhino fell back to the built-in model. Check `"node"` in `config/local.json`, or run `tools\setup_local.py` again. |
| Console shows "Reconnecting" | Supabase: the project may be paused (open the dashboard) or the network is down. Local: `run` is not running. |
| A round was never reviewed | Is `run` running? `status` shows rounds that are closed but not processed; `run` picks them up on start. |
| Supabase project paused | Dashboard → the project → "Restore project", and wait about 2 minutes. |
| Wrong verdict tendencies | Tune the numbers in `config/project_brief.json`, then run `tests\judgment\run_judgment.py`. |
| Decision says "Revit: not applied (…)" | Revit is closed, or another document is active. Open `revit/LangfordA_Plurarch.rvt` and make it the active document; the next decision catches up (the apply is idempotent). `health-check` shows the Revit line. Set `"revit_required": true` in `config/local.json` to refuse decisions instead. |
| Revit shows fins without a Mark | A fin-mark step was interrupted. The next apply replaces those fins. |

## Repository map
- `config/`: parameters, brief and use-case profiles (the single source of truth);
  `config/langford/elements.json` (generated); `config/archive_pavilion/` (the v2 pavilion).
- `design_mcp/`: the MCP server, metrics, the deterministic core, the Langford plan rule
  (`langford_plan.py`), the Revit applier (`revit_apply.py`) and its client (`revit_client.py`).
- `revit/`: the Revit copy the AI changes. `rhino/`: the Rhino render scene.
- `agent/`: the system prompt and decision schema.
- `orchestrator/`: the run loop, tally, agent runner, validation, and the local and Supabase backends.
- `site/`: the participant app and the facilitator console (no build step).
- `supabase/`: schema, RLS and setup steps.
- `grasshopper/`: the model builder and setup steps.
- `tools/`: setup, QR code, load test and the Langford data prep (`langford_prep.py`).
- `tests/`: unit tests, the RLS tests and the judgment suite.
- `docs/ARCHITECTURE.md`: the data contract. `PROGRESS.md`: the progress log.
