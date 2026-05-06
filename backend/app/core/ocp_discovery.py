"""OCP target cluster resource discovery.

VirtValidate doesn't ship the Kubernetes Python client because adding
that dep would mean keeping its version in lockstep with whatever
flavor of OCP each customer is on. Instead we talk to the cluster's
HTTP REST API directly via httpx (already a dep).

The API surface exposed here is intentionally narrow — exactly the
endpoints VirtValidate needs to populate the mapping editor:

  - StorageClasses (storage.k8s.io/v1)
  - NetworkAttachmentDefinitions (k8s.cni.cncf.io/v1) cluster-wide
  - ClusterUserDefinedNetworks (k8s.ovn.org/v1) when the CRD exists
  - UserDefinedNetworks (k8s.ovn.org/v1) when the CRD exists
  - Namespaces (core/v1, system namespaces filtered)
  - Nodes (core/v1) for capacity sums
  - Cluster version (config.openshift.io/v1) when the OCP CRD is present
  - MTV provider list (forklift.konveyor.io/v1beta1) for sanity check

For tests, the entire client is mockable at one method:
:meth:`OCPDiscoveryClient.discover_all`. Tests inject a fake client
into the API layer; we don't try to mock the K8s API surface.

This module is **read-only** — no resource creation or mutation. The
operator's MTV/Forklift install actually runs the migration; we only
read what's there.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


# Kubernetes' default system namespaces. The mapping editor filters
# these out so operators don't accidentally migrate a VM into kube-system.
# This isn't security — operators can still type any namespace they
# want in the form. It's just so the dropdown doesn't surface 50
# meaningless options on a fresh cluster.
SYSTEM_NAMESPACE_PREFIXES = (
    "kube-",
    "openshift-",
    "default",  # exact match, but startswith handles it
    "open-cluster-management",
)


@dataclass
class DiscoveryResult:
    """What ``discover_all`` returns. Each list is exactly the shape
    the schemas in :mod:`app.schemas.target` expect, so the API layer
    can store the raw lists straight onto the OCPTarget row."""

    storage_classes: list[dict]
    network_attachments: list[dict]
    namespaces: list[dict]
    cluster_capacity: dict
    mtv_namespace: Optional[str]
    ocp_version: Optional[str]
    kubernetes_version: Optional[str]


class OCPDiscoveryError(RuntimeError):
    """Raised when the cluster is unreachable, rejects auth, or returns
    a malformed response. The API layer maps this to 502 (the operator's
    network/auth is wrong) or 503 (the cluster is down)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        endpoint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.endpoint = endpoint


