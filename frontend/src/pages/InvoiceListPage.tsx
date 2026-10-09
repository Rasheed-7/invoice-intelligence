import { useEffect, useMemo, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { ApiError, deleteInvoice, listInvoices } from "../api";
import ConfirmDialog from "../components/ConfirmDialog";
import type { Invoice, InvoiceListResponse, InvoiceStatus } from "../types/invoice";

/** Page size requested from the list endpoint. Backend allows 1..200. */
const PAGE_SIZE = 20;

/** Placeholder rendered wherever an invoice field is null or empty. */
export const EMPTY_VALUE = "—";

/** Fallback copy for failures that are not `ApiError` instances. */
export const GENERIC_ERROR_MESSAGE = "Something went wrong. Please try again.";

/**
 * Status -> colour treatment.
 *
 * Declared as an exhaustive `Record`, so adding a member to `InvoiceStatus`
 * becomes a compile error until it is styled here.
 */
const STATUS_BADGE_CLASSES: Record<InvoiceStatus, string> = {
  approved: "bg-emerald-50/80 text-emerald-700 ring-emerald-100",
  ingested: "bg-[#eef7fd] text-[#477cae] ring-[#dcebfb]",
  extracted: "bg-[#eef7fd] text-[#477cae] ring-[#dcebfb]",
  needs_review: "bg-amber-50/80 text-amber-700 ring-amber-100",
  rejected: "bg-red-50/80 text-red-700 ring-red-100",
  failed: "bg-red-50/80 text-red-700 ring-red-100",
  validated: "bg-[#f3f5f7] text-[#5c666e] ring-[#e6ebef]",
};

/** Status -> the dot colour used by badges and the overview strip. */
const STATUS_DOT_CLASSES: Record<InvoiceStatus, string> = {
  approved: "bg-emerald-500",
  ingested: "bg-[#5b9bd5]",
  extracted: "bg-[#5b9bd5]",
  needs_review: "bg-amber-500",
  rejected: "bg-red-500",
  failed: "bg-red-500",
  validated: "bg-[#9aa3aa]",
};

/** "needs_review" -> "Needs review". Also used for document processing status. */
export function formatStatusLabel(value: string): string {
  const spaced = value.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/**
 * Status pill: soft tinted background, small dot, restrained uppercase label.
 *
 * Shared verbatim with `InvoiceDetailPage`, so a status reads identically in the
 * collection and on the document.
 */
export function InvoiceStatusBadge({ status }: { status: InvoiceStatus }) {
  return (
    <span
      className={`inline-flex items-center gap-2 whitespace-nowrap rounded-full px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] ring-1 ring-inset ${STATUS_BADGE_CLASSES[status]}`}
    >
      <span
        aria-hidden="true"
        className={`h-1.5 w-1.5 shrink-0 rounded-full ${STATUS_DOT_CLASSES[status]}`}
      />
      {formatStatusLabel(status)}
    </span>
  );
}

/**
 * Short local datetime. ISO input, e.g. "2026-10-02T14:33:07Z" -> "2026-10-02 14:33".
 * Unparseable input is returned unchanged rather than rendered as "Invalid Date".
 */
export function formatDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  const pad = (part: number): string => String(part).padStart(2, "0");
  const ymd = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  return `${ymd} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** Byte count -> "982 B" | "12.4 KB" | "1.8 MB". */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Renders a nullable or undefined string, falling back to the em-dash placeholder. */
export function displayOrDash(value: string | null | undefined): string {
  return value === null || value === undefined || value === "" ? EMPTY_VALUE : value;
}

/** "USD" + "108.50" -> "USD 108.50"; the dash when there is no total. */
export function formatTotal(total: string | null, currency: string | null): string {
  if (total === null) return EMPTY_VALUE;
  return currency ? `${currency} ${total}` : total;
}

/** Formats a monetary value with currency. Returns "—" if amount is null. */
export function formatMoney(amount: string | null, currency: string | null): string {
  if (amount === null) return EMPTY_VALUE;
  const trimmed = amount.trim();
  if (trimmed === "") return EMPTY_VALUE;
  return currency ? `${currency} ${trimmed}` : trimmed;
}

/** ISO date string -> "YYYY-MM-DD". Returns "—" if null. */
export function formatDate(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return EMPTY_VALUE;
  // Dates are already in YYYY-MM-DD format from backend
  return value;
}

/** True when a YYYY-MM-DD due date is before today (local calendar day). */
function isOverdue(dueDate: string | null | undefined): boolean {
  if (!dueDate) return false;
  const day = dueDate.slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return false;
  const today = new Date();
  const todayDay = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
  return day < todayDay;
}

type ListState =
  | { kind: "loading" }
  | { kind: "loaded"; data: InvoiceListResponse }
  | { kind: "error"; message: string };

/* ------------------------------------------------------------------ *
 * Shared visual tokens. Identical values to App.tsx and
 * InvoiceDetailPage.tsx so the three pages read as one product.
 * ------------------------------------------------------------------ */

/** Visible keyboard focus for every interactive control on the page. */
const FOCUS_RING =
  "focus:outline-none focus-visible:ring-2 focus-visible:ring-[#5b9bd5]/55 focus-visible:ring-offset-2";

/** The application's primary blue, in the app shell's button language. */
const PRIMARY_ACTION_CLASS = `group inline-flex items-center justify-center gap-2.5 rounded-full bg-[#5b9bd5] px-6 py-3.5 text-sm font-medium text-white shadow-[0_14px_30px_-12px_rgba(91,155,213,0.75)] transition-all duration-200 ease-out hover:-translate-y-0.5 hover:bg-[#477cae] hover:shadow-[0_18px_36px_-14px_rgba(91,155,213,0.85)] motion-reduce:transform-none motion-reduce:transition-colors ${FOCUS_RING}`;

/** Quieter sibling to the primary action, for in-flow recovery and reset. */
const SECONDARY_ACTION_CLASS = `inline-flex items-center justify-center gap-2 rounded-full border border-[#e3e9ee] bg-white px-5 py-3 text-sm font-medium text-[#3f474d] transition-all duration-200 ease-out hover:-translate-y-0.5 hover:border-[#dcebfb] hover:bg-[#f7fbfe] hover:text-[#477cae] motion-reduce:transform-none motion-reduce:transition-colors ${FOCUS_RING}`;

/** Hairline card surface, matching the panels on the detail page. */
const SURFACE_CLASS =
  "rounded-[22px] border border-[#e7edf2] bg-white shadow-[0_1px_2px_rgba(23,25,27,0.03),0_24px_60px_-46px_rgba(23,52,74,0.7)]";

/** Small uppercase label used for column heads and section eyebrows. */
const LABEL_CLASS =
  "text-[10.5px] font-semibold uppercase tracking-[0.2em] text-[#a8b1b8]";

/**
 * Status groups used by the overview strip. Each group maps to the statuses
 * that actually exist in `InvoiceStatus`; nothing is invented here.
 */
const STATUS_GROUPS = [
  {
    key: "all",
    label: "All invoices",
    dot: "bg-[#c3ccd3]",
    statuses: [] as InvoiceStatus[],
  },
  {
    key: "needs_review",
    label: "Needs review",
    dot: "bg-amber-500",
    statuses: ["needs_review"] as InvoiceStatus[],
  },
  {
    key: "approved",
    label: "Approved",
    dot: "bg-emerald-500",
    statuses: ["approved"] as InvoiceStatus[],
  },
  {
    key: "in_progress",
    label: "In progress",
    dot: "bg-[#5b9bd5]",
    statuses: ["ingested", "extracted", "validated"] as InvoiceStatus[],
  },
  {
    key: "issues",
    label: "Rejected or failed",
    dot: "bg-red-500",
    statuses: ["rejected", "failed"] as InvoiceStatus[],
  },
] as const;

type StatusGroupKey = (typeof STATUS_GROUPS)[number]["key"];

type SortKey = "newest" | "oldest" | "highest" | "lowest";

const SORT_LABELS: Record<SortKey, string> = {
  newest: "Newest upload first",
  oldest: "Oldest upload first",
  highest: "Highest total",
  lowest: "Lowest total",
};

/** Timestamp for sorting; unparseable dates sort last. */
function timeValue(value: string): number | null {
  const parsed = new Date(value).getTime();
  return Number.isNaN(parsed) ? null : parsed;
}

/** Monetary value for sorting; `null` when missing or unparseable. */
function amountValue(invoice: Invoice): number | null {
  if (invoice.total_amount === null) return null;
  const parsed = Number(invoice.total_amount);
  return Number.isFinite(parsed) ? parsed : null;
}

function sortInvoices(invoices: Invoice[], sort: SortKey): Invoice[] {
  const sorted = [...invoices];

  if (sort === "newest") {
    return sorted.sort((a, b) => {
      const ta = timeValue(a.created_at);
      const tb = timeValue(b.created_at);
      // Missing dates always sort last, regardless of direction.
      if (ta === null && tb === null) return 0;
      if (ta === null) return 1;
      if (tb === null) return -1;
      return tb - ta;
    });
  }

  if (sort === "oldest") {
    return sorted.sort((a, b) => {
      const ta = timeValue(a.created_at);
      const tb = timeValue(b.created_at);
      if (ta === null && tb === null) return 0;
      if (ta === null) return 1;
      if (tb === null) return -1;
      return ta - tb;
    });
  }

  if (sort === "highest") {
    return sorted.sort((a, b) => {
      const aa = amountValue(a);
      const ab = amountValue(b);
      // Missing totals always sort last, regardless of direction.
      if (aa === null && ab === null) return 0;
      if (aa === null) return 1;
      if (ab === null) return -1;
      return ab - aa;
    });
  }

  return sorted.sort((a, b) => {
    const aa = amountValue(a);
    const ab = amountValue(b);
    if (aa === null && ab === null) return 0;
    if (aa === null) return 1;
    if (ab === null) return -1;
    return aa - ab;
  });
}

function matchesQuery(invoice: Invoice, query: string): boolean {
  if (query === "") return true;

  const needle = query.toLowerCase();

  return (
    invoice.id.toLowerCase().includes(needle) ||
    invoice.document_filename.toLowerCase().includes(needle) ||
    (invoice.vendor_name ?? "").toLowerCase().includes(needle) ||
    (invoice.invoice_number ?? "").toLowerCase().includes(needle) ||
    (invoice.po_number ?? "").toLowerCase().includes(needle) ||
    invoice.status.toLowerCase().includes(needle)
  );
}

/** Crisp, dependency-free magnifier. */
function SearchIcon() {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 20 20"
      fill="none"
      className="h-4 w-4"
    >
      <circle
        cx="8.75"
        cy="8.75"
        r="5.25"
        stroke="currentColor"
        strokeWidth="1.5"
      />
      <path
        d="m12.6 12.6 3.4 3.4"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

/** Small document glyph that anchors each invoice identity. */
function DocumentGlyph() {
  return (
    <span
      aria-hidden="true"
      className="relative grid h-10 w-[30px] shrink-0 place-items-center overflow-hidden rounded-lg border border-[#e6edf3] bg-[#fbfdfe] shadow-[0_1px_2px_rgba(23,25,27,0.04)]"
    >
      <span className="mt-1 h-3.5 w-3.5 rounded-[2px] bg-[#dbe9f5]" />
      <span className="absolute bottom-1.5 left-1.5 h-px w-3.5 bg-[#cbd9e4]" />
    </span>
  );
}

/** Invoice identity: number, vendor, and the source document beneath. */
function InvoiceIdentity({ invoice }: { invoice: Invoice }) {
  return (
    <div className="flex min-w-0 items-center gap-4">
      <DocumentGlyph />

      <div className="min-w-0">
        <p className="truncate font-mono text-[14px] font-medium tracking-[-0.01em] text-[#17191b]">
          {displayOrDash(invoice.invoice_number)}
        </p>
        <p className="mt-1 truncate text-[13.5px] font-medium text-[#5c666e]">
          {displayOrDash(invoice.vendor_name)}
        </p>
        <p className="mt-0.5 truncate text-[12px] text-[#a8b1b8]">
          {invoice.document_filename}
        </p>
      </div>
    </div>
  );
}

/** Amount cell: strong, right-aligned, tabular so columns of money align. */
function AmountCell({ invoice }: { invoice: Invoice }) {
  return (
    <span className="whitespace-nowrap text-[15px] font-medium tabular-nums tracking-[-0.01em] text-[#17191b]">
      {formatTotal(invoice.total_amount, invoice.currency)}
    </span>
  );
}

/**
 * Desktop invoice collection.
 *
 * The whole row is clickable; the invoice identifier link is the keyboard focus
 * target, so the destination is always reachable without a pointer.
 */
function InvoicesTable({
  invoices,
  onDelete,
}: {
  invoices: Invoice[];
  onDelete: (invoice: Invoice) => void;
}) {
  const navigate = useNavigate();

  function openInvoice(id: string): void {
    navigate(`/invoices/${id}`);
  }

  return (
    <div className={`${SURFACE_CLASS} overflow-hidden`}>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[760px] border-collapse text-left">
          <caption className="sr-only">
            Invoices on file, newest upload first
          </caption>

          <thead>
            <tr className="border-b border-[#eef2f6]">
              <th scope="col" className={`px-6 py-4 ${LABEL_CLASS}`}>
                Invoice
              </th>
              <th scope="col" className={`px-6 py-4 ${LABEL_CLASS}`}>
                Vendor
              </th>
              <th scope="col" className={`px-6 py-4 ${LABEL_CLASS}`}>
                Status
              </th>
              <th scope="col" className={`px-6 py-4 ${LABEL_CLASS}`}>
                Uploaded
              </th>
              <th
                scope="col"
                className={`px-6 py-4 text-right ${LABEL_CLASS}`}
              >
                Total
              </th>
              <th scope="col" className="w-12 px-2 py-4">
                <span className="sr-only">Open invoice</span>
              </th>
              <th scope="col" className="w-14 px-2 py-4">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>

          <tbody>
            {invoices.map((invoice) => (
              <tr
                key={invoice.id}
                onClick={() => openInvoice(invoice.id)}
                onKeyDown={(event) => {
                  if (event.target !== event.currentTarget) return;
                  if (event.key === "Enter" || event.key === " ") {
                    // Space scrolls the page by default — claim it for activation.
                    event.preventDefault();
                    openInvoice(invoice.id);
                  }
                }}
                tabIndex={0}
                role="link"
                aria-label={`Open invoice ${displayOrDash(invoice.invoice_number)} from ${displayOrDash(invoice.vendor_name)}`}
                className={`group cursor-pointer border-b border-[#f4f7f9] transition-colors duration-200 ease-out last:border-b-0 hover:bg-[#f7fafc] focus-visible:bg-[#f7fafc] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[#5b9bd5]/55 ${FOCUS_RING}`}
              >
                <td className="max-w-[260px] px-6 py-5 align-middle">
                  <span
                    className="block truncate font-mono text-[14px] font-medium tracking-[-0.01em] text-[#17191b] transition-colors duration-200 group-hover:text-[#477cae]"
                  >
                    {displayOrDash(invoice.invoice_number)}
                  </span>
                  <span className="mt-1 block truncate text-[12px] text-[#a8b1b8]">
                    {invoice.document_filename}
                  </span>
                </td>

                <td className="max-w-[240px] px-6 py-5 align-middle">
                  <span
                    className="block truncate text-[13.5px] font-medium text-[#5c666e]"
                    title={invoice.vendor_name ?? undefined}
                  >
                    {displayOrDash(invoice.vendor_name)}
                  </span>
                </td>

                <td className="px-6 py-5 align-middle">
                  <InvoiceStatusBadge status={invoice.status} />
                </td>

                <td className="whitespace-nowrap px-6 py-5 align-middle">
                  <time
                    dateTime={invoice.created_at}
                    className="block text-[13px] tabular-nums text-[#858b90]"
                    title={invoice.created_at}
                  >
                    {formatDateTime(invoice.created_at)}
                  </time>
                </td>

                <td className="px-6 py-5 text-right align-middle">
                  <AmountCell invoice={invoice} />
                </td>

                <td className="px-2 py-5 text-right align-middle">
                  <span
                    aria-hidden="true"
                    className="inline-flex h-8 w-8 items-center justify-center rounded-full text-[13px] text-[#c8d1d8] transition-all duration-200 ease-out group-hover:bg-white group-hover:text-[#5b9bd5] group-hover:shadow-[0_2px_8px_-2px_rgba(23,52,74,0.3)]"
                  >
                    →
                  </span>
                </td>

                <td className="px-2 py-5 text-right align-middle">
                  <button
                    type="button"
                    onClick={(event) => {
                      // The row itself navigates; deleting must not open the invoice.
                      event.stopPropagation();
                      onDelete(invoice);
                    }}
                    onKeyDown={(event) => event.stopPropagation()}
                    aria-label={`Delete invoice ${displayOrDash(invoice.invoice_number)}`}
                    className="inline-flex h-8 w-8 items-center justify-center rounded-full text-[#c8d1d8] transition-all duration-200 ease-out hover:bg-red-50 hover:text-red-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-300 motion-reduce:transition-colors"
                  >
                    <svg
                      aria-hidden="true"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.6"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      className="h-4 w-4"
                    >
                      <path d="M4 7h16M10 7V5a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v2m-9 0 1 12a2 2 0 0 0 2 2h4a2 2 0 0 0 2-2l1-12M10 11v6m4-6v6" />
                    </svg>
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** Mobile invoice items: primary facts first, amount and status always visible. */
function InvoicesList({
  invoices,
  onDelete,
}: {
  invoices: Invoice[];
  onDelete: (invoice: Invoice) => void;
}) {
  return (
    <ul className={`${SURFACE_CLASS} divide-y divide-[#eef2f6] overflow-hidden`}>
      {invoices.map((invoice) => (
        <li key={invoice.id} className="flex items-stretch">
          <Link
            to={`/invoices/${invoice.id}`}
            className={`min-w-0 flex-1 px-5 py-5 transition-colors duration-200 ease-out hover:bg-[#f7fafc] focus-visible:bg-[#f7fafc] ${FOCUS_RING}`}
          >
            <div className="flex items-start justify-between gap-4">
              <InvoiceIdentity invoice={invoice} />

              <span className="shrink-0 pt-0.5 text-right">
                <AmountCell invoice={invoice} />
              </span>
            </div>

            <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
              <InvoiceStatusBadge status={invoice.status} />

              <span
                className="text-[12.5px] tabular-nums text-[#858b90]"
                title={invoice.created_at}
              >
                Uploaded {formatDateTime(invoice.created_at)}
                {invoice.due_date ? (
                  <span
                    className={
                      isOverdue(invoice.due_date)
                        ? "font-medium text-red-600"
                        : undefined
                    }
                  >
                    {" "}
                    · Due {formatDate(invoice.due_date)}
                    {isOverdue(invoice.due_date) ? " · Overdue" : ""}
                  </span>
                ) : null}
              </span>
            </div>
          </Link>

          <div className="flex shrink-0 items-center border-l border-[#eef2f6] pl-1 pr-3">
            <button
              type="button"
              onClick={() => onDelete(invoice)}
              aria-label={`Delete invoice ${displayOrDash(invoice.invoice_number)}`}
              className="inline-flex h-9 w-9 items-center justify-center rounded-full text-[#a8b1b8] transition-colors duration-200 ease-out hover:bg-red-50 hover:text-red-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-300 motion-reduce:transition-colors"
            >
              <svg
                aria-hidden="true"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
                className="h-4 w-4"
              >
                <path d="M4 7h16M10 7V5a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v2m-9 0 1 12a2 2 0 0 0 2 2h4a2 2 0 0 0 2-2l1-12M10 11v6m4-6v6" />
              </svg>
            </button>
          </div>
        </li>
      ))}
    </ul>
  );
}

/** Row-shaped placeholders so the loading state matches the final layout. */
function InvoicesSkeleton() {
  return (
    <div className={`${SURFACE_CLASS} overflow-hidden`}>
      <div className="hidden items-center gap-6 border-b border-[#eef2f6] px-6 py-4 md:flex">
        <span className="h-2.5 w-16 rounded-full bg-[#f1f5f8]" />
        <span className="h-2.5 w-16 rounded-full bg-[#f1f5f8]" />
        <span className="h-2.5 w-14 rounded-full bg-[#f1f5f8]" />
        <span className="h-2.5 w-16 rounded-full bg-[#f1f5f8]" />
        <span className="ml-auto h-2.5 w-12 rounded-full bg-[#f1f5f8]" />
      </div>

      {[0, 1, 2, 3, 4].map((row) => (
        <div
          key={row}
          className="flex items-center gap-4 border-b border-[#f4f7f9] px-5 py-5 last:border-b-0 md:gap-6 md:px-6"
          style={{ opacity: 1 - row * 0.12 }}
        >
          <span className="h-10 w-[30px] shrink-0 rounded-lg bg-[#f1f5f8]" />
          <span className="min-w-0 flex-1 space-y-2">
            <span className="block h-2.5 w-32 rounded-full bg-[#f1f5f8]" />
            <span className="block h-2 w-44 max-w-full rounded-full bg-[#f4f7f9]" />
          </span>
          <span className="hidden h-6 w-24 shrink-0 rounded-full bg-[#f4f7f9] sm:block" />
          <span className="hidden h-2.5 w-28 shrink-0 rounded-full bg-[#f4f7f9] lg:block" />
          <span className="h-3 w-20 shrink-0 rounded-full bg-[#eef2f6]" />
        </div>
      ))}
    </div>
  );
}

/** CSS-only document stack used by the empty states. No image dependency. */
function DocumentIllustration() {
  return (
    <span
      aria-hidden="true"
      className="relative mx-auto flex h-24 w-28 items-center justify-center"
    >
      <span className="absolute left-1 top-4 h-16 w-12 -rotate-6 rounded-lg border border-[#e2eaf1] bg-white shadow-[0_6px_18px_-12px_rgba(23,52,74,0.6)]" />
      <span className="absolute right-1 top-4 h-16 w-12 rotate-6 rounded-lg border border-[#e2eaf1] bg-white shadow-[0_6px_18px_-12px_rgba(23,52,74,0.6)]" />
      <span className="relative h-16 w-12 rounded-lg border border-[#dcebfb] bg-white p-2.5 shadow-[0_10px_24px_-14px_rgba(23,52,74,0.7)]">
        <span className="block h-1.5 w-full rounded-full bg-[#dbe9f5]" />
        <span className="mt-1.5 block h-1.5 w-4/5 rounded-full bg-[#eef2f6]" />
        <span className="mt-1.5 block h-1.5 w-3/5 rounded-full bg-[#eef2f6]" />
        <span className="mt-2 block h-2 w-3 rounded-full bg-[#bcd9f0]" />
      </span>
    </span>
  );
}

/**
 * Empty collection: nothing has ever been uploaded.
 *
 * Distinguished from the filtered "no results" state, which offers a way back
 * out rather than an upload.
 */
function EmptyInvoices() {
  return (
    <div
      className={`${SURFACE_CLASS} relative overflow-hidden px-6 py-16 text-center sm:px-10 sm:py-20`}
    >
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 -top-24 h-48 bg-[radial-gradient(60%_100%_at_50%_100%,rgba(91,155,213,0.16)_0%,rgba(252,253,254,0)_70%)]"
      />

      <div className="relative">
        <DocumentIllustration />

        <h2 className="mt-8 font-serif text-[26px] leading-tight tracking-[-0.025em] text-[#17191b] sm:text-[32px]">
          No invoices yet
        </h2>

        <p className="mx-auto mt-3 max-w-sm text-[14.5px] leading-7 text-[#71777c]">
          Upload a PDF or image to get started. Extracted fields, policies, and
          reviews stay attached to the invoice.
        </p>

        <Link to="/upload" className={`mt-9 ${PRIMARY_ACTION_CLASS}`}>
          <span aria-hidden="true" className="text-[16px] leading-none">
            +
          </span>
          Upload an invoice
        </Link>
      </div>
    </div>
  );
}

/** Pagination controls: numbered pages plus previous / next. */
function PaginationControls({
  page,
  pageCount,
  onPageChange,
}: {
  page: number;
  pageCount: number;
  onPageChange: (page: number) => void;
}) {
  if (pageCount <= 1) return null;

  // Compact window: first, last, current ± 1, with ellipses where pages are skipped.
  const pages: (number | "gap-start" | "gap-end")[] = [];
  for (let p = 1; p <= pageCount; p += 1) {
    if (p === 1 || p === pageCount || Math.abs(p - page) <= 1) {
      pages.push(p);
    } else if (p < page && pages[pages.length - 1] !== "gap-start") {
      pages.push("gap-start");
    } else if (p > page && pages[pages.length - 1] !== "gap-end") {
      pages.push("gap-end");
    }
  }

  const buttonBase = `inline-flex h-9 min-w-9 items-center justify-center rounded-full px-3 text-[13px] font-medium tabular-nums transition-colors duration-200 motion-reduce:transition-colors ${FOCUS_RING}`;

  return (
    <nav aria-label="Invoice pages" className="flex flex-wrap items-center gap-1.5">
      <button
        type="button"
        disabled={page <= 1}
        onClick={() => onPageChange(page - 1)}
        aria-label="Previous page"
        className={`${buttonBase} border border-[#e3e9ee] bg-white text-[#3f474d] hover:border-[#dcebfb] hover:bg-[#f7fbfe] hover:text-[#477cae] disabled:cursor-not-allowed disabled:border-[#eef2f6] disabled:text-[#a8b1b8] disabled:hover:bg-white`}
      >
        <span aria-hidden="true">←</span>
      </button>

      {pages.map((entry) =>
        typeof entry !== "number" ? (
          <span
            key={entry}
            aria-hidden="true"
            className="px-1 text-[13px] text-[#a8b1b8]"
          >
            …
          </span>
        ) : (
          <button
            key={entry}
            type="button"
            onClick={() => onPageChange(entry)}
            aria-label={`Page ${entry}`}
            aria-current={entry === page ? "page" : undefined}
            className={
              entry === page
                ? `${buttonBase} bg-[#5b9bd5] text-white shadow-[0_10px_22px_-12px_rgba(91,155,213,0.85)]`
                : `${buttonBase} text-[#3f474d] hover:bg-[#f4f7f9] hover:text-[#477cae]`
            }
          >
            {entry}
          </button>
        )
      )}

      <button
        type="button"
        disabled={page >= pageCount}
        onClick={() => onPageChange(page + 1)}
        aria-label="Next page"
        className={`${buttonBase} border border-[#e3e9ee] bg-white text-[#3f474d] hover:border-[#dcebfb] hover:bg-[#f7fbfe] hover:text-[#477cae] disabled:cursor-not-allowed disabled:border-[#eef2f6] disabled:text-[#a8b1b8] disabled:hover:bg-white`}
      >
        <span aria-hidden="true">→</span>
      </button>
    </nav>
  );
}

/**
 * No results: the collection exists, but the current search and filters exclude
 * everything in it. Offers the supported way back — clearing the filters.
 */
function NoResults({ note, onClear }: { note: string; onClear: () => void }) {
  return (
    <div className={`${SURFACE_CLASS} px-6 py-16 text-center sm:px-10 sm:py-20`}>
      <DocumentIllustration />

      <h2 className="mt-8 font-serif text-[24px] leading-tight tracking-[-0.025em] text-[#17191b] sm:text-[28px]">
        Nothing matches those filters
      </h2>

      <p className="mx-auto mt-3 max-w-sm text-[14.5px] leading-7 text-[#71777c]">
        {note}
      </p>

      <button type="button" onClick={onClear} className={`mt-9 ${SECONDARY_ACTION_CLASS}`}>
        Clear filters
      </button>
    </div>
  );
}

/**
 * Invoice workspace: paginated list of uploaded invoices.
 *
 * Rendering is driven entirely by `ListState`, so exactly one of loading,
 * collection, empty or error is on screen at a time.
 */
export default function InvoiceListPage() {
  const [state, setState] = useState<ListState>({ kind: "loading" });
  const [reloadToken, setReloadToken] = useState(0);
  const [page, setPage] = useState(1);

  const [query, setQuery] = useState("");
  const [statusGroup, setStatusGroup] = useState<StatusGroupKey>("all");
  const [sort, setSort] = useState<SortKey>("newest");

  // Success banner after deleting from the detail page: navigate("/invoices",
  // { state: { deleted: label } }).
  const location = useLocation();
  const stateDeleted = location.state as { deleted?: unknown } | null;
  const deletedLabel =
    stateDeleted && typeof stateDeleted.deleted === "string"
      ? stateDeleted.deleted
      : null;
  const [showDeletedBanner, setShowDeletedBanner] = useState(
    deletedLabel !== null
  );

  const [pendingDelete, setPendingDelete] = useState<{
    id: string;
    label: string;
  } | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  /** Open the confirmation dialog for one invoice. */
  function requestDelete(invoice: Invoice): void {
    setDeleteError(null);
    setPendingDelete({
      id: invoice.id,
      label:
        invoice.invoice_number ??
        invoice.vendor_name ??
        invoice.document_filename ??
        invoice.id,
    });
  }

  /** Confirm handler: DELETE then refetch the page (server is the source of truth). */
  async function handleConfirmDelete(): Promise<void> {
    if (!pendingDelete) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await deleteInvoice(pendingDelete.id);
      setPendingDelete(null);
      setReloadToken((token) => token + 1);
    } catch (error: unknown) {
      setDeleteError(
        error instanceof ApiError ? error.message : GENERIC_ERROR_MESSAGE
      );
      setPendingDelete(null);
    } finally {
      setDeleteBusy(false);
    }
  }

  useEffect(() => {
    const controller = new AbortController();
    setState({ kind: "loading" });

    listInvoices({
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
      signal: controller.signal,
    })
      .then((data) => {
        if (!controller.signal.aborted) setState({ kind: "loaded", data });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setState({
          kind: "error",
          message: error instanceof ApiError ? error.message : GENERIC_ERROR_MESSAGE,
        });
      });

    return () => {
      controller.abort();
    };
  }, [reloadToken, page]);

  const items = state.kind === "loaded" ? state.data.items : [];

  /** Counts come straight from the loaded invoices, never from a hardcoded number. */
  const counts = useMemo(() => {
    const result: Record<StatusGroupKey, number> = {
      all: items.length,
      needs_review: 0,
      approved: 0,
      in_progress: 0,
      issues: 0,
    };

    for (const invoice of items) {
      for (const group of STATUS_GROUPS) {
        if ((group.statuses as readonly InvoiceStatus[]).includes(invoice.status)) {
          result[group.key] += 1;
        }
      }
    }

    return result;
  }, [items]);

  const visible = useMemo(() => {
    const group = STATUS_GROUPS.find((entry) => entry.key === statusGroup);
    const statuses = (group?.statuses ?? []) as readonly InvoiceStatus[];

    const filtered = items.filter((invoice) => {
      if (statuses.length > 0 && !statuses.includes(invoice.status)) return false;
      return matchesQuery(invoice, query.trim());
    });

    return sortInvoices(filtered, sort);
  }, [items, query, statusGroup, sort]);

  const filtersActive = query.trim() !== "" || statusGroup !== "all";
  const total = state.kind === "loaded" ? state.data.total : 0;
  const limit = state.kind === "loaded" ? state.data.limit : PAGE_SIZE;
  const pageCount = Math.max(1, Math.ceil(total / Math.max(1, limit)));
  // The backend only paginates (limit/offset); search + status + sort apply to
  // the loaded page, so the empty-state copy must say that instead of implying
  // a global search.

  function clearFilters(): void {
    setQuery("");
    setStatusGroup("all");
  }

  function handlePageChange(next: number): void {
    setPage(Math.min(Math.max(1, next), pageCount));
  }

  return (
    <section className="space-y-10">
      {/* -------------------------------------------------- header */}
      <header className="flex flex-col gap-8 lg:flex-row lg:items-end lg:justify-between lg:gap-12">
        <div className="min-w-0">
          <p className="flex items-center gap-3 text-[10.5px] font-semibold uppercase tracking-[0.22em] text-[#9aa3aa]">
            <span
              aria-hidden="true"
              className="h-1.5 w-1.5 rounded-full bg-[#5b9bd5] shadow-[0_0_0_3px_rgba(91,155,213,0.22)]"
            />
            Invoices
            <span aria-hidden="true" className="h-px w-8 bg-[#dfe6ec]" />
          </p>

          <h1 className="mt-5 font-serif text-[34px] leading-[1.06] tracking-[-0.035em] text-[#17191b] sm:text-[44px] lg:text-[52px]">
            Your invoice ledger
          </h1>

          <p className="mt-4 max-w-[52ch] text-[15px] leading-7 text-[#71777c]">
            {state.kind === "loaded"
              ? `${state.data.total} invoice${state.data.total === 1 ? "" : "s"} on file, with extraction status and totals kept in one place.`
              : "Uploaded invoices and their extraction status."}
          </p>
        </div>

        <Link to="/upload" className={`shrink-0 self-start lg:self-auto ${PRIMARY_ACTION_CLASS}`}>
          <span
            aria-hidden="true"
            className="text-[16px] leading-none text-white/80 transition-transform duration-200 ease-out group-hover:rotate-90 motion-reduce:transform-none"
          >
            +
          </span>
          Add invoice
        </Link>
      </header>

      {/* -------------------------------------------------- delete feedback */}
      {deletedLabel && showDeletedBanner && (
        <div
          role="status"
          className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-emerald-100 bg-emerald-50/70 px-5 py-3.5"
        >
          <p className="text-sm text-emerald-800">
            <span className="font-medium">Invoice {deletedLabel} deleted.</span>{" "}
            The audit trail is preserved.
          </p>
          <button
            type="button"
            onClick={() => setShowDeletedBanner(false)}
            className={`rounded-full px-3 py-1.5 text-[12.5px] font-medium text-emerald-700 transition-colors duration-200 hover:bg-emerald-100 ${FOCUS_RING}`}
          >
            Dismiss
          </button>
        </div>
      )}

      {deleteError && !pendingDelete && (
        <div
          role="alert"
          className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-red-100 bg-red-50/70 px-5 py-3.5"
        >
          <p className="text-sm text-red-800">
            <span className="font-medium">Could not delete the invoice.</span>{" "}
            {deleteError}
          </p>
          <button
            type="button"
            onClick={() => setDeleteError(null)}
            className={`rounded-full px-3 py-1.5 text-[12.5px] font-medium text-red-600 transition-colors duration-200 hover:bg-red-100 ${FOCUS_RING}`}
          >
            Dismiss
          </button>
        </div>
      )}

      <ConfirmDialog
        open={pendingDelete !== null}
        title="Delete this invoice?"
        body="This soft-deletes the invoice from active views. The audit trail is preserved."
        confirmLabel="Delete"
        busy={deleteBusy}
        onConfirm={handleConfirmDelete}
        onCancel={() => {
          if (!deleteBusy) setPendingDelete(null);
        }}
      />

      {/* -------------------------------------------------- loading */}
      {state.kind === "loading" && (
        <>
          <div role="status" aria-live="polite" className="sr-only">
            Loading invoices…
          </div>

          <div className="space-y-5">
            {/* Overview skeleton keeps the header from shifting on load. */}
            <div className="hidden flex-wrap items-center gap-x-8 gap-y-4 border-y border-[#eef2f6] py-5 sm:flex">
              {[0, 1, 2, 3, 4].map((cell) => (
                <span key={cell} className="space-y-2">
                  <span className="block h-2 w-20 rounded-full bg-[#f1f5f8]" />
                  <span className="block h-4 w-8 rounded-full bg-[#f4f7f9]" />
                </span>
              ))}
            </div>

            <InvoicesSkeleton />
          </div>
        </>
      )}

      {/* -------------------------------------------------- error */}
      {state.kind === "error" && (
        <div className={`${SURFACE_CLASS} overflow-hidden`}>
          <div className="h-1 w-full bg-gradient-to-r from-[#d98b8b] via-[#f3e2e2] to-transparent" />

          <div className="px-6 py-12 sm:px-10 sm:py-14">
            <p className="text-[10.5px] font-semibold uppercase tracking-[0.22em] text-[#9aa3aa]">
              Unavailable
            </p>

            <h2 className="mt-4 font-serif text-[26px] leading-[1.15] tracking-[-0.03em] text-[#17191b] sm:text-[34px]">
              Could not load invoices
            </h2>

            <p className="mt-4 max-w-md text-[14.5px] leading-7 text-[#71777c]">
              {state.message}
            </p>

            <button
              type="button"
              onClick={() => setReloadToken((token) => token + 1)}
              className={`mt-8 ${SECONDARY_ACTION_CLASS}`}
            >
              Try again
            </button>
          </div>
        </div>
      )}

      {/* -------------------------------------------------- empty collection */}
      {state.kind === "loaded" && items.length === 0 && <EmptyInvoices />}

      {/* -------------------------------------------------- collection */}
      {state.kind === "loaded" && items.length > 0 && (
        <div className="space-y-6">
          {/* Compact overview: an editorial segmented strip, not dashboard cards.
              Scope is the loaded page of invoices, the same scope the filters
              below operate on. */}
          <div
            role="group"
            aria-label="Invoice overview by status"
            className="-mx-1 flex flex-wrap items-center gap-x-8 gap-y-5 border-y border-[#eef2f6] px-1 py-5"
          >
            {STATUS_GROUPS.map((group) => {
              const active = statusGroup === group.key;

              return (
                <button
                  key={group.key}
                  type="button"
                  aria-pressed={active}
                  onClick={() => setStatusGroup(group.key)}
                  className={`group flex min-w-0 flex-col items-start rounded-lg px-2 py-1 text-left transition-colors duration-200 ease-out hover:bg-[#f7fafc] motion-reduce:transition-colors ${FOCUS_RING}`}
                >
                  <span className="flex items-center gap-2">
                    <span
                      aria-hidden="true"
                      className={`h-1.5 w-1.5 shrink-0 rounded-full transition-transform duration-200 ease-out group-hover:scale-125 motion-reduce:transform-none ${group.dot}`}
                    />
                    <span
                      className={`text-[11px] font-medium uppercase tracking-[0.16em] transition-colors duration-200 ${
                        active ? "text-[#477cae]" : "text-[#a8b1b8]"
                      }`}
                    >
                      {group.label}
                    </span>
                  </span>

                  <span
                    className={`mt-1.5 pl-3.5 font-serif text-[26px] leading-none tabular-nums transition-colors duration-200 ${
                      active ? "text-[#17191b]" : "text-[#8b949c]"
                    }`}
                  >
                    {counts[group.key]}
                  </span>
                </button>
              );
            })}
          </div>

          {/* Search and filter workspace */}
          <div className={`${SURFACE_CLASS} p-4 sm:p-5`}>
            <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between lg:gap-6">
              <div className="relative lg:max-w-md lg:flex-1">
                <label htmlFor="invoice-search" className="sr-only">
                  Search this page by invoice ID, filename, vendor, invoice
                  number, or PO number
                </label>

                <span
                  aria-hidden="true"
                  className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-[#a8b1b8] transition-colors duration-200 peer-focus:text-[#5b9bd5]"
                >
                  <SearchIcon />
                </span>

                <input
                  id="invoice-search"
                  type="search"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Search this page by ID, filename, vendor, number, or PO"
                  className={`peer h-12 w-full rounded-xl border border-[#e3eaf0] bg-[#fbfcfd] pl-11 pr-4 text-[14px] text-[#17191b] placeholder:text-[#9aa3aa] transition-colors duration-200 ease-out hover:border-[#d7e6f2] focus:border-[#bcd9f0] focus:bg-white focus:outline-none focus:ring-2 focus:ring-[#5b9bd5]/20 motion-reduce:transition-colors ${FOCUS_RING}`}
                />
              </div>

              <div className="flex items-center gap-3">
                <label
                  htmlFor="invoice-sort"
                  className="text-[11px] font-semibold uppercase tracking-[0.16em] text-[#a8b1b8]"
                >
                  Sort
                </label>

                <select
                  id="invoice-sort"
                  value={sort}
                  onChange={(event) => setSort(event.target.value as SortKey)}
                  className={`h-12 min-w-0 flex-1 rounded-xl border border-[#e3eaf0] bg-white px-3.5 text-[13.5px] text-[#3f474d] transition-colors duration-200 ease-out hover:border-[#d7e6f2] focus:border-[#bcd9f0] focus:outline-none focus:ring-2 focus:ring-[#5b9bd5]/20 motion-reduce:transition-colors lg:flex-none ${FOCUS_RING}`}
                >
                  {(Object.keys(SORT_LABELS) as SortKey[]).map((key) => (
                    <option key={key} value={key}>
                      {SORT_LABELS[key]}
                    </option>
                  ))}
                </select>

                {filtersActive && (
                  <button
                    type="button"
                    onClick={clearFilters}
                    className={`hidden h-12 shrink-0 items-center gap-2 rounded-xl px-4 text-[13px] font-medium text-[#858b90] transition-colors duration-200 ease-out hover:bg-[#f4f7f9] hover:text-[#477cae] motion-reduce:transition-colors lg:inline-flex ${FOCUS_RING}`}
                  >
                    <span aria-hidden="true">×</span>
                    Clear filters
                  </button>
                )}
              </div>
            </div>

            {filtersActive && (
              <div className="mt-4 flex items-center justify-between gap-4 border-t border-[#f1f5f8] pt-4 lg:hidden">
                <p className="text-[12.5px] text-[#858b90]">
                  {visible.length} of {items.length} shown
                </p>

                <button
                  type="button"
                  onClick={clearFilters}
                  className={`inline-flex items-center gap-1.5 rounded-full px-3 py-2 text-[12.5px] font-medium text-[#477cae] transition-colors duration-200 hover:bg-[#eef7fd] motion-reduce:transition-colors ${FOCUS_RING}`}
                >
                  <span aria-hidden="true">×</span>
                  Clear filters
                </button>
              </div>
            )}
          </div>

          {/* Main collection */}
          {visible.length === 0 ? (
            <NoResults
              note={
                pageCount > 1
                  ? "Search and status apply to this page only — try another page or clear the filters."
                  : "Try a different search term or status."
              }
              onClear={clearFilters}
            />
          ) : (
            <>
              <div className="hidden md:block">
                <InvoicesTable invoices={visible} onDelete={requestDelete} />
              </div>

              <div className="md:hidden">
                <InvoicesList invoices={visible} onDelete={requestDelete} />
              </div>
            </>
          )}

          {/* Collection controls */}
          <div className="flex flex-col gap-4 border-t border-[#eef2f6] pt-5 text-[12.5px] text-[#858b90] sm:flex-row sm:items-center sm:justify-between">
            <p className="tabular-nums">
              {filtersActive ? (
                <>
                  {visible.length} of {items.length} on this page match
                  {" · "}
                </>
              ) : null}
              Page {page} of {pageCount}
              {" · "}
              {total} {total === 1 ? "invoice" : "invoices"} on file
            </p>

            <PaginationControls
              page={page}
              pageCount={pageCount}
              onPageChange={handlePageChange}
            />
          </div>
        </div>
      )}
    </section>
  );
}