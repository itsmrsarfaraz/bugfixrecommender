"""
repo_downloader.py — Clone repos safely as bare repositories.

V2 design changes from V1:
- Bare clone (--bare): only downloads git objects, no working tree.
  This means NO files are written to disk → filename-too-long
  impossible on Windows. spring-boot, elasticsearch, dubbo all work.
- depth=500: captures last 500 commits for bug-fix extraction.
  depth=1 was wrong — it gave us exactly one commit to walk.
- Bare repos are smaller than full checkouts by 30-60%.
- gitpython reads commits and diffs from bare repos identically
  to normal repos — no API changes needed in the extractor.
"""

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import List, Dict, Optional, Callable

from git import Repo, GitCommandError, InvalidGitRepositoryError

from src.config_loader import Config
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Documentation/tutorial repos — zero real bug-fix commits.
# Matched against full_name and description (case-insensitive).
_DOCS_REPO_PATTERNS = [
    "awesome-",
    "interview",
    "tutorial",
    "learning",
    "-guide",
    "JavaGuide",
    "book",
    "leetcode",
    "-pdf",
    "tech-interview",
    "low-level-design",
    "source-code-hunter",
    "system-design-resources",
]

# How many commits to fetch per repo.
# 500 is enough to find dozens of bug-fix commits in active repos.
# Increase to 1000 later if dataset is too small.
CLONE_DEPTH = 500


