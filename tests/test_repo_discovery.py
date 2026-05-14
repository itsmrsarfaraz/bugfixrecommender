"""
test_repo_discovery.py — Unit tests for RepoDiscovery.

We mock the GitHub API entirely — tests must run offline
with no token and no network calls.

Run with: pytest tests/ -v
"""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime, timezone, timedelta

from src.config_loader import load_config
from src.discovery.repo_discovery import RepoDiscovery


# ── Fixtures ─────────────────────────────────────────────────

@pytest.fixture
def cfg():
    return load_config("config/config.yaml")


def make_mock_repo(
    full_name="apache/commons-lang",
    stars=500,
    pushed_days_ago=30,
    fork=False,
    archived=False,
    language="Java",
    clone_url="https://github.com/apache/commons-lang.git",
    default_branch="main",
    description="A test repo",
):
    """Helper: build a mock PyGithub Repository object."""
    repo = MagicMock()
    repo.full_name = full_name
    repo.stargazers_count = stars
    repo.pushed_at = datetime.now(timezone.utc) - timedelta(days=pushed_days_ago)
    repo.fork = fork
    repo.archived = archived
    repo.language = language
    repo.clone_url = clone_url
    repo.default_branch = default_branch
    repo.description = description
    return repo


# ── Tests ─────────────────────────────────────────────────────

class TestFilterReason:
    """Test _filter_reason logic directly — no GitHub API needed."""

    def test_good_repo_passes(self, cfg, tmp_path):
        cfg.checkpoints.checkpoint_dir = str(tmp_path)
        with patch("src.discovery.repo_discovery.Github"):
            discoverer = RepoDiscovery(cfg)
        repo = make_mock_repo()
        assert discoverer._filter_reason(repo) is None

    def test_fork_is_rejected(self, cfg, tmp_path):
        cfg.checkpoints.checkpoint_dir = str(tmp_path)
        with patch("src.discovery.repo_discovery.Github"):
            discoverer = RepoDiscovery(cfg)
        repo = make_mock_repo(fork=True)
        assert "fork" in discoverer._filter_reason(repo)

    def test_archived_is_rejected(self, cfg, tmp_path):
        cfg.checkpoints.checkpoint_dir = str(tmp_path)
        with patch("src.discovery.repo_discovery.Github"):
            discoverer = RepoDiscovery(cfg)
        repo = make_mock_repo(archived=True)
        assert "archived" in discoverer._filter_reason(repo)

    def test_inactive_repo_is_rejected(self, cfg, tmp_path):
        cfg.checkpoints.checkpoint_dir = str(tmp_path)
        with patch("src.discovery.repo_discovery.Github"):
            discoverer = RepoDiscovery(cfg)
        # Pushed 400 days ago — older than min_activity_days (365)
        repo = make_mock_repo(pushed_days_ago=400)
        reason = discoverer._filter_reason(repo)
        assert reason is not None
        assert "inactive" in reason

    def test_low_stars_rejected(self, cfg, tmp_path):
        cfg.checkpoints.checkpoint_dir = str(tmp_path)
        with patch("src.discovery.repo_discovery.Github"):
            discoverer = RepoDiscovery(cfg)
        repo = make_mock_repo(stars=5)
        reason = discoverer._filter_reason(repo)
        assert reason is not None


class TestCheckpointing:
    """Test checkpoint save/load round-trip."""

    def test_checkpoint_saves_and_loads(self, cfg, tmp_path):
        cfg.checkpoints.checkpoint_dir = str(tmp_path)
        with patch("src.discovery.repo_discovery.Github"):
            discoverer = RepoDiscovery(cfg)

        repo = make_mock_repo()
        discoverer.discovered["apache/commons-lang"] = discoverer._extract_metadata(repo)
        discoverer._save_checkpoint()

        # New instance should load the checkpoint
        with patch("src.discovery.repo_discovery.Github"):
            discoverer2 = RepoDiscovery(cfg)

        assert "apache/commons-lang" in discoverer2.discovered

    def test_corrupted_checkpoint_starts_fresh(self, cfg, tmp_path):
        cfg.checkpoints.checkpoint_dir = str(tmp_path)
        checkpoint_path = tmp_path / cfg.checkpoints.discovered_repos_file
        checkpoint_path.write_text("NOT VALID JSON {{{")

        with patch("src.discovery.repo_discovery.Github"):
            discoverer = RepoDiscovery(cfg)

        assert discoverer.discovered == {}

    def test_no_checkpoint_starts_fresh(self, cfg, tmp_path):
        cfg.checkpoints.checkpoint_dir = str(tmp_path / "nonexistent")
        with patch("src.discovery.repo_discovery.Github"):
            discoverer = RepoDiscovery(cfg)
        assert discoverer.discovered == {}


class TestMetadataExtraction:

    def test_extract_metadata_fields(self, cfg, tmp_path):
        cfg.checkpoints.checkpoint_dir = str(tmp_path)
        with patch("src.discovery.repo_discovery.Github"):
            discoverer = RepoDiscovery(cfg)

        repo = make_mock_repo()
        meta = discoverer._extract_metadata(repo)

        assert meta["full_name"] == "apache/commons-lang"
        assert meta["stars"] == 500
        assert meta["language"] == "Java"
        assert meta["clone_url"].endswith(".git")
        assert "last_push" in meta

    def test_long_description_is_truncated(self, cfg, tmp_path):
        cfg.checkpoints.checkpoint_dir = str(tmp_path)
        with patch("src.discovery.repo_discovery.Github"):
            discoverer = RepoDiscovery(cfg)

        repo = make_mock_repo(description="x" * 500)
        meta = discoverer._extract_metadata(repo)
        assert len(meta["description"]) <= 200