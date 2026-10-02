// Python <-> JS parity of the Langford plan rule.
//   node tests/model/test_langford_parity.mjs
// Loads site/models/langford/base.json + base.bin from disk, runs planLangford() from
// site/js/model/langford.js for every case in tests/model/langford_plans.json (written by
// design_mcp/langford_plan.py via tests/test_langford_plan.py --write) and compares:
// glazed / solid SE lites, open lanterns, fins (mark, end point, height) and the finish.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { loadLangford, planLangford } from '../../site/js/model/langford.js';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', '..');
const json = JSON.parse(readFileSync(join(root, 'site', 'models', 'langford', 'base.json'), 'utf8'));
const buf = readFileSync(join(root, 'site', 'models', 'langford', 'base.bin'));
const bin = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
const expected = JSON.parse(readFileSync(join(here, 'langford_plans.json'), 'utf8'));

await loadLangford({ json, bin });
let failures = 0;
const fail = (msg) => { failures += 1; console.error('FAIL ' + msg); };
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const sorted = (a) => [...a].sort((x, y) => x - y);

for (const c of expected.cases) {
  const tag = JSON.stringify(c.params);
  const p = planLangford(c.params);
  if (!same(sorted(p.glazed_lites), c.se_glazed)) fail(`${tag}: glazed lites differ (${p.glazed_lites.length} vs ${c.se_glazed.length})`);
  if (!same(sorted(p.solid_lites), c.se_solid)) fail(`${tag}: solid lites differ`);
  if (!same(p.open_lanterns, c.lanterns_open)) fail(`${tag}: open lanterns ${p.open_lanterns} vs ${c.lanterns_open}`);
  if (p.finish !== c.finish) fail(`${tag}: finish ${p.finish} vs ${c.finish}`);
  if (p.fins.length !== c.fins.length) fail(`${tag}: ${p.fins.length} fins vs ${c.fins.length}`);
  for (let i = 0; i < Math.min(p.fins.length, c.fins.length); i++) {
    const a = p.fins[i]; const b = c.fins[i];
    const d = Math.max(Math.abs(a.end[0] - b.end[0]), Math.abs(a.end[1] - b.end[1]),
      Math.abs(a.base[0] - b.start[0]), Math.abs(a.base[1] - b.start[1]), Math.abs(a.height - b.height));
    if (a.mark !== b.mark || d > 1e-3) { fail(`${tag}: fin ${i} ${a.mark} differs by ${d}`); break; }
  }
}
console.log(`${expected.cases.length} cases, ${failures} failure(s)`);
process.exit(failures ? 1 : 0);
