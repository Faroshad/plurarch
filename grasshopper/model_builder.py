#! python3
# -*- coding: utf-8 -*-
"""
PLURARCH - live pavilion model for Grasshopper      (grasshopper/model_builder.py)
=================================================================================

Runs inside a Rhino 8 Grasshopper "Python 3 Script" component (CPython 3.9 +
RhinoCommon), either through the short loader in grasshopper/SETUP.md (which execs
this file and picks up edits on the next Timer tick) or pasted in whole. It watches
<state_dir>/parameters.json, written by the design server, and rebuilds the pavilion.

GEOMETRY SOURCE (v2): the detailed model comes from the SAME JavaScript generator the
phones use (site/js/model/pavilion.js, contract docs/MODEL.md). When the parameters
change, this script runs   node tools/pavilion_cli.mjs   (params as JSON on stdin,
~5 s timeout, no console window) and meshes the returned parts: one flat-shaded Mesh
per material, glass semi-transparent. node is found via config/local.json "node",
else C:\\Program Files\\nodejs\\node.exe, else PATH. If node is missing, fails or
times out, the simplified built-in Python generator below is shown instead, with a
warning, so the projector never goes blank (node is retried every minute).

The built-in (fallback) generator follows the same design rules, in which every
parameter makes a big change:

  facade_material  timber | concrete | glass -> colour AND shape of the skin
                     timber   : slender vertical fins, off-white wall, glazing band
                     concrete : wide solid panels with narrow gaps, punched windows
                     glass    : thin mullions, large blue-green panes, dark vision band
  window_ratio     20..60 %  -> glazed share of every facade (exact, bay by bay)
  roof_angle       0..35 deg -> mono-pitch roof. Low eave 4.5 m at the back, rising
                                toward the entrance (front eave 4.5 m at 0 deg, 11.5 m
                                at 35 deg). The low (back) overhang grows 0.8 -> 3.0 m.
  canopy_depth     0..3 m    -> 8 m wide entrance canopy at 3.2 m; posts from 1.5 m

Footprint 18 m (x) by 10 m (y); the entrance facade faces -y. Units are metres.

COMPONENT INPUTS  (zoom in on the component and use the +/- icons to add inputs;
                   right-click an input to rename it and to set access / type hint)
  path   Item Access, Type hint: str
         Absolute path of parameters.json. A folder also works (parameters.json in it).
  tick   Item Access, Type hint: No Type Hint
         Not needed for the Timer: the Timer's wire targets the whole component and
         re-runs it; `tick` may stay unconnected. Its value is unused, except that a
         Button wired here forces a re-read and rebuild while pressed.
  scale  Item Access, Type hint: float   (OPTIONAL)
         Model units per metre. Leave it unconnected to follow the Rhino document
         units automatically (m -> 1, cm -> 100, mm -> 1000).

COMPONENT OUTPUTS
  geometry  list of Mesh, one flat-shaded mesh per colour  -> Custom Preview "G"
  colors    list of System.Drawing.Color, same length/order -> Custom Preview "M"
  info      text: current values, file time, build stats   -> Panel
  warning   text, empty when everything is fine            -> Panel
            (warnings also appear as an orange balloon on the component)

BEHAVIOUR
  * The file is re-read only when its modification time (or size) changes, so Timer
    ticks are nearly free. Geometry is rebuilt only when the parameters change.
    Parsed parameters and geometry are cached in scriptcontext.sticky.
  * Missing, locked, half-written or invalid file, or out-of-range values: the LAST
    GOOD design stays on screen and `warning` explains why. The script never raises.
    Before the first good file it shows the default design (timber, 40 %, 15, 1.5 m).
  * Values are also clamped and snapped to the slider steps (second line of defence).

TESTING OUTSIDE RHINO (plain CPython 3.9+, no Rhino needed):
    python grasshopper/model_builder.py --selftest
"""

import json
import math
import os
import shutil
import subprocess
import sys
import time

SCRIPT_VERSION = "2.0.0"
DEG = chr(176)   # degree sign (kept out of the source so the file stays plain ASCII)

# True: one Brep per element (Brep.CreateFromBox). False (default): one joined,
# flat-shaded Mesh per colour, which is faster to preview on every Timer tick.
OUTPUT_BREPS = False

# ---------------------------------------------------------------------------
# 1. Parameter spec - a mirror of config/parameters.json (the self-test checks
#    that these ranges and defaults still match the config file)
# ---------------------------------------------------------------------------
PARAM_KEYS = ("facade_material", "window_ratio", "roof_angle", "canopy_depth")
MATERIAL_OPTIONS = ("timber", "concrete", "glass")
NUMERIC_SPEC = (  # key, min, max, step
    ("window_ratio", 20.0, 60.0, 5.0),
    ("roof_angle", 0.0, 35.0, 5.0),
    ("canopy_depth", 0.0, 3.0, 0.5),
)
DEFAULTS = {"facade_material": "timber", "window_ratio": 40.0,
            "roof_angle": 15.0, "canopy_depth": 1.5}

# ---------------------------------------------------------------------------
# 2. Dimensions (metres) and colours
# ---------------------------------------------------------------------------
X0, X1 = -9.0, 9.0          # 18 m along x
Y0, Y1 = -5.0, 5.0          # 10 m along y; Y0 is the entrance (front) facade
FLOOR_TOP = 0.30            # floor slab 0.3 m thick, sitting on the ground (z = 0)
SLAB_MARGIN = 0.6           # slab edge beyond the building line
GROUND = (-12.5, 12.5, -10.5, 9.5, -0.2, 0.0)   # x0, x1, y0, y1, z0, z1

EAVE_LOW = 4.5              # roof underside at the low (back) wall line
ROOF_T = 0.35               # roof slab thickness (perpendicular to the slope)
OH_SIDE = 0.8               # roof overhang at the two sides
OH_HIGH = 0.6               # roof overhang at the high (front) eave
OH_LOW_MIN, OH_LOW_MAX = 0.8, 3.0   # low (back) overhang at 0 and 35 degrees
EMBED = 0.05                # walls/fins tuck this far into the roof slab (no gaps)

# Facade layers, as distance d from the building line (positive = outward)
WALL_IN, WALL_OUT = -0.25, -0.05    # neutral backing wall, closes the building
PANE_OUT = 0.0                      # glazing / spandrel panes: WALL_OUT .. PANE_OUT
TRANSOM_OUT, TRANSOM_H = 0.12, 0.06 # glass facade transoms at sill and head
SILL_SHARE = 0.35           # share of the solid wall height that sits below the glazing
MIN_TOP_SOLID = 0.2         # keep at least this much solid wall above a window
PIER_MIN = 0.15             # concrete: minimum pier beside a punched window
MIN_DIM = 1e-3

MATERIALS = {
    # pitch: target bay width; joint: fin width / panel gap / mullion width;
    # out: how far the skin projects beyond the building line
    "timber":   {"style": "fins",    "pitch": 1.2,  "joint": 0.12, "out": 0.40, "color": "timber"},
    "concrete": {"style": "panels",  "pitch": 2.25, "joint": 0.12, "out": 0.25, "color": "concrete"},
    "glass":    {"style": "curtain", "pitch": 1.8,  "joint": 0.06, "out": 0.25, "color": "glass"},
}

CANOPY_Z, CANOPY_T, CANOPY_W = 3.2, 0.2, 8.0    # underside height, thickness, width
POST_W, POST_INSET, POST_MIN_DEPTH = 0.15, 0.25, 1.5

PALETTE = {  # RGB (optional 4th value = alpha, if Custom Preview honours it)
    "ground":   (206, 206, 201),
    "slab":     (128, 126, 121),
    "wall":     (237, 234, 227),   # neutral off-white background panels
    "glazing":  (60, 90, 110),     # dark blue-gray glass
    "timber":   (176, 122, 74),
    "concrete": (160, 160, 158),
    "glass":    (110, 170, 190),   # cool blue-green panes
    "mullion":  (222, 228, 231),   # light aluminium (glass facade frame)
    "roof":     (74, 78, 84),      # charcoal roof slab
    "canopy":   (74, 78, 84),      # canopy slab and posts
}
DRAW_ORDER = ("ground", "slab", "wall", "glazing", "timber", "concrete", "glass",
              "mullion", "roof", "canopy")

# The four facades: name, origin (x, y), tangent (along the facade), outward
# normal, length. A facade point is origin + s * tangent + d * normal.
FACADES = (
    ("front", (X0, Y0), (1.0, 0.0), (0.0, -1.0), X1 - X0),
    ("right", (X1, Y0), (0.0, 1.0), (1.0, 0.0), Y1 - Y0),
    ("back",  (X1, Y1), (-1.0, 0.0), (0.0, 1.0), X1 - X0),
    ("left",  (X0, Y1), (0.0, -1.0), (-1.0, 0.0), Y1 - Y0),
)

# Part of the rebuild key: editing a colour or dimension above rebuilds the model
# even though sticky still holds geometry from before the edit.
_LAYOUT_SIG = repr((PALETTE, MATERIALS, FACADES, FLOOR_TOP, SLAB_MARGIN, GROUND, EAVE_LOW,
                    ROOF_T, OH_SIDE, OH_HIGH, OH_LOW_MIN, OH_LOW_MAX, EMBED, WALL_IN,
                    WALL_OUT, PANE_OUT, TRANSOM_OUT, TRANSOM_H, SILL_SHARE, MIN_TOP_SOLID,
                    PIER_MIN, CANOPY_Z, CANOPY_T, CANOPY_W, POST_W, POST_INSET,
                    POST_MIN_DEPTH))

# ---------------------------------------------------------------------------
# 3. Reading and validating parameters (pure Python, no Rhino)
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
    """Clamp to [lo, hi] and snap to the slider grid lo + k * inc (ties round up)."""
    x = min(max(float(value), lo), hi)
    x = lo + math.floor((x - lo) / inc + 0.5) * inc
    return round(min(max(x, lo), hi), 6)