# ---------------------------------------------------------------------------
# Discovery client
# ---------------------------------------------------------------------------
class OCPDiscoveryClient:
    """HTTP-REST client for OCP discovery.

    Constructed with an ``api_endpoint`` (e.g. ``https://api.cluster:6443``)
    and a bearer token. The same connection is used across every
    discovery call within one ``discover_all`` invocation; httpx
    handles connection pooling.

    Failure modes:
      - Network error (DNS / connection refused / timeout) → ``OCPDiscoveryError``
      - 401/403 → ``OCPDiscoveryError`` with status_code surfaced
      - Missing CRD (e.g. the cluster doesn't have CUDN installed) →
        treated as "no resources", not an error
    """

    def __init__(
        self,
        api_endpoint: str,
        bearer_token: str,
        *,
        verify_ssl: bool = True,
        timeout: float = 15.0,
    ) -> None:
        self.api_endpoint = api_endpoint.rstrip("/")
        self.bearer_token = bearer_token
        self.verify_ssl = verify_ssl
        self.timeout = timeout

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.api_endpoint,
            verify=self.verify_ssl,
            timeout=self.timeout,
            headers={"Authorization": f"Bearer {self.bearer_token}"},
        )

    def _get(self, client: httpx.Client, path: str) -> dict | None:
        """GET one Kubernetes API path. Returns None when the resource
        isn't installed (404 on the CRD/list endpoint), raises on
        anything else."""
        try:
            resp = client.get(path)
        except httpx.HTTPError as e:
            raise OCPDiscoveryError(
                f"Network error reaching {self.api_endpoint}{path}: {e}",
                endpoint=path,
            ) from e
        if resp.status_code == 404:
            # Missing CRD or list endpoint not registered. Common for
            # CUDN/UDN on older clusters. Treat as "no resources".
            logger.info("Discovery: %s returned 404; treating as empty.", path)
            return None
        if resp.status_code in (401, 403):
            raise OCPDiscoveryError(
                f"Authentication failed at {path}: HTTP {resp.status_code}",
                status_code=resp.status_code,
                endpoint=path,
            )
        if resp.status_code >= 400:
            raise OCPDiscoveryError(
                f"Cluster returned HTTP {resp.status_code} for {path}: {resp.text[:200]}",
                status_code=resp.status_code,
                endpoint=path,
            )
        try:
            return resp.json()
        except ValueError as e:
            raise OCPDiscoveryError(
                f"Non-JSON response from {path}: {e}", endpoint=path
            ) from e

    # ---------- per-resource methods (each returns the schema-shaped list) ----

    def discover_storage_classes(self, client: httpx.Client) -> list[dict]:
        body = self._get(client, "/apis/storage.k8s.io/v1/storageclasses") or {}
        out: list[dict] = []
        for sc in body.get("items", []) or []:
            metadata = sc.get("metadata") or {}
            annotations = metadata.get("annotations") or {}
            is_default = (
                annotations.get("storageclass.kubernetes.io/is-default-class") == "true"
                or annotations.get("storageclass.beta.kubernetes.io/is-default-class") == "true"
            )
            access_modes = []
            allowed = sc.get("allowedTopologies") or []
            # AccessModes aren't on the StorageClass spec itself — they
            # surface from the CSI driver capabilities. We can't tell
            # for sure without a PVC test; report what's commonly
            # implied by the provisioner name.
            provisioner = sc.get("provisioner") or ""
            if "rbd" in provisioner.lower() or "ceph" in provisioner.lower():
                access_modes = ["ReadWriteOnce", "ReadWriteMany"]
            else:
                access_modes = ["ReadWriteOnce"]
            out.append(
                {
                    "name": metadata.get("name") or "",
                    "provisioner": provisioner,
                    "is_default": bool(is_default),
                    "access_modes": access_modes,
                    "reclaim_policy": sc.get("reclaimPolicy"),
                    "volume_binding_mode": sc.get("volumeBindingMode"),
                }
            )
        return out

    def discover_network_attachments(self, client: httpx.Client) -> list[dict]:
        out: list[dict] = []
        # NetworkAttachmentDefinitions — cluster-wide, then we record
        # the namespace each is in.
        nads = (
            self._get(
                client, "/apis/k8s.cni.cncf.io/v1/network-attachment-definitions"
            )
            or {}
        )
        for nad in nads.get("items", []) or []:
            md = nad.get("metadata") or {}
            spec = nad.get("spec") or {}
            out.append(
                {
                    "name": md.get("name") or "",
                    "namespace": md.get("namespace"),
                    "type": "nad",
                    "config_summary": (spec.get("config") or "")[:512],
                }
            )

        # ClusterUserDefinedNetworks — OCP 4.14+ for primary UDN. May
        # not be installed; the helper returns None in that case.
        cudns = self._get(client, "/apis/k8s.ovn.org/v1/clusteruserdefinednetworks")
        for cudn in (cudns or {}).get("items", []) or []:
            md = cudn.get("metadata") or {}
            spec = cudn.get("spec") or {}
            net = spec.get("network") or {}
            topology = net.get("topology") or "Layer2"
            out.append(
                {
                    "name": md.get("name") or "",
                    "namespace": None,  # cluster-scoped
                    "type": "cudn",
                    "config_summary": f"topology={topology}",
                }
            )

        udns = self._get(client, "/apis/k8s.ovn.org/v1/userdefinednetworks")
        for udn in (udns or {}).get("items", []) or []:
            md = udn.get("metadata") or {}
            spec = udn.get("spec") or {}
            topology = (spec.get("topology") or "").lower() or "layer2"
            out.append(
                {
                    "name": md.get("name") or "",
                    "namespace": md.get("namespace"),
                    "type": "udn",
                    "config_summary": f"topology={topology}",
                }
            )
        return out

    def discover_namespaces(self, client: httpx.Client) -> list[dict]:
        body = self._get(client, "/api/v1/namespaces") or {}
        out: list[dict] = []
        for ns in body.get("items", []) or []:
            md = ns.get("metadata") or {}
            name = md.get("name") or ""
            if any(name.startswith(p) for p in SYSTEM_NAMESPACE_PREFIXES):
                continue
            if name == "default":
                # 'default' isn't a startswith match; skip explicitly.
                continue
            out.append({"name": name, "labels": md.get("labels") or {}})
        return out

    def discover_capacity(self, client: httpx.Client) -> dict:
        body = self._get(client, "/api/v1/nodes") or {}
        nodes = body.get("items", []) or []
        cpu_total = 0
        mem_total = 0
        for node in nodes:
            cap = (node.get("status") or {}).get("capacity") or {}
            cpu_total += _parse_cpu_quantity(cap.get("cpu") or "0")
            mem_total += _parse_memory_quantity(cap.get("memory") or "0")

        # Best-effort VM count via KubeVirt. Returns None on missing CRD.
        existing_vm_count: int | None = None
        try:
            kv = self._get(
                client, "/apis/kubevirt.io/v1/virtualmachines"
            )
            if kv is not None:
                existing_vm_count = len(kv.get("items") or [])
        except OCPDiscoveryError:
            existing_vm_count = None

        return {
            "node_count": len(nodes),
            "total_cpu_millicores": cpu_total,
            "total_memory_bytes": mem_total,
            "existing_vm_count": existing_vm_count,
        }

    def discover_mtv_namespace(self, client: httpx.Client) -> str | None:
        """Find the namespace where MTV/Forklift is installed.

        Pragmatic approach: list MTV providers cluster-wide; whichever
        namespace they're in is where MTV lives. If no providers exist,
        we can't tell and return None — the operator manually sets the
        MTV namespace in the target form.
        """
        body = self._get(client, "/apis/forklift.konveyor.io/v1beta1/providers")
        if body is None:
            return None
        for p in body.get("items") or []:
            ns = (p.get("metadata") or {}).get("namespace")
            if ns:
                return ns
        return None

    def discover_versions(self, client: httpx.Client) -> tuple[str | None, str | None]:
        """Return ``(ocp_version, kubernetes_version)``. The OCP version
        is read from the ClusterVersion resource (present only on
        actual OpenShift clusters; vanilla Kubernetes returns None)."""
        kv = self._get(client, "/version")
        kubernetes_version = (
            (kv or {}).get("gitVersion") if kv is not None else None
        )

        ocp_version = None
        cv = self._get(client, "/apis/config.openshift.io/v1/clusterversions")
        if cv is not None:
            for item in cv.get("items") or []:
                history = (item.get("status") or {}).get("history") or []
                # Most recent Completed entry.
                for entry in history:
                    if entry.get("state") == "Completed":
                        ocp_version = entry.get("version")
                        break
                if ocp_version:
                    break
        return ocp_version, kubernetes_version

    def discover_all(self) -> DiscoveryResult:
        """Run every discovery call against one cluster connection.

        Errors during any single discovery call propagate — there's
        no point landing a half-discovered target row in the DB,
        the operator would just see "Run discovery again" anyway.
        """
        with self._client() as client:
            storage_classes = self.discover_storage_classes(client)
            network_attachments = self.discover_network_attachments(client)
            namespaces = self.discover_namespaces(client)
            cluster_capacity = self.discover_capacity(client)
            mtv_namespace = self.discover_mtv_namespace(client)
            ocp_version, kubernetes_version = self.discover_versions(client)

        return DiscoveryResult(
            storage_classes=storage_classes,
            network_attachments=network_attachments,
            namespaces=namespaces,
            cluster_capacity=cluster_capacity,
            mtv_namespace=mtv_namespace,
            ocp_version=ocp_version,
            kubernetes_version=kubernetes_version,
        )


