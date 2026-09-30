"""Unit tests for the tally (votes -> proposal) and the orchestrator's decision validation."""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "orchestrator"))

from design_mcp import core  # noqa: E402
import tally  # noqa: E402
from validate import trim_sentences, validate_decision  # noqa: E402

CURRENT = {"facade_material": "timber", "window_ratio": 40, "roof_angle": 15, "canopy_depth": 1.5}


def votes(key, values):
    return [{"participant_id": f"p{i}", "question_key": key, "value": str(v)} for i, v in enumerate(values)]


class TestTally(unittest.TestCase):
    def test_choice_plurality_and_percentages(self):
        v = votes("facade_material", ["glass"] * 6 + ["timber"] * 3 + ["concrete"])
        p = tally.build_proposal(v, CURRENT, [])
        t = p["tally"]["facade_material"]
        self.assertEqual(p["parameters"]["facade_material"], "glass")
        self.assertEqual(t["counts"], {"timber": 3, "concrete": 1, "glass": 6})
        self.assertEqual(t["percentages"]["glass"], 60.0)
        self.assertEqual(t["consensus_level"], "moderate")

    def test_choice_tie_keeps_current_value(self):
        v = votes("facade_material", ["glass", "glass", "timber", "timber"])
        p = tally.build_proposal(v, CURRENT, [])
        self.assertEqual(p["parameters"]["facade_material"], "timber")
        self.assertEqual(sorted(p["tally"]["facade_material"]["tie"]), ["glass", "timber"])

    def test_leading_options_under_weak_consensus(self):
        v = votes("facade_material", ["concrete"] * 38 + ["timber"] * 35 + ["glass"] * 27)
        t = tally.build_proposal(v, CURRENT, [])["tally"]["facade_material"]
        self.assertEqual(t["consensus_level"], "weak")
        self.assertEqual(t["leading_options"], ["timber", "concrete"])

    def test_slider_median_rounded_to_step(self):
        v = votes("window_ratio", [40, 45, 50, 55])  # median 47.5 -> 50 (ties go up)
        p = tally.build_proposal(v, CURRENT, [])
        self.assertEqual(p["parameters"]["window_ratio"], 50)
        t = p["tally"]["window_ratio"]
        self.assertEqual(t["counts"]["45"], 1)
        self.assertEqual(t["n"], 4)

    def test_slider_consensus_within_one_step(self):
        v = votes("canopy_depth", [1.5] * 6 + [1, 2] + [0, 3])
        t = tally.build_proposal(v, CURRENT, [])["tally"]["canopy_depth"]
        self.assertEqual(t["median"], 1.5)
        self.assertEqual(t["consensus"], 0.8)
        self.assertEqual(t["consensus_level"], "strong")

    def test_invalid_values_are_ignored_and_no_votes_keep_current(self):
        v = votes("roof_angle", ["abc", 99, 20])
        p = tally.build_proposal(v, CURRENT, [])
        self.assertEqual(p["tally"]["roof_angle"]["n"], 1)
        self.assertEqual(p["parameters"]["roof_angle"], 20)
        self.assertEqual(p["parameters"]["canopy_depth"], 1.5)
        self.assertEqual(p["tally"]["canopy_depth"]["consensus_level"], "none")

    def test_participation_and_previous_decisions(self):
        v = votes("window_ratio", [45, 50]) + votes("roof_angle", [20, 25])
        prev = [{"round_number": 1, "status": "ok", "verdict": "ACCEPTED",
                 "applied_parameters": CURRENT, "rationale": "fine"}]
        p = tally.build_proposal(v, CURRENT, prev)
        self.assertEqual(p["participation"], {"participants": 2, "votes": 4, "by_question": {
            "facade_material": 0, "window_ratio": 2, "roof_angle": 2, "canopy_depth": 0}})
        self.assertEqual(p["previous_decisions"][0]["verdict"], "ACCEPTED")


