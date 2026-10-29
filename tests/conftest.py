import os
import tempfile

os.environ["JOBPOT_DATA"] = tempfile.mkdtemp(prefix="jobpot-test-")
os.environ.pop("JOBPOT_SECRET_KEY", None)
os.environ.pop("DATABASE_URL", None)

import pytest  # noqa: E402

from jobpot.db import Base, SessionLocal, engine, init_db  # noqa: E402
from jobpot.security import SETUP_TOKEN, login_throttle, reset_mail_throttle, signup_throttle  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    init_db()
    yield
    Base.metadata.drop_all(engine)
    SETUP_TOKEN.unlink(missing_ok=True)
    for t in (login_throttle, signup_throttle, reset_mail_throttle):
        t.fails.clear()


@pytest.fixture
def db():
    with SessionLocal() as s:
        yield s
