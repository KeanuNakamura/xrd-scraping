"""Runtime configuration for figure-analysis OpenAI vision calls."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Task-specific defaults: expensive enrichment vs cheap classify/triage.
DEFAULT_ANALYSIS_MODEL = "gpt-4.1"
DEFAULT_TRIAGE_MODEL = "gpt-4.1-mini"


def resolve_analysis_model(override: str | None = None) -> str:
    """Model for full scientific figure enrichment (structured vision)."""
    return (
        (override or "").strip()
        or os.environ.get("OPENAI_ANALYSIS_MODEL", "").strip()
        or os.environ.get("OPENAI_MODEL", "").strip()
        or DEFAULT_ANALYSIS_MODEL
    )


def resolve_triage_model(override: str | None = None) -> str:
    """Model for XRD classification and layout/ClipDrop triage."""
    return (
        (override or "").strip()
        or os.environ.get("OPENAI_TRIAGE_MODEL", "").strip()
        or DEFAULT_TRIAGE_MODEL
    )


def env_flag(name: str, default: bool = False) -> bool:
    """Parse a truthy/falsey environment flag."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    """Vision-client configuration loaded from CLI / environment."""

    openai_api_key: str = ""
    model: str = DEFAULT_ANALYSIS_MODEL
    output_dir: Path = Path("outputs/run")
    cache_dir: Path = Path(".cache")
    use_cache: bool = True
    max_retries: int = 2
    debug: bool = False
    verbose: bool = True
    request_timeout_s: float = 120.0
    max_api_retries: int = 3
    temperature: float = 0.0
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        model = overrides.pop("model", None)
        model = resolve_analysis_model(model)
        api_key = overrides.pop("openai_api_key", None) or os.environ.get(
            "OPENAI_API_KEY", ""
        )
        if "use_cache" not in overrides:
            overrides["use_cache"] = env_flag("OPENAI_USE_CACHE", True)
        cache_dir = overrides.pop("cache_dir", None)
        if cache_dir is None:
            cache_dir = Path(os.environ.get("OPENAI_CACHE_DIR", ".cache"))
        return cls(
            openai_api_key=api_key,
            model=model,
            cache_dir=Path(cache_dir),
            **overrides,
        )

    def require_api_key(self) -> None:
        if not self.openai_api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Export it or place it in the environment."
            )
