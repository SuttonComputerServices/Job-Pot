import hmac
import logging
import time

import pyotp
from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from sqlalchemy import func

from .. import ai, crypto, mail
from ..db import get_db
from ..models import PasswordReset, User
from ..security import (EMAIL_RE, RESET_SECONDS, SETUP_TOKEN, USERNAME_RE, USERNAME_RULE, find_user, hash_password, login_throttle,
                        new_reset_token, password_problem, reset_mail_throttle, reset_token_user, setup_token_ok,
                        signup_throttle, verify_password)
from .deps import current_user, flash, login_session, optional_user, redirect, render

router = APIRouter()
log = logging.getLogger("jobpot.auth")
_DUMMY_HASH = hash_password("timing-equaliser-not-a-password")
PENDING_2FA_SECONDS = 300


def totp_ok(user: User, code: str) -> bool:
    """Accepts the current code or one step either side, never a step already used (blocks replay). Caller commits."""
    if not user.totp_secret_enc:
        return False
    totp, code = pyotp.TOTP(crypto.decrypt(user.id, user.totp_secret_enc)), code.replace(" ", "")
    now_step = int(time.time()) // totp.interval
    for step in (now_step - 1, now_step, now_step + 1):
        if step > (user.totp_last_step or 0) and hmac.compare_digest(totp.at(step * totp.interval).encode(), code.encode()):
            user.totp_last_step = step
            return True
    return False


def client_key(request: Request, name: str) -> str:
    # Per client, so a stranger's wrong guesses can't lock the real owner out.
    return f"{name}|{request.client.host if request.client else '?'}"


def _admin_pending(db) -> bool:
    return db.query(User).filter(User.is_admin, User.password_hash.is_(None)).count() > 0


def signups_open(db) -> bool:
    return ai.get_setting(db, "signup_enabled") == "1" and not _admin_pending(db)


def _login_page(request: Request, db, status: int = 200, **ctx):
    return render(request, "login.html", signup=signups_open(db), status=status, **ctx)


@router.get("/login")
def login_form(request: Request, user=Depends(optional_user), db=Depends(get_db)):
    if user:
        return redirect("/")
    return _login_page(request, db)


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(""), db=Depends(get_db)):
    user = find_user(db, username)
    if user and user.is_admin and user.password_hash is None:
        return redirect("/setup")
    key = client_key(request, username.strip().lower())
    if not login_throttle.hit(key):
        return _login_page(request, db, error="Too many failed attempts. Wait five minutes and try again.", status=429)
    if not verify_password(user.password_hash if user else _DUMMY_HASH, password) or not user:
        return _login_page(request, db, error="That username and password don't match.", username=username, status=401)
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
    if not user or not login_throttle.hit(key):
        request.session.clear()
        return _login_page(request, db, error="Too many wrong codes. Wait five minutes and sign in again.", status=429)
    if not totp_ok(user, code):
        return render(request, "login_2fa.html", error="That code didn't work. Codes change every 30 seconds.", status=401)
    db.commit()  # stores the used step
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
    if not login_throttle.hit(client_key(request, "setup")):
        return render(request, "setup.html", error="Too many wrong setup tokens. Wait five minutes.", status=429, **ctx)
    if not setup_token_ok(token):
        return render(request, "setup.html", error="That setup token is wrong. Copy it from the server log or the token file.", status=401, **ctx)
    if problem := password_problem(password, confirm):
        return render(request, "setup.html", error=problem, status=400, **ctx)
    admin = db.query(User).filter(User.is_admin, User.password_hash.is_(None)).order_by(User.id).first()
    admin.password_hash = hash_password(password)
    db.commit()
    SETUP_TOKEN.unlink(missing_ok=True)
    login_session(request, admin)
    return redirect("/settings?welcome=1")


@router.get("/signup")
def signup_form(request: Request, user=Depends(optional_user), db=Depends(get_db)):
    if user:
        return redirect("/")
    if not signups_open(db):
        return redirect("/login")
    return render(request, "signup.html")


