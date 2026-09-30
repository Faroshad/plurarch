// Plurarch charts: vote heatmap, rounded exploded pie (d3-shape), slider distribution strip.
// Each renderer keeps its DOM between updates when the structure is unchanged, so values
// animate (cells fade in, bars grow) instead of being rebuilt.

import { el, svgEl, clear, icon, showTip, hideTip } from './ui.js';
import { fmtInt, fmtPct } from './format.js';

/** Accent ramp step 1..6: 1 = zero (palest), 2..6 scale with count / max. */
export function accentLevel(count, max) {
  if (!count || count <= 0 || !max) return 1;
  return Math.min(6, Math.max(2, 1 + Math.ceil((count / max) * 5)));
}

/* ------------------------------------------------------------------ heatmap */
/**
 * renderHeatmap(host, model)
 * model = {
 *   key,                               // structure signature (rows/cols); rebuild when it changes
 *   rows: [{ label }],                 // top to bottom
 *   cols: [{ label, range }],          // left to right; label may be '' to thin the axis
 *   cells: [[{ count, future }]],      // rows x cols
 *   max, ariaLabel, overlay            // overlay: optional centred message
 * }
 * Elapsed cells use the accent ramp; cells that have not happened yet use the neutral ramp.
 */
export function renderHeatmap(host, model) {
  let st = host._hm;
  if (!st || st.key !== model.key) {
    clear(host);
    const nCols = Math.max(1, model.cols.length);
    const grid = el('div', { class: 'hm-grid', role: 'img', 'aria-label': model.ariaLabel || 'Vote heatmap' });
    grid.style.gridTemplateColumns = `repeat(${nCols}, minmax(0, 1fr)) max-content`;
    // Taller cells when there are few rows (choice questions), compact when there are many.
    const cellH = Math.round(Math.max(22, Math.min(44, 176 / Math.max(1, model.rows.length))));
    grid.style.setProperty('--cell-h', `${cellH}px`);
    const cells = [];
    model.rows.forEach((row, r) => {
      cells[r] = [];
      for (let c = 0; c < nCols; c++) {
        const cell = el('div', { class: 'hm-cell', dataset: { r, c } });
        grid.appendChild(cell);
        cells[r][c] = cell;
      }
      grid.appendChild(el('div', { class: 'hm-row-label', text: row.label }));
    });
    for (let c = 0; c < nCols; c++) {
      grid.appendChild(el('div', { class: 'hm-col-label', text: (model.cols[c] && model.cols[c].label) || '' }));
    }
    grid.appendChild(el('div'));
    const overlay = el('div', { class: 'hm-overlay', hidden: true }, el('span'));
    host.appendChild(grid);
    host.appendChild(overlay);

    const onMove = (e) => {
      const target = e.target && e.target.closest ? e.target.closest('.hm-cell') : null;
      const m = host._hm && host._hm.model;
      if (!target || !m) { hideTip(); return; }
      const r = Number(target.dataset.r);
      const c = Number(target.dataset.c);
      const cell = m.cells[r] && m.cells[r][c];
      if (!cell) { hideTip(); return; }
      const rowLabel = (m.rows[r] && m.rows[r].label) || '';
      const range = (m.cols[c] && m.cols[c].range) || '';
      if (cell.future) showTip(e.clientX, e.clientY, 'Not yet', [rowLabel, range].filter(Boolean).join(' · '));
      else showTip(e.clientX, e.clientY, `${fmtInt(cell.count)} ${cell.count === 1 ? 'vote' : 'votes'}`, [rowLabel, range].filter(Boolean).join(' · '));
    };
    grid.addEventListener('pointermove', onMove);
    grid.addEventListener('pointerdown', onMove);
    grid.addEventListener('pointerleave', hideTip);
    st = host._hm = { key: model.key, cells, overlay, prev: [] };
  }
  st.model = model;

  for (let r = 0; r < st.cells.length; r++) {
    for (let c = 0; c < st.cells[r].length; c++) {
      const node = st.cells[r][c];
      const cell = (model.cells[r] && model.cells[r][c]) || { count: 0, future: true };
      const tone = cell.future ? 'neutral-1' : `accent-${accentLevel(cell.count, model.max)}`;
      if (node._tone !== tone) {
        node.style.backgroundColor = `var(--${tone})`;
        const becamePast = node._future === true && !cell.future;
        const grew = node._count != null && cell.count > node._count;
        if (becamePast || grew) {
          node.classList.remove('pop');
          void node.offsetWidth; // restart the animation
          node.classList.add('pop');
        }
        node._tone = tone;
      }
      node._future = !!cell.future;
      node._count = cell.count;
    }
  }
  if (model.overlay) {
    st.overlay.firstChild.textContent = model.overlay;
    st.overlay.hidden = false;
  } else {
    st.overlay.hidden = true;
  }
}

