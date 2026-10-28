import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from starlette.exceptions import HTTPException
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from ..config import HTTPS
from ..crypto import derive
from ..db import SessionLocal, init_db
from ..security import SETUP_TOKEN, ensure_admin
from . import account, auth
from .deps import LoginRequired, PasswordChangeRequired, csrf_protect, flash, optional_user, redirect, render

log = logging.getLogger("jobpot")


@asynccontextmanager
async def lifespan(_app):
    init_db()
    with SessionLocal() as db:
        if token := ensure_admin(db):
            print(f"\n=== Job Pot first-run setup ===\nSign in as 'admin' to set the password.\n"
                  f"Setup token: {token}\n(also stored in {SETUP_TOKEN})\n", flush=True)
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=derive("session").hex(), session_cookie="jobpot_session",
                   max_age=14 * 24 * 3600, same_site="lax", https_only=HTTPS)
app.mount("/static", StaticFiles(directory=Path(__file__).parent.parent / "static"), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.update({
        "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin",
    })
    if not request.url.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.exception_handler(LoginRequired)
async def _login_required(request, exc):
    return redirect("/login")


@app.exception_handler(PasswordChangeRequired)
async def _password_change(request, exc):
    flash(request, "Choose a new password before you continue.", "problem")
    return redirect("/settings")


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    with SessionLocal() as db:
        user = optional_user(request, db)
    return render(request, "error.html", user, status=exc.status_code, code=exc.status_code,
                  message=exc.detail if exc.status_code in (400, 403, 429) else "That page doesn't exist, or it isn't yours.")


@app.get("/healthz")
def healthz():
    return {"ok": True}


for module in (auth, account):
    app.include_router(module.router, dependencies=[Depends(csrf_protect)])
