/* Network Conditions (T11.6): configured impairment beside observed measurement.
 *
 * The two are kept visually and structurally apart, because conflating them is
 * the specific failure this panel exists to avoid:
 *
 *  - **Configured** is what the run was told to do. The loss is *simulated*, by
 *    a seeded RNG in network/simulator.py, and it is never presented as measured
 *    physical loss.
 *  - **Observed** is what the transfer measured: the D8 estimator's readings,
 *    the retransmissions, the RTT samples.
 *
 * And the estimate is labelled as a reading of the D8 estimator, not as a loss
 * rate — it over-reads under GBN by four to five times.
 */

import { api } from '../api.js';
import {
  el, clear, card, stat, kv, viewHeader, emptyState, errorState, num, percent, MISSING
} from '../ui.js';
import { sparkline, lineChart } from '../chart.js';

let body = null;
let cachedKey = null;

function configuredCard(configured) {
  const schedule = configured.loss_schedule;
  const scheduleText = Array.isArray(schedule) && schedule.length
    ? schedule.map(([at, probability]) => `${at}s → ${(probability * 100).toFixed(1)}%`).join(', ')
    : 'none (a static condition)';

  return card('Configured impairment', {
    note: 'what the run was told to do',
    className: 'configured',
  },
  el('div', { class: 'grid cols-3' },
    stat('Forward DATA loss', percent(configured.loss_rate), { sub: 'simulated, not physical' }),
    stat('Reverse ACK loss', percent(configured.ack_loss_rate), { sub: 'simulated' }),
    stat('Seed', configured.seed ?? MISSING, { small: true, sub: 'the same seed replays the same drops' })),
  el('div', { class: 'grid cols-3', style: 'margin-top:14px' },
    stat('RTT', num(configured.rtt_ms, 1, 'ms'), { sub: 'half applied at each endpoint' }),
    stat('Jitter', num(configured.jitter_ms, 1, 'ms'), { sub: 'bounded one-way' }),
    stat('RTO', num((configured.rto_s ?? 0) * 1000, 0, 'ms'), { sub: 'derived by D7, then held fixed' })),
  el('div', { style: 'margin-top:14px' }, kv([['Loss schedule', scheduleText]])),
  el('p', { class: 'card-caveat', html:
      '<strong>This is simulated loss.</strong> It is a drop decision made by a seeded RNG in '
      + '<code>network/simulator.py</code> before the datagram reaches the socket — a controlled '
      + 'input to the experiment, and never a measurement of a real network. Each direction draws '
      + 'from its own stream, so the forward drop sequence is a pure function of the seed (RP-03).' }));
}

function observedCard(observed, estimator) {
  const estimatePoints = (observed.loss_estimate_series || [])
    .map((point) => ({ x: point.timestamp, y: point.value }));
  const rttPoints = (observed.rtt_series || []).map((point) => ({ x: point.timestamp, y: point.value }));
  const rtt = observed.rtt_statistics || {};

  const retxOverTime = bucket(observed.retransmission_series || []);

  return card('Observed measurement', {
    note: 'what the transfer measured, from its own log',
  },
  el('div', { class: 'grid cols-3' },
    stat('Estimator, last reading', estimatePoints.length
      ? num(estimatePoints[estimatePoints.length - 1].y, 3) : MISSING,
    { sub: `a reading of D8, ${estimator.window}-outcome window` }),
    stat('Retransmissions', num(observed.retransmission_count, 0),
      { sub: `${percent(observed.retransmission_overhead)} of transmitted bytes` }),
    stat('Datagrams dropped', num(observed.drop_count, 0),
      { sub: 'DROP rows — the simulator’s own record' })),

  el('div', { class: 'grid cols-2', style: 'margin-top:16px' },
    el('div', {},
      el('h4', {}, 'Estimator reading over time'),
      estimatePoints.length > 1 ? sparkline(estimatePoints, { color: 'var(--accent)' })
        : el('p', { class: 'note-inline' }, 'no estimator readings recorded'),
      el('p', { class: 'note-inline' }, `${estimatePoints.length} samples`)),
    el('div', {},
      el('h4', {}, 'Retransmissions per second'),
      retxOverTime.length > 1 ? sparkline(retxOverTime, { color: 'var(--sr)' })
        : el('p', { class: 'note-inline' }, 'no retransmissions recorded'),
      el('p', { class: 'note-inline' }, `${observed.retransmission_count} RETX rows`))),

  el('div', { style: 'margin-top:16px' },
    el('h4', {}, 'Round-trip samples'),
    rttPoints.length > 1 ? sparkline(rttPoints, { color: 'var(--gbn)' })
      : el('p', { class: 'note-inline' }, 'no RTT samples recorded on event rows'),
    kv([
      ['samples', rtt.rtt_samples ?? 0],
      ['mean', num(rtt.rtt_mean_ms, 2, 'ms')],
      ['median', num(rtt.rtt_median_ms, 2, 'ms')],
      ['p95', num(rtt.rtt_p95_ms, 2, 'ms')],
      ['max', num(rtt.rtt_max_ms, 2, 'ms')],
    ])),
  el('p', { class: 'note-inline' },
    'A segment that was retransmitted has an ambiguous ACK and is excluded from the RTT '
    + 'samples (Karn’s rule), so under GBN there are fewer samples than segments — fewer, '
    + 'each still honest.'));
}

