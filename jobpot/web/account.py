import re

import httpx
import pyotp
import segno
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from markupsafe import Markup

from .. import ai, crypto
from ..db import get_db
from ..models import User
from ..security import find_user, hash_password, password_problem, verify_password
from .auth import totp_ok
from .deps import admin_user, current_user, flash, login_session, redirect, render

router = APIRouter()
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
USERNAME_RULE = "Usernames are 3–64 characters: letters, numbers, dots, dashes and underscores."


def _qr(user: User) -> Markup | None:
    if not user.totp_secret_enc or user.totp_enabled:
        return None
    uri = pyotp.TOTP(crypto.decrypt(user.id, user.totp_secret_enc)).provisioning_uri(user.username, issuer_name="Job Pot")
    return Markup(segno.make(uri, error="m").svg_inline(scale=5, dark="#000", light="#fff"))


def _secret_text(user: User) -> str:
    return crypto.decrypt(user.id, user.totp_secret_enc) if user.totp_secret_enc and not user.totp_enabled else ""


# ---------- Profile ----------

@router.get("/profile")
def profile(request: Request, user=Depends(current_user)):
    return render(request, "profile.html", user)


@router.post("/profile")
def profile_save(request: Request, full_name: str = Form(""), email: str = Form(""), phone: str = Form(""),
                 location: str = Form(""), links: str = Form(""), user=Depends(current_user), db=Depends(get_db)):
    user.full_name, user.email, user.phone = full_name.strip()[:200], email.strip()[:200], phone.strip()[:50]
    user.location, user.links = location.strip()[:200], links.strip()[:2000]
    db.commit()
    flash(request, "Profile saved.")
    return redirect("/profile")


@router.post("/profile/username")
def username_change(request: Request, new_username: str = Form(...), password: str = Form(...),
                    user=Depends(current_user), db=Depends(get_db)):
    new_username = new_username.strip()
    if not verify_password(user.password_hash, password):
        return render(request, "profile.html", user, error="Your current password is wrong, so the username wasn't changed.", status=400)
    if not USERNAME_RE.match(new_username):
        return render(request, "profile.html", user, error=USERNAME_RULE, status=400)
    other = find_user(db, new_username)
    if other and other.id != user.id:
        return render(request, "profile.html", user, error=f"The username {new_username} is taken.", status=400)
    user.username = new_username
    db.commit()
    flash(request, f"Username changed. Sign in as {new_username} from now on.")
    return redirect("/profile")


# ---------- Settings ----------

@router.get("/settings")
def settings(request: Request, user=Depends(current_user)):
    return render(request, "settings.html", user, qr=_qr(user), secret=_secret_text(user),
                  welcome=request.query_params.get("welcome"))


@router.post("/settings/theme")
def theme(request: Request, theme: str = Form(...), back: str = Form("/settings"), user=Depends(current_user), db=Depends(get_db)):
    if theme not in ("system", "light", "dark"):
        raise HTTPException(400, "Unknown theme.")
    user.theme = theme
    db.commit()
    # Only same-site paths, never an absolute URL (open redirect).
    return redirect(back if back.startswith("/") and not back.startswith("//") else "/settings")


@router.post("/settings/automation")
def automation(request: Request, enabled: str = Form(""), user=Depends(current_user), db=Depends(get_db)):
    user.automation_enabled = enabled == "1"
    db.commit()
    flash(request, "Automation is on. Job Pot checks your sites and mailbox every few minutes." if enabled == "1" else "Automation is off.")
    return redirect("/settings")


@router.post("/settings/password")
def password_change(request: Request, current: str = Form(...), password: str = Form(...), confirm: str = Form(...),
                    user=Depends(current_user), db=Depends(get_db)):
    if not verify_password(user.password_hash, current):
        return render(request, "settings.html", user, qr=_qr(user), secret=_secret_text(user), error="Your current password is wrong.", status=400)
    if problem := password_problem(password, confirm):
        return render(request, "settings.html", user, qr=_qr(user), secret=_secret_text(user), error=problem, status=400)
    user.password_hash, user.must_change_password = hash_password(password), False
    db.commit()
    login_session(request, user)  # keeps this session; other sessions are signed out
    flash(request, "Password changed. Other devices have been signed out.")
    return redirect("/settings")


@router.post("/settings/2fa/start")
def twofa_start(request: Request, user=Depends(current_user), db=Depends(get_db)):
    if user.totp_enabled:
        return redirect("/settings")
    user.totp_secret_enc = crypto.encrypt(user.id, pyotp.random_base32())
    db.commit()
    return redirect("/settings#twofa")


