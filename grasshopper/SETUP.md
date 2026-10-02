# Plurarch in Grasshopper: setup

The live model is a Grasshopper "Python 3 Script" component (**PlurarchModel**). It watches
`state\parameters.json` (written by the design server) and updates the model whenever the
approved parameters change. The same loader component runs one of two scripts:

| Model | Script (`_MB` in the loader) | Rhino document |
|---|---|---|
| **Langford A** (current, real building) | `grasshopper\langford_builder.py` | `rhino\LangfordA_Plurarch_render.3dm` |
| Pavilion (previous, generated) | `grasshopper\model_builder.py` | any; the pavilion is a Grasshopper preview |

---

## Langford A (current)

`langford_builder.py` applies the plan rule of `docs\LANGFORD.md` to the **real objects of the
render scene**. They are IFC meshes, and each one carries its Revit element id in its user text
(`RevitElementId`). It does not draw a Grasshopper preview: it changes the document itself, so
Rendered, Raytraced and Cycles all show the result.

| Question | What changes in Rhino |
|---|---|
| `infill_finish` | Material of every solid infill panel: the 14 penthouse louvre panels, plus every SE lite and lantern panel that is solid. concrete = the scene's *ARCA bush-hammered concrete* (with its 2.4 m box mapping); aluminium = *PLX brushed aluminium (Plurarch infill)*; fritted_glass = *PLX fritted glass (Plurarch infill)*. The last two are created on first use. |
| `se_glass_share` | The first round(share/100 × 142) SE lites by rank keep their glass; the others get the infill finish. |
| `skylights_open` | The first n lanterns by index keep their glass; the glazing of the others gets the infill finish. |
| `fin_depth` | 30 concrete fin breps (0.2 m × depth × floor-to-window-head) on layer `Plurarch::Fins`, one per anchor, replaced when the depth changes. None at 0. |

- The first time an object is changed, its original material is saved in its user text
  (`PLX_orig_material`). Turning it back to glass restores exactly that material.
- Only objects whose material differs from the plan are written, so it is idempotent. Timer
  ticks with no change cost almost nothing.
- **Document guard:** it only edits a saved document inside this repo's `rhino\` folder, and
  never anything in `LangfordA_Fresh`. With any other document open, `warning` says
  *Not applied: ...* and nothing is touched.
- The element ids and the fin anchors come from `config\langford\elements.json` (Revit model
  metres). The Rhino scene is that frame rotated +39.35° about Z at the origin (`to_rhino`).

### Switch the component to Langford
1. Open `rhino\LangfordA_Plurarch_render.3dm` in Rhino 8 (it is large; give it a minute),
   then type `Grasshopper` and open `grasshopper\plurarch.gh`.
2. Double-click **PlurarchModel**. In the loader, change the `_MB` line to
   `_MB = r"E:\Academic\PhD\Fall 2026\AI Workshop\PlurARCH\grasshopper\langford_builder.py"`
   and click **Run**. Nothing else in the loader changes.
3. `info` should read *PLURARCH Langford A - design from file*, with the scene line
   *LangfordA_Plurarch_render.3dm (324 element ids -> N objects)*.
4. The Custom Preview now receives empty lists; you can leave it.
5. Use the Rendered display mode (or Raytraced for stills) and the named view **Plurarch**,
   the SE quad three-quarter view. Keep the Grasshopper window open; it may be minimised.

To go back to the pavilion, set `_MB` back to `model_builder.py`. The Langford changes stay in
the 3dm until the Langford builder runs again.

