/* GBN vs SR vs Hybrid (T11.10), over the recorded matrix.
 *
 * Everything here is `aggregate.csv` — the mean and standard deviation across
 * each cell's five trials, produced by experiments/analyze_results.py. Nothing
 * is re-averaged in the browser and nothing is re-run.
 *
 * The rule that shapes this page: **results that reflect badly on the hybrid
 * are shown with the rest.** The oscillation at 1–2% loss and the cells where
 * the hybrid loses to SR are part of the result (H-05), so they are given their
 * own panel rather than left for a reader to find in a table.
 */

import { api } from '../api.js';
import {
  el, card, badge, viewHeader, emptyState, errorState, modeChip, modeColor, table, num,
  percent, seconds, legend
} from '../ui.js';
import { groupedBars, lineChart } from '../chart.js';

const SYSTEMS = ['gbn', 'sr', 'hybrid', 'fixed-hybrid'];
const LOSS_EXPERIMENTS = ['E1', 'E2', 'E3', 'E4', 'E5', 'E6'];

const value = (row, column) => {
  const raw = row && row[column];
  const number = Number(raw);
  return raw === '' || raw === undefined || Number.isNaN(number) ? null : number;
};

function byCondition(rows, experiments) {
  const conditions = [];
  for (const row of rows) {
    if (experiments && !experiments.includes(row.experiment)) continue;
    const key = `${row.experiment}/${row.condition}`;
    let entry = conditions.find((c) => c.key === key);
    if (!entry) {
      entry = {
        key,
        experiment: row.experiment,
        condition: row.condition,
        loss: value(row, 'loss_rate'),
        rtt: value(row, 'rtt_ms'),
        schedule: row.loss_schedule,
        systems: {},
      };
      conditions.push(entry);
    }
    entry.systems[row.system] = row;
  }
  return conditions;
}

function seriesFor(conditions, column, { systems = SYSTEMS } = {}) {
  return systems
    .filter((system) => conditions.some((c) => c.systems[system]))
    .map((system) => ({
      label: system,
      color: modeColor(system),
      dashed: system === 'fixed-hybrid',
      values: conditions.map((c) => value(c.systems[system], column)),
      points: conditions.map((c) => ({
        x: c.loss ?? c.rtt ?? 0, y: value(c.systems[system], column),
      })).filter((point) => point.y !== null),
    }));
}

/* ------------------------------------------------------------ the losses */

/**
 * Cells where the hybrid did *worse* than the better fixed strategy.
 * Read straight out of the aggregated means — this is a comparison of recorded
 * numbers, labelled as a presentation-only calculation.
 */
function hybridShortfalls(conditions) {
  const found = [];
  for (const condition of conditions) {
    const hybrid = value(condition.systems.hybrid, 'goodput_kib_per_s_mean');
    if (hybrid === null) continue;
    for (const rival of ['gbn', 'sr']) {
      const other = value(condition.systems[rival], 'goodput_kib_per_s_mean');
      if (other === null || other <= hybrid) continue;
      found.push({
        condition, rival, hybrid, other,
        deficit: (other - hybrid) / other,
        switches: value(condition.systems.hybrid, 'switch_count_mean'),
        spread: value(condition.systems.hybrid, 'switch_count_sd'),
      });
    }
  }
  return found.sort((a, b) => b.deficit - a.deficit);
}

function shortfallCard(conditions) {
  const shortfalls = hybridShortfalls(conditions);
  const oscillating = conditions
    .map((condition) => ({
      condition,
      switches: value(condition.systems.hybrid, 'switch_count_mean'),
      spread: value(condition.systems.hybrid, 'switch_count_sd'),
    }))
    .filter((entry) => entry.switches !== null && entry.switches > 1.5)
    .sort((a, b) => b.switches - a.switches);

  return card('Where the hybrid is worse', {
    note: 'H-05 — a result, not an omission',
    className: 'span-2',
  },
  el('p', { style: 'margin-top:0' },
    'A fair evaluation reports the conditions under which the adaptive strategy loses. '
    + 'These are read from the same aggregated means as the figures above; the deficit is a '
    + 'presentation-only calculation over two recorded numbers.'),

  shortfalls.length
    ? table([
      { label: 'Condition', render: (row) => `${row.condition.experiment} ${row.condition.condition}` },
      { label: 'Loss', numeric: true, render: (row) => percent(row.condition.loss) },
      { label: 'Beaten by', render: (row) => modeChip(row.rival) },
      { label: 'Hybrid goodput', numeric: true, render: (row) => num(row.hybrid, 2, 'KiB/s') },
      { label: 'Its goodput', numeric: true, render: (row) => num(row.other, 2, 'KiB/s') },
      { label: 'Shortfall', numeric: true, render: (row) => badge(percent(row.deficit), 'warn') },
      { label: 'Hybrid switches', numeric: true,
        render: (row) => `${num(row.switches, 1)} ± ${num(row.spread, 1)}` },
    ], shortfalls)
    : emptyState('No cell in this matrix has the hybrid behind both fixed strategies',
      'That is what the aggregate says for these conditions; it is not a claim about every '
      + 'condition. The oscillation below is the cost that shows up instead.'),

  oscillating.length
    ? el('div', { style: 'margin-top:16px' },
      el('h4', {}, 'Oscillation: switching where switching buys least'),
      table([
        { label: 'Condition', render: (row) => `${row.condition.experiment} ${row.condition.condition}` },
        { label: 'Loss', numeric: true, render: (row) => percent(row.condition.loss) },
        { label: 'Switches per transfer', numeric: true,
          render: (row) => `${num(row.switches, 1)} ± ${num(row.spread, 1)}` },
        { label: 'Reading', wrap: true, render: (row) => (row.switches > 3
          ? 'A static condition justifies at most one transition. This many, with a spread this '
            + 'wide, is an unstable decision rather than an adaptive one.'
          : 'More transitions than a static condition can justify.') },
      ], oscillating))
    : null,

  el('p', { class: 'card-caveat', html:
      'The full account of every setting under which the hybrid was worse than a fixed strategy '
      + 'is in <code>experiments/results/calibration.md</code> §5. That section is the honest '
      + 'core of the evaluation, not an appendix.' }));
}

