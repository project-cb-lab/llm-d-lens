const valid = point => Number.isFinite(point?.elapsed) && Number.isFinite(Date.parse(point?.timestamp));
export function historicalBounds(data = []) {
  const points = data.filter(valid).sort((a, b) => a.elapsed - b.elapsed);
  if (!points.length) return null;
  const first = points[0], last = points[points.length - 1];
  return { start: first.elapsed, end: last.elapsed, origin: Date.parse(first.timestamp) - first.elapsed * 1000, count: points.length };
}
export function historicalPreset(data, seconds) {
  const bounds = historicalBounds(data);
  return bounds ? [Math.max(bounds.start, bounds.end - seconds), bounds.end] : null;
}
export function parseHistoricalRange(data, start, end) {
  const bounds = historicalBounds(data);
  if (!bounds) return { error: 'No saved timestamps are available.' };
  const parseUtc = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,3})?)?$/.test(value) ? Date.parse(`${value}Z`) : NaN;
  const low = (parseUtc(start) - bounds.origin) / 1000, high = (parseUtc(end) - bounds.origin) / 1000;
  if (!Number.isFinite(low) || !Number.isFinite(high)) return { error: 'Enter both timestamps in UTC.' };
  if (high < low) return { error: 'End time must not be before start time.' };
  if (low < bounds.start || high > bounds.end) return { error: 'Choose times inside the saved sample window.' };
  return { range: [Math.max(bounds.start, low), Math.max(bounds.start, Math.min(bounds.end, high))] };
}
