import os
from dataclasses import dataclass

@dataclass
class Settings:
    """Application configuration loaded from environment variables with sensible defaults."""
    legacy_base_url: str = os.getenv("LEGACY_BASE_URL", "https://urja-ops.flockenergy.tech").rstrip("/")
    operator_email: str = os.getenv("OPERATOR_EMAIL", "operator@urja.local")
    operator_password: str = os.getenv("OPERATOR_PASSWORD", "urja-ops-2026")
    request_timeout: float = float(os.getenv("REQUEST_TIMEOUT", "15.0"))
    cache_ttl_seconds: int = int(os.getenv("CACHE_TTL_SECONDS", "300"))
    api_host: str = os.getenv("API_HOST", "0.0.0.0")
    api_port: int = int(os.getenv("API_PORT", "8000"))
    debug: bool = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")

settings = Settings()
