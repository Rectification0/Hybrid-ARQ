/* Transfer Control (T11.3): configure and launch a run without editing source.
 *
 * The form's fields are exactly the ones the server declares editable, and the
 * frozen values sit beside them as read-only context. That split is the point:
 * a run's window, impairment and seed are per-experiment inputs, while the
 * segment size, the thresholds, the hysteresis count and the RTO policy are
 * frozen against recorded evidence (specs.md §16). Making one of those a form
 * field would invite an evaluator to change, in a browser, a value whose change
 * invalidates every experiment in the repository.
 *
 * Validation happens on the server — one implementation, testable — and its
 * refusal is shown as the sentence it is, never as a stack trace.
 */

import { api, ApiError } from '../api.js';
import {
  el, clear, card, stat, badge, kv, viewHeader, errorState, emptyState, modeChip, num,
  seconds, MISSING
} from '../ui.js';

let configuration = null;
let lastError = null;
let statusNode = null;
let formNode = null;

const FIELD_ORDER = [
  ['Transfer', ['mode', 'file_kib', 'window']],
  ['Endpoint', ['host', 'port']],
  ['Impairment (network/simulator.py)', ['loss', 'ack_loss', 'rtt', 'jitter', 'seed', 'loss_schedule']],
];

function field(name, spec, value) {
  const id = `field-${name}`;
  let input;
  if (spec.type === 'choice') {
    input = el('select', { id, name },
      ...spec.choices.map((choice) => el('option', { value: choice, selected: choice === value }, choice)));
  } else if (spec.type === 'text') {
    input = el('input', { id, name, type: 'text', value: value ?? spec.default, placeholder: spec.note || '' });
  } else {
    input = el('input', {
      id, name, type: 'number', value: value ?? spec.default,
      min: spec.min, max: spec.max, step: spec.type === 'int' ? 1 : 'any',
    });
  }
  return el('label', { class: 'field', for: id },
    el('span', {}, spec.label),
    input,
    spec.note ? el('span', { class: 'field-note' }, spec.note) : null);
}

function readForm() {
  const settings = {};
  for (const [name, spec] of Object.entries(configuration.editable)) {
    const input = formNode.elements[name];
    if (!input) continue;
    const raw = input.value;
    if (spec.type === 'int') settings[name] = raw === '' ? spec.default : Number(raw);
    else if (spec.type === 'float') settings[name] = raw === '' ? spec.default : Number(raw);
    else settings[name] = raw;
  }
  return settings;
}

/* ---------------------------------------------------------------- presets */

/* Conditions taken straight from the recorded matrix (specs.md §17.1, §17.3),
 * so a demonstration reproduces a cell that has already been measured rather
 * than an ad-hoc setting nobody has results for. */
const PRESETS = [
  { name: 'Clean baseline', settings: { mode: 'hybrid', loss: 0, ack_loss: 0, rtt: 0, jitter: 0, file_kib: 256 },
    note: 'E1 at 0% loss — the control every other condition is read against' },
  { name: 'GBN at 10% loss', settings: { mode: 'gbn', loss: 0.10, rtt: 20, file_kib: 256 },
    note: 'the range retransmission, as recorded in lossy_gbn_range_retx.pcapng' },
  { name: 'SR at 10% loss', settings: { mode: 'sr', loss: 0.10, rtt: 20, file_kib: 256 },
    note: 'the same seed and condition, one segment resent at a time' },
  { name: 'Hybrid, rising loss', settings: { mode: 'hybrid', loss: 0, rtt: 20, file_kib: 512, loss_schedule: '4:0.10' },
    note: 'E8 rising: clean, then 10% from 4 s — the GBN→SR transition' },
  { name: 'Hybrid, falling loss', settings: { mode: 'hybrid', loss: 0.10, rtt: 20, file_kib: 512, loss_schedule: '8:0.0' },
    note: 'E8 falling: 10%, then clean from 8 s — the SR→GBN transition after hysteresis' },
  { name: 'Fixed-hybrid control', settings: { mode: 'fixed-hybrid', loss: 0.10, rtt: 20, file_kib: 256 },
    note: 'pays the drain cost without changing mode; its SWITCH rows read FIXED_HYBRID_NOOP' },
];