def _fmt(x):
    """40.0 -> '40', 1.5 -> '1.5'."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    if abs(x - round(x)) < 1e-9:
        return "%d" % int(round(x))
    return ("%.3f" % x).rstrip("0").rstrip(".")


def describe(params):
    """'timber, 40 %, 15 deg, 1.5 m'"""
    return "%s, %s %%, %s deg, %s m" % (
        params["facade_material"], _fmt(params["window_ratio"]),
        _fmt(params["roof_angle"]), _fmt(params["canopy_depth"]))


def validate_parameters(raw, fallback=None):
    """Check a parameters mapping.

    Returns (params, errors, notes). Non-empty `errors` means: reject the whole set
    (keep the last good design). `notes` are repaired soft problems (a missing key
    filled from `fallback`, a value snapped to its slider step).
    """
    base = clamp_params(fallback if isinstance(fallback, dict) else DEFAULTS)
    errors, notes = [], []
    if not isinstance(raw, dict):
        return None, ["'parameters' is not a JSON object"], notes
    params = {}

    if "facade_material" not in raw:
        params["facade_material"] = base["facade_material"]
        notes.append("facade_material missing, kept %s" % base["facade_material"])
    else:
        value = raw["facade_material"]
        mat = value.strip().lower() if isinstance(value, str) else None
        if mat in MATERIAL_OPTIONS:
            params["facade_material"] = mat
        else:
            errors.append("facade_material=%r is not one of %s"
                          % (value, "/".join(MATERIAL_OPTIONS)))

    for key, lo, hi, inc in NUMERIC_SPEC:
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
        snapped = snap(x, lo, hi, inc)
        if abs(snapped - x) > 1e-6:
            notes.append("%s=%s snapped to %s" % (key, _fmt(x), _fmt(snapped)))
        params[key] = snapped

    if errors:
        return None, errors, notes
    return params, errors, notes


def clamp_params(params):
    """Always returns a complete, in-range, snapped parameter set (defaults fill gaps)."""
    src = params if isinstance(params, dict) else {}
    out = {}
    mat = src.get("facade_material")
    mat = mat.strip().lower() if isinstance(mat, str) else ""
    out["facade_material"] = mat if mat in MATERIAL_OPTIONS else DEFAULTS["facade_material"]
    for key, lo, hi, inc in NUMERIC_SPEC:
        x = _as_number(src.get(key))
        out[key] = snap(DEFAULTS[key] if x is None else x, lo, hi, inc)
    return out


def _meta_text(value):
    if value is None or isinstance(value, (dict, list)):
        return None
    return str(value)[:80]


def read_parameters_file(file_path, fallback=None):
    """Read + validate parameters.json. Never raises.

    Returns {"status", "params", "message", "notes", "meta"} where status is one of
    ok | missing | unreadable | invalid_json | invalid.
    """
    res = {"status": "ok", "params": None, "message": "", "notes": [], "meta": {}}
    try:
        with open(file_path, "r", encoding="utf-8-sig") as fh:   # -sig: tolerate a BOM
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
        raw = data                     # tolerate a bare parameters object
    if raw is None:
        res["status"], res["message"] = "invalid", "no 'parameters' object in the file"
        return res
    params, errors, notes = validate_parameters(raw, fallback)
    res["notes"] = notes
    res["meta"] = {k: _meta_text(data.get(k)) for k in ("updated_at", "round_id", "verdict")}
    if errors:
        res["status"], res["message"] = "invalid", "; ".join(errors)
        return res
    res["params"] = params
    return res


# ---------------------------------------------------------------------------
# 4. Geometry layout (pure Python). Every element is a "prism": a quad footprint
#    (counter-clockwise from above) with a bottom and top z per corner, i.e. the
#    8 corners of a (possibly sloped) box in Brep.CreateFromBox order.
#    A part is (colour_key, [8 x (x, y, z)], tag).
# ---------------------------------------------------------------------------
HEX_FACES = ((0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),   # outward-facing quads
             (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7))


def _area2(foot):
    """Twice the signed area of a 2D polygon (positive = counter-clockwise)."""
    a = 0.0
    for i in range(len(foot)):
        x0, y0 = foot[i]
        x1, y1 = foot[(i + 1) % len(foot)]
        a += x0 * y1 - x1 * y0
    return a


def _prism(parts, key, foot, zb, zt, tag):
    """Append a prism; zb / zt are numbers or callables f(x, y) -> z. Skips slivers."""
    area = _area2(foot)
    if abs(area) < MIN_DIM * MIN_DIM:
        return False
    if area < 0:
        foot = [foot[0], foot[3], foot[2], foot[1]]
    bottom, top = [], []
    for (x, y) in foot:
        b = zb(x, y) if callable(zb) else zb
        t = zt(x, y) if callable(zt) else zt
        if t - b < MIN_DIM:
            return False
        bottom.append((x, y, b))
        top.append((x, y, t))
    parts.append((key, bottom + top, tag))
    return True


def _rect_xy(x0, x1, y0, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


class RoofGeometry(object):
    """Mono-pitch roof: low eave (EAVE_LOW) at the back (+y), rising toward the entrance."""

    def __init__(self, angle_deg):
        self.angle = min(max(float(angle_deg), 0.0), 35.0)
        a = math.radians(self.angle)
        self.slope = math.tan(a)
        self.thickness_v = ROOF_T / math.cos(a)          # vertical slab thickness
        self.overhang_low = OH_LOW_MIN + (OH_LOW_MAX - OH_LOW_MIN) * self.angle / 35.0
        self.x0, self.x1 = X0 - OH_SIDE, X1 + OH_SIDE
        self.y0, self.y1 = Y0 - OH_HIGH, Y1 + self.overhang_low

    def under(self, y):
        """Roof underside height at y (valid over the whole slab, overhangs included)."""
        return EAVE_LOW + (Y1 - y) * self.slope

    def top(self, y):
        return self.under(y) + self.thickness_v

    def wall_top(self, x, y):
        return self.under(y) + EMBED

    def front_eave(self):
        return self.under(Y0)

    def lowest_edge(self):
        return self.under(self.y1)


def bay_layout(length, pitch):
    """Split a facade into n equal bays close to `pitch`. Returns (n, bay_width)."""
    n = max(1, int(round(length / pitch)))
    return n, length / n


def window_fractions(material, ratio, bay):
    """Glazing per bay as (width_fraction, height_fraction) of the bay.

    width_fraction * height_fraction == ratio (0..1), so the glazed share of the
    facade equals window_ratio. Timber / glass: glass spans the whole clear width
    between fins or mullions and its height grows. Concrete: punched windows that
    grow in both directions.
    """
    spec = MATERIALS[material]
    if spec["style"] == "panels":
        wf = min(math.sqrt(ratio), (bay - spec["joint"] - 2.0 * PIER_MIN) / bay)
    else:
        wf = (bay - spec["joint"]) / bay
    wf = max(wf, 1e-3)
    return wf, min(ratio / wf, 0.95)


def _build_facade(parts, roof, facade, material, ratio):
    """Backing wall + bays (glazing, fins / panels / curtain wall) for one facade."""
    name, (ox, oy), (tx, ty), (nx, ny), length = facade
    spec = MATERIALS[material]
    style, j, out, prim = spec["style"], spec["joint"], spec["out"], spec["color"]

    def pt(s, d):
        return (ox + s * tx + d * nx, oy + s * ty + d * ny)

    def rect(s0, s1, d0, d1):
        return [pt(s0, d0), pt(s1, d0), pt(s1, d1), pt(s0, d1)]

    def clear_h(s):                      # wall height above the floor at the building line
        return roof.under(pt(s, 0.0)[1]) - FLOOR_TOP

    top = roof.wall_top
    zb = FLOOR_TOP
    # backing wall: front/back run to the side walls' outer face, the side walls fit
    # between them, so no end face lies in the plane of the neighbouring skin
    inset = -WALL_OUT if name in ("front", "back") else -WALL_IN
    _prism(parts, "wall", rect(inset, length - inset, WALL_IN, WALL_OUT), zb, top, "wall:" + name)

    n, bay = bay_layout(length, spec["pitch"])
    wf, hf = window_fractions(material, ratio, bay)
    # one sill line around the whole building; heads follow the local wall height
    sill = zb + SILL_SHARE * (1.0 - hf) * (EAVE_LOW - FLOOR_TOP)
    w_max = (bay - j - 2.0 * PIER_MIN) if style == "panels" else (bay - j)

    for i in range(n):
        s0, s1 = i * bay, (i + 1) * bay
        sm = 0.5 * (s0 + s1)
        h_mid = clear_h(sm)
        h_min = min(clear_h(s0), clear_h(s1))
        target = ratio * bay * h_mid     # glazed area this bay must get (exact share)
        h = min(hf * h_mid, zb + h_min - MIN_TOP_SOLID - sill)
        if h <= MIN_DIM:
            continue
        w = min(target / h, w_max)       # widen if the height had to be capped
        head = sill + h
        ws0, ws1 = sm - 0.5 * w, sm + 0.5 * w

        if style == "fins":              # timber: glazing band + slender fins
            _prism(parts, "glazing", rect(ws0, ws1, WALL_OUT, PANE_OUT), sill, head, "glazing:" + name)
            if i < n - 1:
                _prism(parts, prim, rect(s1 - j / 2, s1 + j / 2, WALL_OUT, out), zb, top, "fin:" + name)

        elif style == "panels":          # concrete: wide panels, narrow gaps, punched windows
            a, b = s0 + j / 2, s1 - j / 2
            _prism(parts, prim, rect(a, b, WALL_OUT, out), zb, sill, "panel:" + name)
            _prism(parts, prim, rect(a, b, WALL_OUT, out), head, top, "panel:" + name + ":top")
            _prism(parts, prim, rect(a, ws0, WALL_OUT, out), sill, head, "panel:" + name)
            _prism(parts, prim, rect(ws1, b, WALL_OUT, out), sill, head, "panel:" + name)
            _prism(parts, "glazing", rect(ws0, ws1, WALL_OUT, PANE_OUT), sill, head, "glazing:" + name)

        else:                            # glass: curtain wall, spandrels + vision band
            a, b = s0 + j / 2, s1 - j / 2
            _prism(parts, prim, rect(a, b, WALL_OUT, PANE_OUT), zb, sill, "spandrel:" + name)
            _prism(parts, "glazing", rect(ws0, ws1, WALL_OUT, PANE_OUT), sill, head, "glazing:" + name)
            _prism(parts, prim, rect(a, b, WALL_OUT, PANE_OUT), head, top, "spandrel:" + name + ":top")
            for z in (sill, head):
                _prism(parts, "mullion", rect(a, b, PANE_OUT, TRANSOM_OUT),
                       z - TRANSOM_H / 2, z + TRANSOM_H / 2, "transom:" + name)
            if i < n - 1:
                _prism(parts, "mullion", rect(s1 - j / 2, s1 + j / 2, WALL_OUT, out), zb, top, "mullion:" + name)


def _build_corners(parts, roof, material):
    """A corner post at each building corner, in the skin's material."""
    spec = MATERIALS[material]
    key = "mullion" if spec["style"] == "curtain" else spec["color"]
    inner, outer = max(spec["joint"] / 2, 0.06), spec["out"]
    for cx, sx in ((X0, -1.0), (X1, 1.0)):
        for cy, sy in ((Y0, -1.0), (Y1, 1.0)):
            xa, xb = sorted((cx - sx * inner, cx + sx * outer))
            ya, yb = sorted((cy - sy * inner, cy + sy * outer))
            _prism(parts, key, _rect_xy(xa, xb, ya, yb), FLOOR_TOP, roof.wall_top, "post:corner")


