from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://virtvalidate:changeme@postgres:5432/virtvalidate"
    ssh_key_path: str = "/app/keys/id_ed25519"
    cluster_name: str = "ocp-virt-prod-01"

    # ----- FIPS 140-3 compliance -----
    # When fips_mode is on, the appliance refuses to load SSH keys that
    # aren't on the FIPS-approved list (RSA ≥3072, ECDSA P-384, ECDSA
    # P-256), warns at startup if the host OS isn't actually running in
    # FIPS mode, and reports its compliance posture via /api/system/fips-status.
    # This is a deployment decision — flip it via the FIPS_MODE env var,
    # not the Settings UI.
    fips_mode: bool = False
    # Operators generating SSH keys themselves can set this to document
    # the algorithm in use. The runtime accepts any key paramiko can load
    # (the FIPS gate enforces approval); this field exists so the
    # Settings UI and audit trail can show the operator's intent.
    # Allowed: "ed25519" (default, NON-FIPS), "rsa-3072", "ecdsa-p384".
    ssh_key_algorithm: str = "ed25519"

    # ----- LLM backend selection -----
    # Pluggable inference backend chosen at deployment time. Supported
    # values: "ollama" (default standalone appliance), "kserve" (RHOAI
    # / OpenShift inference), "vllm" (placeholder, v1.0.0 target).
    # This is a deployment decision, not a runtime one — it is *not*
    # editable from the Settings UI.
    llm_backend_type: str = "ollama"

    # ----- Ollama backend -----
    # Default for standalone deployments. The validation engine, planner,
    # and reporter post chat completions here. VirtValidate is air-gapped
    # by design — point this at the local Ollama instance only.
    ollama_host: str = "http://ollama:11434"
    ollama_model: str = "llama3:8b"
    # Ollama defaults to 4096-token context, which truncates our larger
    # prompts (categorizer batches, plan generation). 8192 doubles
    # headroom while staying inside Llama 3 8B's hard 8192 ceiling. Bump
    # higher only when running a model that supports it (Llama 3.1+).
    ollama_num_ctx: int = 8192

    # ----- LLM transport timeouts -----
    # CPU-only inference for Llama 3 8B can spend 60-120s per call on
    # large prompts. The default 120s read timeout was racing the model
    # and producing 500s; 600s gives Ollama enough room without making
    # operators wait forever on a wedged worker.
    llm_read_timeout: float = 600.0
    llm_connect_timeout: float = 30.0
    # Number of retry attempts on transport-level failures (timeouts,
    # connection resets). Backoff schedule lives in the backend; the
    # value here is the *additional* attempts after the first call.
    llm_max_retries: int = 2

    # ----- Level 1 categorizer -----
    # VMs per LLM call. With Llama 3 8B and num_ctx=8192 a 10-VM batch
    # fits comfortably with system prompt + JSON output overhead. Bump
    # higher on KServe / larger context models, or lower if seeing
    # hallucinated tail entries.
    categorizer_batch_size: int = 10

    # ----- KServe backend (RHOAI / OpenShift inference) -----
    # Endpoint must point at an InferenceService that exposes the
    # OpenAI-compatible /v1/chat/completions surface (vLLM and TGIS
    # predictors do this out of the box).
    kserve_endpoint: str | None = None
    kserve_model_name: str | None = None
    # Token resolution order: explicit env var → token_file path → none.
    # The default file is the in-pod service account token mounted by
    # OpenShift; bare-metal deployments must set kserve_token explicitly.
    kserve_token: str | None = None
    kserve_token_file: str = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    kserve_verify_ssl: bool = True
    kserve_timeout_seconds: int = 120

    # ----- vLLM backend (placeholder for v1.0.0) -----
    # Direct vLLM connection without the KServe wrapper. The implementation
    # raises NotImplementedError today; settings live here so deployments
    # don't need a config migration when v1.0.0 lands.
    vllm_endpoint: str | None = None
    vllm_model_name: str | None = None

    # MTV / Forklift defaults used when generating migration plan YAML.
    # The source/destination Provider resources are expected to already exist
    # in the target cluster — we only reference them.
    mtv_namespace: str = "openshift-mtv"
    mtv_source_provider: str = "vmware"
    mtv_destination_provider: str = "host"
    mtv_default_target_namespace: str = "openshift-mtv"

    # Path to the VM inventory CSV template served by /api/templates/csv.
    # Defaults to the in-container location populated by the Containerfile.
    # Dev workflows running uvicorn from backend/ should override this in
    # .env to point at the source-of-truth at docs/vm-inventory-template.csv.
    csv_template_path: str = "/app/templates/vm-inventory-template.csv"


settings = Settings()
