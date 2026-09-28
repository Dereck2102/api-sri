#!/usr/bin/env python3
"""Download and import official SRI and SCVS snapshots into the local SQLite DB."""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sqlite3
import sys
import tarfile
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))
from app.store import connect, initialize  # noqa: E402

SRI_DATASET_URL = "https://www.sri.gob.ec/datasets"
SCVS_PAGE_URL = "https://mercadodevalores.supercias.gob.ec/reportes/directorioCompanias.jsf"
SCVS_FALLBACK_XLSX = "https://mercadodevalores.supercias.gob.ec/reportes/excel/directorio_companias.xlsx"
USER_AGENT = "api-sri-mvp/0.1 (official public dataset sync)"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm(value: Any) -> str:
    if value is None:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(value))
    value = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def find_column(headers: list[Any], *aliases: str) -> int | None:
    normalized = [norm(header) for header in headers]
    for alias in aliases:
        key = norm(alias)
        if key in normalized:
            return normalized.index(key)
    return None


def find_csv_payload(archive: bytes) -> tuple[bytes, str]:
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            candidates = [m for m in bundle.getmembers() if m.isfile() and m.name.lower().endswith((".csv", ".txt"))]
            if candidates:
                preferred = next((m for m in candidates if "ruc" in m.name.lower()), candidates[0])
                extracted = bundle.extractfile(preferred)
                if extracted:
                    return extracted.read(), preferred.name
    except tarfile.TarError:
        pass
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            candidates = [name for name in bundle.namelist() if name.lower().endswith((".csv", ".txt"))]
            if candidates:
                preferred = next((name for name in candidates if "ruc" in name.lower()), candidates[0])
                return bundle.read(preferred), preferred
    except zipfile.BadZipFile:
        pass
    raise RuntimeError("No se encontró un CSV/TXT dentro del archivo publicado por el SRI.")


