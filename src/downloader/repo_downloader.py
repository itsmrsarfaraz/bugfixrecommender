"""
repo_downloader.py — Clone repos one at a time, safely.

Design decisions:
- Shallow clone (depth=1): saves 80-90% disk vs full clone.
  We only need the commit history from the extractor, and
  gitpython can still walk commits on a shallow clone.
- Clone → extract → delete: never hold more than one repo
  on disk at once. Non-negotiable on a 40GB SSD.
- Pre-clone size check: GitHub reports repo size in KB via API.
  We skip repos above our threshold before touching disk.
- Documentation repo detection: tutorial/learning repos have
  zero real bug-fix commits. Filtering them saves hours.
"""

import json
import shutil
import time
from pathlib import Path
from typing import List, Dict, Optional, Callable

import git
from git import Repo, GitCommandError

from src.config_loader import Config
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Repos whose names/descriptions suggest they are documentation,
# tutorial, awesome-lists, or interview-prep — not real application code.
# These patterns are matched against repo full_name (case-insensitive).
_DOCS_REPO_PATTERNS = [
    "awesome-",
    "interview",
    "tutorial",
    "learning",
    "guide",
    "book",
    "leetcode",
    "algorithm",          # pure algorithm collections, not applications
    "system-design",
    "-pdf",
    "tech-interview",
    "low-level-design",
    "source-code-hunter", # reading list repo
]


