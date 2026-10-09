import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";

import { ApiError, deletePolicy, listPolicies, processPolicy, uploadPolicy } from "../api";
import type { PolicyDocument } from "../types/policy";
import { formatDateTime } from "./InvoiceListPage";

const GENERIC_ERROR_MESSAGE = "Something went wrong. Please try again.";

/* ------------------------------------------------------------------ *
 * Shared visual tokens. Identical values to App.tsx, InvoiceListPage
 * and InvoiceDetailPage so all four pages read as one product.
 * ------------------------------------------------------------------ */

/** Visible keyboard focus for every interactive control on the page. */
const FOCUS_RING =
  "focus:outline-none focus-visible:ring-2 focus-visible:ring-[#5b9bd5]/55 focus-visible:ring-offset-2";

/** The application's primary blue, in the app shell's button language. */
const PRIMARY_ACTION_CLASS = `group inline-flex items-center justify-center gap-2.5 rounded-full bg-[#5b9bd5] px-6 py-3.5 text-sm font-medium text-white shadow-[0_14px_30px_-12px_rgba(91,155,213,0.75)] transition-all duration-200 ease-out hover:-translate-y-0.5 hover:bg-[#477cae] hover:shadow-[0_18px_36px_-14px_rgba(91,155,213,0.85)] disabled:cursor-not-allowed disabled:bg-[#9aa3aa] disabled:shadow-none disabled:hover:translate-y-0 motion-reduce:transform-none motion-reduce:transition-colors ${FOCUS_RING}`;

/** Quieter sibling to the primary action. */
const SECONDARY_ACTION_CLASS = `inline-flex items-center justify-center gap-2 rounded-full border border-[#e3e9ee] bg-white px-5 py-3 text-sm font-medium text-[#3f474d] transition-all duration-200 ease-out hover:-translate-y-0.5 hover:border-[#dcebfb] hover:bg-[#f7fbfe] hover:text-[#477cae] disabled:cursor-not-allowed disabled:border-[#eef2f6] disabled:text-[#a8b1b8] disabled:hover:translate-y-0 disabled:hover:bg-white motion-reduce:transform-none motion-reduce:transition-colors ${FOCUS_RING}`;

/** Hairline card surface, matching the panels elsewhere in the app. */
const SURFACE_CLASS =
  "rounded-[22px] border border-[#e7edf2] bg-white shadow-[0_1px_2px_rgba(23,25,27,0.03),0_24px_60px_-46px_rgba(23,52,74,0.7)]";

/** Small uppercase label used for column heads and section eyebrows. */
const LABEL_CLASS =
  "text-[10.5px] font-semibold uppercase tracking-[0.2em] text-[#a8b1b8]";

/** The muted spinner used while a check or upload is in flight. */
const SPINNER_CLASS =
  "h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-[#dcebfb] border-t-[#5b9bd5]";

type ListState =
  | { kind: "loading" }
  | { kind: "loaded"; items: PolicyDocument[] }
  | { kind: "error"; message: string };

type UploadState =
  | { kind: "idle" }
  | { kind: "uploading" }
  | { kind: "processing"; title: string }
  | { kind: "success"; title: string; chunkCount: number }
  | { kind: "error"; message: string };

/** Per-policy action state, so one row's spinner does not block the table. */
type RowActionState =
  | { kind: "idle" }
  | { kind: "processing" }
  | { kind: "deactivating" };

/**
 * Activity filter.
 *
 * `PolicyDocument` models state as a single `is_active` boolean rather than a
 * status enum, so this is a boolean filter and nothing more. There is no draft,
 * review, or archived state on this resource to filter by.
 */
type ActivityFilter = "all" | "active" | "inactive";

type SortKey = "newest" | "oldest" | "title" | "most_chunks" | "fewest_chunks";

const SORT_LABELS: Record<SortKey, string> = {
  newest: "Newest upload first",
  oldest: "Oldest upload first",
  title: "Title A–Z",
  most_chunks: "Most chunks first",
  fewest_chunks: "Fewest chunks first",
};

const ACTIVITY_FILTERS: { key: ActivityFilter; label: string; dot: string }[] = [
  { key: "all", label: "All policies", dot: "bg-[#c3ccd3]" },
  { key: "active", label: "Active", dot: "bg-emerald-500" },
  { key: "inactive", label: "Inactive", dot: "bg-[#9aa3aa]" },
];