/* ------------------------------------------------------------------ pie */
const PIE_TONES = ['--pie-1', '--pie-2', '--pie-3', '--pie-4', '--pie-5', '--pie-6'];
function toneFor(rank) { return `var(${PIE_TONES[Math.min(rank, PIE_TONES.length - 1)]})`; }
function textOn(rank) { return rank < 2 ? '#FFFFFF' : 'var(--text)'; }

/** Rank items (count desc) and attach share (0–100) and tone. */
export function rankItems(items) {
  const total = items.reduce((s, i) => s + (i.count > 0 ? i.count : 0), 0);
  return items
    .map((i, idx) => ({ ...i, idx, pct: total ? (Math.max(0, i.count) / total) * 100 : 0 }))
    .sort((a, b) => (b.count - a.count) || (a.idx - b.idx))
    .map((i, rank) => ({ ...i, rank, tone: toneFor(rank) }));
}

/**
 * Rounded "exploded" pie: segments with gaps and generous corner radius, grayscale by rank
 * (largest = ink), bold percentages inside (white on dark, dark on light).
 * items: [{ label, icon, count }]. Falls back to a stacked bar if d3-shape is unavailable.
 */
export function renderPie(host, items, { size = 240, animate = true } = {}) {
  clear(host);
  const ranked = rankItems(items).filter((i) => i.count > 0);
  const total = ranked.reduce((s, i) => s + i.count, 0);
  if (!total) {
    host.appendChild(el('p', { class: 'empty', text: 'No votes for this question' }));
    return;
  }
  const d3 = window.d3;
  if (!(d3 && typeof d3.arc === 'function' && typeof d3.pie === 'function')) {
    const bar = el('div', { class: 'pie-fallback', role: 'img', 'aria-label': ranked.map((i) => `${i.label} ${fmtPct(i.pct)}`).join(', ') });
    for (const i of ranked) {
      bar.appendChild(el('span', { style: { flex: `${i.count} 1 0`, background: i.tone, color: textOn(i.rank) }, text: i.pct >= 8 ? fmtPct(i.pct) : '' }));
    }
    host.appendChild(bar);
    return;
  }
  const r = size / 2;
  const n = ranked.length;
  const pad = n > 1 ? 0.05 : 0;
  const pieGen = d3.pie().sort(null).value((d) => d.count).padAngle(pad);
  const arcs = pieGen(ranked);
  const outer = r - 10;
  const arcGen = d3.arc().innerRadius(0).outerRadius(outer).cornerRadius(n > 1 ? 14 : 0).padRadius(outer);
  const labelArc = d3.arc().innerRadius(outer * 0.42).outerRadius(outer * 0.92);
  const svg = svgEl('svg', {
    viewBox: `${-r} ${-r} ${size} ${size}`, role: 'img', class: animate ? 'pie-anim' : null,
    'aria-label': 'Vote share: ' + ranked.map((i) => `${i.label} ${fmtPct(i.pct)}`).join(', '),
  });
  arcs.forEach((a) => {
    const item = a.data;
    const mid = (a.startAngle + a.endAngle) / 2;
    const off = n > 1 ? 5 : 0; // explode each segment outward a little
    const g = svgEl('g', { transform: `translate(${(Math.sin(mid) * off).toFixed(2)},${(-Math.cos(mid) * off).toFixed(2)})` });
    const path = svgEl('path', { d: arcGen(a) || '' });
    path.style.fill = item.tone;
    g.appendChild(path);
    const sweep = a.endAngle - a.startAngle;
    if (sweep > 0.32) {
      const [x, y] = n > 1 ? labelArc.centroid(a) : [0, 0];
      const t = svgEl('text', { x: x.toFixed(1), y: y.toFixed(1), 'text-anchor': 'middle', 'dominant-baseline': 'central' }, fmtPct(item.pct));
      t.style.fill = textOn(item.rank);
      g.appendChild(t);
    }
    svg.appendChild(g);
  });
  host.appendChild(svg);
}

