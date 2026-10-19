// Where one wave's networks and disks land on OpenShift.
//
// `wave.mapping_summary` and `wave.mapping_findings` are written by Stage 7
// (and by manual edits) from app.core.mapping_checks.wave_mapping_view —
// deterministic, not LLM output. Plans generated before that existed carry
// neither, and this renders nothing for them.

import { Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";
import { Alert, Content, Label, List, ListItem } from "@patternfly/react-core";

import { asArray } from "../../utils/asArray";

export const DESTINATION_LABELS = {
  pod: { label: "Pod network", color: "grey" },
  primary_udn: { label: "Primary UDN", color: "purple" },
  localnet: { label: "Localnet", color: "teal" },
  multus: { label: "Multus", color: "blue" },
  ignored: { label: "Dropped", color: "orange" },
  unmapped: { label: "Unmapped", color: "red" },
};

const SEVERITY_VARIANT = { blocking: "danger", warning: "warning", info: "info" };
const SEVERITY_TITLE = {
  blocking: "Will fail or land VMs in the wrong place",
  warning: "Decide knowingly",
  info: "For information",
};

/** Compact header labels: "2 primary UDN", "1 NIC dropped", "Copy offload". */
export function waveMappingBadges(wave) {
  const nets = asArray(wave?.mapping_summary?.networks);
  const stores = asArray(wave?.mapping_summary?.storage);
  const findings = asArray(wave?.mapping_findings);
  const count = (kind) => nets.filter((n) => n?.kind === kind).length;
  const badges = [];
  if (count("primary_udn")) badges.push({ text: "Primary UDN", color: "purple" });
  if (count("localnet")) badges.push({ text: "Localnet", color: "teal" });
  if (count("ignored"))
    badges.push({ text: `${count("ignored")} network(s) dropped`, color: "orange" });
  if (stores.some((s) => s?.offload)) badges.push({ text: "Copy offload", color: "green" });
  if (findings.some((f) => f?.severity === "blocking"))
    badges.push({ text: "Blocking check", color: "red" });
  return badges;
}

export default function WaveMappingSummary({ wave }) {
  const nets = asArray(wave?.mapping_summary?.networks);
  const stores = asArray(wave?.mapping_summary?.storage);
  const findings = asArray(wave?.mapping_findings);
  if (nets.length === 0 && stores.length === 0 && findings.length === 0) return null;

  return (
    <div className="pf-v6-u-mt-md">
      <Content component="p" className="pf-v6-u-font-weight-bold pf-v6-u-font-size-sm">
        Where this wave lands
      </Content>
      {nets.length > 0 && (
        <Table aria-label="Wave networks" variant="compact" borders={false}>
          <Thead>
            <Tr>
              <Th width={35}>vSphere network</Th>
              <Th width={35}>Target</Th>
              <Th width={20}>Lands as</Th>
              <Th width={10}>VMs</Th>
            </Tr>
          </Thead>
          <Tbody>
            {nets.map((n) => {
              const dest = DESTINATION_LABELS[n?.kind] ?? DESTINATION_LABELS.unmapped;
              return (
                <Tr key={n?.source}>
                  <Td dataLabel="vSphere network">{n?.source}</Td>
                  <Td dataLabel="Target">{n?.target ?? "—"}</Td>
                  <Td dataLabel="Lands as">
                    <Label isCompact color={dest.color}>
                      {dest.label}
                    </Label>
                  </Td>
                  <Td dataLabel="VMs">{n?.vm_count ?? 0}</Td>
                </Tr>
              );
            })}
          </Tbody>
        </Table>
      )}
      {stores.length > 0 && (
        <Table aria-label="Wave storage" variant="compact" borders={false} className="pf-v6-u-mt-sm">
          <Thead>
            <Tr>
              <Th width={35}>Datastore</Th>
              <Th width={35}>StorageClass</Th>
              <Th width={20}>Access / volume</Th>
              <Th width={10}>VMs</Th>
            </Tr>
          </Thead>
          <Tbody>
            {stores.map((s) => (
              <Tr key={s?.source}>
                <Td dataLabel="Datastore">{s?.source}</Td>
                <Td dataLabel="StorageClass">
                  {s?.storage_class ?? "—"}
                  {s?.offload && (
                    <Label isCompact color="green" className="pf-v6-u-ml-xs">
                      {`Offload: ${s.offload}`}
                    </Label>
                  )}
                </Td>
                <Td dataLabel="Access / volume">
                  {[s?.access_mode, s?.volume_mode].filter(Boolean).join(" · ") || "Profile default"}
                </Td>
                <Td dataLabel="VMs">{s?.vm_count ?? 0}</Td>
              </Tr>
            ))}
          </Tbody>
        </Table>
      )}
      {["blocking", "warning", "info"].map((sev) => {
        const items = findings.filter((f) => f?.severity === sev);
        if (items.length === 0) return null;
        return (
          <Alert
            key={sev}
            variant={SEVERITY_VARIANT[sev]}
            isInline
            isPlain
            title={SEVERITY_TITLE[sev]}
            className="pf-v6-u-mt-sm"
          >
            <List>
              {items.map((f) => (
                <ListItem key={`${f?.code}-${f?.source ?? ""}-${f?.target ?? ""}`}>
                  {f?.message}
                  {(f?.vm_count ?? 0) > 0
                    ? ` (${f.vm_count} VM${f.vm_count === 1 ? "" : "s"}: ${asArray(f?.vm_names)
                        .slice(0, 4)
                        .join(", ")}${f.vm_count > 4 ? ", …" : ""})`
                    : ""}
                </ListItem>
              ))}
            </List>
          </Alert>
        );
      })}
    </div>
  );
}
