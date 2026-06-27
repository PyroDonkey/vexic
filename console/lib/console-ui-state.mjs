export function projectCreateFailureMessage(status) {
  return status === 403 ? "Project creation requires an active organization." : "Project creation failed. Try again.";
}

export function usageRows(usage) {
  return Object.entries(usage.totals).map(([key, value]) => ({
    key,
    label: formatLabel(key),
    value,
    max: usage.caps[key] ?? 0
  }));
}

export function usageMeterDisplay(value, max) {
  if (max <= 0) {
    return {
      hasCap: false,
      percentage: 0,
      valueLabel: `${formatNumber(value)} / No cap`,
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

function formatLabel(value) {
  return value.replace(/([A-Z])/g, " $1").replace(/^./, (match) => match.toUpperCase());
}

function formatNumber(value) {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(value);
}
