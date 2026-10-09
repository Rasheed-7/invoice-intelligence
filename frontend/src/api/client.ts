/**
 * Thin `fetch` wrapper for the Invoice Intelligence API.
 *
 * No dependencies beyond the browser. All calls go through the Vite dev proxy
 * (`/api/*` -> http://localhost:8000) in development.
 */

/** Base path for the versioned API. */
const API_PREFIX = "/api/v1";

/** Error thrown for non-2xx responses and transport failures. */
export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * Prepends the API prefix to root-relative paths that are not already under
 * `/api`. Absolute URLs and already-prefixed paths are returned unchanged.
 */
function resolvePath(path: string): string {
  if (path.startsWith("/") && !path.startsWith("/api")) {
    return `${API_PREFIX}${path}`;
  }
  return path;
}

/** True for `AbortController.abort()` cancellations (DOMException or Error). */
function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    (error as { name?: unknown }).name === "AbortError"
  );
}

/**
 * Performs a JSON API request.
 *
 * - Root-relative `path`s are prefixed with `/api/v1`.
 * - Non-2xx responses throw `ApiError`, using the JSON `{ detail }` when present.
 * - `204 No Content` resolves to `undefined`.
 * - Transport failures throw `ApiError(0, "network error")`.
 */
export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(resolvePath(path), init);
  } catch (error) {
    // Preserve intentional cancellations so callers can distinguish an aborted
    // request from a genuine connectivity problem.
    if (isAbortError(error)) {
      throw error;
    }
    throw new ApiError(0, "network error");
  }

  if (!response.ok) {
    let detail: string | undefined;
    try {
      const body: unknown = await response.json();
      if (
        typeof body === "object" &&
        body !== null &&
        "detail" in body &&
        typeof (body as { detail?: unknown }).detail === "string"
      ) {
        detail = (body as { detail: string }).detail;
      }
    } catch {
      // Error body was empty or not JSON; fall back to the status text.
    }
    throw new ApiError(response.status, detail || response.statusText);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}
