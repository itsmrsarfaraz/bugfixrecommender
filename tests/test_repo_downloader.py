"""
test_repo_downloader.py — Unit tests for RepoDownloader.

All tests are fully offline. Git clone is mocked.

Run with: pytest tests/ -v
"""

import json
import shutil
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.config_loader import load_config
from src.downloader.repo_downloader import RepoDownloader, _DOCS_REPO_PATTERNS


@pytest.fixture
def cfg(tmp_path):
    cfg = load_config("config/config.yaml")
    cfg.checkpoints.checkpoint_dir = str(tmp_path / "checkpoints")
    cfg.downloader.clone_dir = str(tmp_path / "raw")
    cfg.downloader.batch_size = 5
    cfg.downloader.cleanup_after_extraction = True
    return cfg


def make_repo_meta(
    full_name="apache/commons-lang",
    clone_url="https://github.com/apache/commons-lang.git",
    size_kb=50_000,  # 50MB
    description="Apache Commons Lang",
):
    return {
        "full_name": full_name,
        "clone_url": clone_url,
        "stars": 500,
        "language": "Java",
        "last_push": "2024-01-01T00:00:00+00:00",
        "default_branch": "main",
        "description": description,
        "size_kb": size_kb,
    }


# ── Pre-clone checks ──────────────────────────────────────────

class TestPreCloneCheck:

    def test_good_repo_passes(self, cfg):
        dl = RepoDownloader(cfg)
        meta = make_repo_meta()
        assert dl._pre_clone_check(meta) is None

    def test_large_repo_rejected(self, cfg):
        dl = RepoDownloader(cfg)
        # 600MB — over our 500MB default limit
        meta = make_repo_meta(size_kb=600_000)
        reason = dl._pre_clone_check(meta)
        assert reason is not None
        assert "too large" in reason

    def test_docs_repo_rejected_by_name(self, cfg):
        dl = RepoDownloader(cfg)
        meta = make_repo_meta(full_name="user/awesome-java-resources")
        reason = dl._pre_clone_check(meta)
        assert reason is not None
        assert "documentation" in reason

    def test_tutorial_repo_rejected_by_description(self, cfg):
        dl = RepoDownloader(cfg)
        meta = make_repo_meta(
            full_name="user/myrepo",
            description="A complete tutorial for learning Java"
        )
        reason = dl._pre_clone_check(meta)
        assert reason is not None

    def test_interview_repo_rejected(self, cfg):
        dl = RepoDownloader(cfg)
        meta = make_repo_meta(full_name="gyoogle/tech-interview-for-developer")
        reason = dl._pre_clone_check(meta)
        assert reason is not None

    def test_real_project_passes(self, cfg):
        dl = RepoDownloader(cfg)
        meta = make_repo_meta(
            full_name="apache/kafka",
            description="Mirror of Apache Kafka",
            size_kb=200_000,  # 200MB — under limit
        )
        assert dl._pre_clone_check(meta) is None


# ── Checkpoint ────────────────────────────────────────────────

class TestCheckpointing:

    def test_checkpoint_saves_and_reloads(self, cfg, tmp_path):
        dl = RepoDownloader(cfg)
        meta = make_repo_meta()
        dl.processed["apache/commons-lang"] = {**meta, "status": "processed"}
        dl._save_checkpoint()

        dl2 = RepoDownloader(cfg)
        assert "apache/commons-lang" in dl2.processed

    def test_skipped_repos_are_checkpointed(self, cfg):
        dl = RepoDownloader(cfg)
        meta = make_repo_meta()
        dl._mark_skipped(meta, "too large")
        assert dl.processed["apache/commons-lang"]["status"] == "skipped"
        assert "too large" in dl.processed["apache/commons-lang"]["skip_reason"]


# ── Clone + process cycle ─────────────────────────────────────

class TestProcessCycle:

    def test_extractor_callback_called_on_success(self, cfg, tmp_path):
        """Extractor callback must be invoked after a successful clone."""
        dl = RepoDownloader(cfg)
        callback_calls = []

        def fake_callback(path, meta):
            callback_calls.append((path, meta["full_name"]))

        dl.extractor_callback = fake_callback
        meta = make_repo_meta()

        with patch("src.downloader.repo_downloader.Repo") as mock_repo_cls:
            mock_repo_cls.clone_from.return_value = MagicMock()
            # Create the target directory to simulate a successful clone
            target = Path(cfg.downloader.clone_dir) / "apache__commons-lang"
            target.mkdir(parents=True, exist_ok=True)

            result = dl._process_one_repo(meta)

        assert result is True
        assert len(callback_calls) == 1
        assert callback_calls[0][1] == "apache/commons-lang"

    def test_failed_clone_returns_false(self, cfg):
        dl = RepoDownloader(cfg)

        with patch("src.downloader.repo_downloader.Repo") as mock_repo_cls:
            from git import GitCommandError
            mock_repo_cls.clone_from.side_effect = GitCommandError("clone", 128)
            meta = make_repo_meta()
            result = dl._shallow_clone(
                clone_url=meta["clone_url"],
                target_path=Path(cfg.downloader.clone_dir) / "test",
                full_name=meta["full_name"],
            )

        assert result is False

    def test_no_discovered_repos_returns_empty(self, cfg):
        """run() must exit cleanly if discovery hasn't been run yet."""
        dl = RepoDownloader(cfg)
        result = dl.run()
        assert result == {}

    def test_already_processed_repos_are_skipped(self, cfg):
        """Repos already in the checkpoint must not be re-cloned."""
        meta = make_repo_meta()

        # Pre-populate checkpoint
        dl = RepoDownloader(cfg)
        dl.processed["apache/commons-lang"] = {**meta, "status": "processed"}
        dl._save_checkpoint()

        # Write a fake discovery checkpoint
        Path(cfg.checkpoints.checkpoint_dir).mkdir(parents=True, exist_ok=True)
        disc_path = (
            Path(cfg.checkpoints.checkpoint_dir)
            / "discovered_repos.json"
        )
        disc_path.write_text(json.dumps([meta]))

        clone_called = []
        dl2 = RepoDownloader(cfg)

        with patch.object(dl2, "_shallow_clone") as mock_clone:
            dl2.run()
            mock_clone.assert_not_called()