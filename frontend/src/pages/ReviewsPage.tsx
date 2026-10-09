import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, listReviews } from "../api";
import type { ReviewCase, ReviewListResponse, ReviewStatus } from "../types/review";
import { isReviewOpen } from "../types/review";
import {
  GENERIC_ERROR_MESSAGE,
  displayOrDash,
  formatDateTime,
  formatMoney,
  formatStatusLabel,
} from "./InvoiceListPage";

type ListState =
  | { kind: "loading" }
  | { kind: "loaded"; data: ReviewListResponse }
  | { kind: "error"; message: string };

/* ------------------------------------------------------------------ *
 * Shared visual tokens. Identical values to App.tsx, InvoiceListPage
 * and PoliciesPage so every page reads as one product.
 * ------------------------------------------------------------------ */

/** Visible keyboard focus for every interactive control on the page. */
const FOCUS_RING =
  "focus:outline-none focus-visible:ring-2 focus-visible:ring-[#5b9bd5]/55 focus-visible:ring-offset-2";

/** Quieter action used for recovery and reset. */
const SECONDARY_ACTION_CLASS = `inline-flex items-center justify-center gap-2 rounded-full border border-[#e3e9ee] bg-white px-5 py-3 text-sm font-medium text-[#3f474d] transition-all duration-200 ease-out hover:-translate-y-0.5 hover:border-[#dcebfb] hover:bg-[#f7fbfe] hover:text-[#477cae] motion-reduce:transform-none motion-reduce:transition-colors ${FOCUS_RING}`;

/** Hairline card surface, matching the panels elsewhere in the app. */
const SURFACE_CLASS =
  "rounded-[22px] border border-[#e7edf2] bg-white shadow-[0_1px_2px_rgba(23,25,27,0.03),0_24px_60px_-46px_rgba(23,52,74,0.7)]";

/** Small uppercase label used for column heads and section eyebrows. */
const LABEL_CLASS =
  "text-[10.5px] font-semibold uppercase tracking-[0.2em] text-[#a8b1b8]";

/** One filter tab. `null` status means "no filter". */
type Filter = { label: string; status: ReviewStatus | null };

const FILTERS: Filter[] = [
  { label: "All", status: null },
  { label: "Pending", status: "pending" },
  { label: "In progress", status: "in_progress" },
  { label: "Resolved", status: "resolved" },
  { label: "Dismissed", status: "dismissed" },
];

/**
 * Review status -> badge colours.
 *
 * The semantic reasoning is unchanged from the original mapping: pending is
 * amber (waiting on a human), in_progress means a human already has it,
 * resolved is emerald, and dismissed is neutral (closed, no action). Only the
 * hue for `in_progress` changes - indigo was not part of this application's
 * palette, so it takes the workspace blue the rest of the app uses.
 *
 * The API reports the *case* status only, not the decision behind a resolved
 * case, so the label never claims what was decided.
 */
const REVIEW_STATUS_BADGE_CLASSES: Record<ReviewStatus, string> = {
  pending: "bg-amber-50/80 text-amber-700 ring-amber-100",
  in_progress: "bg-[#eef7fd] text-[#477cae] ring-[#dcebfb]",
  resolved: "bg-emerald-50/80 text-emerald-700 ring-emerald-100",
  dismissed: "bg-[#f3f5f7] text-[#5c666e] ring-[#e6ebef]",
};

/** Status -> the dot colour used by the badge and the overview strip. */
const REVIEW_STATUS_DOT_CLASSES: Record<ReviewStatus, string> = {
  pending: "bg-amber-500",
  in_progress: "bg-[#5b9bd5]",
  resolved: "bg-emerald-500",
  dismissed: "bg-[#9aa3aa]",
};

type SortKey = "newest" | "oldest" | "highest" | "lowest" | "vendor";

const SORT_LABELS: Record<SortKey, string> = {
  newest: "Newest flagged first",
  oldest: "Oldest flagged first",
  highest: "Highest invoice total",
  lowest: "Lowest invoice total",
  vendor: "Vendor A–Z",
};

/** Crisp, dependency-free magnifier. */
function SearchIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 20 20" fill="none" className="h-4 w-4">
      <circle cx="8.75" cy="8.75" r="5.25" stroke="currentColor" strokeWidth="1.5" />
      <path d="m12.6 12.6 3.4 3.4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

