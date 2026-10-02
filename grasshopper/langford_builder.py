#! python3
# -*- coding: utf-8 -*-
"""
PLURARCH - Langford A on the projector              (grasshopper/langford_builder.py)
=================================================================================

Runs inside the Rhino 8 Grasshopper "Python 3 Script" component PlurarchModel, through
the short loader in grasshopper/SETUP.md (it execs this file and picks up edits on
the next Timer tick). The Rhino document must be the render copy
rhino/LangfordA_Plurarch_render.3dm (never the P1 source in LangfordA_Fresh).

It watches <state_dir>/parameters.json and applies the plan rule of docs/LANGFORD.md
to the REAL scene objects (IFC meshes tagged with RevitElementId user text):

  infill_finish   concrete | aluminium | fritted_glass -> material of every solid
                  infill panel (the 14 penthouse louvre panels, the SE lites and the
                  lantern panels that are turned solid)
  se_glass_share  40..100 %, step 10 -> the first round(share/100 * 142) SE lites by
                  rank keep their glass; the others get the infill finish
  fin_depth       0..1.2 m, step 0.3 -> one concrete fin (0.2 m x depth x height) per
                  anchor, as breps on layer Plurarch::Fins (replaced on change)
  skylights_open  0..12, step 2 -> the first n lanterns by index keep their glass;
                  the glazing of the others gets the infill finish

Materials are changed through object attributes (render material, material source
"from object"), so Rendered, Raytraced and Cycles all show them. The first time an
object is touched its original material is stored in its user text
(PLX_orig_material = "<material source name>|<material index>"), so a glazed panel gets
back exactly the material it had in the P1 scene. Everything is idempotent: only
objects whose material differs from the plan are written.

COMPONENT INPUTS   path (str, Item Access): parameters.json (a folder also works)
                   tick (no type hint): optional Button = force re-read and re-apply
                   scale (float, optional): model units per metre; empty = document units
COMPONENT OUTPUTS  geometry, colors (empty lists: the model lives in the document),
                   info (text, for a Panel), warning (text, empty when all is fine)

Elements and frame: config/langford/elements.json (Revit model metres). The Rhino scene
is true-north ENU = the model frame rotated to_rhino.rotate_z_deg (+39.35 deg) about Z
at the origin, no translation.

TESTING OUTSIDE RHINO (plain CPython 3.9+):
    python grasshopper/langford_builder.py --selftest
"""

import json
import math
import os
import sys
import time

SCRIPT_VERSION = "1.0.0"
REPO_DEFAULT = r"E:\Academic\PhD\Fall 2026\AI Workshop\PlurARCH"

# ---------------------------------------------------------------------------
# 1. Parameter spec. Taken from config/parameters.json when it holds the four
#    Langford keys; until then the built-in values below (docs/LANGFORD.md).
# ---------------------------------------------------------------------------
PARAM_KEYS = ("infill_finish", "se_glass_share", "fin_depth", "skylights_open")
BUILTIN_SPEC = {
    "options": ("concrete", "aluminium", "fritted_glass"),
    "numeric": (("se_glass_share", 40.0, 100.0, 10.0),   # key, min, max, step
                ("fin_depth", 0.0, 1.2, 0.3),
                ("skylights_open", 0.0, 12.0, 2.0)),
    "defaults": {"infill_finish": "concrete", "se_glass_share": 100.0,
                 "fin_depth": 0.0, "skylights_open": 12.0},
}

# ---------------------------------------------------------------------------
# 2. Rhino scene constants
# ---------------------------------------------------------------------------
FIN_LAYER = "Plurarch::Fins"
FIN_LAYER_COLOR = (150, 146, 138)
USER_ORIG = "PLX_orig_material"     # "<MaterialSource name>|<MaterialIndex>" before Plurarch
USER_MAPPED = "PLX_box_mapping"     # concrete texture box mapping applied
USER_FIN = "PLX_fin"                # "<mark>|<signature>" on generated fin breps
CONCRETE_MAPPING_SIZE = 2.4         # m, same box mapping as P1 materials.py
FINISH_MATERIALS = {
    # key: (render material name, create-if-missing PBR spec or None = must exist)
    "concrete": ("ARCA bush-hammered concrete (granular_concrete CC0)", None),
    "aluminium": ("PLX brushed aluminium (Plurarch infill)",
                  {"rgb": (196, 199, 202), "rough": 0.30, "metal": 1.0, "aniso": 0.65}),
    "fritted_glass": ("PLX fritted glass (Plurarch infill)",
                      {"rgb": (236, 240, 238), "rough": 0.35, "metal": 0.0, "opacity": 0.45,
                       "ior": 1.52, "opac_rough": 0.55}),
}
CONCRETE_FALLBACK_MATCH = "bush-hammered"   # if the exact name is missing
FIN_MATERIAL = "concrete"

