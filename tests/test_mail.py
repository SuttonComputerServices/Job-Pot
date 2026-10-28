import email
from datetime import datetime, timezone

import pytest

from jobpot import crypto, mail
from jobpot.db import SessionLocal
from jobpot.models import Application, Mailbox, Reply, Response, User

from .helpers import client, csrf, login, make_user

PLAIN = (b"From: Acme Hiring <jobs@acme.com>\r\nSubject: Interview for Data Engineer\r\nMessage-ID: <m1@acme.com>\r\n"
         b"Date: Mon, 26 Oct 2026 10:00:00 +0000\r\n\r\nWe'd like to schedule a call. What is your availability?\r\n")
HTML_ONLY = (b"From: careers@globex.io\r\nSubject: Update\r\nMessage-ID: <m2@globex.io>\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
             b"<html><head><style>p{color:red}</style><title>x</title></head><body><script>alert(1)</script>"
             b"<p>Unfortunately,&nbsp;we are &amp; not   moving forward.</p><p>Best</p></body></html>")
LATIN1 = (b"From: a@b.com\r\nSubject: Hi\r\nContent-Type: text/plain; charset=iso-8859-1\r\nContent-Transfer-Encoding: 8bit\r\n\r\n"
          b"Caf\xe9 meeting\r\n")
UNDECLARED = b"From: a@b.com\r\nSubject: Hi\r\nContent-Transfer-Encoding: 8bit\r\n\r\nNa\xefve text\r\n"
ALT = (b"From: a@b.com\r\nSubject: Hi\r\nMIME-Version: 1.0\r\nContent-Type: multipart/alternative; boundary=XX\r\n\r\n"
       b"--XX\r\nContent-Type: text/html\r\n\r\n<p>html version</p>\r\n--XX\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
       b"plain version\r\n--XX--\r\n")


class FakeIMAP:
    def __init__(self, messages: dict[int, bytes]):
        self.messages, self.searches = messages, []

    def select(self, box, readonly=False):
        assert readonly
        return "OK", [b"1"]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            self.searches.append(args[-1])
            return "OK", [b" ".join(str(u).encode() for u in sorted(self.messages))]
        uid = int(args[0])
        return "OK", [(f"{uid} (UID {uid} BODY[] {{1}}".encode(), self.messages[uid]), b")"]

    def logout(self):
        pass


class FakeSMTP:
    sent = []

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)

    def quit(self):
        pass


def setup_user(db, **mb):
    u = db.get(User, make_user(full_name="Alice Smith"))
    db.add(Mailbox(user_id=u.id, address="alice.jobs@example.com", imap_host="imap.example.com", smtp_host="smtp.example.com",
                   login="alice.jobs@example.com", password_enc=crypto.encrypt(u.id, "pw"), enabled=True, **mb))
    db.commit()
    return u


def add_app(db, u, **kw):
    a = Application(user_id=u.id, site="test", external_id=kw.pop("external_id", kw.get("company", "x")), status="submitted",
                    submitted_at=datetime.now(timezone.utc), **kw)
    db.add(a)
    db.commit()
    return a


def test_extract_body_variants():
    body = lambda raw: mail.extract_body(email.message_from_bytes(raw))  # noqa: E731
    assert "schedule a call" in body(PLAIN)
    html = body(HTML_ONLY)
    assert "Unfortunately, we are & not moving forward." in html and "alert" not in html and "color" not in html
    assert body(LATIN1) == "Café meeting"
    assert body(UNDECLARED) == "Naïve text"
    assert body(ALT) == "plain version"


def test_poll_stores_matches_and_skips_duplicates(db, monkeypatch):
    u = setup_user(db)
    acme = add_app(db, u, title="Data Engineer", company="Acme, Inc.")
    globex = add_app(db, u, title="Analyst", company="Globex LLC")
    fake = FakeIMAP({3: PLAIN, 7: HTML_ONLY})
    monkeypatch.setattr(mail, "_connect", lambda mb, pw: fake if pw == "pw" else None)
    assert mail.poll(db, u) == 2
    mb = mail.mailbox_of(db, u)
    assert mb.last_uid == 7 and mb.last_error == "" and mb.last_checked_at and fake.searches == ["ALL"]
    rs = {r.message_id: r for r in db.query(Response).filter_by(user_id=u.id)}
    assert rs["<m1@acme.com>"].application_id == acme.id and rs["<m1@acme.com>"].category == "interview"
    assert rs["<m2@globex.io>"].application_id == globex.id and rs["<m2@globex.io>"].category == "rejection"
    assert any("[your availability]" in s for s in rs["<m1@acme.com>"].suggestions)
    # Same message again under a new UID is ignored; search starts after last_uid.
    fake.messages[9] = PLAIN
    assert mail.poll(db, u) == 0 and fake.searches[-1] == "UID 8:*" and mb.last_uid == 9


def test_poll_records_error_instead_of_raising(db, monkeypatch):
    u = setup_user(db)

    def boom(mb, pw):
        raise OSError("Connection refused")
    monkeypatch.setattr(mail, "_connect", boom)
    assert mail.poll(db, u) == 0
    assert "Connection refused" in mail.mailbox_of(db, u).last_error


def test_match_by_domain_and_subject(db):
    u = setup_user(db)
    old = add_app(db, u, title="Backend Developer", company="Initech", external_id="1")
    assert mail.match_application(db, u, "HR <no-reply@initech.com>", "Hello", "") == old
    assert mail.match_application(db, u, "x@greenhouse.io", "Your Backend Developer application", "") == old
    assert mail.match_application(db, u, "x@greenhouse.io", "News", "Initechnology") is None