def canopy_extent(depth, material):
    """Entrance canopy box and posts, or None when depth is 0.

    The canopy projects `depth` metres beyond the outer face of the facade skin.
    """
    depth = snap(depth if _as_number(depth) is not None else 0.0, 0.0, 3.0, 0.5)
    if depth < 1e-6:
        return None
    out = MATERIALS[material]["out"] if material in MATERIALS else 0.4
    x0, x1 = -CANOPY_W / 2, CANOPY_W / 2
    y_in, y_out = Y0 - WALL_OUT, Y0 - out - depth
    posts = []
    if depth >= POST_MIN_DEPTH - 1e-9:
        for cx in (x0 + POST_INSET, x1 - POST_INSET):
            cy = y_out + POST_INSET
            posts.append((cx - POST_W / 2, cx + POST_W / 2, cy - POST_W / 2, cy + POST_W / 2))
    return {"x0": x0, "x1": x1, "y_in": y_in, "y_out": y_out, "z0": CANOPY_Z,
            "z1": CANOPY_Z + CANOPY_T, "posts": posts, "projection": depth}


def build_parts(params):
    """The whole pavilion as a list of parts (pure Python)."""
    p = clamp_params(params)                 # second line of defence
    material = p["facade_material"]
    ratio = p["window_ratio"] / 100.0
    roof = RoofGeometry(p["roof_angle"])
    parts = []

    # ground plinth and floor slab
    gx0, gx1, gy0, gy1, gz0, gz1 = GROUND
    _prism(parts, "ground", _rect_xy(gx0, gx1, gy0, gy1), gz0, gz1, "ground")
    m = SLAB_MARGIN
    _prism(parts, "slab", _rect_xy(X0 - m, X1 + m, Y0 - m, Y1 + m), 0.0, FLOOR_TOP, "slab")

    # four facades (walls, glazing, skin) and corner posts
    for facade in FACADES:
        _build_facade(parts, roof, facade, material, ratio)
    _build_corners(parts, roof, material)

    # sloped roof slab with overhangs
    _prism(parts, "roof", _rect_xy(roof.x0, roof.x1, roof.y0, roof.y1),
           lambda x, y: roof.under(y), lambda x, y: roof.top(y), "roof")

    # entrance canopy (+ two slim posts when deep)
    can = canopy_extent(p["canopy_depth"], material)
    if can:
        _prism(parts, "canopy", _rect_xy(can["x0"], can["x1"], can["y_out"], can["y_in"]),
               can["z0"], can["z1"], "canopy")
        for (a, b, c, d) in can["posts"]:
            _prism(parts, "canopy", _rect_xy(a, b, c, d), 0.0, can["z0"] + EMBED, "post:canopy")
    return parts


# ---------------------------------------------------------------------------
# 4b. The detailed generator: node tools/pavilion_cli.mjs (site/js/model/pavilion.js)
# ---------------------------------------------------------------------------
NODE_TIMEOUT = 5.0           # seconds; the projector must never hang for long
NODE_RETRY_SECONDS = 60.0    # after a fallback to the built-in model, try node again
NODE_DEFAULT = r"C:\Program Files\nodejs\node.exe"
REPO_DEFAULT = r"E:\Academic\PhD\Fall 2026\AI Workshop\PlurARCH"
GENERATOR_FILES = (("site", "js", "model", "pavilion.js"), ("tools", "pavilion_cli.mjs"))


def repo_root():
    """The repo folder: from the loader's _MB (path of this file), else __file__, else
    the known location on the lecture laptop."""
    g = globals()
    candidates = []
    for value in (g.get("_MB"), g.get("__file__")):
        if isinstance(value, str) and value.strip():
            candidates.append(os.path.dirname(os.path.dirname(os.path.abspath(value.strip()))))
    candidates.append(REPO_DEFAULT)
    for c in candidates:
        if os.path.isfile(os.path.join(c, *GENERATOR_FILES[1])):
            return c
    return candidates[0]


def find_node(repo=None):
    """node executable: config/local.json "node", else the default install path, else PATH."""
    forced = globals().get("_PLURARCH_NODE")          # set by the self-test only
    if isinstance(forced, str):
        return forced
    repo = repo or repo_root()
    try:
        with open(os.path.join(repo, "config", "local.json"), "r", encoding="utf-8-sig") as fh:
            value = json.load(fh).get("node")
        if isinstance(value, str) and os.path.isfile(value):
            return value
    except (OSError, ValueError, AttributeError):
        pass
    if os.path.isfile(NODE_DEFAULT):
        return NODE_DEFAULT
    return shutil.which("node")


def generator_signature(repo=None):
    """(mtime_ns, size) of pavilion.js and the CLI: part of the rebuild key, so an edited
    or upgraded generator rebuilds the model on the next tick."""
    repo = repo or repo_root()
    sig = []
    paths = [os.path.join(repo, *rel) for rel in GENERATOR_FILES]
    mb = globals().get("_MB")                       # this script itself (via the loader)
    paths.append(mb if isinstance(mb, str) else os.path.join(repo, "grasshopper", "model_builder.py"))
    for path in paths:
        try:
            st = os.stat(path)
            sig.append((st.st_mtime_ns, st.st_size))
        except (OSError, ValueError, TypeError):
            sig.append(None)
    return tuple(sig)


def run_node_model(params, repo=None, node=None, timeout=NODE_TIMEOUT):
    """Run the JavaScript generator. Returns the Model dict; raises RuntimeError with a
    short, clear reason on any problem (missing node, timeout, exit code, bad output)."""
    repo = repo or repo_root()
    node = node or find_node(repo)
    if not node:
        raise RuntimeError("node not found (config/local.json 'node', %s, PATH)" % NODE_DEFAULT)
    cli = os.path.join(repo, *GENERATOR_FILES[1])
    if not os.path.isfile(cli):
        raise RuntimeError("generator CLI missing: %s" % cli)
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        res = subprocess.run([node, cli], input=json.dumps(params).encode("utf-8"),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             timeout=timeout, cwd=repo, **kwargs)
    except subprocess.TimeoutExpired:
        raise RuntimeError("node timed out after %g s" % timeout)
    except (OSError, ValueError) as exc:
        raise RuntimeError("cannot start node %r (%s)" % (node, exc))
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError("node exited with code %d (%s)" % (res.returncode, err[-1][:200] if err else "no message"))
    try:
        model = json.loads(res.stdout.decode("utf-8"))
    except ValueError as exc:
        raise RuntimeError("node output is not JSON (%s)" % exc)
    if not (isinstance(model, dict) and isinstance(model.get("parts"), list) and model["parts"]
            and isinstance(model.get("materials"), dict)):
        raise RuntimeError("node output is not a pavilion model")
    return model


# ---------------------------------------------------------------------------
# 5. File watcher + cache (pure Python). `state` is a dict that survives between
#    runs (scriptcontext.sticky in Grasshopper, a plain dict in the self-test).
# ---------------------------------------------------------------------------
RETRY_SECONDS = 2.0          # re-try a locked / half-written file after this long
BUILD_RETRY_SECONDS = 5.0    # re-try a failed geometry build after this long
_TRANSIENT = ("unreadable", "invalid_json")
_STATUS_TEXT = {"missing": "File not found", "unreadable": "Cannot read the file",
                "invalid_json": "File is not valid JSON", "invalid": "Invalid parameters"}


def normalize_path(value):
    """Tidy a path from a Panel: strip spaces/quotes; a folder -> folder/parameters.json."""
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


def file_signature(file_path):
    """(mtime_ns, size) or None when the file cannot be stat'ed."""
    try:
        st = os.stat(file_path)
    except (OSError, ValueError):
        return None
    return (st.st_mtime_ns, st.st_size)


def _clock(t):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
    except Exception:
        return "?"


