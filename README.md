# Bug Fix Recommender

A production-grade pipeline that mines bug-fix commits from GitHub, builds a structured training dataset, and serves fix recommendations via a BM25 retrieval engine, FastAPI backend, and VS Code extension.

**Thesis project** — demonstrates an end-to-end ML data pipeline from raw GitHub data to a working developer tool, without requiring a GPU.

---

## System Architecture

```
GitHub API (200 Java repos)
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
        ▼
[BM25 Index]      rank-bm25 over 8,555 training pairs
        │
        ▼
[FastAPI Server]  POST /recommend → top-K fix suggestions
        │
        ▼
[VS Code Ext]     Ctrl+Alt+B → WebView results panel
```

---

## Evaluation Results

| Metric                        | Value       |
| ----------------------------- | ----------- |
| Self-retrieval Hit@1          | **94%**     |
| Self-retrieval Hit@5          | **100%**    |
| MRR                           | **0.9648**  |
| Cross-repo Jaccard Top-1      | **0.200**   |
| Meaningful match >20% @ Top-1 | **44.5%**   |
| API query time                | **~75ms**   |
| Tests passing                 | **104/104** |

---

## Dataset Statistics

| Stat                     | Value                        |
| ------------------------ | ---------------------------- |
| Repos processed          | ~160 of 200                  |
| Raw extracted pairs      | 12,490                       |
| After cleaning           | 11,883                       |
| Training pairs (indexed) | 8,555                        |
| Validation pairs         | 1,367                        |
| Test pairs               | 1,961                        |
| Split method             | By repository (zero leakage) |

---

## Requirements

- Python 3.11.9
- Node.js 18+ (for VS Code extension only)
- Git installed and on PATH
- VS Code 1.85+
- 8GB RAM minimum (BM25 index is ~336MB in memory)
- GitHub Personal Access Token

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
venv\Scripts\activate
```

### Step 3 — Install Python dependencies

```powershell
pip install -r requirements.txt
```

### Step 4 — Set your GitHub token

Get a token at: GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic).
Only `public_repo` scope is needed.

Creat a .env at the root and paste it there.

```powershell
GITHUB_TOKEN = "ghp_your_token_here"
```

> To make this permanent across sessions, add it to your Windows environment variables:
> System → Advanced system settings → Environment Variables → New → `GITHUB_TOKEN`

### Step 5 — Discover repositories

Finds 200 Java repositories on GitHub matching the quality filters.
Runs once. Takes about 7–8 minutes.

```powershell
python main.py --step discovery
```

Expected output:

```
[1/200] Discovered: Snailclimb/JavaGuide ★155698
[2/200] Discovered: krahets/hello-algo ★126208
...
[200/200] Discovered: react-native-image-picker/... ★8636
Discovery complete. 200 repos ready.
```

### Step 6 — Download, extract, and build the dataset

This is the most time-consuming step. Each run processes **20 repos** (one batch), clones them, extracts bug-fix pairs, then deletes the clone to save disk space.

**You need to run this step 10 times** to process all 200 repos:

```powershell
# Run 1 of 10
python main.py --step download

# Run 2 of 10
python main.py --step download

