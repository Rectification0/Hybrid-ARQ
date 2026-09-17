/* Retransmission Visualization (T11.8): sequence number against time.
 *
 * The shape is the argument. Go-Back-N, on one loss, resends the whole
 * outstanding range — a diagonal stripe of squares climbing away from the
 * dropped sequence. Selective Repeat resends only the segment that was lost — a
 * single square, alone on its row. Both pictures are drawn from the run's own
 * SEND and RETX rows, at the sequence and the instant the log recorded, so the
 * difference is in the data and not in the styling.
 *
 * Nothing is drawn that did not happen: there is no interpolation, no inferred
 * transmission and no marker for a segment the log does not mention.
 */

import { api } from '../api.js';
import {
  el, clear, card, stat, viewHeader, emptyState, errorState, modeChip, modeColor, num, table,
  legend, MISSING
} from '../ui.js';
import { scatterChart } from '../chart.js';

let body = null;
let cachedKey = null;

/** Consecutive RETX rows at one instant, which is the GBN range as it was logged. */
function burstsOf(retransmissions, toleranceS = 0.02) {
  const bursts = [];
  for (const point of retransmissions) {
    const last = bursts[bursts.length - 1];
    if (last && Math.abs(point.timestamp - last.at) <= toleranceS && last.mode === point.mode) {
      last.sequences.push(point.sequence);
      last.at = point.timestamp;
    } else {
      bursts.push({ at: point.timestamp, mode: point.mode, sequences: [point.sequence] });
    }
  }
  return bursts;
}

function shapeSummary(view) {
  const bursts = burstsOf(view.retransmissions);
  const sizes = bursts.map((burst) => burst.sequences.length);
  const largest = sizes.length ? Math.max(...sizes) : 0;
  const mean = sizes.length ? sizes.reduce((a, b) => a + b, 0) / sizes.length : null;
  return { bursts, largest, mean };
}

function chartCard(view) {
  const series = [
    { label: 'first transmission (SEND)', color: 'var(--gbn)', shape: 'dot', opacity: 0.55,
      points: view.sends.map((p) => ({ x: p.timestamp, y: p.sequence })) },
    { label: 'retransmission (RETX)', color: 'var(--sr)', shape: 'square',
      points: view.retransmissions.map((p) => ({ x: p.timestamp, y: p.sequence })) },
    { label: 'dropped by the simulator (DROP)', color: 'var(--bad)', shape: 'cross',
      points: view.drops.map((p) => ({ x: p.timestamp, y: p.sequence })) },
  ];

  return card('Sequence number over time', {
    note: `${view.counts.send} SEND · ${view.counts.retx} RETX · ${view.counts.drop} DROP`,
  },
  scatterChart(series, {
    width: 980, height: 360, xMax: view.span_s || null, yMax: view.max_sequence || null,
    bands: view.mode_bands.map((band) => ({
      start: band.start, end: band.end, color: modeColor(band.mode),
      label: `${band.mode} ${band.start.toFixed(2)}–${band.end.toFixed(2)}s`,
    })),
  }),
  legend([
    ['SEND (dot)', 'var(--gbn)'], ['RETX (square)', 'var(--sr)'],
    ['DROP (cross)', 'var(--bad)'],
    ['GBN band', 'var(--gbn)'], ['SR band', 'var(--sr)'],
  ]),
  el('p', { class: 'note-inline' },
    'The shaded bands are the mode that was live, from the log’s own <code>mode</code> column. '
    + 'Marker shape carries identity as well as colour, so the three kinds stay apart in a '
    + 'greyscale print or on a projector that flattens hue.'));
}

function readingCard(view) {
  const { bursts, largest, mean } = shapeSummary(view);
  if (!view.counts.retx) {
    return card('What the shape says', { note: 'nothing was retransmitted' },
      emptyState('This run retransmitted nothing',
        'At zero loss neither strategy has anything to resend, which is why the clean condition '
        + 'is the control the lossy ones are read against.'));
  }

  const bandModes = [...new Set(view.mode_bands.map((band) => band.mode))];

  return card('What the shape says', { note: 'derived from the plotted rows only' },
    el('div', { class: 'grid cols-3' },
      stat('Retransmission bursts', num(bursts.length, 0),
        { sub: 'consecutive RETX rows at one instant' }),
      stat('Largest burst', num(largest, 0),
        { sub: 'segments resent together' }),
      stat('Mean burst', num(mean, 2),
        { sub: 'segments per burst' })),
    el('p', { class: 'note-inline', style: 'margin-top:14px' },
      largest > 1
        ? 'Bursts wider than one segment are the Go-Back-N rule made visible: a single loss '
          + 'forces every segment behind it back onto the wire, whether or not it arrived '
          + '(GBN-04).'
        : 'Every burst is a single segment. That is Selective Repeat: only the segment the '
          + 'timer named is resent, and the rest of the window is left alone (SR-10).'),
    bandModes.length > 1
      ? el('p', { class: 'note-inline' },
        'This run changed mode, so both shapes appear in one picture — compare the width of '
        + 'the bursts inside the ', modeChip('gbn'), ' band with those inside the ',
        modeChip('sr'), ' band.')
      : null,
    el('p', { class: 'note-inline' },
      'A burst here is a count of rows the sender logged. It is not the same number as '
      + 'duplicates on the wire: a segment that was dropped never arrived, so resending it puts '
      + 'no duplicate sequence in a capture. The gap between the two is the point of the '
      + 'comparison in <code>captures/README.md</code>.'));
}

function burstTable(view) {
  const { bursts } = shapeSummary(view);
  if (!bursts.length) return null;
  return card('Retransmission bursts, as logged', { note: 'the first 200' },
    table([
      { label: 'At (s)', numeric: true, render: (row) => num(row.at, 3) },
      { label: 'Mode', render: (row) => (row.mode ? modeChip(row.mode) : MISSING) },
      { label: 'Segments', numeric: true, render: (row) => row.sequences.length },
      { label: 'Sequences resent', wrap: true,
        render: (row) => el('code', {}, row.sequences.join(', ')) },
    ], bursts.slice(0, 200), { maxHeight: '40vh' }));
}

async function renderBody(store) {
  const fragment = el('div', { class: 'grid' });
  if (!store.selectedKey) {
    fragment.append(emptyState('No run selected', 'Pick a run from the header, or start one.'));
    return fragment;
  }
  let view;
  try {
    view = await api.retransmissions(store.selectedKey);
  } catch (error) {
    fragment.append(errorState(error, { title: 'The retransmission view could not be read' }));
    return fragment;
  }
  cachedKey = store.selectedKey;

  if (!view.counts.send) {
    fragment.append(emptyState('No SEND rows in this run',
      'Nothing was transmitted, so there is nothing to draw. This is an empty state, not an '
      + 'error — a receiver-only log looks exactly like this.'));
    return fragment;
  }

  fragment.append(chartCard(view));
  fragment.append(el('div', { class: 'grid cols-2' }, readingCard(view), burstTable(view)));
  return fragment;
}

export async function render({ store }) {
  const root = el('div');
  root.append(viewHeader('Retransmission Visualization',
    'Sequence number against time, drawn from the run’s own <code>SEND</code> and '
    + '<code>RETX</code> rows. Go-Back-N resends a range; Selective Repeat resends one segment. '
    + 'The two are different shapes because the events are different.'));
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