### Test designs (never edit the live parameters.json)
Four ready-made files are in `state\`. Point the **StatePath** Panel at one, look, and then
**set StatePath back to `...\state\parameters.json`**:

| File | Design |
|---|---|
| `test_langford_1_asbuilt.json` | concrete, 100 %, fins 0, 12 skylights (as built) |
| `test_langford_2_glass50_fins12.json` | concrete, 50 % SE glass, fins 1.2 m, 12 skylights |
| `test_langford_3_sky4_fritted.json` | fritted glass, 100 %, no fins, 4 skylights |
| `test_langford_4_alu_fins06.json` | aluminium, 70 % SE glass, fins 0.6 m, 12 skylights |

### Langford troubleshooting
| Warning | Meaning and fix |
|---|---|
| *Not applied: the active Rhino document is ...* | The wrong 3dm is active. Open `rhino\LangfordA_Plurarch_render.3dm`. |
| *N element ids not found in the Rhino scene* | The objects are missing, or their `RevitElementId` user text is missing. Click a Button wired to `tick` once; it rebuilds the object index. |
| *no Langford parameters ... in the file* | The state file still holds the old pavilion keys; the as-built design stays on screen. |
| *render material ... not found* | The scene's bush-hammered concrete material was renamed or deleted. |
| Fins missing after editing the scene | Click the `tick` Button: it re-checks every object and replaces the fins. |

Self-test (no Rhino needed): `.\.venv\Scripts\python.exe grasshopper\langford_builder.py --selftest`.
It checks the plan rule, parity with `design_mcp\langford_plan.py` over all 735 valid designs,
the fin geometry and frame, validation, the apply logic (idempotent, exact restore) and the
file watcher.

---

## Pavilion (previous model)

The sections below describe `model_builder.py`, which rebuilds the generated pavilion as a
Grasshopper preview. The component, loader, StatePath, Timer and Panels are the same ones
Langford uses.

## Requirement: Node.js (same model as the phones)

The detailed pavilion (about 850–1150 parts: façade system, framed windows, entrance door, glulam
roof beams, seating, stage, lights, trees) is generated by **one** JavaScript file,
`site\js\model\pavilion.js`, which the phones also use. Rhino runs it through
`node tools\pavilion_cli.mjs` whenever the parameters change (about 50 ms, no console window,
5 s timeout) and meshes the result (about 250 ms).

- Node.js must be installed. This laptop: `C:\Program Files\nodejs\node.exe` (v24).
- The script finds node in this order: `config\local.json` key `"node"` (written by
  `tools\setup_local.py`), then `C:\Program Files\nodejs\node.exe`, then `node` on the PATH.
- `info` shows which generator is on screen: `generator : pavilion.js v2.0.0 via node (50 ms)`.

**What the fallback means.** If node is missing, crashes or takes longer than 5 s, the component
does *not* go blank: it shows the older, simplified built-in Python model (same footprint, roof
rule, façade systems and glazed share, but no interior, door, beams or landscape). It turns orange,
and `warning` / `info` say *Detailed model unavailable ... showing the simplified built-in model*.
It tries node again every 60 s. So a fallback costs detail, never the picture. To fix it, check
the node path in `config\local.json`, or run `node tools\pavilion_cli.mjs "{}"` in a terminal
and read the error.

Editing `pavilion.js` (or `model_builder.py`) rebuilds the model on the next Timer tick.

**Keep the Grasshopper window open** (it may be minimised). If the Grasshopper editor is
*closed* (hidden), Grasshopper draws no preview in Rhino at all, even though the component
keeps computing.

---

## Quick start (this machine)

1. Open Rhino 8, type `Grasshopper`, press Enter.
2. In Grasshopper: **File > Open Document** > `E:\Academic\PhD\Fall 2026\AI Workshop\PlurARCH\grasshopper\plurarch.gh`.
3. Make sure the **Timer** is running. If it shows a lock (it is disabled), right-click the Timer > **Enable**.

Done. Within half a second the pavilion appears in the Rhino viewports. It changes on its own
every time the design server writes a new decision.

What is on the canvas:

| Object | What it does |
|---|---|
| **StatePath** (Panel) | Holds `E:\Academic\PhD\Fall 2026\AI Workshop\PlurARCH\state\parameters.json` and feeds `path`. |
| **Timer** (500 ms) | Re-runs the component twice a second. Its wire goes to the component itself, not to the `tick` input, so `tick` stays unconnected. That is correct. |
| **PlurarchModel** (Python 3 Script) | Inputs `path`, `tick`, `scale`. Outputs `out` (standard output), `geometry`, `colors`, `info`, `warning`. Its own preview is off. |
| **Custom Preview** | G ← `geometry`, M ← `colors`. This is what you see in Rhino. |
| Two **Panels** | Show `info` (current values, file time) and `warning` (empty when all is fine). |

The component's code is a short **loader**. It runs `grasshopper\model_builder.py` straight
from the repo and recompiles it only when that file changes. So edits to
`model_builder.py` show up on the next Timer tick, with no copy and paste.

---

## Build it from scratch (new machine, or if plurarch.gh is lost)

About 5 minutes. Tip: double-click an empty spot on the canvas to open the search box,
type a component name, press Enter.

### 1. The script component
1. Rhino 8 > type `Grasshopper` > Enter.
2. Place a **Python 3 Script** component: **Maths** tab > **Script** panel > **Python 3 Script**
   (or double-click the canvas, type `Python 3 Script`, press Enter).
3. Give it the nickname **PlurarchModel**: right-click the component and type the name into the
   text box at the top of the menu.
4. **Inputs.** The component starts with inputs `x` and `y`. Zoom in on the component
   (mouse wheel) until small **+** and **−** icons appear next to the inputs.
   - Right-click `x` > type `path` in the name box. In the same menu choose **Item Access**,
     then **Type Hint > str**.
   - Right-click `y` > rename to `tick`. Leave it as **Item Access** with **No Type Hint**.
   - Click **+** under the inputs to add a third one. Rename it `scale`, **Item Access**,
     **Type Hint > float**. Leave it unconnected. The model then follows the Rhino document
     units by itself (metres, feet, mm, ...).
5. **Outputs.** Keep `out` (it shows printed text). Rename `a` to `geometry`, then click
   **+** on the output side three times and rename the new outputs `colors`, `info` and
   `warning`, in that order.
6. **Code.** Double-click the component to open the script editor. Select all (Ctrl+A),
   delete it, and paste the loader below. Change the path on the `_MB` line if the repo lives
   somewhere else. Then click **Run** (the green ▶ button, or F5) and close the editor.
   If it asks whether to save or apply the changes, answer Yes.

```python
#! python3
# Plurarch loader: runs grasshopper/model_builder.py from the repo (recompiled only when the file
# changes). If the file cannot be read or has a typo, the last good version keeps running.
import os
import scriptcontext as sc
_MB = r"E:\Academic\PhD\Fall 2026\AI Workshop\PlurARCH\grasshopper\model_builder.py"
_key = "plurarch_loader_code"
_load_error = ""
_cached = sc.sticky.get(_key)
try:
    _mtime = os.path.getmtime(_MB)
    if not _cached or _cached[0] != _mtime:
        with open(_MB, encoding="utf-8") as _f:
            _cached = (_mtime, compile(_f.read(), _MB, "exec"))
        sc.sticky[_key] = _cached
