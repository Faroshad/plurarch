"""Deterministic core shared by design-mcp and the orchestrator.

- config and path loading (config/*.json, config/local.json, environment overrides)
- parameter validation against config/parameters.json
- hard-rule and goal checks from config/project_brief.json
- evaluate(): metrics + rule results + goal results, side-effect free
- atomic reads/writes of <state_dir>/parameters.json and the call log <state_dir>/log.jsonl
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from design_mcp.metrics import compute_metrics

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"


# --- config -------------------------------------------------------------------

def _read_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_local() -> dict:
    """Machine-specific settings (git-ignored). Missing file = empty settings."""
    p = CONFIG_DIR / "local.json"
    return _read_json(p) if p.exists() else {}


def load_schema() -> dict:
    return _read_json(CONFIG_DIR / "parameters.json")


def load_brief() -> dict:
    return _read_json(CONFIG_DIR / "project_brief.json")


def load_profile(name: str | None = None) -> dict:
    name = name or os.environ.get("PLURARCH_USE_CASE") or load_local().get("use_case") or "live_presentation"
    return _read_json(CONFIG_DIR / "use_cases" / f"{name}.json")


def state_dir() -> Path:
    """PLURARCH_STATE_DIR > config/local.json state_dir > <repo>/state."""
    d = os.environ.get("PLURARCH_STATE_DIR") or load_local().get("state_dir") or str(REPO_ROOT / "state")
    path = Path(d)
    path.mkdir(parents=True, exist_ok=True)
    return path


def params_by_key(schema: dict | None = None) -> dict:
    schema = schema or load_schema()
    return {p["key"]: p for p in schema["parameters"]}


def default_parameters(schema: dict | None = None) -> dict:
    return {p["key"]: p["default"] for p in (schema or load_schema())["parameters"]}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


# --- validation -----------------------------------------------------------------

def _on_step(value: float, lo: float, step: float) -> bool:
    k = (value - lo) / step
    return abs(k - round(k)) < 1e-6


def _clean_number(value: float, step: float):
    """40.0 -> 40 when the step is whole; otherwise round to the step's decimals."""
    if float(step).is_integer():
        return int(round(value))
    text = repr(float(step))
    decimals = len(text.split(".")[1]) if "." in text else 0
    return round(float(value), decimals)


def validate_parameters(params, schema: dict | None = None):
    """Return (normalized_params, errors). Every key is required; no extra keys."""
    spec = params_by_key(schema)
    errors: list[str] = []
    if not isinstance(params, dict):
        return None, ["parameters must be an object with keys: " + ", ".join(spec)]
    out = {}
    for key in params:
        if key not in spec:
            errors.append(f"unknown parameter '{key}'")
    for key, p in spec.items():
        if key not in params:
            errors.append(f"missing parameter '{key}'")
            continue
        v = params[key]
        if p["type"] == "choice":
            allowed = [o["value"] for o in p["options"]]
            if not isinstance(v, str) or v not in allowed:
                errors.append(f"{key} must be one of {allowed}, got {v!r}")
            else:
                out[key] = v
        else:
            try:
                if isinstance(v, bool):
                    raise ValueError
                num = float(v)
            except (TypeError, ValueError):
                errors.append(f"{key} must be a number, got {v!r}")
                continue
            if not (p["min"] - 1e-9 <= num <= p["max"] + 1e-9):
                errors.append(f"{key} must be within {p['min']}..{p['max']}, got {v}")
            elif not _on_step(num, p["min"], p["step"]):
                errors.append(f"{key} must be on steps of {p['step']} from {p['min']}, got {v}")
            else:
                out[key] = _clean_number(num, p["step"])
    return (out if not errors else None), errors


def snap_to_step(key: str, value: float, schema: dict | None = None):
    """Round a slider value to the nearest step and clamp it to the range (ties go up)."""
    p = params_by_key(schema)[key]
    k = math.floor((float(value) - p["min"]) / p["step"] + 0.5 + 1e-9)
    snapped = p["min"] + k * p["step"]
    snapped = min(max(snapped, p["min"]), p["max"])
    return _clean_number(snapped, p["step"])


def slider_steps_between(key: str, a, b, schema: dict | None = None) -> int:
    p = params_by_key(schema)[key]
    return int(round(abs(float(a) - float(b)) / p["step"]))


# --- rules and goals ------------------------------------------------------------

_OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}


def _cond_value(cond: dict, params: dict, metrics: dict):
    return params[cond["param"]] if "param" in cond else metrics[cond["metric"]]


def _cond_text(cond: dict) -> str:
    name = cond.get("param") or cond.get("metric")
    return f"{name} {cond['op']} {cond['value']}"


