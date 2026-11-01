"""Runtime settings, read from environment variables only."""

from __future__ import annotations

import os
from dataclasses import dataclass

ENV_PREFIX = "FILEMATCH"
DEFAULT_PORT = 8765
# Current Haiku-tier model ID, checked against the Claude API model list (Nov 2026).
DEFAULT_AI_MODEL = "claude-haiku-5-5"


def _env(name: str, default: str) -> str:
    return os.environ.get(f"{ENV_PREFIX}_{name}", default)


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    max_upload_mb: int = 200
    session_ttl_seconds: int = 60 * 60
    sweep_interval_seconds: int = 60
    max_columns: int = 500
    max_xlsx_uncompressed: int = 1024**3
    max_xlsx_ratio: float = 100.0
    ai_model: str = DEFAULT_AI_MODEL

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            host=_env("HOST", "127.0.0.1"),
            max_upload_mb=int(_env("MAX_UPLOAD_MB", "200")),
            ai_model=_env("AI_MODEL", DEFAULT_AI_MODEL),
        )
