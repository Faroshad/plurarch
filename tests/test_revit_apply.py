"""design_mcp/revit_apply.py against a FAKE Revit add-in (a tiny HTTP server in this process).

Checks the document guard (no write of any kind unless the active document is LangfordA_Plurarch from
this repo's revit/ folder), and apply -> verify -> idempotent re-apply -> reset to the as-built.
The real add-in is never contacted.

    .venv\\Scripts\\python.exe -m unittest tests.test_revit_apply -v
"""
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from design_mcp import langford_plan as lp, revit_apply as ra, revit_client as rc  # noqa: E402

FT = 0.3048
E = lp.load_elements()
GLAZED, SOLID = E["infill_finish"]["glazed_panel_type_id"], E["infill_finish"]["solid_panel_type_id"]
MATERIALS = {"ARCA Bush-hammered Architectural Concrete": 18940, "Aluminum": 101, "Glass": 102}
WRITES = {"change_element_type", "set_parameter", "delete_elements", "create_wall", "import_parameters",
          "set_parameter_batch"}


class FakeRevit:
    def __init__(self, title="LangfordA_Plurarch", path=None):
        self.title = title
        self.path = path or str(REPO / "revit" / "LangfordA_Plurarch.rvt")
        as_built = lp.plan({"infill_finish": "concrete", "se_glass_share": 100, "fin_depth": 0, "skylights_open": 12})
        self.panels = {i: GLAZED for i in as_built["se_glazed"] + as_built["sky_glazed"]}
        self.material = None                 # as modelled: no material
        self.walls = {1674856: {"mark": "SKY-01-W", "type": "Basic Wall: ARCA Fin/Curb - Concrete 200", "bbox": None}}
        self.next_id = 2000000
        self.commands = []                   # every command received, in order

    def doc_ok(self):
        return self.title == "LangfordA_Plurarch"

    def info(self, i):
        if i in self.panels:
            t = self.panels[i]
            return {"id": i, "typeId": t, "name": "Glazed" if t == GLAZED else "Solid", "parameters": []}
        if i == SOLID:
            name = next((k for k, v in MATERIALS.items() if v == self.material), None)
            return {"id": i, "parameters": [{"name": "Material", "value": self.material if self.material else -1,
                                             "valueString": name or "<By Category>"}]}
        if i in self.walls:
            w = self.walls[i]
            bb = w["bbox"]
            d = {"id": i, "name": w["type"], "parameters": [{"name": "Mark", "value": w["mark"], "valueString": w["mark"]},
                                                            {"name": "Family and Type", "valueString": w["type"]}]}
            if bb:
                d["boundingBox"] = {"min": dict(zip("xyz", [v / FT for v in bb[:3]])), "max": dict(zip("xyz", [v / FT for v in bb[3:]]))}
            return d
        return None

    def run(self, cmd, p, dry):
        self.commands.append(cmd)
        if cmd == "get_document_info":
            return {"ok": True, "data": {"title": self.title, "pathName": self.path}}
        if cmd == "find_elements":
            return {"ok": True, "data": {"elements": [{"id": i, "fields": {"Mark": w["mark"], "Family and Type": w["type"]}}
                                                      for i, w in self.walls.items()]}}
        if cmd == "list_materials":
            return {"ok": True, "data": {"materials": [{"id": v, "name": k} for k, v in MATERIALS.items()]}}
        if cmd == "get_element_info":
            d = self.info(int(p["id"]))
            return {"ok": True, "data": d} if d else {"ok": False, "error": {"code": "not_found", "message": "no element"}}
        if cmd in WRITES and not self.doc_ok():
            raise AssertionError(f"WRITE {cmd} sent to the wrong document {self.title}")
        if dry:
            return {"ok": True, "data": {}}
        if cmd == "change_element_type":
            self.panels[int(p["id"])] = int(p["typeId"])
            return {"ok": True, "data": {}}
        if cmd == "set_parameter":
            assert p["id"] == SOLID and p["parameterName"] == "Material"
            self.material = None if p["value"]["id"] == -1 else p["value"]["id"]
            return {"ok": True, "data": {}}
        if cmd == "delete_elements":
            for i in p["ids"]:
                self.walls.pop(int(i))
            return {"ok": True, "data": {}}
        if cmd == "create_wall":
            self.next_id += 1
            s, e = p["start"], p["end"]
            bb = [min(s["x"], e["x"]) - 0.1, min(s["y"], e["y"]), 0.0, max(s["x"], e["x"]) + 0.1, max(s["y"], e["y"]), p["height"]]
            self.walls[self.next_id] = {"mark": "", "type": "Basic Wall: " + p["wallTypeName"], "bbox": bb}
            return {"ok": True, "data": {"id": self.next_id}}
        if cmd == "import_parameters":
            for it in p["items"]:
                if it["parameterName"] == "Mark":
                    self.walls[int(it["elementId"])]["mark"] = it["value"]
            return {"ok": True, "data": {"applied": len(p["items"]), "failed": 0}}
        return {"ok": False, "error": {"code": "unknown_command", "message": cmd}}


