"""
main.py — Pipeline entry point.

Steps:
  python main --step discovery  -> find repos on GitHub
  python main --step download   -> bare clone + extract + delete (integrated)
  python main --step preprocess -> deduplicate, filter, split train/val/test
  python main --step index      -> build BM25 index from train.jsonl
  python main --step query      -> interactive query demo (for testing)
  python main --step all        -> discovery + download + preprocess + index
"""

import sys
import argparse
from pathlib import Path

from src.config_loader import load_config
from src.utils.logger import setup_logger, get_logger


def parse_args():
    parser = argparse.ArgumentParser(description="Bug Fix Recommender Pipeline")
    parser.add_argument(
        "--step",
        choices=["discovery", "download", "preprocess", "index", "query", "all"],
        default="all",
        help="Pipeline step to run (default: all)",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Number of results to return for --step query (default: 5)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        cfg = load_config("config/config.yaml")
    except FileNotFoundError as e:
        print(f"[FATAL] {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[FATAL] Config validation failed:\n{e}")
        sys.exit(1)

    setup_logger(
        log_dir=cfg.logging.log_dir,
        log_file=cfg.logging.log_file,
        level=cfg.logging.level,
        rotation=cfg.logging.rotation,
        retention=cfg.logging.retention,
    )
    logger = get_logger(__name__)
    logger.info("Bug Fix Recommender pipeline starting")

    for d in [
        cfg.downloader.clone_dir,
        cfg.storage.extracted_dir,
        cfg.storage.processed_dir,
        cfg.checkpoints.checkpoint_dir,
        cfg.logging.log_dir,
    ]:
        Path(d).mkdir(parents=True, exist_ok=True)

    if cfg.github.token is None:
        logger.warning("GitHub token not set. Run: $env:GITHUB_TOKEN='ghp_xxxx'")
    else:
        logger.info("GitHub token loaded from environment.")

    step = args.step

    # Discovery
    if step in ("discovery", "all"):
        logger.info("=== STEP: Repository Discovery ===")
        from src.discovery.repo_discovery import RepoDiscovery
        repos = RepoDiscovery(cfg).run()
        logger.info(f"Discovery complete. {len(repos)} repos ready.")

    # Download + Extract
    if step in ("download", "all"):
        logger.info("=== STEP: Download + Extract ===")
        from src.extractor.commit_extractor import CommitExtractor
        from src.storage.dataset_writer import DatasetWriter
        from src.downloader.repo_downloader import RepoDownloader

        extractor = CommitExtractor(cfg)

        with DatasetWriter(cfg) as writer:
            def extract_and_write(repo_path: Path, repo_meta: dict) -> None:
                pairs_from_repo = 0
                for pair in extractor.extract(repo_path, repo_meta):
                    writer.write(pair)
                    pairs_from_repo += 1
                logger.info(f"Extracted {pairs_from_repo} pairs from {repo_meta.get('full_name', '?')}")

            downloader = RepoDownloader(
                cfg=cfg,
                extractor_callback=extract_and_write,
                max_repo_size_mb=cfg.downloader.max_repo_size_mb,
            )
            results = downloader.run()

        processed = sum(1 for r in results.values() if r.get("status") == "processed")
        skipped   = sum(1 for r in results.values() if r.get("status") == "skipped")
        stats = DatasetWriter(cfg).get_stats()
        logger.info(f"Download+Extract complete. Processed: {processed} | Skipped: {skipped}")
        logger.info(f"Dataset: {stats['total_records']} pairs in {stats['chunk_files']} files -> {stats['output_dir']}")

    # Preprocess
    if step in ("preprocess", "all"):
        logger.info("=== STEP: Preprocessing ===")
        from src.preprocessing.preprocessor import Preprocessor
        stats = Preprocessor(cfg).run()
        logger.info(f"Clean dataset: train={stats['train_pairs']} | val={stats['val_pairs']} | test={stats['test_pairs']} | dropped={stats['dropped_total']}")

    # Index
    if step in ("index", "all"):
        logger.info("=== STEP: Build BM25 Index ===")
        from src.retrieval.bm25_engine import BM25Engine
        train_path = Path(cfg.storage.processed_dir) / "train.jsonl"
        engine = BM25Engine(index_dir=cfg.checkpoints.checkpoint_dir)
        engine.build_index(str(train_path))
        s = engine.stats()
        logger.info(f"Index ready: {s['pairs_indexed']} pairs | {s['index_size_mb']} MB | k1={s['k1']} b={s['b']}")

    # Query demo
    if step == "query":
        logger.info("=== STEP: Interactive Query Demo ===")
        from src.retrieval.bm25_engine import BM25Engine
        engine = BM25Engine(index_dir=cfg.checkpoints.checkpoint_dir)
        engine.load_index()
        logger.info(f"Index loaded: {engine.stats()['pairs_indexed']} pairs")
        logger.info("Enter buggy Java code. Type 'quit' to exit.")

        while True:
            print("\n" + "=" * 60)
            print("Paste buggy Java code (or 'quit'):")
            try:
                query = input("> ").strip()
            except (KeyboardInterrupt, EOFError):
                break
            if query.lower() in ("quit", "exit", "q"):
                break
            if not query:
                continue

            results = engine.query(query, top_k=args.top_k)
            if not results:
                print("No matching fixes found.")
                continue

            print(f"\nTop {len(results)} fix recommendation(s):\n")
            for r in results:
                print(f"  Rank {r.rank} | Score: {r.score:.3f} | {r.repo}")
                print(f"  Commit: {r.commit_message[:80]}")
                print(f"  Fix preview: {r.fixed_code[:150].strip()}")
                print()

    logger.info("Pipeline run complete.")


if __name__ == "__main__":
    main()