# ---------------------------------------------------------------------------
# 3. Small helpers (pure Python)
# ---------------------------------------------------------------------------
def _as_number(value):
    """Finite float from an int/float/numeric string, else None (bools rejected)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        x = float(value)
    elif isinstance(value, str):
        try:
            x = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def snap(value, lo, hi, inc):
    """Clamp to [lo, hi] and snap to lo + k * inc (ties round up)."""
    x = min(max(float(value), lo), hi)
    x = lo + math.floor((x - lo) / inc + 0.5) * inc
    return round(min(max(x, lo), hi), 6)


def round_half_up(x):
    """Math.round of JavaScript (Python's round() would round 0.5 to even)."""
    return int(math.floor(x + 0.5))


def _fmt(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    if abs(x - round(x)) < 1e-9:
        return "%d" % int(round(x))
    return ("%.3f" % x).rstrip("0").rstrip(".")


def describe(params):
    return "%s, %s %% SE glass, fins %s m, %s skylights" % (
        params["infill_finish"], _fmt(params["se_glass_share"]),
        _fmt(params["fin_depth"]), _fmt(params["skylights_open"]))


def _this_file():
    """Path of this file, also when exec'd by the Grasshopper loader (no __file__)."""
    f = globals().get("__file__")
    if f:
        return os.path.abspath(f)
    try:
        return os.path.abspath(sys._getframe(0).f_code.co_filename)
    except Exception:
        return ""


def repo_root():
    here = _this_file()
    if here:
        root = os.path.dirname(os.path.dirname(here))
        if os.path.isfile(os.path.join(root, "config", "langford", "elements.json")):
            return root
    return REPO_DEFAULT


def _file_sig(p):
    try:
        st = os.stat(p)
    except (OSError, ValueError):
        return None
    return (st.st_mtime_ns, st.st_size)


# ---------------------------------------------------------------------------
# 4. Spec + elements loading (pure Python)
# ---------------------------------------------------------------------------
def load_spec(config_path, elements=None):
    """Parameter spec from config/parameters.json if it has the Langford keys."""
    spec = {"options": BUILTIN_SPEC["options"], "numeric": BUILTIN_SPEC["numeric"],
            "defaults": dict(BUILTIN_SPEC["defaults"]), "source": "built-in"}
    if elements:   # defaults = the building as modelled (prep script)
        try:
            spec["defaults"]["infill_finish"] = elements["infill_finish"]["default"]
            spec["defaults"]["se_glass_share"] = float(elements["se_glass_share"]["default_share"])
            spec["defaults"]["skylights_open"] = float(elements["skylights_open"]["default_open"])
        except (KeyError, TypeError, ValueError):
            pass
    try:
        with open(config_path, "r", encoding="utf-8-sig") as fh:
            conf = {p["key"]: p for p in json.load(fh)["parameters"]}
    except Exception:
        return spec
    if not all(k in conf for k in PARAM_KEYS):
        return spec
    try:
        opts = tuple(o["value"] if isinstance(o, dict) else o for o in conf["infill_finish"]["options"])
        numeric = tuple((k, float(conf[k]["min"]), float(conf[k]["max"]), float(conf[k]["step"]))
                        for k in PARAM_KEYS[1:])
        defaults = {"infill_finish": conf["infill_finish"]["default"]}
        for k in PARAM_KEYS[1:]:
            defaults[k] = float(conf[k]["default"])
    except (KeyError, TypeError, ValueError):
        return spec
    return {"options": opts, "numeric": numeric, "defaults": defaults, "source": "config"}


def load_elements(path):
    """Compact, validated view of config/langford/elements.json. Raises ValueError."""
    with open(path, "r", encoding="utf-8-sig") as fh:
        raw = json.load(fh)
    try:
        rot = raw.get("to_rhino", {})
        panels = sorted(raw["se_glass_share"]["panels"], key=lambda p: int(p["rank"]))
        lanterns = sorted(raw["skylights_open"]["lanterns"], key=lambda l: int(l["index"]))
        fin = raw["fin_depth"]
        el = {
            "rotate_deg": float(rot.get("rotate_z_deg", 0.0)),
            "translate": tuple(float(v) for v in (rot.get("translate") or (0, 0, 0))),
            "se_panels": [{"id": int(p["id"]), "rank": int(p["rank"]), "key": p.get("key", ""),
                           "center": tuple(p.get("center") or (0, 0, 0))} for p in panels],
            "lanterns": [{"index": int(l["index"]), "mark": l.get("mark", ""),
                          "glazing_ids": [int(g) for g in l["glazing_ids"]]} for l in lanterns],
            "solid_now": [int(p["id"]) for p in raw["infill_finish"].get("solid_panels_now", [])],
            "anchors": [{"index": int(a.get("index", i)), "mark": a.get("mark", "PLX-FIN-%d" % (i + 1)),
                         "base": tuple(float(v) for v in a["base"]),
                         "dir": tuple(float(v) for v in a["dir"]),
                         "height": float(a["height_m"]), "base_offset": float(a.get("base_offset_m", 0.0))}
                        for i, a in enumerate(fin["anchors"])],
            "fin_thickness": float(fin.get("thickness_m", 0.2)),
            "defaults_raw": raw,
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("elements.json is missing %s" % exc)
    if not el["se_panels"] or not el["lanterns"]:
        raise ValueError("elements.json has no SE panels or lanterns")
    return el


# ---------------------------------------------------------------------------
# 5. Parameters file: read + validate (pure Python)
# ---------------------------------------------------------------------------
def clamp_params(params, spec):
    src = params if isinstance(params, dict) else {}
    out = {}
    f = src.get("infill_finish")
    f = f.strip().lower() if isinstance(f, str) else ""
    out["infill_finish"] = f if f in spec["options"] else spec["defaults"]["infill_finish"]
    for key, lo, hi, inc in spec["numeric"]:
        x = _as_number(src.get(key))
        out[key] = snap(spec["defaults"][key] if x is None else x, lo, hi, inc)
    return out


def validate_parameters(raw, spec, fallback=None):
    """(params, errors, notes). errors -> reject the set (keep the last good design)."""
    base = clamp_params(fallback if isinstance(fallback, dict) else spec["defaults"], spec)
    errors, notes = [], []
    if not isinstance(raw, dict):
        return None, ["'parameters' is not a JSON object"], notes
    if not any(k in raw for k in PARAM_KEYS):
        return None, ["no Langford parameters (%s) in the file" % ", ".join(PARAM_KEYS)], notes
    params = {}
    if "infill_finish" not in raw:
        params["infill_finish"] = base["infill_finish"]
        notes.append("infill_finish missing, kept %s" % base["infill_finish"])
    else:
        v = raw["infill_finish"]
        f = v.strip().lower() if isinstance(v, str) else None
        if f in spec["options"]:
            params["infill_finish"] = f
        else:
            errors.append("infill_finish=%r is not one of %s" % (v, "/".join(spec["options"])))
    for key, lo, hi, inc in spec["numeric"]:
        if key not in raw:
            params[key] = base[key]
            notes.append("%s missing, kept %s" % (key, _fmt(base[key])))
            continue
        x = _as_number(raw[key])
        if x is None:
            errors.append("%s=%r is not a number" % (key, raw[key]))
            continue
        tol = 1e-9 * max(1.0, abs(hi))
        if x < lo - tol or x > hi + tol:
            errors.append("%s=%s is outside %s..%s" % (key, _fmt(x), _fmt(lo), _fmt(hi)))
            continue
        s = snap(x, lo, hi, inc)
        if abs(s - x) > 1e-6:
            notes.append("%s=%s snapped to %s" % (key, _fmt(x), _fmt(s)))
        params[key] = s
    if errors:
        return None, errors, notes
    return params, errors, notes


def read_parameters_file(file_path, spec, fallback=None):
    """{"status": ok|missing|unreadable|invalid_json|invalid, "params", "message", "notes", "meta"}"""
    res = {"status": "ok", "params": None, "message": "", "notes": [], "meta": {}}
    try:
        with open(file_path, "r", encoding="utf-8-sig") as fh:
            text = fh.read()
    except (IOError, OSError) as exc:
        missing = isinstance(exc, FileNotFoundError)
        res["status"] = "missing" if missing else "unreadable"
        res["message"] = "file not found" if missing else "%s: %s" % (type(exc).__name__, exc)
        return res
    except UnicodeDecodeError as exc:
        res["status"], res["message"] = "unreadable", "not UTF-8 text (%s)" % exc
        return res
    if not text.strip():
        res["status"], res["message"] = "invalid_json", "file is empty (still being written?)"
        return res
    try:
        data = json.loads(text)
    except ValueError as exc:
        res["status"], res["message"] = "invalid_json", "%s (half-written file?)" % exc
        return res
    if not isinstance(data, dict):
        res["status"], res["message"] = "invalid", "top level is not a JSON object"
        return res
    raw = data.get("parameters")
    if raw is None and any(k in data for k in PARAM_KEYS):
        raw = data
    if raw is None:
        res["status"], res["message"] = "invalid", "no 'parameters' object in the file"
        return res
    params, errors, notes = validate_parameters(raw, spec, fallback)
    res["notes"] = notes
    res["meta"] = {k: (str(data.get(k))[:80] if isinstance(data.get(k), (str, int, float)) else None)
                   for k in ("updated_at", "round_id", "verdict")}
    if errors:
        res["status"], res["message"] = "invalid", "; ".join(errors)
        return res
    res["params"] = params
    return res


# ---------------------------------------------------------------------------
# 6. The plan rule (pure Python; identical to design_mcp/langford_plan.py)
# ---------------------------------------------------------------------------
def to_rhino(pt, el):
    """Revit model frame -> Rhino true-north ENU: rotate about Z at the origin, translate."""
    t = math.radians(el["rotate_deg"])
    c, s = math.cos(t), math.sin(t)
    x, y, z = pt
    tx, ty, tz = el["translate"]
    return (x * c - y * s + tx, x * s + y * c + ty, z + tz)


def fin_corners(anchor, depth, thickness, el):
    """8 corners (Brep.CreateFromBox order, Rhino frame) of one fin: a wall from `base`
    along `dir` with length `depth`, `thickness` wide, from base + offset up `height`."""
    bx, by, bz = anchor["base"]
    dx, dy = anchor["dir"]
    ln = math.hypot(dx, dy) or 1.0
    dx, dy = dx / ln, dy / ln
    px, py = -dy, dx                         # across the fin
    h = thickness / 2.0
    foot = [(0.0, -h), (depth, -h), (depth, h), (0.0, h)]
    pts2 = [(bx + u * dx + v * px, by + u * dy + v * py) for u, v in foot]
    area2 = sum(pts2[i][0] * pts2[(i + 1) % 4][1] - pts2[(i + 1) % 4][0] * pts2[i][1] for i in range(4))
    if area2 < 0:                            # counter-clockwise from above
        pts2 = [pts2[0], pts2[3], pts2[2], pts2[1]]
    z0 = bz + anchor.get("base_offset", 0.0)
    z1 = z0 + anchor["height"]
    return [to_rhino((x, y, z0), el) for x, y in pts2] + [to_rhino((x, y, z1), el) for x, y in pts2]


def plan(params, el, spec):
    """What the scene must look like for `params`."""
    p = clamp_params(params, spec)
    panels, lanterns = el["se_panels"], el["lanterns"]
    k = max(0, min(len(panels), round_half_up(p["se_glass_share"] / 100.0 * len(panels))))
    n_open = max(0, min(len(lanterns), int(round(p["skylights_open"]))))
    glazed = [q["id"] for q in panels[:k]]
    solid = [q["id"] for q in panels[k:]]
    for lan in lanterns[:n_open]:
        glazed.extend(lan["glazing_ids"])
    for lan in lanterns[n_open:]:
        solid.extend(lan["glazing_ids"])
    solid.extend(el["solid_now"])
    depth = p["fin_depth"]
    fins = []
    if depth > 1e-6:
        for a in el["anchors"]:
            fins.append({"mark": a["mark"], "corners": fin_corners(a, depth, el["fin_thickness"], el)})
    return {"params": p, "finish": p["infill_finish"], "n_se_glazed": k, "n_se": len(panels),
            "n_open": n_open, "n_lanterns": len(lanterns), "glazed_ids": glazed, "solid_ids": solid,
            "fins": fins, "fin_sig": "%s|%s" % (_fmt(depth), len(fins))}


def plan_key(pl):
    return (pl["finish"], pl["n_se_glazed"], pl["n_open"], pl["fin_sig"])


# ---------------------------------------------------------------------------
# 7. Apply a plan to a scene (pure logic; the scene adapter does the Rhino work)
#    scene.objects_for(rid) -> [obj]      scene.ensure_material(obj, key_or_None) -> bool
#    scene.fins_signature() -> str|None   scene.replace_fins(fins, sig) -> int
# ---------------------------------------------------------------------------
def apply_plan(scene, pl):
    t0 = time.perf_counter()
    report = {"changed": 0, "checked": 0, "missing": [], "fins": None}
    targets = [(rid, None) for rid in pl["glazed_ids"]] + [(rid, pl["finish"]) for rid in pl["solid_ids"]]
    for rid, target in targets:
        objs = scene.objects_for(rid)
        if not objs:
            report["missing"].append(rid)
            continue
        for obj in objs:
            report["checked"] += 1
            if scene.ensure_material(obj, target):
                report["changed"] += 1
    if scene.fins_signature() != pl["fin_sig"]:
        report["fins"] = scene.replace_fins(pl["fins"], pl["fin_sig"])
    report["ms"] = 1000.0 * (time.perf_counter() - t0)
    return report


# ---------------------------------------------------------------------------
# 8. File watcher + cache (pure Python; `state` survives between runs)
# ---------------------------------------------------------------------------
RETRY_SECONDS = 2.0
APPLY_RETRY_SECONDS = 5.0
_TRANSIENT = ("unreadable", "invalid_json")
_STATUS_TEXT = {"missing": "File not found", "unreadable": "Cannot read the file",
                "invalid_json": "File is not valid JSON", "invalid": "Invalid parameters"}


def normalize_path(value):
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return ""
    try:
        s = str(value)
    except Exception:
        return ""
    s = s.strip().strip('"').strip("'").strip()
    if s and os.path.isdir(s):
        s = os.path.join(s, "parameters.json")
    return s


def _clock(t):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
    except Exception:
        return "?"


def load_inputs(state, repo):
    """elements.json + spec, cached by file signature. Returns (el, spec, error)."""
    ep = os.path.join(repo, "config", "langford", "elements.json")
    cp = os.path.join(repo, "config", "parameters.json")
    sig = (_file_sig(ep), _file_sig(cp))
    if state.get("inputs_sig") != sig or state.get("elements") is None:
        try:
            el = load_elements(ep)
            with open(ep, "r", encoding="utf-8-sig") as fh:
                raw = json.load(fh)
            state["elements"], state["spec"] = el, load_spec(cp, raw)
            state["inputs_error"] = ""
        except Exception as exc:
            state["inputs_error"] = "Cannot load %s (%s: %s)" % (ep, type(exc).__name__, exc)
        state["inputs_sig"] = sig
    return state.get("elements"), state.get("spec"), state.get("inputs_error", "")


def step(state, path, scene_factory, force=False, now=None, repo=None):
    """One component run. Never raises. scene_factory() -> (scene, error_text)."""
    now = time.time() if now is None else float(now)
    stats = state.setdefault("stats", {"reads": 0, "applies": 0})
    warnings = []
    el, spec, err = load_inputs(state, repo or repo_root())
    if el is None:
        return {"info": "PLURARCH Langford - not running\n" + err, "warning": err,
                "short": "Langford: no elements.json (!)", "params": None}
    if err:
        warnings.append(err + " -> using the previously loaded elements.")

    # -- 1. watch the parameters file ------------------------------------------
    fpath = normalize_path(path)
    if not fpath:
        state["read_status"], state["file_mtime"], state["sig"] = "no_path", None, None
        state["read_msg"] = ("No file path: connect a Panel with the full path of "
                             "parameters.json to the 'path' input")
    else:
        sig = _file_sig(fpath)
        if sig is None:
            state["sig"], state["file_mtime"] = None, None
            state["read_status"], state["read_msg"] = "missing", "File not found: %s" % fpath
        else:
            state["file_mtime"] = sig[0] / 1e9
            retry = (state.get("read_status") in _TRANSIENT
                     and now - state.get("last_attempt", -1e18) >= RETRY_SECONDS)
            if sig != state.get("sig") or retry or force:
                state["sig"], state["last_attempt"] = sig, now
                stats["reads"] += 1
                res = read_parameters_file(fpath, spec, fallback=state.get("good"))
                state["read_status"] = res["status"]
                if res["status"] == "ok":
                    state["good"], state["good_meta"] = res["params"], res["meta"]
                    state["read_msg"] = ("Note: " + "; ".join(res["notes"])) if res["notes"] else ""
                else:
                    state["read_msg"] = "%s: %s" % (_STATUS_TEXT.get(res["status"], "Problem"),
                                                    res["message"])
    good = state.get("good")
    source = "file" if good is not None else "default"
    params = clamp_params(good if good is not None else spec["defaults"], spec)
    msg = state.get("read_msg") or ""
    if state.get("read_status") not in ("ok", None):
        what = "keeping the last good design" if good is not None else "showing the as-built defaults"
        warnings.append("%s -> %s (%s)." % (msg, what, describe(params)))
    elif msg:
        warnings.append(msg)

    # -- 2. apply the plan when it changed (or the scene changed) ----------------
    pl = plan(params, el, spec)
    key = plan_key(pl)
    scene, scene_err = scene_factory()
    if scene is None:
        warnings.append(scene_err)
    else:
        scene_id = getattr(scene, "scene_id", None)
        changed = key != state.get("applied_key") or scene_id != state.get("scene_id")
        if changed or force:
            may_try = (force or key != state.get("fail_key")
                       or now - state.get("fail_time", -1e18) >= APPLY_RETRY_SECONDS)
            if may_try:
                try:
                    rep = apply_plan(scene, pl)
                    state["applied_key"], state["scene_id"] = key, scene_id
                    state["applied_params"], state["report"] = dict(pl["params"]), rep
                    state.pop("fail_key", None)
                    stats["applies"] += 1
                except Exception as exc:
                    state["fail_key"], state["fail_time"] = key, now
                    state["fail_msg"] = "%s: %s" % (type(exc).__name__, exc)
        if state.get("fail_key") == key:
            warnings.append("Applying the design failed (%s); will retry." % state.get("fail_msg"))
        rep = state.get("report") or {}
        if rep.get("missing") and state.get("applied_key") == key:
            warnings.append("%d element ids not found in the Rhino scene (e.g. %s)."
                            % (len(rep["missing"]), rep["missing"][:3]))

    shown = state.get("applied_params") or pl["params"]
    info = _format_info(state, fpath, shown, pl, source, now, scene)
    short = "%s | %s%% | fins %s m | %s sky" % (
        shown["infill_finish"], _fmt(shown["se_glass_share"]), _fmt(shown["fin_depth"]),
        _fmt(shown["skylights_open"]))
    warning_text = "\n".join(w for w in warnings if w)
    if warning_text:
        short += "  (!)"
    return {"info": info, "warning": warning_text, "short": short, "params": dict(shown),
            "source": source, "plan": pl}


def _format_info(state, fpath, params, pl, source, now, scene):
    stats = state.get("stats", {})
    meta = state.get("good_meta") or {}
    rep = state.get("report") or {}
    lines = [
        "PLURARCH Langford A - %s" % ("design from file" if source == "file"
                                      else "AS-BUILT defaults (no valid parameters yet)"),
        "  infill_finish   : %s" % params["infill_finish"],
        "  se_glass_share  : %s %%  -> %d of %d SE lites glazed" % (
            _fmt(params["se_glass_share"]), pl["n_se_glazed"], pl["n_se"]),
        "  fin_depth       : %s m  -> %d fins" % (_fmt(params["fin_depth"]), len(pl["fins"])),
        "  skylights_open  : %s     -> %d of %d lanterns glazed" % (
            _fmt(params["skylights_open"]), pl["n_open"], pl["n_lanterns"]),
        "file          : %s" % (fpath or "(no path)"),
        "file modified : %s" % (_clock(state["file_mtime"]) if state.get("file_mtime") else "-"),
    ]
    if meta.get("updated_at"):
        lines.append("updated_at    : %s" % meta["updated_at"])
    if meta.get("round_id") or meta.get("verdict"):
        lines.append("round/verdict : %s / %s" % (meta.get("round_id") or "-", meta.get("verdict") or "-"))
    if rep:
        lines.append("last apply    : %d objects checked, %d changed, fins %s, %.0f ms" % (
            rep.get("checked", 0), rep.get("changed", 0),
            "kept" if rep.get("fins") is None else "%d placed" % rep["fins"], rep.get("ms", 0.0)))
    if scene is not None and getattr(scene, "label", ""):
        lines.append("scene         : %s" % scene.label)
    spec = state.get("spec") or {}
    lines.append("spec          : %s" % spec.get("source", "?"))
    lines.append("checked %s  (reads %d, applies %d, v%s)" % (
        time.strftime("%H:%M:%S", time.localtime(now)), stats.get("reads", 0),
        stats.get("applies", 0), SCRIPT_VERSION))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 9. Rhino scene adapter (only used inside Rhino)
# ---------------------------------------------------------------------------
class RhinoScene(object):
    """Reads and writes the Langford render scene. Built once per document and cached."""

    def __init__(self, doc, scale, ids):
        import Rhino
        self.Rhino = Rhino
        self.doc = doc
        self.scale = scale
        self.scene_id = (doc.RuntimeSerialNumber, round(scale, 9))
        self.label = "%s (%d objects indexed)" % (os.path.basename(doc.Path or "?"), 0)
        self.index = {}
        self._mat_index = {}
        self._build_index(ids)
        self.label = "%s (%d element ids -> %d objects)" % (
            os.path.basename(doc.Path or "?"), len(self.index), sum(len(v) for v in self.index.values()))

    # -- index: RevitElementId -> [object Guid] ---------------------------------
    def _build_index(self, ids):
        Rhino = self.Rhino
        want = set(str(i) for i in ids)
        st = Rhino.DocObjects.ObjectEnumeratorSettings()
        st.NormalObjects = True
        st.LockedObjects = True
        st.HiddenObjects = True
        for obj in self.doc.Objects.GetObjectList(st):
            rid = obj.Attributes.GetUserString("RevitElementId")
            if rid and rid in want:
                self.index.setdefault(int(rid), []).append(obj.Id)

    def objects_for(self, rid):
        out = []
        for gid in self.index.get(int(rid), []):
            obj = self.doc.Objects.FindId(gid)
            if obj is not None and not obj.IsDeleted:
                out.append(obj)
        return out

    # -- materials ---------------------------------------------------------------
    def material_index(self, key):
        if key in self._mat_index:
            return self._mat_index[key]
        rm = self._find_or_create(key)
        idx = -1
        for i in range(self.doc.Materials.Count):       # the document material behind rm
            m = self.doc.Materials[i]
            if m is not None and not m.IsDeleted and m.RenderMaterialInstanceId == rm.Id:
                idx = i
                break
        if idx < 0:
            a = self.Rhino.DocObjects.ObjectAttributes()
            a.RenderMaterial = rm
            idx = a.MaterialIndex
        if idx < 0:
            raise RuntimeError("render material %r has no document material" % rm.Name)
        self._mat_index[key] = idx
        return idx

    def _find_or_create(self, key):
        Rhino = self.Rhino
        name, recipe = FINISH_MATERIALS[key]
        for rm in self.doc.RenderMaterials:
            if rm.Name == name:
                return rm
        if recipe is None:            # must exist (the P1 concrete): loose match
            for rm in self.doc.RenderMaterials:
                if CONCRETE_FALLBACK_MATCH in (rm.Name or "").lower():
                    return rm
            raise RuntimeError("render material %r not found in the scene" % name)
        from Rhino.Display import Color4f
        m = Rhino.DocObjects.Material()
        m.Name = name
        m.ToPhysicallyBased()
        p = m.PhysicallyBased
        r, g, b = recipe["rgb"]
        p.BaseColor = Color4f(r / 255.0, g / 255.0, b / 255.0, 1.0)
        p.Roughness = recipe["rough"]
        p.Metallic = recipe["metal"]
        if "aniso" in recipe:
            try:
                p.Anisotropic = recipe["aniso"]
            except Exception:
                pass
        if "opacity" in recipe:
            p.Opacity = recipe["opacity"]
            p.OpacityIOR = recipe["ior"]
            p.OpacityRoughness = recipe["opac_rough"]
        rm = Rhino.Render.RenderMaterial.FromMaterial(m, self.doc)
        rm.Name = name
        self.doc.RenderMaterials.Add(rm)
        return rm

    def ensure_material(self, obj, key):
        """key None = the object's original material, else a finish. True if written."""
        Rhino = self.Rhino
        oms = Rhino.DocObjects.ObjectMaterialSource
        src_obj = oms.MaterialFromObject
        a = obj.Attributes
        cur = (_enum_name(a.MaterialSource), int(a.MaterialIndex))
        orig = a.GetUserString(USER_ORIG)
        if key is None:
            if not orig:
                return False                      # never touched: already original
            s, i = orig.split("|")
            i = int(i)
            if cur == (s, i):
                return False
            a2 = a.Duplicate()
            a2.MaterialSource = getattr(oms, s, oms.MaterialFromLayer)
            a2.MaterialIndex = i
            return self.doc.Objects.ModifyAttributes(obj, a2, True)
        idx = self.material_index(key)
        if cur == (_enum_name(src_obj), idx) and (key != "concrete" or a.GetUserString(USER_MAPPED)):
            return False
        a2 = a.Duplicate()
        if not orig:
            a2.SetUserString(USER_ORIG, "%s|%d" % cur)
        a2.MaterialSource = src_obj
        a2.MaterialIndex = idx
        if key == "concrete" and not a.GetUserString(USER_MAPPED):
            a2.SetUserString(USER_MAPPED, "1")
            ok = self.doc.Objects.ModifyAttributes(obj, a2, True)
            self._box_mapping(obj.Id)
            return ok
        return self.doc.Objects.ModifyAttributes(obj, a2, True)

    def _box_mapping(self, gid):
        Rhino = self.Rhino
        G = Rhino.Geometry
        s = CONCRETE_MAPPING_SIZE * self.scale
        tm = Rhino.Render.TextureMapping.CreateBoxMapping(
            G.Plane.WorldXY, G.Interval(0, s), G.Interval(0, s), G.Interval(0, s), True)
        obj = self.doc.Objects.FindId(gid)
        if obj is not None:
            self.doc.Objects.ModifyTextureMapping(obj, 1, tm)

    # -- fins ----------------------------------------------------------------------
    def _fin_layer(self):
        Rhino = self.Rhino
        idx = self.doc.Layers.FindByFullPath(FIN_LAYER, -1)
        if idx >= 0:
            return idx
        parent_name, child_name = FIN_LAYER.split("::")
        pidx = self.doc.Layers.FindByFullPath(parent_name, -1)
        if pidx < 0:
            lay = Rhino.DocObjects.Layer()
            lay.Name = parent_name
            pidx = self.doc.Layers.Add(lay)
        lay = Rhino.DocObjects.Layer()
        lay.Name = child_name
        lay.ParentLayerId = self.doc.Layers[pidx].Id
        import System
        r, g, b = FIN_LAYER_COLOR
        lay.Color = System.Drawing.Color.FromArgb(255, r, g, b)
        return self.doc.Layers.Add(lay)

    def _fin_objects(self):
        idx = self.doc.Layers.FindByFullPath(FIN_LAYER, -1)
        if idx < 0:
            return []
        objs = self.doc.Objects.FindByLayer(self.doc.Layers[idx]) or []
        return [o for o in objs if o.Attributes.GetUserString(USER_FIN)]

    def fins_signature(self):
        objs = self._fin_objects()
        if not objs:
            return "0|0"
        sigs = set((o.Attributes.GetUserString(USER_FIN) or "").split("|", 1)[-1] for o in objs)
        if len(sigs) != 1:
            return None
        sig = sigs.pop()
        try:
            n = int(sig.split("|")[1])
        except (IndexError, ValueError):
            return None
        return sig if n == len(objs) else None

    def replace_fins(self, fins, sig):
        Rhino = self.Rhino
        G = Rhino.Geometry
        for o in self._fin_objects():
            self.doc.Objects.Delete(o, True)
        if not fins:
            return 0
        layer = self._fin_layer()
        mat = self.material_index(FIN_MATERIAL)
        from System.Collections.Generic import List
        n = 0
        for fin in fins:
            pts = List[G.Point3d]()
            for (x, y, z) in fin["corners"]:
                pts.Add(G.Point3d(x * self.scale, y * self.scale, z * self.scale))
            brep = G.Brep.CreateFromBox(pts)
            if brep is None:
                continue
            a = Rhino.DocObjects.ObjectAttributes()
            a.LayerIndex = layer
            a.Name = fin["mark"]
            a.MaterialSource = Rhino.DocObjects.ObjectMaterialSource.MaterialFromObject
            a.MaterialIndex = mat
            a.SetUserString(USER_FIN, "%s|%s" % (fin["mark"], sig))
            a.SetUserString("Mark", fin["mark"])
            a.SetUserString(USER_MAPPED, "1")
            gid = self.doc.Objects.AddBrep(brep, a)
            if gid != System_Guid_Empty():
                self._box_mapping(gid)
                n += 1
        return n


def _enum_name(value):
    """'MaterialFromObject' for a .NET enum value (pythonnet 3 safe)."""
    try:
        return str(value.ToString())
    except Exception:
        return str(value).split(".")[-1]


def System_Guid_Empty():
    import System
    return System.Guid.Empty


def _rhino_scale(scale_in):
    if scale_in is not None:
        x = _as_number(scale_in)
        if x is not None and x > 0:
            return x, ""
        return 1.0, "Ignoring invalid 'scale' input %r; using 1." % (scale_in,)
    try:
        import Rhino
        f = float(Rhino.RhinoMath.UnitScale(Rhino.UnitSystem.Meters,
                                            Rhino.RhinoDoc.ActiveDoc.ModelUnitSystem))
        if f > 0 and not math.isinf(f) and not math.isnan(f):
            return f, ""
    except Exception:
        pass
    return 1.0, ""


def _doc_allowed(doc, repo):
    """Only the render copy in this repo's rhino/ folder may be edited (never P1)."""
    p = os.path.normcase(os.path.abspath(doc.Path or "")) if doc.Path else ""
    allowed_dir = os.path.normcase(os.path.join(repo, "rhino"))
    if not p:
        return False, "the active Rhino document is not saved, so it cannot be the Plurarch copy"
    if "langforda_fresh" in p:
        return False, "the active Rhino document is the P1 source (%s); it is never edited" % doc.Path
    if not p.startswith(allowed_dir + os.sep):
        return False, ("the active Rhino document is %s; the Langford builder only edits "
                       "files in %s" % (doc.Path, os.path.join(repo, "rhino")))
    return True, ""


_STICKY_PREFIX = "plurarch.langford_builder"


def _gh_scene_factory(sticky, el, scale):
    def factory():
        import Rhino
        doc = Rhino.RhinoDoc.ActiveDoc
        if doc is None:
            return None, "No active Rhino document."
        ok, why = _doc_allowed(doc, repo_root())
        if not ok:
            return None, "Not applied: " + why + "."
        key = "%s|scene|%s|%s" % (_STICKY_PREFIX, doc.RuntimeSerialNumber, round(scale, 9))
        scene = sticky.get(key)
        if scene is None:
            ids = [p["id"] for p in el["se_panels"]] + el["solid_now"]
            for lan in el["lanterns"]:
                ids.extend(lan["glazing_ids"])
            scene = RhinoScene(doc, scale, ids)
            sticky[key] = scene
        return scene, ""
    return factory


def _gh_feedback(warning_text, short):
    try:
        comp = ghenv.Component  # noqa: F821  (injected by Grasshopper)
    except Exception:
        return
    try:
        comp.Message = short
    except Exception:
        pass
    if warning_text:
        try:
            import Grasshopper
            level = Grasshopper.Kernel.GH_RuntimeMessageLevel.Warning
            for line in warning_text.splitlines():
                if line.strip():
                    comp.AddRuntimeMessage(level, line)
        except Exception:
            pass


def _keep_editor_open():
    """Grasshopper draws NO preview in Rhino while its editor window is closed, which would blank
    the projector. If someone closes it, bring it back minimised (the Timer keeps us running)."""
    try:
        import Grasshopper
        import System
        ed = Grasshopper.Instances.DocumentEditor
        if ed is not None and not ed.Visible:
            ed.Show()
            ed.WindowState = System.Windows.Forms.FormWindowState.Minimized
    except Exception:
        pass  # never let a UI nicety break the model


def _gh_run(path_in, tick_in, scale_in):
    import Rhino
    import scriptcontext as sc
    scale_val, scale_warn = _rhino_scale(scale_in)
    fpath = normalize_path(path_in)
    key = "%s|%s|%s" % (_STICKY_PREFIX, SCRIPT_VERSION, os.path.normcase(fpath))
    state = sc.sticky.get(key)
    if not isinstance(state, dict):
        state = {}
        sc.sticky[key] = state
    repo = repo_root()
    el, _spec, _err = load_inputs(state, repo)
    factory = _gh_scene_factory(sc.sticky, el, scale_val) if el else (lambda: (None, "no elements"))
    force = tick_in is True
    if force:   # a Button also rebuilds the object index (e.g. after editing the scene)
        doc = Rhino.RhinoDoc.ActiveDoc
        if doc is not None:
            sc.sticky.pop("%s|scene|%s|%s" % (_STICKY_PREFIX, doc.RuntimeSerialNumber,
                                              round(scale_val, 9)), None)
    out = step(state, fpath, factory, force=force, repo=repo)
    if state.get("report", {}).get("changed") or state.get("report", {}).get("fins") is not None:
        try:
            Rhino.RhinoDoc.ActiveDoc.Views.Redraw()
        except Exception:
            pass
    warning_text = "\n".join(w for w in (out["warning"], scale_warn) if w)
    _gh_feedback(warning_text, out["short"])
    return [], [], out["info"], warning_text


def _gh_entry():
    """Grasshopper entry point: wraps everything so the component never turns red."""
    g = globals()
    _keep_editor_open()
    try:
        return _gh_run(g.get("path"), g.get("tick"), g.get("scale"))
    except Exception as exc:
        msg = "Plurarch langford_builder: unexpected error, scene left as it is (%s: %s)" % (
            type(exc).__name__, exc)
        _gh_feedback(msg, "Langford: error (!)")
        return [], [], msg, msg


# ---------------------------------------------------------------------------
# 10. Self-test (plain CPython, no Rhino):  python langford_builder.py --selftest
# ---------------------------------------------------------------------------
class FakeScene(object):
    """In-memory stand-in for RhinoScene: objects with (source, index) materials."""

    def __init__(self, ids, subs=1):
        self.scene_id = "fake"
        self.label = "fake scene"
        self.objs = {}
        for rid in ids:
            self.objs[rid] = [{"src": 1, "idx": 7 + (rid % 3), "user": {}} for _ in range(subs)]
        self.fins = []
        self.fin_sig = "0|0"
        self.writes = 0
        self.mats = {"concrete": 101, "aluminium": 102, "fritted_glass": 103}

    def objects_for(self, rid):
        return self.objs.get(rid, [])

    def ensure_material(self, o, key):
        cur = (o["src"], o["idx"])
        orig = o["user"].get(USER_ORIG)
        if key is None:
            if not orig:
                return False
            s, i = (int(v) for v in orig.split("|"))
            if cur == (s, i):
                return False
            o["src"], o["idx"] = s, i
            self.writes += 1
            return True
        idx = self.mats[key]
        if cur == (1, idx):
            return False
        if not orig:
            o["user"][USER_ORIG] = "%d|%d" % cur
        o["src"], o["idx"] = 1, idx
        self.writes += 1
        return True

    def fins_signature(self):
        return self.fin_sig

    def replace_fins(self, fins, sig):
        self.fins = list(fins)
        self.fin_sig = sig
        return len(fins)


def _selftest():
    import ast
    import tempfile
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    results = []

    def check(name, cond, detail=""):
        results.append((bool(cond), name, detail))

    print("Plurarch langford_builder self-test  (v%s, Python %s)"
          % (SCRIPT_VERSION, sys.version.split()[0]))
    here = _this_file()
    with open(here, "r", encoding="utf-8") as fh:
        src = fh.read()
    try:
        ast.parse(src, feature_version=(3, 9))
        ok, why = True, ""
    except SyntaxError as exc:
        ok, why = False, str(exc)
    check("source parses with the Python 3.9 grammar", ok, why)
    check("source is plain ASCII", all(ord(ch) < 128 for ch in src))

    repo = repo_root()
    ep = os.path.join(repo, "config", "langford", "elements.json")
    el = load_elements(ep)
    with open(ep, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    spec = load_spec(os.path.join(repo, "config", "parameters.json"), raw)
    check("elements.json loads: %d SE lites, %d lanterns (%d glazing ids), %d louvres, %d anchors"
          % (len(el["se_panels"]), len(el["lanterns"]),
             sum(len(l["glazing_ids"]) for l in el["lanterns"]), len(el["solid_now"]),
             len(el["anchors"])),
          len(el["se_panels"]) == 142 and len(el["lanterns"]) == 12 and len(el["anchors"]) == 30)
    ranks = [p["rank"] for p in el["se_panels"]]
    check("SE ranks are 0..141 without gaps", ranks == list(range(len(ranks))))
    all_ids = [p["id"] for p in el["se_panels"]] + el["solid_now"] + [
        g for l in el["lanterns"] for g in l["glazing_ids"]]
    check("panel id sets are disjoint (%d ids)" % len(all_ids), len(all_ids) == len(set(all_ids)))
    check("spec: %s; defaults %s" % (spec["source"], describe(clamp_params(spec["defaults"], spec))),
          spec["defaults"]["se_glass_share"] == 100.0 and spec["defaults"]["skylights_open"] == 12.0
          and spec["defaults"]["fin_depth"] == 0.0)

    # -- plan rule ------------------------------------------------------------------
    d = clamp_params(spec["defaults"], spec)
    p0 = plan(d, el, spec)
    check("as-built plan: 142/142 lites, 12/12 lanterns glazed, no fins, only the 14 louvres solid",
          p0["n_se_glazed"] == 142 and p0["n_open"] == 12 and not p0["fins"]
          and sorted(p0["solid_ids"]) == sorted(el["solid_now"]))
    expect = {40: 57, 50: 71, 60: 85, 70: 99, 80: 114, 90: 128, 100: 142}
    got = {s: plan(dict(d, se_glass_share=s), el, spec)["n_se_glazed"] for s in expect}
    check("round(share/100 * 142): %s" % got, got == expect)
    p1 = plan(dict(d, se_glass_share=50, skylights_open=4, fin_depth=1.2), el, spec)
    first = [q["id"] for q in el["se_panels"][:71]]
    check("glazed lites are the first k by rank", p1["glazed_ids"][:71] == first)
    lan_open = set(g for l in el["lanterns"][:4] for g in l["glazing_ids"])
    lan_shut = set(g for l in el["lanterns"][4:] for g in l["glazing_ids"])
    check("lanterns: first 4 by index glazed, other 8 solid",
          lan_open <= set(p1["glazed_ids"]) and lan_shut <= set(p1["solid_ids"])
          and not (lan_open & set(p1["solid_ids"])))
    check("every element is either glazed or solid, never both",
          not (set(p1["glazed_ids"]) & set(p1["solid_ids"]))
          and len(set(p1["glazed_ids"]) | set(p1["solid_ids"])) == len(all_ids))

    # -- parity with design_mcp/langford_plan.py (the Revit side), when present -------------
    lp_path = os.path.join(repo, "design_mcp", "langford_plan.py")
    if os.path.isfile(lp_path):
        try:
            import importlib.util
            spec_mod = importlib.util.spec_from_file_location("_plx_langford_plan", lp_path)
            lp = importlib.util.module_from_spec(spec_mod)
            old_flag, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no .pyc in design_mcp/
            try:
                spec_mod.loader.exec_module(lp)
            finally:
                sys.dont_write_bytecode = old_flag
            mism = []
            for fin in spec["options"]:
                for share in range(40, 101, 10):
                    for depth in (0.0, 0.3, 0.6, 0.9, 1.2):
                        for sky in range(0, 13, 2):
                            prm = {"infill_finish": fin, "se_glass_share": float(share),
                                   "fin_depth": depth, "skylights_open": float(sky)}
                            a, b = plan(prm, el, spec), lp.plan(prm, raw)
                            if (a["glazed_ids"] != b["se_glazed"] + b["sky_glazed"]
                                    or sorted(a["solid_ids"]) != sorted(b["se_solid"] + b["sky_solid"] + el["solid_now"])
                                    or len(a["fins"]) != len(b["fins"])):
                                mism.append(prm)
            check("parity with design_mcp/langford_plan.py over all %d valid designs"
                  % (len(spec["options"]) * 7 * 5 * 7), not mism, str(mism[:2]))
        except Exception as exc:
            print("  (parity check skipped: %s: %s)" % (type(exc).__name__, exc))

    # -- fins and the frame ---------------------------------------------------------------
    a0 = el["anchors"][0]
    c = fin_corners(a0, 1.2, 0.2, el)
    t = math.radians(el["rotate_deg"])
    dx, dy = math.cos(t) * a0["dir"][0] - math.sin(t) * a0["dir"][1], \
        math.sin(t) * a0["dir"][0] + math.cos(t) * a0["dir"][1]

    def dist(p, q):
        return math.sqrt(sum((p[i] - q[i]) ** 2 for i in range(3)))
    edges = sorted(round(dist(c[i], c[j]), 6) for i, j in ((0, 1), (1, 2), (0, 4)))
    check("fin is 0.2 x depth x height (%s)" % edges,
          edges == sorted([1.2, 0.2, round(a0["height"], 6)]))
    mid_in = [(c[0][i] + c[3][i]) / 2 for i in range(3)]
    base_r = to_rhino(a0["base"], el)
    check("fin starts at the anchor base (Rhino frame)", dist(mid_in, base_r) < 1e-9)
    mid_out = [(c[1][i] + c[2][i]) / 2 for i in range(3)]
    v = [(mid_out[i] - mid_in[i]) / 1.2 for i in range(2)]
    check("fin points along the rotated dir (%.3f, %.3f)" % (dx, dy),
          abs(v[0] - dx) < 1e-9 and abs(v[1] - dy) < 1e-9)
    az = (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0
    check("fin direction is SE in true north (azimuth %.2f deg, facade 140.65)" % az,
          abs(az - 140.65) < 0.01)
    area2 = sum(c[i][0] * c[(i + 1) % 4][1] - c[(i + 1) % 4][0] * c[i][1] for i in range(4))
    check("fin corners in Brep.CreateFromBox order (bottom CCW, top above)",
          area2 > 0 and all(abs(c[i][0] - c[i + 4][0]) < 1e-12 for i in range(4)))
    check("fins: 30 at 1.2 m, none at 0", len(p1["fins"]) == 30 and not p0["fins"])

    # -- validation -----------------------------------------------------------------------
    pv, e, n = validate_parameters({"infill_finish": "Aluminium ", "se_glass_share": "60",
                                    "fin_depth": 0.9, "skylights_open": 6}, spec)
    check("valid set accepted (case, numeric strings)", pv is not None and pv["infill_finish"] == "aluminium"
          and pv["se_glass_share"] == 60.0 and pv["fin_depth"] == 0.9)
    pv, e, n = validate_parameters({"infill_finish": "concrete", "se_glass_share": 55,
                                    "fin_depth": 0.5, "skylights_open": 5}, spec)
    check("off-step values snapped (55->60, 0.5->0.6, 5->6)", pv is not None and
          (pv["se_glass_share"], pv["fin_depth"], pv["skylights_open"]) == (60.0, 0.6, 6.0), str(n))
    bad = [{"infill_finish": "brick"}, {"se_glass_share": 30}, {"se_glass_share": 110},
           {"fin_depth": 1.5}, {"fin_depth": -0.3}, {"skylights_open": 13}, {"skylights_open": None},
           {"se_glass_share": True}, {"fin_depth": float("nan")}]
    rejected = 0
    for b in bad:
        r = dict(d)
        r.update(b)
        if validate_parameters(r, spec)[0] is None:
            rejected += 1
    check("bad values rejected (%d/%d)" % (rejected, len(bad)), rejected == len(bad))
    check("a pavilion file (no Langford keys) is rejected",
          validate_parameters({"facade_material": "timber", "window_ratio": 50}, spec)[0] is None)
    pv, e, n = validate_parameters({"fin_depth": 0.6}, spec, fallback=dict(d, infill_finish="aluminium"))
    check("missing keys filled from the last good set", pv is not None and pv["infill_finish"] == "aluminium"
          and len(n) == 3)

    # -- apply: idempotent, restores originals exactly -----------------------------------------
    scene = FakeScene(all_ids)
    orig = {rid: [(o["src"], o["idx"]) for o in scene.objs[rid]] for rid in all_ids}
    r1 = apply_plan(scene, p1)
    w1 = scene.writes
    r2 = apply_plan(scene, p1)
    check("apply: first run writes %d objects, second run writes 0 (idempotent)" % w1,
          w1 == r1["changed"] == len(p1["solid_ids"]) and r2["changed"] == 0 and scene.writes == w1)
    check("apply: 30 fins placed, then kept", r1["fins"] == 30 and r2["fins"] is None)
    p2 = plan(dict(d, infill_finish="fritted_glass", se_glass_share=100, skylights_open=12), el, spec)
    apply_plan(scene, p2)
    back = all((scene.objs[rid][0]["src"], scene.objs[rid][0]["idx"]) == orig[rid][0]
               for rid in [q["id"] for q in el["se_panels"]] + [g for l in el["lanterns"] for g in l["glazing_ids"]])
    check("apply: glazed again -> exact original material restored", back)
    check("apply: louvres carry the finish (fritted_glass)",
          all(scene.objs[rid][0]["idx"] == 103 for rid in el["solid_now"]))
    check("apply: fins removed when depth is 0", scene.fins == [] and scene.fin_sig == "0|0")
    p3 = plan(dict(d, infill_finish="aluminium", se_glass_share=40), el, spec)
    r3 = apply_plan(scene, p3)
    check("apply: finish change rewrites only the solid objects (%d)" % r3["changed"],
          r3["changed"] == len(p3["solid_ids"]))
    sc2 = FakeScene(all_ids[:-5])
    r4 = apply_plan(sc2, p1)
    check("apply: missing scene objects are reported, not fatal", len(r4["missing"]) == 5)

    # -- watcher ----------------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        fp = os.path.join(tmp, "parameters.json")
        st, clock, mt = {}, [1000.0], [1700000000 * 10 ** 9]
        scene = FakeScene(all_ids)

        def factory():
            return scene, ""

        def tick(**kw):
            clock[0] += 0.5
            return step(st, kw.pop("path", fp), kw.pop("factory", factory), now=clock[0], repo=repo, **kw)

        def write(obj):
            text = obj if isinstance(obj, str) else json.dumps({"parameters": obj, "verdict": "ACCEPTED"})
            with open(fp + ".tmp", "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(fp + ".tmp", fp)
            mt[0] += 10 ** 9
            os.utime(fp, ns=(mt[0], mt[0]))

        r = tick()
        check("watch: no file -> as-built defaults applied + warning",
              r["source"] == "default" and "not found" in r["warning"] and st["stats"]["applies"] == 1)
        write({"facade_material": "timber", "window_ratio": 50, "roof_angle": 25, "canopy_depth": 1.5})
        r = tick()
        check("watch: the old pavilion file -> warning, defaults kept",
              "no Langford parameters" in r["warning"] and r["params"]["se_glass_share"] == 100.0)
        write({"infill_finish": "aluminium", "se_glass_share": 50, "fin_depth": 1.2, "skylights_open": 4})
        r = tick()
        applies = st["stats"]["applies"]
        check("watch: valid file -> applied, warning cleared",
              r["warning"] == "" and r["params"]["fin_depth"] == 1.2 and len(scene.fins) == 30)
        for _ in range(5):
            r = tick()
        check("watch: idle ticks -> no re-read, no re-apply", st["stats"]["applies"] == applies)
        write('{"parameters": {"infill_fin')
        r = tick()
        check("watch: half-written file -> warning, last good kept",
              "JSON" in r["warning"] and r["params"]["infill_finish"] == "aluminium"
              and st["stats"]["applies"] == applies)
        write({"infill_finish": "aluminium", "se_glass_share": 50, "fin_depth": 1.2, "skylights_open": 40})
        r = tick()
        check("watch: out of range -> warning, last good kept", "outside" in r["warning"]
              and r["params"]["skylights_open"] == 4.0)
        r = tick(force=True)
        check("watch: force re-applies", st["stats"]["applies"] == applies + 1)
        r = tick(factory=lambda: (None, "Not applied: wrong document."))
        check("watch: wrong document -> warning, nothing written", "wrong document" in r["warning"])
        info_example = r["info"]

    print("")
    for ok, name, detail in results:
        print("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                             ("   <- " + detail) if (detail and not ok) else ""))
    print("")
    print("Example 'info':")
    print("  " + info_example.replace("\n", "\n  "))
    passed = sum(1 for ok, _n, _d in results if ok)
    print("")
    print("RESULT: %d/%d checks passed" % (passed, len(results)))
    return 0 if passed == len(results) else 1


# ---------------------------------------------------------------------------
# 11. Entry point
# ---------------------------------------------------------------------------
if "ghenv" in globals():
    geometry, colors, info, warning = _gh_entry()
elif __name__ == "__main__":
    if "--selftest" in getattr(sys, "argv", [])[1:]:
        sys.exit(_selftest())
    print("Run by the Grasshopper loader (see grasshopper/SETUP.md), or: "
          "python langford_builder.py --selftest")
