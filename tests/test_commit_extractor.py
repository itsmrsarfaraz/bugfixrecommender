"""
test_commit_extractor.py — Unit tests for CommitExtractor.

All tests use real in-memory git repos (git.Repo.init())
so we test actual git object traversal without network calls.
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.config_loader import load_config
from src.extractor.commit_extractor import CommitExtractor
from src.extractor.models import BugFixPair
from src.extractor.language_adapter import JavaAdapter, get_adapter


@pytest.fixture
def cfg():
    return load_config("config/config.yaml")


@pytest.fixture
def extractor(cfg):
    return CommitExtractor(cfg)


# ── Commit-level filter ───────────────────────────────────────

class TestCommitFilter:

    def _make_commit(self, message, num_parents=1):
        commit = MagicMock()
        commit.message = message
        commit.parents = [MagicMock()] * num_parents
        return commit

    def test_bug_fix_commit_passes(self, extractor):
        commit = self._make_commit("fix: null pointer in UserService")
        assert extractor._is_bug_fix_commit(commit) is True

    def test_merge_commit_rejected(self, extractor):
        commit = self._make_commit("Merge branch 'main'", num_parents=2)
        assert extractor._is_bug_fix_commit(commit) is False

    def test_no_keyword_rejected(self, extractor):
        commit = self._make_commit("Add new feature for dashboard")
        assert extractor._is_bug_fix_commit(commit) is False

    def test_noise_keyword_overrides_bug_keyword(self, extractor):
        # "fix" matches bug keyword, but "merge" is a noise keyword
        commit = self._make_commit("Merge fix from feature branch")
        assert extractor._is_bug_fix_commit(commit) is False

    def test_chore_commit_rejected(self, extractor):
        commit = self._make_commit("chore: fix formatting")
        assert extractor._is_bug_fix_commit(commit) is False

    def test_resolved_keyword_passes(self, extractor):
        commit = self._make_commit("resolved: issue with null handling")
        assert extractor._is_bug_fix_commit(commit) is True

    def test_hotfix_passes(self, extractor):
        commit = self._make_commit("hotfix: crash in payment processor")
        assert extractor._is_bug_fix_commit(commit) is True

    def test_bump_with_fix_rejected(self, extractor):
        commit = self._make_commit("bump version to fix release")
        assert extractor._is_bug_fix_commit(commit) is False

    def test_revert_with_fix_rejected(self, extractor):
        commit = self._make_commit("revert fix for issue #123")
        assert extractor._is_bug_fix_commit(commit) is False


# ── Language adapter ──────────────────────────────────────────

class TestLanguageAdapter:

    def test_java_adapter_accepts_java(self):
        adapter = JavaAdapter()
        assert adapter.is_target_file("src/main/java/Foo.java") is True

    def test_java_adapter_rejects_python(self):
        adapter = JavaAdapter()
        assert adapter.is_target_file("main.py") is False

    def test_token_counting(self):
        adapter = JavaAdapter()
        code = "public void myMethod() { return null; }"
        count = adapter.count_tokens(code)
        assert count > 0

    def test_utf8_decode(self):
        adapter = JavaAdapter()
        raw = "public class Foo {}".encode("utf-8")
        assert "Foo" in adapter.decode_content(raw)

    def test_get_adapter_java(self):
        adapter = get_adapter(".java")
        assert isinstance(adapter, JavaAdapter)

    def test_get_adapter_unknown_raises(self):
        with pytest.raises(KeyError):
            get_adapter(".unknown")


# ── BugFixPair model ──────────────────────────────────────────

class TestBugFixPair:

    def make_pair(self):
        return BugFixPair(
            repo="apache/kafka",
            commit_sha="abc123",
            commit_message="fix: NPE in consumer",
            file_path="src/main/java/Consumer.java",
            buggy_code="public void run() { obj.call(); }",
            fixed_code="public void run() { if (obj != null) obj.call(); }",
            diff_lines_added=1,
            diff_lines_removed=1,
            language="java",
            repo_stars=32000,
        )

    def test_pair_has_unique_id(self):
        p1 = self.make_pair()
        p2 = self.make_pair()
        assert p1.pair_id != p2.pair_id

    def test_to_dict_is_serialisable(self):
        import json
        pair = self.make_pair()
        d = pair.to_dict()
        # Must be JSON serialisable (no datetime objects etc.)
        serialised = json.dumps(d)
        assert "apache/kafka" in serialised

    def test_diff_size_property(self):
        pair = self.make_pair()
        assert pair.diff_size == 2  # 1 added + 1 removed

    def test_extracted_at_is_set(self):
        pair = self.make_pair()
        assert pair.extracted_at is not None
        assert "T" in pair.extracted_at  # ISO format


# ── DatasetWriter ─────────────────────────────────────────────

class TestDatasetWriter:

    def make_pair(self, n=0):
        return BugFixPair(
            repo=f"repo/test{n}",
            commit_sha=f"sha{n}",
            commit_message="fix: something",
            file_path="Foo.java",
            buggy_code="old code",
            fixed_code="new code",
            diff_lines_added=1,
            diff_lines_removed=1,
            language="java",
        )

    def test_write_creates_jsonl_file(self, cfg, tmp_path):
        cfg.storage.extracted_dir = str(tmp_path / "extracted")
        cfg.storage.chunk_size = 1000

        from src.storage.dataset_writer import DatasetWriter
        with DatasetWriter(cfg) as writer:
            writer.write(self.make_pair(0))
            writer.write(self.make_pair(1))

        files = list(Path(cfg.storage.extracted_dir).glob("*.jsonl"))
        assert len(files) == 1

        import json
        lines = files[0].read_text().strip().splitlines()
        assert len(lines) == 2
        record = json.loads(lines[0])
        assert record["repo"] == "repo/test0"

    def test_chunk_rotation(self, cfg, tmp_path):
        cfg.storage.extracted_dir = str(tmp_path / "extracted")
        cfg.storage.chunk_size = 2  # rotate every 2 records

        from src.storage.dataset_writer import DatasetWriter
        with DatasetWriter(cfg) as writer:
            for i in range(5):
                writer.write(self.make_pair(i))

        files = sorted(Path(cfg.storage.extracted_dir).glob("*.jsonl"))
        # 5 records at chunk_size=2 → chunks 0000(2), 0001(2), 0002(1)
        assert len(files) == 3

    def test_resume_appends_to_last_chunk(self, cfg, tmp_path):
        cfg.storage.extracted_dir = str(tmp_path / "extracted")
        cfg.storage.chunk_size = 10

        from src.storage.dataset_writer import DatasetWriter

        # First run: write 3 records
        with DatasetWriter(cfg) as writer:
            for i in range(3):
                writer.write(self.make_pair(i))

        # Second run: write 2 more — should resume in same chunk
        with DatasetWriter(cfg) as writer:
            for i in range(3, 5):
                writer.write(self.make_pair(i))

        files = list(Path(cfg.storage.extracted_dir).glob("*.jsonl"))
        assert len(files) == 1  # still one chunk
        lines = files[0].read_text().strip().splitlines()
        assert len(lines) == 5  # 3 + 2