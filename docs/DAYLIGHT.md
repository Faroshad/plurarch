# Daylight study for the roof lanterns (Rhino 8 + genetic algorithm)

The room votes **how much** roof glass stays (`skylights_open`, lanterns' worth of glass). The reviewer agent can
ask Rhino **where** that glass should go: `optimise_skylight_layout` (design-mcp) runs
`rhino/plurarch_daylight.py` inside Rhino 8 through the RhinoMCP socket (`design_mcp/rhino_bridge.py`) and
returns a panel layout for the same glass area. The layout is the optional, non-voted parameter
`skylight_layout` (`design_mcp/skylights.py`); Revit, the verification and the phones all use it.

## Method (a simulation, not a visualisation)
| | |
|---|---|
| Geometry | the app model = the Revit model's IFC export (`site/models/langford/base.*`), Revit model frame, metres |
| Sensors | top-floor studio work plane, L4 + 0.75 m = 14.26 m, 1.25 m grid over the lanterns' footprint hull (418 points) |
| Sky | CIE standard overcast, L(α) ∝ (1 + 2 sin α)/3, Reinhart MF:3 subdivision (1297 patches) |
| Rays | one per sensor and patch: the roof deck (z 18.26 m) blocks everything outside the 12 lantern light wells; inside a light well the first surface hit above the deck (RhinoCommon `MeshRay`, 9,944 faces) decides: a lantern glazing panel passes the patch, anything else blocks it |
| Metric | sky component SC(%) = 100 · τ · Σ w(visible through glass) / Σ w(whole sky), τ = 0.70, w = L(α) sin α dΩ |
| Matrix | C[panel][sensor], so a layout is evaluated by summing the columns of its glazed panels |
| Objective | maximise P5 = the SC reached by 95 % of the floor (the EN 17037 idea of a minimum level over 95 % of the area), tie-break 0.05 × mean SC; daylit area = share of sensors with SC ≥ 2 % |
| Search | genetic algorithm: population 40, 80 generations, tournament 3, uniform crossover with repair to the glass budget, 1–3 swap mutations, elitism 2, seed 7 (3,080 layouts of C(168,112) ≈ 1e45) |

Assumptions, stated wherever results are shown: sky component only (no inter-reflections, so not the full
daylight factor); the deck is open under each lantern and closed elsewhere (the model's deck has no openings
cut, the building does); only glass passes light (16 % of light-well rays escape through unmodelled gaps and are
counted as blocked); angle-independent glass transmittance.

## Checks
- Sky integration: Σ w over the 1297 patches = 2.4448 against the analytic 7π/9 = 2.4435 (0.05 %).
- An independent numpy implementation (global first hit over all 29,608 triangles, 1.5 m grid) reproduces the
  same ordering: closing whole lanterns P5 1.21 %, ranking 3.25 %, GA 3.45 %, all glass 3.66 %.
- Same glass ⇒ same proxies: `evaluate` (cooling, cost, carbon) depends on the amount of glass only
  (`tests/test_skylights.py`).

## Result (8 of 12 lanterns' worth of glass, 112 of 168 panels; Rhino engine, 1.25 m grid)
| | P5 (light reached by 95 % of desks) | mean SC | daylit area (SC ≥ 2 %) |
|---|---|---|---|
| today, all 168 panels | 3.14 % | 7.58 % | 98.1 % |
| room's rule: close 4 whole lanterns | 0.97 % | 4.84 % | 87.8 % |
| simple ranking (keep the brightest panels) | 2.82 % | 6.78 % | 97.6 % |
| genetic algorithm | **3.11 %** | 6.21 % | 98.1 % |

With two thirds of the glass, the darkest desks keep 99 % of today's light; closing whole lanterns gives them
less than a third of it. The GA beats the simple ranking by 10 % on P5, because P5 is a max-min objective that
ranking panels one by one cannot see. Runtime in Rhino: ~1 s ray tracing (206,562 rays), ~7–9 s GA.
