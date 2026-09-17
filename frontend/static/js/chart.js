/* Inline SVG charts, drawn from recorded points only (T11.5, T11.6, T11.8, T11.10).
 *
 * No plotting library, for the same reason the server is stdlib: an evaluator
 * should be able to open the dashboard without installing anything. Every
 * function takes points that came out of a log or a results CSV and draws
 * exactly those — none of them interpolates, smooths or extends a series past
 * its last sample, because a drawn point that no event produced would be a
 * fabrication with a stylesheet.
 *
 * Every chart labels its axes with units and every series carries a label as
 * well as a colour, so the picture survives being read from the back of a room.
 */

import { svg, el, num } from './ui.js';

const PAD = { top: 12, right: 16, bottom: 30, left: 52 };

const scale = (value, lo, hi, from, to) =>
  (hi === lo ? (from + to) / 2 : from + ((value - lo) / (hi - lo)) * (to - from));

function frame(width, height, { xLabel = '', yLabel = '', xTicks = [], yTicks = [], pad = PAD }) {
  const root = svg('svg', {
    class: 'chart', viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: 'xMidYMid meet', role: 'img',
  });
  const plot = { x0: pad.left, x1: width - pad.right, y0: height - pad.bottom, y1: pad.top };

  for (const tick of yTicks) {
    root.append(svg('line', {
      class: 'grid-line', x1: plot.x0, x2: plot.x1, y1: tick.at, y2: tick.at,
    }));
    root.append(svg('text', { x: plot.x0 - 7, y: tick.at + 3.5, 'text-anchor': 'end' }, tick.label));
  }
  for (const tick of xTicks) {
    root.append(svg('text', { x: tick.at, y: plot.y0 + 15, 'text-anchor': 'middle' }, tick.label));
  }
  root.append(svg('line', { class: 'axis-line', x1: plot.x0, x2: plot.x1, y1: plot.y0, y2: plot.y0 }));
  root.append(svg('line', { class: 'axis-line', x1: plot.x0, x2: plot.x0, y1: plot.y0, y2: plot.y1 }));
  if (xLabel) {
    root.append(svg('text', {
      class: 'axis-title', x: (plot.x0 + plot.x1) / 2, y: height - 3, 'text-anchor': 'middle',
    }, xLabel));
  }
  if (yLabel) {
    root.append(svg('text', {
      class: 'axis-title', x: 11, y: (plot.y0 + plot.y1) / 2,
      'text-anchor': 'middle', transform: `rotate(-90 11 ${(plot.y0 + plot.y1) / 2})`,
    }, yLabel));
  }
  return { root, plot };
}

const ticks = (lo, hi, count, format) => {
  const out = [];
  for (let i = 0; i <= count; i += 1) {
    const value = lo + ((hi - lo) * i) / count;
    out.push({ value, label: format(value) });
  }
  return out;
};

/* ------------------------------------------------------- line / sparkline */

/**
 * One or more time series. `series` is [{label, color, points:[{x,y}], dashed}].
 * Points are plotted where they were recorded; gaps are gaps.
 */