function applyPreset(settings) {
  for (const [name, value] of Object.entries(settings)) {
    const input = formNode.elements[name];
    if (input) input.value = value;
  }
}

/* ----------------------------------------------------------------- status */

function renderStatus(store) {
  const { state, run, progress } = store.transfer;
  const node = el('div');

  if (store.transferError) {
    node.append(errorState(store.transferError, { title: 'Could not read the transfer status' }));
    return node;
  }

  if (state === 'idle' || !run) {
    node.append(emptyState(
      'No transfer has been started from this dashboard',
      'Configure a run and press Start. Runs launched from a terminal are not tracked here, '
      + 'but they appear in the run picker and every recorded view as soon as they write a log.'));
    return node;
  }

  const tone = { complete: 'ok', failed: 'bad', stopped: 'warn' }[state] || 'info';
  node.append(el('div', { class: 'grid cols-3' },
    stat('State', badge(state, tone), { sub: run.run_id }),
    stat('UDP port', run.port ?? MISSING, { sub: `Wireshark: udp.port == ${run.port ?? '?'}` }),
    stat('Elapsed', seconds(run.elapsed_s, 1), { sub: `RTO ${num(run.rto_s * 1000, 0)} ms, derived by D7` })));

  if (progress) {
    const fraction = Number.isFinite(progress.fraction) ? Math.min(1, progress.fraction) : null;
    node.append(el('div', { style: 'margin-top:14px' },
      el('div', { class: `progress${fraction === null ? ' indeterminate' : ''}` },
        el('span', { style: fraction === null ? '' : `width:${(fraction * 100).toFixed(1)}%` })),
      el('p', { class: 'note-inline' },
        `${progress.segments_delivered} of ${progress.total_segments} segments delivered`
        + ` · ${progress.segments_sent} sent · ${progress.retransmissions} retransmitted`)));
  }

  if (run.error) {
    node.append(el('div', { class: 'error-state', style: 'margin-top:14px' },
      el('h3', {}, state === 'stopped' ? 'Stopped' : 'The transfer did not complete'),
      el('p', {}, run.error),
      run.stderr && run.stderr.sender.length
        ? el('details', {}, el('summary', {}, "The sender's own output, as logged"),
          el('pre', {}, run.stderr.sender.join('\n')))
        : null));
  }

  node.append(el('details', { style: 'margin-top:14px' },
    el('summary', { class: 'note-inline' }, 'The exact commands this dashboard ran'),
    el('pre', { class: 'transcript' },
      Object.entries(run.commands || {})
        .map(([name, argv]) => `# ${name}\n${argv.join(' ')}`).join('\n\n')
      || 'not recorded'),
    el('p', { class: 'note-inline' },
      'These are the project’s own CLI entry points. A run started here is the same run '
      + 'as one typed into a terminal, writing the same logs through the same emitter.')));
  return node;
}

/* ----------------------------------------------------------------- render */

