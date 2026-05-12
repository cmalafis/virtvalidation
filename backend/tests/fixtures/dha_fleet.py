"""Synthetic federal hospital fleet — 57 VMs across 8 applications.

Mirrors the customer scenario that broke the planner in May 2026: a
DoD-adjacent health-care provider with EHR, PACS imaging, identity,
infrastructure, and a legacy app, spread across production, dev, and
DR environments. The fleet is fictional but the metadata distribution
matches the customer's actual RVTools export.

What this fixture exists for:

  - End-to-end planner tests can validate that the preclassifier
    produces 5-12 rich groups from realistic input (not 20 thin
    groups, which is the failure mode this whole effort fixed).
  - The robustness tests can confirm retry + mechanical fallback
    work at the size that originally broke the LLM path.
  - Test data documentation (``docs/TEST_DATA.md``) cites this as
    the canonical example of what populated metadata looks like.

Apps + counts (matches spec):
  - EHRPro          — 12 VMs (3 web, 4 app, 3 data, 2 worker)
  - PACSImaging     — 10 VMs (2 web, 4 app, 2 data, 2 imaging-process)
  - IdentityServices —  8 VMs (4 AD-DC, 2 RHIDM, 2 PKI)
  - InfraServices   —  6 VMs (2 DNS, 2 NTP, 2 Backup)
  - LegacyApp       —  8 VMs (2 web, 4 app, 2 data)
  - DevSan          —  6 VMs (dev sandbox)
  - EHRStaging      —  4 VMs (1 web, 2 app, 1 data)
  - EHRDR           —  3 VMs (web/app/data DR)

Total: 57 VMs across 8 apps × ~3 tiers × prod/dev/DR environments.

The VM model only carries a subset of the RVTools columns (no
cluster, host, folder, resource_pool — those land in the VM
``role`` + ``application_hint`` + ``vsphere_networks`` +
``vsphere_datastores`` + ``environment`` fields that the
preclassifier reads). Adding cluster/host/folder columns to the VM
model is a deferred enhancement — tracked in PLANNER_ARCHITECTURE.md
"Required VM metadata".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.vm import VM


@dataclass
class FleetVMSpec:
    """One row of the synthetic RVTools export.

    Captures the realistic metadata we'd see from a federal hospital's
    inventory tool. The preclassifier maps these into its grouping
    dimensions; tests assert that the resulting groups carry the
    expected shared_attributes.
    """

    name: str
    application_hint: str          # App custom attribute
    environment: str               # Production | Development | DR
    role_token: str                # web | app | data | worker | dc | pki | dns | ntp | backup | sandbox | imaging
    vcenter_id: int                # source_vcenter_id
    target_namespace: str
    networks: list[str] = field(default_factory=list)
    datastores: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Domain knobs — pinned constants the fixture builds from
# ---------------------------------------------------------------------------
_VCENTER_PROD = 1
_VCENTER_DEV = 2
_VCENTER_DR = 3

_NETWORKS = {
    "ehrpro-web":  "VLAN-100-Web-Prod",
    "ehrpro-app":  "VLAN-110-App-Prod",
    "ehrpro-data": "VLAN-120-Data-Prod",
    "pacs-web":    "VLAN-100-Web-Prod",
    "pacs-app":    "VLAN-110-App-Prod",
    "pacs-data":   "VLAN-120-Data-Prod",
    "identity":    "VLAN-900-Mgmt-Infra",
    "infra":       "VLAN-900-Mgmt-Infra",
    "legacy-web":  "VLAN-150-Legacy-Prod",
    "legacy-app":  "VLAN-160-Legacy-Prod",
    "legacy-data": "VLAN-170-Legacy-Prod",
    "dev-web":     "VLAN-200-Web-Dev",
    "dev-app":     "VLAN-210-App-Dev",
    "dev-data":    "VLAN-220-Data-Dev",
    "dr-web":      "VLAN-300-Web-DR",
    "dr-app":      "VLAN-310-App-DR",
    "dr-data":     "VLAN-320-Data-DR",
}

_DATASTORES = {
    "prod-gold":     "prod-gold-ssd-01",      # data tier
    "prod-silver":   "prod-silver-hdd-01",    # app tier
    "prod-bronze":   "prod-bronze-archive",   # worker / backup
    "infra-shared":  "infra-shared-01",
    "dev-shared":    "dev-shared-01",
    "dr-shared":     "dr-shared-01",
}


def _net(*keys: str) -> list[str]:
    return [_NETWORKS[k] for k in keys]


def _ds(*keys: str) -> list[str]:
    return [_DATASTORES[k] for k in keys]


# ---------------------------------------------------------------------------
# Fleet definition
# ---------------------------------------------------------------------------
def _ehrpro() -> list[FleetVMSpec]:
    """12 VMs: 3 web + 4 app + 3 data + 2 worker."""
    out: list[FleetVMSpec] = []
    for i in range(1, 4):
        out.append(FleetVMSpec(
            name=f"ehrpro-web-{i:02d}",
            application_hint="ehrpro",
            environment="production",
            role_token="web",
            vcenter_id=_VCENTER_PROD,
            target_namespace="ehrpro-prod",
            networks=_net("ehrpro-web"),
            datastores=_ds("prod-silver"),
        ))
    for i in range(1, 5):
        out.append(FleetVMSpec(
            name=f"ehrpro-app-{i:02d}",
            application_hint="ehrpro",
            environment="production",
            role_token="app",
            vcenter_id=_VCENTER_PROD,
            target_namespace="ehrpro-prod",
            networks=_net("ehrpro-app"),
            datastores=_ds("prod-silver"),
        ))
    for i in range(1, 4):
        out.append(FleetVMSpec(
            name=f"ehrpro-postgres-db-{i:02d}",
            application_hint="ehrpro",
            environment="production",
            role_token="data",
            vcenter_id=_VCENTER_PROD,
            target_namespace="ehrpro-prod",
            networks=_net("ehrpro-data"),
            datastores=_ds("prod-gold"),
        ))
    for i in range(1, 3):
        out.append(FleetVMSpec(
            name=f"ehrpro-worker-{i:02d}",
            application_hint="ehrpro",
            environment="production",
            role_token="worker",
            vcenter_id=_VCENTER_PROD,
            target_namespace="ehrpro-prod",
            networks=_net("ehrpro-app"),
            datastores=_ds("prod-bronze"),
        ))
    return out


def _pacs() -> list[FleetVMSpec]:
    """10 VMs: 2 web + 4 app + 2 data + 2 imaging."""
    out: list[FleetVMSpec] = []
    for i in range(1, 3):
        out.append(FleetVMSpec(
            name=f"pacs-web-{i:02d}",
            application_hint="pacsimaging",
            environment="production",
            role_token="web",
            vcenter_id=_VCENTER_PROD,
            target_namespace="pacs-prod",
            networks=_net("pacs-web"),
            datastores=_ds("prod-silver"),
        ))
    for i in range(1, 5):
        out.append(FleetVMSpec(
            name=f"pacs-app-{i:02d}",
            application_hint="pacsimaging",
            environment="production",
            role_token="app",
            vcenter_id=_VCENTER_PROD,
            target_namespace="pacs-prod",
            networks=_net("pacs-app"),
            datastores=_ds("prod-silver"),
        ))
    for i in range(1, 3):
        out.append(FleetVMSpec(
            name=f"pacs-db-{i:02d}",
            application_hint="pacsimaging",
            environment="production",
            role_token="data",
            vcenter_id=_VCENTER_PROD,
            target_namespace="pacs-prod",
            networks=_net("pacs-data"),
            datastores=_ds("prod-gold"),
        ))
    for i in range(1, 3):
        out.append(FleetVMSpec(
            name=f"pacs-imaging-process-{i:02d}",
            application_hint="pacsimaging",
            environment="production",
            role_token="imaging",
            vcenter_id=_VCENTER_PROD,
            target_namespace="pacs-prod",
            networks=_net("pacs-app"),
            datastores=_ds("prod-gold"),
        ))
    return out


def _identity() -> list[FleetVMSpec]:
    """8 VMs: 4 AD-DC + 2 RHIDM + 2 PKI."""
    out: list[FleetVMSpec] = []
    for i in range(1, 5):
        out.append(FleetVMSpec(
            name=f"ad-dc-{i:02d}",
            application_hint="identityservices",
            environment="production",
            role_token="dc",
            vcenter_id=_VCENTER_PROD,
            target_namespace="identity-prod",
            networks=_net("identity"),
            datastores=_ds("infra-shared"),
        ))
    for i in range(1, 3):
        out.append(FleetVMSpec(
            name=f"rhidm-{i:02d}",
            application_hint="identityservices",
            environment="production",
            role_token="dc",
            vcenter_id=_VCENTER_PROD,
            target_namespace="identity-prod",
            networks=_net("identity"),
            datastores=_ds("infra-shared"),
        ))
    for i in range(1, 3):
        out.append(FleetVMSpec(
            name=f"pki-{i:02d}",
            application_hint="identityservices",
            environment="production",
            role_token="pki",
            vcenter_id=_VCENTER_PROD,
            target_namespace="identity-prod",
            networks=_net("identity"),
            datastores=_ds("infra-shared"),
        ))
    return out


def _infra() -> list[FleetVMSpec]:
    """6 VMs: 2 DNS + 2 NTP + 2 Backup."""
    out: list[FleetVMSpec] = []
    for i in range(1, 3):
        out.append(FleetVMSpec(
            name=f"dns-{i:02d}",
            application_hint="infraservices",
            environment="production",
            role_token="dns",
            vcenter_id=_VCENTER_PROD,
            target_namespace="infra-prod",
            networks=_net("infra"),
            datastores=_ds("infra-shared"),
        ))
    for i in range(1, 3):
        out.append(FleetVMSpec(
            name=f"ntp-{i:02d}",
            application_hint="infraservices",
            environment="production",
            role_token="ntp",
            vcenter_id=_VCENTER_PROD,
            target_namespace="infra-prod",
            networks=_net("infra"),
            datastores=_ds("infra-shared"),
        ))
    for i in range(1, 3):
        out.append(FleetVMSpec(
            name=f"backup-{i:02d}",
            application_hint="infraservices",
            environment="production",
            role_token="backup",
            vcenter_id=_VCENTER_PROD,
            target_namespace="infra-prod",
            networks=_net("infra"),
            datastores=_ds("prod-bronze"),
        ))
    return out


def _legacy() -> list[FleetVMSpec]:
    """8 VMs: 2 web + 4 app + 2 data."""
    out: list[FleetVMSpec] = []
    for i in range(1, 3):
        out.append(FleetVMSpec(
            name=f"legacy-web-{i:02d}",
            application_hint="legacyapp",
            environment="production",
            role_token="web",
            vcenter_id=_VCENTER_PROD,
            target_namespace="legacy-prod",
            networks=_net("legacy-web"),
            datastores=_ds("prod-silver"),
        ))
    for i in range(1, 5):
        out.append(FleetVMSpec(
            name=f"legacy-app-{i:02d}",
            application_hint="legacyapp",
            environment="production",
            role_token="app",
            vcenter_id=_VCENTER_PROD,
            target_namespace="legacy-prod",
            networks=_net("legacy-app"),
            datastores=_ds("prod-silver"),
        ))
    for i in range(1, 3):
        out.append(FleetVMSpec(
            name=f"legacy-oracle-db-{i:02d}",
            application_hint="legacyapp",
            environment="production",
            role_token="data",
            vcenter_id=_VCENTER_PROD,
            target_namespace="legacy-prod",
            networks=_net("legacy-data"),
            datastores=_ds("prod-gold"),
        ))
    return out


def _devsan() -> list[FleetVMSpec]:
    """6 dev sandbox VMs in vCenter 2 (dev)."""
    out: list[FleetVMSpec] = []
    for i in range(1, 7):
        role = ["web", "web", "app", "app", "data", "data"][i - 1]
        out.append(FleetVMSpec(
            name=f"devsan-{role}-{i:02d}",
            application_hint="devsan",
            environment="development",
            role_token=role,
            vcenter_id=_VCENTER_DEV,
            target_namespace="devsan",
            networks=_net(f"dev-{role}"),
            datastores=_ds("dev-shared"),
        ))
    return out


def _ehr_staging() -> list[FleetVMSpec]:
    """4 EHR staging VMs in dev vCenter: 1 web + 2 app + 1 data."""
    out: list[FleetVMSpec] = []
    out.append(FleetVMSpec(
        name="ehr-staging-web-01",
        application_hint="ehrstaging",
        environment="development",
        role_token="web",
        vcenter_id=_VCENTER_DEV,
        target_namespace="ehr-staging",
        networks=_net("dev-web"),
        datastores=_ds("dev-shared"),
    ))
    for i in range(1, 3):
        out.append(FleetVMSpec(
            name=f"ehr-staging-app-{i:02d}",
            application_hint="ehrstaging",
            environment="development",
            role_token="app",
            vcenter_id=_VCENTER_DEV,
            target_namespace="ehr-staging",
            networks=_net("dev-app"),
            datastores=_ds("dev-shared"),
        ))
    out.append(FleetVMSpec(
        name="ehr-staging-db-01",
        application_hint="ehrstaging",
        environment="development",
        role_token="data",
        vcenter_id=_VCENTER_DEV,
        target_namespace="ehr-staging",
        networks=_net("dev-data"),
        datastores=_ds("dev-shared"),
    ))
    return out


def _ehr_dr() -> list[FleetVMSpec]:
    """3 EHR DR VMs in DR vCenter."""
    return [
        FleetVMSpec(
            name="ehr-dr-web-01",
            application_hint="ehrdr",
            environment="dr",
            role_token="web",
            vcenter_id=_VCENTER_DR,
            target_namespace="ehr-dr",
            networks=_net("dr-web"),
            datastores=_ds("dr-shared"),
        ),
        FleetVMSpec(
            name="ehr-dr-app-01",
            application_hint="ehrdr",
            environment="dr",
            role_token="app",
            vcenter_id=_VCENTER_DR,
            target_namespace="ehr-dr",
            networks=_net("dr-app"),
            datastores=_ds("dr-shared"),
        ),
        FleetVMSpec(
            name="ehr-dr-postgres-db-01",
            application_hint="ehrdr",
            environment="dr",
            role_token="data",
            vcenter_id=_VCENTER_DR,
            target_namespace="ehr-dr",
            networks=_net("dr-data"),
            datastores=_ds("dr-shared"),
        ),
    ]


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
def dha_fleet_specs() -> list[FleetVMSpec]:
    """Return the 57-VM fleet as plain specs (no SQLAlchemy session)."""
    out: list[FleetVMSpec] = []
    out.extend(_ehrpro())
    out.extend(_pacs())
    out.extend(_identity())
    out.extend(_infra())
    out.extend(_legacy())
    out.extend(_devsan())
    out.extend(_ehr_staging())
    out.extend(_ehr_dr())
    return out


def build_dha_fleet_vms() -> list[VM]:
    """Materialize the fleet as detached VM model instances with ids.

    Detached (no session add) so unit tests using SQLAlchemy fixtures
    don't have to commit them. Database integration tests should
    instead call :func:`persist_dha_fleet` against the test session.
    """
    specs = dha_fleet_specs()
    vms: list[VM] = []
    for i, spec in enumerate(specs, start=1):
        vm = VM(
            name=spec.name,
            source_hostname=f"{spec.name}.us-east.example.mil",
            source_vcenter_id=spec.vcenter_id,
            target_namespace=spec.target_namespace,
            application_hint=spec.application_hint,
            environment=spec.environment,
            os_family="rhel" if spec.role_token != "dc" else "windows",
            role=spec.role_token,
            vsphere_networks=list(spec.networks),
            vsphere_datastores=list(spec.datastores),
        )
        vm.id = i
        vms.append(vm)
    return vms


def persist_dha_fleet(db_session) -> list[VM]:
    """Insert the fleet into a test DB session and return the rows."""
    specs = dha_fleet_specs()
    vms: list[VM] = []
    for spec in specs:
        vm = VM(
            name=spec.name,
            source_hostname=f"{spec.name}.us-east.example.mil",
            source_vcenter_id=spec.vcenter_id,
            target_namespace=spec.target_namespace,
            application_hint=spec.application_hint,
            environment=spec.environment,
            os_family="rhel" if spec.role_token != "dc" else "windows",
            role=spec.role_token,
            vsphere_networks=list(spec.networks),
            vsphere_datastores=list(spec.datastores),
        )
        db_session.add(vm)
        vms.append(vm)
    db_session.commit()
    for vm in vms:
        db_session.refresh(vm)
    return vms
