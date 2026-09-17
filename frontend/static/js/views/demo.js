/* Final demonstration (T11.13): the thirteen steps of specs.md §28, on one screen.
 *
 * `experiments/demonstrate.py` already performs the sequence headlessly, writes
 * a transcript and takes a capture. This page presents *that recorded run*
 * rather than inventing a parallel script — so there is one demonstration, one
 * set of numbers, and nothing that only exists for the screen.
 *
 * No fake events and no pre-recorded animation presented as live. Where the
 * step's evidence is a recorded figure, a log row or a capture, that is what is
 * shown, and it is labelled as recorded. The live panels are live because they
 * are reading a transfer that is actually running.
 */

import { api } from '../api.js';
import {
  el, clear, card, stat, badge, kv, viewHeader, emptyState, errorState, modeChip, modeColor,
  integrityBadge, table, num, seconds, percent, rate, bytes, MISSING,
} from '../ui.js';
import { timelineBand } from '../chart.js';

let currentStep = 1;
let root = null;
let payload = null;

/* Which live panel each step wants beside it, so a demonstration does not
 * hop between pages mid-flow. The steps that are about the wire point at
 * Wireshark rather than pretending to show packets. */
const STEP_PANEL = {
  1: 'endpoints', 2: 'wireshark', 3: 'progress', 4: 'mode', 5: 'network',
  6: 'estimator', 7: 'mode', 8: 'wireshark', 9: 'network', 10: 'mode',
  11: 'wireshark', 12: 'integrity', 13: 'comparison',
};

function stepList(store) {
  return el('ol', { class: 'step-list' },
    ...payload.steps.map((step) => el('li', {
      'aria-current': step.number === currentStep ? 'step' : null,
      tabindex: 0,
      role: 'button',
      onclick: () => { currentStep = step.number; rerender(store); },
      onkeydown: (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          currentStep = step.number;
          rerender(store);
        }
      },
    },
    el('span', { class: 'step-n' }, String(step.number)),
    el('div', {},
      el('div', { class: 'step-title' }, step.title),
      el('div', { class: 'step-detail' }, step.detail),
      payload.rehearsal[step.number]
        ? el('div', { class: 'step-detail' },
          el('strong', {}, 'Rehearsal recorded: '), payload.rehearsal[step.number])
        : null))));
}

/* --------------------------------------------------------------- panels */

function modePanel(detail) {
  const timeline = detail.timeline;
  if (!timeline || !timeline.bands.length) {
    return emptyState('No mode was recorded for the demonstration run',
      'The rehearsal writes its logs under logs/demo/. Run '
      + '`python experiments/demonstrate.py` to produce them.');
  }
  return el('div', {},
    el('div', { class: 'grid cols-3' },
      stat('Ends in', timeline.current_mode ? modeChip(timeline.current_mode, { large: true }) : MISSING,
        { sub: 'last mode in the log' }),
      stat('Transitions', num(timeline.switch_count, 0), { sub: 'excluding no-ops' }),
      stat('Time in SR', percent(
        (timeline.residence_s.sr || 0)
        / Math.max(1e-9, (timeline.residence_s.sr || 0) + (timeline.residence_s.gbn || 0))),
      { sub: 'of the recorded span' })),
    el('div', { style: 'margin-top:14px' },
      timelineBand(timeline.bands, timeline.transitions, {
        width: 860, height: 104, span: timeline.span_s, colorOf: modeColor,
      })),
    timeline.transitions.length
      ? table([
        { label: 'At (s)', numeric: true, render: (row) => num(row.timestamp, 2) },
        { label: 'To', render: (row) => modeChip(row.to_mode) },
        { label: 'Estimate then', numeric: true, render: (row) => num(row.loss_estimate, 3) },
        { label: 'Reason, as recorded', wrap: true, render: (row) => el('code', {}, row.reason) },
      ], timeline.transitions)
      : null);
}