# ---------------------------------------------------------------------------
# Quantity parsing — Kubernetes encodes CPU as "4" (cores) or "500m"
# (millicores), and memory as "16Gi" / "1024Mi" / "1Ti". The parsing
# is mostly trivial but CSI quirks like "Ki" vs "k" trip people up,
# so we centralize it here.
# ---------------------------------------------------------------------------
_BIN_UNITS = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
}
_DEC_UNITS = {
    "k": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "P": 1000**5,
}


def _parse_cpu_quantity(raw: str) -> int:
    """CPU → millicores. ``4`` → 4000; ``500m`` → 500."""
    raw = (raw or "").strip()
    if not raw:
        return 0
    if raw.endswith("m"):
        try:
            return int(raw[:-1])
        except ValueError:
            return 0
    try:
        return int(float(raw) * 1000)
    except ValueError:
        return 0


def _parse_memory_quantity(raw: str) -> int:
    """Memory → bytes. ``16Gi`` → 17179869184."""
    raw = (raw or "").strip()
    if not raw:
        return 0
    for suffix, mult in _BIN_UNITS.items():
        if raw.endswith(suffix):
            try:
                return int(float(raw[: -len(suffix)]) * mult)
            except ValueError:
                return 0
    for suffix, mult in _DEC_UNITS.items():
        if raw.endswith(suffix):
            try:
                return int(float(raw[: -len(suffix)]) * mult)
            except ValueError:
                return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def storage_class_count(target_row: Any) -> int:
    """Helper used by the dashboard summary endpoint — returns 0 when
    discovery hasn't run yet (vs raising NoneType errors)."""
    sc = getattr(target_row, "storage_classes", None) or []
    return len(sc)
