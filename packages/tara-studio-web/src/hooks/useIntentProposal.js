import { useCallback, useRef, useState } from 'react';
import { parseIntent } from '../engine/intentParser.js';
import { previewBatch } from '../engine/commands.js';
import { createLatestRun } from '../engine/latestRun.js';
import { checkProposal, buildForCheck, validateForCheck } from '../engine/proposalCheck.js';
import { applyBlock, describeChanges } from '../engine/proposalView.js';
import { proposePlan } from '../engine/api.js';
import { compilePlan, summariseScenes } from '../engine/planCompile.js';
import { buildEntry, makeRequestId } from '../engine/auditEntry.js';
import { createAuditClient } from '../engine/auditClient.js';

// Plain statuses for a planner call that produced nothing to review (same wording
// style as the AI panel's). Offline is reported, never papered over.
export const PLAN_MESSAGES = {
  unavailable: 'Ollama is not reachable. Make sure it is running locally, then try again.',
  timeout: 'The local model took too long to respond. Try again, or a shorter request.',
};

// What the operator is told when an audit entry did not make it (issue #94). Nothing here
// ever blocks an edit: the note is information, and the log is never a precondition.
export const AUDIT_MESSAGES = {
  skipped: 'Not recorded: no saved project yet.',
  unconfirmed: 'Could not confirm the audit entry was saved.',
  audit_full: 'The audit log for this project is full; this decision was not recorded.',
  project_not_found: 'This project is no longer in the Project Vault; this decision was not recorded.',
  internal: 'The audit entry could not be built; this decision was not recorded.',
};

// Keys already logged are remembered, up to this many.
const LOGGED_KEEP = 200;
// Not final: a request can be stale or blocked more than once, and then still be applied.
const NON_TERMINAL = new Set(['stale', 'blocked']);

// The request a proposal came from, as the log needs it. Read off the proposal itself, so it
// is the request as it was when it started: not the input as it is now, and not the project
// that happens to be open now.
const requestOf = (proposal) => ({
  requestId: proposal.requestId,
  source: proposal.source,
  requestText: proposal.requestText,
  projectId: proposal.projectId,
});
const detailsOf = (proposal, check) => ({ command: proposal.command, check, summary: proposal.summary });

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
 *
 * Every request ends in one decision that is recorded in the project's audit log
 * (engine/auditEntry.js, engine/auditClient.js). Recording is fire and forget: it
 * runs after the action, is never awaited and never throws, and a failure only sets
 * `auditNote`. `projectId` is the open project (a value); `auditClient` is for tests.
 */