def serve(fake: FakeRevit):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            self._send({"ok": True} if self.path == "/health" else {"ok": False}, 200 if self.path == "/health" else 404)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            try:
                if self.path == "/mcp":
                    return self._send(fake.run(body["command"], body.get("params") or {}, body.get("dryRun")))
                res = [{"index": i, "command": s["command"], **fake.run(s["command"], s.get("params") or {}, body.get("dryRun"))}
                       for i, s in enumerate(body["steps"])]
                return self._send({"ok": all(r["ok"] for r in res), "results": res})
            except AssertionError as e:
                fake.violation = str(e)
                return self._send({"ok": False, "error": {"code": "test_violation", "message": str(e)}})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class TestRevitApply(unittest.TestCase):
    def setUp(self):
        self._base = rc.BASE
        ra._MATERIALS = None

    def tearDown(self):
        rc.BASE = self._base
        ra._MATERIALS = None

    def start(self, fake):
        srv = serve(fake)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        rc.BASE = f"http://127.0.0.1:{srv.server_address[1]}"
        return fake

    def test_wrong_document_gets_no_write_at_all(self):
        fake = self.start(FakeRevit(title="H01_Three_Bedroom_House", path=r"C:\Users\x\H01_Three_Bedroom_House.rvt"))
        rep = ra.apply({"infill_finish": "aluminium", "se_glass_share": 60, "fin_depth": 0.9, "skylights_open": 4})
        self.assertFalse(rep["applied"])
        self.assertIn("H01_Three_Bedroom_House", rep["error"])
        self.assertEqual(fake.commands, ["get_document_info"])   # nothing else, not even a read batch
        self.assertFalse(hasattr(fake, "violation"))

    def test_right_title_from_another_folder_is_refused(self):
        fake = self.start(FakeRevit(path=r"E:\elsewhere\LangfordA_Plurarch.rvt"))
        rep = ra.apply({"infill_finish": "concrete", "se_glass_share": 90, "fin_depth": 0.6, "skylights_open": 12})
        self.assertFalse(rep["applied"])
        self.assertIn("not from", rep["error"])
        self.assertEqual(fake.commands, ["get_document_info"])

    def test_apply_verify_reapply_and_reset(self):
        fake = self.start(FakeRevit())
        target = {"infill_finish": "aluminium", "se_glass_share": 70, "fin_depth": 0.6, "skylights_open": 8}
        rep = ra.apply(target)
        self.assertTrue(rep["applied"], rep)
        self.assertTrue(rep["verified"], rep)
        self.assertEqual(rep["changes"], {"panels_retyped": 43 + 4 * 14, "fins_deleted": 0, "fins_created": 30, "material": True})
        self.assertEqual(sum(1 for t in fake.panels.values() if t == SOLID), 43 + 56)
        self.assertEqual(sorted(w["mark"] for w in fake.walls.values() if w["mark"].startswith("PLX-FIN-")),
                         sorted(f"PLX-FIN-{i}" for i in range(1, 31)))
        self.assertEqual(fake.material, MATERIALS["Aluminum"])
        s = ra.revit_state_report(target)
        self.assertTrue(s["available"] and s["matches_applied_parameters"], s)
        self.assertEqual((s["se_panels_glazed"], s["lanterns_open"], s["fins"], s["fin_depths_m"]), (99, 8, 30, [0.6]))

        again = ra.apply(target)                   # idempotent: nothing to do
        self.assertEqual((again["ops"], again["verified"]), (0, True))

        deeper = ra.apply({**target, "fin_depth": 1.2})   # depth change: all 30 fins replaced
        self.assertEqual((deeper["changes"]["fins_deleted"], deeper["changes"]["fins_created"]), (30, 30))
        self.assertTrue(deeper["verified"])

        reset = ra.apply({"infill_finish": "concrete", "se_glass_share": 100, "fin_depth": 0, "skylights_open": 12},
                         original_finish=True)
        self.assertTrue(reset["verified"], reset)
        self.assertTrue(all(t == GLAZED for t in fake.panels.values()))
        self.assertEqual([w["mark"] for w in fake.walls.values()], ["SKY-01-W"])   # P1 walls untouched
        self.assertIsNone(fake.material)
        self.assertFalse(hasattr(fake, "violation"))

    def test_stray_unmarked_fin_is_replaced(self):
        fake = self.start(FakeRevit())
        target = {"infill_finish": "concrete", "se_glass_share": 90, "fin_depth": 0.3, "skylights_open": 12}
        self.assertTrue(ra.apply(target)["verified"])
        some = next(i for i, w in fake.walls.items() if w["mark"] == "PLX-FIN-5")
        fake.walls[some]["mark"] = ""             # an interrupted mark step
        rep = ra.apply(target)
        self.assertEqual((rep["changes"]["fins_deleted"], rep["changes"]["fins_created"]), (1, 1))
        self.assertTrue(rep["verified"])

    def test_unreachable_is_fast_and_clean(self):
        rc.BASE = "http://127.0.0.1:9"            # nothing listens there
        rep = ra.apply({"infill_finish": "concrete", "se_glass_share": 90, "fin_depth": 0.6, "skylights_open": 12})
        self.assertFalse(rep["applied"])
        self.assertIn("not reachable", rep["error"])
        self.assertFalse(ra.revit_state_report(None)["available"])


if __name__ == "__main__":
    unittest.main()
