from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    All secrets must be provided via environment variables, never hardcoded.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    app_name: str = "Invoice Intelligence API"
    environment: str = Field(default="development")
    debug: bool = Field(default=True)

    # API
    api_v1_prefix: str = "/api/v1"

    # CORS - comma-separated list of allowed origins for local dev
    cors_origins: str = Field(default="http://localhost:5173,http://127.0.0.1:5173")

    # Database
    database_url: str = Field(
        default="postgresql+psycopg://invoice:invoice@localhost:5432/invoice_intelligence"
    )

    # Storage - root directory for uploaded files. Relative paths are resolved
    # against the process working directory.
    storage_root: str = Field(default="./var/storage")

    # Uploads
    max_upload_bytes: int = Field(default=20 * 1024 * 1024)  # 20 MB
    # Comma-separated list of accepted upload content types.
    allowed_content_types: str = Field(
        default="application/pdf,image/png,image/jpeg"
    )

    # Comma-separated list of accepted upload file extensions.
    allowed_extensions: str = Field(default=".pdf,.png,.jpg,.jpeg")

    # LLM provider selection: "mock" (default), or future: "local", "commercial"
    llm_provider: str = Field(default="mock")

    # Embedding provider: "hash" (deterministic, offline, zero-cost), or future:
    # a real model. See app/services/embeddings/factory.py.
    embedding_provider: str = Field(default="hash")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def allowed_content_types_list(self) -> list[str]:
        return [c.strip() for c in self.allowed_content_types.split(",") if c.strip()]

    @property
    def allowed_extensions_list(self) -> list[str]:
        return [e.strip().lower() for e in self.allowed_extensions.split(",") if e.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()