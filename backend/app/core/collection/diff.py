"""Deterministic structured diff between a baseline and a current
collection.

Zero LLM calls. Each rule is a Python function that produces a
``DimensionVerdict`` with one of ``pass`` / ``info`` / ``warn`` / ``fail``.
``info`` is informational and does NOT escalate the overall verdict —
it's for changes that are EXPECTED post-migration (new host key, kernel
update via MTV, etc.). The overall verdict is the worst of
``pass`` / ``warn`` / ``fail`` (info is collapsed to pass at the top).

This is the v1 deterministic foundation. A follow-up session can add an
LLM that takes the ``warn`` cases and decides whether each one warrants
human attention; the deterministic layer cleanly identifies what's
worth showing a human, and the LLM never sees the unambiguous regressions.
"""

from __future__ import annotations

from collections.abc import Iterable

DIMENSION_VERDICTS = ("pass", "info", "warn", "fail")
OVERALL_VERDICTS = ("pass", "warn", "fail")

# Rank for "worst per-dimension verdict". info < warn < fail and pass < info.
# When collapsing to the overall verdict we treat info as pass — it does
# NOT escalate a wave to warn.
_RANK = {"pass": 0, "info": 0, "warn": 1, "fail": 2}


def _safe_dict(d: object) -> dict:
    return d if isinstance(d, dict) else {}


def _safe_list(d: object) -> list:
    return d if isinstance(d, list) else []


def _service_state_key(svc: dict) -> tuple[str, str]:
    """Reduce a service entry to (unit, running-ish flag)."""
    name = str(svc.get("unit") or svc.get("name") or "").strip()
    active = str(svc.get("active") or "").lower()
    sub = str(svc.get("sub") or "").lower()
    running = active == "active" and sub in ("running", "exited")
    state = "running" if running else (active or "absent")
    return name, state


def _diff_services(baseline: dict, current: dict) -> dict:
    """Service state regression check.

    Rules:
      - running in baseline, failed/inactive now → fail
      - running in baseline, not present now → fail
      - not in baseline, running now → info (new service is informational)
    """
    base = {n: s for n, s in (_service_state_key(s) for s in _safe_list(baseline.get("services")))}
    curr = {n: s for n, s in (_service_state_key(s) for s in _safe_list(current.get("services")))}

    changes: list[dict] = []
    worst = "pass"

    for unit, base_state in base.items():
        if unit not in curr:
            if base_state == "running":
                changes.append(
                    {"unit": unit, "baseline": base_state, "current": "missing", "verdict": "fail"}
                )
                worst = _bump(worst, "fail")
            else:
                changes.append(
                    {"unit": unit, "baseline": base_state, "current": "missing", "verdict": "info"}
                )
                worst = _bump(worst, "info")
            continue
        cur_state = curr[unit]
        if cur_state == base_state:
            continue
        if base_state == "running" and cur_state != "running":
            changes.append(
                {"unit": unit, "baseline": base_state, "current": cur_state, "verdict": "fail"}
            )
            worst = _bump(worst, "fail")
        elif base_state != "running" and cur_state == "running":
            # Newly-running service we didn't see before — informational.
            changes.append(
                {"unit": unit, "baseline": base_state, "current": cur_state, "verdict": "info"}
            )
            worst = _bump(worst, "info")
        else:
            changes.append(
                {"unit": unit, "baseline": base_state, "current": cur_state, "verdict": "info"}
            )
            worst = _bump(worst, "info")

    new_units = [u for u in curr if u not in base]
    for unit in new_units:
        changes.append({"unit": unit, "baseline": None, "current": curr[unit], "verdict": "info"})
        worst = _bump(worst, "info")

    return {"name": "services", "verdict": worst, "changes": changes}