class RepoDownloader:
    """
    Downloads repositories one at a time from the discovery checkpoint.

    The extractor_callback is called immediately after each successful
    clone, before the repo is deleted from disk. This keeps only one
    repo on disk at any moment.

    Args:
        cfg:               Validated pipeline config.
        extractor_callback: Optional function called with (repo_path, repo_meta).
                           Pass None to clone without extracting (debug mode).
        max_repo_size_mb:  Skip repos larger than this (default: 500MB).
                           JetBrains/intellij-community is ~2GB — we skip it.
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

        # Load which repos are already processed (clone+extract done)
        self.processed: Dict[str, dict] = self._load_checkpoint()

    # ── Public API ────────────────────────────────────────────

    def run(self) -> Dict[str, dict]:
        """
        Process all repos from the discovery checkpoint.

        Returns:
            Dict of successfully processed repo metadata.
        """
        repos = self._load_discovered_repos()
        if not repos:
            logger.error(
                "No discovered repos found. "
                "Run discovery step first: python main.py --step discovery"
            )
            return {}

        total = len(repos)
        batch_size = self.cfg.downloader.batch_size
        logger.info(
            f"Downloader starting. {total} repos discovered. "
            f"{len(self.processed)} already processed. "
            f"Batch size: {batch_size}."
        )

        processed_this_run = 0
        skipped = 0
        failed = 0

        for repo_meta in repos:
            full_name = repo_meta["full_name"]

            # Skip if already processed in a previous run
            if full_name in self.processed:
                logger.debug(f"Already processed: {full_name}")
                continue

            # Stop when batch limit reached for this run
            if processed_this_run >= batch_size:
                logger.info(
                    f"Batch limit of {batch_size} reached. "
                    f"Re-run to process next batch."
                )
                break

            logger.info(
                f"[{processed_this_run + 1}/{batch_size}] Processing: {full_name}"
            )

            # Pre-clone validation
            skip_reason = self._pre_clone_check(repo_meta)
            if skip_reason:
                logger.warning(f"Skipping {full_name}: {skip_reason}")
                skipped += 1
                # Mark as skipped so we don't retry it next run
                self._mark_skipped(repo_meta, skip_reason)
                continue

            # Clone → extract → delete cycle
            success = self._process_one_repo(repo_meta)

            if success:
                processed_this_run += 1
            else:
                failed += 1

        logger.info(
            f"Downloader run complete. "
            f"Processed: {processed_this_run} | "
            f"Skipped: {skipped} | "
            f"Failed: {failed}"
        )
        return self.processed

    # ── Core pipeline: one repo ───────────────────────────────

    def _process_one_repo(self, repo_meta: dict) -> bool:
        """
        Clone → callback → delete cycle for a single repo.

        Returns True on success, False on any failure.
        Cleans up partial clone on any error.
        """
        full_name = repo_meta["full_name"]
        # Use only the repo name (not owner) as the local directory name
        # to avoid path depth issues on Windows.
        repo_dir_name = full_name.replace("/", "__")
        repo_path = self.clone_dir / repo_dir_name

        # Clean up any leftover partial clone from a previous failed run
        if repo_path.exists():
            logger.warning(
                f"Leftover directory found: {repo_path}. "
                f"Removing before fresh clone."
            )
            shutil.rmtree(repo_path, ignore_errors=True)

        # ── Clone ─────────────────────────────────────────────
        cloned = self._shallow_clone(
            clone_url=repo_meta["clone_url"],
            target_path=repo_path,
            full_name=full_name,
        )

        if not cloned:
            return False

        # ── Extract ───────────────────────────────────────────
        try:
            if self.extractor_callback is not None:
                logger.info(f"Running extractor on: {full_name}")
                self.extractor_callback(repo_path, repo_meta)
            else:
                logger.debug(f"No extractor callback. Skipping extraction for: {full_name}")
        except Exception as e:
            logger.error(f"Extractor failed on {full_name}: {e}")
            # Extraction failure does NOT count as a downloader failure.
            # We still delete the repo and checkpoint it so we don't re-clone.

        # ── Delete ────────────────────────────────────────────
        if self.cfg.downloader.cleanup_after_extraction:
            self._delete_repo(repo_path, full_name)

        # ── Checkpoint ────────────────────────────────────────
        self.processed[full_name] = {**repo_meta, "status": "processed"}
        self._save_checkpoint()
        logger.info(f"Done: {full_name}")
        return True

    def _shallow_clone(
        self, clone_url: str, target_path: Path, full_name: str
    ) -> bool:
        """
        Perform a shallow git clone (depth=1) with retry.

        depth=1 means we only download the latest snapshot of each file
        plus enough history to walk commits — typically 80-90% smaller
        than a full clone. For a repo like elasticsearch this is the
        difference between 200MB and 2GB.

        Returns True on success, False after retries exhausted.
        """
        timeout = self.cfg.downloader.clone_timeout_seconds
        max_retries = 2

        for attempt in range(1, max_retries + 1):
            try:
                logger.info(
                    f"Cloning {full_name} "
                    f"(attempt {attempt}/{max_retries}, depth=1)..."
                )
                Repo.clone_from(
                    url=clone_url,
                    to_path=str(target_path),
                    depth=1,                    # shallow — critical for disk safety
                    single_branch=True,         # only default branch
                    no_tags=True,               # skip tag objects
                    env={"GIT_TERMINAL_PROMPT": "0"},  # never prompt for credentials
                )
                logger.info(f"Clone successful: {full_name}")
                return True

            except GitCommandError as e:
                logger.warning(
                    f"Clone attempt {attempt} failed for {full_name}: {e}"
                )
                # Clean up partial clone before retry
                if target_path.exists():
                    shutil.rmtree(target_path, ignore_errors=True)

                if attempt < max_retries:
                    wait = 10 * attempt  # 10s, 20s
                    logger.info(f"Waiting {wait}s before retry...")
                    time.sleep(wait)

            except Exception as e:
                logger.error(f"Unexpected clone error for {full_name}: {e}")
                if target_path.exists():
                    shutil.rmtree(target_path, ignore_errors=True)
                return False

        logger.error(f"All clone attempts failed for {full_name}. Skipping.")
        return False

    def _delete_repo(self, repo_path: Path, full_name: str) -> None:
        """Delete cloned repo from disk to reclaim space."""
        try:
            shutil.rmtree(repo_path, ignore_errors=True)
            logger.info(f"Deleted: {full_name} (disk reclaimed)")
        except Exception as e:
            logger.warning(f"Could not delete {repo_path}: {e}")

    # ── Pre-clone validation ──────────────────────────────────

    def _pre_clone_check(self, repo_meta: dict) -> Optional[str]:
        """
        Return a rejection reason if the repo should be skipped,
        or None if it's safe to clone.

        Checks (in order):
        1. Free disk space
        2. Repo size (from GitHub metadata, in KB)
        3. Documentation/tutorial repo detection
        """
        # 1. Disk space check
        free_gb = self._free_disk_gb()
        min_gb = self.cfg.downloader.min_free_disk_gb
        if free_gb < min_gb:
            return (
                f"insufficient disk space "
                f"({free_gb:.1f}GB free, need {min_gb}GB)"
            )

        # 2. Repo size check
        # GitHub reports size in KB. We cap at max_repo_size_mb.
        # Note: GitHub's size field is approximate and excludes
        # some binary assets, but it's good enough for our filter.
        repo_size_kb = repo_meta.get("size_kb")
        if repo_size_kb is not None:
            size_mb = repo_size_kb / 1024
            if size_mb > self.max_repo_size_mb:
                return (
                    f"repo too large "
                    f"({size_mb:.0f}MB > {self.max_repo_size_mb}MB limit)"
                )

        # 3. Documentation/tutorial repo detection
        full_name_lower = repo_meta["full_name"].lower()
        description_lower = (repo_meta.get("description") or "").lower()

        for pattern in _DOCS_REPO_PATTERNS:
            if pattern in full_name_lower or pattern in description_lower:
                return f"likely documentation/tutorial repo (matched: '{pattern}')"

        return None  # passes all checks

    def _free_disk_gb(self) -> float:
        """Return free disk space in GB at the clone directory."""
        usage = shutil.disk_usage(self.clone_dir)
        return usage.free / (1024 ** 3)

    # ── Checkpoint helpers ────────────────────────────────────

    def _load_discovered_repos(self) -> List[dict]:
        """Load the list of repos from the discovery checkpoint."""
        if not self.discovery_checkpoint.exists():
            return []
        try:
            with open(self.discovery_checkpoint, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Could not load discovery checkpoint: {e}")
            return []

    def _load_checkpoint(self) -> Dict[str, dict]:
        """Load already-processed repos from checkpoint."""
        if not self.checkpoint_path.exists():
            return {}
        try:
            with open(self.checkpoint_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            result = {r["full_name"]: r for r in data}
            logger.info(f"Downloader checkpoint: {len(result)} repos already processed.")
            return result
        except Exception as e:
            logger.warning(f"Could not load downloader checkpoint: {e}. Starting fresh.")
            return {}

    def _save_checkpoint(self) -> None:
        """Atomic checkpoint write."""
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.checkpoint_path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(list(self.processed.values()), f, indent=2, default=str)
            tmp.replace(self.checkpoint_path)
        except Exception as e:
            logger.error(f"Failed to save downloader checkpoint: {e}")
            if tmp.exists():
                tmp.unlink()

    def _mark_skipped(self, repo_meta: dict, reason: str) -> None:
        """
        Mark a repo as skipped in the checkpoint so we don't
        attempt it again on future runs.
        """
        full_name = repo_meta["full_name"]
        self.processed[full_name] = {
            **repo_meta,
            "status": "skipped",
            "skip_reason": reason,
        }
        self._save_checkpoint()