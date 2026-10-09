import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ApiError, uploadInvoice } from "../api";
import FileDropZone from "../components/FileDropZone";
import type { Invoice } from "../types/invoice";
import { displayOrDash, formatStatusLabel } from "./InvoiceListPage";

/** Upload ceiling; mirrors the backend's 20 MB limit. */
const MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024;
const ACCEPTED_EXTENSIONS = [".pdf", ".png", ".jpg", ".jpeg"];
const ACCEPTED_CONTENT_TYPES = ["application/pdf", "image/png", "image/jpeg"];
const GENERIC_ERROR_MESSAGE = "Something went wrong while uploading. Please try again.";

/* ------------------------------------------------------------------ *
 * Shared visual tokens. Identical values to App.tsx, InvoiceListPage,
 * PoliciesPage and ReviewsPage so every page reads as one product.
 * ------------------------------------------------------------------ */

/** Visible keyboard focus for every interactive control on the page. */
const FOCUS_RING =
  "focus:outline-none focus-visible:ring-2 focus-visible:ring-[#5b9bd5]/55 focus-visible:ring-offset-2";

/** The application's primary blue, in the app shell's button language. */
const PRIMARY_ACTION_CLASS = `group inline-flex items-center justify-center gap-2.5 rounded-full bg-[#5b9bd5] px-6 py-3.5 text-sm font-medium text-white shadow-[0_14px_30px_-12px_rgba(91,155,213,0.75)] transition-all duration-200 ease-out hover:-translate-y-0.5 hover:bg-[#477cae] hover:shadow-[0_18px_36px_-14px_rgba(91,155,213,0.85)] disabled:cursor-not-allowed disabled:bg-[#9aa3aa] disabled:shadow-none disabled:hover:translate-y-0 motion-reduce:transform-none motion-reduce:transition-colors ${FOCUS_RING}`;

/** Quieter sibling to the primary action. */
const SECONDARY_ACTION_CLASS = `inline-flex items-center justify-center gap-2 rounded-full border border-[#e3e9ee] bg-white px-5 py-3 text-sm font-medium text-[#3f474d] transition-all duration-200 ease-out hover:-translate-y-0.5 hover:border-[#dcebfb] hover:bg-[#f7fbfe] hover:text-[#477cae] motion-reduce:transform-none motion-reduce:transition-colors ${FOCUS_RING}`;

/** Hairline card surface, matching the panels elsewhere in the app. */
const SURFACE_CLASS =
  "rounded-[22px] border border-[#e7edf2] bg-white shadow-[0_1px_2px_rgba(23,25,27,0.03),0_24px_60px_-46px_rgba(23,52,74,0.7)]";

/** Small uppercase label used for section eyebrows. */
const LABEL_CLASS =
  "text-[10.5px] font-semibold uppercase tracking-[0.2em] text-[#a8b1b8]";

/** The muted spinner used while an upload is in flight. */
const SPINNER_CLASS =
  "h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-[#dcebfb] border-t-[#5b9bd5]";

type UploadState =
  | { kind: "idle" }
  | { kind: "uploading"; file: File }
  | { kind: "success"; invoice: Invoice }
  | { kind: "error"; message: string };

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Returns a human-readable reason the file is rejected, or `null` when valid. */
function validateFile(file: File): string | null {
  const name = file.name.toLowerCase();
  if (!ACCEPTED_EXTENSIONS.some((extension) => name.endsWith(extension))) {
    return `Unsupported file type. Allowed: ${ACCEPTED_EXTENSIONS.join(", ")}.`;
  }
  if (file.size > MAX_FILE_SIZE_BYTES) {
    return `File is too large (${formatFileSize(file.size)}). Maximum size is 20 MB.`;
  }
  // Some operating systems and browsers report an empty MIME type (notably for
  // PDFs on certain platforms). The extension check above already gates the
  // format, so only reject a *known-wrong* type — never an absent one.
  if (
    file.type !== "" &&
    !ACCEPTED_CONTENT_TYPES.some((type) => file.type === type)
  ) {
    return `Unsupported content type "${file.type}". Allowed: PDF, PNG, JPEG.`;
  }
  return null;
}

