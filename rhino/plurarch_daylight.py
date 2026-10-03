# -*- coding: utf-8 -*-
"""Plurarch daylight engine for Rhino 8 (IronPython 2.7 + RhinoCommon), driven by design-mcp.

design_mcp/skylights.py sends:  exec(open(<this file>).read(), {"PLX": {...}})  over the RhinoMCP socket.
PLX = {"repo": <repo>, "action": "optimise", "budget": <glazed panels>, "generations": 60, "population": 40,
       "seed": 7, "threshold": 2.0, "pace": 1.0}

What it computes (a simulation, not a picture):
  * the studio work plane of the top floor (L4 + 0.75 m) under the 12 north-light lanterns, a 1.25 m sensor grid;
  * a CIE standard overcast sky, Reinhart MF:3 subdivision (1297 patches), luminance (1 + 2 sin a) / 3;
  * one ray per sensor and sky patch, traced through the model's own geometry (the app model = the Revit
    model's IFC export): the roof deck blocks everything outside the lanterns' light wells (the model's deck has
    no openings cut, the real building does), then the first surface hit above the deck decides: a lantern
    glazing panel passes the patch's light (visible transmittance 0.70), anything else blocks it;
  * sky component SC (%) per sensor = 100 * tau * sum(w over patches seen through glass) / sum(w over the sky).
    No inter-reflections (so it is the sky component, not the full daylight factor).
  * contribution matrix C[sensor][panel], so any layout of glazed panels is evaluated by a sum.
  * a genetic algorithm (population 40, tournament 3, uniform crossover with repair to the glass budget,
    swap mutation, elitism 2) that chooses WHICH panels stay glazed for a fixed number of glazed panels,
    maximising P5 = the sky component that 95 % of the studio floor reaches (the EN 17037 idea of a minimum
    level over 95 % of the area; a max-min objective, so ranking panels by their own light is not optimal),
    with 0.05 x the mean SC as a tie-break. Reported beside it: the room's rule (whole lanterns closed),
    a simple ranking (keep the panels that deliver the most light) and all panels glazed.
It draws live in the active Rhino viewport (heat map, panel states, a few traced rays, a heads-up readout) and
prints one JSON result between @@PLX@@ and @@END@@.
"""
import json
import math
import random
import time

import System
import clr
import Rhino
import Rhino.Geometry as G
import scriptcontext as sc
from Rhino.Geometry.Intersect import Intersection
from System.Drawing import Color

ARGS = PLX  # noqa: F821  (injected by the caller)
REPO = ARGS["repo"]
DECK = 18.26           # Roof level (top of the L4 studios' ceiling deck), Revit model frame, metres
WORK_PLANE = 13.51 + 0.75
GRID = 1.25
TAU = 0.70
STICKY = "plurarch_daylight_v3"
VIEW_NAME = "Plurarch daylight study"     # a floating viewport the screen recorder places beside the terminal
try:
    UI = System.Drawing.Graphics.FromHwnd(System.IntPtr.Zero).DpiX / 96.0
except Exception:
    UI = 1.0


def px(v):
    return int(round(v * UI))


