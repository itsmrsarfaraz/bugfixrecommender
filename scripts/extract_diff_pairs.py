"""
scripts/extract_diff_pairs.py

WHY: Full-file pairs caused FLAN-T5 to learn file reproduction, not bug fixing.
     The actual fix was always truncated away at the 512-token boundary.

WHAT: Extract only changed lines + 3 lines of context from each pair.
      Result: 20-80 line snippets that contain the actual fix.

RUN:  python scripts/extract_diff_pairs.py
"""

import json
from difflib import unified_diff
from pathlib import Path

CONTEXT_LINES   = 3
MAX_BUGGY_CHARS = 600
MIN_BUGGY_CHARS = 10
SPLITS          = ["train", "val", "test"]


def extract_diff_snippet(buggy: str, fixed: str) -> tuple:
    buggy_lines = buggy.splitlines(keepends=True)
    fixed_lines = fixed.splitlines(keepends=True)
    diff = list(unified_diff(buggy_lines, fixed_lines, n=CONTEXT_LINES))

    if not diff:
        return None, None

    buggy_out, fixed_out = [], []
    for line in diff:
        if line.startswith(("---", "+++", "@@")):
            continue
        if line.startswith("-"):
            buggy_out.append(line[1:])
        elif line.startswith("+"):
            fixed_out.append(line[1:])
        else:
            buggy_out.append(line[1:])
            fixed_out.append(line[1:])

    b = "".join(buggy_out).strip()
    f = "".join(fixed_out).strip()

    if not b or not f or b == f:
        return None, None
    return b, f


def process_split(split: str) -> dict:
    inp = Path(f"data/processed/{split}.jsonl")
    out = Path(f"data/processed/{split}_diff.jsonl")

    if not inp.exists():
        print(f"  [SKIP] {inp} not found")
        return {}

    kept = skip_id = skip_long = skip_short = 0

    with open(inp, encoding="utf-8") as fin, \
         open(out, "w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                pair = json.loads(line)
            except Exception:
                continue

            b, f = extract_diff_snippet(
                pair.get("buggy_code", ""),
                pair.get("fixed_code", ""),
            )

            if b is None:
                skip_id += 1
            elif len(b) > MAX_BUGGY_CHARS:
                skip_long += 1
            elif len(b) < MIN_BUGGY_CHARS:
                skip_short += 1
            else:
                fout.write(json.dumps({
                    "repo":           pair.get("repo", ""),
                    "commit_sha":     pair.get("commit_sha", ""),
                    "commit_message": pair.get("commit_message", ""),
                    "file_path":      pair.get("file_path", ""),
                    "buggy_code":     b,
                    "fixed_code":     f,
                    "language":       pair.get("language", "java"),
                    "pair_id":        pair.get("pair_id", ""),
                }, ensure_ascii=False) + "\n")
                kept += 1

    return {"split": split, "kept": kept,
            "skip_id": skip_id, "skip_long": skip_long,
            "skip_short": skip_short, "output": str(out)}


def main():
    print("Extracting diff-only pairs")
    print("=" * 50)
    total = 0
    for split in SPLITS:
        print(f"\n{split}...")
        s = process_split(split)
        if s:
            print(f"  Kept:      {s['kept']:,}")
            print(f"  Identical: {s['skip_id']:,}")
            print(f"  Too long:  {s['skip_long']:,}")
            print(f"  Too short: {s['skip_short']:,}")
            print(f"  → {s['output']}")
            total += s["kept"]

    print(f"\nTotal diff pairs: {total:,}")

    # Show one sample
    p = Path("data/processed/train_diff.jsonl")
    if p.exists():
        sample = json.loads(open(p).readline())
        print(f"\n--- Sample ---")
        print(f"Commit: {sample['commit_message'][:80]}")
        print(f"Buggy:\n{sample['buggy_code'][:250]}")
        print(f"\nFixed:\n{sample['fixed_code'][:250]}")


if __name__ == "__main__":
    main()