/* Adaptive Mode Visualization (T11.5) — the centrepiece.
 *
 * When and *why* the controller switched, readable from the screen alone.
 *
 * Every field on this page came out of the log. The reason string is the
 * controller's own, written by protocol/hybrid.py at the moment it decided; the
 * loss estimate is the value the estimator held at that instant; the epoch is
 * the one the MODE handshake carried. Nothing is recomputed here — a threshold
 * re-evaluated in JavaScript would be a second controller, and the two would
 * drift apart the first time either changed.
 *
 * A SWITCH row whose reason contains FIXED_HYBRID_NOOP is the --mode
 * fixed-hybrid control paying the drain cost of a handshake without changing
 * semantics. It is drawn as a handshake, not as a transition, and is excluded
 * from the transition count — which is exactly what metrics.py does.
 */

import { api } from '../api.js';
import {
  el, clear, card, stat, badge, viewHeader, emptyState, errorState, modeChip,
  modeColor, num, seconds, percent, table, legend, MISSING,
} from '../ui.js';
import { timelineBand, lineChart, thresholdLine } from '../chart.js';

let body = null;
let cachedKey = null;

function headline(timeline, network) {
  const residence = timeline.residence_s || {};
  const total = Object.values(residence).reduce((a, b) => a + b, 0);
  const fraction = (mode) => (total ? (residence[mode] || 0) / total : null);

  return el('div', { class: 'grid cols-4' },
    stat('Current mode', timeline.current_mode
      ? modeChip(timeline.current_mode, { large: true })
      : el('span', { class: 'stat-value muted small' }, 'none recorded'),
      { sub: 'last mode named in the log' }),
    stat('Previous mode', timeline.previous_mode
      ? modeChip(timeline.previous_mode, { large: true })
      : el('span', { class: 'stat-value muted small' }, 'never changed'),
      { sub: 'mode before the last real transition' }),
    stat('Transitions', num(timeline.switch_count, 0),
      { sub: `${num(timeline.handshake_count, 0)} handshakes · ${num(timeline.noop_count, 0)} no-op` }),
    stat('Time in SR', percent(fraction('sr')),
      { sub: `GBN ${percent(fraction('gbn'))} · ${seconds(total, 1)} recorded` }));
}

function transitionsTable(timeline) {
  if (!timeline.transitions.length) {
    return emptyState('No SWITCH row was recorded',
      'This run never changed mode. A fixed gbn or sr run never can; a hybrid run that stayed '
      + 'in one mode either never crossed a threshold or never confirmed a crossing three times.');
  }

  return table([
    { label: 'At (s)', numeric: true, render: (row) => num(row.timestamp, 3) },
    { label: 'From', render: (row) => (row.from_mode ? modeChip(row.from_mode) : MISSING) },
    { label: 'To', render: (row) => modeChip(row.to_mode) },
    { label: 'Estimate then', numeric: true, title: 'the D8 estimator reading at the moment of the decision',
      render: (row) => num(row.loss_estimate, 3) },
    { label: 'Epoch', numeric: true, render: (row) => (row.epoch ?? MISSING) },
    { label: 'Kind', render: (row) => (row.noop
      ? badge('no-op (control)', 'warn') : badge('mode changed', 'info')) },
    { label: 'Reason, as recorded', wrap: true, render: (row) => el('code', {}, row.reason || MISSING) },
  ], timeline.transitions, { maxHeight: '38vh' });
}

function handshakeTable(timeline) {
  if (!timeline.handshakes.length) {
    return emptyState('No MODE row was recorded',
      'The MODE event (T5.4) records the handshake itself — including one that was refused, '
      + 'repeated or abandoned. Its absence means no handshake was attempted.');
  }
  return table([
    { label: 'At (s)', numeric: true, render: (row) => num(row.timestamp, 3) },
    { label: 'Mode then', render: (row) => (row.mode ? modeChip(row.mode) : MISSING) },
    { label: 'Epoch', numeric: true, render: (row) => (row.epoch ?? MISSING) },
    { label: 'What the handshake did', wrap: true, render: (row) => el('code', {}, row.reason || MISSING) },
  ], timeline.handshakes, { maxHeight: '32vh' });
}

