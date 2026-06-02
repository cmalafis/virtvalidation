"""Single-VM collection engine.

Wraps :class:`app.core.ssh.SSHCollector` with:

  - A DB-agnostic input (:class:`VMTarget`) so the orchestrator can drive
    it without coupling to the VM ORM model.
  - A return-value (not raise) failure contract — every collection produces
    a :class:`CollectionResult` with ``succeeded`` set; per-VM failures
    are categorized in :attr:`failure_category` (see :data:`FAILURE_CATEGORIES`).
  - Host key fingerprint capture for the baseline → validation host-key
    delta tracking.

Per-VM failures NEVER raise out of :meth:`CollectionEngine.collect` — a
single misbehaving VM cannot abort the batch. The orchestrator depends
on this guarantee.
"""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import paramiko

from app.core.collection.collector_spec import (
    PASS1_CATALOG_VERSION,
    pass1_data_keys,
    pass1_probe_names,
)
from app.core.ssh import HostKeyPolicy, SSHCollectionError, SSHCollector

logger = logging.getLogger(__name__)


# Canonical failure taxonomy. Stored on Baseline.failure_category /
# VMValidation.failure_category. ``partial`` is for collections that
# completed enough to produce some collected_data but missed enough to
# be flagged for review.
FAILURE_CATEGORIES = (
    "unreachable",
    "auth_failed",
    "host_key_changed",
    "command_failed",
    "partial",
    "timeout",
)


@dataclass(frozen=True)
class VMTarget:
    """Minimal connection identity for one VM. Keeps the engine DB-agnostic."""

    vm_id: int
    host: str
    port: int = 22
    username: str = "virtvalidate"


@dataclass
class CollectionResult:
    vm_id: int
    succeeded: bool
    collected_data: dict | None = None
    host_key_fingerprint: str | None = None
    probe_catalog_version: str = PASS1_CATALOG_VERSION
    probes_run: list[str] = field(default_factory=pass1_probe_names)
    failure_category: str | None = None
    failure_detail: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    duration_ms: int = 0


class CollectionEngine:
    """Deterministic single-VM collector.

    Construction takes a paramiko private key object (loaded by the
    caller via :func:`app.services.ssh_key_service.load_paramiko_key`)
    and timeouts. :meth:`collect` returns a :class:`CollectionResult`
    for the given target — never raises for per-VM failures.
    """

    def __init__(
        self,
        *,
        paramiko_key: paramiko.PKey,
        connect_timeout: float = 10.0,
        command_timeout: float = 30.0,
        host_key_policy: HostKeyPolicy = "auto_accept",
        known_hosts_path: str | None = None,
    ) -> None:
        self._paramiko_key = paramiko_key
        self._connect_timeout = connect_timeout
        self._command_timeout = command_timeout
        self._host_key_policy = host_key_policy
        self._known_hosts_path = known_hosts_path

    def collect(self, target: VMTarget) -> CollectionResult:
        """SSH into ``target`` and return a :class:`CollectionResult`.

        Per-VM failures are returned as a result with ``succeeded=False``
        and a :data:`FAILURE_CATEGORIES` value in ``failure_category``.
        """
        result = CollectionResult(vm_id=target.vm_id, succeeded=False)
        start = time.monotonic()

        if not target.host:
            result.failure_category = "unreachable"
            result.failure_detail = "VM has no resolvable host address"
            result.completed_at = datetime.now(timezone.utc)
            result.duration_ms = 0
            return result

        try:
            collector = self._make_collector()
            try:
                state = collector.collect(host=target.host, username=target.username)
            except SSHCollectionError as e:
                result.failure_category = _categorize_ssh_error(e)
                result.failure_detail = str(e)
                if e.fingerprint:
                    result.host_key_fingerprint = e.fingerprint
                return result
            except socket.timeout as e:
                result.failure_category = "timeout"
                result.failure_detail = f"socket timeout: {e}"
                return result
            except paramiko.AuthenticationException as e:
                # SSHCollector wraps most auth errors as SSHCollectionError,
                # but bare paramiko exceptions still need a category.
                result.failure_category = "auth_failed"
                result.failure_detail = str(e)
                return result
            except Exception as e:  # noqa: BLE001 — per-VM failures must be values, not exceptions
                result.failure_category = "command_failed"
                result.failure_detail = f"{type(e).__name__}: {e}"
                logger.warning(
                    "collection.unexpected_failure vm_id=%s host=%s err=%s",
                    target.vm_id,
                    target.host,
                    e,
                )
                return result

            # Host key fingerprint — captured even on successful collection
            # so validation can detect (expected) post-migration changes.
            evt = collector.last_host_key_event
            if evt and evt.get("fingerprint"):
                result.host_key_fingerprint = evt["fingerprint"]

            # Determine whether every Pass-1 available probe produced its
            # expected key. Missing keys -> partial success (still useful,
            # but flagged for human review).
            missing = pass1_data_keys() - set((state or {}).keys())
            if missing:
                result.failure_category = "partial"
                result.failure_detail = "missing probe data: " + ", ".join(sorted(missing))
                # We still consider this "succeeded" overall because the
                # partial result is useful for validation; the category
                # is what the operator's review surface highlights.
                result.succeeded = True
                # Probes that ran = those that produced a key.
                result.probes_run = [
                    p for p in pass1_probe_names() if _probe_data_key(p) in (state or {})
                ]
            else:
                result.succeeded = True

            result.collected_data = state
            return result
        finally:
            result.completed_at = datetime.now(timezone.utc)
            result.duration_ms = int((time.monotonic() - start) * 1000)

    def _make_collector(self) -> SSHCollector:
        """Build a ``SSHCollector`` that uses our pre-loaded paramiko key.

        Default ``SSHCollector`` loads its key from a file path at the
        start of every ``collect()``. For the orchestrator we want to load
        once (by key_id, via the multi-key service) and reuse — that's
        why we subclass to inject the key.
        """
        return _InjectedKeyCollector(
            paramiko_key=self._paramiko_key,
            timeout=self._connect_timeout,
            command_timeout=self._command_timeout,
            host_key_policy=self._host_key_policy,
            known_hosts_path=self._known_hosts_path,
        )


