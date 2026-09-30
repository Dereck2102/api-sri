from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from pwdlib import PasswordHash

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import store
from app.security import token_digest

router = APIRouter()
_SESSION_COOKIE = "api_sri_admin_session"
_FLASH_COOKIE = "api_sri_admin_new_key"
_SESSION_MAX_AGE = 8 * 60 * 60
_PASSWORD_HASHER = PasswordHash.recommended()


def _local_mode() -> bool:
    return os.getenv("APP_ENV", "production").strip().casefold() == "local"


def _secret() -> bytes:
    value = os.getenv("ADMIN_SESSION_SECRET", "")
    return value.encode("utf-8")


def _session_secret_is_configured() -> bool:
    secret = _secret()
    return len(secret) >= 32 and not secret.startswith(b"replace-with-")


def _encode_session(payload: dict[str, object]) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def _decode_session(value: str | None) -> dict[str, object] | None:
    if not value or "." not in value or len(_secret()) < 32:
        return None
    body, signature = value.rsplit(".", 1)
    expected = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("role"), str)
        or payload["role"] not in {"owner", "support"}
        or not isinstance(payload.get("email"), str)
        or not payload["email"]
        or not isinstance(payload.get("csrf"), str)
        or not payload["csrf"]
        or type(payload.get("exp")) is not int
        or payload["exp"] < int(time.time())
    ):
        return None
    return payload


def _session(request: Request) -> dict[str, object] | None:
    session = _decode_session(request.cookies.get(_SESSION_COOKIE))
    if not session or not _local_mode() or not _session_secret_is_configured():
        return None
    account = store.admin_account(str(session["email"]))
    if not account or account["role"] != session["role"]:
        return None
    return session


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin:
        parsed_origin = urlsplit(origin)
        host_header = request.headers.get("host", "")
        host = urlsplit(f"//{host_header}")
        origin_host = parsed_origin.netloc.casefold()
        if (
            parsed_origin.scheme in {"http", "https"}
            and parsed_origin.hostname
            and host.hostname
            and parsed_origin.hostname.casefold() == host.hostname.casefold()
            and parsed_origin.port == host.port
            and not parsed_origin.username
            and not parsed_origin.password
            and parsed_origin.path in {"", "/"}
            and not parsed_origin.query
            and not parsed_origin.fragment
        ):
            return True
        return False
    fetch_site = request.headers.get("sec-fetch-site", "").casefold()
    if fetch_site:
        return fetch_site == "same-origin"
    referer = request.headers.get("referer", "")
    if referer:
        parsed_referer = urlsplit(referer)
        host = urlsplit(f"//{request.headers.get('host', '')}")
        return (
            parsed_referer.scheme in {"http", "https"}
            and parsed_referer.hostname is not None
            and host.hostname is not None
            and parsed_referer.hostname.casefold() == host.hostname.casefold()
            and parsed_referer.port == host.port
            and not parsed_referer.username
            and not parsed_referer.password
        )
    return False


def _page(title: str, content: str, active: str = "", no_store: bool = False) -> HTMLResponse:
    nonce = secrets.token_urlsafe(18)
    links = [("overview", "/admin", "Resumen"), ("integrations", "/admin/integrations", "Accesos API"), ("settings", "/admin/settings", "Configuración")]
    nav = "".join(
        f'<a class="nav-link {"active" if key == active else ""}" href="{href}">{label}</a>'
        for key, href, label in links
    )
    page = f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{html.escape(title)} · Panel Maestro</title>
