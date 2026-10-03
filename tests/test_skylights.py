"""Skylight layouts: masks, validation, plan, and the voted-values rule for verdicts."""
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from design_mcp import core, langford_plan, skylights  # noqa: E402


class SkylightLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("PLURARCH_STATE_DIR")
        os.environ["PLURARCH_STATE_DIR"] = self.tmp.name
        self.ids = skylights.panel_ids()
        rnd = random.Random(1)
        self.glazed = set(rnd.sample(self.ids, 8 * 14))
        self.mask = skylights.ids_to_mask(self.glazed, self.ids)
        self.lid = skylights.register(self.mask, 8, {"metrics": {}}, "ga")

    def tearDown(self):
        if self.old is None:
            os.environ.pop("PLURARCH_STATE_DIR", None)
        else:
            os.environ["PLURARCH_STATE_DIR"] = self.old
        self.tmp.cleanup()

    def params(self, **kw):
        p = {"infill_finish": "concrete", "se_glass_share": 90, "fin_depth": 0.6, "skylights_open": 8}
        p.update(kw)
        return p

    def test_mask_round_trip(self):
        self.assertEqual(len(self.ids), 168)
        self.assertEqual(len(self.mask), 42)
        self.assertEqual(skylights.mask_to_ids(self.mask, self.ids), self.glazed)

    def test_validation(self):
        ok, errs = core.validate_parameters(self.params(skylight_layout=self.lid))
        self.assertEqual(errs, [])
        self.assertEqual(ok["skylight_layout"], self.lid)
        self.assertEqual(core.voted(ok), self.params())
        _, errs = core.validate_parameters(self.params(skylight_layout="ga8-zzzz"))
        self.assertTrue(errs and "not a known layout" in errs[0])
        _, errs = core.validate_parameters(self.params(skylights_open=6, skylight_layout=self.lid))
        self.assertTrue(errs and "optimised for skylights_open = 8" in errs[0])
        ok, errs = core.validate_parameters(self.params(skylight_layout=""))
        self.assertEqual(errs, [])
        self.assertNotIn("skylight_layout", ok)

    def test_plan_uses_the_layout(self):
        p = langford_plan.plan(self.params(skylight_layout=self.lid))
        self.assertEqual(set(p["sky_glazed"]), self.glazed)
        self.assertEqual(len(p["sky_solid"]), 168 - 112)
        self.assertEqual(sum(p["lantern_glazed_panels"]), 112)
        plain = langford_plan.plan(self.params())
        self.assertEqual(len(plain["sky_glazed"]), 112)
        self.assertEqual(plain["lantern_glazed_panels"], [14] * 8 + [0] * 4)

    def test_evaluate_unchanged_by_layout(self):
        a = core.evaluate(self.params())
        b = core.evaluate(self.params(skylight_layout=self.lid))
        self.assertEqual(a["metrics"], b["metrics"])

    def test_with_mask(self):
        self.assertEqual(skylights.with_mask(self.params(skylight_layout=self.lid))["skylight_mask"], self.mask)
        self.assertNotIn("skylight_mask", skylights.with_mask(self.params()))

    def test_accepted_with_layout_is_valid(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orchestrator"))
        from validate import validate_decision
        prop = self.params()
        applied, _ = core.validate_parameters(self.params(skylight_layout=self.lid))
        ev = core.evaluate(applied)
        rec = {"verdict": "ACCEPTED", "proposal": prop, "applied_parameters": applied,
               "evidence": {"proposal_metrics": ev["metrics"], "applied_metrics": ev["metrics"],
                            "failed_rules": [], "failed_goals": []},
               "alternatives_considered": [], "changes": [], "rationale": "Kept as voted; glass placed by simulation."}
        log = [{"tool": "set_parameters", "ok": True, "phase": "apply", "output": {"parameters": applied}}]
        clean, fatal, _ = validate_decision(rec, prop, prop, applied, log)
        self.assertEqual(fatal, [], fatal)
        self.assertEqual(clean["verdict"], "ACCEPTED")


if __name__ == "__main__":
    unittest.main()
