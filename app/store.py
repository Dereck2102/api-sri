"""Small SQLite store for dated public-source snapshots."""

from __future__ import annotations

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
