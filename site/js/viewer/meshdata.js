// Plurarch viewer geometry: turns a Model (docs/MODEL.md; Z-up, metres) into flat typed arrays, one
// group per material, in three.js Y-up coordinates. It also builds a compact occluder set for cheap
// ray tests (pin occlusion, tour taps) in MODEL coordinates.
// Part types: hexahedron { p: 8 corners }, vertical cylinder { cyl }, and triangle mesh
// { tri: { pos: Float32Array xyz, idx: Uint16Array | Uint32Array | null } } (flat-shaded; the
// converted arrays are cached per tri object, so rebuilding a model that reuses its meshes only copies).
// Pure ES module: no DOM and no three.js, so Node can test and time it.
//
// Axis change: model (x, y, z), z up  ->  three (x, z, -y), y up. This is a proper rotation
// (-90 degrees about x), so face winding and handedness are kept.

/** Texture tile size in metres per material kind: the procedural texture covers one tile. */
export const TILE_M = {
  wood: 1.2, concrete: 2.4, stone: 2.4, ground: 4, foliage: 2, fabric: 0.45,
  plaster: 3, metal: 1.2, paint: 3, glass: 1, light: 1, frit: 0.6,
};

// Hexahedron faces as corner indices (bottom 0-3 CCW from above, top 4-7 above them).
const FACES = [[0, 3, 2, 1], [4, 5, 6, 7], [0, 1, 5, 4], [1, 2, 6, 5], [2, 3, 7, 6], [3, 0, 4, 7]];

/** Question index (1-based, 0 = none) for a part tag, from QUESTION_TAGS (tag prefixes). */
export function tagMatcher(questionTags) {
  const keys = Object.keys(questionTags || {});
  const list = keys.map((k, i) => [i + 1, [].concat(questionTags[k] || []).map(String).filter(Boolean)]);
  const cache = new Map();
  const fn = (tag) => {
    const t = typeof tag === 'string' ? tag : '';
    let v = cache.get(t);
    if (v === undefined) {
      v = 0;
      for (const [idx, prefixes] of list) {
        if (prefixes.some((p) => t.startsWith(p))) { v = idx; break; }
      }
      cache.set(t, v);
    }
    return v;
  };
  fn.keys = keys;
  return fn;
}

function kindOf(def) {
  const k = def && typeof def.kind === 'string' ? def.kind : 'plaster';
  return Object.prototype.hasOwnProperty.call(TILE_M, k) ? k : 'plaster';
}

function isHex(part) {
  const p = part.p;
  if (!p || p.length < 24) return false;
  for (let i = 0; i < 24; i++) if (!Number.isFinite(p[i])) return false;
  return true;
}

function isCyl(part) {
  const c = part.cyl;
  if (!c || c.length < 5) return false;
  for (let i = 0; i < 5; i++) if (!Number.isFinite(c[i])) return false;
  return c[4] > 0 && c[3] !== c[2];
}

function isTri(part) {
  const t = part.tri;
  if (!t || !t.pos || t.pos.length < 9) return false;
  return t.idx ? t.idx.length >= 3 : true;
}
const triCount = (t) => (t.idx ? Math.floor(t.idx.length / 3) : Math.floor(t.pos.length / 9));

