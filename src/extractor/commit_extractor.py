"""
commit_extractor.py — Extract bug-fix pairs from a bare git repository.

This is the core of the entire pipeline.
Everything before this step was plumbing.
Everything after this step is ML.

Design:
- Three filtering layers (commit → diff → file) eliminate noise early.
- Language adapters make the core logic language-agnostic.
- Generator pattern: yields BugFixPair one at a time — never loads
  all commits or all file content into RAM simultaneously.
- Safe failure: any single commit/file error is logged and skipped.
  One bad commit never crashes the extraction of an entire repo.
"""

import re
from pathlib import Path
from typing import Iterator, List, Optional, Set

from git import Repo, InvalidGitRepositoryError
from git.exc import GitCommandError

from src.config_loader import Config
from src.extractor.language_adapter import get_adapter, LanguageAdapter
from src.extractor.models import BugFixPair
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Max commits to walk per repo.
# With depth=500 this is our ceiling. Adjust if you increase CLONE_DEPTH.
MAX_COMMITS_PER_REPO = 500

# Max file size to process (bytes).
# Files > 200KB are likely generated code (protobuf, auto-generated sources).
# Generated code has zero value as bug-fix training data.
MAX_FILE_SIZE_BYTES = 200_000  # 200 KB


class CommitExtractor:
    """
    Extracts bug-fix (buggy_code, fixed_code) pairs from a bare git repo.

    Usage:
        extractor = CommitExtractor(cfg)
        for pair in extractor.extract(repo_path, repo_meta):
            storage.write(pair)
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

        # Pre-compile keyword patterns for performance.
        # re.IGNORECASE handles "Fix", "FIX", "fix" uniformly.
        bug_kws = cfg.extractor.bug_fix_keywords
        noise_kws = cfg.extractor.noise_keywords

        self._bug_pattern = re.compile(
            r'\b(' + '|'.join(re.escape(k) for k in bug_kws) + r')\b',
            re.IGNORECASE,
        )
        self._noise_pattern = re.compile(
            '|'.join(re.escape(k) for k in noise_kws),
            re.IGNORECASE,
        )

        # Active language adapters based on config target_extensions.
        self._adapters: dict[str, LanguageAdapter] = {}
        for ext in cfg.extractor.target_extensions:
            try:
                self._adapters[ext] = get_adapter(ext)
            except KeyError:
                logger.warning(f"No adapter for extension '{ext}'. Skipping.")

        logger.debug(
            f"CommitExtractor ready. "
            f"Keywords: {len(bug_kws)} | "
            f"Extensions: {list(self._adapters.keys())}"
        )

    # ── Public API ────────────────────────────────────────────

    def extract(
        self,
        repo_path: Path,
        repo_meta: dict,
    ) -> Iterator[BugFixPair]:
        """
        Walk commits in the bare repo and yield BugFixPair records.

        This is a generator — caller iterates and writes to storage.
        No list of pairs is held in memory.

        Args:
            repo_path: Path to the bare .git directory.
            repo_meta: Discovery metadata dict (for stars, full_name, etc.).

        Yields:
            BugFixPair records that pass all three filter layers.
        """
        repo_name = repo_meta.get("full_name", str(repo_path))
        stars = repo_meta.get("stars")

        try:
            repo = Repo(str(repo_path))
        except (InvalidGitRepositoryError, Exception) as e:
            logger.error(f"Cannot open repo {repo_name}: {e}")
            return

        total_commits = 0
        bug_commits = 0
        pairs_yielded = 0

        try:
            commits = list(repo.iter_commits(max_count=MAX_COMMITS_PER_REPO))
        except Exception as e:
            logger.error(f"Cannot iterate commits for {repo_name}: {e}")
            return

        logger.info(f"Walking {len(commits)} commits in {repo_name}")

        for commit in commits:
            total_commits += 1

            # ── Layer 1: Commit-level filter ─────────────────
            if not self._is_bug_fix_commit(commit):
                continue

            bug_commits += 1

            # ── Layer 2: Diff-level filter ───────────────────
            try:
                diffs = self._get_filtered_diffs(commit)
            except Exception as e:
                logger.debug(f"Diff error on {commit.hexsha[:8]} in {repo_name}: {e}")
                continue

            if diffs is None:
                continue  # diff-level filter rejected this commit

            # ── Layer 3: File-level extraction ───────────────
            for diff in diffs:
                pair = self._extract_pair(
                    commit=commit,
                    diff=diff,
                    repo_name=repo_name,
                    stars=stars,
                )
                if pair is not None:
                    pairs_yielded += 1
                    yield pair

        logger.info(
            f"{repo_name}: "
            f"commits={total_commits} | "
            f"bug_commits={bug_commits} | "
            f"pairs_extracted={pairs_yielded}"
        )

    # ── Layer 1: Commit filter ────────────────────────────────

    def _is_bug_fix_commit(self, commit) -> bool:
        """
        Return True if this commit looks like a genuine bug fix.

        Rules (all must pass):
        1. Not a merge commit (merge commits have 2+ parents —
           they contain no original code changes, just merging branches)
        2. Commit message contains at least one bug-fix keyword
        3. Commit message does NOT contain noise keywords
        """
        # Rule 1: reject merge commits
        if len(commit.parents) != 1:
            return False

        msg = commit.message or ""

        # Rule 2: must match a bug-fix keyword
        if not self._bug_pattern.search(msg):
            return False

        # Rule 3: must not match noise keywords
        # Noise check uses substring match (not word boundary)
        # because "merge branch" should match "merge" anywhere.
        if self._noise_pattern.search(msg):
            return False

        return True

    # ── Layer 2: Diff filter ──────────────────────────────────

    def _get_filtered_diffs(self, commit) -> Optional[list]:
        """
        Get the diff between this commit and its parent.
        Return None if the diff-level filter rejects it.
        Return filtered list of diffs otherwise.

        Filters:
        - Number of changed files <= max_files_changed
        - Total diff lines changed <= max_diff_lines
        """
        parent = commit.parents[0]

        # create_patch=True gives us line-level diff content.
        # We need it to count added/removed lines and to detect
        # formatting-only changes.
        try:
            diffs = parent.diff(commit, create_patch=True)
        except Exception:
            # Some commits have binary-only diffs or corrupt objects
            return None

        if not diffs:
            return None

        # Filter 1: total files changed
        if len(diffs) > self.cfg.extractor.max_files_changed:
            return None

        # Filter 2: total lines changed across all files
        total_lines = sum(self._count_diff_lines(d) for d in diffs)
        if total_lines > self.cfg.extractor.max_diff_lines:
            return None

        # Filter 3: only keep diffs that touch target extension files
        target_diffs = [
            d for d in diffs
            if self._is_target_file(d)
        ]

        if not target_diffs:
            return None

        return target_diffs

    def _count_diff_lines(self, diff) -> int:
        """Count total added + removed lines in a single file diff."""
        try:
            patch = diff.diff
            if isinstance(patch, bytes):
                patch = patch.decode("utf-8", errors="replace")
            added = sum(1 for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
            removed = sum(1 for line in patch.splitlines() if line.startswith("-") and not line.startswith("---"))
            return added + removed
        except Exception:
            return 0

    def _is_target_file(self, diff) -> bool:
        """Check if this diff touches a file we want to process."""
        path = diff.b_path or diff.a_path or ""
        return any(path.endswith(ext) for ext in self._adapters)

    # ── Layer 3: File-level extraction ───────────────────────

    def _extract_pair(
        self,
        commit,
        diff,
        repo_name: str,
        stars: Optional[int],
    ) -> Optional[BugFixPair]:
        """
        Extract a BugFixPair from a single file diff.
        Returns None if any file-level filter rejects it.
        """
        # Only process Modified files.
        # 'A' = Added (new file — not a fix, it's new code)
        # 'D' = Deleted (removing code — too ambiguous)
        # 'R' = Renamed (may have content changes but primarily structural)
        # 'M' = Modified — this is what we want
        change_type = diff.change_type
        if change_type != "M":
            return None

        file_path = diff.b_path  # path after the fix
        if not file_path:
            return None

        # Determine which adapter handles this file
        ext = "." + file_path.rsplit(".", 1)[-1] if "." in file_path else ""
        adapter = self._adapters.get(ext.lower())
        if adapter is None:
            return None

        # Get file content at parent (buggy) and commit (fixed)
        buggy_code = self._get_file_content(commit.parents[0], file_path, adapter)
        fixed_code = self._get_file_content(commit, file_path, adapter)

        if buggy_code is None or fixed_code is None:
            return None

        # Skip if files are identical (formatting-only commit slipped through)
        if buggy_code.strip() == fixed_code.strip():
            return None

        # Skip files that are too large (likely generated code)
        if len(buggy_code.encode("utf-8")) > MAX_FILE_SIZE_BYTES:
            return None
        if len(fixed_code.encode("utf-8")) > MAX_FILE_SIZE_BYTES:
            return None

        # Skip if the meaningful token delta is too small
        # (e.g. only a comment or whitespace changed)
        buggy_tokens = adapter.count_tokens(buggy_code)
        fixed_tokens = adapter.count_tokens(fixed_code)
        token_delta = abs(buggy_tokens - fixed_tokens)

        if token_delta < self.cfg.extractor.min_meaningful_tokens:
            return None

        # Count actual diff lines for metadata
        patch_text = diff.diff
        if isinstance(patch_text, bytes):
            patch_text = patch_text.decode("utf-8", errors="replace")

        lines_added = sum(
            1 for line in patch_text.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        lines_removed = sum(
            1 for line in patch_text.splitlines()
            if line.startswith("-") and not line.startswith("---")
        )

        # Truncate commit message to keep JSONL rows reasonable
        commit_msg = (commit.message or "").strip()[:500]

        return BugFixPair(
            repo=repo_name,
            commit_sha=commit.hexsha,
            commit_message=commit_msg,
            file_path=file_path,
            buggy_code=buggy_code,
            fixed_code=fixed_code,
            diff_lines_added=lines_added,
            diff_lines_removed=lines_removed,
            language=ext.lstrip("."),
            repo_stars=stars,
        )

    def _get_file_content(
        self,
        commit,
        file_path: str,
        adapter: LanguageAdapter,
    ) -> Optional[str]:
        """
        Retrieve file content at a specific commit from the bare repo.

        Returns None if the file doesn't exist at this commit
        (shouldn't happen for 'M' diffs, but git objects can be
        missing in shallow clones near the depth boundary).
        """
        try:
            blob = commit.tree / file_path
            raw = blob.data_stream.read()
            return adapter.decode_content(raw)
        except (KeyError, AttributeError, Exception):
            return None