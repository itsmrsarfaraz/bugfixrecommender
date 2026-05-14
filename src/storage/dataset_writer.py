"""
dataset_writer.py — Write BugFixPair records to JSONL files.

WHY JSONL (JSON Lines) for V1:
- Human-readable — you can open the file and inspect records.
- Appendable — no need to load the entire file to add records.
- Streamable — one record per line, no parsing the full file.
- Trivially converts to Parquet/HuggingFace datasets later.
- Compatible with pandas, datasets, and BM25 indexers directly.

WHY chunked files (not one giant file):
- A single 10GB JSONL file is painful to inspect and slow to load.
- 1000-record chunks stay under ~50MB — easy to open, easy to
  load in RAM for indexing or debugging.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.config_loader import Config
from src.extractor.models import BugFixPair
from src.utils.logger import get_logger

logger = get_logger(__name__)


class DatasetWriter:
    """
    Writes BugFixPair records to chunked JSONL files.

    Files are named: extracted_0000.jsonl, extracted_0001.jsonl, ...
    A new file starts every chunk_size records.
    Existing files are NOT overwritten — writer always appends
    to the last incomplete chunk or starts a new one.

    Usage:
        with DatasetWriter(cfg) as writer:
            for pair in extractor.extract(repo_path, meta):
                writer.write(pair)
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.output_dir = Path(cfg.storage.extracted_dir)
        self.chunk_size = cfg.storage.chunk_size
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # State
        self._current_file = None       # open file handle
        self._current_chunk = 0         # which chunk file we're writing to
        self._records_in_chunk = 0      # records written to current chunk
        self._total_written = 0         # total records this session

        # Find where we left off (resume support)
        self._current_chunk, self._records_in_chunk = self._find_resume_point()

    def __enter__(self):
        self._open_current_chunk()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def write(self, pair: BugFixPair) -> None:
        """Write one BugFixPair to the current chunk file."""
        if self._current_file is None:
            self._open_current_chunk()

        assert self._current_file is not None
        try:
            line = json.dumps(pair.to_dict(), ensure_ascii=False)
            self._current_file.write(line + "\n")
            self._records_in_chunk += 1
            self._total_written += 1

            # Rotate to next chunk when current is full
            if self._records_in_chunk >= self.chunk_size:
                self._rotate_chunk()

        except Exception as e:
            logger.error(f"Write failed for pair {pair.pair_id}: {e}")

    def close(self) -> None:
        """Flush and close the current chunk file."""
        if self._current_file is not None:
            try:
                self._current_file.flush()
                self._current_file.close()
            except Exception:
                pass
            self._current_file = None
        logger.info(
            f"DatasetWriter closed. "
            f"Total written this session: {self._total_written}"
        )

    @property
    def total_written(self) -> int:
        return self._total_written

    # ── Internal helpers ──────────────────────────────────────

    def _chunk_path(self, chunk_num: int) -> Path:
        return self.output_dir / f"extracted_{chunk_num:04d}.jsonl"

    def _open_current_chunk(self) -> None:
        """Open the current chunk file in append mode."""
        path = self._chunk_path(self._current_chunk)
        # 'a' mode: append to existing file (resume from last run)
        self._current_file = open(path, "a", encoding="utf-8")
        logger.debug(f"Writing to chunk: {path.name} ({self._records_in_chunk} existing records)")

    def _rotate_chunk(self) -> None:
        """Close current chunk and open the next one."""
        if self._current_file:
            self._current_file.flush()
            self._current_file.close()

        logger.info(
            f"Chunk {self._current_chunk:04d} complete "
            f"({self._records_in_chunk} records)"
        )
        self._current_chunk += 1
        self._records_in_chunk = 0
        path = self._chunk_path(self._current_chunk)
        self._current_file = open(path, "a", encoding="utf-8")
        logger.debug(f"Opened new chunk: {path.name}")

    def _find_resume_point(self) -> tuple[int, int]:
        """
        Find the last incomplete chunk to resume from.

        Returns (chunk_num, records_in_chunk).
        If no chunks exist, returns (0, 0) — start fresh.
        """
        existing = sorted(self.output_dir.glob("extracted_*.jsonl"))
        if not existing:
            return 0, 0

        last_file = existing[-1]
        chunk_num = int(last_file.stem.split("_")[1])

        # Count records in the last file
        try:
            with open(last_file, "r", encoding="utf-8") as f:
                record_count = sum(1 for line in f if line.strip())
        except Exception:
            record_count = 0

        if record_count >= self.chunk_size:
            # Last chunk is full — start a new one
            logger.info(
                f"Resuming: chunk {chunk_num:04d} full. "
                f"Starting chunk {chunk_num + 1:04d}."
            )
            return chunk_num + 1, 0
        else:
            logger.info(
                f"Resuming: chunk {chunk_num:04d} has "
                f"{record_count}/{self.chunk_size} records."
            )
            return chunk_num, record_count

    def get_stats(self) -> dict:
        """Return summary statistics about the dataset on disk."""
        files = sorted(self.output_dir.glob("extracted_*.jsonl"))
        total_records = 0
        for f in files:
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    total_records += sum(1 for line in fh if line.strip())
            except Exception:
                pass
        return {
            "chunk_files": len(files),
            "total_records": total_records,
            "output_dir": str(self.output_dir),
        }