// Converted (de-indexed, three.js frame, flat normals, planar metre UVs) arrays per tri object.
const TRI_CACHE = new WeakMap();
// Groups made only of tri meshes that are identical to the previous build (same meshes, same order,
// same tags) are returned as the SAME object, so the viewer can keep their GPU buffers.
const GROUP_CACHE = new Map();
const OBJ_ID = new WeakMap();
let nextObjId = 1;
const objId = (o) => { let v = OBJ_ID.get(o); if (!v) { v = nextObjId++; OBJ_ID.set(o, v); } return v; };
function triArrays(tri, tile) {
  let c = TRI_CACHE.get(tri);
  if (c && c.tile === tile) return c;
  const P = tri.pos; const I = tri.idx;
  const nT = triCount(tri);
  const pos = new Float32Array(nT * 9); const nrm = new Float32Array(nT * 9); const uv = new Float32Array(nT * 6);
  const n = [0, 0, 0]; const uA = [0, 0, 0]; const vA = [0, 0, 0];
  let v = 0;
  let x0 = Infinity; let y0 = Infinity; let z0 = Infinity; let x1 = -Infinity; let y1 = -Infinity; let z1 = -Infinity;
  for (let t = 0; t < nT; t++) {
    const a = (I ? I[t * 3] : t * 3) * 3; const b = (I ? I[t * 3 + 1] : t * 3 + 1) * 3; const cc = (I ? I[t * 3 + 2] : t * 3 + 2) * 3;
    const ax = P[a]; const ay = P[a + 1]; const az = P[a + 2];
    const e1x = P[b] - ax; const e1y = P[b + 1] - ay; const e1z = P[b + 2] - az;
    const e2x = P[cc] - ax; const e2y = P[cc + 1] - ay; const e2z = P[cc + 2] - az;
    let nx = e1y * e2z - e1z * e2y; let ny = e1z * e2x - e1x * e2z; let nz = e1x * e2y - e1y * e2x;
    const len = Math.hypot(nx, ny, nz);
    if (len < 1e-12) continue; // degenerate
    nx /= len; ny /= len; nz /= len;
    n[0] = nx; n[1] = ny; n[2] = nz;
    uvAxes(n, uA, vA);
    for (const k of [a, b, cc]) {
      const x = P[k]; const y = P[k + 1]; const z = P[k + 2];
      if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; if (z < z0) z0 = z; if (z > z1) z1 = z;
      pos[v * 3] = x; pos[v * 3 + 1] = z; pos[v * 3 + 2] = -y;
      nrm[v * 3] = nx; nrm[v * 3 + 1] = nz; nrm[v * 3 + 2] = -ny;
      uv[v * 2] = (x * uA[0] + y * uA[1] + z * uA[2]) / tile;
      uv[v * 2 + 1] = (x * vA[0] + y * vA[1] + z * vA[2]) / tile;
      v++;
    }
  }
  c = { tile, n: v, pos: pos.subarray(0, v * 3), nrm: nrm.subarray(0, v * 3), uv: uv.subarray(0, v * 2), box: [x0, y0, z0, x1, y1, z1] };
  TRI_CACHE.set(tri, c);
  return c;
}

function segOf(part) {
  const s = Math.round(Number(part.seg) || 12);
  return Math.max(3, Math.min(48, s));
}

// Small deterministic hash -> [0, 1), for per-part texture offsets (wood boards differ).
function hash01(i) {
  let x = (i + 1) * 0x9E3779B1;
  x ^= x >>> 15; x = Math.imul(x, 0x85EBCA77); x ^= x >>> 13;
  return ((x >>> 0) % 10007) / 10007;
}

const isTransparent = (def) => !!def && (def.kind === 'glass' || (Number.isFinite(def.opacity) && def.opacity < 0.95));

/**
 * Build typed arrays per material.
 * @param {object} model  a Model (docs/MODEL.md)
 * @param {{questionTags?: object, qOf?: Function}} opts
 * @returns {{groups: Array, occ: object, footprint: object, aabb: object, stats: object}}
 */
