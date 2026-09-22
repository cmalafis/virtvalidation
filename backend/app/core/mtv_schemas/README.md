# Vendored MTV (forklift) CRD schemas

Source: `https://github.com/kubev2v/forklift`, tag **`v2.12.1`**,
`operator/config/crd/bases/forklift.konveyor.io_*.yaml`. Retrieved 2026-09-20.
Unmodified. Apache-2.0 (upstream license).

Used by `app.core.mtv_validate` to validate every generated document
**offline** — VirtValidate never talks to a cluster. They are the same
schemas the API server enforces, so a document that passes here will not be
rejected for shape by `oc apply`. They cannot tell you whether a referenced
Provider, NAD or StorageClass exists; `oc apply --dry-run=server` on the
operator's own cluster covers that.

To move to a newer MTV: replace these files from the matching forklift tag,
update `docs/MTV-GROUNDING.md`, and run `pytest tests/test_mtv_schema.py`.