export function lineChart(series, {
  width = 720, height = 240, xLabel = '', yLabel = '', yMin = null, yMax = null,
  xFormat = (v) => num(v, 1), yFormat = (v) => num(v, 2), markers = [], bands = [],
} = {}) {
  const all = series.flatMap((s) => s.points).filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y));
  if (!all.length) return el('p', { class: 'note-inline' }, 'No samples were recorded for this series.');

  const xLo = Math.min(...all.map((p) => p.x));
  const xHi = Math.max(...all.map((p) => p.x));
  const yLo = yMin !== null ? yMin : Math.min(...all.map((p) => p.y));
  const yHiRaw = yMax !== null ? yMax : Math.max(...all.map((p) => p.y));
  const yHi = yHiRaw === yLo ? yLo + 1 : yHiRaw;

  const yTickValues = ticks(yLo, yHi, 4, yFormat);
  const xTickValues = ticks(xLo, xHi, 5, xFormat);
  const px = (v) => scale(v, xLo, xHi, PAD.left, width - PAD.right);
  const py = (v) => scale(v, yLo, yHi, height - PAD.bottom, PAD.top);

  const { root } = frame(width, height, {
    xLabel, yLabel,
    yTicks: yTickValues.map((t) => ({ at: py(t.value), label: t.label })),
    xTicks: xTickValues.map((t) => ({ at: px(t.value), label: t.label })),
  });

  for (const band of bands) {
    root.append(svg('rect', {
      x: px(band.start), y: PAD.top, width: Math.max(1, px(band.end) - px(band.start)),
      height: height - PAD.bottom - PAD.top, fill: band.color, opacity: band.opacity ?? 0.1,
    }, svg('title', {}, band.label || '')));
  }

  for (const s of series) {
    const points = s.points.filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y));
    if (!points.length) continue;
    const d = points.map((p, i) => `${i ? 'L' : 'M'}${px(p.x).toFixed(2)} ${py(p.y).toFixed(2)}`).join(' ');
    root.append(svg('path', {
      d, fill: 'none', stroke: s.color, 'stroke-width': s.width || 2,
      'stroke-dasharray': s.dashed ? '5 4' : null, 'stroke-linejoin': 'round',
    }, svg('title', {}, s.label)));
    if (s.dots !== false && points.length <= 80) {
      for (const p of points) {
        root.append(svg('circle', { cx: px(p.x), cy: py(p.y), r: 2.6, fill: s.color },
          svg('title', {}, `${s.label}: ${yFormat(p.y)} at ${xFormat(p.x)}`)));
      }
    }
    const last = points[points.length - 1];
    root.append(svg('text', {
      x: Math.min(width - 4, px(last.x) + 5), y: py(last.y) + 3.5,
      'text-anchor': 'start', fill: s.color, 'font-size': 10.5, 'font-weight': 600,
    }, s.label));
  }

  for (const marker of markers) {
    if (!Number.isFinite(marker.x)) continue;
    root.append(svg('line', {
      x1: px(marker.x), x2: px(marker.x), y1: PAD.top, y2: height - PAD.bottom,
      stroke: marker.color || 'var(--text-faint)', 'stroke-width': 1.4,
      'stroke-dasharray': '3 3',
    }, svg('title', {}, marker.label || '')));
    if (marker.label) {
      root.append(svg('text', {
        x: px(marker.x) + 4, y: PAD.top + 10, fill: marker.color || 'var(--text-faint)',
        'font-size': 10,
      }, marker.label));
    }
  }
  return root;
}

/** A horizontal threshold rule, drawn only where the recorded value means one. */
export function thresholdLine(chart, { y, yLo, yHi, height = 240, width = 720, color, label }) {
  const at = scale(y, yLo, yHi, height - PAD.bottom, PAD.top);
  chart.append(svg('line', {
    x1: PAD.left, x2: width - PAD.right, y1: at, y2: at,
    stroke: color, 'stroke-width': 1.2, 'stroke-dasharray': '6 4', opacity: 0.85,
  }, svg('title', {}, label)));
  chart.append(svg('text', { x: PAD.left + 4, y: at - 4, fill: color, 'font-size': 10 }, label));
  return chart;
}

/* ----------------------------------------------------------- scatter plot */

/**
 * Sequence number against time — the picture that makes GBN's range
 * retransmission and SR's single-segment retransmission different shapes (T11.8).
 */
