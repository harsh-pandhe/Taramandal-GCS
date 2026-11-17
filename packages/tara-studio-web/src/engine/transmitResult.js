// What the Transmit tab says after a transmit (issue #92): the answer the GCS
// gave, as plain text, instead of a browser alert. Pure functions, no DOM.
//
// A view model is { kind: 'sent' | 'refused' | 'failed', title, detail, at }:
//   sent     the GCS accepted the show
//   refused  the GCS answered and said no (an HTTP 4xx)
//   failed   no usable answer: unreachable, timed out, a GCS error (5xx), or the
//            show could not be prepared before it was sent
//
// Only the fields named below are ever read, and nothing here has the session
// token, the signature or any key: the output cannot carry what it never sees.

export const TITLE_MAX = 160;
export const DETAIL_MAX = 400;
const LIST_SHOWN = 3;

/** Cuts by characters (code points), not UTF-16 units, so Devanagari and emoji are not split. */
export const clip = (text, max) => {
  const chars = [...String(text ?? '')];
  return chars.length <= max ? chars.join('') : `${chars.slice(0, max - 1).join('')}…`;
};

// One line, no control characters, and a header value that slipped into a message is masked.
const tidy = (text) => String(text ?? '')
  .replace(/Bearer\s+[A-Za-z0-9._~+/=-]+/gi, 'Bearer …')
  .replace(/\p{Cc}+/gu, ' ')
  .replace(/\s+/g, ' ')
  .trim();

const itemText = (item) => {
  if (typeof item === 'string') return item;
  if (!item || typeof item !== 'object') return '';
  const reason = typeof item.msg === 'string' ? item.msg : typeof item.message === 'string' ? item.message : '';
  if (!reason) return '';
  // FastAPI's validation errors point at the field: ["body", "file"].
  const where = Array.isArray(item.loc)
    ? item.loc.filter((p) => (typeof p === 'string' || typeof p === 'number') && p !== 'body').join('.')
    : '';
  return where ? `${where}: ${reason}` : reason;
};

/**
 * The reason a GCS error body gives, as one readable string: `detail` as a
 * string, as an object with a message, or as a list of them. Never the body
 * itself, so a field this does not name cannot reach the screen.
 */
export const readableDetail = (detail) => {
  try {
    if (typeof detail === 'string') return tidy(detail);
    if (Array.isArray(detail)) {
      const texts = detail.map(itemText).map(tidy).filter(Boolean);
      if (!texts.length) return '';
      const more = texts.length > LIST_SHOWN ? ` (+${texts.length - LIST_SHOWN} more)` : '';
      return `${texts.slice(0, LIST_SHOWN).join('; ')}${more}`;
    }
    if (detail && typeof detail === 'object') return tidy(itemText(detail));
  } catch { /* a hostile or odd body is "no reason given", not a crash in the send flow */ }
  return '';
};

const hostOf = (gcs) => {
  if (!gcs) return '';
  try { return new URL(gcs).host; } catch { return clip(tidy(gcs), 60); }
};

/** "HH:MM", local time. */
export const formatTime = (at) => {
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return '';
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
};

const make = (kind, title, detail, now) => ({
  kind, title: clip(title, TITLE_MAX), detail: clip(detail, DETAIL_MAX), at: now,
});

/**
 * The show was accepted. `body` is the GCS's response (it lists one entry per
 * drone in `drones_summary`); `droneCount` is the Studio's own count, used when
 * the response has none; `gcs` is the GCS base URL the Studio sent it to.
 */
export const describeSent = (body, { droneCount = null, gcs = '', now = Date.now() } = {}) => {
  try {
    const fromBody = body && typeof body.drones_summary === 'object' && body.drones_summary
      ? Object.keys(body.drones_summary).length : 0;
    const n = fromBody > 0 ? fromBody : (Number.isInteger(droneCount) && droneCount > 0 ? droneCount : null);
    const host = hostOf(gcs);
    const what = n === null ? 'the show' : `the show for ${n} vehicle${n === 1 ? '' : 's'}`;
    const title = `Sent ${what} to the GCS${host ? ` at ${host}` : ''}.`;

    const sig = body && typeof body.signature === 'object' ? body.signature : null;
    let signature = '';
    if (sig && sig.verified === true) {
      const signer = typeof sig.signer === 'string' ? tidy(sig.signer) : '';
      signature = `Signature verified${signer ? ` (signer: ${clip(signer, 60)})` : ''}. `;
    } else if (sig && sig.verified === false) {
      signature = 'The GCS accepted it without a signature. ';
    }
    return make('sent', title, `${signature}Run launch checks on the GCS dashboard.`, now);
  } catch {
    return make('sent', 'Sent the show to the GCS.', 'Run launch checks on the GCS dashboard.', now);
  }
};

const NOT_CONFIRMED = 'Nothing is confirmed as received. Check that the GCS is running and reachable, then transmit again.';

/**
 * The transmit did not go through. `error` is whatever the send threw: an HTTP
 * error carries `status` and `detail` (the GCS's own reason), a network failure
 * carries `gcsUnreachable` (and `timedOut`), anything else is a plain Error.
 */
export const describeFailure = (error, { now = Date.now() } = {}) => {
  try {
    const e = error && typeof error === 'object' ? error : {};
    if (e.gcsUnreachable) {
      return make('failed', e.timedOut ? 'The GCS could not be reached (it did not answer in time).' : 'The GCS could not be reached.', NOT_CONFIRMED, now);
    }
    const reason = readableDetail(e.detail) || tidy(typeof e.message === 'string' ? e.message : '');
    if (Number.isInteger(e.status)) {
      if (e.status >= 400 && e.status < 500) {
        return make('refused', `Not sent: the GCS refused the show (HTTP ${e.status}).`, reason || 'The GCS gave no reason.', now);
      }
      return make('failed', `Not sent: the GCS reported an error (HTTP ${e.status}).`, reason || 'The GCS gave no reason.', now);
    }
    return make('failed', 'Not sent: the transmit failed.', reason || 'No reason was given.', now);
  } catch {
    return make('failed', 'Not sent: the transmit failed.', '', now);
  }
};

/** What a transmit result was made for: it stops applying once any of these changes. */
export const sameInputs = (a, b) => Array.isArray(a) && Array.isArray(b)
  && a.length === b.length && a.every((v, i) => Object.is(v, b[i]));
