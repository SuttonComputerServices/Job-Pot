import re
import time

import pyotp
import pytest

from jobpot import crypto, mail
from jobpot.db import SessionLocal
from jobpot.models import AppSetting, PasswordReset, User

from .helpers import PASSWORD, client, csrf, login, make_user

NEW = "a brand new passphrase"


def admin_client(c):
    login(c, "boss")


def turn_on_mail(c):
    r = c.post("/admin/mail", data={"host": "smtp.example.com", "port": "587", "starttls": "1", "sender": "jobpot@example.com",
                                    "password": "smtp-secret-pw", "public_url": "https://jobs.example.com/",
                                    "csrf": csrf(c, "/admin")}, follow_redirects=False)
    assert r.status_code == 303


@pytest.fixture(autouse=True)
def boss():
    make_user("boss", is_admin=True, email="boss@example.com")  # before app startup, so no password-less admin is made


def capture(monkeypatch) -> list:
    sent = []
    monkeypatch.setattr(mail, "_send", lambda mb, password, msg: sent.append((mb, password, msg)))
    return sent


def link_in(msg) -> str:
    return re.search(r"https://jobs\.example\.com(/reset/\S+)", msg.get_content()).group(1)


def test_signup_is_off_by_default():
    with client() as c:
        assert "Create an account" not in c.get("/login").text
        assert c.get("/signup", follow_redirects=False).headers["location"] == "/login"
        r = c.post("/signup", data={"username": "eve", "email": "e@x.com", "password": PASSWORD, "confirm": PASSWORD,
                                    "csrf": csrf(c)}, follow_redirects=False)
        assert r.headers["location"] == "/login"
    with SessionLocal() as db:
        assert db.query(User).filter_by(username="eve").count() == 0


def test_signup_when_admin_allows_it():
    with client() as c:
        admin_client(c)
        c.post("/admin/signup", data={"enabled": "1", "csrf": csrf(c, "/admin")})
        c.post("/logout", data={"csrf": csrf(c, "/admin")})
        assert 'href="/signup"' in c.get("/login").text
        form = lambda **kw: {"username": "newbie", "email": "new@example.com", "password": PASSWORD, "confirm": PASSWORD,
                             "csrf": csrf(c, "/signup"), **kw}
        assert c.post("/signup", data=form(username="Boss")).status_code == 400  # taken, any case
        assert c.post("/signup", data=form(email="not-an-email")).status_code == 400
        assert c.post("/signup", data=form(username="a b")).status_code == 400
        assert c.post("/signup", data=form(confirm="different password")).status_code == 400
        r = c.post("/signup", data=form(), follow_redirects=False)
        assert r.headers["location"] == "/profile" and c.get("/profile").status_code == 200
    with SessionLocal() as db:
        u = db.query(User).filter_by(username="newbie").one()
        assert u.email == "new@example.com" and not u.is_admin and not u.must_change_password


def test_forgot_without_outgoing_mail_points_to_admin():
    make_user()
    with client() as c:
        assert "ask the person who runs it" in c.get("/forgot").text
        assert c.post("/forgot", data={"who": "alice", "csrf": csrf(c)}).status_code == 400


def test_mail_password_is_encrypted_and_kept():
    with client() as c:
        admin_client(c)
        turn_on_mail(c)
        c.post("/admin/mail", data={"host": "smtp2.example.com", "port": "465", "sender": "jobpot@example.com",
                                    "public_url": "https://jobs.example.com", "csrf": csrf(c, "/admin")})
    with SessionLocal() as db:
        enc = db.get(AppSetting, "smtp_password_enc").value
        assert "smtp-secret-pw" not in enc and crypto.decrypt(crypto.APP_ID, enc) == "smtp-secret-pw"
        mb, pw = mail.system_mailbox(db)
        assert (mb.smtp_host, mb.smtp_port, mb.smtp_starttls, pw) == ("smtp2.example.com", 465, False, "smtp-secret-pw")
        assert db.get(AppSetting, "public_url").value == "https://jobs.example.com"


def test_reset_by_email_link(monkeypatch):
    sent = capture(monkeypatch)
    make_user(email="alice@example.com")
    with client() as c:
        admin_client(c)
        turn_on_mail(c)
    with client() as other, client() as c:
        login(other)  # an existing session elsewhere
        same = c.post("/forgot", data={"who": "nobody@example.com", "csrf": csrf(c, "/forgot")}).text
        assert "reset link is on its way" in same and not sent
        c.post("/forgot", data={"who": "ALICE@example.com", "csrf": csrf(c, "/forgot")}, headers={"host": "evil.example"})
        assert len(sent) == 1 and sent[0][2]["To"] == "alice@example.com" and sent[0][1] == "smtp-secret-pw"
        path = link_in(sent[0][2])  # built from the saved address, never the request's Host header
        bad = c.post(path, data={"password": NEW, "confirm": "nope", "csrf": csrf(c, path)})
        assert bad.status_code == 400
        r = c.post(path, data={"password": NEW, "confirm": NEW, "csrf": csrf(c, path)}, follow_redirects=False)
        assert r.headers["location"] == "/login"
        assert "expired or was already used" in c.get(path).text
        login(c, "alice", NEW)
        assert other.get("/", follow_redirects=False).headers["location"] == "/login"
    with SessionLocal() as db:
        assert db.query(PasswordReset).count() == 0


def test_reset_link_expires_and_new_link_replaces_old(monkeypatch):
    sent = capture(monkeypatch)
    make_user(email="alice@example.com")
    with client() as c:
        admin_client(c)
        turn_on_mail(c)
        c.post("/logout", data={"csrf": csrf(c, "/admin")})
        for _ in range(2):
            c.post("/forgot", data={"who": "alice", "csrf": csrf(c, "/forgot")})
        first, second = link_in(sent[0][2]), link_in(sent[1][2])
        assert c.get(first).status_code == 400
        assert c.get(second).status_code == 200
        with SessionLocal() as db:
            db.query(PasswordReset).update({"expires_at": int(time.time()) - 1})
            db.commit()
        assert c.get(second).status_code == 400


def test_reset_emails_per_account_are_limited(monkeypatch):
    sent = capture(monkeypatch)
    make_user(email="alice@example.com")
    with client() as c:
        admin_client(c)
        turn_on_mail(c)
        c.post("/logout", data={"csrf": csrf(c, "/admin")})
        for _ in range(4):
            c.post("/forgot", data={"who": "alice", "csrf": csrf(c, "/forgot")})
    assert len(sent) == 3


def test_reset_needs_authenticator_code_when_two_step_is_on(monkeypatch):
    sent = capture(monkeypatch)
    secret = pyotp.random_base32()
    uid = make_user(email="alice@example.com", totp_enabled=True)
    with SessionLocal() as db:
        db.get(User, uid).totp_secret_enc = crypto.encrypt(uid, secret)
        db.commit()
    with client() as c:
        admin_client(c)
        turn_on_mail(c)
        c.post("/logout", data={"csrf": csrf(c, "/admin")})
        c.post("/forgot", data={"who": "alice", "csrf": csrf(c, "/forgot")})
        path = link_in(sent[0][2])
        assert "authenticator" in c.get(path).text
        assert c.post(path, data={"password": NEW, "confirm": NEW, "code": "000000", "csrf": csrf(c, path)}).status_code == 401
        r = c.post(path, data={"password": NEW, "confirm": NEW, "code": pyotp.TOTP(secret).now(), "csrf": csrf(c, path)},
                   follow_redirects=False)
        assert r.headers["location"] == "/login"
