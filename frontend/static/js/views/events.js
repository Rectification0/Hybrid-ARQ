/* Packet / Event Activity (T11.7): the specs.md §21 columns, as recorded.
 *
 * This is a reader for events.csv, and nothing else. It writes no log of its
 * own — there is exactly one event log per endpoint per run, and a second one
 * kept by the browser would give every metric two sources that could disagree
 * (CC-06).
 *
 * Tracing one segment is the thing this view exists for: filter by sequence and
 * a single segment's SEND, any DROP, its RETX attempts and the ACK that closed
 * it line up in order.
 */

import { api } from '../api.js';
import {
  el, clear, card, viewHeader, emptyState, errorState, modeChip, table, num, MISSING
} from '../ui.js';

let body = null;
let controls = null;
let cachedKey = null;

const state = {
  endpoint: 'sender',
  event: '',
  mode: '',
  sequence: '',
  limit: 500,
  autoScroll: true,
  paused: false,
};

const EVENT_NAMES = [
  'SEND', 'RETX', 'ACK', 'TIMEOUT', 'TIMER_START', 'TIMER_STOP', 'SWITCH',
  'DROP', 'CHECKSUM_FAIL', 'MALFORMED', 'DUPLICATE', 'DELIVER', 'LOSS_CHANGE',
  'START', 'START_ACK', 'FIN', 'FIN_ACK', 'MODE', 'ERROR',
];

const COLUMNS = [
  { label: 'timestamp', numeric: true, title: 'seconds from THIS endpoint’s start',
    render: (row) => num(row.timestamp, 6) },
  { label: 'endpoint', render: (row) => row.endpoint },
  { label: 'event', render: (row) => el('span', { class: `ev ev-${row.event}` }, row.event) },
  { label: 'sequence', numeric: true, render: (row) => (row.sequence === '' ? MISSING : row.sequence) },
  { label: 'ack', numeric: true, render: (row) => (row.ack === '' ? MISSING : row.ack) },
  { label: 'mode', render: (row) => (row.mode ? modeChip(row.mode) : MISSING) },
  { label: 'window', numeric: true, render: (row) => (row.window_size === '' ? MISSING : row.window_size) },
  { label: 'loss_estimate', numeric: true, render: (row) => (row.loss_estimate === '' ? MISSING : row.loss_estimate) },
  { label: 'rtt_ms', numeric: true, render: (row) => (row.rtt_ms === '' ? MISSING : row.rtt_ms) },
  { label: 'bytes', numeric: true, render: (row) => (row.bytes === '' ? MISSING : row.bytes) },
  { label: 'reason', wrap: true, render: (row) => (row.reason ? el('code', {}, row.reason) : MISSING) },
];

function toolbar(rerender) {
  const select = (name, options, label) => el('label', { class: 'field' },
    el('span', {}, label),
    el('select', {
      value: state[name],
      onchange: (event) => { state[name] = event.target.value; rerender(); },
    }, ...options.map(([value, text]) =>
      el('option', { value, selected: state[name] === value }, text))));

  return el('div', { class: 'toolbar' },
    select('endpoint', [['sender', 'sender'], ['receiver', 'receiver'], ['', 'both']], 'Endpoint'),
    select('event', [['', 'every event'], ...EVENT_NAMES.map((name) => [name, name])], 'Event'),
    select('mode', [['', 'every mode'], ['gbn', 'GBN'], ['sr', 'SR'], ['saw', 'stop-and-wait']], 'Mode'),
    el('label', { class: 'field' }, el('span', {}, 'Sequence'),
      el('input', {
        type: 'number', min: 0, placeholder: 'trace one segment', value: state.sequence,
        oninput: (event) => { state.sequence = event.target.value; rerender(); },
      })),
    el('label', { class: 'field' }, el('span', {}, 'Rows'),
      el('select', {
        onchange: (event) => { state.limit = Number(event.target.value); rerender(); },
      }, ...[200, 500, 2000, 5000].map((value) =>
        el('option', { value, selected: state.limit === value }, value)))),
    el('label', { class: 'field' }, el('span', {}, 'Live'),
      el('button', {
        class: 'secondary', type: 'button',
        onclick: (event) => {
          state.paused = !state.paused;
          event.target.textContent = state.paused ? 'Paused' : 'Following';
        },
      }, state.paused ? 'Paused' : 'Following')),
    el('label', { class: 'field' }, el('span', {}, 'Auto-scroll'),
      el('button', {
        class: 'secondary', type: 'button',
        onclick: (event) => {
          state.autoScroll = !state.autoScroll;
          event.target.textContent = state.autoScroll ? 'On' : 'Off';
        },
      }, state.autoScroll ? 'On' : 'Off')));
}