except Exception as _e:
    _load_error = "model_builder.py could not be loaded (%s: %s); the last good version is running." % (
        type(_e).__name__, _e)
if _cached:
    exec(_cached[1])
if _load_error:
    try:
        warning = _load_error + ("\n" + warning if warning else "")
    except NameError:
        warning = _load_error
        geometry, colors, info = [], [], _load_error
```

   *Alternative:* paste the **whole** `model_builder.py` instead of the loader. It works the
   same way, but later edits to the repo file are not picked up, so you must paste again.

7. Right-click the component > untick **Preview**. Only the coloured Custom Preview should
   draw the model; otherwise you also get a grey double.

### 2. The file path
1. Add a **Panel**: **Params** tab > **Input** > **Panel** (or search `Panel`).
2. Right-click it > rename to **StatePath**.
3. Double-click the Panel and type or paste the full path, on one line:
   `E:\Academic\PhD\Fall 2026\AI Workshop\PlurARCH\state\parameters.json`
   Quotes from Explorer's "Copy as path" are fine. A folder path also works; the script
   then looks for `parameters.json` inside it. On another machine, use the state folder
   from `config\local.json`.
4. Drag a wire from the Panel's right side to the `path` input.

### 3. The Timer
1. Add a **Timer**: **Params** tab > **Util** > **Timer**.
2. Right-click it > **Interval** > **500 ms**.
3. Drag a wire from the Timer's right-hand grip and drop it on the PlurarchModel component.
   The wire attaches to the component as a whole, not to an input. That is all it needs;
   `tick` stays empty.
4. To stop the Timer: right-click > **Disable** (or select it and press Ctrl+E).
   To start it: right-click > **Enable**.

### 4. Preview and read-outs
1. Add a **Custom Preview**: **Display** tab > **Preview** > **Custom Preview**.
   Wire `geometry` → **G** and `colors` → **M**.
2. Add two more Panels. Wire `info` into one and `warning` into the other. Stretch the info
   Panel so about 12 lines fit.
3. In the Grasshopper toolbar (top right of the canvas), make sure **Shaded Preview** is on.

### 5. Save
**File > Save Document As** > `grasshopper\plurarch.gh`. If you want to keep the existing
file, use a new name.

*Optional:* a **Button** (Params > Input > Button) wired to `tick` forces a re-read and
rebuild while you click it. You won't normally need it.

---

## Projector view

1. In Rhino, double-click the **Perspective** viewport title to maximise it.
2. Click the viewport title > **Shaded**. **Rendered** also works and adds soft lighting.
   Do not use Arctic, Pen or Technical: they ignore the colours.
3. Frame a 3/4 view from the **front-left**, looking slightly down. The entrance faces **−Y**:
   in the Top view it is the bottom edge of the building. Right-drag to orbit,
   Shift+right-drag to pan, mouse wheel to zoom.
4. Leave headroom. At the default 15° roof the front is about 7 m tall; at 35° it grows to
   about 11.5 m and the back overhang to 3 m. Let the default model fill about half of the
   screen height.
5. Hide the grid: press **F7**. For the red/green axis lines and the small XYZ icon, type
   `DocumentProperties` > **Grid** page > untick *Show grid axes* and *Show world axes icon*.
6. Save the view: type `NamedView` to open the Named Views panel > **Save** > name it
   `Plurarch`. To get it back at any time, double-click it in that panel, or type
   `-NamedView`, choose **Restore** and enter `Plurarch`.
7. Keep the Grasshopper window open, for example on the laptop screen. If you minimise it,
   check that the model still updates when a new decision comes in.

### Check the extremes before the session (optional)
Don't write test values into the live `parameters.json`: the design server uses it. Use a
separate test file and point StatePath at it for a moment:

```powershell
Set-Content -Encoding utf8 -Path "E:\Academic\PhD\Fall 2026\AI Workshop\PlurARCH\state\test_parameters.json" -Value '{"parameters": {"facade_material": "glass", "window_ratio": 60, "roof_angle": 35, "canopy_depth": 3}}'
```

Change the StatePath text to `...\state\test_parameters.json`. Look at the tallest roof and
the deepest canopy, and adjust the named view if needed. Try `concrete / 20 / 0 / 0` for the
other extreme. **Then set StatePath back to `...\state\parameters.json`.**

---

## Troubleshooting

Read the **warning** Panel first; it says what is wrong in plain words. Whenever something
is wrong, the model keeps showing the **last good design**.

| Symptom | Cause and fix |
|---|---|
| Component **red** | Should not happen with the loader above. If it does, check the `_MB` path in the loader. |
| Warning says *model_builder.py could not be loaded* | The file was just edited and has a typo (or the path is wrong). The last good version keeps running; fix the file and run the self-test (below). |
| Component **orange** | A warning; read the warning Panel. Typical ones: *File not found* (wrong path, or the design server has not written a decision yet; the default design is shown until then), *not valid JSON*, *outside 20..60* (a bad value; the last good design is kept). |
| Warning says *Detailed model unavailable* | node was not found, failed or timed out; the simplified built-in model is shown (see *What the fallback means* above). Check `"node"` in `config\local.json`; run `node tools\pavilion_cli.mjs "{}"` to see the error. It retries every 60 s. |
| **Nothing shows** in Rhino, `info` looks fine | The Grasshopper window is closed: GH draws previews only while its editor is open. Type `Grasshopper` in Rhino to reopen it (you may minimise it). |
| **Nothing shows** in Rhino | Check the Custom Preview wires (G ← geometry, M ← colors) and that the Custom Preview itself is not disabled. Grasshopper toolbar: Shaded Preview must be on. Viewport in Shaded or Rendered mode. Zoom out: the model may be off-screen. Look at `info`: it should say *N parts -> M objects*. |
| A grey copy on top of the coloured model | The script component's own preview is still on: right-click it > untick Preview. |
| **File path wrong** | Compare the StatePath text with the real file in Explorer; one line, no typos. `info` shows the exact path being watched. |
| Model does **not update** after a decision | Is the Timer running? Right-click > Enable; check that its wire goes to PlurarchModel. Is the solver on? If the Grasshopper **Solution** menu offers *Enable Solver*, click it. Does `file modified` in `info` change when a decision is written? If not, the server writes to another folder (see `config\local.json`). |
| Timer is running but nothing happens | Delete the Timer wire and drag it onto the component again. |
| Model **tiny or huge** | Document units. With `scale` unconnected the model follows the units by itself; `info` shows e.g. `scale : 3.281 (auto: document units are Feet)`. To force a value, connect a Panel or Number Slider to `scale` (1 = metres, 1000 = mm). |
| Edited `model_builder.py` but the model looks the same | The loader reloads the file on the next tick. Colours and dimensions rebuild automatically; for other code changes, click a Button wired to `tick` once. |
| Colours look white or grey | Wrong display mode: use Shaded or Rendered, not Arctic, Pen or Technical. |

### Self-test (no Rhino needed)
From the repo folder:

```powershell
.\.venv\Scripts\python.exe grasshopper\model_builder.py --selftest
```

It checks reading, validation, snapping, the file watcher, the built-in (fallback) geometry for
every parameter combination, that node runs the JavaScript generator and returns the same
parameters, and the Grasshopper branch against a mocked Rhino (including the node-missing
fallback). It ends with `RESULT: N/N checks passed`.

The generator itself has its own tests:

```powershell
& "C:\Program Files\nodejs\node.exe" tests\model\test_pavilion.mjs
```

They build all 1512 parameter combinations and check timing, validity of every part, the exact
glazed share per façade, the canopy and roof rules, pins, tour, bad inputs and z-fighting.
