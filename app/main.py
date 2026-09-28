from __future__ import annotations

import asyncio
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from app import store
from app.sources import sri_live

SRI_DATASET_URL = "https://www.sri.gob.ec/datasets"
SCVS_URL = "https://mercadodevalores.supercias.gob.ec/reportes/directorioCompanias.jsf"
LIVE_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}
LIVE_CACHE_LOCK = asyncio.Lock()


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.initialize()
    yield


app = FastAPI(
    title=os.getenv("API_TITLE", "API RUC Ecuador"),
    version="0.1.0",
    description=(
        "Consulta de RUC con procedencia por campo. El estado tributario del SRI "
        "y la situación legal societaria de la SCVS se reportan por separado."
    ),
    lifespan=lifespan,
)


async def get_live(ruc: str) -> dict[str, Any] | None:
    ttl = max(0, int(os.getenv("SRI_CACHE_TTL_SECONDS", "180")))
    now = time.monotonic()
    async with LIVE_CACHE_LOCK:
        entry = LIVE_CACHE.get(ruc)
        if entry and now - entry[0] < ttl:
            return entry[1]
    result = await sri_live.lookup(ruc)
    async with LIVE_CACHE_LOCK:
        LIVE_CACHE[ruc] = (time.monotonic(), result)
        if len(LIVE_CACHE) > 2048:
            oldest = sorted(LIVE_CACHE, key=lambda key: LIVE_CACHE[key][0])[:256]
            for key in oldest:
                LIVE_CACHE.pop(key, None)
    return result


def observation(value: Any, source_id: str, authority: str, url: str,
                observed_at: str | None = None, as_of: str | None = None,
                freshness: str = "unknown") -> dict[str, Any] | None:
    if value is None or not str(value).strip():
        return None
    return {
        "value": str(value).strip(),
        "authority": authority,
        "source_id": source_id,
        "source_url": url,
        "observed_at": observed_at,
        "as_of": as_of,
        "freshness": freshness,
    }


def _name_key(value: str | None) -> str:
    if not value:
        return ""
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _snapshot_freshness(source_updated_at: str | None, observed_at: str | None,
                        source_period: str | None = None) -> str:
    if source_updated_at:
        try:
            updated = datetime.fromisoformat(source_updated_at.replace("Z", "+00:00"))
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            return "stale" if datetime.now(timezone.utc) - updated.astimezone(timezone.utc) > timedelta(days=14) else "snapshot"
        except ValueError:
            pass
    if source_period:
        try:
            return "stale" if int(source_period) < datetime.now(timezone.utc).year else "snapshot"
        except ValueError:
            pass
    if observed_at:
        try:
            observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            return "stale" if datetime.now(timezone.utc) - observed.astimezone(timezone.utc) > timedelta(days=14) else "snapshot"
        except ValueError:
            pass
    return "unknown"


@app.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/api/v1/sources")
def list_sources() -> dict[str, Any]:
    synced = {row["source_id"]: row for row in store.sources_state()}
    sri_state = synced.get("sri_ruc_snapshot", {})
    scvs_state = synced.get("scvs_company_directory", {})
    return {
        "sources": [
            {
                "source_id": "sri_ruc_live",
                "authority": "sri",
                "role": "Estado y datos tributarios consultados en vivo",
                "url": sri_live.SRI_URL,
                "delivery": "public_web_query",
                "status": "on_demand",
            },
            {
                "source_id": "sri_ruc_snapshot",
                "authority": "sri",
                "role": "Respaldo histórico del catastro descargable",
                "url": SRI_DATASET_URL,
                "delivery": "annual_snapshot",
                "status": (
                    _snapshot_freshness(sri_state.get("source_updated_at"), sri_state.get("synced_at"), sri_state.get("source_period"))
                    if sri_state.get("status") == "ok" else sri_state.get("status", "not_synced")
                ),
                "synced_at": sri_state.get("synced_at"),
                "source_period": sri_state.get("source_period"),
                "record_count": sri_state.get("record_count"),
            },
            {
                "source_id": "scvs_company_directory",
                "authority": "scvs",
                "role": "Situación societaria para compañías dentro del ámbito SCVS",
                "url": SCVS_URL,
                "delivery": "daily_snapshot",
                "status": (
                    _snapshot_freshness(scvs_state.get("source_updated_at"), scvs_state.get("synced_at"))
                    if scvs_state.get("status") == "ok" else scvs_state.get("status", "not_synced")
                ),
                "synced_at": scvs_state.get("synced_at"),
                "source_updated_at": scvs_state.get("source_updated_at"),
                "record_count": scvs_state.get("record_count"),
                "limitation": "El directorio excluye ciertas compañías canceladas; ausencia no significa inexistencia.",
            },
        ]
    }