# ------------------------------------------------------------------ model
def load_model():
    B = json.load(open(REPO + r"\site\models\langford\base.json"))
    E = json.load(open(REPO + r"\config\langford\elements.json"))
    raw = System.IO.File.ReadAllBytes(REPO + r"\site\models\langford\base.bin")
    q, lay = B["quantization"], B["layout"]
    n, off, ioff = lay["positions"]["count"], lay["positions"]["offset"], lay["indices"]["offset"]
    mn, scl = q["min"], q["scale"]
    BC = System.BitConverter
    P = [G.Point3d(mn[0] + BC.ToUInt16(raw, off + 6 * i) * scl[0], mn[1] + BC.ToUInt16(raw, off + 6 * i + 2) * scl[1],
                   mn[2] + BC.ToUInt16(raw, off + 6 * i + 4) * scl[2]) for i in range(n)]

    def element_mesh(e, mesh=None, owners=None, tag=None):
        mesh = mesh or G.Mesh()
        v0, vn = e["v"]
        i0, inn = e["i"]
        base = mesh.Vertices.Count
        for j in range(vn):
            mesh.Vertices.Add(P[v0 + j])
        for j in range(0, inn, 3):
            mesh.Faces.AddFace(base + BC.ToUInt16(raw, ioff + 2 * (i0 + j)), base + BC.ToUInt16(raw, ioff + 2 * (i0 + j + 1)),
                               base + BC.ToUInt16(raw, ioff + 2 * (i0 + j + 2)))
            if owners is not None:
                owners.append(tag)
        return mesh

    lanterns = sorted(E["skylights_open"]["lanterns"], key=lambda l: l["index"])
    panel_ids = sorted(i for l in lanterns for i in l["glazing_ids"])           # canonical panel order
    panel_index = dict((pid, k) for k, pid in enumerate(panel_ids))
    lantern_of = {}
    for l in lanterns:
        for pid in l["glazing_ids"]:
            lantern_of[panel_index[pid]] = l["index"]
    byid = dict((e["id"], e) for e in B["elements"])
    foot = []
    for l in lanterns:
        e = byid[l["roof_id"]]
        v0, vn = e["v"]
        xs = [P[v0 + j].X for j in range(vn)]
        ys = [P[v0 + j].Y for j in range(vn)]
        foot.append((min(xs), max(xs), min(ys), max(ys)))

    occ, owners = G.Mesh(), []          # everything above the deck that can block or pass light
    structure = G.Mesh()                # lantern frames, roofs and curbs (display)
    context = G.Mesh()                  # other roof volumes: penthouse, spine, parapets (display as wires)
    panels = [None] * len(panel_ids)
    deck = None
    for e in B["elements"]:
        roofish = e.get("lvl") in ("Roof", "T.O. Parapet", "L5 - Penthouse Roof") or e.get("q") == "skylights_open"
        if not roofish:
            continue
        if "Roof Deck" in (e.get("type") or ""):
            deck = element_mesh(e)
            continue
        v0, vn = e["v"]
        if max(P[v0 + j].Z for j in range(vn)) <= DECK + 0.05:
            continue
        k = panel_index.get(e["id"], -1)
        element_mesh(e, occ, owners, k)
        if k >= 0:
            panels[k] = element_mesh(e)
        else:
            xs = [P[v0 + j].X for j in range(vn)]
            ys = [P[v0 + j].Y for j in range(vn)]
            in_lantern = any(f[0] - 0.6 <= min(xs) and max(xs) <= f[1] + 0.6 and f[2] - 0.6 <= min(ys) and max(ys) <= f[3] + 0.6
                             for f in foot)
            element_mesh(e, structure if in_lantern else context)
    occ.Normals.ComputeNormals()
    structure.Normals.ComputeNormals()
    structure.Compact()
    context.Normals.ComputeNormals()
    for m in panels:
        m.Normals.ComputeNormals()
    xmin = min(f[0] for f in foot); xmax = max(f[1] for f in foot)
    ymin = min(f[2] for f in foot); ymax = max(f[3] for f in foot)
    nx = int(round((xmax - xmin) / GRID)) + 1
    ny = int(round((ymax - ymin) / GRID)) + 1
    sensors = [G.Point3d(xmin + i * (xmax - xmin) / (nx - 1), ymin + j * (ymax - ymin) / (ny - 1), WORK_PLANE)
               for j in range(ny) for i in range(nx)]
    outline = []
    if deck is not None:
        for pl in (deck.GetOutlines(G.Plane(G.Point3d(0, 0, DECK + 0.3), G.Vector3d.ZAxis)) or []):
            pl.Transform(G.Transform.Translation(0, 0, DECK + 0.3 - pl[0].Z))
            outline.append(pl)
    return {"panel_ids": panel_ids, "lantern_of": lantern_of, "lanterns": lanterns, "foot": foot, "occ": occ,
            "owners": owners, "panels": panels, "structure": structure, "context": context, "sensors": sensors, "nx": nx, "ny": ny,
            "outline": outline, "zone": (xmin, xmax, ymin, ymax)}


