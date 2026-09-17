/* Transfer Overview (T11.4): what the protocol is doing right now, at a glance.
 *
 * Lifecycle states are the ones design.md §7 already defines — STARTING,
 * SENDING, FINISHING, COMPLETE, ERROR for the sender; LISTENING, RECEIVING,
 * FINALIZING, COMPLETE, ERROR for the receiver. No new vocabulary is invented
 * for the screen.
 *
 * Progress, mode and counts are tailed from the run's own events.csv, which is
 * flushed every 64 rows on purpose — so this view is *near*-real-time, and says
 * so rather than hiding it. The flush policy is not to be loosened to make the
 * dashboard smoother: that would trade measurement fidelity for animation.
 */

import { api } from '../api.js';
import {
  el, clear, card, stat, badge, kv, viewHeader, emptyState, errorState, modeChip,
  integrityBadge, bytes, num, seconds, rate, clock, MISSING,
} from '../ui.js';

let body = null;
let cachedKey = null;

const SENDER_STATES = ['IDLE', 'STARTING', 'SENDING', 'SWITCHING', 'FINISHING', 'COMPLETE'];

function lifecycle(current, states) {
  const index = states.indexOf(current);
  return el('div', { class: 'legend', style: 'gap:8px' },
    ...states.map((state, position) => el('span', {
      class: `badge ${state === current ? (state === 'COMPLETE' ? 'ok' : 'info')
        : position < index ? 'neutral' : 'neutral'}`,
      style: position > index && index >= 0 ? 'opacity:.45' : '',
      title: `design.md §7.1 sender state`,
    }, state)));
}

function hashRow(label, value, className) {
  return el('div', {},
    el('span', { class: 'stat-label' }, label),
    el('div', { class: `hash ${className || ''}` }, value || `${MISSING} not recorded`));
}

/* -------------------------------------------------------------- live card */

function liveCard(store) {
  const { state, run, progress } = store.transfer;
  if (state === 'idle' || !run) {
    return card('Live transfer', { note: 'nothing started from this dashboard' },
      emptyState('No transfer is running',
        'Start one from Transfer Control, or pick a recorded run below — every panel on this '
        + 'page works the same way for a finished run as for a live one, because both read '
        + 'the same event log.'));
  }

  const senderState = progress ? progress.sender_state : 'STARTING';
  const fraction = progress && Number.isFinite(progress.fraction) ? Math.min(1, progress.fraction) : null;

  return card('Live transfer', { note: run.run_id },
    el('div', { class: 'grid cols-4' },
      stat('Lifecycle', badge(senderState, state === 'failed' ? 'bad' : state === 'complete' ? 'ok' : 'info'),
        { sub: 'design.md §7.1' }),
      stat('Active mode', progress && progress.current_mode ? modeChip(progress.current_mode, { large: true })
        : el('span', { class: 'stat-value muted small' }, 'not yet recorded'),
        { sub: `requested: ${run.settings.mode}` }),
      stat('Elapsed', seconds(run.elapsed_s, 1), { sub: `port ${run.port ?? MISSING}` }),
      stat('Window', progress ? progress.window_size : MISSING, { sub: 'segments outstanding' })),

    el('div', { style: 'margin-top:16px' },
      el('div', { class: `progress${fraction === null ? ' indeterminate' : ''}` },
        el('span', { style: fraction === null ? '' : `width:${(fraction * 100).toFixed(1)}%` })),
      el('p', { class: 'note-inline' }, progress
        ? `${progress.segments_delivered} / ${progress.total_segments} segments delivered · `
          + `${progress.segments_sent} unique sent · ${progress.segments_acked} ACKs · `
          + `${progress.retransmissions} retransmissions`
        : 'waiting for the first flushed rows')),

    run.error ? el('div', { class: 'card-caveat', style: 'border-color:var(--bad)' },
      el('strong', {}, 'Reported: '), run.error) : null,

    el('p', { class: 'card-caveat' }, store.transfer.live_lag_note));
}

/* --------------------------------------------------------- recorded cards */

function transferCard(detail) {
  const senderSummary = detail.sender_summary || {};
  const senderConfig = senderSummary.config || {};
  const derived = detail.derived_metrics || {};
  const senderMetrics = senderSummary.metrics || {};

  return card('Transfer', { note: `run ${detail.run_id}` },
    el('div', { class: 'grid cols-4' },
      stat('File', bytes(senderConfig.transfer_file_size_bytes),
        { sub: senderSummary.file ? String(senderSummary.file).split(/[\\/]/).pop() : 'source not named in summary' }),
      stat('Segments', num(derived.unique_data_packets, 0),
        { sub: `${num(derived.total_data_transmissions, 0)} transmissions in total` }),
      stat('Completion', seconds(derived.completion_time_s),
        { sub: 'sender clock, to the FIN_ACK verdict' }),
      stat('Goodput', rate(derived.goodput_bytes_per_s),
        { sub: 'delivered application bytes / time' })),
    el('div', { class: 'grid cols-4', style: 'margin-top:16px' },
      stat('Final mode', senderMetrics.final_active_mode
        ? modeChip(senderMetrics.final_active_mode, { large: true })
        : el('span', { class: 'stat-value muted small' }, 'not recorded'),
        { sub: `requested: ${senderSummary.mode || detail.index.requested_mode || MISSING}` }),
      stat('Switches', num(derived.switch_count, 0),
        { sub: `${num(derived.handshake_count, 0)} MODE handshakes` }),
      stat('Retransmissions', num(derived.retransmission_count, 0),
        { sub: `${(Number(derived.retransmission_overhead) * 100 || 0).toFixed(1)}% of bytes sent` }),
      stat('Started', clock(senderSummary.started_wall_clock), { small: true, sub: 'wall clock' })),
    el('div', { style: 'margin-top:14px' }, lifecycle(
      derived.integrity_success === true ? 'COMPLETE' : 'SENDING', SENDER_STATES)));
}

