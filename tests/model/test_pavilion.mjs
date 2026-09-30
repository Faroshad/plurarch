// Tests for the single-source pavilion generator (site/js/model/pavilion.js, docs/MODEL.md).
//     node tests/model/test_pavilion.mjs            (add --quick to skip the slow z-fighting scan)
// Prints PASS/FAIL per check and exits non-zero on any failure.

import { buildPavilion, MODEL_VERSION, QUESTION_TAGS, _internal as I } from '../../site/js/model/pavilion.js';
import { hexProblem, aabb, distToBox, makeInside, makePlaneDistance, coplanarConflicts, makeRayTester } from './geom_checks.mjs';

const QUICK = process.argv.includes('--quick');
const results = [];
const check = (name, ok, detail = '') => results.push({ ok: !!ok, name, detail });

const MATS = ['timber', 'concrete', 'glass'];
const WR = [20, 25, 30, 35, 40, 45, 50, 55, 60];
const RA = [0, 5, 10, 15, 20, 25, 30, 35];
const CD = [0, 0.5, 1, 1.5, 2, 2.5, 3];
const KINDS = new Set(['wood', 'concrete', 'glass', 'metal', 'plaster', 'fabric', 'stone', 'ground', 'foliage', 'light', 'paint']);
const TAG_OK = (t) => /^(facade|glazing|roof|canopy)(:|$)/.test(t) || /^(interior|structure|landscape):/.test(t)
  || /^(ground|slab|door)(:|$)/.test(t);
const hasPrefix = (t, prefixes) => prefixes.some((p) => t === p || t.startsWith(p + ':'));
const fmt = (x, n = 1) => Number(x).toFixed(n);

// glazed share per facade, measured from the panes
function shares(model) {
  const roof = I.roofGeometry(model.params.roof_angle);
  const glazed = { front: 0, right: 0, back: 0, left: 0 };
  for (const q of model.parts) {
    if (q.t !== 'glazing:pane') continue;
    const b = aabb(q), cx = (b[0] + b[3]) / 2, cy = (b[1] + b[4]) / 2;
    const f = Math.abs(cy - I.Y0) < 0.6 ? 'front' : Math.abs(cy - I.Y1) < 0.6 ? 'back' : cx < 0 ? 'left' : 'right';
    const w = f === 'front' || f === 'back' ? b[3] - b[0] : b[4] - b[1];
    glazed[f] += w * (b[5] - b[2]);
  }
  const H = (y) => roof.under(y) - I.FLOOR;
  const wall = { front: 18 * H(I.Y0), back: 18 * H(I.Y1), right: 10 * H(0), left: 10 * H(0) };
  const out = {};
  for (const k of Object.keys(glazed)) out[k] = glazed[k] / wall[k];
  return out;
}

// ---------------------------------------------------------------------------
// 1. every combination builds; timing; validity; bounds; shares; canopy; roof; pins; tour
// ---------------------------------------------------------------------------
const combos = [];
for (const m of MATS) for (const w of WR) for (const r of RA) for (const c of CD) {
  combos.push({ facade_material: m, window_ratio: w, roof_angle: r, canopy_depth: c });
}
const times = [];
let nParts = [], badHex = [], badCyl = [], nan = [], outOfBounds = [], shareWorst = { err: 0 }, canopyBad = [];
let roofBad = [], pinBad = [], tourBad = [], statsBad = [], matBad = [], tagBad = [], walkBad = [], paramBad = [];
let errors = [], treeBad = [], coneBad = [], spacingBad = [];
const coneMin = {};
const CONE = (40 * Math.PI) / 180;
const allowedInWalk = (t) => /^(interior|structure|slab|door):/.test(t) || /^roof(:|$)/.test(t) || /^slab$/.test(t)
  || t === 'glazing:frame' || t === 'ground';