def sky_patches(mf=3):
    dirs, w = [], []
    counts = [30, 30, 24, 24, 18, 12, 6]
    for b, n in enumerate(counts):
        for s in range(mf):
            a1 = math.radians(12 * b + 12.0 * s / mf)
            a2 = math.radians(12 * b + 12.0 * (s + 1) / mf)
            m = n * mf
            daz = 2 * math.pi / m
            ac = (a1 + a2) / 2
            om = (math.sin(a2) - math.sin(a1)) * daz
            lum = (1 + 2 * math.sin(ac)) / 3.0
            for j in range(m):
                az = (j + 0.5) * daz
                dirs.append(G.Vector3d(math.cos(ac) * math.cos(az), math.cos(ac) * math.sin(az), math.sin(ac)))
                w.append(lum * math.sin(ac) * om)
    om = (1 - math.sin(math.radians(84))) * 2 * math.pi
    dirs.append(G.Vector3d(0, 0, 1))
    w.append(1.0 * 1.0 * om)
    return dirs, w


# ------------------------------------------------------------------ display (a conduit: no document churn)
def ramp(v, vmax=10.0):
    stops = [(0.0, (48, 18, 59)), (0.15, (70, 60, 180)), (0.3, (41, 120, 220)), (0.45, (24, 190, 200)),
             (0.6, (60, 220, 130)), (0.78, (190, 230, 60)), (1.0, (250, 250, 140))]
    t = max(0.0, min(1.0, v / vmax))
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            f = (t - t0) / (t1 - t0) if t1 > t0 else 0
            return Color.FromArgb(int(c0[0] + (c1[0] - c0[0]) * f), int(c0[1] + (c1[1] - c0[1]) * f), int(c0[2] + (c1[2] - c0[2]) * f))
    return Color.FromArgb(*stops[-1][1])


