// Plurarch pavilion generator (docs/MODEL.md contract, v2): buildPavilion(params) -> Model.
// Pure ES module: no DOM, no three.js, no imports. Runs in browsers and in Node.
// Z-up, metres. Footprint 18 m (x -9..9) by 10 m (y -5..5); the entrance facade faces -y.
// The same code drives the phone viewer and Rhino (via tools/pavilion_cli.mjs), so keep it
// deterministic (no randomness except a fixed-seed generator) and never throwing.

export const MODEL_VERSION = '2.0.0';

export const QUESTION_TAGS = {
  facade_material: ['facade'],
  window_ratio: ['glazing'],
  roof_angle: ['roof'],
  canopy_depth: ['canopy'],
};

// ---------------------------------------------------------------------------
// 1. Parameters (mirror of config/parameters.json)
// ---------------------------------------------------------------------------
const DEFAULTS = { facade_material: 'timber', window_ratio: 40, roof_angle: 15, canopy_depth: 1.5 };
const MATERIAL_OPTIONS = ['timber', 'concrete', 'glass'];
const SPEC = { window_ratio: [20, 60, 5], roof_angle: [0, 35, 5], canopy_depth: [0, 3, 0.5] };

function toNumber(v) {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  if (typeof v === 'string' && v.trim() !== '') {
    const x = Number(v.trim());
    return Number.isFinite(x) ? x : null;
  }
  return null; // booleans, null, objects, arrays
}

function snap(x, lo, hi, st) {
  const c = Math.min(Math.max(x, lo), hi);
  const s = lo + Math.floor((c - lo) / st + 0.5) * st;
  return Math.round(Math.min(Math.max(s, lo), hi) * 1e6) / 1e6;
}

/** Complete, clamped, snapped parameter set. Never throws; defaults fill any gap. */
export function normalizeParams(params) {
  const src = params && typeof params === 'object' && !Array.isArray(params) ? params : {};
  const out = { ...DEFAULTS };
  let mat;
  try { mat = src.facade_material; } catch (e) { mat = null; }
  if (typeof mat === 'string' && MATERIAL_OPTIONS.includes(mat.trim().toLowerCase())) {
    out.facade_material = mat.trim().toLowerCase();
  }
  for (const key of Object.keys(SPEC)) {
    const [lo, hi, st] = SPEC[key];
    let v;
    try { v = src[key]; } catch (e) { v = null; }
    const x = toNumber(v);
    out[key] = snap(x === null ? DEFAULTS[key] : x, lo, hi, st);
  }
  return out;
}

// ---------------------------------------------------------------------------
// 2. Dimensions (metres)
// ---------------------------------------------------------------------------
const X0 = -9, X1 = 9, Y0 = -5, Y1 = 5;
const FLOOR = 0.30;            // finished floor level (top of the floor finish / slab)
const SLAB_TOP = 0.28;         // structural slab; a 2 cm finish / perimeter band on top
const SLAB_M = 0.6;            // slab edge beyond the building line
const GROUND = [-14, 14, -12.5, 11, -0.2, 0];
const PAVE_Z = 0.02;           // paving sits 2 cm proud of the lawn

const EAVE_LOW = 4.5;          // roof underside at the back wall line (+y)
const ROOF_T = 0.35;           // slab thickness, perpendicular to the slope
const OH_SIDE = 0.8, OH_HIGH = 0.6, OH_LOW_MIN = 0.8, OH_LOW_MAX = 3.0;
const EMBED = 0.05;            // walls / fins / posts run this far into the roof slab
const CEIL_T = 0.03;           // ceiling / soffit lining under the slab
const BEAM_W = 0.14, BEAM_D = 0.36;  // glulam roof beams (depth perpendicular to the slope)
const FASCIA_T = 0.04;

const WALL_IN = -0.25;         // inner face of every wall (d, distance from the building line)
const SILL_SHARE = 0.35;       // share of the solid wall height that sits below the glazing
const MIN_TOP_SOLID = 0.2;     // solid wall kept above any window
const PIER_MIN = 0.15;         // concrete: minimum pier beside a punched window
const CORNER_IN = 0.06;        // corner post reaches this far along each facade
const CORNER_CLEAR = 0.35;     // no opening closer than this to a building corner
const SILL_MIN = FLOOR + 0.05;
const UPPER_MIN = 0.3;         // smallest clerestory pane above the canopy band
const MIN_DIM = 1e-3;

const DOOR_BAY = 2.4;          // central bay of the long facades (entrance on the front)
const DOOR_HALF = 1.0;         // door frame outer half width
const DOOR_JAMB = 0.08;
const DOOR_LEAF_TOP = FLOOR + 2.3;
const DOOR_TRANSOM_TOP = DOOR_LEAF_TOP + 0.08;

const CANOPY_Z = 3.2, CANOPY_T = 0.2, CANOPY_W = 8.0;
const POST_R = 0.07, POST_INSET = 0.3, POST_MIN_DEPTH = 1.5;
const BAND_LO = CANOPY_Z - 0.1, BAND_HI = CANOPY_Z + CANOPY_T + 0.1;  // solid band where the canopy meets the wall
const CANOPY_ZONE = CANOPY_W / 2 + 0.1;
const FAN_TOP = BAND_LO;       // fanlight over the entrance door

// Facade systems. j: fin / joint / mullion width; out: outer face of the skin (d);
// wallOut: outer face of the wall body; pane / frame: depth ranges (d) of glass and frames.
const STYLE = {
  timber: { pitch: 1.2, j: 0.12, out: 0.40, wallOut: -0.02, wall: 'timber_board', canopyFace: -0.02,
    pane: [-0.15, -0.13], frame: [-0.19, -0.09], wfNom: () => 1.08 / 1.2 },
  concrete: { pitch: 2.25, j: 0.12, out: 0.25, wallOut: -0.05, wall: 'concrete_fair', canopyFace: 0.25,
    pane: [-0.15, -0.13], frame: [-0.19, -0.09], wfNom: (r) => Math.sqrt(r) },
  glass: { pitch: 1.8, j: 0.06, out: 0.25, wallOut: -0.05, wall: 'plaster', canopyFace: -0.02,
    pane: [-0.035, -0.015], frame: null, wfNom: () => 1.74 / 1.8 },
};

// The four facades: origin, tangent (along), outward normal, length.
// A facade point is origin + s * tangent + d * normal.
const FACADES = [
  { name: 'front', ox: X0, oy: Y0, tx: 1, ty: 0, nx: 0, ny: -1, L: X1 - X0, long: true },
  { name: 'right', ox: X1, oy: Y0, tx: 0, ty: 1, nx: 1, ny: 0, L: Y1 - Y0, long: false },
  { name: 'back', ox: X1, oy: Y1, tx: -1, ty: 0, nx: 0, ny: 1, L: X1 - X0, long: true },
  { name: 'left', ox: X0, oy: Y1, tx: 0, ty: -1, nx: -1, ny: 0, L: Y1 - Y0, long: false },
];