export function buildMeshData(model, opts = {}) {
  const t0 = nowMs();
  const qOf = opts.qOf || tagMatcher(opts.questionTags);
  const mats = (model && model.materials) || {};
  const all = Array.isArray(model && model.parts) ? model.parts : [];
  // The flat site plate (tag 'ground', material kind 'ground') is drawn by the viewer as an endless
  // ground at the same height instead, so its edges never show. Everything else is meshed.
  let groundZ = null;
  const parts = opts.dropGroundPlate ? all.filter((part) => {
    if (!part || part.t !== 'ground' || !mats[part.m] || mats[part.m].kind !== 'ground' || !isHex(part)) return true;
    for (let c = 4; c < 8; c++) groundZ = groundZ == null ? part.p[c * 3 + 2] : Math.max(groundZ, part.p[c * 3 + 2]);
    return false;
  }) : all;

  // Pass 1: count vertices and indices per material.
  const groups = new Map();
  let nOcc = 0;
  for (const part of parts) {
    if (!part || typeof part !== 'object') continue;
    const hex = isHex(part);
    const cyl = !hex && isCyl(part);
    const tri = !hex && !cyl && isTri(part);
    if (!hex && !cyl && !tri) continue;
    const key = Object.prototype.hasOwnProperty.call(mats, part.m) ? String(part.m) : '__default';
    let g = groups.get(key);
    if (!g) {
      g = { key, kind: kindOf(mats[key]), maxV: 0, maxI: 0, allTri: true, sig: 0, nParts: 0 };
      groups.set(key, g);
    }
    g.nParts++;
    if (tri) g.sig = (Math.imul(g.sig, 31) + objId(part.tri) * 7 + qOf(part.t)) | 0; else g.allTri = false;
    if (hex) { g.maxV += 24; g.maxI += 36; } else if (cyl) {
      const s = segOf(part);
      g.maxV += (s + 1) * 4; g.maxI += s * 12;
    } else { const nt = triCount(part.tri); g.maxV += nt * 3; g.maxI += nt * 3; }
    if (!isTransparent(mats[key]) && !part.noOcclude) nOcc++;
  }
  for (const g of groups.values()) {
    g.tile = TILE_M[g.kind] || 2;
    const hit = g.allTri ? GROUP_CACHE.get(g.key) : null;
    if (hit && hit.sig === g.sig && hit.nParts === g.nParts && hit.tile === g.tile) { g.reuse = hit.out; continue; }
    g.pos = new Float32Array(g.maxV * 3);
    g.nrm = new Float32Array(g.maxV * 3);
    g.uv = new Float32Array(g.maxV * 2);
    g.q = new Uint8Array(g.maxV);
    g.idx = g.maxV > 65535 ? new Uint32Array(g.maxI) : new Uint16Array(g.maxI);
    g.v = 0;
    g.i = 0;
    g.tile = TILE_M[g.kind] || 2;
  }

  // Occluders (model coordinates): type 0 = hexahedron (6 planes), 1 = vertical cylinder,
  // 2 = triangle mesh (its AABB first, then its triangles, kept in the three.js frame in occ.tri).
  const occ = {
    n: 0,
    type: new Uint8Array(nOcc),
    q: new Uint8Array(nOcc),
    aabb: new Float32Array(nOcc * 6),
    data: new Float32Array(nOcc * 24),
    tri: new Array(nOcc),
  };
  const fpMin = [Infinity, Infinity, Infinity];
  const fpMax = [-Infinity, -Infinity, -Infinity];
  const allMin = [Infinity, Infinity, Infinity];
  const allMax = [-Infinity, -Infinity, -Infinity];

  const n = [0, 0, 0];
  const uA = [0, 0, 0];
  const vA = [0, 0, 0];
  const planes = new Float32Array(24);
  let tris = 0;

  for (let pi = 0; pi < parts.length; pi++) {
    const part = parts[pi];
    if (!part || typeof part !== 'object') continue;
    const hex = isHex(part);
    const cyl = !hex && isCyl(part);
    const tri = !hex && !cyl && isTri(part);
    if (!hex && !cyl && !tri) continue;
    const key = Object.prototype.hasOwnProperty.call(mats, part.m) ? String(part.m) : '__default';
    const g = groups.get(key);
    const q = qOf(part.t);
    const tile = g.tile;
    const offU = g.kind === 'wood' ? hash01(pi) * 7.31 : 0;
    const offV = g.kind === 'wood' ? hash01(pi + 7919) * 3.17 : 0;
    const tag = typeof part.t === 'string' ? part.t : '';
    const inFootprint = !(tag === 'ground' || tag.startsWith('ground:') || tag.startsWith('landscape'));
    const occluder = !isTransparent(mats[key]) && !part.noOcclude;
    const bmin = [Infinity, Infinity, Infinity];
    const bmax = [-Infinity, -Infinity, -Infinity];

    if (tri && g.reuse) {
      const c = triArrays(part.tri, tile);
      tris += c.n / 3;
      bmin[0] = c.box[0]; bmin[1] = c.box[1]; bmin[2] = c.box[2];
      bmax[0] = c.box[3]; bmax[1] = c.box[4]; bmax[2] = c.box[5];
      if (occluder && c.n) {
        const o = occ.n++;
        occ.type[o] = 2;
        occ.q[o] = q;
        occ.tri[o] = c.pos;
        setAabb(occ.aabb, o, bmin, bmax);
      }
    } else if (tri) {
      const c = triArrays(part.tri, tile);
      const base = g.v;
      g.pos.set(c.pos, base * 3);
      g.nrm.set(c.nrm, base * 3);
      g.uv.set(c.uv, base * 2);
      g.q.fill(q, base, base + c.n);
      for (let k = 0; k < c.n; k++) g.idx[g.i++] = base + k;
      g.v += c.n;
      tris += c.n / 3;
      bmin[0] = c.box[0]; bmin[1] = c.box[1]; bmin[2] = c.box[2];
      bmax[0] = c.box[3]; bmax[1] = c.box[4]; bmax[2] = c.box[5];
      if (occluder && c.n) {
        const o = occ.n++;
        occ.type[o] = 2;
        occ.q[o] = q;
        occ.tri[o] = c.pos;
        setAabb(occ.aabb, o, bmin, bmax);
      }
    } else if (hex) {
      const p = part.p;
      let cx = 0; let cy = 0; let cz = 0;
      for (let c = 0; c < 8; c++) {
        const x = p[c * 3]; const y = p[c * 3 + 1]; const z = p[c * 3 + 2];
        cx += x; cy += y; cz += z;
        if (x < bmin[0]) bmin[0] = x; if (x > bmax[0]) bmax[0] = x;
        if (y < bmin[1]) bmin[1] = y; if (y > bmax[1]) bmax[1] = y;
        if (z < bmin[2]) bmin[2] = z; if (z > bmax[2]) bmax[2] = z;
      }
      cx /= 8; cy /= 8; cz /= 8;
      planes.fill(0);
      for (let f = 0; f < 6; f++) {
        const F = FACES[f];
        // Newell normal of the quad (robust for slightly non-planar quads)
        let nx = 0; let ny = 0; let nz = 0; let fx = 0; let fy = 0; let fz = 0;
        for (let k = 0; k < 4; k++) {
          const a = F[k] * 3; const b = F[(k + 1) & 3] * 3;
          const ax = p[a]; const ay = p[a + 1]; const az = p[a + 2];
          const bx = p[b]; const by = p[b + 1]; const bz = p[b + 2];
          nx += (ay - by) * (az + bz);
          ny += (az - bz) * (ax + bx);
          nz += (ax - bx) * (ay + by);
          fx += ax; fy += ay; fz += az;
        }
        fx /= 4; fy /= 4; fz /= 4;
        const len = Math.hypot(nx, ny, nz);
        if (len < 1e-9) continue; // degenerate face (zero area)
        nx /= len; ny /= len; nz /= len;
        let order = F;
        if (nx * (fx - cx) + ny * (fy - cy) + nz * (fz - cz) < -1e-9) { // points inwards: flip
          nx = -nx; ny = -ny; nz = -nz;
          order = [F[0], F[3], F[2], F[1]];
        }
        planes[f * 4] = nx; planes[f * 4 + 1] = ny; planes[f * 4 + 2] = nz;
        planes[f * 4 + 3] = nx * fx + ny * fy + nz * fz;
        n[0] = nx; n[1] = ny; n[2] = nz;
        uvAxes(n, uA, vA);
        const base = g.v;
        for (let k = 0; k < 4; k++) {
          const c = order[k] * 3;
          const x = p[c]; const y = p[c + 1]; const z = p[c + 2];
          const vi = g.v * 3;
          g.pos[vi] = x; g.pos[vi + 1] = z; g.pos[vi + 2] = -y;
          g.nrm[vi] = nx; g.nrm[vi + 1] = nz; g.nrm[vi + 2] = -ny;
          g.uv[g.v * 2] = (x * uA[0] + y * uA[1] + z * uA[2]) / tile + offU;
          g.uv[g.v * 2 + 1] = (x * vA[0] + y * vA[1] + z * vA[2]) / tile + offV;
          g.q[g.v] = q;
          g.v++;
        }
        g.idx[g.i++] = base; g.idx[g.i++] = base + 1; g.idx[g.i++] = base + 2;
        g.idx[g.i++] = base; g.idx[g.i++] = base + 2; g.idx[g.i++] = base + 3;
        tris += 2;
      }
      if (occluder) {
        const o = occ.n++;
        occ.type[o] = 0;
        occ.q[o] = q;
        occ.data.set(planes, o * 24);
        setAabb(occ.aabb, o, bmin, bmax);
      }
    } else {
      const [cx, cy, za, zb, r] = part.cyl;
      const z0 = Math.min(za, zb); const z1 = Math.max(za, zb);
      const s = segOf(part);
      bmin[0] = cx - r; bmin[1] = cy - r; bmin[2] = z0;
      bmax[0] = cx + r; bmax[1] = cy + r; bmax[2] = z1;
      // side (smooth normals, seam duplicated for UVs)
      const base = g.v;
      for (let k = 0; k <= s; k++) {
        const a = (k / s) * Math.PI * 2;
        const ca = Math.cos(a); const sa = Math.sin(a);
        const x = cx + r * ca; const y = cy + r * sa;
        const u = (a * r) / tile + offU;
        for (let e = 0; e < 2; e++) {
          const z = e ? z1 : z0;
          const vi = g.v * 3;
          g.pos[vi] = x; g.pos[vi + 1] = z; g.pos[vi + 2] = -y;
          g.nrm[vi] = ca; g.nrm[vi + 1] = 0; g.nrm[vi + 2] = -sa;
          g.uv[g.v * 2] = u; g.uv[g.v * 2 + 1] = z / tile + offV;
          g.q[g.v] = q;
          g.v++;
        }
      }
      for (let k = 0; k < s; k++) {
        const b0 = base + k * 2; const t0 = b0 + 1; const b1 = b0 + 2; const t1 = b0 + 3;
        g.idx[g.i++] = b0; g.idx[g.i++] = b1; g.idx[g.i++] = t1;
        g.idx[g.i++] = b0; g.idx[g.i++] = t1; g.idx[g.i++] = t0;
        tris += 2;
      }
      // caps: centre + ring (bottom faces down, top faces up)
      for (let e = 0; e < 2; e++) {
        const z = e ? z1 : z0;
        const ny = e ? 1 : -1;
        const c0 = g.v;
        const push = (x, y) => {
          const vi = g.v * 3;
          g.pos[vi] = x; g.pos[vi + 1] = z; g.pos[vi + 2] = -y;
          g.nrm[vi] = 0; g.nrm[vi + 1] = ny; g.nrm[vi + 2] = 0;
          g.uv[g.v * 2] = x / tile + offU; g.uv[g.v * 2 + 1] = y / tile + offV;
          g.q[g.v] = q;
          g.v++;
        };
        push(cx, cy);
        for (let k = 0; k < s; k++) {
          const a = (k / s) * Math.PI * 2;
          push(cx + r * Math.cos(a), cy + r * Math.sin(a));
        }
        for (let k = 0; k < s; k++) {
          const a = c0 + 1 + k; const b = c0 + 1 + ((k + 1) % s);
          if (e) { g.idx[g.i++] = c0; g.idx[g.i++] = a; g.idx[g.i++] = b; } else { g.idx[g.i++] = c0; g.idx[g.i++] = b; g.idx[g.i++] = a; }
          tris += 1;
        }
      }
      if (occluder) {
        const o = occ.n++;
        occ.type[o] = 1;
        occ.q[o] = q;
        occ.data[o * 24] = cx; occ.data[o * 24 + 1] = cy; occ.data[o * 24 + 2] = z0;
        occ.data[o * 24 + 3] = z1; occ.data[o * 24 + 4] = r;
        setAabb(occ.aabb, o, bmin, bmax);
      }
    }
    for (let a = 0; a < 3; a++) {
      if (bmin[a] < allMin[a]) allMin[a] = bmin[a];
      if (bmax[a] > allMax[a]) allMax[a] = bmax[a];
      if (inFootprint) {
        if (bmin[a] < fpMin[a]) fpMin[a] = bmin[a];
        if (bmax[a] > fpMax[a]) fpMax[a] = bmax[a];
      }
    }
  }

  const out = [];
  for (const g of groups.values()) {
    if (g.reuse) { out.push(g.reuse); continue; }
    if (!g.v) continue;
    const res = {
      key: g.key,
      kind: g.kind,
      position: g.pos.subarray(0, g.v * 3),
      normal: g.nrm.subarray(0, g.v * 3),
      uv: g.uv.subarray(0, g.v * 2),
      q: g.q.subarray(0, g.v),
      index: g.idx.subarray(0, g.i),
      vertexCount: g.v,
      indexCount: g.i,
    };
    out.push(res);
    if (g.allTri) GROUP_CACHE.set(g.key, { sig: g.sig, nParts: g.nParts, tile: g.tile, out: res });
  }
  const finite = (a) => a.every(Number.isFinite);
  const aabb = finite(allMin) ? { min: allMin, max: allMax } : { min: [-9, -5, 0], max: [9, 5, 6] };
  const footprint = finite(fpMin) ? { min: fpMin, max: fpMax } : aabb;
  return { groups: out, occ, footprint, aabb, groundZ, obstacles: obstaclesOf(parts, model), stats: { parts: all.length, tris, occluders: occ.n, ms: nowMs() - t0 } };
}