class Scene(Rhino.Display.DisplayConduit):
    def __init__(self, M):
        self.M = M
        self.values = None
        self.glazed = None
        self.rays = []
        self.lines = []
        self.big = ""
        self.history = []
        self.threshold = 2.0
        self.heat = None
        self.glass = Rhino.Display.DisplayMaterial(Color.FromArgb(120, 190, 255), 0.35)
        self.solid = Rhino.Display.DisplayMaterial(Color.FromArgb(150, 150, 155), 0.0)
        self.struct = Rhino.Display.DisplayMaterial(Color.FromArgb(215, 215, 210), 0.55)
        bb = M["occ"].GetBoundingBox(True)
        for s in M["sensors"]:
            bb.Union(s)
        self.bbox = bb

    def CalculateBoundingBox(self, e):
        e.IncludeBoundingBox(self.bbox)

    def set_values(self, values):
        M = self.M
        nx, ny = M["nx"], M["ny"]
        mesh = G.Mesh()
        for s in M["sensors"]:
            mesh.Vertices.Add(s)
        for j in range(ny - 1):
            for i in range(nx - 1):
                a = j * nx + i
                mesh.Faces.AddFace(a, a + 1, a + 1 + nx, a + nx)
        for k, v in enumerate(values):
            mesh.VertexColors.Add(ramp(v) if v is not None else Color.FromArgb(60, 60, 66))
        mesh.Normals.ComputeNormals()
        self.heat = mesh
        self.values = values

    def PostDrawObjects(self, e):
        d = e.Display
        if self.heat is not None:
            d.DrawMeshFalseColors(self.heat)
        for pl in (self.M["outline"] or []):
            d.DrawPolyline(pl, Color.FromArgb(160, 160, 170), 1)
        d.DrawMeshWires(self.M["context"], Color.FromArgb(175, 175, 182))
        d.DrawMeshShaded(self.M["structure"], self.struct)
        for k, m in enumerate(self.M["panels"]):
            on = self.glazed is None or self.glazed[k]
            d.DrawMeshShaded(m, self.glass if on else self.solid)
        for ln in self.rays:
            d.DrawLine(ln, Color.FromArgb(255, 214, 110), 1)

    def DrawForeground(self, e):
        try:
            self._fg(e)
        except Exception as ex:
            sc.sticky["plurarch_daylight_fg_error"] = repr(ex)

    def _fg(self, e):
        d = e.Display
        x, y = px(22), px(18)
        vpw = e.Viewport.Bounds.Width
        d.Draw2dRectangle(System.Drawing.Rectangle(x - px(12), y - px(10), min(vpw - px(20), px(640)),
                                                   px(30) + px(24) * len(self.lines) + (px(36) if self.big else 0)),
                          Color.FromArgb(0, 0, 0, 0), 0, Color.FromArgb(225, 14, 15, 18))
        d.Draw2dText("Plurarch · daylight study (Rhino 8, RhinoCommon ray tracing)", Color.FromArgb(160, 165, 175),
                     Rhino.Geometry.Point2d(x, y), False, px(13), "Segoe UI")
        yy = y + px(24)
        if self.big:
            d.Draw2dText(self.big, Color.White, Rhino.Geometry.Point2d(x, yy), False, px(20), "Segoe UI Semibold")
            yy += px(34)
        for text, col in self.lines:
            d.Draw2dText(text, col, Rhino.Geometry.Point2d(x, yy), False, px(14), "Segoe UI")
            yy += px(22)
        # legend
        vp = e.Viewport.Bounds
        lx, ly = vp.Width - px(310), vp.Height - px(62)
        d.Draw2dRectangle(System.Drawing.Rectangle(lx - px(12), ly - px(30), px(300), px(76)), Color.FromArgb(0, 0, 0, 0), 0, Color.FromArgb(225, 14, 15, 18))
        for i in range(40):
            v = 10.0 * i / 39
            d.Draw2dRectangle(System.Drawing.Rectangle(lx + i * px(6.6), ly, px(6.6) + 1, px(12)), ramp(v), 0, ramp(v))
        d.Draw2dText("sky component on the studio desks", Color.FromArgb(200, 200, 205), Rhino.Geometry.Point2d(lx, ly - px(22)), False, px(12), "Segoe UI")
        d.Draw2dText("0 %", Color.FromArgb(200, 200, 205), Rhino.Geometry.Point2d(lx, ly + px(16)), False, px(11), "Segoe UI")
        d.Draw2dText("10 %+", Color.FromArgb(200, 200, 205), Rhino.Geometry.Point2d(lx + px(234), ly + px(16)), False, px(11), "Segoe UI")
        tx = lx + int(self.threshold / 10.0 * px(264))
        d.Draw2dLine(System.Drawing.Point(int(tx), int(ly - px(3))), System.Drawing.Point(int(tx), int(ly + px(15))), Color.White, 2)
        d.Draw2dText("target %g %%" % self.threshold, Color.White, Rhino.Geometry.Point2d(tx + px(3), ly + px(16)), False, px(11), "Segoe UI")
        # GA curve
        if len(self.history) > 1:
            gx, gy, gw, gh = vp.Width - px(300), vp.Height - px(250), px(270), px(110)
            d.Draw2dRectangle(System.Drawing.Rectangle(gx - px(12), gy - px(12), px(294), gh + px(46)), Color.FromArgb(0, 0, 0, 0), 0, Color.FromArgb(225, 14, 15, 18))
            d.Draw2dText("GA: light reached by 95 % of the desks", Color.FromArgb(200, 200, 205), Rhino.Geometry.Point2d(gx, gy - px(4)), False, px(12), "Segoe UI")
            lo = min(self.history) * 0.9
            hi = max(self.history) * 1.05
            pts = []
            for i, v in enumerate(self.history):
                qx = gx + gw * i / max(1, ARGS.get("generations", 60))
                qy = gy + px(20) + gh - gh * (v - lo) / max(1e-6, hi - lo)
                pts.append(System.Drawing.Point(int(qx), int(qy)))
            for a, b in zip(pts, pts[1:]):
                d.Draw2dLine(a, b, Color.FromArgb(120, 230, 160), px(2))


def show(scene, wait=0.0):
    for v in sc.doc.Views:
        v.Redraw()
    Rhino.RhinoApp.Wait()
    if wait > 0:
        time.sleep(wait * ARGS.get("pace", 1.0))
        Rhino.RhinoApp.Wait()


