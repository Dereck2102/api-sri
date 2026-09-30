"""Small SQLite store for dated public-source snapshots."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Any


def database_path() -> Path:
    path = Path(os.getenv("DATABASE_PATH", "./data/snapshots.sqlite3"))
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(database_path(), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def initialize() -> None:
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sri_ruc_snapshot (
                ruc TEXT PRIMARY KEY,
                legal_name TEXT,
                tax_status TEXT,
                source_period TEXT,
                source_updated_at TEXT,
                observed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scvs_company_snapshot (
                ruc TEXT PRIMARY KEY,
                legal_name TEXT,
                legal_status TEXT,
                company_type TEXT,
                province TEXT,
                canton TEXT,
                source_period TEXT,
                source_updated_at TEXT,
                observed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_state (
                source_id TEXT PRIMARY KEY,
                authority TEXT NOT NULL,
                url TEXT NOT NULL,
                status TEXT NOT NULL,
                synced_at TEXT,
                source_updated_at TEXT,
                source_period TEXT,
                record_count INTEGER,
                detail TEXT
            );
            CREATE TABLE IF NOT EXISTS organizations (
                organization_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'invited'
                    CHECK (status IN ('invited', 'active', 'suspended', 'revoked')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS integrations (
                integration_id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'invited'
                    CHECK (status IN ('invited', 'active', 'suspended', 'revoked')),
                permissions TEXT NOT NULL DEFAULT '["ruc:read"]'
                    CHECK (json_valid(permissions)),
                requests_per_minute INTEGER NOT NULL CHECK (requests_per_minute > 0),
                daily_limit INTEGER NOT NULL CHECK (daily_limit > 0),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS integration_credentials (
                credential_id TEXT PRIMARY KEY,
                integration_id TEXT NOT NULL REFERENCES integrations(integration_id),
                secret_hash TEXT NOT NULL UNIQUE CHECK (length(secret_hash) = 64),
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'revoked', 'expired')),
                created_at TEXT NOT NULL,
                expires_at TEXT,
                revoked_at TEXT,
                rotated_from TEXT REFERENCES integration_credentials(credential_id)
            );
            CREATE TABLE IF NOT EXISTS manual_ruc_results (
                ruc TEXT PRIMARY KEY CHECK (length(ruc) = 13),
                result_json TEXT NOT NULL CHECK (json_valid(result_json)),
                source_id TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_period TEXT,
                observed_at TEXT NOT NULL,
                freshness TEXT NOT NULL CHECK (freshness IN ('live', 'snapshot', 'stale', 'unknown')),
                updated_by TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                event_id TEXT PRIMARY KEY,
                actor_email TEXT NOT NULL,
                action TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT,
                occurred_at TEXT NOT NULL,
                request_id TEXT,
                details_json TEXT CHECK (details_json IS NULL OR json_valid(details_json))
            );
            CREATE INDEX IF NOT EXISTS audit_events_date_idx ON audit_events(occurred_at);
            CREATE TABLE IF NOT EXISTS admin_accounts (
                email TEXT PRIMARY KEY COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('owner', 'support')),
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_admin_owner_idx
                ON admin_accounts(role) WHERE role = 'owner';
            CREATE TABLE IF NOT EXISTS integration_usage_daily (
                integration_id TEXT NOT NULL REFERENCES integrations(integration_id),
                usage_date TEXT NOT NULL,
                request_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (integration_id, usage_date)
            );
            """
        )


def admin_accounts_exist() -> bool:
    with connect() as db:
        return db.execute(
            "SELECT 1 FROM admin_accounts WHERE role = 'owner' LIMIT 1"
        ).fetchone() is not None


def admin_create_initial_owner(
    email: str,
    password_hash: str,
    now: str,
    request_id: str | None = None,
) -> bool:
    try:
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM admin_accounts WHERE role = 'owner' LIMIT 1").fetchone():
                return False
            db.execute(
                "INSERT INTO admin_accounts (email, password_hash, role, created_at) VALUES (?, ?, 'owner', ?)",
                (email, password_hash, now),
            )
            db.execute(
                """INSERT INTO audit_events
                   (event_id, actor_email, action, target_type, target_id, occurred_at, request_id, details_json)
                   VALUES (?, ?, 'admin.owner.initialized', 'admin_account', ?, ?, ?, '{}')""",
                (secrets.token_hex(16), email, email, now, request_id),
            )
    except sqlite3.IntegrityError:
        return False
    return True


