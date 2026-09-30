"""Indicative performance metrics for the Plurarch pavilion.

These are TRANSPARENT PROXIES, not simulations. Each one is a short, deterministic formula of the
four design parameters, chosen so that the trade-offs are easy to explain in a lecture:
more glass brings daylight but also heat; shading (roof overhang + canopy) cuts heat but costs
money and carbon; materials differ in heat gain, cost and embodied carbon.

Inputs (validated before they get here):
    facade_material  one of "timber", "concrete", "glass"
    window_ratio     20..60 (% of the facade that is glazing)
    roof_angle       0..35 (degrees; a steeper mono-pitch roof overhangs more)
    canopy_depth     0..3 (m; entrance canopy on the front facade)

Every output is rounded (indices to 0.1, shading to 0.001) so the numbers the agent cites are
exactly the numbers the tools print.
"""
from __future__ import annotations

import math

# Material properties (relative, dimensionless). Documented in the README.
#   heat:   share of solar heat that the opaque part of the facade lets in
#           (a glass facade's opaque spandrel panels still heat up a lot)
#   cost:   cost of 1 m2 of opaque facade, relative to timber
#   carbon: embodied carbon of 1 m2 of opaque facade, relative units
#   light:  extra daylight from reflective / translucent facade elements
MATERIALS = {
    "timber":   {"heat": 0.15, "cost": 1.00, "carbon": 0.30, "light": 0.00},
    "concrete": {"heat": 0.20, "cost": 0.80, "carbon": 1.40, "light": 0.00},
    "glass":    {"heat": 0.55, "cost": 1.20, "carbon": 1.00, "light": 0.03},
}

GLAZING_COST = 1.30    # 1 m2 of glazing costs 1.3x an m2 of timber facade
GLAZING_CARBON = 0.90  # relative embodied carbon of 1 m2 of glazing (frames, glass)


def shading_fraction(roof_angle: float, canopy_depth: float) -> float:
    """Share of the glazing shaded from direct sun (0..0.6).

    roof overhang: 0.12 at a flat roof, +0.005 per degree (0.295 at 35 deg)
    canopy:        the canopy shades the front facade (~32% of all facade area) with
                   ~80% effectiveness once it is 2.5 m deep: 0.256 * min(1, depth / 2.5)
    """
    s_roof = 0.12 + 0.005 * roof_angle
    s_canopy = 0.256 * min(1.0, canopy_depth / 2.5)
    return min(0.6, s_roof + s_canopy)


def daylight_index(window_ratio: float, shading: float, material: str) -> float:
    """Daylight index, 0..100 (higher is better). Saturating curve of effective glazing:

        g = w * (1 - 0.35 * S) + light_bonus
        daylight = 100 * (1 - exp(-3.2 * g))

    w = window_ratio / 100, S = shading fraction. Shading removes some light; more glazing gives
    diminishing returns.
    """
    w = window_ratio / 100.0
    g = w * (1.0 - 0.35 * shading) + MATERIALS[material]["light"]
    return 100.0 * (1.0 - math.exp(-3.2 * g))


def cooling_index(window_ratio: float, shading: float, material: str) -> float:
    """Cooling-load index (lower is better). Solar gain through glazing plus the opaque facade:

        cooling = 20 + 110 * w * (1 - S) + 40 * (1 - w) * heat_m

    20 = internal gains (people, equipment). Unshaded glazing dominates in a hot climate.
    """
    w = window_ratio / 100.0
    return 20.0 + 110.0 * w * (1.0 - shading) + 40.0 * (1.0 - w) * MATERIALS[material]["heat"]


def cost_index(window_ratio: float, roof_angle: float, canopy_depth: float, material: str) -> float:
    """Cost index (budget = 100). Structure + facade + roof + canopy:

        cost = 25 + 40 * ((1 - w) * cost_m + w * 1.3) + 12 * (1 + 0.6 * angle / 35) + 4 * depth

    25 = structure and floor, a steeper roof has more area and complexity, the canopy costs
    4 per metre of depth.
    """
    w = window_ratio / 100.0
    facade = 40.0 * ((1.0 - w) * MATERIALS[material]["cost"] + w * GLAZING_COST)
    roof = 12.0 * (1.0 + 0.6 * roof_angle / 35.0)
    canopy = 4.0 * canopy_depth
    return 25.0 + facade + roof + canopy


def carbon_index(window_ratio: float, roof_angle: float, canopy_depth: float, material: str) -> float:
    """Embodied-carbon index (lower is better):

        carbon = 20 + 40 * ((1 - w) * carbon_m + w * 0.9) + 10 * (1 + 0.5 * angle / 35) + 3 * depth

    Concrete is carbon-heavy, timber is light, glazing sits in between.
    """
    w = window_ratio / 100.0
    facade = 40.0 * ((1.0 - w) * MATERIALS[material]["carbon"] + w * GLAZING_CARBON)
    roof = 10.0 * (1.0 + 0.5 * roof_angle / 35.0)
    canopy = 3.0 * canopy_depth
    return 20.0 + facade + roof + canopy


def compute_metrics(p: dict) -> dict:
    """All metrics for one validated parameter set, rounded for display and citation."""
    m = p["facade_material"]
    w = float(p["window_ratio"])
    a = float(p["roof_angle"])
    c = float(p["canopy_depth"])
    s = shading_fraction(a, c)
    return {
        "daylight": round(daylight_index(w, s, m), 1),
        "cooling": round(cooling_index(w, s, m), 1),
        "cost": round(cost_index(w, a, c, m), 1),
        "carbon": round(carbon_index(w, a, c, m), 1),
        "shading": round(s, 3),
    }


METRIC_INFO = {
    "daylight": {"label": "Daylight", "unit": "index", "better": "higher"},
    "cooling": {"label": "Cooling load", "unit": "index", "better": "lower"},
    "cost": {"label": "Cost", "unit": "index", "better": "lower"},
    "carbon": {"label": "Embodied carbon", "unit": "index", "better": "lower"},
    "shading": {"label": "Shading fraction", "unit": "0-1", "better": "higher"},
}
