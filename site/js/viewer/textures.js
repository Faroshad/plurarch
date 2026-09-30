// Plurarch procedural textures: small, seamless luminance maps drawn once on a canvas per material
// kind (the material colour supplies the hue; the map multiplies it). No downloads.
// Deterministic (fixed seeds), so every phone draws the same textures.

/** Pixel size per kind at full quality (halved on the lowest quality tier). */
export const TEXTURE_SIZE = {
  wood: 512, stone: 512, concrete: 256, ground: 256, foliage: 256, fabric: 256, plaster: 256, metal: 256, paint: 128,
};

function rng(seed) {
  let s = (seed >>> 0) || 1;
  return () => {
    s ^= s << 13; s >>>= 0;
    s ^= s >>> 17;
    s ^= s << 5; s >>>= 0;
    return s / 4294967296;
  };
}

// Periodic value noise on a px * py lattice; x, y in lattice units (wraps, so the texture tiles).
function noise2(px, py, rand) {
  const g = new Float32Array(px * py);
  for (let i = 0; i < g.length; i++) g[i] = rand();
  return (x, y) => {
    const xi = Math.floor(x); const yi = Math.floor(y);
    const fx = x - xi; const fy = y - yi;
    const x0 = ((xi % px) + px) % px; const y0 = ((yi % py) + py) % py;
    const x1 = (x0 + 1) % px; const y1 = (y0 + 1) % py;
    const sx = fx * fx * (3 - 2 * fx); const sy = fy * fy * (3 - 2 * fy);
    const a = g[y0 * px + x0]; const b = g[y0 * px + x1];
    const c = g[y1 * px + x0]; const d = g[y1 * px + x1];
    return a + (b - a) * sx + (c - a) * sy + (a - b - c + d) * sx * sy;
  };
}

const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);

