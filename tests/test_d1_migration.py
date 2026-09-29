import sqlite3
from pathlib import Path

import pytest


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0001_initial.sql"


def test_initial_schema_has_single_province_catalog_and_disabled_source():
    with sqlite3.connect(":memory:") as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript(MIGRATION.read_text(encoding="utf-8"))

        assert db.execute(
            "SELECT province_code, enabled FROM province_catalog WHERE province_name = ?",
            ("Pichincha",),
        ).fetchone() == ("EC-P", 0)
        assert db.execute(
            "SELECT source_id, enabled FROM source_catalog WHERE source_id = ?",
            ("sri_ruc_pichincha",),
        ).fetchone() == ("sri_ruc_pichincha", 0)
        assert db.execute(
            "SELECT source_id, enabled FROM source_catalog WHERE source_id = ?",
            ("sri_ruc_manual_live",),
        ).fetchone() == ("sri_ruc_manual_live", 1)


def test_credentials_reject_non_sha256_length_and_keep_org_scope():
    with sqlite3.connect(":memory:") as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript(MIGRATION.read_text(encoding="utf-8"))
        db.execute(
            "INSERT INTO organizations (organization_id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("org-test", "CRM de prueba", "2026-09-01", "2026-09-01"),
        )
        db.execute(
            "INSERT INTO integrations (integration_id, organization_id, name, requests_per_minute, daily_limit, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("int-test", "org-test", "ERP de prueba", 30, 1000, "2026-09-01", "2026-09-01"),
        )
        db.execute(
            "INSERT INTO integration_credentials (credential_id, integration_id, secret_hash, created_at) VALUES (?, ?, ?, ?)",
            ("cred-test", "int-test", "a" * 64, "2026-09-01"),
        )

        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO integration_credentials (credential_id, integration_id, secret_hash, created_at) VALUES (?, ?, ?, ?)",
                ("cred-bad", "int-test", "plaintext-secret", "2026-09-01"),
            )


def test_ruc_snapshot_is_indexed_by_ruc_and_version():
    with sqlite3.connect(":memory:") as db:
        db.executescript(MIGRATION.read_text(encoding="utf-8"))
        indexes = {
            row[1]: [item[2] for item in db.execute(f"PRAGMA index_info('{row[1]}')")]
            for row in db.execute("PRAGMA index_list('ruc_snapshot')")
        }

    assert ["ruc", "version_id"] in indexes.values()
