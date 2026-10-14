import { useState, useCallback } from 'react';
import {
  buildSequencedShow, computeAssignmentChain, computeAssignmentChainLocal,
} from '../engine/sequencer.js';
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
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const build = useCallback(async (scenes, numDrones, rate) => {
    if (!scenes.length) {
      setError('Add at least one scene.');
      return null;
    }
    setLoading(true);
    setError(null);

    // The morphs are only collision-free under an optimal assignment, so a
    // backend that's down falls back to solving it in the browser rather than
    // to the naive drone i → point i mapping.
    let targetIdx;
    try {
      targetIdx = await computeAssignmentChain(scenes, numDrones, API_BASE);
    } catch (e) {
      console.warn('Backend assignment unavailable, solving locally:', e.message);
      targetIdx = computeAssignmentChainLocal(scenes, numDrones);
    }

    let show;
    try {
      show = buildSequencedShow(scenes, numDrones, Number(rate), targetIdx);
      setChoreoData(show);
      setValidation(null);
    } catch (e) {
      setError(`Sequence build failed: ${e.message}`);
      setLoading(false);
      return null;
    }
    setLoading(false);

    // Validate in the background — preview works, but export is locked until PASSED.
    validateShow(show)
      .then((v) => {
        if (v && v.metrics) setValidation(v);
        else setValidation({ status: 'UNVERIFIED', summary: 'Invalid response from validator.' });
      })
      .catch((err) => {
        setValidation({ status: 'UNVERIFIED', summary: `Validation service unavailable: ${err?.message || 'offline'}` });
      });

    return show;
  }, []);

  const clear = useCallback(() => {
    setChoreoData(null);
    setValidation(null);
    setError(null);
  }, []);

  return { choreoData, setChoreoData, validation, setValidation, loading, error, setError, build, clear };
}