function paint(size, fn) {
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  const img = ctx.createImageData(size, size);
  const d = img.data;
  const rgb = [1, 1, 1];
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      rgb[0] = rgb[1] = rgb[2] = 1;
      fn(x / size, y / size, x, y, rgb);
      const i = (y * size + x) * 4;
      d[i] = clamp01(rgb[0]) * 255;
      d[i + 1] = clamp01(rgb[1]) * 255;
      d[i + 2] = clamp01(rgb[2]) * 255;
      d[i + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
  return canvas;
}

const set = (rgb, l) => { rgb[0] = rgb[1] = rgb[2] = l; };

function wood(size) {
  const r = rng(11);
  const boards = 6;
  const tone = Array.from({ length: boards }, () => 0.9 + r() * 0.1);
  const streak = noise2(96, 5, r);
  const fine = noise2(192, 9, r);
  const ring = noise2(12, 3, r);
  const seam = Math.max(1, Math.round(size / 256));
  const bw = size / boards;
  return paint(size, (u, v, x, y, rgb) => {
    const b = Math.min(boards - 1, Math.floor(x / bw));
    const g = 0.55 * streak(u * 96, v * 5) + 0.45 * fine(u * 192, v * 9);
    const rr = Math.abs(Math.sin((u * 22 + ring(u * 12, v * 3) * 3.2) * Math.PI));
    let l = tone[b] * (0.84 + 0.12 * g + 0.06 * rr * rr * rr);
    const xb = x - b * bw;
    if (xb < seam || xb >= bw - seam * 0.5) l *= 0.7;
    // warm the grain slightly: the dark streaks are a touch redder
    rgb[0] = l; rgb[1] = l * (0.985 + 0.015 * g); rgb[2] = l * (0.965 + 0.035 * g);
  });
}

function concrete(size) {
  const r = rng(23);
  const blot = noise2(5, 5, r);
  const mid = noise2(24, 24, r);
  const rad = size / 110;
  const holes = [[0.25, 0.25], [0.75, 0.25], [0.25, 0.75], [0.75, 0.75]];
  return paint(size, (u, v, x, y, rgb) => {
    let l = 0.9 + 0.07 * (blot(u * 5, v * 5) - 0.5) + 0.05 * (mid(u * 24, v * 24) - 0.5) + 0.05 * (r() - 0.5);
    if (r() < 0.004) l *= 0.78; // pores
    for (const [hx, hy] of holes) { // formwork tie holes
      const dd = Math.hypot(x - hx * size, y - hy * size);
      if (dd < rad * 1.6) l *= dd < rad ? 0.72 : 0.9;
    }
    if (x < 1 || y < 1) l *= 0.9; // faint panel joint at the tile edge
    set(rgb, l);
  });
}

function stone(size) {
  const r = rng(37);
  const n = 4;
  const tones = Array.from({ length: n * n }, () => 0.86 + r() * 0.14);
  const mott = noise2(16, 16, r);
  const cell = size / n;
  const joint = Math.max(2, Math.round(size / 150));
  return paint(size, (u, v, x, y, rgb) => {
    const cx = Math.floor(x / cell); const cy = Math.floor(y / cell);
    const lx = x - cx * cell; const ly = y - cy * cell;
    let l = tones[cy * n + cx] * (0.93 + 0.07 * mott(u * 16, v * 16)) + 0.04 * (r() - 0.5);
    if (lx < joint || ly < joint) l = 0.6 + 0.05 * r();
    else if (lx < joint + 1 || ly < joint + 1) l *= 0.9;
    set(rgb, l);
  });
}

function ground(size) {
  const r = rng(41);
  const a = noise2(6, 6, r);
  const b = noise2(24, 24, r);
  const c = noise2(64, 64, r);
  return paint(size, (u, v, x, y, rgb) => {
    const g = 0.45 * a(u * 6, v * 6) + 0.35 * b(u * 24, v * 24) + 0.2 * c(u * 64, v * 64);
    const l = 0.88 + 0.12 * g + 0.04 * (r() - 0.5);
    rgb[0] = l * (0.98 + 0.02 * g); rgb[1] = l; rgb[2] = l * (0.96 + 0.04 * g);
  });
}

function foliage(size) {
  const r = rng(53);
  const a = noise2(8, 8, r);
  const b = noise2(32, 32, r);
  return paint(size, (u, v, x, y, rgb) => {
    const g = 0.5 * a(u * 8, v * 8) + 0.5 * b(u * 32, v * 32);
    const l = 0.62 + 0.42 * Math.pow(g, 1.4) + 0.08 * (r() - 0.5);
    set(rgb, l);
  });
}

function fabric(size) {
  const r = rng(61);
  const threads = 32;
  const t = size / threads;
  return paint(size, (u, v, x, y, rgb) => {
    const cx = Math.floor(x / t); const cy = Math.floor(y / t);
    const horiz = ((cx + cy) & 1) === 0;
    const f = horiz ? (y / t - cy) : (x / t - cx);
    const l = (0.8 + 0.2 * Math.sin(f * Math.PI)) * (0.95 + 0.05 * r());
    set(rgb, l);
  });
}

function plaster(size) {
  const r = rng(71);
  const a = noise2(6, 6, r);
  const b = noise2(40, 40, r);
  return paint(size, (u, v, x, y, rgb) => {
    set(rgb, 0.95 + 0.035 * (a(u * 6, v * 6) - 0.5) + 0.025 * (b(u * 40, v * 40) - 0.5) + 0.02 * (r() - 0.5));
  });
}

function metal(size) {
  const r = rng(83);
  const brush = noise2(3, 160, r);
  const half = size / 2;
  return paint(size, (u, v, x, y, rgb) => {
    let l = 0.92 + 0.07 * (brush(u * 3, v * 160) - 0.5) + 0.02 * (r() - 0.5);
    const sx = x % half;
    if (sx < 2) l *= 0.8; else if (sx < 3) l *= 1.06; // standing seam
    set(rgb, l);
  });
}

const PAINTERS = { wood, concrete, stone, ground, foliage, fabric, plaster, metal, paint: plaster };

/** Draw the texture canvas for a kind, or null for kinds without a map (glass, light). */
export function drawTexture(kind, scale = 1) {
  const fn = PAINTERS[kind];
  if (!fn) return null;
  const base = TEXTURE_SIZE[kind] || 256;
  const size = Math.max(64, Math.round(base * scale));
  return fn(size);
}

/** Soft rounded-rectangle shadow (alpha only) for the contact shadow under the building. */
export function drawBlobShadow(size = 256) {
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  const pad = size * 0.2;
  // Draw the rectangle off-canvas and keep only its blurred shadow (ctx.filter is not in Safari).
  ctx.shadowColor = 'rgba(0, 0, 0, 1)';
  ctx.shadowBlur = size * 0.09;
  ctx.shadowOffsetX = size * 4;
  ctx.fillStyle = '#000';
  const w = size - pad * 2;
  const rr = size * 0.06;
  const x = pad - size * 4; const y = pad;
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + w, rr);
  ctx.arcTo(x + w, y + w, x, y + w, rr);
  ctx.arcTo(x, y + w, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
  ctx.fill();
  return canvas;
}
