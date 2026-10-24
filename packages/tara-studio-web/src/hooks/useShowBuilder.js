import { useState, useCallback } from 'react';
import {
  buildSequencedShow, computeAssignmentChain, computeAssignmentChainLocal, MorphRefusalError,
} from '../engine/sequencer.js';
import { API_BASE, validateShow } from '../engine/api.js';

/**
 * Assignment chain, then the checked build. Throws:
 *   - MorphRefusalError when a formation or morph is refused (an expected,
 *     actionable outcome; never retried, since the local solver would refuse the
 *     same scene);
 *   - anything else unchanged (a bug or corrupt data), never turned into a
 *     show. In particular a corrupt assignment reaches buildSequencedShow, which
 *     rejects it.
 * Only an unreachable or malformed backend response falls back to solving the
 * assignment in the browser; the optimal mapping is what makes the morphs
 * collision-free, so the naive drone i -> point i mapping is never substituted.
 * `deps` exists so tests can inject failures.
 */
export async function assignAndBuild(scenes, numDrones, rate, deps = {}) {
  const {
    chain = computeAssignmentChain,
    chainLocal = computeAssignmentChainLocal,
    build = buildSequencedShow,
    apiBase = API_BASE,
  } = deps;
  let targetIdx;
  try {
    targetIdx = await chain(scenes, numDrones, apiBase);
  } catch (e) {
    if (e instanceof MorphRefusalError) throw e;
    console.warn('Backend assignment unavailable, solving locally:', e.message);
    targetIdx = chainLocal(scenes, numDrones);
  }
  return build(scenes, numDrones, Number(rate), targetIdx);
}

/**
 * Builds a show from the scene list and runs it through backend validation.
 *
 * The backend assignment and the validation are best-effort: a backend that is
 * down degrades the result (local assignment, no safety report) rather than
 * failing the build. Safety refusals are not best-effort: they are shown to the
 * designer and no show is produced.
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

    let show;
    try {
      show = await assignAndBuild(scenes, numDrones, rate);
      setChoreoData(show);
      setValidation(null);
    } catch (e) {
      if (e instanceof MorphRefusalError) {
        setError(`Sequence build failed: ${e.message}`);
      } else {
        // Not a safety refusal: a bug or corrupt data. Keep the full error
        // visible and produce nothing.
        console.error(e);
        setError(`Unexpected error while building the show (${e?.name}: ${e?.message}). This is a bug, not a safety refusal; nothing was built.`);
      }
      return null;
    } finally {
      setLoading(false);
    }

    // Validate in the background — the preview is already usable.
    validateShow(show)
      .then((v) => { if (v && v.metrics) setValidation(v); })
      .catch(() => { /* backend offline; preview still works */ });

    return show;
  }, []);

  const clear = useCallback(() => {
    setChoreoData(null);
    setValidation(null);
    setError(null);
  }, []);

  return { choreoData, setChoreoData, validation, setValidation, loading, error, setError, build, clear };
}
