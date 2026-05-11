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
//
// JSON request body is auto-stringified + content-type set when the
// caller passes a non-string `body`.

import { formatApiErrorDetail } from "./apiError";

export async function fetchJSON(url, opts = {}) {
  const init = { method: "GET", ...opts };
  if (init.body !== undefined && typeof init.body !== "string") {
    init.headers = { "Content-Type": "application/json", ...(init.headers || {}) };
    init.body = JSON.stringify(init.body);
  }

  let response;
  try {
    response = await fetch(url, init);
  } catch (networkErr) {
    // Distinguishable from HTTP errors so UI can show a "check your
    // connection" message rather than "the server rejected the request."
    throw new Error(`Network error: ${networkErr.message || networkErr}`);
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
