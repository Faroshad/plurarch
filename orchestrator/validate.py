"""Check the agent's decision record independently of the agent.

Fatal problems (the decision becomes "failed" and the previous design is restored):
- the record does not match agent/decision_schema.json
- applied parameters are invalid or break a hard rule
- the verdict does not match what happened (ACCEPTED must equal the proposal; REJECTED must keep the
  design; ACCEPTED/MODIFIED need a successful set_parameters in this run's tool log)
- the model file does not hold the applied parameters

Cosmetic problems are fixed and reported as warnings: evidence metrics are replaced by freshly computed
values, missing `changes` entries are added, the rationale is trimmed to 3 sentences, a MODIFIED record
that equals the proposal is relabelled ACCEPTED.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema

from design_mcp import core

REPO = Path(__file__).resolve().parent.parent
_SCHEMA = None


def decision_schema() -> dict:
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = json.loads((REPO / "agent" / "decision_schema.json").read_text(encoding="utf-8"))
    return _SCHEMA


def trim_sentences(text: str, n: int = 3) -> str:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", text.strip())
    return " ".join(parts[:n]).strip()


def validate_decision(record, proposal_params: dict, before_params: dict, state_params: dict,
                      log_entries: list, schema=None, brief=None):
    """Return (clean_record, fatal_problems, warnings)."""
    schema = schema or core.load_schema()
    brief = brief or core.load_brief()
    fatal, warnings = [], []
    if not isinstance(record, dict):
        return None, ["no decision record"], warnings
    try:
        jsonschema.Draft7Validator(decision_schema()).validate(record)
    except jsonschema.ValidationError as e:
        return None, [f"decision record does not match the schema: {e.message} at {list(e.absolute_path)}"], warnings

    rec = json.loads(json.dumps(record))
    verdict = rec["verdict"]

    norm_prop, errs = core.validate_parameters(rec["proposal"], schema)
    if errs or norm_prop != proposal_params:
        warnings.append("record.proposal differs from the tallied proposal; using the tallied proposal")
    rec["proposal"] = proposal_params

    applied, errs = core.validate_parameters(rec["applied_parameters"], schema)
    if errs:
        return None, ["applied_parameters invalid: " + "; ".join(errs)], warnings
    rec["applied_parameters"] = applied
    ev_applied = core.evaluate(applied, schema, brief)
    if not ev_applied["hard_rules_pass"]:
        fatal.append("applied parameters break hard rules: " + ", ".join(ev_applied["failed_rules"]))

    applied_ok = [e for e in log_entries if e.get("tool") == "set_parameters" and e.get("ok")]
    if verdict == "MODIFIED" and applied == proposal_params:
        warnings.append("MODIFIED record equals the proposal; relabelled ACCEPTED")
        rec["verdict"] = verdict = "ACCEPTED"
    if verdict == "ACCEPTED" and applied != proposal_params:
        fatal.append("ACCEPTED but the applied parameters differ from the proposal")
    if verdict == "REJECTED":
        if applied != before_params:
            warnings.append("REJECTED record listed different applied parameters; using the unchanged design")
            rec["applied_parameters"] = applied = before_params
        if state_params != before_params:
            fatal.append("REJECTED but the model file changed")
        if applied_ok:
            fatal.append("REJECTED but set_parameters was called successfully")
    else:
        if not applied_ok:
            fatal.append(f"{verdict} but set_parameters never succeeded")
        elif applied_ok[-1].get("output", {}).get("parameters") != applied:
            fatal.append("the record's applied parameters differ from what set_parameters applied")
        if state_params != applied:
            fatal.append("the model file does not hold the applied parameters")

    explore = [e for e in log_entries if e.get("tool") == "evaluate" and e.get("phase") == "explore" and e.get("ok")]
    limit = brief.get("agent_limits", {}).get("max_evaluate_calls", 5)
    if len(explore) > limit:
        fatal.append(f"{len(explore)} evaluate calls, limit {limit}")
    if verdict == "MODIFIED" and len(explore) < 3:
        warnings.append(f"MODIFIED after only {len(explore) - 1} alternative(s); the brief asks for at least 2")

    # Replace the agent's evidence numbers with freshly computed ones (display must be exact).
    ev_prop = core.evaluate(proposal_params, schema, brief)
    computed = {"proposal_metrics": ev_prop["metrics"], "applied_metrics": ev_applied["metrics"],
                "failed_rules": ev_prop["failed_rules"], "failed_goals": ev_prop["failed_goals"]}
    for k, v in computed.items():
        if rec["evidence"].get(k) != v:
            if k.endswith("metrics") and rec["evidence"].get(k):
                diffs = [m for m in v if rec["evidence"][k].get(m) != v[m]]
                if diffs:
                    warnings.append(f"evidence.{k} corrected for {', '.join(diffs)}")
            rec["evidence"][k] = v
    for alt in rec.get("alternatives_considered", []):
        a_norm, a_err = core.validate_parameters(alt.get("parameters"), schema)
        if not a_err:
            a_ev = core.evaluate(a_norm, schema, brief)
            alt["parameters"], alt["metrics"], alt["hard_rules_pass"] = a_norm, a_ev["metrics"], a_ev["hard_rules_pass"]
            alt["goals_status"] = a_ev["goals_status"]

    listed = {c["parameter"] for c in rec.get("changes", [])}
    rec["changes"] = [c for c in rec.get("changes", []) if c["parameter"] in proposal_params
                      and proposal_params[c["parameter"]] != applied.get(c["parameter"])]
    if verdict == "MODIFIED":
        for k, v in proposal_params.items():
            if applied[k] != v and k not in listed:
                warnings.append(f"changes entry added for {k}")
                rec["changes"].append({"parameter": k, "from": v, "to": applied[k], "reason": ""})
    else:
        rec["changes"] = []

    trimmed = trim_sentences(rec["rationale"], 3)
    if trimmed != rec["rationale"].strip():
        warnings.append("rationale trimmed to 3 sentences")
    rec["rationale"] = trimmed
    return (None if fatal else rec), fatal, warnings
