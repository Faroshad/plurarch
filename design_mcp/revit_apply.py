"""Apply a Plurarch decision to the real Langford A Revit model (revit/LangfordA_Plurarch.rvt).

    guard()                      -> (ok, doc_info, reason)   the document guard (read-only)
    read_state()                 -> what Revit holds now for the controlled elements (read-only)
    apply(params)                -> {applied, ops, verified, mismatches, error, ...}
    revit_state_report(params)   -> the read-only report behind the get_revit_state tool

Document guard: every write first checks get_document_info. The ACTIVE document must be
"LangfordA_Plurarch" with its path inside this repo's revit/ folder. Otherwise nothing is written and the
report says why. This never touches another open project or the P1 source file.

Idempotent: read the current state, diff it against the plan (design_mcp/langford_plan.py), send only
the changes in ONE batch (= one Revit transaction, all or nothing), then set Mark/Comments on the new fins
(a second small batch: the add-in cannot reference ids created in the same batch), read back and verify.
A fin wall of the fin type with no Mark (left by an interrupted second step) is treated as a stray fin
and replaced on the next apply.

Timeouts: every HTTP call is capped at 20 s (revit_client.MAX_TIMEOUT_S) and the whole apply has a
deadline, so the design tool never hangs.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from design_mcp import langford_plan, revit_client as rc

REPO = Path(__file__).resolve().parent.parent
REVIT_DIR = REPO / "revit"
DOC_TITLE = "LangfordA_Plurarch"
FT = 0.3048
APPLY_DEADLINE_S = float(os.environ.get("PLURARCH_REVIT_DEADLINE_S", "75"))
_MATERIALS: dict[str, int] | None = None


class _Deadline:
    def __init__(self, seconds: float):
        self.end = time.monotonic() + seconds

    def left(self, cap: float = rc.MAX_TIMEOUT_S) -> float:
        rem = self.end - time.monotonic()
        if rem < 1.0:
            raise rc.RevitUnavailable("Revit deadline reached")
        return min(cap, rem)


# --- the document guard --------------------------------------------------------------------------------

def _doc_fields(info: dict) -> tuple[str, str]:
    title = str(info.get("title") or info.get("name") or info.get("documentTitle") or "")
    path = str(info.get("pathName") or info.get("path") or info.get("filePath") or info.get("fullPath") or "")
    return title, path


def guard(timeout: float = 10.0) -> tuple[bool, dict, str]:
    """(ok, info, reason). ok only if the ACTIVE document is LangfordA_Plurarch from this repo's revit/."""
    try:
        env = rc.call("get_document_info", {}, timeout=timeout)
    except rc.RevitUnavailable as e:
        return False, {}, str(e)
    if not env.get("ok"):
        return False, {}, "get_document_info failed: " + rc.error_text(env)
    info = rc.data(env) or {}
    title, path = _doc_fields(info)
    short = {"title": title, "path": path}
    if title.lower().removesuffix(".rvt") != DOC_TITLE.lower():
        return False, short, (f"the active Revit document is '{title or '?'}', not '{DOC_TITLE}'; "
                              "nothing was written")
    try:
        inside = Path(path).resolve().parent == REVIT_DIR.resolve()
    except (OSError, ValueError):
        inside = False
    if not inside:
        return False, short, (f"'{DOC_TITLE}' is open from '{path or '?'}', not from {REVIT_DIR}; nothing was written")
    return True, short, "ok"


# --- reading the model -------------------------------------------------------------------------------------

def _results(env: dict) -> list:
    if not isinstance(env, dict):
        return []
    if isinstance(env.get("results"), list):
        return env["results"]
    d = env.get("data")
    return d.get("results", []) if isinstance(d, dict) else []


def _params(d: dict) -> dict:
    return {p.get("name"): p for p in (d.get("parameters") or []) if isinstance(p, dict)}


def _pval(P: dict, name: str, string: bool = True):
    p = P.get(name) or {}
    return p.get("valueString") if string and p.get("valueString") not in (None, "") else p.get("value")


def _bbox_m(d: dict):
    bb = d.get("boundingBox") or d.get("bbox")
    if not bb:
        return None
    return [bb["min"][k] * FT for k in "xyz"] + [bb["max"][k] * FT for k in "xyz"]


