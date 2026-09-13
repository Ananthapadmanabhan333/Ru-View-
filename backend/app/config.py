"""Configuration settings for RuView Fall Detection Backend."""
from typing import List, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "RuView Fall Detection Backend"
    VERSION: str = "1.0.0"
    API_PREFIX: str = "/api"
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DEBUG: bool = False

    # Security & CORS
    CORS_ORIGINS: List[str] = ["*"]
    API_KEY: Optional[str] = None  # Optional API key for REST/WS authentication

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./fall_detection.db"

    # UDP Ingestion (Direct from ESP32-S3)
    UDP_ENABLED: bool = True
    UDP_BIND_HOST: str = "0.0.0.0"
    UDP_PORT: int = 5005

    # Upstream RuView Sensing Server (Bridge Mode)
    RUVIEW_WS_URL: Optional[str] = None  # e.g., "ws://localhost:8765/ws/sensing"
    RUVIEW_HTTP_URL: Optional[str] = None  # e.g., "http://localhost:8080"
    RUVIEW_API_TOKEN: Optional[str] = None

    # Fall detection & Node tracking parameters (aligned with RuView defaults)
    NODE_OFFLINE_TIMEOUT_S: float = 5.0
    FALL_COOLDOWN_MS: int = 5000
    FALL_CONSEC_MIN: int = 3
    RAW_CSI_RECORDING: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


settings = Settings()