<style>
:root{{font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#17243b;background:#f4f7fb;font-synthesis:none;text-rendering:optimizeLegibility}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh}}a{{color:inherit}}button,input{{font:inherit}}.layout{{min-height:100vh;display:grid;grid-template-columns:245px minmax(0,1fr)}}aside{{padding:26px 18px;background:#111d32;color:#d5deed}}.brand{{display:flex;align-items:center;gap:11px;padding:0 10px 28px;font-weight:750;color:#fff}}.mark{{display:grid;place-items:center;width:38px;height:38px;border-radius:12px;background:#2663eb;color:#fff}}.nav{{display:grid;gap:6px}}.nav-link{{padding:11px 12px;border-radius:9px;text-decoration:none;color:#b6c3d8;font-size:14px}}.nav-link:hover,.nav-link.active{{background:#22324b;color:#fff}}.side-note{{margin:30px 10px 0;color:#91a0b7;font-size:12px;line-height:1.6}}.main{{min-width:0}}header{{height:72px;padding:0 clamp(20px,4vw,48px);display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #e2e8f0;background:#fff}}header span{{font-size:13px;color:#68778d}}.logout{{border:0;background:transparent;color:#39506f;cursor:pointer;font-weight:650}}main{{width:min(1120px,100%);margin:0 auto;padding:38px clamp(20px,4vw,48px) 60px}}.eyebrow{{margin:0 0 8px;color:#3064d8;font-size:12px;font-weight:750;letter-spacing:.11em;text-transform:uppercase}}h1{{margin:0;font-size:clamp(26px,4vw,34px);letter-spacing:-.04em}}.sub{{margin:9px 0 26px;color:#68778d;line-height:1.6}}.grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}}.card,.panel{{border:1px solid #e1e8f1;border-radius:14px;background:#fff;box-shadow:0 4px 14px #17243b05}}.card{{padding:19px}}.label{{color:#66768c;font-size:13px}}.value{{margin-top:12px;font-size:30px;font-weight:750;letter-spacing:-.04em}}.panel{{margin-top:22px;padding:22px}}.panel h2{{margin:0 0 16px;font-size:18px}}.muted{{color:#68778d;font-size:13px;line-height:1.65}}table{{width:100%;border-collapse:collapse;text-align:left}}th,td{{padding:12px 10px;border-bottom:1px solid #edf1f6;font-size:13px}}th{{color:#68778d;font-weight:650}}td{{color:#26364f}}.badge{{display:inline-flex;padding:4px 9px;border-radius:999px;background:#edf3ff;color:#2857b7;font-size:12px}}.empty{{padding:20px;text-align:center;color:#68778d}}.form-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}label{{display:grid;gap:7px;color:#40516b;font-size:13px;font-weight:650}}input{{width:100%;min-height:44px;padding:10px 12px;border:1px solid #ccd6e3;border-radius:9px;background:#fff;color:#17243b}}input:focus-visible,button:focus-visible,a:focus-visible{{outline:3px solid #9bbcff;outline-offset:2px}}.actions{{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:18px}}.btn{{min-height:42px;padding:0 15px;border:1px solid #d3deec;border-radius:9px;background:#fff;color:#283a55;font-weight:650;cursor:pointer}}.btn.primary{{border-color:#245fe5;background:#245fe5;color:#fff}}.btn.danger{{color:#b42318;border-color:#f1c5c1}}.notice{{margin:16px 0;padding:13px 15px;border:1px solid #e2e9f2;border-radius:10px;background:#f8faff;color:#52647c;font-size:13px;line-height:1.6}}.notice.error{{border-color:#f2c9c5;background:#fff7f6;color:#a5281d}}.notice.success{{border-color:#bfe4d1;background:#f3fbf6;color:#17643d}}.secret{{overflow-wrap:anywhere;padding:13px;border-radius:9px;background:#eef3fa;font-family:ui-monospace,monospace}}.inline{{display:inline}}.topline{{display:flex;align-items:flex-start;justify-content:space-between;gap:18px}}.login-wrap{{min-height:100vh;display:grid;place-items:center;padding:20px}}.login{{width:min(100%,440px);padding:32px;border:1px solid #e1e8f1;border-radius:18px;background:#fff;box-shadow:0 20px 60px #17243b12}}.login h1{{margin:24px 0 8px;font-size:27px}}.login .sub{{margin-bottom:22px}}footer{{margin-top:24px;color:#8794a7;font-size:12px}}
@media(max-width:920px){{.grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}.layout{{grid-template-columns:205px minmax(0,1fr)}}}}
@media(max-width:650px){{.layout{{display:block}}aside{{padding:12px 14px}}.brand{{padding:3px 5px 12px}}.nav{{display:flex;overflow:auto}}.nav-link{{white-space:nowrap;padding:9px}}.side-note{{display:none}}header{{height:56px;padding:0 18px}}main{{padding-top:25px}}.grid{{gap:9px}}.card{{padding:14px}}.value{{font-size:25px}}.form-grid{{grid-template-columns:1fr}}.panel{{padding:16px;overflow-x:auto}}}}
.password-field{{display:flex;gap:8px;align-items:stretch}}.password-field input{{flex:1;min-width:0}}.password-toggle{{min-width:76px;border:1px solid #ccd6e3;border-radius:9px;background:#fff;color:#253750;font-size:13px;font-weight:650;cursor:pointer}}.password-toggle:focus-visible{{outline:3px solid #9bbcff;outline-offset:2px}}
</style>
</head>
<body>{content}<script nonce="{nonce}">document.addEventListener('click',function(event){{const button=event.target.closest('.password-toggle');if(!button)return;const input=document.getElementById(button.getAttribute('aria-controls'));if(!input)return;const visible=input.type==='password';input.type=visible?'text':'password';button.textContent=visible?'Ocultar':'Mostrar';button.setAttribute('aria-label',visible?'Ocultar contraseña':'Mostrar contraseña');button.setAttribute('aria-pressed',String(visible));}});document.addEventListener('input',function(event){{if(!event.target.matches('[data-password-primary],[data-password-confirmation]'))return;const password=document.querySelector('[data-password-primary]');const confirmation=document.querySelector('[data-password-confirmation]');const message=document.querySelector('[data-password-match-message]');if(!password||!confirmation||!message)return;const matches=password.value===confirmation.value;confirmation.setCustomValidity(matches?'':'Las contraseñas no coinciden.');message.textContent=confirmation.value?(matches?'Las contraseñas coinciden.':'Las contraseñas no coinciden.') : '';message.className=confirmation.value?(matches?'notice success':'notice error'): 'muted';}});document.addEventListener('submit',function(event){{const form=event.target;if(!form.matches('form[action="/admin/setup"]'))return;const password=form.querySelector('[data-password-primary]');const confirmation=form.querySelector('[data-password-confirmation]');if(!password||!confirmation||password.value!==confirmation.value){{event.preventDefault();confirmation?.setCustomValidity('Las contraseñas no coinciden.');confirmation?.reportValidity();}}}});</script></body>
</html>"""
    headers = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Content-Security-Policy": f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"}
    if no_store:
        headers["Cache-Control"] = "no-store"
    return HTMLResponse(page, headers=headers)


def _login_page(error: str = "", no_store: bool = False) -> HTMLResponse:
    message = f'<div class="notice error" role="alert">{html.escape(error)}</div>' if error else ""
    if _registration_available():
        form_content = f"""<p class="sub">Crea la única cuenta propietaria de este entorno local. El alta se cerrará al guardar la cuenta.</p>{message}
<form method="post" action="/admin/setup">
<label>Correo propietario<input type="email" name="email" autocomplete="username" required maxlength="254"></label>
<label style="margin-top:14px">Contraseña (16 caracteres como mínimo)<span class="password-field"><input id="owner-password" type="password" name="password" autocomplete="new-password" required minlength="16" maxlength="256" aria-describedby="password-requirements" data-password-primary><button class="password-toggle" type="button" aria-label="Mostrar contraseña" aria-controls="owner-password" aria-pressed="false">Mostrar</button></span></label>
<small id="password-requirements" class="muted">Usa al menos 16 caracteres. Se permiten espacios; no reutilices una contraseña de otra cuenta.</small>
<label style="margin-top:14px">Confirmar contraseña<span class="password-field"><input id="owner-password-confirmation" type="password" name="password_confirmation" autocomplete="new-password" required minlength="16" maxlength="256" data-password-confirmation><button class="password-toggle" type="button" aria-label="Mostrar confirmación de contraseña" aria-controls="owner-password-confirmation" aria-pressed="false">Mostrar</button></span></label>
<small class="muted" data-password-match-message aria-live="polite"></small>
<div class="actions"><button class="btn primary" type="submit">Crear cuenta propietaria</button></div></form>"""
    elif not _session_secret_is_configured():
        form_content = f"""{message}<div class="notice error" role="alert">El panel no está configurado: define ADMIN_SESSION_SECRET con al menos 32 bytes aleatorios en .env y reinicia el servidor antes de crear la cuenta propietaria.</div>"""
    else:
        form_content = f"""<p class="sub">Inicia sesión para supervisar el servicio y gestionar accesos de integración.</p>{message}
<form method="post" action="/admin/login">
<label>Correo administrador<input type="email" name="email" autocomplete="username" required maxlength="254"></label>
<label style="margin-top:14px">Contraseña<span class="password-field"><input id="admin-password" type="password" name="password" autocomplete="current-password" required maxlength="256"><button class="password-toggle" type="button" aria-label="Mostrar contraseña" aria-controls="admin-password" aria-pressed="false">Mostrar</button></span></label>
<div class="actions"><button class="btn primary" type="submit">Iniciar sesión</button></div></form>"""
    return _page("Iniciar sesión", f"""<div class="login-wrap"><section class="login" aria-labelledby="login-title">
<div class="brand"><span class="mark" aria-hidden="true">R</span><span>API RUC Ecuador</span></div>
<p class="eyebrow">Área privada · Solo administración</p><h1 id="login-title">Panel Maestro</h1>
{form_content}
<footer>Este panel está limitado al entorno local. No compartas las credenciales.</footer></section></div>""", no_store=no_store or bool(error))


def _require_local(request: Request) -> None:
    if not _local_mode():
        raise HTTPException(status_code=404)
    host = request.client.host if request.client else ""
    forwarded_for = request.headers.get("x-forwarded-for")
    host_header = request.headers.get("host", "").split(":", 1)[0].strip("[]")
    if host not in {"127.0.0.1", "::1", "testclient"} or forwarded_for or host_header not in {"127.0.0.1", "localhost", "testserver", "[::1]"}:
        raise HTTPException(status_code=404)


def _registration_available() -> bool:
    return _local_mode() and _session_secret_is_configured() and not store.admin_accounts_exist()


def _require_admin(request: Request) -> dict[str, object]:
    _require_local(request)
    session = _session(request)
    if not session or session.get("role") not in {"owner", "support"}:
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})
    return session


def _require_owner(request: Request) -> dict[str, object]:
    session = _require_admin(request)
    if session.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Esta acción requiere el rol propietario.")
    return session


async def _form(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > 16 * 1024:
        raise HTTPException(status_code=413, detail="El formulario supera el tamaño permitido.")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().casefold() != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="Formato de formulario no admitido.")
    try:
        decoded_body = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="El formulario contiene texto no válido.") from exc
    try:
        parsed = parse_qs(decoded_body, keep_blank_values=True, max_num_fields=30, strict_parsing=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="El formulario tiene un formato no válido.") from exc
    return {key: values[0] for key, values in parsed.items() if values}


def _check_csrf(request: Request, session: dict[str, object], form: dict[str, str]) -> None:
    if not _same_origin(request) or not hmac.compare_digest(str(session.get("csrf", "")), form.get("csrf", "")):
        raise HTTPException(status_code=403, detail="Solicitud no autorizada.")


def _shell(request: Request, session: dict[str, object], title: str, content: str, active: str = "") -> HTMLResponse:
    email = html.escape(str(session.get("email", "")))
    shell = f"""<div class="layout"><aside><div class="brand"><span class="mark" aria-hidden="true">R</span><span>Panel Maestro</span></div>
<nav class="nav" aria-label="Navegación principal">{_navigation(active)}</nav>
<div class="side-note">Entorno local de pruebas.<br>Este panel no es todavía apto para exponer en Cloudflare.</div></aside>
<div class="main"><header><strong>{html.escape(title)}</strong><span>Sesión: {email} &nbsp;·&nbsp; <form class="inline" method="post" action="/admin/logout"><input type="hidden" name="csrf" value="{html.escape(str(session.get('csrf', '')))}"><button class="logout" type="submit">Cerrar sesión</button></form></span></header>
<main>{content}</main></div></div>"""
    return _page(title, shell, active, no_store=True)


def _navigation(active: str) -> str:
    links = [("overview", "/admin", "Resumen"), ("integrations", "/admin/integrations", "Accesos API"), ("settings", "/admin/settings", "Configuración")]
    return "".join(f'<a class="nav-link {"active" if key == active else ""}" href="{href}">{label}</a>' for key, href, label in links)


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_dashboard(request: Request) -> HTMLResponse:
    _require_local(request)
    session = _session(request)
    if not session:
        return _login_page()
    if session.get("role") not in {"owner", "support"}:
        raise HTTPException(status_code=403, detail="La sesión no tiene un rol administrativo válido.")
    data = store.admin_overview()
    counts = data["counts"]
    cards = [("Organizaciones", counts["organizations"]), ("Integraciones activas", counts["active_integrations"]), ("Credenciales activas", counts["active_credentials"]), ("Resultados manuales guardados", counts["saved_manual_results"])]
    card_html = "".join(f'<article class="card"><div class="label">{label}</div><div class="value">{value}</div></article>' for label, value in cards)
    sources = data["sources"]
    source_rows = "".join(f'<tr><td>{html.escape(str(row["source_id"]))}</td><td>{html.escape(str(row["authority"]))}</td><td><span class="badge">{html.escape(str(row["status"]))}</span></td><td>{html.escape(str(row["synced_at"] or "Sin sincronizar"))}</td><td>{html.escape(str(row["record_count"] if row["record_count"] is not None else "—"))}</td></tr>' for row in sources)
    if not source_rows:
        source_rows = '<tr><td colspan="5" class="empty">Todavía no hay sincronizaciones registradas.</td></tr>'
    events = data["recent_events"]
    event_rows = "".join(f'<tr><td>{html.escape(str(row["occurred_at"]))}</td><td>{html.escape(str(row["actor_email"]))}</td><td>{html.escape(str(row["action"]))}</td><td>{html.escape(str(row["target_id"] or "—"))}</td></tr>' for row in events)
    if not event_rows:
        event_rows = '<tr><td colspan="4" class="empty">No hay acciones administrativas registradas.</td></tr>'
    usage = data["usage_7d"]
    usage_rows = "".join(
        f'<tr><td>{html.escape(str(row["organization_name"]))}</td><td>{html.escape(str(row["integration_name"]))}</td><td>{row["requests_7d"]}</td><td>{row["errors_7d"]}</td></tr>'
        for row in data["usage_by_integration"]
    ) or '<tr><td colspan="4" class="empty">Sin peticiones autenticadas registradas esta semana.</td></tr>'
    content = f"""<p class="eyebrow">Operaciones</p><h1>Resumen del servicio</h1><p class="sub">Vista local de configuración y datos registrados; no representa telemetría de producción.</p>
<div class="grid">{card_html}</div>
<div class="grid" style="margin-top:14px"><article class="card"><div class="label">Peticiones autenticadas · 7 días</div><div class="value">{usage["requests_7d"]}</div></article><article class="card"><div class="label">Respuestas de error · 7 días</div><div class="value">{usage["errors_7d"]}</div></article></div>
<section class="panel"><h2>Uso por integración · últimos 7 días</h2><div style="overflow-x:auto"><table><thead><tr><th>Organización</th><th>Integración</th><th>Peticiones</th><th>Errores</th></tr></thead><tbody>{usage_rows}</tbody></table></div></section>
<section class="panel"><h2>Estado de fuentes</h2><div class="muted">El estado se obtiene de las últimas sincronizaciones almacenadas.</div><div style="overflow-x:auto;margin-top:12px"><table><thead><tr><th>Fuente</th><th>Autoridad</th><th>Estado</th><th>Última sincronización</th><th>Registros</th></tr></thead><tbody>{source_rows}</tbody></table></div></section>
<section class="panel"><h2>Actividad administrativa reciente</h2><div style="overflow-x:auto"><table><thead><tr><th>Fecha</th><th>Usuario</th><th>Acción</th><th>Recurso</th></tr></thead><tbody>{event_rows}</tbody></table></div></section>
<div class="notice">La cuota y el rate limit por integración todavía no se hacen cumplir. Las estadísticas se generan localmente; el despliegue Cloudflare aún requiere su adaptador D1.</div>"""
    return _shell(request, session, "Resumen", content, "overview")


@router.get("/admin/login", response_class=HTMLResponse, include_in_schema=False)
def admin_login_page(request: Request) -> HTMLResponse:
    _require_local(request)
    return _login_page()


@router.post("/admin/setup", include_in_schema=False)
async def admin_initial_setup(request: Request):
    _require_local(request)
    if not _local_mode() or store.admin_accounts_exist():
        raise HTTPException(status_code=404)
    if not _session_secret_is_configured():
        return _login_page(
            "El panel no está configurado. Define ADMIN_SESSION_SECRET con al menos 32 bytes aleatorios y reinicia el servidor antes de crear la cuenta propietaria.",
            no_store=True,
        )
    if not _same_origin(request):
        return _login_page("El navegador rechazó el envío porque no pudo confirmar que procede de esta misma dirección. Abre de nuevo el panel usando exactamente la URL que aparece en la barra del navegador y vuelve a intentarlo.", no_store=True)
    form = await _form(request)
    email = form.get("email", "").strip().casefold()
    password = form.get("password", "")
    confirmation = form.get("password_confirmation", "")
    if not email or len(email) > 254 or "@" not in email:
        return _login_page("Introduce una dirección de correo válida.", no_store=True)
    if len(password) < 16 or len(password) > 256:
        return _login_page("La contraseña debe tener entre 16 y 256 caracteres.", no_store=True)
    if not hmac.compare_digest(password, confirmation):
        return _login_page("Las contraseñas no coinciden.", no_store=True)
    if password.isspace() or password.casefold() in {"passwordpassword", "contraseñacontraseña", "1234567890123456"}:
        return _login_page("Elige una contraseña menos predecible; no uses únicamente espacios ni una clave común.", no_store=True)
    created = store.admin_create_initial_owner(
        email,
        _PASSWORD_HASHER.hash(password),
        datetime.now(timezone.utc).isoformat(),
        request.headers.get("x-request-id"),
    )
    if not created:
        raise HTTPException(status_code=404)
    now = int(time.time())
    payload = {
        "email": email,
        "role": "owner",
        "iat": now,
        "exp": now + _SESSION_MAX_AGE,
        "csrf": secrets.token_urlsafe(32),
    }
    response = RedirectResponse("/admin", status_code=303, headers={"Cache-Control": "no-store"})
    response.set_cookie(
        _SESSION_COOKIE,
        _encode_session(payload),
        max_age=_SESSION_MAX_AGE,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/admin",
    )
    response.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})
    return response


@router.post("/admin/login", response_class=HTMLResponse, include_in_schema=False)
async def admin_login(request: Request) -> HTMLResponse:
    _require_local(request)
    if not _same_origin(request):
        return _login_page("El navegador rechazó el envío porque no pudo confirmar que procede de esta misma dirección. Abre de nuevo el panel usando exactamente la URL que aparece en la barra del navegador y vuelve a intentarlo.", no_store=True)
    form = await _form(request)
    submitted_email = form.get("email", "").strip().casefold()
    submitted_password = form.get("password", "")
    account = store.admin_account(submitted_email)
    if not _session_secret_is_configured() or not account or not _PASSWORD_HASHER.verify(submitted_password, account["password_hash"]):
        return _login_page("No se pudo iniciar sesión. Revisa las credenciales y la configuración local.")
    now = int(time.time())
    payload = {"email": account["email"], "role": account["role"], "iat": now, "exp": now + _SESSION_MAX_AGE, "csrf": secrets.token_urlsafe(32)}
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(_SESSION_COOKIE, _encode_session(payload), max_age=_SESSION_MAX_AGE, httponly=True, secure=request.url.scheme == "https", samesite="strict", path="/admin")
    response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})
    return response


@router.post("/admin/logout", include_in_schema=False)
async def admin_logout(request: Request) -> RedirectResponse:
    session = _require_admin(request)
    form = await _form(request)
    _check_csrf(request, session, form)
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(_SESSION_COOKIE, path="/admin", httponly=True, secure=request.url.scheme == "https", samesite="strict")
    return response


@router.get("/admin/integrations", response_class=HTMLResponse, include_in_schema=False)
def admin_integrations_page(request: Request) -> HTMLResponse:
    session = _require_admin(request)
    integrations = store.admin_list_integrations()
    created_key = request.cookies.get(_FLASH_COOKIE)
    key_panel = ""
    if created_key and session.get("role") == "owner":
        key_panel = f'<section class="panel"><h2>Credencial generada · se muestra una sola vez</h2><div class="notice success">Copia y guarda esta credencial ahora. No volverá a mostrarse y la base solo conserva su hash.</div><div class="secret">{html.escape(created_key)}</div></section>'
    revoke_action = (
        lambda integration_id, disabled: f'''<form method="post" action="/admin/integrations/{html.escape(str(integration_id))}/revoke"><input type="hidden" name="csrf" value="{html.escape(str(session.get("csrf", "")))}"><button class="btn danger" type="submit" {disabled}>Revocar</button></form>
<form method="post" action="/admin/integrations/{html.escape(str(integration_id))}/rotate"><input type="hidden" name="csrf" value="{html.escape(str(session.get("csrf", "")))}"><button class="btn" type="submit" {disabled}>Rotar clave</button></form>'''
        if session.get("role") == "owner"
        else '<span class="muted">Solo propietario</span>'
    )
    rows = "".join(
        f'''<tr><td>{html.escape(str(row["organization_name"]))}<div class="muted">{html.escape(str(row["organization_id"]))}</div></td>
<td>{html.escape(str(row["integration_name"]))}<div class="muted">{html.escape(str(row["integration_id"]))}</div></td>
<td><span class="badge">{html.escape(str(row["integration_status"]))}</span></td><td>{row["active_credentials"]}</td><td>{row["requests_per_minute"]}/min · {row["daily_limit"]}/día</td><td>
{revoke_action(row["integration_id"], 'disabled' if row["integration_status"] == 'revoked' else '')}</td></tr>'''
        for row in integrations
    ) or '<tr><td colspan="6" class="empty">Aún no se han creado integraciones.</td></tr>'
    content = f"""<p class="eyebrow">Accesos de integradores</p><h1>Organizaciones e integraciones</h1><p class="sub">Cada nueva integración crea una organización activa y una credencial independiente.</p>{key_panel}
<section class="panel"><h2>Crear integración de prueba</h2><div class="notice">La credencial se enseña únicamente en la respuesta inmediata de creación. No compartas capturas con el secreto. Las cuotas quedan registradas, pero aún no hay enforcement de tasa/cuota.</div>
{'<form method="post" action="/admin/integrations"><input type="hidden" name="csrf" value="' + html.escape(str(session.get('csrf', ''))) + '"><div class="form-grid">' if session.get('role') == 'owner' else '<div class="notice">El rol de soporte puede consultar este panel, pero no crear ni revocar credenciales.</div><div class="form-grid">'}
<label>Nombre de organización<input name="organization_name" required maxlength="120" autocomplete="organization"></label><label>Nombre del sistema/integración<input name="integration_name" required maxlength="120"></label>
<label>Peticiones/minuto registradas<input type="number" name="requests_per_minute" value="30" min="1" max="10000" required></label><label>Cuota diaria registrada<input type="number" name="daily_limit" value="1000" min="1" max="10000000" required></label>
</div>{'<div class="actions"><button class="btn primary" type="submit">Crear y generar credencial</button></div></form>' if session.get('role') == 'owner' else '</div>'}</section>
<section class="panel"><h2>Integraciones existentes</h2><div style="overflow-x:auto"><table><thead><tr><th>Organización</th><th>Integración</th><th>Estado</th><th>Claves activas</th><th>Límites registrados</th><th>Acción</th></tr></thead><tbody>{rows}</tbody></table></div></section>"""
    response = _shell(request, session, "Accesos API", content, "integrations")
    if created_key:
        response.delete_cookie(_FLASH_COOKIE, path="/admin/integrations", httponly=True, secure=request.url.scheme == "https", samesite="strict")
    return response


@router.post("/admin/integrations", include_in_schema=False)
async def admin_create_integration(request: Request) -> RedirectResponse:
    session = _require_owner(request)
    form = await _form(request)
    _check_csrf(request, session, form)
    organization_name = form.get("organization_name", "").strip()
    integration_name = form.get("integration_name", "").strip()
    try:
        requests_per_minute = int(form.get("requests_per_minute", ""))
        daily_limit = int(form.get("daily_limit", ""))
    except ValueError:
        raise HTTPException(status_code=400, detail="Límites inválidos.") from None
    if not organization_name or not integration_name or len(organization_name) > 120 or len(integration_name) > 120:
        raise HTTPException(status_code=400, detail="Los nombres son obligatorios y deben tener hasta 120 caracteres.")
    if not 1 <= requests_per_minute <= 10000 or not 1 <= daily_limit <= 10000000:
        raise HTTPException(status_code=400, detail="Los límites están fuera del rango permitido.")
    token = "ruc_test_" + secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()
    expires_at = datetime.fromtimestamp(time.time() + 90 * 24 * 60 * 60, timezone.utc).isoformat()
    integration_id = secrets.token_hex(16)
    store.admin_create_integration(
        secrets.token_hex(16), organization_name, integration_id, integration_name,
        secrets.token_hex(16), token_digest(token), expires_at, requests_per_minute, daily_limit,
        str(session["email"]), now,
    )
    response = RedirectResponse("/admin/integrations", status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    response.set_cookie(_FLASH_COOKIE, token, max_age=60, httponly=True, secure=request.url.scheme == "https", samesite="strict", path="/admin/integrations")
    return response


@router.post("/admin/integrations/{integration_id}/revoke", include_in_schema=False)
async def admin_revoke_integration(request: Request, integration_id: str) -> RedirectResponse:
    session = _require_owner(request)
    form = await _form(request)
    _check_csrf(request, session, form)
    if not _valid_hex_id(integration_id):
        raise HTTPException(status_code=404)
    if not store.admin_revoke_integration(integration_id, str(session["email"]), datetime.now(timezone.utc).isoformat()):
        raise HTTPException(status_code=404)
    return RedirectResponse("/admin/integrations", status_code=303)


@router.post("/admin/integrations/{integration_id}/rotate", include_in_schema=False)
async def admin_rotate_integration_credential(request: Request, integration_id: str) -> RedirectResponse:
    session = _require_owner(request)
    form = await _form(request)
    _check_csrf(request, session, form)
    if not _valid_hex_id(integration_id):
        raise HTTPException(status_code=404)
    token = "ruc_test_" + secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = datetime.fromtimestamp(now.timestamp() + 90 * 24 * 60 * 60, timezone.utc).isoformat()
    if not store.admin_rotate_credential(
        integration_id, secrets.token_hex(16), token_digest(token), expires_at,
        str(session["email"]), now.isoformat(),
    ):
        raise HTTPException(status_code=404)
    response = RedirectResponse("/admin/integrations", status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    response.set_cookie(_FLASH_COOKIE, token, max_age=60, httponly=True, secure=request.url.scheme == "https", samesite="strict", path="/admin/integrations")
    return response


def _valid_hex_id(value: str) -> bool:
    return len(value) == 32 and all(char in "0123456789abcdef" for char in value)


@router.get("/admin/settings", response_class=HTMLResponse, include_in_schema=False)
def admin_settings_page(request: Request) -> HTMLResponse:
    session = _require_admin(request)
    settings = [
        ("Entorno", os.getenv("APP_ENV", "production")),
        ("Secreto de sesión", "Configurado" if _session_secret_is_configured() else "Falta o es inseguro"),
        ("Título", os.getenv("API_TITLE", "API RUC Ecuador")),
        ("Base de datos local", "SQLite"),
        ("Ruta de base", os.getenv("DATABASE_PATH", "./data/snapshots.sqlite3")),
        ("Año configurado de dataset SRI", os.getenv("SRI_DATASET_YEAR", "no definido")),
        ("Documentación OpenAPI pública", "Desactivada"),
        ("Autenticación del panel", "Contraseña local + cookie firmada"),
        ("Rate limit enforcement", "No implementado"),
        ("Despliegue Cloudflare", "No configurado"),
    ]
    rows = "".join(f'<tr><td>{html.escape(name)}</td><td>{html.escape(value)}</td></tr>' for name, value in settings)
    content = f"""<p class="eyebrow">Estado y configuración</p><h1>Configuración del servicio</h1><p class="sub">Solo lectura: la configuración se gestiona localmente mediante variables de entorno; los secretos no se muestran.</p>
<section class="panel"><h2>Valores activos</h2><div style="overflow-x:auto"><table><thead><tr><th>Parámetro</th><th>Valor</th></tr></thead><tbody>{rows}</tbody></table></div></section>
<div class="notice">Este panel no cambia variables de entorno ni configuraciones de Cloudflare. El login current es exclusivamente para pruebas locales; antes de desplegar, debe sustituirse por verificación de identidad con Cloudflare Access.</div>"""
    return _shell(request, session, "Configuración", content, "settings")
