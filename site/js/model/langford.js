// Langford Architecture Center, Building A (Texas A&M): the real building for the phone and the stage.
// Same Model contract as pavilion.js (docs/MODEL.md: materials, parts, pins, tour, walkable, bounds,
// stats) so js/viewer3d.js keeps working, with one more part type for the IFC meshes:
//   { m, t, tri: { pos: Float32Array (xyz, model frame), idx: Uint16Array | null }, noOcclude? }
// The base geometry (site/models/langford/base.json + base.bin, see its README) is decoded ONCE by
// loadLangford(); buildLangford(params) only re-assigns materials and adds the fin boxes, so a
// rebuild on a slider change stays fast. The plan rule (docs/LANGFORD.md) is identical to
// design_mcp/langford_plan.py (tests/model/test_langford.mjs checks the parity):
//   glazed SE lites  = the first round_half_up(share/100 * N) lites by rank; the others solid infill
//   glazed lanterns  = the first n lanterns by index; the others solid
//   fins             = one box per anchor: 0.2 m thick, from base along dir, length = depth (none at 0)
//   solid finish     = every solid infill panel takes the chosen finish
// Frame: metres, X along the long axis (NE), Y toward NW, Z up, Z = 0 at Level 1; the SE facade faces -Y.
// Pure ES module: no DOM, no three.js (fetch is used only when loadLangford() gets no data).

export const MODEL_VERSION = 'langford-3.0';

export const QUESTION_TAGS = {
  infill_finish: ['facade'], // the solid infill panels
  se_glass_share: ['glazing'], // the 142 ranked SE studio lites (glazed or switched to solid)
  fin_depth: ['fins'], // the generated sunshade fins
  skylights_open: ['roof'], // the 12 north-light lanterns
};

// Defaults = the building as modelled (all lites and lanterns glazed, no fins, concrete finish).
export const DEFAULTS = { infill_finish: 'concrete', se_glass_share: 100, fin_depth: 0, skylights_open: 12 };
const SPEC = { se_glass_share: [40, 100, 10], fin_depth: [0, 1.2, 0.3], skylights_open: [0, 12, 2] };
const FINISHES = ['concrete', 'aluminium', 'fritted_glass'];

/** Complete, clamped, snapped parameters. Never throws; defaults fill any gap. */
export function normalizeParams(params) {
  const src = params && typeof params === 'object' ? params : {};
  const out = { ...DEFAULTS };
  if (FINISHES.includes(src.infill_finish)) out.infill_finish = src.infill_finish;
  for (const [k, [lo, hi, st]] of Object.entries(SPEC)) {
    const v = typeof src[k] === 'string' ? Number(src[k].trim()) : Number(src[k]);
    if (src[k] == null || src[k] === '' || !Number.isFinite(v)) continue;
    const s = lo + Math.round((Math.min(hi, Math.max(lo, v)) - lo) / st) * st;
    out[k] = Math.round(Math.min(hi, Math.max(lo, s)) * 1e6) / 1e6;
  }
  // the reviewer's lantern-panel layout (design_mcp/skylights.py): one bit per lantern glazing panel in
  // ascending Revit id order, hex, most significant bit first
  if (typeof src.skylight_mask === 'string' && /^[0-9a-f]{20,64}$/.test(src.skylight_mask)) {
    out.skylight_mask = src.skylight_mask;
    if (src.skylight_layout) out.skylight_layout = String(src.skylight_layout);
  }
  return out;
}

/** The glazed lantern panel ids of a layout mask, or null for the plain whole-lantern rule. */
export function skyGlazedIds(P) {
  if (!BASE || !P || !P.skylight_mask) return null;
  if (!BASE.skyIds) BASE.skyIds = BASE.elements.filter((e) => e.q === 'skylights_open').map((e) => e.id).sort((a, b) => a - b);
  const bits = [...P.skylight_mask].map((c) => parseInt(c, 16).toString(2).padStart(4, '0')).join('');
  const set = new Set();
  BASE.skyIds.forEach((id, i) => { if (bits[i] === '1') set.add(id); });
  return set;
}

/* ------------------------------------------------------------------ base geometry */
let BASE = null;
let LOADING = null;

const rgb255 = (c) => [Math.round(c[0] * 255), Math.round(c[1] * 255), Math.round(c[2] * 255)];

/**
 * Fetch and decode the base geometry once. Returns a promise of the decoded base.
 * opts.url: folder URL of base.json/base.bin (default: ../../models/langford/ next to this module),
 * or opts.json + opts.bin (an object and an ArrayBuffer) to skip the network (Node tests).
 */
