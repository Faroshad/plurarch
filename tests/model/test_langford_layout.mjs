// The phone draws the reviewer's lantern-panel layout exactly (same mask rule as design_mcp/skylights.py).
// node tests/model/test_langford_layout.mjs   (reads the mask and expected counts from tests/model/layout_case.json)
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const L = await import(pathToFileURL(path.join(ROOT, 'site/js/model/langford.js')));
const dir = path.join(ROOT, 'site/models/langford');
const buf = fs.readFileSync(path.join(dir, 'base.bin'));
await L.loadLangford({ json: JSON.parse(fs.readFileSync(path.join(dir, 'base.json'), 'utf8')), bin: buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength) });
const c = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests/model/layout_case.json'), 'utf8'));
const params = { infill_finish: 'concrete', se_glass_share: 90, fin_depth: 0.6, skylights_open: 8, skylight_layout: 'case', skylight_mask: c.mask };
const plan = L.planLangford(params);
const built = L.buildLangford(params);
const lanternOf = new Map(JSON.parse(fs.readFileSync(path.join(dir, 'base.json'), 'utf8')).elements.filter((e) => e.q === 'skylights_open').map((e) => [e.id, e.lantern]));
const perLantern = Array(12).fill(0);
for (const part of built.parts) if (lanternOf.has(part.id) && part.m === 'glass') perLantern[lanternOf.get(part.id)] += 1;
let fail = 0;
const check = (ok, msg) => { if (!ok) { fail += 1; console.log('FAIL', msg); } };
check(plan.sky_glazed.length === 112, `plan glazed ${plan.sky_glazed.length} != 112`);
check(JSON.stringify([...plan.sky_glazed].sort((a, b) => a - b)) === JSON.stringify(c.glazed_ids), 'plan glazed ids differ from Python');
check(JSON.stringify(perLantern) === JSON.stringify(c.per_lantern), `drawn per lantern ${perLantern} != ${c.per_lantern}`);
const plain = L.buildLangford({ ...params, skylight_mask: undefined, skylight_layout: undefined });
const plainPer = Array(12).fill(0);
for (const part of plain.parts) if (lanternOf.has(part.id) && part.m === 'glass') plainPer[lanternOf.get(part.id)] += 1;
check(JSON.stringify(plainPer) === JSON.stringify([14, 14, 14, 14, 14, 14, 14, 14, 0, 0, 0, 0]), `plain rule ${plainPer}`);
console.log(`layout drawn per lantern: ${perLantern.join(', ')}`);
console.log(fail ? `${fail} failure(s)` : 'RESULT: layout drawn exactly (4/4 checks)');
process.exit(fail ? 1 : 0);
