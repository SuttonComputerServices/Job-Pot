import hmac
import os
import secrets
import threading
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import HTTPException
from sqlalchemy import func, select

from .config import DATA_DIR
from .models import User

_ph = PasswordHasher()
SETUP_TOKEN = DATA_DIR / "setup-token"
MIN_PASSWORD = 12


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(hashed: str | None, password: str) -> bool:
    if not hashed:
        return False
    try:
        return _ph.verify(hashed, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def password_problem(password: str, confirm: str) -> str | None:
    if password != confirm:
        return "Passwords do not match."
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    return None


def get_owned(db, model, obj_id, user):
    """The only way to load a user-owned row by id: someone else's row looks like a missing one."""
    obj = db.get(model, obj_id)
    if obj is None or obj.user_id != user.id:
        raise HTTPException(404)
    return obj


def find_user(db, username: str) -> User | None:
    return db.scalar(select(User).where(func.lower(User.username) == username.strip().lower()))


def ensure_admin(db) -> str | None:
    """Create the password-less admin on first boot. Returns the one-time setup token while setup is pending."""
    admin = db.scalar(select(User).where(User.is_admin).order_by(User.id))
    if admin is None:
        admin = User(username="admin", is_admin=True, password_hash=None)
        db.add(admin)
        db.commit()
    if admin.password_hash:
        SETUP_TOKEN.unlink(missing_ok=True)
        return None
    if not SETUP_TOKEN.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(SETUP_TOKEN, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(24))
    return SETUP_TOKEN.read_text().strip()


def setup_token_ok(token: str) -> bool:
    return SETUP_TOKEN.exists() and hmac.compare_digest(SETUP_TOKEN.read_text().strip(), token.strip())


class Throttle:
    """Counts attempts before they are checked, under a lock, so parallel guesses can't slip past the limit.
    shortcut: in-memory per process, resets on restart; move to the DB if running several web workers."""

    def __init__(self, limit: int = 5, window: int = 300):
        self.limit, self.window, self.fails, self._lock = limit, window, {}, threading.Lock()

    def hit(self, key: str) -> bool:
        """Record an attempt. False means locked: reject without checking the password."""
        with self._lock:
            count, since = self.fails.get(key, (0, time.monotonic()))
            if time.monotonic() - since >= self.window:
                count, since = 0, time.monotonic()
            if count >= self.limit:
                return False
            self.fails[key] = (count + 1, since)
            return True

    def ok(self, key: str):
        with self._lock:
            self.fails.pop(key, None)


login_throttle = Throttle()
