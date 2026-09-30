# PROJECT SPEC: Plurarch
## An AI-mediated participatory design platform

## Concept
Plurarch (from "plural" + "arch," meaning both architecture and rule): many hands shape the architecture.

Plurarch lets many stakeholders take part in shaping a design, while an accountable AI reviewer ensures every decision is grounded in evidence. Participants vote on design parameters from any device. A reviewer agent evaluates their collective proposal against a project brief, tests alternatives, and decides to ACCEPT, MODIFY, or REJECT it, with a transparent, evidence-based rationale. It then applies the result to a live parametric model through MCP and verifies the change.

The core principle: the agent exercises real, auditable judgment, not blind execution. Every decision must be traceable to tool outputs. Participants propose, the rules constrain, the agent judges and executes, and everyone sees why.

## Use cases
The platform is designed to be general. Target use cases:
1. Live lectures and presentations (FIRST USE CASE, the one to build now): an audience scans a QR code and takes part in real time while the model changes on the projector.
2. Design studio reviews and juries: students and critics evaluate design options.
3. Client workshops: clients and consultants steer early design decisions.
4. Community consultation: residents shape public projects.

Build only what the first use case needs. Do not build features for the other use cases, but avoid naming, data structures, or design choices that would block them. For example, use general terms (session, participant, facilitator) instead of event-specific ones (audience, presenter), and keep use-case settings in config.

## Context (ask me for anything missing before starting)
- Development environment: VS Code with the Claude Code extension. The Claude Code CLI is also installed.
- Design environment: Rhino with Grasshopper (Python 3 script components). Ask me for any detail about my setup that you need.
- GitHub username and repo name: [fill in, e.g. plurarch]
- Supabase account: [yes / no]
- Facilitator email (for the console login): [fill in]
- First use case: a live presentation with [~N] participants. Design for up to a few hundred devices at once.
- Style reference image: docs/style-reference.jpg (see "Visual design system").
- Timeline is short and fixed by the first live session. Estimate the time for each milestone yourself, tell me the plan up front, and prioritize reliability over features at every step.

## How the system runs
- Build time: you (Claude Code) build the project with me in VS Code.
- Session time: VS Code does not need to be open. I start the orchestrator in a terminal. Each time a round closes, the orchestrator launches a new, headless Claude Code process (CLI) as the reviewer agent, passes it the proposal, collects its decision, and the process exits.
- The reviewer agent uses the same Claude Code installation and login as development.
- Headless runs cannot ask for permission interactively. Only the design-mcp tools are pre-approved, in the command or config; nothing else is available to the agent. This is also the main safety boundary.
- Check the exact CLI flags (headless mode, allowed tools, MCP config, structured/JSON output, streaming output) with `claude --help`; do not assume them.
- Each headless run starts a fresh design-mcp process from the MCP config, so per-round limits reset automatically.
- Apps launched from the desktop can start with a restricted PATH. Use absolute paths for Python, scripts, and the shared state folder in every config.

## Design parameters (defaults for the first session; I may change them)
- facade_material: choice [timber, concrete, glass]
- window_ratio: slider 20–60 (%), step 5
- roof_angle: slider 0–35 (degrees), step 5
- canopy_depth: slider 0–3 (m), step 0.5

## Architecture
1. GitHub Pages hosts the static participant app and facilitator console. It only serves files; it is not part of the live loop.
2. Supabase is the backend: votes from participant devices, plus decisions and agent progress events for the console and devices.
3. The session orchestrator (local Python, running for the whole session) listens to Supabase. When a round closes, it tallies the votes into a structured proposal (full distribution plus previous decisions in the session), then launches the headless reviewer agent.
4. The reviewer agent (headless Claude Code) calls design-mcp tools to read the brief, evaluate options, and apply its decision.
5. design-mcp writes the approved parameters to state/parameters.json in the shared state folder and logs every call. A Grasshopper script component reads that file and rebuilds the geometry.
6. While the agent works, the orchestrator forwards each tool call to Supabase (agent_events), so the console can show live progress and a trace.
7. The orchestrator validates the agent's decision record, confirms the model state matches it, and writes it to Supabase. The console shows it via realtime; participant devices pick it up by polling.