// ---------------------------------------------------------------------------
// 3. Materials (colour 0-255). Keys are stable; kinds pick the viewer's texture.
// ---------------------------------------------------------------------------
function mat(label, kind, color, roughness, metalness, extra) {
  return Object.assign({ label, kind, color, opacity: 1, roughness, metalness }, extra || {});
}
const MATERIALS = {
  grass: mat('Lawn', 'ground', [112, 142, 84], 1, 0),
  paving: mat('Paving stone', 'stone', [186, 181, 172], 0.9, 0),
  slab: mat('Concrete plinth', 'concrete', [150, 148, 143], 0.85, 0),
  stone: mat('Limestone', 'stone', [214, 208, 196], 0.6, 0),
  floor: mat('Light oak floor', 'wood', [203, 172, 132], 0.35, 0),
  timber: mat('Cedar fins', 'wood', [150, 100, 60], 0.75, 0),
  timber_board: mat('Larch boarding', 'wood', [196, 158, 114], 0.7, 0),
  concrete: mat('Precast concrete', 'concrete', [170, 168, 162], 0.9, 0),
  concrete_fair: mat('Fair-faced concrete', 'concrete', [190, 188, 182], 0.85, 0),
  plaster: mat('White plaster', 'plaster', [240, 238, 232], 0.9, 0),
  spandrel: mat('Spandrel glass', 'paint', [58, 70, 80], 0.2, 0.3),
  aluminium: mat('Aluminium', 'metal', [196, 200, 204], 0.35, 0.8),
  glazing: mat('Glazing', 'glass', [150, 188, 204], 0.05, 0.1, { opacity: 0.35 }),
  frame: mat('Window frame', 'metal', [58, 60, 64], 0.45, 0.6),
  roof: mat('Zinc roof', 'metal', [104, 108, 112], 0.6, 0.5),
  fascia: mat('Fascia', 'metal', [54, 56, 60], 0.5, 0.6),
  ceiling: mat('Ceiling', 'plaster', [236, 234, 228], 0.95, 0),
  glulam: mat('Glulam', 'wood', [206, 162, 110], 0.6, 0),
  canopy: mat('Canopy', 'metal', [66, 68, 74], 0.5, 0.5),
  steel_black: mat('Black steel', 'metal', [34, 34, 36], 0.5, 0.7),
  steel: mat('Stainless steel', 'metal', [182, 184, 188], 0.3, 0.9),
  fabric: mat('Seat fabric', 'fabric', [52, 56, 68], 1, 0),
  oak: mat('Oak', 'wood', [168, 124, 82], 0.55, 0),
  stage: mat('Stained oak', 'wood', [104, 74, 50], 0.5, 0),
  screen: mat('Projection screen', 'paint', [246, 246, 244], 0.95, 0, { emissive: [70, 72, 78] }),
  core: mat('Core walls', 'paint', [104, 116, 106], 0.8, 0),
  display: mat('Display board', 'paint', [242, 241, 236], 0.85, 0),
  accent: mat('Exhibit', 'paint', [196, 96, 62], 0.6, 0),
  light: mat('Lamp', 'light', [255, 238, 205], 0.4, 0, { emissive: [255, 222, 170] }),
  bark: mat('Bark', 'wood', [92, 70, 52], 1, 0),
  foliage: mat('Foliage', 'foliage', [82, 124, 66], 1, 0),
  foliage_light: mat('Foliage (light)', 'foliage', [118, 152, 78], 1, 0),
};

// ---------------------------------------------------------------------------
// 4. Geometry helpers. A hexahedron is 8 corners: the bottom quad counter-clockwise
//    seen from above, then the top quad in the same order (4 above 0).
// ---------------------------------------------------------------------------
function makeBuilder() {
  const parts = [];

  function prism(m, t, quad, zb, zt) {
    let a2 = 0;
    for (let i = 0; i < 4; i++) {
      const [xa, ya] = quad[i], [xb, yb] = quad[(i + 1) & 3];
      a2 += xa * yb - xb * ya;
      if (Math.hypot(xb - xa, yb - ya) < MIN_DIM) return null;
    }
    if (!(Math.abs(a2) > 2e-6)) return null;
    const q = a2 > 0 ? quad : [quad[0], quad[3], quad[2], quad[1]];
    const p = new Array(24);
    for (let i = 0; i < 4; i++) {
      const x = q[i][0], y = q[i][1];
      const b = typeof zb === 'function' ? zb(x, y) : zb;
      const u = typeof zt === 'function' ? zt(x, y) : zt;
      if (!(u - b >= MIN_DIM)) return null;
      p[i * 3] = x; p[i * 3 + 1] = y; p[i * 3 + 2] = b;
      p[12 + i * 3] = x; p[12 + i * 3 + 1] = y; p[12 + i * 3 + 2] = u;
    }
    const part = { m, t, p };
    parts.push(part);
    return part;
  }

  function box(m, t, x0, x1, y0, y1, z0, z1) {
    return prism(m, t, [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], z0, z1);
  }

  function cyl(m, t, cx, cy, z0, z1, r, seg) {
    if (!(z1 - z0 >= MIN_DIM) || !(r >= 5e-4)) return null;
    const part = { m, t, cyl: [cx, cy, z0, z1, r], seg: seg || 12 };
    parts.push(part);
    return part;
  }

  // square frustum (4 corners at rot + k * 90 deg): radius r0 at z0, r1 at z1
  function frustum(m, t, cx, cy, z0, r0, z1, r1, rot) {
    if (!(z1 - z0 >= MIN_DIM) || !(r0 >= MIN_DIM) || !(r1 >= MIN_DIM)) return null;
    const p = new Array(24);
    for (let i = 0; i < 4; i++) {
      const a = rot + (i * Math.PI) / 2, c = Math.cos(a), s = Math.sin(a);
      p[i * 3] = cx + r0 * c; p[i * 3 + 1] = cy + r0 * s; p[i * 3 + 2] = z0;
      p[12 + i * 3] = cx + r1 * c; p[12 + i * 3 + 1] = cy + r1 * s; p[12 + i * 3 + 2] = z1;
    }
    const part = { m, t, p };
    parts.push(part);
    return part;
  }

  return { parts, prism, box, cyl, frustum };
}

function fpt(F, s, d) {
  return [F.ox + s * F.tx + d * F.nx, F.oy + s * F.ty + d * F.ny];
}

// a box in facade coordinates (s along, d outward, z up)
function fbox(B, F, m, t, s0, s1, d0, d1, zb, zt) {
  if (!(s1 - s0 >= MIN_DIM) || !(d1 - d0 >= MIN_DIM)) return null;
  return B.prism(m, t, [fpt(F, s0, d0), fpt(F, s1, d0), fpt(F, s1, d1), fpt(F, s0, d1)], zb, zt);
}

// fixed-seed pseudo random numbers (mulberry32)
function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// ---------------------------------------------------------------------------
// 5. Roof: mono-pitch, low eave 4.5 m at the back (+y), rising toward the entrance (-y)
// ---------------------------------------------------------------------------
export function roofGeometry(angleDeg) {
  const angle = Math.min(Math.max(Number(angleDeg) || 0, 0), 35);
  const a = (angle * Math.PI) / 180;
  const slope = Math.tan(a), cos = Math.cos(a);
  const under = (y) => EAVE_LOW + (Y1 - y) * slope;
  const tv = ROOF_T / cos;
  const ohLow = OH_LOW_MIN + ((OH_LOW_MAX - OH_LOW_MIN) * angle) / 35;
  return {
    angle, slope, cos, tv, ohLow,
    x0: X0 - OH_SIDE, x1: X1 + OH_SIDE, y0: Y0 - OH_HIGH, y1: Y1 + ohLow,
    under, top: (y) => under(y) + tv,
    ceil: (y) => under(y) - CEIL_T,                       // underside of the ceiling / soffit
    beamBottom: (y) => under(y) - CEIL_T - BEAM_D / cos,
  };
}

// ---------------------------------------------------------------------------
// 6. Facade layout: bays and windows with the EXACT glazed share per facade.
//    Glazed area = total area of the glass panes (tag glazing:pane) on a facade;
//    wall area = length x wall height at the facade middle (exact for the sloped
//    side walls, whose height is linear along the facade).
// ---------------------------------------------------------------------------
function facadeBays(F, st) {
  const L = F.L, bays = [];
  if (F.long) {                                   // central 2.4 m bay (entrance on the front)
    const half = (L - DOOR_BAY) / 2;
    const n = Math.max(1, Math.round(half / st.pitch)), w = half / n;
    for (let i = 0; i < n; i++) bays.push({ s0: i * w, s1: (i + 1) * w });
    bays.push({ s0: half, s1: half + DOOR_BAY, central: true });
    for (let i = 0; i < n; i++) {
      bays.push({ s0: half + DOOR_BAY + i * w, s1: i === n - 1 ? L : half + DOOR_BAY + (i + 1) * w });
    }
  } else {
    const n = Math.max(1, Math.round(L / st.pitch)), w = L / n;
    for (let i = 0; i < n; i++) bays.push({ s0: i * w, s1: i === n - 1 ? L : (i + 1) * w });
  }
  return bays;
}

