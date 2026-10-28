import re

from fastapi.testclient import TestClient

from jobpot.db import SessionLocal
from jobpot.models import User
from jobpot.security import hash_password

PASSWORD = "correct horse battery"


def client() -> TestClient:
    from jobpot.web.app import app
    return TestClient(app)


def csrf(c: TestClient, path: str = "/login") -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', c.get(path).text)
    assert m, f"no csrf token on {path}"
    return m.group(1)


def make_user(username="alice", password=PASSWORD, **kw) -> int:
    with SessionLocal() as db:
        u = User(username=username, password_hash=hash_password(password), **kw)
        db.add(u)
        db.commit()
        return u.id


def login(c: TestClient, username="alice", password=PASSWORD):
    r = c.post("/login", data={"username": username, "password": password, "csrf": csrf(c)}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r
