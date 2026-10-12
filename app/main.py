import hmac
import time
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import models  # noqa: F401  (enregistre les tables)
from .config import APP_PASSWORD, COOKIE_SECURE, SECRET_KEY
from .db import SessionLocal, migrate
from .routers import core, finance, imports, reports, settings
from .services import seed
from .templating import templates

STATIC = Path(__file__).parent / "static"
PUBLIC = ("/login", "/static/", "/manifest.webmanifest", "/sw.js", "/healthz")

migrate()
with SessionLocal() as _db:
    seed(_db)

app = FastAPI(title="Wazzzxpense", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if not path.startswith(PUBLIC) and not request.session.get("auth"):
        return RedirectResponse("/login", status_code=303)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


# Ajouté après le middleware de login pour s'exécuter avant lui (la session doit être chargée).
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", https_only=COOKIE_SECURE,
                   max_age=60 * 60 * 24 * 30, session_cookie="wzx")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

_failures: dict[str, list[float]] = defaultdict(list)


@app.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None, "configured": bool(APP_PASSWORD)})


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    now = time.time()
    _failures[ip] = [t for t in _failures[ip] if now - t < 900]
    if len(_failures[ip]) >= 8:
        return templates.TemplateResponse(request, "login.html", {
            "error": "Trop de tentatives, réessayez dans 15 minutes.", "configured": True}, status_code=429)
    if APP_PASSWORD and hmac.compare_digest(password.encode(), APP_PASSWORD.encode()):
        request.session.clear()
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
    _failures[ip].append(now)
    time.sleep(1)
    return templates.TemplateResponse(request, "login.html", {
        "error": "Mot de passe incorrect.", "configured": bool(APP_PASSWORD)}, status_code=401)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/healthz")
def healthz():
    return PlainTextResponse("ok")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return FileResponse(STATIC / "sw.js", media_type="application/javascript")


for r in (core.router, imports.router, finance.router, reports.router, settings.router):
    app.include_router(r)