// horizontal extent of the window in a bay (timber / glass: the whole clear width;
// concrete: a punched window of about sqrt(ratio) of the bay, with piers)
function windowSpan(F, st, style, bay, ratio) {
  const L = F.L, j = st.j;
  if (style === 'concrete') {
    const a = Math.max(bay.s0 + j / 2 + PIER_MIN, CORNER_CLEAR);
    const b = Math.min(bay.s1 - j / 2 - PIER_MIN, L - CORNER_CLEAR);
    const w = Math.min(Math.sqrt(ratio) * (bay.s1 - bay.s0), b - a);
    const c = Math.min(Math.max((bay.s0 + bay.s1) / 2, a + w / 2), b - w / 2);
    return [c - w / 2, c + w / 2];
  }
  return [Math.max(bay.s0 + j / 2, CORNER_CLEAR), Math.min(bay.s1 - j / 2, L - CORNER_CLEAR)];
}

// clear region of a bay for panels / spandrels (between joints, or from the corner post)
function bayClear(F, st, bay) {
  return [bay.s0 < 1e-9 ? CORNER_IN : bay.s0 + st.j / 2, bay.s1 > F.L - 1e-9 ? F.L - CORNER_IN : bay.s1 - st.j / 2];
}

// window rectangles (z) of one slot for height factor k and sill drop dl
function slotRects(sl, sill, k, dl, doorUpper) {
  const h = k * sl.hm;
  if (sl.door) {                                  // entrance bay: clerestory above the canopy band only
    const u = sill + h - BAND_LO;
    if (!doorUpper || !(u > 0)) return [];
    const z1 = Math.min(BAND_HI + u, sl.cap);
    return z1 - BAND_HI > 0.01 ? [{ z0: BAND_HI, z1 }] : [];
  }
  if (!sl.zone) {
    const z0 = sill - dl, z1 = Math.min(sill + h, sl.cap);
    return z1 - z0 > 0.01 ? [{ z0, z1 }] : [];
  }
  // front bay behind the canopy: keep the band [BAND_LO, BAND_HI] solid
  const low = (z) => Math.max(SILL_MIN, Math.min(z, z - dl));
  if (sill + h <= BAND_LO) return h > 0.01 ? [{ z0: low(sill), z1: sill + h }] : [];
  const u = sill + h - BAND_LO;
  if (u < UPPER_MIN && sill - u >= SILL_MIN) return [{ z0: low(sill - u), z1: BAND_LO }];  // shift down
  const out = [{ z0: low(sill), z1: BAND_LO }];                                            // split
  const z1 = Math.min(BAND_HI + u, sl.cap);
  if (z1 - BAND_HI > 0.01) out.push({ z0: BAND_HI, z1 });
  return out;
}

function layoutFacade(F, style, ratio, roof, canopyOn) {
  const st = STYLE[style];
  const bays = facadeBays(F, st);
  const H = (s) => roof.under(fpt(F, s, 0)[1]) - FLOOR;
  const target = ratio * F.L * H(F.L / 2);
  const hfNom = Math.min(ratio / st.wfNom(ratio), 0.95);
  const sill = FLOOR + SILL_SHARE * (1 - hfNom) * (EAVE_LOW - FLOOR);
  const isFront = F.name === 'front';
  const slots = [];
  const fixedOpenings = [];
  let fixed = 0;
  if (isFront) {                                  // entrance door + fanlight
    const c = F.L / 2;
    fixedOpenings.push({ s0: c - DOOR_HALF, s1: c + DOOR_HALF, z0: FLOOR, z1: FAN_TOP, kind: 'door' });
    fixed = 2 * DOOR_HALF * (FAN_TOP - DOOR_TRANSOM_TOP);
  }
  for (const bay of bays) {
    const [wa, wb] = windowSpan(F, st, style, bay, ratio);
    if (!(wb - wa > 0.05)) continue;
    const pa = fpt(F, wa, 0), pb = fpt(F, wb, 0);
    const cap = Math.min(roof.under(pa[1]), roof.under(pb[1])) - MIN_TOP_SOLID;
    const door = isFront && !!bay.central;
    const zone = isFront && canopyOn && !door
      && Math.min(pa[0], pb[0]) < CANOPY_ZONE && Math.max(pa[0], pb[0]) > -CANOPY_ZONE;
    slots.push({ bay, wa, wb, w: wb - wa, cap, hm: H((wa + wb) / 2), door, zone });
  }
  const areaOf = (k, dl, du) => {
    let a = fixed;
    for (const sl of slots) for (const r of slotRects(sl, sill, k, dl, du)) a += sl.w * (r.z1 - r.z0);
    return a;
  };
  const solveK = (du) => {
    let lo = 0, hi = 6;
    if (areaOf(hi, 0, du) <= target) return hi;
    for (let i = 0; i < 60; i++) {
      const mid = 0.5 * (lo + hi);
      if (areaOf(mid, 0, du) < target) lo = mid; else hi = mid;
    }
    return 0.5 * (lo + hi);
  };
  let doorUpper = true;
  let k = solveK(true);
  const ds = slots.find((s) => s.door);
  if (ds) {
    const u = sill + k * ds.hm - BAND_LO;
    if (u > 0 && u < UPPER_MIN) { doorUpper = false; k = solveK(false); }  // no sliver clerestory
  }
  let dl = 0;
  if (areaOf(k, 0, doorUpper) < target - 1e-9) {  // every window at its cap: lower the sills
    let lo = 0, hi = Math.max(0, sill - SILL_MIN);
    if (areaOf(k, hi, doorUpper) <= target) dl = hi;
    else {
      for (let i = 0; i < 60; i++) {
        const mid = 0.5 * (lo + hi);
        if (areaOf(k, mid, doorUpper) < target) lo = mid; else hi = mid;
      }
      dl = 0.5 * (lo + hi);
    }
  }
  const openings = fixedOpenings.slice();
  for (const sl of slots) {
    sl.rects = slotRects(sl, sill, k, dl, doorUpper);
    for (const r of sl.rects) openings.push({ s0: sl.wa, s1: sl.wb, z0: r.z0, z1: r.z1, kind: 'window', slot: sl });
  }
  return { F, style, st, bays, slots, openings, sill, target, area: areaOf(k, dl, doorUpper), k, H };
}

// solid rectangles of [e0, e1] x [FLOOR, roof] minus the openings (column decomposition,
// neighbouring columns with the same intervals merged). z1 === null: up to the roof.
function solidRegions(e0, e1, openings) {
  const cuts = [e0, e1];
  for (const o of openings) {
    if (o.s0 > e0 && o.s0 < e1) cuts.push(o.s0);
    if (o.s1 > e0 && o.s1 < e1) cuts.push(o.s1);
  }
  cuts.sort((a, b) => a - b);
  const cols = [];
  for (let i = 0; i + 1 < cuts.length; i++) {
    const c0 = cuts[i], c1 = cuts[i + 1];
    if (c1 - c0 < MIN_DIM) continue;
    const mid = 0.5 * (c0 + c1);
    const holes = openings.filter((o) => o.s0 < mid && o.s1 > mid).map((o) => [o.z0, o.z1]);
    holes.sort((a, b) => a[0] - b[0]);
    const iv = [];
    let z = FLOOR;
    for (const [h0, h1] of holes) {
      if (h0 - z >= MIN_DIM) iv.push([z, h0]);
      z = Math.max(z, h1);
    }
    iv.push([z, null]);
    const last = cols[cols.length - 1];
    const same = last && Math.abs(last.c1 - c0) < 1e-9 && last.iv.length === iv.length
      && last.iv.every((a, n) => Math.abs(a[0] - iv[n][0]) < 1e-9
        && (a[1] === null ? iv[n][1] === null : iv[n][1] !== null && Math.abs(a[1] - iv[n][1]) < 1e-9));
    if (same) last.c1 = c1; else cols.push({ c0, c1, iv });
  }
  const out = [];
  for (const c of cols) for (const [z0, z1] of c.iv) out.push({ s0: c.c0, s1: c.c1, z0, z1 });
  return out;
}

