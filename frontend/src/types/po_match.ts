/**
 * Purchase order <-> invoice matching read types.
 *
 * These mirror the backend `PurchaseOrderMatchReport` pydantic model exactly.
 * Values inside a `FieldMismatch` are serialized from `Decimal | None`, so
 * `po_value` / `invoice_value` / `difference` arrive as strings (or null when
 * the value was missing on one side). Do not parse them to `number` here.
 */

export type MatchOutcome = "match" | "partial_match" | "mismatch" | "skipped";

export type MismatchSeverity = "high" | "medium" | "low";

export interface FieldMismatch {
  field: string;
  po_value: string | null;
  invoice_value: string | null;
  difference: string | null;
  severity: MismatchSeverity;
}

export interface LineMatch {
  invoice_item_id: string | null;
  po_item_id: string | null;
  outcome: MatchOutcome;
  mismatches: FieldMismatch[];
}

export interface PurchaseOrderMatchReport {
  invoice_id: string;
  po_reference: string | null;
  po_found: boolean;
  overall_outcome: MatchOutcome;
  line_matches: LineMatch[];
  unmatched_invoice_items: string[];
  unmatched_po_items: string[];
  mismatch_summary: Record<string, number>;
  vendor_matches: boolean | null;
  total_matches: boolean | null;
  notes: string[];
}

/**
 * Note the backend emits when the invoice has no po_match row yet.
 *
 * The read endpoint returns this as the single note of a neutral skipped-shaped
 * payload. A skipped report produced by an actual run says something different
 * ("no PO reference on invoice"), so the two are distinguishable.
 */
export const NEVER_RAN_NOTE = "no PO match has been run";

/** True when the report is the neutral "matching has not run yet" payload. */
export function isNeverRanReport(report: PurchaseOrderMatchReport): boolean {
  return (
    report.overall_outcome === "skipped" &&
    report.notes.includes(NEVER_RAN_NOTE)
  );
}