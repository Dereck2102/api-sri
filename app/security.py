from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Header, HTTPException, Request

from app import store


@dataclass(frozen=True)
class IntegrationPrincipal:
    organization_id: str
    integration_id: str
    permissions: frozenset[str]
    requests_per_minute: int
    daily_limit: int


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def require_ruc_read(
    request: Request,
    authorization: str | None = Header(default=None),
) -> IntegrationPrincipal:
    if not authorization or len(authorization) > 512:
        raise _unauthorized()
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.casefold() != "bearer" or not token or not token.isprintable():
        raise _unauthorized()

    credential = store.integration_credential_by_hash(token_digest(token))
    if not credential:
        raise _unauthorized()

    if credential["organization_status"] != "active":
        raise _unauthorized()
    if credential["integration_status"] != "active":
        raise _unauthorized()
    if credential["credential_status"] != "active":
        raise _unauthorized()

    expires_at = credential.get("expires_at")
    if expires_at:
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= datetime.now(timezone.utc):
                raise _unauthorized()
        except ValueError:
            raise _unauthorized() from None

    try:
        permissions = frozenset(json.loads(credential["permissions"]))
    except (TypeError, ValueError):
        raise _unauthorized() from None
    if "ruc:read" not in permissions:
        raise HTTPException(status_code=403, detail="La integración no tiene permiso para consultar RUC.")

    principal = IntegrationPrincipal(
        organization_id=credential["organization_id"],
        integration_id=credential["integration_id"],
        permissions=permissions,
        requests_per_minute=credential["requests_per_minute"],
        daily_limit=credential["daily_limit"],
    )
    request.state.integration_principal = principal
    return principal


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Se requiere una credencial de integración válida.",
        headers={"WWW-Authenticate": "Bearer"},
    )
