from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://virtvalidate:changeme@postgres:5432/virtvalidate"
    ssh_key_path: str = "/app/keys/id_ed25519"
    cluster_name: str = "ocp-virt-prod-01"

    # ----- Wave-scoped SSH collection (deterministic engine) -----
    # Bounded concurrency for the multi-VM orchestrator. Each in-flight VM
    # holds one paramiko connection + ~1-2 MB of buffers; 25 keeps a 1000-VM
    # baseline run sane on the appliance pod (peak ~50 MB working set for
    # SSH I/O) while delivering minutes-scale wall-clock instead of hours.
    # Tunable per-deployment if larger VM fleets or smaller appliance pods
    # change the trade-off.
    ssh_max_concurrency: int = 25
    # Per-command timeout for a single SSH command inside the collector.
    # Hung commands (the classic "ssh into a dying VM and the shell stalls")
    # are the most common reason a per-VM collection wedges; this caps how
    # much wall-clock one bad VM can burn.
    ssh_command_timeout_seconds: int = 30
    # Per-connection timeout for the initial TCP + SSH banner exchange.
    # Lower than the command timeout because a connection that can't be
    # made in 10s isn't coming.
    ssh_connect_timeout_seconds: int = 10

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
    # Pluggable inference backend. Supported values:
    #   "ollama" — local Ollama (default standalone appliance)
    #   "kserve" — RHOAI / OpenShift in-cluster inference
    #   "vllm"   — direct vLLM (placeholder, v1.0.0 target)
    #   "maas"   — authenticated Model-as-a-Service (OpenAI-compatible)
    #   "mock"   — dev/test only
    #
    # NOTE: as of the runtime-backend-switching change, this env var
    # is the BOOTSTRAP value only — it seeds ``app_settings.active_llm_backend``
    # on the first migration run. Thereafter the DB is authoritative;
    # operators flip the active backend from the Settings UI without
    # a redeploy. See ``app.core.llm.runtime.get_active_backend``.
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

    # ----- LLM input ceiling (architectural rule) -----
    # Maximum items (VMs, groups, log lines, diff chunks) any single
    # LLM call may receive in a collection. Note: this is PER CALL,
    # not per plan/feature — features that need to reason over more
    # items must decompose into multiple LLM calls, each at ≤ this
    # ceiling. The planner does this by running wave assignment
    # MECHANICALLY (no LLM) and only calling the LLM once per wave
    # for rationale text on the wave's ≤10 groups.
    #
    # Empirically pinned at 10 in May 2026 testing. Llama 3.2 3B
    # drops VM ids past 20; Granite 3.1 8B fails at 57. The 10-item
    # ceiling provides safety margin and keeps per-call latency
    # constant regardless of overall plan size.
    #
    # See CLAUDE.md "LLM Input Discipline" for the architectural
    # rule + `app.core.wave_skeleton.MechanicalWaveAssigner` for
    # the reference decomposition.
    llm_max_items_per_call: int = 10

    # ----- Plan selection cap -----
    # Maximum VMs an operator may select for a single plan-creation
    # request. With the multi-cluster target architecture, the
    # planner's partition key includes target_namespace and
    # target_cluster_id — a selection spanning N namespaces / clusters
    # naturally fans out into N independent plans, each
    # sub-partitioned to ≤10-VM waves. So 1000 is safe: the
    # deterministic stages are O(n log n) and complete in well under
    # 1s for 1000 VMs; the LLM annotation stage is bounded by wave
    # count, not VM count, and parallelizes per wave.
    # The cap exists to keep a single operator action ergonomic, not
    # because of structural limits.
    max_vms_per_plan: int = 1000

    # ----- Level 1 categorizer -----
    # VMs per LLM call. With Llama 3 8B and num_ctx=8192 a 10-VM batch
    # fits comfortably with system prompt + JSON output overhead. Bump
    # higher on KServe / larger context models, or lower if seeing
    # hallucinated tail entries.
    categorizer_batch_size: int = 10

    # ----- LLM pricing (cost estimation) -----
    # USD per 1M tokens. Defaults match Llama 3 8B on local Ollama —
    # zero marginal cost because the model is locally hosted. Federal
    # customers running KServe/vLLM on shared infrastructure can set
    # non-zero values to surface estimated compute cost in the admin
    # dashboard. The cost is computed at query time so updating these
    # doesn't require a restart.
    llm_cost_per_million_input_tokens: float = 0.0
    llm_cost_per_million_output_tokens: float = 0.0

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

    # ----- MaaS backend (Model-as-a-Service, OpenAI-compatible, bearer auth) -----
    # External authenticated inference endpoint — LiteLLM proxy,
    # OpenRouter, hosted vLLM behind a reverse-proxy, etc. The base URL
    # follows OpenAI client convention and INCLUDES the ``/v1`` prefix
    # (e.g. ``https://litellm-prod.apps.maas.redhatworkshops.io/v1``);
    # the backend appends ``/chat/completions`` and ``/models`` to it.
    #
    # The API key is mounted from a Kubernetes Secret as the
    # ``LLM_MAAS_API_KEY`` env var — never set this in values.yaml,
    # never log it, and never embed it in error messages or API
    # responses. The MaaS backend's __repr__ redacts the key; tests
    # in test_llm_backends.py pin the no-leak invariant.
    llm_maas_base_url: str | None = None
    llm_maas_model: str | None = None
    llm_maas_api_key: str | None = None
    # Default 60s — remote inference is slower-floor than local Ollama
    # but the network leg is the dominant cost, not the model.
    llm_maas_timeout_seconds: int = 60
    llm_maas_verify_ssl: bool = True

    # MTV / Forklift defaults used when generating migration plan YAML.
    # The source/destination Provider resources are expected to already exist
    # in the target cluster — we only reference them.
    mtv_namespace: str = "openshift-mtv"
    mtv_source_provider: str = "vmware"
    mtv_destination_provider: str = "host"
    # Empty by default: operators must explicitly pick a workload
    # namespace via the mapping's NamespaceStrategy or per-VM
    # target_namespace. The MTV control-plane namespace
    # (``openshift-mtv``) is NOT a valid default for workload VMs —
    # it would mix admin and workload resources. The mapping
    # validator surfaces the gap at plan creation; the YAML
    # generator surfaces it again at download time as a backstop.
    mtv_default_target_namespace: str = ""

    # Path to the VM inventory CSV template served by /api/templates/csv.
    # Defaults to the in-container location populated by the Containerfile.
    # Dev workflows running uvicorn from backend/ should override this in
    # .env to point at the source-of-truth at docs/vm-inventory-template.csv.
    csv_template_path: str = "/app/templates/vm-inventory-template.csv"


settings = Settings()
