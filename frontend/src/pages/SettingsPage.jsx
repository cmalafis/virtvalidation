// Settings.
//
// Four tabs: general (collection schedule + the SSH kill-switch), the
// LLM backend, SSH keys, and compliance.
//
// Two things get prominence they didn't have before:
//
//  * `last_llm_error` renders as a persistent banner at the top of the
//    page, not just inside the LLM tab. It exists so an operator
//    discovers "your MaaS key was rejected" rather than noticing
//    annotation quality quietly dropped.
//
//  * The SSH kill-switch is a labeled switch with its consequence
//    spelled out, because turning it off makes baseline and validation
//    endpoints return 503.

import { useCallback, useEffect, useState } from "react";
import toast from "react-hot-toast";
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardTitle,
  ClipboardCopy,
  ClipboardCopyVariant,
  Content,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  Flex,
  FlexItem,
  Form,
  FormGroup,
  FormHelperText,
  FormSelect,
  FormSelectOption,
  HelperText,
  HelperTextItem,
  PageSection,
  Skeleton,
  Switch,
  Tab,
  TabTitleText,
  Tabs,
} from "@patternfly/react-core";
import { Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";

import PageFrame from "../common/PageFrame";
import StatusLabel from "../common/StatusLabel";
import ConfirmModal from "../common/ConfirmModal";
import { ErrorEmptyState } from "../common/EmptyStates";
import { fetchJSON } from "../utils/fetchJSON";
import ValidationKeys from "./settings/ValidationKeys";

const SCHEDULES = [
  ["twice_daily", "Twice daily (06:00 and 18:00 UTC)"],
  ["once_daily", "Once daily (06:00 UTC)"],
  ["hourly", "Hourly"],
];

const HOST_KEY_POLICIES = [
  ["auto_accept", "Trust on first use (recommended)"],
  ["strict", "Strict — key must already be in known_hosts"],
];

function GeneralTab({ settings, onSaved }) {
  const [draft, setDraft] = useState(settings);
  const [saving, setSaving] = useState(false);
  const [confirmKill, setConfirmKill] = useState(false);

  useEffect(() => setDraft(settings), [settings]);

  const save = async (patch) => {
    setSaving(true);
    try {
      const updated = await fetchJSON("/api/settings", { method: "PUT", body: patch });
      setDraft(updated);
      onSaved(updated);
      toast.success("Settings saved");
    } catch (e) {
      toast.error(e?.message ?? "Could not save");
      setDraft(settings);
    } finally {
      setSaving(false);
    }
  };

  const sshEnabled = draft?.ssh_operations_enabled ?? true;

  return (
    <>
      <Card className="pf-v6-u-mb-md">
        <CardTitle>SSH operations</CardTitle>
        <CardBody>
          <Switch
            id="ssh-kill-switch"
            label="Allow VirtValidate to connect to managed VMs over SSH"
            isChecked={sshEnabled}
            isDisabled={saving}
            onChange={(_e, checked) => {
              // Turning it ON is safe and immediate; turning it OFF stops
              // in-flight capability, so confirm that direction only.
              if (checked) save({ ssh_operations_enabled: true });
              else setConfirmKill(true);
            }}
          />
          <FormHelperText>
            <HelperText>
              <HelperTextItem variant={sshEnabled ? "default" : "warning"}>
                {sshEnabled
                  ? "Baseline capture and validation can reach managed hosts. Every command is read-only and recorded in agent activity."
                  : "Baseline capture and validation are disabled — those endpoints return 503 while this is off."}
              </HelperTextItem>
            </HelperText>
          </FormHelperText>
        </CardBody>
      </Card>

      <Card>
        <CardTitle>Collection</CardTitle>
        <CardBody>
          <Form>
            <FormGroup label="Baseline schedule" fieldId="sched">
              <FormSelect
                id="sched"
                value={draft?.schedule_preset ?? "twice_daily"}
                isDisabled={saving}
                onChange={(_e, v) => save({ schedule_preset: v })}
              >
                {SCHEDULES.map(([value, label]) => (
                  <FormSelectOption key={value} value={value} label={label} />
                ))}
              </FormSelect>
              <FormHelperText>
                <HelperText>
                  <HelperTextItem>
                    How often automatic baseline collection runs. Several
                    captures over a few days let the validator tell normal
                    variation from migration damage.
                  </HelperTextItem>
                </HelperText>
              </FormHelperText>
            </FormGroup>

            <FormGroup label="SSH host key policy" fieldId="hostkey">
              <FormSelect
                id="hostkey"
                value={draft?.ssh_host_key_policy ?? "auto_accept"}
                isDisabled={saving}
                onChange={(_e, v) => save({ ssh_host_key_policy: v })}
              >
                {HOST_KEY_POLICIES.map(([value, label]) => (
                  <FormSelectOption key={value} value={value} label={label} />
                ))}
              </FormSelect>
              <FormHelperText>
                <HelperText>
                  <HelperTextItem>
                    Strict rejects any host whose key isn&apos;t already in
                    known_hosts — for sites where keys are distributed out
                    of band.
                  </HelperTextItem>
                </HelperText>
              </FormHelperText>
            </FormGroup>
          </Form>
        </CardBody>
      </Card>

      <ConfirmModal
        isOpen={confirmKill}
        title="Disable all SSH operations?"
        confirmLabel="Disable SSH"
        isDanger
        onConfirm={async () => {
          await save({ ssh_operations_enabled: false });
          setConfirmKill(false);
        }}
        onClose={() => setConfirmKill(false)}
      >
        Baseline capture and validation will be refused with a 503 until
        this is turned back on. Use this as a kill-switch when you need
        VirtValidate to stop touching production immediately.
      </ConfirmModal>
    </>
  );
}

function LLMTab({ llm, onSaved }) {
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);

  const backends = llm?.available_backends ?? [];

  const select = async (type) => {
    setSaving(true);
    setTestResult(null);
    try {
      const updated = await fetchJSON("/api/settings/llm", {
        method: "PUT",
        body: { active_llm_backend: type },
      });
      onSaved(updated);
      toast.success(`Active backend is now ${type}`);
    } catch (e) {
      toast.error(e?.message ?? "Could not switch backend");
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const result = await fetchJSON("/api/settings/llm/test-connection", {
        method: "POST",
        body: { active_llm_backend: llm?.active_llm_backend },
      });
      setTestResult(result);
    } catch (e) {
      setTestResult({ ok: false, error: e?.message ?? "Test failed" });
    } finally {
      setTesting(false);
    }
  };

  return (
    <Card>
      <CardTitle>Inference backend</CardTitle>
      <CardBody>
        <Content component="p" className="pf-v6-u-mb-md pf-v6-u-color-200">
          The active backend is a runtime setting — switching it takes
          effect without a redeploy. Connection details come from the
          deployment&apos;s environment.
        </Content>

        <Table aria-label="LLM backends" variant="compact">
          <Thead>
            <Tr>
              <Th width={20}>Backend</Th>
              <Th width={40}>Status</Th>
              <Th width={40}>Action</Th>
            </Tr>
          </Thead>
          <Tbody>
            {backends.map((b) => {
              const isActive = b.type === llm?.active_llm_backend;
              return (
                <Tr key={b.type}>
                  <Td dataLabel="Backend">
                    {b.label || b.type}
                    {b.dev_only && (
                      <StatusLabel kind="severity" value="warn" className="pf-v6-u-ml-sm">
                        DEV ONLY
                      </StatusLabel>
                    )}
                  </Td>
                  <Td dataLabel="Status">
                    {b.configured ? (
                      <StatusLabel kind="status" value="healthy">
                        Configured
                      </StatusLabel>
                    ) : (
                      <>
                        <StatusLabel kind="status" value="pending">
                          Not configured
                        </StatusLabel>
                        {(b.missing_config ?? []).length > 0 && (
                          <Content
                            component="small"
                            className="pf-v6-u-display-block pf-v6-u-color-200"
                          >
                            {`Missing: ${b.missing_config.join(", ")}`}
                          </Content>
                        )}
                      </>
                    )}
                  </Td>
                  <Td dataLabel="Action">
                    {isActive ? (
                      <Flex spaceItems={{ default: "spaceItemsSm" }}>
                        <FlexItem>
                          <StatusLabel kind="status" value="healthy">
                            Active
                          </StatusLabel>
                        </FlexItem>
                        <FlexItem>
                          <Button
                            variant="link"
                            isInline
                            onClick={test}
                            isLoading={testing}
                            isDisabled={testing}
                          >
                            Test connection
                          </Button>
                        </FlexItem>
                      </Flex>
                    ) : (
                      <Button
                        variant="secondary"
                        isDisabled={saving || !b.configured}
                        onClick={() => select(b.type)}
                      >
                        Make active
                      </Button>
                    )}
                  </Td>
                </Tr>
              );
            })}
          </Tbody>
        </Table>

        {testResult && (
          <Alert
            className="pf-v6-u-mt-md"
            variant={testResult?.ok === false || testResult?.error ? "danger" : "success"}
            isInline
            title={
              testResult?.ok === false || testResult?.error
                ? "Connection test failed"
                : "Connection test succeeded"
            }
          >
            {testResult?.error ??
              testResult?.message ??
              `Reached ${llm?.active_llm_backend}.`}
          </Alert>
        )}
      </CardBody>
    </Card>
  );
}

