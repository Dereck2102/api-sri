import hashlib
import json
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.admin import _PASSWORD_HASHER
from app import store

TEST_TOKEN = "qa_test_integration_token_that_is_never_used_outside_tests"
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "snapshots.sqlite3"))
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-only-session-secret-with-more-than-32-bytes")
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


def admin_login(client):
    password = "first-owner-password-long-enough"
    if not store.admin_account("first-owner@example.test"):
        store.admin_create_initial_owner(
            "first-owner@example.test",
            _PASSWORD_HASHER.hash(password),
            "2026-09-01T00:00:00+00:00",
        )
    return admin_login_as(client, "first-owner@example.test", password)


def admin_login_as(client, email, password):
    if not store.admin_account(email):
        with store.connect() as db:
            role = "owner" if email == "first-owner@example.test" else "support"
            db.execute(
                "INSERT INTO admin_accounts (email, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                (email, _PASSWORD_HASHER.hash(password), role, "2026-09-01T00:00:00+00:00"),
            )
    response = client.post(
        "/admin/login",
        data={"email": email, "password": password},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "httponly" in response.headers["set-cookie"].casefold()
    dashboard = client.get("/admin")
    csrf = re.search(r'name="csrf" value="([^"]+)"', dashboard.text)
    assert dashboard.status_code == 200
    assert csrf
    return csrf.group(1)


def test_health_returns_ok(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_home_page_shows_service_information(client):
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "API RUC Ecuador" in response.text
    assert "snapshots guardados" in response.text


def test_unknown_web_page_shows_friendly_not_found_page(client):
    response = client.get("/ruta-inexistente")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "Esta dirección no existe" in response.text
    assert '{"detail":"Not Found"}' not in response.text


def test_unknown_api_route_keeps_json_not_found_response(client):
    response = client.get("/api/v1/no-existe")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "Not Found"}


def test_api_unauthorized_keeps_json_and_bearer_challenge(client):
    response = client.get("/api/v1/sources")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "Se requiere una credencial de integración válida."}


def test_unauthorized_web_page_is_friendly_and_links_to_admin_login(client):
    response = client.get("/api/v1/sources", headers={"Accept": "text/html"})

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("text/html")
    assert "Necesitas autorización para acceder" in response.text
    assert 'href="/admin/login"' in response.text


def test_admin_login_route_offers_initial_setup(client):
    response = client.get("/admin/login")

    assert response.status_code == 200
    assert "Crear cuenta propietaria" in response.text


def test_admin_panel_offers_initial_owner_setup_when_no_owner_exists(client):
    response = client.get("/admin")

    assert response.status_code == 200
    assert "Panel Maestro" in response.text
    assert "Crear cuenta propietaria" in response.text
    assert "Set-Cookie" not in response.headers