/** Status pill: tinted background, small dot, uppercase label, quiet ring. */
function ReviewStatusBadge({ status }: { status: ReviewStatus }) {
  return (
    <span
      className={`inline-flex items-center gap-2 whitespace-nowrap rounded-full px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] ring-1 ring-inset ${REVIEW_STATUS_BADGE_CLASSES[status]}`}
    >
      <span
        aria-hidden="true"
        className={`h-1.5 w-1.5 shrink-0 rounded-full ${REVIEW_STATUS_DOT_CLASSES[status]}`}
      />
      {formatStatusLabel(status.replace(/_/g, " "))}
    </span>
  );
}

/**
 * Filters client-side.
 *
 * The backend page is fetched unfiltered and narrowed here. That is deliberate:
 * a reviewer's queue is a working set, not an archive, and keeping one request
 * means the tabs respond instantly without refetching on every click. The
 * backend's own `status` parameter still exists and is used by
 * `InvoiceDetailPage` where a single case is all that is needed.
 */
function applyFilter(data: ReviewListResponse, status: ReviewStatus | null) {
  if (status === null) {
    return data.items;
  }
  return data.items.filter((item) => item.status === status);
}

/** Timestamp for sorting; unparseable dates fall back to 0 so they sort last. */
function timeValue(value: string): number {
  const parsed = new Date(value).getTime();
  return Number.isNaN(parsed) ? 0 : parsed;
}

/**
 * Monetary value for sorting.
 *
 * `total_amount` is a serialised Decimal kept as a string so the backend's
 * precision survives. Parsing here is for ordering only - the displayed value
 * still goes through `formatMoney` on the untouched string.
 */
function amountValue(review: ReviewCase): number {
  if (review.invoice_summary.total_amount === null) return Number.NEGATIVE_INFINITY;
  const parsed = Number(review.invoice_summary.total_amount);
  return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY;
}

function sortReviews(items: ReviewCase[], sort: SortKey): ReviewCase[] {
  const sorted = [...items];

  switch (sort) {
    case "oldest":
      return sorted.sort((a, b) => timeValue(a.created_at) - timeValue(b.created_at));
    case "highest":
      return sorted.sort((a, b) => amountValue(b) - amountValue(a));
    case "lowest":
      return sorted.sort((a, b) => amountValue(a) - amountValue(b));
    case "vendor":
      return sorted.sort((a, b) =>
        (a.invoice_summary.vendor_name ?? "").localeCompare(
          b.invoice_summary.vendor_name ?? ""
        )
      );
    default:
      return sorted.sort((a, b) => timeValue(b.created_at) - timeValue(a.created_at));
  }
}

/**
 * Search across every human-authored string on a case.
 *
 * There is no server-side search parameter on this endpoint, so this narrows
 * the already-fetched page only - the same scope the status tabs operate on.
 */
function matchesQuery(review: ReviewCase, query: string): boolean {
  if (query === "") return true;

  const needle = query.toLowerCase();

  return (
    (review.invoice_summary.vendor_name ?? "").toLowerCase().includes(needle) ||
    (review.invoice_summary.invoice_number ?? "").toLowerCase().includes(needle) ||
    review.reason.toLowerCase().includes(needle) ||
    (review.assigned_to ?? "").toLowerCase().includes(needle) ||
    review.status.toLowerCase().includes(needle)
  );
}

/** Quiet glyph that anchors each case's identity. */
function CaseGlyph({ open }: { open: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={`relative grid h-10 w-[30px] shrink-0 place-items-center overflow-hidden rounded-lg border shadow-[0_1px_2px_rgba(23,25,27,0.04)] ${
        open
          ? "border-[#dcebfb] bg-[#fbfdfe]"
          : "border-[#eef2f6] bg-[#fafbfc]"
      }`}
    >
      <span className={`h-3.5 w-3.5 rounded-[2px] ${open ? "bg-[#dbe9f5]" : "bg-[#eef2f6]"}`} />
      <span
        className={`absolute bottom-1.5 left-1.5 h-px w-3.5 ${
          open ? "bg-[#cbd9e4]" : "bg-[#e6ebef]"
        }`}
      />
    </span>
  );
}