function ComplianceTab({ fips, sshKey, onKeyChanged }) {
  const [busy, setBusy] = useState(false);
  const [confirmRotate, setConfirmRotate] = useState(false);

  const generate = async () => {
    setBusy(true);
    try {
      await fetchJSON("/api/system/ssh-key/generate", { method: "POST", body: {} });
      toast.success("Appliance SSH key generated");
      onKeyChanged();
    } catch (e) {
      toast.error(e?.message ?? "Could not generate the key");
    } finally {
      setBusy(false);
    }
  };

  const rotate = async () => {
    await fetchJSON("/api/system/ssh-key/rotate", { method: "POST", body: {} });
    toast.success("Appliance SSH key rotated");
    setConfirmRotate(false);
    onKeyChanged();
  };

  const hasKey = Boolean(sshKey?.public_key);

  return (
    <>
      <Card className="pf-v6-u-mb-md">
        <CardTitle>Appliance SSH key</CardTitle>
        <CardBody>
          {hasKey ? (
            <>
              <ClipboardCopy
                isReadOnly
                isCode
                variant={ClipboardCopyVariant.expansion}
                hoverTip="Copy"
                clickTip="Copied"
              >
                {sshKey.public_key}
              </ClipboardCopy>
              <DescriptionList isCompact isHorizontal className="pf-v6-u-mt-md">
                <DescriptionListGroup>
                  <DescriptionListTerm>Algorithm</DescriptionListTerm>
                  <DescriptionListDescription>
                    {sshKey?.algorithm ?? "—"}
                  </DescriptionListDescription>
                </DescriptionListGroup>
                <DescriptionListGroup>
                  <DescriptionListTerm>Fingerprint</DescriptionListTerm>
                  <DescriptionListDescription>
                    <code>{sshKey?.fingerprint ?? "—"}</code>
                  </DescriptionListDescription>
                </DescriptionListGroup>
              </DescriptionList>
              <Button
                variant="secondary"
                className="pf-v6-u-mt-md"
                onClick={() => setConfirmRotate(true)}
                isDisabled={busy}
              >
                Rotate key
              </Button>
            </>
          ) : (
            <>
              <Alert variant="warning" isInline title="No appliance key yet">
                VirtValidate needs an SSH key before it can reach any managed
                VM. The private key is generated on and never leaves the
                appliance.
              </Alert>
              <Button
                variant="primary"
                className="pf-v6-u-mt-md"
                onClick={generate}
                isLoading={busy}
                isDisabled={busy}
              >
                Generate key
              </Button>
            </>
          )}
        </CardBody>
      </Card>

      <Card>
        <CardTitle>FIPS 140-3</CardTitle>
        <CardBody>
          <DescriptionList isCompact isHorizontal className="pf-v6-u-mb-md">
            <DescriptionListGroup>
              <DescriptionListTerm>Configured</DescriptionListTerm>
              <DescriptionListDescription>
                {fips?.configured ? "Yes" : "No"}
              </DescriptionListDescription>
            </DescriptionListGroup>
            <DescriptionListGroup>
              <DescriptionListTerm>Host OS in FIPS mode</DescriptionListTerm>
              <DescriptionListDescription>
                {fips?.detected ? "Yes" : "No"}
              </DescriptionListDescription>
            </DescriptionListGroup>
            <DescriptionListGroup>
              <DescriptionListTerm>Effective</DescriptionListTerm>
              <DescriptionListDescription>
                <StatusLabel kind="status" value={fips?.effective ? "healthy" : "pending"}>
                  {fips?.effective ? "Active" : "Not active"}
                </StatusLabel>
              </DescriptionListDescription>
            </DescriptionListGroup>
          </DescriptionList>

          {fips?.mismatch_warning && (
            <Alert variant="warning" isInline title="FIPS configuration mismatch">
              {fips.mismatch_warning}
            </Alert>
          )}

          {(fips?.operations ?? []).length > 0 && (
            <Table aria-label="FIPS operations" variant="compact" className="pf-v6-u-mt-md">
              <Thead>
                <Tr>
                  <Th width={35}>Operation</Th>
                  <Th width={35}>Configured</Th>
                  <Th width={30}>FIPS approved</Th>
                </Tr>
              </Thead>
              <Tbody>
                {fips.operations.map((op) => (
                  <Tr key={op.name}>
                    <Td dataLabel="Operation">{op.name}</Td>
                    <Td dataLabel="Configured">{String(op.configured)}</Td>
                    <Td dataLabel="FIPS approved">
                      <StatusLabel
                        kind="status"
                        value={op.fips_approved ? "healthy" : "failed"}
                      >
                        {op.fips_approved ? "Yes" : "No"}
                      </StatusLabel>
                    </Td>
                  </Tr>
                ))}
              </Tbody>
            </Table>
          )}
        </CardBody>
      </Card>

      <ConfirmModal
        isOpen={confirmRotate}
        title="Rotate the appliance SSH key?"
        confirmLabel="Rotate"
        isDanger
        onConfirm={rotate}
        onClose={() => setConfirmRotate(false)}
      >
        A new keypair is generated immediately. Every managed VM keeps the
        OLD public key in authorized_keys until you distribute the new one,
        so collection will fail against those hosts in the meantime.
      </ConfirmModal>
    </>
  );
}