def step(state, path, scale=1.0, force=False, build=None, now=None, scale_label=None):
    """One component run. Never raises (build errors are caught and reported).

    build(params, scale) -> (geometry_list, colors_list, part_count)
    Returns {"geometry", "colors", "info", "warning", "short", "params", "source"}.
    """
    now = time.time() if now is None else float(now)
    stats = state.setdefault("stats", {"reads": 0, "builds": 0})
    fpath = normalize_path(path)

    # -- 1. watch the file: re-read only when its signature changes -----------
    if not fpath:
        state["sig"], state["file_mtime"] = None, None
        state["read_status"] = "no_path"
        state["read_msg"] = ("No file path: connect a Panel with the full path of "
                             "parameters.json to the 'path' input")
    else:
        sig = file_signature(fpath)
        if sig is None:
            state["sig"], state["file_mtime"] = None, None
            state["read_status"] = "missing"
            state["read_msg"] = "File not found: %s" % fpath
        else:
            state["file_mtime"] = sig[0] / 1e9
            changed = sig != state.get("sig")
            retry = (state.get("read_status") in _TRANSIENT
                     and now - state.get("last_attempt", -1e18) >= RETRY_SECONDS)
            if changed or retry or force:
                state["sig"], state["last_attempt"] = sig, now
                stats["reads"] += 1
                res = read_parameters_file(fpath, fallback=state.get("good"))
                state["read_status"] = res["status"]
                if res["status"] == "ok":
                    meta = dict(res["meta"])
                    meta["file_mtime"], meta["path"] = sig[0] / 1e9, fpath
                    state["good"], state["good_meta"] = res["params"], meta
                    state["read_msg"] = ("Note: " + "; ".join(res["notes"])) if res["notes"] else ""
                else:
                    state["read_msg"] = "%s: %s" % (_STATUS_TEXT.get(res["status"], "Problem"),
                                                    res["message"])

    # -- 2. choose the design: last good, else the defaults --------------------
    good = state.get("good")
    source = "file" if good is not None else "default"
    params = clamp_params(good if good is not None else DEFAULTS)
    warnings = []
    msg = state.get("read_msg") or ""
    if state.get("read_status") not in ("ok", None):
        if good is not None:
            warnings.append("%s -> keeping the last good design (%s)." % (msg, describe(params)))
        else:
            warnings.append("%s -> showing the default design (%s)." % (msg, describe(params)))
    elif msg:
        warnings.append(msg)

    s = _as_number(scale)
    if s is None or s <= 0:
        if scale is not None:
            warnings.append("Ignoring invalid scale %r; using 1." % (scale,))
        s = 1.0
    s = min(max(s, 1e-6), 1e6)

    # -- 3. rebuild only when the parameters (or scale) changed ----------------
    # the key includes the generator files' signature: an edited / new pavilion.js rebuilds
    key = (params["facade_material"], params["window_ratio"], params["roof_angle"],
           params["canopy_depth"], round(s, 9), bool(OUTPUT_BREPS), _LAYOUT_SIG,
           generator_signature())
    meta = state.get("build_meta") or {}
    retry_node = (meta.get("source") == "python" and key == state.get("geo_key")
                  and now - state.get("build_time", -1e18) >= NODE_RETRY_SECONDS)
    if build is not None and (force or retry_node or key != state.get("geo_key")):
        may_try = (force or retry_node or key != state.get("fail_key")
                   or now - state.get("fail_time", -1e18) >= BUILD_RETRY_SECONDS)
        if may_try:
            t0 = time.perf_counter()
            try:
                out = build(params, s)
                geo, cols, nparts = out[0], out[1], out[2]
                meta = dict(out[3]) if len(out) > 3 and isinstance(out[3], dict) else {}
                state["geometry"], state["colors"] = list(geo), list(cols)
                state["part_count"], state["geo_key"] = nparts, key
                state["built_params"] = dict(params)
                state["build_ms"] = 1000.0 * (time.perf_counter() - t0)
                state["build_meta"], state["build_time"] = meta, now
                state.pop("fail_key", None)
                stats["builds"] += 1
            except Exception as exc:
                state["fail_key"], state["fail_time"] = key, now
                state["fail_msg"] = "%s: %s" % (type(exc).__name__, exc)
    if state.get("fail_key") == key:
        warnings.append("Geometry build failed (%s) -> showing the previous geometry."
                        % state.get("fail_msg"))
    elif (state.get("build_meta") or {}).get("warning"):
        warnings.append(state["build_meta"]["warning"])

    shown = state.get("built_params") or params
    warning_text = "\n".join(warnings)
    info = _format_info(state, fpath, shown, source, s, scale_label, now)
    short = "%s | %s%% | %s%s | %s m" % (
        shown["facade_material"], _fmt(shown["window_ratio"]),
        _fmt(shown["roof_angle"]), DEG, _fmt(shown["canopy_depth"]))
    if warning_text:
        short += "  (!)"
    return {"geometry": list(state.get("geometry") or []),
            "colors": list(state.get("colors") or []),
            "info": info, "warning": warning_text, "short": short,
            "params": dict(shown), "source": source}


def _format_info(state, fpath, params, source, scale, scale_label, now):
    stats = state.get("stats", {})
    meta = state.get("good_meta") or {}
    title = ("design from file" if source == "file"
             else "DEFAULT design (no valid parameters file yet)")
    lines = [
        "PLURARCH model - %s" % title,
        "  facade_material : %s" % params["facade_material"],
        "  window_ratio    : %s %%" % _fmt(params["window_ratio"]),
        "  roof_angle      : %s%s" % (_fmt(params["roof_angle"]), DEG),
        "  canopy_depth    : %s m" % _fmt(params["canopy_depth"]),
        "file          : %s" % (fpath or "(no path)"),
        "file modified : %s" % (_clock(state["file_mtime"]) if state.get("file_mtime") else "-"),
    ]
    if meta.get("updated_at"):
        lines.append("updated_at    : %s" % meta["updated_at"])
    if meta.get("round_id") or meta.get("verdict"):
        lines.append("round/verdict : %s / %s" % (meta.get("round_id") or "-", meta.get("verdict") or "-"))
    if state.get("geo_key") is not None:
        lines.append("geometry      : %d parts -> %d objects, built in %.0f ms"
                     % (state.get("part_count", 0), len(state.get("geometry") or []),
                        state.get("build_ms", 0.0)))
        meta = state.get("build_meta") or {}
        if meta.get("source") == "node":
            lines.append("generator     : pavilion.js v%s via node (%.0f ms)"
                         % (meta.get("version") or "?", meta.get("node_ms", 0.0)))
        elif meta.get("source") == "python":
            lines.append("generator     : BUILT-IN fallback (node unavailable, retry every %ds)"
                         % int(NODE_RETRY_SECONDS))
    lines.append("scale         : %s" % (scale_label or _fmt(scale)))
    lines.append("checked %s  (reads %d, builds %d, v%s)" % (
        time.strftime("%H:%M:%S", time.localtime(now)), stats.get("reads", 0),
        stats.get("builds", 0), SCRIPT_VERSION))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 6. Rhino / Grasshopper layer (only runs inside the Grasshopper component)
# ---------------------------------------------------------------------------
def _draw_rank(key):
    return DRAW_ORDER.index(key) if key in DRAW_ORDER else len(DRAW_ORDER)


def _rhino_color(key):
    from System.Drawing import Color
    rgb = PALETTE.get(key, (255, 0, 255))
    alpha = rgb[3] if len(rgb) > 3 else 255
    return Color.FromArgb(int(alpha), int(rgb[0]), int(rgb[1]), int(rgb[2]))


def _model_color(mat):
    """System.Drawing.Color from a model material (alpha from opacity)."""
    from System.Drawing import Color
    rgb = mat.get("color") if isinstance(mat, dict) else None
    if not (isinstance(rgb, (list, tuple)) and len(rgb) >= 3):
        rgb = (255, 0, 255)
    try:
        opacity = float(mat.get("opacity", 1.0))
    except (TypeError, ValueError):
        opacity = 1.0
    alpha = int(round(255 * min(max(opacity, 0.0), 1.0)))
    c = [int(min(max(round(float(v)), 0), 255)) for v in rgb[:3]]
    return Color.FromArgb(alpha, c[0], c[1], c[2])


def _model_groups(model):
    """Parts grouped by material: [(key, material, [parts])], opaque first, glass last."""
    mats = model.get("materials") or {}
    groups, order = {}, []
    for part in model.get("parts") or []:
        if not isinstance(part, dict):
            continue
        key = part.get("m")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(part)

    def transparent(key):
        m = mats.get(key) or {}
        return m.get("kind") == "glass" or float(m.get("opacity", 1.0)) < 0.99
    order.sort(key=lambda k: (1 if transparent(k) else 0))   # stable: keeps the model order
    return [(k, mats.get(k) or {}, groups[k]) for k in order]


def _mesh_hex(mesh, p, scale, count):
    """Add a hexahedron (24 numbers) as 6 flat quads with their own vertices."""
    import Rhino.Geometry as rg
    c = [(p[i * 3] * scale, p[i * 3 + 1] * scale, p[i * 3 + 2] * scale) for i in range(8)]
    for face in HEX_FACES:
        for k in face:
            mesh.Vertices.Add(rg.Point3d(*c[k]))
        mesh.Faces.AddFace(count, count + 1, count + 2, count + 3)
        count += 4
    return count


def _mesh_cyl(mesh, cyl, seg, scale, count):
    """Add a vertical cylinder: flat side quads and triangle-fan caps."""
    import Rhino.Geometry as rg
    cx, cy, z0, z1, r = [float(v) * scale for v in cyl]
    n = max(3, int(round(float(seg or 12))))
    ring = [(cx + r * math.cos(2 * math.pi * k / n), cy + r * math.sin(2 * math.pi * k / n))
            for k in range(n)]
    for k in range(n):
        (xa, ya), (xb, yb) = ring[k], ring[(k + 1) % n]
        for (x, y, z) in ((xa, ya, z0), (xb, yb, z0), (xb, yb, z1), (xa, ya, z1)):
            mesh.Vertices.Add(rg.Point3d(x, y, z))
        mesh.Faces.AddFace(count, count + 1, count + 2, count + 3)
        count += 4
    for z, up in ((z0, False), (z1, True)):
        centre = count
        mesh.Vertices.Add(rg.Point3d(cx, cy, z))
        for (x, y) in ring:
            mesh.Vertices.Add(rg.Point3d(x, y, z))
        for k in range(n):
            a, b = centre + 1 + k, centre + 1 + (k + 1) % n
            if up:
                mesh.Faces.AddFace(centre, a, b)
            else:
                mesh.Faces.AddFace(centre, b, a)
        count += n + 1
    return count


