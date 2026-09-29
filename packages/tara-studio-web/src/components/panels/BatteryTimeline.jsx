import { useMemo, useRef, useState } from 'react';
import { formatClock, linePath, nearestIndex, scaleLinear, tableRows } from '../../engine/energy.js';

// Chart box in viewBox units; the SVG scales to the drawer's width.
const W = 320;
const H = 168;
const M = { top: 10, right: 44, bottom: 22, left: 30 };
const PLOT_W = W - M.left - M.right;
const PLOT_H = H - M.top - M.bottom;
const Y_TICKS = [0, 25, 50, 75, 100];

// Direct end-labels closer than this (viewBox units) would overlap; the legend
// still names both series, so the lower-priority label is dropped instead.
const LABEL_MIN_GAP = 11;

/**
 * Estimated remaining battery over the show: the worst drone and the fleet
 * mean, against the depth-of-discharge reserve floor. One y-axis (percent);
 * peak current is a different measure and lives in the tooltip and table.
 */
export default function BatteryTimeline({ timeline, reservePct }) {
  const [hover, setHover] = useState(null);
  const svgRef = useRef(null);
  const times = timeline.time_s;
  const worst = timeline.min_remaining_pct;
  const mean = timeline.mean_remaining_pct;
  const current = timeline.max_current_a;

  const { x, y, worstPath, meanPath } = useMemo(() => {
    const xScale = scaleLinear([0, times[times.length - 1] || 1], [M.left, M.left + PLOT_W]);
    const yScale = scaleLinear([0, 100], [M.top + PLOT_H, M.top]);
    return {
      x: xScale,
      y: yScale,
      worstPath: linePath(times, worst, xScale, yScale),
      meanPath: linePath(times, mean, xScale, yScale),
    };
  }, [times, worst, mean]);

  if (!times.length) return null;

  const last = times.length - 1;
  const endX = x(times[last]) + 4;
  const worstEndY = y(worst[last]);
  const meanEndY = y(mean[last]);
  const showMeanLabel = Math.abs(meanEndY - worstEndY) >= LABEL_MIN_GAP;
  const reserveY = y(reservePct);
  const midT = times[last] / 2;

  const moveTo = (clientX) => {
    const svg = svgRef.current;
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    const vx = ((clientX - rect.left) / rect.width) * W;
    const t = ((vx - M.left) / PLOT_W) * times[last];
    setHover(nearestIndex(times, t));
  };

  const onKeyDown = (e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault();
    const step = e.shiftKey ? 10 : 1;
    setHover((h) => {
      const start = h ?? 0;
      return Math.min(last, Math.max(0, start + (e.key === 'ArrowRight' ? step : -step)));
    });
  };

  const h = hover;
  const hx = h !== null ? x(times[h]) : 0;

  return (
    <div className="battery-chart">
      <div className="battery-chart-legend" aria-hidden="true">
        <span><i className="key key-worst" />Worst drone</span>
        <span><i className="key key-mean" />Fleet mean</span>
        <span><i className="key key-reserve" />{reservePct}% reserve</span>
      </div>

      <div className="battery-chart-plot">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${W} ${H}`}
          role="img"
          aria-label={`Estimated battery remaining over ${formatClock(times[last])}: worst drone ends at ${worst[last]}%, fleet mean at ${mean[last]}%. Use left and right arrow keys to inspect.`}
          tabIndex={0}
          onPointerMove={(e) => moveTo(e.clientX)}
          onPointerLeave={() => setHover(null)}
          onFocus={() => setHover((prev) => prev ?? last)}
          onBlur={() => setHover(null)}
          onKeyDown={onKeyDown}
        >
          {Y_TICKS.map((v) => (
            <g key={v}>
              <line className="grid" x1={M.left} x2={M.left + PLOT_W} y1={y(v)} y2={y(v)} />
              <text className="tick" x={M.left - 5} y={y(v) + 3} textAnchor="end">{v}%</text>
            </g>
          ))}
          {[0, midT, times[last]].map((t, i) => (
            <text key={i} className="tick" x={x(t)} y={H - 6} textAnchor={['start', 'middle', 'end'][i]}>
              {formatClock(t)}
            </text>
          ))}

          <line className="reserve" x1={M.left} x2={M.left + PLOT_W} y1={reserveY} y2={reserveY} />

          {/* Mean first, so on a one-drone show (where the two coincide) the worst line reads on top. */}
          <path className="series series-mean" d={meanPath} />
          <path className="series series-worst" d={worstPath} />

          <text className="end-label" x={endX} y={worstEndY + 3}>{Math.round(worst[last])}%</text>
          {showMeanLabel && (
            <text className="end-label end-label-muted" x={endX} y={meanEndY + 3}>{Math.round(mean[last])}%</text>
          )}

          {h !== null && (
            <g className="crosshair" pointerEvents="none">
              <line x1={hx} x2={hx} y1={M.top} y2={M.top + PLOT_H} />
              <circle className="dot dot-mean" cx={hx} cy={y(mean[h])} r={4} />
              <circle className="dot dot-worst" cx={hx} cy={y(worst[h])} r={4} />
            </g>
          )}
        </svg>

        {h !== null && (
          <div
            className="battery-tooltip"
            style={{ left: `${(hx / W) * 100}%`, transform: `translateX(${hx > W / 2 ? '-105%' : '5%'})` }}
            role="status"
          >
            <div className="tt-time">{formatClock(times[h])}</div>
            <div className="tt-row"><i className="key key-worst" /><strong>{worst[h].toFixed(1)}%</strong> worst drone</div>
            <div className="tt-row"><i className="key key-mean" /><strong>{mean[h].toFixed(1)}%</strong> fleet mean</div>
            <div className="tt-row tt-muted"><strong>{current[h].toFixed(1)} A</strong> peak draw</div>
          </div>
        )}
      </div>

      <details className="battery-table">
        <summary>Data table</summary>
        <table>
          <thead>
            <tr><th>Time</th><th>Worst</th><th>Mean</th><th>Peak A</th></tr>
          </thead>
          <tbody>
            {tableRows(times.length).map((i) => (
              <tr key={i}>
                <td>{formatClock(times[i])}</td>
                <td>{worst[i].toFixed(1)}%</td>
                <td>{mean[i].toFixed(1)}%</td>
                <td>{current[i].toFixed(1)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}
