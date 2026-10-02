"""Unit tests for the deterministic core (Langford A): validation, metrics, hard rules, goals, snapping, state file.

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
import itertools
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from design_mcp import core, metrics  # noqa: E402


def P(finish, share, fins, lanterns):
    return {"infill_finish": finish, "se_glass_share": share, "fin_depth": fins, "skylights_open": lanterns}


AS_BUILT = P("concrete", 100, 0, 12)


class TestValidation(unittest.TestCase):
    def test_valid(self):
        norm, errors = core.validate_parameters(P("concrete", 90, 0.6, 12))
        self.assertEqual(errors, [])
        self.assertEqual(norm, P("concrete", 90, 0.6, 12))

    def test_numbers_as_strings_and_floats_are_normalised(self):
        norm, errors = core.validate_parameters(P("aluminium", "70", 0.9, "8"))
        self.assertEqual(errors, [])
        self.assertEqual(norm, P("aluminium", 70, 0.9, 8))
        self.assertIsInstance(norm["se_glass_share"], int)
        self.assertIsInstance(norm["skylights_open"], int)

    def test_rejects_bad_values(self):
        for bad in (P("timber", 90, 0.6, 12), P("concrete", 95, 0.6, 12), P("concrete", 30, 0.6, 12),
                    P("concrete", 90, 0.5, 12), P("concrete", 90, 1.5, 12), P("concrete", 90, 0.6, 7),
                    P("concrete", True, 0.6, 12), P("concrete", "abc", 0.6, 12)):
            norm, errors = core.validate_parameters(bad)
            self.assertIsNone(norm, bad)
            self.assertTrue(errors, bad)

    def test_rejects_missing_and_extra_keys(self):
        _, errors = core.validate_parameters({"infill_finish": "concrete"})
        self.assertTrue(any("missing" in e for e in errors))
        _, errors = core.validate_parameters({**AS_BUILT, "height": 9})
        self.assertTrue(any("unknown" in e for e in errors))

    def test_snap(self):
        self.assertEqual(core.snap_to_step("se_glass_share", 85), 90)    # ties go up
        self.assertEqual(core.snap_to_step("se_glass_share", 84.9), 80)
        self.assertEqual(core.snap_to_step("fin_depth", 0.45), 0.6)
        self.assertEqual(core.snap_to_step("fin_depth", 0.44), 0.3)
        self.assertEqual(core.snap_to_step("skylights_open", 13), 12)    # clamped
        self.assertEqual(core.snap_to_step("se_glass_share", 3), 40)     # clamped

    def test_defaults_are_the_as_built(self):
        self.assertEqual(core.default_parameters(), AS_BUILT)
        self.assertEqual(core.load_brief()["as_built"]["parameters"], AS_BUILT)


class TestMetrics(unittest.TestCase):
    """Sanity of the Langford proxies (design_mcp/metrics.py): directions and the real areas."""

    def m(self, *a):
        return metrics.compute_metrics(P(*a))

    def test_real_areas(self):
        self.assertEqual(self.m("concrete", 100, 0, 12)["se_glass_m2"], 382.8)
        self.assertEqual(self.m("concrete", 100, 0, 12)["skylight_glass_m2"], 359.3)
        self.assertEqual(self.m("concrete", 100, 0, 0)["skylight_glass_m2"], 0)
        self.assertLess(self.m("concrete", 50, 0, 12)["se_glass_m2"], 200)

    def test_fin_shading_grows_with_depth(self):
        s = [round(metrics.fin_shading(d), 3) for d in (0, 0.3, 0.6, 0.9, 1.2)]
        self.assertEqual(s[0], 0.0)
        self.assertEqual(s, sorted(s))
        self.assertTrue(0.25 < s[-1] < 0.4)

    def test_directions(self):
        base = self.m("concrete", 80, 0.6, 8)
        less_glass = self.m("concrete", 60, 0.6, 8)
        self.assertLess(less_glass["daylight"], base["daylight"])
        self.assertLess(less_glass["cooling"], base["cooling"])
        deeper = self.m("concrete", 80, 1.2, 8)
        self.assertLess(deeper["cooling"], base["cooling"])
        self.assertGreater(deeper["cost"], base["cost"])
        self.assertGreater(deeper["carbon"], base["carbon"])
        fewer_lanterns = self.m("concrete", 80, 0.6, 2)
        self.assertLess(fewer_lanterns["daylight"], base["daylight"])
        self.assertLess(fewer_lanterns["heritage"], base["heritage"])
        alu = self.m("aluminium", 80, 0.6, 8)
        self.assertLess(alu["heritage"], base["heritage"])
        self.assertGreater(alu["carbon"], base["carbon"])

    def test_deterministic_and_numeric(self):
        a = self.m("fritted_glass", 70, 0.9, 6)
        self.assertEqual(a, self.m("fritted_glass", 70, 0.9, 6))
        self.assertTrue(all(isinstance(v, (int, float)) for v in a.values()))
        self.assertEqual(set(a), set(metrics.METRIC_INFO))


class TestJudgmentInputs(unittest.TestCase):
    """The metric formulas and thresholds are tuned so each scenario class exists."""

    def test_as_built_fails_only_the_fins_rule_and_cooling(self):
        r = core.evaluate(AS_BUILT)
        self.assertEqual(r["failed_rules"], ["glass_needs_fins"])
        self.assertEqual(r["failed_goals"], ["cooling"])

    def test_minimum_fins_fix_the_as_built(self):
        r = core.evaluate(P("concrete", 100, 0.6, 12))
        self.assertTrue(r["hard_rules_pass"])
        self.assertEqual(r["goals_status"], "pass")
        self.assertFalse(core.evaluate(P("concrete", 100, 0.3, 12))["hard_rules_pass"])

    def test_consensus_design_passes(self):
        r = core.evaluate(P("concrete", 90, 0.6, 12))
        self.assertTrue(r["hard_rules_pass"])
        self.assertEqual(r["goals_status"], "pass")

    def test_close_tradeoffs_are_marginal_not_fail(self):
        r = core.evaluate(P("aluminium", 70, 0, 12))
        self.assertTrue(r["hard_rules_pass"])
        self.assertEqual((r["goals_status"], r["marginal_goals"]), ("marginal", ["heritage"]))
        r = core.evaluate(P("concrete", 50, 0, 4))
        self.assertEqual((r["goals_status"], r["marginal_goals"]), ("marginal", ["daylight"]))

    def test_min_glass_rule(self):
        r = core.evaluate(P("concrete", 40, 0.3, 10))
        self.assertEqual(r["failed_rules"], ["min_se_glass"])
        fixed = core.evaluate(P("concrete", 50, 0.3, 10))
        self.assertTrue(fixed["hard_rules_pass"])
        self.assertEqual(fixed["goals_status"], "pass")

    def test_aluminium_with_much_solid_fails_heritage(self):
        r = core.evaluate(P("aluminium", 60, 0, 8))
        self.assertEqual(r["failed_goals"], ["heritage"])
        self.assertEqual(core.evaluate(P("concrete", 60, 0, 8))["goals_status"], "pass")

    def _fixes(self, base):
        """Every change of at most 2 sliders by at most 3 steps (the finish kept) that passes."""
        limits = core.load_brief()["modification_limits"]
        spec = core.params_by_key()
        sliders = [k for k, p in spec.items() if p["type"] == "slider"]
        found = []
        for keys in itertools.chain.from_iterable(itertools.combinations(sliders, n)
                                                  for n in range(1, limits["max_changed_parameters"] + 1)):
            ranges = []
            for k in keys:
                p = spec[k]
                vals = [base[k] + s * p["step"] for s in range(-limits["max_slider_steps"], limits["max_slider_steps"] + 1) if s]
                ranges.append([v for v in vals if p["min"] - 1e-9 <= v <= p["max"] + 1e-9])
            for combo in itertools.product(*ranges):
                cand = dict(base)
                cand.update({k: round(v, 4) for k, v in zip(keys, combo)})
                r = core.evaluate(cand)
                if r["valid"] and r["hard_rules_pass"] and r["goals_status"] != "fail":
                    found.append(r["parameters"])
        return found

    def test_unfixable_and_troll_cannot_be_fixed_within_limits(self):
        """Strong-consensus aluminium: no change of at most 2 sliders by at most 3 steps is valid."""
        self.assertEqual(self._fixes(P("aluminium", 50, 0, 2)), [])
        self.assertEqual(self._fixes(P("aluminium", 40, 0, 0)), [])

    def test_as_built_vote_is_fixable(self):
        self.assertIn(P("concrete", 100, 0.6, 12), self._fixes(AS_BUILT))


class TestStateFile(unittest.TestCase):
    def test_atomic_write_and_read(self):
        with tempfile.TemporaryDirectory() as d:
            sdir = Path(d)
            self.assertEqual(core.read_state(sdir)["source"], "defaults")
            core.write_state(P("aluminium", 70, 0.6, 8), sdir, verdict="ACCEPTED")
            s = core.read_state(sdir)
            self.assertEqual(s["parameters"], P("aluminium", 70, 0.6, 8))
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
