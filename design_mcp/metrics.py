"""Indicative performance metrics for the Langford A studio-facade retrofit.

These are TRANSPARENT PROXIES, not simulations. Every formula is short, deterministic and uses the REAL
quantities of the Revit model (config/langford/elements.json, generated from the P1 model):

    SE studio lites      142 panels, 382.8 m2 of glass, facing azimuth 140.65 deg (SE, the quad)
    fins                 30 anchors on the SE glass, 2 per bay, spacing 3.77 m (piers act as bay-end fins)
    roof lanterns        12 north-light lanterns, 29.9 m2 of glazing each (359.3 m2)
    solid panels now     14 penthouse louvre panels (18.2 m2)

Which panels are glazed for a given share comes from the plan rule (design_mcp/langford_plan.py), so the
areas here are the areas of the actual Revit panels that would be glass or solid.

Inputs (validated before they get here):
    infill_finish    "concrete" | "aluminium" | "fritted_glass"
    se_glass_share   40..100 (% of the 142 SE lites that stay clear glass)
    fin_depth        0..1.2 (m)
    skylights_open   0..12 (lanterns that stay glazed)

Normalised quantities used below (all 0..1):
    g      glazed SE area / all SE lite area (382.8 m2)
    g_fin  glazed SE area in the finned bays / 382.8 (L2 bay 2, the entrance, has no fins)
    s_se   SE area turned solid / 382.8 (= 1 - g)
    k      open lantern glazing / all lantern glazing (= skylights_open / 12)
    s_sky  closed lantern glazing / all lantern glazing (= 1 - k)
    F      fin shading fraction: share of the beam sun on the finned SE glass that the fins block
           (see fin_shading below; 0.08 / 0.16 / 0.24 / 0.32 for 0.3 / 0.6 / 0.9 / 1.2 m)

Every output is rounded (indices to 0.1, fractions to 0.001, areas to 0.1 m2) so the numbers the agent
cites are exactly the numbers the tools print.
"""
from __future__ import annotations

import math

from design_mcp import langford_plan

E = langford_plan.load_elements()
SE_PANELS = {p["id"]: p for p in E["se_glass_share"]["panels"]}
G_FULL = sum(p["area_m2"] for p in SE_PANELS.values())                      # 382.8 m2
FINNED_HOSTS = {a["host"] for a in E["fin_depth"]["anchors"]}
FIN_SPACING = E["fin_depth"]["anchors"][0]["spacing_m"]                      # 3.77 m
K_LANTERN = {l["index"]: l["glazing_area_m2"] for l in E["skylights_open"]["lanterns"]}
K_FULL = sum(K_LANTERN.values())                                             # 359.3 m2
LOUVRE_M2 = E["quantities"]["solid_panel_area_now_m2"]                       # 18.2 m2

LATITUDE = 30.62          # College Station, TX
FACADE_AZIMUTH = 140.65   # SE facade normal (deg from north, clockwise) = model X bearing 50.65 + 90

# Finish properties (relative, dimensionless; documented in the README):
#   heat      solar heat the solid panel adds to the studio, per unit of solid area relative to the SE lites
#             (concrete: heavy, shaded by its own mass; aluminium: thin hot metal skin; fritted glass still
#             transmits ~45 % of the sun)
#   light     diffuse daylight a solid panel still lets through, relative to clear glass
#   cost      cost of 1 m2 of new solid panel, relative to precast concrete
#   carbon    embodied carbon of 1 m2 of new solid panel, relative to precast concrete
#   heritage  (base penalty, penalty per unit solid area) for the fit with the 1970s bush-hammered concrete
FINISHES = {
    "concrete":      {"heat": 3.0,  "light": 0.00, "cost": 1.00, "carbon": 1.00, "heritage": (0.0, 0.0)},
    "aluminium":     {"heat": 9.0,  "light": 0.00, "cost": 1.45, "carbon": 1.90, "heritage": (18.0, 25.0)},
    "fritted_glass": {"heat": 16.0, "light": 0.40, "cost": 1.25, "carbon": 1.15, "heritage": (4.0, 8.0)},
}


