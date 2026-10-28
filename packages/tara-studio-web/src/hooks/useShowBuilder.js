import { useState, useCallback } from 'react';
import {
  buildSequencedShow, computeAssignmentChain, computeAssignmentChainLocal,
} from '../engine/sequencer.js';
import { ShowRefusalError } from '../engine/showErrors.js';
import { API_BASE, validateShow } from '../engine/api.js';

/**
 * Builds a show from the scene list and runs it through backend validation.
 *
 * The assignment chain and the validation are both best-effort: neither should
 * be able to stop a designer from previewing their show. A backend that's down
 * degrades the result (naive drone→target mapping, no safety report) rather
 * than failing the build.
 */
export default function useShowBuilder() {
  const [choreoData, setChoreoData] = useState(null);
  const [validation, setValidation] = useState(null);
  const [validating, setValidating] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const build = useCallback(async (scenes, numDrones, rate) => {
    if (!scenes.length) {
      setError('Add at least one scene.');
      return null;
    }
    setLoading(true);
    setError(null);

    let show;
    try {
      // The morphs are only collision-free under an optimal assignment, so a
      // backend that's down falls back to solving it in the browser rather than
      // to the naive drone i → point i mapping.
      let targetIdx;
      try {
        targetIdx = await computeAssignmentChain(scenes, numDrones, API_BASE);
      } catch (e) {
        console.warn('Backend assignment unavailable, solving locally:', e.message);
        // computeAssignmentChainLocal builds every scene's own formation to
        // solve the chain (self-review, pre-merge): a shape/count combination
        // that can't fit the site throws ShowRefusalError here too, same as
        // buildSequencedShow below - previously uncaught at this specific
        // call site, so it escaped as an unhandled rejection and left
        // `loading` stuck true forever (the Generate Sequence button frozen
        // on "BUILDING…" with no error shown). Falls through to the same
        // classification and `setError` as the buildSequencedShow catch
        // below, instead of duplicating it.
        targetIdx = computeAssignmentChainLocal(scenes, numDrones);
      }

      show = buildSequencedShow(scenes, numDrones, Number(rate), targetIdx);
      setChoreoData(show);
      setValidation(null);
    } catch (e) {
      // A refusal (unsafe spacing/altitude/layering) is expected input and is
      // shown as the build error; anything else is a genuine bug and must not
      // be reported — or swallowed — as if it were the same thing.
      if (!(e instanceof ShowRefusalError)) throw e;
      setError(`Sequence build failed: ${e.message}`);
      return null;
    } finally {
      setLoading(false);
    }

    // Validate in the background — preview works, but export is locked until
    // PASSED. `validating` exists only so the UI can distinguish "never
    // validated yet" from "a validate_show + validate_dynamics sweep is
    // currently running" (issue #173: at 1000 drones this takes ~15s with
    // nothing on screen saying why) - not for sequencing, which already
    // happens naturally since validateShow isn't awaited before returning.
    setValidating(true);
    validateShow(show)
      .then((v) => {
        if (v && v.metrics) setValidation(v);
        else setValidation({ status: 'UNVERIFIED', summary: 'Invalid response from validator.' });
      })
      .catch((err) => {
        setValidation({ status: 'UNVERIFIED', summary: `Validation service unavailable: ${err?.message || 'offline'}` });
      })
      .finally(() => setValidating(false));

    return show;
  }, []);

  const clear = useCallback(() => {
    setChoreoData(null);
    setValidation(null);
    setValidating(false);
    setError(null);
  }, []);

  return {
    choreoData, setChoreoData, validation, setValidation, validating, loading, error, setError, build, clear,
  };
}