function integrityCard(detail) {
  const integrity = detail.integrity;
  const match = integrity.success;
  const className = match === true ? 'match' : match === false ? 'mismatch' : '';

  return card('Integrity', { note: 'IN-05, CC-01' },
    el('div', { style: 'margin-bottom:12px' }, integrityBadge(match)),
    hashRow('Source hash (sender, over the file it read)', integrity.expected_sha256, className),
    el('div', { style: 'height:10px' }),
    hashRow('Received hash (receiver, over the file it wrote)', integrity.received_sha256, className),
    integrity.verdict_reasons.length
      ? el('p', { class: 'note-inline' }, `FIN_ACK verdict: ${integrity.verdict_reasons.join(', ')}`)
      : null,
    match === null
      ? el('p', { class: 'card-caveat' },
        'No verdict was recorded. That is <strong>not</strong> read as success — a run with no '
        + 'FIN_ACK simply never got one (CC-01).')
      : null,
    match === false
      ? el('p', { class: 'card-caveat', style: 'border-color:var(--bad)' },
        '<strong>The hashes do not match.</strong> This is a reported failure, not a warning: '
        + 'the transfer did not deliver the file it claimed to.')
      : null,
    !integrity.received_sha256 && match !== null
      ? el('p', { class: 'note-inline' },
        'The receiver’s own hash is recorded in its summary.json from T11.4 onward; runs '
        + 'logged before that show only the expected hash.')
      : null);
}

function endpointCard(detail) {
  const sender = detail.sender_summary;
  const receiver = detail.receiver_summary;
  return card('Endpoints', { note: 'two processes, two logs, two clock origins' },
    el('div', { class: 'grid cols-2' },
      el('div', {},
        el('h4', {}, 'Sender'),
        sender ? kv([
          ['events', Object.values(detail.event_counts.sender || {}).reduce((a, b) => a + b, 0)],
          ['duration', seconds(sender.duration_s)],
          ['window', sender.config.window_size],
          ['RTO', `${num(sender.config.rto_s * 1000, 0)} ms`],
          ['seed', sender.config.random_seed],
        ]) : emptyState('No sender log', 'This run directory has no sender/events.csv.')),
      el('div', {},
        el('h4', {}, 'Receiver'),
        receiver ? kv([
          ['events', Object.values(detail.event_counts.receiver || {}).reduce((a, b) => a + b, 0)],
          ['duration', seconds(receiver.duration_s)],
          ['state', receiver.final_state || MISSING],
          ['written', bytes(receiver.metrics.bytes_written)],
          ['duplicates suppressed', receiver.metrics.duplicates_suppressed ?? MISSING],
        ]) : emptyState('No receiver log',
          'A receiver that never heard from anyone still writes one, so this means the '
          + 'process never started or its logs are elsewhere.'))),
    el('p', { class: 'note-inline' },
      'Each log’s timestamps are seconds from <em>that endpoint’s</em> start, so they are '
      + 'interleaved by aligning on the shared <code>START</code> row and never subtracted from '
      + 'one another (specs.md §21).'));
}

/* ----------------------------------------------------------------- render */

async function renderBody(store) {
  const fragment = el('div', { class: 'grid' });
  fragment.append(liveCard(store));

  if (!store.selectedKey) {
    fragment.append(emptyState('No run selected',
      'No run directory was found under logs/. Start a transfer, or run the CLI, and it will '
      + 'appear here as soon as it writes an event log.'));
    return fragment;
  }

  try {
    const detail = await api.run(store.selectedKey);
    cachedKey = store.selectedKey;
    fragment.append(transferCard(detail));
    fragment.append(el('div', { class: 'grid cols-2' },
      integrityCard(detail), endpointCard(detail)));
  } catch (error) {
    fragment.append(errorState(error, { title: 'That run could not be read' }));
  }
  return fragment;
}

export async function render({ store }) {
  const root = el('div');
  root.append(viewHeader('Transfer Overview',
    'The lifecycle, the progress and the integrity verdict for the selected run. '
    + 'A live transfer and a finished one are shown the same way, because both are read '
    + 'from the same <code>events.csv</code>.'));
  body = el('div', {}, await renderBody(store));
  root.append(body);
  return root;
}

let refreshing = false;

export async function onTick({ store }) {
  if (!body || refreshing) return;
  refreshing = true;
  try {
    // Re-read only while something can still change, or when the selection moved.
    if (store.transfer.active || store.selectedKey !== cachedKey) {
      clear(body).append(await renderBody(store));
    } else {
      const live = body.firstElementChild;
      if (live) live.replaceWith(liveCard(store));
    }
  } finally {
    refreshing = false;
  }
}