/* ---------------------------------------------------------------- render */

function matrixTable(conditions) {
  const rows = [];
  for (const condition of conditions) {
    for (const system of SYSTEMS) {
      const row = condition.systems[system];
      if (row) rows.push({ condition, system, row });
    }
  }
  return table([
    { label: 'Exp', render: (entry) => entry.condition.experiment },
    { label: 'Condition', render: (entry) => entry.condition.condition },
    { label: 'Loss', numeric: true, render: (entry) => percent(entry.condition.loss) },
    { label: 'RTT', numeric: true, render: (entry) => num(entry.condition.rtt, 0, 'ms') },
    { label: 'System', render: (entry) => modeChip(entry.system) },
    { label: 'Trials', numeric: true, render: (entry) => entry.row.trials_aggregated },
    { label: 'Goodput', numeric: true,
      render: (entry) => `${num(value(entry.row, 'goodput_kib_per_s_mean'), 2)} ± ${num(value(entry.row, 'goodput_kib_per_s_sd'), 2)}` },
    { label: 'Completion', numeric: true,
      render: (entry) => `${num(value(entry.row, 'completion_time_s_mean'), 1)} s` },
    { label: 'Retx', numeric: true,
      render: (entry) => num(value(entry.row, 'retransmission_count_mean'), 0) },
    { label: 'Overhead', numeric: true,
      render: (entry) => percent(value(entry.row, 'retransmission_overhead_mean')) },
    { label: 'Switches', numeric: true,
      render: (entry) => num(value(entry.row, 'switch_count_mean'), 1) },
    { label: 'SR residence', numeric: true,
      render: (entry) => percent(value(entry.row, 'sr_residence_fraction_mean')) },
    { label: 'Integrity failures', numeric: true,
      render: (entry) => (Number(entry.row.integrity_failures)
        ? badge(entry.row.integrity_failures, 'bad') : '0') },
  ], rows, { maxHeight: '56vh' });
}

function figureCards(figures) {
  if (!figures.exists) {
    return emptyState('No figures have been generated',
      'Run `python experiments/analyze_results.py` to produce plots/ from the recorded matrix. '
      + 'Nothing is drawn here by hand.');
  }
  return el('div', { class: 'grid cols-2' },
    ...figures.figures.map((figure) => card(figure.name, {
      note: figure.required ? 'required by §27' : 'optional (T9.3)',
      className: 'figure-card',
    },
    el('img', { src: figure.url, alt: figure.shows || figure.name, loading: 'lazy' }),
    figure.shows ? el('p', { class: 'note-inline' }, el('strong', {}, 'Shows: '), figure.shows) : null,
    figure.reading ? el('p', { class: 'note-inline' }, el('strong', {}, 'The reading: '), figure.reading) : null)));
}

