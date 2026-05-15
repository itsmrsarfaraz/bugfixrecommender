"""
test_preprocessor.py — Unit tests for Preprocessor.
All tests use in-memory temp files. No real dataset needed.

Run with: pytest tests/ -v
"""

import json
import pytest
from pathlib import Path

from src.config_loader import load_config
from src.preprocessing.preprocessor import Preprocessor, MIN_TOKENS, MAX_TOKENS


@pytest.fixture
def cfg(tmp_path):
    cfg = load_config("config/config.yaml")
    cfg.storage.extracted_dir = str(tmp_path / "extracted")
    cfg.storage.processed_dir = str(tmp_path / "processed")
    Path(cfg.storage.extracted_dir).mkdir(parents=True)
    Path(cfg.storage.processed_dir).mkdir(parents=True)
    return cfg


def make_pair(
    repo="apache/kafka",
    buggy="public void run() { obj.call(); " + "x " * 30 + "}",
    fixed="public void run() { if(obj!=null) obj.call(); " + "x " * 30 + "}",
    added=1,
    removed=1,
):
    """Create a valid BugFixPair dict."""
    return {
        "pair_id": "test-id",
        "repo": repo,
        "commit_sha": "abc123",
        "commit_message": "fix: null pointer",
        "file_path": "Foo.java",
        "buggy_code": buggy,
        "fixed_code": fixed,
        "diff_lines_added": added,
        "diff_lines_removed": removed,
        "language": "java",
        "extracted_at": "2024-01-01T00:00:00+00:00",
        "repo_stars": 1000,
    }


