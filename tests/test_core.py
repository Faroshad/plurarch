"""Unit tests for the deterministic core: validation, hard rules, goals, snapping, state file.

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from design_mcp import core  # noqa: E402


def P(material, window, roof, canopy):
    return {"facade_material": material, "window_ratio": window, "roof_angle": roof, "canopy_depth": canopy}


class TestValidation(unittest.TestCase):
    def test_valid(self):
        norm, errors = core.validate_parameters(P("timber", 40, 15, 1.5))
        self.assertEqual(errors, [])
        self.assertEqual(norm, P("timber", 40, 15, 1.5))

    def test_numbers_as_strings_and_floats_are_normalised(self):
        norm, errors = core.validate_parameters(P("glass", "45", 20.0, "2"))
        self.assertEqual(errors, [])
        self.assertEqual(norm, P("glass", 45, 20, 2.0))
        self.assertIsInstance(norm["window_ratio"], int)

    def test_rejects_bad_values(self):
        for bad in (P("steel", 40, 15, 1.5), P("timber", 42, 15, 1.5), P("timber", 65, 15, 1.5),
                    P("timber", 40, 15, 1.25), P("timber", True, 15, 1.5), P("timber", "abc", 15, 1.5)):
            norm, errors = core.validate_parameters(bad)
            self.assertIsNone(norm, bad)
            self.assertTrue(errors, bad)

    def test_rejects_missing_and_extra_keys(self):
        _, errors = core.validate_parameters({"facade_material": "timber"})
        self.assertTrue(any("missing" in e for e in errors))
        _, errors = core.validate_parameters({**P("timber", 40, 15, 1.5), "height": 9})
        self.assertTrue(any("unknown" in e for e in errors))

    def test_snap(self):
        self.assertEqual(core.snap_to_step("window_ratio", 42.5), 45)  # ties go up
        self.assertEqual(core.snap_to_step("window_ratio", 42.4), 40)
        self.assertEqual(core.snap_to_step("canopy_depth", 1.26), 1.5)
        self.assertEqual(core.snap_to_step("roof_angle", 37), 35)       # clamped
        self.assertEqual(core.snap_to_step("window_ratio", 3), 20)      # clamped


class TestJudgmentInputs(unittest.TestCase):
    """The metric formulas and thresholds are tuned so each scenario class exists."""

    def test_default_design_passes_everything(self):
        r = core.evaluate(core.default_parameters())
        self.assertTrue(r["hard_rules_pass"])
        self.assertEqual(r["goals_status"], "pass")

    def test_consensus_design_passes(self):
        r = core.evaluate(P("timber", 45, 20, 2))
        self.assertTrue(r["hard_rules_pass"])
        self.assertEqual(r["goals_status"], "pass")

    def test_close_tradeoff_is_marginal_not_fail(self):
        r = core.evaluate(P("timber", 55, 15, 1.5))
        self.assertTrue(r["hard_rules_pass"])
        self.assertEqual(r["goals_status"], "marginal")
        self.assertIn("cooling", r["marginal_goals"])

    def test_min_window_rule(self):
        r = core.evaluate(P("timber", 20, 15, 1.5))
        self.assertEqual(r["failed_rules"], ["min_window_ratio"])
        fixed = core.evaluate(P("timber", 25, 15, 1.5))
        self.assertTrue(fixed["hard_rules_pass"])
        self.assertNotEqual(fixed["goals_status"], "fail")

    def test_glass_shading_rule(self):
        troll = core.evaluate(P("glass", 60, 0, 0))
        self.assertIn("glass_needs_shading", troll["failed_rules"])
        shaded = core.evaluate(P("glass", 50, 20, 2))
        self.assertTrue(shaded["hard_rules_pass"])
        self.assertNotEqual(shaded["goals_status"], "fail")

    def test_troll_cannot_be_fixed_within_limits(self):
        """glass/60/0/0: every change of at most 2 sliders by at most 3 steps (material kept) fails."""
        brief = core.load_brief()
        limits = brief["modification_limits"]
        spec = core.params_by_key()
        sliders = [k for k, p in spec.items() if p["type"] == "slider"]
        base = P("glass", 60, 0, 0)
        import itertools
        found = []
        for keys in itertools.chain.from_iterable(itertools.combinations(sliders, n)
                                                  for n in range(1, limits["max_changed_parameters"] + 1)):
            ranges = []
            for k in keys:
                p = spec[k]
                vals = [base[k] + s * p["step"] for s in range(-limits["max_slider_steps"], limits["max_slider_steps"] + 1) if s]
                ranges.append([v for v in vals if p["min"] <= v <= p["max"]])
            for combo in itertools.product(*ranges):
                cand = dict(base)
                cand.update(dict(zip(keys, combo)))
                r = core.evaluate(cand)
                if r["valid"] and r["hard_rules_pass"] and r["goals_status"] != "fail":
                    found.append(cand)
        self.assertEqual(found, [], f"troll proposal is fixable: {found[:3]}")

    def test_concrete_heavy_fails_carbon(self):
        r = core.evaluate(P("concrete", 25, 35, 3))
        self.assertIn("carbon", r["failed_goals"])
        self.assertEqual(core.evaluate(P("timber", 25, 35, 3))["failed_goals"], [])


class TestStateFile(unittest.TestCase):
    def test_atomic_write_and_read(self):
        with tempfile.TemporaryDirectory() as d:
            sdir = Path(d)
            self.assertEqual(core.read_state(sdir)["source"], "defaults")
            core.write_state(P("glass", 50, 20, 2.0), sdir, verdict="ACCEPTED")
            s = core.read_state(sdir)
            self.assertEqual(s["parameters"], P("glass", 50, 20, 2.0))
            self.assertEqual(s["verdict"], "ACCEPTED")
            self.assertEqual([p.name for p in sdir.iterdir()], ["parameters.json"])  # no temp files left

    def test_corrupt_file_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "parameters.json").write_text("{not json", encoding="utf-8")
            s = core.read_state(Path(d))
            self.assertEqual(s["source"], "defaults")
            self.assertIn("warning", s)


if __name__ == "__main__":
    unittest.main()