def check_hard_rules(params: dict, metrics: dict, brief: dict | None = None) -> list[dict]:
    brief = brief or load_brief()
    results = []
    for rule in brief["hard_rules"]:
        applicable = all(_OPS[c["op"]](_cond_value(c, params, metrics), c["value"]) for c in rule["if"])
        then = rule["then"]
        actual = _cond_value(then, params, metrics)
        ok = (not applicable) or _OPS[then["op"]](actual, then["value"])
        name = then.get("param") or then.get("metric")
        if not applicable:
            reason = "not applicable (" + " and ".join(_cond_text(c) for c in rule["if"]) + " is not true)"
        elif ok:
            reason = f"{name} = {actual} meets {then['op']} {then['value']}"
        else:
            reason = f"{name} = {actual} fails {then['op']} {then['value']}"
        results.append({"id": rule["id"], "label": rule["label"], "pass": ok,
                        "applicable": applicable, "reason": reason})
    return results


def check_goals(metrics: dict, brief: dict | None = None) -> list[dict]:
    brief = brief or load_brief()
    margin = brief["close_tradeoff_margin"]
    out = []
    for g in brief["goals"]:
        v = metrics[g["metric"]]
        t = g["threshold"]
        miss = (t - v) if g["direction"] == "min" else (v - t)  # > 0 means the goal is missed
        miss = round(miss, 1)
        status = "pass" if miss <= 0 else ("marginal" if miss <= margin else "fail")
        bound = "min" if g["direction"] == "min" else "max"
        out.append({"id": g["id"], "metric": g["metric"], "value": v, "threshold": t,
                    "direction": g["direction"], "status": status,
                    "miss": max(miss, 0.0),
                    "text": f"{g['metric']} {v} ({bound} {t}): {status}"})
    return out


def evaluate(params, schema: dict | None = None, brief: dict | None = None) -> dict:
    """Side-effect free evaluation of one parameter set."""
    norm, errors = validate_parameters(params, schema)
    if errors:
        return {"valid": False, "errors": errors}
    metrics = compute_metrics(norm)
    rules = check_hard_rules(norm, metrics, brief)
    goals = check_goals(metrics, brief)
    rules_pass = all(r["pass"] for r in rules)
    statuses = {g["status"] for g in goals}
    goals_status = "fail" if "fail" in statuses else ("marginal" if "marginal" in statuses else "pass")
    summary = " · ".join(
        f"{g['metric']} {g['value']} {'ok' if g['status'] == 'pass' else g['status'].upper()}" for g in goals
    ) + f" · rules {sum(r['pass'] for r in rules)}/{len(rules)}"
    return {
        "valid": True,
        "parameters": norm,
        "metrics": metrics,
        "hard_rules": rules,
        "hard_rules_pass": rules_pass,
        "failed_rules": [r["id"] for r in rules if not r["pass"]],
        "goals": goals,
        "goals_status": goals_status,
        "failed_goals": [g["id"] for g in goals if g["status"] == "fail"],
        "marginal_goals": [g["id"] for g in goals if g["status"] == "marginal"],
        "summary": summary,
        "note": "Indicative proxies, not simulations.",
    }


# --- state file ------------------------------------------------------------------

def parameters_path(sdir: Path | None = None) -> Path:
    return (sdir or state_dir()) / "parameters.json"


def read_state(sdir: Path | None = None) -> dict:
    """Current applied state. Falls back to config defaults if the file is missing or invalid."""
    path = parameters_path(sdir)
    try:
        data = _read_json(path)
        norm, errors = validate_parameters(data.get("parameters"))
        if errors:
            raise ValueError("; ".join(errors))
        data["parameters"] = norm
        data["source"] = "file"
        return data
    except FileNotFoundError:
        return {"parameters": default_parameters(), "source": "defaults", "updated_at": None}
    except Exception as e:  # corrupt or half-written: never crash, report it
        return {"parameters": default_parameters(), "source": "defaults", "updated_at": None,
                "warning": f"could not read {path.name}: {e}"}


def write_state(params: dict, sdir: Path | None = None, **extra) -> Path:
    """Atomic write: temp file in the same folder, then os.replace (retried on Windows locks)."""
    path = parameters_path(sdir)
    payload = {"parameters": params, "updated_at": now_iso(), **extra}
    fd, tmp = tempfile.mkstemp(prefix=".parameters.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:  # the Grasshopper reader may hold the file for a moment
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path


def append_log(entry: dict, sdir: Path | None = None) -> None:
    path = (sdir or state_dir()) / "log.jsonl"
    line = json.dumps(entry, ensure_ascii=False, default=str)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
