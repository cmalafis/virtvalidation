"""SIMULATED TrustyAI Guardrails Orchestrator.

This is NOT RHOAI TrustyAI. It is a protocol-faithful stand-in, written
because the Red Hat Developer Sandbox has no GuardrailsOrchestrator CRD and
this namespace cannot create TrustyAIService resources.

What is real: the wire protocol. It serves
``POST /api/v2/chat/completions-detection`` exactly as the FMS-Guardrails
contract specifies, so VirtValidate's ``trustyai`` backend runs completely
unmodified against it — clean calls return a normal OpenAI envelope, and a
detector hit returns a 200 carrying a ``warnings`` array plus structured
``detections``.

What is simulated: the detector's judgement. A real orchestrator runs a
trained classifier; this runs a heuristic pattern set. Every detection
payload therefore carries a ``detector_backend`` disclaimer so the evidence
persisted in VirtValidate's InferenceLog is self-labelling and can never be
mistaken later for a genuine orchestrator result.

Clean requests are proxied to the real MaaS Granite endpoint, so the model
answers are genuine.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("UPSTREAM_BASE_URL", "").rstrip("/")
UPSTREAM_KEY = os.environ.get("UPSTREAM_API_KEY", "")
DISCLAIMER = "SIMULATED - heuristic pattern matcher, not RHOAI TrustyAI"

# Heuristic prompt-injection patterns. A real detector is a trained
# classifier; this is a deliberately legible substitute.
PATTERNS = [
    (r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+instructions", "instruction_override"),
    (r"disregard\s+(all\s+)?(previous|prior|above|earlier)", "instruction_override"),
    (r"forget\s+(everything|all)\s+(you|above)", "instruction_override"),
    (r"(reveal|print|show|output|dump)\s+(your|the)\s+(system\s+)?(prompt|instructions)", "prompt_extraction"),
    (r"you\s+are\s+now\s+", "role_hijack"),
    (r"\bact\s+as\s+(an?\s+)?(admin|root|developer|unrestricted)", "role_hijack"),
    (r"(id_ed25519|id_rsa|private\s+key|/etc/shadow|/etc/passwd)", "credential_exfiltration"),
    (r"(exfiltrate|send|post)\s+.{0,40}\s+to\s+https?://", "credential_exfiltration"),
    (r"curl\s+.{0,60}\|\s*(ba)?sh", "shell_payload"),
    (r"base64\s+-d\s*\|\s*(ba)?sh", "shell_payload"),
    (r"mark\s+(every|all)\s+waves?\s+(as\s+)?approved", "decision_manipulation"),
    (r"(approve|authorize)\s+(everything|all\s+waves)", "decision_manipulation"),
]
COMPILED = [(re.compile(p, re.IGNORECASE), label) for p, label in PATTERNS]


def scan(messages):
    """Return a list of detection records, one per offending message."""
    out = []
    for idx, msg in enumerate(messages or []):
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        results = []
        for rx, label in COMPILED:
            m = rx.search(content)
            if m:
                start = max(0, m.start() - 30)
                results.append(
                    {
                        "detector_id": "prompt_injection",
                        "detection": "INJECTION",
                        "detection_type": label,
                        "score": 0.97,
                        "text": content[start : m.end() + 60].strip(),
                    }
                )
        if results:
            out.append({"message_index": idx, "role": msg.get("role"), "results": results})
    return out


def call_upstream(payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{UPSTREAM}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {UPSTREAM_KEY}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.status, json.loads(resp.read().decode())


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        # Carry the disclaimer at the transport layer too.
        self.send_header("X-Guardrails-Backend", DISCLAIMER)
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        sys.stderr.write("[sim] " + (fmt % args) + "\n")

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", ""):
            self._send(200, {"status": "ok", "detector_backend": DISCLAIMER})
        else:
            self._send(404, {"detail": "not found"})

    def do_POST(self):
        if not self.path.startswith("/api/v2/chat/completions-detection"):
            self._send(404, {"detail": "not found"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode() or "{}")
        except ValueError:
            self._send(400, {"detail": "invalid json"})
            return

        messages = payload.get("messages") or []
        detections = scan(messages)

        if detections:
            types = sorted({r["detection_type"] for d in detections for r in d["results"]})
            self.log_message("DETECTION fired: %s", ", ".join(types))
            self._send(
                200,
                {
                    "id": "chatcmpl-guardrail-block",
                    "object": "chat.completion",
                    "model": payload.get("model"),
                    "choices": [],
                    "warnings": [
                        {
                            "type": "UNSUITABLE_INPUT",
                            "message": (
                                "Unsuitable input detected. Please check the detected "
                                "entities on your input and try again."
                            ),
                        }
                    ],
                    "detections": {
                        "input": detections,
                        "detector_backend": DISCLAIMER,
                    },
                },
            )
            return

        # Clean — proxy to the real model so the answer is genuine.
        upstream_payload = {k: v for k, v in payload.items() if k != "detectors"}
        try:
            status, body = call_upstream(upstream_payload)
        except urllib.error.HTTPError as e:
            self._send(e.code, {"detail": f"upstream HTTP {e.code}"})
            return
        except Exception as e:  # noqa: BLE001 - surface any transport failure
            self._send(502, {"detail": f"upstream error: {type(e).__name__}"})
            return
        self.log_message("clean passthrough -> upstream %s", status)
        self._send(status, body)


if __name__ == "__main__":
    if not UPSTREAM:
        sys.exit("UPSTREAM_BASE_URL is required")
    sys.stderr.write(f"[sim] {DISCLAIMER}\n[sim] upstream={UPSTREAM}\n")
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
