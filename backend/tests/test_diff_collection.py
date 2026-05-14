"""Tests for the deterministic diff_collection function.

Each rule has at least one positive (regression triggers) and one negative
(no false positive) case. The overall verdict semantics are pinned:
``info`` never escalates beyond ``pass``; ``warn`` and ``fail`` do.
"""

from __future__ import annotations

from app.core.collection.diff import diff_collection


def _baseline(**overrides) -> dict:
    base = {
        "meta": {
            "host": "h",
            "hostname": "h.corp",
            "kernel": "5.14.0-362",
            "os": {"id": "rhel", "version_id": "9.2", "pretty_name": "RHEL 9.2"},
        },
        "services": [
            {"unit": "sshd.service", "active": "active", "sub": "running"},
            {"unit": "postgresql.service", "active": "active", "sub": "running"},
        ],
        "network": {
            "interfaces": {"eth0": {"ipv4": ["10.0.0.5/24"]}},
            "routes": ["default via 10.0.0.1 dev eth0"],
            "dns": ["8.8.8.8"],
        },
        "ports": [
            {"proto": "tcp", "address": "0.0.0.0", "port": 22},
            {"proto": "tcp", "address": "0.0.0.0", "port": 5432},
        ],
        "mounts": [
            {"target": "/", "source": "/dev/sda1", "fstype": "xfs"},
        ],
        "cron": {
            "user_crontabs": {"root": ["0 * * * * /usr/local/bin/backup.sh"]},
            "system": [],
        },
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
def test_service_running_to_failed_is_fail():
    base = _baseline()
    curr = _baseline(
        services=[
            {"unit": "sshd.service", "active": "active", "sub": "running"},
            {"unit": "postgresql.service", "active": "failed", "sub": "failed"},
        ]
    )
    diff = diff_collection(base, curr)
    assert diff["overall"] == "fail"
    services_dim = next(d for d in diff["dimensions"] if d["name"] == "services")
    assert services_dim["verdict"] == "fail"


def test_service_running_to_absent_is_fail():
    base = _baseline()
    curr = _baseline(
        services=[{"unit": "sshd.service", "active": "active", "sub": "running"}]
    )
    diff = diff_collection(base, curr)
    assert diff["overall"] == "fail"


def test_new_service_is_info_not_warn():
    base = _baseline()
    curr = _baseline(
        services=[
            {"unit": "sshd.service", "active": "active", "sub": "running"},
            {"unit": "postgresql.service", "active": "active", "sub": "running"},
            {"unit": "newrelic.service", "active": "active", "sub": "running"},
        ]
    )
    diff = diff_collection(base, curr)
    # info doesn't escalate overall verdict
    assert diff["overall"] == "pass"


def test_all_services_running_is_pass():
    base = _baseline()
    curr = _baseline()
    diff = diff_collection(base, curr)
    assert diff["overall"] == "pass"


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------
def test_port_disappearing_is_warn():
    base = _baseline()
    curr = _baseline(ports=[{"proto": "tcp", "address": "0.0.0.0", "port": 22}])
    diff = diff_collection(base, curr)
    assert diff["overall"] == "warn"
    ports_dim = next(d for d in diff["dimensions"] if d["name"] == "ports")
    assert ports_dim["verdict"] == "warn"


def test_new_port_is_info_only():
    base = _baseline()
    curr = _baseline(
        ports=[
            {"proto": "tcp", "address": "0.0.0.0", "port": 22},
            {"proto": "tcp", "address": "0.0.0.0", "port": 5432},
            {"proto": "tcp", "address": "0.0.0.0", "port": 9090},  # new
        ]
    )
    diff = diff_collection(base, curr)
    assert diff["overall"] == "pass"  # info doesn't escalate


# ---------------------------------------------------------------------------
# Mounts
# ---------------------------------------------------------------------------
def test_mount_disappearing_is_warn():
    base = _baseline()
    curr = _baseline(mounts=[])
    diff = diff_collection(base, curr)
    assert diff["overall"] == "warn"


def test_fstype_change_is_warn():
    base = _baseline()
    curr = _baseline(mounts=[{"target": "/", "source": "/dev/sda1", "fstype": "ext4"}])
    diff = diff_collection(base, curr)
    assert diff["overall"] == "warn"


def test_new_mount_is_info():
    base = _baseline()
    curr = _baseline(
        mounts=[
            {"target": "/", "source": "/dev/sda1", "fstype": "xfs"},
            {"target": "/data", "source": "/dev/sdb1", "fstype": "xfs"},
        ]
    )
    diff = diff_collection(base, curr)
    assert diff["overall"] == "pass"


# ---------------------------------------------------------------------------
# Cron
# ---------------------------------------------------------------------------
def test_cron_entry_disappearing_is_warn():
    base = _baseline()
    curr = _baseline(cron={"user_crontabs": {}, "system": []})
    diff = diff_collection(base, curr)
    assert diff["overall"] == "warn"


def test_new_cron_entry_is_info():
    base = _baseline()
    curr = _baseline(
        cron={
            "user_crontabs": {
                "root": [
                    "0 * * * * /usr/local/bin/backup.sh",
                    "5 * * * * /usr/local/bin/sync.sh",
                ]
            },
            "system": [],
        }
    )
    diff = diff_collection(base, curr)
    assert diff["overall"] == "pass"


# ---------------------------------------------------------------------------
# Kernel / OS
# ---------------------------------------------------------------------------
def test_kernel_change_is_info_only():
    base = _baseline()
    curr = _baseline(
        meta={
            "host": "h",
            "hostname": "h.corp",
            "kernel": "5.14.0-999",
            "os": {"id": "rhel", "version_id": "9.2", "pretty_name": "RHEL 9.2"},
        }
    )
    diff = diff_collection(base, curr)
    assert diff["overall"] == "pass"
    kos = next(d for d in diff["dimensions"] if d["name"] == "kernel_os")
    assert kos["verdict"] == "info"
    assert len(kos["changes"]) == 1


def test_os_pretty_name_change_is_info_only():
    base = _baseline()
    curr = _baseline(
        meta={
            "host": "h",
            "hostname": "h.corp",
            "kernel": "5.14.0-362",
            "os": {"id": "rhel", "version_id": "9.3", "pretty_name": "RHEL 9.3"},
        }
    )
    diff = diff_collection(base, curr)
    assert diff["overall"] == "pass"


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------
def test_dropped_interface_is_warn():
    base = _baseline()
    curr = _baseline(
        network={"interfaces": {}, "routes": ["default via 10.0.0.1 dev eth0"], "dns": ["8.8.8.8"]}
    )
    diff = diff_collection(base, curr)
    assert diff["overall"] == "warn"


def test_missing_default_route_is_warn():
    base = _baseline()
    curr = _baseline(
        network={
            "interfaces": {"eth0": {"ipv4": ["10.0.0.5/24"]}},
            "routes": ["10.0.0.0/24 dev eth0"],
            "dns": ["8.8.8.8"],
        }
    )
    diff = diff_collection(base, curr)
    assert diff["overall"] == "warn"


def test_dns_count_change_is_info():
    base = _baseline()
    curr = _baseline(
        network={
            "interfaces": {"eth0": {"ipv4": ["10.0.0.5/24"]}},
            "routes": ["default via 10.0.0.1 dev eth0"],
            "dns": ["8.8.8.8", "1.1.1.1"],
        }
    )
    diff = diff_collection(base, curr)
    assert diff["overall"] == "pass"


# ---------------------------------------------------------------------------
# Overall verdict precedence
# ---------------------------------------------------------------------------
def test_fail_wins_over_warn():
    base = _baseline()
    curr = _baseline(
        services=[
            {"unit": "sshd.service", "active": "active", "sub": "running"},
            {"unit": "postgresql.service", "active": "failed", "sub": "failed"},
        ],
        ports=[{"proto": "tcp", "address": "0.0.0.0", "port": 22}],
    )
    diff = diff_collection(base, curr)
    assert diff["overall"] == "fail"


def test_warn_wins_over_info():
    base = _baseline()
    curr = _baseline(
        ports=[{"proto": "tcp", "address": "0.0.0.0", "port": 22}],
        meta={
            "host": "h",
            "hostname": "h.corp",
            "kernel": "5.14.0-999",
            "os": {"id": "rhel", "version_id": "9.2", "pretty_name": "RHEL 9.2"},
        },
    )
    diff = diff_collection(base, curr)
    assert diff["overall"] == "warn"


def test_empty_inputs_return_pass():
    diff = diff_collection({}, {})
    assert diff["overall"] == "pass"


def test_none_inputs_dont_crash():
    diff = diff_collection(None, None)
    assert diff["overall"] == "pass"


def test_diff_shape_contract():
    diff = diff_collection(_baseline(), _baseline())
    assert set(diff) == {"overall", "dimensions"}
    assert diff["overall"] in ("pass", "warn", "fail")
    names = {d["name"] for d in diff["dimensions"]}
    assert names == {"services", "ports", "mounts", "cron", "kernel_os", "network"}
    for dim in diff["dimensions"]:
        assert set(dim) >= {"name", "verdict", "changes"}
        assert dim["verdict"] in ("pass", "info", "warn", "fail")
