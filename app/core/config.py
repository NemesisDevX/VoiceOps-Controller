"""Application configuration loaded from environment variables and `.env`."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralized runtime configuration for the VoiceOps Controller backend."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    ASSEMBLYAI_API_KEY: SecretStr = Field(
        ...,
        description="API key used to authenticate with the AssemblyAI Realtime Streaming API.",
    )
    HOST: str = Field(default="0.0.0.0", description="Interface the ASGI server binds to.")
    PORT: int = Field(default=8000, ge=1, le=65535, description="Port the ASGI server binds to.")
    LOG_LEVEL: str = Field(default="INFO", description="Root logging level for structured JSON logs.")

    TELEMETRY_INTERVAL_MS: int = Field(
        default=1000,
        ge=100,
        description="Interval in milliseconds between telemetry broadcasts.",
    )
    CONFIRMATION_TOKEN_TTL_SECONDS: int = Field(
        default=60,
        ge=5,
        description="Time-to-live for a mutating command confirmation token.",
    )
    TOP_PROCESS_LIMIT: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Number of top processes returned by inspection queries.",
    )
    GROQ_API_KEY: SecretStr | None = Field(
        default=None,
        description="Optional Groq API key used to generate agentic SRE post-mortem reports.",
    )
    GROQ_MODEL: str = Field(
        default="llama3-8b-8192",
        description="Groq model name for agentic post-mortem generation.",
    )
    DISCORD_WEBHOOK_URL: str | None = Field(
        default=None,
        description="Optional Discord webhook URL for real-time incident resolution alerts.",
    )
    DISCORD_ALERTS_ENABLED: bool = Field(
        default=True,
        description="Whether to dispatch Discord webhook alerts on successful mitigations.",
    )


@lru_cache
def get_settings() -> Settings:
    """Return a cached `Settings` singleton for the process lifetime."""
    return Settings()  # type: ignore[call-arg]