// frame members around one pane (timber / concrete windows), split so no two members overlap
function windowFrame(B, F, st, s0, s1, z0, z1) {
  const fw = 0.06, [d0, d1] = st.frame, T = 'glazing:frame', M = 'frame';
  if (s1 - s0 < 3 * fw || z1 - z0 < 3 * fw) return;
  fbox(B, F, M, T, s0, s0 + fw, d0, d1, z0, z1);
  fbox(B, F, M, T, s1 - fw, s1, d0, d1, z0, z1);
  const levels = [z0 + fw / 2];                   // centre lines of the horizontal members
  const h = z1 - z0;
  const n = h > 2.8 ? Math.ceil(h / 2.4) : 1;
  for (let i = 1; i < n; i++) levels.push(z0 + (h * i) / n);
  levels.push(z1 - fw / 2);
  for (const zc of levels) {
    fbox(B, F, M, T, s0 + fw, s1 - fw, d0, d1, Math.max(zc - fw / 2, z0), Math.min(zc + fw / 2, z1));
  }
  if (s1 - s0 > 1.6) {                            // central mullion between the horizontals
    const sm = 0.5 * (s0 + s1);
    for (let i = 0; i + 1 < levels.length; i++) {
      fbox(B, F, M, T, sm - fw / 2, sm + fw / 2, d0, d1, levels[i] + fw / 2, levels[i + 1] - fw / 2);
    }
  }
}

function buildFacade(B, lay, roof) {
  const { F, st, style, bays, openings } = lay;
  const L = F.L, j = st.j;
  const top = (x, y) => roof.under(y) + EMBED;
  const zTop = (z) => (z === null ? top : z);
  // wall body (its inner face is the interior lining)
  const e0 = F.long ? -st.wallOut : -WALL_IN;
  for (const r of solidRegions(e0, L - e0, openings)) {
    fbox(B, F, st.wall, 'facade:wall', r.s0, r.s1, WALL_IN, st.wallOut, r.z0, zTop(r.z1));
  }
  const lines = bays.slice(0, -1).map((b) => b.s1);   // interior bay lines
  if (style === 'timber') {
    for (const s of lines) fbox(B, F, 'timber', 'facade:fin', s - j / 2, s + j / 2, st.wallOut, st.out, FLOOR, top);
  } else {
    const skinM = style === 'concrete' ? 'concrete' : 'spandrel';
    const skinT = style === 'concrete' ? 'facade:panel' : 'facade:spandrel';
    const skinD1 = style === 'concrete' ? st.out : -0.02;
    for (const bay of bays) {
      const [a, b] = bayClear(F, st, bay);
      const own = openings.filter((o) => o.s1 > a && o.s0 < b);
      for (const r of solidRegions(a, b, own)) fbox(B, F, skinM, skinT, r.s0, r.s1, st.wallOut, skinD1, r.z0, zTop(r.z1));
      if (style === 'glass') {                    // transoms at every pane edge (and inside tall panes)
        for (const o of own) {
          if (o.kind !== 'window') continue;
          const zs = [o.z0, o.z1], h = o.z1 - o.z0;
          const n = h > 3.2 ? Math.ceil(h / 2.8) : 1;
          for (let i = 1; i < n; i++) zs.push(o.z0 + (h * i) / n);
          for (const z of zs) fbox(B, F, 'aluminium', 'glazing:frame', a, b, -0.015, 0.12, z - 0.03, z + 0.03);
        }
      }
    }
    if (style === 'glass') {
      for (const s of lines) fbox(B, F, 'aluminium', 'facade:mullion', s - j / 2, s + j / 2, st.wallOut, st.out, FLOOR, top);
    }
  }
  // glass panes and frames
  for (const o of openings) {
    if (o.kind !== 'window') continue;
    o.pane = fbox(B, F, 'glazing', 'glazing:pane', o.s0, o.s1, st.pane[0], st.pane[1], o.z0, o.z1);
    if (st.frame) windowFrame(B, F, st, o.s0, o.s1, o.z0, o.z1);
  }
}

function buildCorners(B, style, roof) {
  const st = STYLE[style];
  const m = style === 'glass' ? 'aluminium' : style;
  const top = (x, y) => roof.under(y) + EMBED;
  for (const [cx, sx] of [[X0, -1], [X1, 1]]) {
    for (const [cy, sy] of [[Y0, -1], [Y1, 1]]) {
      const xs = [cx - sx * CORNER_IN, cx + sx * st.out].sort((a, b) => a - b);
      const ys = [cy - sy * CORNER_IN, cy + sy * st.out].sort((a, b) => a - b);
      B.box(m, 'facade:corner', xs[0], xs[1], ys[0], ys[1], FLOOR, top);
    }
  }
}

// glazed double entrance door with frame, fanlight and pull handles (front facade)
function buildDoor(B, F) {
  const c = F.L / 2, s0 = c - DOOR_HALF, s1 = c + DOOR_HALF, jw = DOOR_JAMB;
  const fd0 = -0.19, fd1 = -0.09, M = 'frame';
  fbox(B, F, M, 'door:frame', s0, s0 + jw, fd0, fd1, FLOOR, FAN_TOP);
  fbox(B, F, M, 'door:frame', s1 - jw, s1, fd0, fd1, FLOOR, FAN_TOP);
  fbox(B, F, M, 'door:frame', s0 + jw, s1 - jw, fd0, fd1, DOOR_LEAF_TOP, DOOR_TRANSOM_TOP);
  fbox(B, F, M, 'door:frame', s0 + jw, s1 - jw, fd0, fd1, FAN_TOP - 0.06, FAN_TOP);
  const fan = fbox(B, F, 'glazing', 'glazing:pane', s0, s1, -0.15, -0.13, DOOR_TRANSOM_TOP, FAN_TOP);
  const sw = 0.08;
  for (const [a, b] of [[s0 + jw, c], [c, s1 - jw]]) {
    fbox(B, F, M, 'door:leaf', a, a + sw, -0.16, -0.12, FLOOR, DOOR_LEAF_TOP);
    fbox(B, F, M, 'door:leaf', b - sw, b, -0.16, -0.12, FLOOR, DOOR_LEAF_TOP);
    fbox(B, F, M, 'door:leaf', a + sw, b - sw, -0.16, -0.12, DOOR_LEAF_TOP - sw, DOOR_LEAF_TOP);
    fbox(B, F, M, 'door:leaf', a + sw, b - sw, -0.16, -0.12, FLOOR, FLOOR + 0.2);
    fbox(B, F, 'glazing', 'door:glass', a + sw, b - sw, -0.145, -0.135, FLOOR + 0.2, DOOR_LEAF_TOP - sw);
  }
  for (const s of [c - 0.14, c + 0.14]) {
    for (const d of [-0.08, -0.2]) {
      const [x, y] = fpt(F, s, d);
      B.cyl('steel', 'door:handle', x, y, FLOOR + 0.8, FLOOR + 1.8, 0.016, 8);
    }
  }
  return fan;
}

// ---------------------------------------------------------------------------
// 7. Plinth, floor and entrance steps
// ---------------------------------------------------------------------------
const IX0 = X0 - WALL_IN, IX1 = X1 + WALL_IN, IY0 = Y0 - WALL_IN, IY1 = Y1 + WALL_IN;  // interior faces

function buildPlinth(B) {
  const m = SLAB_M;
  B.box('slab', 'slab', X0 - m, X1 + m, Y0 - m, Y1 + m, 0, SLAB_TOP);
  B.box('floor', 'slab:floor', IX0, IX1, IY0, IY1, SLAB_TOP, FLOOR);
  B.box('slab', 'slab:edge', X0 - m, X1 + m, Y0 - m, IY0, SLAB_TOP, FLOOR);
  B.box('slab', 'slab:edge', X0 - m, X1 + m, IY1, Y1 + m, SLAB_TOP, FLOOR);
  B.box('slab', 'slab:edge', X0 - m, IX0, IY0, IY1, SLAB_TOP, FLOOR);
  B.box('slab', 'slab:edge', IX1, X1 + m, IY0, IY1, SLAB_TOP, FLOOR);
  const sy = Y0 - m;                              // two steps: risers 0.09 / 0.09 / 0.10 m
  B.box('stone', 'slab:step', -2.2, 2.2, sy - 0.35, sy, PAVE_Z, FLOOR - 0.09);
  B.box('stone', 'slab:step', -2.2, 2.2, sy - 0.70, sy - 0.35, PAVE_Z, FLOOR - 0.18);
}