def _sun_positions():
    """Clear-sky sun positions for the hot season in College Station: the 21st of May..September,
    every 30 min of solar time, only while the sun is in front of the SE facade.
    Weight = beam irradiance on the vertical facade: DNI (Meinel: 1000 * 0.7^(AM^0.678)) * cos(incidence)."""
    lat = math.radians(LATITUDE)
    out = []
    for day in (141, 172, 203, 234, 264):
        decl = math.radians(23.45 * math.sin(math.radians(360.0 / 365.0 * (284 + day))))
        for i in range(29):
            hour = 5.0 + 0.5 * i
            ha = math.radians(15.0 * (hour - 12.0))
            s_alt = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(ha)
            if s_alt <= 0.02:
                continue
            alt = math.asin(s_alt)
            az = math.degrees(math.atan2(-math.sin(ha) * math.cos(decl),
                                         math.cos(lat) * math.sin(decl) - math.sin(lat) * math.cos(decl) * math.cos(ha))) % 360
            gamma = (az - FACADE_AZIMUTH + 180.0) % 360.0 - 180.0   # horizontal angle to the facade normal
            if abs(gamma) >= 89.9:
                continue
            dni = 1000.0 * 0.7 ** ((1.0 / s_alt) ** 0.678)
            out.append((math.radians(gamma), dni * math.cos(alt) * math.cos(math.radians(gamma))))
    return out


SUN = _sun_positions()
SUN_WEIGHT = sum(w for _, w in SUN)


def fin_shading(depth: float, spacing: float = FIN_SPACING) -> float:
    """Share of the beam sun on the finned SE glass blocked by vertical fins (0..1).

    A vertical fin of depth d at spacing s shades min(1, d * |tan(gamma)| / s) of the glass width, gamma =
    the horizontal angle between the sun and the facade normal. Averaged over the hot-season sun positions,
    weighted by beam irradiance on the facade. The existing stepped overhangs (constant in every design)
    are part of the calibration constants below."""
    if depth <= 0:
        return 0.0
    return sum(w * min(1.0, depth * abs(math.tan(g)) / spacing) for g, w in SUN) / SUN_WEIGHT


def quantities(p: dict) -> dict:
    """Real areas for one parameter set, from the plan rule."""
    pl = langford_plan.plan(p, E)
    glazed = [SE_PANELS[i] for i in pl["se_glazed"]]
    g_m2 = sum(x["area_m2"] for x in glazed)
    g_fin_m2 = sum(x["area_m2"] for x in glazed if x["mark"] in FINNED_HOSTS)
    k_m2 = sum(K_LANTERN[i] for i in pl["lanterns_open"])
    return {"g_m2": g_m2, "g_fin_m2": g_fin_m2, "se_solid_m2": G_FULL - g_m2, "k_m2": k_m2,
            "sky_solid_m2": K_FULL - k_m2, "closed_lanterns": 12 - len(pl["lanterns_open"]),
            "fins": len(pl["fins"])}


def daylight_index(g, g_fin, s_se, k, s_sky, F, finish) -> float:
    """Studio daylight index, 0..100 (higher is better). Saturating curve of the effective aperture:

        a = 1.9 * (g - 0.30 * F * g_fin) + 0.6 * k + light_f * (1.9 * s_se + 0.6 * s_sky)
        daylight = 100 * (1 - exp(-a))

    The SE ribbon windows light four floors of studios (weight 1.9), the lanterns only the top floor
    (0.6). Fins also cut some diffuse light (0.30 of what they block). Fritted solid panels still pass
    some light (light_f = 0.40 of clear glass)."""
    lf = FINISHES[finish]["light"]
    a = 1.9 * (g - 0.30 * F * g_fin) + 0.6 * k + lf * (1.9 * s_se + 0.6 * s_sky)
    return 100.0 * (1.0 - math.exp(-a))


def cooling_index(g, g_fin, s_se, k, s_sky, F, finish) -> float:
    """Cooling-load index (lower is better):

        cooling = 28 + 36 * (g - F * g_fin) + 9 * k + heat_f * (s_se + 0.6 * s_sky)

    28 = internal gains (studios full of people, screens, lighting). 36 = beam sun through the SE glass in
    College Station's hot, humid summer (after the existing overhangs); fins remove the share F on the
    finned bays. 9 = the north-light lanterns (mostly diffuse gain). Solid panels add heat by finish."""
    hf = FINISHES[finish]["heat"]
    return 28.0 + 36.0 * (g - F * g_fin) + 9.0 * k + hf * (s_se + 0.6 * s_sky)


