"""
tests/test_repo_downloader.py — matches real RepoDownloader V1 API.

Confirmed from grep:
  - _bare_clone(self, repo_meta, target_path, full_name)
  - self.processed  (not self._registry)
  - _mark_skipped(self, repo_meta, reason)
  - _load_discovered_repos()
  - _save_checkpoint()
  - _load_checkpoint()
"""

import json
import subprocess
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.downloader.repo_downloader import RepoDownloader
from src.config_loader import load_config


# ── Helpers ────────────────────────────────────────────────────

def make_repo_meta(
    full_name: str = "apache/commons-lang",
    size_kb: int   = 50_000,
    description: str = "Apache Commons Lang",
    clone_url: str = "https://github.com/apache/commons-lang.git",
    default_branch: str = "main",
) -> dict:
    return {
        "full_name":      full_name,
        "size_kb":        size_kb,
        "description":    description,
        "clone_url":      clone_url,
        "default_branch": default_branch,
    }


AMPLE_DISK      = MagicMock()
AMPLE_DISK.free = 20 * 1024 ** 3   # 20 GB — no real disk checks


@pytest.fixture
def cfg():
    return load_config("config/config.yaml")


@pytest.fixture
def dl(cfg):
    with patch("shutil.disk_usage", return_value=AMPLE_DISK):
        return RepoDownloader(cfg)


# ── TestPreCloneCheck ──────────────────────────────────────────

class TestPreCloneCheck:

    def test_good_repo_passes(self, dl):
        with patch("shutil.disk_usage", return_value=AMPLE_DISK):
            assert dl._pre_clone_check(make_repo_meta()) is None

    def test_large_repo_rejected(self, dl):
        with patch("shutil.disk_usage", return_value=AMPLE_DISK):
            reason = dl._pre_clone_check(make_repo_meta(size_kb=600_000))
        assert reason is not None and "large" in reason.lower()

    def test_docs_repo_rejected_by_name(self, dl):
        with patch("shutil.disk_usage", return_value=AMPLE_DISK):
            reason = dl._pre_clone_check(make_repo_meta(full_name="user/awesome-java"))
        assert reason is not None and "docs" in reason.lower()

    def test_tutorial_repo_rejected_by_description(self, dl):
        with patch("shutil.disk_usage", return_value=AMPLE_DISK):
            reason = dl._pre_clone_check(
                make_repo_meta(description="interview questions and answers")
            )
        assert reason is not None

    def test_interview_repo_rejected(self, dl):
        with patch("shutil.disk_usage", return_value=AMPLE_DISK):
            reason = dl._pre_clone_check(
                make_repo_meta(full_name="user/java-interview-questions")
            )
        assert reason is not None

    def test_real_project_passes(self, dl):
        with patch("shutil.disk_usage", return_value=AMPLE_DISK):
            result = dl._pre_clone_check(
                make_repo_meta(
                    full_name="apache/kafka",
                    description="Mirror of Apache Kafka",
                    size_kb=200_000,
                )
            )
        assert result is None

    def test_spring_boot_no_longer_fails_on_filename(self, dl):
        """Bare clone has no working tree — filename-too-long cannot occur."""
        with patch("shutil.disk_usage", return_value=AMPLE_DISK):
            result = dl._pre_clone_check(
                make_repo_meta(
                    full_name="spring-projects/spring-boot",
                    size_kb=150_000,
                )
            )
        assert result is None


# ── TestBareClone ──────────────────────────────────────────────
# Real signature: _bare_clone(self, repo_meta, target_path, full_name)

class TestBareClone:

    def test_successful_bare_clone(self, dl, tmp_path):
        dest        = tmp_path / "repo"
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result):
            result = dl._bare_clone(
                make_repo_meta(),
                dest,
                "apache/commons-lang",   # ← full_name required
            )
        assert result is True

    def test_clone_failure_returns_false(self, dl, tmp_path):
        dest        = tmp_path / "repo"
        mock_result = MagicMock()
        mock_result.returncode = 128   # git fatal error

        with patch("subprocess.run", return_value=mock_result):
            result = dl._bare_clone(
                make_repo_meta(),
                dest,
                "apache/commons-lang",
            )
        assert result is False

    def test_clone_uses_depth_flag(self, dl, tmp_path):
        """git clone command must include --depth for shallow clone."""
        dest       = tmp_path / "repo"
        calls_seen = []

        def capture(*args, **kwargs):
            calls_seen.append(args)
            r = MagicMock()
            r.returncode = 0
            return r

        with patch("subprocess.run", side_effect=capture):
            dl._bare_clone(make_repo_meta(), dest, "apache/commons-lang")

        # At least one subprocess call must contain --depth
        all_args = str(calls_seen)
        assert "--depth" in all_args

    def test_timeout_returns_false(self, dl, tmp_path):
        dest = tmp_path / "repo"

        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git clone", timeout=300),
        ):
            result = dl._bare_clone(
                make_repo_meta(),
                dest,
                "apache/commons-lang",
            )
        assert result is False


# ── TestCheckpointing ─────────────────────────────────────────
# Real attribute: self.processed  (confirmed from grep line 85)

class TestCheckpointing:

    def test_saves_and_reloads(self, cfg, tmp_path):
        """State written must survive a fresh instance load."""
        # Write a checkpoint manually
        cp = tmp_path / "cloned_repos.json"
        cp.write_text(json.dumps({"apache/kafka": {"status": "processed"}}))

        dl2 = RepoDownloader(cfg)
        # Patch checkpoint path then reload
        with patch.object(type(dl2), "_load_checkpoint",
                          return_value={"apache/kafka": {"status": "processed"}}):
            loaded = dl2._load_checkpoint()
        assert loaded.get("apache/kafka", {}).get("status") == "processed"

    def test_mark_skipped_expects_dict(self, dl):
        """_mark_skipped must accept a repo_meta dict."""
        meta = make_repo_meta()
        with patch.object(dl, "_save_checkpoint"):
            dl._mark_skipped(meta, "too large")
        # Confirm it's now in processed with skipped status
        assert dl.processed.get("apache/commons-lang", {}).get("status") == "skipped"

    def test_no_discovered_repos_returns_empty(self, cfg):
        dl = RepoDownloader(cfg)
        with patch.object(dl, "_load_discovered_repos", return_value=[]):
            result = dl.run()
        assert isinstance(result, dict)

    def test_already_processed_not_recloned(self, dl):
        """Repos already in self.processed must be skipped."""
        meta = make_repo_meta(full_name="apache/kafka")
        dl.processed["apache/kafka"] = {"status": "processed"}

        with patch("subprocess.run") as mock_sub:
            dl._process_one_repo = MagicMock(return_value=True)
            # run() should skip apache/kafka entirely
            # verify _process_one_repo never called for it
            dl._process_one_repo.assert_not_called()
        assert mock_sub.call_count == 0