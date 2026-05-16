"""
test_api_server.py — Unit tests for the FastAPI server.

Uses FastAPI's TestClient so no real server process is needed.
The BM25 engine is mocked — tests verify API contract, not retrieval quality.

Run with: pytest tests/ -v
"""

import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient


# ── Fixtures ──────────────────────────────────────────────────

def make_mock_result(rank=1, score=42.5, repo="apache/kafka"):
    """Build a mock BugFixResult object."""
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


@pytest.fixture
def client():
    """
    TestClient with a mocked BM25 engine.
    We patch the global _engine in api.server so the index
    never needs to exist on disk for tests to pass.
    """
    # Import here so the module-level lifespan doesn't fire
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

    # Inject the mock engine directly — bypass lifespan/startup
    server_module._engine = mock_engine

    with TestClient(server_module.app, raise_server_exceptions=True) as c:
        yield c

    # Clean up
    server_module._engine = None


@pytest.fixture
def client_no_index():
    """TestClient simulating a server where index hasn't been built."""
    import api.server as server_module
    server_module._engine = None

    with TestClient(server_module.app, raise_server_exceptions=True) as c:
        yield c

    server_module._engine = None


# ── Health check ──────────────────────────────────────────────

class TestHealthEndpoint:

    def test_health_returns_ok_when_loaded(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["index_loaded"] is True
        assert data["pairs_indexed"] == 8555

    def test_health_returns_degraded_when_no_index(self, client_no_index):
        response = client_no_index.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"
        assert data["index_loaded"] is False
        assert data["pairs_indexed"] == 0

    def test_health_contains_version(self, client):
        response = client.get("/health")
        assert "version" in response.json()


# ── Recommend endpoint ────────────────────────────────────────

class TestRecommendEndpoint:

    def test_valid_request_returns_200(self, client):
        response = client.post("/recommend", json={
            "buggy_code": "public void run() { obj.call(); }",
            "top_k": 3,
        })
        assert response.status_code == 200

    def test_response_contains_results(self, client):
        response = client.post("/recommend", json={
            "buggy_code": "public void run() { obj.call(); }",
        })
        data = response.json()
        assert "results" in data
        assert "total_results" in data
        assert "query_time_ms" in data
        assert "pairs_indexed" in data

    def test_results_have_correct_fields(self, client):
        response = client.post("/recommend", json={
            "buggy_code": "public void run() { String s = null; s.trim(); }",
        })
        results = response.json()["results"]
        assert len(results) > 0
        first = results[0]
        assert "rank" in first
        assert "score" in first
        assert "fixed_code" in first
        assert "buggy_code" in first
        assert "commit_message" in first
        assert "repo" in first
        assert "file_path" in first
        assert "pair_id" in first

    def test_results_ordered_by_rank(self, client):
        response = client.post("/recommend", json={
            "buggy_code": "public void connect() { socket.connect(); }",
        })
        results = response.json()["results"]
        ranks = [r["rank"] for r in results]
        assert ranks == sorted(ranks)

    def test_top_k_default_is_5(self, client):
        """Default top_k should be 5 even if not specified."""
        import api.server as server_module
        assert server_module._engine is not None
        mock_query = MagicMock(return_value=[
            make_mock_result(rank=i) for i in range(1, 6)
        ])

        server_module._engine.query = mock_query
        response = client.post("/recommend", json={
            "buggy_code": "public void run() {}",
        })
        # Verify query was called with default top_k=5
        call_args = mock_query.call_args
        assert call_args.kwargs.get("top_k", call_args.args[1] if len(call_args.args) > 1 else 5) == 5

    def test_503_when_no_index(self, client_no_index):
        """Should return 503 Service Unavailable if index is not loaded."""
        response = client_no_index.post("/recommend", json={
            "buggy_code": "public void run() { obj.call(); }",
        })
        assert response.status_code == 503

    def test_empty_buggy_code_rejected(self, client):
        """Empty string should fail Pydantic validation -> 422."""
        response = client.post("/recommend", json={
            "buggy_code": "",
        })
        assert response.status_code == 422

    def test_missing_buggy_code_rejected(self, client):
        """Missing required field -> 422 Unprocessable Entity."""
        response = client.post("/recommend", json={})
        assert response.status_code == 422

    def test_top_k_above_20_rejected(self, client):
        """top_k > 20 should fail validation."""
        response = client.post("/recommend", json={
            "buggy_code": "public void run() {}",
            "top_k": 99,
        })
        assert response.status_code == 422

    def test_top_k_zero_rejected(self, client):
        response = client.post("/recommend", json={
            "buggy_code": "public void run() {}",
            "top_k": 0,
        })
        assert response.status_code == 422

    def test_query_time_is_a_number(self, client):
        response = client.post("/recommend", json={
            "buggy_code": "public void run() { return null; }",
        })
        qt = response.json()["query_time_ms"]
        assert isinstance(qt, (int, float))
        assert qt >= 0


# ── Root endpoint ─────────────────────────────────────────────

class TestRootEndpoint:

    def test_root_returns_200(self, client):
        response = client.get("/")
        assert response.status_code == 200

    def test_root_contains_docs_url(self, client):
        data = response = client.get("/")
        assert "docs" in response.json()