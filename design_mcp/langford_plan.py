"""The Langford plan rule: parameters -> target state of the real Revit elements, and the diff to get there.

Pure functions (no Revit needed): load_elements(), glazed_count(), plan(params), diff(plan, state),
compare(plan, state). The Revit read lives in read_state() (via design_mcp/revit_client.py) and is only
imported when called.

The rule (identical in site/js/model/langford.js; tests/model/langford_plans.json keeps them equal):
    glazed SE panels  = the first round_half_up(share / 100 * N) panels by rank; the others solid
    glazed lanterns   = the first n lanterns by index; the others solid (all 14 glazing panels)
    fins              = for every anchor, a wall from base along dir with length = depth (none when 0)
    solid finish      = materials[infill_finish] on the solid panel type (all solid panels share it)

round_half_up(x) = floor(x + 0.5). With N = 142 and shares in steps of 10, x is never a half, so
Python round(), JS Math.round() and this all agree.
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ELEMENTS_PATH = REPO / "config" / "langford" / "elements.json"
FIN_TOL_M = 0.02


@lru_cache(maxsize=1)
def load_elements() -> dict:
    with open(ELEMENTS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def glazed_count(share: float, n: int) -> int:
    return int(math.floor(n * float(share) / 100.0 + 0.5 + 1e-9))


def _r4(v: float) -> float:
    return round(float(v) + 0.0, 4)


def plan(params: dict, elements: dict | None = None) -> dict:
    """Target state for one validated parameter set (JSON-serialisable, deterministic)."""
    E = elements or load_elements()
    se = sorted(E["se_glass_share"]["panels"], key=lambda p: p["rank"])
    k = glazed_count(params["se_glass_share"], len(se))
    lanterns = sorted(E["skylights_open"]["lanterns"], key=lambda l: l["index"])
    n_open = max(0, min(len(lanterns), int(params["skylights_open"])))
    depth = round(float(params["fin_depth"]), 4)
    fins = []
    if depth > 1e-9:
        for a in E["fin_depth"]["anchors"]:
            bx, by, bz = a["base"]
            dx, dy = a["dir"]
            fins.append({"mark": a["mark"], "index": a["index"], "level": a["level"],
                         "start": [_r4(bx), _r4(by)], "end": [_r4(bx + dx * depth), _r4(by + dy * depth)],
                         "z": bz, "height": a["height_m"], "length": depth})
    finish = params["infill_finish"]
    se_glazed = [p["id"] for p in se[:k]]
    se_solid = [p["id"] for p in se[k:]]
    sky_glazed = [i for l in lanterns[:n_open] for i in l["glazing_ids"]]
    sky_solid = [i for l in lanterns[n_open:] for i in l["glazing_ids"]]
    layout = None
    open_ls, closed_ls = lanterns[:n_open], lanterns[n_open:]
    if params.get("skylight_layout"):  # the reviewer's panel layout: same glass, spread by simulation
        from design_mcp import skylights
        glazed = skylights.glazed_panels(params, E)
        if glazed is not None:
            layout = params["skylight_layout"]
            every = [i for l in lanterns for i in l["glazing_ids"]]
            sky_glazed = [i for i in every if i in glazed]
            sky_solid = [i for i in every if i not in glazed]
            open_ls = [l for l in lanterns if all(i in glazed for i in l["glazing_ids"])]
            closed_ls = [l for l in lanterns if l not in open_ls]
    return {
        "params": dict(params),
        "se_glazed": se_glazed, "se_solid": se_solid,
        "se_glazed_count": k, "se_n": len(se),
        "se_glazed_area_m2": round(sum(p["area_m2"] for p in se[:k]), 2),
        "se_solid_area_m2": round(sum(p["area_m2"] for p in se[k:]), 2),
        "lanterns_open": [l["index"] for l in open_ls],
        "lanterns_open_marks": [l["mark"] for l in open_ls],
        "lanterns_closed_marks": [l["mark"] for l in closed_ls],
        "sky_glazed": sky_glazed, "sky_solid": sky_solid, "skylight_layout": layout,
        "lantern_glazed_panels": [sum(1 for i in l["glazing_ids"] if i in set(sky_glazed)) for l in lanterns],
        "fins": fins, "fin_depth": depth,
        "finish": finish, "material": E["infill_finish"]["materials"][finish],
    }


def panel_targets(p: dict) -> dict[int, str]:
    out = {i: "glazed" for i in p["se_glazed"] + p["sky_glazed"]}
    out.update({i: "solid" for i in p["se_solid"] + p["sky_solid"]})
    return out


def _fin_matches(want: dict, have: dict) -> bool:
    if have.get("mark") != want["mark"]:
        return False
    try:
        return (math.dist(have["start"], want["start"]) <= FIN_TOL_M
                and abs(have["length"] - want["length"]) <= FIN_TOL_M
                and abs(have["height"] - want["height"]) <= FIN_TOL_M)
    except (KeyError, TypeError):
        return False


def diff(p: dict, state: dict, elements: dict | None = None) -> dict:
    """Operations that bring Revit from `state` (read_state()) to plan `p`.

    Returns {"delete": [ids], "retype": [{"id", "to"}], "material": {...} | None, "create": [fins]}.
    Fins are matched by Mark, start point, length and height; anything else is deleted and recreated.
    """
    E = elements or load_elements()
    types = state.get("panel_types", {})
    retype = []
    for pid, want in sorted(panel_targets(p).items()):
        have = types.get(str(pid), types.get(pid))
        if have != want:
            retype.append({"id": pid, "to": want, "from": have})
    keep, delete, create = set(), [], []
    have_fins = list(state.get("fins", []))
    for f in p["fins"]:
        match = next((h for h in have_fins if h["id"] not in keep and _fin_matches(f, h)), None)
        if match:
            keep.add(match["id"])
        else:
            create.append(f)
    delete = sorted(h["id"] for h in have_fins if h["id"] not in keep)
    material = None
    have_mat = (state.get("solid_material") or {}).get("name")
    if have_mat != p["material"]:
        material = {"type_id": E["infill_finish"]["solid_panel_type_id"], "name": p["material"], "from": have_mat}
    return {"delete": delete, "retype": retype, "material": material, "create": create,
            "n_ops": len(delete) + len(retype) + (1 if material else 0) + len(create)}


def compare(p: dict, state: dict) -> dict:
    """How far the Revit state is from the plan (for verification and get_revit_state)."""
    d = diff(p, state)
    mismatches = []
    if d["retype"]:
        ex = d["retype"][:3]
        mismatches.append(f"{len(d['retype'])} panel(s) not {'/'.join(sorted({r['to'] for r in d['retype']}))} "
                          f"(e.g. {', '.join(str(r['id']) for r in ex)})")
    if d["create"]:
        mismatches.append(f"{len(d['create'])} planned fin(s) missing")
    if d["delete"]:
        mismatches.append(f"{len(d['delete'])} fin wall(s) that should not be there")
    if d["material"]:
        mismatches.append(f"solid panel material is {d['material']['from']!r}, planned {d['material']['name']!r}")
    return {"matches": not mismatches, "mismatches": mismatches, "pending_ops": d["n_ops"]}


def summarize_state(state: dict, elements: dict | None = None) -> dict:
    """Counts the reviewer can read: SE glazed/solid, open lanterns, fins and their depth, the finish."""
    E = elements or load_elements()
    types = state.get("panel_types", {})

    def t(pid):
        return types.get(str(pid), types.get(pid))
    se = E["se_glass_share"]["panels"]
    se_glazed = sum(1 for x in se if t(x["id"]) == "glazed")
    se_solid = sum(1 for x in se if t(x["id"]) == "solid")
    open_l, closed_l, per_lantern = [], [], []
    for l in sorted(E["skylights_open"]["lanterns"], key=lambda x: x["index"]):
        st = {t(i) for i in l["glazing_ids"]}
        (open_l if st == {"glazed"} else closed_l).append(l["mark"])
        per_lantern.append(sum(1 for i in l["glazing_ids"] if t(i) == "glazed"))
    fins = state.get("fins", [])
    depths = sorted({round(f.get("length", 0), 2) for f in fins})
    mat = (state.get("solid_material") or {}).get("name")
    finish = next((k for k, v in E["infill_finish"]["materials"].items() if v == mat), None)
    n = len(se)
    return {
        "se_panels_glazed": se_glazed, "se_panels_solid": se_solid, "se_panels_total": n,
        "se_glass_share_pct": round(100.0 * se_glazed / n, 1) if n else None,
        "lanterns_open": len(open_l), "lanterns_closed": closed_l,
        "lantern_panels_glazed": sum(per_lantern), "lantern_glazed_panels": per_lantern,
        "fins": len(fins), "fin_depths_m": depths,
        "fin_marks_ok": all(str(f.get("mark", "")).startswith("PLX-FIN-") for f in fins),
        "solid_material": mat, "solid_finish": finish or ("as modelled (no material)" if not mat else "other"),
    }


def read_state(timeout: float = 20.0) -> dict:
    """Read the controlled elements from Revit (lazy import: needs the add-in)."""
    from design_mcp import revit_apply
    return revit_apply.read_state(timeout=timeout)