@router.post("/signup")
def signup(request: Request, username: str = Form(...), email: str = Form(...), password: str = Form(...),
           confirm: str = Form(...), db=Depends(get_db)):
    if not signups_open(db):
        return redirect("/login")
    username, email = username.strip(), email.strip()
    page = lambda error, status: render(request, "signup.html", error=error, username=username, email=email, status=status)
    if not signup_throttle.hit(client_key(request, "signup")):
        return page("Too many sign-up attempts from here. Try again in an hour.", 429)
    if not USERNAME_RE.match(username):
        return page(USERNAME_RULE, 400)
    if not EMAIL_RE.match(email):
        return page("Enter a working email address. It's where password reset links go.", 400)
    if find_user(db, username):
        return page(f"The username {username} is taken. Choose another.", 400)
    if problem := password_problem(password, confirm):
        return page(problem, 400)
    user = User(username=username, email=email, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    login_session(request, user)
    flash(request, "Account created. Add your details here, then your resume and job sites.")
    return redirect("/profile")


def _send_reset(mb, password: str, to: str, username: str, link: str):
    body = (f"Someone asked to reset the Job Pot password for the account {username}.\n\n"
            f"To choose a new password, open this link within {RESET_SECONDS // 60} minutes:\n{link}\n\n"
            "If you didn't ask for this, ignore this email. Your password stays the same.")
    try:
        mail.send_notice(mb, password, to, "Reset your Job Pot password", body)
    except ValueError as e:
        log.warning("Password reset email for %s failed: %s", username, e)


@router.get("/forgot")
def forgot_form(request: Request, db=Depends(get_db)):
    return render(request, "forgot.html", mail_ready=mail.system_mailbox(db) is not None)


@router.post("/forgot")
def forgot(request: Request, background: BackgroundTasks, who: str = Form(...), db=Depends(get_db)):
    sender = mail.system_mailbox(db)
    if sender is None:
        return render(request, "forgot.html", mail_ready=False, status=400)
    if not login_throttle.hit(client_key(request, "forgot")):
        return render(request, "forgot.html", mail_ready=True, error="Too many requests. Wait five minutes and try again.", status=429)
    who = who.strip()
    found = find_user(db, who)
    users = [found] if found else (db.query(User).filter(func.lower(User.email) == who.lower()).all() if "@" in who else [])
    base = ai.get_setting(db, "public_url")
    for u in users:
        if u.email and u.password_hash and reset_mail_throttle.hit(f"user:{u.id}"):
            # Sent after the response, so the reply takes the same time whether or not an account matched.
            background.add_task(_send_reset, *sender, u.email, u.username, f"{base}/reset/{new_reset_token(db, u)}")
    db.commit()
    return render(request, "forgot.html", mail_ready=True, sent=True)


@router.get("/reset/{token}")
def reset_form(request: Request, token: str, db=Depends(get_db)):
    user = reset_token_user(db, token)
    if user is None:
        return render(request, "reset.html", invalid=True, status=400)
    return render(request, "reset.html", token=token, needs_code=user.totp_enabled)


@router.post("/reset/{token}")
def reset(request: Request, token: str, password: str = Form(...), confirm: str = Form(...), code: str = Form(""),
          db=Depends(get_db)):
    user = reset_token_user(db, token)
    if user is None:
        return render(request, "reset.html", invalid=True, status=400)
    page = lambda error, status: render(request, "reset.html", token=token, needs_code=user.totp_enabled, error=error, status=status)
    if user.totp_enabled:
        # An emailed link alone isn't enough when two-step sign-in is on.
        if not login_throttle.hit(f"2fa:{user.id}"):
            return page("Too many wrong codes. Wait five minutes and try again.", 429)
        if not totp_ok(user, code):
            return page("That code didn't work. Codes change every 30 seconds.", 401)
    if problem := password_problem(password, confirm):
        return page(problem, 400)
    user.password_hash, user.must_change_password = hash_password(password), False
    db.query(PasswordReset).filter(PasswordReset.user_id == user.id).delete()
    db.commit()  # the new hash also signs out every other session
    request.session.clear()
    flash(request, "Password changed. Sign in with your new password.")
    return redirect("/login")


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