def _element_infos(ids: list[int], dl: _Deadline) -> dict[int, dict]:
    """get_element_info for many ids in read-only batches (no changes; the transaction commits nothing)."""
    out = {}
    for i in range(0, len(ids), 200):
        part = ids[i:i + 200]
        env = rc.batch([{"command": "get_element_info", "params": {"id": int(x)}} for x in part],
                       stop_on_error=False, timeout=dl.left())
        res = _results(env)
        if not res and not env.get("ok", True):
            raise rc.RevitUnavailable("get_element_info batch failed: " + rc.error_text(env))
        for x, r in zip(part, res):
            if isinstance(r, dict) and r.get("ok"):
                out[int(x)] = r.get("data") or {}
    return out


def _find(category: str, dl: _Deadline, fields: list[str] | None = None) -> list[dict]:
    env = rc.call("find_elements", {"category": category, "limit": 5000, **({"fields": fields} if fields else {})},
                  timeout=dl.left())
    if not env.get("ok"):
        raise rc.RevitUnavailable(f"find_elements {category} failed: " + rc.error_text(env))
    d = rc.data(env) or {}
    return d.get("elements") or d.get("rows") or d.get("items") or []


def _field(row: dict, name: str):
    for k in ("fields", "values", "parameters", "params"):
        v = row.get(k)
        if isinstance(v, dict) and name in v:
            x = v[name]
            return x.get("valueString", x.get("value")) if isinstance(x, dict) else x
        if isinstance(v, list):
            for p in v:
                if isinstance(p, dict) and p.get("name") == name:
                    return p.get("valueString") if p.get("valueString") not in (None, "") else p.get("value")
    return row.get(name)


def materials(dl: _Deadline | None = None, refresh: bool = False) -> dict[str, int]:
    global _MATERIALS
    if _MATERIALS is None or refresh:
        dl = dl or _Deadline(20)
        env = rc.call("list_materials", {}, timeout=dl.left())
        if not env.get("ok"):
            raise rc.RevitUnavailable("list_materials failed: " + rc.error_text(env))
        d = rc.data(env) or {}
        rows = d.get("materials") if isinstance(d, dict) else d
        _MATERIALS = {str(m.get("name")): int(m.get("id")) for m in (rows or []) if isinstance(m, dict) and m.get("id") is not None}
    return _MATERIALS


def _panel_kind(info: dict, E: dict) -> str:
    tid = info.get("typeId")
    if tid == E["infill_finish"]["glazed_panel_type_id"]:
        return "glazed"
    if tid == E["infill_finish"]["solid_panel_type_id"]:
        return "solid"
    name = str(info.get("typeName") or _pval(_params(info), "Family and Type") or info.get("name") or "")
    if "Glazed" in name:
        return "glazed"
    if "Solid" in name:
        return "solid"
    return "other:" + (name or str(tid))


def read_state(timeout: float = 20.0, deadline: _Deadline | None = None, check_guard: bool = True) -> dict:
    """Current state of every controlled element. Read-only."""
    E = langford_plan.load_elements()
    dl = deadline or _Deadline(max(timeout, 30.0))
    if check_guard:
        ok, info, reason = guard(timeout=dl.left(10))
        if not ok:
            return {"ok": False, "reason": reason, "document": info}
    t0 = time.monotonic()
    p_all = langford_plan.plan({"infill_finish": "concrete", "se_glass_share": 100, "fin_depth": 0,
                                "skylights_open": 12}, E)
    panel_ids = sorted(set(p_all["se_glazed"] + p_all["sky_glazed"]))
    solid_type = E["infill_finish"]["solid_panel_type_id"]
    infos = _element_infos(panel_ids + [solid_type], dl)
    panel_types = {str(i): (_panel_kind(infos[i], E) if i in infos else "missing") for i in panel_ids}

    # the solid panel type's material
    P = _params(infos.get(solid_type, {}))
    mp = P.get("Material") or {}
    mat_name = mp.get("valueString")
    mat_id = mp.get("value") if isinstance(mp.get("value"), int) else None
    if mat_name in ("", "<By Category>", "None", None) or mat_id in (-1, None) and not mat_name:
        mat_name = None
    solid_material = {"name": mat_name, "id": mat_id}

    # fins: walls of the fin type with a PLX-FIN-* mark, or with no mark at all (strays)
    walls = _find("OST_Walls", dl, fields=["Mark", "Family and Type"])
    fin_type = E["fin_depth"]["wall_type"]
    cand = []
    if walls and not any(_field(w, "Mark") is not None or _field(w, "Family and Type") for w in walls):
        # the add-in returned bare rows: read every wall (two read-only batches)
        infos_w = _element_infos([int(w.get("id") or w.get("elementId")) for w in walls], dl)
        walls = [{"id": i, "fields": {"Mark": _pval(_params(d), "Mark") or "",
                                      "Family and Type": _pval(_params(d), "Family and Type") or d.get("name") or ""}}
                 for i, d in infos_w.items()]
    for w in walls:
        wid = w.get("id") or w.get("elementId")
        mark = _field(w, "Mark")
        ftype = str(_field(w, "Family and Type") or w.get("typeName") or w.get("name") or "")
        if mark and str(mark).startswith(E["fin_depth"]["mark_prefix"]):
            cand.append(int(wid))
        elif fin_type in ftype and not mark:
            cand.append(int(wid))
    finfo = _element_infos(cand, dl) if cand else {}
    anchors = {a["mark"]: a for a in E["fin_depth"]["anchors"]}
    fins = []
    for wid, d in finfo.items():
        Pw = _params(d)
        mark = _pval(Pw, "Mark") or None
        bb = _bbox_m(d)
        ftype = str(_pval(Pw, "Family and Type") or d.get("name") or "")
        if not mark and fin_type not in ftype:
            continue
        f = {"id": wid, "mark": mark, "bbox": [round(v, 4) for v in bb] if bb else None, "type": ftype}
        if bb:
            a = anchors.get(mark)
            dx, dy = (a["dir"] if a else (0.0, -1.0))
            if abs(dy) >= abs(dx):   # fin runs along Y
                y_start = bb[4] if dy < 0 else bb[1]
                f.update({"start": [round((bb[0] + bb[3]) / 2, 4), round(y_start, 4)], "length": round(bb[4] - bb[1], 4)})
            else:
                x_start = bb[3] if dx < 0 else bb[0]
                f.update({"start": [round(x_start, 4), round((bb[1] + bb[4]) / 2, 4)], "length": round(bb[3] - bb[0], 4)})
            f["height"] = round(bb[5] - bb[2], 4)
        fins.append(f)
    fins.sort(key=lambda f: (str(f["mark"]), f["id"]))
    return {"ok": True, "panel_types": panel_types, "solid_material": solid_material, "fins": fins,
            "read_s": round(time.monotonic() - t0, 2)}


