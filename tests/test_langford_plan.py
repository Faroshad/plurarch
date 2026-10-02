"""The Langford plan rule (design_mcp/langford_plan.py): ranking, plan, diff, and Python <-> JS parity.

    .venv\\Scripts\\python.exe -m unittest tests.test_langford_plan -v
    .venv\\Scripts\\python.exe tests\\test_langford_plan.py --write     # regenerate tests/model/langford_plans.json

The JS half (site/js/model/langford.js planLangford) is checked by tests/model/test_langford_parity.mjs
against the same JSON; ParityTest runs it through node when node is installed.
"""
import itertools
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from design_mcp import core, langford_plan as lp  # noqa: E402

PLANS_JSON = REPO / "tests" / "model" / "langford_plans.json"
PARITY_MJS = REPO / "tests" / "model" / "test_langford_parity.mjs"


def P(f, s, d, k):
    return {"infill_finish": f, "se_glass_share": s, "fin_depth": d, "skylights_open": k}


CASES = [P("concrete", 100, 0, 12), P("concrete", 90, 0.6, 12), P("aluminium", 70, 0.3, 10),
         P("fritted_glass", 50, 1.2, 4), P("concrete", 40, 0.9, 0), P("aluminium", 80, 0.6, 2),
         P("fritted_glass", 60, 0, 6), P("concrete", 100, 1.2, 8)]


def expected_plan(params: dict) -> dict:
    p = lp.plan(params)
    return {"params": params, "se_glazed_count": p["se_glazed_count"],
            "se_glazed": sorted(p["se_glazed"]), "se_solid": sorted(p["se_solid"]),
            "lanterns_open": p["lanterns_open"], "lanterns_open_marks": p["lanterns_open_marks"],
            "fins": [{"mark": f["mark"], "start": f["start"], "end": f["end"], "height": f["height"]} for f in p["fins"]],
            "finish": p["finish"], "material": p["material"]}


def write_plans() -> None:
    data = {"note": "Expected plans from design_mcp/langford_plan.py for the Python <-> JS parity test "
                    "(tests/model/test_langford_parity.mjs). Regenerate: python tests/test_langford_plan.py --write",
            "se_n": len(lp.load_elements()["se_glass_share"]["panels"]),
            "cases": [expected_plan(c) for c in CASES]}
    PLANS_JSON.write_text(json.dumps(data, indent=1), encoding="utf-8")
    print("wrote", PLANS_JSON)


class TestElements(unittest.TestCase):
    def setUp(self):
        self.E = lp.load_elements()

    def test_counts_match_the_revit_schedule(self):
        c = self.E["counts"]
        self.assertTrue(c["schedule_check"]["ok"])
        self.assertEqual((c["revit_panels"], c["revit_panels_glazed"], c["revit_panels_solid"]), (549, 535, 14))
        self.assertEqual(c["se_ranked_panels"], 142)
        self.assertEqual(c["lanterns"], 12)
        self.assertEqual(c["lantern_glazing_panels"], 168)
        self.assertTrue(20 <= c["fin_anchors"] <= 60)

    def test_ranks_are_a_permutation_and_edges_go_first(self):
        se = self.E["se_glass_share"]["panels"]
        self.assertEqual(sorted(p["rank"] for p in se), list(range(len(se))))
        by_rank = sorted(se, key=lambda p: p["rank"])
        edges = [p["edge"] for p in by_rank]
        self.assertEqual(edges, sorted(edges))           # bay centres keep glass longest
        self.assertEqual(by_rank[-1]["edge"], 1.0)        # the first panel to go solid is an edge lite
        self.assertEqual(by_rank[-1]["level"], "L1")      # ... on the lowest floor
        self.assertTrue(all(p["glazed_now"] for p in se))
        self.assertEqual(self.E["se_glass_share"]["default_share"], 100)

    def test_ranking_is_deterministic(self):
        """Rebuild the ranking from the documented key; it must equal the stored ranks."""
        se = self.E["se_glass_share"]["panels"]
        level_value = {"L4": 0, "L3": 1, "L2": 2, "L1": 3}
        hosts = {}
        for p in se:
            hosts.setdefault(p["mark"], []).append(p["center"][0])
        width = {m: (min(xs), max(xs)) for m, xs in hosts.items()}

        def key(p):
            lo, hi = width[p["mark"]]
            return (p["edge"], level_value[p["level"]], -abs((lo + hi) / 2 - 32.325), p["center"][0])
        again = sorted(se, key=key)
        self.assertEqual([p["id"] for p in again], [p["id"] for p in sorted(se, key=lambda p: p["rank"])])

    def test_lantern_order_is_fixed_pairs(self):
        L = self.E["skylights_open"]["lanterns"]
        self.assertEqual([l["index"] for l in L], list(range(12)))
        self.assertEqual([l["mark"] for l in L], ["SKY-01", "SKY-12", "SKY-06", "SKY-07", "SKY-03", "SKY-10",
                                                  "SKY-02", "SKY-11", "SKY-04", "SKY-09", "SKY-05", "SKY-08"])
        self.assertTrue(all(len(l["glazing_ids"]) == 14 for l in L))

    def test_fin_anchors_face_the_quad(self):
        A = self.E["fin_depth"]["anchors"]
        self.assertEqual(len(A), 30)
        self.assertTrue(all(a["facade"] == "SE" and a["dir"] == [0.0, -1.0] for a in A))
        self.assertEqual([a["mark"] for a in A], [f"PLX-FIN-{i}" for i in range(1, 31)])
        self.assertTrue(all(3.3 < a["height_m"] < 3.6 for a in A))