// ---------------------------------------------------------------------------
// 8. Roof: slab, soffit / ceiling, fascia, gutter, glulam beams with exposed ends,
//    columns under the main beams along the long walls
// ---------------------------------------------------------------------------
function beamLines(frontLay, backLay, roof) {
  const out = [];
  const checks = [
    [frontLay, roof.beamBottom(IY0) - 0.03, (o) => [X0 + o.s0, X0 + o.s1]],
    [backLay, roof.beamBottom(IY1) - 0.03, (o) => [X1 - o.s1, X1 - o.s0]],
  ];
  const clear = (x) => {
    for (const [lay, zb, span] of checks) {
      for (const o of lay.openings) {
        const [a, b] = span(o);
        if (b > x - 0.12 && a < x + 0.12 && o.z1 > zb) return false;
      }
    }
    return true;
  };
  frontLay.bays.forEach((bay, i) => {
    if (i > 0) out.push({ x: X0 + bay.s0, primary: true });   // on the piers / fins / mullions
    const bw = bay.s1 - bay.s0, n = Math.round(bw / 1.2);
    for (let q = 1; q < n; q++) {                              // secondary beams where the windows allow
      const x = X0 + bay.s0 + (bw * q) / n;
      if (clear(x)) out.push({ x, primary: false });
    }
  });
  return out;
}

function buildRoof(B, roof, lines, style) {
  const st = STYLE[style];
  const { x0, x1, y0, y1 } = roof;
  const under = (x, y) => roof.under(y), topF = (x, y) => roof.top(y);
  const slab = B.box('roof', 'roof:slab', x0, x1, y0, y1, under, topF);
  B.box('ceiling', 'roof:ceiling', x0, x1, y0, y1, (x, y) => roof.ceil(y), under);
  const ft = FASCIA_T, drop = 0.12, rise = 0.05;
  const front = B.box('fascia', 'roof:fascia', x0 - ft, x1 + ft, y0 - ft, y0, roof.under(y0) - drop, roof.top(y0) + rise);
  B.box('fascia', 'roof:fascia', x0 - ft, x1 + ft, y1, y1 + ft, roof.under(y1) - drop, roof.top(y1) + rise);
  for (const [a, b] of [[x0 - ft, x0], [x1, x1 + ft]]) {
    B.box('fascia', 'roof:fascia', a, b, y0, y1, (x, y) => roof.under(y) - drop, (x, y) => roof.top(y) + rise);
  }
  B.box('fascia', 'roof:gutter', x0, x1, y1 + ft, y1 + ft + 0.16, roof.under(y1) - 0.10, roof.under(y1) + 0.06);
  const bDn = (x, y) => roof.beamBottom(y), bUp = (x, y) => roof.ceil(y);
  for (const bl of lines) {
    const xa = bl.x - BEAM_W / 2, xb = bl.x + BEAM_W / 2;
    B.box('glulam', 'roof:beam', xa, xb, IY0, IY1, bDn, bUp);
    // exposed beam ends: from the skin (or through the panel joint) out to the fascia
    let d = st.out;
    if (style === 'concrete') d = st.wallOut;
    else if (style === 'glass' && !bl.primary) d = -0.02;
    B.box('glulam', 'roof:beam_end', xa, xb, y0, Y0 - d, bDn, bUp);
    B.box('glulam', 'roof:beam_end', xa, xb, Y1 + d, y1, bDn, bUp);
    if (bl.primary) {
      B.box('glulam', 'structure:column', bl.x - 0.1, bl.x + 0.1, IY0, IY0 + 0.2, FLOOR, bDn);
      B.box('glulam', 'structure:column', bl.x - 0.1, bl.x + 0.1, IY1 - 0.2, IY1, FLOOR, bDn);
    }
  }
  return { slab, front };
}

// ---------------------------------------------------------------------------
// 9. Entrance canopy: projects canopy_depth beyond the outer face of the skin
// ---------------------------------------------------------------------------
function buildCanopy(B, depth, style) {
  const st = STYLE[style];
  if (!(depth > 1e-6)) return null;
  const hw = CANOPY_W / 2, ft = FASCIA_T;
  const face = Y0 - st.out;                       // outer face of the skin
  const tip = face - depth;                       // outer face of the canopy fascia
  const z0 = CANOPY_Z, z1 = CANOPY_Z + CANOPY_T;
  B.box('canopy', 'canopy:slab', -hw, hw, tip + ft, Y0 + 0.10, z0, z1);
  B.box('fascia', 'canopy:fascia', -hw - ft, hw + ft, tip, tip + ft, z0 - 0.05, z1 + 0.05);
  const back = Y0 - st.canopyFace;
  for (const [a, b] of [[-hw - ft, -hw], [hw, hw + ft]]) {
    B.box('fascia', 'canopy:fascia', a, b, tip + ft, back, z0 - 0.05, z1 + 0.05);
  }
  if (depth >= 1.0 - 1e-9) {
    for (const x of [-2.4, 0, 2.4]) B.cyl('light', 'canopy:light', x, 0.5 * (tip + face), z0 - 0.02, z0, 0.09, 12);
  }
  if (depth >= POST_MIN_DEPTH - 1e-9) {
    for (const x of [-hw + POST_INSET, hw - POST_INSET]) {
      B.cyl('steel_black', 'canopy:post', x, tip + POST_INSET, PAVE_Z, z0, POST_R, 12);
    }
  }
  return { tip, face, z0, z1, projection: face - tip };
}

// ---------------------------------------------------------------------------
// 10. Interior: stage, lectern, screen, seating, reception, exhibition, core, lights
// ---------------------------------------------------------------------------
function chair(B, x, y) {                         // faces +x (the stage)
  const w = 0.46, dp = 0.46, zs = FLOOR + 0.43, leg = 0.03, T = 'interior:chair';
  const x0 = x - dp / 2, x1 = x + dp / 2, y0 = y - w / 2, y1 = y + w / 2;
  B.box('fabric', T, x0, x1, y0, y1, zs, zs + 0.05);
  B.box('fabric', T, x0, x0 + 0.04, y0, y1, zs + 0.05, zs + 0.47);
  for (const lx of [x0 + 0.02, x1 - 0.02 - leg]) {
    for (const ly of [y0 + 0.02, y1 - 0.02 - leg]) B.box('steel_black', T, lx, lx + leg, ly, ly + leg, FLOOR, zs);
  }
}

function lightXs(lines) {
  const xs = [IX0, ...lines.map((l) => l.x).sort((a, b) => a - b), IX1];
  return (x) => {
    for (let i = 0; i + 1 < xs.length; i++) if (x >= xs[i] && x <= xs[i + 1]) return 0.5 * (xs[i] + xs[i + 1]);
    return x;
  };
}

