"""
bm25_engine.py — BM25 retrieval engine. This IS the V1 model.

WHY BM25 (not a neural model):
- Zero training required — index is built from the dataset directly.
- Sub-50ms query time on 11K pairs. Fits comfortably in 16GB RAM.
- Cannot hallucinate: every returned fix exists verbatim in the dataset.
- Tunable without retraining: adjust k1/b hyperparameters in config.

HOW it works:
1. INDEX: tokenize every buggy_code in train.jsonl → build BM25 inverted index.
2. QUERY: tokenize incoming buggy snippet → score against index → return top-K pairs.

The index is saved to disk as a pickle file so indexing only runs once.
Subsequent server starts load from disk in <2 seconds.
"""

import json
import pickle
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from rank_bm25 import BM25Okapi

from src.utils.logger import get_logger

logger = get_logger(__name__)

# Default BM25 hyperparameters.
# k1: term frequency saturation. Higher = more weight to repeated terms.
#     Range: 1.2–2.0. Start at 1.5 for code (more repetitive than prose).
# b:  length normalization. 0 = no normalization, 1 = full normalization.
#     Code files vary wildly in length so 0.75 is a reasonable start.
DEFAULT_K1 = 1.5
DEFAULT_B  = 0.75

# Index filename
INDEX_FILENAME = "bm25_index.pkl"


@dataclass
class BugFixResult:
    """
    One retrieval result returned to the caller.

    rank:           1-based position in results (1 = best match).
    score:          BM25 relevance score (higher = more relevant).
    buggy_code:     The original buggy code from the training pair.
    fixed_code:     The fix that was applied — this is the recommendation.
    commit_message: Commit message explaining what was fixed.
    repo:           Source repository (e.g. 'apache/kafka').
    file_path:      File path within the repo.
    pair_id:        Unique ID of this training pair.
    """
    rank: int
    score: float
    buggy_code: str
    fixed_code: str
    commit_message: str
    repo: str
    file_path: str
    pair_id: str

    def to_dict(self) -> dict:
        return {
            "rank": self.rank,
            "score": round(self.score, 4),
            "buggy_code": self.buggy_code,
            "fixed_code": self.fixed_code,
            "commit_message": self.commit_message,
            "repo": self.repo,
            "file_path": self.file_path,
            "pair_id": self.pair_id,
        }


