"""Offline validation of generated MTV documents against the real CRD schemas.

VirtValidate never authenticates to a cluster, so "will ``oc apply`` accept
this?" has to be answered locally. The forklift CRDs vendored in
``app/core/mtv_schemas`` carry the same openAPIV3 schemas the API server
enforces; every document the generator emits is checked against them
before it leaves the process (``generate_wave_yaml`` calls
:func:`assert_valid`).

A schema can only judge shape. The checks in ``_semantic_errors`` cover the
mistakes that are schema-valid but still wrong — the class of bug this
module was added for: a ``vms[].id`` holding a display name passes the
schema and resolves nothing on a real cluster.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft7Validator

_SCHEMA_DIR = Path(__file__).parent / "mtv_schemas"
_API_VERSION = "forklift.konveyor.io/v1beta1"
_PLURALS = {
    "Provider": "providers",
    "NetworkMap": "networkmaps",
    "StorageMap": "storagemaps",
    "Plan": "plans",
    "Migration": "migrations",
    "Hook": "hooks",
}
# vSphere managed object reference for a VM.
_MOREF_RE = re.compile(r"^vm-\d+$")
_DNS1123_SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")


@lru_cache(maxsize=None)
def _validator(kind: str) -> Draft7Validator:
    crd = yaml.safe_load((_SCHEMA_DIR / f"forklift.konveyor.io_{_PLURALS[kind]}.yaml").read_text())
    version = next(v for v in crd["spec"]["versions"] if v["name"] == "v1beta1")
    return Draft7Validator(version["schema"]["openAPIV3Schema"])


def _semantic_errors(doc: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    meta = doc.get("metadata") or {}
    for key in ("name", "namespace"):
        value = meta.get(key) or ""
        if not _DNS1123_SUBDOMAIN_RE.match(value):
            errors.append(f"metadata.{key} {value!r} is not a DNS-1123 name")
    spec = doc.get("spec") or {}
    if doc.get("kind") == "Plan":
        for i, vm in enumerate(spec.get("vms") or []):
            if not vm.get("id") and not vm.get("name"):
                errors.append(f"spec.vms[{i}] has neither id nor name")
            if vm.get("id") and not _MOREF_RE.match(str(vm["id"])):
                errors.append(
                    f"spec.vms[{i}].id {vm['id']!r} is not a vSphere MoRef (vm-<n>); "
                    "MTV resolves id before name, so a wrong id makes the VM unresolvable"
                )
            if "namespace" in vm:
                errors.append(
                    f"spec.vms[{i}].namespace is the SOURCE namespace (OpenShift providers "
                    "only) — it is not a per-VM target override"
                )
    if doc.get("kind") == "NetworkMap":
        for i, entry in enumerate(spec.get("map") or []):
            dest = entry.get("destination") or {}
            if dest.get("type") == "multus" and not (dest.get("name") and dest.get("namespace")):
                errors.append(f"spec.map[{i}] is multus but lacks the NAD name/namespace")
    return errors


def validate_documents(yaml_text: str) -> list[str]:
    """Every problem found, as ``"<Kind>/<name>: <path>: <message>"``. Empty = valid."""
    problems: list[str] = []
    for doc in yaml.safe_load_all(yaml_text):
        if not isinstance(doc, dict):
            problems.append("document is not a mapping")
            continue
        kind = doc.get("kind")
        label = f"{kind}/{(doc.get('metadata') or {}).get('name')}"
        if doc.get("apiVersion") != _API_VERSION:
            problems.append(f"{label}: apiVersion must be {_API_VERSION}")
        if kind not in _PLURALS:
            problems.append(f"{label}: unknown kind")
            continue
        for err in sorted(_validator(kind).iter_errors(doc), key=lambda e: list(e.path)):
            path = ".".join(str(p) for p in err.path) or "<root>"
            problems.append(f"{label}: {path}: {err.message[:200]}")
        problems.extend(f"{label}: {e}" for e in _semantic_errors(doc))
    return problems


def assert_valid(yaml_text: str) -> None:
    from app.core.mtv import MTVGenerationError

    problems = validate_documents(yaml_text)
    if problems:
        raise MTVGenerationError(
            "Generated MTV YAML failed offline CRD validation (this is a VirtValidate "
            "bug, not an input problem): " + "; ".join(problems[:5])
        )
