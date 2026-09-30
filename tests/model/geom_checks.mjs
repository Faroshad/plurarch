// Geometry helpers shared by the pavilion tests: hexahedron faces, validity, point-in-part,
// and a coplanar-overlap (z-fighting) detector.

export const HEX_FACES = [[0, 3, 2, 1], [4, 5, 6, 7], [0, 1, 5, 4], [1, 2, 6, 5], [2, 3, 7, 6], [3, 0, 4, 7]];
const EDGES = [[0, 1], [1, 2], [2, 3], [3, 0], [4, 5], [5, 6], [6, 7], [7, 4], [0, 4], [1, 5], [2, 6], [3, 7]];

export const corners = (p) => [0, 1, 2, 3, 4, 5, 6, 7].map((i) => [p[i * 3], p[i * 3 + 1], p[i * 3 + 2]]);
const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];

function quadArea2(c, idx) {        // twice the signed area of a quad projected on xy
  let a = 0;
  for (let i = 0; i < 4; i++) {
    const p = c[idx[i]], q = c[idx[(i + 1) % 4]];
    a += p[0] * q[1] - q[0] * p[1];
  }
  return a;
}

export function hexVolume(c) {      // divergence theorem over the 6 (triangulated) faces
  let v = 0;
  for (const f of HEX_FACES) {
    const [a, b, cc, d] = f.map((i) => c[i]);
    v += dot(a, cross(b, cc)) + dot(a, cross(cc, d));
  }
  return v / 6;
}

/** Returns '' when the hexahedron is valid, else the reason. */
export function hexProblem(p) {
  if (!Array.isArray(p) || p.length !== 24) return 'not 24 numbers';
  if (!p.every(Number.isFinite)) return 'non-finite coordinate';
  const c = corners(p);
  if (!(quadArea2(c, [0, 1, 2, 3]) > 1e-9)) return 'bottom quad not counter-clockwise';
  if (!(quadArea2(c, [4, 5, 6, 7]) > 1e-9)) return 'top quad not counter-clockwise';
  for (const [i, j] of EDGES) if (Math.hypot(...sub(c[i], c[j])) < 1e-3 - 1e-9) return `edge ${i}-${j} shorter than 1 mm`;
  for (let i = 0; i < 4; i++) if (!(c[i + 4][2] > c[i][2])) return `top corner ${i + 4} not above bottom corner ${i}`;
  if (!(hexVolume(c) > 1e-9)) return 'zero or negative volume';
  return '';
}

export function faceNormal(pts) {    // Newell's method (not normalised)
  const n = [0, 0, 0];
  for (let i = 0; i < pts.length; i++) {
    const a = pts[i], b = pts[(i + 1) % pts.length];
    n[0] += (a[1] - b[1]) * (a[2] + b[2]);
    n[1] += (a[2] - b[2]) * (a[0] + b[0]);
    n[2] += (a[0] - b[0]) * (a[1] + b[1]);
  }
  return n;
}

/** Outward face planes of a part: [{n (unit), d, pts}] (cylinders: polygonal prism). */
export function partFaces(part) {
  let polys;
  if (part.p) {
    const c = corners(part.p);
    polys = HEX_FACES.map((f) => f.map((i) => c[i]));
  } else {
    const [cx, cy, z0, z1, r] = part.cyl, seg = Math.max(3, Math.round(part.seg || 12));
    const ring = (z) => Array.from({ length: seg }, (_, k) => {
      const a = (2 * Math.PI * k) / seg;
      return [cx + r * Math.cos(a), cy + r * Math.sin(a), z];
    });
    const lo = ring(z0), hi = ring(z1);
    polys = [lo.slice().reverse(), hi];
    for (let k = 0; k < seg; k++) polys.push([lo[k], lo[(k + 1) % seg], hi[(k + 1) % seg], hi[k]]);
  }
  const out = [];
  for (const pts of polys) {
    const n = faceNormal(pts), len = Math.hypot(...n);
    if (len < 1e-12) continue;
    const u = n.map((v) => v / len);
    out.push({ n: u, d: dot(u, pts[0]), pts });
  }
  return out;
}

export function makeInside(parts) {
  const faces = parts.map(partFaces);
  const boxes = parts.map(aabb);
  /** strictly inside part i (by more than eps)? */
  return (i, pt, eps = 1e-6) => {
    const b = boxes[i];
    if (pt[0] <= b[0] || pt[0] >= b[3] || pt[1] <= b[1] || pt[1] >= b[4] || pt[2] <= b[2] || pt[2] >= b[5]) return false;
    for (const f of faces[i]) if (dot(f.n, pt) - f.d > -eps) return false;
    return true;
  };
}

/** Largest signed distance of pt to the face planes of a convex part (< 0: inside). A lower
 *  bound of the true distance, so "< r" is a conservative "within r" test. */
export function makePlaneDistance(parts) {
  const faces = parts.map(partFaces);
  return (i, pt) => faces[i].reduce((m, f) => Math.max(m, dot(f.n, pt) - f.d), -Infinity);
}

export function aabb(part) {
  if (part.p) {
    const b = [Infinity, Infinity, Infinity, -Infinity, -Infinity, -Infinity];
    for (let i = 0; i < 24; i += 3) {
      for (let k = 0; k < 3; k++) {
        b[k] = Math.min(b[k], part.p[i + k]);
        b[k + 3] = Math.max(b[k + 3], part.p[i + k]);
      }
    }
    return b;
  }
  const [cx, cy, z0, z1, r] = part.cyl;
  return [cx - r, cy - r, z0, cx + r, cy + r, z1];
}

export function distToBox(pt, b) {
  const dx = Math.max(b[0] - pt[0], 0, pt[0] - b[3]);
  const dy = Math.max(b[1] - pt[1], 0, pt[1] - b[4]);
  const dz = Math.max(b[2] - pt[2], 0, pt[2] - b[5]);
  return Math.hypot(dx, dy, dz);
}