function networkPanel(detail) {
  const network = detail.network;
  if (!network) return emptyState('No network view', 'The demonstration run has no log here.');
  const changes = network.observed.loss_changes || [];
  return el('div', {},
    el('div', { class: 'grid cols-3' },
      stat('Configured loss', percent(network.configured.loss_rate), { sub: 'simulated, seeded' }),
      stat('Schedule', Array.isArray(network.configured.loss_schedule) && network.configured.loss_schedule.length
        ? network.configured.loss_schedule.map(([at, p]) => `${at}s→${(p * 100).toFixed(0)}%`).join('  ')
        : 'static', { small: true, sub: 'step function, §17.3' }),
      stat('Seed', network.configured.seed ?? MISSING, { small: true, sub: 'replays the same drops' })),
    changes.length
      ? table([
        { label: 'At (s)', numeric: true, render: (row) => num(row.timestamp, 2) },
        { label: 'LOSS_CHANGE, as recorded', wrap: true, render: (row) => el('code', {}, row.reason) },
      ], changes)
      : el('p', { class: 'note-inline' }, 'No LOSS_CHANGE rows were recorded for this run.'),
    el('p', { class: 'card-caveat', html:
      '<strong>This loss is simulated.</strong> It is a seeded drop decision made before the '
      + 'datagram reaches the socket — a controlled input to the experiment, not a measurement '
      + 'of a real network.' }));
}

function estimatorPanel(detail) {
  const network = detail.network;
  const timeline = detail.timeline;
  if (!network) return emptyState('No estimator readings', 'The demonstration run has no log here.');
  const series = network.observed.loss_estimate_series || [];
  const atSwitch = (timeline.transitions[0] || {}).loss_estimate;
  return el('div', {},
    el('div', { class: 'grid cols-3' },
      stat('SWITCH_HIGH', network.estimator.switch_high, { sub: 'enter SR above this' }),
      stat('Estimate at the first switch', num(atSwitch, 3),
        { sub: 'the value the controller held then' }),
      stat('Confirmations required', network.estimator.hysteresis_count,
        { sub: 'consecutive, before acting (HY-09)' })),
    el('p', { class: 'card-caveat', html:
      `<strong>A reading of the estimator, not a loss rate.</strong> D8 counts the fraction of `
      + `the last ${network.estimator.window} acknowledged segment outcomes that needed a `
      + `retransmission. Under GBN it over-reads true loss four to five times over, which is why `
      + `SWITCH_HIGH = ${network.estimator.switch_high} fires at roughly 2% physical loss.` }),
    el('p', { class: 'note-inline' }, `${series.length} estimator readings recorded.`));
}

function integrityPanel(detail) {
  const integrity = detail.detail ? detail.detail.integrity : null;
  if (!integrity) return emptyState('No integrity verdict', 'The demonstration run has no log here.');
  return el('div', {},
    el('div', { style: 'margin-bottom:12px' }, integrityBadge(integrity.success)),
    el('div', {}, el('span', { class: 'stat-label' }, 'Source hash (sender)'),
      el('div', { class: `hash ${integrity.success ? 'match' : 'mismatch'}` },
        integrity.expected_sha256 || `${MISSING} not recorded`)),
    el('div', { style: 'margin-top:10px' }, el('span', { class: 'stat-label' }, 'Received hash (receiver)'),
      el('div', { class: `hash ${integrity.success ? 'match' : 'mismatch'}` },
        integrity.received_sha256 || `${MISSING} not recorded`)),
    el('p', { class: 'note-inline' },
      'A hash mismatch is always a reported failure, never silent corruption and never '
      + 'presented as success (CC-01).'));
}

function comparisonPanel(detail) {
  if (!detail.comparison.length) {
    return emptyState('The three systems have not been run under one condition here',
      'Step 13 compares the hybrid with pure GBN and pure SR on the same file, condition and '
      + 'seed. `python experiments/demonstrate.py` runs all three.');
  }
  return el('div', {},
    table([
      { label: 'System', render: (row) => modeChip(row.system) },
      { label: 'Completion', numeric: true, render: (row) => seconds(row.completion_time_s, 1) },
      { label: 'Goodput', numeric: true, render: (row) => rate(row.goodput_bytes_per_s) },
      { label: 'Retransmissions', numeric: true, render: (row) => num(row.retransmission_count, 0) },
      { label: 'Overhead', numeric: true, render: (row) => percent(row.retransmission_overhead) },
      { label: 'Switches', numeric: true, render: (row) => num(row.switch_count, 0) },
      { label: 'Integrity', render: (row) => integrityBadge(row.integrity_success) },
    ], detail.comparison),
    el('p', { class: 'note-inline' },
      'One file, one condition, one seed, three systems — the same drop sequence offered to '
      + 'each (RP-04). The headline claim of the project belongs to the matrix, not to one run; '
      + 'this is evidence for the mechanism.'));
}