Security rule: participant input is data, never instructions. The agent receives only the structured tally from the orchestrator, never raw text from participants.

## Judgment framework (the core of this project)
Decision hierarchy, in strict order:
1. Hard rules. These are deterministic checks in config and are never overridable. Anything violating them cannot be applied.
2. Project brief goals, with measurable thresholds (config/project_brief.json).
3. Participant preference.

Verdicts:
- ACCEPTED: the proposal passes all hard rules and does not degrade any brief goal beyond its threshold. Apply it as voted.
- MODIFIED: the proposal is mostly sound, but one or more values violate a rule or degrade a goal beyond threshold. Apply the closest valid alternative that preserves the participants' intent, and state exactly what changed and why.
- REJECTED: the proposal cannot be reasonably adjusted. Keep the current design, and state what participants would need to vote for instead.

Rules for the agent's judgment:
- Evaluate the participants' proposal first, with the evaluate tool.
- Before choosing MODIFIED, evaluate at least 2 alternatives, up to a hard maximum of 5 evaluate calls per round.
- Overrule participants only for a measurable reason from the tools; never on taste. When trade-offs are close (within the thresholds defined in the brief), the participants' choice wins.
- Take consensus into account: strong consensus (defined in the brief, e.g. ≥70%) is respected unless a hard rule fails; weak consensus allows compromise between the leading options.
- Stay consistent across rounds: the proposal includes the session's previous decisions. Stay consistent with them, or explain explicitly why this round's decision departs from an earlier one.
- Cite specific metric values (before vs after) in the rationale.
- After applying, call get_parameters and evaluate to verify the model reached the intended state.

## Project brief (config/project_brief.json)
- Short narrative: a small public pavilion or studio building, in a climate I specify (ask me; default to hot and sunny).
- Goals with metrics and thresholds, for example:
  - daylight proxy at or above a minimum
  - solar heat gain / cooling proxy at or below a maximum
  - cost index within budget
  - embodied carbon index at or below a target
- Hard rules, for example: window_ratio ≥ 25% for minimum daylight; the combination of glass façade and high window ratio without adequate shading is not allowed in a hot climate.
- Consensus thresholds and a "close trade-off" margin.
- All numbers live in this file, so I can tune them without touching code.

## Use-case profile (config/use_cases/live_presentation.json)
- Settings specific to the first use case:
  - polling interval (default 4 seconds, with jitter)
  - suggested round duration (default 60 seconds)
  - expected participant count
  - projection scale for the console (default 1.4)
  - privacy notice text
- Components read these settings instead of hard-coding them, so future use cases can be added as new profiles.

