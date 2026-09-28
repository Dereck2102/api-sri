import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "snapshots.sqlite3"))
    with TestClient(app) as test_client:
        yield test_client


def test_health_returns_ok(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_sources_endpoint_returns_source_list(client):
    response = client.get("/api/v1/sources")

    assert response.status_code == 200
    assert {source["source_id"] for source in response.json()["sources"]} == {
        "sri_ruc_live",
        "sri_ruc_snapshot",
        "scvs_company_directory",
    }


@pytest.mark.parametrize("ruc", ["123", "12345678901234", "123456789012a"])
def test_invalid_ruc_is_rejected_without_live_lookup(client, ruc):
    response = client.get(f"/api/v1/ruc/{ruc}")

    assert response.status_code == 422