export async function render({ store }) {
  configuration = await api.config();
  const root = el('div');

  root.append(viewHeader('Transfer Control',
    'Configure and launch a transfer over the real protocol. The dashboard builds the same '
    + '<code>sender.py</code> / <code>receiver.py</code> command lines an operator would type '
    + 'and runs them; it adds no way of its own to move a file.'));

  const preflightIssues = store.health
    ? store.health.preflight.checks.filter((check) => !check.ok) : [];

  formNode = el('form', {
    onsubmit: async (event) => {
      event.preventDefault();
      lastError = null;
      const button = formNode.querySelector('button.action');
      button.disabled = true;
      button.textContent = 'Starting…';
      try {
        await store.start(readForm());
        location.hash = '#/overview';
      } catch (error) {
        lastError = error instanceof ApiError ? error : new ApiError(String(error));
        refresh(store);
      } finally {
        button.disabled = false;
        button.textContent = 'Start transfer';
      }
    },
  });

  for (const [group, names] of FIELD_ORDER) {
    const set = el('fieldset', {}, el('legend', {}, group));
    const grid = el('div', { class: 'form-grid' });
    for (const name of names) {
      const spec = configuration.editable[name];
      if (spec) grid.append(field(name, spec, spec.default));
    }
    set.append(grid);
    formNode.append(set);
  }

  const busy = store.transfer.active;
  formNode.append(el('div', { class: 'form-actions' },
    el('button', { class: 'action', type: 'submit', disabled: busy || !store.health },
      busy ? 'A transfer is running' : 'Start transfer'),
    el('button', {
      class: 'action danger', type: 'button', disabled: !busy,
      onclick: async () => {
        try { await store.stop(); } catch (error) { lastError = error; }
        refresh(store);
      },
    }, 'Stop'),
    el('button', {
      class: 'secondary', type: 'button', disabled: busy,
      onclick: async () => {
        try { await store.reset(); } catch (error) { lastError = error; }
        refresh(store);
      },
    }, 'Clear status'),
    el('span', { class: 'note-inline' },
      'Stopping is safe: logs are append-only and flushed as they go, so an abandoned '
      + 'transfer leaves a partial but valid log rather than a corrupt one.')));

  const presetList = el('div', { class: 'form-grid' },
    ...PRESETS.map((preset) => el('button', {
      class: 'secondary', type: 'button', title: preset.note,
      onclick: () => applyPreset(preset.settings),
    }, preset.name)));

  statusNode = el('div', {}, renderStatus(store));

  root.append(el('div', { class: 'grid split' },
    el('div', { class: 'grid' },
      card('Configuration', { note: 'every field is validated on the server' },
        lastError ? errorState(lastError, { title: 'That configuration was refused' }) : null,
        !store.health ? errorState(store.healthError, { title: 'Backend unreachable' }) : null,
        preflightIssues.length
          ? el('div', { class: 'card-caveat' },
            el('strong', {}, 'Preflight: '),
            preflightIssues.map((check) => `${check.name} — ${check.detail}`).join('; '))
          : null,
        formNode),
      card('Recorded conditions', { note: 'from specs.md §17' },
        el('p', { class: 'note-inline', style: 'margin-top:0' },
          'Load a condition the matrix already measured, so a demonstration reproduces a cell '
          + 'with recorded results rather than an ad-hoc setting.'),
        presetList)),

    el('div', { class: 'grid' },
      card('Current transfer', { note: 'state is read back from the run’s own event log' }, statusNode),
      card('Frozen — shown, not editable', { note: 'specs.md §16, design.md §12' },
        el('p', { class: 'note-inline', style: 'margin-top:0' },
          'Each of these is frozen against recorded evidence. Changing one invalidates '
          + 'experiments that have already been run, so none of them is a form field.'),
        kv(configuration.read_only.map((entry) => [
          entry.name,
          el('span', {}, entry.value, el('span', { class: 'stat-sub' }, ` — ${entry.why}`)),
        ])),
        el('p', { class: 'card-caveat', html:
          `<strong>The thresholds are readings of the estimator, not loss rates.</strong> `
          + `D8 over-reads loss under GBN by four to five times, so SWITCH_HIGH = `
          + `${configuration.snapshot.switch_high} fires at roughly 2% physical loss.` }),
        el('p', { class: 'note-inline' }, configuration.rto_note)),
      card('Modes', { note: 'specs.md §18' },
        el('div', { class: 'grid' },
          ...['gbn', 'sr', 'hybrid', 'fixed-hybrid'].map((mode) =>
            el('div', {}, modeChip(mode), ' ',
              el('span', { class: 'stat-sub' }, {
                gbn: 'A measured baseline.',
                sr: 'A measured baseline.',
                hybrid: 'The system under evaluation.',
                'fixed-hybrid': 'The switching-overhead control.',
              }[mode])))),
        el('p', { class: 'note-inline' },
          'Stop-and-wait (<code>--mode saw</code>) exists because Phase 2 needed to move data '
          + 'before either real mode was written. It is not a system under evaluation and is '
          + 'not offered here.')))));

  return root;
}

function refresh(store) {
  if (statusNode) clear(statusNode).append(renderStatus(store));
}

/** Called on every poll while this view is open; only the status block moves. */
export function onTick({ store }) {
  refresh(store);
  const start = formNode && formNode.querySelector('button.action');
  const stop = formNode && formNode.querySelector('button.action.danger');
  if (start) start.disabled = store.transfer.active || !store.health;
  if (stop) stop.disabled = !store.transfer.active;
}