class _InjectedKeyCollector(SSHCollector):
    """``SSHCollector`` that uses a pre-loaded paramiko key.

    Avoids the per-call disk read + FIPS gate that the default
    ``SSHCollector`` does — the multi-key service already enforces FIPS
    at key generation time, so re-checking on every connect is wasted
    work that also makes mocking awkward.
    """

    def __init__(
        self,
        *,
        paramiko_key: paramiko.PKey,
        port: int = 22,
        timeout: float = 15.0,
        command_timeout: float = 30.0,
        host_key_policy: HostKeyPolicy = "auto_accept",
        known_hosts_path: str | None = None,
    ) -> None:
        # SSHCollector's __init__ wants a key_path — we override _connect
        # so the path is never opened. Set a sentinel path that won't be
        # touched.
        super().__init__(
            key_path="/dev/null",
            port=port,
            timeout=timeout,
            command_timeout=command_timeout,
            host_key_policy=host_key_policy,
            known_hosts_path=known_hosts_path,
        )
        self._injected_key = paramiko_key

    def _connect(self, host: str, username: str):  # type: ignore[override]
        # Reproduce the parent's _connect logic but with the injected key
        # in place of load_private_key + FIPS validation. We use the
        # context-manager helper from the parent class to share the host-
        # key + close semantics.
        from contextlib import contextmanager

        from app.core.ssh import _RecordingAutoAddPolicy

        @contextmanager
        def _conn():
            client = paramiko.SSHClient()
            client.load_system_host_keys()
            if self.known_hosts_path.is_file():
                try:
                    client.load_host_keys(str(self.known_hosts_path))
                except OSError:
                    pass

            self.last_host_key_event = None
            added_keys: list[dict] = []

            if self.host_key_policy == "strict":
                client.set_missing_host_key_policy(paramiko.RejectPolicy())
            else:
                client.set_missing_host_key_policy(_RecordingAutoAddPolicy(added_keys))

            try:
                client.connect(
                    hostname=host,
                    port=self.port,
                    username=username,
                    pkey=self._injected_key,
                    timeout=self.timeout,
                    auth_timeout=self.timeout,
                    banner_timeout=self.timeout,
                    allow_agent=False,
                    look_for_keys=False,
                )
            except paramiko.BadHostKeyException as e:
                from app.core.ssh import _fingerprint_for

                fp = _fingerprint_for(e.key)
                client.close()
                raise SSHCollectionError(
                    f"Host key verification failed for {host}: fingerprint={fp}",
                    kind="host_key_mismatch",
                    host=host,
                    fingerprint=fp,
                ) from e
            except paramiko.AuthenticationException as e:
                client.close()
                raise SSHCollectionError(
                    f"SSH authentication to {host} failed: {e}",
                    kind="auth_failed",
                    host=host,
                ) from e
            except paramiko.SSHException as e:
                client.close()
                raise SSHCollectionError(
                    f"SSH connection to {host} failed: {e}",
                    host=host,
                ) from e
            except OSError as e:
                client.close()
                raise SSHCollectionError(
                    f"SSH connection to {host} failed: {e}",
                    host=host,
                ) from e

            if added_keys:
                self.last_host_key_event = added_keys[0]
            else:
                try:
                    transport = client.get_transport()
                    if transport is not None:
                        key = transport.get_remote_server_key()
                        from app.core.ssh import _fingerprint_for

                        self.last_host_key_event = {
                            "action": "verified",
                            "host": host,
                            "key_type": key.get_name(),
                            "fingerprint": _fingerprint_for(key),
                        }
                except Exception:  # noqa: BLE001 — purely advisory
                    pass

            try:
                yield client
            finally:
                client.close()

        return _conn()


def _probe_data_key(probe_name: str) -> str:
    """Look up the ``data_key`` for a probe by name (small helper for the
    partial-success counter)."""
    from app.core.collection.collector_spec import pass1_spec

    for p in pass1_spec():
        if p.name == probe_name:
            return p.data_key
    return probe_name


def _categorize_ssh_error(err: SSHCollectionError) -> str:
    """Map ``SSHCollectionError.kind`` into the public failure taxonomy."""
    msg_lc = str(err).lower()
    if err.kind == "host_key_mismatch":
        return "host_key_changed"
    if err.kind == "host_key_unknown":
        return "auth_failed"
    if err.kind == "auth_failed":
        return "auth_failed"
    if "authentication" in msg_lc:
        return "auth_failed"
    if "timed out" in msg_lc or "timeout" in msg_lc:
        return "timeout"
    if "unreachable" in msg_lc or "no route" in msg_lc or "connection refused" in msg_lc:
        return "unreachable"
    if "name or service not known" in msg_lc or "could not resolve" in msg_lc:
        return "unreachable"
    return "command_failed"