@router.post("/settings/2fa/confirm")
def twofa_confirm(request: Request, code: str = Form(...), user=Depends(current_user), db=Depends(get_db)):
    if user.totp_enabled or not user.totp_secret_enc:
        return redirect("/settings")
    if not totp_ok(user, code):
        return render(request, "settings.html", user, qr=_qr(user), secret=_secret_text(user),
                      error="That code didn't match. Check your phone's clock is right and try the newest code.", status=400)
    user.totp_enabled = True
    db.commit()
    flash(request, "Two-step sign-in is on. You'll be asked for a code each time you sign in.")
    return redirect("/settings")


@router.post("/settings/2fa/disable")
def twofa_disable(request: Request, password: str = Form(...), code: str = Form(""), user=Depends(current_user), db=Depends(get_db)):
    if not verify_password(user.password_hash, password) or (user.totp_enabled and not totp_ok(user, code)):
        return render(request, "settings.html", user, qr=_qr(user), secret=_secret_text(user),
                      error="Password or code is wrong, so two-step sign-in is still on.", status=400)
    user.totp_enabled, user.totp_secret_enc = False, None
    db.commit()
    flash(request, "Two-step sign-in is off.")
    return redirect("/settings")


# ---------- Administration ----------

@router.get("/admin")
def admin(request: Request, user=Depends(admin_user), db=Depends(get_db)):
    enabled, url, model = ai.config(db)
    return render(request, "admin.html", user, users=db.query(User).order_by(User.id).all(),
                  ai_enabled=enabled, ollama_url=url, ollama_model=model, models=request.session.pop("ai_models", None))


@router.post("/admin/users")
def admin_create(request: Request, username: str = Form(...), password: str = Form(...),
                 user=Depends(admin_user), db=Depends(get_db)):
    username = username.strip()
    if not USERNAME_RE.match(username):
        flash(request, USERNAME_RULE, "problem")
    elif find_user(db, username):
        flash(request, f"The username {username} is taken.", "problem")
    elif problem := password_problem(password, password):
        flash(request, problem, "problem")
    else:
        db.add(User(username=username, password_hash=hash_password(password), must_change_password=True))
        db.commit()
        flash(request, f"Account {username} created. They'll be asked to choose their own password at first sign-in.")
    return redirect("/admin")


def _target(db, uid: int, admin: User) -> User:
    target = db.get(User, uid)
    if target is None:
        raise HTTPException(404)
    if target.id == admin.id:
        raise HTTPException(400, "Use Settings to change your own account.")
    return target


@router.post("/admin/users/{uid}/delete")
def admin_delete(request: Request, uid: int, user=Depends(admin_user), db=Depends(get_db)):
    target = _target(db, uid, user)
    db.delete(target)
    db.commit()
    flash(request, f"Account {target.username} and all its data were deleted.")
    return redirect("/admin")


@router.post("/admin/users/{uid}/reset")
def admin_reset(request: Request, uid: int, password: str = Form(...), user=Depends(admin_user), db=Depends(get_db)):
    target = _target(db, uid, user)
    if problem := password_problem(password, password):
        flash(request, problem, "problem")
        return redirect("/admin")
    target.password_hash, target.must_change_password = hash_password(password), True
    target.totp_enabled, target.totp_secret_enc = False, None
    db.commit()
    flash(request, f"{target.username} has a temporary password and two-step sign-in was turned off.")
    return redirect("/admin")


@router.post("/admin/ai")
def admin_ai(request: Request, enabled: str = Form(""), ollama_url: str = Form(""), ollama_model: str = Form(""),
             action: str = Form("save"), user=Depends(admin_user), db=Depends(get_db)):
    url = ollama_url.strip().rstrip("/")
    if url and not re.match(r"^https?://[^\s/]+(:\d+)?(/.*)?$", url):
        flash(request, "The Ollama address must start with http:// or https://", "problem")
        return redirect("/admin")
    if action == "test":
        try:
            request.session["ai_models"] = ai.list_models(url)
            flash(request, "Connected to Ollama.")
        except (httpx.HTTPError, ValueError) as e:
            flash(request, f"Couldn't reach Ollama at {url}: {e.__class__.__name__}", "problem")
        return redirect("/admin#ai")
    ai.set_setting(db, "ai_enabled", "1" if enabled == "1" else "0")
    ai.set_setting(db, "ollama_url", url)
    ai.set_setting(db, "ollama_model", ollama_model.strip()[:100])
    db.commit()
    flash(request, "AI settings saved.")
    return redirect("/admin#ai")
