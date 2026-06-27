"""
repo_discovery.py — Discover Java repositories from GitHub.

V2 change from V1:
- GitHub search API hard-caps results at 1,000 per query.
  One query (stars:>=50) hits this wall immediately.
- Fix: run multiple queries over non-overlapping star bands.
  Each band yields up to 1,000 results → 6 bands = ~6,000 repos.
- Everything else (checkpointing, filtering, rate limits) unchanged.

Star bands chosen so each returns close to 1,000 repos:
  50–100   | 101–250  | 251–500
  501–1000 | 1001–5000 | >5000
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

# Star bands — each maps to one GitHub search query.
# GitHub returns max 1,000 results per query sorted by stars desc.
# Non-overlapping bands prevent duplicate repos across queries.
_STAR_BANDS = [
    (50,   100),
    (101,  250),
    (251,  500),
    (501,  1000),
    (1001, 5000),
    (5001, None),   # None = no upper bound (stars:>5000)
]


class RepoDiscovery:
    """
    Discovers GitHub repositories matching our quality filters.

    V2: runs one search query per star band to work around
    GitHub's 1,000-result-per-query hard cap.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.checkpoint_path = (
            Path(cfg.checkpoints.checkpoint_dir)
            / cfg.checkpoints.discovered_repos_file
        )
        self.discovered: Dict[str, dict] = self._load_checkpoint()

        token = cfg.github.token
        if token:
            self._gh = Github(token)
            logger.info("GitHub client: authenticated (5000 req/hr)")
        else:
            self._gh = Github()
            logger.warning(
                "GitHub client: unauthenticated (60 req/hr) — set GITHUB_TOKEN"
            )

    # ── Public API ────────────────────────────────────────────

    def run(self) -> List[dict]:
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
            f"Need {target - already_have} more. "
            f"Running {len(_STAR_BANDS)} star-band queries."
        )

        for lo, hi in _STAR_BANDS:
            if len(self.discovered) >= target:
                logger.info(f"Reached target of {target} repos. Stopping.")
                break

            query = self._build_band_query(lo, hi)
            logger.info(
                f"Band query: '{query}' | "
                f"Discovered so far: {len(self.discovered)}/{target}"
            )
            self._run_one_band(query, target)

        final_count = len(self.discovered)
        logger.info(f"Discovery complete. Total repos: {final_count}")
        return list(self.discovered.values())

    # ── Per-band search ───────────────────────────────────────

    def _run_one_band(self, query: str, target: int) -> None:
        """
        Run one GitHub search query and collect repos until:
        - we hit the per-query 1,000-result cap, OR
        - we reach the global target, OR
        - results are exhausted.
        """
        try:
            repos = self._gh.search_repositories(
                query=query,
                sort="stars",
                order="desc",
            )
        except GithubException as e:
            logger.error(f"GitHub search failed for '{query}': {e}")
            return

        band_added = 0

        for repo in repos:
            if len(self.discovered) >= target:
                break

            self._handle_rate_limit()

            if repo.full_name in self.discovered:
                logger.debug(f"Skip (already discovered): {repo.full_name}")
                continue

            reason = self._filter_reason(repo)
            if reason:
                logger.debug(f"Skip ({reason}): {repo.full_name}")
                continue

            entry = self._extract_metadata(repo)
            self.discovered[repo.full_name] = entry
            band_added += 1
            logger.info(
                f"[{len(self.discovered)}/{target}] Discovered: "
                f"{repo.full_name} ★{repo.stargazers_count}"
            )
            self._save_checkpoint()
            time.sleep(self.cfg.github.request_delay_seconds)

        logger.info(
            f"Band '{query}' complete. Added {band_added} repos. "
            f"Total: {len(self.discovered)}"
        )

    # ── Query builder ─────────────────────────────────────────

    def _build_band_query(self, lo: int, hi: Optional[int]) -> str:
        """
        Build a star-range GitHub search query.

        Examples:
          lo=50,  hi=100  → 'language:Java stars:50..100'
          lo=5001, hi=None → 'language:Java stars:>5000'
        """
        lang = self.cfg.github.language
        if hi is None:
            return f"language:{lang} stars:>{lo - 1}"
        return f"language:{lang} stars:{lo}..{hi}"

    # ── Filtering ─────────────────────────────────────────────

    def _filter_reason(self, repo) -> Optional[str]:
        if repo.stargazers_count < self.cfg.github.min_stars:
            return f"stars too low ({repo.stargazers_count})"

        if repo.pushed_at is None:
            return "no push date"

        cutoff = datetime.now(timezone.utc) - timedelta(
            days=self.cfg.github.min_activity_days
        )
        pushed = repo.pushed_at
        if pushed.tzinfo is None:
            pushed = pushed.replace(tzinfo=timezone.utc)
        if pushed < cutoff:
            return f"inactive (last push: {pushed.date()})"

        if repo.fork:
            return "is a fork"

        if repo.archived:
            return "archived"

        return None

    # ── Metadata extraction ───────────────────────────────────

    def _extract_metadata(self, repo) -> dict:
        pushed = repo.pushed_at
        if pushed is not None and pushed.tzinfo is None:
            pushed = pushed.replace(tzinfo=timezone.utc)
        return {
            "full_name":      repo.full_name,
            "clone_url":      repo.clone_url,
            "stars":          repo.stargazers_count,
            "language":       repo.language,
            "last_push":      pushed.isoformat() if pushed else None,
            "default_branch": repo.default_branch,
            "description":    (repo.description or "")[:200],
        }

    # ── Rate limit handling ───────────────────────────────────

    def _handle_rate_limit(self) -> None:
        try:
            rate = self._gh.get_rate_limit().search
            if rate.remaining < 5:
                reset_time = rate.reset
                now = datetime.now(timezone.utc)
                sleep_seconds = max((reset_time - now).total_seconds() + 5, 10)
                logger.warning(
                    f"Rate limit low ({rate.remaining} remaining). "
                    f"Sleeping {sleep_seconds:.0f}s until "
                    f"{reset_time.strftime('%H:%M:%S')} UTC"
                )
                time.sleep(sleep_seconds)
                logger.info("Rate limit reset. Resuming.")
        except Exception as e:
            logger.warning(f"Rate limit check failed: {e}. Sleeping 10s.")
            time.sleep(10)

    # ── Checkpoint helpers ────────────────────────────────────

    def _load_checkpoint(self) -> Dict[str, dict]:
        if not self.checkpoint_path.exists():
            logger.debug("No discovery checkpoint found. Starting fresh.")
            return {}
        try:
            with open(self.checkpoint_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            discovered = {r["full_name"]: r for r in data}
            logger.info(
                f"Loaded checkpoint: {len(discovered)} repos already discovered."
            )
            return discovered
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Checkpoint corrupted ({e}). Starting fresh.")
            return {}

    def _save_checkpoint(self) -> None:
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.checkpoint_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(
                    list(self.discovered.values()), f,
                    indent=2, default=str
                )
            tmp_path.replace(self.checkpoint_path)
        except Exception as e:
            logger.error(f"Failed to save checkpoint: {e}")
            if tmp_path.exists():
                tmp_path.unlink()