for (const prm of combos) {
  const label = `${prm.facade_material} ${prm.window_ratio}% ${prm.roof_angle}deg ${prm.canopy_depth}m`;
  let model;
  const t0 = performance.now();
  try { model = buildPavilion(prm); } catch (e) { errors.push(`${label}: ${e.message}`); continue; }
  times.push(performance.now() - t0);
  if (model.error) errors.push(`${label}: fallback model (${model.error})`);
  const { parts } = model;
  nParts.push(parts.length);
  if (JSON.stringify(model.params) !== JSON.stringify(prm)) paramBad.push(label);
  // parts
  const b = model.bounds;
  const inside = makeInside(parts);
  const planeDist = makePlaneDistance(parts);
  for (const q of parts) {
    if (!(q.m in model.materials)) matBad.push(`${label}: ${q.m}`);
    if (!TAG_OK(q.t)) tagBad.push(q.t);
    if (q.p) {
      const why = hexProblem(q.p);
      if (why) badHex.push(`${label}: ${q.t} ${why}`);
    } else if (q.cyl) {
      const c = q.cyl;
      if (!(c.length === 5 && c.every(Number.isFinite) && c[3] - c[2] >= 1e-3 && c[4] >= 5e-4 && q.seg >= 3)) badCyl.push(`${label}: ${q.t}`);
    } else badHex.push(`${label}: ${q.t} neither p nor cyl`);
    const bb = aabb(q);
    if (!bb.every(Number.isFinite)) nan.push(`${label}: ${q.t}`);
    if (bb[0] < b.min[0] - 1e-3 || bb[1] < b.min[1] - 1e-3 || bb[2] < b.min[2] - 1e-3
      || bb[3] > b.max[0] + 1e-3 || bb[4] > b.max[1] + 1e-3 || bb[5] > b.max[2] + 1e-3) outOfBounds.push(`${label}: ${q.t}`);
    // the walkable rectangle is free of walls / skin / landscape (furniture and columns are fine)
    const W = model.walkable;
    if (!allowedInWalk(q.t) && bb[3] > W.min[0] && bb[0] < W.max[0] && bb[4] > W.min[1] && bb[1] < W.max[1]
      && bb[5] > W.floor_z + 0.05 && bb[2] < W.floor_z + 2.1) walkBad.push(`${label}: ${q.t}`);
  }
  // glazed share
  for (const [f, s] of Object.entries(shares(model))) {
    const err = Math.abs(s - prm.window_ratio / 100);
    if (err > shareWorst.err) shareWorst = { err, where: `${label} ${f}: ${fmt(100 * s, 3)}%` };
  }
  // canopy: projection beyond the skin = canopy_depth, posts from 1.5 m, nothing at 0
  const st = I.STYLE[prm.facade_material];
  const can = parts.filter((q) => hasPrefix(q.t, ['canopy']));
  if (prm.canopy_depth === 0) {
    if (can.length) canopyBad.push(`${label}: canopy parts at depth 0`);
  } else {
    const tip = Math.min(...can.filter((q) => q.t === 'canopy:fascia' || q.t === 'canopy:slab').map((q) => aabb(q)[1]));
    const proj = (I.Y0 - st.out) - tip;
    const posts = can.filter((q) => q.t === 'canopy:post').length;
    const roof = I.roofGeometry(prm.roof_angle);
    const top = Math.max(...can.map((q) => aabb(q)[5]));
    if (Math.abs(proj - prm.canopy_depth) > 1e-6 || posts !== (prm.canopy_depth >= 1.5 ? 2 : 0) || top > roof.under(I.Y0) - 0.5) {
      canopyBad.push(`${label}: projection ${fmt(proj, 4)} posts ${posts}`);
    }
  }
  // roof rule: underside 4.5 m at the back wall, rising toward the entrance; overhangs
  {
    const roof = I.roofGeometry(prm.roof_angle);
    const slab = parts.find((q) => q.t === 'roof:slab');
    const bb = aabb(slab), p = slab.p;
    const zAt = (yy) => { // underside at y from the bottom corners (linear in y)
      const ys = [p[1], p[4], p[7], p[10]], zs = [p[2], p[5], p[8], p[11]];
      const i0 = ys.indexOf(Math.min(...ys)), i1 = ys.indexOf(Math.max(...ys));
      return zs[i0] + ((zs[i1] - zs[i0]) * (yy - ys[i0])) / (ys[i1] - ys[i0]);
    };
    const t = Math.tan((prm.roof_angle * Math.PI) / 180);
    const ohLow = 0.8 + (2.2 * prm.roof_angle) / 35;
    const ok = Math.abs(zAt(I.Y1) - 4.5) < 1e-6 && Math.abs(zAt(I.Y0) - (4.5 + 10 * t)) < 1e-6
      && Math.abs(bb[4] - (I.Y1 + ohLow)) < 1e-6 && Math.abs(bb[1] - (I.Y0 - 0.6)) < 1e-6
      && Math.abs(bb[0] - (I.X0 - 0.8)) < 1e-6 && Math.abs(bb[3] - (I.X1 + 0.8)) < 1e-6
      && Math.abs(roof.under(I.Y1) - 4.5) < 1e-9;
    if (!ok) roofBad.push(label);
  }
  // pins
  for (const q of Object.keys(QUESTION_TAGS)) {
    for (const mode of ['exterior', 'interior']) {
      const ps = model.pins.filter((p) => p.question === q && p.mode === mode);
      if (!ps.length) { pinBad.push(`${label}: no ${mode} pin for ${q}`); continue; }
      for (const pin of ps) {
        const prefixes = q === 'canopy_depth' && prm.canopy_depth === 0 ? ['door'] : QUESTION_TAGS[q];
        const near = parts.some((part) => hasPrefix(part.t, prefixes) && distToBox(pin.pos, aabb(part)) <= 0.5);
        const nlen = Math.hypot(...pin.normal);
        if (!near || Math.abs(nlen - 1) > 1e-3 || !pin.pos.every(Number.isFinite) || typeof pin.label !== 'string') {
          pinBad.push(`${label}: ${q} ${mode} pin at ${pin.pos}`);
        }
        for (let i = 0; i < parts.length; i++) {
          if (inside(i, pin.pos, 1e-4)) { pinBad.push(`${label}: ${q} ${mode} pin inside ${parts[i].t}`); break; }
        }
      }
    }
  }
  // tour
  if (model.tour.length < 5 || model.tour.length > 7) tourBad.push(`${label}: ${model.tour.length} stops`);
  const W = model.walkable;
  for (const stop of model.tour) {
    if (stop.id !== 'approach') {
      const [x, y, z] = stop.eye;
      const inRect = x > W.min[0] && x < W.max[0] && y > W.min[1] && y < W.max[1];
      if (!inRect || z - W.floor_z < 1.2) tourBad.push(`${label}: ${stop.id} eye outside the walkable area`);
    }
    for (let i = 0; i < parts.length; i++) {
      if (distToBox(stop.eye, aabb(parts[i])) < 0.15 && planeDist(i, stop.eye) < 0.15) {
        tourBad.push(`${label}: ${stop.id} eye inside / touching ${parts[i].t}`);
        break;
      }
    }
  }
  // view cone: from every tour stop (looking at its target) pins of >= 3 different questions
  // (roof stop, which looks steeply up: >= 2) lie within +-40 deg, face the eye and are in clear
  // sight (a ray against every opaque part; glass does not block). Approach: exterior pins.
  {
    const blocked = makeRayTester(parts, (q) => model.materials[q.m].kind !== 'glass');
    for (const s of model.tour) {
      const mode = s.id === 'approach' ? 'exterior' : 'interior';
      const v = s.target.map((x, i) => x - s.eye[i]), vl = Math.hypot(...v);
      const seen = new Set();
      for (const p of model.pins) {
        if (p.mode !== mode || seen.has(p.question)) continue;
        const d = p.pos.map((x, i) => x - s.eye[i]), dl = Math.hypot(...d);
        const cos = (d[0] * v[0] + d[1] * v[1] + d[2] * v[2]) / (dl * vl);
        if (Math.acos(Math.max(-1, Math.min(1, cos))) > CONE) continue;
        if (p.normal[0] * d[0] + p.normal[1] * d[1] + p.normal[2] * d[2] >= 0) continue;   // faces away
        if (blocked(s.eye, p.pos.map((x, i) => x - (d[i] / dl) * 0.05))) continue;
        seen.add(p.question);
      }
      coneMin[s.id] = Math.min(coneMin[s.id] ?? 9, seen.size);
      if (seen.size < (s.id === 'roof' ? 2 : 3)) coneBad.push(`${label} ${s.id}: only ${[...seen].join('/') || 'none'}`);
    }
  }
  // pins of different questions (same mode) do not crowd each other
  for (let i = 0; i < model.pins.length; i++) {
    for (let j = i + 1; j < model.pins.length; j++) {
      const a = model.pins[i], b2 = model.pins[j];
      if (a.mode === b2.mode && a.question !== b2.question && Math.hypot(...a.pos.map((v, k) => v - b2.pos[k])) < 0.45) {
        spacingBad.push(`${label}: ${a.question}/${b2.question} at ${a.pos}`);
      }
    }
  }
  // trees clear of the roof, the plinth and the canopy
  {
    const hard = parts.filter((q) => /^(roof|canopy|slab|facade)(:|$)/.test(q.t)).map(aabb);
    for (const tree of parts.filter((q) => q.t === 'landscape:tree')) {
      const a = aabb(tree);
      if (hard.some((h) => a[0] < h[3] && a[3] > h[0] && a[1] < h[4] && a[4] > h[1] && a[2] < h[5] && a[5] > h[2])) {
        treeBad.push(label);
        break;
      }
    }
  }
  // stats
  const byTag = {};
  for (const q of parts) byTag[q.t] = (byTag[q.t] || 0) + 1;
  if (model.stats.parts !== parts.length || JSON.stringify(byTag) !== JSON.stringify(model.stats.byTag)) statsBad.push(label);
}

