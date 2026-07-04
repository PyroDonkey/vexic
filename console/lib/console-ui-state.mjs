export function projectCreateFailureMessage(status) {
  return status === 403 ? "Project creation requires an active organization." : "Project creation failed. Try again.";
}

export function usageRows(usage) {
  return Object.entries(usage.totals).map(([key, value]) => ({
    key,
    label: formatLabel(key),
    value,
    max: usage.caps[key] ?? 0,
    ...(key === "cost" ? { valueLabel: formatCost(value) } : {})
  }));
}

export function usageMeterDisplay(value, max, valueLabel = formatNumber(value)) {
  if (max <= 0) {
    return {
      hasCap: false,
      percentage: 0,
      valueLabel: `${valueLabel} / No cap`,
      statusLabel: "No cap",
      ariaNow: null,
      ariaText: "No cap"
    };
  }

  const percentage = Math.min(100, (value / max) * 100);
  const ariaNow = Math.min(value, max);
  const ariaText =
    value > max
      ? `${formatNumber(value)} of ${formatNumber(max)} (over cap)`
      : `${formatNumber(value)} of ${formatNumber(max)}`;

  return {
    hasCap: true,
    percentage,
    valueLabel: `${formatNumber(value)} / ${formatNumber(max)}`,
    statusLabel: `${percentage.toFixed(1)}% used`,
    ariaNow,
    ariaText
  };
}

const STALE_KEY_DAYS = 30;

export function keyFreshness(lastUsedAt, nowIso = new Date().toISOString()) {
  if (!lastUsedAt) {
    return { label: "Never used", stale: false };
  }
  const last = new Date(lastUsedAt);
  const ageDays = (new Date(nowIso).getTime() - last.getTime()) / 86_400_000;
  return {
    label: last.toLocaleDateString(undefined, { dateStyle: "medium" }),
    stale: ageDays > STALE_KEY_DAYS
  };
}

function formatLabel(value) {
  return value.replace(/([A-Z])/g, " $1").replace(/^./, (match) => match.toUpperCase());
}

function formatNumber(value) {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(value);
}

function formatCost(value) {
  return new Intl.NumberFormat(undefined, {
    currency: "USD",
    currencyDisplay: "narrowSymbol",
    style: "currency"
  }).format(value);
}
