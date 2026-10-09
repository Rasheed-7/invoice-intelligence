import type { PolicyDocument, PolicyListResponse, RetrievalResult } from "../types/policy";

import { apiFetch } from "./client";

/**
 * Uploads a policy PDF.
 *
 * `Content-Type` is intentionally left unset so the browser adds the multipart
 * boundary itself.
 */
export async function uploadPolicy(title: string, file: File): Promise<PolicyDocument> {
  const form = new FormData();
  form.append("title", title);
  form.append("file", file);

  return apiFetch<PolicyDocument>("/policies", { method: "POST", body: form });
}

/**
 * Extracts, chunks and embeds an already-uploaded policy.
 *
 * Separate from upload so it can be retried independently and re-run after a
 * provider change. Idempotent: chunks are replaced, not appended.
 */
export async function processPolicy(id: string): Promise<PolicyDocument> {
  return apiFetch<PolicyDocument>(`/policies/${id}/process`, { method: "POST" });
}

/** Fetches a paginated list of policies, including soft-deleted ones. */
export async function listPolicies(params?: {
  limit?: number;
  offset?: number;
}): Promise<PolicyListResponse> {
  const qs = new URLSearchParams();
  if (params?.limit !== undefined) qs.set("limit", String(params.limit));
  if (params?.offset !== undefined) qs.set("offset", String(params.offset));

  const search = qs.toString();
  const suffix = search ? `?${search}` : "";
  return apiFetch<PolicyListResponse>(`/policies${suffix}`);
}

/** Fetches one policy by id. */
export async function getPolicy(id: string): Promise<PolicyDocument> {
  return apiFetch<PolicyDocument>(`/policies/${id}`);
}

/**
 * Soft-deletes a policy by clearing `is_active`.
 *
 * Chunks and embeddings are retained; retrieval filters on active policies, so
 * a deactivated policy drops out of results immediately.
 */
export async function deletePolicy(id: string): Promise<void> {
  return apiFetch<void>(`/policies/${id}`, { method: "DELETE" });
}

/** Searches embedded policy chunks. `min_similarity` is applied server-side. */
export async function searchPolicies(
  query: string,
  topK = 5,
  minSimilarity?: number,
): Promise<RetrievalResult> {
  return apiFetch<RetrievalResult>("/policies/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      query,
      top_k: topK,
      ...(minSimilarity === undefined ? {} : { min_similarity: minSimilarity }),
    }),
  });
}