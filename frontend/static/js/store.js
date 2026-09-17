/* Shared state: which run every view is looking at, and what the live one is doing.
 *
 * One selected run, held here rather than per view, so moving between the mode
 * timeline, the event log and the retransmission picture keeps showing the same
 * transfer — which is what T11.13 means by "no page-hopping mid-demo".
 *
 * The live poll is the only timer in the app. It asks the server for the active
 * transfer's status; the server in turn reads that run's events.csv. Nothing on
 * this side accumulates its own idea of what happened, so a reload shows the
 * same numbers as a first load (rule 1: recorded data only).
 */

import { api, ApiError } from './api.js';

const POLL_ACTIVE_MS = 500;
const POLL_IDLE_MS = 4000;

class Store extends EventTarget {
  constructor() {
    super();
    this.health = null;
    this.healthError = null;
    this.runs = [];
    this.selectedKey = null;
    this.transfer = { active: false, state: 'idle', run: null, progress: null };
    this.transferError = null;
    this._timer = null;
    this._followLive = true;
  }

  emit(name) { this.dispatchEvent(new CustomEvent(name)); }

  get selectedRun() {
    return this.runs.find((run) => run.key === this.selectedKey) || null;
  }

  /** True when the selected run is the one currently being transferred. */
  get selectionIsLive() {
    const live = this.transfer.run;
    return Boolean(live && this.transfer.active && live.key === this.selectedKey);
  }

  async refreshHealth() {
    try {
      this.health = await api.health();
      this.healthError = null;
    } catch (error) {
      this.health = null;
      this.healthError = error instanceof ApiError ? error : new ApiError(String(error));
    }
    this.emit('health');
  }

  async refreshRuns({ keepSelection = true } = {}) {
    try {
      const payload = await api.runs();
      this.runs = payload.runs;
      if (!keepSelection || !this.runs.some((run) => run.key === this.selectedKey)) {
        this.selectedKey = this.runs.length ? this.runs[0].key : null;
      }
    } catch (error) {
      this.runs = [];
      this.healthError = error;
    }
    this.emit('runs');
  }

  select(key, { follow = false } = {}) {
    if (key === this.selectedKey) return;
    this.selectedKey = key;
    this._followLive = follow;
    this.emit('selection');
  }

  async refreshTransfer() {
    try {
      const status = await api.transferStatus();
      const previousState = this.transfer.state;
      this.transfer = status;
      this.transferError = null;

      // Follow a run this dashboard started, so the live views point at it
      // without the operator having to pick it out of the list mid-demo.
      if (status.run && this._followLive && status.run.key !== this.selectedKey) {
        const known = this.runs.some((run) => run.key === status.run.key);
        if (known || status.active) {
          this.selectedKey = status.run.key;
          this.emit('selection');
        }
      }
      if (previousState !== status.state) {
        if (['complete', 'failed', 'stopped'].includes(status.state)) {
          await this.refreshRuns();
        }
        this.emit('transfer-state');
      }
      this.emit('transfer');
    } catch (error) {
      this.transferError = error;
      this.emit('transfer');
    }
    this._schedule();
  }

  _schedule() {
    clearTimeout(this._timer);
    const delay = this.transfer.active ? POLL_ACTIVE_MS : POLL_IDLE_MS;
    this._timer = setTimeout(() => this.refreshTransfer(), delay);
  }

  startPolling() {
    clearTimeout(this._timer);
    this.refreshTransfer();
  }

  async start(settings) {
    this._followLive = true;
    const status = await api.startTransfer(settings);
    this.transfer = status;
    if (status.run) {
      this.selectedKey = status.run.key;
      this.emit('selection');
    }
    await this.refreshRuns({ keepSelection: true });
    this.emit('transfer');
    this._schedule();
    return status;
  }

  async stop() {
    const status = await api.stopTransfer();
    this.transfer = status;
    await this.refreshRuns();
    this.emit('transfer');
    return status;
  }

  async reset() {
    const status = await api.resetTransfer();
    this.transfer = status;
    this.emit('transfer');
    return status;
  }
}

export const store = new Store();