/** Legend list: outline icon + "Label · 56%" (with a small tone swatch), ranked. */
export function pieLegend(items) {
  const ranked = rankItems(items);
  const ul = el('ul', { class: 'legend' });
  for (const i of ranked) {
    ul.appendChild(el('li', null,
      el('span', { class: 'sw', style: { background: i.count > 0 ? i.tone : 'var(--neutral-1)' }, 'aria-hidden': 'true' }),
      icon(i.icon || 'circle', 18),
      el('span', null, `${i.label} · `, el('b', { text: fmtPct(i.pct) })),
      el('span', { class: 't-secondary', text: ` (${fmtInt(i.count)})` }),
    ));
  }
  return ul;
}

/* ------------------------------------------------------------------ strip */
/**
 * Slider distribution strip: rounded bars per step value (accent ramp by count), the median
 * marked with an ink line and a bold value label.
 * model = { key, bins: [{ label, count }], max, medianPos (0..1 or null), medianLabel }
 */
export function renderStrip(host, model) {
  let st = host._strip;
  if (!st || st.key !== model.key) {
    clear(host);
    const plot = el('div', { class: 'strip-plot' });
    const bars = model.bins.map(() => {
      const bar = el('div', { class: 'strip-bar' });
      plot.appendChild(el('div', { class: 'strip-bin' }, bar));
      return bar;
    });
    const median = el('div', { class: 'strip-median', hidden: true });
    const medianLabel = el('div', { class: 'strip-median-label', hidden: true });
    plot.appendChild(median);
    plot.appendChild(medianLabel);
    const axis = el('div', { class: 'strip-axis', 'aria-hidden': 'true' }, model.bins.map((b) => el('span', { text: b.label })));
    const wrap = el('div', { class: 'strip' }, plot, axis);
    host.appendChild(wrap);
    const onMove = (e) => {
      const bin = e.target && e.target.closest ? e.target.closest('.strip-bin') : null;
      const m = host._strip && host._strip.model;
      if (!bin || !m) { hideTip(); return; }
      const i = Array.prototype.indexOf.call(plot.querySelectorAll('.strip-bin'), bin);
      const b = m.bins[i];
      if (!b) { hideTip(); return; }
      showTip(e.clientX, e.clientY, `${fmtInt(b.count)} ${b.count === 1 ? 'vote' : 'votes'}`, b.label);
    };
    plot.addEventListener('pointermove', onMove);
    plot.addEventListener('pointerdown', onMove);
    plot.addEventListener('pointerleave', hideTip);
    st = host._strip = { key: model.key, bars, median, medianLabel, plot };
  }
  st.model = model;
  const max = model.max || 0;
  model.bins.forEach((b, i) => {
    const bar = st.bars[i];
    if (!bar) return;
    if (b.count > 0 && max > 0) {
      bar.style.height = `${Math.max(10, (b.count / max) * 100).toFixed(1)}%`;
      bar.style.backgroundColor = `var(--accent-${accentLevel(b.count, max)})`;
    } else {
      bar.style.height = '4px';
      bar.style.backgroundColor = 'var(--neutral-1)';
    }
  });
  st.plot.setAttribute('aria-label', `Distribution: ${model.bins.map((b) => `${b.label} ${b.count}`).join(', ')}${model.medianLabel ? `; median ${model.medianLabel}` : ''}`);
  st.plot.setAttribute('role', 'img');
  if (model.medianPos != null && Number.isFinite(model.medianPos)) {
    const left = `${(model.medianPos * 100).toFixed(2)}%`;
    st.median.style.left = left;
    st.medianLabel.style.left = left;
    st.median.hidden = false;
    st.medianLabel.hidden = false;
    clear(st.medianLabel).append('Median ', el('b', { text: model.medianLabel || '' }));
    // keep the label inside the plot near the right edge
    st.medianLabel.style.transform = model.medianPos > 0.72 ? 'translate(calc(-100% - 8px), -100%)' : 'translate(8px, -100%)';
  } else {
    st.median.hidden = true;
    st.medianLabel.hidden = true;
  }
}
