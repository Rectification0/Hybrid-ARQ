/* Application shell: navigation, the shared run selector, and the router (T11.2).
 *
 * Views are modules with a `render(container)`; the shell owns everything they
 * share — which run is selected, whether the backend is reachable, and the
 * live transfer's state. A view that needs the live run re-renders on the
 * store's events rather than polling on its own.
 */

import { store } from './store.js';
import { el, clear, errorState, loading, modeChip, badge } from './ui.js';

import * as control from './views/control.js';
import * as overview from './views/overview.js';
import * as modes from './views/modes.js';
import * as network from './views/network.js';
import * as events from './views/events.js';
import * as retransmissions from './views/retransmissions.js';
import * as metrics from './views/metrics.js';
import * as compare from './views/compare.js';
import * as history from './views/history.js';
import * as wireshark from './views/wireshark.js';
import * as demo from './views/demo.js';

/* The order is the demonstration order: control and overview first, the mode
 * visualization third because it is the centrepiece (T11.5), the comparison and
 * the history after the live screens, Wireshark and the guided demo last. */
const VIEWS = [
  { id: 'control', label: 'Transfer Control', module: control, primary: true },
  { id: 'overview', label: 'Overview', module: overview, primary: true },
  { id: 'modes', label: 'Adaptive Mode', module: modes, primary: true },
  { id: 'network', label: 'Network', module: network },
  { id: 'events', label: 'Events', module: events },
  { id: 'retransmissions', label: 'Retransmissions', module: retransmissions },
  { id: 'metrics', label: 'Metrics', module: metrics },
  { id: 'compare', label: 'GBN vs SR vs Hybrid', module: compare },
  { id: 'history', label: 'Run History', module: history },
  { id: 'wireshark', label: 'Wireshark', module: wireshark },
  { id: 'demo', label: 'Demonstration', module: demo, primary: true },
];

const container = document.getElementById('view');
const navList = document.getElementById('nav-list');
const runSelect = document.getElementById('run-select');
const liveStatus = document.getElementById('live-status');
const backendStatus = document.getElementById('backend-status');
const footerLag = document.getElementById('footer-lag');

let currentId = null;
let currentView = null;

/* --------------------------------------------------------------- routing */

function viewFromHash() {
  const id = (location.hash || '').replace(/^#\/?/, '');
  return VIEWS.find((view) => view.id === id) || VIEWS[0];
}

async function renderCurrent() {
  const view = viewFromHash();
  currentId = view.id;
  currentView = view.module;

  for (const button of navList.querySelectorAll('button')) {
    button.toggleAttribute('aria-current', button.dataset.id === view.id);
    if (button.dataset.id === view.id) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  }

  clear(container).append(loading(`Loading ${view.label}…`));
  try {
    const content = await view.module.render({ store });
    clear(container).append(content);
  } catch (error) {
    clear(container).append(errorState(error, { title: `${view.label} could not be shown` }));
  }
}

function buildNav() {
  clear(navList);
  for (const view of VIEWS) {
    navList.append(el('li', { class: view.primary ? 'nav-primary' : '' },
      el('button', {
        dataset: { id: view.id },
        onclick: () => { location.hash = `#/${view.id}`; },
      }, view.label)));
  }
}

/* ------------------------------------------------------------- run picker */

function renderRunPicker() {
  const previous = runSelect.value;
  clear(runSelect);
  if (!store.runs.length) {
    runSelect.append(el('option', { value: '' }, 'no recorded runs found under logs/'));
    runSelect.disabled = true;
    return;
  }
  runSelect.disabled = false;

  const groups = new Map();
  for (const run of store.runs) {
    const name = run.group || '(top level)';
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(run);
  }
  for (const [name, runs] of groups) {
    const group = el('optgroup', { label: `${name} — ${runs.length} run${runs.length === 1 ? '' : 's'}` });
    for (const run of runs) {
      const verdict = run.integrity_success === true ? 'ok'
        : run.integrity_success === false ? 'HASH MISMATCH' : 'no verdict';
      group.append(el('option', { value: run.key },
        `${run.run_id}  ·  ${run.mode || run.requested_mode || 'mode not recorded'}  ·  ${verdict}`));
    }
    runSelect.append(group);
  }
  runSelect.value = store.selectedKey || previous || '';
}

runSelect.addEventListener('change', () => {
  store.select(runSelect.value, { follow: false });
});

/* ----------------------------------------------------------- live status */

function renderLiveStatus() {
  const { state, run, progress } = store.transfer;
  liveStatus.dataset.state = state;
  const text = liveStatus.querySelector('.live-text');
  clear(text);

  if (state === 'idle' || !run) {
    text.append('no transfer started from this dashboard');
    return;
  }
  const label = {
    starting: 'starting', running: 'transfer running',
    complete: 'transfer complete', failed: 'transfer failed', stopped: 'transfer stopped',
  }[state] || state;

  text.append(el('span', {}, `${label} · ${run.run_id}`));
  if (progress && progress.current_mode) text.append(' ', modeChip(progress.current_mode));
  if (progress && Number.isFinite(progress.fraction) && state === 'running') {
    text.append(` · ${(progress.fraction * 100).toFixed(0)}%`);
  }
}

function renderBackendStatus() {
  clear(backendStatus);
  if (store.healthError) {
    backendStatus.dataset.ok = 'false';
    backendStatus.append(badge('backend unreachable', 'bad'), ' ', store.healthError.message);
    return;
  }
  if (!store.health) return;
  backendStatus.dataset.ok = String(Boolean(store.health.preflight.ok));
  const failing = store.health.preflight.checks.filter((check) => !check.ok);
  backendStatus.append(failing.length
    ? badge(`${failing.length} preflight check failing`, 'bad')
    : badge('backend ready', 'ok'));
  footerLag.textContent = store.health.live_lag_note;
}

/* ------------------------------------------------------------------- run */

function rerenderIfLive() {
  // Only the views that show a running transfer need refreshing on each poll;
  // the recorded ones do not change underneath the reader.
  if (currentView && typeof currentView.onTick === 'function') currentView.onTick({ store });
}

store.addEventListener('runs', renderRunPicker);
store.addEventListener('selection', () => {
  renderRunPicker();
  renderCurrent();
});
store.addEventListener('health', renderBackendStatus);
store.addEventListener('transfer', () => { renderLiveStatus(); rerenderIfLive(); });
store.addEventListener('transfer-state', () => {
  // A transfer that just finished changes what a recorded view would show.
  if (currentView && typeof currentView.onTick !== 'function') renderCurrent();
});

window.addEventListener('hashchange', renderCurrent);

async function boot() {
  buildNav();
  await store.refreshHealth();
  renderBackendStatus();
  await store.refreshRuns();
  renderRunPicker();
  if (!location.hash) location.hash = '#/control';
  await renderCurrent();
  store.startPolling();
}

boot();

export { VIEWS, currentId };
