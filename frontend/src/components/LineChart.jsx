import { useEffect, useRef, useState } from "react";
import { formatClock } from "../format.js";

// Gráfico de líneas en SVG (sin librerías): líneas de 2 px, cuadrícula fina sólida, punto final con
// anillo del color de la superficie, etiqueta final sólo si no choca, cruz de lectura con tooltip
// (puntero y teclado) y una vista de tabla equivalente.
const MARGIN = { top: 14, right: 64, bottom: 28, left: 48 };

function niceMax(value) {
  if (!value || value <= 0) return 1;
  const exponent = 10 ** Math.floor(Math.log10(value));
  const fraction = value / exponent;
  const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 2.5 ? 2.5 : fraction <= 5 ? 5 : 10;
  return nice * exponent;
}

function useWidth(ref, fallback = 560) {
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    if (!ref.current || typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(260, Math.floor(entry.contentRect.width))));
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, [ref]);
  return width;
}

function LineKey({ color }) {
  return (
    <svg width="16" height="4" aria-hidden="true" className="line-key">
      <line x1="0" x2="16" y1="2" y2="2" stroke={color} strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

export default function LineChart({ title, subtitle, times, series, format = (v) => String(v), height = 230, unit = "" }) {
  const bodyRef = useRef(null);
  const width = useWidth(bodyRef);
  const [hover, setHover] = useState(null);
  const [showTable, setShowTable] = useState(false);

  const n = times.length;
  const plotW = Math.max(10, width - MARGIN.left - MARGIN.right);
  const plotH = height - MARGIN.top - MARGIN.bottom;
  const values = series.flatMap((s) => s.values.filter((v) => v != null));
  const yMax = niceMax(Math.max(0, ...values));
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * yMax);
  const x = (i) => MARGIN.left + (n <= 1 ? plotW : (i / (n - 1)) * plotW);
  const y = (v) => MARGIN.top + plotH - (v / yMax) * plotH;

  const paths = series.map((s) => {
    let d = "";
    let pen = false;
    s.values.forEach((v, i) => {
      if (v == null) {
        pen = false; // sin datos: hueco en la línea, nunca un cero inventado
        return;
      }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  });
  const ends = series.map((s) => {
    for (let i = s.values.length - 1; i >= 0; i -= 1) if (s.values[i] != null) return i;
    return -1;
  });
  const endYs = ends.map((i, k) => (i >= 0 ? y(series[k].values[i]) : null));
  const endLabelsFit = endYs.every((a, i) => endYs.every((b, j) => i === j || a == null || b == null || Math.abs(a - b) >= 14));
  const xTickIdx = n > 1 ? [...new Set([0, Math.round((n - 1) / 3), Math.round((2 * (n - 1)) / 3), n - 1])] : [0];

  const pick = (event) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const px = event.clientX - rect.left;
    setHover(Math.max(0, Math.min(n - 1, Math.round(((px - MARGIN.left) / plotW) * (n - 1)))));
  };
  const onKeyDown = (event) => {
    if (event.key === "ArrowLeft") setHover((h) => Math.max(0, (h ?? n - 1) - 1));
    else if (event.key === "ArrowRight") setHover((h) => Math.min(n - 1, (h ?? n - 1) + 1));
    else if (event.key === "Escape") setHover(null);
  };
  const tooltipLeft = hover == null ? 0 : x(hover) > width - 190 ? x(hover) - 178 : x(hover) + 12;
  const lastTime = n ? formatClock(times[n - 1] * 1000) : "—";
  const ariaLabel = `${title}. ${series
    .map((s, k) => `${s.label}: último valor ${ends[k] >= 0 ? `${format(s.values[ends[k]])}${unit}` : "sin datos"}`)
    .join("; ")}. Usa las flechas para recorrer los valores.`;

  return (
    <article className="card chart-card">
      <header className="card-head">
        <div>
          <h3>{title}</h3>
          {subtitle && <p className="chart-subtitle muted small">{subtitle}</p>}
        </div>
        <button type="button" className="btn btn-link" onClick={() => setShowTable((v) => !v)} aria-pressed={showTable}>
          {showTable ? "Ver gráfico" : "Ver tabla"}
        </button>
      </header>
      <div className="chart-legend" aria-hidden={showTable}>
        {series.map((s) => (
          <span key={s.key} className="legend-item">
            <LineKey color={s.color} /> {s.label}
          </span>
        ))}
      </div>
      <div className="chart-body" ref={bodyRef}>
        {showTable ? (
          <div className="table-wrap chart-table">
            <table>
              <thead>
                <tr>
                  <th>Hora</th>
                  {series.map((s) => (
                    <th key={s.key} className="num">
                      {s.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {times
                  .map((t, i) => ({ t, i }))
                  .reverse()
                  .map(({ t, i }) => (
                    <tr key={t}>
                      <td className="mono">{formatClock(t * 1000)}</td>
                      {series.map((s) => (
                        <td key={s.key} className="num mono">
                          {s.values[i] == null ? "—" : `${format(s.values[i])}${unit}`}
                        </td>
                      ))}
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        ) : (
          <>
            <svg
              width={width}
              height={height}
              role="img"
              aria-label={ariaLabel}
              tabIndex={0}
              className="chart-svg"
              onPointerMove={pick}
              onPointerLeave={() => setHover(null)}
              onFocus={() => setHover(n - 1)}
              onBlur={() => setHover(null)}
              onKeyDown={onKeyDown}
            >
              {ticks.map((t) => (
                <g key={t}>
                  <line className="chart-grid" x1={MARGIN.left} x2={MARGIN.left + plotW} y1={y(t)} y2={y(t)} />
                  <text className="chart-tick" x={MARGIN.left - 8} y={y(t)} textAnchor="end" dominantBaseline="middle">
                    {format(t)}
                  </text>
                </g>
              ))}
              {xTickIdx.map((i) => (
                <text key={i} className="chart-tick" x={x(i)} y={height - 8} textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"}>
                  {formatClock(times[i] * 1000)}
                </text>
              ))}
              {paths.map((d, k) => (
                <path key={series[k].key} d={d} fill="none" stroke={series[k].color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
              ))}
              {ends.map((i, k) =>
                i < 0 ? null : (
                  <g key={series[k].key}>
                    <circle cx={x(i)} cy={y(series[k].values[i])} r="4" fill={series[k].color} stroke="#ffffff" strokeWidth="2" />
                    {endLabelsFit && (
                      <text className="chart-end-label" x={x(i) + 9} y={y(series[k].values[i])} dominantBaseline="middle">
                        {format(series[k].values[i])}
                        {unit}
                      </text>
                    )}
                  </g>
                ),
              )}
              {hover != null && (
                <g>
                  <line className="chart-crosshair" x1={x(hover)} x2={x(hover)} y1={MARGIN.top} y2={MARGIN.top + plotH} />
                  {series.map((s) =>
                    s.values[hover] == null ? null : (
                      <circle key={s.key} cx={x(hover)} cy={y(s.values[hover])} r="4" fill={s.color} stroke="#ffffff" strokeWidth="2" />
                    ),
                  )}
                </g>
              )}
            </svg>
            {hover != null && (
              <div className="chart-tooltip" role="status" style={{ left: `${tooltipLeft}px`, top: `${MARGIN.top}px` }}>
                <div className="tt-time">{formatClock(times[hover] * 1000)}</div>
                {series.map((s) => (
                  <div key={s.key} className="tt-row">
                    <LineKey color={s.color} />
                    <strong>{s.values[hover] == null ? "—" : `${format(s.values[hover])}${unit}`}</strong>
                    <span className="muted">{s.label}</span>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>
      <p className="chart-foot muted small">Último dato: {lastTime}</p>
    </article>
  );
}
