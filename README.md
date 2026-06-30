# Bug Fix Recommender

A production-grade pipeline that mines bug-fix commits from GitHub, builds a structured training dataset, trains a CodeT5 Seq2Seq model, and serves fix recommendations via a hybrid BM25 + CodeT5 engine, FastAPI backend, and VS Code extension.

**Thesis project** — demonstrates an end-to-end ML data pipeline from raw GitHub data to a working developer tool, without requiring a GPU for inference.

---

## System Architecture

```
GitHub API (1,927 Java repos)
        │
        ▼
[Discovery]       filter by stars, activity, language
        │
        ▼
[Downloader]      bare clone (depth=500), extract, delete
        │
        ▼
[Extractor]       3-layer filter: commit → diff → file
        │
        ▼
[Preprocessor]    dedup + quality filter + repo-level split
        │
        ├──────────────────────────────────┐
        ▼                                  ▼
[BM25 Index]                     [CodeT5 Fine-tuning]
rank-bm25 over 30,000            Salesforce/codet5-base
training pairs                   trained on 26,754 diff pairs
        │                                  │
        └──────────────┬───────────────────┘
                       ▼
              [FastAPI Server]
         POST /recommend  →  BM25 top-K results
         POST /generate-fix  →  CodeT5 generated fix
                       │
                       ▼
              [VS Code Extension]
         Ctrl+Alt+B → WebView panel
         shows CodeT5 fix + BM25 similar cases
```

---

## Dual-Model Approach

| Component | Model | Role |
|-----------|-------|------|
| BM25 | rank-bm25 | Retrieves top-K most similar historical bug-fix pairs from 30,000 indexed commits |
| CodeT5 | Salesforce/codet5-base (222M params) | Generates a fix specifically for the selected code using Seq2Seq |

**Why both?**
- BM25 cannot hallucinate — every result is a real fix from a real commit
- CodeT5 generates a fix tailored to your exact code, not just a similar one
- Together they give the developer two complementary signals

---

## Evaluation Results

### BM25 Retrieval

| Metric | Value |
|--------|-------|
| Self-retrieval Hit@1 | **94%** |
| Self-retrieval Hit@5 | **100%** |
| MRR | **0.9648** |
| Cross-repo Jaccard Top-1 | **0.200** |
| Meaningful match >20% @ Top-1 | **44.5%** |
| API query time | **~75ms** |

### CodeT5 Fine-tuning (Google Colab T4)

| Metric | Value |
|--------|-------|
| Model | Salesforce/codet5-base |
| Training pairs | 26,754 diff pairs |
| Validation pairs | 6,293 |
| Test pairs | 6,079 |
| BLEU (val) | **30.68** |
| Exact Match | 0.4% |
| Edit Similarity | ~55% |
| Training time | ~6 hours on T4 |

---

## Dataset Statistics

| Stat | Value |
|------|-------|
| Repos discovered | 1,927 |
| Repos processed | 1,849 |
| Raw extracted pairs | 80,448 |
| After cleaning | 76,429 |
| Training pairs (BM25 indexed) | 30,000 |
| Training pairs (CodeT5) | 26,754 diff pairs |
| Validation pairs | 6,293 |
| Test pairs | 6,079 |
| Split method | By repository (zero leakage) |

---

## Requirements

- Python 3.12.x
- Node.js 18+ (for VS Code extension only)
- Git installed and on PATH
- VS Code 1.85+
- 16GB RAM minimum
- GitHub Personal Access Token
- Google Colab (for CodeT5 training only — T4 GPU)

---

## Step-by-Step Setup

### Step 1 — Clone the repository

```powershell
git clone https://github.com/itsmrsarfaraz/bugfixrecommender.git
cd bugfixrecommender
```

### Step 2 — Create and activate virtual environment

```powershell
python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # Linux/WSL
```

### Step 3 — Install Python dependencies

```powershell
pip install -r requirements.txt
pip install httpx python-dotenv transformers torch --break-system-packages
```

### Step 4 — Set your GitHub token

Create a `.env` file at the project root:

```
GITHUB_TOKEN=ghp_your_token_here
```

Get a token at: GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic). Only `public_repo` scope needed.

### Step 5 — Discover repositories

```powershell
python main.py --step discovery
```

Runs 6 star-band queries to bypass GitHub's 1,000-result-per-query cap. Discovers up to ~1,927 active Java repos.

### Step 6 — Download, extract, and build the dataset

