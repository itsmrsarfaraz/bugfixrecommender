"""
preprocessor.py — Clean, deduplicate, and split the raw extracted dataset.

WHY this step is critical:
- Raw extraction has no deduplication. The same buggy/fixed pair can appear
  if two commits touch the same file in similar ways.
- Some "pairs" are noise: tiny diffs, whitespace-only changes that slipped
  through the token_delta filter, or encoding artifacts.
- Train/val/test MUST be split by repository, not by record.
  Splitting by record causes leakage: train and test may contain pairs from
  the same codebase, making the model appear better than it is on real code.

OUTPUT:
  data/processed/train.jsonl
  data/processed/val.jsonl
  data/processed/test.jsonl
  data/processed/dataset_stats.json
"""

import hashlib
import json
import random
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterator, List, Dict, Tuple

from src.config_loader import Config
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Quality thresholds — these complement the extractor's filters.
# The extractor uses token_delta (change size).
# We use absolute token counts to remove trivially small or huge files.
MIN_TOKENS = 20        # buggy or fixed file with <20 tokens = useless
MAX_TOKENS = 2000      # files > 2000 tokens exceed BM25's sweet spot

# Split ratios — must sum to 1.0
TRAIN_RATIO = 0.70
VAL_RATIO   = 0.15
TEST_RATIO  = 0.15

# Random seed for reproducible splits
SPLIT_SEED = 42


