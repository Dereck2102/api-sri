"""Small SQLite store for dated public-source snapshots."""

from __future__ import annotations

import json
import os
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
            """
        )


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