## Repo structure (adjust if you have a good reason, and explain why)
- SPEC.md: this file
- PROGRESS.md: progress log, updated after every milestone (see "Working method")
- docs/style-reference.jpg: visual style reference (provided by me)
- config/parameters.json: parameter keys, labels, types, options/ranges, steps. The single source of truth for all components.
- config/project_brief.json: goals, thresholds, hard rules, consensus settings
- config/use_cases/live_presentation.json: the first use-case profile
- config/local.json (git-ignored): absolute paths for this machine (Python, the shared state folder, the Grasshopper-watched file)
- site/index.html: participant app (mobile-first)
- site/console.html: facilitator console
- site/styles.css: shared design tokens and components
- site/config.js: Supabase URL, anon key (public by design; security comes from RLS), and the active use-case profile
- supabase/schema.sql: tables, views, constraints, auth settings notes, RLS, realtime
- orchestrator/orchestrator.py: CLI with run, open-round, close-round, simulate, status, health-check, test-agent, and reset-session commands
- design_mcp/server.py: custom MCP server (Python, FastMCP)
- design_mcp/metrics.py: transparent, documented metric formulas
- agent/SYSTEM_PROMPT.md: the reviewer agent's role and judgment framework
- agent/mcp.json: MCP config exposing only design-mcp (absolute paths)
- agent/decision_schema.json: JSON schema for the decision record
- grasshopper/model_builder.py: GhPython 3 component that reads state/parameters.json and generates the geometry
- grasshopper/SETUP.md: wiring instructions for a non-programmer
- state/ (git-ignored): parameters.json and log.jsonl, at the absolute path from config/local.json
- tools/make_qr.py: QR code PNG for the participant app URL
- tools/load_test.py: simulates a full set of participant devices polling and voting
- tests/: unit tests plus a judgment test suite (see below)
- .github/workflows/deploy.yml: deploys site/ to GitHub Pages
- .env.example, .gitignore (the service role key and .env must never be committed)
- README.md: concept, setup, run commands, checklists, troubleshooting

## Component requirements

### Supabase
- Tables:
  - sessions (id, title, use_case, created_at)
  - rounds (id, session_id, status open/closed, opened_at, closed_at, processed_at)
  - votes (id, round_id, participant_id, question_key, value, created_at)
  - decisions (id, round_id, proposal jsonb, verdict, applied_parameters jsonb, evidence jsonb, rationale text, alternatives_considered jsonb, status, created_at)
  - agent_events (id, round_id, seq, step, tool, summary, created_at): one row per agent tool call, for live progress and the trace
  - facilitators (user_id or email): the allowlist of facilitator accounts
- A tiny public status view (one row per active session): current round id and status, and the latest decision id. Participant devices poll only this view, and fetch a full decision only when its id changes.
- One vote per participant per question per round, via a unique constraint on (round_id, participant_id, question_key).
- Validate vote values against the allowed options/ranges in the database, not only in the app.
- Auth: disable public sign-ups (give me the exact dashboard steps). Only accounts listed in facilitators get facilitator permissions; any other signed-in account gets no extra access.
- RLS rules:
  - Anonymous participants can insert votes only while the round is open.
  - Anonymous participants can read the status view and decisions (so they can see verdicts).
  - Anonymous participants cannot read, update, or delete votes, and cannot read agent_events.
  - Facilitators can read votes and agent_events, and can open and close rounds.
  - Only the orchestrator (service role) writes decisions and agent_events.
- Realtime:
  - Free-plan limit: 200 concurrent realtime connections.
  - Only the facilitator console (votes, decisions, agent_events) and, if needed, the orchestrator (rounds) use realtime subscriptions.
  - Participant devices never open realtime subscriptions.
- Free-plan projects pause after a period of inactivity. The health check and pre-session checklist must account for this.
- Tests:
  - An anonymous client cannot read votes or agent_events.
  - A signed-in account that is not in facilitators cannot read votes.

### Participant app (site/index.html)
- Visual style and layout: follow "Visual design system" (participant app layout).
- Mobile-first, works on iPhone Safari and Android Chrome, no login.
- Anonymous participant ID in localStorage.
- Submits votes as normal inserts. Polls the status view at the interval from the use-case profile, with random jitter so devices don't all request at the same moment. Pause polling when the tab is hidden; resume when visible.
- Shows the latest design image if one is available (see the optional viewport capture under Grasshopper), otherwise the initial design image in site/.
- One question per parameter, built from config.
- States: waiting for round, voting open, vote recorded, and the decision.
- When a decision arrives, show a decision card: the verdict badge, what participants voted for vs what was applied, and the rationale.
- A "rules of the game" panel summarizing the brief's hard rules and goals in plain language.
- Privacy notice from the use-case profile (default: anonymous; only votes are recorded, for this session only).
- Handles network errors gracefully: retry votes automatically, and show a small "reconnecting" indicator instead of an error screen.
- Loads supabase-js from a CDN; no build step.