export function loadLangford(opts = {}) {
  if (BASE) return Promise.resolve(BASE);
  if (LOADING) return LOADING;
  LOADING = (async () => {
    let json = opts.json; let bin = opts.bin;
    if (!json || !bin) {
      const dir = opts.url ? new URL(opts.url, globalThis.location ? globalThis.location.href : undefined) : new URL('../../models/langford/', import.meta.url);
      const get = async (name, kind) => {
        const res = await fetch(new URL(name, dir), { cache: 'force-cache' });
        if (!res.ok) throw new Error(`${name}: HTTP ${res.status}`);
        return kind === 'json' ? res.json() : res.arrayBuffer();
      };
      json = await get('base.json', 'json');
      bin = await get(json.bin || 'base.bin', 'bin');
    }
    BASE = decode(json, bin);
    return BASE;
  })();
  LOADING.catch(() => { LOADING = null; });
  return LOADING;
}

export function isLoaded() { return !!BASE; }

function decode(json, bin) {
  const t0 = nowMs();
  const L = json.layout;
  const q = json.quantization;
  const qp = new Uint16Array(bin, L.positions.offset, L.positions.count * 3);
  const qi = new Uint16Array(bin, L.indices.offset, L.indices.count);
  const pos = new Float32Array(qp.length);
  for (let i = 0; i < qp.length; i += 3) {
    pos[i] = q.min[0] + qp[i] * q.scale[0];
    pos[i + 1] = q.min[1] + qp[i + 1] * q.scale[1];
    pos[i + 2] = q.min[2] + qp[i + 2] * q.scale[2];
  }
  const elements = json.elements.map((e) => {
    const [vo, vc] = e.v; const [io, ic] = e.i;
    const p = pos.subarray(vo * 3, (vo + vc) * 3);
    let x0 = Infinity; let y0 = Infinity; let z0 = Infinity; let x1 = -Infinity; let y1 = -Infinity; let z1 = -Infinity;
    for (let k = 0; k < p.length; k += 3) {
      if (p[k] < x0) x0 = p[k]; if (p[k] > x1) x1 = p[k];
      if (p[k + 1] < y0) y0 = p[k + 1]; if (p[k + 1] > y1) y1 = p[k + 1];
      if (p[k + 2] < z0) z0 = p[k + 2]; if (p[k + 2] > z1) z1 = p[k + 2];
    }
    return { ...e, tri: { pos: p, idx: qi.subarray(io, io + ic) }, box: [x0, y0, z0, x1, y1, z1] };
  });
  const base = {
    json, elements, seN: json.se_n || elements.filter((e) => e.q === 'se_glass_share').length,
    lanternsN: json.lanterns_n || 12,
    anchors: json.fin_anchors || [],
    decodeMs: 0,
  };
  base.static = buildStatic(base);
  base.decodeMs = nowMs() - t0;
  return base;
}

/* ------------------------------------------------------------------ site: terrain, trees, context */
// A light terrain: inverse-distance heights from the tree bases and the paving / planting tops,
// dipped under the building, blending to the outer ground level at its edge (the viewer's endless
// ground continues there). Trees and the neighbouring buildings then stand on it.
const TERRAIN = { x0: -70, x1: 140, y0: -95, y1: 124, step: 3 };