# ... repeat until you see:
# "Downloader starting. 200 discovered. 200 already processed."
```

> **How many runs do you actually need?**
>
> - 200 repos ÷ 20 per batch = **10 runs minimum**
> - Some repos are automatically skipped (docs, tutorials, too large)
>   so you may finish in 8–9 runs
> - Each run takes 15–45 minutes depending on repo sizes
> - The pipeline is fully resumable — if interrupted, re-run and it picks up where it stopped
> - You can check progress: look for `"X already processed"` in the log output
>
> **Total estimated time:** 3–6 hours for all 200 repos

To check how many repos have been processed so far:

```powershell
python -c "
import json; from pathlib import Path
data = json.loads(Path('checkpoints/cloned_repos.json').read_text(encoding='utf-8'))
processed = sum(1 for v in data.values() if v.get('status') == 'processed')
skipped   = sum(1 for v in data.values() if v.get('status') == 'skipped')
print(f'Processed: {processed} | Skipped: {skipped} | Remaining: {200 - processed - skipped}')
"
```

### Step 7 — Preprocess the dataset

Deduplicates, applies quality filters, and creates the train/val/test split.
Runs in ~15 seconds.

```powershell
python main.py --step preprocess
```

Expected output:

```
Clean dataset: train=8555 | val=1367 | test=1961 | dropped=607
```

Check dataset statistics:

```powershell
python -c "
import json; from pathlib import Path
s = json.loads(Path('data/processed/dataset_stats.json').read_text(encoding='utf-8'))
print(f'Raw:     {s[\"raw_total\"]}')
print(f'Clean:   {s[\"clean_total\"]}')
print(f'Train:   {s[\"train_pairs\"]}')
print(f'Val:     {s[\"val_pairs\"]}')
print(f'Test:    {s[\"test_pairs\"]}')
print(f'Dropped: {s[\"dropped_total\"]}')
"
```

### Step 8 — Build the BM25 index

Tokenises all training pairs and builds the retrieval index.
Runs in ~16 seconds. Index file is ~336MB.

```powershell
python main.py --step index
```

Expected output:

```
Index ready. 8555 pairs indexed. File size: 336.4 MB
```

### Step 9 — Run all tests

Verify everything is working correctly.

```powershell
pytest tests/ -v
# Expected: 104 passed
```

### Step 10 — Start the API server

```powershell
python -m api.server
```

Expected output:

```
BM25 index ready: 8555 pairs | 336.39 MB
Uvicorn running on http://127.0.0.1:8000
```

### Step 11 — Verify the API

Open a second PowerShell terminal:

```powershell
# Health check
curl http://127.0.0.1:8000/health

# Query for fix recommendations
$body = '{"buggy_code": "public void run() { String s = null; s.trim(); }", "top_k": 5}'
Invoke-RestMethod -Uri http://127.0.0.1:8000/recommend -Method Post -ContentType "application/json" -Body $body
```

Or open the interactive docs in your browser: `http://127.0.0.1:8000/docs`

### Step 12 — Interactive query demo (optional)

```powershell
python main.py --step query
```

Paste any Java code snippet and get ranked fix recommendations.

### Step 13 — Run evaluation

```powershell
# Self-retrieval metric (proves retrieval works)
python evaluate.py --metric self --sample 200 --top-k 5

# Cross-repo similarity metric (real-world usefulness)
python evaluate.py --metric similarity --sample 200 --top-k 5

# Both metrics together
python evaluate.py --metric both --sample 200 --top-k 5
```

---

## VS Code Extension

### Build and install the extension

```powershell
# Step 1: Go to extension folder
cd bugfixrecommender\extension

# Step 2: Install dependencies
npm install

# Step 3: Compile TypeScript
npm run compile

# Step 4: Package as .vsix
npx vsce package --no-dependencies

# Step 5: Install into VS Code
code --install-extension bugfix-recommender-0.1.0.vsix

# Step 6: Reload VS Code when prompted
```

### Use the extension

1. Make sure the API server is running (Step 10 above)
2. Open any `.java` file in VS Code
3. Select some buggy code
4. Press `Ctrl+Alt+B` — OR — right-click → **Bug Fix: Get Recommendations for Selection**
5. A side panel opens with the top-5 ranked fix suggestions

### Test the extension in debug mode (without installing)

```powershell
cd bugfixrecommender\extension
code .
# In VS Code: Ctrl+Shift+D → select "Run Extension" → press F5
# A second VS Code window opens — test there
```

---

## Pipeline Steps Reference

