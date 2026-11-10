import { useEffect, useId, useRef } from 'react';
import { createPortal } from 'react-dom';
import { dialogKeyAction } from '../../engine/confirmations.js';

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * A confirmation step in front of an irreversible action (issue #92). Never
 * confirms by accident: focus starts on Cancel, Esc and a click on the
 * backdrop cancel, and only the confirm button calls onConfirm. Focus stays
 * inside while open and goes back to the button that opened it on close.
 */
export default function ConfirmDialog({
  title, message, confirmLabel, cancelLabel = 'Cancel', danger = false, onConfirm, onCancel,
}) {
  const uid = useId();
  const titleId = `${uid}-title`;
  const messageId = `${uid}-message`;
  const cardRef = useRef(null);
  const cancelRef = useRef(null);

  useEffect(() => {
    const opener = document.activeElement;
    cancelRef.current?.focus();
    return () => { if (opener instanceof HTMLElement && opener.isConnected) opener.focus(); };
  }, []);

  const onKeyDown = (e) => {
    const nodes = [...(cardRef.current?.querySelectorAll(FOCUSABLE) ?? [])];
    const action = dialogKeyAction(e, nodes.indexOf(cardRef.current?.ownerDocument.activeElement), nodes.length);
    if (!action) return;
    e.preventDefault();
    e.stopPropagation();
    if (action.type === 'cancel') onCancel();
    else nodes[action.index].focus();
  };

  const dialog = (
    <div className="modal-backdrop" onPointerDown={onCancel} onKeyDown={onKeyDown}>
      <div
        ref={cardRef}
        className="modal-card confirm-dialog"
        onPointerDown={(e) => e.stopPropagation()}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={messageId}
      >
        <div className="modal-head"><h3 id={titleId}>{title}</h3></div>
        <p id={messageId} className="confirm-dialog-message">{message}</p>
        <div className="confirm-dialog-actions">
          <button ref={cancelRef} type="button" className="btn-secondary btn-sm" onClick={onCancel}>
            {cancelLabel}
          </button>
          <button
            type="button"
            className={`btn-sm ${danger ? 'btn-danger-solid' : 'btn-primary'}`}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
  // Portalled to <body>: the console drawer's backdrop-filter would otherwise
  // make this "fixed" backdrop cover only the drawer, not the screen.
  return typeof document === 'undefined' ? dialog : createPortal(dialog, document.body);
}