export async function render({ store }) {
  const root = el('div');
  root.append(viewHeader('GBN vs SR vs Hybrid',
    'The recorded matrix, read from <code>experiments/results/aggregate.csv</code>: one row per '
    + '(experiment, condition, system), the mean and standard deviation across that cell’s '
    + 'five trials. Runs whose integrity check failed are excluded from every mean and counted '
    + 'separately (CC-01).'));

  let aggregate;
  let figures;
  try {
    [aggregate, figures] = await Promise.all([api.aggregate(), api.figures()]);
  } catch (error) {
    root.append(errorState(error, { title: 'The aggregated results could not be read' }));
    return root;
  }

  if (!aggregate.exists || !aggregate.rows.length) {
    root.append(emptyState('No aggregated results on disk',
      `Expected ${aggregate.path}. Run the matrix with \`python experiments/run_experiment.py\` `
      + 'and aggregate it with `python experiments/analyze_results.py`. Nothing on this page is '
      + 'synthesised in its absence.'));
    return root;
  }

  const lossConditions = byCondition(aggregate.rows, LOSS_EXPERIMENTS)
    .sort((a, b) => (a.loss ?? 0) - (b.loss ?? 0));
  const rttConditions = byCondition(aggregate.rows, ['E7'])
    .sort((a, b) => (a.rtt ?? 0) - (b.rtt ?? 0));
  const dynamicConditions = byCondition(aggregate.rows, ['E8']);

  const labels = lossConditions.map((c) => percent(c.loss, 0));

  root.append(el('div', { class: 'grid cols-2' },
    card('Goodput by loss', { note: 'mean over five trials, KiB/s' },
      groupedBars(labels, seriesFor(lossConditions, 'goodput_kib_per_s_mean'),
        { yLabel: 'Goodput (KiB/s)', xLabel: 'Configured forward loss', yFormat: (v) => v.toFixed(0) }),
      legend(SYSTEMS.map((system) => [system, modeColor(system)]))),
    card('Retransmissions by loss', { note: 'mean count over five trials' },
      groupedBars(labels, seriesFor(lossConditions, 'retransmission_count_mean'),
        { yLabel: 'Retransmissions', xLabel: 'Configured forward loss', yFormat: (v) => v.toFixed(0) }),
      el('p', { class: 'note-inline' },
        'GBN and fixed-hybrid sitting on one another is the control doing its job: the hybrid’s '
        + 'advantage cannot be attributed to anything the controller does except changing mode.')),
    card('Completion time by loss', { note: 'mean seconds' },
      groupedBars(labels, seriesFor(lossConditions, 'completion_time_s_mean'),
        { yLabel: 'Completion time (s)', xLabel: 'Configured forward loss', yFormat: (v) => v.toFixed(0) })),
    card('Switches and SR residence by loss', { note: 'hybrid only' },
      lineChart([
        { label: 'switches per transfer', color: 'var(--hybrid)',
          points: lossConditions.map((c) => ({ x: c.loss, y: value(c.systems.hybrid, 'switch_count_mean') })).filter((p) => p.y !== null) },
        { label: 'fraction of transfer in SR', color: 'var(--sr)',
          points: lossConditions.map((c) => ({ x: c.loss, y: value(c.systems.hybrid, 'sr_residence_fraction_mean') })).filter((p) => p.y !== null) },
      ], { width: 700, height: 240, yMin: 0, xLabel: 'Configured forward loss', yLabel: 'Switches / fraction',
        xFormat: (v) => `${(v * 100).toFixed(0)}%`, yFormat: (v) => v.toFixed(1) }),
      el('p', { class: 'note-inline' },
        'The switching peak sits where the benefit is lowest. That is the failure the evaluation '
        + 'reports, not a detail to bury.'))));

  root.append(shortfallCard(lossConditions));

  if (rttConditions.length) {
    root.append(card('Goodput across RTT (E7, at 5% loss)', { note: 'a fiftyfold RTT range' },
      groupedBars(rttConditions.map((c) => `${num(c.rtt, 0)} ms`),
        seriesFor(rttConditions, 'goodput_kib_per_s_mean', { systems: ['gbn', 'sr', 'hybrid'] }),
        { yLabel: 'Goodput (KiB/s)', xLabel: 'Emulated RTT', yFormat: (v) => v.toFixed(0) }),
      el('p', { class: 'note-inline' },
        'Near-parallel lines across the range: the switching decision does not depend on delay.')));
  }

  if (dynamicConditions.length) {
    root.append(card('Dynamic loss (E8)', { note: 'hybrid under a loss schedule' },
      table([
        { label: 'Condition', render: (c) => c.condition },
        { label: 'Schedule', wrap: true, render: (c) => el('code', {}, c.schedule || 'static') },
        { label: 'Switches', numeric: true,
          render: (c) => `${num(value(c.systems.hybrid, 'switch_count_mean'), 1)} ± ${num(value(c.systems.hybrid, 'switch_count_sd'), 1)}` },
        { label: 'SR residence', numeric: true,
          render: (c) => percent(value(c.systems.hybrid, 'sr_residence_fraction_mean')) },
        { label: 'Goodput', numeric: true,
          render: (c) => num(value(c.systems.hybrid, 'goodput_kib_per_s_mean'), 2, 'KiB/s') },
      ], dynamicConditions),
      el('p', { class: 'note-inline' },
        'The mode-selection timeline for these conditions is the figure '
        + '<code>mode_selection_over_time.png</code> below — one band per trial, with the '
        + 'scheduled loss changes marked.')));
  }

  root.append(card('The recorded figures', {
    note: 'generated by experiments/analyze_results.py, never edited by hand',
  }, figureCards(figures)));

  root.append(card('The whole aggregate', { note: `${aggregate.rows.length} rows from ${aggregate.path}` },
    matrixTable(byCondition(aggregate.rows, null))));

  return root;
}