const sum = (a) => a.reduce((s, v) => s + v, 0);
const first = (a) => (a.length ? `${a.length} problems, e.g. ${a.slice(0, 3).join(' | ')}` : '');
check(`all ${combos.length} combinations build without throwing`, errors.length === 0 && times.length === combos.length, first(errors));
check(`build time: mean ${fmt(sum(times) / times.length, 2)} ms, max ${fmt(Math.max(...times), 1)} ms (< 30 ms)`,
  Math.max(...times.slice(5)) < 30, '');
check(`part count ${Math.min(...nParts)}..${Math.max(...nParts)} (target 800-2500)`,
  Math.min(...nParts) >= 800 && Math.max(...nParts) <= 2500);
check('normalized params echo the (valid) input', paramBad.length === 0, first(paramBad));
check('no NaN / Infinity in any part', nan.length === 0, first(nan));
check('every hexahedron valid (bottom and top CCW, edges >= 1 mm, positive volume)', badHex.length === 0, first(badHex));
check('every cylinder valid (finite, height >= 1 mm, r > 0, seg >= 3)', badCyl.length === 0, first(badCyl));
check('every part lies inside model.bounds', outOfBounds.length === 0, first(outOfBounds));
check('every part uses a defined material', matBad.length === 0, first(matBad));
check('every tag has a contract prefix', tagBad.length === 0, first([...new Set(tagBad)]));
check(`glazed share of every facade == window_ratio (max error ${fmt(100 * shareWorst.err, 4)} %-points)`,
  shareWorst.err <= 0.005, shareWorst.where || '');
