"""Roof-lantern glazing layouts: which of the 168 lantern glazing panels stay glass.

The room votes how many lanterns' worth of glass stays (skylights_open, 0..12). The plain rule closes whole
lanterns. A layout instead spreads the same number of glazed panels (skylights_open x 14) over all lanterns;
optimise() finds one with a daylight simulation and a genetic algorithm in Rhino (rhino/plurarch_daylight.py).

A layout is the optional, non-voted parameter "skylight_layout": a short id (e.g. "ga8-3f2a") registered in
<state>/skylight_layouts.json with its panel mask. Mask = hex string, one bit per panel in ascending Revit
element id order of the 168 lantern glazing panels (bit = 1: glazed), most significant bit first; the phone's
3D model reads the same mask (site/js/model/langford.js).
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from design_mcp import core

KEY = "skylight_layout"
MASK_KEY = "skylight_mask"          # added to stored decisions so the phones can draw the layout
REPO = Path(__file__).resolve().parent.parent
ENGINE = REPO / "rhino" / "plurarch_daylight.py"


def _elements() -> dict:
    return json.loads((REPO / "config" / "langford" / "elements.json").read_text(encoding="utf-8"))


def panel_ids(E: dict | None = None) -> list[int]:
    E = E or _elements()
    return sorted(i for l in E["skylights_open"]["lanterns"] for i in l["glazing_ids"])


def panels_per_lantern(E: dict | None = None) -> int:
    E = E or _elements()
    return len(E["skylights_open"]["lanterns"][0]["glazing_ids"])


def mask_to_ids(mask: str, ids: list[int]) -> set[int]:
    bits = "".join(f"{int(c, 16):04b}" for c in mask)
    return {pid for pid, b in zip(ids, bits) if b == "1"}


def ids_to_mask(glazed: set[int], ids: list[int]) -> str:
    bits = "".join("1" if pid in glazed else "0" for pid in ids)
    bits += "0" * ((-len(bits)) % 4)
    return "".join(f"{int(bits[i:i + 4], 2):x}" for i in range(0, len(bits), 4))


def _registry_path(sdir: Path | None = None) -> Path:
    return (sdir or core.state_dir()) / "skylight_layouts.json"


def load_registry(sdir: Path | None = None) -> dict:
    try:
        return json.loads(_registry_path(sdir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def register(mask: str, lanterns_open: int, meta: dict, kind: str = "ga", sdir: Path | None = None) -> str:
    reg = load_registry(sdir)
    lid = f"{kind}{lanterns_open}-{hashlib.sha1(mask.encode()).hexdigest()[:4]}"
    reg[lid] = {"mask": mask, "lanterns_open": lanterns_open, "kind": kind, "created": core.now_iso(), **meta}
    p = _registry_path(sdir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=1), encoding="utf-8")
    tmp.replace(p)
    return lid


def lookup(layout_id: str, sdir: Path | None = None) -> dict | None:
    return load_registry(sdir).get(layout_id)


def check(value, lanterns_open, sdir: Path | None = None) -> tuple[str | None, str | None]:
    """Validate a skylight_layout value for a given skylights_open. Returns (normalized, error)."""
    if value in (None, "", "standard"):
        return None, None
    if not isinstance(value, str):
        return None, f"{KEY} must be a layout id string from optimise_skylight_layout, got {value!r}"
    entry = lookup(value.strip(), sdir)
    if not entry:
        return None, f"{KEY} {value!r} is not a known layout (run optimise_skylight_layout first)"
    try:
        n = int(round(float(lanterns_open)))
    except (TypeError, ValueError):
        return None, f"{KEY} needs a valid skylights_open"
    if entry.get("lanterns_open") != n:
        return None, (f"{KEY} {value!r} was optimised for skylights_open = {entry.get('lanterns_open')}, "
                      f"not {n}; it keeps the same glass only for that number")
    return value.strip(), None


def glazed_panels(params: dict, E: dict | None = None) -> set[int] | None:
    """The glazed lantern panels of a layout, or None when the plain whole-lantern rule applies."""
    lid = params.get(KEY)
    if not lid:
        return None
    entry = lookup(lid)
    if not entry:
        return None
    return mask_to_ids(entry["mask"], panel_ids(E))


def with_mask(params: dict | None) -> dict | None:
    """params plus the panel mask of its layout (for stored decisions the phones read)."""
    if not isinstance(params, dict) or not params.get(KEY):
        return params
    entry = lookup(params[KEY])
    return {**params, MASK_KEY: entry["mask"]} if entry else params


def optimise(lanterns_open: int, generations: int = 80, population: int = 40, seed: int = 7,
             threshold: float = 2.0, pace: float = 1.0, timeout: float = 240.0) -> dict:
    """Run the daylight simulation + GA in Rhino; register the GA layout and the ranking layout."""
    from design_mcp import rhino_bridge
    E = _elements()
    per = panels_per_lantern(E)
    n = int(lanterns_open)
    n_l = len(E["skylights_open"]["lanterns"])
    if not 0 < n < n_l:
        return {"available": True, "skipped": f"nothing to optimise: skylights_open = {n} keeps "
                                               f"{'all' if n >= n_l else 'no'} lantern glass"}
    t0 = time.time()
    try:
        r = rhino_bridge.run_file(str(ENGINE), {"repo": str(REPO), "action": "optimise", "budget": n * per,
                                                "generations": generations, "population": population,
                                                "seed": seed, "threshold": threshold, "pace": pace}, timeout)
    except rhino_bridge.RhinoError as e:
        return {"available": False, "reason": str(e)[:300]}
    meta = {"engine": r.get("engine"), "sky": r.get("sky"), "sensors": r.get("sensors")}
    ga_id = register(r["best_mask"], n, {**meta, "metrics": r["best"]}, "ga")
    rank_id = register(r["ranked_mask"], n, {**meta, "metrics": r["ranked"]}, "rank")

    def m(x):
        return {"p5_sky_component_pct": x["p5_sky_component_pct"], "mean_sky_component_pct": x["mean_sky_component_pct"],
                "daylit_area_pct": x["daylit_area_pct"], "glazed_panels": x["glazed_panels"],
                "glazed_panels_per_lantern": x["per_lantern"]}
    return {
        "available": True,
        "question": f"{n} lanterns' worth of glass = {n * per} of {n_l * per} panels: which panels should stay glass?",
        "method": (f"{r['sky']}; {r['sensors']} sensors on the top-floor studio work plane ({r['grid_m']} m grid); "
                   f"{r['rays_traced']} rays traced through the model's lantern geometry in {r['engine']}; "
                   f"sky component with glass transmittance {r['tau']}; genetic algorithm (population "
                   f"{r['ga']['population']}, {r['ga']['generations']} generations, {r['ga']['evaluations']} layouts "
                   f"tested of ~1e{int(round(r['ga']['search_space_log10']))})"),
        "objective": "P5 = sky component reached by 95% of the studio floor (EN 17037-style minimum level), "
                     "tie-break mean; daylit area = share of the floor with sky component >= "
                     f"{r['threshold_sc_pct']}%",
        "today_all_glazed": m(r["all_glazed"]),
        "rooms_rule_close_whole_lanterns": {**m(r["standard"]), "layout_id": None},
        "simple_ranking": {**m(r["ranked"]), "layout_id": rank_id},
        "genetic_algorithm_best": {**m(r["best"]), "layout_id": ga_id},
        "same_glass_note": "Every option has the same glazed area, so evaluate's cooling, cost and carbon are "
                           "unchanged; only where the light lands changes.",
        "how_to_apply": f"add \"{KEY}\": \"{ga_id}\" to the parameters of set_parameters (keep skylights_open = {n})",
        "seconds": round(time.time() - t0, 1),
        "assumptions": ["sky component only (no inter-reflections)", "the roof deck is open under each lantern "
                        "(light well), closed elsewhere", "only glass passes light; unmodelled gaps count as closed"],
    }
