import { useState } from "react";

import { REVIEWER_ID_IS_CONFIGURED } from "../config/reviewer";
import type { ReviewCase, ReviewDecision } from "../types/review";
import { isReviewOpen } from "../types/review";

const HEADING_CLASS = "mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500";
const CARD_CLASS = "rounded-lg border border-slate-200 bg-white p-5";

/** Submit lifecycle for the resolve form. */
type SubmitState =
  | { kind: "idle" }
  | { kind: "submitting" }
  | { kind: "error"; message: string };

/** Review status -> badge pill classes. Mirrors the review queue page. */
const REVIEW_STATUS_BADGE_CLASSES: Record<ReviewCase["status"], string> = {
  pending: "border-amber-200 bg-amber-50 text-amber-700",
  in_progress: "border-indigo-200 bg-indigo-50 text-indigo-700",
  resolved: "border-emerald-200 bg-emerald-50 text-emerald-700",
  dismissed: "border-slate-200 bg-slate-100 text-slate-600",
};

/** Decision -> label, and the colour its segmented-control button takes. */
const DECISION_LABELS: Record<ReviewDecision, string> = {
  approve: "Approve",
  reject: "Reject",
  dismiss: "Dismiss",
};

const DECISION_ACTIVE_CLASSES: Record<ReviewDecision, string> = {
  approve: "border-emerald-300 bg-emerald-50 text-emerald-800",
  reject: "border-red-300 bg-red-50 text-red-800",
  dismiss: "border-slate-300 bg-slate-100 text-slate-700",
};

function ReviewStatusBadge({ status }: { status: ReviewCase["status"] }) {
  return (
    <span
      className={`inline-flex items-center whitespace-nowrap rounded-full border px-2 py-0.5 text-xs font-medium ${REVIEW_STATUS_BADGE_CLASSES[status]}`}
    >
      {status.replace(/_/g, " ")}
    </span>
  );
}

/**
 * The decision form.
 *
 * Disabled wholesale when no reviewer id is configured. Submitting without one
 * would either 422 on an empty reviewer_id or, worse, silently attribute the
 * decision to a placeholder identity — so the form is inert and says why.
 */
function ResolveForm({
  onSubmit,
  submitState,
}: {
  onSubmit: (decision: ReviewDecision, resolution: string) => void;
  submitState: SubmitState;
}) {
  const [decision, setDecision] = useState<ReviewDecision>("approve");
  const [resolution, setResolution] = useState("");

  const trimmed = resolution.trim();
  const canSubmit = trimmed.length > 0 && submitState.kind !== "submitting";

  if (!REVIEWER_ID_IS_CONFIGURED) {
    return (
      <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3">
        <p className="text-sm text-amber-900">
          Set VITE_REVIEWER_ID in frontend/.env to enable review decisions. See
          .env.example.
        </p>
      </div>
    );
  }

  return (
    <form
      className="space-y-3"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit(decision, trimmed);
      }}
    >
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium text-slate-700">Decision</legend>
        <div className="flex flex-wrap gap-2">
          {(Object.keys(DECISION_LABELS) as ReviewDecision[]).map((option) => (
            <label
              key={option}
              className={`cursor-pointer rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors ${
                decision === option
                  ? DECISION_ACTIVE_CLASSES[option]
                  : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
              }`}
            >
              <input
                type="radio"
                name="review-decision"
                value={option}
                checked={decision === option}
                onChange={() => setDecision(option)}
                className="sr-only"
              />
              {DECISION_LABELS[option]}
            </label>
          ))}
        </div>
      </fieldset>

      <div className="space-y-1">
        <label
          htmlFor="review-resolution"
          className="block text-sm font-medium text-slate-700"
        >
          Resolution notes
        </label>
        <textarea
          id="review-resolution"
          rows={3}
          value={resolution}
          onChange={(event) => setResolution(event.target.value)}
          placeholder="Why this decision? This note is stored on the case permanently."
          className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
        />
        {trimmed.length === 0 && (
          <p className="text-xs text-slate-500">
            A note is required — a decision with no stated reasoning is not
            auditable.
          </p>
        )}
      </div>

      {submitState.kind === "error" && (
        <p className="text-sm text-red-700">{submitState.message}</p>
      )}

      <button
        type="submit"
        disabled={!canSubmit}
        className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:bg-slate-300 disabled:hover:bg-slate-300"
      >
        {submitState.kind === "submitting" ? "Submitting…" : "Submit decision"}
      </button>
    </form>
  );
}

/**
 * Review section for one invoice: the actionable summary, sitting directly under
 * the risk score that produced it.
 */
export default function ReviewSection({
  reviewCase,
  onResolve,
  submitState,
}: {
  reviewCase: ReviewCase | null;
  onResolve: (decision: ReviewDecision, resolution: string) => void;
  submitState: SubmitState;
}) {
  if (reviewCase === null) {
    return (
      <div className={CARD_CLASS}>
        <h2 className={HEADING_CLASS}>Review</h2>
        <div className="rounded-lg border border-slate-200 bg-slate-50 px-4 py-3">
          <p className="text-sm text-slate-700">
            No review case. HIGH-risk invoices are flagged automatically by the
            risk assessment.
          </p>
        </div>
      </div>
    );
  }

  const open = isReviewOpen(reviewCase.status);

  // Deliberately no "reviewer approved but the invoice was rejected" note.
  //
  // The API does not return the decision as a field, and it cannot be inferred:
  // `invoice.status === "rejected"` is produced both by a REJECT decision and by
  // an APPROVE decision that hit the terminal-status guard, so the two are
  // indistinguishable from the client. Showing the note would therefore claim a
  // reviewer approved things that were in fact rejected — a confident falsehood
  // in a compliance UI. Silence is the honest rendering until the API exposes
  // the decision directly. A future schema addition is the fix, not a smarter
  // guess.

  return (
    <div className={CARD_CLASS}>
      <h2 className={HEADING_CLASS}>Review</h2>

      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-3">
          <ReviewStatusBadge status={reviewCase.status} />
          <span className="text-xs text-slate-400">
            opened {new Date(reviewCase.created_at).toLocaleString()}
          </span>
        </div>

        <p className="text-sm text-slate-700">{reviewCase.reason}</p>

        {open ? (
          <ResolveForm onSubmit={onResolve} submitState={submitState} />
        ) : (
          <div className="space-y-1 rounded-lg border border-slate-200 bg-slate-50 px-4 py-3">
            <p className="text-sm font-medium text-slate-700">Resolution</p>
            <p className="text-sm text-slate-600">
              {reviewCase.resolution ?? "No resolution recorded."}
            </p>
            {reviewCase.resolved_at !== null && (
              <p className="text-xs text-slate-400">
                closed {new Date(reviewCase.resolved_at).toLocaleString()}
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}