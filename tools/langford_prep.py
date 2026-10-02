"""One-off data prep for the Langford A model (run with C:\\Python314\\python.exe: ifcopenshell, numpy).

    C:\\Python314\\python.exe tools\\langford_prep.py            # writes everything, prints a summary
    C:\\Python314\\python.exe tools\\langford_prep.py --no-mesh  # elements.json only (fast)

Reads the P1 Revit-first source READ-ONLY (never writes there):
    03_revit/build_manifest.json      every created element (params, marks, ids)
    04_transfer/revit_truth.json      every Revit element: category, type, area, bbox in the MODEL frame
    04_transfer/transfer_elements.json  IFC products: GlobalId, RevitElementId, parent curtain wall, material
    04_transfer/LangfordA_P1_IFC4RV_psets.ifc   geometry (shared coordinates = model frame rotated +39.35 deg)
    05_rhino/context.json             trees (ENU = the IFC/Rhino frame)
    03_revit/schedules_csv/ARCA_-_Curtain_Panel_Schedule.csv   (count check)

Writes:
    config/langford/elements.json     which Revit elements each question controls (docs/LANGFORD.md)
    site/models/langford/base.json + base.bin + README.md   light phone geometry, in the Revit model frame

Frames: "revit_model_m" = Revit internal model coordinates in metres (X along the long axis toward NE,
Y toward NW, origin at the S corner, Z = 0 at Level 1 = 101.64 m NAVD88). The IFC and the Rhino render
scene use true-north ENU = the model frame rotated +39.35 deg about Z at the origin (no translation).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import struct
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
P1 = Path(r"E:\Academic\PhD\Fall 2026\AI Workshop\LangfordA_Fresh\P1_RevitFirst")
IFC_PATH = P1 / "04_transfer" / "LangfordA_P1_IFC4RV_psets.ifc"
OUT_ELEMENTS = REPO / "config" / "langford" / "elements.json"
OUT_SITE = REPO / "site" / "models" / "langford"

ROT_DEG = 39.35  # model -> IFC/Rhino ENU (Revit Project Base Point angle to true north)
LEVELS = {"L1 - Lower": 0.00, "L2 - Entry": 4.50, "L3": 8.99, "L4": 13.51, "Roof": 18.26,
          "T.O. Parapet": 19.48, "L5 - Penthouse Roof": 22.66}
LEVEL_SHORT = {"L1 - Lower": "L1", "L2 - Entry": "L2", "L3": "L3", "L4": "L4", "Roof": "Roof"}
GLAZED_TYPE = "System Panel:Glazed"
SOLID_TYPE = "System Panel:Solid"
FIN_TYPE = "ARCA Fin/Curb - Concrete 200"
SLIVER_MAX_M2 = 0.25      # head-row slivers of the ribbon windows (2-163 mm tall) are not part of the question
FIN_GAP_M = 0.02          # fins start 20 mm in front of the host wall face (no wall join with the facade)
FINISH_MATERIALS = {      # Revit materials for the solid infill finish (verify with list_materials in Revit)
    "concrete": {"revit_material": "ARCA Bush-hammered Architectural Concrete", "exists_in_p1": True,
                 "note": "P1 material (id 18940, renamed from the template); used by the piers and brackets"},
    "aluminium": {"revit_material": "Aluminum", "exists_in_p1": True,
                  "note": "template material used by all 1,349 mullions (IFC material 'Aluminum')"},
    "fritted_glass": {"revit_material": "Glass", "exists_in_p1": True,
                      "note": "P1 has no fritted/frosted glass material; 'Glass' (the glazed panel material) is the "
                              "best existing one. Check list_materials for 'Glass, Frosted' or similar."},
}


def rot(x: float, y: float, deg: float):
    t = math.radians(deg)
    c, s = math.cos(t), math.sin(t)
    return x * c - y * s, x * s + y * c


def load_json(p: Path):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------------------------------
# elements.json
# --------------------------------------------------------------------------------------------------

def build_elements():
    manifest = load_json(P1 / "03_revit" / "build_manifest.json")
    truth = load_json(P1 / "04_transfer" / "revit_truth.json")
    rt = truth["elements"]
    transfer = load_json(P1 / "04_transfer" / "transfer_elements.json")
    by_mark = {m["mark"]: m for m in manifest if m.get("mark")}
    ifc_by_rid = {e["RevitElementId"]: e for e in transfer}

    # children (panels) per host curtain wall, from the IFC parent link (527 of 549 panels)
    panels_by_host = defaultdict(list)
    for e in transfer:
        if e["IfcClass"] == "IfcPlate" and e.get("ParentMark"):
            panels_by_host[e["ParentMark"]].append(e)

    def panel_row(e):
        r = rt[e["RevitElementId"]]
        b = r["bbox_model"]
        return {"id": int(e["RevitElementId"]), "global_id": e["GlobalId"], "type": e["TypeName"],
                "area_m2": round(r["area_m2"], 4), "bbox": [round(v, 4) for v in b],
                "x": (b[0] + b[3]) / 2, "z": (b[2] + b[5]) / 2}

    # ---- SE studio facade: the ribbon-window lites of SE-L1..L4 (not doors, not the entrance transom)
    se_hosts = sorted(m for m in by_mark if m.startswith("SE-L") and "-W" in m)
    lites, slivers = [], []
    host_info = {}
    for hm in se_hosts:
        host = by_mark[hm]
        rows = [panel_row(e) for e in panels_by_host[hm]]
        main = sorted([r for r in rows if r["area_m2"] >= SLIVER_MAX_M2], key=lambda r: r["x"])
        slivers += [r for r in rows if r["area_m2"] < SLIVER_MAX_M2]
        lvl_name = host["params"]["levelName"]
        x0 = min(host["params"]["start"]["x"], host["params"]["end"]["x"])
        x1 = max(host["params"]["start"]["x"], host["params"]["end"]["x"])
        bay = int(hm.split("-W")[1][0])
        host_info[hm] = {"id": host["id"], "level": lvl_name, "x0": x0, "x1": x1, "y": host["params"]["start"]["y"],
                         "base_offset": host["post"].get("Base Offset", 0.0), "height": host["params"]["height"],
                         "n_lites": len(main), "bay": bay}
        n = len(main)
        for c, r in enumerate(main):
            half = (n - 1) / 2.0
            r.update({"host": hm, "host_id": host["id"], "level": LEVEL_SHORT[lvl_name], "level_name": lvl_name,
                      "bay": bay, "col": c, "n_cols": n,
                      "edge": round(abs(c - half) / half, 4) if half > 0 else 0.0})
            lites.append(r)

    # Ranking (rank 0 = most valuable, glazed longest; lowering the share turns the HIGHEST ranks solid first).
    # The P1 lites are full height (sill to head), so there is no separate spandrel row: the least valuable
    # glass is the EDGE BAND of every ribbon (lites next to the piers / towers, in their shade, little view),
    # removed ring by ring toward the bay centre, so every bay keeps a central eye-level vision band.
    # Ties: lower floors first (L1 looks onto the moat retaining walls, L4 over the quad), then the outer
    # bays (next to the corner towers) before the inner bays, then by x.
    level_value = {"L4": 0, "L3": 1, "L2": 2, "L1": 3}
    fac_mid = 32.325

    def value_key(r):
        bay_centre = (host_info[r["host"]]["x0"] + host_info[r["host"]]["x1"]) / 2
        return (r["edge"], level_value[r["level"]], -abs(bay_centre - fac_mid), r["x"])

    lites.sort(key=value_key)
    for i, r in enumerate(lites):
        r["rank"] = i
    se_panels = [{"id": r["id"], "mark": r["host"], "key": f"{r['host']}.c{r['col'] + 1}", "global_id": r["global_id"],
                  "host_id": r["host_id"], "area_m2": r["area_m2"], "level": r["level"], "bay": r["bay"],
                  "col": r["col"], "n_cols": r["n_cols"], "edge": r["edge"],
                  "center": [round(r["x"], 3), round((r["bbox"][1] + r["bbox"][4]) / 2, 3), round(r["z"], 3)],
                  "glazed_now": r["type"] == GLAZED_TYPE, "rank": r["rank"]} for r in lites]

    # ---- skylight lanterns: SKY-01..12, glazing = the 14 panels of SKY-xx-G
    lanterns = []
    for k in range(1, 13):
        gm = f"SKY-{k:02d}-G"
        host = by_mark[gm]
        rows = [panel_row(e) for e in panels_by_host[gm]]
        sx = (host["params"]["start"]["x"] + host["params"]["end"]["x"]) / 2
        sy = (host["params"]["start"]["y"] + host["params"]["end"]["y"]) / 2
        w = by_mark[f"SKY-{k:02d}-W"]["params"]
        s = by_mark[f"SKY-{k:02d}-S"]["params"]
        # right-triangle plan: corner = W wall start/S wall end shared point
        tri = [(w["start"]["x"], w["start"]["y"]), (s["start"]["x"], s["start"]["y"]), (w["end"]["x"], w["end"]["y"])]
        cx = sum(p[0] for p in tri) / 3
        cy = sum(p[1] for p in tri) / 3
        lanterns.append({"mark": f"SKY-{k:02d}", "glazing_wall_id": host["id"], "glazing_wall_mark": gm,
                         "glazing_ids": sorted(r["id"] for r in rows),
                         "glazing_area_m2": round(sum(r["area_m2"] for r in rows), 3),
                         "glazed_now": all(r["type"] == GLAZED_TYPE for r in rows),
                         "glazing_mid": [round(sx, 3), round(sy, 3)], "centroid": [round(cx, 3), round(cy, 3)],
                         "roof_id": by_mark[f"SKY-{k:02d}-R"]["id"]})
    # Fixed order: point-symmetric pairs about the roof centre (each slider step of 2 opens one pair),
    # pairs chosen greedily so the open lanterns stay spread over the studios (max-min distance).
    rc = (sum(l["centroid"][0] for l in lanterns) / 12, sum(l["centroid"][1] for l in lanterns) / 12)

    def partner(l):
        tx, ty = 2 * rc[0] - l["centroid"][0], 2 * rc[1] - l["centroid"][1]
        return min(lanterns, key=lambda o: (o["centroid"][0] - tx) ** 2 + (o["centroid"][1] - ty) ** 2)

    pairs, seen = [], set()
    for l in lanterns:
        if l["mark"] in seen:
            continue
        p = partner(l)
        seen.update({l["mark"], p["mark"]})
        pairs.append(sorted([l, p], key=lambda o: o["mark"]))

    def d(a, b):
        return math.dist(a["centroid"], b["centroid"])

    # start: the pair that is most spread (longest diagonal), tie -> lowest mark
    order_pairs = [max(pairs, key=lambda pr: (round(d(pr[0], pr[1]), 6), [-int(x["mark"][4:]) for x in pr]))]
    rest = [pr for pr in pairs if pr is not order_pairs[0]]
    while rest:
        chosen = [x for pr in order_pairs for x in pr]
        best = max(rest, key=lambda pr: (round(min(d(x, c) for x in pr for c in chosen), 6),
                                          [-int(x["mark"][4:]) for x in pr]))
        order_pairs.append(best)
        rest.remove(best)
    ordered = [x for pr in order_pairs for x in pr]
    for i, l in enumerate(ordered):
        l["index"] = i

    # ---- fins: vertical fins at the third points of every full SE bay (mullion lines k = n/3, 2n/3)
    # The 3.6 m x 2.5 m piers and the corner towers already act as fins at the bay ends; two new fins split
    # each bay into thirds (spacing 3.77 m). L2 bay 2 (the quad entrance: doors + short ribbons) gets none.
    # The P1 model has NO SW glass (SW-PROJ is a blank concrete projection), so there are no SW anchors.
    anchors = []
    for hm, h in sorted(host_info.items(), key=lambda kv: (LEVELS[kv[1]["level"]], kv[1]["x0"])):
        n = h["n_lites"]
        if n < 6:
            continue
        s = (h["x1"] - h["x0"]) / n
        for k in (round(n / 3), round(2 * n / 3)):
            x = h["x0"] + k * s
            y = h["y"] - 0.15 - FIN_GAP_M   # host wall (300 mm) exterior face, minus a 20 mm gap; outward = -Y
            anchors.append({"facade": "SE", "host": hm, "mullion_line": k,
                            "base": [round(x, 4), round(y, 4), LEVELS[h["level"]]], "dir": [0.0, -1.0],
                            "height_m": round(h["base_offset"] + h["height"], 3), "level": h["level"],
                            "base_offset_m": 0.0, "spacing_m": round(3 * s, 4),
                            "window_z": [round(LEVELS[h["level"]] + h["base_offset"], 3),
                                         round(LEVELS[h["level"]] + h["base_offset"] + h["height"], 3)]})
    for i, a in enumerate(anchors):
        a["index"] = i
        a["mark"] = f"PLX-FIN-{i + 1}"
    finned_hosts = {a["host"] for a in anchors}
    finned_area = sum(p["area_m2"] for p in se_panels if p["mark"] in finned_hosts)

    # ---- solid panels as modelled (the penthouse SE louvre bands) and quantities
    solid_now = [{"id": int(k), "area_m2": round(v["area_m2"], 4)} for k, v in rt.items()
                 if v["category"] == "OST_CurtainWallPanels" and v["family_type"] == "System Panel: Solid"]
    all_panels = [v for v in rt.values() if v["category"] == "OST_CurtainWallPanels"]
    slab = {v["mark"]: v for v in rt.values() if v["category"] == "OST_Floors" and v.get("mark")}
    gfa = sum(slab[m]["area_m2"] for m in ("SLAB-L1", "SLAB-L2", "SLAB-L3", "SLAB-L4", "PH-SE-ROOF", "PH-NW-ROOF"))
    se_all_glass = sum(r["area_m2"] for r in lites) + sum(r["area_m2"] for r in slivers)
    se_extra = sum(panel_row(e)["area_m2"] for e in panels_by_host.get("SE-L2-TRANSOM", []))
    quantities = {
        "se_facade_area_m2": round((60.35 - 4.30) * LEVELS["Roof"], 1),
        "se_facade_note": "SE studio facade between the corner towers (x 4.30-60.35) from L1 to the roof (18.26 m)",
        "se_glazed_area_m2": round(sum(r["area_m2"] for r in lites), 2),
        "se_glazed_area_note": "the ranked lites (the question); the head slivers and the entrance transom/doors stay glazed",
        "se_glazed_area_finned_bays_m2": round(finned_area, 2),
        "se_sliver_area_m2": round(sum(r["area_m2"] for r in slivers), 2),
        "se_transom_area_m2": round(se_extra, 2),
        "se_total_glass_area_m2": round(se_all_glass + se_extra, 2),
        "sw_glass_area_m2": 0.0,
        "sw_note": "the P1 model has no SW glazing (SW-PROJ is a blank concrete projection)",
        "skylight_glazing_area_m2": round(sum(l["glazing_area_m2"] for l in lanterns), 2),
        "skylight_glazing_area_per_lantern_m2": round(sum(l["glazing_area_m2"] for l in lanterns) / 12, 3),
        "solid_panel_area_now_m2": round(sum(p["area_m2"] for p in solid_now), 2),
        "roof_area_m2": round(slab["ROOF-MAIN"]["area_m2"], 1),
        "gross_floor_area_m2": round(gfa, 1),
        "gross_floor_area_note": "slabs L1-L4 + the two penthouse roofs (as penthouse floor proxies)",
        "storeys": 4,
        "facade_azimuth_deg": {"SE": 140.65, "NW": 320.65, "NE": 50.65, "SW": 230.65},
    }
    counts = {
        "revit_panels": len(all_panels),
        "revit_panels_glazed": sum(1 for v in all_panels if v["family_type"] == "System Panel: Glazed"),
        "revit_panels_solid": len(solid_now),
        "revit_mullions": truth["counts"]["OST_CurtainWallMullions"],
        "ifc_plates": sum(1 for e in transfer if e["IfcClass"] == "IfcPlate"),
        "se_hosts": len(se_hosts), "se_ranked_panels": len(se_panels), "se_head_slivers": len(slivers),
        "lanterns": len(lanterns), "lantern_glazing_panels": sum(len(l["glazing_ids"]) for l in lanterns),
        "fin_anchors": len(anchors), "fins_existing_plx": 0,
    }
    out = {
        "version": 1,
        "source": {"ifc": str(IFC_PATH), "revit": str(REPO / "revit" / "LangfordA_Plurarch.rvt"),
                   "revit_source_p1": str(P1 / "03_revit" / "LangfordA_P1.rvt"),
                   "generated_by": "tools/langford_prep.py",
                   "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
        "frame": "revit_model_m",
        "frame_note": ("Revit model coordinates in metres: X along the long axis (bearing 50.65 deg, NE), Y toward "
                       "NW, origin at the S corner of the roof outline, Z = 0 at Level 1 = 101.64 m NAVD88. The IFC "
                       "and the Rhino render scene use true-north ENU = this frame rotated +39.35 deg about Z at the "
                       "origin (no translation): E = X cos t - Y sin t, N = X sin t + Y cos t, t = 39.35 deg."),
        "to_rhino": {"rotate_z_deg": ROT_DEG, "translate": [0, 0, 0]},
        "levels": LEVELS,
        "counts": counts,
        "infill_finish": {
            "solid_panel_type": SOLID_TYPE, "glazed_panel_type": GLAZED_TYPE,
            "solid_panel_type_id": 62119, "glazed_panel_type_id": 20916,
            "material_parameter": "Material",
            "materials": {k: v["revit_material"] for k, v in FINISH_MATERIALS.items()},
            "materials_detail": FINISH_MATERIALS,
            "as_modelled_material": "Default (none: the 14 penthouse louvre panels have no material)",
            "default": "concrete",
            "solid_panels_now": solid_now,
            "note": ("All solid panels share the type, so the finish also applies to the 14 penthouse SE louvre "
                     "panels (PH-SE-LOU-1/2) and to every SE lite or lantern panel turned solid."),
        },
        "se_glass_share": {
            "panels": se_panels,
            "n": len(se_panels),
            "total_area_m2": quantities["se_glazed_area_m2"],
            "glazed_now": sum(1 for p in se_panels if p["glazed_now"]),
            "default_share": round(100 * sum(1 for p in se_panels if p["glazed_now"]) / len(se_panels)),
            "rule": "glazed = the first round(share/100 * n) panels by rank; the others solid",
            "ranking": ("rank 0 = most valuable. Primary: edge (0 = bay centre, 1 = lite next to a pier/tower), "
                        "edges go first so each bay keeps a central vision band; then lower floors first "
                        "(L1, L2, L3, L4); then outer bays first; then x."),
            "excluded": {"head_slivers": len(slivers), "head_slivers_note":
                         "top-row slivers of each ribbon window (2-163 mm tall, < 0.25 m2): not part of the question",
                         "entrance": "SE-L2-TRANSOM (4 panels) and the doors DR-SE-1..4 stay glazed"},
        },
        "skylights_open": {
            "lanterns": [{k: l[k] for k in ("index", "mark", "glazing_ids", "glazing_area_m2", "glazed_now",
                                             "glazing_wall_id", "glazing_wall_mark", "roof_id", "centroid",
                                             "glazing_mid")} for l in ordered],
            "n": 12,
            "default_open": sum(1 for l in ordered if l["glazed_now"]),
            "rule": "glazed lanterns = the first n by index; the others get the solid panel type",
            "ordering": ("point-symmetric pairs about the roof centre, chosen greedily (max-min distance) so the "
                         "open lanterns stay spread over the top-floor studios; one pair per slider step"),
        },
        "fin_depth": {
            "anchors": anchors,
            "n": len(anchors),
            "wall_type": FIN_TYPE,
            "mark_prefix": "PLX-FIN-",
            "thickness_m": 0.20,
            "rule": "for every anchor, a wall from base along dir with length = depth (none when 0)",
            "placement": ("two fins per full SE bay at the third points (mullion lines 3 and 6 of 9; spacing 3.77 m); "
                          "the piers and corner towers act as the bay-end fins; L2 bay 2 (quad entrance) has none; "
                          "fins run from the floor to the window head; no SW anchors (no SW glass in the model)"),
            "finned_glazed_area_m2": round(finned_area, 2),
        },
        "quantities": quantities,
    }
    return out, rt, transfer, by_mark, se_panels, ordered, solid_now


def check_schedules(out):
    """Cross-check the counts against the P1 curtain panel schedule (CSV)."""
    path = P1 / "03_revit" / "schedules_csv" / "ARCA_-_Curtain_Panel_Schedule.csv"
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))[3:]
    c = Counter(r[0] for r in rows if r and r[0])
    ok = (sum(c.values()) == out["counts"]["revit_panels"]
          and c.get("System Panel: Glazed", 0) == out["counts"]["revit_panels_glazed"]
          and c.get("System Panel: Solid", 0) == out["counts"]["revit_panels_solid"])
    return ok, dict(c)


# --------------------------------------------------------------------------------------------------
# phone base geometry
# --------------------------------------------------------------------------------------------------

def material_key(e: dict) -> str:
    cls, t = e["IfcClass"], e["TypeName"]
    if cls == "IfcPlate":
        return "glass" if t == GLAZED_TYPE else "infill"
    if cls == "IfcMember":
        return "aluminium" if "Mullion" in t else "concrete"
    if cls in ("IfcWindow", "IfcDoor"):
        return "glass"
    if cls == "IfcSlab":
        if "Planting" in t:
            return "planting"
        if "Paving" in t or "Stair Tread" in t:
            return "paving"
        if "Roof Deck" in t:
            return "roof"
        return "concrete"
    return "concrete"


def build_mesh(elements: dict, transfer: list, by_mark: dict):
    import numpy as np
    import ifcopenshell
    import ifcopenshell.geom

    t0 = time.time()
    f = ifcopenshell.open(str(IFC_PATH))
    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)

    se_rank = {p["id"]: p["rank"] for p in elements["se_glass_share"]["panels"]}
    lantern_of = {pid: l["index"] for l in elements["skylights_open"]["lanterns"] for pid in l["glazing_ids"]}
    solid_ids = {p["id"] for p in elements["infill_finish"]["solid_panels_now"]}
    host_level = {m: by_mark[m]["params"].get("levelName") for m in by_mark if by_mark[m].get("params")}

    c, s = math.cos(math.radians(-ROT_DEG)), math.sin(math.radians(-ROT_DEG))
    items = []
    for e in transfer:
        try:
            prod = f.by_guid(e["GlobalId"])
            shape = ifcopenshell.geom.create_shape(settings, prod)
        except Exception as ex:  # noqa: BLE001
            print("  no geometry:", e["GlobalId"], e["IfcClass"], ex)
            continue
        v = np.array(shape.geometry.verts, dtype=np.float64).reshape(-1, 3)
        tri = np.array(shape.geometry.faces, dtype=np.int64).reshape(-1, 3)
        if len(tri) == 0:
            continue
        x = v[:, 0] * c - v[:, 1] * s
        y = v[:, 0] * s + v[:, 1] * c
        v = np.stack([x, y, v[:, 2]], axis=1)
        items.append((e, v, tri))
    print(f"  tessellated {len(items)} products in {time.time() - t0:.1f} s")

    allv = np.concatenate([it[1] for it in items])
    lo, hi = allv.min(0), allv.max(0)
    lo = np.floor(lo * 100) / 100
    hi = np.ceil(hi * 100) / 100
    scale = (hi - lo) / 65535.0

    pos_chunks, idx_chunks, rows = [], [], []
    vofs = iofs = 0
    max_err = 0.0
    for e, v, tri in items:
        q = np.round((v - lo) / scale).astype(np.uint32)
        # weld identical quantized vertices per element
        uq, inv = np.unique(q, axis=0, return_inverse=True)
        inv = inv.reshape(-1)
        t2 = inv[tri]
        t2 = t2[(t2[:, 0] != t2[:, 1]) & (t2[:, 1] != t2[:, 2]) & (t2[:, 0] != t2[:, 2])]
        if len(t2) == 0:
            continue
        deq = uq * scale + lo
        max_err = max(max_err, float(np.abs(deq[inv] - v).max()))
        assert len(uq) < 65536
        pos_chunks.append(uq.astype(np.uint16))
        idx_chunks.append(t2.astype(np.uint16).reshape(-1))
        rid = int(e["RevitElementId"]) if e.get("RevitElementId") else None
        group, extra = None, {}
        if rid in se_rank:
            group, extra = "se_glass_share", {"rank": se_rank[rid]}
        elif rid in lantern_of:
            group, extra = "skylights_open", {"lantern": lantern_of[rid]}
        elif rid in solid_ids:
            group = "infill_finish"
        pm = e.get("ParentMark")
        lvl = host_level.get(e.get("Mark") or "") or host_level.get(pm or "")
        row = {"id": rid, "gid": e["GlobalId"], "mark": e.get("Mark"), "pmark": pm, "cls": e["IfcClass"],
               "type": e["TypeName"], "mat": material_key(e), "q": group,
               "lvl": LEVEL_SHORT.get(lvl, lvl) if lvl else None,
               "v": [vofs, len(uq)], "i": [iofs, int(t2.size)]}
        row.update(extra)
        rows.append(row)
        vofs += len(uq)
        iofs += int(t2.size)

    pos = np.concatenate(pos_chunks)
    idx = np.concatenate(idx_chunks)
    pos_bytes = pos.tobytes()
    idx_bytes = idx.tobytes()
    pad = (4 - len(pos_bytes) % 4) % 4
    blob = pos_bytes + b"\0" * pad + idx_bytes
    return {
        "rows": rows, "blob": blob, "lo": lo.tolist(), "hi": hi.tolist(), "scale": scale.tolist(),
        "n_vertices": int(len(pos)), "n_indices": int(len(idx)), "pos_bytes": len(pos_bytes), "pad": pad,
        "max_quant_err_m": round(max_err, 5),
    }


def build_trees():
    ctx = load_json(P1 / "05_rhino" / "context.json")
    trees = []
    for t in ctx["trees"]:
        h_l, h_g = t.get("h_lidar_m"), t.get("h_gis_m") or 0
        h = h_l if (h_l and 2.5 < h_l < 22) else (h_g if h_g > 1.5 else 5.0)
        h = min(h, 18.0)
        r = max((t.get("spread_gis_m") or 0) / 2.0, 0.3 * h)
        r = min(r, 0.55 * h)
        x, y = rot(t["e"], t["n"], -ROT_DEG)
        trees.append([round(x, 2), round(y, 2), round(t["z"], 2), round(h, 2), round(r, 2)])
    buildings = []
    for b in ctx["buildings"]:
        ring = [[round(v, 2) for v in rot(p[0], p[1], -ROT_DEG)] for p in b["ring"]]
        buildings.append({"name": b.get("name"), "abbr": b.get("abbr"), "z0": round(b["z0"], 2),
                          "z1": round(b["z1"], 2), "ring": ring})
    return trees, buildings


MATERIALS = {   # suggested viewer colours (linear-ish sRGB 0..1); "infill" follows infill_finish
    "concrete": {"color": [0.72, 0.70, 0.66], "roughness": 0.9, "opacity": 1.0, "label": "Bush-hammered concrete"},
    "glass": {"color": [0.42, 0.55, 0.62], "roughness": 0.05, "opacity": 0.45, "label": "Glass"},
    "aluminium": {"color": [0.62, 0.64, 0.66], "roughness": 0.4, "opacity": 1.0, "label": "Aluminium mullion"},
    "paving": {"color": [0.66, 0.64, 0.60], "roughness": 0.95, "opacity": 1.0, "label": "Site paving / steps"},
    "planting": {"color": [0.33, 0.45, 0.25], "roughness": 1.0, "opacity": 1.0, "label": "Planting bed"},
    "roof": {"color": [0.30, 0.30, 0.31], "roughness": 0.9, "opacity": 1.0, "label": "Roof membrane"},
    "infill": {"color": [0.72, 0.70, 0.66], "roughness": 0.9, "opacity": 1.0,
               "label": "Solid infill panel (colour follows infill_finish)"},
}
FINISH_COLOURS = {
    "concrete": {"color": [0.72, 0.70, 0.66], "roughness": 0.9, "opacity": 1.0},
    "aluminium": {"color": [0.78, 0.80, 0.82], "roughness": 0.3, "opacity": 1.0, "metalness": 0.8},
    "fritted_glass": {"color": [0.85, 0.88, 0.88], "roughness": 0.2, "opacity": 0.85},
}


def write_site(elements, mesh, trees, buildings):
    OUT_SITE.mkdir(parents=True, exist_ok=True)
    (OUT_SITE / "base.bin").write_bytes(mesh["blob"])
    base = {
        "version": 1,
        "frame": "revit_model_m",
        "units": "m", "up": "z",
        "frame_note": elements["frame_note"],
        "source": "P1 IFC4 RV export (ifcopenshell 0.8.5 tessellation), rotated -39.35 deg into the model frame",
        "bin": "base.bin",
        "bin_bytes": len(mesh["blob"]),
        "quantization": {"type": "uint16", "min": mesh["lo"], "max": mesh["hi"], "scale": mesh["scale"],
                         "decode": "xyz = min + q * scale", "max_error_m": mesh["max_quant_err_m"]},
        "layout": {"positions": {"offset": 0, "count": mesh["n_vertices"], "type": "uint16x3"},
                   "indices": {"offset": mesh["pos_bytes"] + mesh["pad"], "count": mesh["n_indices"],
                               "type": "uint16, LOCAL to each element (add the element's vertex offset)"}},
        "materials": MATERIALS,
        "finish_colours": FINISH_COLOURS,
        "groups": {
            "se_glass_share": "SE studio lites; glazed iff rank < round(share/100 * n), else material 'infill'",
            "skylights_open": "lantern glazing; glazed iff lantern < n_open, else material 'infill'",
            "infill_finish": "solid panels as modelled (penthouse louvres): material 'infill'",
            "fin_depth": "no base geometry: generate one box per anchor (config/langford/elements.json)",
        },
        "counts": {"elements": len(mesh["rows"]), "vertices": mesh["n_vertices"],
                   "triangles": mesh["n_indices"] // 3, "trees": len(trees),
                   "by_material": dict(Counter(r["mat"] for r in mesh["rows"])),
                   "by_group": dict(Counter(r["q"] for r in mesh["rows"] if r["q"]))},
        "se_n": elements["se_glass_share"]["n"],
        "lanterns_n": 12,
        "fin_anchors": [{"base": a["base"], "dir": a["dir"], "height_m": a["height_m"], "mark": a["mark"],
                         "thickness_m": 0.2} for a in elements["fin_depth"]["anchors"]],
        "elements": mesh["rows"],
        "trees": {"fields": ["x", "y", "z", "height_m", "radius_m"], "items": trees,
                  "note": "TAMU GIS inventory; height = LiDAR canopy where 2.5-22 m else GIS (as the P1 render); "
                          "radius = max(GIS spread / 2, 0.3 h), capped at 0.55 h"},
        "context_buildings": {"note": "TAMU GIS footprints, massing only (z0..z1 m above L1)", "items": buildings},
    }
    text = json.dumps(base, separators=(",", ":"), ensure_ascii=False)
    (OUT_SITE / "base.json").write_text(text, encoding="utf-8")
    return len(text), len(mesh["blob"])


README = """# Langford A phone base geometry (`base.json` + `base.bin`)

