"""Turn a round's votes into the structured proposal the reviewer agent receives.

Choice questions: plurality winner (ties go to the current value, then config order), full counts and
percentages. Sliders: median rounded to the step, the full distribution, quartiles, min and max.
Consensus: the winner's share (choice) or the share of votes within one step of the median (slider).
Invalid values are ignored (the database also rejects them).
"""
from __future__ import annotations

import statistics

from design_mcp import core


def fmt_num(v):
    """45.0 -> 45, 1.5 -> 1.5 (as used for distribution keys and display)."""
    f = float(v)
    return int(f) if f.is_integer() else round(f, 3)


def consensus_level(share: float, n: int, brief: dict) -> str:
    if n == 0:
        return "none"
    c = brief["consensus"]
    if share >= c["strong"]:
        return "strong"
    if share < c["weak"]:
        return "weak"
    return "moderate"


def tally_choice(p: dict, values: list[str], current, brief: dict) -> dict:
    options = [o["value"] for o in p["options"]]
    counts = {o: 0 for o in options}
    for v in values:
        if v in counts:
            counts[v] += 1
    n = sum(counts.values())
    if n == 0:
        return {"type": "choice", "n": 0, "winner": current, "counts": counts,
                "percentages": {o: 0.0 for o in options}, "consensus": 0.0,
                "consensus_level": "none", "leading_options": [current], "note": "no votes: current value kept"}
    top = max(counts.values())
    tied = [o for o in options if counts[o] == top]
    winner = current if current in tied else tied[0]
    share = top / n
    gap = brief["consensus"]["leading_option_gap"]
    leading = [o for o in options if counts[o] / n >= share - gap - 1e-9]
    out = {"type": "choice", "n": n, "winner": winner, "counts": counts,
           "percentages": {o: round(100.0 * counts[o] / n, 1) for o in options},
           "consensus": round(share, 3), "consensus_level": consensus_level(share, n, brief),
           "leading_options": leading}
    if len(tied) > 1:
        out["tie"] = tied
    return out


def tally_slider(p: dict, values: list, current, brief: dict, schema: dict | None = None) -> dict:
    nums = []
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if p["min"] - 1e-9 <= f <= p["max"] + 1e-9:
            nums.append(f)
    steps = []
    x = p["min"]
    while x <= p["max"] + 1e-9:
        steps.append(fmt_num(round(x, 6)))
        x += p["step"]
    counts = {str(s): 0 for s in steps}
    for f in nums:
        key = str(fmt_num(core.snap_to_step(p["key"], f, schema)))
        counts[key] = counts.get(key, 0) + 1
    n = len(nums)
    if n == 0:
        return {"type": "slider", "n": 0, "median": current, "counts": counts, "consensus": 0.0,
                "consensus_level": "none", "note": "no votes: current value kept"}
    median = core.snap_to_step(p["key"], statistics.median(nums), schema)
    band = brief["consensus"].get("slider_band_steps", 1) * p["step"]
    near = sum(1 for f in nums if abs(f - median) <= band + 1e-9)
    share = near / n
    if n >= 2:
        q1, _, q3 = statistics.quantiles(nums, n=4, method="inclusive")
    else:
        q1 = q3 = nums[0]
    return {"type": "slider", "n": n, "median": median, "counts": counts,
            "min": fmt_num(min(nums)), "max": fmt_num(max(nums)),
            "q1": fmt_num(round(q1, 2)), "q3": fmt_num(round(q3, 2)),
            "mean": round(statistics.fmean(nums), 2),
            "consensus": round(share, 3), "consensus_level": consensus_level(share, n, brief)}


def build_proposal(votes: list[dict], current: dict, previous_decisions: list[dict],
                   schema: dict | None = None, brief: dict | None = None) -> dict:
    schema = schema or core.load_schema()
    brief = brief or core.load_brief()
    by_q: dict[str, list] = {}
    participants = set()
    for v in votes:
        by_q.setdefault(v["question_key"], []).append(v["value"])
        participants.add(v["participant_id"])
    tally, params = {}, {}
    for p in schema["parameters"]:
        vals = by_q.get(p["key"], [])
        if p["type"] == "choice":
            t = tally_choice(p, vals, current[p["key"]], brief)
            params[p["key"]] = t["winner"]
        else:
            t = tally_slider(p, vals, current[p["key"]], brief, schema)
            params[p["key"]] = t["median"]
        tally[p["key"]] = t
    norm, errors = core.validate_parameters(params, schema)
    if errors:  # cannot happen with valid config; keep the current design rather than crash
        norm = dict(current)
    prev = [{
        "round_number": d.get("round_number"), "status": d.get("status"), "verdict": d.get("verdict"),
        "applied_parameters": d.get("applied_parameters"), "rationale": d.get("rationale") or d.get("message"),
    } for d in previous_decisions]
    return {
        "parameters": norm,
        "current_parameters": current,
        "tally": tally,
        "participation": {"participants": len(participants), "votes": len(votes),
                          "by_question": {k: t["n"] for k, t in tally.items()}},
        "previous_decisions": prev,
    }
