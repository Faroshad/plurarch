// Parity check: the JavaScript plan (site/js/model/langford.js) must equal the Python plan
// (design_mcp/langford_plan.py) for every parameter set.
//   node tests/model/test_langford.mjs
// Reference: tests/model/langford_plans.json when it exists ({cases: [{params, plan}]} or a list of
// {params, plan}); otherwise the Python plan() is called directly through the repo venv.
// Also checks the Model: material re-assignment, fin boxes, pins per question, rebuild time.
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const L = await import(pathToFileURL(path.join(ROOT, 'site/js/model/langford.js')));
const dir = path.join(ROOT, 'site/models/langford');
const buf = fs.readFileSync(path.join(dir, 'base.bin'));
await L.loadLangford({ json: JSON.parse(fs.readFileSync(path.join(dir, 'base.json'), 'utf8')), bin: buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength) });

let pass = 0; let fail = 0;
const check = (name, ok, detail = '') => { if (ok) pass++; else { fail++; console.log('FAIL', name, detail); } };
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const sorted = (a) => [...a].map(Number).sort((x, y) => x - y);

// reference plans
const grid = [];
for (const share of [40, 50, 60, 70, 80, 90, 100]) {
  for (const sky of [0, 2, 4, 6, 8, 10, 12]) {
    for (const fin of [0, 0.3, 0.6, 0.9, 1.2]) {
      grid.push({ infill_finish: ['concrete', 'aluminium', 'fritted_glass'][(share + sky) % 3], se_glass_share: share, fin_depth: fin, skylights_open: sky });
    }
  }
}
let cases = null; let source = '';
const plansFile = path.join(ROOT, 'tests/model/langford_plans.json');
if (fs.existsSync(plansFile)) {
  const raw = JSON.parse(fs.readFileSync(plansFile, 'utf8'));
  cases = (Array.isArray(raw) ? raw : raw.cases || []).map((c) => ({ params: c.params, plan: c.plan || c.expected || c }));
  source = 'tests/model/langford_plans.json';
} else {
  const py = path.join(ROOT, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  const code = 'import json, sys\nsys.path.insert(0, ".")\nfrom design_mcp.langford_plan import plan\n'
    + 'cases = json.loads(sys.stdin.read())\nprint(json.dumps([{"params": c, "plan": plan(c)} for c in cases]))';
  try {
    cases = JSON.parse(execFileSync(fs.existsSync(py) ? py : 'python', ['-c', code], { cwd: ROOT, input: JSON.stringify(grid), maxBuffer: 1 << 28 }).toString());
    source = 'design_mcp/langford_plan.py (live)';
  } catch (e) {
    console.log('SKIP plan parity: no tests/model/langford_plans.json and the Python plan could not run:', e.message.split('\n')[0]);
  }
}

if (cases) {
  for (const c of cases) {
    const js = L.planLangford(c.params);
    const py = c.plan;
    const tag = JSON.stringify(c.params);
    if ('se_glazed' in py) check('se_glazed ' + tag, same(sorted(js.se_glazed), sorted(py.se_glazed)));
    if ('se_solid' in py) check('se_solid ' + tag, same(sorted(js.se_solid), sorted(py.se_solid)));
    if ('se_glazed_count' in py) check('se_glazed_count ' + tag, js.se_glazed_count === py.se_glazed_count, `${js.se_glazed_count} vs ${py.se_glazed_count}`);
    if ('lanterns_open' in py) check('lanterns_open ' + tag, same(js.lanterns_open, py.lanterns_open));
    if ('sky_glazed' in py) check('sky_glazed ' + tag, same(sorted(js.sky_glazed), sorted(py.sky_glazed)));
    if ('sky_solid' in py) check('sky_solid ' + tag, same(sorted(js.sky_solid), sorted(py.sky_solid)));
    if ('finish' in py) check('finish ' + tag, js.finish === py.finish);
    if ('fins' in py) {
      // compare the fields the reference records (mark, start, end, height; z and length when present)
      const fields = py.fins.length ? Object.keys(py.fins[0]).filter((k) => k in js.fins[0] || !js.fins.length) : [];
      const key = (f) => fields.map((k) => (typeof f[k] === 'number' ? Number(f[k]).toFixed(4) : JSON.stringify(f[k])));
      check('fins ' + tag, js.fins.length === py.fins.length && same(js.fins.map(key), py.fins.map(key)), `${js.fins.length} vs ${py.fins.length}`);
    }
  }
  console.log(`plan parity against ${source}: ${cases.length} parameter sets`);
}

// The Model follows the plan: materials, fins, pins, and a fast rebuild.
for (const p of [L.DEFAULTS, { infill_finish: 'aluminium', se_glass_share: 50, fin_depth: 1.2, skylights_open: 4 }, { infill_finish: 'fritted_glass', se_glass_share: 40, fin_depth: 0.3, skylights_open: 0 }]) {
  const t0 = performance.now();
  const m = L.buildLangford(p);
  const ms = performance.now() - t0;
  const plan = L.planLangford(p);
  const byId = new Map(m.parts.filter((x) => x.id != null).map((x) => [x.id, x]));
  const infill = 'infill_' + plan.finish;
  check('glazed lites are glass ' + JSON.stringify(p), plan.se_glazed.every((id) => byId.get(id).m === 'glass'));
  check('solid lites use the finish ' + JSON.stringify(p), plan.se_solid.every((id) => byId.get(id).m === infill));
  check('lantern glazing follows the plan ' + JSON.stringify(p), plan.sky_glazed.every((id) => byId.get(id).m === 'glass') && plan.sky_solid.every((id) => byId.get(id).m === infill));
  check('one fin box per anchor ' + JSON.stringify(p), m.parts.filter((x) => x.t === 'fins:fin').length === plan.fins.length);
  check('a material for the finish ' + JSON.stringify(p), !!m.materials[infill]);
  for (const q of Object.keys(L.QUESTION_TAGS)) check(`exterior pin for ${q}`, m.pins.some((x) => x.question === q && x.mode === 'exterior'));
  check('rebuild under 30 ms ' + JSON.stringify(p), ms < 30, ms.toFixed(1) + ' ms');
}
const m = L.buildLangford({});
check('tour has 5-7 stops', m.tour.length >= 5 && m.tour.length <= 7, String(m.tour.length));
check('walkable excludes the building', Array.isArray(m.walkable.obstacles) && m.walkable.obstacles.length >= 1);

console.log(`\nRESULT: ${pass}/${pass + fail} checks passed`);
process.exit(fail ? 1 : 0);