function progressPanel(store, detail) {
  const { state, run, progress } = store.transfer;
  if (state !== 'idle' && run) {
    const fraction = progress && Number.isFinite(progress.fraction) ? Math.min(1, progress.fraction) : null;
    return el('div', {},
      el('div', { class: 'grid cols-3' },
        stat('State', badge(state, state === 'failed' ? 'bad' : 'info'), { sub: run.run_id }),
        stat('Mode now', progress && progress.current_mode ? modeChip(progress.current_mode, { large: true }) : MISSING,
          { sub: 'live, from the event log' }),
        stat('Elapsed', seconds(run.elapsed_s, 1), { sub: `port ${run.port ?? MISSING}` })),
      el('div', { class: `progress${fraction === null ? ' indeterminate' : ''}`, style: 'margin-top:14px' },
        el('span', { style: fraction === null ? '' : `width:${(fraction * 100).toFixed(1)}%` })),
      el('p', { class: 'note-inline' }, store.transfer.live_lag_note));
  }
  return el('div', {},
    emptyState('No transfer is running right now',
      'The panel below is the recorded rehearsal. To demonstrate live instead, start a hybrid '
      + 'run with a loss schedule from Transfer Control — the "Hybrid, rising loss" preset is '
      + 'the condition this rehearsal used.'),
    detail.detail ? el('div', { style: 'margin-top:14px' }, el('div', { class: 'grid cols-3' },
      stat('Rehearsal run', detail.detail.run_id, { small: true, sub: 'logs/demo/' }),
      stat('Completion', seconds((detail.detail.derived_metrics || {}).completion_time_s, 1), {}),
      stat('Delivered', bytes((detail.detail.derived_metrics || {}).delivered_bytes), {}))) : null);
}

function endpointsPanel(detail) {
  const receiver = detail.detail && detail.detail.receiver_summary;
  return el('div', {},
    el('p', {},
      'The receiver binds first and reports its port; the sender is started only once that line '
      + 'appears. A sender that starts before the socket exists spends its START retry budget on '
      + 'a receiver that was merely slow.'),
    receiver ? kv([
      ['port', receiver.config.port],
      ['final state', receiver.final_state || MISSING],
      ['bytes written', bytes(receiver.metrics.bytes_written)],
      ['duplicates suppressed', receiver.metrics.duplicates_suppressed],
    ]) : emptyState('No receiver log for the rehearsal run', 'Run the demonstration script first.'));
}

function wiresharkPanel(detail) {
  const companion = detail.wireshark;
  if (!companion) return emptyState('No Wireshark companion', 'Select a run first.');
  return el('div', {},
    kv([
      ['Display filter', el('code', {}, companion.filter)],
      ['Dissector', el('code', {}, companion.dissector)],
      ['Transitions at', companion.switch_timestamps.length
        ? companion.switch_timestamps.map((t) => `${t.toFixed(2)}s`).join(', ') : 'none'],
      ['Retransmissions logged', num(companion.retransmission_count, 0)],
    ]),
    companion.reference_captures.length
      ? el('ul', { class: 'note-inline' }, ...companion.reference_captures.map((capture) =>
        el('li', {}, el('code', {}, capture.name), ' — ', capture.expectation || 'recorded evidence')))
      : el('p', { class: 'note-inline' }, 'No captures on this machine.'),
    el('p', { class: 'card-caveat', html:
      '<strong>The dashboard does not inspect packets.</strong> Keep Wireshark open beside this '
      + 'screen; the timestamps above say where to look.' }));
}

const PANELS = {
  endpoints: ['Receiver and sender', endpointsPanel],
  wireshark: ['Wireshark, beside this screen', wiresharkPanel],
  progress: ['The transfer', null],
  mode: ['Mode over time', modePanel],
  network: ['The condition', networkPanel],
  estimator: ['The controller’s reading', estimatorPanel],
  integrity: ['Hashes', integrityPanel],
  comparison: ['Step 13 — against pure GBN and pure SR', comparisonPanel],
};

function panelFor(store, detail) {
  const which = STEP_PANEL[currentStep] || 'mode';
  const [title, builder] = PANELS[which];
  const content = which === 'progress' ? progressPanel(store, detail) : builder(detail);
  return card(title, { note: `step ${currentStep} of 13` }, content);
}

/* ---------------------------------------------------------------- render */