/** Extension -> the label shown on a file chip. */
function fileKindLabel(file: File): string {
  const name = file.name.toLowerCase();
  if (name.endsWith(".pdf")) return "PDF";
  if (name.endsWith(".png")) return "PNG";
  if (name.endsWith(".jpg") || name.endsWith(".jpeg")) return "JPEG";
  return "File";
}

/**
 * What happens after a document lands.
 *
 * Three honest statements, in the order a user needs them: the bytes are
 * stored, the pipeline has queued the document for extraction, and the fields
 * do not exist yet. Claiming a completed extraction here would be a lie —
 * extraction is a separate `POST /analyze` the reviewer triggers on the
 * document page.
 */
function PipelineNotes() {
  return (
    <ul className="space-y-2.5">
      {[
        "The document is stored and listed in your invoice ledger.",
        "Extraction, validation, risk and policy checks run from the invoice page.",
        "Nothing is deleted on deactivate — policy and invoice history is kept.",
      ].map((note) => (
        <li key={note} className="flex gap-2.5 text-[13px] leading-6 text-[#5c666e]">
          <span
            aria-hidden="true"
            className="mt-2 h-1 w-1 shrink-0 rounded-full bg-[#c3ccd3]"
          />
          {note}
        </li>
      ))}
    </ul>
  );
}

/**
 * Drag-and-drop invoice upload.
 *
 * Rendering is driven entirely by the `UploadState` union, so exactly one of the
 * states (drop zone, in-flight, success card, error card) is on screen at a time.
 */
