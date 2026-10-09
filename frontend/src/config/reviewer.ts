/**
 * The MVP has no authentication. Reviewer identity comes from an env var so
 * the resolve form can record *who* decided without inventing auth. If unset,
 * the resolve form renders disabled with an explanatory message.
 *
 * This is an honest placeholder, not a stand-in for authentication: every
 * browser hitting this build claims the same identity. It is appropriate for a
 * single-operator demo and is not appropriate for a shared deployment. Wiring a
 * real identity means replacing this constant with a session lookup, not
 * extending it.
 */
export const REVIEWER_ID: string | null = import.meta.env.VITE_REVIEWER_ID ?? null;

export const REVIEWER_ID_IS_CONFIGURED =
  REVIEWER_ID !== null && REVIEWER_ID.trim() !== "";