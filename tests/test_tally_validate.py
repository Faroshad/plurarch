"""Unit tests for the tally (votes -> proposal) and the orchestrator's decision validation (Langford A)."""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "orchestrator"))

from design_mcp import core  # noqa: E402
import tally  # noqa: E402
from validate import trim_sentences, validate_decision  # noqa: E402

AS_BUILT = {"infill_finish": "concrete", "se_glass_share": 100, "fin_depth": 0, "skylights_open": 12}
CURRENT = {"infill_finish": "concrete", "se_glass_share": 90, "fin_depth": 0.6, "skylights_open": 12}


def votes(key, values):
    return [{"participant_id": f"p{i}", "question_key": key, "value": str(v)} for i, v in enumerate(values)]


class TestTally(unittest.TestCase):
    def test_choice_plurality_and_percentages(self):
        v = votes("infill_finish", ["aluminium"] * 6 + ["concrete"] * 3 + ["fritted_glass"])
        p = tally.build_proposal(v, CURRENT, [])
        t = p["tally"]["infill_finish"]
        self.assertEqual(p["parameters"]["infill_finish"], "aluminium")
        self.assertEqual(t["counts"], {"concrete": 3, "aluminium": 6, "fritted_glass": 1})
        self.assertEqual(t["percentages"]["aluminium"], 60.0)
        self.assertEqual(t["consensus_level"], "moderate")

    def test_choice_tie_keeps_current_value(self):
        v = votes("infill_finish", ["aluminium", "aluminium", "concrete", "concrete"])
        p = tally.build_proposal(v, CURRENT, [])
        self.assertEqual(p["parameters"]["infill_finish"], "concrete")
        self.assertEqual(sorted(p["tally"]["infill_finish"]["tie"]), ["aluminium", "concrete"])

    def test_leading_options_under_weak_consensus(self):
        v = votes("infill_finish", ["aluminium"] * 38 + ["concrete"] * 35 + ["fritted_glass"] * 27)
        t = tally.build_proposal(v, CURRENT, [])["tally"]["infill_finish"]
        self.assertEqual(t["consensus_level"], "weak")
        self.assertEqual(sorted(t["leading_options"]), ["aluminium", "concrete"])

    def test_slider_median_rounded_to_step(self):
        v = votes("se_glass_share", [60, 70, 80, 90])  # median 75 -> 80 (ties go up)
        p = tally.build_proposal(v, CURRENT, [])
        self.assertEqual(p["parameters"]["se_glass_share"], 80)
        t = p["tally"]["se_glass_share"]
        self.assertEqual(t["counts"]["70"], 1)
        self.assertEqual(t["n"], 4)

    def test_slider_consensus_within_one_step(self):
        v = votes("fin_depth", [0.6] * 6 + [0.3, 0.9] + [0, 1.2])
        t = tally.build_proposal(v, CURRENT, [])["tally"]["fin_depth"]
        self.assertEqual(t["median"], 0.6)
        self.assertEqual(t["consensus"], 0.8)
        self.assertEqual(t["consensus_level"], "strong")

    def test_invalid_values_are_ignored_and_no_votes_keep_current(self):
        v = votes("skylights_open", ["abc", 99, 6])
        p = tally.build_proposal(v, CURRENT, [])
        self.assertEqual(p["tally"]["skylights_open"]["n"], 1)
        self.assertEqual(p["parameters"]["skylights_open"], 6)
        self.assertEqual(p["parameters"]["fin_depth"], 0.6)
        self.assertEqual(p["tally"]["fin_depth"]["consensus_level"], "none")

    def test_participation_and_previous_decisions(self):
        v = votes("se_glass_share", [80, 90]) + votes("fin_depth", [0.6, 0.9])
        prev = [{"round_number": 1, "status": "ok", "verdict": "ACCEPTED",
                 "applied_parameters": CURRENT, "rationale": "fine"}]
        p = tally.build_proposal(v, CURRENT, prev)
        self.assertEqual(p["participation"], {"participants": 2, "votes": 4, "by_question": {
            "infill_finish": 0, "se_glass_share": 2, "fin_depth": 2, "skylights_open": 0}})
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
    P = {"infill_finish": "concrete", "se_glass_share": 80, "fin_depth": 0.6, "skylights_open": 10}

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
        bad = {"infill_finish": "concrete", "se_glass_share": 40, "fin_depth": 0.6, "skylights_open": 12}
        _, fatal, _ = validate_decision(record("ACCEPTED", bad, bad), bad, CURRENT, bad, log_applied(bad))
        self.assertTrue(any("hard rules" in f for f in fatal))

    def test_rejected_must_keep_the_design(self):
        rec, fatal, _ = validate_decision(record("REJECTED", self.P, CURRENT), self.P, CURRENT, CURRENT, [])
        self.assertEqual(fatal, [])
        self.assertEqual(rec["applied_parameters"], CURRENT)
        _, fatal, _ = validate_decision(record("REJECTED", self.P, CURRENT), self.P, CURRENT, self.P, [])
        self.assertTrue(any("model file changed" in f for f in fatal))

    def test_rejected_may_keep_the_non_compliant_as_built(self):
        """The as-built fails glass_needs_fins; REJECTED keeps it: a warning, not fatal."""
        troll = {"infill_finish": "aluminium", "se_glass_share": 50, "fin_depth": 0, "skylights_open": 2}
        rec, fatal, warnings = validate_decision(record("REJECTED", troll, AS_BUILT), troll, AS_BUILT, AS_BUILT, [])
        self.assertEqual(fatal, [])
        self.assertEqual(rec["applied_parameters"], AS_BUILT)
        self.assertTrue(any("glass_needs_fins" in w for w in warnings))
        # ... but applying the as-built (ACCEPTED of a vote for it) is still fatal
        _, fatal, _ = validate_decision(record("ACCEPTED", AS_BUILT, AS_BUILT), AS_BUILT, CURRENT, AS_BUILT,
                                        log_applied(AS_BUILT))
        self.assertTrue(any("hard rules" in f for f in fatal))

    def test_modified_that_still_fails_a_goal_is_flagged(self):
        prop = {"infill_finish": "aluminium", "se_glass_share": 40, "fin_depth": 0, "skylights_open": 0}
        bad = {"infill_finish": "aluminium", "se_glass_share": 50, "fin_depth": 0, "skylights_open": 6}
        rec, fatal, warnings = validate_decision(record("MODIFIED", prop, bad), prop, AS_BUILT, bad, log_applied(bad))
        self.assertEqual(fatal, [])
        self.assertTrue(any("still fails goals: heritage" in w for w in warnings))

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
        self.assertEqual(trim_sentences("Cooling 73.0 → 67.4. Cost 48.0. Carbon 30.0. Extra."),
                         "Cooling 73.0 → 67.4. Cost 48.0. Carbon 30.0.")


if __name__ == "__main__":
    unittest.main()
