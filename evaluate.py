"""
evaluate.py — Measure retrieval quality on the held-out test set.

Metrics:
  Hit@K:  Does the correct fix appear in the top-K results?
          The "correct fix" is matched by pair_id — exact retrieval.
          This is a strict lower bound on real usefulness, because
          semantically similar fixes from other pairs also help users.

  MRR:    Mean Reciprocal Rank. Average of 1/rank for each hit.
          MRR=1.0 means always top result. MRR=0.2 means avg rank ~5.

WHY pair_id matching (not edit similarity):
  Edit similarity between two Java files is expensive to compute
  and noisy — small whitespace differences kill the score.
  For V1 evaluation, we check if the BM25 engine retrieves the
  exact pair it was trained on. This is conservative but clean.

WHY test set is valid:
  Test repos were never seen during indexing (train.jsonl only).
  So a Hit@1 means the engine found the right fix pattern from
  a completely different codebase — genuine generalisation.

Run:
  python evaluate.py
  python evaluate.py --top-k 10
  python evaluate.py --sample 200   (quick smoke test on 200 pairs)
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import List, Dict, Optional

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent))

from src.config_loader import load_config
from src.retrieval.bm25_engine import BM25Engine
from src.utils.logger import setup_logger, get_logger

logger = get_logger(__name__)


def parse_args():
    p = argparse.ArgumentParser(description="Evaluate BM25 retrieval on test set")
    p.add_argument("--top-k",  type=int, default=5,    help="Max K for Hit@K (default: 5)")
    p.add_argument("--sample", type=int, default=None, help="Evaluate on first N test pairs (default: all)")
    p.add_argument("--output", type=str, default="data/processed/eval_results.json", help="Where to save results")
    return p.parse_args()


def load_test_pairs(test_path: str, sample: Optional[int] = None) -> List[dict]:
    """Stream test.jsonl and return pairs (optionally truncated)."""
    path = Path(test_path)
    if not path.exists():
        raise FileNotFoundError(f"Test file not found: {path}")

    pairs = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                pairs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if sample and len(pairs) >= sample:
                break

    return pairs


def evaluate(
    engine: BM25Engine,
    test_pairs: List[dict],
    top_k: int = 5,
) -> Dict:
    """
    Run evaluation loop.

    For each test pair:
      1. Query BM25 with the buggy_code.
      2. Check if the correct pair_id appears in results.
      3. Record rank if found, else mark as miss.

    Returns a dict of all metrics.
    """
    hits_at_k   = {k: 0 for k in range(1, top_k + 1)}
    reciprocals = []   # for MRR
    query_times = []
    zero_result_count = 0
    total = len(test_pairs)

    logger.info(f"Evaluating {total} test pairs with top_k={top_k}...")

    for i, pair in enumerate(test_pairs, 1):
        if i % 100 == 0:
            logger.info(f"Progress: {i}/{total}")

        pair_id    = pair.get("pair_id", "")
        buggy_code = pair.get("buggy_code", "")

        if not buggy_code:
            continue

        start = time.perf_counter()
        results = engine.query(buggy_code, top_k=top_k)
        elapsed_ms = (time.perf_counter() - start) * 1000
        query_times.append(elapsed_ms)

        if not results:
            zero_result_count += 1
            reciprocals.append(0.0)
            continue

        # Check if the correct pair_id appears in results
        found_at_rank = None
        for result in results:
            if result.pair_id == pair_id:
                found_at_rank = result.rank
                break

        if found_at_rank is not None:
            # Hit — record at all K >= found_at_rank
            for k in range(found_at_rank, top_k + 1):
                hits_at_k[k] += 1
            reciprocals.append(1.0 / found_at_rank)
        else:
            reciprocals.append(0.0)

    # Compute final metrics
    hit_rates = {
        f"hit@{k}": round(hits_at_k[k] / total, 4)
        for k in range(1, top_k + 1)
    }
    mrr = round(sum(reciprocals) / total, 4) if total > 0 else 0.0
    avg_query_ms = round(sum(query_times) / len(query_times), 2) if query_times else 0.0

    return {
        "total_test_pairs": total,
        "top_k_evaluated":  top_k,
        **hit_rates,
        "mrr": mrr,
        "avg_query_ms": avg_query_ms,
        "zero_result_pairs": zero_result_count,
        "pairs_indexed": engine.stats()["pairs_indexed"],
    }


def print_report(metrics: dict) -> None:
    """Print a clean evaluation report to stdout."""
    print("\n" + "=" * 55)
    print("  BUG FIX RECOMMENDER — V1 EVALUATION REPORT")
    print("=" * 55)
    print(f"  Test pairs evaluated : {metrics['total_test_pairs']}")
    print(f"  Training pairs (index): {metrics['pairs_indexed']}")
    print(f"  Top-K evaluated      : {metrics['top_k_evaluated']}")
    print("-" * 55)
    print("  RETRIEVAL METRICS")
    print("-" * 55)

    top_k = metrics["top_k_evaluated"]
    for k in range(1, top_k + 1):
        key = f"hit@{k}"
        val = metrics.get(key, 0.0)
        bar = "█" * int(val * 30)
        print(f"  Hit@{k:<2}  {val:.1%}  {bar}")

    print(f"\n  MRR           {metrics['mrr']:.4f}")
    print(f"  Avg query time {metrics['avg_query_ms']:.1f} ms")
    print(f"  Zero-result    {metrics['zero_result_pairs']} pairs")
    print("=" * 55)

    # Interpretation
    hit1 = metrics.get("hit@1", 0.0)
    hit5 = metrics.get("hit@5", 0.0) if top_k >= 5 else None
    print("\n  INTERPRETATION")
    print("-" * 55)
    if hit1 >= 0.35:
        print("  Hit@1 ≥ 35% — Strong V1 baseline. Ready to ship.")
    elif hit1 >= 0.15:
        print("  Hit@1 15-35% — Acceptable V1. Upgrade to V2 reranker.")
    else:
        print("  Hit@1 < 15%  — Weak. Check data quality and filters.")

    if hit5:
        print(f"  Hit@5 {hit5:.1%} — {hit5/hit1:.1f}x improvement over Hit@1 with K=5")

    print("\n  NEXT STEPS FOR V2")
    print("  - Add CodeBERT embedding reranker on top of BM25 results")
    print("  - Add repo diversity penalty (cap results per repo)")
    print("  - Expand dataset to 50K+ pairs")
    print("=" * 55)


def main():
    args = parse_args()

    cfg = load_config("config/config.yaml")
    setup_logger(
        log_dir=cfg.logging.log_dir,
        log_file=cfg.logging.log_file,
        level=cfg.logging.level,
        rotation=cfg.logging.rotation,
        retention=cfg.logging.retention,
    )

    # Load BM25 index
    engine = BM25Engine(index_dir=cfg.checkpoints.checkpoint_dir)
    engine.load_index()
    logger.info(f"Index loaded: {engine.stats()['pairs_indexed']} pairs")

    # Load test pairs
    test_path = Path(cfg.storage.processed_dir) / "test.jsonl"
    test_pairs = load_test_pairs(str(test_path), sample=args.sample)
    logger.info(f"Test pairs loaded: {len(test_pairs)}")

    # Run evaluation
    start = time.perf_counter()
    metrics = evaluate(engine, test_pairs, top_k=args.top_k)
    total_time = time.perf_counter() - start
    metrics["total_eval_time_s"] = round(total_time, 1)

    # Print report
    print_report(metrics)

    # Save results
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
    logger.info(f"Results saved to: {output_path}")


if __name__ == "__main__":
    main()