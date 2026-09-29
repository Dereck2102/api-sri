import sqlite3
import zipfile

from scripts.preflight_pichincha import build_preflight_database


def test_preflight_deduplicates_establishments_and_preserves_main_office(tmp_path):
    archive_path = tmp_path / "sample.zip"
    database_path = tmp_path / "snapshot.sqlite3"
    headers = [
        "NUMERO_RUC",
        "RAZON_SOCIAL",
        "ESTADO_CONTRIBUYENTE",
        "CLASE_CONTRIBUYENTE",
        "TIPO_CONTRIBUYENTE",
        "OBLIGADO",
        "CODIGO_CIIU",
        "ACTIVIDAD_ECONOMICA",
        "NUMERO_ESTABLECIMIENTO",
    ]
    rows = [
        ["1790000000001", "Sucursal", "ACTIVO", "SOCIEDAD", "GENERAL", "SI", "A0111", "Cultivo", "002"],
        ["1790000000001", "Casa matriz", "ACTIVO", "SOCIEDAD", "GENERAL", "SI", "A0111", "Cultivo", "001"],
        ["inválido", "Fila inválida", "ACTIVO", "SOCIEDAD", "GENERAL", "SI", "A0111", "Cultivo", "001"],
    ]
    content = "\n".join(["|".join(headers), *("|".join(row) for row in rows)])
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SRI_RUC_Pichincha.csv", content)

    metrics = build_preflight_database(archive_path, database_path, "2026-09-01")

    assert metrics["source_rows"] == 3
    assert metrics["invalid_ruc_rows"] == 1
    assert metrics["duplicate_establishment_rows"] == 1
    assert metrics["unique_ruc_records"] == 1
    assert metrics["estimated_initial_d1_writes"] >= 1
    assert metrics["normalized_sqlite_bytes"] == database_path.stat().st_size

    with sqlite3.connect(database_path) as db:
        record = db.execute(
            """SELECT r.legal_name, r.establishments_count, a.ciiu_code
               FROM ruc_snapshot AS r
               LEFT JOIN economic_activities AS a ON a.activity_id = r.main_economic_activity_id
               WHERE r.ruc = ?""",
            ("1790000000001",),
        ).fetchone()

    assert record == ("Casa matriz", 2, "A0111")
