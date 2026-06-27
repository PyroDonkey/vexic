import { cn } from "@/lib/utils";
import { usageMeterDisplay } from "@/lib/console-ui-state.mjs";

type UsageMeterProps = {
  label: string;
  value: number;
  max: number;
  className?: string;
};

export function UsageMeter({ label, value, max, className }: UsageMeterProps) {
  const meter = usageMeterDisplay(value, max);

  return (
    <div className={cn("grid gap-2", className)} data-tremor-id="usage-meter">
      <div className="flex items-center justify-between gap-3 text-sm">
        <span className="font-medium text-foreground">{label}</span>
        <span className="font-mono text-muted-foreground">{meter.valueLabel}</span>
      </div>
      <div
        aria-label={`${label} usage`}
        aria-valuemax={meter.hasCap ? max : undefined}
        aria-valuemin={meter.hasCap ? 0 : undefined}
        aria-valuenow={meter.hasCap ? (meter.ariaNow ?? undefined) : undefined}
        aria-valuetext={meter.hasCap ? meter.ariaText : undefined}
        className="h-2 overflow-hidden rounded-full bg-muted"
        role={meter.hasCap ? "progressbar" : undefined}
      >
        <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${meter.percentage}%` }} />
      </div>
      <span className="text-xs text-muted-foreground">{meter.statusLabel}</span>
    </div>
  );
}