function estimateChart(network, timeline) {
  const points = (network.observed.loss_estimate_series || [])
    .filter((point) => Number.isFinite(point.timestamp) && Number.isFinite(point.value))
    .map((point) => ({ x: point.timestamp, y: point.value }));

  if (points.length < 2) {
    return emptyState('Too few estimator readings to plot',
      'The loss estimate is written on rows where the controller held one. A fixed gbn or sr '
      + 'run has no controller, so it records none.');
  }

  const yHi = Math.max(network.estimator.switch_high * 1.35, ...points.map((p) => p.y)) || 1;
  const chart = lineChart([{
    label: 'D8 estimator', color: 'var(--accent)', points, dots: false,
  }], {
    width: 900, height: 250, yMin: 0, yMax: yHi,
    xLabel: 'Time (s, sender clock)', yLabel: 'Estimator reading',
    xFormat: (v) => `${v.toFixed(1)}`, yFormat: (v) => v.toFixed(3),
    markers: timeline.transitions.map((entry) => ({
      x: entry.timestamp,
      color: entry.noop ? 'var(--fixed-hybrid)' : modeColor(entry.to_mode),
      label: entry.noop ? 'no-op' : String(entry.to_mode || '').toUpperCase(),
    })),
    bands: timeline.bands.map((band) => ({
      start: band.start, end: band.end, color: modeColor(band.mode),
      label: `${band.mode} ${band.start.toFixed(2)}–${band.end.toFixed(2)}s`, opacity: 0.09,
    })),
  });

  if (chart.tagName === 'svg') {
    thresholdLine(chart, {
      y: network.estimator.switch_high, yLo: 0, yHi, width: 900, height: 250,
      color: 'var(--sr)', label: `SWITCH_HIGH ${network.estimator.switch_high} → enter SR`,
    });
    thresholdLine(chart, {
      y: network.estimator.switch_low, yLo: 0, yHi, width: 900, height: 250,
      color: 'var(--gbn)', label: `SWITCH_LOW ${network.estimator.switch_low} → return to GBN`,
    });
  }
  return chart;
}

async function renderBody(store) {
  const fragment = el('div', { class: 'grid' });
  if (!store.selectedKey) {
    fragment.append(emptyState('No run selected', 'Pick a run from the header, or start one.'));
    return fragment;
  }

  let timeline;
  let network;
  try {
    [timeline, network] = await Promise.all([
      api.modes(store.selectedKey), api.network(store.selectedKey),
    ]);
  } catch (error) {
    fragment.append(errorState(error, { title: 'The mode timeline could not be read' }));
    return fragment;
  }
  cachedKey = store.selectedKey;

  fragment.append(card('Mode over time', {
    note: `${timeline.span_s ? `${timeline.span_s.toFixed(1)} s recorded` : 'no rows'}`,
  },
  headline(timeline, network),
  el('div', { style: 'margin-top:16px' },
    timelineBand(timeline.bands, timeline.transitions, {
      width: 960, height: 104, span: timeline.span_s, colorOf: modeColor,
    })),
  legend([
    ['GBN', 'var(--gbn)'], ['SR', 'var(--sr)'],
    ['transition (mode changed)', 'var(--text)'],
    ['no-op handshake (fixed-hybrid control)', 'var(--fixed-hybrid)'],
  ]),
  el('p', { class: 'note-inline' },
    'Bands come from the <code>mode</code> column of every row, so an abandoned handshake — '
    + 'which leaves the mode unchanged — shows as a mark with no change in the band beneath it.')));

  fragment.append(card('The estimator and the thresholds it was read against', {
    note: 'D8 and D9, as recorded',
  },
  estimateChart(network, timeline),
  el('p', { class: 'card-caveat', html:
      '<strong>This is an estimator reading, not a loss rate.</strong> D8 is the fraction of the '
      + `last ${network.estimator.window} acknowledged segment outcomes that needed a `
      + 'retransmission, and under GBN it over-reads true loss by four to five times — so '
      + `SWITCH_HIGH = ${network.estimator.switch_high} fires at roughly 2% physical loss. `
      + `A crossing must be confirmed ${network.estimator.hysteresis_count} times in a row before `
      + 'the controller acts (HY-09).' })));

  fragment.append(el('div', { class: 'grid cols-2' },
    card('Transitions', { note: 'reason and estimate taken from the SWITCH row' },
      transitionsTable(timeline)),
    card('MODE handshakes', { note: 'the exchange itself — including refused and abandoned' },
      handshakeTable(timeline),
      el('p', { class: 'note-inline' },
        'A handshake that exceeds its retry budget abandons the switch and the transfer '
        + 'continues in the current mode. A half-switched transfer is never acceptable, '
        + 'so an abandoned handshake is a normal outcome, not an error (design.md §6.3).'))));

  if (timeline.noop_count) {
    fragment.append(card('This run is the fixed-hybrid control', { note: 'T5.6' },
      el('p', {},
        `${timeline.noop_count} of its ${timeline.handshake_count} handshakes carry `
        + 'FIXED_HYBRID_NOOP. The control drains its window and exchanges MODE exactly as the '
        + 'hybrid does, and then stays where it was — so its cost is the switching machinery '
        + 'without the switch. Its line lying on GBN’s in the recorded figures is the '
        + 'clearest statement that the hybrid’s advantage comes from changing mode and not '
        + 'from anything else the controller does.')));
  }
  return fragment;
}

export async function render({ store }) {
  const root = el('div');
  root.append(viewHeader('Adaptive Mode Visualization',
    'When the controller switched, and why. Every reason, estimate and epoch on this page is '
    + 'read from the recorded <code>SWITCH</code> and <code>MODE</code> rows — the decision was '
    + 'made in <code>protocol/hybrid.py</code> and is not re-evaluated here.'));
  body = el('div', {}, await renderBody(store));
  root.append(body);
  return root;
}

let refreshing = false;

export async function onTick({ store }) {
  if (!body || refreshing) return;
  if (!store.transfer.active && store.selectedKey === cachedKey) return;
  refreshing = true;
  try {
    clear(body).append(await renderBody(store));
  } finally {
    refreshing = false;
  }
}