check('canopy projects exactly canopy_depth beyond the skin; posts from 1.5 m; none at 0; below the roof',
  canopyBad.length === 0, first(canopyBad));
check('roof: underside 4.5 m at the back wall, 4.5 + 10 tan(angle) at the front, overhangs 0.6 / 0.8 / 0.8-3.0 m',
  roofBad.length === 0, first(roofBad));
check('every question has >= 1 exterior and >= 1 interior pin, each within 0.5 m of a part of its tags, not inside a part',
  pinBad.length === 0, first(pinBad));
check('tour: 5-7 stops, eyes inside the walkable rectangle (except the approach), not in or touching any part',
  tourBad.length === 0, first(tourBad));
check('walkable rectangle free of walls, skin and landscape up to 2.1 m (only furniture and columns inside)',
  walkBad.length === 0, first(walkBad));
check(`view cone: every tour stop sees pins of >= 3 questions (roof stop >= 2) within +-40 deg, in clear sight (min per stop ${Object.entries(coneMin).map(([k, n]) => k + ' ' + n).join(', ')})`, coneBad.length === 0, first(coneBad));
check('pins of different questions (same mode) at least 0.45 m apart', spacingBad.length === 0, first(spacingBad));
check('trees clear of the roof, canopy, plinth and facades', treeBad.length === 0, first(treeBad));
check('stats.parts and stats.byTag match the parts', statsBad.length === 0, first(statsBad));

