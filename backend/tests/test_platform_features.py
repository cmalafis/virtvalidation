"""The OCP / MTV support matrix (app.core.platform_features)."""

from __future__ import annotations

import typing

from app.core import platform_features as pf
from app.schemas import target_entities as te


def test_version_parsing_and_pairing():
    assert pf.parse_version("4.21.30") == (4, 21)
    assert pf.parse_version("v4.22") == (4, 22)
    assert pf.parse_version("") is None
    assert pf.mtv_for("4.21") == (2, 11)
    assert pf.mtv_for("4.22.3") == (2, 12)
    # Newer than the matrix → newest known pairing; older → unknown.
    assert pf.mtv_for("4.23") == (2, 12)
    assert pf.mtv_for("4.12") is None


def test_feature_status_by_release():
    assert pf.feature_status("bgp_evpn_primary_cudn", ocp=(4, 21), mtv=None) is None
    assert pf.feature_status("copy_offload_warm", ocp=None, mtv=(2, 11)) == "TP"
    assert pf.feature_status("copy_offload_warm", ocp=None, mtv=(2, 12)) == "GA"
    assert pf.feature_status("passt_binding", ocp=(4, 22), mtv=None) == "TP"
    # Unknown versions are judged against the newest release.
    assert pf.feature_status("ipv6_single_stack_vms", ocp=None, mtv=None) == "GA"


def test_every_feature_has_a_source():
    for f in pf.FEATURES:
        assert f.source.startswith("https://"), f.key
        assert f.status in ("GA", "TP", "DP")


def test_prompt_context_is_small():
    assert len(pf.prompt_context("4.22")) <= 6


def test_schema_literals_match_vocabularies():
    pairs = [
        (te.NetworkRole, pf.NETWORK_ROLES),
        (te.NetworkTopology, pf.NETWORK_TOPOLOGIES),
        (te.IpamMode, pf.IPAM_MODES),
        (te.Binding, pf.BINDINGS),
        (te.Transport, pf.TRANSPORTS),
        (te.VolumeMode, pf.VOLUME_MODES),
        (te.CopyOffloadVendor, pf.COPY_OFFLOAD_VENDORS),
    ]
    for literal, vocab in pairs:
        assert typing.get_args(literal) == vocab


def test_platform_features_endpoint(client, db_session):
    from app.models.target import OCPTarget

    t = OCPTarget(name="c421", api_endpoint="https://api:6443", ocp_version="4.21.30")
    db_session.add(t)
    db_session.commit()
    body = client.get(f"/api/sources/targets/{t.id}/platform-features").json()
    assert body["ocp_version"] == "4.21" and body["mtv_version"] == "2.11"
    assert body["mtv_version_inferred"] is True
    feats = {f["key"]: f["status"] for f in body["features"]}
    assert feats["bgp_evpn_primary_cudn"] == "unavailable"
    assert feats["storage_live_migration"] == "GA"
    assert client.get("/api/sources/targets/9999/platform-features").status_code == 404