# --- applying -------------------------------------------------------------------------------------------------

def _steps(d: dict, E: dict, mats: dict[str, int]) -> list[dict]:
    steps = []
    if d["delete"]:
        steps.append({"command": "delete_elements", "params": {"ids": [int(i) for i in d["delete"]]}})
    tid = {"glazed": E["infill_finish"]["glazed_panel_type_id"], "solid": E["infill_finish"]["solid_panel_type_id"]}
    for r in d["retype"]:
        steps.append({"command": "change_element_type", "params": {"id": int(r["id"]), "typeId": tid[r["to"]]}})
    if d["material"]:
        name = d["material"]["name"]
        value = {"id": -1} if name is None else {"id": mats[name]}
        steps.append({"command": "set_parameter", "params": {"id": d["material"]["type_id"],
                                                             "parameterName": "Material", "value": value}})
    for f in d["create"]:
        steps.append({"command": "create_wall", "params": {
            "start": {"x": f["start"][0], "y": f["start"][1]}, "end": {"x": f["end"][0], "y": f["end"][1]},
            "height": f["height"], "levelName": f["level"], "wallTypeName": E["fin_depth"]["wall_type"],
            "structural": False, "units": "meters"}})
    return steps


def _created_id(r: dict):
    d = (r or {}).get("data") or {}
    for k in ("id", "elementId", "wallId", "newId"):
        if isinstance(d.get(k), int):
            return d[k]
    ids = d.get("createdIds") or d.get("ids") or []
    return ids[0] if ids else None