function buildInterior(B, roof, lines) {
  // stage with a step, lectern (sloped reading top), hanging projection screen
  const SX = 6.4, SZ = FLOOR + 0.4;
  B.box('stage', 'interior:stage', SX, IX1, -3.2, 3.2, FLOOR, SZ);
  B.box('stage', 'interior:stage', SX - 0.4, SX, 2.2, 3.0, FLOOR, FLOOR + 0.2);
  B.box('oak', 'interior:lectern', 6.95, 7.4, -2.55, -2.0, SZ, SZ + 1.02);
  B.box('steel_black', 'interior:lectern', 6.9, 7.45, -2.6, -1.95, SZ + 1.02, (x) => SZ + 1.06 + ((7.45 - x) / 0.55) * 0.1);
  B.box('screen', 'interior:screen', 8.3, 8.33, -1.9, 1.9, FLOOR + 1.2, FLOOR + 3.5);
  B.box('steel_black', 'interior:screen', 8.27, 8.37, -2.0, 2.0, FLOOR + 3.5, FLOOR + 3.62);
  for (const y of [-1.8, 1.8]) B.cyl('steel_black', 'interior:screen', 8.32, y, FLOOR + 3.62, roof.ceil(y), 0.008, 6);
  // six rows of chairs facing the stage, central aisle
  for (let r = 0; r < 6; r++) {
    for (let i = 0; i < 5; i++) {
      const x = 4.75 - 0.9 * r, y = 0.95 + 0.55 * i;
      chair(B, x, y);
      chair(B, x, -y);
    }
  }
  // reception desk by the entrance
  B.box('oak', 'interior:desk', -4.6, -2.6, -3.9, -3.3, FLOOR, FLOOR + 1.02);
  B.box('stone', 'interior:desk', -4.65, -2.55, -3.95, -3.25, FLOOR + 1.02, FLOOR + 1.06);
  B.box('steel_black', 'interior:desk', -3.95, -3.45, -3.55, -3.5, FLOOR + 1.06, FLOOR + 1.4);
  // exhibition: boards along the back wall, two plinths with models
  for (const xc of [-5.2, -3.6, -2.0]) {
    B.box('display', 'interior:display', xc - 0.6, xc + 0.6, 4.08, 4.13, FLOOR + 0.6, FLOOR + 2.1);
    B.box('accent', 'interior:display', xc - 0.45, xc + 0.45, 4.07, 4.08, FLOOR + 0.95, FLOOR + 1.85);
    for (const lx of [xc - 0.55, xc + 0.52]) B.box('steel_black', 'interior:display', lx, lx + 0.03, 4.04, 4.17, FLOOR, FLOOR + 0.6);
  }
  B.box('display', 'interior:plinth', -5.0, -4.4, 2.9, 3.5, FLOOR, FLOOR + 0.9);
  B.cyl('accent', 'interior:plinth', -4.7, 3.2, FLOOR + 0.9, FLOOR + 1.25, 0.16, 10);
  B.box('display', 'interior:plinth', -3.4, -2.8, 2.9, 3.5, FLOOR, FLOOR + 0.9);
  B.box('accent', 'interior:plinth', -3.3, -2.9, 3.0, 3.4, FLOOR + 0.9, (x, y) => FLOOR + 1.02 + (3.4 - y) * 0.35);
  // service core (toilets / storage) in the back-left corner, with two doors
  B.box('core', 'interior:core', IX0, -6.2, 2.8, IY1, FLOOR, FLOOR + 2.7);
  B.box('oak', 'interior:core_door', -7.9, -7.0, 2.77, 2.8, FLOOR, FLOOR + 2.1);
  B.box('oak', 'interior:core_door', -6.2, -6.17, 3.3, 4.2, FLOOR, FLOOR + 2.1);
  // pendant lights between the beams; cords run up to the sloped ceiling
  for (const [x, y] of lampPositions(lines)) {
    B.cyl('light', 'interior:light', x, y, LAMP_Z0, LAMP_Z1, LAMP_R, 16);
    B.cyl('steel_black', 'interior:light', x, y, LAMP_Z1, roof.ceil(y), 0.006, 6);
  }
}

const LAMP_Z0 = FLOOR + 2.75, LAMP_Z1 = LAMP_Z0 + 0.22, LAMP_R = 0.2;
function lampPositions(lines) {
  const snapX = lightXs(lines), out = [];
  for (const xd of [-6.6, -4.2, -1.8, 1.8, 4.2, 6.6]) for (const y of [-2.2, 0, 2.2]) out.push([snapX(xd), y]);
  return out;
}

// does the straight line a -> b pass clear of every pendant lamp (shade and cord)?
function lampClear(lamps, a, b) {
  for (const [lx, ly] of lamps) {
    for (let i = 0; i <= 40; i++) {
      const t = i / 40, x = a[0] + t * (b[0] - a[0]), y = a[1] + t * (b[1] - a[1]), z = a[2] + t * (b[2] - a[2]);
      if (z > LAMP_Z0 - 0.1 && Math.hypot(x - lx, y - ly) < (z < LAMP_Z1 + 0.1 ? LAMP_R + 0.12 : 0.08)) return false;
    }
  }
  return true;
}

// ---------------------------------------------------------------------------
// 11. Landscape: lawn, paving, benches, low-poly trees (fixed seed)
// ---------------------------------------------------------------------------
// kept out of the front-left quadrant, so the usual 3/4 projector view is clear
const TREES = [[-12.3, 3.2], [-12.2, 8.9], [12.2, 8.8], [12.3, 0.8], [12.0, -6.8], [7.2, -10.6]];

function buildLandscape(B) {
  const [gx0, gx1, gy0, gy1, gz0, gz1] = GROUND;
  B.box('grass', 'ground', gx0, gx1, gy0, gy1, gz0, gz1);
  const sy = Y0 - SLAB_M;
  B.box('paving', 'landscape:paving', -7.5, 7.5, -9.0, sy, 0, PAVE_Z);
  B.box('paving', 'landscape:paving', -1.5, 1.5, gy0, -9.0, 0, PAVE_Z);
  for (const [x0, x1] of [[-7.1, -5.3], [5.3, 7.1]]) {
    const y0 = -8.25, y1 = -7.8;
    B.box('oak', 'landscape:bench', x0, x1, y0, y1, 0.4, 0.46);
    for (const lx of [x0 + 0.15, x1 - 0.25]) B.box('concrete', 'landscape:bench', lx, lx + 0.1, y0 + 0.03, y1 - 0.03, PAVE_Z, 0.4);
  }
  const r = rng(20260930);
  for (const [x, y] of TREES) {
    const s = 0.85 + 0.3 * r(), rot = r() * Math.PI / 2, R = 1.3 * s + 0.12 * r();
    const trunk = 1.8 * s, fol = r() < 0.5 ? 'foliage' : 'foliage_light';
    B.cyl('bark', 'landscape:tree', x, y, 0, trunk + 0.5 * s, 0.13 * s + 0.04, 7);
    const zb = trunk - 0.2, zm = zb + 1.2 * s, zt = zm + 1.5 * s;
    B.frustum(fol, 'landscape:tree', x, y, zb, 0.45 * R, zm, R, rot);
    B.frustum(fol, 'landscape:tree', x, y, zm, R, zt, 0.18 * R, rot);
    const ox = (r() - 0.5) * 0.8 * R, oy = (r() - 0.5) * 0.8 * R, R2 = 0.68 * R, rot2 = rot + Math.PI / 4;
    const zb2 = zm - 0.2 * s, zm2 = zb2 + 0.9 * s, zt2 = zm2 + 1.1 * s;
    const fol2 = fol === 'foliage' ? 'foliage_light' : 'foliage';
    B.frustum(fol2, 'landscape:tree', x + ox, y + oy, zb2, 0.4 * R2, zm2, R2, rot2);
    B.frustum(fol2, 'landscape:tree', x + ox, y + oy, zm2, R2, zt2, 0.2 * R2, rot2);
  }
}

// ---------------------------------------------------------------------------
// 12. Pins, tour, bounds
// ---------------------------------------------------------------------------
const r3 = (v) => Math.round(v * 1000) / 1000;
const unit = (v) => { const n = Math.hypot(v[0], v[1], v[2]) || 1; return v.map((c) => Math.round((c / n) * 1e4) / 1e4); };

// nearest window slot (not the entrance bay) to a position s along the facade
function nearestSlotS(lay, s, keep) {
  let best = null, bd = Infinity;
  for (const sl of lay.slots) {
    if (sl.door || !sl.rects || !sl.rects.length || (keep && !keep(sl))) continue;
    const d = Math.abs(0.5 * (sl.wa + sl.wb) - s);
    if (d < bd) { bd = d; best = sl; }
  }
  return best;
}
const sOfX = (F, x) => (x - F.ox) * F.tx;        // long facades (tangent +-x)
const sOfY = (F, y) => (y - F.oy) * F.ty;        // end walls (tangent +-y)

// centre of the lowest glass cell of a pane (clear of transoms and of a central mullion)
function paneCell(style, sl) {
  const r = sl.rects[0], h = r.z1 - r.z0;
  const n = style === 'glass' ? (h > 3.2 ? Math.ceil(h / 2.8) : 1) : (h > 2.8 ? Math.ceil(h / 2.4) : 1);
  const s = sl.wb - sl.wa > 1.6 ? sl.wa + 0.27 * (sl.wb - sl.wa) : 0.5 * (sl.wa + sl.wb);
  return [s, r.z0 + 0.5 * (h / n), r.z0 + 0.1, r.z0 + h / n - 0.1];   // s, centre, lowest / highest z in the cell
}