def test_categorise_labels():
    assert mail.categorise("Your application", "Unfortunately we went with other candidates.") == "rejection"
    assert mail.categorise("Next steps", "Can we schedule a call?") == "interview"
    assert mail.categorise("Good news", "We are pleased to offer you the role.") == "offer"
    assert mail.categorise("Thanks", "We received your application.") == "info"
    assert mail.categorise("Newsletter", "Read our blog.") == "other"


def test_categorise_uses_ai_only_for_valid_labels(db, monkeypatch):
    monkeypatch.setattr(mail.ai, "ai_enabled", lambda db: True)
    monkeypatch.setattr(mail.ai, "generate", lambda db, prompt: "Offer.")
    assert mail.categorise("Hi", "unfortunately", db) == "offer"
    monkeypatch.setattr(mail.ai, "generate", lambda db, prompt: "maybe a rejection")
    assert mail.categorise("Hi", "unfortunately", db) == "rejection"


def test_send_reply_headers_and_stores_reply(db, monkeypatch):
    u = setup_user(db)
    r = Response(user_id=u.id, message_id="<m1@acme.com>", from_addr="Acme Hiring <jobs@acme.com>", subject="Re: Interview")
    db.add(r)
    db.commit()
    FakeSMTP.sent = []
    monkeypatch.setattr(mail, "_smtp", lambda mb, pw: FakeSMTP())
    mail.send_reply(db, u, r, "Tuesday works.")
    msg = FakeSMTP.sent[0]
    assert msg["To"] == "jobs@acme.com" and msg["Subject"] == "Re: Interview"
    assert msg["From"] == "Alice Smith <alice.jobs@example.com>"
    assert msg["In-Reply-To"] == msg["References"] == "<m1@acme.com>"
    assert db.query(Reply).filter_by(response_id=r.id).one().body == "Tuesday works."
    with pytest.raises(ValueError):
        mail.send_reply(db, u, r, "  ")


def test_routes_save_encrypted_never_render_password_and_isolate_users(monkeypatch):
    uid = make_user()
    make_user("bob")
    with client() as c:
        login(c)
        data = {"address": "a@example.com", "imap_host": "imap.example.com", "imap_port": "993", "smtp_host": "smtp.example.com",
                "smtp_port": "587", "smtp_starttls": "1", "login": "a@example.com", "password": "s3cret-app-pass", "enabled": "1"}
        bad = c.post("/mailbox", data={**data, "imap_host": "imap.example.com/x", "csrf": csrf(c, "/mailbox")})
        assert bad.status_code == 400 and "s3cret" not in bad.text
        assert c.post("/mailbox", data={**data, "csrf": csrf(c, "/mailbox")}).status_code == 200
        page = c.get("/mailbox").text
        assert "s3cret-app-pass" not in page and "Saved. Leave blank" in page
        with SessionLocal() as db:
            mb = db.query(Mailbox).filter_by(user_id=uid).one()
            assert "s3cret" not in mb.password_enc and crypto.decrypt(uid, mb.password_enc) == "s3cret-app-pass"
            r = Response(user_id=uid, from_addr="x@y.com", subject="Hi", body="hello there")
            db.add(r)
            db.commit()
            rid = r.id
        assert "hello there" in c.get("/mailbox").text  # unmatched list
        with SessionLocal() as db:
            aid = add_app(db, db.get(User, uid), title="Analyst", company="Globex").id
        r = c.post(f"/responses/{rid}/attach", data={"application_id": aid, "csrf": csrf(c, "/mailbox")}, follow_redirects=False)
        assert r.headers["location"] == f"/#app-{aid}"
        monkeypatch.setattr(mail, "_smtp", lambda mb, pw: FakeSMTP())
        r = c.post(f"/responses/{rid}/reply", data={"body": "Thanks", "csrf": csrf(c, "/mailbox")}, follow_redirects=False)
        assert r.headers["location"] == f"/#app-{aid}"
        c.post("/logout", data={"csrf": csrf(c, "/mailbox")})
        login(c, "bob")
        for action in ("reply", "suggest", "delete"):
            assert c.post(f"/responses/{rid}/{action}", data={"body": "x", "csrf": csrf(c, "/mailbox")}).status_code == 404


def test_check_host_refuses_internal_addresses(monkeypatch):
    import pytest
    from jobpot import config, mail
    for host in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "localhost"):
        with pytest.raises(OSError, match="private or local"):
            mail.check_host(host, 993)
    monkeypatch.setattr(config, "ALLOW_PRIVATE_MAIL_HOSTS", True)
    mail.check_host("127.0.0.1", 993)


def test_saved_password_dropped_when_server_changes():
    from jobpot import crypto
    from jobpot.db import SessionLocal
    from jobpot.models import Mailbox
    from .helpers import client, csrf, login, make_user
    uid = make_user()
    base = {"address": "jobs@example.com", "imap_host": "imap.example.com", "imap_port": "993", "smtp_host": "smtp.example.com",
            "smtp_port": "587", "smtp_starttls": "1", "login": "jobs@example.com", "enabled": "1"}
    with client() as c:
        login(c)
        c.post("/mailbox", data={**base, "password": "app-pass-1", "csrf": csrf(c, "/mailbox")})
        c.post("/mailbox", data={**base, "csrf": csrf(c, "/mailbox")})  # unchanged: keeps the password
        with SessionLocal() as db:
            assert crypto.decrypt(uid, db.query(Mailbox).one().password_enc) == "app-pass-1"
        r = c.post("/mailbox", data={**base, "imap_host": "imap.attacker.example", "csrf": csrf(c, "/mailbox")})
        assert r.status_code == 400 and "enter the mailbox password again" in r.text
        c.post("/mailbox", data={**base, "imap_host": "imap.attacker.example", "enabled": "", "csrf": csrf(c, "/mailbox")})
        with SessionLocal() as db:
            assert db.query(Mailbox).one().password_enc is None