def apply(params: dict, *, original_finish: bool = False, dry: bool = False, label: str = "") -> dict:
    """Bring Revit to the plan of `params`. Never raises; the report says what happened.

    original_finish: for a reset to the as-built model, the default finish restores the as-modelled
    material of the solid panel type (none: <By Category>) instead of the concrete material."""
    t0 = time.monotonic()
    E = langford_plan.load_elements()
    report = {"applied": False, "ops": 0, "verified": False, "mismatches": [], "error": None, "dry_run": dry}
    if not rc.health(timeout=2.0):
        report["error"] = f"Revit add-in not reachable at {rc.BASE}"
        return report
    dl = _Deadline(APPLY_DEADLINE_S)
    try:
        ok, info, reason = guard(timeout=dl.left(10))
        report["document"] = info
        if not ok:
            report["error"] = reason
            return report
        p = langford_plan.plan(params, E)
        default_finish = E["infill_finish"].get("default", "concrete")
        if original_finish and params.get("infill_finish") == default_finish:
            p["material"] = None   # the as-modelled solid panels have no material
        state = read_state(deadline=dl, check_guard=False)
        d = langford_plan.diff(p, state, E)
        mats = materials(dl) if (d["material"] and d["material"]["name"]) else {}
        if d["material"] and d["material"]["name"] and d["material"]["name"] not in mats:
            report["error"] = f"material '{d['material']['name']}' is not in the Revit document"
            return report
        steps = _steps(d, E, mats)
        report.update({"ops": len(steps), "plan": {"se_glazed": p["se_glazed_count"], "se_n": p["se_n"],
                                                   "lanterns_open": len(p["lanterns_open"]), "fins": len(p["fins"]),
                                                   "fin_depth": p["fin_depth"], "material": p["material"]},
                       "changes": {"panels_retyped": len(d["retype"]), "fins_deleted": len(d["delete"]),
                                   "fins_created": len(d["create"]), "material": bool(d["material"])}})
        created = []
        if steps:
            ok, info, reason = guard(timeout=dl.left(10))   # again, right before the write
            if not ok:
                report["error"] = reason
                return report
            t1 = time.monotonic()
            env = rc.batch(steps, stop_on_error=True, dry=dry, timeout=dl.left())
            report["batch_s"] = round(time.monotonic() - t1, 2)
            res = _results(env)
            bad = [r for r in res if isinstance(r, dict) and not r.get("ok")]
            if not env.get("ok", True) or bad:
                first = bad[0] if bad else env
                report["error"] = ("Revit batch rolled back: " + rc.error_text(first) +
                                   (f" (step {first.get('index')}: {first.get('command')})" if bad else ""))
                return report
            if dry:
                report.update({"applied": False, "verified": None, "s": round(time.monotonic() - t0, 2)})
                return report
            n_create = len(d["create"])
            created = [_created_id(r) for r in res[-n_create:]] if n_create else []
            if n_create and None not in created:
                items = []
                for fid, f in zip(created, d["create"]):
                    items.append({"elementId": int(fid), "parameterName": "Mark", "value": f["mark"]})
                    items.append({"elementId": int(fid), "parameterName": "Comments",
                                  "value": f"Plurarch sunshade fin, depth {f['length']} m{(' | ' + label) if label else ''}"})
                env2 = rc.call("import_parameters", {"items": items}, timeout=dl.left())
                if not env2.get("ok"):
                    report["mismatches"].append("fin marks not set: " + rc.error_text(env2))
            elif n_create:
                report["mismatches"].append("could not read the new fin ids; fins have no Mark yet (fixed next apply)")
        report["applied"] = True
        after = read_state(deadline=dl, check_guard=False)
        cmp_ = langford_plan.compare(p, after)
        report["verified"] = cmp_["matches"] and not report["mismatches"]
        report["mismatches"] += cmp_["mismatches"]
        report["state"] = langford_plan.summarize_state(after, E)
    except rc.RevitUnavailable as e:
        report["error"] = str(e)
    except Exception as e:  # noqa: BLE001  (never break the design tool)
        report["error"] = f"{type(e).__name__}: {e}"
    report["s"] = round(time.monotonic() - t0, 2)
    return report


def revit_state_report(applied_params: dict | None) -> dict:
    """Read-only: what Revit holds now and whether it matches the applied parameters."""
    t0 = time.monotonic()
    E = langford_plan.load_elements()
    if not rc.health(timeout=2.0):
        return {"available": False, "reason": f"Revit add-in not reachable at {rc.BASE}"}
    try:
        dl = _Deadline(30)
        state = read_state(deadline=dl)
        if not state.get("ok"):
            return {"available": False, "reason": state.get("reason"), "document": state.get("document")}
        out = {"available": True, "document": DOC_TITLE, **langford_plan.summarize_state(state, E)}
        if applied_params:
            p = langford_plan.plan(applied_params, E)
            cmp_ = langford_plan.compare(p, state)
            if not cmp_["matches"] and state["solid_material"]["name"] is None \
                    and applied_params.get("infill_finish") == E["infill_finish"].get("default"):
                p["material"] = None   # as modelled = the default finish (after a reset)
                cmp_ = langford_plan.compare(p, state)
            out.update({"matches_applied_parameters": cmp_["matches"], "mismatches": cmp_["mismatches"],
                        "applied_parameters": applied_params})
        out["read_s"] = round(time.monotonic() - t0, 2)
        return out
    except rc.RevitUnavailable as e:
        return {"available": False, "reason": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"{type(e).__name__}: {e}"}


def compact(report: dict) -> dict:
    """The short form for tool results and decision records."""
    keys = ("applied", "ops", "verified", "mismatches", "error", "changes", "s", "dry_run", "skipped")
    return {k: report[k] for k in keys if k in report}