@pytest.mark.parametrize("session_secret", [None, "replace-with-at-least-32-random-bytes"])
def test_admin_initial_setup_requires_a_valid_session_secret_before_registration(client, monkeypatch, session_secret):
    if session_secret is None:
        monkeypatch.delenv("ADMIN_SESSION_SECRET")
    else:
        monkeypatch.setenv("ADMIN_SESSION_SECRET", session_secret)

    login_page = client.get("/admin/login")
    assert "define ADMIN_SESSION_SECRET" in login_page.text
    assert "Crear cuenta propietaria" not in login_page.text

    response = client.post(
        "/admin/setup",
        data={
            "email": "owner@example.test",
            "password": "owner-password-long-enough",
            "password_confirmation": "owner-password-long-enough",
        },
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "define ADMIN_SESSION_SECRET" in response.text
    assert not store.admin_accounts_exist()


def test_first_visit_offers_one_time_owner_registration_and_locks_it(client):
    client.cookies.clear()
    with store.connect() as db:
        db.execute("DELETE FROM admin_accounts")
    login_page = client.get("/admin/login")
    assert "Crear cuenta propietaria" in login_page.text

    created = client.post(
        "/admin/setup",
        data={
            "email": "first-owner@example.test",
            "password": "first-owner-password-long-enough",
            "password_confirmation": "first-owner-password-long-enough",
        },
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert created.headers["location"] == "/admin"
    assert "api_sri_admin_session=" in created.headers["set-cookie"]
    assert "httponly" in created.headers["set-cookie"].casefold()
    dashboard = client.get("/admin")
    assert dashboard.status_code == 200
    assert "Resumen del servicio" in dashboard.text

    locked_login = client.get("/admin/login")
    assert "Crear cuenta propietaria" not in locked_login.text
    assert "Iniciar sesión" in locked_login.text
    second_setup = client.post(
        "/admin/setup",
        data={
            "email": "attacker@example.test",
            "password": "attacker-password-long-enough",
            "password_confirmation": "attacker-password-long-enough",
        },
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert second_setup.status_code == 404

    with store.connect() as db:
        owners = db.execute("SELECT COUNT(*) FROM admin_accounts WHERE role = 'owner'").fetchone()[0]
        account = db.execute("SELECT password_hash FROM admin_accounts WHERE email = ?", ("first-owner@example.test",)).fetchone()
    assert owners == 1
    assert account["password_hash"] != "first-owner-password-long-enough"
    assert _PASSWORD_HASHER.verify("first-owner-password-long-enough", account["password_hash"])

    valid_login = client.post(
        "/admin/login",
        data={"email": "first-owner@example.test", "password": "first-owner-password-long-enough"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert valid_login.status_code == 303
    with store.connect() as db:
        db.execute(
            "INSERT INTO admin_accounts (email, password_hash, role, created_at) VALUES (?, ?, 'support', ?)",
            ("support@example.test", _PASSWORD_HASHER.hash("local-support-password-not-production"), "2026-09-01T00:00:00+00:00"),
        )


def test_initial_owner_setup_rejects_mismatched_passwords(client):
    client.cookies.clear()
    with store.connect() as db:
        db.execute("DELETE FROM admin_accounts")

    response = client.post(
        "/admin/setup",
        data={
            "email": "owner@example.test",
            "password": "password-long-enough-001",
            "password_confirmation": "password-long-enough-002",
        },
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 200
    assert "Las contraseñas no coinciden" in response.text
    assert not store.admin_accounts_exist()


@pytest.mark.parametrize(
    "password",
    ["short", "                ", "1234567890123456", "passwordpassword"],
)
def test_initial_owner_setup_rejects_weak_passwords(client, password):
    response = client.post(
        "/admin/setup",
        data={
            "email": "owner@example.test",
            "password": password,
            "password_confirmation": password,
        },
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 200
    assert "Contraseña" in response.text or "contraseña" in response.text
    assert not store.admin_accounts_exist()


def test_admin_panel_login_rejects_bad_credentials_and_untrusted_origin(client):
    bad_password = client.post(
        "/admin/login",
        data={"email": "first-owner@example.test", "password": "incorrect"},
        headers={"Origin": "http://testserver"},
    )
    bad_origin = client.post(
        "/admin/setup",
        data={"email": "first-owner@example.test", "password": "first-owner-password-long-enough"},
        headers={"Origin": "https://attacker.invalid"},
    )

    assert bad_password.status_code == 200
    assert "No se pudo iniciar sesión" in bad_password.text
    assert bad_origin.status_code == 200
    assert "El navegador rechazó el envío" in bad_origin.text
    assert not store.admin_accounts_exist()


def test_admin_initial_setup_rejects_untrusted_origin(client):
    response = client.post(
        "/admin/setup",
        data={
            "email": "attacker@example.test",
            "password": "attacker-password-long-enough",
            "password_confirmation": "attacker-password-long-enough",
        },
        headers={"Origin": "https://attacker.invalid"},
    )

    assert response.status_code == 200
    assert "El navegador rechazó el envío" in response.text
    assert not store.admin_accounts_exist()


def test_admin_initial_setup_accepts_loopback_origin_used_by_local_browser(client):
    response = client.post(
        "/admin/setup",
        data={
            "email": "owner@example.test",
            "password": "owner-password-long-enough",
            "password_confirmation": "owner-password-long-enough",
        },
        headers={"Origin": "http://127.0.0.1:8000", "Host": "127.0.0.1:8000"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    assert store.admin_accounts_exist()


@pytest.mark.parametrize(
    "origin,host",
    [
        ("http://127.0.0.1:8000", "127.0.0.1:8000"),
        ("http://localhost:8000", "localhost:8000"),
    ],
)
def test_admin_setup_accepts_browser_origin_with_matching_localhost_host(client, origin, host):
    response = client.post(
        "/admin/setup",
        data={
            "email": "owner@example.test",
            "password": "owner-password-long-enough",
            "password_confirmation": "owner-password-long-enough",
        },
        headers={"Origin": origin, "Host": host},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    assert store.admin_accounts_exist()


def test_admin_initial_setup_accepts_same_origin_fetch_metadata_when_origin_is_absent(client):
    response = client.post(
        "/admin/setup",
        data={
            "email": "owner@example.test",
            "password": "owner-password-long-enough",
            "password_confirmation": "owner-password-long-enough",
        },
        headers={"Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    assert store.admin_accounts_exist()


def test_admin_setup_rejects_request_without_origin_or_same_origin_fetch_metadata(client):
    response = client.post(
        "/admin/setup",
        data={
            "email": "owner@example.test",
            "password": "owner-password-long-enough",
            "password_confirmation": "owner-password-long-enough",
        },
    )

    assert response.status_code == 200
    assert "El navegador rechazó el envío" in response.text
    assert not store.admin_accounts_exist()


def test_admin_session_is_invalidated_when_account_is_removed(client):
    admin_login(client)

    with store.connect() as db:
        db.execute("DELETE FROM admin_accounts WHERE email = ?", ("first-owner@example.test",))

    response = client.get("/admin")

    assert response.status_code == 200
    assert "Iniciar sesión" in response.text
    assert "Resumen del servicio" not in response.text


def test_admin_dashboard_shows_monitoring_and_settings_are_read_only(client):
    admin_login(client)

    dashboard = client.get("/admin")
    settings = client.get("/admin/settings")

    assert dashboard.status_code == 200
    assert "Resumen del servicio" in dashboard.text
    assert "Estado de fuentes" in dashboard.text
    assert "Peticiones autenticadas · 7 días" in dashboard.text
    assert settings.status_code == 200
    assert "Configuración del servicio" in settings.text
    assert "Rate limit enforcement" in settings.text


def test_admin_can_create_and_revoke_integration_key_once(client):
    csrf = admin_login(client)
    created = client.post(
        "/admin/integrations",
        data={
            "csrf": csrf,
            "organization_name": "Empresa de prueba",
            "integration_name": "Facturador QA",
            "requests_per_minute": "30",
            "daily_limit": "1000",
        },
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert created.status_code == 303
    assert "created_key" not in created.headers["location"]
    set_cookie = created.headers["set-cookie"]
    token_match = re.search(r"api_sri_admin_new_key=([^;]+)", set_cookie)
    assert token_match
    token = token_match.group(1)

    integrations = client.get("/admin/integrations")
    assert integrations.status_code == 200
    assert token in integrations.text
    assert "se muestra una sola vez" in integrations.text
    integration_id_match = re.search(r"/admin/integrations/([0-9a-f]{32})/revoke", integrations.text)
    assert integration_id_match
    integration_id = integration_id_match.group(1)
    assert store.integration_credential_by_hash(hashlib.sha256(token.encode()).hexdigest())
    assert store.integration_credential_by_hash(token) is None

    with store.connect() as db:
        db.execute(
            "INSERT INTO sri_ruc_snapshot (ruc, legal_name, tax_status, source_period, observed_at) VALUES (?, ?, ?, ?, ?)",
            ("1790013731001", "Empresa de prueba", "ACTIVO", "2026", "2026-09-01T00:00:00+00:00"),
        )
    assert client.get(
        "/api/v1/ruc/1790013731001", headers={"Authorization": f"Bearer {token}"}
    ).status_code == 200

    csrf = re.search(r'name="csrf" value="([^"]+)"', integrations.text).group(1)
    revoked = client.post(
        f"/admin/integrations/{integration_id}/revoke",
        data={"csrf": csrf},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert revoked.status_code == 303
    assert client.get(
        "/api/v1/ruc/1790013731001", headers={"Authorization": f"Bearer {token}"}
    ).status_code == 401

    usage = store.admin_overview()["usage_7d"]
    assert usage["requests_7d"] == 1
    assert usage["errors_7d"] == 0

    with store.connect() as db:
        actions = [row[0] for row in db.execute("SELECT action FROM audit_events ORDER BY occurred_at")]
    assert "integration.created" in actions
    assert "integration.revoked" in actions


def test_rotation_revokes_old_key_and_shows_new_key_once(client):
    csrf = admin_login(client)
    created = client.post(
        "/admin/integrations",
        data={"csrf": csrf, "organization_name": "Rotación", "integration_name": "CRM", "requests_per_minute": "10", "daily_limit": "200"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    token = re.search(r"api_sri_admin_new_key=([^;]+)", created.headers["set-cookie"]).group(1)
    page = client.get("/admin/integrations")
    integration_id = re.search(r"/admin/integrations/([0-9a-f]{32})/rotate", page.text).group(1)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

    rotated = client.post(
        f"/admin/integrations/{integration_id}/rotate",
        data={"csrf": csrf},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    new_token = re.search(r"api_sri_admin_new_key=([^;]+)", rotated.headers["set-cookie"]).group(1)

    assert new_token != token
    old_credential = store.integration_credential_by_hash(hashlib.sha256(token.encode()).hexdigest())
    new_credential = store.integration_credential_by_hash(hashlib.sha256(new_token.encode()).hexdigest())
    assert old_credential["credential_status"] == "revoked"
    assert new_credential["credential_status"] == "active"


def test_admin_mutation_rejects_missing_csrf_or_origin(client):
    admin_login(client)

    response = client.post(
        "/admin/integrations",
        data={"organization_name": "No autorizada", "integration_name": "X", "requests_per_minute": "1", "daily_limit": "1"},
        headers={"Origin": "https://attacker.invalid"},
    )

    assert response.status_code == 403
    assert all(row["integration_id"] != "integration-x" for row in store.admin_list_integrations())


def test_support_role_can_view_but_cannot_manage_integration_credentials(client):
    admin_login(client)
    with store.connect() as db:
        db.execute(
            "INSERT INTO admin_accounts (email, password_hash, role, created_at) VALUES (?, ?, 'support', ?)",
            ("support@example.test", _PASSWORD_HASHER.hash("local-support-password-not-production"), "2026-09-01T00:00:00+00:00"),
        )
    client.cookies.clear()
    admin_login_as(client, "support@example.test", "local-support-password-not-production")

    page = client.get("/admin/integrations")
    created = client.post(
        "/admin/integrations",
        data={
            "organization_name": "No autorizada",
            "integration_name": "Soporte",
            "requests_per_minute": "1",
            "daily_limit": "1",
        },
        headers={"Origin": "http://testserver"},
    )

    assert page.status_code == 200
    assert "Solo propietario" in page.text
    assert "Crear y generar credencial" not in page.text
    assert created.status_code == 403
    assert all(row["integration_name"] != "Soporte" for row in store.admin_list_integrations())


def test_admin_panel_routes_are_disabled_outside_local_mode(client, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")

    response = client.get("/admin/login")

    assert response.status_code == 404  # no-op validation marker


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
