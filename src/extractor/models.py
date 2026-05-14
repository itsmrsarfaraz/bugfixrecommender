"""
models.py — Data models for extracted bug-fix pairs.

WHY a dedicated models file:
- Single definition of the output schema.
- Both extractor and storage import from here — no duplication.
- Easy to extend fields without hunting through multiple files.
- dataclass = lightweight, no ORM overhead, JSON-serialisable.
"""

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional


@dataclass
class BugFixPair:
    """
    One extracted bug-fix training example.

    Fields:
        pair_id:          Unique ID for this record. Used for dedup.
        repo:             full_name from GitHub (owner/repo).
        commit_sha:       Full SHA of the fix commit.
        commit_message:   Full commit message (truncated to 500 chars).
        file_path:        Path of the changed file within the repo.
        buggy_code:       Full file content BEFORE the fix.
        fixed_code:       Full file content AFTER the fix.
        diff_lines_added: Number of lines added in this file's diff.
        diff_lines_removed: Number of lines removed in this file's diff.
        language:         File language (e.g. "java").
        extracted_at:     ISO timestamp of extraction.
        repo_stars:       Stars at discovery time (dataset quality signal).

    WHY full file content (not just diff):
        BM25 retrieval needs the full code context to find similar bugs.
        If we only store diff lines, we can't match the surrounding code.
        Disk is cheap. Context is valuable.
    """

    repo: str
    commit_sha: str
    commit_message: str
    file_path: str
    buggy_code: str
    fixed_code: str
    diff_lines_added: int
    diff_lines_removed: int
    language: str

    # Optional fields with defaults
    pair_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    extracted_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    repo_stars: Optional[int] = None

    def to_dict(self) -> dict:
        """Convert to plain dict for JSON serialisation."""
        return asdict(self)

    @property
    def diff_size(self) -> int:
        """Total lines changed — useful for filtering after extraction."""
        return self.diff_lines_added + self.diff_lines_removed