// 2D footprints (x0, y0, x1, y1 in model coordinates) of furniture, columns and other interior
// parts that stand in a walker's way: tags 'interior:*' and 'structure:*' whose height range meets
// the body (floor to floor + 1.7 m). Hanging lamps and ceiling parts are left out.
function obstaclesOf(parts, model) {
  const wk = model && model.walkable;
  const fz = wk && Number.isFinite(wk.floor_z) ? wk.floor_z : 0;
  const out = [];
  for (const part of parts) {
    const t = part && typeof part.t === 'string' ? part.t : '';
    if (!(t.startsWith('interior') || t.startsWith('structure'))) continue;
    let x0 = Infinity; let y0 = Infinity; let z0 = Infinity; let x1 = -Infinity; let y1 = -Infinity; let z1 = -Infinity;
    if (isHex(part)) {
      const p = part.p;
      for (let c = 0; c < 8; c++) {
        const x = p[c * 3]; const y = p[c * 3 + 1]; const z = p[c * 3 + 2];
        if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; if (z < z0) z0 = z; if (z > z1) z1 = z;
      }
    } else if (isCyl(part)) {
      const [cx, cy, za, zb, r] = part.cyl;
      x0 = cx - r; x1 = cx + r; y0 = cy - r; y1 = cy + r; z0 = Math.min(za, zb); z1 = Math.max(za, zb);
    } else if (isTri(part)) {
      const P = part.tri.pos;
      for (let k = 0; k < P.length; k += 3) {
        if (P[k] < x0) x0 = P[k]; if (P[k] > x1) x1 = P[k];
        if (P[k + 1] < y0) y0 = P[k + 1]; if (P[k + 1] > y1) y1 = P[k + 1];
        if (P[k + 2] < z0) z0 = P[k + 2]; if (P[k + 2] > z1) z1 = P[k + 2];
      }
    } else continue;
    if (z0 > fz + 1.7 || z1 < fz + 0.05) continue;
    out.push(x0, y0, x1, y1);
  }
  return new Float32Array(out);
}

