/**
 * Risk scoring read types.
 *
 * These mirror the backend `RiskAssessment` / `RiskContributor` pydantic models
 * in `app/services/risk/types.py` exactly.
 *
 * Two things are worth knowing before reading them:
 *
 * - `score` is the 0..100 integer the reviewer sees. The `AnalysisResult.score`
 *   column in Postgres stores the same number as a 0.0000..1.0000 fraction, to
 *   match its `Numeric(5, 4)` type. `details.score` — which is what this type
 *   describes — is always the 0..100 form, so no rescaling happens here.
 * - `"unknown"` is not a real verdict. The read endpoint returns a neutral
 *   payload for an invoice that has never been scored, and `"unknown"` is how
 *   that state is distinguished from a genuine `low`/`high` result. Use
 *   {@link isRiskNeverRan} rather than comparing level strings directly.
 */

export type RiskLevel = "low" | "medium" | "high" | "unknown";

export type RecommendedAction = "auto_approve" | "review" | "reject" | "unknown";

export type RiskSeverity = "info" | "warn" | "error";

export interface RiskContributor {
  /** Stable machine-readable id, e.g. "duplicate_finding". */
  rule_id: string;
  category: string;
  /** Points this finding added. Always within `0..cap`. */
  points: number;
  /** Maximum this finding could ever add. */
  cap: number;
  severity: RiskSeverity;
  /** One-sentence human-readable explanation. */
  message: string;
  /** Structured payload behind the finding, JSON-safe. */
  evidence: Record<string, unknown>;
}

export interface RiskAssessment {
  invoice_id: string;
  /** 0..100, or null when the assessment has never run. */
  score: number | null;
  level: RiskLevel;
  recommended_action: RecommendedAction;
  contributors: RiskContributor[];
  /** Sum of contributor points *before* the global cap; may exceed 100. */
  contributing_total: number;
  caps_applied: boolean;
  notes: string[];
}

/**
 * Note the backend emits when risk scoring has never run.
 *
 * Exported so the empty state and a real assessment that happens to mention
 * the same phrasing cannot be confused, and so the copy in the UI has one
 * source of truth.
 */
export const NEVER_RAN_RISK_NOTE = "risk assessment has not been run";

/**
 * True when the payload is the neutral "has not run" placeholder.
 *
 * Requires all three markers rather than any one of them: `score === null`
 * alone would also match a hypothetical future "scored but unreadable"
 * response, and `level === "unknown"` alone is weaker evidence than the
 * combination.
 */
export function isRiskNeverRan(assessment: RiskAssessment): boolean {
  return (
    assessment.score === null &&
    assessment.level === "unknown" &&
    assessment.contributors.length === 0
  );
}