// ---------------------------------------------------------------------------
// 2. determinism, contract basics, bad inputs
// ---------------------------------------------------------------------------
{
  let diff = 0;
  for (let i = 0; i < combos.length; i += 7) {
    if (JSON.stringify(buildPavilion(combos[i])) !== JSON.stringify(buildPavilion({ ...combos[i] }))) diff++;
  }
  check(`deterministic: identical output on repeated builds (${Math.ceil(combos.length / 7)} combos)`, diff === 0, `${diff} differ`);
}
{
  const m = buildPavilion({});
  const keys = ['version', 'units', 'up', 'params', 'materials', 'parts', 'pins', 'tour', 'walkable', 'bounds', 'stats'];
  const kindsOk = Object.values(m.materials).every((d) => KINDS.has(d.kind) && d.color.length === 3
    && d.color.every((c) => Number.isInteger(c) && c >= 0 && c <= 255) && d.opacity >= 0 && d.opacity <= 1
    && typeof d.label === 'string' && d.roughness >= 0 && d.roughness <= 1 && d.metalness >= 0 && d.metalness <= 1);
  check(`contract shape: keys, version ${MODEL_VERSION}, units m, up z, material kinds/colours`,
    keys.every((k) => k in m) && m.version === '2.0.0' && m.units === 'm' && m.up === 'z' && kindsOk);
  check('QUESTION_TAGS as in the contract',
    JSON.stringify(QUESTION_TAGS) === JSON.stringify({ facade_material: ['facade'], window_ratio: ['glazing'], roof_angle: ['roof'], canopy_depth: ['canopy'] }));
  const glassOk = m.materials.glazing.kind === 'glass' && m.materials.glazing.opacity >= 0.3 && m.materials.glazing.opacity <= 0.4;
  const lightOk = Object.values(m.materials).some((d) => d.kind === 'light' && Array.isArray(d.emissive));
  check('glazing opacity 0.3-0.4; an emissive light material exists', glassOk && lightOk);
}
{
  const D = { facade_material: 'timber', window_ratio: 40, roof_angle: 15, canopy_depth: 1.5 };
  const trap = new Proxy({}, { get() { throw new Error('trap'); } });
  const cases = [
    [null, D], [undefined, D], ['glass', D], [42, D], [[1, 2], D], [trap, D],
    [{ facade_material: 'brick' }, D],
    [{ facade_material: ' GLASS ' }, { ...D, facade_material: 'glass' }],
    [{ window_ratio: 999 }, { ...D, window_ratio: 60 }],
    [{ window_ratio: -5 }, { ...D, window_ratio: 20 }],
    [{ window_ratio: '45' }, { ...D, window_ratio: 45 }],
    [{ window_ratio: 42 }, { ...D, window_ratio: 40 }],
    [{ window_ratio: true }, D],
    [{ window_ratio: 'abc' }, D],
    [{ roof_angle: -20 }, { ...D, roof_angle: 0 }],
    [{ roof_angle: 17.5 }, { ...D, roof_angle: 20 }],
    [{ canopy_depth: NaN }, D],
    [{ canopy_depth: Infinity }, D],
    [{ canopy_depth: 1.3 }, { ...D, canopy_depth: 1.5 }],
    [{ canopy_depth: 7 }, { ...D, canopy_depth: 3 }],
    [{ facade_material: 3, window_ratio: null, roof_angle: {}, canopy_depth: [] }, D],
  ];
  const bad = [];
  for (const [inp, want] of cases) {
    try {
      const m = buildPavilion(inp);
      if (JSON.stringify(m.params) !== JSON.stringify(want) || m.parts.length < 800 || m.error) bad.push(`${String(inp && inp.constructor ? JSON.stringify(inp) : inp)} -> ${JSON.stringify(m.params)}`);
    } catch (e) { bad.push(`threw ${e.message}`); }
  }
  check(`bad inputs (${cases.length} cases: null, strings, out of range, unknown material, traps) -> defaults / clamped, no throw`,
    bad.length === 0, bad.slice(0, 3).join(' | '));
}

