/* Experiment History / run browser (T11.11).
 *
 * Two indexes, deliberately kept apart:
 *
 *  - `experiment_runs.csv` is the recorded matrix — 195 runs with the condition,
 *    seed, status and metrics each one produced. It is the reproducibility
 *    record (RP-01, RP-02) and it is read, never rewritten.
 *  - `logs/` is every run present on this machine, including the ones started
 *    from this dashboard and from a terminal.
 *
 * A matrix row whose log directory is not on this checkout says so rather than
 * linking to nothing: the CSV records absolute paths from the machine that ran
 * it, and a clone will not have them.
 */

import { api } from '../api.js';
import {
  el, clear, card, stat, badge, viewHeader, emptyState, errorState, modeChip,
  integrityBadge, table, num, bytes, rate, seconds, percent, clock, MISSING,
} from '../ui.js';

const filters = { system: '', experiment: '', status: '', search: '' };

const num0 = (value) => num(value, 0);

function open(store, key) {
  store.select(key, { follow: false });
  location.hash = '#/overview';
}

/* ------------------------------------------------------------ the matrix */

function matrixCard(payload, store) {
  if (!payload.exists || !payload.rows.length) {
    return card('Recorded experiment matrix', {},
      emptyState('No experiment index on disk',
        `Expected ${payload.path}. Run \`python experiments/run_experiment.py\` to produce it. `
        + 'Nothing is invented in its absence.'));
  }

  const rows = payload.rows.filter((row) => {
    if (filters.system && row.system !== filters.system) return false;
    if (filters.experiment && row.experiment !== filters.experiment) return false;
    if (filters.status && row.status !== filters.status) return false;
    if (filters.search && !row.run_id.toLowerCase().includes(filters.search.toLowerCase())) return false;
    return true;
  });

  const distinct = (column) => [...new Set(payload.rows.map((row) => row[column]))].sort();

  const select = (name, label, options) => el('label', { class: 'field' },
    el('span', {}, label),
    el('select', {
      onchange: (event) => { filters[name] = event.target.value; rerender(store); },
    }, el('option', { value: '', selected: !filters[name] }, `all`),
    ...options.map((option) => el('option', { value: option, selected: filters[name] === option }, option))));

  const failures = payload.rows.filter((row) => row.status !== 'ok').length;
  const integrityFailures = payload.rows.filter((row) => row.integrity_success !== 'True').length;

  return card('Recorded experiment matrix', {
    note: `${payload.rows.length} runs from ${payload.path}`,
  },
  el('div', { class: 'grid cols-4' },
    stat('Runs recorded', num0(payload.rows.length), { sub: 'one row per transfer' }),
    stat('Non-ok status', num0(failures),
      { sub: 'aborted or failed', tone: failures ? 'warn' : 'ok' }),
    stat('Without an integrity pass', num0(integrityFailures),
      { sub: 'a mismatch is never softened', tone: integrityFailures ? 'bad' : 'ok' }),
    stat('Showing', num0(rows.length), { sub: 'after filters' })),

  el('div', { class: 'toolbar', style: 'margin-top:16px' },
    select('experiment', 'Experiment', distinct('experiment')),
    select('system', 'System', distinct('system')),
    select('status', 'Status', distinct('status')),
    el('label', { class: 'field grow' }, el('span', {}, 'Run id contains'),
      el('input', {
        type: 'text', value: filters.search, placeholder: 'E5_hybrid',
        oninput: (event) => { filters.search = event.target.value; rerender(store); },
      }))),

  table([
    { label: 'Run id', render: (row) => el('code', {}, row.run_id) },
    { label: 'Exp', render: (row) => row.experiment },
    { label: 'Condition', render: (row) => row.condition },
    { label: 'System', render: (row) => modeChip(row.system) },
    { label: 'Trial', numeric: true, render: (row) => row.trial },
    { label: 'Loss', numeric: true, render: (row) => percent(Number(row.loss_rate)) },
    { label: 'RTT', numeric: true, render: (row) => num(Number(row.rtt_ms), 0, 'ms') },
    { label: 'Seed', numeric: true, render: (row) => row.seed },
    { label: 'File', numeric: true, render: (row) => bytes(Number(row.file_bytes)) },
    { label: 'Status', render: (row) => badge(row.status, row.status === 'ok' ? 'ok' : 'bad') },
    { label: 'Completion', numeric: true, render: (row) => seconds(Number(row.completion_time_s), 1) },
    { label: 'Goodput', numeric: true, render: (row) => rate(Number(row.goodput_bytes_per_s)) },
    { label: 'Retx', numeric: true, render: (row) => num0(Number(row.retransmission_count)) },
    { label: 'Switches', numeric: true, render: (row) => num0(Number(row.switch_count)) },
    { label: 'Final mode', render: (row) => (row.final_mode ? modeChip(row.final_mode) : MISSING) },
    { label: 'Integrity',
      render: (row) => integrityBadge(row.integrity_success === 'True' ? true
        : row.integrity_success === 'False' ? false : null) },
    { label: 'Logs', render: (row) => (row.log_key
      ? el('button', { class: 'secondary', onclick: () => open(store, row.log_key) }, 'open')
      : el('span', { class: 'stat-sub' }, 'not on this machine')) },
  ], rows, { maxHeight: '58vh' }),

  el('p', { class: 'note-inline' },
    'Every row carries the seed its condition was derived from, so a run can be replayed rather '
    + 'than taken on trust (RP-03). A row whose logs are not on this machine records an absolute '
    + 'path from the machine that produced it — the result stands; only the raw log is elsewhere.'));
}

