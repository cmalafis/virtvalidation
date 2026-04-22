from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://virtvalidate:changeme@postgres:5432/virtvalidate"
    ollama_host: str = "http://ollama:11434"
    ollama_model: str = "llama3:8b"
    ssh_key_path: str = "/app/keys/id_ed25519"


settings = Settings()
