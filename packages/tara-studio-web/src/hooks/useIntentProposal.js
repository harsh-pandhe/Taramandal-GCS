import { useCallback, useRef, useState } from 'react';
import { parseIntent } from '../engine/intentParser.js';
import { previewBatch } from '../engine/commands.js';
import { createLatestRun } from '../engine/latestRun.js';
import { checkProposal, buildForCheck, validateForCheck } from '../engine/proposalCheck.js';
import { applyBlock, describeChanges } from '../engine/proposalView.js';
import { proposePlan } from '../engine/api.js';
import { compilePlan, summariseScenes } from '../engine/planCompile.js';

// Plain statuses for a planner call that produced nothing to review (same wording
// style as the AI panel's). Offline is reported, never papered over.
export const PLAN_MESSAGES = {
  unavailable: 'Ollama is not reachable. Make sure it is running locally, then try again.',
  timeout: 'The local model took too long to respond. Try again, or a shorter request.',
};

/**
 * The typed scene-edit proposal (issue #89): one sentence -> a pinned batch
 * command -> a dry-run safety check -> an explicit Apply that is one undo step.
 *
 * Nothing here writes to Studio state until `apply`. The check builds and
 * validates the edited scenes on its own (engine/proposalCheck.js) and keeps
 * its result here - never in the show builder, the history or the project.
 * Its own latest-run gate means an older check can never fill in the result of
 * a newer proposal, so a stale PASSED cannot enable Apply.
 *
 * A proposal has two sources that end in the same place: the fixed-grammar
 * parser (`preview`) and the local planner (`plan`, issue #94). The planner only
 * drafts: its answer is compiled to one scene/batch (engine/planCompile.js) and
 * from there goes through exactly the same preview, safety check and applyBlock.
 */
export default function useIntentProposal({ doc, execute, numDrones, rate, pacing }) {
  const [text, setText] = useState('');
  const [proposal, setProposal] = useState(null); // { command, summary, context, changes }
  const [check, setCheck] = useState(null);       // { status, summary, violations } | { status: 'CHECKING' }
  const [error, setError] = useState(null);
  const [planning, setPlanning] = useState(false);
  const [planStatus, setPlanStatus] = useState(null); // why the planner produced no proposal
  const runRef = useRef(null);
  if (runRef.current === null) runRef.current = createLatestRun();

  const context = `${numDrones}|${rate}|${pacing}`;
  // What the click handlers read, so they judge the document as it is when the
  // click happens, not as it was at the last render of an old closure.
  const live = useRef({});
  live.current = { doc, execute, numDrones, rate, pacing, context, proposal, check, text };

  const clear = useCallback(() => {
    runRef.current.invalidate();
    setProposal(null);
    setCheck(null);
    setError(null);
    setPlanning(false);
    setPlanStatus(null);
  }, []);

  // Shared by both sources: `parsed` is { ok, command, summary } | { ok: false, reason }.
  // Dry-runs it against the document as it is now, then shows it and starts the safety check.
  const showProposal = useCallback(async (parsed) => {
    const cur = live.current;
    const dry = parsed.ok ? previewBatch(cur.doc, parsed.command) : null;
    if (!parsed.ok || !dry.ok) {
      setProposal(null);
      setCheck(null);
      setError(parsed.ok ? dry.reason : parsed.reason);
      return;
    }
    setError(null);
    setProposal({
      command: parsed.command, summary: parsed.summary, context: cur.context,
      // Parts of a planner request it could not do, so the operator is told rather than left to assume.
      unsupported: parsed.unsupported ?? [],
      // Fixed now, so the list still reads right if the scenes change underneath it.
      changes: describeChanges(cur.doc, parsed.command),
    });
    setCheck({ status: 'CHECKING' });
    const id = runRef.current.begin();
    const result = await checkProposal({
      afterDoc: dry.doc,
      build: (afterDoc) => buildForCheck(afterDoc, { numDrones: cur.numDrones, rate: cur.rate, pacing: cur.pacing }),
      validate: validateForCheck,
    });
    if (runRef.current.isCurrent(id)) setCheck(result);
  }, []);

  const preview = useCallback(async () => {
    const cur = live.current;
    runRef.current.invalidate();
    setPlanning(false);
    setPlanStatus(null);
    await showProposal(parseIntent(cur.text, cur.doc));
  }, [showProposal]);

  // The local planner. At most two model calls per request: the first, and one
  // repair whose note is the exact reason the first draft was refused (by the
  // schema check or by the batch dry-run). Then it stops and says why. A newer
  // request, a preview or a dismiss invalidates a call still in flight.
  const plan = useCallback(async () => {
    const cur = live.current;
    const prompt = cur.text.trim();
    if (!prompt) return;
    const doc = cur.doc; // compiled against the document as of the request
    const run = runRef.current;
    const id = run.begin();
    setProposal(null);
    setCheck(null);
    setError(null);
    setPlanStatus(null);
    setPlanning(true);
    const scenes = summariseScenes(doc);
    const stop = (message) => {
      if (!run.isCurrent(id)) return;
      setPlanning(false);
      setPlanStatus(message);
    };

    // Whatever goes wrong below, `planning` must not stay true.
    try {
      let repairNote = null;
      for (let attempt = 0; attempt < 2; attempt += 1) {
        let result;
        try {
          result = await proposePlan({ prompt, scenes, repairNote });
        } catch (e) {
          stop(`The planner request failed: ${e?.message || 'unknown error'}`);
          return;
        }
        if (!run.isCurrent(id)) return;
        if (Object.hasOwn(PLAN_MESSAGES, result?.status)) { stop(PLAN_MESSAGES[result.status]); return; }

        let reason;
        if (result?.status === 'ok') {
          const compiled = compilePlan(doc, result);
          if (compiled.ok) {
            setPlanning(false);
            await showProposal(compiled);
            return;
          }
          if (compiled.kind === 'unsupported') { stop(compiled.reason); return; }
          // A value the batch dry-run refuses (a scale or time outside Studio's range) is the
          // operator's answer to give, not the model's to re-guess: sent back for a repair, the
          // real qwen2.5:7b swapped "scale 5" for the smallest allowed value. Show the reason and stop.
          if (compiled.kind === 'refused') { stop(`The planner's draft was refused: ${compiled.reason}`); return; }
          reason = compiled.reason;
        } else {
          reason = result?.error || 'the draft did not match the schema';
        }
        if (attempt === 1) { stop(`The planner's draft was refused: ${reason}`); return; }
        repairNote = reason;
      }
    } catch (e) {
      stop(`The planner failed unexpectedly: ${e?.message || 'unknown error'}`);
    }
  }, [showProposal]);

  const block = proposal
    ? applyBlock({ doc, command: proposal.command, check, checkContext: proposal.context, context })
    : null;

  const apply = useCallback(() => {
    const cur = live.current;
    if (!cur.proposal) return;
    const blocked = applyBlock({
      doc: cur.doc, command: cur.proposal.command, check: cur.check,
      checkContext: cur.proposal.context, context: cur.context,
    });
    if (blocked) { setError(blocked.reason); return; }
    runRef.current.invalidate();
    cur.execute(cur.proposal.command);
    setProposal(null);
    setCheck(null);
    setError(null);
    setPlanStatus(null);
    setText('');
  }, []);

  return {
    text, setText, proposal, check, error, block, preview, apply, dismiss: clear,
    plan, planning, planStatus,
  };
}