function setAabb(arr, o, bmin, bmax) {
  const k = o * 6;
  arr[k] = bmin[0]; arr[k + 1] = bmin[1]; arr[k + 2] = bmin[2];
  arr[k + 3] = bmax[0]; arr[k + 4] = bmax[1]; arr[k + 5] = bmax[2];
}

// Planar UV axes for a face normal (model coordinates, z up): walls get u along the wall and v up;
// near-horizontal faces get u = x and v in the plane. Units are metres (divided by the tile).
function uvAxes(n, u, v) {
  if (Math.abs(n[2]) < 0.95) {
    // u = normalize(Z x n) = (-ny, nx, 0)
    const l = Math.hypot(n[0], n[1]) || 1;
    u[0] = -n[1] / l; u[1] = n[0] / l; u[2] = 0;
  } else {
    u[0] = 1; u[1] = 0; u[2] = 0;
  }
  // v = n x u
  v[0] = n[1] * u[2] - n[2] * u[1];
  v[1] = n[2] * u[0] - n[0] * u[2];
  v[2] = n[0] * u[1] - n[1] * u[0];
}

/**
 * First occluder hit along a ray in MODEL coordinates (z up). Returns the ray parameter t of the
 * entry point (dir need not be unit length; t scales with it), or Infinity if nothing is hit
 * before maxT. `skipQ` (optional) ignores occluders of one question index.
 */
