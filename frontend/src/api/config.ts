/** `VITE_*` env vars are inlined at build time by Vite — see `.env.example`. */
export const API_BASE_URL: string =
  import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000/api/v1'

/** Bounds on the client's own 503 retry loop (Spec v1.0 §10 / PRD M14) —
 * named constants, not magic numbers at the call site, matching this
 * project's own backend convention (kairos/core/constants.py). */
export const SERVICE_UNAVAILABLE_MAX_RETRIES = 3
export const SERVICE_UNAVAILABLE_DEFAULT_RETRY_AFTER_SECONDS = 1
