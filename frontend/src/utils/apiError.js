// Shared response-error formatter. Used by every component's fetchJSON
// so 422s from Pydantic validators render as readable strings instead
// of "[object Object]".
//
// Pydantic 422 detail shape:
//   { detail: [{ loc: ["body", "field"], msg: "...", type: "..." }, ...] }
//
// Plain-string FastAPI HTTPException detail shape:
//   { detail: "Some error message" }

export function formatApiErrorDetail(body) {
  if (!body) return "";
  const detail = body.detail;
  if (!detail) return "";
  if (Array.isArray(detail)) {
    const lines = detail.slice(0, 5).map((e) => {
      const path =
        Array.isArray(e?.loc) && e.loc.length
          ? (e.loc[0] === "body" ? e.loc.slice(1) : e.loc).join(".") || "field"
          : "field";
      return `${path}: ${e?.msg ?? "invalid"}`;
    });
    let out = lines.join("; ");
    if (detail.length > 5) out += ` (and ${detail.length - 5} more)`;
    return out;
  }
  return String(detail);
}

// Helper for the common fetchJSON shape — read+format the response body
// and throw a Error with a useful message. Caller still owns the fetch.
export async function throwForResponse(r) {
  let detail = "";
  try {
    const body = await r.json();
    detail = formatApiErrorDetail(body);
  } catch { /* response wasn't JSON */ }
  throw new Error(detail ? `HTTP ${r.status}: ${detail}` : `HTTP ${r.status}`);
}
