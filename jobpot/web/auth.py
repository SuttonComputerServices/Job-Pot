import time

import pyotp
from fastapi import APIRouter, Depends, Form, Request

from .. import crypto
from ..db import get_db
from ..models import User
from ..security import SETUP_TOKEN, find_user, hash_password, login_throttle, password_problem, setup_token_ok, verify_password
from .deps import current_user, login_session, optional_user, redirect, render

router = APIRouter()
_DUMMY_HASH = hash_password("timing-equaliser-not-a-password")
PENDING_2FA_SECONDS = 300


def totp_ok(user: User, code: str) -> bool:
    if not user.totp_secret_enc:
        return False
    secret = crypto.decrypt(user.id, user.totp_secret_enc)
    # shortcut: a code can be reused inside its 30 s window; store the last used step to block replay if needed.
    return pyotp.TOTP(secret).verify(code.replace(" ", ""), valid_window=1)


def _admin_pending(db) -> bool:
    return db.query(User).filter(User.is_admin, User.password_hash.is_(None)).count() > 0


@router.get("/login")
def login_form(request: Request, user=Depends(optional_user)):
    if user:
        return redirect("/")
    return render(request, "login.html")


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(""), db=Depends(get_db)):
    key = username.strip().lower()
    if login_throttle.locked(key):
        return render(request, "login.html", error="Too many failed attempts. Wait five minutes and try again.", status=429)
    user = find_user(db, username)
    if user and user.is_admin and user.password_hash is None:
        return redirect("/setup")
    if not verify_password(user.password_hash if user else _DUMMY_HASH, password) or not user:
        login_throttle.fail(key)
        return render(request, "login.html", error="That username and password don't match.", username=username, status=401)
    login_throttle.ok(key)
    if user.totp_enabled:
        request.session.clear()
        request.session.update(pending_uid=user.id, pending_at=time.time())
        return redirect("/login/2fa")
    login_session(request, user)
    return redirect("/")


@router.get("/login/2fa")
def twofa_form(request: Request):
    if not request.session.get("pending_uid"):
        return redirect("/login")
    return render(request, "login_2fa.html")


@router.post("/login/2fa")
def twofa(request: Request, code: str = Form(...), db=Depends(get_db)):
    uid, at = request.session.get("pending_uid"), request.session.get("pending_at", 0)
    if not uid or time.time() - at > PENDING_2FA_SECONDS:
        request.session.clear()
        return redirect("/login")
    key = f"2fa:{uid}"
    user = db.get(User, uid)
    if login_throttle.locked(key) or not user:
        request.session.clear()
        return render(request, "login.html", error="Too many wrong codes. Wait five minutes and sign in again.", status=429)
    if not totp_ok(user, code):
        login_throttle.fail(key)
        return render(request, "login_2fa.html", error="That code didn't work. Codes change every 30 seconds.", status=401)
    login_throttle.ok(key)
    login_session(request, user)
    return redirect("/")


@router.get("/setup")
def setup_form(request: Request, db=Depends(get_db)):
    if not _admin_pending(db):
        return redirect("/login")
    return render(request, "setup.html", token_path=str(SETUP_TOKEN))


@router.post("/setup")
def setup(request: Request, token: str = Form(...), password: str = Form(...), confirm: str = Form(...), db=Depends(get_db)):
    if not _admin_pending(db):
        return redirect("/login")
    ctx = {"token_path": str(SETUP_TOKEN)}
    if login_throttle.locked("setup"):
        return render(request, "setup.html", error="Too many wrong setup tokens. Wait five minutes.", status=429, **ctx)
    if not setup_token_ok(token):
        login_throttle.fail("setup")
        return render(request, "setup.html", error="That setup token is wrong. Copy it from the server log or the token file.", status=401, **ctx)
    if problem := password_problem(password, confirm):
        return render(request, "setup.html", error=problem, status=400, **ctx)
    admin = db.query(User).filter(User.is_admin, User.password_hash.is_(None)).order_by(User.id).first()
    admin.password_hash = hash_password(password)
    db.commit()
    SETUP_TOKEN.unlink(missing_ok=True)
    login_session(request, admin)
    return redirect("/settings?welcome=1")


@router.post("/logout")
def logout(request: Request, user=Depends(current_user)):
    request.session.clear()
    return redirect("/login")


@router.get("/faq")
def faq(request: Request, user=Depends(optional_user)):
    return render(request, "faq.html", user)


@router.get("/about")
def about(request: Request, user=Depends(optional_user)):
    return render(request, "about.html", user)


@router.get("/terms")
def terms(request: Request, user=Depends(optional_user)):
    return render(request, "terms.html", user)