### Facilitator console (site/console.html)
- Visual style and layout: follow "Visual design system" (console layout).
- Facilitator login (Supabase email auth, facilitators allowlist).
- Live participant count and live results per question via realtime, showing the full distribution, not only the winner.
- Open and close round controls.
- Reviewer-agent status card with live progress from agent_events.
- Decision card, readable on a projector from the back of a room:
  - The verdict badge (ACCEPTED / MODIFIED / REJECTED).
  - The proposal vs the applied values.
  - Before/after metrics.
  - Alternatives the agent considered, with their metrics.
  - A rationale of at most 3 sentences.
- Live trace panel from agent_events, one line per tool call, in order.
- A projection-mode toggle that applies the projection scale from the use-case profile.
- If the realtime connection drops, reconnect automatically and show a visible connection indicator.

### Session orchestrator (orchestrator/orchestrator.py)
- Uses the service role key from .env; runs only on the facilitator's machine.
- `run`: the main loop for a session. Listens for closed rounds (realtime or short polling) and processes each round exactly once. Processing is idempotent: after a restart, rounds already processed (processed_at set) are never processed again.
- For each closed round:
  1. Tallies votes into a proposal:
     - For choice questions: the plurality winner plus full counts and percentages.
     - For sliders: median, rounded to step, plus the distribution/spread.
     - Participation counts.
     - The session's previous decisions (verdict, applied parameters, rationale), so the agent can stay consistent across rounds.
  2. Launches the headless reviewer agent with the proposal, the system prompt, and the MCP config, with only design-mcp tools pre-approved and a configurable timeout (default 120 seconds). Passes the round id to design-mcp (for example via an environment variable) for logging.
  3. Forwards each agent tool call to agent_events in near real time (by tailing state/log.jsonl, or from the CLI's streaming output if available).
  4. Parses and validates the agent's decision record against agent/decision_schema.json.
  5. Confirms state/parameters.json matches applied_parameters.
  6. Writes the decision to Supabase and sets processed_at on the round.
- If the agent fails, times out, or returns an invalid record: keep the current design, write a decision with status "failed" and a clear message, and never crash mid-session.
- Timestamped, colored console output of every step, suitable for showing on screen next to the console.
- `open-round` / `close-round` / `status`: manual controls and a quick status summary.
- `simulate --n 40 --profile <name>` inserts realistic fake votes. Profiles: consensus, split, extreme (troll-like), rule_violating. Used for rehearsal and as a fallback if few people join.
- `health-check` confirms, with a clear pass/fail report:
  - the Supabase project is active (not paused)
  - RLS blocks anonymous reads of votes
  - a test vote round-trips
  - the console realtime channel receives events
  - design-mcp responds
  - a minimal headless Claude Code call succeeds
  - the shared state folder is writable
- `test-agent`: runs the headless agent once against a sample proposal, using a temporary state folder so the live model is untouched. Prints the decision record and how long the run took.
- `reset-session` closes open rounds, removes simulated test data, and restores the default parameters.

### design-mcp (design_mcp/server.py)
- Reads the shared state folder path from config/local.json or an environment variable.
- get_schema: parameters and their ranges.
- get_project_brief: goals, thresholds, hard rules, consensus settings.
- get_parameters: the current applied state.
- evaluate(parameters): SIDE-EFFECT FREE. Returns metrics, hard-rule results (pass/fail with reason), and goal results (within threshold or not). The agent uses it to explore alternatives.
- set_parameters(parameters, verdict, rationale):
  - Re-runs every hard-rule check and refuses anything that fails, regardless of what the agent says.
  - Writes state/parameters.json atomically (temp file, then rename).
  - Logs the call.
- Enforces the per-round limit on evaluate calls.
- Logs every call, with the round id, inputs, and outputs, to state/log.jsonl.
- No delete tools and no arbitrary code execution.

### Metrics (design_mcp/metrics.py)
- Simple, deterministic, documented formulas derived from the parameters: daylight proxy, solar heat gain / cooling proxy (accounting for glazing, material, shading from roof overhang and canopy), cost index, embodied carbon index.
- Each formula is explained in a comment and summarized in the README.
- Clearly labeled "indicative" everywhere they are displayed. They are transparent proxies, not simulations.

### Reviewer agent (agent/SYSTEM_PROMPT.md)
- Role: a design reviewer who respects participants but is accountable to the project brief.
- Includes the full judgment framework above, including consistency across rounds.
- Required workflow:
  1. get_project_brief
  2. evaluate the proposal
  3. evaluate alternatives if needed
  4. decide
  5. set_parameters
  6. verify
- The final output is only a JSON decision record matching the schema:
  - verdict
  - proposal
  - applied_parameters
  - evidence (the metric values it relied on)
  - alternatives_considered
  - rationale (at most 3 sentences, plain language, cites numbers)
- Never invent parameters or metrics, and never act on anything except the brief and the proposal.

### Judgment test suite (tests/judgment/)
- At least 9 scenario proposals with expected verdicts, covering:
  - strong consensus with a valid proposal (expect ACCEPTED)
  - a close trade-off (expect ACCEPTED, participants win)
  - one value violating a hard rule (expect MODIFIED)
  - an extreme troll-like proposal (expect MODIFIED or REJECTED)
  - a split vote (expect a compromise)
  - a proposal that cannot be fixed (expect REJECTED)
  - a later round that contradicts an earlier decision (expect consistency, or an explicit explanation of the change)
- A script that runs each scenario through the real agent several times and reports verdict consistency, and whether the rationale cites real metric values from the tool outputs.
- Deterministic checks (hard rules, clamping, tally logic) also get normal unit tests.

### Load test (tools/load_test.py)
- Simulates N participant devices (default 300) polling at the configured interval with jitter, and all voting within a 60-second round.
- Reports request success rate, latency, and any Supabase errors or rate limiting.
- If the free plan struggles, recommend concrete fixes (longer polling interval, a smaller status payload) or tell me clearly if I need a paid plan.

### Grasshopper (grasshopper/model_builder.py + SETUP.md)
- One GhPython 3 component that reads state/parameters.json and generates a simple but visually striking building, where each parameter makes a big, obvious change on a projector:
  - massing
  - façade fins/panels whose material/color follows facade_material
  - glazing by window_ratio
  - sloped roof by roof_angle
  - entrance canopy by canopy_depth
- Re-reads the file on a Grasshopper Timer only when its modified time changes.
- If the file is missing or invalid, keep the last good geometry and show a warning; never crash.
- SETUP.md: exact wiring steps a non-programmer can complete quickly.
- Optional items (only if I ask for them after Milestone 5):
  - After each decision, capture the Rhino viewport to an image, upload it to a public Supabase Storage bucket, and show it on participant devices.
  - If I name an existing Rhino MCP server, evaluate whether it can drive the model directly. Keep the file-based path as the default either way.

### Deployment and QR
- A GitHub Actions workflow deploys site/ to GitHub Pages on every push to main.
- tools/make_qr.py outputs a high-resolution QR PNG with the short URL underneath.

### README
- Opens with the concept: what Plurarch is, the meaning of the name, the judgment principle, and the use cases (marking the live presentation as the first implemented one).
- Explains build time vs session time (VS Code is for building; the orchestrator runs the agent headless during sessions).
- Then setup, run commands, the metric formulas, and the checklists (see Milestone 5).

## Visual design system (applies to all UI)

### Direction
- Style reference: docs/style-reference.jpg. Use it for visual style only: do not copy its logo, brand name, brand colors, or exact layout. Plurarch must look like its own product.
- Character: calm, clean, "soft modern" dashboard. Lots of white space, large rounded cards, a mostly monochrome palette, and a single vivid accent used sparingly for live data. Numbers are the heroes: big, bold, and easy to read from a distance.
- Light theme only for the first version (it projects well).

### Design tokens (CSS variables in site/styles.css)
Colors:
- --bg: #FFFFFF (page background)
- --surface: #F2F2F3 (control tracks, stat tiles, gray buttons)
- --surface-hover: #E9E9EB
- --border: #E6E6E8 (1px card outlines)
- --text: #111111 (primary text, big numbers)
- --text-secondary: #5C5C63
- --text-tertiary: #8E8E95 (small labels only, never body text)
- --ink: #1A1A1A (dark fills: primary buttons, toggles, count badges, darkest chart segment)
- --accent: #F0489B (vivid magenta; the only saturated UI color), with a 6-step ramp from very pale to full strength for heatmaps
- Neutral ramp (6 steps, from #EEEEEF to #6E6E73) for "not yet" or inactive data
- --gradient-agent: a soft pastel gradient from mint (#D9F5EE) through light blue (#DCE6FA) to periwinkle (#C9CFF7), used only for the reviewer-agent status card and decision cards
- --gradient-ring: an iridescent multi-color gradient used only as a 2px ring around the single most important action button
- Verdict colors (soft pastel background + dark text of the same hue; the only other colors allowed):
  - ACCEPTED: #DDF3E4 background, #1E6B3A text
  - MODIFIED: #FFF1D6 background, #8A5A00 text
  - REJECTED: #FDE2E2 background, #A12626 text
- Keep all colors as tokens so the accent can be changed in one place.

Shape and spacing:
- Radii: cards 24px, inner panels 16px, controls 12px, heatmap cells 6px, pills 999px.
- Spacing on a 4/8px grid: 4, 8, 12, 16, 24, 32, 48. Card padding 24px (scaled up in projection mode). Gaps between cards 12px.
- Cards: white fill, 1px --border outline, no heavy shadows. Only tooltips and floating chips get a soft shadow.

Typography:
- Font: Inter from Google Fonts, with a system-ui fallback stack. Weights 400, 500, 700, 800.
- Use tabular figures for all numbers, so values don't jump as they update.
- Scale (desktop base; the console multiplies it by the projection scale in projection mode):
  - Display (page title): 48px / 800 / letter-spacing -0.02em
  - Big stat number: 32px / 700
  - Card title: 20px / 700
  - Body: 15px / 400
  - Label: 12px / 500, --text-tertiary (used above or below values, as in "Rejected" over "2518")
  - Axis labels: 11px / 500, --text-tertiary
- Page header pattern: a very large bold title, then one short line with key values in bold, separated by middle dots. Example: "Live session", then "**128** participants · Round **2** open · Last decision at **15:14**".
- Number formatting: thin-space thousands separators (10 140), 24-hour times, sentence case everywhere, middle dot (·) as the separator in compact lines.

Icons:
- One open-source outline icon set (for example Lucide, loaded from a CDN), 1.5px stroke, rounded ends, 20px in navigation and 18px in buttons.

### Components
- App shell (console): a top bar about 64px tall; a left sidebar about 260px wide; the main content area to the right, max width about 1440px.
- Top bar: the Plurarch wordmark on the left; the session title as a gray pill in the center; the connection indicator and a square facilitator avatar tile on the right.
- Sidebar:
  - A session block: a large rounded tile with the project image or wordmark, the session title in bold, and one line of meta info.
  - Two small gray stat tiles side by side (for example "Participants 128" and "Rounds 2 / 3"), with the label small and gray above a bold value.
  - Navigation: outline icon + label. The active item gets a gray pill background, bold text, and a small ink dot at the right end. Counts appear as small ink rounded badges with white numbers. Keep navigation to what exists (for example Live session, Decisions, Brief, Trace).
  - A full-width gray pill button pinned at the bottom (for example "Projection mode").
- Buttons:
  - Gray pill button with an icon and label (secondary actions).
  - Gray rounded-square icon button, 40px, with an ink glyph; a small ink dot in the corner signals something new.
  - Ink pill button with white text (primary actions in the participant app).
  - The single most important action on each screen ("Open round" / "Close round" on the console) gets the --gradient-ring outline. Use it nowhere else.
- Segmented control: a gray pill track; the selected segment is a white pill with bold text and an optional chevron (for example "Round 2 · 15:02–15:03"). Used for round and question selection.
- Toggle switch: ink track with a white knob when on; gray track when off.
- Progress bar: 4px tall, ink fill on a light track, fully rounded.
- Count badge: small ink rounded rectangle, white bold number, overlapping the top-right corner of an icon button.
- Status chip: small pill showing Idle / Reviewing / Done / Failed.
- Tooltip chip: white rounded chip with a bold value and a soft shadow, following the pointer.
- Notice block: plain text on white with an outline info icon at the top right and an underlined link ending in a small chevron (for example "Read the project brief ›"). No colored alert boxes.
- Stat card: a big bold number, a regular label under it, then optionally a row of 2–4 small stats (tertiary label above a bold value), with generous space around them.

### Data visualizations
- Heatmap (the signature element):
  - A grid of rounded square cells (6px radius) with 3–4px gaps, axis labels in small gray text (time along the bottom, categories along the right side).
  - Color meaning: cells with actual data use the accent ramp (darker = more votes); cells that have not happened yet use the neutral gray ramp. This "color = real, gray = not yet" rule applies across the whole product.
  - Hover shows the tooltip chip with the exact count.
  - Plurarch use: vote activity during a round for the selected question, with rows as options or value bins and columns as time buckets. New cells fade in as votes arrive.
- Rounded "exploded" pie:
  - Segments separated by clear gaps, each with generously rounded corners. Implement with d3-shape's arc generator (cornerRadius and padAngle) loaded from a CDN, rendered as SVG.
  - Grayscale by rank: the largest segment in --ink, then progressively lighter grays. Percentages written inside each segment in bold (white on dark segments, dark on light ones).
  - Always paired with a legend list: outline icon + "Label · 56%".
  - Plurarch use: vote share for choice questions (for example façade material).
- Slider results: a horizontal distribution strip of rounded bars (accent ramp for counts), with the median marked by an ink line and a bold value label.

### Screen layouts
Facilitator console (site/console.html):
1. Page header: the display title and the meta line with bold values.
2. Main card (full width), split into two areas:
   - Left: a toolbar (segmented control for rounds, segmented control or tabs for the question shown, the gradient-ring "Open round" / "Close round" button, and gray icon buttons for projection mode and refresh), then the live vote heatmap.
   - Right (about 280px): the reviewer-agent status card with --gradient-agent. It shows a bold title ("Reviewer agent"), a status chip, a status line ("Evaluating alternatives · 15:14"), a progress bar for the 6-step workflow driven by agent_events, and a row of icon + count stats (evaluations used 3/5, hard rules passed, alternatives considered). Under it, a notice block for brief or rule information.
3. Bottom row:
   - A wide card: the decision. The verdict badge, what participants voted for vs what was applied, the rationale, and the rounded pie with its legend for the choice question.
   - Two stacked cards on the right: "Metrics" (big before/after numbers with small labeled deltas) and "Participation" (big participant count, with small stats underneath such as votes cast, rounds completed, and failed submissions).
4. The live trace panel: a compact card listing each agent tool call from agent_events, in a small monospace font, newest at the bottom.

Participant app (site/index.html), mobile:
- Single column, 16px side padding, same tokens and components.
- Header: the Plurarch wordmark and a round-status pill ("Round 2 · open").
- Display title for the current question, with a short gray explainer line.
- Choice questions: large rounded tiles (16px radius) in --surface; the selected tile turns --ink with white text.
- Slider questions: a thick rounded track with an ink fill and a large round thumb; the current value shown above it as a big bold number.
- A full-width ink pill "Submit vote" button, fixed at the bottom of the screen.
- The decision card uses --gradient-agent, with the verdict badge, "Room voted / Applied" values in bold, and the rationale.
- "Rules of the game" and the privacy notice use the plain notice-block style.

### Motion
- Subtle and quick: 150–250ms ease-out for hovers, segment changes, and cards appearing.
- New votes: heatmap cells fade in; pie segments and bars animate to their new values; the decision card slides up gently; agent trace lines appear one by one.
- Respect prefers-reduced-motion by turning animations off.

### Projection mode and accessibility
- Projection mode multiplies type sizes and card padding on the console by the projection scale from the use-case profile. At 1920×1080 in projection mode, no text may render below 14px.
- Text contrast must meet WCAG AA: use --text or --text-secondary for anything that must be read; --text-tertiary only for short labels paired with a bold value.
- Never rely on color alone: verdict badges always include the word (ACCEPTED / MODIFIED / REJECTED), and heatmap tooltips show exact numbers.
- Every interactive element has a visible focus state (a 2px ink outline with a 2px offset).
- Touch targets in the participant app are at least 44px.

## Working method
- Work in milestones. After each one, stop, tell me exactly how to test it, and wait for my confirmation before continuing.
- At the start, give me a time estimate per milestone, and flag anything at risk given a short, fixed deadline.
- Keep PROGRESS.md updated after every milestone: what is done, how to run and test it, open issues, and the next step. If a new session starts, read SPEC.md and PROGRESS.md before doing anything else.
- Ask before installing anything globally or creating accounts; give me exact click-by-click steps for anything I must do in a browser.
- Keep dependencies minimal; pin exact versions.
- Never commit secrets; check .gitignore before the first commit.
- If something in this spec is unclear, or a better approach exists, say so briefly before building.

## Milestones (in this order)
- Milestone 0: Repo structure, config files (including the use-case profile and config/local.json), design-mcp with evaluate and set_parameters, metrics, the Grasshopper script, and a headless smoke test. Acceptance:
  - From Claude in VS Code, I evaluate two options and apply one, and the Rhino model visibly changes.
  - From a normal terminal, a headless Claude Code call with agent/mcp.json successfully calls get_schema.
  This is the riskiest step and comes first.
- Milestone 1: Supabase schema, status view, auth settings, and RLS. Acceptance: both access tests pass.
- Milestone 2: Participant app with polling and facilitator console with realtime, both built with the visual design system; GitHub Pages deployment; QR code. Acceptance: three devices vote, the console updates live, devices receive a test decision via polling, and the console is readable in projection mode.
- Milestone 3: Orchestrator (run loop, tally with previous decisions, headless agent, agent_events forwarding, validation), decision cards, the agent status card and trace, simulate profiles, health-check, and test-agent. Acceptance:
  - Five consecutive clean end-to-end runs of a 3-round session.
  - Every simulate profile produces a sensible verdict.
  - Restarting the orchestrator mid-session does not reprocess any round.
- Milestone 4: Judgment test suite with a consistency report, and a load test at the expected participant count.
- Milestone 5: README with:
  - the concept section and the build-time vs session-time explanation
  - a rehearsal checklist
  - a pre-session checklist: open the Supabase project shortly before the session so it is not paused; run health-check on the day of the session; Rhino open with the Timer on; orchestrator `run` started in a terminal with a large font; console logged in with projection mode on; all rounds closed; QR code on the slide
  - troubleshooting for the most likely failures
  - a verified reset-session

After Milestone 5: bug fixes only. No new features unless I explicitly ask for one of the optional items in this spec.
