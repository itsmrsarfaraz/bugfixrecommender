"""
repo_discovery.py — Discover Java repositories from GitHub.

WHY this is a separate module from downloader:
- Discovery is read-only and fast (API calls only).
- Downloader is destructive (writes to disk, takes hours).
- Separating them lets you re-run either independently.
- Clean single responsibility.

OUTPUT:
    checkpoints/discovered_repos.json
    [
      {
        "full_name": "apache/commons-lang",
        "clone_url": "https://github.com/apache/commons-lang.git",
        "stars": 2400,
        "language": "Java",
        "last_push": "2024-03-01T12:00:00Z",
        "default_branch": "master"
      },
      ...
    ]
"""

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Optional

from github import Github, GithubException, RateLimitExceededException

from src.config_loader import Config
from src.utils.logger import get_logger

logger = get_logger(__name__)


class RepoDiscovery:
    """
    Discovers GitHub repositories matching our quality filters.

    Attributes:
        cfg:              Validated pipeline config.
        checkpoint_path:  Path to discovered_repos.json.
        discovered:       Dict[full_name → repo_metadata] for O(1) dedup.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.checkpoint_path = Path(cfg.checkpoints.checkpoint_dir) / cfg.checkpoints.discovered_repos_file

        # Load any repos already discovered in a previous run.
        # This is what makes discovery resumable.
        self.discovered: Dict[str, dict] = self._load_checkpoint()

        # Build PyGithub client.
        # Authenticated = 5000 req/hr. Unauthenticated = 60 req/hr.
        token = cfg.github.token
        if token:
            self._gh = Github(token)
            logger.info("GitHub client: authenticated (5000 req/hr)")
        else:
            self._gh = Github()
            logger.warning("GitHub client: unauthenticated (60 req/hr) — set GITHUB_TOKEN")

    # ── Public API ────────────────────────────────────────────

    def run(self) -> List[dict]:
        """
        Run discovery until we have cfg.github.max_repos repos
        or we exhaust GitHub search results.

        Returns:
            List of repo metadata dicts.
        """
        target = self.cfg.github.max_repos
        already_have = len(self.discovered)

        if already_have >= target:
            logger.info(
                f"Checkpoint already has {already_have} repos "
                f"(target: {target}). Skipping discovery."
            )
            return list(self.discovered.values())

        logger.info(
            f"Starting discovery. Have {already_have}/{target} repos. "
            f"Need {target - already_have} more."
        )

        query = self._build_search_query()
        logger.info(f"GitHub search query: '{query}'")

        try:
            repos = self._gh.search_repositories(
                query=query,
                sort="stars",
                order="desc",
            )
        except GithubException as e:
            logger.error(f"GitHub search failed: {e}")
            return list(self.discovered.values())

        page_num = 0
        for repo in repos:
            # Stop if we have enough repos
            if len(self.discovered) >= target:
                logger.info(f"Reached target of {target} repos. Stopping.")
                break

            # Rate limit — check and sleep if needed
            self._handle_rate_limit()

            # Skip if already discovered (dedup across runs)
            if repo.full_name in self.discovered:
                logger.debug(f"Skip (already discovered): {repo.full_name}")
                continue

            # Apply quality filters
            reason = self._filter_reason(repo)
            if reason:
                logger.debug(f"Skip ({reason}): {repo.full_name}")
                continue

            # Repo passes — record it
            entry = self._extract_metadata(repo)
            self.discovered[repo.full_name] = entry
            logger.info(
                f"[{len(self.discovered)}/{target}] Discovered: "
                f"{repo.full_name} ★{repo.stargazers_count}"
            )

            # Checkpoint after every accepted repo so we never
            # lose progress to a rate limit or crash
            self._save_checkpoint()

            # Polite delay — avoid secondary rate limits
            time.sleep(self.cfg.github.request_delay_seconds)

            # Log page progress every 20 repos
            page_num += 1
            if page_num % 20 == 0:
                self._log_rate_limit_status()

        final_count = len(self.discovered)
        logger.info(f"Discovery complete. Total repos: {final_count}")
        return list(self.discovered.values())

    # ── Private helpers ───────────────────────────────────────

    def _build_search_query(self) -> str:
        """
        Build the GitHub search query string.

        We use stars threshold in the query itself so GitHub
        pre-filters before we even paginate — saves API calls.
        """
        lang = self.cfg.github.language
        stars = self.cfg.github.min_stars
        return f"language:{lang} stars:>={stars}"

    def _filter_reason(self, repo) -> Optional[str]:
        """
        Return a rejection reason string if repo should be skipped,
        or None if the repo passes all filters.

        WHY per-filter reason strings:
        - Readable debug logs tell you exactly why a repo was skipped.
        - Easy to tune thresholds based on actual skip reasons.
        """
        # Stars check (redundant with query but explicit for logging)
        if repo.stargazers_count < self.cfg.github.min_stars:
            return f"stars too low ({repo.stargazers_count})"

        # Activity check — skip repos with no pushes recently
        if repo.pushed_at is None:
            return "no push date"

        cutoff = datetime.now(timezone.utc) - timedelta(
            days=self.cfg.github.min_activity_days
        )
        # pushed_at may be naive or aware depending on PyGithub version
        pushed = repo.pushed_at
        if pushed.tzinfo is None:
            pushed = pushed.replace(tzinfo=timezone.utc)

        if pushed < cutoff:
            return f"inactive (last push: {pushed.date()})"

        # Skip forks — we want original codebases.
        # Forks produce duplicate code pairs which inflates
        # dataset size without adding new patterns.
        if repo.fork:
            return "is a fork"

        # Skip archived repos — no longer maintained
        if repo.archived:
            return "archived"

        return None  # passes all filters

    def _extract_metadata(self, repo) -> dict:
        """
        Extract only the fields we need from the PyGithub repo object.

        WHY minimal metadata:
        - PyGithub objects hold 50+ fields. We don't need most of them.
        - Storing only what we use keeps checkpoint files small and clear.
        """
        pushed = repo.pushed_at
        if pushed is not None and pushed.tzinfo is None:
            pushed = pushed.replace(tzinfo=timezone.utc)

        return {
            "full_name": repo.full_name,
            "clone_url": repo.clone_url,
            "stars": repo.stargazers_count,
            "language": repo.language,
            "last_push": pushed.isoformat() if pushed else None,
            "default_branch": repo.default_branch,
            "description": (repo.description or "")[:200],  # truncate
        }

    def _handle_rate_limit(self) -> None:
        """
        Check remaining API calls. If below safety threshold,
        sleep until the rate limit window resets.

        WHY 50-call safety buffer (not 0):
        - Other PyGithub calls (repo metadata fetch) also consume quota.
        - Cutting it to zero causes unexpected 403s mid-loop.
        """
        try:
            rate = self._gh.get_rate_limit().search
            remaining = rate.remaining

            if remaining < 5:
                reset_time = rate.reset  # datetime (UTC)
                now = datetime.now(timezone.utc)
                sleep_seconds = max((reset_time - now).total_seconds() + 5, 10)
                logger.warning(
                    f"Rate limit low ({remaining} remaining). "
                    f"Sleeping {sleep_seconds:.0f}s until reset at {reset_time.strftime('%H:%M:%S')} UTC"
                )
                time.sleep(sleep_seconds)
                logger.info("Rate limit reset. Resuming discovery.")

        except Exception as e:
            # Don't crash the pipeline if rate limit check itself fails
            logger.warning(f"Could not check rate limit: {e}. Sleeping 10s as precaution.")
            time.sleep(10)

    def _log_rate_limit_status(self) -> None:
        """Log current rate limit status for operational visibility."""
        try:
            core = self._gh.get_rate_limit().core
            search = self._gh.get_rate_limit().search
            logger.info(
                f"Rate limit — core: {core.remaining}/{core.limit} | "
                f"search: {search.remaining}/{search.limit}"
            )
        except Exception:
            pass  # Non-critical — don't interrupt pipeline

    def _load_checkpoint(self) -> Dict[str, dict]:
        """
        Load previously discovered repos from checkpoint file.
        Returns empty dict if checkpoint doesn't exist yet.
        """
        if not self.checkpoint_path.exists():
            logger.debug("No discovery checkpoint found. Starting fresh.")
            return {}

        try:
            with open(self.checkpoint_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # data is a list → convert to dict keyed by full_name for O(1) dedup
            discovered = {r["full_name"]: r for r in data}
            logger.info(
                f"Loaded checkpoint: {len(discovered)} repos already discovered."
            )
            return discovered
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Checkpoint corrupted ({e}). Starting fresh.")
            return {}

    def _save_checkpoint(self) -> None:
        """
        Write current discovered repos to checkpoint file (atomic write).

        WHY atomic write (write to .tmp then rename):
        - If the process crashes mid-write, the checkpoint file
          stays valid. A direct write could produce a half-written
          JSON file that fails to parse on the next run.
        """
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.checkpoint_path.with_suffix(".tmp")

        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(list(self.discovered.values()), f, indent=2, default=str)
            # Atomic rename — replaces checkpoint only if write succeeded
            tmp_path.replace(self.checkpoint_path)
        except Exception as e:
            logger.error(f"Failed to save checkpoint: {e}")
            if tmp_path.exists():
                tmp_path.unlink()  # Clean up partial file