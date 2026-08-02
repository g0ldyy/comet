import type { ReactNode } from "react";

function sparkline(values: number[]) {
  const minimum = Math.min(...values);
  const range = Math.max(...values) - minimum || 1;
  const points = values.map((value, index) => ({
    x: (index / (values.length - 1)) * 100,
    y: 28 - ((value - minimum) / range) * 24,
  }));
  return {
    end: points[points.length - 1] as { x: number; y: number },
    points: points.map(({ x, y }) => `${x},${y}`).join(" "),
  };
}

export function MetricCard({
  detail,
  label,
  signal,
  tone = "default",
  value,
}: {
  detail?: ReactNode;
  label: string;
  signal?: number[];
  tone?: "danger" | "default" | "live" | "usenet" | "warning";
  value: ReactNode;
}) {
  const line = signal && signal.length > 1 ? sparkline(signal) : null;
  return (
    <article className={`metric-card metric-card--${tone}`}>
      <span className="metric-card__label">{label}</span>
      <strong className="metric-card__value" key={String(value)}>
        {value}
      </strong>
      {detail === undefined ? null : <small className="metric-card__detail">{detail}</small>}
      {line ? (
        <svg
          aria-hidden="true"
          className="metric-card__signal"
          preserveAspectRatio="none"
          viewBox="0 0 100 32"
        >
          <polyline points={line.points} vectorEffect="non-scaling-stroke" />
          <circle cx={line.end.x} cy={line.end.y} r="1.8" vectorEffect="non-scaling-stroke" />
        </svg>
      ) : null}
    </article>
  );
}
