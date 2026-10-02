"""Live Revit tests for Plurarch (run step by step; only on revit/LangfordA_Plurarch.rvt as the ACTIVE document).

    .venv\Scripts\python.exe tools\revit_live_test.py <step>   (every write step checks the document guard first)

    revit_live_test.py probe            read-only: guard, materials, one panel, the solid type, wall rows, views
    revit_live_test.py state            read-only: read_state summary
    revit_live_test.py dry <name>       dry-run apply of a named parameter set (rolled back)
    revit_live_test.py apply <name>     apply + verify + state report
    revit_live_test.py image <name>     export the 3D view to PNG
    revit_live_test.py reset            back to the as-built (original finish)
"""
import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from design_mcp import langford_plan as lp, revit_apply as ra, revit_client as rc  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "state" / "revit_live"
OUT.mkdir(exist_ok=True)
SETS = {
    "A": {"infill_finish": "concrete", "se_glass_share": 60, "fin_depth": 0.6, "skylights_open": 8},
    "B": {"infill_finish": "aluminium", "se_glass_share": 90, "fin_depth": 1.2, "skylights_open": 12},
    "C": {"infill_finish": "fritted_glass", "se_glass_share": 40, "fin_depth": 0.3, "skylights_open": 2},
    "default": {"infill_finish": "concrete", "se_glass_share": 100, "fin_depth": 0, "skylights_open": 12},
}


def must_guard():
    ok, info, reason = ra.guard()
    print("guard:", ok, info, reason)
    if not ok:
        sys.exit("STOP: not the Langford copy")
    return info


def short(x, n=1500):
    return json.dumps(x, ensure_ascii=False)[:n]


def main():
    step = sys.argv[1]
    if step == "probe":
        print("health", rc.health())
        env = rc.call("get_document_info")
        print("doc:", short(env, 1200))
        must_guard()
        print("materials:", short(rc.call("list_materials"), 3000))
        E = lp.load_elements()
        pid = E["se_glass_share"]["panels"][0]["id"]
        print("panel:", short(rc.call("get_element_info", {"id": pid}), 2500))
        print("solid type:", short(rc.call("get_element_info", {"id": E["infill_finish"]["solid_panel_type_id"]}), 4000))
        print("walls:", short(rc.call("find_elements", {"category": "OST_Walls", "limit": 3, "fields": ["Mark", "Family and Type"]}), 2000))
        print("views:", short(rc.call("get_views"), 4000))
        print("active view:", short(rc.call("get_active_view"), 800))
    elif step == "state":
        t = time.time()
        st = ra.read_state()
        print("read in", round(time.time() - t, 2), "s; ok", st.get("ok"), st.get("reason", ""))
        if st.get("ok"):
            print(json.dumps(lp.summarize_state(st), indent=1))
            print("fins:", short(st["fins"][:3], 800), "material:", st["solid_material"])
            kinds = {}
            for v in st["panel_types"].values():
                kinds[v] = kinds.get(v, 0) + 1
            print("panel kinds:", kinds)
    elif step in ("dry", "apply"):
        must_guard()
        rep = ra.apply(SETS[sys.argv[2]], dry=(step == "dry"), label=f"live test {sys.argv[2]}")
        print(json.dumps({k: v for k, v in rep.items() if k != "state"}, indent=1)[:4000])
        if rep.get("state"):
            print("state:", json.dumps(rep["state"]))
    elif step == "report":
        print(json.dumps(ra.revit_state_report(SETS[sys.argv[2]]), indent=1))
    elif step == "image":
        must_guard()
        params = {"pixelSize": 1600}
        if len(sys.argv) > 3:
            params["viewId"] = int(sys.argv[3])
        env = rc.call("get_view_image", params, timeout=20)
        d = rc.data(env) or {}
        b = d.get("imageBase64")
        if not b:
            print("no image:", short(env, 800))
            return
        p = OUT / f"view_{sys.argv[2]}.png"
        p.write_bytes(base64.b64decode(b))
        print("wrote", p, d.get("width"), d.get("height"))
    elif step == "reset":
        must_guard()
        rep = ra.apply(SETS["default"], original_finish=True, label="live test reset")
        print(json.dumps({k: v for k, v in rep.items() if k != "state"}, indent=1)[:3000])
        print("state:", json.dumps(rep.get("state")))


if __name__ == "__main__":
    main()