Generated by `tools/langford_prep.py` (run with `C:\\Python314\\python.exe`) from the P1 Revit-first
IFC export of Langford Architecture Center Building A. Do not edit by hand; re-run the script.

## Frame
`revit_model_m`: Revit model coordinates in metres, Z up. X runs along the long axis toward NE
(bearing 50.65 deg), Y toward NW, origin at the S corner of the roof outline, Z = 0 at Level 1
(101.64 m NAVD88). The SE studio facade faces -Y. The IFC and the Rhino render scene use true-north
ENU, which is this frame rotated +39.35 deg about Z at the origin (no translation). The same frame
is used in `config/langford/elements.json` (fin anchors, panel centres).

## Files
- `base.json`: metadata, materials, one row per element, fin anchors, trees, context buildings.
- `base.bin`: little-endian binary. Two arrays, back to back:
  1. positions: `uint16 x 3` per vertex, `count = layout.positions.count`, at byte 0.
     Decode: `xyz = quantization.min + q * quantization.scale` (per axis; ~1.5 mm steps).
  2. indices: `uint16` triangle corners, at byte `layout.indices.offset` (4-byte aligned),
     `count = layout.indices.count`. Indices are LOCAL to each element: add the element's vertex
     offset `v[0]`.

## Element rows (`elements[]`)
| field | meaning |
|---|---|
| `id` | Revit ElementId (same id the AI changes in Revit) |
| `gid` | IFC GlobalId |
| `mark` / `pmark` | P1 Mark of the element / of its parent curtain wall (panels have only `pmark`, e.g. `SE-L1-W1a`) |
| `cls`, `type` | IFC class and Revit `Family:Type` |
| `mat` | material key: `concrete`, `glass`, `aluminium`, `paving`, `planting`, `roof`, `infill` |
| `q` | question group: `se_glass_share`, `skylights_open`, `infill_finish`, or null |
| `rank` | (`se_glass_share` only) glazed iff `rank < round(share/100 * se_n)` |
| `lantern` | (`skylights_open` only) lantern index; glazed iff `lantern < skylights_open` |
| `lvl` | level (L1..L4, Roof) where known |
| `v` | `[vertex offset, vertex count]` into positions |
| `i` | `[index offset, index count]` into indices |

