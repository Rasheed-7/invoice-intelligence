import type { AnomalyReport } from "../types/anomaly";
import type { DuplicateReport } from "../types/duplicate";
import type { Invoice, InvoiceListResponse } from "../types/invoice";
import type { PurchaseOrderMatchReport } from "../types/po_match";
import type { PolicyFinding } from "../types/policy";
import type { RiskAssessment } from "../types/risk";
import type { ValidationSummary } from "../types/validation";

import { apiFetch } from "./client";

/** Query parameters accepted by the invoice list endpoint. */
export interface ListInvoicesParams {
  limit?: number;
  offset?: number;
  /** Allows the caller to cancel an in-flight page fetch. */
  signal?: AbortSignal;
}

/** Serializes list params into a query string (including the leading `?`). */
function buildQueryString(params?: ListInvoicesParams): string {
  if (!params) {
    return "";
  }
  const search = new URLSearchParams();
  if (params.limit !== undefined) {
    search.set("limit", String(params.limit));
  }
  if (params.offset !== undefined) {
    search.set("offset", String(params.offset));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

/**
 * Uploads a single invoice document as multipart form data.
 *
 * `Content-Type` is intentionally left unset so the browser adds the multipart
 * boundary itself.
 */
export async function uploadInvoice(file: File, signal?: AbortSignal): Promise<Invoice> {
  const formData = new FormData();
  formData.append("file", file);

  return apiFetch<Invoice>("/invoices/upload", {
    method: "POST",
    body: formData,
    signal,
  });
}

/** Fetches a paginated page of invoices. */
export async function listInvoices(params?: ListInvoicesParams): Promise<InvoiceListResponse> {
  const qs = buildQueryString(params);
  return apiFetch<InvoiceListResponse>(`/invoices${qs}`, {
    signal: params?.signal,
  });
}

/** Fetches a single invoice by id. */
export async function getInvoice(id: string): Promise<Invoice> {
  return apiFetch<Invoice>(`/invoices/${id}`);
}

/**
 * Soft-deletes an invoice. Resolves on 204; throws ApiError(404) when the id
 * never existed and ApiError(409) when the invoice is already deleted.
 */
export async function deleteInvoice(id: string): Promise<void> {
  return apiFetch<void>(`/invoices/${id}`, { method: "DELETE" });
}

/** Triggers analysis (extraction + validation) for an invoice and returns the updated invoice. */
export async function analyzeInvoice(id: string): Promise<Invoice> {
  return apiFetch<Invoice>(`/invoices/${id}/analyze`, { method: "POST" });
}

/** Fetches the persisted validation results for an invoice. */
export async function getValidation(id: string): Promise<ValidationSummary> {
  return apiFetch<ValidationSummary>(`/invoices/${id}/validation`);
}

/**
 * Runs every validation check, persists the results, and returns the updated
 * invoice (including its new status).
 */
export async function validateInvoice(id: string): Promise<Invoice> {
  return apiFetch<Invoice>(`/invoices/${id}/validate`, { method: "POST" });
}

/**
 * Runs PO matching, persists the report, and returns the updated invoice
 * (including a possible new status).
 */
export async function matchPurchaseOrder(id: string): Promise<Invoice> {
  return apiFetch<Invoice>(`/invoices/${id}/match-po`, { method: "POST" });
}

/**
 * Fetches the persisted PO match report for an invoice.
 *
 * Returns a neutral skipped-shaped payload when matching has never run, and
 * throws `ApiError(404)` when the invoice itself does not exist.
 */
export async function getPoMatch(id: string): Promise<PurchaseOrderMatchReport> {
  return apiFetch<PurchaseOrderMatchReport>(`/invoices/${id}/po-match`);
}

/**
 * Runs policy retrieval and persists the finding, returning the invoice.
 *
 * The invoice status is not changed by this call - evidence alone does not
 * force review.
 */
export async function runPolicyCheck(id: string): Promise<Invoice> {
  return apiFetch<Invoice>(`/invoices/${id}/policy-check`, { method: "POST" });
}

/**
 * Fetches the persisted policy finding for an invoice.
 *
 * Returns the neutral NOT_RUN finding when the invoice exists but has never
 * been checked, and throws `ApiError(404)` when the invoice does not exist.
 */
export async function getPolicyEvidence(id: string): Promise<PolicyFinding> {
  return apiFetch<PolicyFinding>(`/invoices/${id}/policy-evidence`);
}

/**
 * Runs duplicate detection, persists the finding, and returns the updated
 * invoice (including a possible new status).
 */
export async function checkDuplicates(id: string): Promise<Invoice> {
  return apiFetch<Invoice>(`/invoices/${id}/check-duplicates`, { method: "POST" });
}

/**
 * Fetches the persisted duplicate report for an invoice.
 *
 * Returns a neutral unique-shaped payload when detection has never run, and
 * throws `ApiError(404)` when the invoice itself does not exist.
 */
export async function getDuplicates(id: string): Promise<DuplicateReport> {
  return apiFetch<DuplicateReport>(`/invoices/${id}/duplicates`);
}

/**
 * Runs anomaly detection, persists the finding, and returns the updated
 * invoice (including a possible new status).
 */
export async function checkAnomalies(id: string): Promise<Invoice> {
  return apiFetch<Invoice>(`/invoices/${id}/check-anomalies`, { method: "POST" });
}

/**
 * Fetches the persisted anomaly report for an invoice.
 *
 * Returns a neutral insufficient-history-shaped payload when detection has
 * never run, and throws `ApiError(404)` when the invoice itself does not
 * exist.
 */
export async function getAnomalies(id: string): Promise<AnomalyReport> {
  return apiFetch<AnomalyReport>(`/invoices/${id}/anomalies`);
}

/**
 * Scores the invoice's prior analyses and persists the assessment, returning
 * the updated invoice.
 *
 * Idempotent — a re-run replaces the invoice's prior `risk_score` row rather
 * than appending. The returned status may have changed: a `reject`
 * recommendation sets the terminal `rejected` status, and high risk without
 * one routes the invoice to `needs_review`.
 */
export async function assessRisk(id: string): Promise<Invoice> {
  return apiFetch<Invoice>(`/invoices/${id}/assess-risk`, { method: "POST" });
}

/**
 * Fetches the persisted risk assessment for an invoice.
 *
 * Returns the neutral empty payload when the invoice exists but has never been
 * scored — check {@link isRiskNeverRan} rather than treating it as a result.
 * Throws `ApiError(404)` when the invoice itself does not exist, which is a
 * genuinely different condition.
 */
export async function getRisk(id: string): Promise<RiskAssessment> {
  return apiFetch<RiskAssessment>(`/invoices/${id}/risk`);
}
