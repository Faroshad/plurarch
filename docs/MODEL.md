# Pavilion model contract (v2: detailed model, phone 3D view and tour)

One geometry generator, written once in JavaScript, is used everywhere:

```
site/js/model/pavilion.js   pure ES module: buildPavilion(params) -> Model   (no DOM, no three.js)
   ├── phone (site/index.html): three.js viewer, live preview of the participant's own choices
   └── Rhino (grasshopper/model_builder.py): calls  node tools/pavilion_cli.mjs  and meshes the parts
```

So the phone and the projector show the **same** design from the same code. The phone can
also rebuild the model instantly for any parameter set: the voter's draft, the current design, or
the applied decision.

## API

```js
import { buildPavilion, MODEL_VERSION, QUESTION_TAGS } from './model/pavilion.js';
const model = buildPavilion({ facade_material: 'glass', window_ratio: 45, roof_angle: 20, canopy_depth: 2 });
```

- `params`: the four design parameters (keys from `config/parameters.json`). The generator clamps
  and snaps values itself and never throws on bad input (it falls back to defaults per key).
- Deterministic: the same params always give the same output (no randomness; any "variation", for
  example trees, uses a fixed seed).
- Fast: under 30 ms on a mid-range phone for the full model.

CLI (for Rhino and tests): `node tools/pavilion_cli.mjs '{"facade_material":"glass",...}'` (or the JSON
on stdin) prints the Model as compact JSON on stdout. Exit code 0; 1 with a message on stderr on failure.

## Model object

```js
{
  version: '2.0.0',
  units: 'm',
  up: 'z',                      // Rhino convention: x = length (18 m), y = depth (10 m), z = up
  params: { ...normalized },
  materials: {
    <key>: { label, kind, color: [r, g, b], opacity, roughness, metalness, emissive?: [r, g, b] }
    // kind ∈ 'wood' | 'concrete' | 'glass' | 'metal' | 'plaster' | 'fabric' | 'stone' | 'ground'
    //        | 'foliage' | 'light' | 'paint'. The viewer picks a procedural texture by kind.
    // colour values 0-255; opacity 0-1 (glass < 1).
  },
  parts: [
    { m: <materialKey>, t: <tag>, p: [x0,y0,z0, x1,y1,z1, ... 8 corners] },   // hexahedron
    { m: <materialKey>, t: <tag>, cyl: [cx, cy, z0, z1, r], seg: 12 },       // vertical cylinder
  ],
  pins: [
    { question: 'facade_material', mode: 'exterior' | 'interior', pos: [x,y,z], normal: [x,y,z], label: 'Façade' }
  ],
  tour: [
    { id: 'entrance', label: 'Entrance', eye: [x,y,z], target: [x,y,z] }       // eye height about 1.6 m
  ],
  walkable: { min: [x, y], max: [x, y], floor_z: z },  // interior rectangle a visitor may walk in
  bounds: { min: [x,y,z], max: [x,y,z] },
  stats: { parts: n, byTag: { <tag>: n } }
}
```

Hexahedron corner order (the same as Rhino `Brep.CreateFromBox`): corners 0–3 are the bottom quad,
counter-clockwise seen from above; corners 4–7 are the top quad, in the same order (4 is above 0).
Tops may be sloped (roof, walls under the roof).

## Tags and which question each element belongs to

Tags are `category` or `category:detail` strings. `QUESTION_TAGS` (exported) maps each question to
the tag prefixes it controls. The viewer highlights those parts when a pin is open:

```js
QUESTION_TAGS = {
  facade_material: ['facade'],             // skin: fins / panels / mullions, interior wall lining
  window_ratio:    ['glazing'],            // glass panes (and their frames: 'glazing:frame')
  roof_angle:      ['roof'],               // roof slab, fascia, exposed beams ('roof:beam'), ceiling
  canopy_depth:    ['canopy'],             // canopy slab, fascia, posts
}
```

Other tags: `ground`, `landscape:*` (trees, benches, paving), `slab`, `structure:*` (columns),
`door`, `interior:*` (chairs, stage, lectern, screen, desk, displays, lights, core walls).
The flat `ground` part (tag `ground`, material kind `ground`) may be replaced by the viewer's endless ground.

## Pins
- At least one `exterior` and one `interior` pin per question, placed on a clearly visible point of the
  element the question is about (for example the middle of the entrance façade, a window pane on the
  long wall, the roof edge, the canopy tip). `normal` points away from the surface, so the viewer
  can hide pins that face away or are hidden behind geometry.
- Pins move with the geometry: for example the canopy pin sits at the canopy tip for any depth, and
  at the entrance when the depth is 0.

## Tour
- 5–7 stops: the approach outside the entrance (under the canopy), entrance, hall centre, stage,
  gallery or exhibition side, the window wall, and one looking up at the roof structure. Eyes about
  1.6 m above the floor, inside the walkable rectangle except for the approach stop.
