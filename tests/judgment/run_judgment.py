"""Judgment test suite: run each scenario through the REAL reviewer agent several times.

    .venv\\Scripts\\python.exe tests\\judgment\\run_judgment.py [--runs 3] [--parallel 3] [--only id,id] [--model sonnet]
    .venv\\Scripts\\python.exe tests\\judgment\\run_judgment.py --dry     # tally + deterministic check only, no agent

Every run uses its own temporary state folder (the live model is untouched). Reports, per scenario:
verdict distribution, pass rate against the expected verdicts, consistency (share of runs that agree
with the most common verdict), whether the rationale cites real metric values from the tool outputs,
and the run time. Writes tests/judgment/report.md and report.json.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "orchestrator"))

from design_mcp import core  # noqa: E402
import tally  # noqa: E402
from agent_runner import run_agent  # noqa: E402
from validate import validate_decision  # noqa: E402

NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")


def expand_votes(spec: dict) -> list[dict]:
    rows = []
    for key, counts in spec.items():
        i = 0
        for value, n in counts.items():
            for _ in range(n):
                rows.append({"participant_id": f"p{i:03d}", "question_key": key, "value": str(value)})
                i += 1
    return rows


def build(sc: dict, schema, brief) -> dict:
    prop = tally.build_proposal(expand_votes(sc["votes"]), sc["current"], sc.get("previous_decisions", []),
                                schema, brief)
    return {"session_id": "judgment", "round_id": f"judgment-{sc['id']}",
            "round_number": sc.get("round_number", 1), **prop}


def known_numbers(log_entries: list, proposal: dict, brief: dict) -> set[float]:
    """Every number the agent could legitimately cite: tool outputs, the proposal, the brief."""
    nums: set[float] = set()

    def walk(x):
        if isinstance(x, bool):
            return
        if isinstance(x, (int, float)):
            nums.add(round(float(x), 3))
        elif isinstance(x, dict):
            for k, v in x.items():
                walk(v)
                if isinstance(k, str):
                    try:
                        nums.add(round(float(k), 3))
                    except ValueError:
                        pass
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, str):
            for m in NUM.findall(x):
                nums.add(round(float(m), 3))
    for e in log_entries:
        walk(e.get("input"))
        walk(e.get("output"))
    walk(proposal)
    walk(brief)
    return nums


def metric_values(log_entries: list) -> set[float]:
    vals = set()
    for e in log_entries:
        out = e.get("output") or {}
        for src in (out.get("metrics"), (out.get("after") or {}).get("metrics"), (out.get("before") or {}).get("metrics")):
            if isinstance(src, dict):
                vals.update(round(float(v), 3) for v in src.values() if isinstance(v, (int, float)))
    return vals


def one_run(sc: dict, proposal: dict, model: str, timeout: float, schema, brief, local) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix=f"plurarch-judge-{sc['id']}-"))
    try:
        core.write_state(sc["current"], tmp, verdict="TEST")
        res = run_agent(proposal, state_dir=tmp, claude=local.get("claude", "claude"),
                        python=local.get("python", sys.executable), model=model, timeout_s=timeout)
        out = {"ok": res.ok, "error": res.error, "duration_s": res.duration_s, "verdict": None}
        if not res.ok:
            return out
        after = core.read_state(tmp)["parameters"]
        rec, fatal, warnings = validate_decision(res.record, proposal["parameters"], sc["current"], after,
                                                 res.log_entries, schema, brief)
        out.update({"fatal": fatal, "warnings": warnings})
        if fatal:
            out["ok"] = False
            out["error"] = "; ".join(fatal)
            out["verdict"] = (res.record or {}).get("verdict")
            return out
        cited = [round(float(x), 3) for x in NUM.findall(rec["rationale"])]
        known = known_numbers(res.log_entries, proposal, brief)
        metrics = metric_values(res.log_entries)
        applied_ok = all(rec["applied_parameters"].get(k) == v for k, v in sc.get("applied", {}).items())
        out.update({
            "verdict": rec["verdict"],
            "applied": rec["applied_parameters"],
            "applied_ok": applied_ok,
            "rationale": rec["rationale"],
            "consistency_note": rec.get("consistency_note") or "",
            "cites_metric": any(c in metrics for c in cited),
            "unverified_numbers": [c for c in cited if c not in known],
            "evaluate_calls": sum(1 for e in res.log_entries if e.get("tool") == "evaluate" and e.get("counted")),
        })
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--only", help="comma-separated scenario ids")
    ap.add_argument("--model")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    schema, brief, local = core.load_schema(), core.load_brief(), core.load_local()
    profile = core.load_profile(local.get("use_case"))
    model = args.model or local.get("agent_model") or profile.get("agent_model", "sonnet")
    timeout = float(profile.get("agent_timeout_s", 120))
    scenarios = json.loads((HERE / "scenarios.json").read_text(encoding="utf-8"))["scenarios"]
    if args.only:
        wanted = set(args.only.split(","))
        scenarios = [s for s in scenarios if s["id"] in wanted]

    proposals = {s["id"]: build(s, schema, brief) for s in scenarios}
    if args.dry:
        for s in scenarios:
            p = proposals[s["id"]]
            ev = core.evaluate(p["parameters"], schema, brief)
            levels = {k: t["consensus_level"] for k, t in p["tally"].items()}
            print(f"{s['id']:<34} {p['parameters']}  expect {s['expect']}\n{'':34} {ev['summary']}  {levels}")
        return

    jobs = [(s, i) for s in scenarios for i in range(args.runs)]
    print(f"{len(scenarios)} scenarios x {args.runs} runs = {len(jobs)} agent runs ({model}, {args.parallel} in parallel)")
    results: dict[str, list] = {s["id"]: [] for s in scenarios}
    t0 = time.monotonic()

    def job(item):
        s, i = item
        r = one_run(s, proposals[s["id"]], model, timeout, schema, brief, local)
        mark = "ok " if (r.get("verdict") in s["expect"] and r.get("ok")) else "BAD"
        print(f"  {mark} {s['id']:<34} run {i + 1}: {r.get('verdict') or r.get('error')} ({r['duration_s']} s)", flush=True)
        return s["id"], r

    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as ex:
        for sid, r in ex.map(job, jobs):
            results[sid].append(r)

    rows, lines = [], []
    lines.append(f"# Judgment test report\n\n{datetime.now():%Y-%m-%d %H:%M} · model `{model}` · "
                 f"{args.runs} runs per scenario · {time.monotonic() - t0:.0f} s total\n")
    lines.append("| Scenario | Expected | Verdicts | Pass | Consistency | Cites metrics | Unverified numbers | Avg time |")
    lines.append("|---|---|---|---|---|---|---|---|")
    total_pass = total = 0
    for s in scenarios:
        rs = results[s["id"]]
        verdicts = Counter(r.get("verdict") or "FAILED" for r in rs)
        passed = sum(1 for r in rs if r.get("ok") and r.get("verdict") in s["expect"] and r.get("applied_ok", True)
                     and (not s.get("needs_consistency_note") or r.get("consistency_note")))
        total_pass += passed
        total += len(rs)
        consistency = verdicts.most_common(1)[0][1] / len(rs) if rs else 0
        cites = sum(1 for r in rs if r.get("cites_metric"))
        unverified = sorted({n for r in rs for n in r.get("unverified_numbers", [])})
        avg = sum(r["duration_s"] for r in rs) / len(rs) if rs else 0
        lines.append(f"| {s['id']} | {'/'.join(s['expect'])} | "
                     f"{', '.join(f'{k} x{v}' for k, v in verdicts.items())} | {passed}/{len(rs)} | "
                     f"{consistency:.0%} | {cites}/{len(rs)} | {', '.join(map(str, unverified)) or '-'} | {avg:.1f} s |")
        rows.append({"scenario": s, "results": rs, "passed": passed, "consistency": consistency})
    lines.append(f"\n**Overall: {total_pass}/{total} runs passed.**\n")
    lines.append("## Rationales (first run of each scenario)\n")
    for row in rows:
        r = next((x for x in row["results"] if x.get("rationale")), None)
        if r:
            note = f"\n  - consistency: {r['consistency_note']}" if r.get("consistency_note") else ""
            lines.append(f"- **{row['scenario']['id']}** ({r['verdict']}): {r['rationale']}{note}")
        else:
            err = next((x.get("error") for x in row["results"]), "")
            lines.append(f"- **{row['scenario']['id']}**: no valid decision ({err})")
    (HERE / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (HERE / "report.json").write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
    print("\n".join(lines[:len(scenarios) + 4]))
    print(f"\nOverall {total_pass}/{total} · report: {HERE / 'report.md'}")


if __name__ == "__main__":
    main()
