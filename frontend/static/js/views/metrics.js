/* Metrics Dashboard (T11.9): every specs.md §20 metric, with units.
 *
 * The numbers here are the ones metrics.py derives from events.csv, computed on
 * the server by that module — not re-derived in the browser. That is what makes
 * "a completed run's numbers match what metrics.py derives for the same run"
 * true by construction rather than by coincidence.
 *
 * Where a panel does arithmetic of its own — a ratio for display, a percentage
 * of a total — it is labelled a presentation-only calculation, so no reader
 * mistakes it for a recorded measurement.
 */

import { api } from '../api.js';
import {
  el, clear, card, stat, badge, viewHeader, emptyState, errorState, integrityBadge, table,
  num, bytes, rate, seconds, percent, MISSING
} from '../ui.js';

let body = null;
let cachedKey = null;

const SPEC_20 = [
  ['Goodput', 'Successfully delivered application bytes / transfer time'],
  ['Completion time', 'Time from transfer start to verified completion'],
  ['Retransmission count', 'Total DATA retransmission events'],
  ['Retransmission overhead', 'Retransmitted bytes / total transmitted bytes'],
  ['Integrity success', 'Whether source and received hashes match'],
  ['Switch count', 'Total GBN↔SR transitions'],
  ['GBN residence', 'Time spent in GBN'],
  ['SR residence', 'Time spent in SR'],
  ['Latency', 'Measured delivery/transfer delay where defined'],
  ['Optional RTT statistics', 'Mean, median, tail RTT'],
];

function headline(derived) {
  return el('div', { class: 'grid cols-4' },
    stat('Goodput', rate(derived.goodput_bytes_per_s),
      { sub: 'delivered bytes / completion time' }),
    stat('Completion time', seconds(derived.completion_time_s),
      { sub: 'to the FIN_ACK that carried the verdict' }),
    stat('Retransmissions', num(derived.retransmission_count, 0),
      { sub: 'count of RETX rows' }),
    stat('Retransmission overhead', percent(derived.retransmission_overhead),
      { sub: 'resent bytes / all transmitted bytes' }));
}

function residenceBar(derived) {
  const gbn = Number(derived.gbn_residence_s) || 0;
  const sr = Number(derived.sr_residence_s) || 0;
  const total = gbn + sr;
  if (!total) return el('p', { class: 'note-inline' }, 'No mode residence was recorded.');
  return el('div', {},
    el('div', { class: 'progress', style: 'height:18px' },
      el('span', {
        style: `width:${((gbn / total) * 100).toFixed(2)}%;background:var(--gbn)`,
        title: `GBN ${gbn.toFixed(3)} s`,
      })),
    el('p', { class: 'note-inline' },
      `GBN ${seconds(gbn)} (${percent(gbn / total)}) · SR ${seconds(sr)} (${percent(sr / total)})`,
      el('span', { class: 'stat-sub' },
        ' — the fractions are a presentation-only calculation over the two recorded seconds.')));
}

function specTable(derived) {
  const rows = [
    ['Goodput', rate(derived.goodput_bytes_per_s), 'bytes/s', 'receiver DELIVER rows ÷ sender completion time'],
    ['Completion time', seconds(derived.completion_time_s), 's', 'sender log, to FIN_ACK'],
    ['Retransmission count', num(derived.retransmission_count, 0), 'events', 'RETX rows'],
    ['Retransmission overhead', percent(derived.retransmission_overhead), 'fraction', 'bytes column of RETX ÷ SEND+RETX'],
    ['Integrity success', integrityBadge(derived.integrity_success), '', 'FIN_ACK verdict'],
    ['Switch count', num(derived.switch_count, 0), 'transitions', 'SWITCH rows, excluding FIXED_HYBRID_NOOP'],
    ['MODE handshakes', num(derived.handshake_count, 0), 'exchanges', 'all SWITCH rows'],
    ['GBN residence', seconds(derived.gbn_residence_s), 's', 'mode column over time'],
    ['SR residence', seconds(derived.sr_residence_s), 's', 'mode column over time'],
    ['Delivered bytes', bytes(derived.delivered_bytes), 'B', 'receiver DELIVER rows'],
    ['Bytes transmitted', bytes(derived.bytes_transmitted), 'B', 'sender SEND + RETX rows'],
    ['Unique DATA packets', num(derived.unique_data_packets, 0), 'packets', 'SEND rows'],
    ['Total DATA transmissions', num(derived.total_data_transmissions, 0), 'packets', 'SEND + RETX rows'],
    ['First delivery', seconds(derived.first_delivery_s), 's', 'receiver log, first DELIVER'],
    ['Last delivery', seconds(derived.last_delivery_s), 's', 'receiver log, last DELIVER'],
    ['RTT samples', num(derived.rtt_samples, 0), 'samples', 'unambiguous SEND→ACK pairs (Karn)'],
    ['RTT mean', num(derived.rtt_mean_ms, 2), 'ms', 'of those samples'],
    ['RTT median', num(derived.rtt_median_ms, 2), 'ms', 'of those samples'],
    ['RTT p95', num(derived.rtt_p95_ms, 2), 'ms', 'of those samples'],
    ['RTT max', num(derived.rtt_max_ms, 2), 'ms', 'of those samples'],
  ];

  return table([
    { label: 'Metric', render: (row) => row[0] },
    { label: 'Value', numeric: true, render: (row) => row[1] },
    { label: 'Unit', render: (row) => row[2] || MISSING },
    { label: 'Derived from', wrap: true, render: (row) => row[3] },
  ], rows, { maxHeight: '58vh' });
}

