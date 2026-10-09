/**
 * Duplicate detection read types.
 *
 * These mirror the backend `DuplicateReport` / `DuplicateMatch` pydantic
 * models in `app/services/duplicate_detection/types.py` exactly. Scores are
 * 0.0..1.0 fractions; the UI formats them as percentages at the render site.
 * IDs are UUIDs serialized as strings.
 */

export type DuplicateVerdict = "unique" | "suspected_duplicate" | "duplicate";

export type DuplicateSignal =
  | "identical_file"
  | "same_vendor_invoice_number"
  | "same_vendor_amount"
  | "same_po_amount"
  | "line_item_overlap";

export interface DuplicateMatch {
  matching_invoice_id: string;
  score: number;
  signals: DuplicateSignal[];
  verdict: DuplicateVerdict;
}

export interface DuplicateReport {
  invoice_id: string;
  verdict: DuplicateVerdict;
  top_match: DuplicateMatch | null;
  all_matches: DuplicateMatch[];
  notes: string[];
}

/**
 * Note the backend emits when duplicate detection has never run.
 *
 * The read endpoint returns this as the single note of a neutral
 * unique-shaped payload (`verdict: "unique"`, no matches). A genuinely unique
 * invoice says something different ("no duplicate signals found"), so the two
 * are distinguishable.
 */
export const NEVER_RAN_DUPLICATE_NOTE = "duplicate check has not been run";

/** True when the report is the neutral "check has not run yet" payload. */
export function isDuplicateNeverRanReport(report: DuplicateReport): boolean {
  return (
    report.verdict === "unique" &&
    report.top_match === null &&
    report.all_matches.length === 0 &&
    report.notes.includes(NEVER_RAN_DUPLICATE_NOTE)
  );
}
