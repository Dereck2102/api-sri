import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import store

TEST_TOKEN = "qa_test_integration_token_that_is_never_used_outside_tests"
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "snapshots.sqlite3"))
    with TestClient(app) as test_client:
        now = "2026-09-01T00:00:00+00:00"
        with store.connect() as db:
            db.execute(
                "INSERT INTO organizations VALUES (?, ?, ?, ?, ?)",
                ("org-test", "Test organization", "active", now, now),
            )
            db.execute(
                "INSERT INTO integrations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("integration-test", "org-test", "Test integration", "active", '["ruc:read"]', 30, 1000, now, now),
            )
            db.execute(
                "INSERT INTO integration_credentials (credential_id, integration_id, secret_hash, created_at) VALUES (?, ?, ?, ?)",
                ("credential-test", "integration-test", hashlib.sha256(TEST_TOKEN.encode()).hexdigest(), now),
            )
        yield test_client


def test_health_returns_ok(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_sources_requires_a_valid_integration_credential(client):
    anonymous = client.get("/api/v1/sources")
    authenticated = client.get("/api/v1/sources", headers=AUTH_HEADERS)

    assert anonymous.status_code == 401
    assert anonymous.headers["www-authenticate"] == "Bearer"
    assert authenticated.status_code == 200


def test_anonymous_ruc_query_is_rejected_before_ruc_validation(client):
    response = client.get("/api/v1/ruc/not-a-ruc")

    assert response.status_code == 401


def test_sources_endpoint_returns_source_list(client):
    response = client.get("/api/v1/sources", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert {source["source_id"] for source in response.json()["sources"]} == {
        "sri_ruc_live",
        "sri_ruc_snapshot",
        "scvs_company_directory",
    }


@pytest.mark.parametrize("ruc", ["123", "12345678901234", "123456789012a"])
def test_invalid_ruc_is_rejected_without_live_lookup(client, ruc):
    response = client.get(f"/api/v1/ruc/{ruc}", headers=AUTH_HEADERS)

    assert response.status_code == 422


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_interactive_docs_and_openapi_are_not_public(client, path):
    assert client.get(path).status_code == 404


def test_ruc_query_does_not_call_the_live_sri(client, monkeypatch):
    async def fail_if_called(_ruc):
        pytest.fail("La API de integración no debe consultar el SRI en vivo.")

    monkeypatch.setattr("app.main.sri_live.lookup", fail_if_called)
    response = client.get("/api/v1/ruc/1790013731001", headers=AUTH_HEADERS)

    assert response.status_code == 404


def test_ruc_query_returns_the_saved_snapshot_without_calling_sri(client, monkeypatch):
    with store.connect() as db:
        db.execute(
            "INSERT INTO sri_ruc_snapshot (ruc, legal_name, tax_status, source_period, observed_at) VALUES (?, ?, ?, ?, ?)",
            ("1790013731001", "Empresa de prueba", "ACTIVO", "2026", "2026-09-01T00:00:00+00:00"),
        )

    async def fail_if_called(_ruc):
        pytest.fail("La API de integración no debe consultar el SRI en vivo.")

    monkeypatch.setattr("app.main.sri_live.lookup", fail_if_called)
    response = client.get("/api/v1/ruc/1790013731001", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json()["record"]["legal_name"]["value"] == "Empresa de prueba"
    assert response.json()["record"]["legal_name"]["freshness"] == "snapshot"


def test_integration_without_read_permission_is_forbidden(client):
    with store.connect() as db:
        db.execute(
            "UPDATE integrations SET permissions = ? WHERE integration_id = ?",
            (json.dumps(["admin:read"]), "integration-test"),
        )

    response = client.get("/api/v1/sources", headers=AUTH_HEADERS)

    assert response.status_code == 403


def test_expired_credential_is_rejected(client):
    with store.connect() as db:
        db.execute(
            "UPDATE integration_credentials SET expires_at = ? WHERE credential_id = ?",
            ("2000-01-01T00:00:00+00:00", "credential-test"),
        )

    response = client.get("/api/v1/sources", headers=AUTH_HEADERS)

    assert response.status_code == 401


def test_revoked_integration_credential_is_rejected(client):
    with store.connect() as db:
        db.execute(
            "UPDATE integration_credentials SET status = 'revoked' WHERE credential_id = ?",
            ("credential-test",),
        )

    response = client.get("/api/v1/sources", headers=AUTH_HEADERS)

    assert response.status_code == 401


def test_revoked_organization_credential_is_rejected(client):
    with store.connect() as db:
        db.execute(
            "UPDATE organizations SET status = 'revoked' WHERE organization_id = ?",
            ("org-test",),
        )

    response = client.get("/api/v1/sources", headers=AUTH_HEADERS)

    assert response.status_code == 401