Each run processes 50 repos. Run in a loop:

```powershell
# Linux/WSL
for i in $(seq 1 40); do python main.py --step download; done

# PowerShell
for ($i = 1; $i -le 40; $i++) { python main.py --step download }
```

### Step 7 — Preprocess the dataset

```powershell
python main.py --step preprocess
python scripts/extract_diff_pairs.py
```

### Step 8 — Build the BM25 index

```powershell
python main.py --step index
```

Indexes 30,000 training pairs. Index file is ~1.1 GB. Loads in ~8 seconds at server startup.

### Step 9 — Place the CodeT5 model

Download the trained model from Google Drive and place it at:

```
bugfixrecommender/
└── models/
    └── codet5_bugfix/
        ├── notebook.ipynb
        └── final_production_model/
            ├── config.json
            ├── generation_config.json
            ├── model.safetensors
            ├── tokenizer.json
            ├── tokenizer_config.json
            ├── merges.txt
            ├── special_tokens_map.json
            └── vocab.json
```

### Step 10 — Run all tests

```powershell
pytest tests/ -v
```

Expected: 104 passed.

### Step 11 — Start the API server

```powershell
python -m api.server
```

Expected output:

```
BM25 index ready: 30000 pairs | 1130.05 MB
CodeT5 model loaded successfully.
Uvicorn running on http://127.0.0.1:8000
```

### Step 12 — Verify the API

```powershell
# Health check — should show both index_loaded and codet5_loaded as true
curl http://127.0.0.1:8000/health

# BM25 recommendations
curl -X POST http://127.0.0.1:8000/recommend \
  -H "Content-Type: application/json" \
  -d '{"buggy_code": "public void run() { String s = null; s.trim(); }", "top_k": 3}'

# CodeT5 generated fix
curl -X POST http://127.0.0.1:8000/generate-fix \
  -H "Content-Type: application/json" \
  -d '{"buggy_code": "public boolean isEqual(String s1, String s2) { return s1 == s2; }"}'
```

---

## VS Code Extension

### Build and install

```powershell
cd bugfixrecommender/extension
npm install
npm run compile
npx vsce package --no-dependencies
code --install-extension bugfix-recommender-0.1.0.vsix
```

### Use the extension

1. Make sure the API server is running (Step 11)
2. Open any `.java` file in VS Code
3. Select some buggy code
4. Press `Ctrl+Alt+B`
5. A side panel opens showing:
   - **CodeT5 Generated Fix** — AI-generated fix specific to your code with an "Apply This Fix" button
   - **BM25 Similar Cases** — top-5 real historical fixes from GitHub with inline diffs

### Test in debug mode

```powershell
cd bugfixrecommender/extension
code .
# Press F5 → Run Extension → test in the new VS Code window
```

---

## CodeT5 Training (Google Colab)

The model was trained on Google Colab using a T4 GPU.

### Dataset preparation

```powershell
# Run locally after preprocessing
python scripts/extract_diff_pairs.py
```

Upload `train_diff.jsonl`, `val_diff.jsonl`, `test_diff.jsonl` to a Kaggle/Colab dataset.

### Training configuration

| Setting | Value |
|---------|-------|
| Base model | Salesforce/codet5-base |
| Task prefix | `fix java bug:` |
| Max input tokens | 256 |
| Max output tokens | 128 |
| Batch size | 4 (per device) |
| Gradient accumulation | 8 (effective batch = 32) |
| Learning rate | 2e-4 |
| Epochs | 3 |
| Early stopping patience | 3 |
| Training time | ~1.5 hours (T4) |

### Re-training

Open `Untitled2.ipynb` in Google Colab and run all cells in order. The model saves automatically to Google Drive at `/content/drive/MyDrive/java_bugfix_project/codet5_v1/final_production_model`.

---

## Pipeline Steps Reference

| Command | What it does | Time |
|---------|-------------|------|
| `--step discovery` | Find Java repos on GitHub (6 star bands) | ~15 min |
| `--step download` | Clone + extract 50 repos (one batch) | ~30 min each |
| `--step preprocess` | Dedup, filter, split train/val/test | ~15 sec |
| `--step index` | Build BM25 index (30K pairs) | ~5 min |
| `--step query` | Interactive query demo | — |

---

## API Reference

### GET /health

```json
{
  "status": "ok",
  "index_loaded": true,
  "codet5_loaded": true,
  "pairs_indexed": 30000,
  "index_size_mb": 1130.05,
  "version": "2.0.0"
}
```

