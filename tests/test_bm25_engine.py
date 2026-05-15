"""
test_bm25_engine.py — Unit tests for BM25Engine.

All tests are fully offline — no network, no real dataset needed.
Tests build a tiny in-memory index to verify retrieval behaviour.

Run with: pytest tests/ -v
"""

import json
import pytest
from pathlib import Path

from src.retrieval.bm25_engine import BM25Engine, BugFixResult


# ── Helpers ───────────────────────────────────────────────────

def make_training_pair(
    pair_id: str,
    buggy: str,
    fixed: str,
    repo: str = "apache/kafka",
    file_path: str = "Foo.java",
    commit_msg: str = "fix: null pointer",
) -> dict:
    return {
        "pair_id":        pair_id,
        "repo":           repo,
        "file_path":      file_path,
        "commit_message": commit_msg,
        "buggy_code":     buggy,
        "fixed_code":     fixed,
        "diff_lines_added":   1,
        "diff_lines_removed": 1,
        "language":       "java",
        "repo_stars":     1000,
    }


def write_train_file(path: Path, pairs: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for pair in pairs:
            f.write(json.dumps(pair) + "\n")


@pytest.fixture
def tiny_engine(tmp_path):
    """
    Build a small BM25Engine over 5 hand-crafted pairs.
    Returns the loaded engine ready to query.
    """
    train_file = tmp_path / "train.jsonl"
    pairs = [
        make_training_pair(
            "id-1",
            buggy="public void processPayment() { double amount = null; amount.toString(); }",
            fixed="public void processPayment() { double amount = 0.0; if(amount != null) amount.toString(); }",
            commit_msg="fix: NullPointerException in payment processing",
        ),
        make_training_pair(
            "id-2",
            buggy="public List getUsers() { return null; }",
            fixed="public List getUsers() { return Collections.emptyList(); }",
            commit_msg="fix: return empty list instead of null",
        ),
        make_training_pair(
            "id-3",
            buggy="public void connect() { socket.connect(host, port); }",
            fixed="public void connect() { if(socket != null) socket.connect(host, port); }",
            commit_msg="fix: socket null check before connect",
            repo="netty/netty",
        ),
        make_training_pair(
            "id-4",
            buggy="public String serialize(Object obj) { return mapper.writeValueAsString(obj); }",
            fixed="public String serialize(Object obj) { if(obj == null) return null; return mapper.writeValueAsString(obj); }",
            commit_msg="fix: handle null input in serializer",
            repo="google/gson",
        ),
        make_training_pair(
            "id-5",
            buggy="public void updateCache(String key, Object value) { cache.put(key, value); }",
            fixed="public void updateCache(String key, Object value) { if(key != null) cache.put(key, value); }",
            commit_msg="fix: null key causes cache corruption",
            repo="ben-manes/caffeine",
        ),
    ]
    write_train_file(train_file, pairs)

    engine = BM25Engine(index_dir=str(tmp_path))
    engine.build_index(str(train_file))
    return engine


# ── Tokenization ──────────────────────────────────────────────

class TestTokenization:

    def test_basic_java_code_tokenized(self):
        tokens = BM25Engine._tokenize("public void main() { return null; }")
        assert "public" in tokens
        assert "void" in tokens
        assert "main" in tokens
        assert "null" in tokens

    def test_single_char_tokens_filtered(self):
        tokens = BM25Engine._tokenize("{ } ; ( ) . , !")
        # Single chars should be filtered out
        assert all(len(t) >= 2 for t in tokens)

    def test_tokens_are_lowercase(self):
        tokens = BM25Engine._tokenize("NullPointerException IOException")
        assert "nullpointerexception" in tokens
        assert "ioexception" in tokens

    def test_empty_code_returns_empty(self):
        assert BM25Engine._tokenize("") == []
        assert BM25Engine._tokenize("   ") == []

    def test_numbers_included(self):
        tokens = BM25Engine._tokenize("int count = 42;")
        assert "42" in tokens
        assert "count" in tokens


# ── Index building ────────────────────────────────────────────

class TestIndexBuilding:

    def test_build_creates_index_file(self, tmp_path):
        train_file = tmp_path / "train.jsonl"
        pairs = [make_training_pair("id-1", buggy="public void run() {}", fixed="public void run() { log(); }")]
        write_train_file(train_file, pairs)

        engine = BM25Engine(index_dir=str(tmp_path))
        engine.build_index(str(train_file))

        assert (tmp_path / "bm25_index.pkl").exists()

    def test_index_contains_correct_pair_count(self, tiny_engine):
        assert tiny_engine.stats()["pairs_indexed"] == 5

    def test_missing_train_file_raises(self, tmp_path):
        engine = BM25Engine(index_dir=str(tmp_path))
        with pytest.raises(FileNotFoundError):
            engine.build_index(str(tmp_path / "nonexistent.jsonl"))

    def test_is_ready_after_build(self, tiny_engine):
        assert tiny_engine.is_ready() is True


# ── Index persistence ─────────────────────────────────────────

class TestIndexPersistence:

    def test_save_and_load_round_trip(self, tmp_path):
        train_file = tmp_path / "train.jsonl"
        pairs = [
            make_training_pair("id-1", buggy="public void fix() { return null; }", fixed="public void fix() { return 0; }"),
            make_training_pair("id-2", buggy="public void run() { socket.connect(); }", fixed="public void run() { if(socket != null) socket.connect(); }"),
        ]
        write_train_file(train_file, pairs)

        # Build and save
        engine1 = BM25Engine(index_dir=str(tmp_path))
        engine1.build_index(str(train_file))

        # Load fresh instance
        engine2 = BM25Engine(index_dir=str(tmp_path))
        engine2.load_index()

        assert engine2.is_ready()
        assert engine2.stats()["pairs_indexed"] == 2

    def test_load_missing_index_raises(self, tmp_path):
        engine = BM25Engine(index_dir=str(tmp_path / "empty"))
        with pytest.raises(FileNotFoundError):
            engine.load_index()

    def test_query_before_load_raises(self, tmp_path):
        engine = BM25Engine(index_dir=str(tmp_path))
        with pytest.raises(RuntimeError):
            engine.query("public void run() {}")


# ── Retrieval quality ─────────────────────────────────────────

class TestRetrieval:

    def test_query_returns_results(self, tiny_engine):
        results = tiny_engine.query("public void processPayment() { double amount = null; }")
        assert len(results) > 0

    def test_results_are_bugfix_results(self, tiny_engine):
        results = tiny_engine.query("public void getUsers() { return null; }")
        for r in results:
            assert isinstance(r, BugFixResult)
            assert r.fixed_code
            assert r.repo
            assert r.rank >= 1

    def test_results_ranked_by_score(self, tiny_engine):
        results = tiny_engine.query("public void getUsers() { return null; }", top_k=3)
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_null_pointer_query_finds_null_fixes(self, tiny_engine):
        """
        Query about NullPointerException should rank null-related fixes higher.
        Not a strict test — BM25 is probabilistic. We check top result is relevant.
        """
        results = tiny_engine.query(
            "public void process() { String value = null; value.length(); }",
            top_k=5,
        )
        assert len(results) > 0
        # Top result should mention null handling
        top_fixed = results[0].fixed_code.lower()
        assert "null" in top_fixed

    def test_top_k_respected(self, tiny_engine):
        results = tiny_engine.query("public void run() {}", top_k=2)
        assert len(results) <= 2

    def test_empty_query_returns_empty(self, tiny_engine):
        results = tiny_engine.query("")
        assert results == []

    def test_whitespace_only_query_returns_empty(self, tiny_engine):
        results = tiny_engine.query("   ")
        assert results == []

    def test_result_to_dict_is_serialisable(self, tiny_engine):
        import json
        results = tiny_engine.query("public void connect() { socket.connect(); }")
        if results:
            d = results[0].to_dict()
            serialised = json.dumps(d)
            assert "fixed_code" in serialised
            assert "score" in serialised

    def test_zero_score_results_excluded(self, tiny_engine):
        """A completely unrelated query should return 0 or low-score results."""
        results = tiny_engine.query("SELECT * FROM users WHERE id = 1")
        # All results (if any) must have score > 0
        for r in results:
            assert r.score > 0


# ── Stats ─────────────────────────────────────────────────────

class TestStats:

    def test_stats_returns_expected_keys(self, tiny_engine):
        s = tiny_engine.stats()
        assert "loaded" in s
        assert "pairs_indexed" in s
        assert "k1" in s
        assert "b" in s

    def test_stats_before_load(self, tmp_path):
        engine = BM25Engine(index_dir=str(tmp_path))
        s = engine.stats()
        assert s["loaded"] is False