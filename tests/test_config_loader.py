"""
test_config_loader.py — Verify config loading and validation.

Run with:  pytest tests/ -v
"""

import pytest
from pydantic import ValidationError

from src.config_loader import load_config, Config


class TestConfigLoader:

    def test_valid_config_loads(self):
        """Smoke test: default config.yaml must load without error."""
        cfg = load_config("config/config.yaml")
        assert isinstance(cfg, Config)

    def test_github_min_stars_is_positive(self):
        cfg = load_config("config/config.yaml")
        assert cfg.github.min_stars >= 0

    def test_target_extensions_have_dot(self):
        cfg = load_config("config/config.yaml")
        for ext in cfg.extractor.target_extensions:
            assert ext.startswith("."), f"Extension missing dot: {ext}"

    def test_output_format_is_valid(self):
        cfg = load_config("config/config.yaml")
        assert cfg.storage.output_format in {"jsonl", "parquet"}

    def test_missing_config_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_config("config/does_not_exist.yaml")

    def test_token_not_in_config_object(self):
        """
        The raw token string must never live in the config object.
        Only the env var NAME is stored.
        """
        cfg = load_config("config/config.yaml")
        assert "ghp_" not in cfg.github.token_env