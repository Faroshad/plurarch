"""The pavilion CLI (tools/pavilion_cli.mjs) returns a Model with the docs/MODEL.md shape.

Runs node through subprocess; skipped when node is not installed.
    python -m unittest discover -s tests
"""
import json
import os
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO, "tools", "pavilion_cli.mjs")


def find_node():
    try:
        with open(os.path.join(REPO, "config", "local.json"), encoding="utf-8") as fh:
            p = json.load(fh).get("node")
        if p and os.path.isfile(p):
            return p
    except (OSError, ValueError):
        pass
    default = r"C:\Program Files\nodejs\node.exe"
    if os.path.isfile(default):
        return default
    return shutil.which("node")


NODE = find_node()


@unittest.skipIf(not NODE, "node is not installed")
class ModelCliTest(unittest.TestCase):
    def run_cli(self, stdin_text, *args):
        return subprocess.run([NODE, CLI, *args], input=stdin_text.encode("utf-8"),
                              capture_output=True, timeout=30)

    def test_model_shape(self):
        params = {"facade_material": "glass", "window_ratio": 45, "roof_angle": 20, "canopy_depth": 2}
        res = self.run_cli(json.dumps(params))
        self.assertEqual(res.returncode, 0, res.stderr.decode("utf-8", "replace"))
        model = json.loads(res.stdout.decode("utf-8"))
        for key in ("version", "units", "up", "params", "materials", "parts", "pins", "tour",
                    "walkable", "bounds", "stats"):
            self.assertIn(key, model)
        self.assertEqual(model["version"], "2.0.0")
        self.assertEqual((model["units"], model["up"]), ("m", "z"))
        self.assertEqual(model["params"], params)
        self.assertGreaterEqual(len(model["parts"]), 800)
        self.assertEqual(model["stats"]["parts"], len(model["parts"]))
        for part in model["parts"]:
            self.assertIn(part["m"], model["materials"])
            self.assertIsInstance(part["t"], str)
            if "p" in part:
                self.assertEqual(len(part["p"]), 24)
                # rounded to 1 mm
                self.assertTrue(all(abs(v * 1000 - round(v * 1000)) < 1e-6 for v in part["p"]))
            else:
                self.assertEqual(len(part["cyl"]), 5)
                self.assertGreaterEqual(part["seg"], 3)
        for mat in model["materials"].values():
            self.assertEqual(len(mat["color"]), 3)
            self.assertTrue(0 <= mat["opacity"] <= 1)
        questions = {p["question"] for p in model["pins"]}
        self.assertEqual(questions, {"facade_material", "window_ratio", "roof_angle", "canopy_depth"})
        self.assertTrue(5 <= len(model["tour"]) <= 7)
        self.assertEqual(len(model["bounds"]["min"]), 3)

    def test_argument_and_wrapper(self):
        res = subprocess.run([NODE, CLI, '{"parameters": {"facade_material": "concrete"}}'],
                             capture_output=True, timeout=30)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(json.loads(res.stdout)["params"]["facade_material"], "concrete")

    def test_bad_json_fails_cleanly(self):
        res = self.run_cli("{not json")
        self.assertEqual(res.returncode, 1)
        self.assertIn(b"not valid JSON", res.stderr)


if __name__ == "__main__":
    unittest.main()
