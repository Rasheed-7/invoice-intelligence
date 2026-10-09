/**
 * Anomaly detection read types.
 *
 * These mirror the backend `AnomalyReport` / `FeatureContribution` pydantic
 * models in `app/services/anomaly_detection/types.py` exactly. Decimal-backed
 * values (`current_value`, `historical_mean`, `historical_std`, `z_score`)
 * arrive as strings (or null when absent) so precision survives JSON; they
 * stay strings here and are formatted at the render site. `score` is a
 * 0.0..1.0 fraction (or null when there is no honest score to report) and
 * `raw_decision` is the Isolation Forest decision value (or null when the
 * forest did not run).
 */

export type AnomalyVerdict =
  | "normal"
  | "mild_anomaly"
  | "strong_anomaly"
  | "insufficient_history";

export type AnomalyMethod = "statistical_baseline" | "isolation_forest" | "none";

export type ContributionSeverity = "high" | "medium" | "low";

export interface FeatureContribution {
  feature: string;
  current_value: string;
  historical_mean: string | null;
  historical_std: string | null;
  z_score: string | null;
  severity: ContributionSeverity;
  note: string;
}

export interface AnomalyReport {
  invoice_id: string;
  vendor_name: string | null;
  verdict: AnomalyVerdict;
  method: AnomalyMethod;
  score: number | null;
  raw_decision: number | null;
  contributions: FeatureContribution[];
  history_size: number;
  notes: string[];
}

/**
 * Note the backend emits when anomaly detection has never run.
 *
 * The read endpoint returns this as the single note of a neutral
 * insufficient-history-shaped payload. A genuinely insufficient history after
 * a real run says something different (vendor history counts), so the two are
 * distinguishable.
 */
export const NEVER_RAN_ANOMALY_NOTE = "anomaly check has not been run";

/** True when the report is the neutral "check has not run yet" payload. */
export function isAnomalyNeverRanReport(report: AnomalyReport): boolean {
  return (
    report.verdict === "insufficient_history" &&
    report.method === "none" &&
    report.contributions.length === 0 &&
    report.history_size === 0 &&
    report.notes.includes(NEVER_RAN_ANOMALY_NOTE)
  );
}
