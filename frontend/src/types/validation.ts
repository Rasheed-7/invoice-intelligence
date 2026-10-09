/**
 * Validation read types.
 *
 * These mirror the backend `ValidationSummaryResponse` / `ValidationCheckResponse`
 * schemas exactly. Check evidence (`details`) arrives as plain JSON, so its
 * values stay `unknown` here and are narrowed at the render site — the UI must
 * never invent data the API didn't return.
 */

export type ValidationStatus = "passed" | "failed" | "warning" | "skipped";

export type CheckSeverity = "info" | "warn" | "error";

export interface ValidationCheck {
  check_name: string;
  status: ValidationStatus;
  message: string;
  severity: CheckSeverity;
  details: Record<string, unknown>;
}

export interface ValidationSummary {
  invoice_id: string;
  overall_status: ValidationStatus;
  checks: ValidationCheck[];
  counts: { passed: number; failed: number; warning: number; skipped: number };
}
