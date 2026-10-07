import { getRunSeries } from './resultExplorer.js';
const finite = value => typeof value === 'number' && Number.isFinite(value);
export function resourceSeries(run) {
  return getRunSeries(run).map(point => {
    const row = { ...point };
    for (const [field, display, scale] of [
      ['gpu_framebuffer_used_bytes', 'gpu_framebuffer_used_gib', 2 ** 30],
      ['gpu_memory_usage_bytes', 'gpu_memory_usage_gib', 2 ** 30],
      ['cpu_memory_usage_bytes', 'cpu_memory_usage_gib', 2 ** 30],
      ['network_receive_bytes_per_second', 'network_receive_mib_s', 2 ** 20],
      ['network_transmit_bytes_per_second', 'network_transmit_mib_s', 2 ** 20],
    ]) if (finite(point[field])) row[display] = point[field] / scale;
    return row;
  });
}
