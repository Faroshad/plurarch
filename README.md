# Plurarch

**An AI-mediated participatory design platform.** *Plurarch* comes from "plural" + "arch", where
*arch* means both architecture and rule: many hands shape the architecture.

Participants vote on design parameters from any device. A reviewer agent checks their collective
proposal against a project brief and tests alternatives. It then decides to **ACCEPT**, **MODIFY** or
**REJECT** it, with a transparent rationale based on evidence. It applies the result to a live
parametric model (Rhino + Grasshopper) through MCP and verifies the change.

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
  - The agent sees exactly five tools (design-mcp) and nothing else: no shell, no files, no web, no
    other MCP servers, no CLAUDE.md.
  - It returns a decision record, the orchestrator validates it, and the process exits.

```
phones ──votes──▶ backend (local server or Supabase) ◀──realtime/polling── facilitator console
                        │ round closed
                        ▼
              orchestrator (your laptop) ──tally──▶ headless reviewer agent (Claude Code -p)
                        ▲                                   │ design-mcp tools only
                        │ validate + write decision         ▼
                        └────────────── state/parameters.json ──▶ Grasshopper rebuilds the model
```

## The model: one generator, projector and phones

`site/js/model/pavilion.js` generates the pavilion from the four parameters, inside and out: about
1,000 parts, 32 materials, question pins and tour stops. Its contract is `docs/MODEL.md`.
- **Rhino** runs it through `node tools/pavilion_cli.mjs`, so the projector and the phones always
  show the same building. If Node fails, Rhino falls back to a simpler built-in model.
- **Phones** run it in the browser with three.js. Participants can:
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
| `test-agent [--profile P \| --proposal file]` | One reviewer run in a temporary state folder; the live model is untouched. |
| `reset-session [--yes]` | Closes rounds, removes simulated votes, restores the default design, starts a fresh session. |

Other tools:
- `tools\make_qr.py <url>`: a QR code PNG with the URL underneath.
- `tools\load_test.py --devices 300`: simulated phones polling and voting.
- `tests\judgment\run_judgment.py --runs 3`: the judgment suite against the real agent.

## How the agent judges

The hierarchy, in strict order:
1. **Hard rules**, deterministic and never overridable:
   - window ratio ≥ 25%;
   - a glass façade with ≥ 50% windows needs a shading fraction ≥ 0.35.
2. **Brief goals** with thresholds:
   - daylight ≥ 50, cooling ≤ 62, cost ≤ 100, carbon ≤ 90;
   - a goal missed by ≤ 3 points is *marginal*: a close trade-off, so the participants win.
3. **Participant preference.**

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

Defined in `design_mcp/metrics.py`. Notation:
- w = window ratio / 100; a = roof angle in degrees; d = canopy depth in m; S = shading fraction.
- `heat_m`, `cost_m`, `carbon_m`, `light_m` are per-material factors: timber, concrete and glass
  have different heat gain, cost and embodied carbon.

| Metric | Formula |
|---|---|
| Shading fraction S | min(0.6, 0.12 + 0.005·a + 0.256·min(1, d/2.5)) |
| Daylight (higher is better) | 100·(1 − e^(−3.2·(w·(1 − 0.35·S) + light_m))) |
| Cooling load (lower is better) | 20 + 110·w·(1 − S) + 40·(1 − w)·heat_m |
| Cost (budget 100) | 25 + 40·((1 − w)·cost_m + 1.3·w) + 12·(1 + 0.6·a/35) + 4·d |
| Embodied carbon | 20 + 40·((1 − w)·carbon_m + 0.9·w) + 10·(1 + 0.5·a/35) + 3·d |

Material factors:

| | heat_m | cost_m | carbon_m | light_m |
|---|---|---|---|---|
| timber | 0.15 | 1.00 | 0.30 | 0 |
| concrete | 0.20 | 0.80 | 1.40 | 0 |
| glass | 0.55 | 1.20 | 1.00 | 0.03 |

## Rehearsal checklist
1. `health-check` passes.
2. Rhino is open, the Timer is running, and the Custom Preview shows the pavilion.
3. `run` is going in a terminal with a large font, placed next to the console.
4. Log in to the console, switch projection mode on, and read it from the back of the room.
5. Simulate three rounds in a row:
   - `simulate --profile consensus --open --close` → expect ACCEPTED;
   - `simulate --profile rule_violating --open --close` → expect MODIFIED;
   - `simulate --profile extreme --open --close` → expect REJECTED.
   The model should change for the first two.
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

## Repository map
- `config/`: parameters, brief and use-case profiles (the single source of truth).
- `design_mcp/`: the MCP server, metrics and the deterministic core.
- `agent/`: the system prompt and decision schema.
- `orchestrator/`: the run loop, tally, agent runner, validation, and the local and Supabase backends.
- `site/`: the participant app and the facilitator console (no build step).
- `supabase/`: schema, RLS and setup steps.
- `grasshopper/`: the model builder and setup steps.
- `tools/`: setup, QR code and load test.
- `tests/`: unit tests, the RLS tests and the judgment suite.
- `docs/ARCHITECTURE.md`: the data contract. `PROGRESS.md`: the progress log.
