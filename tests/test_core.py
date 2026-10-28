import pytest

from jobpot import crypto
from jobpot.models import User
from jobpot.security import SETUP_TOKEN, Throttle, ensure_admin, hash_password, setup_token_ok, verify_password


def test_encrypt_roundtrip_and_isolation():
    token = crypto.encrypt(1, "hunter2-linkedin")
    assert "hunter2" not in token
    assert crypto.decrypt(1, token) == "hunter2-linkedin"
    with pytest.raises(crypto.InvalidToken):
        crypto.decrypt(2, token)


def test_secret_key_file_is_private():
    crypto.master_key()
    assert (crypto.DATA_DIR / "secret.key").stat().st_mode & 0o077 == 0


def test_admin_bootstrap_has_no_password_and_token(db):
    token = ensure_admin(db)
    admin = db.query(User).one()
    assert admin.is_admin and admin.password_hash is None
    assert token and setup_token_ok(token) and not setup_token_ok("wrong")
    assert SETUP_TOKEN.stat().st_mode & 0o077 == 0
    assert ensure_admin(db) == token and db.query(User).count() == 1
    admin.password_hash = hash_password("a-long-password!")
    db.commit()
    assert ensure_admin(db) is None and not SETUP_TOKEN.exists()


def test_password_hash():
    h = hash_password("correct horse battery")
    assert verify_password(h, "correct horse battery")
    assert not verify_password(h, "wrong") and not verify_password(None, "x")


def test_throttle_locks_after_limit():
    t = Throttle(limit=3, window=60)
    assert all(t.hit("bob") for _ in range(3))
    assert not t.hit("bob") and t.hit("carol")
    t.ok("bob")
    assert t.hit("bob")


def test_throttle_is_atomic_under_threads():
    from concurrent.futures import ThreadPoolExecutor
    t = Throttle(limit=5, window=60)
    with ThreadPoolExecutor(16) as pool:
        assert sum(pool.map(lambda _: t.hit("x"), range(200))) == 5