function agreementCard(detail) {
  /* The endpoints computed their own counters while they ran, and metrics.py
   * derives the same figures from the log afterwards. T6.2 asserts the two
   * agree; showing them side by side makes that checkable on the screen too. */
  const derived = detail.derived_metrics || {};
  const own = (detail.sender_summary || {}).metrics || {};
  const pairs = [
    ['completion_time_s', 'Completion time', (v) => seconds(v)],
    ['retransmission_count', 'Retransmissions', (v) => num(v, 0)],
    ['switch_count', 'Switches', (v) => num(v, 0)],
    ['goodput_bytes_per_s', 'Goodput', (v) => rate(v)],
  ].filter(([key]) => own[key] !== undefined);

  if (!pairs.length) {
    return card('Derived vs the endpoint’s own counters', {},
      emptyState('No sender summary for this run',
        'Only the derivation from events.csv is available, which is the authoritative one — '
        + 'CC-06 requires every §20 metric to be computable from the log alone.'));
  }

  return card('Derived vs the endpoint’s own counters', { note: 'T6.2 asserts these agree' },
    table([
      { label: 'Metric', render: (row) => row.label },
      { label: 'Derived from events.csv', numeric: true, render: (row) => row.derived },
      { label: 'Recorded in summary.json', numeric: true, render: (row) => row.own },
      { label: '', render: (row) => (row.agrees ? badge('agree', 'ok') : badge('differ', 'warn')) },
    ], pairs.map(([key, label, format]) => {
      const a = Number(derived[key]);
      const b = Number(own[key]);
      const agrees = !Number.isFinite(a) || !Number.isFinite(b)
        ? a === b : Math.abs(a - b) <= Math.max(1e-6, Math.abs(b) * 0.02);
      return { label, derived: format(derived[key]), own: format(own[key]), agrees };
    })),
    el('p', { class: 'note-inline' },
      'The left column is what <code>metrics.py</code> reads out of the event log; the right is '
      + 'what the endpoint counted as it ran. Comparison is to within 2%, because the two stop '
      + 'their clocks a fraction of a second apart.'));
}

async function renderBody(store) {
  const fragment = el('div', { class: 'grid' });
  if (!store.selectedKey) {
    fragment.append(emptyState('No run selected', 'Pick a run from the header, or start one.'));
    return fragment;
  }
  let detail;
  try {
    detail = await api.run(store.selectedKey);
  } catch (error) {
    fragment.append(errorState(error, { title: 'The metrics could not be read' }));
    return fragment;
  }
  cachedKey = store.selectedKey;
  const derived = detail.derived_metrics || {};

  if (!Object.keys(derived).length) {
    fragment.append(emptyState('This run has no event rows to derive from',
      'Every §20 metric is computed from events.csv alone (CC-06), so an empty log yields '
      + 'no metrics rather than zeros.'));
    return fragment;
  }

  fragment.append(card('Headline', {
    note: `run ${detail.run_id}`,
  }, headline(derived),
  el('div', { class: 'grid cols-4', style: 'margin-top:16px' },
    stat('Integrity', integrityBadge(derived.integrity_success), { sub: 'source vs received hash' }),
    stat('Switches', num(derived.switch_count, 0),
      { sub: `${num(derived.handshake_count, 0)} handshakes` }),
    stat('Delivered', bytes(derived.delivered_bytes), { sub: 'application bytes written' }),
    stat('Transmitted', bytes(derived.bytes_transmitted), { sub: 'datagram bytes on the wire' })),
  el('div', { style: 'margin-top:16px' },
    el('h4', {}, 'Mode residence'), residenceBar(derived)),
  derived.integrity_success === false
    ? el('p', { class: 'card-caveat', style: 'border-color:var(--bad)' },
      '<strong>Integrity failed for this run.</strong> Goodput is reported as unavailable rather '
      + 'than computed: a transfer that did not deliver the file has no meaningful throughput, '
      + 'and averaging one in later would flatter the result.')
    : null));

  fragment.append(el('div', { class: 'grid cols-2' },
    card('Every specs.md §20 metric', { note: 'derived by metrics.py from events.csv alone' },
      specTable(derived)),
    el('div', { class: 'grid' },
      agreementCard(detail),
      card('What §20 asks for', { note: 'the specification, verbatim' },
        table([
          { label: 'Metric', render: (row) => row[0] },
          { label: 'Definition', wrap: true, render: (row) => row[1] },
        ], SPEC_20)))));
  return fragment;
}

export async function render({ store }) {
  const root = el('div');
  root.append(viewHeader('Metrics Dashboard',
    'Every metric in specs.md §20, with units, for the selected run. These are the values '
    + '<code>metrics.py</code> derives from <code>events.csv</code> — the same derivation T6.2 '
    + 'asserts against the endpoints’ own counters.'));
  body = el('div', {}, await renderBody(store));
  root.append(body);
  return root;
}

let refreshing = false;

export async function onTick({ store }) {
  if (!body || refreshing) return;
  if (!store.transfer.active && store.selectedKey === cachedKey) return;
  refreshing = true;
  try { clear(body).append(await renderBody(store)); } finally { refreshing = false; }
}