| Command             | What it does                         | Runs                       | Time         |
| ------------------- | ------------------------------------ | -------------------------- | ------------ |
| `--step discovery`  | Find 200 Java repos on GitHub        | Once                       | ~8 min       |
| `--step download`   | Clone + extract 20 repos (one batch) | **10 times**               | ~30 min each |
| `--step preprocess` | Dedup, filter, split train/val/test  | Once (after all downloads) | ~15 sec      |
| `--step index`      | Build BM25 index from train.jsonl    | Once (after preprocess)    | ~16 sec      |
| `--step query`      | Interactive query demo               | Anytime                    | —            |
| `--step all`        | Run everything in sequence           | Fresh start only           | —            |

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
│   ├── discovery/              ← GitHub API repo discovery
│   ├── downloader/             ← bare git clone + disk guard
│   ├── extractor/              ← commit filter + diff parser + language adapters
│   ├── preprocessing/          ← dedup + quality filter + repo-level splits
│   ├── storage/                ← chunked JSONL writer with resume support
│   └── retrieval/              ← BM25 index + query engine
│
├── api/
│   └── server.py               ← FastAPI HTTP server
│
├── extension/
│   ├── src/
│   │   ├── extension.ts        ← VS Code command handler
│   │   └── resultsPanel.ts     ← WebView results panel
│   ├── package.json
│   ├── tsconfig.json
│   └── .vscode/
│       ├── launch.json         ← F5 debug config
│       └── tasks.json          ← build task
│
├── data/
│   ├── raw/                    ← cloned repos (auto-deleted after extract)
│   ├── extracted/              ← bug-fix pair chunks (gitignored)
│   └── processed/              ← train/val/test splits (gitignored)
│
├── checkpoints/                ← resume state + BM25 index (gitignored)
├── logs/
├── tests/                      ← 104 unit tests
│
├── evaluate.py                 ← Hit@K + MRR + Jaccard evaluation
├── main.py                     ← pipeline entry point
└── requirements.txt
```

---

## API Reference

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
      "score": 42.31,
      "fixed_code": "public void run() { if (s != null) s.trim(); }",
      "buggy_code": "...",
      "commit_message": "fix: null check before string operation",
      "repo": "apache/kafka",
      "file_path": "src/main/java/Runner.java",
      "pair_id": "uuid"
    }
  ],
  "total_results": 5,
  "query_time_ms": 75.4,
  "pairs_indexed": 8555
}
```

### GET /health

Returns index status, pair count, and server version.

### GET /docs

Interactive Swagger UI for manual testing.

---

## Configuration

All thresholds are in `config/config.yaml`. Key settings:

```yaml
github:
  min_stars: 100
  min_activity_days: 365
  max_repos: 200
  language: Java

downloader:
  batch_size: 20 # repos per run — change to 50 for faster processing
  max_repo_size_mb: 500
  clone_timeout_seconds: 300

extractor:
  max_diff_lines: 200
  max_files_changed: 5

storage:
  chunk_size: 1000
```

---

## Hardware Used

- Windows 10
- 16GB RAM
- Intel CPU (no GPU used at any stage)
- ~200GB disk
- Training: CPU-only, no GPU required

---

## Known Limitations (V1)

- **336MB RAM** for BM25 index (loads once at server startup)
- **Java only** — Python/JS adapters scaffolded but not activated
- **Repo dominance** — repos with many pairs can dominate results. Fix: add `max_results_per_repo=2` cap
- **Lexical only** — BM25 matches tokens, not semantics. V2 adds CodeBERT reranker
- **English commit messages** — Chinese-language repos yield fewer pairs

---

## Upgrade Path

| Version | Model                    | Requirement               | Status      |
| ------- | ------------------------ | ------------------------- | ----------- |
| V1      | BM25 retrieval           | CPU only                  | ✅ Complete |
| V2      | BM25 + CodeBERT reranker | CPU (slow) or GPU         | Planned     |
| V3      | Fine-tuned CodeBERT      | 50K+ pairs, CPU trainable | Future      |
| V4      | CodeT5 generative repair | GPU required              | Research    |

---

## Troubleshooting

**Server won't start / index not found:**

```powershell
python main.py --step index
python -m api.server
```

**Extension says "server not running":**

```powershell
cd bugfixrecommender
python -m api.server   ← keep this terminal open
```

**Ctrl+Alt+B opens something else:**
Go to VS Code settings → Keyboard Shortcuts → search `bugfix.recommend` → reassign.

**Download keeps timing out on large repos:**
The pipeline retries automatically. If a repo fails both attempts it is skipped and logged. Re-run `--step download` to continue with the next batch.

**Pytest shows import errors:**

```powershell
cd bugfixrecommender
venv\Scripts\activate
pip install -r requirements.txt
```

---

## License

MIT

---

## Repository

https://github.com/itsmrsarfaraz/bugfixrecommender
