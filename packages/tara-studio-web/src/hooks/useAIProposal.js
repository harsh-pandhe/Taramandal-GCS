import { useCallback, useState } from 'react';
import { proposeScene } from '../engine/api.js';

/**
 * Narrow local AI authoring (issue #133): holds the in-flight prompt/SVG and
 * the last proposal, independent of the console drawer's tab, so switching
 * away to look at (or edit) the scene list and back doesn't lose a pending
 * proposal — which is also how staleness is actually observable: it has to
 * survive long enough to be compared against a document that changed while
 * the operator wasn't looking at this tab.
 */
export default function useAIProposal(scenes, addScene) {
  const [prompt, setPrompt] = useState('');
  const [svg, setSvg] = useState(null); // {name, text} | null
  const [proposing, setProposing] = useState(false);
  const [proposal, setProposal] = useState(null); // the typed draft, once received
  const [requestError, setRequestError] = useState(null);
  // The scene list's identity at the moment the proposal was generated, so an
  // edit made elsewhere (add/delete/reorder a scene) while this proposal is
  // sitting unreviewed can be detected before Apply acts on stale context.
  const [baselineIds, setBaselineIds] = useState(null);

  const stale = proposal != null && proposal.status === 'ok' && (
    baselineIds == null
    || scenes.length !== baselineIds.length
    || scenes.some((s, i) => s.id !== baselineIds[i])
  );

  const propose = useCallback(async () => {
    if (!prompt.trim() || proposing) return;
    setProposing(true);
    setRequestError(null);
    setProposal(null);
    try {
      const result = await proposeScene(prompt.trim(), svg?.text ?? null);
      setProposal(result);
      setBaselineIds(scenes.map((s) => s.id));
    } catch (err) {
      setRequestError(err.message);
    } finally {
      setProposing(false);
    }
  }, [prompt, proposing, svg, scenes]);

  const apply = useCallback(() => {
    if (!proposal || proposal.status !== 'ok' || stale) return;
    addScene({
      name: proposal.name,
      shapeType: proposal.shape_type,
      shapePayload: proposal.shape_payload,
      scale: proposal.scale,
      holdDurationSeconds: proposal.hold_seconds,
      transitionDurationSeconds: proposal.transition_seconds,
    });
    setProposal(null);
    setBaselineIds(null);
  }, [proposal, stale, addScene]);

  const discard = useCallback(() => {
    setProposal(null);
    setBaselineIds(null);
  }, []);

  return {
    prompt, setPrompt, svg, setSvg, proposing, proposal, requestError, stale,
    propose, apply, discard,
  };
}