@app.get("/api/v1/ruc/{ruc}")
async def get_ruc(ruc: str) -> dict[str, Any]:
    if not re.fullmatch(r"\d{13}", ruc):
        raise HTTPException(status_code=422, detail="El RUC debe contener exactamente 13 dígitos.")

    warnings: list[str] = []
    stale_sources: list[str] = []
    live: dict[str, Any] | None = None
    live_error: str | None = None
    try:
        live = await get_live(ruc)
    except sri_live.SRIUnavailable as exc:
        live_error = str(exc)
        warnings.append(live_error)

    # A definitive live SRI response with no match must not be overwritten by
    # an older snapshot.
    if live is None and live_error is None:
        raise HTTPException(status_code=404, detail="El SRI no encontró ese RUC en la consulta pública.")

    snapshot = store.sri_snapshot(ruc) if live is None else None
    company = store.scvs_snapshot(ruc)
    sri_record = live or snapshot

    if sri_record is None and company is None:
        raise HTTPException(
            status_code=503,
            detail="El SRI no está disponible y no hay un respaldo local para este RUC.",
        )

    record: dict[str, Any] = {"ruc": ruc}
    if sri_record:
        is_live = live is not None
        source_id = "sri_ruc_live" if is_live else "sri_ruc_snapshot"
        source_url = sri_record.get("source_url", SRI_DATASET_URL)
        observed_at = sri_record.get("observed_at")
        as_of = None if is_live else (
            sri_record.get("source_updated_at") or sri_record.get("source_period")
        )
        freshness = "live" if is_live else _snapshot_freshness(
            sri_record.get("source_updated_at"), sri_record.get("observed_at"), sri_record.get("source_period")
        )
        if freshness == "stale":
            stale_sources.append("SRI snapshot")
        sri_fields = {
            "legal_name": "legal_name",
            "commercial_name": "commercial_name",
            "tax_status": "tax_status",
            "taxpayer_class": "taxpayer_class",
            "taxpayer_type": "taxpayer_type",
            "accounting_required": "accounting_required",
            "main_economic_activity": "main_economic_activity",
            "start_date": "start_date",
            "cessation_date": "cessation_date",
            "resumption_date": "resumption_date",
            "taxpayer_updated_at": "taxpayer_updated_at",
        }
        for public_field, source_field in sri_fields.items():
            item = observation(
                sri_record.get(source_field), "sri_ruc_live" if is_live else source_id,
                "sri", source_url, observed_at, as_of, freshness,
            )
            if item:
                record[public_field] = item

    if company:
        scvs_as_of = company.get("source_updated_at") or company.get("source_period")
        scvs_freshness = _snapshot_freshness(
            company.get("source_updated_at"), company.get("observed_at"), company.get("source_period")
        )
        if scvs_freshness == "stale":
            stale_sources.append("directorio SCVS")
        record["company_legal_name"] = observation(
            company.get("legal_name"), "scvs_company_directory", "scvs", SCVS_URL,
            company.get("observed_at"), scvs_as_of, scvs_freshness,
        )
        record["company_legal_status"] = observation(
            company.get("legal_status"), "scvs_company_directory", "scvs", SCVS_URL,
            company.get("observed_at"), scvs_as_of, scvs_freshness,
        )
        for field, source_field in (("company_type", "company_type"),
                                    ("province", "province"), ("canton", "canton")):
            item = observation(company.get(source_field), "scvs_company_directory", "scvs",
                               SCVS_URL, company.get("observed_at"), scvs_as_of, scvs_freshness)
            if item:
                record[field] = item

    conflicts: list[dict[str, Any]] = []
    sri_name = record.get("legal_name", {}).get("value")
    scvs_name = record.get("company_legal_name", {}).get("value")
    if sri_name and scvs_name and _name_key(sri_name) != _name_key(scvs_name):
        conflicts.append({
            "field": "legal_name",
            "values": [
                {"authority": "sri", "value": sri_name},
                {"authority": "scvs", "value": scvs_name},
            ],
        })

    corroborated_fields = ["legal_name"] if sri_name and scvs_name and not conflicts else []
    if live:
        quality_status = "conflict" if conflicts else ("multi_authority" if company else "single_authority")
    elif sri_record and company:
        quality_status = "conflict" if conflicts else "snapshot_cross_checked"
    elif sri_record or company:
        quality_status = "snapshot_only" if snapshot else "single_authority"
    else:
        quality_status = "unavailable"

    if snapshot and live_error:
        warnings.append("Se usó el conjunto descargable del SRI como respaldo; puede estar desactualizado.")
    if stale_sources:
        warnings.append("Hay snapshots antiguos: " + ", ".join(stale_sources) + ".")
    if not company:
        warnings.append("No hay coincidencia en el snapshot societario; esto no confirma inexistencia de la entidad.")

    return {
        "query": {"type": "ruc", "value": ruc},
        "record": record,
        "quality": {
            "status": quality_status,
            "corroborated_fields": corroborated_fields,
            "conflicts": conflicts,
            "warnings": warnings,
        },
    }
