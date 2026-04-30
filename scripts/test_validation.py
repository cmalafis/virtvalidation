#!/usr/bin/env python3
"""End-to-end smoke test for the validation pipeline.

Captures a "pre-migration" baseline from one VM, optionally pauses so the
operator can hand-modify the VM (stop a service, remove a cron entry,
etc.), captures a "post-migration" current state, then hands both to
``app.core.llm.LLMClient`` for a real verdict from the local Ollama
model.

Three artifacts land in the output directory:

    baseline.json   — raw SSHCollector dump from the source VM
    current.json    — raw SSHCollector dump from the target VM
    verdict.json    — full LLM verdict (status + findings + remediation + diff)

Run with ``--help`` for the full argument reference.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import TextIO


# ---------------------------------------------------------------------------
# Import bootstrap — same dual-layout strategy as scripts/test_ssh.py.
# ---------------------------------------------------------------------------
def _bootstrap_imports() -> None:
    try:
        if importlib.util.find_spec("app.core.ssh") is not None:
            return
    except ModuleNotFoundError:
        pass

    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent / "backend",
        Path("/app"),
    ]
    for c in candidates:
        if (c / "app" / "core" / "ssh.py").is_file():
            sys.path.insert(0, str(c))
            return
    print(
        "error: cannot locate app.core — run from the repo root or from "
        "inside the backend container with /app on PYTHONPATH.",
        file=sys.stderr,
    )
    sys.exit(2)


_bootstrap_imports()


# Required keys per the SSHCollector contract today. If the source/target
# capture is missing one, the LLM diff will be partial; surface this loudly
# rather than silently produce a useless verdict.
REQUIRED_BASELINE_KEYS: tuple[str, ...] = (
    "meta",
    "services",
    "network",
    "ports",
    "mounts",
    "cron",
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="test_validation.py",
        description=(
            "End-to-end smoke test for the validation pipeline: capture a "
            "baseline, capture a current state, and ask the local LLM for "
            "a verdict. SOURCE and TARGET can be the same host — when they "
            "are, --pause-between-captures lets you simulate a 'migration' "
            "by hand-editing the VM between captures."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "environment variable fallbacks:\n"
            "  TEST_VM_HOST                      — used for both source/target if neither given\n"
            "  TEST_VM_USER                      — default user (default: virtvalidate)\n"
            "  TEST_VM_PORT                      — default port (default: 22)\n"
            "  SSH_KEY_PATH                      — Ed25519 key (default: /app/keys/id_ed25519)\n"
            "  OLLAMA_HOST                       — Ollama URL (default: http://ollama:11434)\n"
            "  OLLAMA_MODEL                      — model name (default: llama3:8b)\n"
        ),
    )

    src = p.add_argument_group("source VM (pre-migration baseline)")
    src.add_argument("--source-host", default=os.environ.get("TEST_VM_HOST"))
    src.add_argument("--source-user", default=os.environ.get("TEST_VM_USER", "virtvalidate"))
    src.add_argument(
        "--source-port", type=int, default=int(os.environ.get("TEST_VM_PORT") or "22")
    )

    tgt = p.add_argument_group("target VM (post-migration current state)")
    tgt.add_argument(
        "--target-host",
        default=os.environ.get("TEST_VM_HOST"),
        help="Defaults to --source-host when omitted.",
    )
    tgt.add_argument("--target-user", default=os.environ.get("TEST_VM_USER", "virtvalidate"))
    tgt.add_argument(
        "--target-port", type=int, default=int(os.environ.get("TEST_VM_PORT") or "22")
    )

    common = p.add_argument_group("connection + LLM")
    common.add_argument(
        "--key", default=os.environ.get("SSH_KEY_PATH", "/app/keys/id_ed25519"),
        help="Path to the Ed25519 private key.",
    )
    common.add_argument(
        "--role", default="unspecified",
        help="VM role hint for the LLM (e.g. database, app, lb). Default: unspecified.",
    )
    common.add_argument(
        "--llm-timeout", type=float, default=120.0,
        help="LLM request timeout in seconds. Bump if the model is slow on CPU. Default: 120.",
    )

    flow = p.add_argument_group("flow")
    flow.add_argument(
        "--pause-between-captures",
        dest="pause",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Prompt before the second capture so you can hand-edit the VM "
            "to simulate migration drift. Disable with --no-pause-between-captures "
            "for fully unattended runs."
        ),
    )
    flow.add_argument(
        "--output-dir", default="./test_output",
        help="Directory for baseline.json / current.json / verdict.json. Created if missing.",
    )
    flow.add_argument(
        "--quiet", action="store_true",
        help="Suppress progress + per-step summaries on stderr. Verdict still prints.",
    )

    return p


def _log(stream: TextIO, msg: str = "", *, quiet: bool = False) -> None:
    if not quiet:
        print(msg, file=stream)


def _summarize_capture(name: str, state: dict, *, quiet: bool) -> None:
    """One-line "captured X services, Y interfaces, Z mounts" report."""
    services = len(state.get("services") or [])
    interfaces = len((state.get("network") or {}).get("interfaces") or {})
    ports = len(state.get("ports") or [])
    mounts = len(state.get("mounts") or [])
    cron = state.get("cron") or {}
    user_crons = len(cron.get("user_crontabs") or {})
    sys_crons = len(cron.get("system") or [])
    _log(
        sys.stderr,
        f"  → {name}: {services} services, {interfaces} interfaces, {ports} ports, "
        f"{mounts} mounts, {user_crons} user crontabs + {sys_crons} system cron files",
        quiet=quiet,
    )


def _check_required_keys(name: str, state: dict) -> list[str]:
    return [k for k in REQUIRED_BASELINE_KEYS if k not in state]


def _explain_llm_error(err: Exception, *, llm_host: str, model: str, timeout: float) -> None:
    msg = str(err).lower()
    print(file=sys.stderr)
    if "connection" in msg or "refused" in msg or "name or service" in msg or "could not" in msg:
        print(f"  → Cannot reach Ollama at {llm_host}.", file=sys.stderr)
        print("    Verify the container is up and the port is exposed:", file=sys.stderr)
        print("      podman ps | grep ollama", file=sys.stderr)
        print("      podman logs $(podman ps -q --filter name=ollama) | tail", file=sys.stderr)
        print("    Override the URL with: OLLAMA_HOST=http://localhost:11434", file=sys.stderr)
        return
    if "timeout" in msg or "timed out" in msg or "read timeout" in msg:
        print(f"  → LLM call exceeded the {timeout:.0f}s timeout.", file=sys.stderr)
        print("    Re-run with a larger budget, e.g. --llm-timeout 300.", file=sys.stderr)
        print(f"    A smaller model also helps; current OLLAMA_MODEL={model}.", file=sys.stderr)
        print("    Try llama3:8b → llama3.2:3b on CPU-only hosts.", file=sys.stderr)
        return
    if "empty message" in msg or "non-json" in msg or "not valid json" in msg:
        print("  → Model returned malformed output. Common causes:", file=sys.stderr)
        print("      - Model not pulled yet:  podman exec -it ollama ollama pull " + model, file=sys.stderr)
        print("      - Model hallucinated past the JSON schema. Re-run; lower temp is on.", file=sys.stderr)
        return
    if "invalid verdict status" in msg:
        print("  → Model returned a status outside pass|warn|fail.", file=sys.stderr)
        print("    Usually means the model is too small to follow the schema. Try llama3:8b.", file=sys.stderr)
        return
    print("  → Unhandled LLM failure. Inspect the verdict.json (if written) for clues.", file=sys.stderr)


# ---------------------------------------------------------------------------
# Verdict pretty-printer
# ---------------------------------------------------------------------------
STATUS_LABEL = {"pass": "HEALTHY", "warn": "DEGRADED", "fail": "FAILED"}
SEVERITY_ORDER = {"critical": 0, "warn": 1, "info": 2}


def _print_verdict(verdict: dict) -> None:
    status = verdict.get("status", "unknown")
    label = STATUS_LABEL.get(status, status.upper())
    summary = verdict.get("summary", "") or "(no summary returned)"
    findings = verdict.get("findings") or []
    remediation = verdict.get("remediation") or []

    print()
    print("══════ VALIDATION VERDICT ══════")
    print(f"  status     : {label}  ({status})")
    print(f"  summary    : {summary}")
    print(f"  findings   : {len(findings)}")
    print(f"  remediation: {len(remediation)} step(s)")
    print()

    if findings:
        print("── findings ──")
        ordered = sorted(findings, key=lambda f: SEVERITY_ORDER.get(f.get("severity"), 99))
        for i, f in enumerate(ordered, start=1):
            sev = (f.get("severity") or "?").upper()
            cat = f.get("category") or "?"
            msg = f.get("message") or ""
            print(f"  [{i:>2}] [{sev:<8}] [{cat:<8}] {msg}")
        print()

    if remediation:
        print("── remediation ──")
        for r in remediation:
            step = r.get("step") or "?"
            action = r.get("action") or ""
            cmd = r.get("command")
            print(f"  {step}. {action}")
            if cmd:
                print(f"       $ {cmd}")
        print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    # Source must be set; target falls back to source for single-VM testing.
    if not args.source_host:
        parser.error("--source-host is required (or set TEST_VM_HOST)")
    if not args.target_host:
        args.target_host = args.source_host
        args.target_user = args.target_user or args.source_user
        args.target_port = args.target_port or args.source_port

    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Imports deferred so --help works without backend deps installed.
    try:
        from app.core.llm import LLMClient, LLMError
        from app.core.ssh import SSHCollectionError, SSHCollector
    except ModuleNotFoundError as e:
        print(
            f"error: failed to import app.core ({e}).\n"
            "  Install backend deps first: pip install -r backend/requirements.txt",
            file=sys.stderr,
        )
        return 2

    collector = SSHCollector(key_path=args.key, port=args.source_port)
    # SSHCollector binds port at construction; re-instantiate for the target
    # if the port differs so the same key_path is reused.
    target_collector = (
        collector
        if args.target_port == args.source_port
        else SSHCollector(key_path=args.key, port=args.target_port)
    )

    same_host = (
        args.source_host == args.target_host
        and args.source_port == args.target_port
        and args.source_user == args.target_user
    )

    _log(sys.stderr, "── VirtValidate validation smoke test ──", quiet=args.quiet)
    _log(sys.stderr, f"  source : {args.source_user}@{args.source_host}:{args.source_port}", quiet=args.quiet)
    _log(sys.stderr, f"  target : {args.target_user}@{args.target_host}:{args.target_port}", quiet=args.quiet)
    _log(sys.stderr, f"  key    : {args.key}", quiet=args.quiet)
    _log(sys.stderr, f"  output : {out_dir}", quiet=args.quiet)
    _log(sys.stderr, f"  role   : {args.role}", quiet=args.quiet)
    if same_host:
        _log(
            sys.stderr,
            "  note   : source == target — single-VM mode; pause lets you simulate drift",
            quiet=args.quiet,
        )
    _log(sys.stderr, quiet=args.quiet)

    # ------------ STEP 1: capture source baseline ------------
    _log(sys.stderr, "[1/4] capturing SOURCE baseline…", quiet=args.quiet)
    t0 = time.monotonic()
    try:
        baseline = collector.collect(host=args.source_host, username=args.source_user)
    except SSHCollectionError as e:
        print(f"\nbaseline capture failed: {e}", file=sys.stderr)
        print("  → Run scripts/test_ssh.py against the source VM first.", file=sys.stderr)
        return 1
    dur = time.monotonic() - t0
    _summarize_capture("baseline", baseline, quiet=args.quiet)
    _log(sys.stderr, f"  duration: {dur:.1f}s", quiet=args.quiet)

    missing = _check_required_keys("baseline", baseline)
    if missing:
        print(
            f"\nbaseline is missing required keys {missing}. "
            "Re-run scripts/test_ssh.py to debug the SSH collector first.",
            file=sys.stderr,
        )
        return 1

    baseline_path = out_dir / "baseline.json"
    baseline_path.write_text(
        json.dumps(baseline, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    _log(sys.stderr, f"  saved   : {baseline_path}", quiet=args.quiet)
    _log(sys.stderr, quiet=args.quiet)

    # ------------ STEP 2: optional pause for hand-edits ------------
    if args.pause:
        _log(sys.stderr, "[2/4] pause for manual changes", quiet=args.quiet)
        prompt = (
            "      Make any changes to the VM you want to test detection on\n"
            "      (stop a service, remove a cron job, change DNS, etc.),\n"
            "      then press Enter to capture post-migration state… "
        )
        try:
            # Always read from the controlling tty/stdin even with --quiet so
            # automation doesn't accidentally hang silently.
            input(prompt)
        except EOFError:
            print(
                "\nstdin closed before pause completed — re-run with "
                "--no-pause-between-captures for non-interactive use.",
                file=sys.stderr,
            )
            return 1
        _log(sys.stderr, quiet=args.quiet)
    else:
        _log(sys.stderr, "[2/4] pause disabled (--no-pause-between-captures)", quiet=args.quiet)
        _log(sys.stderr, quiet=args.quiet)

    # ------------ STEP 3: capture target current state ------------
    _log(sys.stderr, "[3/4] capturing TARGET current state…", quiet=args.quiet)
    t0 = time.monotonic()
    try:
        current = target_collector.collect(host=args.target_host, username=args.target_user)
    except SSHCollectionError as e:
        print(f"\ncurrent-state capture failed: {e}", file=sys.stderr)
        print("  → The baseline was captured; only the target SSH leg failed.", file=sys.stderr)
        print(f"     baseline.json is preserved at {baseline_path}.", file=sys.stderr)
        return 1
    dur = time.monotonic() - t0
    _summarize_capture("current ", current, quiet=args.quiet)
    _log(sys.stderr, f"  duration: {dur:.1f}s", quiet=args.quiet)

    missing = _check_required_keys("current", current)
    if missing:
        print(f"\ncurrent state is missing required keys {missing}.", file=sys.stderr)
        return 1

    current_path = out_dir / "current.json"
    current_path.write_text(
        json.dumps(current, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    _log(sys.stderr, f"  saved   : {current_path}", quiet=args.quiet)
    _log(sys.stderr, quiet=args.quiet)

    # ------------ STEP 4: ask the LLM for a verdict ------------
    llm = LLMClient(timeout=args.llm_timeout)
    _log(sys.stderr, f"[4/4] LLM verdict ({llm.host}, {llm.model})…", quiet=args.quiet)
    t0 = time.monotonic()
    try:
        verdict = llm.validate(baseline=baseline, current_state=current, vm_role=args.role)
    except LLMError as e:
        print(f"\nLLM validation failed: {e}", file=sys.stderr)
        _explain_llm_error(e, llm_host=llm.host, model=llm.model, timeout=args.llm_timeout)
        return 1
    dur = time.monotonic() - t0
    _log(sys.stderr, f"  duration: {dur:.1f}s", quiet=args.quiet)

    verdict_path = out_dir / "verdict.json"
    verdict_path.write_text(
        json.dumps(verdict, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    _log(sys.stderr, f"  saved   : {verdict_path}", quiet=args.quiet)

    _print_verdict(verdict)

    # Exit code mirrors the verdict so this script slots into shell pipelines.
    status = verdict.get("status")
    if status == "fail":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