export function scatterChart(series, {
  width = 900, height = 320, xLabel = 'Time (s, sender clock)', yLabel = 'Sequence number',
  bands = [], xMax = null, yMax = null,
} = {}) {
  const all = series.flatMap((s) => s.points);
  if (!all.length) return el('p', { class: 'note-inline' }, 'No SEND or RETX rows were recorded for this run.');

  const xHi = xMax ?? Math.max(...all.map((p) => p.x), 0.001);
  const yHi = yMax ?? Math.max(...all.map((p) => p.y), 1);
  const px = (v) => scale(v, 0, xHi, PAD.left, width - PAD.right);
  const py = (v) => scale(v, 0, yHi, height - PAD.bottom, PAD.top);

  const { root } = frame(width, height, {
    xLabel, yLabel,
    yTicks: ticks(0, yHi, 4, (v) => Math.round(v).toLocaleString()).map((t) => ({ at: py(t.value), label: t.label })),
    xTicks: ticks(0, xHi, 6, (v) => v.toFixed(1)).map((t) => ({ at: px(t.value), label: t.label })),
  });

  for (const band of bands) {
    root.append(svg('rect', {
      x: px(band.start), y: PAD.top, width: Math.max(1, px(band.end) - px(band.start)),
      height: height - PAD.bottom - PAD.top, fill: band.color, opacity: 0.09,
    }, svg('title', {}, band.label || '')));
  }

  for (const s of series) {
    const group = svg('g', { fill: s.color, opacity: s.opacity ?? 1 });
    for (const point of s.points) {
      if (!Number.isFinite(point.x) || !Number.isFinite(point.y)) continue;
      const x = px(point.x);
      const y = py(point.y);
      // Marker shape carries identity as well as colour: dot = first
      // transmission, square = retransmission, cross = drop.
      if (s.shape === 'square') {
        group.append(svg('rect', { x: x - 2.4, y: y - 2.4, width: 4.8, height: 4.8 },
          svg('title', {}, `${s.label} seq ${point.y} at ${point.x.toFixed(3)}s`)));
      } else if (s.shape === 'cross') {
        group.append(svg('path', {
          d: `M${x - 3} ${y - 3}L${x + 3} ${y + 3}M${x + 3} ${y - 3}L${x - 3} ${y + 3}`,
          stroke: s.color, 'stroke-width': 1.5, fill: 'none',
        }, svg('title', {}, `${s.label} seq ${point.y} at ${point.x.toFixed(3)}s`)));
      } else {
        group.append(svg('circle', { cx: x, cy: y, r: 1.7 },
          svg('title', {}, `${s.label} seq ${point.y} at ${point.x.toFixed(3)}s`)));
      }
    }
    root.append(group);
  }
  return root;
}

/* -------------------------------------------------------- mode timeline */

/**
 * Contiguous mode spans as a band, with transitions marked (T11.5).
 * The spans come from the `mode` column of the log, so an abandoned handshake —
 * which leaves the mode unchanged — is visible as a mark with no band change.
 */
export function timelineBand(bands, transitions, {
  width = 900, height = 96, span = null, colorOf = () => 'var(--text-faint)',
} = {}) {
  const total = span || Math.max(...bands.map((b) => b.end), 0.001);
  const px = (v) => scale(v, 0, total, PAD.left, width - PAD.right);
  const top = 22;
  const bandHeight = 30;

  const root = svg('svg', {
    class: 'chart', viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: 'xMidYMid meet', role: 'img',
  });

  if (!bands.length) {
    root.append(svg('text', { x: width / 2, y: height / 2, 'text-anchor': 'middle' },
      'No mode was recorded for this run.'));
    return root;
  }

  for (const band of bands) {
    const x = px(band.start);
    const w = Math.max(1.5, px(band.end) - x);
    root.append(svg('rect', {
      x, y: top, width: w, height: bandHeight, rx: 3,
      fill: colorOf(band.mode), opacity: 0.85,
    }, svg('title', {}, `${band.mode} from ${band.start.toFixed(3)}s to ${band.end.toFixed(3)}s`)));
    if (w > 34) {
      root.append(svg('text', {
        x: x + w / 2, y: top + bandHeight / 2 + 3.5, 'text-anchor': 'middle',
        fill: '#08121a', 'font-size': 11, 'font-weight': 700,
      }, String(band.mode).toUpperCase()));
    }
  }

  for (const transition of transitions) {
    if (!Number.isFinite(transition.timestamp)) continue;
    const x = px(transition.timestamp);
    const color = transition.noop ? 'var(--fixed-hybrid)' : 'var(--text)';
    root.append(svg('line', {
      x1: x, x2: x, y1: top - 8, y2: top + bandHeight + 8,
      stroke: color, 'stroke-width': 1.6,
      'stroke-dasharray': transition.noop ? '3 3' : null,
    }, svg('title', {}, `${transition.reason} at ${transition.timestamp.toFixed(3)}s`)));
    root.append(svg('circle', { cx: x, cy: top - 8, r: 3.2, fill: color }));
  }

  // Axis under the band, in seconds on the sender's own clock.
  root.append(svg('line', {
    class: 'axis-line', x1: PAD.left, x2: width - PAD.right,
    y1: top + bandHeight + 12, y2: top + bandHeight + 12,
  }));
  for (const tick of ticks(0, total, 6, (v) => `${v.toFixed(1)}s`)) {
    root.append(svg('text', {
      x: px(tick.value), y: top + bandHeight + 26, 'text-anchor': 'middle',
    }, tick.label));
  }
  return root;
}