def _rhino_mesh_model(model, scale):
    """Model parts -> one flat-shaded Mesh per material (or Breps) + colours."""
    import Rhino.Geometry as rg
    geometry, colors, nparts = [], [], 0
    for key, mat, parts in _model_groups(model):
        col = _model_color(mat)
        if OUTPUT_BREPS:
            from System.Collections.Generic import List
            for part in parts:
                brep = None
                if isinstance(part.get("p"), list) and len(part["p"]) == 24:
                    pts = List[rg.Point3d]()
                    for i in range(8):
                        pts.Add(rg.Point3d(part["p"][i * 3] * scale, part["p"][i * 3 + 1] * scale,
                                           part["p"][i * 3 + 2] * scale))
                    brep = rg.Brep.CreateFromBox(pts)
                elif isinstance(part.get("cyl"), list) and len(part["cyl"]) == 5:
                    cx, cy, z0, z1, r = [float(v) * scale for v in part["cyl"]]
                    circle = rg.Circle(rg.Plane(rg.Point3d(cx, cy, z0), rg.Vector3d.ZAxis), r)
                    brep = rg.Cylinder(circle, z1 - z0).ToBrep(True, True)
                if brep is not None:
                    geometry.append(brep)
                    colors.append(col)
                    nparts += 1
            continue
        mesh = rg.Mesh()
        count = 0
        for part in parts:
            p, cyl = part.get("p"), part.get("cyl")
            if isinstance(p, list) and len(p) == 24:
                count = _mesh_hex(mesh, p, scale, count)
            elif isinstance(cyl, list) and len(cyl) == 5:
                count = _mesh_cyl(mesh, cyl, part.get("seg"), scale, count)
            else:
                continue
            nparts += 1
        if count == 0:
            continue
        mesh.Normals.ComputeNormals()
        mesh.FaceNormals.ComputeFaceNormals()
        mesh.Compact()
        geometry.append(mesh)
        colors.append(col)
    return geometry, colors, nparts


def _rhino_build(params, scale):
    """The detailed model from node; if node fails, the built-in model with a warning.
    Returns (geometry, colors, part_count, meta)."""
    t0 = time.perf_counter()
    try:
        model = run_node_model(params)
        node_ms = 1000.0 * (time.perf_counter() - t0)
        geo, cols, n = _rhino_mesh_model(model, scale)
        if not geo:
            raise RuntimeError("the node model produced no geometry")
        return geo, cols, n, {"source": "node", "version": model.get("version"), "node_ms": node_ms,
                              "params": model.get("params"), "warning": ""}
    except Exception as exc:
        geo, cols, n = _rhino_build_builtin(params, scale)   # if this fails too, step() reports it
        return geo, cols, n, {"source": "python", "error": str(exc),
                              "warning": "Detailed model unavailable (%s) -> showing the simplified "
                                         "built-in model; node is retried every %ds."
                                         % (exc, int(NODE_RETRY_SECONDS))}


def _rhino_build_builtin(params, scale):
    """Built-in parts -> RhinoCommon geometry + colours (one Mesh per colour, or Breps)."""
    import Rhino.Geometry as rg
    parts = build_parts(params)
    geometry, colors = [], []

    if OUTPUT_BREPS:
        from System.Collections.Generic import List
        for key, corners, _tag in sorted(parts, key=lambda p: _draw_rank(p[0])):
            pts = List[rg.Point3d]()
            for (x, y, z) in corners:
                pts.Add(rg.Point3d(x * scale, y * scale, z * scale))
            brep = rg.Brep.CreateFromBox(pts)
            if brep is not None:
                geometry.append(brep)
                colors.append(_rhino_color(key))
        return geometry, colors, len(parts)

    groups = {}
    for key, corners, _tag in parts:
        groups.setdefault(key, []).append(corners)
    for key in sorted(groups, key=_draw_rank):
        mesh = rg.Mesh()
        count = 0
        for corners in groups[key]:
            for face in HEX_FACES:       # 4 own vertices per quad -> crisp flat shading
                for k in face:
                    x, y, z = corners[k]
                    mesh.Vertices.Add(rg.Point3d(x * scale, y * scale, z * scale))
                mesh.Faces.AddFace(count, count + 1, count + 2, count + 3)
                count += 4
        mesh.Normals.ComputeNormals()
        mesh.FaceNormals.ComputeFaceNormals()
        mesh.Compact()
        geometry.append(mesh)
        colors.append(_rhino_color(key))
    return geometry, colors, len(parts)


def _enum_name(value):
    try:
        return str(value.ToString())
    except Exception:
        return str(value)


def _rhino_scale(scale_in):
    """(scale, label, warning). Unconnected input -> follow the document units."""
    if scale_in is not None:
        x = _as_number(scale_in)
        if x is not None and x > 0:
            return x, "%s (from the scale input)" % _fmt(x), ""
        return 1.0, "1 (invalid scale input ignored)", \
            "Ignoring invalid 'scale' input %r; using 1." % (scale_in,)
    try:
        import Rhino
        units = Rhino.RhinoDoc.ActiveDoc.ModelUnitSystem
        f = float(Rhino.RhinoMath.UnitScale(Rhino.UnitSystem.Meters, units))
        if f > 0 and not math.isinf(f) and not math.isnan(f):
            return f, "%s (auto: document units are %s)" % (_fmt(f), _enum_name(units)), ""
    except Exception:
        pass
    return 1.0, "1 (default)", ""


def _gh_feedback(warning_text, short):
    """Orange balloon + text under the component. Silently ignored if unsupported."""
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


_STICKY_LAST = "plurarch.model_builder|last"


def _gh_run(path_in, tick_in, scale_in):
    import scriptcontext as sc
    scale_val, scale_label, scale_warn = _rhino_scale(scale_in)
    fpath = normalize_path(path_in)
    key = "plurarch.model_builder|%s|%s" % (SCRIPT_VERSION, os.path.normcase(fpath))
    state = sc.sticky.get(key)
    if not isinstance(state, dict):
        state = {}
        sc.sticky[key] = state
    sc.sticky[_STICKY_LAST] = key
    out = step(state, fpath, scale_val, force=(tick_in is True), build=_rhino_build,
               scale_label=scale_label)
    warning_text = "\n".join(w for w in (out["warning"], scale_warn) if w)
    _gh_feedback(warning_text, out["short"])
    return out["geometry"], out["colors"], out["info"], warning_text


def _gh_entry():
    """Grasshopper entry point: wraps everything so the component never turns red."""
    g = globals()
    try:
        return _gh_run(g.get("path"), g.get("tick"), g.get("scale"))
    except Exception as exc:
        msg = "Plurarch model_builder: unexpected error, showing the last geometry (%s: %s)" % (
            type(exc).__name__, exc)
        geo, cols = [], []
        try:
            import scriptcontext as sc
            st = sc.sticky.get(sc.sticky.get(_STICKY_LAST))
            if isinstance(st, dict):
                geo, cols = list(st.get("geometry") or []), list(st.get("colors") or [])
        except Exception:
            pass
        _gh_feedback(msg, "Plurarch: error (!)")
        return geo, cols, msg, msg


# ---------------------------------------------------------------------------
# 7. Self-test (plain CPython, no Rhino):  python model_builder.py --selftest
# ---------------------------------------------------------------------------
def _facade_shares(parts, params):
    """Glazed area / wall area per facade, measured from the built parts."""
    roof = RoofGeometry(params["roof_angle"])
    glazed = {}
    for _key, c, tag in parts:
        if tag.startswith("glazing:"):
            name = tag.split(":", 1)[1]
            xs = [q[0] for q in c[:4]]
            ys = [q[1] for q in c[:4]]
            width = (max(xs) - min(xs)) if name in ("front", "back") else (max(ys) - min(ys))
            glazed[name] = glazed.get(name, 0.0) + width * (c[4][2] - c[0][2])
    shares = {}
    for name, (ox, oy), _t, _n, length in FACADES:
        y_mid = oy if name in ("front", "back") else 0.5 * (Y0 + Y1)
        shares[name] = glazed.get(name, 0.0) / (length * (roof.under(y_mid) - FLOOR_TOP))
    return shares


def _inside_part(c, pt):
    """Is pt strictly inside prism c (axis-aligned footprint, planar top/bottom)?"""
    xs, ys = [q[0] for q in c[:4]], [q[1] for q in c[:4]]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    if not (x0 < pt[0] < x1 and y0 < pt[1] < y1):
        return False
    a, b = (pt[0] - x0) / (x1 - x0), (pt[1] - y0) / (y1 - y0)

    def z_at(level):                   # bilinear = exact for a planar top/bottom
        z = {}
        for q in c[level:level + 4]:
            z[(q[0] == x1, q[1] == y1)] = q[2]
        return ((1 - a) * (1 - b) * z[(False, False)] + a * (1 - b) * z[(True, False)]
                + a * b * z[(True, True)] + (1 - a) * b * z[(False, True)])
    return z_at(0) < pt[2] < z_at(4)


def _coplanar_conflicts(parts):
    """Visible z-fighting: axis-aligned faces in the same plane, facing the same way,
    overlapping with a real area, with different colours, and not hidden inside or
    against another solid (checked just outside the overlap)."""
    groups = {}
    for idx, (key, c, _tag) in enumerate(parts):
        for face in HEX_FACES:
            q = [c[k] for k in face]
            e1 = [q[1][i] - q[0][i] for i in range(3)]
            e2 = [q[3][i] - q[0][i] for i in range(3)]
            nv = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2],
                  e1[0] * e2[1] - e1[1] * e2[0])
            ln = math.sqrt(sum(v * v for v in nv)) or 1.0
            axis = max(range(3), key=lambda i: abs(nv[i]))
            if abs(nv[axis]) / ln < 1.0 - 1e-9:
                continue                           # sloped face: skip
            u, v = [i for i in range(3) if i != axis]
            bbox = (min(p[u] for p in q), max(p[u] for p in q),
                    min(p[v] for p in q), max(p[v] for p in q))
            gk = (axis, nv[axis] > 0, round(q[0][axis], 6))
            groups.setdefault(gk, []).append((idx, key, bbox, u, v))
    conflicts = []
    for (axis, positive, level), faces in groups.items():
        for i in range(len(faces)):
            for k in range(i + 1, len(faces)):
                (ia, ka, a, u, v), (ib, kb, b, _u, _v) = faces[i], faces[k]
                if ka == kb or ia == ib:
                    continue
                lo_u, hi_u = max(a[0], b[0]), min(a[1], b[1])
                lo_v, hi_v = max(a[2], b[2]), min(a[3], b[3])
                if hi_u - lo_u <= 1e-6 or hi_v - lo_v <= 1e-6 or (hi_u - lo_u) * (hi_v - lo_v) <= 1e-6:
                    continue
                pt = [0.0, 0.0, 0.0]
                pt[axis] = level + (1e-4 if positive else -1e-4)   # just outside the faces
                pt[u], pt[v] = 0.5 * (lo_u + hi_u), 0.5 * (lo_v + hi_v)
                hidden = any(_inside_part(c, pt) for j, (_k, c, _t) in enumerate(parts)
                             if j not in (ia, ib))
                if not hidden:
                    conflicts.append((parts[ia][2], parts[ib][2]))
    return conflicts


