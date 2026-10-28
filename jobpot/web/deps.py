import secrets
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ..db import get_db
from ..models import User

templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


class LoginRequired(Exception):
    pass


class PasswordChangeRequired(Exception):
    pass


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = request.session["csrf"] = secrets.token_urlsafe(32)
    return tok


templates.env.globals["csrf_token"] = csrf_token


async def csrf_protect(request: Request):
    if request.method != "POST":
        return
    form = await request.form()  # cached on the request, so route Form() params still work
    sent = str(form.get("csrf") or request.headers.get("x-csrf-token", ""))
    expected = request.session.get("csrf", "")
    if not expected or not secrets.compare_digest(sent.encode(), expected.encode()):
        raise HTTPException(403, "This form expired. Reload the page and try again.")


def session_version(user: User) -> str:
    # Changes whenever the password hash changes, which signs out other sessions.
    return (user.password_hash or "")[-16:]


def login_session(request: Request, user: User):
    request.session.clear()
    request.session.update(uid=user.id, sv=session_version(user), csrf=secrets.token_urlsafe(32))


def optional_user(request: Request, db=Depends(get_db)) -> User | None:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if user and request.session.get("sv") == session_version(user) and user.password_hash:
        return user
    return None


def current_user(request: Request, user=Depends(optional_user)) -> User:
    if user is None:
        request.session.pop("uid", None)
        raise LoginRequired
    if user.must_change_password and not request.url.path.startswith(("/settings", "/logout")):
        raise PasswordChangeRequired
    return user


def admin_user(user=Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Only the administrator can open this page.")
    return user


def flash(request: Request, message: str, kind: str = "ok"):
    request.session["flash"] = request.session.get("flash", []) + [[kind, message]]


def render(request: Request, name: str, user: User | None = None, status: int = 200, **ctx):
    flashes = request.session.pop("flash", [])
    return templates.TemplateResponse(request, name, {"user": user, "flashes": flashes, **ctx}, status_code=status)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)
