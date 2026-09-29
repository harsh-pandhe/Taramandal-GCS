import { describe, expect, it } from 'vitest';
import {
  DEFAULT_BATTERY,
  formatClock,
  linePath,
  loadBatterySettings,
  nearestIndex,
  saveBatterySettings,
  scaleLinear,
  tableRows,
} from './energy.js';

const memoryStorage = () => {
  const data = new Map();
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => data.set(k, String(v)),
  };
};

const throwingStorage = {
  getItem: () => { throw new Error('blocked'); },
  setItem: () => { throw new Error('blocked'); },
};

describe('battery settings persistence', () => {
  it('round-trips the pack through storage', () => {
    const storage = memoryStorage();
    const pack = { chemistry: 'liion', cells_series: 6, capacity_mah: 10000, ambient_temp_c: -5 };
    saveBatterySettings(pack, storage);
    expect(loadBatterySettings(storage)).toEqual(pack);
  });

  it('falls back to the defaults when nothing is saved', () => {
    expect(loadBatterySettings(memoryStorage())).toEqual(DEFAULT_BATTERY);
  });

  it('survives storage that throws, as in a private window', () => {
    expect(loadBatterySettings(throwingStorage)).toEqual(DEFAULT_BATTERY);
    expect(() => saveBatterySettings(DEFAULT_BATTERY, throwingStorage)).not.toThrow();
  });

  it('ignores corrupt or wrongly-typed saved values field by field', () => {
    const storage = memoryStorage();
    storage.setItem('tara-studio.battery', JSON.stringify({ chemistry: 'lipo', cells_series: 'six', capacity_mah: 2200 }));
    expect(loadBatterySettings(storage)).toEqual({ ...DEFAULT_BATTERY, capacity_mah: 2200 });
    storage.setItem('tara-studio.battery', '{not json');
    expect(loadBatterySettings(storage)).toEqual(DEFAULT_BATTERY);
  });
});

describe('chart geometry', () => {
  it('maps a domain onto a range, flipping for SVG y', () => {
    const y = scaleLinear([0, 100], [150, 10]);
    expect(y(0)).toBe(150);
    expect(y(100)).toBe(10);
    expect(y(50)).toBe(80);
  });

  it('maps a zero-width domain to the range start instead of NaN', () => {
    expect(scaleLinear([5, 5], [0, 100])(5)).toBe(0);
  });

  it('draws a polyline through every sample', () => {
    const id = (v) => v;
    expect(linePath([0, 1, 2], [3, 4, 5], id, id)).toBe('M0.0,3.0L1.0,4.0L2.0,5.0');
  });

  it('snaps the crosshair to the nearest sample', () => {
    const times = [0, 1, 2, 4, 8];
    expect(nearestIndex(times, -3)).toBe(0);
    expect(nearestIndex(times, 1.4)).toBe(1);
    expect(nearestIndex(times, 3.1)).toBe(3);
    expect(nearestIndex(times, 99)).toBe(4);
    expect(nearestIndex([], 1)).toBe(-1);
  });

  it('thins the table view but always keeps the last sample', () => {
    expect(tableRows(5)).toEqual([0, 1, 2, 3, 4]);
    const rows = tableRows(400, 12);
    expect(rows.length).toBeLessThanOrEqual(13);
    expect(rows[0]).toBe(0);
    expect(rows[rows.length - 1]).toBe(399);
    expect(tableRows(0)).toEqual([]);
  });

  it('formats show time as m:ss', () => {
    expect(formatClock(0)).toBe('0:00');
    expect(formatClock(75.4)).toBe('1:15');
    expect(formatClock(1210)).toBe('20:10');
  });
});
