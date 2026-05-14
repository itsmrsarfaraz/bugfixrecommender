"""
config_loader.py — Load and validate config.yaml using Pydantic v2.

WHY validate config at startup:
- Bad config (wrong type, missing key, wrong path) fails LOUDLY
  at boot, not silently mid-pipeline after 2 hours of cloning.
- Pydantic gives you free type coercion + clear error messages.
- One validated config object flows through the entire pipeline.

USAGE:
    from src.config_loader import load_config
    cfg = load_config()
    print(cfg.github.min_stars)
"""

import os
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import BaseModel, Field, field_validator


# ── Sub-models: one per top-level YAML section ──────────────

class GitHubConfig(BaseModel):
    token_env: str = "GITHUB_TOKEN"
    min_stars: int = Field(ge=0)          # ge = greater-than-or-equal
    min_activity_days: int = Field(ge=1)
    max_repos: int = Field(ge=1)
    language: str
    per_page: int = Field(ge=1, le=100)   # GitHub max is 100
    request_delay_seconds: float = Field(ge=0.0)

    @property
    def token(self) -> Optional[str]:
        """
        Read token from environment at runtime.
        Never store the actual token in the config object
        — it would appear in logs/debug output.
        """
        return os.environ.get(self.token_env)


class DownloaderConfig(BaseModel):
    clone_dir: str
    min_free_disk_gb: float = Field(ge=0.5)
    batch_size: int = Field(ge=1)
    clone_timeout_seconds: int = Field(ge=10)
    cleanup_after_extraction: bool


class ExtractorConfig(BaseModel):
    bug_fix_keywords: List[str]
    target_extensions: List[str]
    max_diff_lines: int = Field(ge=1)
    max_files_changed: int = Field(ge=1)
    min_meaningful_tokens: int = Field(ge=1)
    noise_keywords: List[str]

    @field_validator("target_extensions")
    @classmethod
    def extensions_must_start_with_dot(cls, exts: List[str]) -> List[str]:
        """Ensure every extension is formatted correctly e.g. '.java'"""
        for ext in exts:
            if not ext.startswith("."):
                raise ValueError(
                    f"Extension '{ext}' must start with a dot. Use '.{ext}'"
                )
        return [e.lower() for e in exts]


class StorageConfig(BaseModel):
    extracted_dir: str
    processed_dir: str
    output_format: str
    chunk_size: int = Field(ge=1)

    @field_validator("output_format")
    @classmethod
    def valid_format(cls, fmt: str) -> str:
        allowed = {"jsonl", "parquet"}
        if fmt not in allowed:
            raise ValueError(f"output_format must be one of {allowed}, got '{fmt}'")
        return fmt


class CheckpointConfig(BaseModel):
    checkpoint_dir: str
    discovered_repos_file: str
    cloned_repos_file: str
    processed_repos_file: str


class LoggingConfig(BaseModel):
    level: str
    log_dir: str
    log_file: str
    rotation: str
    retention: str

    @field_validator("level")
    @classmethod
    def valid_level(cls, lvl: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR"}
        if lvl.upper() not in allowed:
            raise ValueError(f"Log level must be one of {allowed}")
        return lvl.upper()


# ── Root config model ────────────────────────────────────────

class Config(BaseModel):
    """
    Root config model.
    Every pipeline component receives this object — no component
    reads the YAML file directly.
    """
    github: GitHubConfig
    downloader: DownloaderConfig
    extractor: ExtractorConfig
    storage: StorageConfig
    checkpoints: CheckpointConfig
    logging: LoggingConfig


# ── Loader function ──────────────────────────────────────────

def load_config(config_path: str = "config/config.yaml") -> Config:
    """
    Read config.yaml from disk, parse with PyYAML,
    validate with Pydantic, return a Config object.

    Fails fast with a clear error if:
    - the file doesn't exist
    - a required key is missing
    - a value is the wrong type
    - a validator rule is violated

    Args:
        config_path: Path to config.yaml (relative to project root).

    Returns:
        Validated Config object.

    Raises:
        FileNotFoundError: config.yaml not found.
        pydantic.ValidationError: Config values are invalid.
    """
    path = Path(config_path)

    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path.resolve()}\n"
            f"Make sure you are running from the project root directory."
        )

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    # Pydantic validates all fields. Any error here = fix config.yaml.
    config = Config(**raw)
    return config