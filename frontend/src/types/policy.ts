/**
 * Policy document and retrieval types.
 *
 * These mirror the backend `PolicyDocumentResponse` / `RetrievalResult` /
 * `PolicyFinding` schemas exactly.
 *
 * `PolicyFinding.rule_id` is deliberately a plain string rather than a union:
 * the backend also returns "NOT_RUN" for an invoice that has never been
 * checked, and narrowing here would make a new rule id a compile error in the
 * frontend for no benefit. Callers that branch on it should handle the unknown
 * case rather than assume an exhaustive set.
 */

export interface PolicyDocument {
  id: string;
  title: string;
  source_filename: string;
  sha256: string;
  is_active: boolean;
  uploaded_at: string;
  updated_at: string;
  chunk_count: number;
}

export interface PolicyListResponse {
  items: PolicyDocument[];
  total: number;
  limit: number;
  offset: number;
}

export interface RetrievedChunk {
  chunk_id: string;
  policy_document_id: string;
  policy_title: string;
  page_number: number;
  position: number;
  text: string;
  similarity: number;
}

export interface RetrievalResult {
  query: string;
  top_k: number;
  chunks: RetrievedChunk[];
  used_provider: string;
}

/** One retrieved policy chunk, with the query that surfaced it. */
export interface PolicyEvidence {
  chunk_id: string;
  policy_document_id: string;
  policy_title: string;
  page_number: number;
  similarity: number;
  /** Verbatim chunk content. Never summarized or rewritten by the backend. */
  text: string;
  retrieval_query: string;
}

export interface PolicyFinding {
  /**
   * "POLICY_EVIDENCE_FOUND" | "NO_APPLICABLE_POLICY" | "NOT_RUN".
   *
   * The last two are different claims and must not be conflated: NOT_RUN means
   * the check was never performed, NO_APPLICABLE_POLICY means it ran and found
   * nothing applicable.
   */
  rule_id: string;
  status: "passed" | "skipped";
  message: string;
  evidence: PolicyEvidence[];
}

/** rule_id returned when an invoice has never been checked. */
export const NOT_RUN_RULE_ID = "NOT_RUN";

/** True when the finding means "we never looked", not "nothing applies". */
export function isNotRun(finding: PolicyFinding): boolean {
  return finding.rule_id === NOT_RUN_RULE_ID;
}