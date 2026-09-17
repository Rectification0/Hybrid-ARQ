/* DOM and formatting helpers shared by every view (T11.2, T11.16).
 *
 * Two rules live here because they must hold everywhere:
 *
 *  - A value that was not recorded renders as an explicit dash with the reason,
 *    never as zero. Zero is a measurement; "not recorded" is not (T11.3, rule 3).
 *  - A mode always renders through `modeChip`, so GBN, SR, Hybrid and the
 *    fixed-hybrid control look the same on every screen they appear on.
 */

export const MODES = {
  gbn: { label: 'GBN', color: 'var(--gbn)', blurb: 'Go-Back-N — cumulative ACK, resends the whole outstanding range' },
  sr: { label: 'SR', color: 'var(--sr)', blurb: 'Selective Repeat — per-segment ACK, resends only what was lost' },
  hybrid: { label: 'Hybrid', color: 'var(--hybrid)', blurb: 'Switches between GBN and SR at runtime on the D8 estimator' },
  'fixed-hybrid': { label: 'Fixed-hybrid', color: 'var(--fixed-hybrid)', blurb: 'Control: pays the drain cost of a handshake without changing mode' },
  saw: { label: 'Stop-and-wait', color: 'var(--saw)', blurb: 'Phase 2 placeholder — not a system under evaluation' },
};

export const modeColor = (mode) => (MODES[String(mode || '').toLowerCase()] || {}).color || 'var(--text-faint)';

/* ------------------------------------------------------------- elements */

export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key in node && key !== 'list' && key !== 'form') {
      node[key] = value;
    } else {
      node.setAttribute(key, value);
    }
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const child of children.flat(4)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export const svg = (tag, props = {}, ...children) => {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    node.setAttribute(key, value);
  }
  append(node, children);
  return node;
};

export const clear = (node) => { while (node.firstChild) node.firstChild.remove(); return node; };

/* ------------------------------------------------------------ formatting */

export const MISSING = '—';

/** A number, or an explicit dash. Never a zero standing in for "not recorded". */
export function num(value, digits = 2, unit = '') {
  if (value === null || value === undefined || value === '' || Number.isNaN(Number(value))) {
    return MISSING;
  }
  const n = Number(value);
  const text = Number.isInteger(n) && digits === 0 ? n.toLocaleString()
    : n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return unit ? `${text} ${unit}` : text;
}

export function integer(value) {
  if (value === null || value === undefined || value === '') return MISSING;
  const n = Number(value);
  return Number.isFinite(n) ? Math.round(n).toLocaleString() : MISSING;
}

export function bytes(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return MISSING;
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KiB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(2)} MiB`;
  return `${(n / 1024 ** 3).toFixed(2)} GiB`;
}

export function rate(bytesPerSecond) {
  const n = Number(bytesPerSecond);
  return Number.isFinite(n) ? `${(n / 1024).toFixed(1)} KiB/s` : MISSING;
}

export function seconds(value, digits = 3) {
  const n = Number(value);
  return Number.isFinite(n) ? `${n.toFixed(digits)} s` : MISSING;
}

export function percent(fraction, digits = 1) {
  const n = Number(fraction);
  return Number.isFinite(n) ? `${(n * 100).toFixed(digits)}%` : MISSING;
}

export function clock(wallSeconds) {
  const n = Number(wallSeconds);
  if (!Number.isFinite(n)) return MISSING;
  return new Date(n * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'medium' });
}

export const shortHash = (hash) => (hash ? `${String(hash).slice(0, 12)}…${String(hash).slice(-8)}` : MISSING);

/* ------------------------------------------------------------ components */

export function modeChip(mode, { large = false } = {}) {
  const key = String(mode || '').toLowerCase();
  const known = MODES[key];
  return el('span', {
    class: `mode-chip mode-${known ? key : 'none'}${large ? ' large' : ''}`,
    title: known ? known.blurb : 'No mode recorded for this row',
  }, known ? known.label : 'none');
}

export function badge(text, kind = 'neutral') {
  return el('span', { class: `badge ${kind}` }, text);
}

/** Integrity, stated the one way CC-01 allows: never softened, never assumed. */
export function integrityBadge(success) {
  if (success === true) return badge('hash match', 'ok');
  if (success === false) return badge('HASH MISMATCH', 'bad');
  return badge('no verdict recorded', 'neutral');
}

export function stat(label, value, { sub = '', tone = '', unit = '', small = false } = {}) {
  return el('div', { class: 'stat' },
    el('span', { class: 'stat-label' }, label),
    el('span', { class: `stat-value${tone ? ` ${tone}` : ''}${small ? ' small' : ''}` },
      value instanceof Node ? value : String(value),
      unit ? el('span', { class: 'stat-unit' }, unit) : null),
    sub ? el('span', { class: 'stat-sub' }, sub) : null);
}

export function card(title, { note = '', className = '', caveat = '' } = {}, ...children) {
  return el('section', { class: `card ${className}`.trim() },
    title ? el('header', {}, el('h3', {}, title), note ? el('span', { class: 'card-note' }, note) : null) : null,
    ...children,
    caveat ? el('p', { class: 'card-caveat', html: caveat }) : null);
}

export function kv(pairs) {
  const list = el('dl', { class: 'kv' });
  for (const [key, value] of pairs) {
    if (value === undefined) continue;
    list.append(el('dt', {}, key),
      el('dd', {}, value instanceof Node ? value : String(value ?? MISSING)));
  }
  return list;
}

export function emptyState(title, detail, extra = null) {
  return el('div', { class: 'empty-state' },
    el('strong', {}, title), el('p', { class: 'note-inline' }, detail), extra);
}

/**
 * An error the reader can act on, with the underlying message kept (T11.14).
 * The detail is disclosed rather than discarded: the logged error is the thing
 * that says what actually went wrong.
 */
export function errorState(error, { title = 'Something went wrong' } = {}) {
  const detail = error && error.detail ? String(error.detail) : '';
  return el('div', { class: 'error-state', role: 'alert' },
    el('h3', {}, title),
    el('p', {}, error ? error.message : 'Unknown error'),
    detail ? el('details', {}, el('summary', {}, 'Underlying error, as recorded'),
      el('pre', {}, detail)) : null);
}

export function loading(text = 'Loading…') {
  return el('div', { class: 'loading-block' }, el('span', { class: 'spinner' }), text);
}

export function table(columns, rows, { rowAttrs = () => ({}), maxHeight = '' } = {}) {
  const head = el('tr', {}, ...columns.map((column) =>
    el('th', { class: column.numeric ? 'num' : '', title: column.title || '' }, column.label)));
  const body = el('tbody');
  for (const row of rows) {
    const tr = el('tr', rowAttrs(row));
    for (const column of columns) {
      const value = column.render(row);
      tr.append(el('td', { class: [column.numeric ? 'num' : '', column.wrap ? 'wrap' : ''].filter(Boolean).join(' ') },
        value instanceof Node ? value : String(value ?? MISSING)));
    }
    body.append(tr);
  }
  const wrap = el('div', { class: 'table-wrap' }, el('table', {}, el('thead', {}, head), body));
  if (maxHeight) wrap.style.maxHeight = maxHeight;
  return wrap;
}

export function viewHeader(title, lede) {
  return el('header', {}, el('h2', {}, title), lede ? el('p', { class: 'view-lede', html: lede }) : null);
}

export function legend(items) {
  return el('div', { class: 'legend' }, ...items.map(([label, color]) =>
    el('span', {}, el('i', { style: `background:${color}` }), label)));
}
