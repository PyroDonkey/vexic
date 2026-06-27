import { cn } from "@/lib/utils";

type UsageMeterProps = {
  label: string;
  value: number;
  max: number;
  className?: string;
};

export function UsageMeter({ label, value, max, className }: UsageMeterProps) {
  const percentage = max > 0 ? Math.min(100, (value / max) * 100) : 0;

  return (
    <div className={cn("grid gap-2", className)} data-tremor-id="usage-meter">
      <div className="flex items-center justify-between gap-3 text-sm">
        <span className="font-medium text-foreground">{label}</span>
        <span className="font-mono text-muted-foreground">
          {formatNumber(value)} / {formatNumber(max)}
        </span>
      </div>
      <div
        aria-label={`${label} usage`}
        aria-valuemax={max}
        aria-valuemin={0}
        aria-valuenow={value}
        className="h-2 overflow-hidden rounded-full bg-muted"
        role="progressbar"
      >
        <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${percentage}%` }} />
      </div>
      <span className="text-xs text-muted-foreground">{percentage.toFixed(1)}% used</span>
    </div>
  );
}

function formatNumber(value: number) {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(value);
}
