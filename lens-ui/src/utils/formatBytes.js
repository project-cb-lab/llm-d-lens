const BINARY_UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];

/** Scale bytes only; callers retain input validation, precision and empty labels. */
export function scaleBytes(bytes, maxUnitIndex = BINARY_UNITS.length - 1) {
    let value = Number(bytes);
    let unitIndex = 0;
    while (value >= 1024 && unitIndex < Math.min(maxUnitIndex, BINARY_UNITS.length - 1)) {
        value /= 1024;
        unitIndex += 1;
    }
    return { value, unit: BINARY_UNITS[unitIndex], unitIndex };
}
