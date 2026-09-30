#!/usr/bin/env node
// Print the pavilion Model (docs/MODEL.md) as compact JSON, for Rhino and tests.
//
//   node tools/pavilion_cli.mjs '{"facade_material":"glass","window_ratio":45,"roof_angle":20,"canopy_depth":2}'
//   echo {...} | node tools/pavilion_cli.mjs          (JSON on stdin; also accepts {"parameters": {...}})
//
// Coordinates are rounded to 1 mm. Exit code 0; 1 with a message on stderr on failure.

import { buildPavilion } from '../site/js/model/pavilion.js';

const r3 = (v) => Math.round(v * 1000) / 1000;
const r4 = (v) => Math.round(v * 10000) / 10000;

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (c) => { data += c; });
    process.stdin.on('end', () => resolve(data));
    process.stdin.on('error', reject);
  });
}

function compact(model) {
  return {
    ...model,
    parts: model.parts.map((q) => {
      if (q.p) return { m: q.m, t: q.t, p: q.p.map(r3) };
      const [cx, cy, z0, z1, r] = q.cyl;
      return { m: q.m, t: q.t, cyl: [r3(cx), r3(cy), r3(z0), r3(z1), r4(r)], seg: q.seg };
    }),
  };
}

async function main() {
  const arg = process.argv.slice(2).find((a) => !a.startsWith('--'));
  let text = arg !== undefined && arg !== '-' ? arg : (process.stdin.isTTY ? '{}' : await readStdin());
  text = String(text).replace(/^﻿/, '').trim() || '{}';
  let data;
  try {
    data = JSON.parse(text);
  } catch (e) {
    throw new Error(`parameters are not valid JSON (${e.message})`);
  }
  if (data && typeof data === 'object' && data.parameters && typeof data.parameters === 'object') data = data.parameters;
  const model = buildPavilion(data);
  if (model.error) throw new Error(`generator fell back to a minimal model: ${model.error}`);
  process.stdout.write(JSON.stringify(compact(model)) + '\n');
}

process.stdout.on('error', (e) => { if (e && e.code === 'EPIPE') process.exit(0); });

main().then(
  () => { process.exitCode = 0; },
  (err) => {
    process.stderr.write(`pavilion_cli: ${err && err.message ? err.message : err}\n`);
    process.exitCode = 1;
  },
);
