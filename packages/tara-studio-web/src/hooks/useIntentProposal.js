import { useCallback, useRef, useState } from 'react';
import { parseIntent } from '../engine/intentParser.js';
import { previewBatch } from '../engine/commands.js';
import { createLatestRun } from '../engine/latestRun.js';
import { checkProposal, buildForCheck, validateForCheck } from '../engine/proposalCheck.js';
import { applyBlock, describeChanges } from '../engine/proposalView.js';

/**
 * The typed scene-edit proposal (issue #89): one sentence -> a pinned batch
 * command -> a dry-run safety check -> an explicit Apply that is one undo step.
 *
 * Nothing here writes to Studio state until `apply`. The check builds and
 * validates the edited scenes on its own (engine/proposalCheck.js) and keeps
 * its result here - never in the show builder, the history or the project.
 * Its own latest-run gate means an older check can never fill in the result of
 * a newer proposal, so a stale PASSED cannot enable Apply.
 */
export default function useIntentProposal({ doc, execute, numDrones, rate, pacing }) {
  const [text, setText] = useState('');
  const [proposal, setProposal] = useState(null); // { command, summary, context, changes }
  const [check, setCheck] = useState(null);       // { status, summary, violations } | { status: 'CHECKING' }
  const [error, setError] = useState(null);
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
  }, []);

  const preview = useCallback(async () => {
    const cur = live.current;
    runRef.current.invalidate();
    const parsed = parseIntent(cur.text, cur.doc);
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
    setText('');
  }, []);

  return { text, setText, proposal, check, error, block, preview, apply, dismiss: clear };
}