// ---------------------------------------------------------------------------
// 3. every parameter makes a big change
// ---------------------------------------------------------------------------
{
  const base = { facade_material: 'timber', window_ratio: 40, roof_angle: 15, canopy_depth: 1.5 };
  const sig = (m, prefixes) => JSON.stringify(m.parts.filter((q) => hasPrefix(q.t, prefixes)));
  const changes = [];
  for (const [key, a, b] of [['facade_material', 'timber', 'glass'], ['window_ratio', 20, 60], ['roof_angle', 0, 35], ['canopy_depth', 0, 3]]) {
    const ma = buildPavilion({ ...base, [key]: a }), mb = buildPavilion({ ...base, [key]: b });
    const pre = QUESTION_TAGS[key];
    const na = ma.parts.filter((q) => hasPrefix(q.t, pre)).length, nb = mb.parts.filter((q) => hasPrefix(q.t, pre)).length;
    changes.push(`${key}: ${na} -> ${nb} parts`);
    if (sig(ma, pre) === sig(mb, pre)) changes.push(`${key} UNCHANGED`);
  }
  const sh = (m) => new Set(m.parts.filter((q) => q.t.startsWith('facade:')).map((q) => q.t));
  const t = sh(buildPavilion({ ...base, facade_material: 'timber' })), c = sh(buildPavilion({ ...base, facade_material: 'concrete' }));
  const g = sh(buildPavilion({ ...base, facade_material: 'glass' }));
  const shapesOk = t.has('facade:fin') && !t.has('facade:panel') && c.has('facade:panel') && !c.has('facade:fin')
    && g.has('facade:mullion') && g.has('facade:spandrel') && !g.has('facade:fin');
  check(`every parameter changes its elements (${changes.join('; ')}); fins / panels / curtain wall differ`,
    !changes.some((s) => s.includes('UNCHANGED')) && shapesOk);
}

// ---------------------------------------------------------------------------
// 4. z-fighting: coplanar overlapping visible faces
// ---------------------------------------------------------------------------
{
  const sample = QUICK ? [
    { facade_material: 'timber', window_ratio: 40, roof_angle: 15, canopy_depth: 1.5 },
  ] : [];
  if (!QUICK) {
    for (const m of MATS) for (const w of [20, 40, 45, 60]) for (const r of [0, 15, 35]) for (const c of [0, 0.5, 1.5, 3]) {
      sample.push({ facade_material: m, window_ratio: w, roof_angle: r, canopy_depth: c });
    }
    sample.push({ facade_material: 'concrete', window_ratio: 40, roof_angle: 15, canopy_depth: 1.5 });
    sample.push({ facade_material: 'glass', window_ratio: 50, roof_angle: 20, canopy_depth: 2 });
  }
  let diffMat = [], sameMat = [];
  const t0 = performance.now();
  for (const prm of sample) {
    const label = `${prm.facade_material} ${prm.window_ratio}% ${prm.roof_angle}deg ${prm.canopy_depth}m`;
    for (const c of coplanarConflicts(buildPavilion(prm).parts)) {
      const s = `${label}: ${c.a.t}(${c.a.m}) / ${c.b.t}(${c.b.m}) ${fmt(c.area, 4)} m2 at ${c.at.map((v) => fmt(v, 2))}`;
      (c.sameMaterial ? sameMat : diffMat).push(s);
    }
  }
  check(`no visible coplanar overlapping faces of different materials (${sample.length} designs, ${fmt((performance.now() - t0) / 1000, 1)} s)`,
    diffMat.length === 0, first(diffMat));
  check('no visible coplanar overlapping faces of the same material (texture flicker)', sameMat.length === 0, first(sameMat));
}

// ---------------------------------------------------------------------------
// summary
// ---------------------------------------------------------------------------
console.log(`Plurarch pavilion generator tests (model ${MODEL_VERSION}, Node ${process.version})\n`);
for (const r of results) console.log(`[${r.ok ? 'PASS' : 'FAIL'}] ${r.name}${!r.ok && r.detail ? `\n       <- ${r.detail}` : ''}`);
const m0 = buildPavilion({});
console.log('\nDefault design: %d parts, %d materials, %d pins, %d tour stops', m0.parts.length,
  Object.keys(m0.materials).length, m0.pins.length, m0.tour.length);
for (const mat of MATS) {
  const counts = [20, 40, 60].map((w) => buildPavilion({ facade_material: mat, window_ratio: w, roof_angle: 35, canopy_depth: 3 }).parts.length);
  console.log(`  ${mat.padEnd(8)} parts at 20/40/60 pct (35 deg, 3 m): ${counts.join(' / ')}`);
}
const passed = results.filter((r) => r.ok).length;
console.log(`\nRESULT: ${passed}/${results.length} checks passed`);
process.exit(passed === results.length ? 0 : 1);