class TestPlan(unittest.TestCase):
    def test_glazed_count_rounding(self):
        n = 142
        self.assertEqual([lp.glazed_count(s, n) for s in range(40, 101, 10)], [57, 71, 85, 99, 114, 128, 142])
        for s in range(40, 101, 10):   # never a half, so every rounding convention agrees
            self.assertNotAlmostEqual((s * n / 100) % 1, 0.5)

    def test_plan_default_is_the_as_built(self):
        p = lp.plan(core.default_parameters())
        self.assertEqual((len(p["se_glazed"]), len(p["se_solid"])), (142, 0))
        self.assertEqual(len(p["lanterns_open"]), 12)
        self.assertEqual(p["fins"], [])
        self.assertEqual(p["material"], "ARCA Bush-hammered Architectural Concrete")

    def test_plan_rule(self):
        p = lp.plan(P("aluminium", 60, 0.9, 4))
        E = lp.load_elements()
        ranks = {x["id"]: x["rank"] for x in E["se_glass_share"]["panels"]}
        self.assertEqual(len(p["se_glazed"]), 85)
        self.assertTrue(all(ranks[i] < 85 for i in p["se_glazed"]) and all(ranks[i] >= 85 for i in p["se_solid"]))
        self.assertEqual(p["lanterns_open"], [0, 1, 2, 3])
        self.assertEqual(len(p["sky_solid"]), 8 * 14)
        self.assertEqual(len(p["fins"]), 30)
        f = p["fins"][0]
        self.assertAlmostEqual(f["start"][1] - f["end"][1], 0.9, places=6)
        self.assertEqual(p["material"], "Aluminum")

    def test_diff_and_compare(self):
        target = lp.plan(P("concrete", 90, 0.6, 10))
        as_built = lp.plan(core.default_parameters())
        state = {"panel_types": {str(i): "glazed" for i in as_built["se_glazed"] + as_built["sky_glazed"]},
                 "fins": [], "solid_material": {"name": None}}
        d = lp.diff(target, state)
        self.assertEqual(len(d["retype"]), (142 - 128) + 2 * 14)
        self.assertEqual(len(d["create"]), 30)
        self.assertEqual(d["delete"], [])
        self.assertEqual(d["material"]["name"], "ARCA Bush-hammered Architectural Concrete")
        self.assertFalse(lp.compare(target, state)["matches"])
        # the state after a perfect apply matches; one wrong fin is replaced
        done = {"panel_types": {str(k): v for k, v in lp.panel_targets(target).items()},
                "fins": [{"id": 900 + i, "mark": f["mark"], "start": f["start"], "length": f["length"],
                          "height": f["height"]} for i, f in enumerate(target["fins"])],
                "solid_material": {"name": target["material"]}}
        self.assertTrue(lp.compare(target, done)["matches"])
        done["fins"][3]["length"] = 0.3
        d2 = lp.diff(target, done)
        self.assertEqual((d2["delete"], len(d2["create"])), ([903], 1))
        # a stray unmarked fin is removed
        done["fins"][3]["length"] = 0.6
        done["fins"].append({"id": 999, "mark": None, "start": [1, 1], "length": 0.6, "height": 3.4})
        self.assertEqual(lp.diff(target, done)["delete"], [999])

    def test_summary(self):
        target = lp.plan(P("fritted_glass", 70, 0.3, 6))
        state = {"panel_types": {str(k): v for k, v in lp.panel_targets(target).items()},
                 "fins": [{"id": 1, "mark": "PLX-FIN-1", "start": [0, 0], "length": 0.3, "height": 3.4}],
                 "solid_material": {"name": "Glass"}}
        s = lp.summarize_state(state)
        self.assertEqual((s["se_panels_glazed"], s["lanterns_open"], s["solid_finish"]), (99, 6, "fritted_glass"))

    def test_expected_plans_json_is_current(self):
        data = json.loads(PLANS_JSON.read_text(encoding="utf-8"))
        self.assertEqual(data["cases"], [expected_plan(c) for c in CASES],
                         "run: python tests/test_langford_plan.py --write")


def find_node():
    try:
        p = core.load_local().get("node")
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    d = r"C:\Program Files\nodejs\node.exe"
    return d if os.path.isfile(d) else shutil.which("node")


@unittest.skipIf(not find_node() or not (REPO / "site" / "js" / "model" / "langford.js").exists(),
                 "node or site/js/model/langford.js missing")
class ParityTest(unittest.TestCase):
    def test_js_plan_equals_python_plan(self):
        r = subprocess.run([find_node(), str(PARITY_MJS)], capture_output=True, timeout=60, cwd=str(REPO))
        self.assertEqual(r.returncode, 0, (r.stdout + r.stderr).decode("utf-8", "replace")[-2000:])


if __name__ == "__main__":
    if "--write" in sys.argv:
        write_plans()
    else:
        unittest.main()
