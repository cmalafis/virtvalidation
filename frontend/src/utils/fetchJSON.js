// Shared fetch helper. Every component used to copy a slightly
// different version of this; consolidating here so error handling
// stays consistent across the UI.
//
// Behavior:
//   - Network failures (DNS, refused connection, CORS) → throw a
//     readable Error with the "Network error:" prefix.
//   - Non-OK HTTP responses → throw with "HTTP <status>: <detail>".
//     Detail extraction:
//       * Pydantic 422 array → flatten to "field.path: msg" (first 5)
//       * Plain {detail: "..."} → use as-is
//       * Other JSON → first 200 chars of body for diagnostics
//       * Non-JSON body → first 200 chars of text
//       * Empty body → response.statusText
//   - 204 responses → return null.
//   - 2xx responses → parsed JSON.
//   - Requests that hang → aborted after `timeoutMs` (default 60s) and
//     thrown as an Error with `isTimeout === true`.
//
// JSON request body is auto-stringified + content-type set when the
// caller passes a non-string `body`. A `FormData` body (file upload) is
// passed through untouched so the browser sets the multipart boundary.
//
// Options beyond fetch's own: `timeoutMs` (0 disables). A caller-supplied
// `signal` still works and composes with the timeout.

import { formatApiErrorDetail } from "./apiError";

// A request that never settles is worse than one that fails: the caller
// sits on a loading skeleton forever with no error state and no retry.
// That is not hypothetical — the dev proxy holds a connection open
// indefinitely when the backend is down, and nginx in production is
// configured with `proxy_read_timeout 600s`, so a wedged backend would
// spin the UI for ten minutes.
//
// 60s is well above the slowest legitimate call (LLM-backed plan
// generation and bulk validation are async-with-polling, so no single
// request should approach it).
const DEFAULT_TIMEOUT_MS = 60000;

export async function fetchJSON(url, opts = {}) {
  const { timeoutMs = DEFAULT_TIMEOUT_MS, signal: callerSignal, ...rest } = opts;
  const init = { method: "GET", ...rest };
  const isForm = typeof FormData !== "undefined" && init.body instanceof FormData;
  if (init.body !== undefined && typeof init.body !== "string" && !isForm) {
    init.headers = { "Content-Type": "application/json", ...(init.headers || {}) };
    init.body = JSON.stringify(init.body);
  }

  const controller = new AbortController();
  init.signal = controller.signal;

  // Respect a caller-supplied signal too — whichever aborts first wins.
  const onCallerAbort = () => controller.abort(callerSignal?.reason);
  if (callerSignal) {
    if (callerSignal.aborted) controller.abort(callerSignal.reason);
    else callerSignal.addEventListener("abort", onCallerAbort, { once: true });
  }

  let timedOut = false;
  const timer =
    timeoutMs > 0
      ? setTimeout(() => {
          timedOut = true;
          controller.abort();
        }, timeoutMs)
      : null;

  let response;
  try {
    response = await fetch(url, init);
  } catch (networkErr) {
    if (timedOut) {
      const err = new Error(
        `Request timed out after ${Math.round(timeoutMs / 1000)}s: ${url}`,
      );
      err.isTimeout = true;
      throw err;
    }
    // A caller-initiated abort (component unmounted, superseded request)
    // is not an error condition — re-throw it as-is so `useEffect`
    // cleanup paths can ignore it by name.
    if (networkErr?.name === "AbortError") throw networkErr;
    // Distinguishable from HTTP errors so UI can show a "check your
    // connection" message rather than "the server rejected the request."
    throw new Error(`Network error: ${networkErr.message || networkErr}`);
  } finally {
    if (timer) clearTimeout(timer);
    callerSignal?.removeEventListener?.("abort", onCallerAbort);
  }

  if (!response.ok) {
    let detail = "";
    try {
      const body = await response.json();
      detail = formatApiErrorDetail(body);
      if (!detail) {
        // Some endpoints emit non-`detail` error shapes (e.g.,
        // {error: "..."}). Surface the raw body so the operator can
        // diagnose without round-tripping to the server logs.
        detail = JSON.stringify(body).slice(0, 200);
      }
    } catch {
      try {
        detail = (await response.text()).slice(0, 200);
      } catch {
        detail = response.statusText;
      }
    }
    const err = new Error(`HTTP ${response.status}: ${detail || response.statusText}`);
    err.status = response.status;
    err.detail = detail;
    throw err;
  }

  if (response.status === 204) return null;
  return await response.json();
}
