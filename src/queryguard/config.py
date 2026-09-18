"""Runtime configuration, loaded from the environment.

Everything that varies between my laptop, a CI run and someone else's clone
lives here. Nothing else in the codebase reads os.environ directly.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM ---
    google_api_key: SecretStr | None = Field(default=None, alias="GOOGLE_API_KEY")
    model: str = Field(default="gemini-2.5-flash", alias="QG_MODEL")
    temperature: float = Field(default=0.0, alias="QG_TEMPERATURE")

    # --- Databases ---
    sqlite_dir: Path = Field(default=Path("data/db"), alias="QG_SQLITE_DIR")
    pg_dsn: str | None = Field(default=None, alias="QG_PG_DSN")
    pg_admin_dsn: str | None = Field(default=None, alias="QG_PG_ADMIN_DSN")

    # --- Safety limits ---
    # These are hard caps enforced in execution/, not suggestions made to the model.
    max_rows: int = Field(default=200, alias="QG_MAX_ROWS")
    query_timeout_s: int = Field(default=10, alias="QG_QUERY_TIMEOUT_S")
    max_repair_attempts: int = Field(default=3, alias="QG_MAX_REPAIR_ATTEMPTS")

    # --- Reproducibility ---
    seed: int = Field(default=20260918, alias="QG_SEED")
    llm_cache: bool = Field(default=True, alias="QG_LLM_CACHE")

    @property
    def sqlite_path_root(self) -> Path:
        p = self.sqlite_dir
        return p if p.is_absolute() else REPO_ROOT / p

    def sqlite_path(self, db_id: str) -> Path:
        return self.sqlite_path_root / f"{db_id}.sqlite"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
