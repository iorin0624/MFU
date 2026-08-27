export function percentile(sortedValues, percentileValue) {
  if (!sortedValues.length) return null;
  const index = Math.ceil((percentileValue / 100) * sortedValues.length) - 1;
  return sortedValues[Math.max(0, Math.min(sortedValues.length - 1, index))];
}

export function summarizeErrors(values) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mean = values.reduce((sum, value) => sum + value, 0) / values.length;
  const variance = values.reduce((sum, value) => sum + ((value - mean) ** 2), 0) / values.length;
  const middle = Math.floor(sorted.length / 2);
  const median = sorted.length % 2
    ? sorted[middle]
    : (sorted[middle - 1] + sorted[middle]) / 2;
  return {
    count: values.length,
    mean,
    median,
    earliest: sorted[0],
    latest: sorted[sorted.length - 1],
    standardDeviation: Math.sqrt(variance),
    p95: percentile(sorted, 95),
    p99: percentile(sorted, 99)
  };
}
