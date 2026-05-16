"""
api/server.py — FastAPI HTTP server wrapping the BM25 engine.

WHY FastAPI over Flask:
- Automatic request/response validation via Pydantic models.
- Auto-generated OpenAPI docs at /docs — useful for debugging.
- async-ready (we don't need it now, but it's free).
- Type hints make the contract explicit.

Architecture:
- Engine loads ONCE at startup (lifespan event).
- All requests share the same in-memory BM25 index.
- No per-request disk I/O after startup.
- Single worker is fine — BM25 query is CPU-bound ~10ms, not I/O-bound.

Run with:
    python -m api.server
    uvicorn api.server:app --host 127.0.0.1 --port 8000 --reload
"""

import time
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Add project root to path so imports work when running as a module
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config_loader import load_config
from src.retrieval.bm25_engine import BM25Engine, BugFixResult
from src.utils.logger import setup_logger, get_logger

logger = get_logger(__name__)

# ── Global engine instance ────────────────────────────────────
# Shared across all requests. Loaded once at startup.
_engine: Optional[BM25Engine] = None


# ── Lifespan: startup + shutdown ──────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Load the BM25 index at server startup.
    FastAPI's lifespan replaces the old @app.on_event("startup") pattern.
    """
    global _engine

    try:
        cfg = load_config("config/config.yaml")
        setup_logger(
            log_dir=cfg.logging.log_dir,
            log_file=cfg.logging.log_file,
            level=cfg.logging.level,
            rotation=cfg.logging.rotation,
            retention=cfg.logging.retention,
        )
    except Exception as e:
        logger.warning(f"Could not load config: {e}. Using defaults.")
        cfg = None

    index_dir = (
        cfg.checkpoints.checkpoint_dir if cfg else "checkpoints"
    )

    logger.info("Server starting — loading BM25 index...")
    _engine = BM25Engine(index_dir=index_dir)

    try:
        _engine.load_index()
        s = _engine.stats()
        logger.info(
            f"BM25 index ready: {s['pairs_indexed']} pairs | "
            f"{s['index_size_mb']} MB"
        )
    except FileNotFoundError:
        logger.error(
            "BM25 index not found. Build it first:\n"
            "  python main.py --step index\n"
            "Server will start but /recommend will return 503 until index is built."
        )
        _engine = None

    yield  # Server is running

    logger.info("Server shutting down.")


# ── App ───────────────────────────────────────────────────────

app = FastAPI(
    title="Bug Fix Recommender API",
    description=(
        "BM25-based bug-fix recommendation engine. "
        "Given a buggy Java code snippet, returns the most similar "
        "historical bug fixes from real GitHub repositories."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# CORS: allow VS Code extension (which runs as a local webview)
# to call this API without browser CORS errors.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # local-only server, safe to allow all
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── Request / Response models ─────────────────────────────────

class RecommendRequest(BaseModel):
    """Input: buggy code snippet to find fixes for."""

    buggy_code: str = Field(
        ...,
        min_length=1,
        max_length=50_000,
        description="The buggy Java code snippet to find fixes for.",
        examples=["public void run() { String s = null; s.trim(); }"],
    )
    top_k: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of fix recommendations to return (1-20).",
    )


class FixRecommendation(BaseModel):
    """One fix recommendation."""

    rank: int
    score: float
    fixed_code: str
    buggy_code: str
    commit_message: str
    repo: str
    file_path: str
    pair_id: str


class RecommendResponse(BaseModel):
    """Response containing ranked fix recommendations."""

    results: List[FixRecommendation]
    total_results: int
    query_time_ms: float
    pairs_indexed: int


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    index_loaded: bool
    pairs_indexed: int
    index_size_mb: float
    version: str


# ── Endpoints ─────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse, tags=["system"])
async def health_check():
    """
    Health check endpoint.
    Returns index status and pair count.
    VS Code extension should call this on startup to verify the server is ready.
    """
    if _engine is None or not _engine.is_ready():
        return HealthResponse(
            status="degraded",
            index_loaded=False,
            pairs_indexed=0,
            index_size_mb=0.0,
            version="1.0.0",
        )

    s = _engine.stats()
    return HealthResponse(
        status="ok",
        index_loaded=True,
        pairs_indexed=s["pairs_indexed"],
        index_size_mb=s["index_size_mb"],
        version="1.0.0",
    )


@app.post("/recommend", response_model=RecommendResponse, tags=["recommendation"])
async def recommend(request: RecommendRequest):
    """
    Find the top-K most similar bug fixes for a given buggy code snippet.

    The engine uses BM25 retrieval over 8,555+ historical bug-fix pairs
    extracted from high-quality Java repositories on GitHub.

    Response time: typically 10-50ms after index is loaded.
    """
    if _engine is None or not _engine.is_ready():
        raise HTTPException(
            status_code=503,
            detail=(
                "BM25 index not loaded. "
                "Build the index first: python main.py --step index"
            ),
        )

    start = time.perf_counter()

    try:
        results: List[BugFixResult] = _engine.query(
            buggy_code=request.buggy_code,
            top_k=request.top_k,
        )
    except Exception as e:
        logger.error(f"Query failed: {e}")
        raise HTTPException(status_code=500, detail=f"Query error: {str(e)}")

    elapsed_ms = (time.perf_counter() - start) * 1000

    recommendations = [
        FixRecommendation(
            rank=r.rank,
            score=round(r.score, 4),
            fixed_code=r.fixed_code,
            buggy_code=r.buggy_code,
            commit_message=r.commit_message,
            repo=r.repo,
            file_path=r.file_path,
            pair_id=r.pair_id,
        )
        for r in results
    ]

    logger.info(
        f"Query: {len(request.buggy_code)} chars -> "
        f"{len(recommendations)} results in {elapsed_ms:.1f}ms"
    )

    return RecommendResponse(
        results=recommendations,
        total_results=len(recommendations),
        query_time_ms=round(elapsed_ms, 2),
        pairs_indexed=_engine.stats()["pairs_indexed"],
    )


@app.get("/", tags=["system"])
async def root():
    """Redirect browsers to the interactive docs."""
    return {
        "message": "Bug Fix Recommender API v1.0.0",
        "docs": "http://127.0.0.1:8000/docs",
        "health": "http://127.0.0.1:8000/health",
        "recommend": "POST http://127.0.0.1:8000/recommend",
    }


# ── Entry point ───────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api.server:app",
        host="127.0.0.1",
        port=8000,
        reload=False,     # reload=True causes double index load — avoid
        log_level="info",
    )