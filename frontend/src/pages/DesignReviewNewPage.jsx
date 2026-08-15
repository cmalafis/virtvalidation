// New design review — one component for both kinds.
//
// NetworkReviewNew and StorageReviewNew were 296 and 266 lines of
// near-identical code differing only in endpoint, wording, and where they
// navigated on success. They're a single parameterized page here.
//
// File upload is kept for both the notes and the YAML: operators paste
// for a quick check but attach a real file for anything sizeable.

import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import toast from "react-hot-toast";
import {
  ActionGroup,
  Button,
  Checkbox,
  Form,
  FormGroup,
  FormHelperText,
  HelperText,
  HelperTextItem,
  PageSection,
  TextArea,
  TextInput,
  Content,
} from "@patternfly/react-core";

import PageFrame from "../common/PageFrame";
import { fetchJSON } from "../utils/fetchJSON";

export const REVIEW_KINDS = {
  network: {
    label: "Network",
    endpoint: "/api/network-reviews",
    detailPath: (id) => `/design-reviews/${id}`,
    yamlLabel: "Proposed network design (YAML)",
    yamlHelp:
      "NetworkAttachmentDefinitions, NodeNetworkConfigurationPolicies, or whatever describes the target network design.",
    notesHelp:
      "Anything the YAML doesn't say — VLAN constraints, firewall boundaries, which segments must stay isolated.",
  },
  storage: {
    label: "Storage",
    endpoint: "/api/storage-reviews",
    detailPath: (id) => `/design-reviews/storage/${id}`,
    yamlLabel: "Proposed storage design (YAML)",
    yamlHelp:
      "StorageClasses, StorageProfiles, or the manifests describing your target storage layout.",
    notesHelp:
      "Performance requirements, RWX needs, capacity ceilings, or datastore quirks the manifests don't capture.",
  },
};

// Guard against someone attaching a multi-hundred-MB file to a textarea.
const MAX_UPLOAD_BYTES = 2 * 1024 * 1024;

function FileField({ id, label, help, value, onChange, rows = 10, accept }) {
  const inputRef = useRef(null);
  const [filename, setFilename] = useState(null);

  const pick = async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    if (file.size > MAX_UPLOAD_BYTES) {
      toast.error(`${file.name} is larger than 2 MB — paste the relevant section instead.`);
      event.target.value = "";
      return;
    }
    try {
      onChange(await file.text());
      setFilename(file.name);
    } catch {
      toast.error(`Could not read ${file.name}`);
    } finally {
      // Clear so re-picking the same file fires change again.
      event.target.value = "";
    }
  };

  return (
    <FormGroup label={label} fieldId={id}>
      <TextArea
        id={id}
        value={value}
        onChange={(_e, v) => {
          onChange(v);
          setFilename(null);
        }}
        rows={rows}
        resizeOrientation="vertical"
      />
      <FormHelperText>
        <HelperText>
          <HelperTextItem>{help}</HelperTextItem>
        </HelperText>
      </FormHelperText>
      <div className="pf-v6-u-mt-sm">
        <Button variant="secondary" onClick={() => inputRef.current?.click()}>
          Upload a file
        </Button>
        {filename && (
          <Content component="small" className="pf-v6-u-ml-sm pf-v6-u-color-200">
            Loaded {filename}
          </Content>
        )}
        <input
          ref={inputRef}
          type="file"
          accept={accept}
          onChange={pick}
          style={{ display: "none" }}
        />
      </div>
    </FormGroup>
  );
}

export default function DesignReviewNewPage({ kind }) {
  const meta = REVIEW_KINDS[kind];
  const navigate = useNavigate();

  const [name, setName] = useState("");
  const [notes, setNotes] = useState("");
  const [yaml, setYaml] = useState("");
  const [analyzeNow, setAnalyzeNow] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  const submit = async (event) => {
    event?.preventDefault();
    if (!name.trim() || submitting) return;

    setSubmitting(true);
    setError(null);
    try {
      const created = await fetchJSON(meta.endpoint, {
        method: "POST",
        body: { name: name.trim(), customer_notes: notes, proposed_yaml: yaml },
      });
      toast.success(`Created review: ${created?.name ?? name}`);

      if (analyzeNow) {
        // A failed analyze shouldn't lose the review the operator just
        // created — report it and still navigate to the detail page,
        // where they can retry.
        try {
          await fetchJSON(`${meta.endpoint}/${created.id}/analyze`, { method: "POST" });
          toast("Analysis started — usually 30–90 seconds", { icon: "🧠" });
        } catch (analyzeError) {
          toast.error(`Review saved, but analysis failed to start: ${analyzeError.message}`);
        }
      }

      navigate(meta.detailPath(created.id));
    } catch (e) {
      setError(e?.message ?? "Failed to create review");
      setSubmitting(false);
    }
  };

  return (
    <PageFrame
      title={`New ${meta.label.toLowerCase()} design review`}
      description={`Analyze a proposed OpenShift ${meta.label.toLowerCase()} design against the source estate and surface the gaps before you migrate.`}
      breadcrumbs={[
        { label: "Discover" },
        { label: "Design reviews", to: "/design-reviews" },
        { label: `New ${meta.label.toLowerCase()} review` },
      ]}
    >
      <PageSection hasBodyWrapper={false}>
        {error && (
          <HelperText className="pf-v6-u-mb-md">
            <HelperTextItem variant="error">{error}</HelperTextItem>
          </HelperText>
        )}

        <Form onSubmit={submit}>
          <FormGroup label="Review name" isRequired fieldId="dr-name">
            <TextInput
              id="dr-name"
              value={name}
              onChange={(_e, v) => setName(v)}
              isRequired
              placeholder={`${meta.label} design — production estate`}
            />
          </FormGroup>

          <FileField
            id="dr-notes"
            label="Customer notes"
            help={meta.notesHelp}
            value={notes}
            onChange={setNotes}
            rows={6}
            accept=".txt,.md,text/plain,text/markdown"
          />

          <FileField
            id="dr-yaml"
            label={meta.yamlLabel}
            help={meta.yamlHelp}
            value={yaml}
            onChange={setYaml}
            rows={14}
            accept=".yaml,.yml,text/yaml,application/x-yaml"
          />

          <FormGroup fieldId="dr-analyze">
            <Checkbox
              id="dr-analyze"
              label="Run the analysis now"
              description="Uncheck to save the review and analyze later."
              isChecked={analyzeNow}
              onChange={(_e, checked) => setAnalyzeNow(checked)}
            />
          </FormGroup>

          <ActionGroup>
            <Button
              variant="primary"
              type="submit"
              isDisabled={!name.trim() || submitting}
              isLoading={submitting}
            >
              {analyzeNow ? "Create and analyze" : "Create review"}
            </Button>
            <Button
              variant="link"
              onClick={() => navigate("/design-reviews")}
              isDisabled={submitting}
            >
              Cancel
            </Button>
          </ActionGroup>
        </Form>
      </PageSection>
    </PageFrame>
  );
}
