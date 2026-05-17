# Bug Fix Recommender

A production-grade pipeline that mines bug-fix commits from GitHub,
builds a structured training dataset, and serves fix recommendations
via a BM25 retrieval engine and FastAPI backend.

Built as the foundation for a VS Code extension that recommends fixes
for buggy Java code based on patterns from 11,000+ real historical fixes.

---

## System Architecture

```
GitHub Repos
    │
    ▼
[Discovery]      → filter by stars, activity, language
    │
    ▼
[Downloader]     → bare clone (depth=500), no checkout
    │
    ▼
[Extractor]      → keyword filter → diff filter → file filter
    │
    ▼
[Preprocessor]   → dedup + quality filter + repo-level train/val/test split
    │
    ▼
[BM25 Index]     → rank-bm25 inverted index over 8,555 training pairs
    │
    ▼
[FastAPI Server] → POST /recommend → top-K fix suggestions
    │
    ▼
[VS Code Ext]    → (Phase 3)
```

---

## Dataset (V1)

| Stat                     | Value                        |
| ------------------------ | ---------------------------- |
| Repos processed          | ~160 Java repositories       |
| Raw extracted pairs      | 12,490                       |
| After cleaning           | 11,883                       |
| Training pairs (indexed) | 8,555                        |
| Validation pairs         | 1,367                        |
| Test pairs               | 1,961                        |
| Split method             | By repository (zero leakage) |
| Languages                | Java (v1)                    |

---

## Requirements

- Python 3.11.9
- Windows 10 / Linux / macOS
- 8GB RAM minimum (index is ~336MB in memory)
- Git installed and on PATH
- GitHub Personal Access Token (for > 60 req/hr)

---

## Quick Start

### 1. Clone and set up

```bash
git clone https://github.com/itsmrsarfaraz/bugfixrecommender.git
cd bugfixrecommender
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/Mac
pip install -r requirements.txt
```

### 2. Set your GitHub token

```powershell
# PowerShell
$env:GITHUB_TOKEN="ghp_your_token_here"
```

Get a token at: GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic). Only `public_repo` scope needed.

### 3. Run the full pipeline

```powershell
# Run all steps in order
python main.py --step discovery    # find 200 Java repos
python main.py --step download     # clone, extract, delete (repeat until done)
python main.py --step preprocess   # deduplicate + split
python main.py --step index        # build BM25 index
```

Or if you already have a dataset (skip to serving):

```powershell
python main.py --step index
python -m api.server
```

### 4. Query the API

```powershell
# Start server
python -m api.server

# Health check
curl http://127.0.0.1:8000/health

# Get fix recommendations (PowerShell)
$body = '{"buggy_code": "public void run() { String s = null; s.trim(); }", "top_k": 5}'
Invoke-RestMethod -Uri http://127.0.0.1:8000/recommend -Method Post -ContentType "application/json" -Body $body

# Or use the interactive docs
# http://127.0.0.1:8000/docs
```

### 5. Interactive query demo

```powershell
python main.py --step query
```

### 6. Evaluate retrieval quality

```powershell
python evaluate.py --top-k 5
```

---

## Pipeline Steps Reference

| Command             | What it does                              | When to run                      |
| ------------------- | ----------------------------------------- | -------------------------------- |
| `--step discovery`  | Find 200 Java repos on GitHub             | Once, or when expanding dataset  |
| `--step download`   | Clone + extract + delete (20 repos/batch) | Repeat until all repos processed |
| `--step preprocess` | Dedup, quality filter, split              | After all downloads complete     |
| `--step index`      | Build BM25 index from train.jsonl         | After preprocessing              |
| `--step query`      | Interactive query demo                    | For manual testing               |
| `--step all`        | Run everything                            | Fresh start only                 |

---

## Project Structure

