"""
main.py — Pipeline entry point.
 
Run the full pipeline:    python main.py
Run discovery only:       python main.py --step discovery
Run download only:        python main.py --step download
Run both:                 python main.py --step all
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
        choices=["discovery", "download", "extract", "preprocess", "all"],
        default="all",
        help="Which pipeline step to run (default: all)",
    )
    return parser.parse_args()
 
 
def main() -> None:
    args = parse_args()
 
    # ── 1. Load and validate config ──────────────────────────
    try:
        cfg = load_config("config/config.yaml")
    except FileNotFoundError as e:
        print(f"[FATAL] {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[FATAL] Config validation failed:\n{e}")
        sys.exit(1)
 
    # ── 2. Set up logging ────────────────────────────────────
    setup_logger(
        log_dir=cfg.logging.log_dir,
        log_file=cfg.logging.log_file,
        level=cfg.logging.level,
        rotation=cfg.logging.rotation,
        retention=cfg.logging.retention,
    )
    logger = get_logger(__name__)
    logger.info("Bug Fix Recommender pipeline starting")
 
    # ── 3. Ensure directories exist ──────────────────────────
    for d in [
        cfg.downloader.clone_dir,
        cfg.storage.extracted_dir,
        cfg.storage.processed_dir,
        cfg.checkpoints.checkpoint_dir,
        cfg.logging.log_dir,
    ]:
        Path(d).mkdir(parents=True, exist_ok=True)

    # ── 4. Token check ───────────────────────────────────────
    if cfg.github.token is None:
        logger.warning(
            "GitHub token not set. "
            "Run in PowerShell: os.getenv('GITHUB_TOKEN')"
        )
    else:
        logger.info("GitHub token loaded from environment.")

    # ── 5. Run requested step ────────────────────────────────
    step = args.step
 
    # ── 6. Discovery ─────────────────────────────────────────
    if step in ("discovery", "all"):
        logger.info("=== STEP: Repository Discovery ===")
        from src.discovery.repo_discovery import RepoDiscovery
        discoverer = RepoDiscovery(cfg)
        repos = discoverer.run()
        logger.info(f"Discovery complete. {len(repos)} repos ready for download.")
 
    # ── 7. Download ──────────────────────────────────────────
    if step in ("download", "all"):
        logger.info("=== STEP: Repository Download ===")
        from src.downloader.repo_downloader import RepoDownloader
 
        # Extractor callback wired in Step 4.
        # For now: download and immediately delete (no extraction yet).
        downloader = RepoDownloader(
            cfg=cfg,
            extractor_callback=None,        # Step 4 will inject this
            max_repo_size_mb=cfg.downloader.max_repo_size_mb,
        )
        results = downloader.run()
 
        processed = sum(1 for r in results.values() if r.get("status") == "processed")
        skipped   = sum(1 for r in results.values() if r.get("status") == "skipped")
        logger.info(
            f"Download step complete. "
            f"Processed: {processed} | Skipped: {skipped}"
        )
 
    if step in ("extract", "all"):
        logger.info("=== STEP: Commit Extraction === (wired in Step 4)")
 
    if step in ("preprocess", "all"):
        logger.info("=== STEP: Preprocessing === (wired in Step 5)")
 
    logger.info("Pipeline run complete.")
 
 
if __name__ == "__main__":
    main()
