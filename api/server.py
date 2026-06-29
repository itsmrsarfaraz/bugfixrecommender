"""
api/server.py — FastAPI server with BM25 retrieval + CodeT5 local inference.
"""

import time
import sys
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config_loader import load_config
from src.retrieval.bm25_engine import BM25Engine, BugFixResult
from src.utils.logger import setup_logger, get_logger

logger = get_logger(__name__)

_engine: Optional[BM25Engine] = None
_codet5_model = None
_codet5_tokenizer = None

MODEL_PATH = str(Path(__file__).parent.parent / "models" / "codet5_bugfix" / "final_production_model")
PREFIX = "fix java bug: "
MAX_IN = 256
MAX_OUT = 128


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _engine, _codet5_model, _codet5_tokenizer

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

    index_dir = cfg.checkpoints.checkpoint_dir if cfg else "checkpoints"

    logger.info("Server starting — loading BM25 index...")
    _engine = BM25Engine(index_dir=index_dir)
    try:
        _engine.load_index()
        s = _engine.stats()
        logger.info(f"BM25 index ready: {s['pairs_indexed']} pairs | {s['index_size_mb']} MB")
    except FileNotFoundError:
        logger.error("BM25 index not found. Run: python main.py --step index")
        _engine = None

    logger.info(f"Loading CodeT5 model from: {MODEL_PATH}")
    try:
        import torch
        from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
        _codet5_tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH)
        _codet5_model = AutoModelForSeq2SeqLM.from_pretrained(MODEL_PATH)
        _codet5_model.eval()
        logger.info("CodeT5 model loaded successfully.")
    except Exception as e:
        logger.error(f"CodeT5 model failed to load: {e}")
        _codet5_model = None
        _codet5_tokenizer = None

    yield

    logger.info("Server shutting down.")


app = FastAPI(
    title="Bug Fix Recommender API",
    description="BM25 retrieval + CodeT5 local fix generation.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class RecommendRequest(BaseModel):
    buggy_code: str = Field(..., min_length=1, max_length=50_000)
    top_k: int = Field(default=5, ge=1, le=20)


class FixRecommendation(BaseModel):
    rank: int
    score: float
    fixed_code: str
    buggy_code: str
    commit_message: str
    repo: str
    file_path: str
    pair_id: str


class RecommendResponse(BaseModel):
    results: List[FixRecommendation]
    total_results: int
    query_time_ms: float
    pairs_indexed: int


class HealthResponse(BaseModel):
    status: str
    index_loaded: bool
    codet5_loaded: bool
    pairs_indexed: int
    index_size_mb: float
    version: str


class GenerateFixRequest(BaseModel):
    buggy_code: str = Field(..., min_length=1, max_length=5000)


class GenerateFixResponse(BaseModel):
    fixed_code: str
    model: str


@app.get("/health", response_model=HealthResponse, tags=["system"])
async def health_check():
    if _engine is None or not _engine.is_ready():
        return HealthResponse(
            status="degraded",
            index_loaded=False,
            codet5_loaded=_codet5_model is not None,
            pairs_indexed=0,
            index_size_mb=0.0,
            version="2.0.0",
        )
    s = _engine.stats()
    return HealthResponse(
        status="ok",
        index_loaded=True,
        codet5_loaded=_codet5_model is not None,
        pairs_indexed=s["pairs_indexed"],
        index_size_mb=s["index_size_mb"],
        version="2.0.0",
    )


@app.post("/recommend", response_model=RecommendResponse, tags=["recommendation"])
async def recommend(request: RecommendRequest):
    if _engine is None or not _engine.is_ready():
        raise HTTPException(status_code=503, detail="BM25 index not loaded.")

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

    return RecommendResponse(
        results=recommendations,
        total_results=len(recommendations),
        query_time_ms=round(elapsed_ms, 2),
        pairs_indexed=_engine.stats()["pairs_indexed"],
    )


@app.post("/generate-fix", response_model=GenerateFixResponse, tags=["generation"])
async def generate_fix(request: GenerateFixRequest):
    if _codet5_model is None or _codet5_tokenizer is None:
        raise HTTPException(status_code=503, detail="CodeT5 model not loaded.")

    try:
        import torch
        
        # Best Practice: Detect execution context dynamically
        device = "cuda" if torch.cuda.is_available() else "cpu"
        input_text = PREFIX + request.buggy_code[:2000]
        
        # Tokenize and push tensors directly to your execution device
        inputs = _codet5_tokenizer(
            input_text,
            return_tensors="pt",
            max_length=MAX_IN,
            truncation=True,
        ).to(device)

        with torch.no_grad():
            outputs = _codet5_model.generate(
                **inputs,
                max_new_tokens=MAX_OUT,
                num_beams=5,
                temperature=0.3,
                no_repeat_ngram_size=2,
                early_stopping=True,
            )

        fixed = _codet5_tokenizer.decode(outputs[0], skip_special_tokens=True).strip()
        return GenerateFixResponse(fixed_code=fixed, model="codet5-base")

    except Exception as e:
        logger.error(f"CodeT5 generation failed: {e}")
        raise HTTPException(status_code=500, detail=f"Generation error: {str(e)}")


@app.get("/", tags=["system"])
async def root():
    return {
        "message": "Bug Fix Recommender API v2.0.0",
        "docs": "http://127.0.0.1:8000/docs",
        "health": "http://127.0.0.1:8000/health",
        "recommend": "POST http://127.0.0.1:8000/recommend",
        "generate-fix": "POST http://127.0.0.1:8000/generate-fix",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api.server:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
        log_level="info",
    )