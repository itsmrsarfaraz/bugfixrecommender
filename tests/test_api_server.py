"""
test_api_server.py — Unit tests for the FastAPI V2 server.

V2 changes covered:
  - /recommend results now include generated_fix, fix_confidence, fix_source
  - /recommend response now includes generation_time_ms, codet5_available
  - /health response now includes codet5_available
  - retrieved_fix replaces fixed_code in result items

CodeT5 is mocked via patching _run_codet5 — no subprocess or model needed.

Run with: pytest tests/ -v
"""

import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient


# ── Fixtures ──────────────────────────────────────────────────

def make_mock_result(rank=1, score=42.5, repo="apache/kafka"):
    result = MagicMock()
    result.rank           = rank
    result.score          = score
    result.fixed_code     = "public void run() { if(obj != null) obj.call(); }"
    result.buggy_code     = "public void run() { obj.call(); }"
    result.commit_message = "fix: null pointer in run()"
    result.repo           = repo
    result.file_path      = "src/main/java/Runner.java"
    result.pair_id        = f"test-pair-{rank}"
    return result


CODET5_SUCCESS = {
    "generated_fix": "public void run() { if (obj != null) obj.call(); }",
    "confidence": 0.82,
    "fallback": False,
}

CODET5_COMMENT = {
    "generated_fix": "// TODO: add a null check before dereferencing\npublic void run() { obj.call(); }",
    "confidence": 0.09,
    "fallback": True,
}


@pytest.fixture
def client():
    import api.server as server_module
    mock_engine = MagicMock()
    mock_engine.is_ready.return_value = True
    mock_engine.stats.return_value = {
        "loaded": True,
        "pairs_indexed": 8555,
        "index_size_mb": 336.4,
        "k1": 1.5,
        "b": 0.75,
        "index_file": "checkpoints/bm25_index.pkl",
    }
    mock_engine.query.return_value = [
        make_mock_result(rank=1, score=51.7, repo="apache/kafka"),
        make_mock_result(rank=2, score=45.3, repo="netty/netty"),
    ]
    with patch("api.server._run_codet5", return_value=CODET5_SUCCESS):
        with patch("api.server._FIXER_SCRIPT") as mock_path:
            mock_path.exists.return_value = True
            with TestClient(server_module.app, raise_server_exceptions=True) as c:
                server_module._engine = mock_engine
                yield c
    server_module._engine = None


@pytest.fixture
def client_no_index():
    import api.server as server_module
    with TestClient(server_module.app, raise_server_exceptions=True) as c:
        server_module._engine = None
        yield c
    server_module._engine = None


@pytest.fixture
def client_no_codet5():
    """Client where CodeT5 script is missing."""
    import api.server as server_module
    mock_engine = MagicMock()
    mock_engine.is_ready.return_value = True
    mock_engine.stats.return_value = {
        "loaded": True, "pairs_indexed": 8555, "index_size_mb": 336.4,
        "k1": 1.5, "b": 0.75, "index_file": "checkpoints/bm25_index.pkl",
    }
    mock_engine.query.return_value = [make_mock_result(rank=1, score=51.7)]
    with patch("api.server._run_codet5", return_value=None):
        with patch("api.server._FIXER_SCRIPT") as mock_path:
            mock_path.exists.return_value = False
            with TestClient(server_module.app, raise_server_exceptions=True) as c:
                server_module._engine = mock_engine
                yield c
    server_module._engine = None


@pytest.fixture
def client_codet5_comment():
    """Client where CodeT5 runs but returns low-confidence comment."""
    import api.server as server_module
    mock_engine = MagicMock()
    mock_engine.is_ready.return_value = True
    mock_engine.stats.return_value = {
        "loaded": True, "pairs_indexed": 8555, "index_size_mb": 336.4,
        "k1": 1.5, "b": 0.75, "index_file": "checkpoints/bm25_index.pkl",
    }
    mock_engine.query.return_value = [make_mock_result(rank=1, score=51.7)]
    with patch("api.server._run_codet5", return_value=CODET5_COMMENT):
        with patch("api.server._FIXER_SCRIPT") as mock_path:
            mock_path.exists.return_value = True
            with TestClient(server_module.app, raise_server_exceptions=True) as c:
                server_module._engine = mock_engine
                yield c
    server_module._engine = None


# ── Health endpoint ───────────────────────────────────────────