function buildStatic(base) {
  const els = base.elements;
  const roof = els.find((e) => e.mat === 'roof');
  const fp = roof ? roof.box : [-1.1, 0.5, 0, 65.8, 52.2, 18.3]; // building footprint (roof outline)
  const trees = (base.json.trees && base.json.trees.items) || [];

  // height samples
  const S = [];
  for (const t of trees) S.push([t[0], t[1], t[2]]);
  for (const e of els) {
    if (e.mat !== 'paving' && e.mat !== 'planting') continue;
    if (/stair|tread|landing/i.test(e.type || '')) continue; // steps and the entrance bridge are not ground
    const b = e.box;
    const lift = e.mat === 'planting' ? 0.25 : 0.12; // stay just under the surfaces
    S.push([(b[0] + b[3]) / 2, (b[1] + b[4]) / 2, b[5] - lift]);
    S.push([b[0] + 0.2, b[1] + 0.2, b[5] - lift], [b[3] - 0.2, b[4] - 0.2, b[5] - lift]);
  }
  const T = TERRAIN;
  const nx = Math.round((T.x1 - T.x0) / T.step) + 1;
  const ny = Math.round((T.y1 - T.y0) / T.step) + 1;
  const H = new Float32Array(nx * ny);
  const inFoot = (x, y, m) => x > fp[0] - m && x < fp[3] + m && y > fp[1] - m && y < fp[4] + m;
  for (let j = 0; j < ny; j++) {
    for (let i = 0; i < nx; i++) {
      const x = T.x0 + i * T.step; const y = T.y0 + j * T.step;
      let ws = 0; let hs = 0;
      for (const s of S) {
        const d2 = (s[0] - x) * (s[0] - x) + (s[1] - y) * (s[1] - y) + 1;
        const w = 1 / (d2 * d2);
        ws += w; hs += w * s[2];
      }
      H[j * nx + i] = ws > 0 ? hs / ws : 2.5;
    }
  }
  // Sunken site parts (the SE areaway: its paving strip, stepped planters and retaining walls, more
  // than 1 m below the site): the terrain goes under each of them (their boxes widened by 2 m so the
  // 3 m grid catches the narrow terraces), else the lawn would fill the areaway.
  const bases = trees.filter((t) => inFoot(t[0], t[1], 40)).map((t) => t[2]).sort((a, b) => a - b);
  const siteZ = bases.length ? bases[bases.length >> 1] : 2.5;
  const sunken = els.filter((e) => e.box && (e.mat === 'paving' || e.mat === 'planting' || /site/i.test(e.type || ''))
    && !/stair|tread|landing|bridge/i.test(e.type || '') && e.box[5] < siteZ - 1);
  const dipAt = (x, y) => {
    let d = Infinity;
    for (const e of sunken) {
      const b = e.box;
      if (x > b[0] - 2 && x < b[3] + 2 && y > b[1] - 2 && y < b[4] + 2) d = Math.min(d, b[5] - 0.15);
    }
    return d;
  };
  const lowest = sunken.reduce((m, e) => Math.min(m, e.box[5] - 0.15), Infinity);
  // The outer ground (the viewer's ground plane) sits just under the low end of the site (and under
  // the areaway), the terrain blends down to it near its edge and never dips below it, so the plane
  // never shows through the lawn.
  // (its level: the low end of the ground within 30 m of the building; sparse corners further out
  // are lifted to it)
  const around = [];
  for (let j = 0; j < ny; j++) {
    for (let i = 0; i < nx; i++) {
      const x = T.x0 + i * T.step; const y = T.y0 + j * T.step;
      if (!inFoot(x, y, 1) && inFoot(x, y, 30)) around.push(H[j * nx + i]);
    }
  }
  around.sort((a, b) => a - b);
  const groundZ = Math.min((around.length ? around[Math.floor(around.length * 0.03)] : 2.5) - 0.06, lowest - 0.12);
  for (let j = 0; j < ny; j++) {
    for (let i = 0; i < nx; i++) {
      const x = T.x0 + i * T.step; const y = T.y0 + j * T.step;
      let h = Math.max(H[j * nx + i], groundZ + 0.06);
      const edge = Math.min(x - T.x0, T.x1 - x, y - T.y0, T.y1 - y);
      const t = Math.min(1, Math.max(0, edge / 40));
      h = groundZ + 0.04 + (h - groundZ - 0.04) * t * t * (3 - 2 * t);
      h = Math.min(h, dipAt(x, y)); // the areaway
      if (inFoot(x, y, 1)) h = Math.min(h, -0.5); // under the building
      H[j * nx + i] = h;
    }
  }
  const heightAt = (x, y) => {
    const fx = (x - T.x0) / T.step; const fy = (y - T.y0) / T.step;
    if (fx < 0 || fy < 0 || fx > nx - 1 || fy > ny - 1) return groundZ;
    const i = Math.min(nx - 2, Math.floor(fx)); const j = Math.min(ny - 2, Math.floor(fy));
    const u = fx - i; const v = fy - j;
    const a = H[j * nx + i]; const b = H[j * nx + i + 1]; const c = H[(j + 1) * nx + i]; const d = H[(j + 1) * nx + i + 1];
    return a * (1 - u) * (1 - v) + b * u * (1 - v) + c * (1 - u) * v + d * u * v;
  };
  const tpos = new Float32Array(nx * ny * 3);
  for (let j = 0; j < ny; j++) {
    for (let i = 0; i < nx; i++) {
      const k = (j * nx + i) * 3;
      tpos[k] = T.x0 + i * T.step; tpos[k + 1] = T.y0 + j * T.step; tpos[k + 2] = H[j * nx + i];
    }
  }
  const tidx = new Uint16Array((nx - 1) * (ny - 1) * 6);
  let w = 0;
  for (let j = 0; j < ny - 1; j++) {
    for (let i = 0; i < nx - 1; i++) {
      const a = j * nx + i; const b = a + 1; const c = a + nx; const d = c + 1;
      tidx[w++] = a; tidx[w++] = b; tidx[w++] = d; // counter-clockwise from above (+z)
      tidx[w++] = a; tidx[w++] = d; tidx[w++] = c;
    }
  }
  const parts = [{ m: 'grass', t: 'ground:terrain', tri: { pos: tpos, idx: tidx }, noOcclude: true }];

  // low-poly trees (a hexagonal trunk and a two-frustum crown, rotated by a fixed pseudo-random angle),
  // merged into three static meshes so a rebuild never touches them
  const rnd = mulberry32(0x5eed);
  const meshes = { bark: [[], []], foliage: [[], []], foliage_light: [[], []] };
  const ring = (cx, cy, z, r, n, rot) => Array.from({ length: n }, (_, i) => [cx + r * Math.cos(rot + (i * 2 * Math.PI) / n), cy + r * Math.sin(rot + (i * 2 * Math.PI) / n), z]);
  const band = (mesh, lo, hi, capTop) => { // a ring of side quads between two loops (+ an optional top cap)
    const [P, I] = mesh; const o = P.length / 3; const n = lo.length;
    for (const v of lo) P.push(...v);
    for (const v of hi) P.push(...v);
    for (let i = 0; i < n; i++) { const a = o + i; const b = o + ((i + 1) % n); I.push(a, b, n + b, a, n + b, n + a); }
    if (capTop) for (let i = 1; i < n - 1; i++) I.push(o + n, o + n + i, o + n + i + 1);
  };
  for (const t of trees) {
    const [x, y, , hh, rr] = t;
    if (x > fp[0] - 0.5 && x < fp[3] + 0.5 && y > fp[1] - 0.5 && y < fp[4] + 0.5) continue; // inside the building outline
    const h = Math.max(2, hh); const r = Math.max(0.8, rr);
    const z = heightAt(x, y);
    const rot = rnd() * Math.PI;
    const crown = rnd() < 0.5 ? meshes.foliage : meshes.foliage_light;
    const tr = Math.max(0.12, h * 0.022);
    band(meshes.bark, ring(x, y, z - 0.2, tr, 6, rot), ring(x, y, z + h * 0.42, tr * 0.8, 6, rot), false);
    band(crown, ring(x, y, z + h * 0.28, r * 0.35, 5, rot), ring(x, y, z + h * 0.62, r, 5, rot + 0.3), false);
    band(crown, ring(x, y, z + h * 0.62, r, 5, rot + 0.3), ring(x, y, z + h, r * 0.12, 5, rot + 0.6), true);
  }
  for (const [m, [P, I]] of Object.entries(meshes)) {
    if (!I.length) continue;
    const big = P.length / 3 > 65535;
    // trees never hide a pin or stop a walk (one merged mesh per material: its box covers the site)
    parts.push({ m, t: 'landscape:tree', tri: { pos: new Float32Array(P), idx: big ? new Uint32Array(I) : new Uint16Array(I) }, noOcclude: true });
  }

  // context buildings: simple gray massing (extruded footprints)
  const ctx = (base.json.context_buildings && base.json.context_buildings.items) || [];
  for (const c of ctx) {
    // the base goes into the terrain (z0 is the GIS level; the terrain is smoothed and lower at its edge)
    const zb = Math.min(Number(c.z0) || 0, ...(Array.isArray(c.ring) ? c.ring : []).map((p) => heightAt(Number(p[0]) || 0, Number(p[1]) || 0))) - 0.3;
    const tri = prism(c.ring, zb, Number(c.z1) || 10);
    if (tri) parts.push({ m: 'context', t: 'landscape:context', tri });
  }
  const obstacles = [[fp[0], fp[1], fp[3], fp[4]]];
  for (const c of ctx) {
    if (!Array.isArray(c.ring) || c.ring.length < 3) continue;
    let x0 = Infinity; let y0 = Infinity; let x1 = -Infinity; let y1 = -Infinity;
    for (const [x, y] of c.ring) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
    obstacles.push([x0, y0, x1, y1]);
  }
  return { parts, heightAt, groundZ, footprint: fp, obstacles };
}