### POST /recommend

**Request:**
```json
{
  "buggy_code": "public void run() { String s = null; s.trim(); }",
  "top_k": 5
}
```

**Response:**
```json
{
  "results": [
    {
      "rank": 1,
      "score": 79.19,
      "fixed_code": "...",
      "buggy_code": "...",
      "commit_message": "fix: null check before string operation",
      "repo": "apache/tomcat",
      "file_path": "...",
      "pair_id": "uuid"
    }
  ],
  "total_results": 5,
  "query_time_ms": 75.4,
  "pairs_indexed": 30000
}
```

### POST /generate-fix

**Request:**
```json
{
  "buggy_code": "public boolean isEqual(String s1, String s2) { return s1 == s2; }"
}
```

**Response:**
```json
{
  "fixed_code": "public boolean isEqual(String s1, String s2) { return s1.equals(s2); }",
  "model": "codet5-base"
}
```

### GET /docs

Interactive Swagger UI: `http://127.0.0.1:8000/docs`

---

## Project Structure

```
bugfixrecommender/
│
├── config/config.yaml          ← all thresholds and settings
│
├── src/
│   ├── config_loader.py        ← Pydantic v2 config validation
│   ├── utils/logger.py         ← loguru setup
│   ├── discovery/              ← GitHub API repo discovery (6 star bands)
│   ├── downloader/             ← bare git clone + disk guard
│   ├── extractor/              ← commit filter + diff parser + language adapters
│   ├── preprocessing/          ← dedup + quality filter + repo-level splits
│   ├── storage/                ← chunked JSONL writer with resume support
│   └── retrieval/              ← BM25 index + query engine
│
├── api/
│   └── server.py               ← FastAPI: /recommend (BM25) + /generate-fix (CodeT5)
│
├── models/
│   └── codet5_bugfix/
│       └── final_production_model/   ← trained CodeT5 weights
│
├── extension/
│   ├── src/
│   │   ├── extension.ts        ← VS Code command handler
│   │   └── resultsPanel.ts     ← WebView panel (CodeT5 banner + BM25 cards)
│   └── package.json
│
├── scripts/
│   └── extract_diff_pairs.py   ← converts full-file pairs to diff-only pairs
│
├── Untitled2.ipynb             ← CodeT5 training notebook (Google Colab)
├── evaluate.py                 ← BM25 evaluation: Hit@K, MRR, Jaccard
├── main.py                     ← pipeline entry point
└── requirements.txt
```

---

## Hardware Used

- **Local development**: Windows 10 / WSL Ubuntu, 16GB RAM, Intel CPU (no GPU)
- **Model training**: Google Colab T4 (16GB VRAM), ~1.5 hours
- **Inference**: CPU-only, ~1-3 seconds per CodeT5 generation

---

## Known Limitations

- **CodeT5 inference on CPU** takes 1-3 seconds per request — acceptable for a developer tool
- **BM25 index is 1.1 GB** — loads once at startup in ~8 seconds
- **Java only** — Python/JS adapters scaffolded but not activated
- **CodeT5 BLEU 30.68** — good for short diff pairs but may not always produce compilable code; BM25 results serve as a safety net

---

## Upgrade Path

| Version | Model | Status |
|---------|-------|--------|
| V1 | BM25 retrieval (8,555 pairs) | ✅ Complete |
| V2 | BM25 + CodeT5 Seq2Seq (30,000 pairs + 26,754 diff pairs) | ✅ Complete |
| V3 | BM25 + CodeBERT reranker | Planned |
| V4 | CodeT5+ or CodeLlama generative repair | Future |

---

## Troubleshooting

**Server won't start / index not found:**
```powershell
python main.py --step index
python -m api.server
```

**CodeT5 model not loading:**
```
Verify: ls models/codet5_bugfix/final_production_model/
Should contain: model.safetensors, config.json, tokenizer.json
```

**Extension says "server not running":**
```powershell
cd bugfixrecommender
python -m api.server   # keep this terminal open
```

**Ctrl+Alt+B opens something else:**
Go to VS Code → Keyboard Shortcuts → search `bugfix.recommend` → reassign.

**Out of RAM when building index:**
Reduce `max_pairs` in `main.py`:
```python
engine.build_index(str(train_path), max_pairs=15000)
```

---

## License

MIT

---

## Repository

https://github.com/itsmrsarfaraz/bugfixrecommender