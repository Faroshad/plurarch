"""design-mcp over real MCP stdio, in a temporary state folder (the live model is untouched).

    .venv\\Scripts\\python.exe -m unittest tests.test_design_mcp -v
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from mcp import Client, StdioServerParameters  # noqa: E402

P_OK = {"infill_finish": "concrete", "se_glass_share": 90, "fin_depth": 0.6, "skylights_open": 12}
P_TROLL = {"infill_finish": "aluminium", "se_glass_share": 40, "fin_depth": 0, "skylights_open": 0}


def payload(result):
    """Structured content if present, else the JSON text content."""
    sc = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        return sc.get("result", sc) if set(sc) == {"result"} else sc
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError(f"no payload in {result!r}")


class TestDesignMcp(unittest.TestCase):
    def test_tools_end_to_end(self):
        asyncio.run(self._run())

    async def _run(self):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "PLURARCH_STATE_DIR": d, "PLURARCH_ROUND_ID": "test-round",
                   "PLURARCH_RUN_ID": "test-run", "PYTHONUTF8": "1",
                   "PLURARCH_REVIT": "off"}
            params = StdioServerParameters(command=sys.executable, args=[str(REPO / "design_mcp" / "server.py")], env=env)
            async with Client(params) as client:
                tools = await client.list_tools()
                names = sorted(t.name for t in tools.tools)
                self.assertEqual(names, ["evaluate", "get_parameters", "get_project_brief", "get_revit_state", "get_schema",
                                         "set_parameters"])

                schema = payload(await client.call_tool("get_schema", {}))
                self.assertEqual(len(schema["parameters"]), 4)
                brief = payload(await client.call_tool("get_project_brief", {}))
                self.assertIn("hard_rules", brief)

                ev = payload(await client.call_tool("evaluate", {"parameters": P_TROLL}))
                self.assertFalse(ev["hard_rules_pass"])
                self.assertEqual(ev["evaluate_calls_used"], 1)

                # a malformed call does not use the budget
                bad = payload(await client.call_tool("evaluate", {"parameters": {"infill_finish": "steel"}}))
                self.assertFalse(bad["valid"])
                self.assertEqual(bad["evaluate_calls_used"], 1)

                refused = payload(await client.call_tool("set_parameters", {
                    "parameters": P_TROLL, "verdict": "ACCEPTED", "rationale": "Because the room wants it."}))
                self.assertFalse(refused["applied"])
                self.assertEqual(refused["error"], "hard_rule_failed")
                self.assertFalse((Path(d) / "parameters.json").exists())

                rejected = payload(await client.call_tool("set_parameters", {
                    "parameters": P_OK, "verdict": "REJECTED", "rationale": "Keep the design."}))
                self.assertEqual(rejected["error"], "invalid_verdict")

                for _ in range(4):
                    payload(await client.call_tool("evaluate", {"parameters": P_OK}))
                limit = payload(await client.call_tool("evaluate", {"parameters": P_OK}))
                self.assertEqual(limit.get("error"), "evaluate_limit_reached")

                applied = payload(await client.call_tool("set_parameters", {
                    "parameters": P_OK, "verdict": "ACCEPTED", "rationale": "Passes every rule and goal."}))
                self.assertTrue(applied["applied"], applied)
                state = json.loads((Path(d) / "parameters.json").read_text(encoding="utf-8"))
                self.assertEqual(state["parameters"], P_OK)
                self.assertEqual(applied["revit"]["applied"], False)
                self.assertIn("disabled", applied["revit"]["skipped"])
                self.assertEqual(state["round_id"], "test-round")

                again = payload(await client.call_tool("set_parameters", {
                    "parameters": P_OK, "verdict": "ACCEPTED", "rationale": "Again."}))
                self.assertEqual(again["error"], "already_applied")

                current = payload(await client.call_tool("get_parameters", {}))
                self.assertEqual(current["parameters"]["se_glass_share"], 90)
                rs = payload(await client.call_tool("get_revit_state", {}))
                self.assertFalse(rs["available"])
                verify = payload(await client.call_tool("evaluate", {"parameters": P_OK}))
                self.assertTrue(verify["valid"])  # the exempt verification call
                self.assertFalse(verify["counted"])
                after = payload(await client.call_tool("evaluate", {"parameters": P_OK}))
                self.assertEqual(after.get("error"), "evaluate_limit_reached")

            lines = [json.loads(l) for l in (Path(d) / "log.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all(l["round_id"] == "test-round" and l["run_id"] == "test-run" for l in lines))
            self.assertEqual([l["seq"] for l in lines], list(range(1, len(lines) + 1)))

    def test_rejected_path_can_verify_after_using_all_evaluations(self):
        asyncio.run(self._run_rejected())

    async def _run_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "PLURARCH_STATE_DIR": d, "PLURARCH_ROUND_ID": "r", "PYTHONUTF8": "1", "PLURARCH_REVIT": "off"}
            params = StdioServerParameters(command=sys.executable, args=[str(REPO / "design_mcp" / "server.py")], env=env)
            async with Client(params) as client:
                for _ in range(5):
                    payload(await client.call_tool("evaluate", {"parameters": P_TROLL}))
                current = payload(await client.call_tool("get_parameters", {}))["parameters"]
                other = payload(await client.call_tool("evaluate", {"parameters": P_TROLL}))
                self.assertEqual(other.get("error"), "evaluate_limit_reached")  # exploring is still capped
                verify = payload(await client.call_tool("evaluate", {"parameters": current}))
                self.assertTrue(verify["valid"])
                self.assertFalse(verify["counted"])


if __name__ == "__main__":
    unittest.main()
