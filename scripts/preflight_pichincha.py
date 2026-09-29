"""Measure a local SQLite representation of the official Pichincha snapshot."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import math
import re
import sqlite3
import tempfile
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

SRI_PICHINCHA_URL = "https://descargas.sri.gob.ec/download/datosAbiertos/SRI_RUC_Pichincha.zip"
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
D1_FREE_WRITES_PER_DAY = 100_000
BATCH_SIZE = 2_000

FIELD_ALIASES = {
    "ruc": ("NUMERO RUC", "RUC"),
    "legal_name": ("RAZON SOCIAL", "NOMBRE"),
    "tax_status": ("ESTADO CONTRIBUYENTE", "ESTADO DEL CONTRIBUYENTE"),
    "taxpayer_class": ("CLASE CONTRIBUYENTE",),
    "taxpayer_type": ("TIPO CONTRIBUYENTE",),
    "accounting_required": ("OBLIGADO",),
    "activity_code": ("CODIGO CIIU",),
    "activity_description": ("ACTIVIDAD ECONOMICA",),
    "commercial_name": ("NOMBRE FANTASIA COMERCIAL",),
    "start_date": ("FECHA INICIO ACTIVIDADES",),
    "cessation_date": ("FECHA SUSPENSION DEFINITIVA",),
    "resumption_date": ("FECHA REINICIO ACTIVIDADES",),
    "taxpayer_updated_at": ("FECHA ACTUALIZACION",),
    "establishment_number": ("NUMERO ESTABLECIMIENTO",),
}


def normalize_header(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^A-Z0-9]", "", unaccented.upper())


def open_csv(
    archive_path: Path,
) -> tuple[zipfile.ZipFile, TextIO, csv.DictReader[str], dict[str, str]]:
    archive = zipfile.ZipFile(archive_path)
    candidates = [item for item in archive.infolist() if item.filename.lower().endswith((".csv", ".txt"))]
    if len(candidates) != 1:
        archive.close()
        raise ValueError("El ZIP debe contener exactamente un archivo CSV/TXT.")
    member = candidates[0]
    if member.file_size > MAX_UNCOMPRESSED_BYTES:
        archive.close()
        raise ValueError("El archivo descomprimido supera el límite de seguridad local.")

    raw = archive.open(member)
    sample = raw.read(8192)
    raw.seek(0)
    encoding = "utf-8-sig"
    try:
        sample.decode(encoding)
    except UnicodeDecodeError:
        encoding = "cp1252"
    header_sample = sample.decode(encoding, errors="replace")
    try:
        delimiter = "|" if "|" in header_sample.splitlines()[0] else csv.Sniffer().sniff(
            header_sample, delimiters=";,\t"
        ).delimiter
    except (IndexError, csv.Error) as exc:
        raw.close()
        archive.close()
        raise ValueError("No se pudo reconocer el separador del CSV.") from exc

    text = io.TextIOWrapper(raw, encoding=encoding, newline="")
    reader = csv.DictReader(text, delimiter=delimiter)
    headers = {normalize_header(value): value for value in (reader.fieldnames or [])}
    resolved: dict[str, str] = {}
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            source_header = headers.get(normalize_header(alias))
            if source_header:
                resolved[field] = source_header
                break
    if "ruc" not in resolved:
        text.close()
        archive.close()
        raise ValueError("No se encontró una columna RUC reconocible en el CSV.")
    return archive, text, reader, resolved


def build_preflight_database(
    archive_path: Path,
    database_path: Path,
    source_period: str,
) -> dict[str, int | str]:
    archive, text, rows, source_columns = open_csv(archive_path)
    observed_at = datetime.now(timezone.utc).isoformat()
    source_digest = hashlib.sha256()
    with archive_path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            source_digest.update(chunk)

    db = sqlite3.connect(database_path)
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = OFF")
    db.execute("PRAGMA synchronous = OFF")
    category_ids: dict[tuple[str, str], int] = {}
    activity_ids: dict[tuple[str, str], int] = {}
    source_rows = invalid_ruc_rows = 0
    batch: list[tuple[Any, ...]] = []

    def category_id(kind: str, value: str | None) -> int | None:
        if not value:
            return None
        key = kind, value.strip()
        if key not in category_ids:
            db.execute("INSERT OR IGNORE INTO lookup_values(kind, value) VALUES (?, ?)", key)
            category_ids[key] = db.execute(
                "SELECT value_id FROM lookup_values WHERE kind = ? AND value = ?", key
            ).fetchone()[0]
        return category_ids[key]

    def activity_id(code: str | None, description: str | None) -> int | None:
        code = (code or "").strip()
        description = (description or "").strip()
        if not code and not description:
            return None
        if not code:
            code = "description:" + hashlib.sha256(description.encode("utf-8")).hexdigest()
        key = code, description
        if key not in activity_ids:
            db.execute(
                "INSERT OR IGNORE INTO economic_activities(ciiu_code, description) VALUES (?, ?)", key
            )
            activity_ids[key] = db.execute(
                "SELECT activity_id FROM economic_activities WHERE ciiu_code = ? AND description = ?",
                key,
            ).fetchone()[0]
        return activity_ids[key]

    try:
        migration_path = Path(__file__).resolve().parents[1] / "migrations" / "0001_initial.sql"
        db.executescript(migration_path.read_text(encoding="utf-8"))
        province_code = db.execute(
            "SELECT province_code FROM province_catalog WHERE province_name = ?", ("Pichincha",)
        ).fetchone()[0]
        version_id = db.execute(
            """INSERT INTO dataset_versions
               (version_key, source_id, province_code, status, source_period,
                source_updated_at, observed_at, archive_sha256)
               VALUES (?, ?, ?, 'staging', ?, ?, ?, ?)""",
            (
                f"preflight-pichincha-{source_digest.hexdigest()[:16]}",
                "sri_ruc_pichincha",
                province_code,
                source_period,
                source_period,
                observed_at,
                source_digest.hexdigest(),
            ),
        ).lastrowid

        insert_sql = """INSERT INTO ruc_snapshot (
            version_id, ruc, legal_name, tax_status_id, taxpayer_class_id,
            taxpayer_type_id, accounting_required_id, main_economic_activity_id,
            commercial_name, start_date, cessation_date, resumption_date,
            taxpayer_updated_at, establishments_count, preferred_establishment
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(ruc, version_id) DO UPDATE SET
            legal_name=CASE WHEN excluded.preferred_establishment > ruc_snapshot.preferred_establishment THEN excluded.legal_name ELSE ruc_snapshot.legal_name END,
            tax_status_id=CASE WHEN excluded.preferred_establishment > ruc_snapshot.preferred_establishment THEN excluded.tax_status_id ELSE ruc_snapshot.tax_status_id END,
            taxpayer_class_id=CASE WHEN excluded.preferred_establishment > ruc_snapshot.preferred_establishment THEN excluded.taxpayer_class_id ELSE ruc_snapshot.taxpayer_class_id END,
            taxpayer_type_id=CASE WHEN excluded.preferred_establishment > ruc_snapshot.preferred_establishment THEN excluded.taxpayer_type_id ELSE ruc_snapshot.taxpayer_type_id END,
            accounting_required_id=CASE WHEN excluded.preferred_establishment > ruc_snapshot.preferred_establishment THEN excluded.accounting_required_id ELSE ruc_snapshot.accounting_required_id END,
            main_economic_activity_id=CASE WHEN excluded.preferred_establishment > ruc_snapshot.preferred_establishment THEN excluded.main_economic_activity_id ELSE ruc_snapshot.main_economic_activity_id END,
            commercial_name=CASE WHEN excluded.preferred_establishment > ruc_snapshot.preferred_establishment THEN excluded.commercial_name ELSE ruc_snapshot.commercial_name END,
            start_date=CASE WHEN excluded.preferred_establishment > ruc_snapshot.preferred_establishment THEN excluded.start_date ELSE ruc_snapshot.start_date END,
            cessation_date=CASE WHEN excluded.preferred_establishment > ruc_snapshot.preferred_establishment THEN excluded.cessation_date ELSE ruc_snapshot.cessation_date END,
            resumption_date=CASE WHEN excluded.preferred_establishment > ruc_snapshot.preferred_establishment THEN excluded.resumption_date ELSE ruc_snapshot.resumption_date END,
            taxpayer_updated_at=CASE WHEN excluded.preferred_establishment > ruc_snapshot.preferred_establishment THEN excluded.taxpayer_updated_at ELSE ruc_snapshot.taxpayer_updated_at END,
            establishments_count=ruc_snapshot.establishments_count + 1,
            preferred_establishment=MAX(ruc_snapshot.preferred_establishment, excluded.preferred_establishment)"""

        def flush() -> None:
            if batch:
                db.executemany(insert_sql, batch)
                batch.clear()

        for row in rows:
            source_rows += 1
            ruc = re.sub(r"\D", "", row.get(source_columns["ruc"], "") or "")
            if len(ruc) != 13:
                invalid_ruc_rows += 1
                continue
            establishment = re.sub(
                r"\D", "", row.get(source_columns.get("establishment_number", ""), "") or ""
            )
            batch.append((
                version_id,
                ruc,
                row.get(source_columns.get("legal_name", "")) or None,
                category_id("tax_status", row.get(source_columns.get("tax_status", ""))),
                category_id("taxpayer_class", row.get(source_columns.get("taxpayer_class", ""))),
                category_id("taxpayer_type", row.get(source_columns.get("taxpayer_type", ""))),
                category_id("accounting_required", row.get(source_columns.get("accounting_required", ""))),
                activity_id(
                    row.get(source_columns.get("activity_code", "")),
                    row.get(source_columns.get("activity_description", "")),
                ),
                row.get(source_columns.get("commercial_name", "")) or None,
                row.get(source_columns.get("start_date", "")) or None,
                row.get(source_columns.get("cessation_date", "")) or None,
                row.get(source_columns.get("resumption_date", "")) or None,
                row.get(source_columns.get("taxpayer_updated_at", "")) or None,
                int(establishment.zfill(3) == "001"),
            ))
            if len(batch) >= BATCH_SIZE:
                flush()
        flush()
        if source_rows == 0:
            raise ValueError("El CSV oficial no contiene registros.")

        unique_ruc_records = db.execute(
            "SELECT COUNT(*) FROM ruc_snapshot WHERE version_id = ?", (version_id,)
        ).fetchone()[0]
        activity_records = db.execute("SELECT COUNT(*) FROM economic_activities").fetchone()[0]
        lookup_records = db.execute("SELECT COUNT(*) FROM lookup_values").fetchone()[0]
        estimated_d1_writes = unique_ruc_records + activity_records + lookup_records + 6
        db.execute(
            """UPDATE dataset_versions
               SET status = 'validated', row_count = ?, unique_ruc_count = ?,
                   archive_bytes = ?, uncompressed_bytes = ?, normalized_bytes = ?
               WHERE version_id = ?""",
            (
                source_rows,
                unique_ruc_records,
                archive_path.stat().st_size,
                sum(item.file_size for item in archive.infolist()),
                None,
                version_id,
            ),
        )
        db.commit()
        db.execute("VACUUM")
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        page_count = db.execute("PRAGMA page_count").fetchone()[0]
        normalized_bytes = database_path.stat().st_size
        db.execute(
            "UPDATE dataset_versions SET normalized_bytes = ? WHERE version_id = ?",
            (normalized_bytes, version_id),
        )
        db.commit()
        return {
            "source_rows": source_rows,
            "invalid_ruc_rows": invalid_ruc_rows,
            "duplicate_establishment_rows": source_rows - invalid_ruc_rows - unique_ruc_records,
            "unique_ruc_records": unique_ruc_records,
            "lookup_reference_rows": lookup_records,
            "activity_reference_rows": activity_records,
            "estimated_initial_d1_writes": estimated_d1_writes,
            "estimated_days_at_100k_writes_per_day": math.ceil(
                estimated_d1_writes / D1_FREE_WRITES_PER_DAY
            ),
            "normalized_sqlite_bytes": normalized_bytes,
            "sqlite_page_size": page_size,
            "sqlite_page_count": page_count,
            "archive_sha256": source_digest.hexdigest(),
        }
    finally:
        db.close()
        text.close()
        archive.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mide localmente el tamaño del esquema D1 para el snapshot oficial de Pichincha."
    )
    parser.add_argument("archive", type=Path, help="ZIP oficial ya descargado; no se aceptan URLs.")
    parser.add_argument("--source-period", required=True, help="Fecha ISO del archivo oficial, por ejemplo 2026-09-01.")
    args = parser.parse_args()
    if not args.archive.is_file():
        parser.error("No existe el archivo ZIP indicado.")
    try:
        with tempfile.TemporaryDirectory(prefix="sri-pichincha-preflight-") as temp_dir:
            database_path = Path(temp_dir) / "d1-schema-estimate.sqlite3"
            metrics = build_preflight_database(args.archive, database_path, args.source_period)
            print(f"source={SRI_PICHINCHA_URL}")
            print(f"source_period={args.source_period}")
            print(f"archive_bytes={args.archive.stat().st_size}")
            with zipfile.ZipFile(args.archive) as archive:
                print(f"archive_uncompressed_bytes={sum(item.file_size for item in archive.infolist())}")
            for key, value in metrics.items():
                print(f"{key}={value}")
            print("La base temporal local se elimina al finalizar; no se accede ni escribe en Cloudflare D1.")
            print("El tamaño SQLite es una estimación local, no confirma el almacenamiento D1 ni la cuota de escrituras.")
    except (OSError, sqlite3.Error, ValueError, zipfile.BadZipFile, csv.Error) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