async function renderTable(store) {
  const payload = await api.events({
    run: store.selectedKey,
    endpoint: state.endpoint,
    event: state.event,
    mode: state.mode,
    sequence: state.sequence,
    limit: state.limit,
  });
  cachedKey = store.selectedKey;

  const total = Object.values(payload.totals).reduce((a, b) => a + b, 0);
  const node = el('div');

  if (!total) {
    node.append(emptyState('This run has no event rows',
      'The log exists but is empty. A receiver that never heard from anyone still writes one '
      + '(T6.1), so this is a real state rather than a missing file.'));
    return node;
  }

  node.append(el('p', { class: 'note-inline', style: 'margin-top:0' },
    `${payload.matched.toLocaleString()} of ${total.toLocaleString()} rows match`,
    payload.truncated ? ` · showing the first ${state.limit.toLocaleString()}` : '',
    state.sequence ? ` · tracing sequence ${state.sequence} through SEND, DROP, RETX and ACK` : ''));

  if (!payload.rows.length) {
    node.append(emptyState('No row matches this filter',
      'Clear a filter, or pick another event type. An empty result is information: it means '
      + 'the run genuinely recorded nothing of that kind.'));
    return node;
  }

  node.append(table(COLUMNS, payload.rows, {
    rowAttrs: (row) => ({ dataset: { event: row.event } }),
    maxHeight: '64vh',
  }));
  node.append(el('p', { class: 'card-caveat' }, payload.live_lag_note));

  if (state.autoScroll && !state.paused && store.transfer.active) {
    const wrap = node.querySelector('.table-wrap');
    if (wrap) requestAnimationFrame(() => { wrap.scrollTop = wrap.scrollHeight; });
  }
  return node;
}

async function refresh(store) {
  if (!body) return;
  try {
    clear(body).append(await renderTable(store));
  } catch (error) {
    clear(body).append(errorState(error, { title: 'The event log could not be read' }));
  }
}

export async function render({ store }) {
  const root = el('div');
  root.append(viewHeader('Packet / Event Activity',
    'The <code>events.csv</code> columns exactly as specs.md §21 defines them. '
    + 'Filter by sequence to follow one segment from <code>SEND</code> through <code>DROP</code>, '
    + '<code>RETX</code> and <code>ACK</code>.'));

  if (!store.selectedKey) {
    root.append(emptyState('No run selected', 'Pick a run from the header, or start one.'));
    return root;
  }

  controls = toolbar(() => refresh(store));
  body = el('div');
  root.append(card('Events', {
    note: '<code>SEND</code> and <code>RETX</code> are distinct events, deliberately',
  }, controls, body));
  await refresh(store);

  root.append(el('p', { class: 'note-inline' },
    'Row colour marks the event type, and the type is also spelled out in its own column — '
    + 'colour is never the only carrier. Both endpoints are shown separately because their '
    + 'timestamps have different origins and must not be interleaved by subtraction (§21).'));
  return root;
}

export async function onTick({ store }) {
  if (!body || state.paused) return;
  if (!store.transfer.active && store.selectedKey === cachedKey) return;
  await refresh(store);
}
