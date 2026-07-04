"use client";

type DailyRow = {
  date: string;
  writes: number;
  retrievals: number;
  other: number;
};

const SEGMENTS = [
  { key: "writes" as const, label: "Writes", className: "bg-chart-1" },
  { key: "retrievals" as const, label: "Retrievals", className: "bg-chart-2" },
  { key: "other" as const, label: "Other", className: "bg-chart-3" }
];

export function DailyBars({ rows }: { rows: DailyRow[] }) {
  if (rows.length === 0) {
    return <p className="text-sm text-muted-foreground">No usage recorded in the last 30 days.</p>;
  }
  const maxTotal = Math.max(...rows.map((row) => row.writes + row.retrievals + row.other), 1);
  return (
    <div>
      <div className="flex h-40 items-end gap-1" role="img" aria-label="Daily operations, last 30 days">
        {rows.map((row) => {
          const total = row.writes + row.retrievals + row.other;
          return (
            <div
              key={row.date}
              className="flex flex-1 flex-col justify-end"
              title={`${row.date}: ${row.writes} writes, ${row.retrievals} retrievals, ${row.other} other`}
            >
              {SEGMENTS.map(({ key, className }) =>
                row[key] > 0 ? (
                  <div
                    key={key}
                    className={className}
                    style={{ height: `${(row[key] / maxTotal) * 100}%` }}
                  />
                ) : null
              )}
              <span className="sr-only">{`${row.date}: ${total} operations`}</span>
            </div>
          );
        })}
      </div>
      <div className="mt-2 flex gap-4 text-xs text-muted-foreground">
        {SEGMENTS.map(({ key, label, className }) => (
          <span key={key} className="inline-flex items-center gap-1">
            <span className={`inline-block size-2 rounded-sm ${className}`} />
            {label}
          </span>
        ))}
      </div>
    </div>
  );
}
