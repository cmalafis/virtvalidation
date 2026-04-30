#!/usr/bin/env python3
"""Manual smoke test for the SSH collection engine.

This is *not* a pytest test. It's a CLI utility for verifying that
``app.core.ssh.SSHCollector`` can authenticate against a real VM, run the
remote commands, and produce a parseable JSON result.

Quick usage:

    export TEST_VM_HOST=10.0.0.5
    export TEST_VM_USER=virtvalidate
    export SSH_KEY_PATH=backend/app/keys/id_ed25519
    python3 scripts/test_ssh.py

Run with ``--help`` for full argument reference.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import TextIO


# ---------------------------------------------------------------------------
# Import bootstrap.
#
# Make `app.core.ssh` importable whether the script is run from the repo
# root (code lives at backend/app/...) or from inside the backend container
# (WORKDIR=/app, code at /app/app/...). Done before any third-party imports
# so paramiko gets resolved through whichever layout we land in.
# ---------------------------------------------------------------------------
def _bootstrap_imports() -> None:
    # find_spec can raise ModuleNotFoundError when the parent package itself
    # is missing (3.7+ behavior); treat that the same as "not found".
    try:
        if importlib.util.find_spec("app.core.ssh") is not None:
            return
    except ModuleNotFoundError:
        pass

    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent / "backend",  # repo root layout
        Path("/app"),                    # container layout
    ]
    for c in candidates:
        if (c / "app" / "core" / "ssh.py").is_file():
            sys.path.insert(0, str(c))
            return
    print(
        "error: cannot locate app.core.ssh.\n"
        "  Run from the repo root, or from inside the backend container "
        "with /app on PYTHONPATH.",
        file=sys.stderr,
    )
    sys.exit(2)


# Bootstrap is cheap and import-free; the actual app.core.ssh import happens
# inside main() after argparse so `--help` works even if paramiko isn't
# installed in the current interpreter.
_bootstrap_imports()


# ---------------------------------------------------------------------------
# Validation contract.
#
# REQUIRED_TOP_LEVEL is the gate — every key here MUST be present in the
# collector output for the script to exit 0. It mirrors what
# SSHCollector.collect() actually returns today.
#
# ASPIRATIONAL is the broader schema the platform is targeting (processes,
# config hashes, perf metrics, etc.). Missing entries here are reported as
# an informational TODO list but do NOT fail the smoke test. When the
# collector grows new sections, promote keys from ASPIRATIONAL to
# REQUIRED_TOP_LEVEL.
# ---------------------------------------------------------------------------
REQUIRED_TOP_LEVEL: tuple[str, ...] = (
    "meta",
    "services",
    "network",
    "ports",
    "mounts",
    "cron",
)

ASPIRATIONAL: tuple[str, ...] = (
    "storage",
    "processes",
    "users",
    "os_info",
    "performance",
    "config_hashes",
)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="test_ssh.py",
        description=(
            "Manual smoke test for app.core.ssh.SSHCollector. SSHes into a "
            "target VM with the appliance's Ed25519 key, runs the production "
            "collection routine, and validates the resulting JSON shape."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "environment variable fallbacks:\n"
            "  TEST_VM_HOST  — VM hostname or IP (required if --host omitted)\n"
            "  TEST_VM_USER  — SSH username (default: virtvalidate)\n"
            "  TEST_VM_PORT  — SSH port (default: 22)\n"
            "  SSH_KEY_PATH  — Ed25519 private key (default: /app/keys/id_ed25519)\n"
        ),
    )
    p.add_argument(
        "--host",
        default=os.environ.get("TEST_VM_HOST"),
        help="VM IP or hostname. Falls back to TEST_VM_HOST.",
    )
    p.add_argument(
        "--user",
        default=os.environ.get("TEST_VM_USER", "virtvalidate"),
        help="SSH username on the VM. Falls back to TEST_VM_USER, default 'virtvalidate'.",
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("TEST_VM_PORT") or "22"),
        help="SSH port. Falls back to TEST_VM_PORT, default 22.",
    )
    p.add_argument(
        "--key",
        default=os.environ.get("SSH_KEY_PATH", "/app/keys/id_ed25519"),
        help="Path to the Ed25519 private key. Falls back to SSH_KEY_PATH.",
    )
    p.add_argument(
        "--output",
        metavar="PATH",
        help="Write the JSON result to this file instead of stdout.",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress + validation summary on stderr.",
    )
    return p


def _log(stream: TextIO, msg: str = "", *, quiet: bool = False) -> None:
    if not quiet:
        print(msg, file=stream)


def _print_remediation(err_text: str, args: argparse.Namespace) -> None:
    """Best-effort hints for the most common SSH failure modes."""
    msg = err_text.lower()
    print(file=sys.stderr)
    if "key not found" in msg or "no such file" in msg:
        print("  → SSH key missing. Generate one with:", file=sys.stderr)
        print(
            "      ssh-keygen -t ed25519 -N '' -f backend/app/keys/id_ed25519",
            file=sys.stderr,
        )
        print(f"    or set SSH_KEY_PATH/--key to an existing key (currently: {args.key}).", file=sys.stderr)
        return
    if "load ed25519 key" in msg or "failed to load" in msg:
        print("  → Key file exists but could not be parsed. Verify it's an Ed25519", file=sys.stderr)
        print("    private key and not encrypted with a passphrase the appliance", file=sys.stderr)
        print("    cannot supply.", file=sys.stderr)
        return
    if "auth" in msg or "publickey" in msg or "permission denied" in msg:
        print("  → Authentication failed. Verify the public key is in the VM's", file=sys.stderr)
        print(f"    ~{args.user}/.ssh/authorized_keys:", file=sys.stderr)
        print(
            f"      ssh-copy-id -i {args.key}.pub -p {args.port} {args.user}@{args.host}",
            file=sys.stderr,
        )
        print("    If the user needs sudo for systemctl/ss/findmnt, add to sudoers:", file=sys.stderr)
        print(
            f"      {args.user} ALL=(ALL) NOPASSWD: /bin/systemctl, /usr/bin/ss, /bin/findmnt",
            file=sys.stderr,
        )
        return
    if "timed out" in msg or "timeout" in msg:
        print("  → Connection timed out. Check firewall + reachability:", file=sys.stderr)
        print(f"      nc -vz {args.host} {args.port}", file=sys.stderr)
        print("    If the VM is on a private subnet, the appliance may need a", file=sys.stderr)
        print("    route/jump host to reach it.", file=sys.stderr)
        return
    if "host key" in msg or "rejecting" in msg or "missing host key" in msg:
        print("  → Unknown host key. The collector uses RejectPolicy by design.", file=sys.stderr)
        print("    Capture the host key once with:", file=sys.stderr)
        print(
            f"      ssh-keyscan -p {args.port} {args.host} >> ~/.ssh/known_hosts",
            file=sys.stderr,
        )
        return
    print("  → Re-run with --quiet=false (default) for the full traceback path.", file=sys.stderr)


def _validation_summary(
    result: dict,
    *,
    stream: TextIO,
    quiet: bool,
) -> tuple[list[str], list[str]]:
    actual = set(result.keys())
    missing_required = [k for k in REQUIRED_TOP_LEVEL if k not in actual]
    missing_aspirational = [k for k in ASPIRATIONAL if k not in actual]

    _log(stream, quiet=quiet)
    _log(stream, "── validation ──", quiet=quiet)
    for key in REQUIRED_TOP_LEVEL:
        present = key in actual
        marker = "✓" if present else "✗"
        _log(stream, f"  {marker} {key}", quiet=quiet)

    if missing_aspirational:
        _log(stream, quiet=quiet)
        _log(
            stream,
            "  not yet collected (informational — TODO for collector):",
            quiet=quiet,
        )
        for key in missing_aspirational:
            _log(stream, f"    · {key}", quiet=quiet)

    return missing_required, missing_aspirational


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if not args.host:
        parser.error("--host is required (or set TEST_VM_HOST)")

    # Imported here (after argparse) so `--help` works even when the
    # backend deps aren't installed in this interpreter.
    try:
        from app.core.ssh import SSHCollectionError, SSHCollector
    except ModuleNotFoundError as e:
        print(
            f"error: failed to import app.core.ssh ({e}).\n"
            "  Install backend deps first: pip install -r backend/requirements.txt",
            file=sys.stderr,
        )
        return 2

    collector = SSHCollector(key_path=args.key, port=args.port)

    _log(sys.stderr, f"→ connecting to {args.user}@{args.host}:{args.port}", quiet=args.quiet)
    _log(sys.stderr, f"  using key {args.key}", quiet=args.quiet)

    try:
        result = collector.collect(host=args.host, username=args.user)
    except SSHCollectionError as e:
        print(f"\nSSH collection failed: {e}", file=sys.stderr)
        _print_remediation(str(e), args)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001 — last-resort catchall in a CLI utility
        print(f"\nunexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    _log(sys.stderr, "→ collection succeeded", quiet=args.quiet)

    text = json.dumps(result, indent=2, sort_keys=True, default=str)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
        _log(sys.stderr, f"→ JSON written to {args.output}", quiet=args.quiet)
    else:
        print(text)

    missing_required, _ = _validation_summary(result, stream=sys.stderr, quiet=args.quiet)

    if missing_required:
        print(
            f"\nFAIL: missing required top-level keys: {missing_required}",
            file=sys.stderr,
        )
        return 1

    _log(sys.stderr, quiet=args.quiet)
    _log(sys.stderr, "PASS — all required top-level fields present.", quiet=args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
