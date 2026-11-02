import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { createElement } from 'react';
import { canExport, exportGate } from './exportGate.js';
import SequencerHeader from '../components/sequencer/SequencerHeader.jsx';
import TransmitPanel from '../components/panels/TransmitPanel.jsx';

const CLEAN = { proximity: [], velocity: [], acceleration: [], yaw_rate: [], jerk: [] };
const passed = (extra = {}) => ({ status: 'PASSED', violations: CLEAN, ...extra });

// Every state the gate can be in, named for what an operator would call it.
const CASES = [
  ['PASSED, certified, show built', { hasShow: true, validation: passed() }, true, 'open'],
  ['PASSED with feasibility_certified true', { hasShow: true, validation: passed({ feasibility_certified: true }) }, true, 'open'],
  ['PASSED with feasibility_certified false', { hasShow: true, validation: passed({ feasibility_certified: false }) }, false, 'failed'],
  ['PASSED with jerk violations', { hasShow: true, validation: passed({ violations: { ...CLEAN, jerk: [{ time: 1 }] } }) }, false, 'failed'],
  ['PASSED with no show built', { hasShow: false, validation: passed() }, false, 'none'],
  ['FAILED', { hasShow: true, validation: passed({ status: 'FAILED' }) }, false, 'failed'],
  ['UNVERIFIED', { hasShow: true, validation: { status: 'UNVERIFIED' } }, false, 'unverified'],
  ['no validation yet', { hasShow: true, validation: null }, false, 'unverified'],
  ['nothing at all', { hasShow: false, validation: null }, false, 'none'],
];

describe('exportGate', () => {
  it.each(CASES)('%s', (_name, input, expectedCan, expectedKind) => {
    expect(canExport(input)).toBe(expectedCan);
    expect(exportGate(input).kind).toBe(expectedKind);
  });

  it('flags jerk / feasibility refusals as not flight-certified, and a plain FAILED as not', () => {
    expect(exportGate({ hasShow: true, validation: passed({ feasibility_certified: false }) }).notFlightCertified).toBe(true);
    expect(exportGate({ hasShow: true, validation: passed({ status: 'FAILED' }) }).notFlightCertified).toBe(false);
  });
});

// The review finding itself: the header and the Transmit tab must never disagree.
describe('header badge and Transmit buttons share one verdict', () => {
  const noop = () => {};
  const panel = (input) => renderToStaticMarkup(createElement(TransmitPanel, {
    homeLat: 12.97, homeLon: 77.59, onHomeLat: noop, onHomeLon: noop,
    onExport: noop, onExportPreview: noop, onSendToGCS: noop, sending: false, ...input,
  }));
  const header = (input) => renderToStaticMarkup(createElement(SequencerHeader, {
    sceneCount: 2, numDrones: 6, rate: 10, predictedDuration: 20, builtDuration: input.hasShow ? 20 : 0,
    stale: false, predictionRefused: false, refusalMessage: null, ...input,
  }));

  it.each(CASES)('%s', (_name, input) => {
    const html = panel(input);
    // The JSON export button: its markup carries `disabled` exactly when locked.
    const jsonButton = html.match(/<button[^>]*>(?:(?!<\/button>).)*JSON<\/button>/s)[0];
    const buttonsEnabled = !/\sdisabled/.test(jsonButton);
    const badgeSaysEnabled = header(input).includes('exports enabled');
    expect(badgeSaysEnabled).toBe(buttonsEnabled);
  });
});