class TestHealthEndpoint:

    def test_health_returns_ok_when_loaded(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["index_loaded"] is True
        assert data["pairs_indexed"] == 8555

    def test_health_contains_codet5_field(self, client):
        response = client.get("/health")
        assert "codet5_available" in response.json()

    def test_health_returns_degraded_when_no_index(self, client_no_index):
        response = client_no_index.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"
        assert data["index_loaded"] is False
        assert data["pairs_indexed"] == 0

    def test_health_contains_version(self, client):
        assert "version" in client.get("/health").json()


# ── Recommend endpoint — V2 response shape ────────────────────

class TestRecommendEndpoint:

    def test_valid_request_returns_200(self, client):
        response = client.post("/recommend", json={
            "buggy_code": "public void run() { obj.call(); }",
            "top_k": 3,
        })
        assert response.status_code == 200

    def test_response_contains_required_top_level_fields(self, client):
        data = client.post("/recommend", json={
            "buggy_code": "public void run() { obj.call(); }",
        }).json()
        assert "results" in data
        assert "total_results" in data
        assert "query_time_ms" in data
        assert "pairs_indexed" in data
        assert "codet5_available" in data

    def test_results_have_v2_fields(self, client):
        results = client.post("/recommend", json={
            "buggy_code": "public void run() { String s = null; s.trim(); }",
        }).json()["results"]
        assert len(results) > 0
        first = results[0]
        # V2 renamed field
        assert "retrieved_fix" in first
        # V2 new fields
        assert "generated_fix" in first
        assert "fix_confidence" in first
        assert "fix_source" in first
        # V1 core fields still present
        assert "rank" in first
        assert "score" in first
        assert "commit_message" in first
        assert "repo" in first
        assert "pair_id" in first

    def test_fix_source_is_codet5_when_model_succeeds(self, client):
        results = client.post("/recommend", json={
            "buggy_code": "public void run() { obj.call(); }",
        }).json()["results"]
        assert results[0]["fix_source"] == "codet5"

    def test_generated_fix_present_when_model_succeeds(self, client):
        results = client.post("/recommend", json={
            "buggy_code": "public void run() { obj.call(); }",
        }).json()["results"]
        assert results[0]["generated_fix"] is not None
        assert len(results[0]["generated_fix"]) > 0

    def test_fix_source_is_retrieval_when_no_codet5(self, client_no_codet5):
        results = client_no_codet5.post("/recommend", json={
            "buggy_code": "public void run() { obj.call(); }",
        }).json()["results"]
        assert results[0]["fix_source"] == "retrieval"
        assert results[0]["generated_fix"] is None

    def test_fix_source_is_comment_when_low_confidence(self, client_codet5_comment):
        results = client_codet5_comment.post("/recommend", json={
            "buggy_code": "public void run() { obj.call(); }",
        }).json()["results"]
        assert results[0]["fix_source"] == "comment"
        assert results[0]["generated_fix"] is not None
        assert "TODO" in results[0]["generated_fix"]

    def test_generation_time_present_when_codet5_runs(self, client):
        data = client.post("/recommend", json={
            "buggy_code": "public void run() { obj.call(); }",
        }).json()
        assert data["generation_time_ms"] is not None

    def test_generation_time_null_when_codet5_unavailable(self, client_no_codet5):
        data = client_no_codet5.post("/recommend", json={
            "buggy_code": "public void run() { obj.call(); }",
        }).json()
        assert data["generation_time_ms"] is None

    def test_results_ordered_by_rank(self, client):
        results = client.post("/recommend", json={
            "buggy_code": "public void connect() { socket.connect(); }",
        }).json()["results"]
        ranks = [r["rank"] for r in results]
        assert ranks == sorted(ranks)

    def test_503_when_no_index(self, client_no_index):
        response = client_no_index.post("/recommend", json={
            "buggy_code": "public void run() { obj.call(); }",
        })
        assert response.status_code == 503

    def test_empty_buggy_code_rejected(self, client):
        assert client.post("/recommend", json={"buggy_code": ""}).status_code == 422

    def test_missing_buggy_code_rejected(self, client):
        assert client.post("/recommend", json={}).status_code == 422

    def test_top_k_above_20_rejected(self, client):
        assert client.post("/recommend", json={
            "buggy_code": "public void run() {}",
            "top_k": 99,
        }).status_code == 422

    def test_top_k_zero_rejected(self, client):
        assert client.post("/recommend", json={
            "buggy_code": "public void run() {}",
            "top_k": 0,
        }).status_code == 422

    def test_query_time_is_a_number(self, client):
        qt = client.post("/recommend", json={
            "buggy_code": "public void run() { return null; }",
        }).json()["query_time_ms"]
        assert isinstance(qt, (int, float)) and qt >= 0

    def test_codet5_available_true_when_script_exists(self, client):
        data = client.post("/recommend", json={
            "buggy_code": "public void run() {}",
        }).json()
        assert data["codet5_available"] is True

    def test_codet5_available_false_when_script_missing(self, client_no_codet5):
        data = client_no_codet5.post("/recommend", json={
            "buggy_code": "public void run() {}",
        }).json()
        assert data["codet5_available"] is False


# ── Root endpoint ─────────────────────────────────────────────

class TestRootEndpoint:

    def test_root_returns_200(self, client):
        assert client.get("/").status_code == 200

    def test_root_contains_docs_url(self, client):
        assert "docs" in client.get("/").json()

    def test_root_mentions_v2(self, client):
        data = client.get("/").json()
        # V2 key in root response
        assert "new_in_v2" in data or "2.0" in data.get("message", "")