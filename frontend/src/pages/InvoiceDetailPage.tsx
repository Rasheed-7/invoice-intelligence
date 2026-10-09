import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import {
  ApiError,
  analyzeInvoice,
  assessRisk,
  checkAnomalies,
  checkDuplicates,
  deleteInvoice,
  getAnomalies,
  getDuplicates,
  getInvoice,
  getPoMatch,
  getPolicyEvidence,
  getRisk,
  getValidation,
  isAnomalyNeverRanReport,
  isDuplicateNeverRanReport,
  isNeverRanReport,
  isRiskNeverRan,
  listReviews,
  matchPurchaseOrder,
  NOT_RUN_RULE_ID,
  resolveReview,
  runPolicyCheck,
  validateInvoice,
} from "../api";
import ErrorBoundary from "../components/ErrorBoundary";
import ConfirmDialog from "../components/ConfirmDialog";
import ReviewSection from "../components/ReviewSection";
import type { AnomalyReport, FeatureContribution } from "../types/anomaly";
import type {
  DuplicateMatch,
  DuplicateReport,
  DuplicateSignal,
  DuplicateVerdict,
} from "../types/duplicate";
import type { Invoice, InvoiceStatus } from "../types/invoice";
import type {
  FieldMismatch,
  LineMatch,
  MatchOutcome,
  PurchaseOrderMatchReport,
} from "../types/po_match";
import type { PolicyEvidence, PolicyFinding } from "../types/policy";
import type { ReviewCase, ReviewDecision } from "../types/review";
import { REVIEWER_ID } from "../config/reviewer";
import type {
  RecommendedAction,
  RiskAssessment,
  RiskContributor,
  RiskLevel,
  RiskSeverity,
} from "../types/risk";
import type {
  ValidationCheck,
  ValidationStatus,
  ValidationSummary,
} from "../types/validation";
import {
  EMPTY_VALUE,
  GENERIC_ERROR_MESSAGE,
  displayOrDash,
  formatBytes,
  formatDate,
  formatDateTime,
  formatMoney,
  formatStatusLabel,
} from "./InvoiceListPage";

/**
 * Visual language shared with the app shell: off-white workspace, white
 * surfaces, hairline borders, ink text, and blue reserved for actions.
 */
const HEADING_CLASS =
  "mb-3 text-[10.5px] font-semibold uppercase tracking-[0.2em] text-[#8b949c]";

const PANEL_CLASS =
  "rounded-2xl border border-[#e7edf2] bg-white p-5 shadow-[0_1px_2px_rgba(23,25,27,0.03),0_18px_44px_-34px_rgba(23,52,74,0.5)] sm:p-6";

const CARD_CLASS = PANEL_CLASS;

const FOCUS_RING =
  "focus:outline-none focus-visible:ring-2 focus-visible:ring-[#5b9bd5]/55 focus-visible:ring-offset-2";

const PRIMARY_ACTION_CLASS = `inline-flex items-center justify-center gap-2 rounded-full bg-[#5b9bd5] px-4 py-2.5 text-sm font-medium text-white shadow-[0_10px_22px_-12px_rgba(91,155,213,0.85)] transition-all duration-200 ease-out hover:-translate-y-0.5 hover:bg-[#477cae] ${FOCUS_RING}`;

const SECONDARY_ACTION_CLASS = `inline-flex items-center justify-center rounded-full border border-[#e3e9ee] bg-white px-4 py-2.5 text-sm font-medium text-[#3f474d] transition-all duration-200 ease-out hover:-translate-y-0.5 hover:border-[#dcebfb] hover:bg-[#f7fbfe] hover:text-[#477cae] ${FOCUS_RING}`;

const RETRY_ACTION_CLASS = `inline-flex items-center justify-center rounded-full border border-red-200 bg-white px-4 py-2 text-sm font-medium text-red-800 transition-colors duration-200 hover:bg-red-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-400 focus-visible:ring-offset-2`;

const SPINNER_CLASS =
  "h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-[#dcebfb] border-t-[#5b9bd5]";

/** Muted destructive action — red only on hover, never louder than primary actions. */
const DELETE_ACTION_CLASS = `inline-flex items-center gap-2 rounded-full border border-red-100 bg-white px-4 py-2.5 text-sm font-medium text-red-600 transition-all duration-200 ease-out hover:-translate-y-0.5 hover:border-red-200 hover:bg-red-50 hover:text-red-700 disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:translate-y-0 motion-reduce:transform-none motion-reduce:transition-colors ${FOCUS_RING}`;

/** Small quiet label above a document value. */
function MetaItem({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="min-w-0">
      <dt className="text-[10.5px] font-semibold uppercase tracking-[0.18em] text-[#9aa3aa]">
        {label}
      </dt>
      <dd
        className={`mt-1.5 break-words text-[14px] text-[#282d31] ${
          mono ? "font-mono tabular-nums" : "font-medium"
        }`}
      >
        {value}
      </dd>
    </div>
  );
}

/** Single total line inside the document totals block. */
function TotalRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-6">
      <dt className="text-[13.5px] text-[#71777c]">{label}</dt>
      <dd className="text-[15px] font-medium tabular-nums text-[#282d31]">
        {value}
      </dd>
    </div>
  );
}

type DetailState =
  | { kind: "loading" }
  | { kind: "loaded"; data: Invoice }
  | { kind: "error"; message: string };

type AnalysisState =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "error"; message: string };

type ValidationActionState =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "error"; message: string };

type ValidationSectionState =
  | { kind: "loading" }
  | { kind: "loaded"; data: ValidationSummary }
  | { kind: "error"; message: string };

type PoMatchActionState =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "error"; message: string };

type PoMatchSectionState =
  | { kind: "loading" }
  | { kind: "loaded"; data: PurchaseOrderMatchReport }
  | { kind: "error"; message: string };

type PolicyCheckActionState =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "error"; message: string };

type PolicyEvidenceSectionState =
  | { kind: "loading" }
  | { kind: "loaded"; data: PolicyFinding }
  | { kind: "error"; message: string };

type DuplicateCheckActionState =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "error"; message: string };

type DuplicateSectionState =
  | { kind: "loading" }
  | { kind: "loaded"; data: DuplicateReport }
  | { kind: "error"; message: string };

type AnomalyCheckActionState =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "error"; message: string };

type AnomalySectionState =
  | { kind: "loading" }
  | { kind: "loaded"; data: AnomalyReport }
  | { kind: "error"; message: string };

type RiskActionState =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "error"; message: string };

type RiskSectionState =
  | { kind: "loading" }
  | { kind: "loaded"; data: RiskAssessment }
  | { kind: "error"; message: string };

type ReviewSubmitState =
  | { kind: "idle" }
  | { kind: "submitting" }
  | { kind: "error"; message: string };

const REVIEW_LIST_LIMIT = 200;

const OUTCOME_BADGE_CLASSES: Record<ValidationStatus, string> = {
  passed: "border-emerald-200 bg-emerald-50 text-emerald-700",
  failed: "border-red-200 bg-red-50 text-red-700",
  warning: "border-amber-200 bg-amber-50 text-amber-700",
  skipped: "border-slate-200 bg-slate-100 text-slate-600",
};

function ValidationOutcomeBadge({ status }: { status: ValidationStatus }) {
  return (
    <span
      className={`inline-flex items-center whitespace-nowrap rounded-full border px-2 py-0.5 text-xs font-medium ${OUTCOME_BADGE_CLASSES[status]}`}
    >
      {formatStatusLabel(status)}
    </span>
  );
}

function humanizeCheckName(checkName: string): string {
  return formatStatusLabel(checkName.replace(/_/g, " "));
}

function asDisplayString(value: unknown): string | null {
  if (typeof value === "string" && value !== "") return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return null;
}

function CheckEvidence({ check }: { check: ValidationCheck }) {
  if (check.status !== "failed") return null;

  const expected = asDisplayString(check.details["expected"]);
  if (expected === null) return null;

  const actual = asDisplayString(check.details["actual"]);
  const delta = asDisplayString(check.details["delta"]);

  return (
    <p className="mt-1 text-xs text-[#71777c]">
      Expected {expected}
      {actual !== null ? `, got ${actual}` : ""}
      {delta !== null ? ` (delta ${delta})` : ""}
    </p>
  );
}