## Applying parameters (the plan rule, identical to `design_mcp/langford_plan.py`)
- `se_glass_share` (40-100 %): panels with `q = se_glass_share` keep `mat = glass` iff
  `rank < round(share / 100 * se_n)`, else they use `infill`. round = half up (floor(x + 0.5)); the
  shares in steps of 10 never produce a half here (se_n = 142), so `Math.round` gives the same result.
- `skylights_open` (0-12): lantern glazing with `lantern < n` stays `glass`, else `infill`.
- `infill_finish`: every `infill` element (the as-modelled solid panels plus everything switched to
  solid above) takes the colour from `finish_colours[infill_finish]`.
- `fin_depth` (0-1.2 m): for every `fin_anchors[k]`, a box from `base` along `dir` with length =
  depth, thickness 0.2 m (centred on the line), height `height_m` upward from `base[2]`. None at 0.

## Trees and context
`trees.items`: `[x, y, z, height_m, radius_m]` in the same frame (draw low-poly trees: trunk plus
a cone/ico crown). `context_buildings.items`: neighbouring footprints (`ring` in XY) with `z0..z1`.
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-mesh", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    elements, rt, transfer, by_mark, se_panels, lanterns, solid_now = build_elements()
    ok, sched = check_schedules(elements)
    elements["counts"]["schedule_check"] = {"ok": ok, "curtain_panel_schedule": sched}
    OUT_ELEMENTS.parent.mkdir(parents=True, exist_ok=True)
    OUT_ELEMENTS.write_text(json.dumps(elements, indent=1, ensure_ascii=False), encoding="utf-8")

    c, q = elements["counts"], elements["quantities"]
    print("Langford A elements.json")
    print(f"  panels in Revit: {c['revit_panels']} ({c['revit_panels_glazed']} glazed, {c['revit_panels_solid']} solid) "
          f"| schedule: {sched} -> {'OK' if ok else 'MISMATCH'}")
    print(f"  SE studio lites (ranked): {c['se_ranked_panels']} on {c['se_hosts']} ribbon windows "
          f"(+{c['se_head_slivers']} head slivers excluded); glazed now {elements['se_glass_share']['glazed_now']} "
          f"-> default share {elements['se_glass_share']['default_share']} %")
    print(f"  lanterns: {c['lanterns']} ({c['lantern_glazing_panels']} glazing panels); order "
          + " ".join(l["mark"][4:] for l in elements["skylights_open"]["lanterns"]))
    print(f"  fin anchors: {c['fin_anchors']} (SE only; SW has no glass)")
    print(f"  areas: SE lites {q['se_glazed_area_m2']} m2 (finned bays {q['se_glazed_area_finned_bays_m2']}), "
          f"SE facade {q['se_facade_area_m2']} m2, skylight glass {q['skylight_glazing_area_m2']} m2, "
          f"solid now {q['solid_panel_area_now_m2']} m2, roof {q['roof_area_m2']} m2, GFA {q['gross_floor_area_m2']} m2")
    print(f"  -> {OUT_ELEMENTS}")
    if args.no_mesh:
        return
    mesh = build_mesh(elements, transfer, by_mark)
    trees, buildings = build_trees()
    jlen, blen = write_site(elements, mesh, trees, buildings)
    (OUT_SITE / "README.md").write_text(README, encoding="utf-8")
    print(f"Phone base: {len(mesh['rows'])} elements, {mesh['n_vertices']} vertices, {mesh['n_indices'] // 3} triangles, "
          f"{len(trees)} trees, {len(buildings)} context buildings; quantization error {mesh['max_quant_err_m']} m")
    print(f"  base.json {jlen / 1e6:.2f} MB + base.bin {blen / 1e6:.2f} MB = {(jlen + blen) / 1e6:.2f} MB -> {OUT_SITE}")
    print(f"done in {time.time() - t0:.1f} s")


if __name__ == "__main__":
    sys.exit(main())
