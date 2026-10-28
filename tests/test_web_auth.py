import time

import pyotp

from jobpot import crypto
from jobpot.db import SessionLocal
from jobpot.models import User
from jobpot.security import SETUP_TOKEN, login_throttle

from .helpers import PASSWORD, client, csrf, login, make_user


def setup_function():
    login_throttle.fails.clear()


def test_post_without_csrf_is_rejected():
    with client() as c:
        r = c.post("/login", data={"username": "x", "password": "y"})
        assert r.status_code == 403


def test_admin_first_login_needs_setup_token():
    with client() as c:  # lifespan creates the admin + token
        token = SETUP_TOKEN.read_text().strip()
        r = c.post("/login", data={"username": "admin", "password": "", "csrf": csrf(c)}, follow_redirects=False)
        assert r.headers["location"] == "/setup"
        bad = c.post("/setup", data={"token": "nope", "password": PASSWORD, "confirm": PASSWORD, "csrf": csrf(c, "/setup")})
        assert bad.status_code == 401
        short = c.post("/setup", data={"token": token, "password": "short", "confirm": "short", "csrf": csrf(c, "/setup")})
        assert short.status_code == 400
        ok = c.post("/setup", data={"token": token, "password": PASSWORD, "confirm": PASSWORD, "csrf": csrf(c, "/setup")}, follow_redirects=False)
        assert ok.status_code == 303 and not SETUP_TOKEN.exists()
        assert c.get("/settings").status_code == 200
        assert c.get("/setup", follow_redirects=False).headers["location"] == "/login"


def test_wrong_password_then_lockout():
    make_user()
    with client() as c:
        for _ in range(5):
            assert c.post("/login", data={"username": "alice", "password": "bad", "csrf": csrf(c)}).status_code == 401
        r = c.post("/login", data={"username": "ALICE", "password": PASSWORD, "csrf": csrf(c)})
        assert r.status_code == 429


def test_pages_need_login():
    with client() as c:
        assert c.get("/settings", follow_redirects=False).headers["location"] == "/login"
        assert c.get("/faq").status_code == 200


def test_totp_enrol_and_login():
    uid = make_user()
    with client() as c:
        login(c)
        c.post("/settings/2fa/start", data={"csrf": csrf(c, "/settings")})
        with SessionLocal() as db:
            secret = crypto.decrypt(uid, db.get(User, uid).totp_secret_enc)
        assert "<svg" in c.get("/settings").text
        assert c.post("/settings/2fa/confirm", data={"code": "000000", "csrf": csrf(c, "/settings")}).status_code == 400
        c.post("/settings/2fa/confirm", data={"code": pyotp.TOTP(secret).now(), "csrf": csrf(c, "/settings")})
        with SessionLocal() as db:
            assert db.get(User, uid).totp_enabled
        c.post("/logout", data={"csrf": csrf(c, "/settings")})
        r = login(c)
        assert r.headers["location"] == "/login/2fa"
        assert c.get("/settings", follow_redirects=False).status_code == 303  # not signed in yet
        used = pyotp.TOTP(secret).now()  # already used to enrol: a replay must fail
        assert c.post("/login/2fa", data={"code": used, "csrf": csrf(c, "/login/2fa")}).status_code == 401
        nxt = pyotp.TOTP(secret).at(time.time() + 30)
        r = c.post("/login/2fa", data={"code": nxt, "csrf": csrf(c, "/login/2fa")}, follow_redirects=False)
        assert r.headers["location"] == "/"
        assert c.get("/settings").status_code == 200


def test_username_change_rejects_existing_name_any_case():
    make_user()
    make_user("bob")
    with client() as c:
        login(c)
        r = c.post("/profile/username", data={"new_username": "BOB", "password": PASSWORD, "csrf": csrf(c, "/profile")})
        assert r.status_code == 400 and "taken" in r.text
        r = c.post("/profile/username", data={"new_username": "alice2", "password": PASSWORD, "csrf": csrf(c, "/profile")}, follow_redirects=False)
        assert r.status_code == 303
        c.post("/logout", data={"csrf": csrf(c, "/profile")})
        login(c, "alice2")


def test_admin_page_is_admin_only_and_creates_users():
    make_user()
    make_user("root", is_admin=True)
    with client() as c:
        login(c)
        assert c.get("/admin").status_code == 403
        c.post("/logout", data={"csrf": csrf(c, "/settings")})
        login(c, "root")
        c.post("/admin/users", data={"username": "carol", "password": "temporary-pass-1", "csrf": csrf(c, "/admin")})
        c.post("/logout", data={"csrf": csrf(c, "/admin")})
        login(c, "carol", "temporary-pass-1")
        r = c.get("/profile", follow_redirects=False)
        assert r.headers["location"] == "/settings"  # must pick own password first


def test_password_change_signs_out_other_sessions():
    make_user()
    with client() as a, client() as b:
        login(a)
        login(b)
        a.post("/settings/password", data={"current": PASSWORD, "password": "another long pass", "confirm": "another long pass", "csrf": csrf(a, "/settings")})
        assert a.get("/settings").status_code == 200
        assert b.get("/settings", follow_redirects=False).status_code == 303


def test_theme_redirect_is_local_only():
    make_user()
    with client() as c:
        login(c)
        r = c.post("/settings/theme", data={"theme": "dark", "back": "//evil.example", "csrf": csrf(c, "/settings")}, follow_redirects=False)
        assert r.headers["location"] == "/settings"
        assert 'data-theme="dark"' in c.get("/settings").text


def test_security_headers():
    with client() as c:
        r = c.get("/login")
        assert "default-src 'self'" in r.headers["content-security-policy"]
        assert r.headers["x-frame-options"] == "DENY"


def test_strangers_failures_dont_lock_out_the_owner():
    from fastapi.testclient import TestClient
    from jobpot.web.app import app
    make_user()
    with TestClient(app, client=("203.0.113.9", 5000)) as attacker, TestClient(app, client=("198.51.100.7", 5000)) as owner:
        for _ in range(6):
            attacker.post("/login", data={"username": "alice", "password": "bad", "csrf": csrf(attacker)})
        assert attacker.post("/login", data={"username": "alice", "password": PASSWORD, "csrf": csrf(attacker)}).status_code == 429
        login(owner)
