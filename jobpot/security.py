import hashlib
import hmac
import os
import re
import secrets
import threading
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import HTTPException
from sqlalchemy import func, select

from .config import DATA_DIR
from .models import PasswordReset, User

_ph = PasswordHasher()
SETUP_TOKEN = DATA_DIR / "setup-token"
MIN_PASSWORD = 12
RESET_SECONDS = 30 * 60
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
USERNAME_RULE = "Usernames are 3–64 characters: letters, numbers, dots, dashes and underscores."
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")


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


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_reset_token(db, user: User) -> str:
    """Replaces any earlier link for this user. Caller commits."""
    db.query(PasswordReset).filter(PasswordReset.user_id == user.id).delete()
    token = secrets.token_urlsafe(32)
    db.add(PasswordReset(user_id=user.id, token_hash=_token_hash(token), expires_at=int(time.time()) + RESET_SECONDS))
    return token


def reset_token_user(db, token: str) -> User | None:
    row = db.scalar(select(PasswordReset).where(PasswordReset.token_hash == _token_hash(token)))
    return db.get(User, row.user_id) if row and row.expires_at > time.time() else None


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
    return SETUP_TOKEN.exists() and hmac.compare_digest(SETUP_TOKEN.read_text().strip().encode(), token.strip().encode())


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
signup_throttle = Throttle(limit=10, window=3600)  # per client address: every attempt counts
reset_mail_throttle = Throttle(limit=3, window=3600)  # per account: stops someone flooding a person's inbox
