from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    database_url: str = "sqlite:///./household_context.db"

    # Security — mandatory, no defaults; service refuses to start without them
    data_encryption_key: str = Field(..., description="Fernet key (URL-safe base64) for PII encryption")
    hmac_secret_key: str = Field(..., description="Hex secret for HMAC tax_id hashing")

    # API principals
    agent_api_key: str = Field(..., description="Bearer token for the agent principal")
    ops_api_key: str = Field(..., description="Bearer token for the ops principal")

    # CRM connector
    crm_base_url: str = "http://localhost:8081"
    crm_token: str = "pravar-dev-token"
    crm_page_limit: int = 25
    crm_max_retries: int = 5
    crm_retry_base_delay: float = 0.5

    # Custodian connector
    custodian_drops_dir: str = "custodian_drops"

    # Paths
    data_policy_path: str = "contracts/data_policy.yaml"

    # Logging
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