/** RETX rows counted into one-second buckets. A count of recorded rows, nothing more. */
function bucket(points) {
  const counts = new Map();
  for (const point of points) {
    if (!Number.isFinite(point.timestamp)) continue;
    const second = Math.floor(point.timestamp);
    counts.set(second, (counts.get(second) || 0) + 1);
  }
  return [...counts.entries()].sort((a, b) => a[0] - b[0]).map(([x, y]) => ({ x, y }));
}

function scheduleCard(network) {
  const changes = network.observed.loss_changes || [];
  if (!changes.length) return null;
  return card('Scheduled loss changes', { note: 'LOSS_CHANGE rows' },
    el('div', { class: 'grid' }, ...changes.map((change) => el('div', {},
      el('span', { class: 'stat-label' }, `${num(change.timestamp, 3)} s`),
      el('div', {}, el('code', {}, change.reason))))),
    el('p', { class: 'note-inline' },
      'The step function was applied by the simulator at these instants. What the controller '
      + 'did about it is on the Adaptive Mode page — the two are deliberately separate, because '
      + 'the interesting quantity is the delay between them.'));
}

function comparisonCard(network) {
  const configured = network.configured.loss_rate;
  const points = (network.observed.loss_estimate_series || [])
    .filter((point) => Number.isFinite(point.timestamp) && Number.isFinite(point.value))
    .map((point) => ({ x: point.timestamp, y: point.value }));
  if (!points.length || !Number.isFinite(configured)) return null;

  const series = [
    { label: 'observed: D8 estimator reading', color: 'var(--accent)', points, dots: false },
    { label: 'configured: simulated loss probability', color: 'var(--text-faint)', dashed: true, dots: false,
      points: [{ x: points[0].x, y: configured }, { x: points[points.length - 1].x, y: configured }] },
  ];

  return card('The two, on one axis — and why they differ', { note: 'D8’s known bias' },
    lineChart(series, {
      width: 900, height: 230, yMin: 0,
      xLabel: 'Time (s, sender clock)', yLabel: 'Fraction',
      yFormat: (v) => v.toFixed(3),
    }),
    el('p', { class: 'card-caveat', html:
      '<strong>The gap between these two lines is expected, not an error.</strong> The estimator '
      + 'counts segment <em>outcomes that needed a retransmission</em>, and under GBN one lost '
      + 'segment forces the whole outstanding range to be resent — so the reading runs four to '
      + 'five times above the physical loss probability. That bias is why the thresholds are '
      + 'calibrated on the estimator’s scale and must never be described as loss rates.' }));
}

async function renderBody(store) {
  const fragment = el('div', { class: 'grid' });
  if (!store.selectedKey) {
    fragment.append(emptyState('No run selected', 'Pick a run from the header, or start one.'));
    return fragment;
  }
  let network;
  try {
    network = await api.network(store.selectedKey);
  } catch (error) {
    fragment.append(errorState(error, { title: 'The network view could not be read' }));
    return fragment;
  }
  cachedKey = store.selectedKey;

  fragment.append(el('div', { class: 'grid cols-2' },
    configuredCard(network.configured),
    observedCard(network.observed, network.estimator)));
  const comparison = comparisonCard(network);
  if (comparison) fragment.append(comparison);
  const schedule = scheduleCard(network);
  if (schedule) fragment.append(schedule);
  return fragment;
}

export async function render({ store }) {
  const root = el('div');
  root.append(viewHeader('Network Conditions',
    'Configured impairment and observed measurement, kept apart. The loss on the left is '
    + '<em>simulated</em> — a seeded drop decision, a controlled input. The estimate on the '
    + 'right is a reading of the D8 estimator, not a loss rate.'));
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