/* ------------------------------------------------------------ sparkline */

export function sparkline(points, { width = 260, height = 44, color = 'var(--accent)', fill = true } = {}) {
  const usable = points.filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y));
  if (usable.length < 2) return el('span', { class: 'note-inline' }, 'too few samples to plot');

  const xLo = Math.min(...usable.map((p) => p.x));
  const xHi = Math.max(...usable.map((p) => p.x));
  const yLo = Math.min(0, ...usable.map((p) => p.y));
  const yHi = Math.max(...usable.map((p) => p.y)) || 1;
  const px = (v) => scale(v, xLo, xHi, 1, width - 1);
  const py = (v) => scale(v, yLo, yHi, height - 2, 2);
  const d = usable.map((p, i) => `${i ? 'L' : 'M'}${px(p.x).toFixed(1)} ${py(p.y).toFixed(1)}`).join(' ');

  const root = svg('svg', {
    class: 'chart', viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: 'none',
    role: 'img', 'aria-label': `${usable.length} samples, ${yLo.toFixed(3)} to ${yHi.toFixed(3)}`,
  });
  if (fill) {
    root.append(svg('path', {
      d: `${d}L${px(xHi).toFixed(1)} ${height} L${px(xLo).toFixed(1)} ${height}Z`,
      fill: color, opacity: 0.14,
    }));
  }
  root.append(svg('path', { d, fill: 'none', stroke: color, 'stroke-width': 1.6 }));
  return root;
}

/* ------------------------------------------------------------ bar chart */

/** Grouped bars: one group per category, one bar per series (T11.10). */
export function groupedBars(categories, series, {
  width = 760, height = 280, yLabel = '', yFormat = (v) => num(v, 1), xLabel = '',
} = {}) {
  const values = series.flatMap((s) => s.values).filter(Number.isFinite);
  if (!values.length) return el('p', { class: 'note-inline' }, 'No aggregated rows matched.');
  const yHi = Math.max(...values) * 1.08 || 1;
  const py = (v) => scale(v, 0, yHi, height - PAD.bottom, PAD.top);

  const { root } = frame(width, height, {
    yLabel,
    xLabel,
    yTicks: ticks(0, yHi, 4, yFormat).map((t) => ({ at: py(t.value), label: t.label })),
  });

  const usable = width - PAD.left - PAD.right;
  const groupWidth = usable / categories.length;
  const barWidth = Math.max(4, (groupWidth * 0.74) / series.length);

  categories.forEach((category, index) => {
    const centre = PAD.left + groupWidth * (index + 0.5);
    series.forEach((s, seriesIndex) => {
      const value = s.values[index];
      if (!Number.isFinite(value)) return;
      const x = centre - (series.length * barWidth) / 2 + seriesIndex * barWidth;
      root.append(svg('rect', {
        x, y: py(value), width: barWidth - 1.5, height: Math.max(0, py(0) - py(value)),
        fill: s.color, rx: 1.5,
      }, svg('title', {}, `${s.label}, ${category}: ${yFormat(value)}`)));
    });
    root.append(svg('text', {
      x: centre, y: height - PAD.bottom + 15, 'text-anchor': 'middle',
    }, category));
  });
  return root;
}