class Preprocessor:
    """
    Reads raw JSONL chunks, filters, deduplicates, and writes
    train/val/test splits.

    Memory design:
    - Streams records one at a time — never loads all 8K+ records into RAM.
    - Only the dedup hash set grows in memory (~50 bytes per pair × 8K = ~400KB).
    - Split assignment is done at the REPO level, not the record level.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.input_dir = Path(cfg.storage.extracted_dir)
        self.output_dir = Path(cfg.storage.processed_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ── Public API ────────────────────────────────────────────

    def run(self) -> dict:
        """
        Execute the full preprocessing pipeline.

        Returns:
            Stats dict summarising the final dataset.
        """
        logger.info("=== Preprocessing Pipeline ===")

        # ── Step 1: Discover all repos in the raw data ───────
        # We must know the full repo list before splitting,
        # so we do one lightweight pass to collect repo names only.
        logger.info("Step 1: Scanning repos in raw data...")
        repos = self._scan_repos()
        logger.info(f"Found {len(repos)} unique repos across raw data.")

        # ── Step 2: Assign each repo to a split ──────────────
        repo_split = self._assign_repo_splits(repos)
        split_counts = defaultdict(int)
        for split in repo_split.values():
            split_counts[split] += 1
        logger.info(
            f"Repo split: train={split_counts['train']} | "
            f"val={split_counts['val']} | "
            f"test={split_counts['test']}"
        )

        # ── Step 3: Stream records → filter → write ──────────
        logger.info("Step 2: Filtering and writing splits...")
        stats = self._process_and_write(repo_split)

        # ── Step 4: Write stats report ───────────────────────
        self._write_stats(stats, repo_split)
        logger.info(
            f"Preprocessing complete. "
            f"Train: {stats['train_pairs']} | "
            f"Val: {stats['val_pairs']} | "
            f"Test: {stats['test_pairs']} | "
            f"Dropped: {stats['dropped_total']}"
        )
        return stats

    # ── Step 1: Repo scanning ─────────────────────────────────

    def _scan_repos(self) -> List[str]:
        """
        Lightweight first pass: collect unique repo names.
        Reads only the 'repo' field from each line.
        """
        repos = set()
        for chunk_file in sorted(self.input_dir.glob("extracted_*.jsonl")):
            with open(chunk_file, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        # Fast parse: only extract 'repo' field
                        # (avoids full JSON parse for speed)
                        record = json.loads(line)
                        repo = record.get("repo", "")
                        if repo:
                            repos.add(repo)
                    except json.JSONDecodeError:
                        continue
        return sorted(repos)  # sorted for reproducibility

    # ── Step 2: Repo-level split assignment ───────────────────

    def _assign_repo_splits(self, repos: List[str]) -> Dict[str, str]:
        """
        Assign each repo to train, val, or test.

        WHY repo-level splitting (not record-level):
        If we split by record, train and test contain pairs from the same
        repo. BM25 retrieval on test would find training pairs from the
        same codebase — not because the model generalises, but because it
        memorised the repo's patterns. Split by repo prevents this.

        We use stratified shuffling so the split ratios are respected
        approximately even with small repo counts.
        """
        rng = random.Random(SPLIT_SEED)
        shuffled = repos[:]
        rng.shuffle(shuffled)

        n = len(shuffled)
        train_end = int(n * TRAIN_RATIO)
        val_end   = train_end + int(n * VAL_RATIO)

        assignment = {}
        for i, repo in enumerate(shuffled):
            if i < train_end:
                assignment[repo] = "train"
            elif i < val_end:
                assignment[repo] = "val"
            else:
                assignment[repo] = "test"

        return assignment

    # ── Step 3: Filter + write ────────────────────────────────

    def _process_and_write(self, repo_split: Dict[str, str]) -> dict:
        """
        Stream all raw pairs, apply quality filters, deduplicate,
        and write to split files.
        """
        output_files = {
            "train": open(self.output_dir / "train.jsonl", "w", encoding="utf-8"),
            "val":   open(self.output_dir / "val.jsonl",   "w", encoding="utf-8"),
            "test":  open(self.output_dir / "test.jsonl",  "w", encoding="utf-8"),
        }

        # Deduplication set — content hash of (buggy_code, fixed_code)
        seen_hashes: set = set()

        stats = {
            "raw_total": 0,
            "train_pairs": 0,
            "val_pairs": 0,
            "test_pairs": 0,
            "dropped_duplicate": 0,
            "dropped_too_short": 0,
            "dropped_too_long": 0,
            "dropped_identical": 0,
            "dropped_bad_diff": 0,
            "dropped_unknown_repo": 0,
            "dropped_total": 0,
        }

        try:
            for record in self._stream_raw():
                stats["raw_total"] += 1

                # ── Filter 1: repo must be in our split map ───
                repo = record.get("repo", "")
                split = repo_split.get(repo)
                if split is None:
                    stats["dropped_unknown_repo"] += 1
                    stats["dropped_total"] += 1
                    continue

                # ── Filter 2: identical buggy/fixed ──────────
                buggy = record.get("buggy_code", "")
                fixed = record.get("fixed_code", "")
                if buggy.strip() == fixed.strip():
                    stats["dropped_identical"] += 1
                    stats["dropped_total"] += 1
                    continue

                # ── Filter 3: token count bounds ─────────────
                buggy_tokens = self._count_tokens(buggy)
                fixed_tokens = self._count_tokens(fixed)

                if buggy_tokens < MIN_TOKENS or fixed_tokens < MIN_TOKENS:
                    stats["dropped_too_short"] += 1
                    stats["dropped_total"] += 1
                    continue

                if buggy_tokens > MAX_TOKENS or fixed_tokens > MAX_TOKENS:
                    stats["dropped_too_long"] += 1
                    stats["dropped_total"] += 1
                    continue

                # ── Filter 4: diff sanity check ───────────────
                added   = record.get("diff_lines_added", 0)
                removed = record.get("diff_lines_removed", 0)
                diff_size = added + removed
                if diff_size < 1 or diff_size > 150:
                    stats["dropped_bad_diff"] += 1
                    stats["dropped_total"] += 1
                    continue

                # ── Filter 5: deduplication ───────────────────
                content_hash = hashlib.md5(
                    (buggy.strip() + "|||" + fixed.strip()).encode("utf-8"),
                    usedforsecurity=False,
                ).hexdigest()

                if content_hash in seen_hashes:
                    stats["dropped_duplicate"] += 1
                    stats["dropped_total"] += 1
                    continue

                seen_hashes.add(content_hash)

                # ── Write to correct split ────────────────────
                line = json.dumps(record, ensure_ascii=False)
                output_files[split].write(line + "\n")
                stats[f"{split}_pairs"] += 1

        finally:
            for f in output_files.values():
                f.flush()
                f.close()

        stats["clean_total"] = (
            stats["train_pairs"] + stats["val_pairs"] + stats["test_pairs"]
        )
        return stats

    # ── Step 4: Stats report ──────────────────────────────────

    def _write_stats(self, stats: dict, repo_split: Dict[str, str]) -> None:
        """Write a JSON stats report for inspection."""
        # Count repos per split
        repos_per_split = defaultdict(list)
        for repo, split in repo_split.items():
            repos_per_split[split].append(repo)

        report = {
            **stats,
            "split_ratios": {
                "train": TRAIN_RATIO,
                "val": VAL_RATIO,
                "test": TEST_RATIO,
            },
            "repos_per_split": {
                "train": len(repos_per_split["train"]),
                "val": len(repos_per_split["val"]),
                "test": len(repos_per_split["test"]),
            },
            "min_tokens_threshold": MIN_TOKENS,
            "max_tokens_threshold": MAX_TOKENS,
            "split_seed": SPLIT_SEED,
        }

        stats_path = self.output_dir / "dataset_stats.json"
        with open(stats_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)

        logger.info(f"Stats written to: {stats_path}")

    # ── Helpers ───────────────────────────────────────────────

    def _stream_raw(self) -> Iterator[dict]:
        """
        Generator: yield one parsed record at a time from all chunk files.
        Memory-safe — only one line is in RAM at a time.
        """
        chunk_files = sorted(self.input_dir.glob("extracted_*.jsonl"))
        logger.info(f"Reading {len(chunk_files)} chunk files...")

        for chunk_file in chunk_files:
            with open(chunk_file, "r", encoding="utf-8", errors="ignore") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError as e:
                        logger.warning(
                            f"Bad JSON in {chunk_file.name} line {line_num}: {e}"
                        )
                        continue

    @staticmethod
    def _count_tokens(code: str) -> int:
        """Fast word-token count. Consistent with language adapter."""
        return len(re.findall(r'\w+', code))