// The one definition of "may this show be exported", shared by everything that
// shows or enforces it in the UI (issue #132, found in review of PR #210: the
// header badge read "export open" while the Transmit tab's buttons were locked,
// because the two had grown separate, different predicates).
//
// This is the rule the Transmit tab has always applied (issues #139, #151,
// #165): export needs a built show AND a PASSED validation AND no jerk
// violations AND feasibility not explicitly refused. The backend re-checks on
// export and stays the authority; this only decides what the UI offers.

const jerkViolationCount = (validation) => validation?.violations?.jerk?.length || 0;

/**
 * @param {{hasShow: boolean, validation: object|null|undefined}} input
 * @returns {{
 *   canExport: boolean,
 *   kind: 'none' | 'open' | 'failed' | 'unverified',
 *   isCertified: boolean,
 *   isFailed: boolean,
 *   isUnverified: boolean,
 *   notFlightCertified: boolean,
 * }}
 *   kind 'none'       - no show is built, so there is nothing to gate.
 *   kind 'open'       - the export buttons are enabled.
 *   kind 'failed'     - validation failed, or it passed its hard limits but not
 *                       the jerk / feasibility certification.
 *   kind 'unverified' - a show exists but there is no conclusive PASSED/FAILED
 *                       result (not run yet, still running, or inconclusive).
 *   isCertified / isFailed / isUnverified are the Transmit tab's own flags,
 *   unchanged, so it can render its messages from the same call.
 *   notFlightCertified is the 'failed' sub-case where the validator did not say
 *   FAILED: jerk violations, or feasibility_certified === false, say the show
 *   clears its hard limits but is not flight-certified.
 */
export function exportGate({ hasShow, validation }) {
  const isCertified = validation?.status === 'PASSED'
    && validation?.feasibility_certified !== false
    && jerkViolationCount(validation) === 0;
  const isFailed = validation?.status === 'FAILED'
    || jerkViolationCount(validation) > 0
    || (validation?.status === 'PASSED' && validation?.feasibility_certified === false);
  const isUnverified = !!hasShow && !isCertified && !isFailed;
  const canExport = !!hasShow && isCertified;
  const notFlightCertified = isFailed && validation?.status !== 'FAILED';

  let kind = 'unverified';
  if (!hasShow) kind = 'none';
  else if (canExport) kind = 'open';
  else if (isFailed) kind = 'failed';

  return { canExport, kind, isCertified, isFailed, isUnverified, notFlightCertified };
}

export const canExport = (input) => exportGate(input).canExport;