def record(verdict, proposal, applied, rationale="The proposal is fine. Numbers look good."):
    m = core.evaluate(applied)["metrics"]
    return {"verdict": verdict, "proposal": proposal, "applied_parameters": applied,
            "evidence": {"proposal_metrics": m, "applied_metrics": m, "failed_rules": [], "failed_goals": []},
            "alternatives_considered": [], "changes": [], "rationale": rationale}


def log_applied(params):
    return [{"tool": "evaluate", "phase": "explore", "ok": True},
            {"tool": "set_parameters", "ok": True, "output": {"parameters": params}}]


class TestValidate(unittest.TestCase):
    P = {"facade_material": "timber", "window_ratio": 45, "roof_angle": 20, "canopy_depth": 2.0}

    def test_accepted_ok(self):
        rec, fatal, _ = validate_decision(record("ACCEPTED", self.P, self.P), self.P, CURRENT, self.P,
                                          log_applied(self.P))
        self.assertEqual(fatal, [])
        self.assertEqual(rec["verdict"], "ACCEPTED")

    def test_accepted_but_state_not_changed_is_fatal(self):
        _, fatal, _ = validate_decision(record("ACCEPTED", self.P, self.P), self.P, CURRENT, CURRENT,
                                        log_applied(self.P))
        self.assertTrue(any("model file" in f for f in fatal))

    def test_accepted_without_set_parameters_is_fatal(self):
        _, fatal, _ = validate_decision(record("ACCEPTED", self.P, self.P), self.P, CURRENT, self.P, [])
        self.assertTrue(any("never succeeded" in f for f in fatal))

    def test_hard_rule_breaking_applied_is_fatal(self):
        bad = {"facade_material": "timber", "window_ratio": 20, "roof_angle": 15, "canopy_depth": 1.5}
        _, fatal, _ = validate_decision(record("ACCEPTED", bad, bad), bad, CURRENT, bad, log_applied(bad))
        self.assertTrue(any("hard rules" in f for f in fatal))

    def test_rejected_must_keep_the_design(self):
        rec, fatal, _ = validate_decision(record("REJECTED", self.P, CURRENT), self.P, CURRENT, CURRENT, [])
        self.assertEqual(fatal, [])
        self.assertEqual(rec["applied_parameters"], CURRENT)
        _, fatal, _ = validate_decision(record("REJECTED", self.P, CURRENT), self.P, CURRENT, self.P, [])
        self.assertTrue(any("model file changed" in f for f in fatal))

    def test_schema_violation_is_fatal(self):
        _, fatal, _ = validate_decision({"verdict": "MAYBE"}, self.P, CURRENT, CURRENT, [])
        self.assertTrue(fatal)

    def test_modified_equal_to_proposal_is_relabelled_and_rationale_trimmed(self):
        long = "One is fine. Two is fine. Three is fine. Four is too many."
        rec, fatal, warnings = validate_decision(record("MODIFIED", self.P, self.P, long), self.P, CURRENT, self.P,
                                                 log_applied(self.P))
        self.assertEqual(fatal, [])
        self.assertEqual(rec["verdict"], "ACCEPTED")
        self.assertEqual(rec["rationale"], "One is fine. Two is fine. Three is fine.")
        self.assertTrue(warnings)

    def test_evidence_is_recomputed(self):
        r = record("ACCEPTED", self.P, self.P)
        r["evidence"]["applied_metrics"] = {"daylight": 99.0}
        rec, fatal, warnings = validate_decision(r, self.P, CURRENT, self.P, log_applied(self.P))
        self.assertEqual(rec["evidence"]["applied_metrics"], core.evaluate(self.P)["metrics"])

    def test_trim_keeps_decimals(self):
        self.assertEqual(trim_sentences("Cooling 66.9 → 58.4. Cost 90.9. Carbon 58.2. Extra."),
                         "Cooling 66.9 → 58.4. Cost 90.9. Carbon 58.2.")


if __name__ == "__main__":
    unittest.main()