```
bugfixrecommender/
├── config/
│    └── config.yaml          ← single source of truth for all thresholds
│
├── src/
│    ├── config_loader.py     ← pydantic v2 validated config
│    ├── utils/logger.py      ← loguru setup
│    ├── discovery/           ← GitHub API repo discovery
│    ├── downloader/          ← bare git clone + disk quota guard
│    ├── extractor/           ← commit filter + diff parser + language adapters
│    ├── preprocessing/       ← dedup + quality filter + repo-level splits
│    ├── storage/             ← chunked JSONL writer with resume support
│    └── retrieval/           ← BM25 index + query engine
│
├── api/
│    └── server.py            ← FastAPI HTTP server
│
├── data/
│    ├── raw/                 ← cloned repos (gitignored, cleaned after extract)
│    ├── extracted/           ← bug-fix pair chunks (gitignored)
│    └── processed/           ← train/val/test splits (gitignored)
│
├── checkpoints/             ← resume state + BM25 index (gitignored)
├── logs/
├── tests/                   ← 104 unit tests
├── evaluate.py              ← Hit@K + MRR evaluation
├── main.py                  ← pipeline entry point
└── requirements.txt
```

---

## Configuration

All thresholds live in `config/config.yaml`. Key settings:

```yaml
github:
  min_stars: 100 # repos below this are too obscure
  min_activity_days: 365 # dead repos skipped
  max_repos: 200 # discovery target

downloader:
  batch_size: 20 # repos per pipeline run
  max_repo_size_mb: 500 # skip unusually large repos
  clone_timeout_seconds: 300

extractor:
  max_diff_lines: 200 # skip large refactors
  max_files_changed: 5 # skip broad commits
  min_meaningful_tokens: 3

storage:
  output_format: jsonl
  chunk_size: 1000
```

---

## API Reference

### `POST /recommend`

Request:

```json
{
  "buggy_code": "public void run() { String s = null; s.trim(); }",
  "top_k": 5
}
```

Response:

```json
{
  "results": [
    {
      "rank": 1,
      "score": 42.3,
      "fixed_code": "public void run() { if(s != null) s.trim(); }",
      "buggy_code": "...",
      "commit_message": "fix: null pointer in run()",
      "repo": "apache/kafka",
      "file_path": "src/main/java/Runner.java",
      "pair_id": "uuid"
    }
  ],
  "total_results": 5,
  "query_time_ms": 18.4,
  "pairs_indexed": 8555
}
```

### `GET /health`

Returns index status, pair count, and server version.

### `GET /docs`

Interactive Swagger UI for manual testing.

---

## Upgrade Path

| Version | Model                                | Status                       |
| ------- | ------------------------------------ | ---------------------------- |
| V1      | BM25 retrieval (rank-bm25)           | ✅ Shipped                   |
| V2      | BM25 + CodeBERT reranker (FAISS ANN) | Planned                      |
| V3      | Fine-tuned CodeBERT classifier       | Needs 10K+ quality pairs     |
| V4      | CodeT5 generative repair             | Needs RTX-class GPU or cloud |

---

## Test Suite

```powershell
pytest tests/ -v
# 104 tests across 6 modules
```

| Module           | Tests | Coverage                             |
| ---------------- | ----- | ------------------------------------ |
| Config loader    | 6     | Config validation, env vars          |
| Repo discovery   | 10    | Filters, checkpointing, dedup        |
| Downloader       | 11    | Bare clone, disk guard, docs filter  |
| Commit extractor | 13    | 3-layer filtering, language adapters |
| Preprocessor     | 11    | Dedup, quality filters, split logic  |
| BM25 engine      | 23    | Tokenize, index, query, persistence  |
| API server       | 16    | Endpoints, validation, error codes   |

---

## Known Limitations (V1)

- **336MB RAM** for the BM25 index (loaded once at startup)
- **Repo dominance bias**: repos with many pairs can dominate results. Fix: add `max_results_per_repo` cap in V2
- **English commit messages only**: repos with Chinese/other-language commits have lower extraction yield
- **Java only**: architecture supports Python/JS/TS via language adapters (V2)
- **BM25 is lexical**: semantic similarity not captured. Similar bugs with different variable names may not match

---

## Hardware Used for Development

- Windows 10
- 16GB RAM
- Intel i-series CPU (no GPU used)
- ~200GB disk

All training is CPU-based. GPU not required for V1.

---

## License

MIT
