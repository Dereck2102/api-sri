"""Read-only adapter for the SRI's public RUC lookup page."""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

import httpx
from bs4 import BeautifulSoup

SRI_URL = "https://srienlinea.sri.gob.ec/facturacion-internet/consultas/publico/ruc-datos2.jspa"
SOURCE_ID = "sri_ruc_live"

FIELD_LABELS: dict[str, tuple[str, ...]] = {
    "legal_name": ("Razón Social", "Razon Social"),
    "commercial_name": ("Nombre Comercial",),
    "tax_status": ("Estado del Contribuyente en el RUC", "Estado del Contribuyente"),
    "taxpayer_class": ("Clase de Contribuyente",),
    "taxpayer_type": ("Tipo de Contribuyente",),
    "accounting_required": ("Obligado a llevar Contabilidad",),
    "main_economic_activity": ("Actividad Económica Principal", "Actividad Economica Principal"),
    "start_date": ("Fecha de inicio de actividades", "Fecha de Inicio de Actividades"),
    "cessation_date": ("Fecha de cese de actividades", "Fecha de Cese de Actividades"),
    "resumption_date": ("Fecha reinicio de actividades", "Fecha de reinicio de actividades"),
    "taxpayer_updated_at": ("Fecha actualización", "Fecha de actualización"),
}


class SRIUnavailable(RuntimeError):
    pass


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    unaccented = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", unaccented).strip().casefold().rstrip(":")


def _extract_from_rows(soup: BeautifulSoup) -> dict[str, str]:
    labels = {
        _normalize(label): field
        for field, variants in FIELD_LABELS.items()
        for label in variants
    }
    found: dict[str, str] = {}
    for row in soup.select("tr"):
        cells = [
            re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip()
            for cell in row.select("th, td")
        ]
        for index, cell in enumerate(cells[:-1]):
            field = labels.get(_normalize(cell))
            if field and cells[index + 1] and field not in found:
                found[field] = cells[index + 1]
    return found


def _extract_from_text(soup: BeautifulSoup) -> dict[str, str]:
    """Fallback for the SRI page if its label/value table markup changes."""
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    searchable_text = text.casefold()
    labels = sorted(
        {(label.casefold(), field) for field, variants in FIELD_LABELS.items() for label in variants},
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    positions: list[tuple[int, int, str]] = []
    for label, field in labels:
        start = 0
        while True:
            pos = searchable_text.find(label, start)
            if pos < 0:
                break
            end = pos + len(label)
            positions.append((pos, end, field))
            start = end
    positions.sort()
    found: dict[str, str] = {}
    for index, (pos, end, field) in enumerate(positions):
        if field in found:
            continue
        next_pos = positions[index + 1][0] if index + 1 < len(positions) else len(text)
        value = text[end:next_pos].lstrip(" :-").strip()
        if value:
            found[field] = value
    return found


def parse_profile(html: str, ruc: str) -> dict[str, Any] | None:
    soup = BeautifulSoup(html, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    if "no se encontraron resultados" in _normalize(text) or "no se encontro informacion" in _normalize(text):
        return None
    if ruc not in text:
        raise SRIUnavailable("La respuesta del SRI no contiene el RUC consultado.")

    fields = _extract_from_rows(soup)
    fields.update({key: value for key, value in _extract_from_text(soup).items() if key not in fields})
    fields = {key: value.strip() for key, value in fields.items() if value and value.strip()}
    if not fields:
        raise SRIUnavailable("No se pudieron leer los campos de la consulta pública del SRI.")
    return {
        "ruc": ruc,
        **fields,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "source_id": SOURCE_ID,
        "source_url": f"{SRI_URL}?ruc={ruc}",
        "authority": "sri",
        "freshness": "live",
    }


async def lookup(ruc: str) -> dict[str, Any] | None:
    timeout = float(os.getenv("SRI_LIVE_TIMEOUT_SECONDS", "12"))
    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "es-EC,es;q=0.9",
        "User-Agent": "api-sri-mvp/0.1 (public-source lookup)",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(SRI_URL, params={"ruc": ruc}, headers=headers)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise SRIUnavailable(f"El portal público del SRI no respondió: {type(exc).__name__}.") from exc
    return parse_profile(response.text, ruc)