/**
 * Desktop queue.
 *
 * Semantic table markup. `reason` wraps rather than truncating - it is the
 * justification a reviewer acts on, so clamping it away would hide the one
 * field that matters.
 */
function ReviewsTable({ items }: { items: ReviewCase[] }) {
  return (
    <div className={`${SURFACE_CLASS} overflow-hidden`}>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[840px] border-collapse text-left">
          <caption className="sr-only">
            Invoice review cases awaiting or carrying a human decision
          </caption>

          <thead>
            <tr className="border-b border-[#eef2f6]">
              <th scope="col" className={`px-6 py-4 ${LABEL_CLASS}`}>
                Invoice
              </th>
              <th scope="col" className={`px-6 py-4 ${LABEL_CLASS}`}>
                Reason
              </th>
              <th scope="col" className={`px-6 py-4 ${LABEL_CLASS}`}>
                Status
              </th>
              <th scope="col" className={`px-6 py-4 ${LABEL_CLASS}`}>
                Assigned
              </th>
              <th scope="col" className={`px-6 py-4 text-right ${LABEL_CLASS}`}>
                Amount
              </th>
              <th scope="col" className={`px-6 py-4 ${LABEL_CLASS}`}>
                Flagged
              </th>
            </tr>
          </thead>

          <tbody>
            {items.map((item) => {
              const open = isReviewOpen(item.status);

              return (
                <tr
                  key={item.id}
                  className="border-b border-[#f4f7f9] transition-colors duration-200 ease-out last:border-b-0 hover:bg-[#f7fafc] focus-within:bg-[#f7fafc] motion-reduce:transition-colors"
                >
                  <td className="max-w-[220px] px-6 py-5 align-middle">
                    <div className="flex min-w-0 items-center gap-4">
                      <CaseGlyph open={open} />

                      <div className="min-w-0">
                        <Link
                          to={`/invoices/${item.invoice_id}`}
                          className={`block truncate text-[14px] font-medium text-[#17191b] transition-colors duration-200 hover:text-[#477cae] motion-reduce:transition-colors ${FOCUS_RING}`}
                        >
                          {displayOrDash(item.invoice_summary.vendor_name)}
                        </Link>
                        <span className="mt-1 block truncate font-mono text-[12px] text-[#a8b1b8]">
                          {displayOrDash(item.invoice_summary.invoice_number)}
                        </span>
                      </div>
                    </div>
                  </td>

                  <td className="max-w-[320px] px-6 py-5 align-middle">
                    <p className="break-words text-[13.5px] leading-6 text-[#5c666e]">
                      {item.reason}
                    </p>

                    {item.resolution !== null && (
                      <p className="mt-1.5 break-words border-l-2 border-[#e6ebef] pl-3 text-[12.5px] leading-5 text-[#858b90]">
                        {item.resolution}
                      </p>
                    )}
                  </td>

                  <td className="px-6 py-5 align-middle">
                    <ReviewStatusBadge status={item.status} />
                  </td>

                  <td className="max-w-[140px] px-6 py-5 align-middle">
                    <span className="block truncate text-[13px] text-[#71777c]">
                      {displayOrDash(item.assigned_to)}
                    </span>
                  </td>

                  <td className="whitespace-nowrap px-6 py-5 text-right align-middle text-[14px] font-medium tabular-nums tracking-[-0.01em] text-[#17191b]">
                    {formatMoney(
                      item.invoice_summary.total_amount,
                      item.invoice_summary.currency
                    )}
                  </td>

                  <td className="whitespace-nowrap px-6 py-5 align-middle">
                    <time
                      dateTime={item.created_at}
                      className="block text-[13px] tabular-nums text-[#858b90]"
                      title={item.created_at}
                    >
                      {formatDateTime(item.created_at)}
                    </time>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** Mobile queue items: identity and reason first, metadata beneath. */
function ReviewsList({ items }: { items: ReviewCase[] }) {
  return (
    <ul className={`${SURFACE_CLASS} divide-y divide-[#eef2f6] overflow-hidden`}>
      {items.map((item) => {
        const open = isReviewOpen(item.status);

        return (
          <li
            key={item.id}
            className="px-5 py-5 transition-colors duration-200 ease-out hover:bg-[#f7fafc] motion-reduce:transition-colors"
          >
            <div className="flex items-start gap-4">
              <CaseGlyph open={open} />

              <div className="min-w-0 flex-1">
                <Link
                  to={`/invoices/${item.invoice_id}`}
                  className={`block truncate text-[14.5px] font-medium text-[#17191b] transition-colors duration-200 hover:text-[#477cae] motion-reduce:transition-colors ${FOCUS_RING}`}
                >
                  {displayOrDash(item.invoice_summary.vendor_name)}
                </Link>
                <span className="mt-1 block truncate font-mono text-[12px] text-[#a8b1b8]">
                  {displayOrDash(item.invoice_summary.invoice_number)}
                </span>
              </div>

              <span className="shrink-0 text-right text-[14px] font-medium tabular-nums tracking-[-0.01em] text-[#17191b]">
                {formatMoney(
                  item.invoice_summary.total_amount,
                  item.invoice_summary.currency
                )}
              </span>
            </div>

            <p className="mt-3.5 break-words text-[13.5px] leading-6 text-[#5c666e]">
              {item.reason}
            </p>

            {item.resolution !== null && (
              <p className="mt-2 break-words border-l-2 border-[#e6ebef] pl-3 text-[12.5px] leading-5 text-[#858b90]">
                {item.resolution}
              </p>
            )}

            <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
              <ReviewStatusBadge status={item.status} />

              <span
                className="text-[12.5px] tabular-nums text-[#858b90]"
                title={item.created_at}
              >
                Flagged {formatDateTime(item.created_at)}
              </span>
            </div>

            {item.assigned_to !== null && (
              <p className="mt-2.5 truncate text-[12.5px] text-[#858b90]">
                Assigned to {item.assigned_to}
              </p>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** Row-shaped placeholders so the loading state matches the final layout. */
function ReviewsSkeleton() {
  return (
    <div className={`${SURFACE_CLASS} overflow-hidden`}>
      <div className="hidden items-center gap-6 border-b border-[#eef2f6] px-6 py-4 md:flex">
        <span className="h-2.5 w-16 rounded-full bg-[#f1f5f8]" />
        <span className="h-2.5 w-16 rounded-full bg-[#f1f5f8]" />
        <span className="h-2.5 w-16 rounded-full bg-[#f1f5f8]" />
        <span className="ml-auto h-2.5 w-16 rounded-full bg-[#f1f5f8]" />
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
            <span className="block h-2 w-52 max-w-full rounded-full bg-[#f4f7f9]" />
          </span>
          <span className="hidden h-6 w-20 shrink-0 rounded-full bg-[#f4f7f9] sm:block" />
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
 * Review queue: every invoice the risk engine flagged, newest first.
 *
 * Fetches one unfiltered page on mount and filters in memory, so switching tabs
 * is instant and cannot show a stale count.
 */
export default function ReviewsPage() {
  const [state, setState] = useState<ListState>({ kind: "loading" });
  const [reloadToken, setReloadToken] = useState(0);
  const [filter, setFilter] = useState<ReviewStatus | null>(null);
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<SortKey>("newest");

  useEffect(() => {
    let isActive = true;
    setState({ kind: "loading" });

    listReviews({ limit: 200 })
      .then((data) => {
        if (isActive) setState({ kind: "loaded", data });
      })
      .catch((error: unknown) => {
        if (!isActive) return;
        setState({
          kind: "error",
          message: error instanceof ApiError ? error.message : GENERIC_ERROR_MESSAGE,
        });
      });

    return () => {
      isActive = false;
    };
  }, [reloadToken]);

  const items = state.kind === "loaded" ? state.data.items : [];

  /** Counts come straight from the loaded cases, never from a hardcoded number. */
  const counts = useMemo(() => {
    const result: Record<ReviewStatus, number> = {
      pending: 0,
      in_progress: 0,
      resolved: 0,
      dismissed: 0,
    };

    for (const item of items) {
      result[item.status] += 1;
    }

    return result;
  }, [items]);

  /**
   * The overview strip is scoped to the fetched page, which is why it says so.
   * `total` is the server's count for the whole collection and may exceed the
   * page, so it is never presented as if the strip described everything.
   */
  const awaitingCount = useMemo(
    () => items.filter((item) => isReviewOpen(item.status)).length,
    [items]
  );

  /**
   * Deliberately no money total in the overview: invoices in one queue can
   * carry different currencies, and summing them would be a meaningless figure.
   */
  const statusFiltered = useMemo(
    () =>
      applyFilter(
        state.kind === "loaded"
          ? state.data
          : { items: [], total: 0, limit: 0, offset: 0 },
        filter
      ),
    [state, filter]
  );

  const visible = useMemo(
    () => sortReviews(
      statusFiltered.filter((item) => matchesQuery(item, query.trim())),
      sort
    ),
    [statusFiltered, query, sort]
  );

  const filtersActive = query.trim() !== "" || filter !== null;
  const total = state.kind === "loaded" ? state.data.total : 0;
  const hasMoreThanLoaded = state.kind === "loaded" && total > items.length;

  function clearFilters(): void {
    setQuery("");
    setFilter(null);
  }

  return (
    <section className="space-y-10">
      {/* -------------------------------------------------- header */}
      <header className="min-w-0">
        <p className="flex items-center gap-3 text-[10.5px] font-semibold uppercase tracking-[0.22em] text-[#9aa3aa]">
          <span
            aria-hidden="true"
            className="h-1.5 w-1.5 rounded-full bg-[#5b9bd5] shadow-[0_0_0_3px_rgba(91,155,213,0.22)]"
          />
          Reviews
          <span aria-hidden="true" className="h-px w-8 bg-[#dfe6ec]" />
        </p>

        <h1 className="mt-5 font-serif text-[34px] leading-[1.06] tracking-[-0.035em] text-[#17191b] sm:text-[44px] lg:text-[52px]">
          The review queue
        </h1>

        <p className="mt-4 max-w-[52ch] text-[15px] leading-7 text-[#71777c]">
          {state.kind === "loaded"
            ? `${total} invoice${total === 1 ? "" : "s"} the risk assessment flagged for a human decision.`
            : "Invoices the risk assessment flagged for a human decision."}
        </p>
      </header>

      {/* -------------------------------------------------- loading */}
      {state.kind === "loading" && (
        <>
          <div role="status" aria-live="polite" className="sr-only">
            Loading review cases…
          </div>

          <div className="space-y-5">
            <div className="flex flex-wrap items-center gap-x-8 gap-y-5 border-y border-[#eef2f6] py-5">
              {[0, 1, 2, 3, 4].map((cell) => (
                <span key={cell} className="space-y-2">
                  <span className="block h-2 w-20 rounded-full bg-[#f1f5f8]" />
                  <span className="block h-4 w-8 rounded-full bg-[#f4f7f9]" />
                </span>
              ))}
            </div>

            <ReviewsSkeleton />
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
              Could not load the queue
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
      {state.kind === "loaded" && items.length === 0 && (
        <div className={`${SURFACE_CLASS} px-6 py-16 text-center sm:px-10 sm:py-20`}>
          <DocumentIllustration />

          <h2 className="mt-8 font-serif text-[26px] leading-tight tracking-[-0.025em] text-[#17191b] sm:text-[32px]">
            Nothing needs a decision
          </h2>

          <p className="mx-auto mt-3 max-w-sm text-[14.5px] leading-7 text-[#71777c]">
            No review cases. Invoices flagged as HIGH risk appear here
            automatically.
          </p>
        </div>
      )}

      {/* -------------------------------------------------- collection */}
      {state.kind === "loaded" && items.length > 0 && (
        <div className="space-y-6">
          {/* Compact overview: an editorial segmented strip that doubles as the
              status filter, so the counts and the control cannot disagree. */}
          <div
            role="group"
            aria-label="Review queue overview by status"
            className="-mx-1 flex flex-wrap items-center gap-x-8 gap-y-5 border-y border-[#eef2f6] px-1 py-5"
          >
            {FILTERS.map((entry) => {
              const active = filter === entry.status;
              const count =
                entry.status === null ? items.length : counts[entry.status];

              return (
                <button
                  key={entry.label}
                  type="button"
                  aria-pressed={active}
                  onClick={() => setFilter(entry.status)}
                  className={`group flex flex-col items-start rounded-lg px-2 py-1 text-left transition-colors duration-200 ease-out hover:bg-[#f7fafc] motion-reduce:transition-colors ${FOCUS_RING}`}
                >
                  <span className="flex items-center gap-2">
                    <span
                      aria-hidden="true"
                      className={`h-1.5 w-1.5 shrink-0 rounded-full transition-transform duration-200 ease-out group-hover:scale-125 motion-reduce:transform-none ${
                        entry.status === null
                          ? "bg-[#c3ccd3]"
                          : REVIEW_STATUS_DOT_CLASSES[entry.status]
                      }`}
                    />
                    <span
                      className={`text-[11px] font-medium uppercase tracking-[0.16em] transition-colors duration-200 ${
                        active ? "text-[#477cae]" : "text-[#a8b1b8]"
                      }`}
                    >
                      {entry.label}
                    </span>
                  </span>

                  <span
                    className={`mt-1.5 pl-3.5 font-serif text-[26px] leading-none tabular-nums transition-colors duration-200 ${
                      active ? "text-[#17191b]" : "text-[#8b949c]"
                    }`}
                  >
                    {count}
                  </span>
                </button>
              );
            })}
          </div>

          {/* Search and filter workspace */}
          <div className={`${SURFACE_CLASS} p-4 sm:p-5`}>
            <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between lg:gap-6">
              <div className="relative lg:max-w-md lg:flex-1">
                <label htmlFor="review-search" className="sr-only">
                  Search review cases by vendor, invoice number, reason, or
                  assignee
                </label>

                <span
                  aria-hidden="true"
                  className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-[#a8b1b8] transition-colors duration-200 peer-focus:text-[#5b9bd5]"
                >
                  <SearchIcon />
                </span>

                <input
                  id="review-search"
                  type="search"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Search vendors, reasons, or assignees"
                  className={`peer h-12 w-full rounded-xl border border-[#e3eaf0] bg-[#fbfcfd] pl-11 pr-4 text-[14px] text-[#17191b] placeholder:text-[#9aa3aa] transition-colors duration-200 ease-out hover:border-[#d7e6f2] focus:border-[#bcd9f0] focus:bg-white focus:outline-none focus:ring-2 focus:ring-[#5b9bd5]/20 motion-reduce:transition-colors ${FOCUS_RING}`}
                />
              </div>

              <div className="flex items-center gap-3">
                <label
                  htmlFor="review-sort"
                  className="text-[11px] font-semibold uppercase tracking-[0.16em] text-[#a8b1b8]"
                >
                  Sort
                </label>

                <select
                  id="review-sort"
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
                <p className="text-[12.5px] tabular-nums text-[#858b90]">
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
            <div className={`${SURFACE_CLASS} px-6 py-16 text-center sm:px-10 sm:py-20`}>
              <DocumentIllustration />

              <h2 className="mt-8 font-serif text-[24px] leading-tight tracking-[-0.025em] text-[#17191b] sm:text-[28px]">
                Nothing matches those filters
              </h2>

              <p className="mx-auto mt-3 max-w-sm text-[14.5px] leading-7 text-[#71777c]">
                {hasMoreThanLoaded
                  ? "Only the review cases already loaded can be filtered here."
                  : "Try a different search term or status."}
              </p>

              <button
                type="button"
                onClick={clearFilters}
                className={`mt-9 ${SECONDARY_ACTION_CLASS}`}
              >
                Clear filters
              </button>
            </div>
          ) : (
            <>
              <div className="hidden md:block">
                <ReviewsTable items={visible} />
              </div>

              <div className="md:hidden">
                <ReviewsList items={visible} />
              </div>
            </>
          )}

          {/* Collection controls */}
          <div className="flex flex-col gap-3 border-t border-[#eef2f6] pt-5 text-[12.5px] tabular-nums text-[#858b90] sm:flex-row sm:items-center sm:justify-between">
            <p>
              Showing {visible.length} of {items.length} loaded{" "}
              {items.length === 1 ? "case" : "cases"}
              {filter !== null &&
                ` · filtered to ${formatStatusLabel(filter.replace(/_/g, " "))}`}
            </p>

            {hasMoreThanLoaded && (
              <p>
                {total} {total === 1 ? "case" : "cases"} on file ·{" "}
                {awaitingCount} awaiting a decision
              </p>
            )}
          </div>
        </div>
      )}
    </section>
  );
}