def cost_index(depth, s_se, s_sky, finish) -> float:
    """Retrofit cost index (the budget is 100):

        cost = 35 + 26 * depth / 1.2 + cost_f * (40 * s_se + 20 * s_sky)

    35 = the base facade refurbishment (re-sealing, repairs, access). 26 = the 30 precast concrete fins
    at full depth (1.2 m, ~25 m3). New solid panels cost by area and finish; closing a lantern is half
    the rate per unit (smaller, simpler panels)."""
    return 35.0 + 26.0 * depth / 1.2 + FINISHES[finish]["cost"] * (40.0 * s_se + 20.0 * s_sky)


def carbon_index(depth, s_se, s_sky, finish) -> float:
    """Embodied-carbon index of the retrofit (lower is better):

        carbon = 15 + 30 * depth / 1.2 + carbon_f * (40 * s_se + 20 * s_sky)

    Concrete fins are carbon-heavy (30 at full depth); aluminium panels have about twice the embodied
    carbon of precast concrete per m2."""
    return 15.0 + 30.0 * depth / 1.2 + FINISHES[finish]["carbon"] * (40.0 * s_se + 20.0 * s_sky)


def heritage_index(depth, s_se, closed_lanterns, finish) -> float:
    """Heritage-fit index, 0..100 (higher is better): how well the retrofit keeps the character of the
    1970s brutalist building (bush-hammered concrete, continuous ribbon windows, the sawtooth lanterns):

        heritage = 100 - (base_f + per_f * s_se) - 22 * s_se - 2.2 * closed_lanterns - 6 * depth / 1.2

    Aluminium is foreign to the building (base 18, +25 per unit solid area); fritted glass fits better;
    concrete matches. Losing ribbon glass, closing lanterns and very deep fins also change the character."""
    base, per = FINISHES[finish]["heritage"]
    return 100.0 - (base + per * s_se) - 22.0 * s_se - 2.2 * closed_lanterns - 6.0 * depth / 1.2


def compute_metrics(p: dict) -> dict:
    """All metrics for one validated parameter set, rounded for display and citation."""
    q = quantities(p)
    finish = p["infill_finish"]
    depth = float(p["fin_depth"])
    g, g_fin, s_se = q["g_m2"] / G_FULL, q["g_fin_m2"] / G_FULL, q["se_solid_m2"] / G_FULL
    k, s_sky = q["k_m2"] / K_FULL, q["sky_solid_m2"] / K_FULL
    F = fin_shading(depth)
    return {
        "daylight": round(daylight_index(g, g_fin, s_se, k, s_sky, F, finish), 1),
        "cooling": round(cooling_index(g, g_fin, s_se, k, s_sky, F, finish), 1),
        "cost": round(cost_index(depth, s_se, s_sky, finish), 1),
        "carbon": round(carbon_index(depth, s_se, s_sky, finish), 1),
        "heritage": round(heritage_index(depth, s_se, q["closed_lanterns"], finish), 1),
        "shading": round(F, 3),
        "se_glass_m2": round(q["g_m2"], 1),
        "skylight_glass_m2": round(q["k_m2"], 1),
    }


METRIC_INFO = {
    "daylight": {"label": "Studio daylight", "unit": "index", "better": "higher"},
    "cooling": {"label": "Cooling load", "unit": "index", "better": "lower"},
    "cost": {"label": "Retrofit cost", "unit": "index", "better": "lower"},
    "carbon": {"label": "Embodied carbon", "unit": "index", "better": "lower"},
    "heritage": {"label": "Heritage fit", "unit": "index", "better": "higher"},
    "shading": {"label": "Fin shading fraction", "unit": "0-1", "better": "higher"},
    "se_glass_m2": {"label": "SE studio glass", "unit": "m2", "better": "info"},
    "skylight_glass_m2": {"label": "Lantern glass", "unit": "m2", "better": "info"},
}
