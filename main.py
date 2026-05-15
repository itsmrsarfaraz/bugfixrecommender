"""
main.py — Pipeline entry point.


Steps:
python main.py --step discovery  → find repos on GitHub
python main.py --step download   → bare clone + extract + delete (integrated)
python main.py --step preprocess → deduplicate, filter, split train/val/test
python main.py --step all        → discovery + download + preprocess
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
        choices=["discovery", "download", "preprocess", "all"],
        default="all",
        help="Pipeline step to run (default: all)",
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

    # ── Discovery ─────────────────────────────────────────────
    if step in ("discovery", "all"):
        logger.info("=== STEP: Repository Discovery ===")
        from src.discovery.repo_discovery import RepoDiscovery
        repos = RepoDiscovery(cfg).run()
        logger.info(f"Discovery complete. {len(repos)} repos ready.")

    # ── Download + Extract (integrated) ──────────────────────
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
                logger.info(
                    f"Extracted {pairs_from_repo} pairs from "
                    f"{repo_meta.get('full_name', '?')}"
                )

            downloader = RepoDownloader(
                cfg=cfg,
                extractor_callback=extract_and_write,
                max_repo_size_mb=cfg.downloader.max_repo_size_mb,
            )
            results = downloader.run()

        processed = sum(1 for r in results.values() if r.get("status") == "processed")
        skipped   = sum(1 for r in results.values() if r.get("status") == "skipped")

        stats = DatasetWriter(cfg).get_stats()
        logger.info(
            f"Download+Extract complete. "
            f"Processed: {processed} | Skipped: {skipped}"
        )
        logger.info(
            f"Dataset: {stats['total_records']} total pairs "
            f"in {stats['chunk_files']} chunk files -> {stats['output_dir']}"
        )

    # ── Preprocess ────────────────────────────────────────────
    if step in ("preprocess", "all"):
        logger.info("=== STEP: Preprocessing ===")
        from src.preprocessing.preprocessor import Preprocessor
        stats = Preprocessor(cfg).run()
        logger.info(
            f"Clean dataset: "
            f"train={stats['train_pairs']} | "
            f"val={stats['val_pairs']} | "
            f"test={stats['test_pairs']} | "
            f"dropped={stats['dropped_total']}"
        )

    logger.info("Pipeline run complete.")


if __name__ == "__main__":
    main()