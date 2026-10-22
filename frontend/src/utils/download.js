// Save a fetch() Response as a file download.
//
// A bare <a href download> can't see the status: on a 422 the browser
// saves the JSON error body as "the YAML". Callers fetch() inline (so the
// architecture-doc generator still sees the /api/ literal) and hand the
// Response here, which throws the backend's error text on a non-2xx and
// otherwise names the file from Content-Disposition.

import { throwForResponse } from "./apiError";

function filenameFrom(res, fallback) {
  const cd = res?.headers?.get?.("Content-Disposition") ?? "";
  const m = /filename="?([^";]+)"?/i.exec(cd);
  return m?.[1] || fallback;
}

export async function saveResponseAsFile(res, fallbackName) {
  if (!res.ok) await throwForResponse(res);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filenameFrom(res, fallbackName);
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
