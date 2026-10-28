"""Secrets at rest: one master key, a separate Fernet key per user (HKDF), so one user's rows are useless under another's key."""
import base64
import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken  # noqa: F401  re-exported for callers
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import DATA_DIR


@lru_cache
def master_key() -> bytes:
    env = os.environ.get("JOBPOT_SECRET_KEY")
    if env:
        if len(env) < 32:
            raise RuntimeError("JOBPOT_SECRET_KEY must be at least 32 characters")
        return env.encode()
    path = DATA_DIR / "secret.key"
    if not path.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:  # the other process (web/worker) won the race
            pass
        else:
            with os.fdopen(fd, "wb") as f:
                f.write(base64.urlsafe_b64encode(os.urandom(32)))
    return path.read_bytes().strip()


def derive(info: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info.encode()).derive(master_key())


@lru_cache(maxsize=1024)
def _fernet(user_id: int) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(derive(f"user:{user_id}")))


def encrypt(user_id: int, plaintext: str) -> str:
    return _fernet(user_id).encrypt(plaintext.encode()).decode()


def decrypt(user_id: int, token: str) -> str:
    """Raises InvalidToken if the value was not encrypted for this user."""
    return _fernet(user_id).decrypt(token.encode()).decode()
