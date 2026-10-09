import { useEffect, useRef } from "react";

const FOCUS_RING =
  "focus:outline-none focus-visible:ring-2 focus-visible:ring-[#5b9bd5]/55 focus-visible:ring-offset-2";

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  body: string;
  confirmLabel?: string;
  /** Disables both buttons while the confirm action is in flight. */
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * Modal confirmation for destructive actions.
 *
 * Closes on Escape, overlay click and the Cancel button; focus starts on
 * Cancel so a stray Enter never confirms a deletion.
 */
export default function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel = "Delete",
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;

    cancelRef.current?.focus();

    function handleKeyDown(event: KeyboardEvent): void {
      if (event.key === "Escape" && !busy) onCancel();
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open, busy, onCancel]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirm-dialog-title"
    >
      {/* Overlay */}
      <button
        type="button"
        aria-label="Close dialog"
        disabled={busy}
        onClick={onCancel}
        className="absolute inset-0 h-full w-full cursor-default bg-[#17191b]/35 backdrop-blur-[2px]"
      />

      {/* Card */}
      <div className="relative w-full max-w-md overflow-hidden rounded-[22px] border border-[#e7edf2] bg-white p-6 shadow-[0_24px_60px_-24px_rgba(23,52,74,0.35)] sm:p-7">
        <span
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-0 top-0 h-1 bg-gradient-to-r from-red-300 via-red-100 to-transparent"
        />

        <h2
          id="confirm-dialog-title"
          className="font-serif text-[22px] leading-tight tracking-[-0.02em] text-[#17191b]"
        >
          {title}
        </h2>

        <p className="mt-3 text-[14px] leading-6 text-[#71777c]">{body}</p>

        <div className="mt-7 flex flex-wrap items-center justify-end gap-3">
          <button
            ref={cancelRef}
            type="button"
            disabled={busy}
            onClick={onCancel}
            className={`inline-flex items-center justify-center rounded-full border border-[#e3e9ee] bg-white px-5 py-2.5 text-sm font-medium text-[#3f474d] transition-all duration-200 ease-out hover:-translate-y-0.5 hover:border-[#dcebfb] hover:bg-[#f7fbfe] disabled:cursor-not-allowed disabled:hover:translate-y-0 disabled:hover:bg-white ${FOCUS_RING}`}
          >
            Cancel
          </button>

          <button
            type="button"
            disabled={busy}
            onClick={onConfirm}
            className={`inline-flex items-center justify-center gap-2 rounded-full bg-red-600 px-5 py-2.5 text-sm font-medium text-white shadow-[0_12px_26px_-14px_rgba(220,38,38,0.7)] transition-all duration-200 ease-out hover:-translate-y-0.5 hover:bg-red-700 disabled:cursor-not-allowed disabled:bg-red-300 disabled:shadow-none disabled:hover:translate-y-0 ${FOCUS_RING}`}
          >
            {busy && (
              <span
                aria-hidden="true"
                className="h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-red-200 border-t-white"
              />
            )}
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