// a point of solid wall lining near sPref (not in or next to an opening, not behind a
// column or a beam end, below the ceiling). Returns [s, z] or null.
function solidWallPoint(lay, roof, lines, sPref, sLo, sHi, zPrefs, avoid) {
  const F = lay.F;
  const prim = F.long ? lines.filter((l) => l.primary).map((l) => l.x) : [];
  const beams = F.long ? lines.map((l) => l.x) : [];
  const ok = (s, z) => {
    const [x, y] = fpt(F, s, WALL_IN);
    if (z > roof.ceil(y) - 0.15) return false;
    if (avoid && avoid.some((a) => Math.hypot(a[0] - x, a[1] - y, a[2] - z) < 0.5)) return false;
    for (const o of lay.openings) {
      if (s > o.s0 - 0.01 && s < o.s1 + 0.01 && z > o.z0 - 0.03 && z < o.z1 + 0.03) return false;
    }
    if (prim.some((px) => Math.abs(x - px) < 0.45)) return false;   // columns stand in front
    if (z > roof.beamBottom(y) - 0.1 && beams.some((bx) => Math.abs(x - bx) < 0.15)) return false;
    return true;
  };
  for (const z of zPrefs) {
    for (let k = 0; k <= 60; k++) {
      for (const sg of k ? [1, -1] : [1]) {
        const s = sPref + sg * k * 0.05;
        if (s >= sLo && s <= sHi && ok(s, z)) return [s, z];
      }
    }
  }
  return null;
}

// the bottom of a roof beam inside a tour stop's view: of all points along all beams, the
// one closest to the view direction whose sight line clears the lamps and the other beams
function beamPinAlong(roof, lines, eye, target, avoid) {
  const lamps = lampPositions(lines);
  const xs = lines.map((l) => l.x);
  const v = [target[0] - eye[0], target[1] - eye[1], target[2] - eye[2]];
  const vl = Math.hypot(v[0], v[1], v[2]) || 1;
  const cands = [];
  for (const x of xs) {
    for (let y = IY0 + 0.4; y <= IY1 - 0.4 + 1e-9; y += 0.25) {
      if (Math.abs(y - Math.round(y / 2.2) * 2.2) < 0.45) continue;      // off the rows of lamps
      const p = [x, y, roof.beamBottom(y) - 0.02];
      const d = [p[0] - eye[0], p[1] - eye[1], p[2] - eye[2]], dl = Math.hypot(d[0], d[1], d[2]);
      if (dl < 1.5 || (avoid && avoid.some((a) => Math.hypot(a[0] - p[0], a[1] - p[1], a[2] - p[2]) < 0.5))) continue;
      const ang = Math.acos(Math.max(-1, Math.min(1, (d[0] * v[0] + d[1] * v[1] + d[2] * v[2]) / (dl * vl))));
      if (ang < (34 * Math.PI) / 180) cands.push({ pos: p, ang });
    }
  }
  cands.sort((a, b) => a.ang - b.ang);
  const beamClear = (p) => {
    for (let i = 1; i < 60; i++) {
      const t = i / 60, x = eye[0] + t * (p[0] - eye[0]), y = eye[1] + t * (p[1] - eye[1]), z = eye[2] + t * (p[2] - eye[2]);
      if (Math.hypot(x - p[0], y - p[1], z - p[2]) < 0.12) break;
      if (y < IY0 || y > IY1) continue;
      if (z > roof.beamBottom(y) - 0.02 && xs.some((bx) => Math.abs(x - bx) < BEAM_W / 2 + 0.02)) return false;
    }
    return true;
  };
  for (const c of cands) if (lampClear(lamps, eye, c.pos) && beamClear(c.pos)) return c;
  return null;
}

function makePins(lays, roof, can, style, lines, tour) {
  const st = STYLE[style], [front, right, back, left] = lays, F = front.F;
  const pins = [];
  const add = (question, mode, pos, normal, label) => {
    if (pos && pos.every(Number.isFinite)) pins.push({ question, mode, pos: pos.map(r3), normal: unit(normal), label });
  };
  const inward = (lay) => [-lay.F.nx, -lay.F.ny, 0];
  const outward = (lay) => [lay.F.nx, lay.F.ny, 0];
  const panePin = (lay, sl, mode, sPref, zMode) => {
    if (!sl) return;
    let [s, z, zLo, zHi] = paneCell(style, sl);
    if (mode === 'interior') z = Math.min(Math.max(FLOOR + 1.9, zLo), Math.max(zLo, zHi));   // eye height, over the furniture
    if (sPref !== undefined) {
      s = Math.min(Math.max(sPref, sl.wa + 0.15), sl.wb - 0.15);
      const mid = 0.5 * (sl.wa + sl.wb);
      if (st.frame && sl.wb - sl.wa > 1.6 && Math.abs(s - mid) < 0.1) s = mid + (s < mid ? -0.15 : 0.15);
    }
    if (zMode === 'top') { const r = sl.rects[sl.rects.length - 1]; z = Math.max(r.z0 + 0.1, r.z1 - 0.3); }
    const d = mode === 'exterior' ? st.pane[1] + 0.02 : st.pane[0] - 0.02;
    add('window_ratio', mode, [...fpt(lay.F, s, d), z], mode === 'exterior' ? outward(lay) : inward(lay), 'Windows');
  };
  const liningPin = (lay, sPref, sLo, sHi, zPrefs) => {
    const avoid = pins.filter((q) => q.mode === 'interior' && q.question !== 'facade_material').map((q) => q.pos);
    const p = solidWallPoint(lay, roof, lines, sPref, sLo, sHi, zPrefs, avoid);
    if (p) add('facade_material', 'interior', [...fpt(lay.F, p[0], WALL_IN - 0.02), p[1]], inward(lay), 'Façade');
  };
  const skinPin = (s) => add('facade_material', 'exterior', [...fpt(F, s, st.out + 0.02), FLOOR + 1.7], [0, -1, 0], 'Façade');
  const clearOfCanopy = (sl) => Math.min(Math.abs(X0 + sl.wa), Math.abs(X0 + sl.wb)) > CANOPY_ZONE + 0.4
    && (X0 + sl.wa) * (X0 + sl.wb) > 0;
  const stop = (id) => tour.find((t) => t.id === id);

  // --- exterior -----------------------------------------------------------------------
  // facade: the skin left of the entrance, and beside the door (seen from the approach)
  const fs = nearestSlotS(front, sOfX(F, -5.5));
  skinPin(style === 'concrete' ? 0.5 * (fs.bay.s0 + st.j / 2 + fs.wa) : fs.bay.s0);
  const door = front.bays.find((b) => b.central);
  skinPin(style === 'concrete' ? 0.5 * (door.s0 + st.j / 2 + F.L / 2 - DOOR_HALF) : door.s0);
  // windows: a front pane clear of the canopy, a pane on the -x end wall, a pane by the door
  panePin(front, nearestSlotS(front, sOfX(F, 6.5), clearOfCanopy), 'exterior');
  panePin(left, nearestSlotS(left, sOfY(left.F, 0)), 'exterior');
  panePin(front, nearestSlotS(front, sOfX(F, 1.9)), 'exterior');
  // roof: the high front fascia
  const fz = 0.5 * (roof.under(roof.y0) - 0.12 + roof.top(roof.y0) + 0.05);
  add('roof_angle', 'exterior', [-3.0, roof.y0 - FASCIA_T - 0.02, fz], [0, -1, 0], 'Roof');
  // canopy: the tip, or the entrance when there is no canopy
  if (can) add('canopy_depth', 'exterior', [0, can.tip - 0.02, CANOPY_Z + CANOPY_T / 2], [0, -1, 0], 'Canopy');
  else add('canopy_depth', 'exterior', [0, Y0 - st.canopyFace - 0.02, CANOPY_Z + CANOPY_T / 2], [0, -1, 0], 'Canopy');

  // --- interior (several per question, so every tour stop sees some ahead) --------------
  // Fixed spots first (panes, above the door, the ceiling); the searched spots (wall lining,
  // canopy band, beams) then keep 0.5 m away from pins of other questions.
  // windows: a front pane (by the window-wall stop), both end walls, the back wall, a high
  // front pane and a front pane near the door
  panePin(front, nearestSlotS(front, sOfX(F, -5.5)), 'interior');
  panePin(right, nearestSlotS(right, sOfY(right.F, 3.0)), 'interior', sOfY(right.F, 3.2));
  panePin(back, nearestSlotS(back, sOfX(back.F, 3.0)), 'interior');
  panePin(front, nearestSlotS(front, sOfX(F, -3.4)), 'interior', sOfX(F, -3.4), 'top');
  panePin(left, nearestSlotS(left, sOfY(left.F, 1.6)), 'interior');
  panePin(front, nearestSlotS(front, sOfX(F, -1.9)), 'interior');
  // canopy: above the door inside
  add('canopy_depth', 'interior', [0, IY0 + 0.02, CANOPY_Z + CANOPY_T / 2], [0, 1, 0], 'Canopy');
  // roof: the sloped ceiling between two beams
  const xr = lightXs(lines)(-3.0), yr = -1.1;
  const nDown = [0, -roof.slope, -1];
  add('roof_angle', 'interior', [xr, yr, roof.ceil(yr) - 0.02], nDown, 'Roof');
  const others = (q) => pins.filter((p) => p.mode === 'interior' && p.question !== q).map((p) => p.pos);
  // canopy: where it meets the wall either side, on the solid band, clear of columns
  if (can) {
    for (const [lo, pref, hi] of [[-3.9, -3.3, -2.8], [2.8, 3.3, 3.9]]) {
      const p = solidWallPoint(front, roof, lines, sOfX(F, pref), sOfX(F, lo), sOfX(F, hi),
        [CANOPY_Z + CANOPY_T / 2], others('canopy_depth'));
      if (p) add('canopy_depth', 'interior', [...fpt(F, p[0], WALL_IN - 0.02), p[1]], [0, 1, 0], 'Canopy');
    }
  }
  // facade lining: -x end wall (middle and by the exhibition), +x end wall beside the screen,
  // the front wall across from the window-wall stop (low and high)
  liningPin(left, sOfY(left.F, 0), sOfY(left.F, 1.2), sOfY(left.F, -1.2), [FLOOR + 1.6, FLOOR + 2.1, FLOOR + 1.1]);
  liningPin(left, sOfY(left.F, 2.0), sOfY(left.F, 2.6), sOfY(left.F, 0.9), [FLOOR + 1.7, FLOOR + 2.2, FLOOR + 1.2]);
  liningPin(right, sOfY(right.F, 3.0), sOfY(right.F, 2.25), sOfY(right.F, 4.3), [FLOOR + 2.0, FLOOR + 2.5, FLOOR + 1.5, FLOOR + 3.0]);
  liningPin(front, sOfX(F, -2.2), sOfX(F, -4.3), sOfX(F, -0.3), [CANOPY_Z + CANOPY_T / 2, FLOOR + 2.4, FLOOR + 3.4, FLOOR + 1.8, FLOOR + 4.0]);
  const zHigh = roof.ceil(IY0) - 0.35;
  liningPin(front, sOfX(F, -2.6), sOfX(F, -4.3), sOfX(F, -0.3), [zHigh, zHigh - 0.3, zHigh - 0.6, zHigh - 1.0]);
  // roof: a beam ahead of each interior stop
  for (const id of ['entrance', 'hall', 'stage', 'gallery', 'roof']) {
    const t = stop(id);
    const b = t && beamPinAlong(roof, lines, t.eye, t.target, others('roof_angle'));
    if (b) add('roof_angle', 'interior', b.pos, nDown, 'Roof');
  }
  return pins;
}