/* ------------------------------------------------------------ local logs */

function logsCard(runs, store) {
  if (!runs.length) {
    return card('Runs on this machine', {},
      emptyState('No run directories under logs/',
        'A run appears here as soon as either endpoint writes an events.csv.'));
  }
  return card('Runs on this machine', { note: `${runs.length} under logs/` },
    table([
      { label: 'Run id', render: (row) => el('code', {}, row.run_id) },
      { label: 'Group', render: (row) => row.group || '(top level)' },
      { label: 'Mode', render: (row) => (row.mode ? modeChip(row.mode) : MISSING) },
      { label: 'Started', render: (row) => clock(row.started_wall_clock) },
      { label: 'Completion', numeric: true, render: (row) => seconds(row.completion_time_s, 1) },
      { label: 'Goodput', numeric: true, render: (row) => rate(row.goodput_bytes_per_s) },
      { label: 'Retx', numeric: true, render: (row) => num0(row.retransmission_count) },
      { label: 'Switches', numeric: true, render: (row) => num0(row.switch_count) },
      { label: 'Integrity', render: (row) => integrityBadge(row.integrity_success) },
      { label: 'Logs', render: (row) => (row.endpoints || []).join(' + ') },
      { label: '', render: (row) => el('button', {
        class: 'secondary', onclick: () => open(store, row.key),
      }, 'open') },
    ], runs, { maxHeight: '48vh' }),
    el('p', { class: 'note-inline' },
      'Opening a run points every other page at it — the overview, the mode timeline, the event '
      + 'log, the retransmission picture and the metrics all read that one transfer.'));
}

let root = null;

async function rerender(store) {
  if (!root) return;
  clear(root).append(await build(store));
}

async function build(store) {
  const fragment = el('div', { class: 'grid' });
  let matrix;
  try {
    matrix = await api.experimentRuns();
  } catch (error) {
    fragment.append(errorState(error, { title: 'The experiment index could not be read' }));
    return fragment;
  }
  fragment.append(matrixCard(matrix, store));
  fragment.append(logsCard(store.runs, store));
  return fragment;
}

export async function render({ store }) {
  const outer = el('div');
  outer.append(viewHeader('Experiment History',
    'Every recorded run, browsable without re-running one. The matrix index is '
    + '<code>experiments/results/experiment_runs.csv</code>; the second table is what is on '
    + 'this machine under <code>logs/</code>.'));
  root = el('div', {}, await build(store));
  outer.append(root);
  return outer;
}
