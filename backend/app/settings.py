"""Application settings and server-side budget limits.

All limits are validated against hard server-side caps: a deployment may lower a
budget, but never raise it above the cap defined here (ImplementationPlan.md §9.4).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"


class ConfigurationError(RuntimeError):
    """Raised when the runtime configuration cannot satisfy a request."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --- server-side caps (never raised by configuration) -----------------------
CAP_MAX_REPAIR_ROUNDS = 10
CAP_MAX_VERIFICATION_RETRIES = 10
CAP_MAX_REVIEW_RETRIES = 10
CAP_MAX_FIX_RETRIES = 10
CAP_MAX_EVIDENCE_RETRIES = 10
CAP_MAX_GENERATE_RETRIES = 10
CAP_MAX_PARENT_CORRECTIONS = 5
CAP_MAX_MODEL_RETRIES = 5
CAP_MAX_TOOL_STEPS = 64
CAP_MODEL_TIMEOUT_SECONDS = 180
CAP_TOOL_TIMEOUT_SECONDS = 120
CAP_ATTEMPT_TIMEOUT_SECONDS = 600
CAP_MAX_GRAPH_STEPS = 2048


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        env_prefix="HW2_",
    )

    # --- model connection ---------------------------------------------------
    model_provider: Literal["deepseek"] = "deepseek"
    model_base_url: str = "https://api.deepseek.com"
    model_name: str = "deepseek-chat"
    model_api_key: str | None = None
    model_temperature: float = 0.0

    # --- storage / server ---------------------------------------------------
    data_dir: Path = DEFAULT_DATA_DIR
    config_dir: Path = DEFAULT_CONFIG_DIR
    frontend_origin: str = "http://localhost:5173"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"

    # --- upload limits ------------------------------------------------------
    max_upload_files: int = 64
    max_upload_file_bytes: int = 512 * 1024
    max_upload_total_bytes: int = 8 * 1024 * 1024
    allowed_upload_suffixes: tuple[str, ...] = (".py", ".txt", ".md", ".cfg", ".toml", ".ini")

    # --- budgets: business counting ----------------------------------------
    max_repair_rounds: int = Field(default=2, ge=0)
    max_verification_retries: int = Field(default=2, ge=0)
    max_review_retries: int = Field(default=2, ge=0)
    max_fix_retries: int = Field(default=2, ge=0)
    max_evidence_retries: int = Field(default=2, ge=0)
    max_generate_retries: int = Field(default=2, ge=0)
    max_parent_corrections: int = Field(default=2, ge=0)
    max_model_retries: int = Field(default=2, ge=0)

    # --- budgets: execution limits -----------------------------------------
    max_tool_steps: int = Field(default=12, ge=1)
    model_timeout_seconds: float = Field(default=60.0, gt=0)
    tool_timeout_seconds: float = Field(default=30.0, gt=0)
    attempt_timeout_seconds: float = Field(default=180.0, gt=0)
    max_graph_steps: int = Field(default=256, ge=1)

    # --- testing ------------------------------------------------------------
    # Only honored when an explicit scripted model is injected by tests.
    force_scripted_model: bool = False

    @field_validator("data_dir", "config_dir", mode="before")
    @classmethod
    def _expand_data_dir(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(value).expanduser()
        return value

    @model_validator(mode="after")
    def _enforce_caps(self) -> "Settings":
        checks = [
            ("max_repair_rounds", self.max_repair_rounds, CAP_MAX_REPAIR_ROUNDS),
            ("max_verification_retries", self.max_verification_retries, CAP_MAX_VERIFICATION_RETRIES),
            ("max_review_retries", self.max_review_retries, CAP_MAX_REVIEW_RETRIES),
            ("max_fix_retries", self.max_fix_retries, CAP_MAX_FIX_RETRIES),
            ("max_evidence_retries", self.max_evidence_retries, CAP_MAX_EVIDENCE_RETRIES),
            ("max_generate_retries", self.max_generate_retries, CAP_MAX_GENERATE_RETRIES),
            ("max_parent_corrections", self.max_parent_corrections, CAP_MAX_PARENT_CORRECTIONS),
            ("max_model_retries", self.max_model_retries, CAP_MAX_MODEL_RETRIES),
            ("max_tool_steps", self.max_tool_steps, CAP_MAX_TOOL_STEPS),
            ("model_timeout_seconds", self.model_timeout_seconds, CAP_MODEL_TIMEOUT_SECONDS),
            ("tool_timeout_seconds", self.tool_timeout_seconds, CAP_TOOL_TIMEOUT_SECONDS),
            ("attempt_timeout_seconds", self.attempt_timeout_seconds, CAP_ATTEMPT_TIMEOUT_SECONDS),
            ("max_graph_steps", self.max_graph_steps, CAP_MAX_GRAPH_STEPS),
        ]
        for name, value, cap in checks:
            if value > cap:
                raise ValueError(f"{name}={value} exceeds server cap {cap}")
        return self

    # --- derived paths ------------------------------------------------------
    @property
    def business_db_path(self) -> Path:
        return self.data_dir / "business.sqlite"

    @property
    def checkpoints_db_path(self) -> Path:
        return self.data_dir / "checkpoints.sqlite"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def patches_dir(self) -> Path:
        return self.data_dir / "patches"

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.uploads_dir,
            self.snapshots_dir,
            self.workspaces_dir,
            self.patches_dir,
            self.evidence_dir,
            self.reports_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    # --- model credential helpers ------------------------------------------
    @property
    def model_configured(self) -> bool:
        return bool(self.model_api_key and self.model_api_key.strip())

    def require_model_credentials(self) -> None:
        """Raise a clear configuration error instead of silently faking a model."""
        if not self.model_configured:
            raise ConfigurationError(
                "MODEL_NOT_CONFIGURED",
                "模型凭据未配置：请设置 HW2_MODEL_API_KEY 后再提交新的模型任务。"
                "已有任务与工作台仍可查询。",
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