export default function SettingsPage() {
  const [tab, setTab] = useState("general");
  const [data, setData] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const [settings, llm, fips, sshKey] = await Promise.allSettled([
        fetchJSON("/api/settings"),
        fetchJSON("/api/settings/llm"),
        fetchJSON("/api/system/fips-status"),
        fetchJSON("/api/system/ssh-key"),
      ]);
      if (settings.status === "rejected") throw settings.reason;
      const val = (r) => (r.status === "fulfilled" ? r.value : null);
      setData({
        settings: settings.value,
        llm: val(llm),
        fips: val(fips),
        sshKey: val(sshKey),
      });
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const frameProps = {
    title: "Settings",
    breadcrumbs: [{ label: "Administration" }, { label: "Settings" }],
  };

  if (error) {
    return (
      <PageFrame {...frameProps}>
        <PageSection>
          <ErrorEmptyState error={error} onRetry={load} isRetrying={loading} />
        </PageSection>
      </PageFrame>
    );
  }

  if (loading && !data.settings) {
    return (
      <PageFrame {...frameProps}>
        <PageSection>
          <Skeleton width="100%" height="200px" />
        </PageSection>
      </PageFrame>
    );
  }

  const lastError = data.llm?.last_llm_error;

  return (
    <PageFrame
      {...frameProps}
      description="Runtime configuration. Changes take effect without a redeploy."
    >
      {lastError && (
        <PageSection hasBodyWrapper={false}>
          {/* Persistent and page-level, not tucked into the LLM tab: this
              banner is how an operator learns a key was rejected rather
              than noticing annotation quality quietly dropped. */}
          <Alert variant="danger" isInline title="The last LLM call failed">
            {lastError}
            {data.llm?.last_llm_error_at && (
              <Content component="small" className="pf-v6-u-display-block pf-v6-u-mt-sm">
                {new Date(data.llm.last_llm_error_at).toLocaleString()}
              </Content>
            )}
          </Alert>
        </PageSection>
      )}

      <PageSection hasBodyWrapper={false}>
        <Tabs activeKey={tab} onSelect={(_e, k) => setTab(k)} aria-label="Settings sections">
          <Tab eventKey="general" title={<TabTitleText>General</TabTitleText>}>
            <div className="pf-v6-u-mt-md">
              <GeneralTab
                settings={data.settings}
                onSaved={(s) => setData((d) => ({ ...d, settings: s }))}
              />
            </div>
          </Tab>
          <Tab eventKey="llm" title={<TabTitleText>Inference</TabTitleText>}>
            <div className="pf-v6-u-mt-md">
              <LLMTab llm={data.llm} onSaved={(l) => setData((d) => ({ ...d, llm: l }))} />
            </div>
          </Tab>
          <Tab eventKey="keys" title={<TabTitleText>SSH keys</TabTitleText>}>
            <div className="pf-v6-u-mt-md">
              <ValidationKeys fipsMode={Boolean(data.fips?.effective)} />
            </div>
          </Tab>
          <Tab eventKey="compliance" title={<TabTitleText>Compliance</TabTitleText>}>
            <div className="pf-v6-u-mt-md">
              <ComplianceTab fips={data.fips} sshKey={data.sshKey} onKeyChanged={load} />
            </div>
          </Tab>
        </Tabs>
      </PageSection>
    </PageFrame>
  );
}
