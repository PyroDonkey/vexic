import * as React from "react"

import { cn } from "@/lib/utils"

type BarListItem = {
  key?: string
  name: string
  value: number
  valueLabel?: string
}

type BarListProps = React.HTMLAttributes<HTMLDivElement> & {
  data: BarListItem[]
  valueFormatter?: (value: number) => string
}

export function BarList({ className, data, valueFormatter = formatNumber, ...props }: BarListProps) {
  const maxValue = Math.max(...data.map((item) => item.value), 0)

  return (
    <div className={cn("grid gap-2", className)} data-tremor-id="bar-list" {...props}>
      {data.map((item) => {
        const width = maxValue === 0 || item.value === 0 ? 0 : Math.max((item.value / maxValue) * 100, 2)

        return (
          <div key={item.key ?? item.name} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-4">
            <div className="relative h-8 overflow-hidden rounded-sm bg-muted">
              <div className="h-full rounded-sm bg-primary/30" style={{ width: `${width}%` }} />
              <span className="absolute inset-y-0 left-2 flex max-w-[calc(100%-1rem)] items-center truncate text-sm text-foreground">
                {item.name}
              </span>
            </div>
            <span className="font-mono text-sm text-foreground">{item.valueLabel ?? valueFormatter(item.value)}</span>
          </div>
        )
      })}
    </div>
  )
}

function formatNumber(value: number) {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(value)
}