function ValidationSection({
  state,
  onRunValidation,
}: {
  state: ValidationSectionState;
  onRunValidation: () => void;
}) {
  if (state.kind === "loading") {
    return (
      <div className={CARD_CLASS}>
        <h2 className={HEADING_CLASS}>Validation</h2>
        <p className="text-sm text-[#71777c]">
          Loading validation results…
        </p>
      </div>
    );
  }

  if (state.kind === "error") {
    return (
      <div className={CARD_CLASS}>
        <h2 className={HEADING_CLASS}>Validation</h2>
        <p className="text-sm text-red-600">{state.message}</p>
      </div>
    );
  }

  const { data } = state;
  const checks = data.checks ?? [];
  const counts = {
    passed: data.counts?.passed ?? 0,
    failed: data.counts?.failed ?? 0,
    warning: data.counts?.warning ?? 0,
    skipped: data.counts?.skipped ?? 0,
  };

  return (
    <div className={CARD_CLASS}>
      <h2 className={HEADING_CLASS}>Validation</h2>

      {checks.length === 0 ? (
        <div className="rounded-lg border border-[#DCEBFB] bg-[#EEF7FD] px-4 py-3">
          <p className="text-sm text-slate-700">
            Validation has not run yet.{" "}
            <button
              onClick={onRunValidation}
              className="font-medium text-[#4F88BD] hover:text-[#477CAE]"
            >
              Run validation
            </button>
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          <ValidationBanner status={data.overall_status} />

          <p className="text-sm text-[#5c666e]">
            {counts.passed} passed · {counts.failed} failed ·{" "}
            {counts.warning} warnings · {counts.skipped} skipped
          </p>

          <table className="w-full text-left text-sm">
            <thead className="text-slate-500">
              <tr>
                <th className="py-2 font-medium">Check</th>
                <th className="py-2 font-medium">Outcome</th>
                <th className="py-2 font-medium">Details</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {checks.map((check) => (
                <tr key={check.check_name}>
                  <td className="py-3 font-medium text-slate-900">
                    {humanizeCheckName(check.check_name)}
                  </td>
                  <td className="py-3">
                    <ValidationOutcomeBadge status={check.status} />
                  </td>
                  <td className="py-3 text-slate-600">
                    {check.message}
                    <CheckEvidence check={check} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function ValidationBanner({ status }: { status: ValidationStatus }) {
  if (status === "failed") {
    return (
      <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3">
        <p className="text-sm font-medium text-red-900">
          Validation failed — this invoice needs review.
        </p>
      </div>
    );
  }

  if (status === "warning") {
    return (
      <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3">
        <p className="text-sm font-medium text-amber-900">
          Validation completed with warnings — please review the details below.
        </p>
      </div>
    );
  }

  if (status === "passed") {
    return (
      <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3">
        <p className="text-sm font-medium text-emerald-900">
          All checks passed.
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-[#DCEBFB] bg-[#EEF7FD] px-4 py-3">
      <p className="text-sm font-medium text-slate-700">
        Validation skipped — there is nothing to check yet.
      </p>
    </div>
  );
}

const MATCH_OUTCOME_BADGE_CLASSES: Record<MatchOutcome, string> = {
  match: "border-emerald-200 bg-emerald-50 text-emerald-700",
  partial_match: "border-amber-200 bg-amber-50 text-amber-700",
  mismatch: "border-red-200 bg-red-50 text-red-700",
  skipped: "border-slate-200 bg-slate-100 text-slate-600",
};

function MatchOutcomeBadge({ outcome }: { outcome: MatchOutcome }) {
  return (
    <span
      className={`inline-flex items-center whitespace-nowrap rounded-full border px-2 py-0.5 text-xs font-medium ${MATCH_OUTCOME_BADGE_CLASSES[outcome]}`}
    >
      {formatStatusLabel(outcome.replace(/_/g, " "))}
    </span>
  );
}

function PoMatchBanner({ outcome }: { outcome: MatchOutcome }) {
  if (outcome === "match") {
    return (
      <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3">
        <p className="text-sm font-medium text-emerald-900">
          Purchase order matched.
        </p>
      </div>
    );
  }

  if (outcome === "partial_match") {
    return (
      <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3">
        <p className="text-sm font-medium text-amber-900">
          Partial match — review recommended.
        </p>
      </div>
    );
  }

  if (outcome === "mismatch") {
    return (
      <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3">
        <p className="text-sm font-medium text-red-900">
          Purchase order mismatch.
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-[#DCEBFB] bg-[#EEF7FD] px-4 py-3">
      <p className="text-sm font-medium text-slate-700">
        No purchase order referenced.
      </p>
    </div>
  );
}

function MismatchDetail({ mismatch }: { mismatch: FieldMismatch }) {
  const poValue = displayOrDash(mismatch.po_value);
  const invoiceValue = displayOrDash(mismatch.invoice_value);
  const difference = displayOrDash(mismatch.difference);

  return (
    <li className="text-xs text-slate-600">
      <span className="font-medium text-slate-800">{mismatch.field}</span>: PO{" "}
      {poValue} → Invoice {invoiceValue}
      {mismatch.difference !== null
        ? ` (difference ${difference}, ${mismatch.severity})`
        : ""}
    </li>
  );
}

function pluralize(
  count: number,
  singular: string,
  plural?: string
): string {
  return count === 1 ? singular : plural ?? `${singular}s`;
}

/** True when a YYYY-MM-DD due date is before today (local calendar day). */
function isOverdueDetail(dueDate: string | null | undefined): boolean {
  if (!dueDate) return false;
  const day = dueDate.slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return false;
  const today = new Date();
  const todayDay = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
  return day < todayDay;
}

/** Copies the full invoice id; the truncated id keeps `title` as fallback. */
function CopyIdButton({ id }: { id: string }) {
  const [copied, setCopied] = useState(false);

  async function copy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(id);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard unavailable — the full id is still in the `title` tooltip.
    }
  }

  return (
    <button
      type="button"
      onClick={copy}
      title="Copy full invoice ID"
      aria-label="Copy full invoice ID"
      className={`rounded-full px-2 py-0.5 font-mono text-[10px] font-medium normal-case tracking-normal transition-colors duration-200 motion-reduce:transition-colors ${FOCUS_RING} ${
        copied
          ? "bg-emerald-50 text-emerald-700"
          : "text-[#477cae] hover:bg-[#eef7fd]"
      }`}
    >
      {copied ? "Copied" : "Copy ID"}
    </button>
  );
}

function LineMatchDetails({ line }: { line: LineMatch }) {
  if (line.mismatches.length === 0) {
    return (
      <span className="text-xs text-[#71777c]">All fields match.</span>
    );
  }

  return (
    <ul className="space-y-1">
      {line.mismatches.map((mismatch, index) => (
        <MismatchDetail
          key={`${mismatch.field}-${index}`}
          mismatch={mismatch}
        />
      ))}
    </ul>
  );
}

function UnmatchedIds({
  label,
  ids,
}: {
  label: string;
  ids: string[];
}) {
  if (ids.length === 0) return null;

  return (
    <div>
      <p className="text-xs font-medium text-slate-700">
        {label} ({ids.length} {pluralize(ids.length, "line")})
      </p>

      <ul className="mt-1 flex flex-wrap gap-1.5">
        {ids.map((id) => (
          <li
            key={id}
            title={id}
            className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs text-slate-600"
          >
            {truncateId(id)}
          </li>
        ))}
      </ul>
    </div>
  );
}

function PoMatchCounts({
  report,
}: {
  report: PurchaseOrderMatchReport;
}) {
  const lineMatches = report.line_matches ?? [];

  let matched = 0;
  let mismatched = 0;

  for (const line of lineMatches) {
    if (line.outcome === "match") {
      matched += 1;
    } else if (line.outcome === "mismatch") {
      mismatched += 1;
    }
  }

  const invoiceOnly = (report.unmatched_invoice_items ?? []).length;
  const poOnly = (report.unmatched_po_items ?? []).length;

  return (
    <p className="text-sm text-[#5c666e]">
      {matched} {pluralize(matched, "line")} matched · {mismatched}{" "}
      {pluralize(mismatched, "mismatch", "mismatches")} · {invoiceOnly}{" "}
      {pluralize(invoiceOnly, "invoice-only line")} · {poOnly}{" "}
      {pluralize(poOnly, "PO-only line")}
    </p>
  );
}

function PoMatchSection({
  state,
}: {
  state: PoMatchSectionState;
}) {
  if (state.kind === "loading") {
    return (
      <div className={CARD_CLASS}>
        <h2 className={HEADING_CLASS}>Purchase Order match</h2>
        <p className="text-sm text-[#71777c]">
          Loading PO match results…
        </p>
      </div>
    );
  }

  if (state.kind === "error") {
    return (
      <div className={CARD_CLASS}>
        <h2 className={HEADING_CLASS}>Purchase Order match</h2>
        <p className="text-sm text-red-600">{state.message}</p>
      </div>
    );
  }

  const { data } = state;

  if (isNeverRanReport(data)) {
    return (
      <div className={CARD_CLASS}>
        <h2 className={HEADING_CLASS}>Purchase Order match</h2>

        <div className="rounded-lg border border-[#DCEBFB] bg-[#EEF7FD] px-4 py-3">
          <p className="text-sm text-slate-700">
            PO matching has not run yet. Click &apos;Run PO match&apos; above.
          </p>
        </div>
      </div>
    );
  }

  const notes = data.notes ?? [];
  const lineMatches = data.line_matches ?? [];
  const unmatchedInvoiceItems = data.unmatched_invoice_items ?? [];
  const unmatchedPoItems = data.unmatched_po_items ?? [];

  return (
    <div className={CARD_CLASS}>
      <h2 className={HEADING_CLASS}>Purchase Order match</h2>

      <div className="space-y-4">
        <PoMatchBanner outcome={data.overall_outcome} />

        <p className="text-sm text-[#5c666e]">
          {data.po_reference
            ? `PO: ${data.po_reference}`
            : "No PO referenced."}
        </p>

        {notes.length > 0 && (
          <ul className="list-disc space-y-1 pl-5 text-sm text-[#5c666e]">
            {notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        )}

        <PoMatchCounts report={data} />

        {lineMatches.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-slate-100">
                  <th className="w-12 py-2 text-left text-[10.5px] font-semibold uppercase tracking-[0.16em] text-[#a8b1b8]">
                    #
                  </th>
                  <th className="py-2 text-left text-[10.5px] font-semibold uppercase tracking-[0.16em] text-[#a8b1b8]">
                    Outcome
                  </th>
                  <th className="py-2 text-left text-[10.5px] font-semibold uppercase tracking-[0.16em] text-[#a8b1b8]">
                    Details
                  </th>
                </tr>
              </thead>

              <tbody className="divide-y divide-slate-100">
                {lineMatches.map((line, index) => (
                  <tr
                    key={`${line.invoice_item_id ?? "none"}-${line.po_item_id ?? "none"}-${index}`}
                  >
                    <td className="py-3 text-slate-500">{index + 1}</td>

                    <td className="py-3">
                      <MatchOutcomeBadge outcome={line.outcome} />
                    </td>

                    <td className="py-3 text-slate-600">
                      <LineMatchDetails line={line} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <UnmatchedIds
          label="Invoice lines not on PO"
          ids={unmatchedInvoiceItems}
        />

        <UnmatchedIds
          label="PO lines not on invoice"
          ids={unmatchedPoItems}
        />
      </div>
    </div>
  );
}

function EvidenceCard({
  evidence,
}: {
  evidence: PolicyEvidence;
}) {
  const percent = `${(evidence.similarity * 100).toFixed(1)}%`;

  return (
    <div className="rounded-2xl border border-[#DCEBFB] bg-white p-4 shadow-[0_8px_28px_rgba(55,105,150,0.045)]">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-medium text-slate-900">
            {evidence.policy_title}
          </p>
          <p className="text-xs text-[#71777c]">
            page {evidence.page_number}
          </p>
        </div>

        <span
          title={`Cosine similarity ${percent}`}
          className="inline-flex shrink-0 items-center whitespace-nowrap rounded-full border border-[#DCEBFB] bg-[#EEF7FD] px-2 py-0.5 text-xs font-medium tabular-nums text-[#4F88BD]"
        >
          {percent}
        </span>
      </div>

      <blockquote className="mt-3 border-l-2 border-slate-200 pl-3 font-mono text-xs leading-relaxed text-slate-700">
        {evidence.text}
      </blockquote>

      <p
        className="mt-3 truncate text-xs text-slate-400"
        title={evidence.chunk_id}
      >
        Chunk {evidence.chunk_id.slice(0, 8)}… · retrieved with query &quot;
        {evidence.retrieval_query}&quot;
      </p>
    </div>
  );
}

function PolicyEvidenceSection({
  state,
}: {
  state: PolicyEvidenceSectionState;
}) {
  if (state.kind === "loading") {
    return (
      <div className={CARD_CLASS}>
        <h2 className={HEADING_CLASS}>Policy evidence</h2>
        <p className="text-sm text-[#71777c]">
          Loading policy evidence…
        </p>
      </div>
    );
  }

  if (state.kind === "error") {
    return (
      <div className={CARD_CLASS}>
        <h2 className={HEADING_CLASS}>Policy evidence</h2>
        <p className="text-sm text-red-600">{state.message}</p>
      </div>
    );
  }

  const { data } = state;

  if (data.rule_id === NOT_RUN_RULE_ID) {
    return (
      <div className={CARD_CLASS}>
        <h2 className={HEADING_CLASS}>Policy evidence</h2>

        <div className="rounded-lg border border-[#DCEBFB] bg-[#EEF7FD] px-4 py-3">
          <p className="text-sm text-slate-700">
            Policy analysis has not been run. Click &apos;Run policy
            check&apos; above.
          </p>
        </div>
      </div>
    );
  }

  const passed = data.status === "passed";
  const evidenceItems = data.evidence ?? [];

  return (
    <div className={CARD_CLASS}>
      <h2 className={HEADING_CLASS}>Policy evidence</h2>

      <div className="space-y-4">
        {passed ? (
          <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3">
            <p className="text-sm font-medium text-emerald-900">
              Policy evidence retrieved.
            </p>
          </div>
        ) : (
          <div className="rounded-lg border border-[#DCEBFB] bg-[#EEF7FD] px-4 py-3">
            <p className="text-sm font-medium text-slate-700">
              No applicable policy found.
            </p>
          </div>
        )}

        <p className="text-sm text-[#5c666e]">{data.message}</p>

        {evidenceItems.length === 0 ? (
          <p className="text-sm text-[#71777c]">
            No chunks were retrieved
          </p>
        ) : (
          <div className="space-y-3">
            {evidenceItems.map((evidence, index) => (
              <EvidenceCard
                key={`${evidence.chunk_id}-${index}`}
                evidence={evidence}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function DuplicateBanner({
  verdict,
}: {
  verdict: DuplicateVerdict;
}) {
  if (verdict === "duplicate") {
    return (
      <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3">
        <p className="text-sm font-medium text-red-900">
          Duplicate detected.
        </p>
      </div>
    );
  }

  if (verdict === "suspected_duplicate") {
    return (
      <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3">
        <p className="text-sm font-medium text-amber-900">
          Possible duplicate — review recommended.
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3">
      <p className="text-sm font-medium text-emerald-900">
        No duplicate found.
      </p>
    </div>
  );
}

const DUPLICATE_SIGNAL_LABELS: Record<
  DuplicateSignal,
  string
> = {
  identical_file: "Identical file",
  same_vendor_invoice_number: "Same vendor + invoice number",
  same_vendor_amount: "Same vendor + amount",
  same_po_amount: "Same PO + amount",
  line_item_overlap: "Line item overlap",
};

const DUPLICATE_SIGNAL_CHIP_CLASSES: Record<
  DuplicateSignal,
  string
> = {
  identical_file: "border-red-200 bg-red-50 text-red-700",
  same_vendor_invoice_number:
    "border-red-200 bg-red-50 text-red-700",
  same_po_amount:
    "border-amber-200 bg-amber-50 text-amber-800",
  same_vendor_amount:
    "border-amber-200 bg-amber-50 text-amber-800",
  line_item_overlap:
    "border-slate-200 bg-slate-100 text-slate-600",
};

function DuplicateSignalChip({
  signal,
}: {
  signal: DuplicateSignal;
}) {
  return (
    <span
      className={`inline-flex items-center whitespace-nowrap rounded-full border px-2 py-0.5 text-xs font-medium ${DUPLICATE_SIGNAL_CHIP_CLASSES[signal]}`}
    >
      {DUPLICATE_SIGNAL_LABELS[signal]}
    </span>
  );
}

function formatScorePercent(score: number | null | undefined): string | null {
  if (score === null || score === undefined || !Number.isFinite(score)) return null;
  // Backend contracts differ: duplicates are 0..1, anomaly may arrive 0..100.
  const normalized = score > 1 ? score / 100 : score;
  const clamped = Math.min(1, Math.max(0, normalized));
  return `${(clamped * 100).toFixed(1)}%`;
}

function DuplicateMatchCard({
  match,
}: {
  match: DuplicateMatch;
}) {
  const percent = formatScorePercent(match.score) ?? "—";

  return (
    <div className="rounded-2xl border border-[#DCEBFB] bg-white p-4 shadow-[0_8px_28px_rgba(55,105,150,0.045)]">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <Link
            to={`/invoices/${match.matching_invoice_id}`}
            title={match.matching_invoice_id}
            className="block truncate font-mono text-sm font-medium text-[#4F88BD] hover:text-[#477CAE]"
          >
            {truncateId(match.matching_invoice_id)}
          </Link>
        </div>

        <span
          title={`Duplicate score ${percent}`}
          className="inline-flex shrink-0 items-center whitespace-nowrap rounded-full border border-slate-200 bg-slate-100 px-2 py-0.5 text-xs font-medium tabular-nums text-slate-700"
        >
          {percent}
        </span>
      </div>

      {match.signals.length > 0 && (
        <ul className="mt-3 flex flex-wrap gap-1.5">
          {match.signals.map((signal) => (
            <li key={signal}>
              <DuplicateSignalChip signal={signal} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function DuplicateSection({
  state,
}: {
  state: DuplicateSectionState;
}) {
  if (state.kind === "loading") {
    return (
      <div className={CARD_CLASS}>
        <h2 className={HEADING_CLASS}>Duplicate check</h2>
        <p className="text-sm text-[#71777c]">
          Loading duplicate results…
        </p>
      </div>
    );
  }

  if (state.kind === "error") {
    return (
      <div className={CARD_CLASS}>
        <h2 className={HEADING_CLASS}>Duplicate check</h2>
        <p className="text-sm text-red-600">{state.message}</p>
      </div>
    );
  }

  const { data } = state;

  if (isDuplicateNeverRanReport(data)) {
    return (
      <div className={CARD_CLASS}>
        <h2 className={HEADING_CLASS}>Duplicate check</h2>

        <div className="rounded-lg border border-[#DCEBFB] bg-[#EEF7FD] px-4 py-3">
          <p className="text-sm text-slate-700">
            Duplicate check has not been run. Click &apos;Run duplicate
            check&apos; above.
          </p>
        </div>
      </div>
    );
  }

  // `top_match` is the highest-scoring entry; remaining matches are everything
  // else by id — never `slice(1)`, which breaks if the backend reorders.
  const topId = data.top_match?.matching_invoice_id ?? null;
  const otherMatches = (data.all_matches ?? []).filter(
    (match) => match.matching_invoice_id !== topId
  );
  const notes = data.notes ?? [];

  return (
    <div className={CARD_CLASS}>
      <h2 className={HEADING_CLASS}>Duplicate check</h2>

      <div className="space-y-4">
        <DuplicateBanner verdict={data.verdict} />

        {notes.length > 0 && (
          <ul className="list-disc space-y-1 pl-5 text-sm text-[#5c666e]">
            {notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        )}

        {data.top_match && (
          <DuplicateMatchCard match={data.top_match} />
        )}

        {otherMatches.length > 0 && (
          <details className="rounded-lg border border-[#DCEBFB] bg-[#EEF7FD] px-4 py-3">
            <summary className="cursor-pointer text-sm font-medium text-slate-700">
              {otherMatches.length} other{" "}
              {pluralize(
                otherMatches.length,
                "match",
                "matches"
              )}
            </summary>

            <div className="mt-3 space-y-3">
              {otherMatches.map((match) => (
                <DuplicateMatchCard
                  key={match.matching_invoice_id}
                  match={match}
                />
              ))}
            </div>
          </details>
        )}
      </div>
    </div>
  );
}

function AnomalyBanner({
  verdict,
}: {
  verdict: AnomalyReport["verdict"];
}) {
  if (verdict === "strong_anomaly") {
    return (
      <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3">
        <p className="text-sm font-medium text-red-900">
          Strong anomaly detected.
        </p>
      </div>
    );
  }

  if (verdict === "mild_anomaly") {
    return (
      <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3">
        <p className="text-sm font-medium text-amber-900">
          Mild anomaly detected.
        </p>
      </div>
    );
  }

  if (verdict === "normal") {
    return (
      <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3">
        <p className="text-sm font-medium text-emerald-900">
          No anomaly detected.
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-[#DCEBFB] bg-[#EEF7FD] px-4 py-3">
      <p className="text-sm font-medium text-slate-700">
        Insufficient vendor history to score.
      </p>
    </div>
  );
}

function formatAnomalyMethod(
  method: AnomalyReport["method"]
): string {
  if (method === "statistical_baseline") return "Statistical baseline";
  if (method === "isolation_forest") return "Isolation forest";
  return "Not run";
}

const CONTRIBUTION_SEVERITY_CHIP_CLASSES: Record<
  FeatureContribution["severity"],
  string
> = {
  high: "border-red-200 bg-red-50 text-red-700",
  medium: "border-amber-200 bg-amber-50 text-amber-800",
  low: "border-slate-200 bg-slate-100 text-slate-600",
};

function ContributionRow({
  contribution,
}: {
  contribution: FeatureContribution;
}) {
  if (contribution.z_score === null) {
    return (
      <li className="rounded-lg border border-[#DCEBFB] bg-white px-4 py-3 shadow-[0_8px_28px_rgba(55,105,150,0.045)]">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <p className="font-medium text-slate-900">
            {formatStatusLabel(
              contribution.feature.replace(/_/g, " ")
            )}
          </p>

          <span
            className={`inline-flex shrink-0 items-center whitespace-nowrap rounded-full border px-2 py-0.5 text-xs font-medium ${CONTRIBUTION_SEVERITY_CHIP_CLASSES[contribution.severity]}`}
          >
            {formatStatusLabel(contribution.severity)}
          </span>
        </div>

        <p className="mt-1 text-sm text-[#5c666e]">
          {contribution.note}
        </p>
      </li>
    );
  }

  const baseline =
    contribution.historical_mean !== null &&
    contribution.historical_std !== null
      ? `${contribution.historical_mean} ± ${contribution.historical_std}`
      : null;

  return (
    <li className="flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded-lg border border-[#DCEBFB] bg-white px-4 py-3 text-sm shadow-[0_8px_28px_rgba(55,105,150,0.045)]">
      <span className="font-medium text-slate-900">
        {formatStatusLabel(
          contribution.feature.replace(/_/g, " ")
        )}
      </span>

      <span className="tabular-nums text-slate-600">
        {contribution.current_value}
      </span>

      {baseline !== null && (
        <span className="tabular-nums text-slate-500">
          mean ± std {baseline}
        </span>
      )}

      <span className="tabular-nums text-slate-500">
        z={contribution.z_score}
      </span>

      <span
        className={`inline-flex shrink-0 items-center whitespace-nowrap rounded-full border px-2 py-0.5 text-xs font-medium ${CONTRIBUTION_SEVERITY_CHIP_CLASSES[contribution.severity]}`}
      >
        {formatStatusLabel(contribution.severity)}
      </span>
    </li>
  );
}

function AnomalySection({
  state,
}: {
  state: AnomalySectionState;
}) {
  if (state.kind === "loading") {
    return (
      <div className={CARD_CLASS}>
        <h2 className={HEADING_CLASS}>Anomaly check</h2>
        <p className="text-sm text-[#71777c]">
          Loading anomaly results…
        </p>
      </div>
    );
  }

  if (state.kind === "error") {
    return (
      <div className={CARD_CLASS}>
        <h2 className={HEADING_CLASS}>Anomaly check</h2>
        <p className="text-sm text-red-600">{state.message}</p>
      </div>
    );
  }

  const { data } = state;

  if (isAnomalyNeverRanReport(data)) {
    return (
      <div className={CARD_CLASS}>
        <h2 className={HEADING_CLASS}>Anomaly check</h2>

        <div className="rounded-lg border border-[#DCEBFB] bg-[#EEF7FD] px-4 py-3">
          <p className="text-sm text-slate-700">
            Anomaly check has not been run. Click &apos;Run anomaly
            check&apos; above.
          </p>
        </div>
      </div>
    );
  }

  const contributions = data.contributions ?? [];
  const notes = data.notes ?? [];
  const historySize = data.history_size ?? 0;

  return (
    <div className={CARD_CLASS}>
      <h2 className={HEADING_CLASS}>Anomaly check</h2>

      <div className="space-y-4">
        <AnomalyBanner verdict={data.verdict} />

        <p className="text-sm text-[#5c666e]">
          Method: {formatAnomalyMethod(data.method)} · History:{" "}
          {historySize} {pluralize(historySize, "prior invoice")}
        </p>

        {formatScorePercent(data.score) === null ? (
          <p className="text-sm text-slate-400">
            Score not reported (see notes)
          </p>
        ) : (
          <p className="text-sm tabular-nums text-slate-600">
            Score: {formatScorePercent(data.score)}
          </p>
        )}

        {data.raw_decision !== null &&
          data.raw_decision !== undefined && (
            <p
              className="text-xs text-slate-400"
              title="Isolation Forest decision value. Lower = more anomalous."
            >
              Raw decision: {data.raw_decision.toFixed(4)} (lower =
              more anomalous)
            </p>
          )}

        {contributions.length > 0 ? (
          <ul className="space-y-2">
            {contributions.map((contribution) => (
              <ContributionRow
                key={`${contribution.feature}-${contribution.note}`}
                contribution={contribution}
              />
            ))}
          </ul>
        ) : (
          data.verdict !== "normal" &&
          data.verdict !== "insufficient_history" && (
            <p className="text-sm text-[#71777c]">
              Attribution not available.
            </p>
          )
        )}

        {notes.length > 0 && (
          <ul className="list-disc space-y-1 pl-5 text-sm text-[#5c666e]">
            {notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

const RISK_LEVEL_TEXT_CLASSES: Record<RiskLevel, string> = {
  low: "text-emerald-600",
  medium: "text-amber-600",
  high: "text-red-600",
  unknown: "text-slate-400",
};

const RISK_LEVEL_BADGE_CLASSES: Record<RiskLevel, string> = {
  low: "border-emerald-200 bg-emerald-50 text-emerald-700",
  medium: "border-amber-200 bg-amber-50 text-amber-700",
  high: "border-red-200 bg-red-50 text-red-700",
  unknown: "border-slate-200 bg-slate-100 text-slate-600",
};

const RISK_ACTION_LABELS: Record<
  RecommendedAction,
  string
> = {
  auto_approve: "Auto-approve recommended",
  review: "Review recommended",
  reject: "Reject recommended",
  unknown: "Not assessed",
};

const RISK_ACTION_BADGE_CLASSES: Record<
  RecommendedAction,
  string
> = {
  auto_approve:
    "border-emerald-200 bg-emerald-50 text-emerald-700",
  review:
    "border-amber-200 bg-amber-50 text-amber-700",
  reject:
    "border-red-200 bg-red-50 text-red-700",
  unknown:
    "border-slate-200 bg-slate-100 text-slate-600",
};

const RISK_SEVERITY_BORDER_CLASSES: Record<
  RiskSeverity,
  string
> = {
  error: "border-l-red-400",
  warn: "border-l-amber-400",
  info: "border-l-slate-300",
};

function RiskLevelBadge({
  level,
}: {
  level: RiskLevel;
}) {
  return (
    <span
      className={`inline-flex items-center whitespace-nowrap rounded-full border px-2 py-0.5 text-xs font-medium uppercase tracking-wide ${RISK_LEVEL_BADGE_CLASSES[level]}`}
    >
      {level}
    </span>
  );
}

function RiskActionPill({
  action,
}: {
  action: RecommendedAction;
}) {
  return (
    <span
      className={`inline-flex items-center whitespace-nowrap rounded-full border px-3 py-1 text-sm font-semibold ${RISK_ACTION_BADGE_CLASSES[action]}`}
    >
      {RISK_ACTION_LABELS[action]}
    </span>
  );
}

function RiskContributorRow({
  contributor,
}: {
  contributor: RiskContributor;
}) {
  return (
    <li
      className={`rounded-r-lg border border-l-4 border-[#DCEBFB] bg-white px-4 py-3 ${RISK_SEVERITY_BORDER_CLASSES[contributor.severity]}`}
    >
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1">
        <p className="min-w-0 flex-1 text-sm font-medium text-slate-900">
          {contributor.message}
        </p>

        <p className="shrink-0 whitespace-nowrap text-sm tabular-nums">
          <span className="font-bold text-slate-900">
            +{contributor.points}
          </span>
          <span className="text-slate-400">
            {" "}
            /{contributor.cap}
          </span>
        </p>
      </div>

      <p className="mt-1 font-mono text-xs text-slate-400">
        {contributor.rule_id}
      </p>

      <details className="mt-2">
        <summary className="cursor-pointer text-xs text-[#71777c] hover:text-slate-700">
          Evidence
        </summary>

        <pre className="mt-2 overflow-x-auto rounded bg-[#EEF7FD] p-3 font-mono text-xs leading-relaxed text-slate-700">
          {JSON.stringify(contributor.evidence, null, 2)}
        </pre>
      </details>
    </li>
  );
}

function RiskScoreExplanation({
  data,
}: {
  data: RiskAssessment;
}) {
  const notes = data.notes ?? [];

  return (
    <details className="rounded-lg border border-[#DCEBFB] bg-[#EEF7FD] px-4 py-3">
      <summary className="cursor-pointer text-sm font-medium text-slate-700">
        How the score is computed
      </summary>

      <div className="mt-3 space-y-3 text-sm">
        <p className="text-slate-700">
          Total:{" "}
          <span className="font-semibold tabular-nums">
            {data.contributing_total}
          </span>

          {data.caps_applied && (
            <span className="text-amber-700">
              {" "}
              — capped to 100. The uncapped sum is shown so the
              clamping is visible rather than silently applied.
            </span>
          )}
        </p>

        {notes.length > 0 && (
          <ul className="list-disc space-y-1 pl-5 text-slate-600">
            {notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        )}

        <p className="border-t border-[#DCEBFB] pt-3 text-xs text-[#71777c]">
          MVP model: weights are engineering judgment, not empirically
          calibrated. Each contributor has a fixed maximum impact and
          the score is their sum, so no LLM or ML model produced this
          number.
        </p>
      </div>
    </details>
  );
}

function RiskSection({
  state,
}: {
  state: RiskSectionState;
}) {
  if (state.kind === "loading") {
    return (
      <div className={CARD_CLASS}>
        <h2 className={HEADING_CLASS}>Risk assessment</h2>
        <p className="text-sm text-[#71777c]">
          Loading risk assessment…
        </p>
      </div>
    );
  }

  if (state.kind === "error") {
    return (
      <div className={CARD_CLASS}>
        <h2 className={HEADING_CLASS}>Risk assessment</h2>
        <p className="text-sm text-red-600">{state.message}</p>
      </div>
    );
  }

  const { data } = state;

  if (isRiskNeverRan(data)) {
    return (
      <div className={CARD_CLASS}>
        <h2 className={HEADING_CLASS}>Risk assessment</h2>

        <div className="rounded-lg border border-[#DCEBFB] bg-[#EEF7FD] px-4 py-3">
          <p className="text-sm text-slate-700">
            Risk assessment has not been run. Click &apos;Run risk
            assessment&apos; above.
          </p>
        </div>
      </div>
    );
  }

  const contributors = [...(data.contributors ?? [])].sort(
    (a, b) => b.points - a.points
  );

  return (
    <div className={CARD_CLASS}>
      <h2 className={HEADING_CLASS}>Risk assessment</h2>

      <div className="space-y-5">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <p className="flex items-baseline gap-3">
            <span
              className={`text-5xl font-bold tabular-nums leading-none ${RISK_LEVEL_TEXT_CLASSES[data.level]}`}
            >
              {data.score}
            </span>

            <span className="text-sm text-slate-400">
              / 100
            </span>
          </p>

          <RiskLevelBadge level={data.level} />
        </div>

        <RiskActionPill action={data.recommended_action} />

        {contributors.length > 0 ? (
          <ul className="space-y-2">
            {contributors.map((contributor) => (
              <RiskContributorRow
                key={contributor.rule_id}
                contributor={contributor}
              />
            ))}
          </ul>
        ) : (
          <p className="text-sm text-[#71777c]">
            No risk contributors detected.
          </p>
        )}

        <RiskScoreExplanation data={data} />
      </div>
    </div>
  );
}

/**
 * Invoice status treatment for this page.
 *
 * Same exhaustive `Record` discipline as the list page badge, but rendered as
 * a quiet marker: a small dot plus uppercase label on a soft background, so it
 * reads as part of the document rather than a pasted-on chip.
 */
const DOCUMENT_STATUS_CLASSES: Record<
  InvoiceStatus,
  { shell: string; dot: string }
> = {
  approved: {
    shell: "bg-emerald-50/80 text-emerald-700 ring-emerald-100",
    dot: "bg-emerald-500",
  },
  ingested: {
    shell: "bg-[#eef7fd] text-[#477cae] ring-[#dcebfb]",
    dot: "bg-[#5b9bd5]",
  },
  extracted: {
    shell: "bg-[#eef7fd] text-[#477cae] ring-[#dcebfb]",
    dot: "bg-[#5b9bd5]",
  },
  needs_review: {
    shell: "bg-amber-50/80 text-amber-700 ring-amber-100",
    dot: "bg-amber-500",
  },
  rejected: {
    shell: "bg-red-50/80 text-red-700 ring-red-100",
    dot: "bg-red-500",
  },
  failed: {
    shell: "bg-red-50/80 text-red-700 ring-red-100",
    dot: "bg-red-500",
  },
  validated: {
    shell: "bg-[#f3f5f7] text-[#5c666e] ring-[#e6ebef]",
    dot: "bg-[#9aa3aa]",
  },
};

function InvoiceDocumentStatus({ status }: { status: InvoiceStatus }) {
  const { shell, dot } = DOCUMENT_STATUS_CLASSES[status];

  return (
    <span
      className={`inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] ring-1 ring-inset ${shell}`}
    >
      <span
        aria-hidden="true"
        className={`h-1.5 w-1.5 rounded-full ${dot}`}
      />
      {formatStatusLabel(status)}
    </span>
  );
}

/**
 * The invoice itself: a paper-like sheet holding the extracted fields, the
 * line items, and the totals. Same values the "Extracted fields" and "Line
 * items" panels used to show, composed as one document.
 */
function InvoiceDocument({ invoice }: { invoice: Invoice }) {
  const lineItems = invoice.line_items ?? [];

  return (
    <article className="overflow-hidden rounded-[22px] border border-[#e7edf2] bg-white shadow-[0_1px_2px_rgba(23,25,27,0.04),0_36px_80px_-52px_rgba(23,52,74,0.75)] sm:rounded-[26px]">
      <div className="border-b border-[#eef2f6] px-6 py-8 sm:px-10 sm:py-10">
        <div className="flex flex-col gap-8 lg:flex-row lg:items-start lg:justify-between lg:gap-12">
          <div className="min-w-0">
            <p className="text-[10.5px] font-semibold uppercase tracking-[0.22em] text-[#9aa3aa]">
              Invoice
            </p>

            <h2 className="mt-4 break-words font-serif text-[32px] leading-none tracking-[-0.03em] text-[#17191b] sm:text-[42px]">
              {displayOrDash(invoice.invoice_number)}
            </h2>

            <div className="mt-7 border-l-2 border-[#dcebfb] pl-4">
              <p className="text-[10.5px] font-semibold uppercase tracking-[0.18em] text-[#9aa3aa]">
                From
              </p>
              <p className="mt-1.5 break-words text-[16px] font-medium text-[#282d31]">
                {displayOrDash(invoice.vendor_name)}
              </p>
            </div>
          </div>

          <dl className="grid shrink-0 grid-cols-2 gap-x-10 gap-y-6 sm:gap-x-14 lg:text-right">
            <MetaItem
              label="Issued"
              value={formatDate(invoice.invoice_date)}
              mono
            />
            <MetaItem
              label="Due"
              value={formatDate(invoice.due_date)}
              mono
            />
            <MetaItem
              label="PO number"
              value={displayOrDash(invoice.po_number)}
              mono
            />
            <MetaItem
              label="Currency"
              value={displayOrDash(invoice.currency)}
            />
          </dl>
        </div>
      </div>

      <div className="px-6 py-8 sm:px-10 sm:py-10">
        <h3 className="text-[10.5px] font-semibold uppercase tracking-[0.2em] text-[#8b949c]">
          Line items
        </h3>

        {lineItems.length > 0 ? (
          <div className="mt-5 -mx-6 overflow-x-auto px-6 sm:-mx-10 sm:px-10">
            <table className="w-full min-w-[520px] text-[13.5px]">
              <thead>
                <tr className="border-b border-[#eef2f6]">
                  <th className="w-10 pb-3 pr-3 text-left text-[10.5px] font-semibold uppercase tracking-[0.16em] text-[#a8b1b8]">
                    #
                  </th>
                  <th className="pb-3 pr-3 text-left text-[10.5px] font-semibold uppercase tracking-[0.16em] text-[#a8b1b8]">
                    Description
                  </th>
                  <th className="pb-3 px-3 text-right text-[10.5px] font-semibold uppercase tracking-[0.16em] text-[#a8b1b8]">
                    Qty
                  </th>
                  <th className="pb-3 px-3 text-right text-[10.5px] font-semibold uppercase tracking-[0.16em] text-[#a8b1b8]">
                    Unit price
                  </th>
                  <th className="pb-3 pl-3 text-right text-[10.5px] font-semibold uppercase tracking-[0.16em] text-[#a8b1b8]">
                    Line total
                  </th>
                </tr>
              </thead>

              <tbody>
                {lineItems.map((item) => (
                  <tr
                    key={item.id}
                    className="border-b border-[#f4f7f9] last:border-b-0"
                  >
                    <td className="py-4 pr-3 align-top text-[13px] tabular-nums text-[#a8b1b8]">
                      {item.position}
                    </td>

                    <td className="py-4 pr-3 align-top text-[#282d31]">
                      {displayOrDash(item.description)}
                    </td>

                    <td className="py-4 px-3 text-right align-top tabular-nums text-[#4d565d]">
                      {item.quantity === null || item.quantity.trim() === ""
                        ? EMPTY_VALUE
                        : item.quantity.trim()}
                    </td>

                    <td className="py-4 px-3 text-right align-top tabular-nums text-[#4d565d]">
                      {formatMoney(
                        item.unit_price,
                        invoice.currency
                      )}
                    </td>

                    <td className="py-4 pl-3 text-right align-top font-medium tabular-nums text-[#17191b]">
                      {formatMoney(
                        item.line_total,
                        invoice.currency
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="mt-4 border-l-2 border-[#dcebfb] pl-4 text-[13.5px] leading-6 text-[#858b90]">
            Line items will appear here after analysis.
          </p>
        )}
      </div>

      <div className="flex justify-end border-t border-[#eef2f6] bg-[#fbfcfd] px-6 py-8 sm:px-10 sm:py-10">
        <dl className="w-full max-w-[320px] space-y-3.5">
          <TotalRow
            label="Subtotal"
            value={formatMoney(
              invoice.subtotal,
              invoice.currency
            )}
          />
          <TotalRow
            label="Tax"
            value={formatMoney(
              invoice.tax_amount,
              invoice.currency
            )}
          />

          <div className="flex items-baseline justify-between gap-6 border-t border-[#e2e9ef] pt-5">
            <dt className="text-[10.5px] font-semibold uppercase tracking-[0.18em] text-[#8b949c]">
              Total due
            </dt>
            <dd className="font-serif text-[24px] leading-none tracking-[-0.02em] tabular-nums text-[#17191b] sm:text-[28px]">
              {formatMoney(
                invoice.total_amount,
                invoice.currency
              )}
            </dd>
          </div>
        </dl>
      </div>
    </article>
  );
}

function truncateId(id: string): string {
  if (id.length <= 20) return id;
  return `${id.slice(0, 10)}…${id.slice(-6)}`;
}

function hasExtractedFields(invoice: Invoice): boolean {
  return !!(
    invoice.vendor_name ||
    invoice.invoice_number ||
    invoice.invoice_date ||
    invoice.due_date ||
    invoice.currency ||
    invoice.subtotal ||
    invoice.tax_amount ||
    invoice.total_amount
  );
}

function InvoiceDetailContent({
  invoice,
  analysisState,
  validationActionState,
  validationSectionState,
  poMatchActionState,
  poMatchSectionState,
  policyCheckActionState,
  policyEvidenceSectionState,
  duplicateActionState,
  duplicateSectionState,
  anomalyActionState,
  anomalySectionState,
  riskActionState,
  riskSectionState,
  reviewCase,
  reviewSubmitState,
  onResolveReview,
  onRunAnalysis,
  onRunValidation,
  onRunPoMatch,
  onRunPolicyCheck,
  onRunDuplicateCheck,
  onRunAnomalyCheck,
  onRunRisk,
  onDelete,
}: {
  invoice: Invoice;
  analysisState: AnalysisState;
  validationActionState: ValidationActionState;
  validationSectionState: ValidationSectionState;
  poMatchActionState: PoMatchActionState;
  poMatchSectionState: PoMatchSectionState;
  policyCheckActionState: PolicyCheckActionState;
  policyEvidenceSectionState: PolicyEvidenceSectionState;
  duplicateActionState: DuplicateCheckActionState;
  duplicateSectionState: DuplicateSectionState;
  anomalyActionState: AnomalyCheckActionState;
  anomalySectionState: AnomalySectionState;
  riskActionState: RiskActionState;
  riskSectionState: RiskSectionState;
  reviewCase: ReviewCase | null;
  reviewSubmitState: ReviewSubmitState;
  onResolveReview: (
    decision: ReviewDecision,
    resolution: string
  ) => void;
  onRunAnalysis: () => void;
  onRunValidation: () => void;
  onRunPoMatch: () => void;
  onRunPolicyCheck: () => void;
  onRunDuplicateCheck: () => void;
  onRunAnomalyCheck: () => void;
  onRunRisk: () => void;
  onDelete: () => void;
}) {
  const isExtracted =
    invoice.status === "extracted" ||
    hasExtractedFields(invoice);

  const showControls =
    invoice.status === "ingested" ||
    invoice.status === "extracted" ||
    invoice.status === "validated" ||
    invoice.status === "needs_review";

  /**
   * Secondary checks, in the order they are surfaced today. Each entry keeps
   * the original "only offer the button while idle" rule, but the buttons are
   * grouped in one overflow menu so the primary action stays dominant.
   */
  const secondaryActions = [
    {
      key: "validation",
      label: "Run validation",
      visible: validationActionState.kind === "idle",
      onClick: onRunValidation,
    },
    {
      key: "po-match",
      label: "Run PO match",
      visible: poMatchActionState.kind === "idle",
      onClick: onRunPoMatch,
    },
    {
      key: "policy",
      label: "Run policy check",
      visible: policyCheckActionState.kind === "idle",
      onClick: onRunPolicyCheck,
    },
    {
      key: "duplicates",
      label: "Run duplicate check",
      visible: duplicateActionState.kind === "idle",
      onClick: onRunDuplicateCheck,
    },
    {
      key: "anomalies",
      label: "Run anomaly check",
      visible: anomalyActionState.kind === "idle",
      onClick: onRunAnomalyCheck,
    },
    {
      key: "risk",
      label: "Run risk assessment",
      visible: riskActionState.kind === "idle",
      onClick: onRunRisk,
    },
  ].filter((action) => action.visible);

  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  // Close on outside click / Escape so the menu never sticks open.
  useEffect(() => {
    if (!menuOpen) return;

    function handlePointerDown(event: PointerEvent): void {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setMenuOpen(false);
      }
    }

    function handleKeyDown(event: KeyboardEvent): void {
      if (event.key === "Escape") {
        setMenuOpen(false);
      }
    }

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [menuOpen]);

  /** Run a check, then close the menu so the feedback strip is visible. */
  function handleMenuAction(onClick: () => void): void {
    setMenuOpen(false);
    onClick();
  }

  /** Only surface the feedback strip while a check is actually running or failed. */
  const hasActionFeedback = [
    analysisState,
    validationActionState,
    poMatchActionState,
    policyCheckActionState,
    duplicateActionState,
    anomalyActionState,
    riskActionState,
  ].some((state) => state.kind === "running" || state.kind === "error");

  return (
    <div className="space-y-10">
      <header className="space-y-7">
        <div className="flex flex-col gap-7 lg:flex-row lg:items-end lg:justify-between lg:gap-10">
          <div className="min-w-0">
            <p className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[10.5px] font-semibold uppercase tracking-[0.22em] text-[#9aa3aa]">
              Invoice
              <span
                aria-hidden="true"
                className="h-px w-6 bg-[#dfe6ec]"
              />
              <span
                className="font-mono normal-case tracking-normal text-[#a8b1b8]"
                title={invoice.id}
              >
                {truncateId(invoice.id)}
              </span>
              <CopyIdButton id={invoice.id} />
            </p>

            <h1 className="mt-4 font-serif text-[30px] leading-[1.1] tracking-[-0.03em] text-[#17191b] sm:text-[40px] lg:text-[46px]">
              {displayOrDash(invoice.vendor_name)}
            </h1>

            <div className="mt-5 flex flex-wrap items-center gap-x-4 gap-y-3">
              <InvoiceDocumentStatus status={invoice.status} />

              <span className="inline-flex items-baseline gap-2 text-[13.5px] text-[#858b90]">
                <span className="font-mono text-[#477cae]">
                  {displayOrDash(invoice.invoice_number)}
                </span>
                <span aria-hidden="true">&middot;</span>
                Uploaded{" "}
                <time dateTime={invoice.created_at}>
                  {formatDateTime(invoice.created_at)}
                </time>
                {invoice.due_date ? (
                  <>
                    <span aria-hidden="true">&middot;</span>
                    <span
                      title={invoice.due_date}
                      className={
                        isOverdueDetail(invoice.due_date)
                          ? "font-medium text-red-600"
                          : undefined
                      }
                    >
                      Due {formatDate(invoice.due_date)}
                      {isOverdueDetail(invoice.due_date) ? " · Overdue" : ""}
                    </span>
                  </>
                ) : null}
              </span>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-4 lg:flex-col lg:items-end">
          {showControls && (
            <div className="flex flex-wrap items-center gap-3">
              {analysisState.kind === "idle" && (
                <button
                  type="button"
                  onClick={onRunAnalysis}
                  className={PRIMARY_ACTION_CLASS}
                >
                  {isExtracted
                    ? "Re-run analysis"
                    : "Run analysis"}
                </button>
              )}

              {secondaryActions.length > 0 && (
                <div ref={menuRef} className="relative">
                  <button
                    type="button"
                    onClick={() => setMenuOpen((open) => !open)}
                    aria-expanded={menuOpen}
                    aria-haspopup="menu"
                    className={SECONDARY_ACTION_CLASS}
                  >
                    More checks
                    <span
                      aria-hidden="true"
                      className="ml-2 text-[10px] text-[#9aa3aa]"
                    >
                      {secondaryActions.length}
                    </span>
                    <span
                      aria-hidden="true"
                      className={`text-[10px] text-[#9aa3aa] transition-transform duration-200 ${menuOpen ? "rotate-180" : ""}`}
                    >
                      ▾
                    </span>
                  </button>

                  {menuOpen && (
                    <div
                      role="menu"
                      className="absolute right-0 z-20 mt-2 w-60 overflow-hidden rounded-2xl border border-[#e7edf2] bg-white p-1.5 shadow-[0_24px_50px_-24px_rgba(23,52,74,0.55)]"
                    >
                      {secondaryActions.map((action) => (
                        <button
                          key={action.key}
                          type="button"
                          role="menuitem"
                          onClick={() => handleMenuAction(action.onClick)}
                          className={`block w-full rounded-xl px-3.5 py-2.5 text-left text-[13.5px] text-[#3f474d] transition-colors duration-150 hover:bg-[#eef7fd] hover:text-[#477cae] ${FOCUS_RING}`}
                        >
                          {action.label}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

            <button
              type="button"
              onClick={onDelete}
              aria-label="Delete this invoice"
              className={DELETE_ACTION_CLASS}
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
              Delete invoice
            </button>
          </div>
        </div>
      </header>

      <ErrorBoundary sectionName="Extracted fields">
        <InvoiceDocument invoice={invoice} />
      </ErrorBoundary>

      {showControls && hasActionFeedback && (
        <div className="space-y-4 rounded-2xl border border-[#eef2f6] bg-white/70 px-5 py-4 sm:px-6">

          {analysisState.kind === "running" && (
            <div className="flex items-center gap-3">
              <span
                aria-hidden="true"
                className={SPINNER_CLASS}
              />
              <span className="text-sm text-[#5c666e]">
                Analyzing…
              </span>
            </div>
          )}

          {analysisState.kind === "error" && (
            <div className="space-y-3">
              <div className="space-y-1">
                <p className="text-sm font-medium text-red-900">
                  Analysis failed
                </p>
                <p className="text-sm text-red-800">
                  {analysisState.message}
                </p>
              </div>

              <button
                type="button"
                onClick={onRunAnalysis}
                className={RETRY_ACTION_CLASS}
              >
                Try again
              </button>
            </div>
          )}

          {validationActionState.kind === "running" && (
            <div className="flex items-center gap-3">
              <span
                aria-hidden="true"
                className={SPINNER_CLASS}
              />
              <span className="text-sm text-[#5c666e]">
                Validating…
              </span>
            </div>
          )}

          {validationActionState.kind === "error" && (
            <div className="space-y-3">
              <div className="space-y-1">
                <p className="text-sm font-medium text-red-900">
                  Validation failed
                </p>
                <p className="text-sm text-red-800">
                  {validationActionState.message}
                </p>
              </div>

              <button
                type="button"
                onClick={onRunValidation}
                className={RETRY_ACTION_CLASS}
              >
                Try again
              </button>
            </div>
          )}

          {poMatchActionState.kind === "running" && (
            <div className="flex items-center gap-3">
              <span
                aria-hidden="true"
                className={SPINNER_CLASS}
              />
              <span className="text-sm text-[#5c666e]">
                Matching against PO…
              </span>
            </div>
          )}

          {poMatchActionState.kind === "error" && (
            <div className="space-y-3">
              <div className="space-y-1">
                <p className="text-sm font-medium text-red-900">
                  PO match failed
                </p>
                <p className="text-sm text-red-800">
                  {poMatchActionState.message}
                </p>
              </div>

              <button
                type="button"
                onClick={onRunPoMatch}
                className={RETRY_ACTION_CLASS}
              >
                Try again
              </button>
            </div>
          )}

          {policyCheckActionState.kind === "running" && (
            <div className="flex items-center gap-3">
              <span
                aria-hidden="true"
                className={SPINNER_CLASS}
              />
              <span className="text-sm text-[#5c666e]">
                Searching policy corpus…
              </span>
            </div>
          )}

          {policyCheckActionState.kind === "error" && (
            <div className="space-y-3">
              <div className="space-y-1">
                <p className="text-sm font-medium text-red-900">
                  Policy check failed
                </p>
                <p className="text-sm text-red-800">
                  {policyCheckActionState.message}
                </p>
              </div>

              <button
                type="button"
                onClick={onRunPolicyCheck}
                className={RETRY_ACTION_CLASS}
              >
                Try again
              </button>
            </div>
          )}

          {duplicateActionState.kind === "running" && (
            <div className="flex items-center gap-3">
              <span
                aria-hidden="true"
                className={SPINNER_CLASS}
              />
              <span className="text-sm text-[#5c666e]">
                Checking for duplicates…
              </span>
            </div>
          )}

          {duplicateActionState.kind === "error" && (
            <div className="space-y-3">
              <div className="space-y-1">
                <p className="text-sm font-medium text-red-900">
                  Duplicate check failed
                </p>
                <p className="text-sm text-red-800">
                  {duplicateActionState.message}
                </p>
              </div>

              <button
                type="button"
                onClick={onRunDuplicateCheck}
                className={RETRY_ACTION_CLASS}
              >
                Try again
              </button>
            </div>
          )}

          {anomalyActionState.kind === "running" && (
            <div className="flex items-center gap-3">
              <span
                aria-hidden="true"
                className={SPINNER_CLASS}
              />
              <span className="text-sm text-[#5c666e]">
                Checking for anomalies…
              </span>
            </div>
          )}

          {anomalyActionState.kind === "error" && (
            <div className="space-y-3">
              <div className="space-y-1">
                <p className="text-sm font-medium text-red-900">
                  Anomaly check failed
                </p>
                <p className="text-sm text-red-800">
                  {anomalyActionState.message}
                </p>
              </div>

              <button
                type="button"
                onClick={onRunAnomalyCheck}
                className={RETRY_ACTION_CLASS}
              >
                Try again
              </button>
            </div>
          )}

          {riskActionState.kind === "running" && (
            <div className="flex items-center gap-3">
              <span
                aria-hidden="true"
                className={SPINNER_CLASS}
              />
              <span className="text-sm text-[#5c666e]">
                Scoring risk…
              </span>
            </div>
          )}

          {riskActionState.kind === "error" && (
            <div className="space-y-3">
              <div className="space-y-1">
                <p className="text-sm font-medium text-red-900">
                  Risk assessment failed
                </p>
                <p className="text-sm text-red-800">
                  {riskActionState.message}
                </p>
              </div>

              <button
                type="button"
                onClick={onRunRisk}
                className={RETRY_ACTION_CLASS}
              >
                Try again
              </button>
            </div>
          )}
        </div>
      )}

      <ErrorBoundary sectionName="Document">
        <section className={CARD_CLASS}>
          <h2 className={HEADING_CLASS}>Source document</h2>

          <dl className="grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
            <MetaItem
              label="Filename"
              value={invoice.document_filename}
              mono
            />
            <MetaItem
              label="Content type"
              value={displayOrDash(
                invoice.document_content_type
              )}
            />
            <MetaItem
              label="Size"
              value={formatBytes(
                invoice.document_size_bytes
              )}
            />
            <MetaItem
              label="Processing status"
              value={formatStatusLabel(
                invoice.document_processing_status
              )}
            />
          </dl>
        </section>
      </ErrorBoundary>

      <div className="flex items-center gap-4 pt-2">
        <h2 className="text-[10.5px] font-semibold uppercase tracking-[0.22em] text-[#9aa3aa]">
          Intelligence
        </h2>
        <span
          aria-hidden="true"
          className="h-px flex-1 bg-gradient-to-r from-[#e2e9ef] to-transparent"
        />
      </div>

      <ErrorBoundary sectionName="Risk assessment">
        <RiskSection state={riskSectionState} />
      </ErrorBoundary>

      <ErrorBoundary sectionName="Review">
        <ReviewSection
          reviewCase={reviewCase}
          onResolve={onResolveReview}
          submitState={reviewSubmitState}
        />
      </ErrorBoundary>

      <ErrorBoundary sectionName="Validation">
        <ValidationSection
          state={validationSectionState}
          onRunValidation={onRunValidation}
        />
      </ErrorBoundary>

      <ErrorBoundary sectionName="Purchase order match">
        <PoMatchSection state={poMatchSectionState} />
      </ErrorBoundary>

      <ErrorBoundary sectionName="Policy evidence">
        <PolicyEvidenceSection
          state={policyEvidenceSectionState}
        />
      </ErrorBoundary>

      <ErrorBoundary sectionName="Duplicate check">
        <DuplicateSection state={duplicateSectionState} />
      </ErrorBoundary>

      <ErrorBoundary sectionName="Anomaly check">
        <AnomalySection state={anomalySectionState} />
      </ErrorBoundary>
    </div>
  );
}

function BackToList({ current }: { current?: string }) {
  return (
    <nav aria-label="Breadcrumb">
      <ol className="flex flex-wrap items-center gap-2 text-[12.5px]">
        <li>
          <Link
            to="/invoices"
            aria-label="Back to invoices"
            className={`group inline-flex items-center gap-1.5 rounded-md py-1 text-[#858b90] transition-colors duration-200 hover:text-[#477cae] ${FOCUS_RING}`}
          >
            <span
              aria-hidden="true"
              className="transition-transform duration-200 ease-out group-hover:-translate-x-0.5"
            >
              ←
            </span>
            Invoices
          </Link>
        </li>

        {current ? (
          <>
            <li
              aria-hidden="true"
              className="text-[#c8d1d8]"
            >
              /
            </li>
            <li
              aria-current="page"
              aria-label={`Invoice ${current}`}
              title={current}
              className="max-w-45 truncate py-1 font-mono text-[#477cae]"
            >
              {current}
            </li>
          </>
        ) : null}
      </ol>
    </nav>
  );
}

export default function InvoiceDetailPage() {
  const { id } = useParams<{ id: string }>();

  const [state, setState] = useState<DetailState>({
    kind: "loading",
  });

  const [analysisState, setAnalysisState] =
    useState<AnalysisState>({
      kind: "idle",
    });

  const [validationActionState, setValidationActionState] =
    useState<ValidationActionState>({
      kind: "idle",
    });

  const [validationSectionState, setValidationSectionState] =
    useState<ValidationSectionState>({
      kind: "loading",
    });

  const [poMatchActionState, setPoMatchActionState] =
    useState<PoMatchActionState>({
      kind: "idle",
    });

  const [poMatchSectionState, setPoMatchSectionState] =
    useState<PoMatchSectionState>({
      kind: "loading",
    });

  const [policyCheckActionState, setPolicyCheckActionState] =
    useState<PolicyCheckActionState>({
      kind: "idle",
    });

  const [
    policyEvidenceSectionState,
    setPolicyEvidenceSectionState,
  ] = useState<PolicyEvidenceSectionState>({
    kind: "loading",
  });

  const [duplicateActionState, setDuplicateActionState] =
    useState<DuplicateCheckActionState>({
      kind: "idle",
    });

  const [duplicateSectionState, setDuplicateSectionState] =
    useState<DuplicateSectionState>({
      kind: "loading",
    });

  const [anomalyActionState, setAnomalyActionState] =
    useState<AnomalyCheckActionState>({
      kind: "idle",
    });

  const [anomalySectionState, setAnomalySectionState] =
    useState<AnomalySectionState>({
      kind: "loading",
    });

  const [riskActionState, setRiskActionState] =
    useState<RiskActionState>({
      kind: "idle",
    });

  const [riskSectionState, setRiskSectionState] =
    useState<RiskSectionState>({
      kind: "loading",
    });

  const [reviewCase, setReviewCase] =
    useState<ReviewCase | null>(null);

  const [reviewSubmitState, setReviewSubmitState] =
    useState<ReviewSubmitState>({
      kind: "idle",
    });

  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const navigate = useNavigate();

  /** Confirm handler: DELETE, then land on the list with a success banner. */
  async function handleConfirmDelete(): Promise<void> {
    if (!id) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await deleteInvoice(id);
      const label =
        state.kind === "loaded"
          ? (state.data.invoice_number ?? state.data.vendor_name ?? id)
          : id;
      navigate("/invoices", { state: { deleted: label } });
    } catch (error: unknown) {
      setDeleteError(
        error instanceof ApiError ? error.message : GENERIC_ERROR_MESSAGE
      );
      setDeleteDialogOpen(false);
    } finally {
      setDeleteBusy(false);
    }
  }

  useEffect(() => {
    if (!id) {
      setState({
        kind: "error",
        message: "No invoice id was provided.",
      });
      return;
    }

    let isActive = true;

    setState({ kind: "loading" });
    setValidationSectionState({ kind: "loading" });
    setPoMatchSectionState({ kind: "loading" });
    setPoMatchActionState({ kind: "idle" });
    setPolicyEvidenceSectionState({ kind: "loading" });
    setPolicyCheckActionState({ kind: "idle" });
    setDuplicateSectionState({ kind: "loading" });
    setDuplicateActionState({ kind: "idle" });
    setAnomalySectionState({ kind: "loading" });
    setAnomalyActionState({ kind: "idle" });
    setRiskSectionState({ kind: "loading" });
    setRiskActionState({ kind: "idle" });
    setReviewCase(null);
    setReviewSubmitState({ kind: "idle" });

    getInvoice(id)
      .then((data) => {
        if (isActive) {
          setState({ kind: "loaded", data });
        }
      })
      .catch((error: unknown) => {
        if (!isActive) return;

        setState({
          kind: "error",
          message:
            error instanceof ApiError
              ? error.message
              : GENERIC_ERROR_MESSAGE,
        });
      });

    getValidation(id)
      .then((data) => {
        if (isActive) {
          setValidationSectionState({
            kind: "loaded",
            data,
          });
        }
      })
      .catch((error: unknown) => {
        if (!isActive) return;

        setValidationSectionState({
          kind: "error",
          message:
            error instanceof ApiError
              ? error.message
              : GENERIC_ERROR_MESSAGE,
        });
      });

    getPoMatch(id)
      .then((data) => {
        if (isActive) {
          setPoMatchSectionState({
            kind: "loaded",
            data,
          });
        }
      })
      .catch((error: unknown) => {
        if (!isActive) return;

        if (
          error instanceof ApiError &&
          error.status === 404
        ) {
          setState({
            kind: "error",
            message: "invoice not found",
          });
          return;
        }

        setPoMatchSectionState({
          kind: "error",
          message:
            error instanceof ApiError
              ? error.message
              : GENERIC_ERROR_MESSAGE,
        });
      });

    getPolicyEvidence(id)
      .then((data) => {
        if (isActive) {
          setPolicyEvidenceSectionState({
            kind: "loaded",
            data,
          });
        }
      })
      .catch((error: unknown) => {
        if (!isActive) return;

        if (
          error instanceof ApiError &&
          error.status === 404
        ) {
          setState({
            kind: "error",
            message: "invoice not found",
          });
          return;
        }

        setPolicyEvidenceSectionState({
          kind: "error",
          message:
            error instanceof ApiError
              ? error.message
              : GENERIC_ERROR_MESSAGE,
        });
      });

    getDuplicates(id)
      .then((data) => {
        if (isActive) {
          setDuplicateSectionState({
            kind: "loaded",
            data,
          });
        }
      })
      .catch((error: unknown) => {
        if (!isActive) return;

        if (
          error instanceof ApiError &&
          error.status === 404
        ) {
          setState({
            kind: "error",
            message: "invoice not found",
          });
          return;
        }

        setDuplicateSectionState({
          kind: "error",
          message:
            error instanceof ApiError
              ? error.message
              : GENERIC_ERROR_MESSAGE,
        });
      });

    getAnomalies(id)
      .then((data) => {
        if (isActive) {
          setAnomalySectionState({
            kind: "loaded",
            data,
          });
        }
      })
      .catch((error: unknown) => {
        if (!isActive) return;

        if (
          error instanceof ApiError &&
          error.status === 404
        ) {
          setState({
            kind: "error",
            message: "invoice not found",
          });
          return;
        }

        setAnomalySectionState({
          kind: "error",
          message:
            error instanceof ApiError
              ? error.message
              : GENERIC_ERROR_MESSAGE,
        });
      });

    getRisk(id)
      .then((data) => {
        if (isActive) {
          setRiskSectionState({
            kind: "loaded",
            data,
          });
        }
      })
      .catch((error: unknown) => {
        if (!isActive) return;

        if (
          error instanceof ApiError &&
          error.status === 404
        ) {
          setState({
            kind: "error",
            message: "invoice not found",
          });
          return;
        }

        setRiskSectionState({
          kind: "error",
          message:
            error instanceof ApiError
              ? error.message
              : GENERIC_ERROR_MESSAGE,
        });
      });

    listReviews({ limit: REVIEW_LIST_LIMIT })
      .then((data) => {
        if (!isActive) return;

        const match =
          data.items.find(
            (c) => c.invoice_id === id
          ) ?? null;

        setReviewCase(match);
      })
      .catch(() => {
        if (!isActive) return;
        setReviewCase(null);
      });

    return () => {
      isActive = false;
    };
  }, [id]);

  async function handleRunAnalysis(): Promise<void> {
    if (!id) return;

    setAnalysisState({ kind: "running" });

    try {
      const updatedInvoice = await analyzeInvoice(id);

      setState({
        kind: "loaded",
        data: updatedInvoice,
      });

      setAnalysisState({ kind: "idle" });
    } catch (error: unknown) {
      setAnalysisState({
        kind: "error",
        message:
          error instanceof ApiError
            ? error.message
            : GENERIC_ERROR_MESSAGE,
      });
    }
  }

  async function handleRunValidation(): Promise<void> {
    if (!id) return;

    setValidationActionState({ kind: "running" });

    try {
      const updatedInvoice = await validateInvoice(id);
      const summary = await getValidation(id);

      setState({
        kind: "loaded",
        data: updatedInvoice,
      });

      setValidationSectionState({
        kind: "loaded",
        data: summary,
      });

      setValidationActionState({ kind: "idle" });
    } catch (error: unknown) {
      setValidationActionState({
        kind: "error",
        message:
          error instanceof ApiError
            ? error.message
            : GENERIC_ERROR_MESSAGE,
      });
    }
  }

  async function handleRunPoMatch(): Promise<void> {
    if (!id) return;

    setPoMatchActionState({ kind: "running" });

    try {
      const updatedInvoice = await matchPurchaseOrder(id);
      const report = await getPoMatch(id);

      setState({
        kind: "loaded",
        data: updatedInvoice,
      });

      setPoMatchSectionState({
        kind: "loaded",
        data: report,
      });

      setPoMatchActionState({ kind: "idle" });
    } catch (error: unknown) {
      setPoMatchActionState({
        kind: "error",
        message:
          error instanceof ApiError
            ? error.message
            : GENERIC_ERROR_MESSAGE,
      });
    }
  }

  async function handleRunPolicyCheck(): Promise<void> {
    if (!id) return;

    setPolicyCheckActionState({ kind: "running" });

    try {
      const updatedInvoice = await runPolicyCheck(id);
      const finding = await getPolicyEvidence(id);

      setState({
        kind: "loaded",
        data: updatedInvoice,
      });

      setPolicyEvidenceSectionState({
        kind: "loaded",
        data: finding,
      });

      setPolicyCheckActionState({ kind: "idle" });
    } catch (error: unknown) {
      setPolicyCheckActionState({
        kind: "error",
        message:
          error instanceof ApiError
            ? error.message
            : GENERIC_ERROR_MESSAGE,
      });
    }
  }

  async function handleRunDuplicateCheck(): Promise<void> {
    if (!id) return;

    setDuplicateActionState({ kind: "running" });

    try {
      const updatedInvoice = await checkDuplicates(id);
      const report = await getDuplicates(id);

      setState({
        kind: "loaded",
        data: updatedInvoice,
      });

      setDuplicateSectionState({
        kind: "loaded",
        data: report,
      });

      setDuplicateActionState({ kind: "idle" });
    } catch (error: unknown) {
      setDuplicateActionState({
        kind: "error",
        message:
          error instanceof ApiError
            ? error.message
            : GENERIC_ERROR_MESSAGE,
      });
    }
  }

  async function handleRunAnomalyCheck(): Promise<void> {
    if (!id) return;

    setAnomalyActionState({ kind: "running" });

    try {
      const updatedInvoice = await checkAnomalies(id);
      const report = await getAnomalies(id);

      setState({
        kind: "loaded",
        data: updatedInvoice,
      });

      setAnomalySectionState({
        kind: "loaded",
        data: report,
      });

      setAnomalyActionState({ kind: "idle" });
    } catch (error: unknown) {
      setAnomalyActionState({
        kind: "error",
        message:
          error instanceof ApiError
            ? error.message
            : GENERIC_ERROR_MESSAGE,
      });
    }
  }

  async function handleRunRisk(): Promise<void> {
    if (!id) return;

    setRiskActionState({ kind: "running" });

    try {
      const updatedInvoice = await assessRisk(id);
      const assessment = await getRisk(id);

      setState({
        kind: "loaded",
        data: updatedInvoice,
      });

      setRiskSectionState({
        kind: "loaded",
        data: assessment,
      });

      setRiskActionState({ kind: "idle" });
    } catch (error: unknown) {
      setRiskActionState({
        kind: "error",
        message:
          error instanceof ApiError
            ? error.message
            : GENERIC_ERROR_MESSAGE,
      });
    }
  }

  async function handleResolveReview(
    decision: ReviewDecision,
    resolution: string
  ): Promise<void> {
    if (!id || reviewCase === null) return;

    setReviewSubmitState({ kind: "submitting" });

    try {
      await resolveReview(reviewCase.id, {
        decision,
        reviewer_id: REVIEWER_ID ?? "",
        resolution,
      });

      const [updatedInvoice, reviews] =
        await Promise.all([
          getInvoice(id),
          listReviews({
            limit: REVIEW_LIST_LIMIT,
          }),
        ]);

      setState({
        kind: "loaded",
        data: updatedInvoice,
      });

      setReviewCase(
        reviews.items.find(
          (c) => c.invoice_id === id
        ) ?? null
      );

      setReviewSubmitState({ kind: "idle" });
    } catch (error: unknown) {
      setReviewSubmitState({
        kind: "error",
        message:
          error instanceof ApiError
            ? error.message
            : GENERIC_ERROR_MESSAGE,
      });
    }
  }

  return (
    <section className="space-y-8">
      {state.kind === "loaded" ? (
        <BackToList current={displayOrDash(state.data.invoice_number)} />
      ) : (
        <BackToList />
      )}

      {deleteError && (
        <div
          role="alert"
          className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-red-100 bg-red-50/70 px-5 py-3.5"
        >
          <p className="text-sm font-medium text-red-800">
            Could not delete this invoice
          </p>
          <p className="text-sm text-red-700">{deleteError}</p>
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
        open={deleteDialogOpen}
        title="Delete this invoice?"
        body="This soft-deletes the invoice from active views. The audit trail is preserved."
        confirmLabel="Delete"
        busy={deleteBusy}
        onConfirm={handleConfirmDelete}
        onCancel={() => {
          if (!deleteBusy) setDeleteDialogOpen(false);
        }}
      />

      {state.kind === "loading" && (
        <div
          role="status"
          aria-live="polite"
          className="overflow-hidden rounded-[22px] border border-[#e7edf2] bg-white p-8 shadow-[0_1px_2px_rgba(23,25,27,0.04),0_36px_80px_-52px_rgba(23,52,74,0.75)] sm:p-12"
        >
          <div className="flex items-center gap-3.5">
            <span
              aria-hidden="true"
              className={SPINNER_CLASS}
            />

            <span className="text-[15px] text-[#71777c]">
              Loading invoice…
            </span>
          </div>

          <div
            aria-hidden="true"
            className="mt-10 space-y-4"
          >
            <span className="block h-2.5 w-24 rounded-full bg-[#f1f5f8]" />
            <span className="block h-9 w-3/5 rounded-lg bg-[#f4f7f9]" />
            <span className="block h-2.5 w-2/5 rounded-full bg-[#f1f5f8]" />
          </div>
        </div>
      )}

      {state.kind === "loaded" && (
        <InvoiceDetailContent
          invoice={state.data}
          analysisState={analysisState}
          validationActionState={validationActionState}
          validationSectionState={validationSectionState}
          poMatchActionState={poMatchActionState}
          poMatchSectionState={poMatchSectionState}
          policyCheckActionState={policyCheckActionState}
          policyEvidenceSectionState={
            policyEvidenceSectionState
          }
          duplicateActionState={duplicateActionState}
          duplicateSectionState={duplicateSectionState}
          anomalyActionState={anomalyActionState}
          anomalySectionState={anomalySectionState}
          riskActionState={riskActionState}
          riskSectionState={riskSectionState}
          reviewCase={reviewCase}
          reviewSubmitState={reviewSubmitState}
          onResolveReview={handleResolveReview}
          onRunAnalysis={handleRunAnalysis}
          onRunValidation={handleRunValidation}
          onRunPoMatch={handleRunPoMatch}
          onRunPolicyCheck={handleRunPolicyCheck}
          onRunDuplicateCheck={handleRunDuplicateCheck}
          onRunAnomalyCheck={handleRunAnomalyCheck}
          onRunRisk={handleRunRisk}
          onDelete={() => {
            setDeleteError(null);
            setDeleteDialogOpen(true);
          }}
        />
      )}

      {state.kind === "error" && (
        <div className="overflow-hidden rounded-[22px] border border-[#e7edf2] bg-white shadow-[0_1px_2px_rgba(23,25,27,0.04),0_36px_80px_-56px_rgba(23,52,74,0.7)]">
          <div className="h-1 w-full bg-gradient-to-r from-[#5b9bd5] via-[#dcebfb] to-transparent" />

          <div className="px-6 py-10 sm:px-12 sm:py-14">
            <p className="text-[10.5px] font-semibold uppercase tracking-[0.22em] text-[#9aa3aa]">
              Unavailable
            </p>

            <h2 className="mt-4 font-serif text-[26px] leading-[1.15] tracking-[-0.03em] text-[#17191b] sm:text-[34px]">
              Could not load this invoice
            </h2>

            <p className="mt-4 max-w-md text-[14.5px] leading-7 text-[#71777c]">
              {state.message}
            </p>

            <Link
              to="/invoices"
              className={`mt-8 inline-flex items-center justify-center rounded-full bg-[#17191b] px-5 py-3 text-sm font-medium text-white transition-all duration-200 ease-out hover:-translate-y-0.5 hover:bg-[#24292e] ${FOCUS_RING}`}
            >
              Back to invoices
            </Link>
          </div>
        </div>
      )}
    </section>
  );
}