def write_chunk(path: Path, pairs: list) -> None:
    """Write a list of pair dicts to a JSONL chunk file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for pair in pairs:
            f.write(json.dumps(pair) + "\n")


# ── Deduplication ─────────────────────────────────────────────

class TestDeduplication:

    def test_duplicate_pairs_removed(self, cfg, tmp_path):
        pair = make_pair()
        chunk = Path(cfg.storage.extracted_dir) / "extracted_0000.jsonl"
        # Write same pair twice
        write_chunk(chunk, [pair, pair])

        pre = Preprocessor(cfg)
        stats = pre.run()

        assert stats["dropped_duplicate"] == 1
        assert stats["clean_total"] == 1

    def test_different_pairs_both_kept(self, cfg, tmp_path):
        p1 = make_pair(buggy="public void a() { " + "x " * 30 + "}")
        p2 = make_pair(buggy="public void b() { " + "y " * 30 + "}")
        chunk = Path(cfg.storage.extracted_dir) / "extracted_0000.jsonl"
        write_chunk(chunk, [p1, p2])

        pre = Preprocessor(cfg)
        stats = pre.run()

        assert stats["dropped_duplicate"] == 0
        assert stats["clean_total"] == 2


# ── Quality filters ───────────────────────────────────────────

class TestQualityFilters:

    def test_identical_buggy_fixed_dropped(self, cfg):
        pair = make_pair(buggy="public void x() {}", fixed="public void x() {}")
        chunk = Path(cfg.storage.extracted_dir) / "extracted_0000.jsonl"
        write_chunk(chunk, [pair])

        stats = Preprocessor(cfg).run()
        assert stats["dropped_identical"] == 1

    def test_too_short_dropped(self, cfg):
        # Only 3 tokens — below MIN_TOKENS (20)
        pair = make_pair(buggy="int x = 1;", fixed="int x = 2;")
        chunk = Path(cfg.storage.extracted_dir) / "extracted_0000.jsonl"
        write_chunk(chunk, [pair])

        stats = Preprocessor(cfg).run()
        assert stats["dropped_too_short"] == 1

    def test_too_long_dropped(self, cfg):
        # 2001 tokens — above MAX_TOKENS (2000)
        long_code = "x " * 2001
        pair = make_pair(buggy=long_code, fixed=long_code + " extra")
        chunk = Path(cfg.storage.extracted_dir) / "extracted_0000.jsonl"
        write_chunk(chunk, [pair])

        stats = Preprocessor(cfg).run()
        assert stats["dropped_too_long"] == 1

    def test_zero_diff_dropped(self, cfg):
        pair = make_pair(added=0, removed=0)
        chunk = Path(cfg.storage.extracted_dir) / "extracted_0000.jsonl"
        write_chunk(chunk, [pair])

        stats = Preprocessor(cfg).run()
        assert stats["dropped_bad_diff"] == 1

    def test_valid_pair_passes_all_filters(self, cfg):
        pair = make_pair()
        chunk = Path(cfg.storage.extracted_dir) / "extracted_0000.jsonl"
        write_chunk(chunk, [pair])

        stats = Preprocessor(cfg).run()
        assert stats["clean_total"] == 1
        assert stats["dropped_total"] == 0


# ── Repo-level splitting ──────────────────────────────────────

class TestRepoSplitting:

    def test_same_repo_goes_to_same_split(self, cfg):
        """All pairs from one repo must land in the same split."""
        pairs = [make_pair(repo="apache/kafka") for _ in range(5)]
        chunk = Path(cfg.storage.extracted_dir) / "extracted_0000.jsonl"
        write_chunk(chunk, pairs)

        Preprocessor(cfg).run()

        # Count how many splits contain apache/kafka pairs
        splits_with_repo = 0
        for split_name in ["train", "val", "test"]:
            split_file = Path(cfg.storage.processed_dir) / f"{split_name}.jsonl"
            if split_file.exists():
                lines = split_file.read_text(encoding="utf-8").strip().splitlines()
                if any(
                    json.loads(l).get("repo") == "apache/kafka"
                    for l in lines if l.strip()
                ):
                    splits_with_repo += 1

        # All 5 pairs from one repo must go to exactly ONE split
        assert splits_with_repo == 1

    def test_multiple_repos_distributed(self, cfg):
        """With enough repos, all three splits should be populated."""
        pairs = []
        # Create 20 different repos with valid pairs each
        for i in range(20):
            pairs.append(make_pair(
                repo=f"org/repo{i:02d}",
                buggy=f"public void method{i}() {{ " + "x " * 30 + "}",
                fixed=f"public void method{i}() {{ return null; " + "x " * 30 + "}}",
            ))
        chunk = Path(cfg.storage.extracted_dir) / "extracted_0000.jsonl"
        write_chunk(chunk, pairs)

        stats = Preprocessor(cfg).run()

        assert stats["train_pairs"] > 0
        assert stats["val_pairs"] > 0
        assert stats["test_pairs"] > 0


# ── Output files ──────────────────────────────────────────────

class TestOutputFiles:

    def test_split_files_created(self, cfg):
        pair = make_pair()
        chunk = Path(cfg.storage.extracted_dir) / "extracted_0000.jsonl"
        write_chunk(chunk, [pair])

        Preprocessor(cfg).run()

        assert (Path(cfg.storage.processed_dir) / "train.jsonl").exists()
        assert (Path(cfg.storage.processed_dir) / "val.jsonl").exists()
        assert (Path(cfg.storage.processed_dir) / "test.jsonl").exists()
        assert (Path(cfg.storage.processed_dir) / "dataset_stats.json").exists()

    def test_stats_json_is_valid(self, cfg):
        pair = make_pair()
        chunk = Path(cfg.storage.extracted_dir) / "extracted_0000.jsonl"
        write_chunk(chunk, [pair])

        Preprocessor(cfg).run()

        stats = json.loads(
            (Path(cfg.storage.processed_dir) / "dataset_stats.json")
            .read_text(encoding="utf-8")
        )
        assert "train_pairs" in stats
        assert "val_pairs" in stats
        assert "test_pairs" in stats
        assert "dropped_total" in stats
        assert "clean_total" in stats

    def test_output_records_are_valid_json(self, cfg):
        pairs = [make_pair(repo=f"org/r{i}", buggy="x " * 30, fixed="y " * 30)
                 for i in range(5)]
        chunk = Path(cfg.storage.extracted_dir) / "extracted_0000.jsonl"
        write_chunk(chunk, pairs)

        Preprocessor(cfg).run()

        for split_name in ["train", "val", "test"]:
            f = Path(cfg.storage.processed_dir) / f"{split_name}.jsonl"
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    assert "buggy_code" in record
                    assert "fixed_code" in record
                    assert "repo" in record