def setup_view(M):
    view = None
    for v in sc.doc.Views:
        if v.ActiveViewport.Name == VIEW_NAME:
            view = v
    if view is None:
        rect = System.Drawing.Rectangle(px(880), px(20), px(1000), px(980))
        view = sc.doc.Views.Add(VIEW_NAME, Rhino.Display.DefinedViewportProjection.Perspective, rect, True)
    sc.doc.Views.ActiveView = view
    vp = view.ActiveViewport
    vp.ConstructionGridVisible = False
    vp.ConstructionAxesVisible = False
    vp.WorldAxesVisible = False
    xmin, xmax, ymin, ymax = M["zone"]
    cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
    target = G.Point3d(cx, cy, 15.5)
    north = G.Vector3d(math.sin(math.radians(39.35)), math.cos(math.radians(39.35)), 0)  # true north in the model frame
    cam = target + north * 44 + G.Vector3d(0, 0, 40)
    vp.ChangeToPerspectiveProjection(True, 30)
    vp.SetCameraLocations(target, cam)
    try:
        mode = Rhino.Display.DisplayModeDescription.FindByName("Shaded")
        if mode:
            vp.DisplayMode = mode
    except Exception:
        pass
    view.Redraw()


# ------------------------------------------------------------------ simulation
def trace(M, scene):
    dirs, w = sky_patches(3)
    wtot = sum(w)
    sensors, foot, occ, owners = M["sensors"], M["foot"], M["occ"], M["owners"]
    npan = len(M["panel_ids"])
    C = [[0.0] * len(sensors) for _ in range(npan)]   # C[panel][sensor]
    ref = clr.Reference[System.Array[int]]()
    rays = 0
    gaps = 0
    rnd = random.Random(3)
    values = [None] * len(sensors)
    t0 = time.time()
    for si, o in enumerate(sensors):
        dz = DECK - o.Z
        seen = 0.0
        for di, d in enumerate(dirs):
            t = dz / d.Z
            hx, hy = o.X + d.X * t, o.Y + d.Y * t
            inside = False
            for f in foot:
                if f[0] <= hx <= f[1] and f[2] <= hy <= f[3]:
                    inside = True
                    break
            if not inside:
                continue
            rays += 1
            hit = Intersection.MeshRay(occ, G.Ray3d(o, d), ref)
            if hit < 0:
                gaps += 1
                continue
            k = owners[ref.Value[0]]
            if k >= 0:
                C[k][si] += w[di]
                seen += w[di]
                if rnd.random() < 0.004 and len(scene.rays) < 260:
                    scene.rays.append(G.Line(o, o + d * hit))
        values[si] = 100.0 * TAU * seen / wtot
        if si % 24 == 0 or si == len(sensors) - 1:
            scene.set_values(values)
            scene.lines = [("Ray tracing a CIE overcast sky: %d sky patches x %d sensors" % (len(dirs), len(sensors)), Color.White),
                           ("%d rays through the light wells so far" % rays, Color.FromArgb(200, 200, 205))]
            show(scene, 0.07)
    k = 100.0 * TAU / wtot
    for col in C:
        for i in range(len(col)):
            col[i] *= k
    return {"C": C, "rays": rays, "gaps": gaps, "seconds": round(time.time() - t0, 1), "patches": len(dirs), "wtot": wtot}


def evaluate(C, glazed, threshold):
    n = len(C[0])
    cols = [C[k] for k in range(len(C)) if glazed[k]]
    if not cols:
        vals = [0.0] * n
    else:
        vals = map(sum, zip(*cols))
    vals = list(vals)
    lit = sum(1 for v in vals if v >= threshold)
    mean = sum(vals) / n
    srt = sorted(vals)
    return {"values": vals, "daylit_pct": 100.0 * lit / n, "mean_sc": mean, "p5_sc": srt[int(0.05 * n)], "min_sc": srt[0]}


def summary(r, glazed, M):
    per = [0] * 12
    for k, g in enumerate(glazed):
        if g:
            per[M["lantern_of"][k]] += 1
    return {"daylit_area_pct": round(r["daylit_pct"], 1), "mean_sky_component_pct": round(r["mean_sc"], 2),
            "p5_sky_component_pct": round(r["p5_sc"], 2), "min_sky_component_pct": round(r["min_sc"], 2),
            "glazed_panels": sum(1 for g in glazed if g), "per_lantern": per}


def mask_hex(glazed):
    bits = "".join("1" if g else "0" for g in glazed)
    bits += "0" * ((-len(bits)) % 4)
    return "".join("%x" % int(bits[i:i + 4], 2) for i in range(0, len(bits), 4))