// An extruded polygon (walls + flat top) as a triangle mesh; the ring may be concave.
function prism(ring, z0, z1) {
  if (!Array.isArray(ring) || ring.length < 3) return null;
  let pts = ring.map((p) => [Number(p[0]), Number(p[1])]).filter((p) => Number.isFinite(p[0]) && Number.isFinite(p[1]));
  if (pts.length > 1 && pts[0][0] === pts[pts.length - 1][0] && pts[0][1] === pts[pts.length - 1][1]) pts.pop();
  if (pts.length < 3) return null;
  let area = 0;
  for (let i = 0; i < pts.length; i++) { const a = pts[i]; const b = pts[(i + 1) % pts.length]; area += a[0] * b[1] - b[0] * a[1]; }
  if (area < 0) pts = pts.reverse(); // counter-clockwise from above
  const n = pts.length;
  const pos = new Float32Array(n * 2 * 3);
  for (let i = 0; i < n; i++) {
    pos.set([pts[i][0], pts[i][1], z0], i * 3);
    pos.set([pts[i][0], pts[i][1], z1], (n + i) * 3);
  }
  const idx = [];
  for (let i = 0; i < n; i++) { // walls, outward
    const a = i; const b = (i + 1) % n;
    idx.push(a, b, n + b, a, n + b, n + a);
  }
  for (const [a, b, c] of earClip(pts)) idx.push(n + a, n + b, n + c); // roof
  return { pos, idx: new Uint16Array(idx) };
}