export default function useIntentProposal({
  doc, execute, numDrones, rate, pacing, projectId = null, auditClient = null,
}) {
  const [text, setText] = useState('');
  const [proposal, setProposal] = useState(null); // { command, summary, context, changes, requestId, source, requestText, projectId }
  const [check, setCheck] = useState(null);       // { status, summary, violations } | { status: 'CHECKING' }
  const [error, setError] = useState(null);
  const [planning, setPlanning] = useState(false);
  const [planStatus, setPlanStatus] = useState(null); // why the planner produced no proposal
  const [auditNote, setAuditNote] = useState(null);   // { kind, message, code? } | null: what could not be recorded
  const [auditVersion, setAuditVersion] = useState(0); // moves after every append attempt, so a list can refresh
  const runRef = useRef(null);
  if (runRef.current === null) runRef.current = createLatestRun();
  // Audit bookkeeping that causes no render: the client (made on first use), the keys already
  // logged, and the planner request that is in flight (it has no proposal yet).
  const auditRef = useRef(null);
  if (auditRef.current === null) auditRef.current = { client: null, logged: new Set(), inflight: null };

  const context = `${numDrones}|${rate}|${pacing}`;
  // What the click handlers read, so they judge the document as it is when the
  // click happens, not as it was at the last render of an old closure.
  const live = useRef({});
  live.current = { doc, execute, numDrones, rate, pacing, context, proposal, check, text, projectId, auditClient };

  const setNote = useCallback((note) => {
    try { setAuditNote(note); } catch { /* a note is never worth a throw */ }
  }, []);

  /**
   * Records one decision about one request. Nothing in it can reach the caller: nothing is
   * awaited, buildEntry (which throws for a programmer error), the client (which may throw or
   * reject) and every state update are caught, and the only trace of a problem is `auditNote`.
   * The append is made synchronously here, so a caller that records after an action knows the
   * action came first. It goes to the project the request was made in, however the open project
   * changed since. Never retried: after a failure the entry may or may not be on disk.
   */
  const record = useCallback((request, decision, extra = {}) => {
    try {
      if (!request) return;
      const audit = auditRef.current;
      // A final outcome is logged once per request; stale and blocked once per reason.
      const key = NON_TERMINAL.has(decision) ? `${request.requestId}|${decision}|${extra.reason ?? ''}` : request.requestId;
      if (audit.logged.has(key)) return;
      audit.logged.add(key);
      if (audit.logged.size > LOGGED_KEEP) audit.logged.delete(audit.logged.values().next().value);

      const pid = request.projectId;
      if (pid === null || pid === undefined || pid === '') {
        setNote({ kind: 'skipped', message: AUDIT_MESSAGES.skipped });
        return;
      }
      const finish = (note) => {
        try {
          if (note) setNote(note);
          setAuditVersion((v) => v + 1);
        } catch { /* nothing to do */ }
      };
      let entry;
      try {
        entry = buildEntry({
          requestId: request.requestId,
          source: request.source,
          request: request.requestText,
          summary: extra.summary ?? '',
          command: extra.command ?? null,
          check: extra.check ?? null,
          decision,
          reason: extra.reason,
        });
      } catch {
        finish({ kind: 'rejected', code: 'internal', message: AUDIT_MESSAGES.internal });
        return;
      }
      audit.client ??= live.current.auditClient ?? createAuditClient();
      let sent;
      try { sent = Promise.resolve(audit.client.append(pid, entry)); } catch (e) { sent = Promise.reject(e); }
      sent.then((res) => {
        if (res?.ok) return finish(null);
        if (res?.notStored) {
          return finish({
            kind: 'rejected',
            code: res.code,
            message: AUDIT_MESSAGES[res.code] ?? `The audit entry was refused (${res.code}); this decision was not recorded.`,
          });
        }
        return finish({ kind: 'unconfirmed', code: res?.code, message: AUDIT_MESSAGES.unconfirmed });
      }).catch(() => finish({ kind: 'unconfirmed', message: AUDIT_MESSAGES.unconfirmed }));
    } catch {
      setNote({ kind: 'unconfirmed', message: AUDIT_MESSAGES.unconfirmed });
    }
  }, [setNote]);

  // A new request is starting. Whatever was open (a proposal, or a planner call in flight) is
  // replaced by it, and that is a decision about the OLD request. Returns the new request's
  // identity, or null for an empty one (which is not a request).
  const beginRequest = useCallback((source, requestText) => {
    const cur = live.current;
    const audit = auditRef.current;
    setNote(null);
    if (cur.proposal) {
      record(requestOf(cur.proposal), 'superseded', detailsOf(cur.proposal, cur.check));
    } else if (audit.inflight) {
      record(audit.inflight, 'superseded');
    }
    audit.inflight = null;
    if (!requestText.trim()) return null;
    return { requestId: makeRequestId(), source, requestText, projectId: cur.projectId ?? null };
  }, [record, setNote]);

  const clear = useCallback(() => {
    runRef.current.invalidate();
    setProposal(null);
    setCheck(null);
    setError(null);
    setPlanning(false);
    setPlanStatus(null);
  }, []);

  const dismiss = useCallback(() => {
    const cur = live.current;
    const audit = auditRef.current;
    if (cur.proposal) {
      record(requestOf(cur.proposal), 'dismissed', detailsOf(cur.proposal, cur.check));
    } else if (audit.inflight) {
      record(audit.inflight, 'dismissed');
    }
    audit.inflight = null;
    clear();
  }, [clear, record]);

  // Shared by both sources: `parsed` is { ok, command, summary } | { ok: false, reason }.
  // Dry-runs it against the document as it is now, then shows it and starts the safety check.
  // `request` is what beginRequest returned (or null).
  const showProposal = useCallback(async (parsed, request) => {
    const cur = live.current;
    const dry = parsed.ok ? previewBatch(cur.doc, parsed.command) : null;
    if (!parsed.ok || !dry.ok) {
      const reason = parsed.ok ? dry.reason : parsed.reason;
      setProposal(null);
      setCheck(null);
      setError(reason);
      record(request, 'refused', { reason });
      return;
    }
    setError(null);
    setProposal({
      // The request this came from, as it was when it started: the log's identity for it.
      requestId: request?.requestId ?? null,
      source: request?.source ?? null,
      requestText: request?.requestText ?? '',
      projectId: request?.projectId ?? null,
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
  }, [record]);

  const preview = useCallback(async () => {
    const cur = live.current;
    runRef.current.invalidate();
    setPlanning(false);
    setPlanStatus(null);
    const request = beginRequest('typed', cur.text);
    await showProposal(parseIntent(cur.text, cur.doc), request);
  }, [showProposal, beginRequest]);

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
    const request = beginRequest('planner', prompt);
    auditRef.current.inflight = request;
    const id = run.begin();
    setProposal(null);
    setCheck(null);
    setError(null);
    setPlanStatus(null);
    setPlanning(true);
    const scenes = summariseScenes(doc);
    // `decision` is what the audit log records. It is recorded only while this call is still the
    // current one: a call that was replaced or dismissed already has its own entry, and its late
    // answer is dropped here exactly as it is for the screen.
    const stop = (message, decision) => {
      if (!run.isCurrent(id)) return;
      setPlanning(false);
      setPlanStatus(message);
      if (auditRef.current.inflight === request) auditRef.current.inflight = null;
      record(request, decision, { reason: message });
    };

    // Whatever goes wrong below, `planning` must not stay true.
    try {
      let repairNote = null;
      for (let attempt = 0; attempt < 2; attempt += 1) {
        let result;
        try {
          result = await proposePlan({ prompt, scenes, repairNote });
        } catch (e) {
          stop(`The planner request failed: ${e?.message || 'unknown error'}`, 'planner_failed');
          return;
        }
        if (!run.isCurrent(id)) return;
        if (Object.hasOwn(PLAN_MESSAGES, result?.status)) {
          stop(PLAN_MESSAGES[result.status], result.status === 'timeout' ? 'planner_timeout' : 'planner_unavailable');
          return;
        }

        let reason;
        if (result?.status === 'ok') {
          const compiled = compilePlan(doc, result);
          if (compiled.ok) {
            setPlanning(false);
            if (auditRef.current.inflight === request) auditRef.current.inflight = null;
            await showProposal(compiled, request);
            return;
          }
          if (compiled.kind === 'unsupported') { stop(compiled.reason, 'refused'); return; }
          // A value the batch dry-run refuses (a scale or time outside Studio's range) is the
          // operator's answer to give, not the model's to re-guess: sent back for a repair, the
          // real qwen2.5:7b swapped "scale 5" for the smallest allowed value. Show the reason and stop.
          if (compiled.kind === 'refused') { stop(`The planner's draft was refused: ${compiled.reason}`, 'refused'); return; }
          reason = compiled.reason;
        } else {
          reason = result?.error || 'the draft did not match the schema';
        }
        if (attempt === 1) { stop(`The planner's draft was refused: ${reason}`, 'refused'); return; }
        repairNote = reason;
      }
    } catch (e) {
      stop(`The planner failed unexpectedly: ${e?.message || 'unknown error'}`, 'planner_failed');
    }
  }, [showProposal, beginRequest, record]);

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
    if (blocked) {
      setError(blocked.reason);
      record(requestOf(cur.proposal), blocked.kind === 'stale' ? 'stale' : 'blocked', {
        ...detailsOf(cur.proposal, cur.check), reason: blocked.reason,
      });
      return;
    }
    runRef.current.invalidate();
    cur.execute(cur.proposal.command);
    // Recorded after the edit, never before it: the log follows the action and cannot hold it up.
    record(requestOf(cur.proposal), 'applied', detailsOf(cur.proposal, cur.check));
    setProposal(null);
    setCheck(null);
    setError(null);
    setPlanStatus(null);
    setText('');
  }, [record]);

  return {
    text, setText, proposal, check, error, block, preview, apply, dismiss,
    plan, planning, planStatus,
    // The audit log: what could not be recorded, and a counter that moves after each append
    // attempt. Information only: nothing in the editing flow reads either.
    auditNote, auditVersion,
    // The project new decisions are recorded in (the open one), for the list that reads them back.
    auditProjectId: projectId ?? null,
  };
}
