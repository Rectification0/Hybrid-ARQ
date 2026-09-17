/* Talking to the dashboard server (T11.1).
 *
 * Every response the API refuses carries a readable message and, where there
 * was one, the underlying error. Both are preserved here and handed to the UI,
 * because T11.14 requires the explanation *and* the logged error — a fetch
 * wrapper that collapsed them into "request failed" would throw the second away.
 */

export class ApiError extends Error {
  constructor(message, { status = 0, detail = '', url = '' } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.url = url;
  }
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(path, options);
  } catch (cause) {
    // The server is not answering at all. This is the "backend unreachable"
    // state, and it is reported as that rather than as an empty dataset.
    throw new ApiError(
      'The dashboard server is not responding. Is `python -m frontend` still running?',
      { detail: String(cause), url: path });
  }

  const text = await response.text();
  let payload = null;
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = null; }
  }

  if (!response.ok) {
    const message = (payload && payload.error) || `${response.status} ${response.statusText}`;
    throw new ApiError(message, {
      status: response.status,
      detail: (payload && payload.detail) || (payload ? '' : text.slice(0, 2000)),
      url: path,
    });
  }
  return payload;
}

const query = (params) => {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== undefined && value !== null && value !== '') search.set(key, value);
  }
  const text = search.toString();
  return text ? `?${text}` : '';
};

export const api = {
  health: () => request('/api/health'),
  config: () => request('/api/config'),

  runs: (params) => request(`/api/runs${query(params)}`),
  run: (run) => request(`/api/run${query({ run })}`),
  events: (params) => request(`/api/run/events${query(params)}`),
  modes: (run) => request(`/api/run/modes${query({ run })}`),
  network: (run) => request(`/api/run/network${query({ run })}`),
  retransmissions: (run) => request(`/api/run/retransmissions${query({ run })}`),
  wireshark: (run) => request(`/api/run/wireshark${query({ run })}`),

  experimentRuns: () => request('/api/experiments/runs'),
  aggregate: () => request('/api/experiments/aggregate'),
  figures: () => request('/api/figures'),
  captures: () => request('/api/captures'),
  demo: () => request('/api/demo'),

  transferStatus: () => request('/api/transfer'),
  startTransfer: (settings) => request('/api/transfer', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(settings),
  }),
  stopTransfer: () => request('/api/transfer/stop', { method: 'POST' }),
  resetTransfer: () => request('/api/transfer/reset', { method: 'POST' }),
};