function earClip(pts) {
  const out = [];
  const V = pts.map((_, i) => i);
  const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const inside = (p, a, b, c) => cross(a, b, p) >= 0 && cross(b, c, p) >= 0 && cross(c, a, p) >= 0;
  let guard = 0;
  while (V.length > 3 && guard++ < 5000) {
    let cut = false;
    for (let i = 0; i < V.length; i++) {
      const ia = V[(i + V.length - 1) % V.length]; const ib = V[i]; const ic = V[(i + 1) % V.length];
      const a = pts[ia]; const b = pts[ib]; const c = pts[ic];
      if (cross(a, b, c) <= 1e-9) continue; // reflex or flat
      let ear = true;
      for (const k of V) {
        if (k === ia || k === ib || k === ic) continue;
        if (inside(pts[k], a, b, c)) { ear = false; break; }
      }
      if (!ear) continue;
      out.push([ia, ib, ic]);
      V.splice(i, 1);
      cut = true;
      break;
    }
    if (!cut) break; // degenerate ring: stop rather than loop
  }
  if (V.length === 3) out.push([V[0], V[1], V[2]]);
  return out;
}

function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/* ------------------------------------------------------------------ the plan rule */
// round_half_up(N * share / 100): the same count as design_mcp/langford_plan.py glazed_count()
export const glazedCount = (share, n) => Math.floor((n * Number(share)) / 100 + 0.5 + 1e-9);
const r4 = (v) => Math.round(Number(v) * 1e4) / 1e4;

/**
 * The plan for a parameter set: the same keys and order as design_mcp/langford_plan.py plan()
 * (se_glazed / se_solid ids by rank, lanterns_open, sky_glazed / sky_solid glazing ids, fins, finish).
 */
export function planLangford(params) {
  if (!BASE) throw new Error('loadLangford() first');
  const P = normalizeParams(params);
  const se = BASE.elements.filter((e) => e.q === 'se_glass_share').sort((a, b) => a.rank - b.rank);
  const k = glazedCount(P.se_glass_share, se.length);
  const nOpen = Math.max(0, Math.min(BASE.lanternsN, Math.round(P.skylights_open)));
  const glazing = BASE.elements.filter((e) => e.q === 'skylights_open');
  const layout = skyGlazedIds(P);
  const isGlazed = (e) => (layout ? layout.has(e.id) : e.lantern < nOpen);
  const byLantern = (open) => glazing.filter((e) => isGlazed(e) === open).sort((a, b) => a.lantern - b.lantern).map((e) => e.id);
  const depth = r4(P.fin_depth);
  const fins = depth > 1e-9 ? BASE.anchors.map((a, i) => ({
    mark: a.mark, index: Number.isFinite(a.index) ? a.index : i,
    base: [a.base[0], a.base[1], a.base[2]],
    start: [r4(a.base[0]), r4(a.base[1])],
    end: [r4(a.base[0] + a.dir[0] * depth), r4(a.base[1] + a.dir[1] * depth)],
    z: a.base[2], height: a.height_m, length: depth,
  })) : [];
  const seGlazed = se.slice(0, k).map((e) => e.id);
  const seSolid = se.slice(k).map((e) => e.id);
  const open = layout
    ? Array.from({ length: BASE.lanternsN }, (_, i) => i).filter((i) => glazing.filter((e) => e.lantern === i).every(isGlazed))
    : Array.from({ length: nOpen }, (_, i) => i);
  return {
    params: P,
    se_glazed: seGlazed, se_solid: seSolid,
    glazed_lites: seGlazed, solid_lites: seSolid, open_lanterns: open, // the same lists, short names
    se_glazed_count: k, se_n: se.length,
    lanterns_open: open,
    sky_glazed: byLantern(true), sky_solid: byLantern(false),
    fins, fin_depth: depth, finish: P.infill_finish,
  };
}

