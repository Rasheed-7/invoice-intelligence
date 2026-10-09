import type {
  ReviewCase,
  ReviewListResponse,
  ReviewResolutionRequest,
  ReviewStatus,
} from "../types/review";

import { apiFetch } from "./client";

/** Query parameters accepted by the review list endpoint. */
export interface ListReviewsParams {
  status?: ReviewStatus;
  limit?: number;
  offset?: number;
}

/** Serializes list params into a query string, including the leading `?`. */
function buildQueryString(params?: ListReviewsParams): string {
  if (!params) {
    return "";
  }
  const search = new URLSearchParams();
  if (params.status !== undefined) {
    search.set("status", params.status);
  }
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
 * Fetches a page of review cases, newest first.
 *
 * Throws `ApiError(422)` when `status` names a status the backend does not know.
 */
export async function listReviews(
  params?: ListReviewsParams
): Promise<ReviewListResponse> {
  return apiFetch<ReviewListResponse>(`/reviews${buildQueryString(params)}`);
}

/**
 * Fetches a single review case.
 *
 * Throws `ApiError(404)` when no case has that id.
 */
export async function getReview(id: string): Promise<ReviewCase> {
  return apiFetch<ReviewCase>(`/reviews/${id}`);
}

/**
 * Submits a decision and closes the case.
 *
 * Throws `ApiError(404)` for an unknown case, `ApiError(409)` when the case was
 * already resolved by someone else, and `ApiError(422)` when `reviewer_id` names
 * a user that does not exist.
 */
export async function resolveReview(
  id: string,
  body: ReviewResolutionRequest
): Promise<ReviewCase> {
  return apiFetch<ReviewCase>(`/reviews/${id}/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}