def optimise(M, scene, sim):
    C = sim["C"]
    n = len(C)
    budget = int(ARGS["budget"])
    thr = float(ARGS.get("threshold", 2.0))
    gens = int(ARGS.get("generations", 60))
    popn = int(ARGS.get("population", 40))
    rnd = random.Random(int(ARGS.get("seed", 7)))
    scene.threshold = thr
    lan_n = 12
    per = n // lan_n

    def fitness(r):
        return r["p5_sc"] + 0.05 * r["mean_sc"]

    # the room's rule: whole lanterns, the first k by the standard ordering (lantern index)
    k_whole = budget // per
    standard = [M["lantern_of"][k] < k_whole for k in range(n)]
    allg = [True] * n
    r_all = evaluate(C, allg, thr)
    r_std = evaluate(C, standard, thr)
    scene.glazed = standard
    scene.set_values(r_std["values"])
    scene.rays = []
    scene.big = "The room's rule: close %d whole lanterns" % (lan_n - k_whole)
    scene.lines = [("%d of %d panels glazed: 95 %% of the floor gets %.2f %% sky component (today %.2f %%)" % (budget, n, r_std["p5_sc"], r_all["p5_sc"]), Color.White),
                   ("daylit area (>= %g %%): %.0f %% (today %.0f %%) · mean %.1f %%" % (thr, r_std["daylit_pct"], r_all["daylit_pct"], r_std["mean_sc"]), Color.FromArgb(170, 175, 185))]
    show(scene, 2.5)

    def repair(g):
        on = [k for k in range(n) if g[k]]
        off = [k for k in range(n) if not g[k]]
        rnd.shuffle(on)
        rnd.shuffle(off)
        while len(on) > budget:
            g[on.pop()] = False
        while len(on) < budget:
            k = off.pop()
            g[k] = True
            on.append(k)
        return g

    def random_ind():
        g = [False] * n
        for k in rnd.sample(range(n), budget):
            g[k] = True
        return g

    ranked_order = sorted(range(n), key=lambda k: -sum(C[k]))   # reference: keep the panels that give most light
    ranked = [False] * n
    for k in ranked_order[:budget]:
        ranked[k] = True
    r_rank = evaluate(C, ranked, thr)
    pop = [standard[:]] + [random_ind() for _ in range(popn - 1)]
    scored = [(fitness(evaluate(C, g, thr)), g) for g in pop]
    evals = len(scored)
    best_f, best_g = max(scored, key=lambda t: t[0])
    scene.history = []
    t0 = time.time()
    for gen in range(1, gens + 1):
        scored.sort(key=lambda t: -t[0])
        new = [scored[0], scored[1]]
        while len(new) < popn:
            a = max(rnd.sample(scored, 3), key=lambda t: t[0])[1]
            b = max(rnd.sample(scored, 3), key=lambda t: t[0])[1]
            child = repair([a[k] if rnd.random() < 0.5 else b[k] for k in range(n)])
            for _ in range(rnd.randint(1, 3)):
                on = [k for k in range(n) if child[k]]
                off = [k for k in range(n) if not child[k]]
                i, j = rnd.choice(on), rnd.choice(off)
                child[i], child[j] = False, True
            new.append((fitness(evaluate(C, child, thr)), child))
            evals += 1
        scored = new
        f, g = max(scored, key=lambda t: t[0])
        if f > best_f:
            best_f, best_g = f, g
        rb = evaluate(C, best_g, thr)
        scene.history.append(rb["p5_sc"])
        scene.glazed = best_g
        scene.set_values(rb["values"])
        scene.big = "Genetic algorithm · generation %d / %d" % (gen, gens)
        scene.lines = [("same glass (%d of %d panels): 95 %% of the floor gets %.2f %% · mean %.1f %%" % (budget, n, rb["p5_sc"], rb["mean_sc"]), Color.White),
                       ("room's rule: %.2f %% · population %d · %d layouts tested" % (r_std["p5_sc"], popn, evals), Color.FromArgb(170, 175, 185))]
        show(scene, 0.05)
    if r_rank["p5_sc"] + 0.05 * r_rank["mean_sc"] > best_f:   # never report a GA result worse than the ranking
        best_g = ranked
    rb = evaluate(C, best_g, thr)
    scene.glazed = best_g
    scene.set_values(rb["values"])
    scene.big = "Same glass: 95 %% of the desks get %.1f %% (closing lanterns: %.1f %%)" % (rb["p5_sc"], r_std["p5_sc"])
    scene.lines = [("today, all %d panels: %.1f %%  ·  simple ranking: %.2f %%  ·  GA: %.2f %%" % (n, r_all["p5_sc"], r_rank["p5_sc"], rb["p5_sc"]), Color.White),
                   ("daylit area %.0f %% (was %.0f %%) · %d layouts tested out of ~1e%d" % (rb["daylit_pct"], r_std["daylit_pct"], evals, int(round(log10_comb(n, budget)))), Color.FromArgb(170, 175, 185))]
    show(scene, 0.5)
    return {
        "all_glazed": summary(r_all, allg, M), "standard": summary(r_std, standard, M),
        "ranked": summary(r_rank, ranked, M), "best": summary(rb, best_g, M),
        "best_mask": mask_hex(best_g), "standard_mask": mask_hex(standard), "ranked_mask": mask_hex(ranked),
        "ga": {"population": popn, "generations": gens, "evaluations": evals, "seed": int(ARGS.get("seed", 7)),
               "objective": "maximise P5 sky component (level reached by 95 % of the floor) + 0.05 x mean",
               "history_p5_pct": [round(v, 2) for v in scene.history], "seconds": round(time.time() - t0, 1),
               "search_space_log10": round(log10_comb(n, budget), 1)},
    }


