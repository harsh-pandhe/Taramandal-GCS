// Safety check for a proposed scene edit (issue #89). Builds and validates the
// document a proposal WOULD produce, so the panel can say PASSED / FAILED before
// anything is applied. It is a dry run: it takes the after-document and two
// injected functions and returns a plain result. It never touches the Studio
// history, the show builder state or the project vault, and never throws.
//
//   build(afterDoc)  -> show (async ok); may throw ShowRefusalError
//   validate(show)   -> backend report { status, summary, metrics, violations }

import { ShowRefusalError } from './showErrors.js';
import {
  buildSequencedShow, computeAssignmentChain, computeAssignmentChainLocal,
} from './sequencer.js';
import { API_BASE, validateShow } from './api.js';

const unverified = (summary) => ({ status: 'UNVERIFIED', summary, violations: {} });

export const checkProposal = async ({ afterDoc, build, validate }) => {
  let show;
  try {
    show = await build(afterDoc);
  } catch (e) {
    if (e instanceof ShowRefusalError) {
      return { status: 'REFUSED', summary: e.message, violations: {} };
    }
    return unverified(`Could not build the edited show: ${e?.message || 'unknown error'}`);
  }

  let report;
  try {
    report = await validate(show);
  } catch (e) {
    return unverified(`Validation service unavailable: ${e?.message || 'offline'}`);
  }
  if (!report || !report.metrics) return unverified('Invalid response from validator.');

  const status = ['PASSED', 'FAILED'].includes(report.status) ? report.status : 'UNVERIFIED';
  return {
    status,
    summary: report.summary || (status === 'PASSED' ? 'Passed all safety checks.' : 'Not verified.'),
    violations: report.violations ?? {},
  };
};

/**
 * The app's own build for `afterDoc`, as useShowBuilder.build does it (assignment
 * chain from the backend, solved locally if it is down, then buildSequencedShow)
 * but returning the show instead of storing it anywhere.
 */
export const buildForCheck = async (afterDoc, { numDrones, rate, pacing }) => {
  let targetIdx;
  try {
    targetIdx = await computeAssignmentChain(afterDoc.scenes, numDrones, API_BASE);
  } catch {
    targetIdx = computeAssignmentChainLocal(afterDoc.scenes, numDrones);
  }
  return buildSequencedShow(afterDoc.scenes, numDrones, Number(rate), targetIdx, pacing);
};

export { validateShow as validateForCheck };
