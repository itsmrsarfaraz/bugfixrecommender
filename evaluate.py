"""
evaluate.py — Measure BM25 retrieval quality. Two correct metrics.

WHY the original evaluation gave 0%:
  It used pair_id matching against the training index.
  Test repos are never in the training index (repo-level split),
  so the correct pair CANNOT appear — 0% is mathematically guaranteed.
  That measured train/test isolation, not retrieval quality.

TWO CORRECT METRICS:

1. Self-retrieval (Hit@K):
   Build a combined index (train + test).
   Query each test pair with its own buggy_code.
   Check if the same pair_id comes back in top-K.
   Answers: "Given a bug in our database, can BM25 find it?"
   Expected: 50-90%+ (same file tokenises similarly).

2. Edit Similarity (Jaccard token overlap):
   Query the TRAINING index with test buggy_code.
   Measure token overlap: returned fixed_code vs ground-truth fixed_code.
   Answers: "Are returned fixes related to the actual fix?"
   Expected: 15-35% cross-repo (BM25 is lexical, not semantic).

Run:
  python evaluate.py                     # both metrics, 200 test pairs
  python evaluate.py --metric self       # self-retrieval only (fast)
  python evaluate.py --metric similarity # similarity only
  python evaluate.py --sample 100        # quick smoke test
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import List, Dict, Optional

sys.path.insert(0, str(Path(__file__).parent))

from src.config_loader import load_config
from src.retrieval.bm25_engine import BM25Engine
from src.utils.logger import setup_logger, get_logger

logger = get_logger(__name__)


def parse_args():
    p = argparse.ArgumentParser(description="Evaluate BM25 retrieval quality")
    p.add_argument("--top-k",  type=int, default=5,
                   help="Max K for Hit@K (default: 5)")
    p.add_argument("--sample", type=int, default=200,
                   help="Evaluate on first N test pairs (default: 200)")
    p.add_argument("--metric", choices=["self", "similarity", "both"],
                   default="both",
                   help="Which evaluation to run (default: both)")
    p.add_argument("--output", type=str,
                   default="data/processed/eval_results.json")
    return p.parse_args()


def load_jsonl(path: str, sample: Optional[int] = None) -> List[dict]:
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


def token_overlap(a: str, b: str) -> float:
    """Jaccard token overlap. 0=no overlap, 1=identical token sets."""
    def tokens(s):
        return set(re.findall(r'\w+', s.lower()))
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def truncate(code: str, max_chars: int = 8000) -> str:
    """
    Truncate very large files before querying.
    WHY: BM25 on a 200KB Java file takes 7+ seconds because
    tokenisation produces 50K+ tokens. First 8000 chars
    contains the class signature + key methods — plenty for BM25.
    Query time drops from 7000ms to ~50ms.
    """
    return code[:max_chars] if len(code) > max_chars else code


# ── Evaluation 1: Self-Retrieval ──────────────────────────────

def eval_self_retrieval(
    train_path: str,
    test_path: str,
    index_dir: str,
    top_k: int,
    sample: Optional[int],
) -> dict:
    """
    Build combined (train+test) index.
    Query each test pair with its own buggy_code.
    Check if same pair_id is returned in top-K.
    """
    logger.info("=== EVALUATION 1: Self-Retrieval ===")

    train_pairs = load_jsonl(train_path)
    test_pairs  = load_jsonl(test_path, sample=sample)
    all_pairs   = train_pairs + test_pairs

    logger.info(f"Building combined index: {len(all_pairs)} pairs...")

    # Write combined temp file
    tmp = Path(index_dir) / "_eval_combined.jsonl"
    with open(tmp, "w", encoding="utf-8") as f:
        for p in all_pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    engine = BM25Engine(index_dir=index_dir)
    engine.build_index(str(tmp))
    tmp.unlink()

    hits_at_k   = {k: 0 for k in range(1, top_k + 1)}
    reciprocals = []
    query_times = []
    total = len(test_pairs)

    logger.info(f"Querying {total} test pairs...")

    for i, pair in enumerate(test_pairs, 1):
        if i % 50 == 0:
            logger.info(f"Progress: {i}/{total}")

        pair_id    = pair.get("pair_id", "")
        buggy_code = truncate(pair.get("buggy_code", ""))

        if not buggy_code or not pair_id:
            reciprocals.append(0.0)
            continue

        start = time.perf_counter()
        results = engine.query(buggy_code, top_k=top_k)
        query_times.append((time.perf_counter() - start) * 1000)

        found_rank = next(
            (r.rank for r in results if r.pair_id == pair_id), None
        )

        if found_rank is not None:
            for k in range(found_rank, top_k + 1):
                hits_at_k[k] += 1
            reciprocals.append(1.0 / found_rank)
        else:
            reciprocals.append(0.0)

    return {
        "eval_type":         "self_retrieval",
        "total_test_pairs":  total,
        "total_index_pairs": len(all_pairs),
        **{f"hit@{k}": round(hits_at_k[k] / total, 4)
           for k in range(1, top_k + 1)},
        "mrr":          round(sum(reciprocals) / total, 4) if total else 0.0,
        "avg_query_ms": round(sum(query_times) / len(query_times), 1)
                        if query_times else 0.0,
    }


# ── Evaluation 2: Edit Similarity ─────────────────────────────

def eval_edit_similarity(
    train_index_dir: str,
    test_path: str,
    top_k: int,
    sample: Optional[int],
) -> dict:
    """
    Query the TRAINING index with test buggy_code.
    Measure Jaccard overlap: returned fix vs ground-truth fix.
    """
    logger.info("=== EVALUATION 2: Edit Similarity ===")

    engine = BM25Engine(index_dir=train_index_dir)
    engine.load_index()

    test_pairs = load_jsonl(test_path, sample=sample)
    total = len(test_pairs)

    sims_top1 = []
    sims_top3 = []
    query_times = []
    zero_results = 0

    logger.info(f"Querying {total} test pairs against training index...")

    for i, pair in enumerate(test_pairs, 1):
        if i % 50 == 0:
            logger.info(f"Progress: {i}/{total}")

        buggy_code = truncate(pair.get("buggy_code", ""))
        gt_fix     = pair.get("fixed_code", "")

        if not buggy_code or not gt_fix:
            continue

        start = time.perf_counter()
        results = engine.query(buggy_code, top_k=top_k)
        query_times.append((time.perf_counter() - start) * 1000)

        if not results:
            zero_results += 1
            sims_top1.append(0.0)
            sims_top3.append(0.0)
            continue

        sims_top1.append(token_overlap(results[0].fixed_code, gt_fix))
        sims_top3.append(max(
            token_overlap(r.fixed_code, gt_fix)
            for r in results[:min(3, len(results))]
        ))

    def avg(lst): return round(sum(lst) / len(lst), 4) if lst else 0.0
    def pct_above(lst, t): return round(sum(1 for s in lst if s >= t) / len(lst), 4) if lst else 0.0

    return {
        "eval_type":                  "edit_similarity",
        "total_test_pairs":           total,
        "avg_jaccard_top1":           avg(sims_top1),
        "avg_jaccard_top3":           avg(sims_top3),
        "meaningful_match_top1_pct":  pct_above(sims_top1, 0.20),
        "meaningful_match_top3_pct":  pct_above(sims_top3, 0.20),
        "avg_query_ms":               round(sum(query_times) / len(query_times), 1)
                                      if query_times else 0.0,
        "zero_result_pairs":          zero_results,
    }


# ── Report ────────────────────────────────────────────────────

def print_report(sr: Optional[dict], sim: Optional[dict]) -> None:
    print("\n" + "=" * 62)
    print("  BUG FIX RECOMMENDER — V1 EVALUATION REPORT")
    print("=" * 62)

    if sr:
        top_k = max(int(k.split("@")[1]) for k in sr if k.startswith("hit@"))
        print(f"\n  METRIC 1: Self-Retrieval  (index: {sr['total_index_pairs']} pairs)")
        print(f"  Question: 'Can BM25 retrieve a known pair from ~10K pairs?'")
        print("-" * 62)
        for k in range(1, top_k + 1):
            val = sr.get(f"hit@{k}", 0.0)
            bar = "█" * int(val * 30)
            print(f"  Hit@{k:<2}  {val:.1%}  {bar}")
        print(f"\n  MRR        {sr['mrr']:.4f}")
        print(f"  Avg query  {sr['avg_query_ms']:.0f} ms")

        h1 = sr.get("hit@1", 0)
        print("\n  ↳ ", end="")
        if h1 >= 0.70:
            print(f"Hit@1 {h1:.0%} — BM25 uniquely identifies pairs. Excellent.")
        elif h1 >= 0.40:
            print(f"Hit@1 {h1:.0%} — BM25 working well. Some ambiguity exists.")
        elif h1 >= 0.20:
            print(f"Hit@1 {h1:.0%} — Moderate. Large similar files cause collisions.")
        else:
            print(f"Hit@1 {h1:.0%} — Low self-retrieval. Many files are very similar.")
            print("     Typical for codebases sharing import patterns (Spring, Alibaba).")

    if sim:
        print(f"\n  METRIC 2: Cross-Repo Edit Similarity")
        print(f"  Question: 'Do returned fixes resemble the actual fix?'")
        print("-" * 62)
        print(f"  Avg Jaccard Top-1:    {sim['avg_jaccard_top1']:.3f}  (0=none, 1=identical)")
        print(f"  Avg Jaccard Top-3:    {sim['avg_jaccard_top3']:.3f}  (best of 3 results)")
        print(f"  >20% overlap Top-1:   {sim['meaningful_match_top1_pct']:.1%} of queries")
        print(f"  >20% overlap Top-3:   {sim['meaningful_match_top3_pct']:.1%} of queries")
        print(f"  Avg query time:       {sim['avg_query_ms']:.0f} ms")

        avg1 = sim["avg_jaccard_top1"]
        m1   = sim["meaningful_match_top1_pct"]
        print("\n  ↳ ", end="")
        if avg1 >= 0.30:
            print(f"Avg {avg1:.3f} — Strong. BM25 finds genuinely related fixes.")
        elif avg1 >= 0.15:
            print(f"Avg {avg1:.3f} — Moderate. Expected for lexical cross-repo retrieval.")
            print(f"     {m1:.0%} of queries returned a fix with >20% token overlap.")
        else:
            print(f"Avg {avg1:.3f} — Low average. Check meaningful_match_pct for context.")
            print(f"     Large files skew averages down. {m1:.0%} queries had >20% overlap.")

    print("\n  PRIORITY V2 IMPROVEMENTS")
    print("  1. Truncate query to first 8000 chars (already done in this eval)")
    print("  2. Add max_results_per_repo=2 cap in BM25Engine.query()")
    print("  3. CodeBERT reranker on top-10 BM25 results")
    print("=" * 62)


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

    train_path = str(Path(cfg.storage.processed_dir) / "train.jsonl")
    test_path  = str(Path(cfg.storage.processed_dir) / "test.jsonl")
    index_dir  = cfg.checkpoints.checkpoint_dir

    sr  = None
    sim = None
    t0  = time.perf_counter()

    if args.metric in ("self", "both"):
        sr = eval_self_retrieval(
            train_path=train_path,
            test_path=test_path,
            index_dir=index_dir,
            top_k=args.top_k,
            sample=args.sample,
        )

    if args.metric in ("similarity", "both"):
        sim = eval_edit_similarity(
            train_index_dir=index_dir,
            test_path=test_path,
            top_k=args.top_k,
            sample=args.sample,
        )

    print_report(sr, sim)

    result = {
        "total_eval_time_s": round(time.perf_counter() - t0, 1),
        "sample_size": args.sample,
        "top_k": args.top_k,
    }
    if sr:  result["self_retrieval"]  = sr
    if sim: result["edit_similarity"] = sim

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    logger.info(f"Results saved: {out}")

    # Restore training-only index after self-retrieval
    if args.metric in ("self", "both"):
        logger.info("Restoring training-only index...")
        BM25Engine(index_dir=index_dir).build_index(train_path)
        logger.info("Done.")


if __name__ == "__main__":
    main()