def log10_comb(n, k):
    return (math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)) / math.log(10)


def main():
    st = sc.sticky.get(STICKY)
    if st is None:
        st = {"M": load_model()}
        sc.sticky[STICKY] = st
    M = st["M"]
    for key in list(sc.sticky.keys()):          # retire every earlier study display (any engine version)
        prev = sc.sticky.get(key)
        if str(key).startswith("plurarch_daylight") and isinstance(prev, dict) and prev.get("scene") is not None:
            prev["scene"].Enabled = False
    sc.sticky["plurarch_daylight_fg_error"] = None
    scene = Scene(M)
    st["scene"] = scene
    scene.Enabled = True
    try:
        sc.doc.ModelUnitSystem = Rhino.UnitSystem.Meters
    except Exception:
        pass
    setup_view(M)
    if ARGS.get("action") == "preview":   # the idle scene before a study: lanterns as built, no results yet
        scene.big = "Rhino 8 · Langford A roof lanterns (as built: 168 glass panels)"
        scene.lines = [("waiting for the reviewer agent to ask for a daylight study", Color.FromArgb(170, 175, 185))]
        show(scene)
        emit({"preview": True, "sensors": len(M["sensors"])})
        return
    scene.big = "Top-floor studios under the 12 north-light lanterns"
    scene.lines = [("Langford A, Revit model geometry · work plane L4 + 0.75 m · %d sensors on a %.2f m grid" % (len(M["sensors"]), GRID), Color.White)]
    show(scene, 0.8)
    sim = trace(M, scene)
    out = {"engine": "Rhino %s, RhinoCommon MeshRay" % Rhino.RhinoApp.Version, "sensors": len(M["sensors"]),
           "grid_m": GRID, "sky": "CIE standard overcast, Reinhart MF:3 (%d patches)" % sim["patches"],
           "rays_traced": sim["rays"], "ray_gaps": sim["gaps"], "ray_seconds": sim["seconds"], "tau": TAU,
           "threshold_sc_pct": float(ARGS.get("threshold", 2.0)), "panels": len(M["panel_ids"]),
           "budget": int(ARGS["budget"]), "panel_order": "ascending Revit element id of the 168 lantern glazing panels"}
    if ARGS.get("action") == "optimise":
        out.update(optimise(M, scene, sim))
    emit(out)


def emit(obj):
    path = ARGS.get("out")
    if path:
        with open(path, "w") as f:
            f.write(json.dumps(obj))
        print("Plurarch daylight study: done")
    else:
        print("@@PLX@@" + json.dumps(obj) + "@@END@@")


main()
