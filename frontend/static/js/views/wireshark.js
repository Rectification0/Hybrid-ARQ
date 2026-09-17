/* Wireshark companion (T11.12).
 *
 * This panel moves an evaluator from the dashboard to Wireshark. **It does not
 * inspect packets itself**, and it says so: the dashboard reads logs, Wireshark
 * reads the wire, and the two answer different questions. Keeping that boundary
 * explicit is the whole point — a dashboard that implied it had seen the
 * packets would be claiming evidence it does not hold.
 *
 * `captures/` holds the curated T9.4 evidence set and the demonstration
 * capture, one per *scenario*. An arbitrary run does not have a capture, and
 * nothing here implies otherwise.
 */

import { api } from '../api.js';
import {
  el, clear, card, stat, kv, viewHeader, emptyState, errorState, modeChip, table, num, bytes,
  MISSING
} from '../ui.js';

let body = null;
let cachedKey = null;

/* What to look for, per mode, in the packets themselves — expectations the
 * capture can confirm or refute, taken from specs.md §22.1 and WS-06/WS-07. */
const WHAT_TO_LOOK_FOR = {
  gbn: [
    'ACKs are cumulative: the ack field names the highest in-order sequence received, so it repeats while a gap is open (D5).',
    'One drop is followed by the whole outstanding range going out again — the same sequences reappear in order (WS-06, GBN-04).',
    'A duplicate sequence in the capture means a segment that <em>did</em> arrive was sent again. A segment that was dropped never reached the wire, so resending it adds no duplicate.',
  ],
  sr: [
    'ACKs name exactly the segment acknowledged, not a cumulative high-water mark (D6).',
    'A retransmission is a single segment, out of order with respect to its neighbours (WS-07, SR-10).',
    'Segments after a loss keep flowing: the receiver buffers them rather than discarding them.',
  ],
  hybrid: [
    'A MODE packet (type 7) carries a JSON payload with the epoch and the sequence the new mode takes effect from.',
    'The window drains before the handshake — no DATA is outstanding across the switch, which is what makes "no unacked segment is lost" true by construction (design.md §6.3).',
    'After the switch, the ACK pattern changes shape: cumulative becomes per-segment, or the other way round.',
  ],
};

function guidanceFor(mode) {
  const key = String(mode || '').toLowerCase();
  return WHAT_TO_LOOK_FOR[key] || WHAT_TO_LOOK_FOR.hybrid;
}

function provenanceCard(provenance) {
  return card('Which facts come from where', { note: 'the boundary this panel keeps' },
    el('div', { class: 'grid cols-2' },
      el('div', {},
        el('h4', {}, 'From the event logs (this dashboard)'),
        el('ul', { class: 'note-inline' }, ...provenance.from_logs.map((item) => el('li', {}, item)))),
      el('div', {},
        el('h4', {}, 'From the capture (Wireshark)'),
        el('ul', { class: 'note-inline' }, ...provenance.from_capture.map((item) => el('li', {}, item))))),
    el('p', { class: 'card-caveat', html:
      '<strong>This dashboard does not inspect packets.</strong> It reads what the endpoints '
      + 'recorded. Wireshark is what confirms that the packets on the wire match — which is '
      + 'why the two are kept separate rather than merged into one apparently complete picture.' }));
}

function commandCard(companion) {
  const capture = companion.reference_captures[0];
  return card('Getting there', { note: 'commands, ready to copy' },
    kv([
      ['Display filter', el('code', {}, companion.filter)],
      ['UDP port', companion.port],
      ['Dissector', el('code', {}, companion.dissector)],
      ['Mode at the end of this run', companion.current_mode ? modeChip(companion.current_mode) : MISSING],
    ]),
    el('pre', { class: 'transcript', style: 'margin-top:14px' },
      `# capture a live run on the npcap loopback adapter\n`
      + `tshark -i '\\Device\\NPF_Loopback' -f "udp port ${companion.port}" -a duration:30 -w captures/live.pcapng\n\n`
      + `# name the header fields so they can be filtered, not just read at fixed offsets\n`
      + `tshark -r ${capture ? `captures/${capture.name}` : 'captures/<file>.pcapng'} `
      + `-X lua_script:${companion.dissector} \\\n`
      + `       -T fields -e frame.number -e hybridarq.type_name -e hybridarq.seq -e hybridarq.ack\n\n`
      + `# every transmission of one segment — the retransmission pattern, as a query\n`
      + `tshark -r ${capture ? `captures/${capture.name}` : 'captures/<file>.pcapng'} `
      + `-X lua_script:${companion.dissector} -Y 'hybridarq.type == 1 && hybridarq.seq == 20'`),
    el('p', { class: 'note-inline' },
      'Loopback capture on Windows needs the npcap "Adapter for loopback traffic capture". '
      + 'The header is a fixed 21-byte prefix, so sequence and ACK are readable at constant '
      + 'offsets even without the dissector (WS-05).'));
}