export function rayFirstHit(occ, ox, oy, oz, dx, dy, dz, maxT = Infinity, skipQ = -1) {
  if (!occ || !occ.n) return Infinity;
  const ix = 1 / dx; const iy = 1 / dy; const iz = 1 / dz;
  const A = occ.aabb; const D = occ.data;
  let best = maxT;
  for (let o = 0; o < occ.n; o++) {
    if (skipQ >= 0 && occ.q[o] === skipQ) continue;
    const k = o * 6;
    // slab test on the AABB
    let t1 = (A[k] - ox) * ix; let t2 = (A[k + 3] - ox) * ix;
    let tmin = Math.min(t1, t2); let tmax = Math.max(t1, t2);
    t1 = (A[k + 1] - oy) * iy; t2 = (A[k + 4] - oy) * iy;
    tmin = Math.max(tmin, Math.min(t1, t2)); tmax = Math.min(tmax, Math.max(t1, t2));
    t1 = (A[k + 2] - oz) * iz; t2 = (A[k + 5] - oz) * iz;
    tmin = Math.max(tmin, Math.min(t1, t2)); tmax = Math.min(tmax, Math.max(t1, t2));
    if (!(tmax >= Math.max(tmin, 0)) || tmin >= best) continue;
    const d = o * 24;
    let tEnter = 0; let tExit = best;
    if (occ.type[o] === 2) {
      // the mesh's own triangles (a wall's AABB can cover a whole building); same ray parameter t
      const P = occ.tri ? occ.tri[o] : null;
      best = P ? rayTris(P, ox, oz, -oy, dx, dz, -dy, best) : Math.min(best, Math.max(tmin, 0));
    } else if (occ.type[o] === 0) {
      let hit = true;
      for (let f = 0; f < 6; f++) {
        const nx = D[d + f * 4]; const ny = D[d + f * 4 + 1]; const nz = D[d + f * 4 + 2];
        if (nx === 0 && ny === 0 && nz === 0) continue;
        const dist = D[d + f * 4 + 3] - (nx * ox + ny * oy + nz * oz);
        const den = nx * dx + ny * dy + nz * dz;
        if (Math.abs(den) < 1e-12) { if (dist < 0) { hit = false; break; } continue; }
        const t = dist / den;
        if (den < 0) { if (t > tEnter) tEnter = t; } else if (t < tExit) tExit = t;
        if (tEnter > tExit) { hit = false; break; }
      }
      if (hit && tEnter <= tExit && tEnter < best) best = tEnter;
    } else {
      const cx = D[d]; const cy = D[d + 1]; const z0 = D[d + 2]; const z1 = D[d + 3]; const r = D[d + 4];
      // z range
      if (Math.abs(dz) < 1e-12) { if (oz < z0 || oz > z1) continue; } else {
        let ta = (z0 - oz) / dz; let tb = (z1 - oz) / dz;
        if (ta > tb) { const s = ta; ta = tb; tb = s; }
        tEnter = Math.max(tEnter, ta); tExit = Math.min(tExit, tb);
      }
      // circle in xy
      const px = ox - cx; const py = oy - cy;
      const a = dx * dx + dy * dy;
      const c = px * px + py * py - r * r;
      if (a < 1e-12) { if (c > 0) continue; } else {
        const b = px * dx + py * dy;
        const disc = b * b - a * c;
        if (disc < 0) continue;
        const sq = Math.sqrt(disc);
        tEnter = Math.max(tEnter, (-b - sq) / a); tExit = Math.min(tExit, (-b + sq) / a);
      }
      if (tEnter <= tExit && tEnter < best) best = tEnter;
    }
  }
  return best < maxT ? best : Infinity;
}

