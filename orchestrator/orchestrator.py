"""Plurarch session orchestrator.

    .venv\\Scripts\\python.exe orchestrator\\orchestrator.py <command> [options]

Commands:
    run             main loop for a session (local mode: also serves the site on your Wi-Fi)
    open-round      open the next round            close-round    close the open round
    status          quick status summary
    simulate        insert realistic fake votes: --n 40 --profile consensus|split|extreme|rule_violating
    health-check    pass/fail report of every part of the loop
    test-agent      run the reviewer once on a sample proposal in a temporary state folder
    reset-session   close rounds, remove simulated data, restore the default design, start a fresh session

Backend: config/local.json "backend" = "local" (SQLite + built-in web server, no accounts) or
"supabase" (needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

from design_mcp import core  # noqa: E402
from backend_base import BackendError  # noqa: E402
import tally  # noqa: E402
from agent_runner import run_agent  # noqa: E402
from validate import validate_decision  # noqa: E402

# --- console output -------------------------------------------------------------------

if os.name == "nt":
    os.system("")  # enable ANSI colors in Windows terminals
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

COLORS = {"info": "\033[37m", "ok": "\033[92m", "warn": "\033[93m", "err": "\033[91m", "agent": "\033[96m",
          "head": "\033[1;97m", "dim": "\033[90m", "accent": "\033[95m"}
RESET = "\033[0m"
VERDICT_COLORS = {"ACCEPTED": "\033[1;92m", "MODIFIED": "\033[1;93m", "REJECTED": "\033[1;91m"}


def say(msg: str, kind: str = "info") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{COLORS['dim']}{ts}{RESET}  {COLORS.get(kind, '')}{msg}{RESET}", flush=True)


# --- configuration -----------------------------------------------------------------------

def load_env() -> dict:
    env = {}
    p = REPO / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return {**env, **{k: v for k, v in os.environ.items() if k.startswith("SUPABASE_")}}


class Ctx:
    def __init__(self, backend_override: str | None = None):
        self.local = core.load_local()
        if not self.local:
            say("config/local.json is missing: run  .venv\\Scripts\\python.exe tools\\setup_local.py", "err")
            sys.exit(2)
        self.schema = core.load_schema()
        self.brief = core.load_brief()
        self.profile = core.load_profile(self.local.get("use_case"))
        self.state_dir = core.state_dir()
        self.backend_name = backend_override or self.local.get("backend", "local")
        self.claude = self.local.get("claude") or shutil.which("claude") or "claude"
        self.python = self.local.get("python") or sys.executable
        self.port = int(self.local.get("local_port", 8787))
        self._backend = None

    @property
    def backend(self):
        if self._backend is None:
            if self.backend_name == "local":
                from backend_local import LocalBackend
                self._backend = LocalBackend(self.state_dir / "plurarch.db")
            elif self.backend_name == "supabase":
                env = load_env()
                url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_ROLE_KEY")
                if not url or not key:
                    say("Supabase mode needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env", "err")
                    sys.exit(2)
                from backend_supabase import SupabaseBackend
                self._backend = SupabaseBackend(url, key)
            else:
                say(f"unknown backend {self.backend_name!r}", "err")
                sys.exit(2)
        return self._backend

    def questions(self) -> list[dict]:
        out = []
        for i, p in enumerate(self.schema["parameters"]):
            q = {"key": p["key"], "type": p["type"], "position": i}
            if p["type"] == "choice":
                q["options"] = [o["value"] for o in p["options"]]
            else:
                q.update({"min": p["min"], "max": p["max"], "step": p["step"]})
            out.append(q)
        return out

    def ensure_session(self, title: str | None = None) -> dict:
        s = self.backend.get_active_session()
        if s:
            return s
        s = self.backend.create_session(title or self.profile.get("session_title", "Plurarch session"),
                                        self.profile["id"], self.questions())
        say(f"Started session '{s['title']}'", "ok")
        return s


# --- formatting -------------------------------------------------------------------------------

def short_params(params: dict | None, schema: dict) -> str:
    if not params:
        return "-"
    parts = []
    for p in schema["parameters"]:
        v = params.get(p["key"])
        if v is None:
            continue
        if p["type"] == "choice":
            parts.append(str(v))
        else:
            unit = p.get("unit", "")
            v = tally.fmt_num(v)
            parts.append(f"{v}{unit}" if unit in ("%", "°") else f"{v} {unit}".strip())
    return " · ".join(parts)


def diff_params(a: dict, b: dict, schema: dict) -> str:
    out = []
    for p in schema["parameters"]:
        k = p["key"]
        if a.get(k) != b.get(k):
            out.append(f"{p['label'].lower()} {tally.fmt_num(a[k]) if p['type'] != 'choice' else a[k]} → "
                       f"{tally.fmt_num(b[k]) if p['type'] != 'choice' else b[k]}")
    return ", ".join(out) or "no change"


# --- Revit (the source of truth for Langford A) ----------------------------------------------------

def revit_line(rep: dict) -> str:
    """One console/agent-event line for a Revit apply report."""
    if not rep:
        return "Revit: no report"
    if rep.get("skipped"):
        return f"Revit: skipped ({rep['skipped']})"
    if rep.get("error") and not rep.get("applied"):
        return f"Revit: not applied ({rep['error']})"
    ch = rep.get("changes") or {}
    what = (f"{ch.get('panels_retyped', 0)} panels, {ch.get('fins_created', 0)} fins created, "
            f"{ch.get('fins_deleted', 0)} removed{', finish' if ch.get('material') else ''}")
    ver = "verified" if rep.get("verified") else ("NOT verified: " + "; ".join(rep.get("mismatches") or [])[:160])
    return f"Revit: {rep.get('ops', 0)} ops ({what}), {ver} ({rep.get('s', '?')} s)"


def revit_reapply(ctx, params: dict, why: str, original_finish: bool = False) -> dict | None:
    """Best effort: bring the Revit copy back to `params` (restore after a failed round, reset)."""
    mode = (os.environ.get("PLURARCH_REVIT") or "").lower()
    if mode == "off" or not ctx.local.get("revit_apply", True):
        say(f"  Revit: skipped ({why}; Revit apply is off)", "dim")
        return None
    try:
        from design_mcp import revit_apply
        rep = revit_apply.compact(revit_apply.apply(params, original_finish=original_finish, label=why))
    except Exception as e:  # never crash the orchestrator over Revit
        rep = {"applied": False, "error": f"{type(e).__name__}: {e}"}
    say("  " + revit_line(rep), "ok" if rep.get("verified") else "warn")
    return rep


# --- agent events ---------------------------------------------------------------------------

class EventForwarder:
    """Turn design-mcp log entries into agent_events rows (6-step workflow) and console lines."""

    def __init__(self, ctx: Ctx, round_id: str):
        self.ctx, self.round_id = ctx, round_id
        self.seq = 0
        self.evaluations = 0
        self.applied = False
        self.revit: dict = {}

    def emit(self, step: int, tool: str, summary: str, kind: str = "agent") -> None:
        self.seq += 1
        say(f"  [{step}/6] {tool:<17} {summary}" if step else f"  [ · ] {tool:<17} {summary}", kind)
        try:
            self.ctx.backend.insert_agent_event({"round_id": self.round_id, "seq": self.seq, "step": step,
                                                 "tool": tool, "summary": summary[:300]})
        except Exception as e:
            say(f"  (could not store agent event: {e})", "warn")

    def on_log(self, entry: dict) -> None:
        tool, out, inp = entry.get("tool"), entry.get("output") or {}, entry.get("input") or {}
        schema = self.ctx.schema
        if tool in ("get_project_brief", "get_schema"):
            if tool == "get_project_brief":
                self.emit(1, tool, f"Read the brief: {len(self.ctx.brief['goals'])} goals, "
                                   f"{len(self.ctx.brief['hard_rules'])} hard rules")
            else:
                self.emit(1, tool, "Read the parameter schema")
        elif tool == "get_parameters":
            step = 6 if (self.applied or self.evaluations) else 1
            label = "Verified the model" if step == 6 else "Read the current design"
            self.emit(step, tool, f"{label}: {short_params(out.get('parameters'), schema)}")
        elif tool == "evaluate":
            params = short_params(inp.get("parameters"), schema)
            if not out.get("valid"):
                self.emit(3 if self.evaluations else 2, tool, f"{params}: {out.get('error') or 'invalid'}", "warn")
                return
            if entry.get("phase") == "verify":
                self.emit(6, tool, f"Verified {params} → {out.get('summary')}")
                return
            self.evaluations += 1
            step = 2 if self.evaluations == 1 else 3
            what = "Proposal" if step == 2 else f"Alternative {self.evaluations - 1}"
            self.emit(step, tool, f"{what} {params} → {out.get('summary')} "
                                  f"({out.get('evaluate_calls_used')}/{entry.get('max_evaluate', 5)})")
        elif tool == "set_parameters":
            if out.get("applied"):
                self.applied = True
                self.revit = out.get("revit") or {}
                self.emit(5, tool, f"Applied {inp.get('verdict')}: {short_params(out.get('parameters'), schema)}", "ok")
                self.emit(5, "revit", revit_line(self.revit), "ok" if self.revit.get("verified") else "warn")
            else:
                self.emit(5, tool, f"Refused: {out.get('detail') or out.get('error')}", "warn")
        elif tool == "get_revit_state":
            if out.get("available"):
                m = out.get("matches_applied_parameters")
                self.emit(6, tool, f"Checked Revit: SE glass {out.get('se_panels_glazed')}/{out.get('se_panels_total')}, "
                                   f"{out.get('lanterns_open')} lanterns open, {out.get('fins')} fins "
                                   f"{out.get('fin_depths_m')} m, finish {out.get('solid_finish')}"
                                   + ("" if m is None else (" · matches" if m else " · MISMATCH")),
                          "agent" if m in (None, True) else "warn")
            else:
                self.emit(6, tool, f"Revit not checked: {out.get('reason')}", "warn")


# --- processing a round -----------------------------------------------------------------------

def process_round(ctx: Ctx, session: dict, rnd: dict) -> dict:
    """Process one closed round exactly once. Never raises for agent problems."""
    be = ctx.backend
    existing = be.get_decision_for_round(rnd["id"])
    if existing:
        be.mark_round_processed(rnd["id"])
        say(f"Round {rnd['number']} already has a decision; marked processed", "dim")
        return existing

    say(f"Round {rnd['number']} closed · reviewing", "head")
    votes = be.get_votes(rnd["id"])
    state = core.read_state(ctx.state_dir)
    before = state["parameters"]
    previous = [d for d in be.list_decisions(session["id"]) if d.get("round_id") != rnd["id"]]
    proposal = tally.build_proposal(votes, before, previous, ctx.schema, ctx.brief)
    proposal = {"session_id": session["id"], "round_id": rnd["id"], "round_number": rnd["number"], **proposal}
    part = proposal["participation"]
    try:  # the final count, so the closed round shows how many took part
        be.update_round_participants(rnd["id"], part["participants"])
    except Exception:
        pass
    fwd = EventForwarder(ctx, rnd["id"])
    try:
        be.delete_agent_events(rnd["id"])  # a crashed earlier attempt may have left events
    except Exception:
        pass

    base = {"session_id": session["id"], "round_id": rnd["id"], "round_number": rnd["number"],
            "proposal": proposal}
    m_before = core.evaluate(before, ctx.schema, ctx.brief)["metrics"]

    if part["votes"] == 0:
        say("  No votes in this round: the design stays as it is", "warn")
        row = {**base, "status": "skipped", "verdict": None, "applied_parameters": before,
               "evidence": {}, "alternatives_considered": [], "changes": [], "rationale": None,
               "message": "No votes were cast in this round, so the design stays as it is.",
               "metrics": {"before": m_before, "proposal": m_before, "after": m_before}, "duration_s": 0}
        fwd.emit(6, "decision", "Skipped: no votes", "warn")
        d = be.insert_decision(row)
        be.mark_round_processed(rnd["id"])
        return d

    say(f"  {part['participants']} participants · {part['votes']} votes → proposal "
        f"{short_params(proposal['parameters'], ctx.schema)}", "info")
    fwd.emit(0, "orchestrator", f"Tallied {part['participants']} participants, {part['votes']} votes → "
                                f"{short_params(proposal['parameters'], ctx.schema)}", "info")

    timeout = float(ctx.profile.get("agent_timeout_s", 120))
    model = ctx.local.get("agent_model") or ctx.profile.get("agent_model", "sonnet")
    say(f"  Launching the reviewer agent ({model}, timeout {int(timeout)} s)", "agent")
    try:
        res = run_agent(proposal, state_dir=ctx.state_dir, claude=ctx.claude, python=ctx.python,
                        model=model, timeout_s=timeout, on_event=fwd.on_log)
    except Exception as e:  # never crash mid-session
        from agent_runner import AgentRun
        res = AgentRun(False, None, f"agent runner crashed: {e}", 0.0, "-")
    after_state = core.read_state(ctx.state_dir)["parameters"]

    rec, fatal, warnings = None, [], []
    if res.ok:
        rec, fatal, warnings = validate_decision(res.record, proposal["parameters"], before, after_state,
                                                 res.log_entries, ctx.schema, ctx.brief)
    else:
        fatal = [res.error or "unknown agent error"]
        if res.stderr_tail:
            say("  agent stderr: " + res.stderr_tail.replace("\n", " | ")[:400], "dim")
    for w in warnings:
        say(f"  note: {w}", "warn")

    if fatal:
        if after_state != before:
            core.write_state(before, ctx.state_dir, verdict="RESTORED", round_id=rnd["id"])
            say("  Restored the previous design", "warn")
            if fwd.revit.get("applied"):
                revit_reapply(ctx, before, f"restore after round {rnd['number']}")
        msg = "The reviewer agent could not complete this round (" + "; ".join(fatal)[:300] + \
              "). The design stays as it is."
        say("  FAILED: " + "; ".join(fatal), "err")
        row = {**base, "status": "failed", "verdict": None, "applied_parameters": before, "evidence": {},
               "alternatives_considered": [], "changes": [], "rationale": None, "message": msg,
               "metrics": {"before": m_before,
                           "proposal": core.evaluate(proposal["parameters"], ctx.schema, ctx.brief)["metrics"],
                           "after": m_before},
               "duration_s": res.duration_s}
        fwd.emit(6, "decision", "Failed: the design stays as it is", "err")
    else:
        applied = rec["applied_parameters"]
        row = {**base, "status": "ok", "verdict": rec["verdict"], "applied_parameters": applied,
               "evidence": rec["evidence"], "alternatives_considered": rec.get("alternatives_considered", []),
               "changes": rec.get("changes", []), "rationale": rec["rationale"],
               "consistency_note": rec.get("consistency_note"), "message": rec.get("what_to_vote_for"),
               "metrics": {"before": m_before, "proposal": rec["evidence"]["proposal_metrics"],
                           "after": rec["evidence"]["applied_metrics"]},
               "duration_s": res.duration_s}
        if rec["verdict"] in ("ACCEPTED", "MODIFIED"):
            row["evidence"] = {**row["evidence"], "revit": fwd.revit or {"applied": False, "error": "no report"}}
            if not (fwd.revit or {}).get("applied") and not row.get("message"):
                row["message"] = revit_line(fwd.revit)
        vc = VERDICT_COLORS.get(rec["verdict"], "")
        print(f"\n   {vc} {rec['verdict']} {RESET}  {short_params(applied, ctx.schema)}   "
              f"({res.duration_s} s)\n   {rec['rationale']}\n", flush=True)
        if rec["verdict"] == "REJECTED":
            summary = f"REJECTED: kept {short_params(applied, ctx.schema)}"
        elif rec["verdict"] == "ACCEPTED":
            summary = f"ACCEPTED: applied {short_params(applied, ctx.schema)} as voted"
        else:
            summary = f"MODIFIED: {diff_params(proposal['parameters'], applied, ctx.schema)}"
        fwd.emit(6, "decision", summary, "ok")

    d = be.insert_decision(row)
    be.mark_round_processed(rnd["id"])
    return d


# --- commands -----------------------------------------------------------------------------------

def cmd_run(ctx: Ctx, args) -> None:
    say(f"Plurarch orchestrator · backend {ctx.backend_name} · state {ctx.state_dir}", "head")
    ctx.backend.ping()
    session = ctx.ensure_session(args.title)
    if not core.parameters_path(ctx.state_dir).exists():
        core.write_state(core.default_parameters(ctx.schema), ctx.state_dir, verdict="DEFAULT")
    server = None
    if ctx.backend_name == "local" and not args.no_server:
        from local_server import LocalServer
        server = LocalServer(ctx.backend, ctx.state_dir, args.port or ctx.port, ctx.profile["id"], log=say)
        server.start()
        try:
            sys.path.insert(0, str(REPO / "tools"))
            from make_qr import make_qr
            make_qr(server.join_url, str(ctx.state_dir / "qr.png"), label=server.join_url.replace("http://", ""))
            make_qr(server.join_url, str(ctx.state_dir / "qr_plain.png"), label="")  # code only, for the console
        except Exception as e:
            say(f"(QR code not generated: {e})", "dim")
        say(f"Participants join at  {server.join_url}   (same Wi-Fi; QR: {ctx.state_dir / 'qr.png'})", "accent")
        shown = server.console_url
        if os.environ.get("PLURARCH_HIDE_KEY"):  # screen recordings: never show the facilitator key
            shown = shown.split("#", 1)[0] + "#key=•••••• (state/facilitator_key.txt)"
        say(f"Facilitator console   {shown}", "accent")
    say(f"Session '{session['title']}' · watching for closed rounds (Ctrl+C to stop)", "ok")

    last_seen = {}
    last_count = (None, -1)
    last_tick = 0.0
    count_retry_at = 0.0
    backoff = 1.0
    no_session_noted = False
    while True:
        try:
            # Sessions are created only at startup and by reset-session, never here (that raced
            # with reset-session and produced duplicate active sessions).
            session = ctx.backend.get_active_session()
            if not session:
                if not no_session_noted:
                    say("No active session: waiting (reset-session starts one)", "warn")
                    no_session_noted = True
                time.sleep(1.0)
                continue
            no_session_noted = False
            rounds = ctx.backend.list_rounds(session["id"])
            for r in rounds:
                prev = last_seen.get(r["id"])
                if prev != r["status"]:
                    if r["status"] == "open":
                        say(f"Round {r['number']} is open · voting", "accent")
                    last_seen[r["id"]] = r["status"]
            open_r = next((r for r in rounds if r["status"] == "open"), None)
            if open_r and time.monotonic() - last_tick > 2.5:
                votes = ctx.backend.get_votes(open_r["id"])
                people = len({v["participant_id"] for v in votes})
                n = (open_r["id"], len(votes))
                if n != last_count:
                    say(f"  Round {open_r['number']}: {people} participants, {len(votes)} votes", "dim")
                    last_count = n
                if people != (open_r.get("participants") or 0) and time.monotonic() > count_retry_at:
                    try:  # live count for the phones and the stage view
                        ctx.backend.update_round_participants(open_r["id"], people)
                    except Exception as e:  # e.g. Supabase migration 003 not applied yet
                        say(f"  (live count not updated: {e}; retrying in 60 s)", "warn")
                        count_retry_at = time.monotonic() + 60
                last_tick = time.monotonic()
            for r in ctx.backend.get_closed_unprocessed_rounds(session["id"]):
                process_round(ctx, session, r)
            backoff = 1.0
            time.sleep(1.0)
        except KeyboardInterrupt:
            say("Stopped.", "warn")
            if server:
                server.stop()
            return
        except BackendError as e:
            say(f"Backend problem ({e}); retrying in {int(backoff)} s", "err")
            time.sleep(backoff)
            backoff = min(backoff * 2, 15)
        except Exception as e:
            say(f"Unexpected error: {e}; continuing", "err")
            traceback.print_exc()
            time.sleep(backoff)
            backoff = min(backoff * 2, 15)


def cmd_open_round(ctx: Ctx, args) -> None:
    s = ctx.ensure_session()
    try:
        r = ctx.backend.open_round(s["id"])
        say(f"Round {r['number']} opened", "ok")
    except BackendError as e:
        say(f"Could not open a round: {e}", "err")
        sys.exit(1)


def cmd_close_round(ctx: Ctx, args) -> None:
    s = ctx.ensure_session()
    try:
        r = ctx.backend.close_round(s["id"])
        say(f"Round {r['number']} closed (the running orchestrator will review it)", "ok")
    except BackendError as e:
        say(f"Could not close the round: {e}", "err")
        sys.exit(1)


def cmd_status(ctx: Ctx, args) -> None:
    be = ctx.backend
    be.ping()
    s = be.get_active_session()
    state = core.read_state(ctx.state_dir)
    say(f"Backend {ctx.backend_name} · state folder {ctx.state_dir}", "head")
    say(f"Model: {short_params(state['parameters'], ctx.schema)} ({state.get('source')}, "
        f"{state.get('verdict') or '-'}, {state.get('updated_at') or '-'})")
    if not s:
        say("No active session (run starts one)", "warn")
        return
    rounds = be.list_rounds(s["id"])
    decisions = {d["round_id"]: d for d in be.list_decisions(s["id"])}
    say(f"Session '{s['title']}' · {len(rounds)} round(s)")
    for r in rounds:
        n_votes = len(be.get_votes(r["id"]))
        d = decisions.get(r["id"])
        dec = f"{d['status']} {d.get('verdict') or ''}".strip() if d else ("pending" if r["status"] == "closed" else "-")
        say(f"  Round {r['number']}: {r['status']:<6} · {n_votes} votes · decision {dec}"
            f"{' · processed' if r.get('processed_at') else ''}")


PROFILES = {
    # weights for the choice question, and (center, spread) for sliders (normal, snapped to the step)
    "consensus": {"infill_finish": {"concrete": 0.8, "aluminium": 0.1, "fritted_glass": 0.1},
                  "se_glass_share": (90, 4), "fin_depth": (0.6, 0.1), "skylights_open": (12, 0.8)},
    "split": {"infill_finish": {"aluminium": 0.38, "concrete": 0.35, "fritted_glass": 0.27},
              "se_glass_share": (60, 5), "fin_depth": (0, 0.1), "skylights_open": (8, 1)},
    "extreme": {"infill_finish": {"aluminium": 0.88, "concrete": 0.06, "fritted_glass": 0.06},
                "se_glass_share": (40, 2), "fin_depth": (0, 0.1), "skylights_open": (0, 0.6)},
    "rule_violating": {"infill_finish": {"concrete": 0.62, "fritted_glass": 0.28, "aluminium": 0.10},
                       "se_glass_share": (100, 2), "fin_depth": (0.3, 0.1), "skylights_open": (12, 0.8)},
    # the recorded showcase: lots of glass with thin fins -> the climate rule asks for 0.6 m (MODIFIED)
    "showcase": {"infill_finish": {"concrete": 0.75, "fritted_glass": 0.17, "aluminium": 0.08},
                 "se_glass_share": (90, 4), "fin_depth": (0.3, 0.08), "skylights_open": (12, 0.6)},
}


def simulated_votes(profile: str, n: int, round_id: str, schema: dict, seed: int | None = None) -> list[dict]:
    rng = random.Random(seed)
    prof = PROFILES[profile]
    rows = []
    for i in range(n):
        pid = f"sim-{profile[:3]}-{rng.getrandbits(40):010x}"
        for p in schema["parameters"]:
            if rng.random() < 0.04:  # a few people skip a question
                continue
            spec = prof[p["key"]]
            if p["type"] == "choice":
                opts, weights = zip(*spec.items())
                value = rng.choices(opts, weights)[0]
            else:
                center, spread = spec
                value = core.snap_to_step(p["key"], rng.gauss(center, spread), schema)
            rows.append({"round_id": round_id, "participant_id": pid, "question_key": p["key"],
                         "value": str(tally.fmt_num(value) if p["type"] != "choice" else value),
                         "is_simulated": True})
    return rows


def cmd_simulate(ctx: Ctx, args) -> None:
    be = ctx.backend
    s = ctx.ensure_session()
    rounds = be.list_rounds(s["id"])
    open_r = next((r for r in rounds if r["status"] == "open"), None)
    if not open_r:
        if not args.open:
            say("No open round. Open one first (or add --open).", "err")
            sys.exit(1)
        open_r = be.open_round(s["id"])
        say(f"Round {open_r['number']} opened", "ok")
    rows = simulated_votes(args.profile, args.n, open_r["id"], ctx.schema, args.seed)
    by_pid: dict[str, list] = {}
    for r in rows:
        by_pid.setdefault(r["participant_id"], []).append(r)
    groups = list(by_pid.values())
    say(f"Simulating {len(groups)} participants ({args.profile}) over {args.over:g} s in round {open_r['number']}", "accent")
    inserted = 0
    for i, g in enumerate(groups):
        inserted += be.insert_votes(g)
        if args.over > 0 and i < len(groups) - 1:
            time.sleep(args.over / len(groups) * random.uniform(0.3, 1.7))
    say(f"Inserted {inserted} votes", "ok")
    if args.close:
        be.close_round(s["id"])
        say(f"Round {open_r['number']} closed", "ok")


def _check(name: str, fn) -> bool:
    t0 = time.monotonic()
    try:
        detail = fn()
        say(f"PASS  {name}{(' · ' + detail) if detail else ''}  ({time.monotonic() - t0:.1f} s)", "ok")
        return True
    except Exception as e:
        say(f"FAIL  {name} · {e}", "err")
        return False


def _check_design_mcp(ctx: Ctx) -> str:
    import asyncio
    from mcp import Client, StdioServerParameters

    async def go():
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "PLURARCH_STATE_DIR": d, "PLURARCH_ROUND_ID": "health-check", "PYTHONUTF8": "1", "PLURARCH_REVIT": "off"}
            params = StdioServerParameters(command=ctx.python, args=[str(REPO / "design_mcp" / "server.py")], env=env)
            async with Client(params) as c:
                tools = sorted(t.name for t in (await c.list_tools()).tools)
                await c.call_tool("get_schema", {})
                return f"{len(tools)} tools: {', '.join(tools)}"
    return asyncio.run(go())


def _check_claude(ctx: Ctx) -> str:
    work = ctx.state_dir / "agent_work"
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as d:
        cfg = {"mcpServers": {"design": {"type": "stdio", "command": ctx.python,
                                         "args": [str(REPO / "design_mcp" / "server.py")],
                                         "env": {"PLURARCH_STATE_DIR": d, "PLURARCH_ROUND_ID": "health-check",
                                                 "PLURARCH_REVIT": "off"}}}}
        mcp_path = Path(d) / "mcp.json"
        mcp_path.write_text(json.dumps(cfg), encoding="utf-8")
        cmd = [ctx.claude, "-p", "--output-format", "json", "--mcp-config", str(mcp_path), "--strict-mcp-config",
               "--tools", "", "--allowedTools", "mcp__design__get_schema", "--permission-mode", "dontAsk",
               "--no-session-persistence", "--model", ctx.profile.get("agent_model", "sonnet"),
               "--system-prompt", "You are a health check. Call get_schema once, then answer with only the "
                                  "comma-separated parameter keys."]
        out = subprocess.run(cmd, input="Run the health check.".encode(), capture_output=True, cwd=str(work),
                             timeout=90)
        if out.returncode != 0:
            raise RuntimeError((out.stderr or out.stdout).decode("utf-8", "replace")[:300])
        res = json.loads(out.stdout.decode("utf-8", "replace"))
        text = str(res.get("result", ""))
        if res.get("is_error") or ctx.schema["parameters"][1]["key"] not in text:
            raise RuntimeError(f"unexpected answer: {text[:200]}")
        return f"{res.get('duration_ms')} ms, answer: {text.strip()[:80]}"


def cmd_health_check(ctx: Ctx, args) -> None:
    say(f"Health check · backend {ctx.backend_name}", "head")
    results = []

    def backend_up():
        ctx.backend.ping()
        return "reachable" + (" (not paused)" if ctx.backend_name == "supabase" else "")
    results.append(_check("backend is up", backend_up))

    def state_writable():
        p = ctx.state_dir / ".write_test"
        p.write_text("ok", encoding="utf-8")
        p.unlink()
        s = core.read_state(ctx.state_dir)
        return f"{ctx.state_dir} · model {short_params(s['parameters'], ctx.schema)} ({s['source']})"
    results.append(_check("shared state folder is writable", state_writable))
    results.append(_check("design-mcp responds", lambda: _check_design_mcp(ctx)))
    results.append(_check("headless Claude Code call succeeds", lambda: _check_claude(ctx)))

    def revit_doc():
        from design_mcp import revit_apply
        ok, info, reason = revit_apply.guard(timeout=10)
        if ok:
            return f"active document {info.get('title')} ({info.get('path')})"
        if ctx.local.get("revit_required"):
            raise RuntimeError(reason)
        return "not ready (best effort, decisions still apply to the phones and Rhino): " + reason
    results.append(_check("Revit copy is the active document", revit_doc))

    if ctx.backend_name == "local":
        def vote_roundtrip():
            from backend_local import LocalBackend
            tmp = Path(tempfile.mkdtemp())
            try:
                be = LocalBackend(tmp / "t.db")
                s = be.create_session("health", ctx.profile["id"], ctx.questions())
                r = be.open_round(s["id"])
                sl = next(p for p in ctx.schema["parameters"] if p["type"] == "slider")
                be.insert_votes([{"round_id": r["id"], "participant_id": "hc", "question_key": sl["key"],
                                  "value": tally.fmt_num(sl["default"])}])
                assert len(be.get_votes(r["id"])) == 1
                return "insert + read back"
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        results.append(_check("a test vote round-trips", vote_roundtrip))

        def server_check():
            import urllib.request
            url = f"http://127.0.0.1:{ctx.port}/api/status"
            try:
                with urllib.request.urlopen(url, timeout=3) as r:
                    return f"orchestrator run is serving on port {ctx.port} ({r.status})"
            except Exception:
                import socket
                with socket.socket() as so:
                    so.bind(("0.0.0.0", ctx.port))
                return f"port {ctx.port} is free for `run`"
        results.append(_check("local web server / port", server_check))
    else:
        import supabase_checks
        results.extend(supabase_checks.run_all(ctx, _check, load_env()))

    ok = all(results)
    say(("ALL CHECKS PASSED" if ok else f"{results.count(False)} CHECK(S) FAILED"), "ok" if ok else "err")
    sys.exit(0 if ok else 1)


def cmd_test_agent(ctx: Ctx, args) -> None:
    if args.proposal:
        proposal = json.loads(Path(args.proposal).read_text(encoding="utf-8"))
    else:
        start = core.read_state(ctx.state_dir)["parameters"]
        votes = simulated_votes(args.profile, args.n, "test-round", ctx.schema, args.seed)
        proposal = {"session_id": "test-session", "round_id": "test-round", "round_number": 1,
                    **tally.build_proposal(votes, start, [], ctx.schema, ctx.brief)}
    tmp = Path(tempfile.mkdtemp(prefix="plurarch-test-"))
    try:
        core.write_state(proposal["current_parameters"], tmp, verdict="TEST")
        say(f"Test agent · temporary state {tmp} (the live model is untouched)", "head")
        say(f"Proposal {short_params(proposal['parameters'], ctx.schema)} · current "
            f"{short_params(proposal['current_parameters'], ctx.schema)}")
        model = args.model or ctx.local.get("agent_model") or ctx.profile.get("agent_model", "sonnet")
        res = run_agent(proposal, state_dir=tmp, claude=ctx.claude, python=ctx.python, model=model,
                        timeout_s=float(ctx.profile.get("agent_timeout_s", 120)),
                        extra_env={"PLURARCH_REVIT": "off"},   # a test: never touch the Revit model
                        on_event=lambda e: say(f"  {e['seq']:>2} {e['tool']:<17} {e.get('phase')} "
                                               f"{(e.get('output') or {}).get('summary', '')}", "agent"))
        if not res.ok:
            say(f"FAILED after {res.duration_s} s: {res.error}", "err")
            if res.stderr_tail:
                say(res.stderr_tail, "dim")
            sys.exit(1)
        after = core.read_state(tmp)["parameters"]
        rec, fatal, warnings = validate_decision(res.record, proposal["parameters"], proposal["current_parameters"],
                                                 after, res.log_entries, ctx.schema, ctx.brief)
        for w in warnings:
            say(f"note: {w}", "warn")
        print(json.dumps(rec or res.record, indent=2, ensure_ascii=False))
        if fatal:
            say("INVALID: " + "; ".join(fatal), "err")
            sys.exit(1)
        vc = VERDICT_COLORS.get(rec["verdict"], "")
        print(f"\n   {vc} {rec['verdict']} {RESET}  in {res.duration_s} s ({model})\n", flush=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_reset_session(ctx: Ctx, args) -> None:
    be = ctx.backend
    s = be.get_active_session()
    if not args.yes:
        ans = input("Close open rounds, delete simulated votes, end the session and restore the default design? [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            say("Cancelled", "warn")
            return
    # Every active (non-test) session, so stray duplicates are cleaned up too.
    olds = be.list_active_sessions()
    for old in olds:
        try:
            r = be.close_round(old["id"])
            be.mark_round_processed(r["id"])  # closed by the reset, not reviewed
            say(f"Closed round {r['number']}", "ok")
        except BackendError:
            pass
        n = be.delete_simulated_votes(old["id"])
        say(f"Deleted {n} simulated votes", "ok")
    core.write_state(core.default_parameters(ctx.schema), ctx.state_dir, verdict="DEFAULT")
    say(f"Restored the default design: {short_params(core.default_parameters(ctx.schema), ctx.schema)}", "ok")
    revit_reapply(ctx, core.default_parameters(ctx.schema), "reset-session (as built)", original_finish=True)
    # Create the new session BEFORE ending the old ones: there is never a moment without an active
    # session, so a running orchestrator can never race us into creating a duplicate.
    s2 = be.create_session(args.title or ctx.profile.get("session_title", "Plurarch session"),
                           ctx.profile["id"], ctx.questions())
    for old in olds:
        be.end_session(old["id"])
        say(f"Ended session '{old['title']}' ({old['id'][:8]})", "ok")
    say(f"Ready: fresh session '{s2['title']}' with no rounds", "ok")


def main() -> None:
    ap = argparse.ArgumentParser(prog="orchestrator", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=["local", "supabase"], help="override config/local.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--port", type=int)
    p.add_argument("--title")
    p.add_argument("--no-server", action="store_true", help="local mode: do not serve the site")
    sub.add_parser("open-round")
    sub.add_parser("close-round")
    sub.add_parser("status")
    p = sub.add_parser("simulate")
    p.add_argument("--n", type=int, default=40)
    p.add_argument("--profile", choices=sorted(PROFILES), default="consensus")
    p.add_argument("--over", type=float, default=15.0, help="spread the votes over this many seconds")
    p.add_argument("--open", action="store_true", help="open a round first if none is open")
    p.add_argument("--close", action="store_true", help="close the round afterwards")
    p.add_argument("--seed", type=int)
    sub.add_parser("health-check")
    p = sub.add_parser("test-agent")
    p.add_argument("--proposal", help="a proposal JSON file (default: simulate one)")
    p.add_argument("--profile", choices=sorted(PROFILES), default="rule_violating")
    p.add_argument("--n", type=int, default=40)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--model")
    p = sub.add_parser("reset-session")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--title")
    args = ap.parse_args()
    ctx = Ctx(args.backend)
    {"run": cmd_run, "open-round": cmd_open_round, "close-round": cmd_close_round, "status": cmd_status,
     "simulate": cmd_simulate, "health-check": cmd_health_check, "test-agent": cmd_test_agent,
     "reset-session": cmd_reset_session}[args.cmd](ctx, args)


if __name__ == "__main__":
    main()
