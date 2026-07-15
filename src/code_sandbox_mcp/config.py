from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

PINNED_SANDBOX_IMAGE = "code-sandbox-mcp-javascript:1.0.0"
ALLOWED_SANDBOX_IMAGES = frozenset({PINNED_SANDBOX_IMAGE})


def _default_audit_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "code-sandbox-mcp" / "audit.jsonl"


class SandboxConfig(BaseModel):
    """Trusted server-side limits. None of these values are MCP tool inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    image: str = PINNED_SANDBOX_IMAGE
    max_session_lifetime_seconds: int = Field(default=600, ge=30, le=3600)
    idle_timeout_seconds: int = Field(default=180, ge=10, le=1800)
    max_concurrent_sessions: int = Field(default=3, ge=1, le=20)
    max_files: int = Field(default=100, ge=1, le=1000)
    max_file_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=16 * 1024 * 1024)
    max_workspace_bytes: int = Field(default=64 * 1024 * 1024, ge=1024 * 1024, le=256 * 1024 * 1024)
    max_stdout_bytes: int = Field(default=1024 * 1024, ge=1024, le=8 * 1024 * 1024)
    max_stderr_bytes: int = Field(default=1024 * 1024, ge=1024, le=8 * 1024 * 1024)
    default_command_timeout_seconds: int = Field(default=30, ge=1, le=120)
    max_command_timeout_seconds: int = Field(default=120, ge=1, le=300)
    cleanup_interval_seconds: int = Field(default=5, ge=1, le=60)
    audit_enabled: bool = True
    audit_path: Path = Field(default_factory=_default_audit_path)

    @field_validator("image")
    @classmethod
    def image_is_allowlisted(cls, value: str) -> str:
        if value not in ALLOWED_SANDBOX_IMAGES:
            raise ValueError("sandbox image is not in the server-side allowlist")
        return value

    @classmethod
    def from_env(cls) -> SandboxConfig:
        values: dict[str, object] = {}
        integer_fields = {
            "CODE_SANDBOX_MAX_LIFETIME": "max_session_lifetime_seconds",
            "CODE_SANDBOX_IDLE_TIMEOUT": "idle_timeout_seconds",
            "CODE_SANDBOX_MAX_SESSIONS": "max_concurrent_sessions",
            "CODE_SANDBOX_DEFAULT_TIMEOUT": "default_command_timeout_seconds",
            "CODE_SANDBOX_MAX_TIMEOUT": "max_command_timeout_seconds",
        }
        for env_name, field_name in integer_fields.items():
            raw = os.environ.get(env_name)
            if raw is not None:
                if not raw.isascii() or not raw.isdecimal():
                    raise ValueError(f"{env_name} must be an ASCII integer")
                values[field_name] = int(raw)
        if "CODE_SANDBOX_AUDIT_LOG" in os.environ:
            values["audit_path"] = Path(os.environ["CODE_SANDBOX_AUDIT_LOG"])
        if "CODE_SANDBOX_AUDIT_ENABLED" in os.environ:
            raw_bool = os.environ["CODE_SANDBOX_AUDIT_ENABLED"].lower()
            if raw_bool not in {"true", "false"}:
                raise ValueError("CODE_SANDBOX_AUDIT_ENABLED must be true or false")
            values["audit_enabled"] = raw_bool == "true"
        return cls.model_validate(values)