// Nearest hit t in (0, best) of the ray o + t d with de-indexed triangles P (xyz per vertex), two-sided
// (Moller-Trumbore). d need not be unit length.
function rayTris(P, ox, oy, oz, dx, dy, dz, best) {
  for (let i = 0; i < P.length; i += 9) {
    const ax = P[i]; const ay = P[i + 1]; const az = P[i + 2];
    const e1x = P[i + 3] - ax; const e1y = P[i + 4] - ay; const e1z = P[i + 5] - az;
    const e2x = P[i + 6] - ax; const e2y = P[i + 7] - ay; const e2z = P[i + 8] - az;
    const px = dy * e2z - dz * e2y; const py = dz * e2x - dx * e2z; const pz = dx * e2y - dy * e2x;
    const det = e1x * px + e1y * py + e1z * pz;
    if (det === 0) continue;
    const inv = 1 / det;
    const sx = ox - ax; const sy = oy - ay; const sz = oz - az;
    const u = (sx * px + sy * py + sz * pz) * inv;
    if (u < 0 || u > 1) continue;
    const qx = sy * e1z - sz * e1y; const qy = sz * e1x - sx * e1z; const qz = sx * e1y - sy * e1x;
    const v = (dx * qx + dy * qy + dz * qz) * inv;
    if (v < 0 || u + v > 1) continue;
    const t = (e2x * qx + e2y * qy + e2z * qz) * inv;
    if (t > 1e-7 && t < best) best = t;
  }
  return best;
}

function nowMs() {
  return typeof performance !== 'undefined' && performance.now ? performance.now() : Date.now();
}
