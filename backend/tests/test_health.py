from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_unversioned() -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "service" in body
    assert "environment" in body
    assert "version" in body


def test_health_versioned() -> None:
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_db_when_reachable() -> None:
    """Requires Postgres to be running. See README ("Running the project") - will
    be skipped automatically if the DB is not reachable."""
    resp = client.get("/api/v1/health/db")
    if resp.status_code == 503:
        import pytest

        pytest.skip("Postgres not reachable in this environment")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "reachable"}


def test_docs_available() -> None:
    resp = client.get("/api/docs")
    assert resp.status_code == 200