from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://virtvalidate:changeme@postgres:5432/virtvalidate"
    ollama_host: str = "http://ollama:11434"
    ollama_model: str = "llama3:8b"
    ssh_key_path: str = "/app/keys/id_ed25519"
    cluster_name: str = "ocp-virt-prod-01"

    # MTV / Forklift defaults used when generating migration plan YAML.
    # The source/destination Provider resources are expected to already exist
    # in the target cluster — we only reference them.
    mtv_namespace: str = "openshift-mtv"
    mtv_source_provider: str = "vmware"
    mtv_destination_provider: str = "host"
    mtv_default_target_namespace: str = "openshift-mtv"


settings = Settings()
