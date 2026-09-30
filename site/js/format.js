// Plurarch formatting helpers: thin-space thousands, 24-hour times, sentence case, middle dots.

export const THIN_SPACE = ' ';
export const NBSP = ' ';
export const MINUS = '−';
export const MIDDLE_DOT = '·';
export const SEP = ' · ';

export function isNum(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

/** Number from a number or numeric string; NaN otherwise. */
export function toNum(v) {
  if (isNum(v)) return v;
  if (typeof v === 'string' && v.trim() !== '') {
    const n = Number(v.trim());
    return Number.isFinite(n) ? n : NaN;
  }
  return NaN;
}

function groupDigits(intStr) {
  return intStr.length >= 4 ? intStr.replace(/\B(?=(\d{3})+(?!\d))/g, THIN_SPACE) : intStr;
}

/** Integer with thin-space thousands separators: 10140 -> "10 140". */
export function fmtInt(v, fallback = '–') {
  const n = toNum(v);
  if (!Number.isFinite(n)) return fallback;
  const r = Math.round(n);
  return (r < 0 ? MINUS : '') + groupDigits(String(Math.abs(r)));
}

/** Number with up to `maxDecimals` decimals (trailing zeros dropped), thin-space thousands. */
export function fmtNum(v, maxDecimals = 1, fallback = '–') {
  const n = toNum(v);
  if (!Number.isFinite(n)) return fallback;
  const f = 10 ** maxDecimals;
  let r = Math.round(n * f) / f;
  if (Object.is(r, -0)) r = 0;
  const parts = Math.abs(r).toFixed(maxDecimals).split('.');
  const dec = parts[1] ? parts[1].replace(/0+$/, '') : '';
  return (r < 0 ? MINUS : '') + groupDigits(parts[0]) + (dec ? '.' + dec : '');
}

/** Signed delta: "+3.2", "−1", "±0". */
export function fmtSigned(v, maxDecimals = 1, fallback = '–') {
  const n = toNum(v);
  if (!Number.isFinite(n)) return fallback;
  const f = 10 ** maxDecimals;
  const r = Math.round(n * f) / f;
  if (r === 0) return '±0';
  return (r > 0 ? '+' : '') + fmtNum(r, maxDecimals);
}

/** Percentage from a 0–100 value: 56.4 -> "56%". */
export function fmtPct(v, maxDecimals = 0, fallback = '–') {
  const n = toNum(v);
  if (!Number.isFinite(n)) return fallback;
  return fmtNum(n, maxDecimals) + '%';
}

/**
 * Parse a timestamp into epoch milliseconds (NaN if unusable).
 * Accepts ISO 8601 with or without zone (no zone = UTC, as the backend stores UTC),
 * a space instead of "T", microseconds (trimmed for Safari), "+0000" and "+00" offsets,
 * or epoch seconds / milliseconds.
 */
export function parseTime(v) {
  if (v == null || v === '') return NaN;
  if (typeof v === 'number') return Number.isFinite(v) ? (v < 1e11 ? v * 1000 : v) : NaN;
  if (v instanceof Date) return v.getTime();
  let s = String(v).trim();
  if (/^\d+(\.\d+)?$/.test(s)) return parseTime(Number(s));
  s = s.replace(/^(\d{4}-\d{2}-\d{2})[ T]/, '$1T');
  s = s.replace(/(\.\d{3})\d+/, '$1');
  if (/T\d{2}:\d{2}(:\d{2}(\.\d+)?)?[+-]\d{4}$/.test(s)) s = s.replace(/([+-]\d{2})(\d{2})$/, '$1:$2');
  else if (/T\d{2}:\d{2}(:\d{2}(\.\d+)?)?[+-]\d{2}$/.test(s)) s += ':00';
  else if (/T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(s)) s += 'Z';
  const t = Date.parse(s);
  return Number.isFinite(t) ? t : NaN;
}

const pad2 = (n) => String(n).padStart(2, '0');

/** 24-hour "HH:MM" in local time. */
export function fmtTime(v, fallback = '–') {
  const t = parseTime(v);
  if (!Number.isFinite(t)) return fallback;
  const d = new Date(t);
  return pad2(d.getHours()) + ':' + pad2(d.getMinutes());
}

/** 24-hour "HH:MM:SS" in local time. */
export function fmtTimeS(v, fallback = '––:––:––') {
  const t = parseTime(v);
  if (!Number.isFinite(t)) return fallback;
  const d = new Date(t);
  return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
}

/** Seconds as "m:ss" (e.g. 42 -> "0:42", 75 -> "1:15"). */
export function fmtClock(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  return Math.floor(s / 60) + ':' + pad2(s % 60);
}

/** Duration in seconds as "42 s" or "1 min 5 s". */
export function fmtDuration(sec) {
  const s = Math.round(toNum(sec));
  if (!Number.isFinite(s) || s < 0) return '';
  if (s < 60) return s + NBSP + 's';
  const m = Math.floor(s / 60);
  const r = s % 60;
  return m + NBSP + 'min' + (r ? ' ' + r + NBSP + 's' : '');
}

/** Sentence case: first letter upper case, underscores to spaces, rest untouched. */
export function sentenceCase(s) {
  const str = String(s == null ? '' : s).replace(/_/g, ' ').trim();
  if (!str) return '';
  return str.charAt(0).toUpperCase() + str.slice(1);
}

/** Join non-empty parts with " · ". */
export function joinDot(parts) {
  return (parts || []).filter((p) => p != null && p !== '').join(SEP);
}

/** Attach a unit: "%" and "°" hug the number, other units get a no-break space. */
export function withUnit(numStr, unit) {
  if (!unit) return numStr;
  const u = String(unit);
  if (u === '%' || u === '°' || u === '°') return numStr + u;
  return numStr + NBSP + u;
}

/** Decimal places implied by a slider step (5 -> 0, 0.5 -> 1, 0.25 -> 2). */
export function stepDecimals(step) {
  const n = toNum(step);
  if (!Number.isFinite(n) || n <= 0) return 1;
  const s = String(n);
  if (s.includes('e-')) return Math.min(6, Number(s.split('e-')[1]) || 1);
  const i = s.indexOf('.');
  return i < 0 ? 0 : Math.min(6, s.length - i - 1);
}

/**
 * Format a parameter value for display using its config entry
 * ({type, options:[{value,label}], unit, step}). Unknown values fall back to plain text.
 */
export function fmtParamValue(param, value, fallback = '–') {
  if (value == null || value === '') return fallback;
  if (!param) return String(value);
  if (param.type === 'choice') {
    const opt = (param.options || []).find((o) => String(o.value) === String(value));
    return opt ? String(opt.label || opt.value) : sentenceCase(String(value));
  }
  const n = toNum(value);
  if (!Number.isFinite(n)) return String(value);
  return withUnit(fmtNum(n, Math.max(stepDecimals(param.step), 0)), param.unit);
}

/** Number part only (no unit) of a slider value, for big displays. */
export function fmtSliderNumber(param, value) {
  const n = toNum(value);
  if (!Number.isFinite(n)) return '–';
  return fmtNum(n, stepDecimals(param && param.step));
}

/** Are two parameter values the same (numeric compare for sliders)? */
export function sameValue(param, a, b) {
  if (a == null && b == null) return true;
  if (a == null || b == null) return false;
  if (param && param.type === 'slider') {
    const x = toNum(a);
    const y = toNum(b);
    if (Number.isFinite(x) && Number.isFinite(y)) return Math.abs(x - y) < 1e-9;
  }
  return String(a) === String(b);
}

/** Plural helper: plural(1, 'vote') -> "1 vote", plural(3, 'vote') -> "3 votes". */
export function plural(n, word, pluralWord) {
  const num = toNum(n);
  const w = num === 1 ? word : (pluralWord || word + 's');
  return fmtInt(num) + NBSP + w;
}