def _mock_grasshopper(src, here, check, tmp):
    """Run this file the way the Grasshopper component does (exec with ghenv and the
    inputs as globals), against minimal fake Rhino / System / Grasshopper modules."""
    import types
    counters = {"meshes": 0, "breps": 0, "messages": []}
    units = {"name": "Meters", "factor": 1.0}

    class Point3d(object):
        def __init__(self, x, y, z):
            self.X, self.Y, self.Z = x, y, z

    class _Verts(list):
        def Add(self, p):
            self.append(p)
            return len(self) - 1

    class _Faces(list):
        def AddFace(self, *idx):
            self.append(tuple(idx))
            return len(self) - 1

    class _Normals(object):
        def ComputeNormals(self):
            return True

        def ComputeFaceNormals(self):
            return True

    class Mesh(object):
        fail = False

        def __init__(self):
            if Mesh.fail:
                raise RuntimeError("mock RhinoCommon failure")
            counters["meshes"] += 1
            self.Vertices, self.Faces = _Verts(), _Faces()
            self.Normals, self.FaceNormals = _Normals(), _Normals()

        def Compact(self):
            return True

    class Brep(object):
        @staticmethod
        def CreateFromBox(pts):
            counters["breps"] += 1
            return ("brep", len(list(pts)))

    class _Unit(object):
        def __init__(self, name):
            self.name = name

        def ToString(self):
            return self.name

    class Color(object):
        @staticmethod
        def FromArgb(a, r, g, b):
            return ("argb", a, r, g, b)

    class _GenericList(object):
        def __getitem__(self, _t):
            return _ListOf

    class _ListOf(list):
        def Add(self, p):
            self.append(p)

    class _Component(object):
        Message = ""

        def AddRuntimeMessage(self, level, text):
            counters["messages"].append((level, text))

    rhino = types.ModuleType("Rhino")
    geom = types.ModuleType("Rhino.Geometry")
    geom.Point3d, geom.Mesh, geom.Brep = Point3d, Mesh, Brep
    rhino.Geometry = geom
    rhino.UnitSystem = types.SimpleNamespace(Meters=_Unit("Meters"))
    rhino.RhinoDoc = types.SimpleNamespace(ActiveDoc=None)
    rhino.RhinoMath = types.SimpleNamespace(UnitScale=lambda a, b: units["factor"])
    system = types.ModuleType("System")
    drawing = types.ModuleType("System.Drawing")
    drawing.Color = Color
    system.Drawing = drawing
    generic = types.ModuleType("System.Collections.Generic")
    generic.List = _GenericList()
    sticky_mod = types.ModuleType("scriptcontext")
    sticky_mod.sticky = {}
    gh = types.ModuleType("Grasshopper")
    gh.Kernel = types.SimpleNamespace(GH_RuntimeMessageLevel=types.SimpleNamespace(Warning="WARN"))
    fakes = {"Rhino": rhino, "Rhino.Geometry": geom, "System": system,
             "System.Drawing": drawing, "System.Collections": types.ModuleType("System.Collections"),
             "System.Collections.Generic": generic, "scriptcontext": sticky_mod, "Grasshopper": gh}
    saved = {k: sys.modules.get(k) for k in fakes}
    code = compile(src, here, "exec")
    comp = _Component()

    def run(path_value, scale_value=None, tick_value=None, extra=None):
        rhino.RhinoDoc.ActiveDoc = types.SimpleNamespace(ModelUnitSystem=_Unit(units["name"]))
        counters["messages"] = []
        # like the loader: _MB is the path of model_builder.py, the code runs via exec
        scope = {"__name__": "__main__", "ghenv": types.SimpleNamespace(Component=comp),
                 "path": path_value, "tick": tick_value, "_MB": here}
        if scale_value is not None:
            scope["scale"] = scale_value
        scope.update(extra or {})
        exec(code, scope)
        return scope

    try:
        sys.modules.update(fakes)
        fp = os.path.join(tmp, "gh_parameters.json")
        with open(fp, "w", encoding="utf-8") as fh:
            json.dump({"parameters": {"facade_material": "glass", "window_ratio": 60,
                                      "roof_angle": 35, "canopy_depth": 3}}, fh)
        s = run(fp)
        geo, cols = s.get("geometry"), s.get("colors")
        ok = (isinstance(geo, list) and len(geo) == len(cols) > 0 and s.get("warning") == ""
              and all(isinstance(m, Mesh) for m in geo) and isinstance(s.get("info"), str)
              and "glass" in s["info"] and "glass" in comp.Message)
        n_faces = sum(len(m.Faces) for m in geo)
        check("GH (mocked): outputs geometry/colors/info/warning, %d meshes, %d faces"
              % (len(geo or []), n_faces), ok, str(s.get("warning")))
        alphas = sorted(set(c[1] for c in cols))
        check("GH (mocked): detailed model from node (info says so), glass semi-transparent "
              "(alphas %s)" % alphas, "via node" in s["info"] and min(alphas) < 128 and n_faces > 5000,
              s["info"])
        m0 = counters["meshes"]
        s2 = run(fp)
        check("GH (mocked): Timer tick reuses cached meshes (no rebuild)",
              counters["meshes"] == m0 and s2["geometry"][0] is geo[0])
        with open(fp, "w", encoding="utf-8") as fh:
            fh.write('{"parameters": {"facade_mat')
        st = os.stat(fp)
        os.utime(fp, ns=(st.st_mtime_ns + 10 ** 9, st.st_mtime_ns + 10 ** 9))
        s3 = run(fp)
        check("GH (mocked): broken file -> orange warning, last geometry kept",
              s3["warning"] and counters["messages"] and counters["messages"][0][0] == "WARN"
              and s3["geometry"][0] is geo[0] and comp.Message.endswith("(!)"))
        units["name"], units["factor"] = "Millimeters", 1000.0
        s4 = run(fp)
        zmax = max(p.Z for m in s4["geometry"] for p in m.Vertices)
        check("GH (mocked): mm document -> auto scale 1000 (roof top %.0f mm)" % zmax,
              11500 < zmax < 12500 and "auto" in s4["info"])
        s5 = run(fp, scale_value=1.0)
        zmax = max(p.Z for m in s5["geometry"] for p in m.Vertices)
        check("GH (mocked): scale input overrides the document units", 11.5 < zmax < 12.5)
        Mesh.fail = True
        s6 = run(fp, scale_value=2.0)
        Mesh.fail = False
        check("GH (mocked): RhinoCommon exception -> no raise, warning, old geometry",
              "build failed" in s6["warning"] and s6["geometry"][0] is s5["geometry"][0])
        s7 = run(None)
        check("GH (mocked): unconnected path -> default design + warning",
              "No file path" in s7["warning"] and len(s7["geometry"]) > 0)
        fp2 = os.path.join(tmp, "gh_parameters_2.json")
        with open(fp2, "w", encoding="utf-8") as fh:
            json.dump({"parameters": {"facade_material": "concrete", "window_ratio": 20,
                                      "roof_angle": 0, "canopy_depth": 0}}, fh)
        missing = os.path.join(tmp, "no_such_node.exe")
        s8 = run(fp2, scale_value=1.0, extra={"_PLURARCH_NODE": missing})
        check("GH (mocked): node missing -> built-in fallback model + clear warning (never blank)",
              len(s8["geometry"]) > 0 and "built-in" in s8["warning"] and "BUILT-IN" in s8["info"]
              and comp.Message.endswith("(!)"), s8["warning"])
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _selftest():
    import ast
    import itertools
    import tempfile
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    results = []

    def check(name, cond, detail=""):
        results.append((bool(cond), name, detail))

    print("Plurarch model_builder self-test  (v%s, Python %s)"
          % (SCRIPT_VERSION, sys.version.split()[0]))

    # -- source: Python 3.9 grammar, ASCII only, spec matches the config ------
    here = os.path.abspath(__file__)
    with open(here, "r", encoding="utf-8") as fh:
        src = fh.read()
    try:
        ast.parse(src, feature_version=(3, 9))
        ok, why = True, ""
    except SyntaxError as exc:
        ok, why = False, str(exc)
    check("source parses with the Python 3.9 grammar", ok, why)
    check("source is plain ASCII (safe to paste into the Rhino editor)",
          all(ord(ch) < 128 for ch in src))
    cfg = os.path.join(os.path.dirname(os.path.dirname(here)), "config", "parameters.json")
    if os.path.isfile(cfg):
        with open(cfg, "r", encoding="utf-8") as fh:
            conf = {p["key"]: p for p in json.load(fh)["parameters"]}
        ok = set(conf) == set(PARAM_KEYS)
        ok = ok and tuple(o["value"] for o in conf["facade_material"]["options"]) == MATERIAL_OPTIONS
        ok = ok and conf["facade_material"]["default"] == DEFAULTS["facade_material"]
        for key, lo, hi, inc in NUMERIC_SPEC:
            c = conf[key]
            ok = ok and (float(c["min"]), float(c["max"]), float(c["step"]),
                         float(c["default"])) == (lo, hi, inc, DEFAULTS[key])
        check("ranges/steps/defaults match config/parameters.json", ok)
    else:
        print("  (config/parameters.json not found next to grasshopper/, spec check skipped)")

    # -- validation, clamping, snapping ---------------------------------------
    good = {"facade_material": "glass", "window_ratio": 60, "roof_angle": 35, "canopy_depth": 3}
    p, e, n = validate_parameters(good)
    check("valid set accepted", p is not None and not e and not n and p["roof_angle"] == 35.0)
    p, e, n = validate_parameters(dict(DEFAULTS))
    check("defaults accepted", p == DEFAULTS)
    p, e, n = validate_parameters({"facade_material": " Concrete ", "window_ratio": "45",
                                   "roof_angle": 20, "canopy_depth": 0.5})
    check("material case/space and numeric strings accepted",
          p is not None and p["facade_material"] == "concrete" and p["window_ratio"] == 45.0)
    p, e, n = validate_parameters({"facade_material": "timber", "window_ratio": 42,
                                   "roof_angle": 17.5, "canopy_depth": 1.3})
    check("off-step values snapped (42->40, 17.5->20, 1.3->1.5) with notes",
          p is not None and (p["window_ratio"], p["roof_angle"], p["canopy_depth"]) == (40.0, 20.0, 1.5)
          and len(n) == 3, "; ".join(n))
    p, e, n = validate_parameters({"facade_material": "timber", "window_ratio": 60.0000000001,
                                   "roof_angle": 0, "canopy_depth": 2.9999999})
    check("float noise at the range ends accepted silently", p is not None and not n,
          "; ".join(e + n))
    bad_sets = [
        ("window_ratio 95", {"window_ratio": 95}),
        ("window_ratio 15", {"window_ratio": 15}),
        ("roof_angle -5", {"roof_angle": -5}),
        ("roof_angle 40", {"roof_angle": 40}),
        ("canopy_depth 3.5", {"canopy_depth": 3.5}),
        ("material brick", {"facade_material": "brick"}),
        ("material 3", {"facade_material": 3}),
        ("window_ratio null", {"window_ratio": None}),
        ("window_ratio 'abc'", {"window_ratio": "abc"}),
        ("roof_angle true", {"roof_angle": True}),
        ("canopy_depth NaN", {"canopy_depth": float("nan")}),
        ("canopy_depth inf", {"canopy_depth": float("inf")}),
    ]
    rejected = []
    for label, change in bad_sets:
        raw = dict(DEFAULTS)
        raw.update(change)
        p, e, n = validate_parameters(raw)
        if p is None and e:
            rejected.append(label)
    check("out-of-range / wrong-type values rejected (%d/%d)" % (len(rejected), len(bad_sets)),
          len(rejected) == len(bad_sets),
          "not rejected: %s" % [lb for lb, _ in bad_sets if lb not in rejected])
    p, e, n = validate_parameters({"facade_material": "glass"}, fallback=good)
    check("missing keys filled from the last good set, with notes",
          p is not None and p["window_ratio"] == 60.0 and len(n) == 3)
    check("non-object 'parameters' rejected", validate_parameters([1, 2])[0] is None)
    cp = clamp_params({"facade_material": "wood", "window_ratio": 99, "roof_angle": -3,
                       "canopy_depth": "x"})
    check("clamp_params repairs anything",
          cp == {"facade_material": "timber", "window_ratio": 60.0, "roof_angle": 0.0,
                 "canopy_depth": 1.5}, str(cp))
    check("snap(): 22.4->20, 22.5->25, 1.24->1.0, 1.25->1.5, 99->60",
          (snap(22.4, 20, 60, 5), snap(22.5, 20, 60, 5), snap(1.24, 0, 3, 0.5),
           snap(1.25, 0, 3, 0.5), snap(99, 20, 60, 5)) == (20.0, 25.0, 1.0, 1.5, 60.0))

    # -- reading files -----------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        def put(name, text, encoding="utf-8"):
            fp = os.path.join(tmp, name)
            with open(fp, "w", encoding=encoding) as fh:
                fh.write(text)
            return fp
        full = json.dumps({"parameters": good, "updated_at": "2026-09-30T15:14:02Z",
                           "round_id": "r1", "verdict": "ACCEPTED"})
        cases = [
            ("missing file", os.path.join(tmp, "nope.json"), "missing"),
            ("empty file", put("empty.json", ""), "invalid_json"),
            ("half-written JSON", put("half.json", full[: len(full) // 2]), "invalid_json"),
            ("not JSON", put("junk.json", "hello"), "invalid_json"),
            ("JSON list", put("list.json", "[1, 2, 3]"), "invalid"),
            ("no parameters key", put("nokey.json", '{"updated_at": "x"}'), "invalid"),
            ("out of range", put("oor.json", json.dumps({"parameters": dict(good, window_ratio=80)})), "invalid"),
            ("valid file", put("ok.json", full), "ok"),
            ("valid file with BOM", put("bom.json", full, encoding="utf-8-sig"), "ok"),
            ("bare parameters object", put("bare.json", json.dumps(good)), "ok"),
        ]
        for label, fp, expected in cases:
            res = read_parameters_file(fp)
            check("read: %s -> %s" % (label, expected), res["status"] == expected,
                  "%s: %s" % (res["status"], res["message"]))
        res = read_parameters_file(os.path.join(tmp, "ok.json"))
        check("read: metadata kept (updated_at, round_id, verdict)",
              res["meta"] == {"updated_at": "2026-09-30T15:14:02Z", "round_id": "r1",
                              "verdict": "ACCEPTED"})

    # -- geometry over the whole parameter grid ----------------------------------
    grid = list(itertools.product(MATERIAL_OPTIONS, (20, 40, 60), (0, 15, 35), (0, 0.5, 1.5, 3)))
    max_parts, max_share_err, worst, bad_prisms = 0, 0.0, "", 0
    embed_bad, canopy_bad, times = 0, 0, []
    zfight_bad, zfight_example = 0, ""
    for mat, wr, ra, cd in grid:
        prm = {"facade_material": mat, "window_ratio": wr, "roof_angle": ra, "canopy_depth": cd}
        t0 = time.perf_counter()
        parts = build_parts(prm)
        times.append(1000.0 * (time.perf_counter() - t0))
        max_parts = max(max_parts, len(parts))
        roof = RoofGeometry(ra)
        for _key, c, tag in parts:
            if any(not math.isfinite(v) for q in c for v in q) \
                    or any(c[k + 4][2] - c[k][2] < MIN_DIM for k in range(4)) \
                    or _area2([(q[0], q[1]) for q in c[:4]]) <= 0:
                bad_prisms += 1
            role = tag.split(":", 1)[0]
            if role in ("wall", "fin", "mullion") or tag == "post:corner" or tag.endswith(":top"):
                for q in c[4:]:          # top corners must sit inside the roof slab
                    inside = roof.x0 <= q[0] <= roof.x1 and roof.y0 <= q[1] <= roof.y1
                    if not (inside and roof.under(q[1]) - 1e-9 <= q[2] <= roof.top(q[1]) + 1e-9):
                        embed_bad += 1
        for name, share in _facade_shares(parts, prm).items():
            err = abs(share - wr / 100.0)
            if err > max_share_err:
                max_share_err, worst = err, "%s %s%% %sdeg %s: %.1f%%" % (mat, wr, ra, name, 100 * share)
        conflicts = _coplanar_conflicts(parts)
        if conflicts:
            zfight_bad += 1
            zfight_example = "%s %s%% %sdeg %sm: %s" % (mat, wr, ra, cd, conflicts[:3])
        tags = [t for _k, _c, t in parts]
        can = canopy_extent(cd, mat)
        n_can, n_posts = tags.count("canopy"), tags.count("post:canopy")
        if cd == 0:
            ok = can is None and n_can == 0 and n_posts == 0
        else:
            ok = (can is not None and n_can == 1 and n_posts == (2 if cd >= 1.5 else 0)
                  and abs((Y0 - MATERIALS[mat]["out"]) - can["y_out"] - cd) < 1e-9
                  and can["z1"] < roof.under(can["y_in"]) - 0.5)
        canopy_bad += 0 if ok else 1
    check("all %d parameter combinations build, max %d parts (< 400)" % (len(grid), max_parts),
          max_parts < 400)
    check("every part is a valid prism (finite, positive height and area)", bad_prisms == 0,
          "%d bad" % bad_prisms)
    check("glazed share of every facade == window_ratio (max error %.2f %%)"
          % (100 * max_share_err), max_share_err <= 0.015, worst)
    check("walls, fins, panels and posts end inside the roof slab (no gaps)", embed_bad == 0,
          "%d corners outside" % embed_bad)
    check("no coplanar overlapping faces of different colours (no z-fighting)",
          zfight_bad == 0, "%d combos, e.g. %s" % (zfight_bad, zfight_example))
    check("canopy: none at 0 m, projects exactly canopy_depth, posts from 1.5 m, below the roof",
          canopy_bad == 0, "%d bad" % canopy_bad)
    r0, r35 = RoofGeometry(0), RoofGeometry(35)
    check("roof: flat at 0 deg, front eave %.2f m at 35 deg (+%.2f m)"
          % (r35.front_eave(), r35.front_eave() - EAVE_LOW),
          abs(r0.front_eave() - EAVE_LOW) < 1e-9 and 6.9 < r35.front_eave() - EAVE_LOW < 7.1)
    ohs = [RoofGeometry(a).overhang_low for a in range(0, 40, 5)]
    check("roof: low-eave overhang grows with the angle (%.1f -> %.1f m), edge >= 2.2 m"
          % (ohs[0], ohs[-1]),
          all(b > a for a, b in zip(ohs, ohs[1:])) and r35.lowest_edge() >= 2.2)
    shapes = {}
    for mat in MATERIAL_OPTIONS:
        tg = [t.split(":")[0] for _k, _c, t in build_parts(dict(DEFAULTS, facade_material=mat))]
        shapes[mat] = (tg.count("fin"), tg.count("panel"), tg.count("mullion"), tg.count("transom"))
    check("materials differ in shape (fins / panels / mullions+transoms)",
          shapes["timber"][0] > 0 and shapes["timber"][1:] == (0, 0, 0)
          and shapes["concrete"][1] > 0 and shapes["concrete"][0] == shapes["concrete"][2] == 0
          and shapes["glass"][2] > 0 and shapes["glass"][3] > 0 and shapes["glass"][:2] == (0, 0),
          str(shapes))
    check("build_parts fast enough (max %.1f ms, pure Python)" % max(times), max(times) < 200)

    # -- watcher + cache: simulated Timer ticks -----------------------------------
    calls = []

    def fake_build(prm, scale):
        calls.append((dict(prm), scale))
        parts = build_parts(prm)
        keys = sorted(set(k for k, _c, _t in parts), key=_draw_rank)
        return ["G%d" % len(calls)] * len(keys), keys, len(parts)

    def failing_build(prm, scale):
        raise RuntimeError("simulated RhinoCommon failure")

    with tempfile.TemporaryDirectory() as tmp:
        fp = os.path.join(tmp, "parameters.json")
        st, clock, mt = {}, [1000.0], [1700000000 * 10 ** 9]

        def tick(**kw):
            clock[0] += 0.5
            kw.setdefault("build", fake_build)
            return step(st, kw.pop("path", fp), kw.pop("scale", 1.0), now=clock[0], **kw)

        def write(obj):
            text = obj if isinstance(obj, str) else json.dumps({"parameters": obj,
                                                                "verdict": "ACCEPTED"})
            with open(fp + ".tmp", "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(fp + ".tmp", fp)          # atomic, like the design server
            mt[0] += 10 ** 9
            os.utime(fp, ns=(mt[0], mt[0]))

        design_b = {"facade_material": "concrete", "window_ratio": 20, "roof_angle": 0,
                    "canopy_depth": 0}
        r = tick()
        check("watch: no file yet -> default design + warning",
              r["source"] == "default" and r["params"] == DEFAULTS and "not found" in r["warning"]
              and len(calls) == 1 and r["geometry"] == ["G1"] * len(r["colors"]))
        for _ in range(3):
            r = tick()
        check("watch: repeated ticks without a file do not rebuild", len(calls) == 1)
        write(good)
        r = tick()
        reads = st["stats"]["reads"]
        check("watch: new valid file -> read, rebuilt, warning cleared",
              r["params"]["facade_material"] == "glass" and len(calls) == 2 and r["warning"] == "")
        for _ in range(5):
            r = tick()
        check("watch: 5 idle ticks -> 0 reads, 0 rebuilds",
              st["stats"]["reads"] == reads and len(calls) == 2)
        write(good)
        r = tick()
        check("watch: same content, new mtime -> re-read but no rebuild",
              st["stats"]["reads"] == reads + 1 and len(calls) == 2)
        write('{"parameters": {"facade_material": "tim')
        r = tick()
        check("watch: half-written JSON -> warning, last good geometry kept",
              "JSON" in r["warning"] and "last good" in r["warning"]
              and r["params"]["facade_material"] == "glass" and r["geometry"][0] == "G2")
        reads = st["stats"]["reads"]
        r = tick()
        no_retry_yet = st["stats"]["reads"] == reads
        clock[0] += RETRY_SECONDS
        r = tick()
        check("watch: unchanged broken file retried once after %gs, warning kept" % RETRY_SECONDS,
              no_retry_yet and st["stats"]["reads"] == reads + 1 and "JSON" in r["warning"])
        write(dict(good, window_ratio=95))
        r = tick()
        r2 = tick()
        check("watch: out-of-range value -> warning, last good kept, no rebuild",
              "outside" in r["warning"] and r2["warning"] == r["warning"]
              and r["params"]["window_ratio"] == 60.0 and len(calls) == 2)
        os.remove(fp)
        r = tick()
        check("watch: file deleted -> warning, last good kept",
              "not found" in r["warning"] and r["params"]["facade_material"] == "glass"
              and r["geometry"][0] == "G2")
        write(design_b)
        r = tick()
        check("watch: new design after problems -> rebuilt, warning cleared",
              r["params"]["facade_material"] == "concrete" and len(calls) == 3
              and r["warning"] == "")
        r = tick(scale=1000.0)
        check("watch: scale change -> rebuild with the new scale",
              len(calls) == 4 and calls[-1][1] == 1000.0)
        reads = st["stats"]["reads"]
        r = tick(scale=1000.0, force=True)
        check("watch: force (Button on tick) -> re-read and rebuild",
              st["stats"]["reads"] == reads + 1 and len(calls) == 5)
        write(dict(design_b, window_ratio=42))
        r = tick(scale=1000.0)
        check("watch: off-step value snapped, note in warning",
              r["params"]["window_ratio"] == 40.0 and "snapped" in r["warning"])
        write(dict(design_b, roof_angle=30))
        r = tick(scale=1000.0, build=failing_build)
        check("watch: geometry build error -> no exception, previous geometry kept",
              "build failed" in r["warning"] and r["geometry"][0] == "G6")
        r = tick(scale=1000.0)
        check("watch: failed build not retried every tick", len(calls) == 6)
        clock[0] += BUILD_RETRY_SECONDS
        r = tick(scale=1000.0)
        check("watch: failed build retried later and recovers",
              len(calls) == 7 and r["warning"] == "" and r["params"]["roof_angle"] == 30.0)
        r = tick(scale="abc")
        check("watch: invalid scale -> 1 + warning", "scale" in r["warning"] and calls[-1][1] == 1.0)
        r = step({}, tmp, 1.0, build=fake_build, now=clock[0])
        check("watch: a folder as path -> reads folder/parameters.json",
              r["source"] == "file" and r["params"]["roof_angle"] == 30.0)
        r = step({}, '  "%s"  ' % fp, 1.0, build=fake_build, now=clock[0])
        check("watch: quoted path from 'Copy as path' accepted", r["source"] == "file")
        r = step({}, None, None, build=fake_build, now=clock[0])
        check("watch: no path -> default design + warning",
              r["source"] == "default" and "No file path" in r["warning"] and r["geometry"])
        info_example = tick(scale=1.0)["info"]

    # -- the detailed generator through node ------------------------------------------
    repo = repo_root()
    node = find_node(repo)
    check("node found (%s)" % node, bool(node) and os.path.isfile(node))
    node_times, node_ok, node_detail = [], True, ""
    for prm in ({"facade_material": "glass", "window_ratio": 60, "roof_angle": 35, "canopy_depth": 3},
                {"facade_material": "concrete", "window_ratio": 20, "roof_angle": 0, "canopy_depth": 0},
                dict(DEFAULTS), {"facade_material": "timber", "window_ratio": 45,
                                 "roof_angle": 35, "canopy_depth": 0.5}):
        t0 = time.perf_counter()
        try:
            model = run_node_model(prm, repo=repo)
        except Exception as exc:
            node_ok, node_detail = False, str(exc)
            break
        node_times.append(1000.0 * (time.perf_counter() - t0))
        got = model.get("params") or {}
        same = (got.get("facade_material") == prm["facade_material"]
                and all(abs(float(got.get(k, -1)) - float(prm[k])) < 1e-9 for k, _a, _b, _c in NUMERIC_SPEC))
        if not (same and model.get("version") == "2.0.0" and len(model["parts"]) >= 800
                and set(p.get("question") for p in model.get("pins", [])) == set(PARAM_KEYS)):
            node_ok, node_detail = False, "params %s, version %s, %d parts" % (
                got, model.get("version"), len(model["parts"]))
            break
    check("node generator returns the same parameters, v2.0.0, >= 800 parts, all pins (%s ms)"
          % "/".join("%.0f" % t for t in node_times), node_ok, node_detail)
    try:
        run_node_model(DEFAULTS, repo=repo, node=os.path.join(repo, "no_such_node.exe"))
        raised = ""
    except RuntimeError as exc:
        raised = str(exc)
    check("bad node path -> RuntimeError with a clear message (%s)" % raised[:40],
          raised.startswith("cannot start node"))
    sig = generator_signature(repo)
    check("rebuild key includes the generator signature (pavilion.js + CLI found)",
          all(isinstance(x, tuple) for x in sig))

    # -- the Grasshopper branch itself, against mocked Rhino modules --------------
    with tempfile.TemporaryDirectory() as tmp:
        _mock_grasshopper(src, here, check, tmp)

    # -- summary ------------------------------------------------------------------
    print("")
    for ok, name, detail in results:
        print("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                             ("   <- " + detail) if (detail and not ok) else ""))
    print("")
    print("Geometry at the default values (40 %, 15 deg, 1.5 m):")
    print("  material   parts  objects  fins/panels/mullions/transoms  glazed share at 20/40/60 %")
    for mat in MATERIAL_OPTIONS:
        base = dict(DEFAULTS, facade_material=mat)
        parts = build_parts(base)
        shares = []
        for wr in (20, 40, 60):
            sh = _facade_shares(build_parts(dict(base, window_ratio=wr)), dict(base, window_ratio=wr))
            shares.append("%.1f" % (100 * sum(sh.values()) / len(sh)))
        print("  %-9s  %5d  %7d  %-29s  %s" % (mat, len(parts), len(set(k for k, _c, _t in parts)),
                                                "/".join(str(v) for v in shapes[mat]), " / ".join(shares)))
    print("  roof_angle  front eave  back eave  low overhang  lowest roof edge")
    for a in (0, 15, 35):
        rg_ = RoofGeometry(a)
        print("  %6d deg  %8.2f m  %7.2f m  %10.2f m  %14.2f m" % (
            a, rg_.front_eave(), rg_.under(Y1), rg_.overhang_low, rg_.lowest_edge()))
    print("  canopy_depth  projection beyond skin  canopy parts  posts")
    for cd in (0, 0.5, 1.5, 3):
        can = canopy_extent(cd, "timber")
        print("  %9s m  %20s  %12d  %5d" % (_fmt(cd), ("%s m" % _fmt(can["projection"])) if can else "-",
                                            1 if can else 0, len(can["posts"]) if can else 0))
    print("  build_parts: %d builds, mean %.1f ms, max %.1f ms (pure Python, before Rhino meshing)"
          % (len(times), sum(times) / len(times), max(times)))
    print("")
    print("Example 'info' output:")
    print("  " + info_example.replace(DEG, " deg").replace("\n", "\n  "))
    passed = sum(1 for ok, _n, _d in results if ok)
    print("")
    print("RESULT: %d/%d checks passed" % (passed, len(results)))
    return 0 if passed == len(results) else 1


# ---------------------------------------------------------------------------
# 8. Entry point
# ---------------------------------------------------------------------------
if "ghenv" in globals():
    # Inside the Grasshopper component: assign the four outputs.
    geometry, colors, info, warning = _gh_entry()
elif __name__ == "__main__":
    if "--selftest" in getattr(sys, "argv", [])[1:]:
        sys.exit(_selftest())
    print("Paste this file into a Rhino 8 Grasshopper 'Python 3 Script' component "
          "(see grasshopper/SETUP.md), or run:  python model_builder.py --selftest")