def _diff_ports(baseline: dict, current: dict) -> dict:
    """Listening port comparison.

    Rules:
      - present before, absent now → warn (could be intentional, flag for review)
      - new port → info
    """

    def key(p: dict) -> str:
        return f"{p.get('proto') or 'tcp'}:{p.get('address') or '0.0.0.0'}:{p.get('port')}"

    base = {key(p) for p in _safe_list(baseline.get("ports"))}
    curr = {key(p) for p in _safe_list(current.get("ports"))}

    missing = sorted(base - curr)
    added = sorted(curr - base)
    changes: list[dict] = []
    worst = "pass"

    for k in missing:
        changes.append({"port": k, "baseline": "listening", "current": "absent", "verdict": "warn"})
        worst = _bump(worst, "warn")
    for k in added:
        changes.append({"port": k, "baseline": "absent", "current": "listening", "verdict": "info"})
        worst = _bump(worst, "info")

    return {"name": "ports", "verdict": worst, "changes": changes}


def _diff_mounts(baseline: dict, current: dict) -> dict:
    """Mount table comparison.

    Rules:
      - present before, absent now → warn
      - new mount → info
      - fstype change → warn
    """

    def by_target(items: Iterable[dict]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for m in items:
            target = str(m.get("target") or "").strip()
            if target:
                out[target] = m
        return out

    base = by_target(_safe_list(baseline.get("mounts")))
    curr = by_target(_safe_list(current.get("mounts")))

    changes: list[dict] = []
    worst = "pass"

    for target, m in base.items():
        if target not in curr:
            changes.append(
                {
                    "mount": target,
                    "baseline": m.get("fstype"),
                    "current": "missing",
                    "verdict": "warn",
                }
            )
            worst = _bump(worst, "warn")
            continue
        if (m.get("fstype") or "") != (curr[target].get("fstype") or ""):
            changes.append(
                {
                    "mount": target,
                    "baseline": m.get("fstype"),
                    "current": curr[target].get("fstype"),
                    "verdict": "warn",
                }
            )
            worst = _bump(worst, "warn")

    for target in curr.keys() - base.keys():
        changes.append(
            {
                "mount": target,
                "baseline": None,
                "current": curr[target].get("fstype"),
                "verdict": "info",
            }
        )
        worst = _bump(worst, "info")

    return {"name": "mounts", "verdict": worst, "changes": changes}


def _diff_cron(baseline: dict, current: dict) -> dict:
    """Cron + systemd-timer comparison.

    Rules:
      - present before, absent now → warn
      - new entry → info
    """
    base = _safe_dict(baseline.get("cron"))
    curr = _safe_dict(current.get("cron"))

    def flatten(c: dict) -> set[str]:
        out: set[str] = set()
        user_tabs = _safe_dict(c.get("user_crontabs"))
        for user, entries in user_tabs.items():
            for e in _safe_list(entries):
                out.add(f"user:{user}:{e}")
        for e in _safe_list(c.get("system")):
            out.add(f"system:{e}")
        return out

    base_set = flatten(base)
    curr_set = flatten(curr)
    missing = sorted(base_set - curr_set)
    added = sorted(curr_set - base_set)

    changes: list[dict] = []
    worst = "pass"
    for entry in missing:
        changes.append(
            {"entry": entry, "baseline": "present", "current": "absent", "verdict": "warn"}
        )
        worst = _bump(worst, "warn")
    for entry in added:
        changes.append(
            {"entry": entry, "baseline": "absent", "current": "present", "verdict": "info"}
        )
        worst = _bump(worst, "info")

    return {"name": "cron", "verdict": worst, "changes": changes}


def _diff_kernel_os(baseline: dict, current: dict) -> dict:
    """Kernel + OS version comparison.

    Expected to change post-migration when MTV updates the kernel. Always
    informational — never fail or warn.
    """
    base_meta = _safe_dict(baseline.get("meta"))
    curr_meta = _safe_dict(current.get("meta"))
    changes: list[dict] = []

    base_kernel = (base_meta.get("kernel") or "").strip()
    curr_kernel = (curr_meta.get("kernel") or "").strip()
    if base_kernel and curr_kernel and base_kernel != curr_kernel:
        changes.append(
            {"field": "kernel", "baseline": base_kernel, "current": curr_kernel, "verdict": "info"}
        )

    base_os = _safe_dict(base_meta.get("os"))
    curr_os = _safe_dict(curr_meta.get("os"))
    for field in ("id", "version_id", "pretty_name"):
        b, c = base_os.get(field), curr_os.get(field)
        if b and c and b != c:
            changes.append({"field": f"os.{field}", "baseline": b, "current": c, "verdict": "info"})

    return {"name": "kernel_os", "verdict": "info" if changes else "pass", "changes": changes}


def _diff_network_topology(baseline: dict, current: dict) -> dict:
    """Network topology comparison.

    Rules:
      - interface count drop → warn
      - default route absent → warn
      - DNS resolver count change → info
    """
    base = _safe_dict(baseline.get("network"))
    curr = _safe_dict(current.get("network"))

    base_ifaces = set(_safe_dict(base.get("interfaces")).keys())
    curr_ifaces = set(_safe_dict(curr.get("interfaces")).keys())
    base_routes = _safe_list(base.get("routes"))
    curr_routes = _safe_list(curr.get("routes"))
    base_dns = _safe_list(base.get("dns"))
    curr_dns = _safe_list(curr.get("dns"))

    changes: list[dict] = []
    worst = "pass"

    dropped_ifaces = base_ifaces - curr_ifaces
    new_ifaces = curr_ifaces - base_ifaces
    if dropped_ifaces:
        changes.append(
            {
                "field": "interfaces",
                "baseline": sorted(base_ifaces),
                "current": sorted(curr_ifaces),
                "verdict": "warn",
                "note": f"interfaces dropped: {sorted(dropped_ifaces)}",
            }
        )
        worst = _bump(worst, "warn")
    if new_ifaces:
        changes.append(
            {
                "field": "interfaces",
                "baseline": sorted(base_ifaces),
                "current": sorted(curr_ifaces),
                "verdict": "info",
                "note": f"new interfaces: {sorted(new_ifaces)}",
            }
        )
        worst = _bump(worst, "info")

    had_default = any("default" in str(r) for r in base_routes)
    has_default = any("default" in str(r) for r in curr_routes)
    if had_default and not has_default:
        changes.append(
            {
                "field": "default_route",
                "baseline": "present",
                "current": "absent",
                "verdict": "warn",
            }
        )
        worst = _bump(worst, "warn")

    if len(base_dns) != len(curr_dns):
        changes.append(
            {
                "field": "dns",
                "baseline": list(base_dns),
                "current": list(curr_dns),
                "verdict": "info",
            }
        )
        worst = _bump(worst, "info")

    return {"name": "network", "verdict": worst, "changes": changes}


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------
def diff_collection(baseline_data: dict | None, current_data: dict | None) -> dict:
    """Compare two collection dicts and return a structured diff.

    Shape:
      ``{"overall": "pass|warn|fail",
         "dimensions": [
             {"name": str, "verdict": "pass|info|warn|fail", "changes": [...]},
             ...
         ]}``

    Both inputs may be ``None`` / empty dicts; in that case a "pass"
    overall is returned with empty dimensions. (Validation contexts should
    detect missing collection upstream and short-circuit to ``unreachable``
    rather than diff against nothing.)
    """
    base = baseline_data or {}
    curr = current_data or {}

    dimensions = [
        _diff_services(base, curr),
        _diff_ports(base, curr),
        _diff_mounts(base, curr),
        _diff_cron(base, curr),
        _diff_kernel_os(base, curr),
        _diff_network_topology(base, curr),
    ]

    overall = "pass"
    for d in dimensions:
        v = d["verdict"]
        # info doesn't escalate the overall verdict — collapse to pass.
        scoring_verdict = "pass" if v == "info" else v
        if _RANK[scoring_verdict] > _RANK[overall]:
            overall = scoring_verdict

    return {"overall": overall, "dimensions": dimensions}


def _bump(current: str, candidate: str) -> str:
    """Return the worst of ``current`` and ``candidate`` per :data:`_RANK`."""
    return candidate if _RANK[candidate] > _RANK[current] else current
