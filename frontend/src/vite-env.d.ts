/// <reference types="vite/client" />

/**
 * Ambient types for Vite-injected globals.
 *
 * Without this reference `import.meta.env` is untyped, so `VITE_*` variables
 * read as `any` and a typo like `VITE_REVIWER_ID` would compile silently and
 * resolve to undefined at runtime. `vite` is already a dependency; this only
 * points TypeScript at the types it ships.
 */
interface ImportMetaEnv {
  readonly VITE_REVIEWER_ID?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}