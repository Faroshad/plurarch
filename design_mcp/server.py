"""design-mcp: the only tools the Plurarch reviewer agent can use.

Tools: get_schema, get_project_brief, get_parameters, evaluate (side-effect free), set_parameters.
No delete tools, no code execution, no file access beyond the shared state folder.

Environment (set per run by the orchestrator; every run starts a fresh process, so limits reset):
    PLURARCH_STATE_DIR     shared state folder (else config/local.json, else <repo>/state)
    PLURARCH_ROUND_ID      round id written into every log line
    PLURARCH_RUN_ID        unique id of this agent run
    PLURARCH_MAX_EVALUATE  override the brief's max_evaluate_calls (testing only)

Run: <python> design_mcp/server.py   (stdio transport)
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from design_mcp import core  # noqa: E402

SCHEMA = core.load_schema()
BRIEF = core.load_brief()
LIMITS = BRIEF.get("agent_limits", {})
MAX_EVALUATE = int(os.environ.get("PLURARCH_MAX_EVALUATE") or LIMITS.get("max_evaluate_calls", 5))
VERIFY_EXEMPT = bool(LIMITS.get("verify_evaluate_exempt", True))

RUN = {
    "run_id": os.environ.get("PLURARCH_RUN_ID") or uuid.uuid4().hex[:12],
    "round_id": os.environ.get("PLURARCH_ROUND_ID") or "manual",
    "seq": 0,
    "evaluate_calls": 0,   # valid exploratory evaluations
    "applied": False,      # set_parameters succeeded in this run
    "verify_ready": False,  # get_parameters was called after evaluating (step 6, also for REJECTED)
    "verify_used": False,  # the one exempt verification evaluate
}


def _param_help() -> str:
    parts = []
    for p in SCHEMA["parameters"]:
        if p["type"] == "choice":
            parts.append(f"{p['key']}: one of {[o['value'] for o in p['options']]}")
        else:
            parts.append(f"{p['key']}: number {p['min']}..{p['max']} in steps of {p['step']} ({p['unit']})")
    return "; ".join(parts)


PARAM_HELP = _param_help()


def _log(tool: str, inputs: dict, output: Any, ok: bool, phase: str) -> None:
    RUN["seq"] += 1
    try:
        core.append_log({
            "ts": core.now_iso(), "run_id": RUN["run_id"], "round_id": RUN["round_id"],
            "seq": RUN["seq"], "tool": tool, "phase": phase, "ok": ok,
            "evaluate_calls": RUN["evaluate_calls"], "max_evaluate": MAX_EVALUATE,
            "input": inputs, "output": output,
        })
    except Exception:
        pass  # logging must never break a tool call


def _phase() -> str:
    return "verify" if RUN["applied"] else "explore"


server = MCPServer(
    "design",
    instructions=(
        "Design tools for the Plurarch pavilion. Read the brief, evaluate parameter sets "
        "(side-effect free, limited per round), then apply one with set_parameters. "
        "All metrics are indicative proxies."
    ),
)


@server.tool(description="Return the design parameters: keys, labels, types, allowed options, ranges and steps.")
def get_schema() -> dict:
    out = SCHEMA
    _log("get_schema", {}, {"parameters": [p["key"] for p in SCHEMA["parameters"]]}, True, _phase())
    return out


@server.tool(description="Return the project brief: narrative, goals with thresholds, the close trade-off margin, "
                         "hard rules, consensus settings, modification limits and agent limits.")
def get_project_brief() -> dict:
    _log("get_project_brief", {}, {"goals": [g["id"] for g in BRIEF["goals"]],
                                   "hard_rules": [r["id"] for r in BRIEF["hard_rules"]]}, True, _phase())
    return BRIEF


@server.tool(description="Return the currently applied design parameters and their metrics.")
def get_parameters() -> dict:
    state = core.read_state()
    ev = core.evaluate(state["parameters"], SCHEMA, BRIEF)
    out = {
        "parameters": state["parameters"],
        "updated_at": state.get("updated_at"),
        "verdict": state.get("verdict"),
        "source": state.get("source"),
        "metrics": ev.get("metrics"),
    }
    if state.get("warning"):
        out["warning"] = state["warning"]
    if RUN["evaluate_calls"] or RUN["applied"]:
        RUN["verify_ready"] = True
    _log("get_parameters", {}, out, True, "verify" if RUN["verify_ready"] else "explore")
    return out


def _is_verification(parameters) -> bool:
    """The one exempt evaluate: after set_parameters, or (REJECTED) of the unchanged current design
    after get_parameters."""
    if RUN["verify_used"] or not VERIFY_EXEMPT:
        return False
    if RUN["applied"]:
        return True
    if not RUN["verify_ready"]:
        return False
    norm, errors = core.validate_parameters(parameters, SCHEMA)
    return not errors and norm == core.read_state()["parameters"]


@server.tool(description=(
    "Evaluate one complete parameter set WITHOUT changing the model. Returns indicative metrics "
    "(daylight, cooling, cost, carbon, shading), each hard rule (pass/fail with reason) and each goal "
    "(pass / marginal / fail against its threshold). Limited to a few calls per round; the response says "
    "how many remain. One extra call after set_parameters is allowed for verification. "
    "parameters must contain every key: " + PARAM_HELP
))
def evaluate(parameters: dict[str, Any]) -> dict:
    inputs = {"parameters": parameters}
    phase = _phase()
    if _is_verification(parameters):
        result = core.evaluate(parameters, SCHEMA, BRIEF)
        if result.get("valid"):
            RUN["verify_used"] = True
        result["evaluate_calls_used"] = RUN["evaluate_calls"]
        result["evaluate_calls_remaining"] = max(0, MAX_EVALUATE - RUN["evaluate_calls"])
        result["counted"] = False
        _log("evaluate", inputs, result, result.get("valid", False), "verify")
        return result
    if RUN["evaluate_calls"] >= MAX_EVALUATE:
        out = {"valid": False, "error": "evaluate_limit_reached",
               "detail": f"All {MAX_EVALUATE} evaluate calls for this round are used. Decide with the "
                         "evaluations you already have."}
        _log("evaluate", inputs, out, False, phase)
        return out
    result = core.evaluate(parameters, SCHEMA, BRIEF)
    if result.get("valid"):
        RUN["evaluate_calls"] += 1  # malformed calls do not use up the budget
    result["evaluate_calls_used"] = RUN["evaluate_calls"]
    result["evaluate_calls_remaining"] = max(0, MAX_EVALUATE - RUN["evaluate_calls"])
    result["counted"] = bool(result.get("valid"))
    _log("evaluate", inputs, result, result.get("valid", False), phase)
    return result


@server.tool(description=(
    "Apply a parameter set to the live model. verdict must be ACCEPTED or MODIFIED (a REJECTED verdict "
    "keeps the current design: do not call this tool). Every hard rule is re-checked here and anything that "
    "fails is refused, whatever the verdict says. Can succeed only once per round. rationale: the short "
    "plain-language reason. parameters must contain every key: " + PARAM_HELP
))
def set_parameters(parameters: dict[str, Any], verdict: str, rationale: str) -> dict:
    inputs = {"parameters": parameters, "verdict": verdict, "rationale": rationale}

    def refuse(error: str, detail: str, **extra) -> dict:
        out = {"applied": False, "error": error, "detail": detail, **extra}
        _log("set_parameters", inputs, out, False, _phase())
        return out

    if verdict not in ("ACCEPTED", "MODIFIED"):
        return refuse("invalid_verdict", "verdict must be ACCEPTED or MODIFIED. For REJECTED, do not call "
                                         "set_parameters; the current design stays.")
    if RUN["applied"]:
        return refuse("already_applied", "Parameters were already applied in this round.")
    if not isinstance(rationale, str) or not rationale.strip():
        return refuse("missing_rationale", "A short rationale is required.")

    result = core.evaluate(parameters, SCHEMA, BRIEF)
    if not result.get("valid"):
        return refuse("invalid_parameters", "; ".join(result.get("errors", [])))
    if not result["hard_rules_pass"]:
        failed = [r for r in result["hard_rules"] if not r["pass"]]
        return refuse("hard_rule_failed", "Refused: " + "; ".join(f"{r['label']}: {r['reason']}" for r in failed),
                      failed_rules=[r["id"] for r in failed])

    before = core.read_state()
    before_metrics = core.evaluate(before["parameters"], SCHEMA, BRIEF).get("metrics")
    path = core.write_state(result["parameters"], round_id=RUN["round_id"], run_id=RUN["run_id"],
                            verdict=verdict, rationale=rationale.strip()[:600])
    RUN["applied"] = True
    out = {
        "applied": True,
        "parameters": result["parameters"],
        "before": {"parameters": before["parameters"], "metrics": before_metrics},
        "after": {"metrics": result["metrics"]},
        "summary": result["summary"],
        "file": path.name,
    }
    _log("set_parameters", inputs, out, True, "apply")
    return out


if __name__ == "__main__":
    server.run()