class RepoDownloader:
    """
    Downloads repositories as bare git clones and passes them
    to the extractor callback before deleting.

    Bare clone = git objects only, no checked-out files.
    This solves Windows filename length limits permanently.
    """

    def __init__(
        self,
        cfg: Config,
        extractor_callback: Optional[Callable[[Path, dict], None]] = None,
        max_repo_size_mb: int = 500,
    ) -> None:
        self.cfg = cfg
        self.extractor_callback = extractor_callback
        self.max_repo_size_mb = max_repo_size_mb

        self.clone_dir = Path(cfg.downloader.clone_dir)
        self.clone_dir.mkdir(parents=True, exist_ok=True)

        self.checkpoint_path = (
            Path(cfg.checkpoints.checkpoint_dir)
            / cfg.checkpoints.cloned_repos_file
        )
        self.discovery_checkpoint = (
            Path(cfg.checkpoints.checkpoint_dir)
            / cfg.checkpoints.discovered_repos_file
        )

        self.processed: Dict[str, dict] = self._load_checkpoint()

    # ── Public API ────────────────────────────────────────────

    def run(self) -> Dict[str, dict]:
        repos = self._load_discovered_repos()
        if not repos:
            logger.error(
                "No discovered repos found. "
                "Run: python main.py --step discovery"
            )
            return {}

        total = len(repos)
        batch_size = self.cfg.downloader.batch_size
        logger.info(
            f"Downloader starting. {total} discovered. "
            f"{len(self.processed)} already processed. "
            f"Batch: {batch_size} | Clone depth: {CLONE_DEPTH}"
        )

        processed_this_run = 0
        skipped = 0
        failed = 0

        for repo_meta in repos:
            full_name = repo_meta["full_name"]

            if full_name in self.processed:
                logger.debug(f"Already processed: {full_name}")
                continue

            if processed_this_run >= batch_size:
                logger.info(f"Batch limit of {batch_size} reached. Re-run for next batch.")
                break

            logger.info(f"[{processed_this_run + 1}/{batch_size}] Processing: {full_name}")

            skip_reason = self._pre_clone_check(repo_meta)
            if skip_reason:
                logger.warning(f"Skipping {full_name}: {skip_reason}")
                self._mark_skipped(repo_meta, skip_reason)
                skipped += 1
                continue

            success = self._process_one_repo(repo_meta)
            if success:
                processed_this_run += 1
            else:
                failed += 1

        logger.info(
            f"Run complete. Processed: {processed_this_run} | "
            f"Skipped: {skipped} | Failed: {failed}"
        )
        return self.processed

    # ── Core cycle: one repo ──────────────────────────────────

    def _process_one_repo(self, repo_meta: dict) -> bool:
        full_name = repo_meta["full_name"]
        repo_dir_name = full_name.replace("/", "__") + ".git"  # .git suffix = bare convention
        repo_path = self.clone_dir / repo_dir_name

        # Remove any leftover partial clone from a previous failed run.
        # Force-remove on Windows using rmtree with retry.
        if repo_path.exists():
            logger.warning(f"Leftover bare repo found: {repo_path}. Removing.")
            self._force_remove(repo_path)

        cloned = self._bare_clone(
            clone_url=repo_meta["clone_url"],
            target_path=repo_path,
            full_name=full_name,
        )

        if not cloned:
            return False

        # Verify the bare repo is readable before invoking extractor
        if not self._is_valid_bare_repo(repo_path):
            logger.error(f"Bare repo invalid after clone: {full_name}")
            self._force_remove(repo_path)
            return False

        try:
            if self.extractor_callback is not None:
                logger.info(f"Running extractor on: {full_name}")
                self.extractor_callback(repo_path, repo_meta)
        except Exception as e:
            logger.error(f"Extractor failed on {full_name}: {e}")
            # Don't fail the downloader — still checkpoint and delete

        if self.cfg.downloader.cleanup_after_extraction:
            self._force_remove(repo_path)
            logger.info(f"Deleted bare repo: {full_name}")

        self.processed[full_name] = {**repo_meta, "status": "processed"}
        self._save_checkpoint()
        logger.info(f"Done: {full_name}")
        return True

    def _bare_clone(
        self, clone_url: str, target_path: Path, full_name: str
    ) -> bool:
        """
        Bare clone with depth=500 using subprocess directly.

        WHY subprocess instead of gitpython's Repo.clone_from:
        - gitpython does not expose --bare cleanly with all options.
        - subprocess gives us exact control over git arguments.
        - We capture stderr for clean error logging.

        WHY bare:
        - No working tree → no files written to disk → no Windows
          filename length errors. Ever.
        - Git objects only. Extractor reads diffs from objects directly.
        """
        max_retries = 2

        for attempt in range(1, max_retries + 1):
            # Clean up before each attempt (paranoid but safe)
            if target_path.exists():
                self._force_remove(target_path)

            try:
                logger.info(
                    f"Bare cloning {full_name} "
                    f"(attempt {attempt}/{max_retries}, depth={CLONE_DEPTH})..."
                )

                result = subprocess.run(
                    [
                        "git", "clone",
                        "--bare",
                        f"--depth={CLONE_DEPTH}",
                        "--no-tags",
                        "-q",                   # quiet — suppress progress to logs
                        clone_url,
                        str(target_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=self.cfg.downloader.clone_timeout_seconds,
                    # GIT_TERMINAL_PROMPT=0 prevents git from hanging
                    # waiting for credentials on private repos
                    env={**self._base_env(), "GIT_TERMINAL_PROMPT": "0"},
                )

                if result.returncode == 0:
                    logger.info(f"Bare clone successful: {full_name}")
                    return True
                else:
                    logger.warning(
                        f"Bare clone attempt {attempt} failed for {full_name}. "
                        f"stderr: {result.stderr[:300]}"
                    )

            except subprocess.TimeoutExpired:
                logger.warning(
                    f"Clone timed out for {full_name} "
                    f"(>{self.cfg.downloader.clone_timeout_seconds}s)"
                )
            except Exception as e:
                logger.error(f"Unexpected clone error for {full_name}: {e}")
                return False

            if target_path.exists():
                self._force_remove(target_path)

            if attempt < max_retries:
                wait = 15 * attempt
                logger.info(f"Waiting {wait}s before retry...")
                time.sleep(wait)

        logger.error(f"All clone attempts failed for {full_name}.")
        return False

    def _is_valid_bare_repo(self, repo_path: Path) -> bool:
        """
        Check that the bare repo is readable by gitpython.
        A partial clone can pass subprocess returncode=0 but
        still produce a corrupt git object store.
        """
        try:
            repo = Repo(str(repo_path))
            # Try to access the HEAD commit — if this works, repo is valid
            _ = repo.head.commit
            return True
        except Exception:
            return False

    # ── Pre-clone filtering ───────────────────────────────────

    def _pre_clone_check(self, repo_meta: dict) -> Optional[str]:
        """Return rejection reason or None if safe to clone."""

        # 1. Disk space
        free_gb = self._free_disk_gb()
        min_gb = self.cfg.downloader.min_free_disk_gb
        if free_gb < min_gb:
            return f"insufficient disk ({free_gb:.1f}GB free, need {min_gb}GB)"

        # 2. Repo size (GitHub reports in KB)
        size_kb = repo_meta.get("size_kb")
        if size_kb is not None:
            size_mb = size_kb / 1024
            if size_mb > self.max_repo_size_mb:
                return f"too large ({size_mb:.0f}MB > {self.max_repo_size_mb}MB)"

        # 3. Docs/tutorial repo detection
        full_name_lower = repo_meta["full_name"].lower()
        desc_lower = (repo_meta.get("description") or "").lower()
        for pattern in _DOCS_REPO_PATTERNS:
            p = pattern.lower()
            if p in full_name_lower or p in desc_lower:
                return f"docs/tutorial repo (matched: '{pattern}')"

        return None

    def _free_disk_gb(self) -> float:
        return shutil.disk_usage(self.clone_dir).free / (1024 ** 3)

    # ── Robust deletion ───────────────────────────────────────

    def _force_remove(self, path: Path) -> None:
        """
        Delete a directory tree robustly on Windows.

        Windows problem: git pack files and index files are marked
        read-only by git. shutil.rmtree fails on read-only files.
        Solution: use onerror handler to chmod before retry.
        """
        import stat

        def handle_readonly(func, fpath, exc_info):
            """Make file writable then retry deletion."""
            try:
                Path(fpath).chmod(stat.S_IWRITE)
                func(fpath)
            except Exception:
                pass  # Best effort — log but don't crash

        try:
            shutil.rmtree(path, onerror=handle_readonly)
        except Exception as e:
            logger.warning(f"Could not fully remove {path}: {e}")

    # ── Checkpoint helpers ────────────────────────────────────

    def _load_discovered_repos(self) -> List[dict]:
        if not self.discovery_checkpoint.exists():
            return []
        try:
            with open(self.discovery_checkpoint, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Could not load discovery checkpoint: {e}")
            return []

    def _load_checkpoint(self) -> Dict[str, dict]:
        if not self.checkpoint_path.exists():
            return {}
        try:
            with open(self.checkpoint_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            result = {r["full_name"]: r for r in data}
            logger.info(f"Downloader checkpoint: {len(result)} already processed.")
            return result
        except Exception as e:
            logger.warning(f"Could not load downloader checkpoint: {e}. Starting fresh.")
            return {}

    def _save_checkpoint(self) -> None:
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.checkpoint_path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(list(self.processed.values()), f, indent=2, default=str)
            tmp.replace(self.checkpoint_path)
        except Exception as e:
            logger.error(f"Failed to save checkpoint: {e}")
            if tmp.exists():
                tmp.unlink()

    def _mark_skipped(self, repo_meta: dict, reason: str) -> None:
        full_name = repo_meta["full_name"]
        self.processed[full_name] = {
            **repo_meta,
            "status": "skipped",
            "skip_reason": reason,
        }
        self._save_checkpoint()

    @staticmethod
    def _base_env() -> dict:
        """Return OS environment variables needed for subprocess git calls."""
        import os
        return dict(os.environ)