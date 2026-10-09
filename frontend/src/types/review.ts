/**
 * Review case read/write types.
 *
 * These mirror the backend `ReviewCaseResponse` / `ReviewListResponse` schemas in
 * `app/schemas/review.py`.
 *
 * Two deliberate simplifications on the frontend:
 *
 * - `ReviewStatus` and `ReviewDecision` are separate unions even though they
 *   overlap ("dismissed" vs "dismiss"). Mixing them would let the compiler accept
 *   a decision the API rejects.
 * - `InvoiceSummary.total_amount` is a `string`, not a number. The backend
 *   serialises it as a string to preserve `Decimal` precision, so parsing it here
 *   would reintroduce exactly the float error the string avoids.
 */

export type ReviewStatus = "pending" | "in_progress" | "resolved" | "dismissed";

export type ReviewDecision = "approve" | "reject" | "dismiss";

export interface InvoiceSummary {
  id: string;
  vendor_name: string | null;
  invoice_number: string | null;
  /** Serialised Decimal, e.g. "4820.0000". Kept as a string for precision. */
  total_amount: string | null;
  currency: string | null;
  status: string;
}

export interface ReviewCase {
  id: string;
  invoice_id: string;
  status: ReviewStatus;
  assigned_to: string | null;
  reason: string;
  resolution: string | null;
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
  invoice_summary: InvoiceSummary;
}

export interface ReviewListResponse {
  items: ReviewCase[];
  /** Total matching the filter, not the page size. */
  total: number;
  limit: number;
  offset: number;
}

export interface ReviewResolutionRequest {
  decision: ReviewDecision;
  reviewer_id: string;
  resolution: string;
}

/** True while a case is still awaiting a decision. */
export function isReviewOpen(status: ReviewStatus): boolean {
  return status === "pending" || status === "in_progress";
}