function timestampCard(companion) {
  const switches = companion.switch_timestamps || [];
  const retx = companion.first_retransmissions || [];

  return card('Where to look in the capture', { note: 'timestamps from this run’s log' },
    el('div', { class: 'grid cols-2' },
      stat('Mode transitions', num(switches.length, 0),
        { sub: switches.length ? switches.map((t) => `${t.toFixed(2)}s`).join(', ') : 'none recorded' }),
      stat('Retransmissions', num(companion.retransmission_count, 0),
        { sub: 'first ten listed below' })),
    retx.length
      ? table([
        { label: 'At (s)', numeric: true, render: (row) => num(row.timestamp, 3) },
        { label: 'Sequence', numeric: true, render: (row) => row.sequence },
        { label: 'Mode', render: (row) => (row.mode ? modeChip(row.mode) : MISSING) },
      ], retx)
      : el('p', { class: 'note-inline' }, 'This run retransmitted nothing, so there is no retransmission to find on the wire.'),
    el('p', { class: 'card-caveat' },
      'These are seconds on the <em>sender’s</em> clock, from when its log opened — not '
      + 'Wireshark’s capture clock. Line the two up on the START packet, which both record.'));
}

function capturesCard(companion) {
  if (!companion.reference_captures.length) {
    return card('Recorded captures', {},
      emptyState('No capture files under captures/',
        'The curated evidence set is produced by `python experiments/capture_evidence.py`. '
        + 'Its absence means it has not been recorded on this machine.'));
  }
  return card('Recorded captures', {
    note: 'the curated per-scenario evidence set (T9.4)',
  },
  table([
    { label: 'Capture', render: (row) => el('code', {}, row.name) },
    { label: 'Size', numeric: true, render: (row) => bytes(row.bytes) },
    { label: 'Expected observation', wrap: true, render: (row) => row.expectation || MISSING },
  ], companion.reference_captures),
  el('p', { class: 'card-caveat', html:
      `<strong>These are per scenario, not per run.</strong> The run under inspection `
      + `(<code>${companion.run_id}</code>) has no capture of its own unless one was taken while `
      + `it ran. The files above are the recorded evidence for their own scenarios, and each one `
      + `names the run it came from in <code>captures/README.md</code>.` }));
}

async function renderBody(store) {
  const fragment = el('div', { class: 'grid' });
  if (!store.selectedKey) {
    fragment.append(emptyState('No run selected', 'Pick a run from the header, or start one.'));
    return fragment;
  }
  let companion;
  try {
    companion = await api.wireshark(store.selectedKey);
  } catch (error) {
    fragment.append(errorState(error, { title: 'The Wireshark companion could not be prepared' }));
    return fragment;
  }
  cachedKey = store.selectedKey;

  fragment.append(el('div', { class: 'grid cols-2' },
    commandCard(companion), timestampCard(companion)));

  fragment.append(card('What to look for', {
    note: `for ${companion.current_mode || 'this run’s'} traffic`,
  },
  el('ul', { class: 'note-inline' },
    ...guidanceFor(companion.current_mode).map((item) => el('li', { html: item }))),
  el('p', { class: 'note-inline' },
    'The <code>MODE</code> handshake is packet type 7; DATA is type 1. The full type numbering '
    + 'is part of the frozen wire format (D1), so a capture taken today is readable against one '
    + 'taken at the start of the project.')));

  fragment.append(el('div', { class: 'grid cols-2' },
    capturesCard(companion), provenanceCard(companion.provenance)));
  return fragment;
}

export async function render({ store }) {
  const root = el('div');
  root.append(viewHeader('Wireshark Companion',
    'The filter, the port, the mode and the timestamps worth jumping to — so an evaluator can '
    + 'confirm on the wire what the logs claim. <strong>This page does not inspect packets '
    + 'itself.</strong>'));
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
