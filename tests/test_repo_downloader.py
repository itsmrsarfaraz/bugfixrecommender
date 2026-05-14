"""
test_repo_downloader.py — Unit tests for RepoDownloader (bare clone).
All tests offline. subprocess git is mocked.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import subprocess

from src.config_loader import load_config
from src.downloader.repo_downloader import RepoDownloader


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
    size_kb=50_000,
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


# ── Pre-clone filtering ───────────────────────────────────────

class TestPreCloneCheck:

    def test_good_repo_passes(self, cfg):
        dl = RepoDownloader(cfg)
        assert dl._pre_clone_check(make_repo_meta()) is None

    def test_large_repo_rejected(self, cfg):
        dl = RepoDownloader(cfg)
        reason = dl._pre_clone_check(make_repo_meta(size_kb=600_000))
        assert reason is not None and "large" in reason

    def test_docs_repo_rejected_by_name(self, cfg):
        dl = RepoDownloader(cfg)
        reason = dl._pre_clone_check(make_repo_meta(full_name="user/awesome-java"))
        assert reason is not None and "docs" in reason

    def test_tutorial_repo_rejected_by_description(self, cfg):
        dl = RepoDownloader(cfg)
        reason = dl._pre_clone_check(
            make_repo_meta(full_name="user/repo", description="Java tutorial for beginners")
        )
        assert reason is not None

    def test_interview_repo_rejected(self, cfg):
        dl = RepoDownloader(cfg)
        reason = dl._pre_clone_check(make_repo_meta(full_name="gyoogle/tech-interview-for-developer"))
        assert reason is not None

    def test_real_project_passes(self, cfg):
        dl = RepoDownloader(cfg)
        assert dl._pre_clone_check(
            make_repo_meta(full_name="apache/kafka", description="Mirror of Apache Kafka", size_kb=200_000)
        ) is None

    def test_spring_boot_no_longer_fails_on_filename(self, cfg):
        """
        With bare clone there is NO working tree written to disk.
        spring-boot filename-too-long errors cannot occur.
        This test documents the fix — bare clone should succeed
        even for repos with 260+ char filenames.
        """
        dl = RepoDownloader(cfg)
        # spring-boot has reasonable size, not a docs repo → passes pre-check
        assert dl._pre_clone_check(
            make_repo_meta(full_name="spring-projects/spring-boot", size_kb=150_000)
        ) is None


# ── Bare clone ────────────────────────────────────────────────

class TestBareClone:

    def test_successful_bare_clone(self, cfg, tmp_path):
        dl = RepoDownloader(cfg)
        target = tmp_path / "test.git"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            result = dl._bare_clone(
                clone_url="https://github.com/apache/commons-lang.git",
                target_path=target,
                full_name="apache/commons-lang",
            )

        assert result is True
        # Verify --bare flag was passed
        call_args = mock_run.call_args[0][0]
        assert "--bare" in call_args

    def test_clone_failure_returns_false(self, cfg, tmp_path):
        dl = RepoDownloader(cfg)
        target = tmp_path / "test.git"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=128, stderr="fatal: repository not found"
            )
            result = dl._bare_clone(
                clone_url="https://github.com/bad/repo.git",
                target_path=target,
                full_name="bad/repo",
            )

        assert result is False

    def test_clone_uses_correct_depth(self, cfg, tmp_path):
        dl = RepoDownloader(cfg)
        target = tmp_path / "test.git"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            dl._bare_clone("https://github.com/a/b.git", target, "a/b")

        call_args = mock_run.call_args[0][0]
        depth_args = [a for a in call_args if a.startswith("--depth")]
        assert len(depth_args) == 1
        assert "500" in depth_args[0]

    def test_timeout_returns_false(self, cfg, tmp_path):
        dl = RepoDownloader(cfg)
        target = tmp_path / "test.git"

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 120)):
            result = dl._bare_clone("https://github.com/a/b.git", target, "a/b")

        assert result is False


# ── Checkpoint ────────────────────────────────────────────────

class TestCheckpointing:

    def test_saves_and_reloads(self, cfg):
        dl = RepoDownloader(cfg)
        meta = make_repo_meta()
        dl.processed["apache/commons-lang"] = {**meta, "status": "processed"}
        dl._save_checkpoint()

        dl2 = RepoDownloader(cfg)
        assert "apache/commons-lang" in dl2.processed

    def test_mark_skipped(self, cfg):
        dl = RepoDownloader(cfg)
        dl._mark_skipped(make_repo_meta(), "too large")
        assert dl.processed["apache/commons-lang"]["status"] == "skipped"

    def test_no_discovered_repos_returns_empty(self, cfg):
        dl = RepoDownloader(cfg)
        assert dl.run() == {}

    def test_already_processed_not_recloned(self, cfg):
        meta = make_repo_meta()
        Path(cfg.checkpoints.checkpoint_dir).mkdir(parents=True, exist_ok=True)

        # Pre-populate both checkpoints
        disc_path = Path(cfg.checkpoints.checkpoint_dir) / "discovered_repos.json"
        disc_path.write_text(json.dumps([meta]))

        dl = RepoDownloader(cfg)
        dl.processed["apache/commons-lang"] = {**meta, "status": "processed"}
        dl._save_checkpoint()

        dl2 = RepoDownloader(cfg)
        with patch.object(dl2, "_bare_clone") as mock_clone:
            dl2.run()
            mock_clone.assert_not_called()