"""Mechanical pre-classification for the migration planner.

The migration planner used to send a flat list of N VMs straight to
the LLM and ask for wave assignment. That works for N≤10 but fails
at scale — Llama 3.2 3B drops vm_ids past ~20, Granite 3.1 8B fails
at 57 (May 2026 testing). The failures aren't a model-quality issue;
they're an architecture issue. The LLM should never see N VMs
directly. It should see M ≪ N pre-formed groups.

This module is that pre-processor. Pure Python, no LLM, no DB writes,
no external calls. Deterministic — the same input always produces
byte-identical output, which is the property federal auditors need
to recreate a plan months after it ran.

What it does:

  1. Partition VMs by PRIMARY attributes that must match for two VMs
     to belong to the same group (vCenter, target namespace).
  2. Within each partition, sub-group by SECONDARY attributes — at
     least two of network / datastore / application_hint / folder
     must overlap.
  3. Within sub-groups, fall back to name-prefix clustering so VMs
     like ``web-prod-01..05`` land together even when their RVTools
     metadata is sparse.
  4. Tag each final group with role / state / migration_risk
     heuristics + dependency hints between groups.
  5. Cap output cardinality at ``LLM_MAX_ITEMS_PER_CALL`` by
     consolidating the smallest adjacent groups together; the
     consolidation log is surfaced to the operator so they know what
     was merged.

Output: ``list[VMGroup]``. Typical 57-VM RVTools fleet → 5-15 groups.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from app.models.vm import VM

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GroupKey:
    """Composite identifier for a group.

    Equality / hashing is by-field so two groups with the same key
    compare equal across runs, which is what the deterministic-output
    invariant requires.

    ``environment`` is the partition dimension added in the
    post-N refactor — production VMs can never share a group with
    development or DR VMs, even when sharing every other dimension.

    ``target_cluster_id`` joins the partition tuple under the
    multi-cluster target architecture so groups never span clusters
    (MTV's NetworkMap/StorageMap CRs are scoped per
    (source provider, destination provider) pair). Default ``None``
    keeps the field optional for callers that construct a key
    directly without cluster context.
    """

    vcenter_id: int | None
    target_namespace: str
    role: str
    state: str
    discriminator: str  # network, datastore, app_hint, or name_prefix bucket
    environment: str = "unknown"
    target_cluster_id: int | None = None

    def as_string(self) -> str:
        # Stable string form for the API + audit log. Order is fixed
        # so reordering the dataclass fields doesn't change the
        # rendered id. ``env`` and ``tc`` slots keep prod/dev/dr and
        # cluster-A/cluster-B groups visibly distinct in audit traces
        # even when their other dimensions match.
        vc = f"vc{self.vcenter_id}" if self.vcenter_id is not None else "vc?"
        tn = self.target_namespace or "default"
        tc = f"tc{self.target_cluster_id}" if self.target_cluster_id is not None else "tc?"
        return f"{vc}/{tc}/{tn}/{self.environment}/{self.role}/{self.state}/{self.discriminator}"


@dataclass
class RiskAssessment:
    """Rich risk description surfaced to operators in the plan UI.

    ``level`` is the headline (low / medium / high) used for sorting
    and aggregation. ``factors`` enumerate WHY the level is what it
    is, in plain English. ``mitigations`` list operator actions that
    reduce the impact if the risk materializes. ``rollback_complexity``
    is the SECOND dimension operators ask about: "if this wave fails,
    how hard is it to undo?" — separate from forward-migration risk.
    ``estimated_downtime`` is a coarse bucket — exact timing depends
    on storage class + workload, which the planner doesn't know.
    """

    level: str
    factors: list[str] = field(default_factory=list)
    mitigations: list[str] = field(default_factory=list)
    rollback_complexity: str = "medium"
    estimated_downtime: str = "5-15 minutes per VM"

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "factors": list(self.factors),
            "mitigations": list(self.mitigations),
            "rollback_complexity": self.rollback_complexity,
            "estimated_downtime": self.estimated_downtime,
        }


@dataclass
class HAMember:
    """One VM's HA participation in its containing group.

    Detected mechanically from name patterns (primary/replica/standby,
    sequential numeric suffixes, member labels). Operators can
    override via the ``role`` field on the VM, but we don't try to
    distinguish "no HA" from "HA we couldn't detect" — both surface as
    ``standalone``.
    """

    vm_id: int
    vm_name: str
    ha_role: str  # primary | replica | standby | member | standalone


@dataclass
class VMGroup:
    """A mechanical grouping of VMs that should be considered together.

    The planner ships these to the LLM (M of them, never the raw VMs).
    The expansion back to VM ids is mechanical and bug-free.
    """

    key: GroupKey
    vm_ids: list[int]
    shared_attributes: dict[str, list[str]]
    estimated_role: str
    estimated_state: str
    migration_risk: str
    dependency_hints: list[str] = field(default_factory=list)
    notes: str = ""
    risk_assessment: RiskAssessment | None = None
    ha_members: list[HAMember] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.key.as_string()

    def to_llm_dict(self) -> dict:
        """Compact dict shape sent to the LLM for wave assignment.

        Excludes per-VM detail (count + role + dependencies is all the
        LLM needs) so the prompt stays small regardless of vm_count.
        """
        ha_count = sum(1 for m in self.ha_members if m.ha_role != "standalone")
        out: dict = {
            "id": self.id,
            "vm_count": len(self.vm_ids),
            "role": self.estimated_role,
            "state": self.estimated_state,
            "risk": self.migration_risk,
            "depends_on": list(self.dependency_hints),
            "notes": self.notes,
        }
        if ha_count > 1:
            # Signal HA presence so the LLM keeps the group cohesive
            # when ha_strategy="together" — when "spread", the
            # mechanical splitter has already turned each member into
            # its own micro-group before this dict ever existed.
            out["ha_member_count"] = ha_count
        return out

    def to_api_dict(self) -> dict:
        """Expanded dict shape the API surfaces to the operator UI."""
        return {
            "id": self.id,
            "vm_ids": list(self.vm_ids),
            "vm_count": len(self.vm_ids),
            "role": self.estimated_role,
            "state": self.estimated_state,
            "risk": self.migration_risk,
            "depends_on": list(self.dependency_hints),
            "shared_attributes": {k: list(v) for k, v in self.shared_attributes.items()},
            "notes": self.notes,
            "risk_assessment": (self.risk_assessment.to_dict() if self.risk_assessment else None),
            "ha_members": [
                {
                    "vm_id": m.vm_id,
                    "vm_name": m.vm_name,
                    "ha_role": m.ha_role,
                }
                for m in self.ha_members
            ],
        }


# ---------------------------------------------------------------------------
# Role / state / risk heuristics
# ---------------------------------------------------------------------------
_ROLE_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Order matters — first match wins, so "edge" beats "app" when a
    # name like "edge-app-01" appears.
    ("edge", re.compile(r"\b(lb|haproxy|proxy|edge|gateway|router|ingress|envoy)\b", re.I)),
    (
        "data",
        re.compile(
            r"\b(db|sql|postgres|oracle|mongo|mysql|mariadb|redis|cache|kafka|rabbit|memcache|imaging|process)\b",
            re.I,
        ),
    ),
    # ``infrastructure`` covers identity (AD/LDAP/RHIDM), DNS, NTP,
    # PKI/HSM/KMS, and backup/snapshot servers — all federal-customer
    # "shared services" that migrate ahead of application tiers.
    (
        "infrastructure",
        re.compile(
            r"\b(ad|dc|rhidm|domain|ldap|dns|ntp|kdc|vault|consul|pki|cert|hsm|kms|backup|snapshot)\b",
            re.I,
        ),
    ),
    ("web", re.compile(r"\b(web|www|nginx|apache|httpd|caddy|iis)\b", re.I)),
    ("app", re.compile(r"\b(app|api|service|svc|backend|worker|tomcat|jboss|wildfly)\b", re.I)),
]


def detect_role(vm: VM) -> str:
    """Return one of web|app|data|edge|infrastructure|other."""
    # Operator-supplied role hint wins when it matches a known label.
    role_hint = (getattr(vm, "role", "") or "").strip().lower()
    if role_hint in {"web", "app", "data", "edge", "infrastructure", "other"}:
        return role_hint

    haystack = " ".join(
        s
        for s in [
            vm.name or "",
            getattr(vm, "role", "") or "",
            getattr(vm, "application_hint", "") or "",
        ]
        if s
    )
    for label, pattern in _ROLE_PATTERNS:
        if pattern.search(haystack):
            return label
    return "other"


def detect_state(vm: VM, role: str) -> str:
    """Return stateful|stateless|unknown.

    Stateful roles (data, infrastructure) are pinned stateful. Stateless
    is the default for web/app/edge with no shared-storage signal. The
    "unknown" verdict trips for ``role=other`` so the LLM can use the
    surrounding wave context to break the tie.
    """
    if role in {"data", "infrastructure"}:
        return "stateful"
    if role in {"web", "app", "edge"}:
        return "stateless"
    return "unknown"


def detect_risk(
    role: str,
    state: str,
    vm_count: int,
    has_sequential_names: bool,
) -> str:
    """Return low|medium|high based on role, state, and group shape."""
    if state == "stateful" and role in {"data", "infrastructure"}:
        return "high"
    if state == "stateless" and role in {"web", "app", "edge"}:
        return "low" if vm_count <= 3 else "medium"
    if has_sequential_names and vm_count >= 2:
        # An HA pair / sequence — the cutover surface area is bigger.
        return "medium"
    return "medium"


def assess_risk(
    role: str,
    state: str,
    vm_count: int,
    ha_members: list[HAMember],
    has_sequential_names: bool,
) -> RiskAssessment:
    """Build a RiskAssessment with explicit factors + mitigations.

    Federal customers' compliance review asks operators "why is this
    high-risk?" and "what did you do to mitigate?". A one-letter
    risk level isn't enough — every level above ``low`` must trail
    a list of specific factors the operator can defend in writing.

    The factor catalog is fixed (not free-text from the LLM) so the
    audit trail can correlate factors across plans / customers /
    months without keyword drift.
    """
    factors: list[str] = []
    mitigations: list[str] = []

    if state == "stateful":
        factors.append("Stateful service with persistent data")
        mitigations.append("Verify backup completed within 4 hours of migration")

    if role == "data":
        factors.append("Database / data tier — connection loss affects " "dependent applications")
        mitigations.append(
            "Coordinate with application team for connection " "draining before migration"
        )
    elif role == "infrastructure":
        factors.append("Infrastructure service — failure affects entire cluster")
        mitigations.append("Ensure secondary AD / DNS / PKI available during migration")

    # No HA peers? Stateful groups without redundancy carry a
    # single-point-of-failure risk that operators need to plan around.
    ha_peers = [m for m in ha_members if m.ha_role != "standalone"]
    if state == "stateful" and len(ha_peers) < 2:
        factors.append("No HA replication detected — single point of failure")
        mitigations.append(
            "Consider establishing HA replica before migration or "
            "schedule a documented maintenance window"
        )

    if has_sequential_names and vm_count >= 2:
        factors.append(
            f"Sequential cluster of {vm_count} members — coordinated "
            "cutover across multiple nodes"
        )
        mitigations.append(
            "Spread HA members across waves (ha_strategy=spread is "
            "the default) so at least one node serves the original "
            "infrastructure throughout the migration window"
        )

    if vm_count > 10:
        factors.append(
            f"Large group ({vm_count} VMs) — migration time and "
            "rollback complexity scale with member count"
        )
        mitigations.append(
            "Run pilot migration on 2-3 representative VMs in a "
            "lower environment before the full wave"
        )

    if len(factors) >= 3:
        level = "high"
    elif len(factors) >= 1:
        level = "medium"
    else:
        level = "low"

    if state == "stateful":
        rollback = "high"
    elif role == "infrastructure":
        rollback = "medium"
    else:
        rollback = "low"

    if state == "stateless":
        downtime = "0 (live migration possible)"
    elif role == "data":
        downtime = "10-30 minutes per VM (connection drain + cutover)"
    elif role == "infrastructure":
        downtime = "5-15 minutes per VM"
    else:
        downtime = "5-15 minutes per VM"

    return RiskAssessment(
        level=level,
        factors=factors,
        mitigations=mitigations,
        rollback_complexity=rollback,
        estimated_downtime=downtime,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _name_prefix(name: str) -> str:
    """First 3-5 chars of the name, stripped of trailing digits / dashes.

    ``web-prod-01`` → ``web-prod``. ``app01`` → ``app``. Returns ``""``
    for empty / digit-only names; callers should treat that as "no
    prefix bucket".
    """
    if not name:
        return ""
    # Split off the trailing digit run (and any preceding -/_).
    stem = re.sub(r"[-_]?\d+$", "", name)
    if not stem:
        return ""
    # Keep up to the first 12 chars so very long base names don't blow
    # up the discriminator string in the GroupKey.
    return stem.lower()[:12]


# HA role detection: name patterns that strongly imply primary /
# replica relationships. Order matters — the first matching pattern
# wins, so primary checks come before generic-member checks.
_HA_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("primary", re.compile(r"\b(primary|leader|master|active|writer|main)\b", re.I)),
    ("replica", re.compile(r"\b(replica|secondary|follower|reader|slave)\b", re.I)),
    ("standby", re.compile(r"\b(standby|passive|backup)\b", re.I)),
]


def detect_ha_role(vm_name: str, sequence_index: int | None = None) -> str:
    """Classify one VM's HA role from its name.

    The sequential-naming case is the most common in the field — many
    federal customers number their cluster members ``-01``, ``-02``,
    ``-03``. When that's the only signal, the lowest-numbered member
    gets ``primary`` and the rest get ``member``. The classifier
    can't tell whether a sequential cluster is symmetric (Cassandra-
    style, all peers) or asymmetric (Postgres-style, primary + reads);
    we err on the safe side and treat the first-numbered as the
    promotion target.
    """
    name = vm_name or ""
    for role, pattern in _HA_PATTERNS:
        if pattern.search(name):
            return role
    if sequence_index is not None and sequence_index >= 0:
        return "primary" if sequence_index == 0 else "member"
    return "standalone"


def detect_ha_members(vms: Sequence["VM"]) -> list[HAMember]:
    """Inspect a group's VMs + return the HA membership manifest.

    Returns one ``HAMember`` per input VM. The pattern detector runs
    first; if no VM in the group matched a primary/replica/standby
    pattern AND the group has ≥2 VMs with sequential names, we fall
    back to sequence-index detection. Single-VM groups always report
    ``standalone``.
    """
    if not vms:
        return []
    members: list[HAMember] = []
    pattern_hits = False
    for vm in vms:
        role = detect_ha_role(vm.name or "")
        if role != "standalone":
            pattern_hits = True
        members.append(HAMember(vm_id=vm.id, vm_name=vm.name or "", ha_role=role))

    if pattern_hits or len(vms) < 2:
        return members

    # No explicit pattern matched. If the group is a sequential
    # cluster, tag the lowest-numbered VM as primary and the rest as
    # members. Skip when the suffix numbering is inconsistent — that
    # implies the VMs are independent, not an HA pool.
    suffixes: list[tuple[int, int]] = []
    for i, vm in enumerate(vms):
        m = re.search(r"(\d+)$", vm.name or "")
        if not m:
            return members
        suffixes.append((int(m.group(1)), i))
    if len(suffixes) < 2:
        return members
    suffixes.sort()
    lowest_index = suffixes[0][1]
    out: list[HAMember] = []
    for i, vm in enumerate(vms):
        role = "primary" if i == lowest_index else "member"
        out.append(HAMember(vm_id=vm.id, vm_name=vm.name or "", ha_role=role))
    return out


def _has_sequential_names(names: Sequence[str]) -> bool:
    """True when at least two names share a prefix + differ by a
    monotonically increasing numeric suffix.

    Catches the ``web-01``/``web-02`` HA pattern without false-flagging
    a pair like ``alpha`` / ``beta``.
    """
    by_prefix: dict[str, list[int]] = defaultdict(list)
    for name in names:
        m = re.match(r"^(.*?)(\d+)$", name or "")
        if not m:
            continue
        by_prefix[m.group(1)].append(int(m.group(2)))
    return any(len(suffixes) >= 2 for suffixes in by_prefix.values())


def _shared_overlap(items: list[list[str]]) -> set[str]:
    """Intersection of non-empty lists; empty intersection returns set()."""
    sets = [set(x) for x in items if x]
    if not sets:
        return set()
    out = sets[0]
    for s in sets[1:]:
        out = out & s
    return out


# ---------------------------------------------------------------------------
# Preclassifier
# ---------------------------------------------------------------------------
class PreClassifier:
    """Group VMs into mechanical migration candidates.

    Reuse across calls is safe — no instance state beyond the cap.

    Previous versions of this class hard-capped the output at
    ``llm_max_items_per_call`` by merging the smallest groups
    together when the natural classification produced more. That
    capping was an architectural mistake — it constrained the
    plan-level group count rather than the per-LLM-call ceiling.
    The ceiling now applies per LLM call (via the wave skeleton +
    per-wave rationale path); the preclassifier is free to produce
    as many groups as the input naturally yields.

    ``max_groups`` kwarg is retained for back-compat (tests still
    pass it explicitly to assert capping behaviour). When set, the
    consolidator runs as before. When None / unset, no consolidation
    happens.
    """

    def __init__(self, *, max_groups: int | None = None) -> None:
        # None = unbounded (the new default). Explicit int = legacy
        # capping for tests that need to exercise the consolidation
        # path. The PRODUCTION call path never sets this — wave-level
        # enforcement in MechanicalWaveAssigner does the bounding now.
        self.max_groups = max_groups if max_groups is None else max(1, int(max_groups))

    @staticmethod
    def split_ha_group(group: VMGroup) -> list[VMGroup]:
        """Split a group with HA members into per-member micro-groups.

        Used by the planner's ``ha_strategy="spread"`` path (the
        default): each HA member becomes its own group so the wave
        assigner places it in its own wave. Primary members get the
        lowest wave (earliest cutover); replicas + members follow.

        Returns the original group unchanged when there's no HA
        relationship (single VM, all members ``standalone``, or
        exactly one HA-tagged member).
        """
        ha_count = sum(1 for m in group.ha_members if m.ha_role != "standalone")
        if ha_count < 2:
            return [group]

        # Build per-member micro-groups. Sort by ha_role priority so
        # the wave assigner naturally orders primary → standby →
        # replica → member when iterating in order.
        priority = {"primary": 0, "standby": 1, "replica": 2, "member": 3, "standalone": 4}
        members_sorted = sorted(
            group.ha_members,
            key=lambda m: (priority.get(m.ha_role, 5), m.vm_name),
        )

        micro: list[VMGroup] = []
        for m in members_sorted:
            micro_key = GroupKey(
                vcenter_id=group.key.vcenter_id,
                target_namespace=group.key.target_namespace,
                role=group.estimated_role,
                state=group.estimated_state,
                discriminator=f"{group.key.discriminator}/ha:{m.ha_role}:{m.vm_name}",
                environment=group.key.environment,
                target_cluster_id=group.key.target_cluster_id,
            )
            micro.append(
                VMGroup(
                    key=micro_key,
                    vm_ids=[m.vm_id],
                    shared_attributes=dict(group.shared_attributes),
                    estimated_role=group.estimated_role,
                    estimated_state=group.estimated_state,
                    migration_risk=group.migration_risk,
                    dependency_hints=list(group.dependency_hints),
                    notes=(
                        f"HA {m.ha_role} of {group.id} — spread across waves "
                        "so the original cluster keeps a quorum during the "
                        "migration window"
                    ),
                    risk_assessment=group.risk_assessment,
                    ha_members=[m],
                )
            )
        return micro

    def classify(
        self,
        vms: Iterable[VM],
        target_cluster_id: int | None = None,
    ) -> list[VMGroup]:
        vms_list = list(vms)
        if not vms_list:
            return []

        # PRIMARY partition: vCenter source + target namespace +
        # environment. Same primary key means same physical infra +
        # same destination + same lifecycle stage — the strongest
        # signal for "migrate as a unit". Production never shares a
        # primary partition with development or DR even when both
        # land in the same target namespace; that mirrors the
        # operational reality that operators cut over prod and
        # non-prod separately.
        #
        # The environment field is normalized through
        # ``app.core.environment.normalize`` so free-text variants
        # ("Prod", "production", "live") collapse to the same enum
        # value before partitioning.
        from app.core.environment import normalize as _norm_env

        primary_buckets: dict[tuple, list[VM]] = defaultdict(list)
        for vm in vms_list:
            env = _norm_env(vm.environment)
            # UNKNOWN VMs partition together so a single plan call
            # surfaces them in one batch the operator can label
            # before re-running. We don't merge UNKNOWN into prod or
            # dev — that would obscure the missing-label signal.
            #
            # target_cluster_id joins the tuple under the multi-cluster
            # target architecture: a single source vCenter can fan out
            # to multiple OCP clusters (cluster-per-env / per-BU /
            # per-region). The MTV CRs are scoped per (source provider,
            # destination provider) pair, so groups must never span
            # clusters either.
            primary_key = (
                vm.source_vcenter_id,
                vm.target_cluster_id_override,
                (vm.target_namespace_override or "").strip(),
                env.value,
            )
            primary_buckets[primary_key].append(vm)

        groups: list[VMGroup] = []
        for primary_key, bucket in primary_buckets.items():
            primary_groups = self._classify_within_primary(primary_key, bucket)
            # Small-primary collapse: when a primary partition has
            # fewer than 5 VMs total, sub-splitting by role produces
            # 2-3 thin groups (often 1 VM each). Collapse them into a
            # single group so the LLM doesn't see noise from a tiny
            # DR or staging environment. Production-sized partitions
            # keep their per-role sub-splits intact.
            if sum(len(g.vm_ids) for g in primary_groups) < 5 and len(primary_groups) > 1:
                collapsed = primary_groups[0]
                for g in primary_groups[1:]:
                    collapsed = self._merge_groups(collapsed, g)
                groups.append(collapsed)
            else:
                groups.extend(primary_groups)

        # Cap output cardinality so the LLM stays under the
        # ``llm_max_items_per_call`` ceiling. We consolidate by merging
        # the smallest adjacent groups (within the same primary key
        # only); the LLM's wave-ordering decision doesn't care if a
        # consolidated group is slightly heterogeneous.
        # Consolidation only runs when an explicit cap is set (legacy
        # tests). Production callers leave ``max_groups=None`` so the
        # natural group count survives — wave-level enforcement
        # bounds the LLM input per-call instead.
        if self.max_groups is not None:
            groups = self._consolidate(groups, primary_buckets)

        # Dependency hints — pure-Python heuristic. The LLM may
        # override during wave assignment but the hints seed the
        # prompt so the model doesn't have to re-derive them.
        groups = self._add_dependency_hints(groups)

        # Stable ordering: by primary key, then by (role, state, risk,
        # discriminator). Federal audit trails need to recreate the
        # same list from the same VM input months later.
        groups.sort(
            key=lambda g: (
                g.key.vcenter_id if g.key.vcenter_id is not None else -1,
                g.key.target_namespace,
                g.key.role,
                g.key.state,
                g.key.discriminator,
            )
        )
        return groups

    # ------------------------------------------------------------------
    # Internal — partition / sub-group / tag
    # ------------------------------------------------------------------
    def _classify_within_primary(self, primary_key: tuple, bucket: list[VM]) -> list[VMGroup]:
        # Primary key is now (vcenter_id, target_cluster_id,
        # target_namespace, env_value). Cluster + namespace join env in
        # the partition tuple so production / development / DR never
        # merge AND clusters never merge — MTV's NetworkMap /
        # StorageMap CRs are scoped per (source provider, destination
        # provider) pair, so groups must respect that boundary.
        vcenter_id, target_cluster_id, target_namespace, env = primary_key
        if not bucket:
            return []

        # SECONDARY: group by application_hint when set, then by
        # network ∩ datastore overlap, then fall back to TERTIARY
        # name-prefix bucketing. Doing app_hint first means operator-
        # supplied grouping always wins; the inferred bucketing only
        # fills in the gaps.
        remaining: list[VM] = list(bucket)
        groups: list[VMGroup] = []

        # Pass 1: explicit application_hint groups, sub-split by
        # detected role so a 29-VM application with mixed web/app/db
        # tiers doesn't collapse into one homogeneous "epic-emr" group.
        # The role split is what lets the LLM order web → app → db
        # within an application during wave assignment.
        by_hint: dict[str, list[VM]] = defaultdict(list)
        leftovers: list[VM] = []
        for vm in remaining:
            hint = (vm.application_hint or "").strip().lower()
            if hint:
                by_hint[hint].append(vm)
            else:
                leftovers.append(vm)
        for hint, vms in by_hint.items():
            by_role: dict[str, list[VM]] = defaultdict(list)
            for vm in vms:
                by_role[detect_role(vm)].append(vm)
            for role, role_vms in by_role.items():
                groups.append(
                    self._build_group(
                        vcenter_id,
                        target_namespace,
                        role_vms,
                        discriminator=f"hint:{hint}:{role}",
                        environment=env,
                        target_cluster_id=target_cluster_id,
                    )
                )
        remaining = leftovers

        # Pass 2: network + datastore overlap. We greedy-cluster by
        # iterating remaining VMs, seeding a cluster from the first
        # ungrouped VM, and pulling in every other VM that shares
        # ≥1 network AND ≥1 datastore (or ≥2 of either when only one
        # dimension is populated). Greedy is O(N²) worst case but N
        # is already small post primary partition.
        grouped_ids: set[int] = set()
        for seed in remaining:
            if seed.id in grouped_ids:
                continue
            cluster = [seed]
            grouped_ids.add(seed.id)
            seed_nets = set(seed.vsphere_networks or [])
            seed_ds = set(seed.vsphere_datastores or [])
            for other in remaining:
                if other.id in grouped_ids or other.id == seed.id:
                    continue
                other_nets = set(other.vsphere_networks or [])
                other_ds = set(other.vsphere_datastores or [])
                net_overlap = bool(seed_nets & other_nets)
                ds_overlap = bool(seed_ds & other_ds)
                if (
                    (net_overlap and ds_overlap)
                    or (not seed_nets and not other_nets and ds_overlap)
                    or (not seed_ds and not other_ds and net_overlap)
                ):
                    cluster.append(other)
                    grouped_ids.add(other.id)
            # If the cluster is a single VM, drop it into the name-
            # prefix bucket below instead of producing a singleton
            # group — singletons explode group cardinality fast.
            if len(cluster) == 1:
                # Defer to pass 3.
                grouped_ids.discard(seed.id)
                continue
            disc = self._network_datastore_discriminator(cluster)
            groups.append(
                self._build_group(
                    vcenter_id,
                    target_namespace,
                    cluster,
                    discriminator=disc,
                    environment=env,
                    target_cluster_id=target_cluster_id,
                )
            )

        # Pass 3: name-prefix bucketing for everything still ungrouped.
        leftover_vms = [vm for vm in remaining if vm.id not in grouped_ids]
        by_prefix: dict[str, list[VM]] = defaultdict(list)
        for vm in leftover_vms:
            prefix = _name_prefix(vm.name) or "misc"
            by_prefix[prefix].append(vm)
        for prefix, vms in by_prefix.items():
            groups.append(
                self._build_group(
                    vcenter_id,
                    target_namespace,
                    vms,
                    discriminator=f"prefix:{prefix}",
                    environment=env,
                    target_cluster_id=target_cluster_id,
                )
            )

        return groups

    def _build_group(
        self,
        vcenter_id: int | None,
        target_namespace: str,
        vms: list[VM],
        *,
        discriminator: str,
        environment: str = "unknown",
        target_cluster_id: int | None = None,
    ) -> VMGroup:
        # Role: majority vote across the group's VMs. Ties resolve by
        # the role with highest risk so we don't accidentally
        # classify a mixed web+db cluster as "stateless web".
        role_counts: dict[str, int] = defaultdict(int)
        for vm in vms:
            role_counts[detect_role(vm)] += 1
        role = max(
            role_counts.items(),
            key=lambda kv: (
                kv[1],
                {"data": 5, "infrastructure": 4, "edge": 3, "app": 2, "web": 1, "other": 0}.get(
                    kv[0], 0
                ),
            ),
        )[0]

        # State: derived from role; "other" + powered_state cues fall
        # back to "unknown".
        state = detect_state(vms[0], role)
        # Promote to stateful when ANY VM in the group is stateful — a
        # mixed cluster should travel with its statefulest member.
        for vm in vms[1:]:
            if detect_state(vm, detect_role(vm)) == "stateful":
                state = "stateful"
                break

        sequential = _has_sequential_names([vm.name for vm in vms])
        risk = detect_risk(role, state, len(vms), sequential)
        ha_members = detect_ha_members(vms)
        risk_assessment = assess_risk(
            role,
            state,
            len(vms),
            ha_members,
            sequential,
        )

        # Shared attribute report — what made the group cohere.
        nets = _shared_overlap([list(vm.vsphere_networks or []) for vm in vms])
        ds = _shared_overlap([list(vm.vsphere_datastores or []) for vm in vms])
        hints = {(vm.application_hint or "").strip().lower() for vm in vms if vm.application_hint}
        envs = {(vm.environment or "").strip().lower() for vm in vms if vm.environment}
        oses = {(vm.os_family or "").strip().lower() for vm in vms if vm.os_family}

        notes_parts = []
        if len(vms) >= 2 and sequential:
            notes_parts.append(f"{len(vms)} VMs with sequential names (likely HA group)")
        if nets:
            notes_parts.append(f"share networks: {', '.join(sorted(nets))}")
        if ds:
            notes_parts.append(f"share datastores: {', '.join(sorted(ds))}")
        if hints:
            notes_parts.append(f"application_hint: {', '.join(sorted(hints))}")
        if envs:
            notes_parts.append(f"environment: {', '.join(sorted(envs))}")
        notes = "; ".join(notes_parts) or f"{len(vms)} VMs grouped by {discriminator}"

        key = GroupKey(
            vcenter_id=vcenter_id,
            target_namespace=target_namespace,
            role=role,
            state=state,
            discriminator=discriminator,
            environment=environment,
            target_cluster_id=target_cluster_id,
        )

        # Sort vm_ids so the API output is deterministic across runs.
        vm_ids = sorted(vm.id for vm in vms)

        return VMGroup(
            key=key,
            vm_ids=vm_ids,
            shared_attributes={
                "networks": sorted(nets),
                "datastores": sorted(ds),
                "application_hints": sorted(hints),
                "environments": sorted(envs),
                "os_families": sorted(oses),
            },
            estimated_role=role,
            estimated_state=state,
            migration_risk=risk,
            notes=notes,
            risk_assessment=risk_assessment,
            ha_members=ha_members,
        )

    def _network_datastore_discriminator(self, cluster: list[VM]) -> str:
        nets = _shared_overlap([list(vm.vsphere_networks or []) for vm in cluster])
        ds = _shared_overlap([list(vm.vsphere_datastores or []) for vm in cluster])
        if nets and ds:
            return f"net+ds:{sorted(nets)[0]}+{sorted(ds)[0]}"
        if nets:
            return f"net:{sorted(nets)[0]}"
        if ds:
            return f"ds:{sorted(ds)[0]}"
        return f"cluster:{cluster[0].name}"

    # ------------------------------------------------------------------
    # Consolidation under the LLM input ceiling
    # ------------------------------------------------------------------
    def _consolidate(
        self, groups: list[VMGroup], primary_buckets: dict[tuple, list[VM]]
    ) -> list[VMGroup]:
        if len(groups) <= self.max_groups:
            return groups

        # Group by primary key so we never merge across vCenters, target
        # clusters, or target namespaces (which would break the strongest
        # invariant the planner relies on — MTV CRs are scoped per
        # (vcenter, cluster, namespace) tuple).
        by_primary: dict[tuple, list[VMGroup]] = defaultdict(list)
        for g in groups:
            by_primary[(g.key.vcenter_id, g.key.target_cluster_id, g.key.target_namespace)].append(
                g
            )

        consolidated: list[VMGroup] = []
        for _primary_key, primary_groups in by_primary.items():
            # Sort smallest-first so the consolidation merges the
            # cheapest groups together.
            primary_groups.sort(key=lambda g: len(g.vm_ids))
            # How many groups can this primary key contribute?
            # Roughly proportional to its share of total VMs.
            total_remaining_vms = sum(
                len(g.vm_ids) for primary_list in by_primary.values() for g in primary_list
            )
            primary_vm_count = sum(len(g.vm_ids) for g in primary_groups)
            allowed_here = max(
                1,
                round(self.max_groups * primary_vm_count / max(1, total_remaining_vms)),
            )
            allowed_here = min(allowed_here, len(primary_groups))
            # Keep the largest ``allowed_here - 1`` groups distinct;
            # merge the rest into a single catch-all.
            while len(primary_groups) > allowed_here:
                a = primary_groups.pop(0)
                b = primary_groups.pop(0)
                merged = self._merge_groups(a, b)
                # Insert merged back in sorted order.
                inserted = False
                for i, existing in enumerate(primary_groups):
                    if len(merged.vm_ids) <= len(existing.vm_ids):
                        primary_groups.insert(i, merged)
                        inserted = True
                        break
                if not inserted:
                    primary_groups.append(merged)
            consolidated.extend(primary_groups)

        if len(consolidated) > self.max_groups:
            logger.warning(
                "Pre-classifier could not consolidate to <= %d groups "
                "(landed at %d). Multiple LLM calls will be required "
                "by the planner.",
                self.max_groups,
                len(consolidated),
            )
        return consolidated

    def _merge_groups(self, a: VMGroup, b: VMGroup) -> VMGroup:
        # Merged role = highest-risk role between the two so the merged
        # group inherits the more conservative cutover stance.
        priority = {"data": 5, "infrastructure": 4, "edge": 3, "app": 2, "web": 1, "other": 0}
        role = (
            a.estimated_role
            if priority.get(a.estimated_role, 0) >= priority.get(b.estimated_role, 0)
            else b.estimated_role
        )
        state = (
            "stateful"
            if "stateful" in (a.estimated_state, b.estimated_state)
            else ("unknown" if "unknown" in (a.estimated_state, b.estimated_state) else "stateless")
        )
        risk_priority = {"high": 3, "medium": 2, "low": 1}
        risk = (
            a.migration_risk
            if risk_priority.get(a.migration_risk, 0) >= risk_priority.get(b.migration_risk, 0)
            else b.migration_risk
        )
        key = GroupKey(
            vcenter_id=a.key.vcenter_id,
            target_namespace=a.key.target_namespace,
            role=role,
            state=state,
            discriminator=f"merged:{a.key.discriminator}+{b.key.discriminator}"[:64],
            environment=a.key.environment,
            target_cluster_id=a.key.target_cluster_id,
        )
        merged_ids = sorted(set(a.vm_ids) | set(b.vm_ids))
        shared: dict[str, list[str]] = {}
        for dim in ("networks", "datastores", "application_hints", "environments", "os_families"):
            shared[dim] = sorted(
                set(a.shared_attributes.get(dim, [])) | set(b.shared_attributes.get(dim, []))
            )
        notes = (
            f"Consolidated from {len(a.vm_ids)} + {len(b.vm_ids)} VMs to "
            f"keep LLM input <= {self.max_groups} groups."
        )
        return VMGroup(
            key=key,
            vm_ids=merged_ids,
            shared_attributes=shared,
            estimated_role=role,
            estimated_state=state,
            migration_risk=risk,
            notes=notes,
        )

    # ------------------------------------------------------------------
    # Dependency hints
    # ------------------------------------------------------------------
    def _add_dependency_hints(self, groups: list[VMGroup]) -> list[VMGroup]:
        """Seed the LLM with role-based dependency suggestions.

        Pure heuristic — data groups stand alone, infrastructure stands
        alone, app groups depend on data + infrastructure, web/edge
        depends on app. The LLM may override during wave assignment;
        the hints just shave one reasoning step off the prompt.
        """
        by_role: dict[str, list[str]] = defaultdict(list)
        for g in groups:
            by_role[g.estimated_role].append(g.id)
        for g in groups:
            hints: list[str] = []
            if g.estimated_role == "app":
                hints.extend(by_role.get("data", []))
                hints.extend(by_role.get("infrastructure", []))
            elif g.estimated_role in {"web", "edge"}:
                hints.extend(by_role.get("app", []))
                hints.extend(by_role.get("data", []))
            # Don't list a group as its own dependency.
            g.dependency_hints = [h for h in hints if h != g.id]
        return groups
