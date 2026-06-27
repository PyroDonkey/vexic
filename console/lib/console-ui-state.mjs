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

function formatLabel(value) {
  return value.replace(/([A-Z])/g, " $1").replace(/^./, (match) => match.toUpperCase());
}