export default function UploadPage() {
  const navigate = useNavigate();
  const [state, setState] = useState<UploadState>({ kind: "idle" });

  const abortRef = useRef<AbortController | null>(null);

  /**
   * Abort in-flight work if the page unmounts mid-upload.
   *
   * Without this a completed upload could call `setState` on a component that
   * is no longer mounted. `apiFetch` re-throws aborts untouched, so this never
   * surfaces as a user-facing error.
   */
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  async function startUpload(file: File): Promise<void> {
    const validationError = validateFile(file);
    if (validationError !== null) {
      setState({ kind: "error", message: validationError });
      return;
    }

    const controller = new AbortController();
    abortRef.current = controller;

    setState({ kind: "uploading", file });
    try {
      const invoice = await uploadInvoice(file, controller.signal);
      // A newer upload superseded this one; do not clobber its state.
      if (abortRef.current === controller) {
        abortRef.current = null;
        setState({ kind: "success", invoice });
      }
    } catch (error: unknown) {
      if (abortRef.current !== controller) {
        return;
      }
      abortRef.current = null;

      // A user-initiated cancel is a deliberate act, not a failure. Returning to
      // the drop zone keeps the retry honest instead of reporting an error the
      // user caused on purpose.
      if (error instanceof DOMException && error.name === "AbortError") {
        setState({ kind: "idle" });
        return;
      }

      // ApiError carries the backend's `detail` message; anything else is unexpected.
      setState({
        kind: "error",
        message: error instanceof ApiError ? error.message : GENERIC_ERROR_MESSAGE,
      });
    }
  }

  function reset(): void {
    setState({ kind: "idle" });
  }

  function cancelUpload(): void {
    abortRef.current?.abort();
  }

  return (
    <section className="space-y-10">
      {/* -------------------------------------------------- header */}
      <header className="flex flex-col gap-6 lg:flex-row lg:items-end lg:justify-between lg:gap-12">
        <div className="min-w-0">
          <p className="flex items-center gap-3 text-[10.5px] font-semibold uppercase tracking-[0.22em] text-[#9aa3aa]">
            <span
              aria-hidden="true"
              className="h-1.5 w-1.5 rounded-full bg-[#5b9bd5] shadow-[0_0_0_3px_rgba(91,155,213,0.22)]"
            />
            Documents
            <span aria-hidden="true" className="h-px w-8 bg-[#dfe6ec]" />
          </p>

          <h1 className="mt-5 font-serif text-[34px] leading-[1.06] tracking-[-0.035em] text-[#17191b] sm:text-[44px] lg:text-[52px]">
            Upload documents
          </h1>

          <p className="mt-4 max-w-[52ch] text-[15px] leading-7 text-[#71777c]">
            One document at a time. Drop a PDF or image of an invoice and the
            pipeline takes it from there.
          </p>
        </div>

        {state.kind === "idle" && (
          <Link
            to="/invoices"
            className={`inline-flex shrink-0 items-center justify-center gap-2 self-start text-[13.5px] font-medium text-[#858b90] transition-colors duration-200 ease-out hover:text-[#477cae] motion-reduce:transition-colors lg:self-auto ${FOCUS_RING}`}
          >
            <span aria-hidden="true">←</span>
            Back to invoices
          </Link>
        )}
      </header>

      {/* -------------------------------------------------- idle / drop zone */}
      {state.kind === "idle" && (
        <div className="space-y-6">
          <FileDropZone onFileSelected={startUpload} />

          {/*
            Guidance panel. Every line below is a statement about this
            application's actual behaviour, not marketing.
          */}
          <div className={`${SURFACE_CLASS} p-5 sm:p-7`}>
            <div className="flex items-center gap-4">
              <h2 className={LABEL_CLASS}>Before you upload</h2>

              <span
                aria-hidden="true"
                className="h-px flex-1 bg-gradient-to-r from-[#e2e9ef] to-transparent"
              />
            </div>

            <div className="mt-6 grid gap-8 sm:grid-cols-2 sm:gap-10">
              <div>
                <p className="text-[10.5px] font-semibold uppercase tracking-[0.18em] text-[#9aa3aa]">
                  Accepted files
                </p>
                <dl className="mt-4 space-y-3">
                  {[
                    { label: "PDF", value: "Documents and scans" },
                    { label: "PNG", value: "Screenshots and images" },
                    { label: "JPEG", value: "Photos of paper invoices" },
                  ].map((entry) => (
                    <div key={entry.label} className="flex items-baseline gap-4">
                      <dt className="w-14 shrink-0 font-mono text-[12.5px] text-[#477cae]">
                        {entry.label}
                      </dt>
                      <dd className="text-[13.5px] leading-6 text-[#5c666e]">
                        {entry.value}
                      </dd>
                    </div>
                  ))}
                  <div className="flex items-baseline gap-4">
                    <dt className="w-14 shrink-0 font-mono text-[12.5px] text-[#477cae]">
                      Max
                    </dt>
                    <dd className="text-[13.5px] leading-6 text-[#5c666e]">
                      20 MB per document
                    </dd>
                  </div>
                </dl>
              </div>

              <div>
                <p className="text-[10.5px] font-semibold uppercase tracking-[0.18em] text-[#9aa3aa]">
                  What happens next
                </p>
                <div className="mt-4">
                  <PipelineNotes />
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* -------------------------------------------------- uploading */}
      {state.kind === "uploading" && (
        <div
          role="status"
          aria-live="polite"
          className={`${SURFACE_CLASS} p-6 sm:p-10`}
        >
          <div className="flex items-start gap-4">
            <span aria-hidden="true" className={SPINNER_CLASS} />

            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
                <p className="truncate text-[15px] font-medium text-[#17191b]">
                  {state.file.name}
                </p>

                <span className="shrink-0 text-[12.5px] tabular-nums text-[#858b90]">
                  {fileKindLabel(state.file)} · {formatFileSize(state.file.size)}
                </span>
              </div>

              <p className="mt-1.5 text-[13.5px] text-[#71777c]">
                Uploading…
              </p>
            </div>
          </div>

          {/*
            `fetch` exposes no upload progress events (unlike XHR), so a determinate
            percentage bar is not possible without swapping the transport. This
            indeterminate bar is only an activity indicator - it is deliberately
            not read as a percentage by anyone watching it.
          */}
          <div className="mt-7 h-1 w-full overflow-hidden rounded-full bg-[#f1f5f8]">
            <div className="h-full w-1/3 animate-pulse rounded-full bg-[#5b9bd5]" />
          </div>

          <div className="mt-8 border-t border-[#eef2f6] pt-6">
            <button
              type="button"
              onClick={cancelUpload}
              className={SECONDARY_ACTION_CLASS}
            >
              Cancel upload
            </button>
          </div>
        </div>
      )}

      {/* -------------------------------------------------- success */}
      {state.kind === "success" && (
        <div className={`${SURFACE_CLASS} overflow-hidden`}>
          <div className="h-1 w-full bg-gradient-to-r from-emerald-400 via-emerald-100 to-transparent" />

          <div className="px-6 py-10 sm:px-10 sm:py-12">
            <span
              aria-hidden="true"
              className="inline-flex h-11 w-11 items-center justify-center rounded-full bg-emerald-50 ring-1 ring-inset ring-emerald-100"
            >
              <svg viewBox="0 0 24 24" fill="none" className="h-5 w-5 text-emerald-600">
                <path
                  d="m5 12.5 4.5 4.5L19 7.5"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </span>

            <h2 className="mt-6 font-serif text-[26px] leading-[1.15] tracking-[-0.03em] text-[#17191b] sm:text-[32px]">
              Upload complete
            </h2>

            <p className="mt-2 text-[15px] leading-7 text-[#71777c]">
              {state.invoice.document_filename} is stored and ready for analysis.
            </p>

            <dl className="mt-8 grid gap-6 border-t border-[#eef2f6] pt-7 sm:grid-cols-3">
              <div className="min-w-0">
                <dt className="text-[10.5px] font-semibold uppercase tracking-[0.18em] text-[#9aa3aa]">
                  Invoice ID
                </dt>
                <dd
                  className="mt-1.5 truncate font-mono text-[13px] text-[#477cae]"
                  title={state.invoice.id}
                >
                  {state.invoice.id}
                </dd>
              </div>

              <div className="min-w-0">
                <dt className="text-[10.5px] font-semibold uppercase tracking-[0.18em] text-[#9aa3aa]">
                  Vendor
                </dt>
                <dd className="mt-1.5 truncate text-[14px] font-medium text-[#282d31]">
                  {displayOrDash(state.invoice.vendor_name)}
                </dd>
              </div>

              <div className="min-w-0">
                <dt className="text-[10.5px] font-semibold uppercase tracking-[0.18em] text-[#9aa3aa]">
                  Status
                </dt>
                <dd className="mt-1.5 text-[14px] font-medium text-[#282d31]">
                  {formatStatusLabel(state.invoice.status)}
                </dd>
              </div>
            </dl>

            <div className="mt-9 border-t border-[#eef2f6] pt-7">
              <p className="text-[10.5px] font-semibold uppercase tracking-[0.18em] text-[#9aa3aa]">
                What happens next
              </p>
              <div className="mt-4">
                <PipelineNotes />
              </div>
            </div>

            <div className="mt-9 flex flex-wrap items-center gap-3">
              <button
                type="button"
                onClick={() => navigate(`/invoices/${state.invoice.id}`)}
                className={PRIMARY_ACTION_CLASS}
              >
                View invoice
                <span
                  aria-hidden="true"
                  className="transition-transform duration-200 ease-out group-hover:translate-x-0.5 motion-reduce:transform-none"
                >
                  →
                </span>
              </button>

              <button type="button" onClick={reset} className={SECONDARY_ACTION_CLASS}>
                Upload another
              </button>
            </div>
          </div>
        </div>
      )}

      {/* -------------------------------------------------- error */}
      {state.kind === "error" && (
        <div className={`${SURFACE_CLASS} overflow-hidden`}>
          <div className="h-1 w-full bg-gradient-to-r from-[#d98b8b] via-[#f3e2e2] to-transparent" />

          <div className="px-6 py-10 sm:px-10 sm:py-12">
            <span
              aria-hidden="true"
              className="inline-flex h-11 w-11 items-center justify-center rounded-full bg-red-50 ring-1 ring-inset ring-red-100"
            >
              <svg viewBox="0 0 24 24" fill="none" className="h-5 w-5 text-red-600">
                <path
                  d="M12 7.5v5.5m0 3.5h.01"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                />
                <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.6" />
              </svg>
            </span>

            <h2 className="mt-6 font-serif text-[26px] leading-[1.15] tracking-[-0.03em] text-[#17191b] sm:text-[32px]">
              Upload failed
            </h2>

            <p className="mt-3 max-w-md text-[15px] leading-7 text-[#71777c]">
              {state.message}
            </p>

            <div className="mt-9 flex flex-wrap items-center gap-3">
              <button type="button" onClick={reset} className={PRIMARY_ACTION_CLASS}>
                Try again
              </button>

              <Link
                to="/invoices"
                className="inline-flex items-center justify-center gap-2 rounded-full px-5 py-3 text-sm font-medium text-[#858b90] transition-colors duration-200 ease-out hover:text-[#477cae] motion-reduce:transition-colors"
              >
                <span aria-hidden="true">←</span>
                Back to invoices
              </Link>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}