def admin_account(email: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute(
            "SELECT email, password_hash, role FROM admin_accounts WHERE email = ? COLLATE NOCASE",
            (email,),
        ).fetchone()
    return dict(row) if row else None


def sri_snapshot(ruc: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute(
            "SELECT * FROM sri_ruc_snapshot WHERE ruc = ?", (ruc,)
        ).fetchone()
    return dict(row) if row else None


def scvs_snapshot(ruc: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute(
            "SELECT * FROM scvs_company_snapshot WHERE ruc = ?", (ruc,)
        ).fetchone()
    return dict(row) if row else None


def sources_state() -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT * FROM source_state ORDER BY source_id").fetchall()
    return [dict(row) for row in rows]


def integration_credential_by_hash(secret_hash: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute(
            """SELECT c.status AS credential_status, c.expires_at,
                      i.integration_id, i.status AS integration_status,
                      i.permissions, i.requests_per_minute, i.daily_limit,
                      o.organization_id, o.status AS organization_status
               FROM integration_credentials AS c
               JOIN integrations AS i ON i.integration_id = c.integration_id
               JOIN organizations AS o ON o.organization_id = i.organization_id
               WHERE c.secret_hash = ?
               LIMIT 1""",
            (secret_hash,),
        ).fetchone()
    return dict(row) if row else None


def manual_ruc_result(ruc: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute(
            "SELECT result_json FROM manual_ruc_results WHERE ruc = ?", (ruc,)
        ).fetchone()
    return json.loads(row["result_json"]) if row else None


def admin_overview() -> dict[str, Any]:
    with connect() as db:
        counts = {
            "organizations": db.execute("SELECT COUNT(*) FROM organizations").fetchone()[0],
            "active_integrations": db.execute(
                "SELECT COUNT(*) FROM integrations WHERE status = 'active'"
            ).fetchone()[0],
            "active_credentials": db.execute(
                "SELECT COUNT(*) FROM integration_credentials WHERE status = 'active'"
            ).fetchone()[0],
            "saved_manual_results": db.execute(
                "SELECT COUNT(*) FROM manual_ruc_results"
            ).fetchone()[0],
        }
        sources = [dict(row) for row in db.execute(
            "SELECT source_id, authority, status, synced_at, source_period, record_count "
            "FROM source_state ORDER BY source_id"
        ).fetchall()]
        recent_events = [dict(row) for row in db.execute(
            "SELECT event_id, actor_email, action, target_type, target_id, occurred_at "
            "FROM audit_events ORDER BY occurred_at DESC LIMIT 10"
        ).fetchall()]
        usage = db.execute(
            """SELECT COALESCE(SUM(request_count), 0) AS requests_7d,
                      COALESCE(SUM(error_count), 0) AS errors_7d
               FROM integration_usage_daily
               WHERE usage_date >= date('now', '-6 days')"""
        ).fetchone()
        usage_by_integration = [dict(row) for row in db.execute(
            """SELECT i.integration_id, i.name AS integration_name,
                      o.name AS organization_name,
                      COALESCE(SUM(u.request_count), 0) AS requests_7d,
                      COALESCE(SUM(u.error_count), 0) AS errors_7d
               FROM integrations AS i
               JOIN organizations AS o ON o.organization_id = i.organization_id
               LEFT JOIN integration_usage_daily AS u
                 ON u.integration_id = i.integration_id
                AND u.usage_date >= date('now', '-6 days')
               GROUP BY i.integration_id
               ORDER BY requests_7d DESC, i.name"""
        ).fetchall()]
    return {
        "counts": counts,
        "sources": sources,
        "recent_events": recent_events,
        "usage_7d": dict(usage),
        "usage_by_integration": usage_by_integration,
    }


def record_integration_request(integration_id: str, usage_date: str, failed: bool, now: str) -> None:
    with connect() as db:
        db.execute(
            """INSERT INTO integration_usage_daily
               (integration_id, usage_date, request_count, error_count, updated_at)
               VALUES (?, ?, 1, ?, ?)
               ON CONFLICT(integration_id, usage_date) DO UPDATE SET
                 request_count = request_count + 1,
                 error_count = error_count + excluded.error_count,
                 updated_at = excluded.updated_at""",
            (integration_id, usage_date, int(failed), now),
        )


def admin_list_integrations() -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            """SELECT o.organization_id, o.name AS organization_name, o.status AS organization_status,
                      i.integration_id, i.name AS integration_name, i.status AS integration_status,
                      i.requests_per_minute, i.daily_limit, i.created_at,
                      COUNT(CASE WHEN c.status = 'active' AND (c.expires_at IS NULL OR c.expires_at > strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) THEN 1 END) AS active_credentials
               FROM integrations AS i
               JOIN organizations AS o ON o.organization_id = i.organization_id
               LEFT JOIN integration_credentials AS c ON c.integration_id = i.integration_id
               GROUP BY i.integration_id
               ORDER BY i.created_at DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


def admin_create_integration(
    organization_id: str,
    organization_name: str,
    integration_id: str,
    integration_name: str,
    credential_id: str,
    secret_hash: str,
    expires_at: str,
    requests_per_minute: int,
    daily_limit: int,
    actor: str,
    now: str,
) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO organizations VALUES (?, ?, 'active', ?, ?)",
            (organization_id, organization_name, now, now),
        )
        db.execute(
            """INSERT INTO integrations
               (integration_id, organization_id, name, status, permissions,
                requests_per_minute, daily_limit, created_at, updated_at)
               VALUES (?, ?, ?, 'active', '["ruc:read"]', ?, ?, ?, ?)""",
            (integration_id, organization_id, integration_name, requests_per_minute, daily_limit, now, now),
        )
        db.execute(
            """INSERT INTO integration_credentials
               (credential_id, integration_id, secret_hash, status, created_at, expires_at)
               VALUES (?, ?, ?, 'active', ?, ?)""",
            (credential_id, integration_id, secret_hash, now, expires_at),
        )
        db.execute(
            """INSERT INTO audit_events
               (event_id, actor_email, action, target_type, target_id, occurred_at, details_json)
               VALUES (?, ?, 'integration.created', 'integration', ?, ?, '{}')""",
            (secrets.token_hex(16), actor, integration_id, now),
        )


def admin_revoke_integration(integration_id: str, actor: str, now: str) -> bool:
    with connect() as db:
        row = db.execute(
            "SELECT integration_id FROM integrations WHERE integration_id = ?",
            (integration_id,),
        ).fetchone()
        if not row:
            return False
        db.execute(
            "UPDATE integrations SET status = 'revoked', updated_at = ? WHERE integration_id = ?",
            (now, integration_id),
        )
        db.execute(
            "UPDATE integration_credentials SET status = 'revoked', revoked_at = ? WHERE integration_id = ? AND status = 'active'",
            (now, integration_id),
        )
        db.execute(
            """INSERT INTO audit_events
               (event_id, actor_email, action, target_type, target_id, occurred_at, details_json)
               VALUES (?, ?, 'integration.revoked', 'integration', ?, ?, '{}')""",
            (secrets.token_hex(16), actor, integration_id, now),
        )
    return True


def admin_rotate_credential(
    integration_id: str,
    credential_id: str,
    secret_hash: str,
    expires_at: str,
    actor: str,
    now: str,
) -> bool:
    with connect() as db:
        integration = db.execute(
            "SELECT status FROM integrations WHERE integration_id = ?",
            (integration_id,),
        ).fetchone()
        if not integration or integration["status"] != "active":
            return False
        previous = db.execute(
            """SELECT credential_id FROM integration_credentials
               WHERE integration_id = ? AND status = 'active'
               ORDER BY created_at DESC LIMIT 1""",
            (integration_id,),
        ).fetchone()
        db.execute(
            """UPDATE integration_credentials
               SET status = 'revoked', revoked_at = ?
               WHERE integration_id = ? AND status = 'active'""",
            (now, integration_id),
        )
        db.execute(
            """INSERT INTO integration_credentials
               (credential_id, integration_id, secret_hash, status, created_at, expires_at, rotated_from)
               VALUES (?, ?, ?, 'active', ?, ?, ?)""",
            (credential_id, integration_id, secret_hash, now, expires_at, previous["credential_id"] if previous else None),
        )
        db.execute(
            """INSERT INTO audit_events
               (event_id, actor_email, action, target_type, target_id, occurred_at, details_json)
               VALUES (?, ?, 'credential.rotated', 'integration', ?, ?, '{}')""",
            (secrets.token_hex(16), actor, integration_id, now),
        )
    return True