function makeTour(roof) {
  const eye = FLOOR + 1.6;
  const v = (a) => a.map(r3);
  return [
    { id: 'approach', label: 'Approach', eye: v([0, -10.2, PAVE_Z + 1.6]), target: v([0, Y0, 2.6]) },
    { id: 'entrance', label: 'Entrance', eye: v([0, IY0 + 0.7, eye]), target: v([4.5, 1.0, FLOOR + 1.4]) },
    { id: 'hall', label: 'Hall', eye: v([-2.6, 0, eye]), target: v([8.3, 0, FLOOR + 1.9]) },
    { id: 'stage', label: 'Stage', eye: v([7.7, 1.2, FLOOR + 0.4 + 1.6]), target: v([-6, -0.4, FLOOR + 1.3]) },
    { id: 'gallery', label: 'Exhibition', eye: v([-1.2, 1.4, eye]), target: v([-5.6, 4.2, eye]) },
    // 3.25 m back from the glazed front wall, looking across it toward the entrance
    { id: 'windows', label: 'Window wall', eye: v([-5.5, IY0 + 3.25, eye]), target: v([-1.6, IY0, FLOOR + 2.0]) },
    { id: 'roof', label: 'Roof structure', eye: v([-3.2, 0.8, eye]), target: v([-2.6, -2.8, roof.beamBottom(-2.8)]) },
  ];
}

function computeBounds(parts) {
  const mn = [Infinity, Infinity, Infinity], mx = [-Infinity, -Infinity, -Infinity];
  const put = (x, y, z) => {
    if (x < mn[0]) mn[0] = x; if (y < mn[1]) mn[1] = y; if (z < mn[2]) mn[2] = z;
    if (x > mx[0]) mx[0] = x; if (y > mx[1]) mx[1] = y; if (z > mx[2]) mx[2] = z;
  };
  for (const q of parts) {
    if (q.p) for (let i = 0; i < 24; i += 3) put(q.p[i], q.p[i + 1], q.p[i + 2]);
    else { const [cx, cy, z0, z1, r] = q.cyl; put(cx - r, cy - r, z0); put(cx + r, cy + r, z1); }
  }
  return { min: mn.map(r3), max: mx.map(r3) };
}

// ---------------------------------------------------------------------------
// 13. Entry point
// ---------------------------------------------------------------------------
function build(params) {
  const P = normalizeParams(params);
  const style = P.facade_material, ratio = P.window_ratio / 100, depth = P.canopy_depth;
  const roof = roofGeometry(P.roof_angle);
  const B = makeBuilder();
  buildLandscape(B);
  buildPlinth(B);
  const lays = FACADES.map((F) => layoutFacade(F, style, ratio, roof, depth > 1e-6));
  for (const lay of lays) buildFacade(B, lay, roof);
  buildCorners(B, style, roof);
  buildDoor(B, FACADES[0]);
  const lines = beamLines(lays[0], lays[2], roof);
  buildRoof(B, roof, lines, style);
  const can = buildCanopy(B, depth, style);
  buildInterior(B, roof, lines);
  const parts = B.parts;
  const tour = makeTour(roof);
  const pins = makePins(lays, roof, can, style, lines, tour);
  const byTag = {};
  for (const q of parts) byTag[q.t] = (byTag[q.t] || 0) + 1;
  const materials = {};
  for (const k of Object.keys(MATERIALS)) {
    const d = MATERIALS[k];
    materials[k] = { ...d, color: d.color.slice() };
    if (d.emissive) materials[k].emissive = d.emissive.slice();
  }
  return {
    version: MODEL_VERSION, units: 'm', up: 'z', params: P,
    materials, parts, pins, tour,
    walkable: { min: [IX0 + 0.3, IY0 + 0.3], max: [IX1 - 0.3, IY1 - 0.3], floor_z: FLOOR },
    bounds: computeBounds(parts),
    stats: { parts: parts.length, byTag },
  };
}

// minimal model if something unforeseen goes wrong (never throw to the caller)
function fallbackModel(params, err) {
  const P = normalizeParams(null);
  const B = makeBuilder();
  B.box('grass', 'ground', GROUND[0], GROUND[1], GROUND[2], GROUND[3], GROUND[4], GROUND[5]);
  B.box('slab', 'slab', X0 - SLAB_M, X1 + SLAB_M, Y0 - SLAB_M, Y1 + SLAB_M, 0, FLOOR);
  return {
    version: MODEL_VERSION, units: 'm', up: 'z', params: P, materials: { grass: MATERIALS.grass, slab: MATERIALS.slab },
    parts: B.parts, pins: [], tour: [], walkable: { min: [IX0 + 0.3, IY0 + 0.3], max: [IX1 - 0.3, IY1 - 0.3], floor_z: FLOOR },
    bounds: computeBounds(B.parts), stats: { parts: B.parts.length, byTag: { ground: 1, slab: 1 } },
    error: String(err && err.message ? err.message : err),
  };
}

export function buildPavilion(params) {
  try {
    return build(params);
  } catch (err) {
    try { return build(null); } catch (e2) { return fallbackModel(params, err); }
  }
}

// internals for tests and tools (not part of the viewer contract)
export const _internal = {
  X0, X1, Y0, Y1, FLOOR, EAVE_LOW, STYLE, FACADES, DEFAULTS, SPEC, MATERIAL_OPTIONS,
  roofGeometry, layoutFacade, IX0, IX1, IY0, IY1, CANOPY_Z, CANOPY_T,
};