/* ------------------------------------------------------------------ materials */
function materials(json, finish) {
  const m = json.materials || {};
  const fc = (json.finish_colours || {})[finish] || {};
  const c = (k, d) => (m[k] && Array.isArray(m[k].color) ? rgb255(m[k].color) : d);
  const r = (k, d) => (m[k] && Number.isFinite(m[k].roughness) ? m[k].roughness : d);
  const infillKind = finish === 'aluminium' ? 'metal' : finish === 'fritted_glass' ? 'frit' : 'concrete';
  const infillLabel = { concrete: 'Bush-hammered concrete panel', aluminium: 'Aluminium panel', fritted_glass: 'Fritted glass panel' }[finish];
  return {
    concrete: { label: 'Bush-hammered concrete', kind: 'concrete', color: c('concrete', [184, 179, 168]), opacity: 1, roughness: r('concrete', 0.9), metalness: 0 },
    glass: { label: 'Glass', kind: 'glass', color: c('glass', [107, 140, 158]), opacity: m.glass && Number.isFinite(m.glass.opacity) ? m.glass.opacity : 0.45, roughness: 0.05, metalness: 0.1 },
    aluminium: { label: 'Aluminium mullion', kind: 'metal', color: c('aluminium', [158, 163, 168]), opacity: 1, roughness: r('aluminium', 0.4), metalness: 0.6 },
    paving: { label: 'Site paving', kind: 'stone', color: c('paving', [168, 163, 153]), opacity: 1, roughness: r('paving', 0.95), metalness: 0 },
    planting: { label: 'Planting bed', kind: 'foliage', color: c('planting', [84, 115, 64]), opacity: 1, roughness: 1, metalness: 0 },
    roof: { label: 'Roof', kind: 'concrete', color: c('roof', [77, 77, 79]), opacity: 1, roughness: r('roof', 0.9), metalness: 0 },
    ['infill_' + finish]: {
      label: infillLabel, kind: infillKind,
      color: Array.isArray(fc.color) ? rgb255(fc.color) : [184, 179, 168],
      opacity: Number.isFinite(fc.opacity) ? fc.opacity : 1,
      roughness: Number.isFinite(fc.roughness) ? fc.roughness : 0.9,
      // the phone has no sky to reflect: a strongly metallic panel renders dark gray, so aluminium is
      // shown as a light, half-metallic silver (a look, not a material property)
      metalness: Number.isFinite(fc.metalness) ? Math.min(fc.metalness, 0.45) : 0,
      ...(finish === 'aluminium' ? { color: [212, 217, 222], roughness: 0.35 } : {}),
    },
    fin: { label: 'Concrete fin', kind: 'concrete', color: c('concrete', [184, 179, 168]), opacity: 1, roughness: 0.9, metalness: 0 },
    grass: { label: 'Lawn', kind: 'ground', color: [126, 150, 98], opacity: 1, roughness: 1, metalness: 0 },
    bark: { label: 'Bark', kind: 'wood', color: [92, 72, 54], opacity: 1, roughness: 1, metalness: 0 },
    foliage: { label: 'Tree', kind: 'foliage', color: [86, 122, 70], opacity: 1, roughness: 1, metalness: 0 },
    foliage_light: { label: 'Tree', kind: 'foliage', color: [112, 146, 84], opacity: 1, roughness: 1, metalness: 0 },
    context: { label: 'Neighbouring building', kind: 'plaster', color: [168, 170, 172], opacity: 1, roughness: 0.95, metalness: 0 },
  };
}