// --- 2D convex polygon clipping (Sutherland-Hodgman) for the overlap area --------------
function area2d(poly) {
  let a = 0;
  for (let i = 0; i < poly.length; i++) {
    const p = poly[i], q = poly[(i + 1) % poly.length];
    a += p[0] * q[1] - q[0] * p[1];
  }
  return a / 2;
}
function clip(subject, clipper) {
  let out = subject;
  for (let i = 0; i < clipper.length && out.length; i++) {
    const a = clipper[i], b = clipper[(i + 1) % clipper.length];
    const side = (p) => (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]);
    const inp = out;
    out = [];
    for (let k = 0; k < inp.length; k++) {
      const p = inp[k], q = inp[(k + 1) % inp.length], sp = side(p), sq = side(q);
      if (sp >= 0) out.push(p);
      if ((sp >= 0) !== (sq >= 0)) {
        const t = sp / (sp - sq);
        out.push([p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])]);
      }
    }
  }
  return out;
}

/**
 * Coplanar faces facing the same way, overlapping with a real area, and visible (the point
 * just outside the overlap is not inside another part). Returns [{a, b, sameMaterial, area}].
 */
export function coplanarConflicts(parts, minArea = 1e-4) {
  const inside = makeInside(parts);
  const groups = new Map();
  parts.forEach((part, idx) => {
    for (const f of partFaces(part)) {
      const key = `${f.n.map((v) => Math.round(v * 1e4)).join(',')}|${Math.round(f.d * 1e4)}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push({ idx, f });
    }
  });
  const out = [];
  for (const list of groups.values()) {
    if (list.length < 2) continue;
    const n = list[0].f.n;
    const ax = [0, 1, 2].reduce((m, i) => (Math.abs(n[i]) > Math.abs(n[m]) ? i : m), 0);
    const [u, v] = [0, 1, 2].filter((i) => i !== ax);
    const proj = list.map(({ f }) => {
      let poly = f.pts.map((p) => [p[u], p[v]]);
      if (area2d(poly) < 0) poly = poly.reverse();
      const xs = poly.map((p) => p[0]), ys = poly.map((p) => p[1]);
      return { poly, bb: [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)] };
    });
    for (let i = 0; i < list.length; i++) {
      for (let k = i + 1; k < list.length; k++) {
        const A = list[i], Bf = list[k];
        if (A.idx === Bf.idx) continue;
        const ba = proj[i].bb, bb = proj[k].bb;
        if (ba[2] <= bb[0] + 1e-6 || bb[2] <= ba[0] + 1e-6 || ba[3] <= bb[1] + 1e-6 || bb[3] <= ba[1] + 1e-6) continue;
        const ov = clip(proj[i].poly, proj[k].poly);
        if (ov.length < 3) continue;
        const area = Math.abs(area2d(ov)) / Math.abs(n[ax]);
        if (area < minArea) continue;
        // centroid of the overlap, lifted back onto the plane, then 2 mm outside
        const cu = ov.reduce((s, p) => s + p[0], 0) / ov.length, cv = ov.reduce((s, p) => s + p[1], 0) / ov.length;
        const pt = [0, 0, 0];
        pt[u] = cu; pt[v] = cv;
        pt[ax] = (A.f.d - n[u] * cu - n[v] * cv) / n[ax];
        const probe = [pt[0] + 2e-3 * n[0], pt[1] + 2e-3 * n[1], pt[2] + 2e-3 * n[2]];
        let hidden = false;
        for (let j = 0; j < parts.length && !hidden; j++) {
          if (j !== A.idx && j !== Bf.idx && inside(j, probe)) hidden = true;
        }
        if (!hidden) {
          out.push({ a: parts[A.idx], b: parts[Bf.idx], sameMaterial: parts[A.idx].m === parts[Bf.idx].m, area, at: pt });
        }
      }
    }
  }
  return out;
}

/**
 * Line-of-sight tester. blocked(a, b) is true when the segment a -> b passes through any
 * part accepted by `occludes(part)` (convex clipping against each part's face planes,
 * with a bounding-box prefilter). Glass is usually excluded by the caller.
 */
export function makeRayTester(parts, occludes = () => true) {
  const list = [];
  parts.forEach((part) => {
    if (occludes(part)) list.push({ box: aabb(part), faces: partFaces(part) });
  });
  const hitBox = (o, d, b) => {
    let t0 = 0, t1 = 1;
    for (let k = 0; k < 3; k++) {
      if (Math.abs(d[k]) < 1e-12) {
        if (o[k] < b[k] || o[k] > b[k + 3]) return false;
      } else {
        let ta = (b[k] - o[k]) / d[k], tb = (b[k + 3] - o[k]) / d[k];
        if (ta > tb) [ta, tb] = [tb, ta];
        t0 = Math.max(t0, ta); t1 = Math.min(t1, tb);
        if (t0 > t1) return false;
      }
    }
    return true;
  };
  const hitConvex = (o, d, faces) => {
    let t0 = 0, t1 = 1;
    for (const f of faces) {
      const denom = dot(f.n, d), dist = dot(f.n, o) - f.d;
      if (Math.abs(denom) < 1e-12) {
        if (dist > -1e-9) return false;
      } else {
        const t = -dist / denom;
        if (denom < 0) t0 = Math.max(t0, t); else t1 = Math.min(t1, t);
        if (t0 >= t1 - 1e-9) return false;
      }
    }
    return true;
  };
  return (a, b) => {
    const d = sub(b, a);
    for (const it of list) if (hitBox(a, d, it.box) && hitConvex(a, d, it.faces)) return true;
    return false;
  };
}