def decode_csv(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise RuntimeError("No se pudo decodificar el archivo del SRI.")


def csv_rows(raw: bytes) -> tuple[list[str], Iterable[dict[str, str]]]:
    text = decode_csv(raw)
    sample = text[:8192]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    headers = list(reader.fieldnames or [])
    return headers, reader


def replace_sri_rows(rows: Iterable[dict[str, Any]], source_period: str, source_updated_at: str | None) -> int:
    observed = now_iso()
    with connect() as db:
        db.execute("CREATE TEMP TABLE stage_sri_ruc (ruc TEXT, legal_name TEXT, tax_status TEXT, source_period TEXT, source_updated_at TEXT, observed_at TEXT)")
        count = 0
        batch: list[tuple[Any, ...]] = []
        for row in rows:
            batch.append((row["ruc"], row.get("legal_name"), row.get("tax_status"), source_period, source_updated_at, observed))
            if len(batch) >= 5000:
                db.executemany("INSERT INTO stage_sri_ruc VALUES (?, ?, ?, ?, ?, ?)", batch)
                count += len(batch)
                batch.clear()
        if batch:
            db.executemany("INSERT INTO stage_sri_ruc VALUES (?, ?, ?, ?, ?, ?)", batch)
            count += len(batch)
        if count == 0:
            raise RuntimeError("El archivo del SRI no contenía filas de RUC reconocibles.")
        db.execute("DELETE FROM sri_ruc_snapshot")
        db.execute("INSERT OR REPLACE INTO sri_ruc_snapshot SELECT * FROM stage_sri_ruc")
        db.execute(
            """INSERT OR REPLACE INTO source_state
               (source_id, authority, url, status, synced_at, source_updated_at, source_period, record_count, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("sri_ruc_snapshot", "sri", SRI_DATASET_URL, "ok", observed,
             source_updated_at, source_period, count, None),
        )
    return count


def sync_sri(client: httpx.Client) -> int:
    year = os.getenv("SRI_DATASET_YEAR", "2026")
    url = f"https://descargas.sri.gob.ec/download/datosAbiertos/sri_activos_{year}.tar.gz"
    response = client.get(url, timeout=90)
    response.raise_for_status()
    raw, filename = find_csv_payload(response.content)
    headers, parsed_rows = csv_rows(raw)
    ruc_index = find_column(headers, "RUC", "NUMERO RUC", "NUMERO DE RUC", "RUC CONTRIBUYENTE")
    name_index = find_column(headers, "RAZON SOCIAL", "RAZON_SOCIAL", "NOMBRE", "NOMBRE COMPLETO")
    status_index = find_column(headers, "ESTADO CONTRIBUYENTE", "ESTADO DEL CONTRIBUYENTE", "ESTADO RUC")
    if ruc_index is None:
        raise RuntimeError(f"No se identificó la columna RUC en el archivo {filename}.")

    def records() -> Iterable[dict[str, str]]:
        for row in parsed_rows:
            values = list(row.values())
            ruc = re.sub(r"\D", "", values[ruc_index] if ruc_index < len(values) else "")
            if len(ruc) != 13:
                continue
            yield {
                "ruc": ruc,
                "legal_name": values[name_index].strip() if name_index is not None and name_index < len(values) else None,
                "tax_status": values[status_index].strip() if status_index is not None and status_index < len(values) else None,
            }

    count = replace_sri_rows(records(), year, None)
    print(f"SRI: {count} filas importadas desde {filename} (período {year}).")
    return count


def scvs_resources(client: httpx.Client) -> tuple[bytes, str | None]:
    page = client.get(SCVS_PAGE_URL, timeout=30)
    page.raise_for_status()
    soup = BeautifulSoup(page.text, "html.parser")
    href = None
    for anchor in soup.select("a[href]"):
        label = anchor.get_text(" ", strip=True).lower()
        candidate = anchor.get("href", "")
        if "excel" in label or candidate.lower().endswith(".xlsx"):
            href = candidate
            break
    if href and href.startswith("/"):
        href = "https://mercadodevalores.supercias.gob.ec" + href
    href = href or SCVS_FALLBACK_XLSX
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    match = re.search(r"FECHA DE ACTUALIZACI[ÓO]N\s*[:|]*\s*(\d{1,2}/\d{1,2}/\d{4}\s+\d{2}:\d{2}:\d{2})", text, re.I)
    source_updated_at = None
    if match:
        parsed = datetime.strptime(match.group(1), "%d/%m/%Y %H:%M:%S").replace(tzinfo=ZoneInfo("America/Guayaquil"))
        source_updated_at = parsed.isoformat()
    workbook_response = client.get(href, timeout=120)
    workbook_response.raise_for_status()
    return workbook_response.content, source_updated_at


def xlsx_records(content: bytes) -> Iterable[dict[str, str]]:
    workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    sheet = workbook.active
    header_row = None
    indices: dict[str, int | None] = {}
    for row_num, values in enumerate(sheet.iter_rows(min_row=1, max_row=30, values_only=True), start=1):
        headers = list(values)
        ruc = find_column(headers, "RUC", "NUMERO RUC")
        name = find_column(headers, "NOMBRE", "RAZON SOCIAL", "RAZON_SOCIAL")
        legal = find_column(headers, "SITUACION LEGAL", "ESTADO LEGAL")
        if ruc is not None and name is not None and legal is not None:
            header_row = row_num
            indices = {
                "ruc": ruc,
                "legal_name": name,
                "legal_status": legal,
                "company_type": find_column(headers, "TIPO DE COMPANIA", "TIPO COMPANIA"),
                "province": find_column(headers, "PROVINCIA"),
                "canton": find_column(headers, "CANTON"),
            }
            break
    if header_row is None:
        workbook.close()
        raise RuntimeError("No se reconocieron las columnas RUC, NOMBRE y SITUACIÓN LEGAL del Excel SCVS.")

    def value_at(values: tuple[Any, ...], field: str) -> str | None:
        index = indices[field]
        if index is None or index >= len(values) or values[index] is None:
            return None
        value = values[index]
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        result = str(value).strip()
        return result or None

    try:
        for values in sheet.iter_rows(min_row=header_row + 1, values_only=True):
            ruc = re.sub(r"\D", "", value_at(values, "ruc") or "")
            if 0 < len(ruc) < 13:
                ruc = ruc.zfill(13)
            if len(ruc) != 13:
                continue
            yield {
                "ruc": ruc,
                "legal_name": value_at(values, "legal_name"),
                "legal_status": value_at(values, "legal_status"),
                "company_type": value_at(values, "company_type"),
                "province": value_at(values, "province"),
                "canton": value_at(values, "canton"),
            }
    finally:
        workbook.close()


def replace_scvs_rows(rows: Iterable[dict[str, Any]], source_updated_at: str | None) -> int:
    observed = now_iso()
    with connect() as db:
        db.execute("CREATE TEMP TABLE stage_scvs (ruc TEXT, legal_name TEXT, legal_status TEXT, company_type TEXT, province TEXT, canton TEXT, source_period TEXT, source_updated_at TEXT, observed_at TEXT)")
        count = 0
        batch: list[tuple[Any, ...]] = []
        for row in rows:
            batch.append((row["ruc"], row.get("legal_name"), row.get("legal_status"),
                          row.get("company_type"), row.get("province"), row.get("canton"),
                          None, source_updated_at, observed))
            if len(batch) >= 5000:
                db.executemany("INSERT INTO stage_scvs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", batch)
                count += len(batch)
                batch.clear()
        if batch:
            db.executemany("INSERT INTO stage_scvs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", batch)
            count += len(batch)
        if count == 0:
            raise RuntimeError("El archivo de SCVS no contenía RUC reconocibles; se conserva el snapshot anterior.")
        db.execute("DELETE FROM scvs_company_snapshot")
        db.execute("INSERT OR REPLACE INTO scvs_company_snapshot SELECT * FROM stage_scvs")
        db.execute(
            """INSERT OR REPLACE INTO source_state
               (source_id, authority, url, status, synced_at, source_updated_at, source_period, record_count, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("scvs_company_directory", "scvs", SCVS_PAGE_URL, "ok", observed,
             source_updated_at, None, count,
             "El directorio omite ciertas compañías canceladas; ausencia no equivale a inexistencia."),
        )
    return count


def sync_scvs(client: httpx.Client) -> int:
    workbook, source_updated_at = scvs_resources(client)
    count = replace_scvs_rows(xlsx_records(workbook), source_updated_at)
    print(f"SCVS: {count} filas importadas; actualización reportada: {source_updated_at or 'no detectada'}.")
    return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=("sri", "scvs", "all"), default="all")
    args = parser.parse_args()
    initialize()
    with httpx.Client(headers={"User-Agent": USER_AGENT, "Accept-Language": "es-EC,es;q=0.9"}, follow_redirects=True) as client:
        if args.only in ("sri", "all"):
            sync_sri(client)
        if args.only in ("scvs", "all"):
            sync_scvs(client)


if __name__ == "__main__":
    main()