function tagOf(e) {
  if (e.q === 'se_glass_share') return 'glazing:lite';
  if (e.q === 'skylights_open') return 'roof:lantern';
  if (e.q === 'infill_finish') return 'facade:infill';
  if (e.mat === 'paving') return 'landscape:paving';
  if (e.mat === 'planting') return 'landscape:planting';
  if (e.mat === 'roof') return 'building:roof';
  if (e.cls === 'IfcColumn') return 'structure:column';
  return 'building:' + String(e.cls || 'element').replace(/^Ifc/, '').toLowerCase();
}

/* ------------------------------------------------------------------ the model */
/** The Model (docs/MODEL.md) for a parameter set. Call loadLangford() once first. */
export function buildLangford(params) {
  if (!BASE) throw new Error('loadLangford() first');
  const t0 = nowMs();
  const P = normalizeParams(params);
  const B = BASE;
  const k = glazedCount(P.se_glass_share, B.seN);
  const infill = 'infill_' + P.infill_finish;
  const sky = skyGlazedIds(P);
  const parts = [];
  const solidLites = [];
  for (const e of B.elements) {
    let m = e.mat;
    if (e.q === 'se_glass_share') { if (e.rank >= k) { m = infill; solidLites.push(e); } else m = 'glass'; }
    else if (e.q === 'skylights_open') m = (sky ? sky.has(e.id) : e.lantern < P.skylights_open) ? 'glass' : infill;
    else if (e.q === 'infill_finish' || m === 'infill') m = infill;
    parts.push({ m, t: tagOf(e), tri: e.tri, id: e.id });
  }
  const fins = [];
  if (P.fin_depth > 0) {
    for (const a of B.anchors) {
      const box = finBox(a, P.fin_depth);
      if (box) { const part = { m: 'fin', t: 'fins:fin', p: box, mark: a.mark }; parts.push(part); fins.push(part); }
    }
  }
  for (const s of B.static.parts) parts.push(s);

  const S = B.static;
  const fp = S.footprint;
  const H = (x, y) => S.heightAt(x, y);
  const pins = buildPins(P, solidLites);
  const stops = [
    stop('quad', 'SE quad', [38, -44], 1.6, [32, 4, 9]),
    stop('entrance', 'Entrance', [25.4, -5], 1.6, [25.4, 4.1, 6], 4.5),
    stop('bridge', 'Under the bridge', [12, -1], 1.6, [30, 0, 2.6], -0.3), // on the areaway paving
    stop('street', 'NE street', [92, 30], 1.6, [66, 36, 9]), // between the street trees
    stop('corner', 'East corner', [84, -26], 1.6, [61, 2, 10]),
    // raised, from the north: the north-light lanterns show their glazing
    { id: 'roof', label: 'Roof', eye: [74, 72, 40], target: [32, 30, 19] },
  ];
  function stop(id, label, xy, eyeH, target, floor) {
    const z = floor != null ? floor : H(xy[0], xy[1]);
    return { id, label, eye: [xy[0], xy[1], z + eyeH], target };
  }

  const bounds = { min: [...B.json.quantization.min], max: [...B.json.quantization.max] };
  const byTag = {};
  for (const p of parts) byTag[p.t] = (byTag[p.t] || 0) + 1;
  return {
    version: MODEL_VERSION, units: 'm', up: 'z', params: P,
    materials: materials(B.json, P.infill_finish),
    parts,
    pins,
    tour: stops,
    tour_start: 'quad',
    // orbit home: the SE quad three-quarter view, aimed at the SE facade and the roof lanterns; on a
    // portrait phone the building's ends may run off the sides (it would be tiny otherwise)
    camera: { theta: -0.55, phi: 1.13, target: [32, 14, 11], portrait_fit_x: 1.7 },
    walkable: {
      min: [fp[0] - 40, fp[1] - 48], max: [fp[3] + 38, fp[4] + 30], floor_z: H(31, -30),
      obstacles: S.obstacles, heightAt: H,
    },
    ground: { z: S.groundZ },
    bounds,
    plan: { glazed_lites: k, open_lanterns: P.skylights_open, fins: fins.length, finish: P.infill_finish },
    stats: { parts: parts.length, byTag, buildMs: nowMs() - t0, decodeMs: B.decodeMs },
  };
}

function finBox(a, depth) {
  const [bx, by, bz] = a.base;
  let [dx, dy] = a.dir;
  const dl = Math.hypot(dx, dy) || 1;
  dx /= dl; dy /= dl;
  const t = (Number(a.thickness_m) || 0.2) / 2;
  const h = Number(a.height_m) || 3.4;
  const nx = -dy * t; const ny = dx * t; // half thickness, perpendicular
  const ex = bx + dx * depth; const ey = by + dy * depth;
  const q = [[bx + nx, by + ny], [ex + nx, ey + ny], [ex - nx, ey - ny], [bx - nx, by - ny]];
  const p = [];
  for (const [x, y] of q) p.push(x, y, bz);
  for (const [x, y] of q) p.push(x, y, bz + h);
  return p;
}