async function loadDetail(store) {
  const runs = payload.runs || [];
  const hybrid = runs.find((run) => /hybrid/i.test(run.run_id)) || runs[0];
  if (!hybrid) return { comparison: [], timeline: null, network: null, detail: null, wireshark: null };

  const [detail, timeline, network, wireshark] = await Promise.all([
    api.run(hybrid.key), api.modes(hybrid.key), api.network(hybrid.key), api.wireshark(hybrid.key),
  ]);

  const comparison = [];
  for (const run of runs) {
    const match = /_(gbn|sr|hybrid|fixed-hybrid)_/i.exec(run.run_id);
    comparison.push({
      system: match ? match[1].toLowerCase() : (run.mode || 'unknown'),
      completion_time_s: run.completion_time_s,
      goodput_bytes_per_s: run.goodput_bytes_per_s,
      retransmission_count: run.retransmission_count,
      retransmission_overhead: null,
      switch_count: run.switch_count,
      integrity_success: run.integrity_success,
    });
  }
  // Overhead is not on the index row, so it is read from each run's derivation
  // rather than estimated — an unavailable number stays unavailable.
  await Promise.all(comparison.map(async (entry, index) => {
    try {
      const full = await api.run(runs[index].key);
      entry.retransmission_overhead = (full.derived_metrics || {}).retransmission_overhead;
    } catch { /* leave it null; the table renders a dash */ }
  }));

  return { detail, timeline, network, wireshark, comparison, key: hybrid.key };
}

async function build(store) {
  const fragment = el('div');

  if (!payload.runs.length) {
    fragment.append(emptyState('The demonstration has not been rehearsed on this machine',
      'Run `python experiments/demonstrate.py`. It performs the thirteen steps, writes '
      + `${payload.transcript_path} and takes a capture. This page presents that run — it does `
      + 'not simulate one.'));
  }

  const detail = await loadDetail(store);

  fragment.append(el('div', { class: 'grid split' },
    card('specs.md §28', {
      note: 'click a step',
    }, stepList(store),
    el('div', { class: 'form-actions' },
      el('button', {
        class: 'secondary', disabled: currentStep <= 1,
        onclick: () => { currentStep = Math.max(1, currentStep - 1); rerender(store); },
      }, '← Previous'),
      el('button', {
        class: 'action', disabled: currentStep >= 13,
        onclick: () => { currentStep = Math.min(13, currentStep + 1); rerender(store); },
      }, 'Next step →'),
      detail.key
        ? el('button', {
          class: 'secondary',
          onclick: () => { store.select(detail.key, { follow: false }); },
        }, 'Point every page at this run')
        : null)),
    el('div', { class: 'grid' },
      panelFor(store, detail),
      card('The rehearsal transcript', {
        note: payload.transcript_path,
      }, payload.transcript
        ? el('details', {}, el('summary', { class: 'note-inline' },
          'Open the recorded transcript in full'),
        el('pre', { class: 'transcript' }, payload.transcript))
        : emptyState('No transcript on disk', `Expected ${payload.transcript_path}.`),
      el('p', { class: 'card-caveat' }, payload.note)))));

  return fragment;
}

async function rerender(store) {
  if (!root) return;
  clear(root).append(await build(store));
}

export async function render({ store }) {
  const outer = el('div');
  outer.append(viewHeader('Final Demonstration',
    'The thirteen steps of specs.md §28 as a guided flow, with the evidence for each step '
    + 'beside it. This presents the run <code>experiments/demonstrate.py</code> recorded — '
    + 'there is no separate demo script, no fabricated event and no animation standing in for '
    + 'a measurement.'));

  try {
    payload = await api.demo();
  } catch (error) {
    outer.append(errorState(error, { title: 'The demonstration could not be loaded' }));
    return outer;
  }
  payload.rehearsal = parseRehearsal(payload.transcript);

  root = el('div', {}, await build(store));
  outer.append(root);
  return outer;
}

/** What the rehearsal recorded for each step, from the transcript's own table. */
function parseRehearsal(transcript) {
  const byStep = {};
  if (!transcript) return byStep;
  for (const line of transcript.split('\n')) {
    const match = /^\|\s*(\d{1,2})\s*\|([^|]*)\|([^|]*)\|/.exec(line.trim());
    if (match) byStep[Number(match[1])] = match[3].trim();
  }
  return byStep;
}

export function onTick({ store }) {
  // Only the live panel moves, and only while a transfer is actually running.
  if (STEP_PANEL[currentStep] === 'progress' && store.transfer.active) rerender(store);
}