class BM25Engine:
    """
    Build, persist, and query a BM25 index over bug-fix training pairs.

    Usage — indexing (run once):
        engine = BM25Engine(index_dir="checkpoints")
        engine.build_index("data/processed/train.jsonl")

    Usage — querying (at inference time):
        engine = BM25Engine(index_dir="checkpoints")
        engine.load_index()
        results = engine.query("public void run() { obj.call(); }", top_k=5)
    """

    def __init__(
        self,
        index_dir: str = "checkpoints",
        k1: float = DEFAULT_K1,
        b: float = DEFAULT_B,
    ) -> None:
        self.index_path = Path(index_dir) / INDEX_FILENAME
        self.k1 = k1
        self.b  = b

        # These are populated after build_index() or load_index()
        self._bm25: Optional[BM25Okapi] = None
        self._pairs: List[dict] = []       # parallel list to BM25 corpus
        self._is_loaded = False

    # ── Public API ────────────────────────────────────────────

    def build_index(self, train_jsonl: str, max_pairs: Optional[int] = None) -> None:  
        """
        Tokenize all buggy_code entries in train.jsonl and build the index.
        Saves the index to disk when done.

        Args:
            train_jsonl: Path to data/processed/train.jsonl
        """
        path = Path(train_jsonl)
        if not path.exists():
            raise FileNotFoundError(
                f"Training file not found: {path}\n"
                f"Run preprocessing first: python main.py --step preprocess"
            )

        logger.info(f"Building BM25 index from: {train_jsonl}")
        start = time.perf_counter()

        corpus: List[List[str]] = []
        self._pairs = []

        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(f"Skipping malformed JSON at line {line_num}")
                    continue

                buggy_code = record.get("buggy_code", "")
                if not buggy_code:
                    continue

                if max_pairs and len(self._pairs) >= max_pairs:
                    break

                tokens = self._tokenize(buggy_code)
                if not tokens:
                    continue

                corpus.append(tokens)
                # Store minimal metadata — we don't need to hold full
                # file contents in the index. Load from JSONL on retrieval.
                self._pairs.append({
                    "pair_id":        record.get("pair_id", ""),
                    "repo":           record.get("repo", ""),
                    "file_path":      record.get("file_path", ""),
                    "commit_message": record.get("commit_message", "")[:200],
                    "buggy_code":     buggy_code,
                    "fixed_code":     record.get("fixed_code", ""),
                })

                if line_num % 1000 == 0:
                    logger.info(f"Tokenized {line_num} pairs...")

        if not corpus:
            raise ValueError("No valid pairs found in training file. Check preprocessing.")

        logger.info(f"Building BM25Okapi index over {len(corpus)} pairs (k1={self.k1}, b={self.b})...")
        self._bm25 = BM25Okapi(corpus, k1=self.k1, b=self.b)
        self._is_loaded = True

        elapsed = time.perf_counter() - start
        logger.info(f"Index built in {elapsed:.1f}s. Saving to: {self.index_path}")
        self._save_index()
        logger.info(
            f"Index ready. {len(self._pairs)} pairs indexed. "
            f"File size: {self.index_path.stat().st_size / 1024 / 1024:.1f} MB"
        )

    def load_index(self) -> None:
        """
        Load a previously built index from disk.
        Call this at server startup before serving queries.
        """
        if not self.index_path.exists():
            raise FileNotFoundError(
                f"No index found at: {self.index_path}\n"
                f"Build it first: python main.py --step index"
            )

        logger.info(f"Loading BM25 index from: {self.index_path}")
        start = time.perf_counter()

        with open(self.index_path, "rb") as f:
            data = pickle.load(f)

        self._bm25  = data["bm25"]
        self._pairs = data["pairs"]
        self.k1     = data.get("k1", DEFAULT_K1)
        self.b      = data.get("b",  DEFAULT_B)
        self._is_loaded = True

        elapsed = time.perf_counter() - start
        logger.info(
            f"Index loaded in {elapsed:.2f}s. "
            f"{len(self._pairs)} pairs available."
        )

    def query(
        self,
        buggy_code: str,
        top_k: int = 5,
    ) -> List[BugFixResult]:
        """
        Find the top-K most similar bug-fix pairs for a given buggy snippet.

        Args:
            buggy_code: The buggy code to find fixes for.
            top_k:      Number of results to return (default: 5).

        Returns:
            List of BugFixResult ordered by relevance (best first).
        """
        if not self._is_loaded:
            raise RuntimeError(
                "Index not loaded. Call load_index() or build_index() first."
            )

        if not buggy_code or not buggy_code.strip():
            logger.warning("Empty query received. Returning no results.")
            return []

        # Truncate very large files before tokenising.
        # WHY: a 200KB Java file produces 50K+ tokens → BM25 scoring
        # takes 4000ms+. First 4000 chars contains the class signature
        # and key method bodies — sufficient for BM25 term matching.
        # This brings query time from ~4000ms to <100ms.
        _MAX_QUERY_CHARS = 4000
        if len(buggy_code) > _MAX_QUERY_CHARS:
            buggy_code = buggy_code[:_MAX_QUERY_CHARS]

        start = time.perf_counter()

        query_tokens = self._tokenize(buggy_code)
        if not query_tokens:
            logger.warning("Query tokenized to empty list. Returning no results.")
            return []

        # BM25 scores every document in the corpus against the query.
        # This is a numpy dot product — fast even for 11K documents.
        scores = self._bm25.get_scores(query_tokens) # type: ignore

        # Get indices of top-K scores (descending)
        # argsort gives ascending, so we reverse with [::-1]
        import numpy as np
        top_indices = np.argsort(scores)[::-1][:top_k]

        results = []
        for rank, idx in enumerate(top_indices, start=1):
            score = float(scores[idx])
            if score <= 0:
                # BM25 score of 0 means no term overlap — not useful
                break

            pair = self._pairs[idx]
            results.append(BugFixResult(
                rank=rank,
                score=score,
                buggy_code=pair["buggy_code"],
                fixed_code=pair["fixed_code"],
                commit_message=pair["commit_message"],
                repo=pair["repo"],
                file_path=pair["file_path"],
                pair_id=pair["pair_id"],
            ))

        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.debug(
            f"Query returned {len(results)} results in {elapsed_ms:.1f}ms. "
            f"Top score: {results[0].score:.3f}" if results else
            f"Query returned 0 results in {elapsed_ms:.1f}ms."
        )

        return results

    def is_ready(self) -> bool:
        """Return True if the index is loaded and ready to serve queries."""
        return self._is_loaded and self._bm25 is not None

    def stats(self) -> dict:
        """Return index statistics for health checks and logging."""
        if not self._is_loaded:
            return {"loaded": False}
        return {
            "loaded": True,
            "pairs_indexed": len(self._pairs),
            "index_file": str(self.index_path),
            "index_size_mb": (
                round(self.index_path.stat().st_size / 1024 / 1024, 2)
                if self.index_path.exists() else 0
            ),
            "k1": self.k1,
            "b": self.b,
        }

    # ── Private helpers ───────────────────────────────────────

    @staticmethod
    def _tokenize(code: str) -> List[str]:
        """
        Tokenize Java code for BM25 indexing.

        Strategy:
        - Split on non-alphanumeric characters (covers Java operators,
          brackets, semicolons, whitespace).
        - Lowercase for case-insensitive matching.
        - Filter tokens shorter than 2 characters — single chars like
          '{', '}', ';' are noise that inflate document frequency.
        - No stemming — Java identifiers (getUser, setUser) are already
          precise enough without stemming.

        WHY not use a Java parser/AST tokenizer:
        - AST parsing requires a full Java compiler on the inference machine.
        - For BM25, keyword overlap is the signal — not AST structure.
        - Simple regex tokenization is 100x faster and good enough for V1.
        """
        tokens = re.findall(r'[a-zA-Z0-9_]+', code.lower())
        return [t for t in tokens if len(t) >= 2]

    def _save_index(self) -> None:
        """Pickle the BM25 index and pair metadata to disk."""
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.index_path.with_suffix(".tmp")

        with open(tmp_path, "wb") as f:
            pickle.dump({
                "bm25":  self._bm25,
                "pairs": self._pairs,
                "k1":    self.k1,
                "b":     self.b,
            }, f, protocol=pickle.HIGHEST_PROTOCOL)

        # Atomic rename — protects against partial writes
        tmp_path.replace(self.index_path)