// Exterior pins on the real elements (two candidates per question; the viewer shows the best one).
function buildPins(P, solidLites) {
  const B = BASE;
  const pins = [];
  const centre = (b) => [(b[0] + b[3]) / 2, (b[1] + b[4]) / 2, (b[2] + b[5]) / 2];
  const lites = B.elements.filter((e) => e.q === 'se_glass_share');
  const k = glazedCount(P.se_glass_share, B.seN);
  // glass share: a central lite on L2 and one on L3 (glazed ones when possible)
  const pickLite = (lvl, x) => {
    const pool = lites.filter((e) => e.lvl === lvl);
    const pref = pool.filter((e) => e.rank < k);
    const list = pref.length ? pref : pool;
    return list.reduce((a, e) => (Math.abs(centre(e.box)[0] - x) < Math.abs(centre(a.box)[0] - x) ? e : a), list[0]);
  };
  for (const [lvl, x] of [['L3', 52], ['L2', 10]]) {
    const e = pickLite(lvl, x);
    if (!e) continue;
    const c = centre(e.box);
    pins.push({ question: 'se_glass_share', mode: 'exterior', pos: [c[0], e.box[1] - 0.25, c[2]], normal: [0, -1, 0], label: 'Glass' });
  }
  // skylights: on top of two lanterns
  const lant = {};
  for (const e of B.elements) {
    if (e.q !== 'skylights_open') continue;
    const b = lant[e.lantern];
    lant[e.lantern] = b ? [Math.min(b[0], e.box[0]), Math.min(b[1], e.box[1]), Math.min(b[2], e.box[2]), Math.max(b[3], e.box[3]), Math.max(b[4], e.box[4]), Math.max(b[5], e.box[5])] : e.box.slice();
  }
  for (const i of [3, 8]) {
    const b = lant[i];
    if (!b) continue;
    // the glazing faces true north (model +x +y): look from there when the question is in focus
    pins.push({ question: 'skylights_open', mode: 'exterior', pos: [(b[0] + b[3]) / 2, (b[1] + b[4]) / 2, b[5] + 0.3], normal: [0, 0, 1], look: [0.7071, 0.7071, 0], label: 'Skylights' });
  }
  // fins: on the fin (or where it would go) at two anchors
  for (const idx of [16, 6]) {
    const a = B.anchors[idx];
    if (!a) continue;
    const d = P.fin_depth;
    pins.push({
      question: 'fin_depth', mode: 'exterior',
      pos: [a.base[0] + a.dir[0] * (d + 0.15), a.base[1] + a.dir[1] * (d + 0.15), a.base[2] + a.height_m * 0.62],
      normal: [a.dir[0], a.dir[1], 0], label: 'Fins',
    });
  }
  // finish: a solid SE lite when there is one (the most central), and the penthouse louvre panels
  if (solidLites.length) {
    const e = solidLites.reduce((a, s) => (Math.abs(centre(s.box)[0] - 32) + Math.abs(centre(s.box)[2] - 9) < Math.abs(centre(a.box)[0] - 32) + Math.abs(centre(a.box)[2] - 9) ? s : a), solidLites[0]);
    const c = centre(e.box);
    pins.push({ question: 'infill_finish', mode: 'exterior', pos: [c[0], e.box[1] - 0.25, c[2]], normal: [0, -1, 0], label: 'Finish' });
  }
  const louvres = B.elements.filter((e) => e.q === 'infill_finish');
  if (louvres.length) {
    let b = louvres[0].box.slice();
    for (const e of louvres) b = [Math.min(b[0], e.box[0]), Math.min(b[1], e.box[1]), Math.min(b[2], e.box[2]), Math.max(b[3], e.box[3]), Math.max(b[4], e.box[4]), Math.max(b[5], e.box[5])];
    pins.push({ question: 'infill_finish', mode: 'exterior', pos: [(b[0] + b[3]) / 2, b[1] - 0.25, (b[2] + b[5]) / 2], normal: [0, -1, 0], label: 'Finish' });
  }
  return pins;
}

function nowMs() {
  return typeof performance !== 'undefined' && performance.now ? performance.now() : Date.now();
}