/**
 * Translate an upload or process failure into something a user can act on.
 *
 * The backend distinguishes these cases for a reason - 409 means the bytes are
 * already stored, 415 means it is not a PDF, 413 means it is too big - and
 * collapsing them into one "upload failed" string throws away the only signal
 * the user gets about what to change.
 */
function describeError(error: unknown, phase: "upload" | "process"): string {
  if (!(error instanceof ApiError)) {
    return GENERIC_ERROR_MESSAGE;
  }

  switch (error.status) {
    case 409:
      return "This policy has already been uploaded. Its content is stored under the same file hash.";
    case 413:
      return "That file is larger than the 20 MB upload limit.";
    case 415:
      return "Policies must be PDF files.";
    case 422:
      return phase === "process"
        ? "The policy could not be processed. It may have no extractable text (a scan)."
        : "The policy was rejected as invalid.";
    default:
      return error.message || GENERIC_ERROR_MESSAGE;
  }
}

/** Timestamp for sorting; unparseable dates fall back to 0 so they sort last. */
function timeValue(value: string): number {
  const parsed = new Date(value).getTime();
  return Number.isNaN(parsed) ? 0 : parsed;
}

/** Title + source filename are the only human-authored text on a policy. */
function matchesQuery(policy: PolicyDocument, query: string): boolean {
  if (query === "") return true;

  const needle = query.toLowerCase();

  return (
    policy.title.toLowerCase().includes(needle) ||
    policy.source_filename.toLowerCase().includes(needle)
  );
}

function sortPolicies(policies: PolicyDocument[], sort: SortKey): PolicyDocument[] {
  const sorted = [...policies];

  switch (sort) {
    case "oldest":
      return sorted.sort((a, b) => timeValue(a.uploaded_at) - timeValue(b.uploaded_at));
    case "title":
      return sorted.sort((a, b) => a.title.localeCompare(b.title));
    case "most_chunks":
      return sorted.sort((a, b) => b.chunk_count - a.chunk_count);
    case "fewest_chunks":
      return sorted.sort((a, b) => a.chunk_count - b.chunk_count);
    default:
      return sorted.sort((a, b) => timeValue(b.uploaded_at) - timeValue(a.uploaded_at));
  }
}

/** Crisp, dependency-free magnifier. */
function SearchIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 20 20" fill="none" className="h-4 w-4">
      <circle cx="8.75" cy="8.75" r="5.25" stroke="currentColor" strokeWidth="1.5" />
      <path d="m12.6 12.6 3.4 3.4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

/** Quiet policy glyph that anchors each document's identity. */
function PolicyGlyph() {
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

/**
 * Activity indicator.
 *
 * `is_active` is the only state this resource has. The label carries the
 * meaning so the state never depends on colour alone, and the treatment mirrors
 * the status badges on the invoice pages.
 */
function ActivityPill({ isActive }: { isActive: boolean }) {
  return (
    <span
      className={`inline-flex items-center gap-2 whitespace-nowrap rounded-full px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] ring-1 ring-inset ${
        isActive
          ? "bg-emerald-50/80 text-emerald-700 ring-emerald-100"
          : "bg-[#f3f5f7] text-[#5c666e] ring-[#e6ebef]"
      }`}
    >
      <span
        aria-hidden="true"
        className={`h-1.5 w-1.5 shrink-0 rounded-full ${
          isActive ? "bg-emerald-500" : "bg-[#9aa3aa]"
        }`}
      />
      {isActive ? "Active" : "Inactive"}
    </span>
  );
}

/** Small quiet label above a value. */
function MetaItem({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-[10.5px] font-semibold uppercase tracking-[0.18em] text-[#9aa3aa]">
        {label}
      </dt>
      <dd
        title={title}
        className="mt-1.5 truncate text-[14px] font-medium tabular-nums text-[#282d31]"
      >
        {value}
      </dd>
    </div>
  );
}

/**
 * Row action buttons, shared by the table and the mobile items so the two
 * presentations can never drift apart.
 */
function PolicyActions({
  policy,
  action,
  onReprocess,
  onDeactivate,
  layout,
}: {
  policy: PolicyDocument;
  action: RowActionState;
  onReprocess: (policy: PolicyDocument) => void;
  onDeactivate: (policy: PolicyDocument) => void;
  layout: "table" | "item";
}) {
  const busy = action.kind !== "idle";
  const base =
    "inline-flex items-center justify-center rounded-full text-[12.5px] font-medium transition-all duration-200 ease-out disabled:cursor-not-allowed motion-reduce:transform-none motion-reduce:transition-colors";
  const size =
    layout === "table"
      ? "gap-1.5 px-3.5 py-2"
      : "gap-1.5 px-4 py-2.5";

  return (
    <div className="flex flex-wrap items-center gap-2">
      <button
        type="button"
        onClick={() => onReprocess(policy)}
        disabled={busy}
        className={`${base} ${size} border border-[#e3e9ee] bg-white text-[#3f474d] hover:border-[#dcebfb] hover:bg-[#f7fbfe] hover:text-[#477cae] disabled:border-[#eef2f6] disabled:text-[#a8b1b8] disabled:hover:bg-white ${FOCUS_RING}`}
      >
        {action.kind === "processing" ? (
          <>
            <span aria-hidden="true" className={SPINNER_CLASS} />
            Processing…
          </>
        ) : (
          "Reprocess"
        )}
      </button>

      {policy.is_active ? (
        <button
          type="button"
          onClick={() => onDeactivate(policy)}
          disabled={busy}
          className={`${base} ${size} border border-red-200 bg-white text-red-700 transition-colors duration-200 ease-out hover:border-red-300 hover:bg-red-50 motion-reduce:transition-colors disabled:cursor-not-allowed disabled:border-[#eef2f6] disabled:text-[#a8b1b8] disabled:hover:bg-white ${FOCUS_RING}`}
        >
          {action.kind === "deactivating" ? (
            <>
              <span
                aria-hidden="true"
                className="h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-red-100 border-t-red-500"
              />
              Deactivating…
            </>
          ) : (
            "Deactivate"
          )}
        </button>
      ) : (
        // No reactivation endpoint exists. Leaving the button disabled is
        // honest; wiring it to something that silently does nothing would be
        // worse.
        <button
          type="button"
          disabled
          title="Reactivation is not implemented yet"
          className={`${base} ${size} border border-[#eef2f6] bg-[#fbfcfd] text-[#a8b1b8]`}
        >
          Reactivate
        </button>
      )}
    </div>
  );
}

/**
 * Desktop policy collection.
 *
 * Semantic table markup is preserved. Inactive rows stay visually muted - that
 * was a deliberate existing rule, not an accident of styling.
 */
function PoliciesTable({
  policies,
  rowActions,
  onReprocess,
  onDeactivate,
}: {
  policies: PolicyDocument[];
  rowActions: Record<string, RowActionState>;
  onReprocess: (policy: PolicyDocument) => void;
  onDeactivate: (policy: PolicyDocument) => void;
}) {
  return (
    <div className={`${SURFACE_CLASS} overflow-hidden`}>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[880px] border-collapse text-left">
          <caption className="sr-only">Policy documents in the retrieval corpus</caption>

          <thead>
            <tr className="border-b border-[#eef2f6]">
              <th scope="col" className={`px-6 py-4 ${LABEL_CLASS}`}>
                Policy
              </th>
              <th scope="col" className={`px-6 py-4 ${LABEL_CLASS}`}>
                Activity
              </th>
              <th scope="col" className={`px-6 py-4 ${LABEL_CLASS}`}>
                Source file
              </th>
              <th scope="col" className={`px-6 py-4 text-right ${LABEL_CLASS}`}>
                Chunks
              </th>
              <th scope="col" className={`px-6 py-4 ${LABEL_CLASS}`}>
                Updated
              </th>
              <th scope="col" className={`px-6 py-4 text-right ${LABEL_CLASS}`}>
                Actions
              </th>
            </tr>
          </thead>

          <tbody>
            {policies.map((policy) => {
              const action = rowActions[policy.id] ?? { kind: "idle" as const };

              return (
                <tr
                  key={policy.id}
                  // Inactive rows are muted so the table reads at a glance.
                  className={`border-b border-[#f4f7f9] transition-colors duration-200 ease-out last:border-b-0 hover:bg-[#f7fafc] focus-within:bg-[#f7fafc] motion-reduce:transition-colors ${
                    policy.is_active ? "align-middle" : "align-middle opacity-70"
                  }`}
                >
                  <td className="max-w-[280px] px-6 py-5 align-middle">
                    <div className="flex min-w-0 items-center gap-4">
                      <PolicyGlyph />

                      <div className="min-w-0">
                        <p className="truncate text-[14px] font-medium text-[#17191b]">
                          {policy.title}
                        </p>
                        <p className="mt-1 truncate text-[12px] tabular-nums text-[#a8b1b8]">
                          {policy.chunk_count}{" "}
                          {policy.chunk_count === 1 ? "chunk" : "chunks"} indexed
                        </p>
                      </div>
                    </div>
                  </td>

                  <td className="px-6 py-5 align-middle">
                    <ActivityPill isActive={policy.is_active} />
                  </td>

                  <td className="max-w-[240px] px-6 py-5 align-middle">
                    <span
                      className="block truncate font-mono text-[12.5px] text-[#5c666e]"
                      title={policy.sha256}
                    >
                      {policy.source_filename}
                    </span>
                  </td>

                  <td className="whitespace-nowrap px-6 py-5 text-right align-middle text-[14px] font-medium tabular-nums text-[#282d31]">
                    {policy.chunk_count}
                  </td>

                  <td className="whitespace-nowrap px-6 py-5 align-middle">
                    <time
                      dateTime={policy.updated_at}
                      className="block text-[13px] tabular-nums text-[#858b90]"
                      title={policy.uploaded_at}
                    >
                      {formatDateTime(policy.updated_at)}
                    </time>
                  </td>

                  <td className="px-6 py-5 text-right align-middle">
                    <div className="flex justify-end">
                      <PolicyActions
                        policy={policy}
                        action={action}
                        onReprocess={onReprocess}
                        onDeactivate={onDeactivate}
                        layout="table"
                      />
                    </div>
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

/** Mobile policy items: title and activity first, metadata beneath. */
function PoliciesList({
  policies,
  rowActions,
  onReprocess,
  onDeactivate,
}: {
  policies: PolicyDocument[];
  rowActions: Record<string, RowActionState>;
  onReprocess: (policy: PolicyDocument) => void;
  onDeactivate: (policy: PolicyDocument) => void;
}) {
  return (
    <ul className={`${SURFACE_CLASS} divide-y divide-[#eef2f6] overflow-hidden`}>
      {policies.map((policy) => {
        const action = rowActions[policy.id] ?? { kind: "idle" as const };

        return (
          <li
            key={policy.id}
            className={`px-5 py-5 ${
              policy.is_active
                ? "transition-colors duration-200 ease-out hover:bg-[#f7fafc] motion-reduce:transition-colors"
                : "opacity-70"
            }`}
          >
            <div className="flex items-start gap-4">
              <PolicyGlyph />

              <div className="min-w-0 flex-1">
                <p className="text-[14.5px] font-medium leading-snug text-[#17191b]">
                  {policy.title}
                </p>
                <p
                  className="mt-1 truncate font-mono text-[12px] text-[#a8b1b8]"
                  title={policy.sha256}
                >
                  {policy.source_filename}
                </p>
              </div>
            </div>

            <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
              <ActivityPill isActive={policy.is_active} />

              <span className="text-[12.5px] tabular-nums text-[#858b90]">
                {policy.chunk_count} {policy.chunk_count === 1 ? "chunk" : "chunks"}
              </span>
            </div>

            <dl className="mt-4 grid grid-cols-2 gap-4">
              <MetaItem label="Uploaded" value={formatDateTime(policy.uploaded_at)} />
              <MetaItem label="Updated" value={formatDateTime(policy.updated_at)} />
            </dl>

            <div className="mt-4 border-t border-[#f4f7f9] pt-4">
              <PolicyActions
                policy={policy}
                action={action}
                onReprocess={onReprocess}
                onDeactivate={onDeactivate}
                layout="item"
              />
            </div>
          </li>
        );
      })}
    </ul>
  );
}

/** Row-shaped placeholders so the loading state matches the final layout. */
function PoliciesSkeleton() {
  return (
    <div className={`${SURFACE_CLASS} overflow-hidden`}>
      <div className="hidden items-center gap-6 border-b border-[#eef2f6] px-6 py-4 md:flex">
        <span className="h-2.5 w-16 rounded-full bg-[#f1f5f8]" />
        <span className="h-2.5 w-16 rounded-full bg-[#f1f5f8]" />
        <span className="h-2.5 w-20 rounded-full bg-[#f1f5f8]" />
        <span className="ml-auto h-2.5 w-24 rounded-full bg-[#f1f5f8]" />
      </div>

      {[0, 1, 2, 3, 4].map((row) => (
        <div
          key={row}
          className="flex items-center gap-4 border-b border-[#f4f7f9] px-5 py-5 last:border-b-0 md:gap-6 md:px-6"
          style={{ opacity: 1 - row * 0.12 }}
        >
          <span className="h-10 w-[30px] shrink-0 rounded-lg bg-[#f1f5f8]" />
          <span className="min-w-0 flex-1 space-y-2">
            <span className="block h-2.5 w-44 max-w-full rounded-full bg-[#f1f5f8]" />
            <span className="block h-2 w-28 rounded-full bg-[#f4f7f9]" />
          </span>
          <span className="hidden h-6 w-20 shrink-0 rounded-full bg-[#f4f7f9] sm:block" />
          <span className="hidden h-2.5 w-28 shrink-0 rounded-full bg-[#f4f7f9] lg:block" />
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

export default function PoliciesPage() {
  const [listState, setListState] = useState<ListState>({ kind: "loading" });
  const [uploadState, setUploadState] = useState<UploadState>({ kind: "idle" });
  const [title, setTitle] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [rowActions, setRowActions] = useState<Record<string, RowActionState>>({});
  const [actionError, setActionError] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [activity, setActivity] = useState<ActivityFilter>("all");
  const [sort, setSort] = useState<SortKey>("newest");

  const titleInputRef = useRef<HTMLInputElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const uploadPanelRef = useRef<HTMLDivElement>(null);

  const refresh = useCallback(async () => {
    try {
      const response = await listPolicies();
      setListState({ kind: "loaded", items: response.items });
    } catch (error: unknown) {
      setListState({
        kind: "error",
        message: error instanceof ApiError ? error.message : GENERIC_ERROR_MESSAGE,
      });
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  function setRowAction(id: string, next: RowActionState): void {
    setRowActions((previous) => ({ ...previous, [id]: next }));
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();

    const trimmedTitle = title.trim();
    if (!trimmedTitle) {
      setUploadState({ kind: "error", message: "A title is required." });
      return;
    }
    if (!file) {
      setUploadState({ kind: "error", message: "Choose a PDF to upload." });
      return;
    }

    setUploadState({ kind: "uploading" });
    let policyId: string;
    try {
      const created = await uploadPolicy(trimmedTitle, file);
      policyId = created.id;
    } catch (error: unknown) {
      setUploadState({ kind: "error", message: describeError(error, "upload") });
      return;
    }

    // Processing is a separate call so it can be retried on its own. Uploading
    // succeeded either way, so report the failure as a processing failure
    // rather than implying the upload was lost.
    setUploadState({ kind: "processing", title: trimmedTitle });
    try {
      const processed = await processPolicy(policyId);
      setUploadState({
        kind: "success",
        title: trimmedTitle,
        chunkCount: processed.chunk_count,
      });
      setTitle("");
      setFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      await refresh();
    } catch (error: unknown) {
      setUploadState({ kind: "error", message: describeError(error, "process") });
      await refresh();
    }
  }

  async function handleReprocess(policy: PolicyDocument): Promise<void> {
    setActionError(null);
    setRowAction(policy.id, { kind: "processing" });
    try {
      await processPolicy(policy.id);
      await refresh();
    } catch (error: unknown) {
      setActionError(describeError(error, "process"));
    } finally {
      setRowAction(policy.id, { kind: "idle" });
    }
  }

  async function handleDeactivate(policy: PolicyDocument): Promise<void> {
    const confirmed = window.confirm(
      `Deactivate "${policy.title}"?\n\nIt will stop appearing in policy search results. ` +
        "Its chunks and embeddings are kept, so nothing is deleted.",
    );
    if (!confirmed) {
      return;
    }

    setActionError(null);
    setRowAction(policy.id, { kind: "deactivating" });
    try {
      await deletePolicy(policy.id);
      await refresh();
    } catch (error: unknown) {
      setActionError(error instanceof ApiError ? error.message : GENERIC_ERROR_MESSAGE);
    } finally {
      setRowAction(policy.id, { kind: "idle" });
    }
  }

  /**
   * Overview counts are computed from the loaded policies, so they describe
   * exactly what the list below describes - never a partial page presented as
   * the whole collection.
   */
  const overview = useMemo(() => {
    const source = listState.kind === "loaded" ? listState.items : [];

    return {
      total: source.length,
      active: source.filter((policy) => policy.is_active).length,
      inactive: source.filter((policy) => !policy.is_active).length,
      chunks: source.reduce((sum, policy) => sum + policy.chunk_count, 0),
    };
  }, [listState]);

  const visible = useMemo(() => {
    const source = listState.kind === "loaded" ? listState.items : [];

    const filtered = source.filter((policy) => {
      if (activity === "active" && !policy.is_active) return false;
      if (activity === "inactive" && policy.is_active) return false;
      return matchesQuery(policy, query.trim());
    });

    return sortPolicies(filtered, sort);
  }, [listState, query, activity, sort]);

  const filtersActive = query.trim() !== "" || activity !== "all";

  function clearFilters(): void {
    setQuery("");
    setActivity("all");
  }

  /** The header action focuses the existing form; the workflow is unchanged. */
  function focusUploadForm(): void {
    uploadPanelRef.current?.scrollIntoView({
      behavior: "smooth",
      block: "center",
    });
    titleInputRef.current?.focus();
  }

  const uploadBusy =
    uploadState.kind === "uploading" || uploadState.kind === "processing";

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
            Policies
            <span aria-hidden="true" className="h-px w-8 bg-[#dfe6ec]" />
          </p>

          <h1 className="mt-5 font-serif text-[34px] leading-[1.06] tracking-[-0.035em] text-[#17191b] sm:text-[44px] lg:text-[52px]">
            The policy corpus
          </h1>

          <p className="mt-4 max-w-[52ch] text-[15px] leading-7 text-[#71777c]">
            {listState.kind === "loaded"
              ? `${overview.total} policy document${overview.total === 1 ? "" : "s"} indexed for retrieval, so invoice checks can cite the rules that apply.`
              : "Upload policy documents so invoice checks can cite the rules that apply."}
          </p>
        </div>

        <button
          type="button"
          onClick={focusUploadForm}
          className={`shrink-0 self-start lg:self-auto ${PRIMARY_ACTION_CLASS}`}
        >
          <span aria-hidden="true" className="text-[16px] leading-none">
            +
          </span>
          Add policy
        </button>
      </header>

      {/* -------------------------------------------------- upload */}
      <div ref={uploadPanelRef} className={`${SURFACE_CLASS} p-5 sm:p-7`}>
        <form onSubmit={handleSubmit} className="space-y-6">
          <div className="flex items-center gap-4">
            <h2 className="text-[10.5px] font-semibold uppercase tracking-[0.2em] text-[#8b949c]">
              Upload a policy
            </h2>

            <span
              aria-hidden="true"
              className="h-px flex-1 bg-gradient-to-r from-[#e2e9ef] to-transparent"
            />
          </div>

          <div className="grid gap-5 sm:grid-cols-2">
            <label className="block">
              <span className="mb-2 block text-[12.5px] font-medium text-[#3f474d]">
                Title
              </span>
              <input
                ref={titleInputRef}
                type="text"
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                placeholder="e.g. Travel and expense policy"
                disabled={uploadBusy}
                className={`h-12 w-full rounded-xl border border-[#e3eaf0] bg-[#fbfcfd] px-4 text-[14px] text-[#17191b] placeholder:text-[#9aa3aa] transition-colors duration-200 ease-out hover:border-[#d7e6f2] focus:border-[#bcd9f0] focus:bg-white focus:outline-none focus:ring-2 focus:ring-[#5b9bd5]/20 disabled:bg-[#f6f8fa] motion-reduce:transition-colors ${FOCUS_RING}`}
              />
            </label>

            <div className="block">
              <span className="mb-2 block text-[12.5px] font-medium text-[#3f474d]">
                PDF file
              </span>
              <div
                className={`flex h-12 w-full cursor-default items-center justify-start gap-3 rounded-xl border border-[#e3eaf0] bg-[#fbfcfd] px-3 text-[13px] text-[#5c666e] transition-colors duration-200 ease-out hover:border-[#d7e6f2] focus-within:border-[#bcd9f0] focus-within:bg-white focus-within:ring-2 focus-within:ring-[#5b9bd5]/20 motion-reduce:transition-colors`}
              >
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".pdf"
                  tabIndex={-1}
                  aria-hidden="true"
                  onChange={(event) => setFile(event.target.files?.[0] ?? null)}
                  disabled={uploadBusy}
                  className="hidden"
                />
                <button
                  type="button"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={uploadBusy}
                  className={`shrink-0 cursor-pointer rounded-lg border-0 bg-[#eef7fd] px-3 py-1.5 text-[12.5px] font-medium text-[#477cae] transition-colors duration-200 ease-out hover:bg-[#e2f0fa] disabled:cursor-not-allowed disabled:opacity-60 ${FOCUS_RING}`}
                >
                  Choose file
                </button>
                <span className="min-w-0 truncate">
                  {file ? file.name : "No file chosen"}
                </span>
              </div>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-4">
            <button
              type="submit"
              disabled={uploadBusy}
              className={PRIMARY_ACTION_CLASS}
            >
              {uploadState.kind === "uploading" ? "Uploading…" : "Upload and process"}
            </button>

            {uploadBusy && (
              <span className="flex items-center gap-2.5 text-[13.5px] text-[#5c666e]">
                <span aria-hidden="true" className={SPINNER_CLASS} />
                {uploadState.kind === "uploading"
                  ? "Uploading…"
                  : "Extracting and embedding…"}
              </span>
            )}
          </div>

          {uploadState.kind === "success" && (
            <p className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-[13.5px] text-emerald-900">
              Uploaded &ldquo;{uploadState.title}&rdquo; with {uploadState.chunkCount} chunk
              {uploadState.chunkCount === 1 ? "" : "s"}.
            </p>
          )}

          {uploadState.kind === "error" && (
            <p className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-[13.5px] text-red-900">
              {uploadState.message}
            </p>
          )}
        </form>
      </div>

      {actionError !== null && (
        <p className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-[13.5px] text-red-900">
          {actionError}
        </p>
      )}

      {/* -------------------------------------------------- loading */}
      {listState.kind === "loading" && (
        <>
          <div role="status" aria-live="polite" className="sr-only">
            Loading policies…
          </div>

          <div className="space-y-5">
            <div className="flex flex-wrap items-center gap-x-8 gap-y-5 border-y border-[#eef2f6] py-5">
              {[0, 1, 2, 3].map((cell) => (
                <span key={cell} className="space-y-2">
                  <span className="block h-2 w-20 rounded-full bg-[#f1f5f8]" />
                  <span className="block h-4 w-8 rounded-full bg-[#f4f7f9]" />
                </span>
              ))}
            </div>

            <PoliciesSkeleton />
          </div>
        </>
      )}

      {/* -------------------------------------------------- error */}
      {listState.kind === "error" && (
        <div className={`${SURFACE_CLASS} overflow-hidden`}>
          <div className="h-1 w-full bg-gradient-to-r from-[#d98b8b] via-[#f3e2e2] to-transparent" />

          <div className="px-6 py-12 sm:px-10 sm:py-14">
            <p className="text-[10.5px] font-semibold uppercase tracking-[0.22em] text-[#9aa3aa]">
              Unavailable
            </p>

            <h2 className="mt-4 font-serif text-[26px] leading-[1.15] tracking-[-0.03em] text-[#17191b] sm:text-[34px]">
              Could not load policies
            </h2>

            <p className="mt-4 max-w-md text-[14.5px] leading-7 text-[#71777c]">
              {listState.message}
            </p>

            <button
              type="button"
              onClick={() => void refresh()}
              className={`mt-8 ${SECONDARY_ACTION_CLASS}`}
            >
              Try again
            </button>
          </div>
        </div>
      )}

      {/* -------------------------------------------------- empty collection */}
      {listState.kind === "loaded" && listState.items.length === 0 && (
        <div className={`${SURFACE_CLASS} px-6 py-16 text-center sm:px-10 sm:py-20`}>
          <DocumentIllustration />

          <h2 className="mt-8 font-serif text-[26px] leading-tight tracking-[-0.025em] text-[#17191b] sm:text-[32px]">
            No policies yet
          </h2>

          <p className="mx-auto mt-3 max-w-sm text-[14.5px] leading-7 text-[#71777c]">
            Upload a policy PDF above to enable retrieval. Its text is extracted,
            chunked, and embedded so invoice checks can cite it.
          </p>
        </div>
      )}

      {/* -------------------------------------------------- collection */}
      {listState.kind === "loaded" && listState.items.length > 0 && (
        <div className="space-y-6">
          {/* Compact overview: an editorial segmented strip, not dashboard
              cards. The activity cells double as the filter, so the counts and
              the control cannot disagree. */}
          <div
            role="group"
            aria-label="Policy overview"
            className="-mx-1 flex flex-wrap items-center gap-x-8 gap-y-5 border-y border-[#eef2f6] px-1 py-5"
          >
            <div className="flex flex-col items-start px-2">
              <span className="text-[11px] font-medium uppercase tracking-[0.16em] text-[#a8b1b8]">
                Total
              </span>
              <span className="mt-1.5 pl-0.5 font-serif text-[26px] leading-none tabular-nums text-[#17191b]">
                {overview.total}
              </span>
            </div>

            {ACTIVITY_FILTERS.map((filter) => {
              const active = activity === filter.key;
              const count =
                filter.key === "all"
                  ? overview.total
                  : filter.key === "active"
                    ? overview.active
                    : overview.inactive;

              return (
                <button
                  key={filter.key}
                  type="button"
                  aria-pressed={active}
                  onClick={() => setActivity(filter.key)}
                  className={`group flex flex-col items-start rounded-lg px-2 py-1 text-left transition-colors duration-200 ease-out hover:bg-[#f7fafc] motion-reduce:transition-colors ${FOCUS_RING}`}
                >
                  <span className="flex items-center gap-2">
                    <span
                      aria-hidden="true"
                      className={`h-1.5 w-1.5 shrink-0 rounded-full transition-transform duration-200 ease-out group-hover:scale-125 motion-reduce:transform-none ${filter.dot}`}
                    />
                    <span
                      className={`text-[11px] font-medium uppercase tracking-[0.16em] transition-colors duration-200 ${
                        active ? "text-[#477cae]" : "text-[#a8b1b8]"
                      }`}
                    >
                      {filter.label}
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

            <div className="flex flex-col items-start px-2">
              <span className="text-[11px] font-medium uppercase tracking-[0.16em] text-[#a8b1b8]">
                Chunks indexed
              </span>
              <span className="mt-1.5 pl-0.5 font-serif text-[26px] leading-none tabular-nums text-[#8b949c]">
                {overview.chunks}
              </span>
            </div>
          </div>

          {/* Search and filter workspace */}
          <div className={`${SURFACE_CLASS} p-4 sm:p-5`}>
            <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between lg:gap-6">
              <div className="relative lg:max-w-md lg:flex-1">
                <label htmlFor="policy-search" className="sr-only">
                  Search policies by title or source filename
                </label>

                <span
                  aria-hidden="true"
                  className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-[#a8b1b8] transition-colors duration-200 peer-focus:text-[#5b9bd5]"
                >
                  <SearchIcon />
                </span>

                <input
                  id="policy-search"
                  type="search"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Search policies by title or filename"
                  className={`peer h-12 w-full rounded-xl border border-[#e3eaf0] bg-[#fbfcfd] pl-11 pr-4 text-[14px] text-[#17191b] placeholder:text-[#9aa3aa] transition-colors duration-200 ease-out hover:border-[#d7e6f2] focus:border-[#bcd9f0] focus:bg-white focus:outline-none focus:ring-2 focus:ring-[#5b9bd5]/20 motion-reduce:transition-colors ${FOCUS_RING}`}
                />
              </div>

              <div className="flex items-center gap-3">
                <label
                  htmlFor="policy-sort"
                  className="text-[11px] font-semibold uppercase tracking-[0.16em] text-[#a8b1b8]"
                >
                  Sort
                </label>

                <select
                  id="policy-sort"
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
                  {visible.length} of {overview.total} shown
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
                Try a different search term or activity filter.
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
                <PoliciesTable
                  policies={visible}
                  rowActions={rowActions}
                  onReprocess={handleReprocess}
                  onDeactivate={handleDeactivate}
                />
              </div>

              <div className="md:hidden">
                <PoliciesList
                  policies={visible}
                  rowActions={rowActions}
                  onReprocess={handleReprocess}
                  onDeactivate={handleDeactivate}
                />
              </div>
            </>
          )}

          {/* Collection controls */}
          <div className="flex flex-col gap-3 border-t border-[#eef2f6] pt-5 text-[12.5px] tabular-nums text-[#858b90] sm:flex-row sm:items-center sm:justify-between">
            <p>
              Showing {visible.length} of {overview.total} loaded{" "}
              {overview.total === 1 ? "policy" : "policies"}
            </p>

            <p>{overview.chunks} chunks indexed</p>
          </div>
        </div>
      )}
    </section>
  );
}