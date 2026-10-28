from datetime import timedelta

import pytest
from sqlalchemy import select

from jobpot import crypto, worker
from jobpot.connectors import registry
from jobpot.connectors.base import ApplyResult, Connector, JobPosting
from jobpot.models import Application, Criteria, Resume, SiteAccount, User, now
from tests.helpers import make_user


class Fake(Connector):
    """Scriptable connector: postings to discover, results to hand out, optional failures."""

    def __init__(self, key, postings=(), results=(), boom=None, **kw):
        super().__init__(key=key, label=key, can_apply=True, **kw)
        self.postings, self.results, self.boom = list(postings), list(results), boom
        self.applied, self.secrets, self.paths = [], [], []

    def discover(self, account, criteria, secret):
        if self.boom:
            raise self.boom
        self.secrets.append(secret)
        return self.postings

    def apply(self, account, application, profile, secret):
        self.applied.append(application.external_id)
        self.paths.append(profile.resume_path)
        assert profile.resume_path.read_bytes() == b"%PDF-1.4 fake"
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def job(i, title="Python developer", location="Remote", salary=""):
    return JobPosting(external_id=str(i), title=title, company="Acme", location=location,
                      url=f"https://example.com/{i}", description="python fastapi", salary=salary)


@pytest.fixture
def sites(monkeypatch):
    fakes = {}
    monkeypatch.setattr(registry, "REGISTRY", fakes)
    monkeypatch.setattr(worker, "REGISTRY", fakes)
    monkeypatch.setattr(worker, "pause", lambda: None)
    return fakes


@pytest.fixture
def user(db):
    uid = make_user(full_name="Ann Example", email="ann@example.com", automation_enabled=True)
    db.add(Criteria(user_id=uid, keywords="python", exclude_keywords="senior", locations="", min_fit=0, max_per_day=2))
    db.add(Resume(user_id=uid, filename="Ann Resume.pdf", data=b"%PDF-1.4 fake", text="python fastapi developer"))
    db.commit()
    return db.get(User, uid)


def account(db, user, site, **kw):
    a = SiteAccount(user_id=user.id, site=site, **{"enabled": True, **kw})
    db.add(a)
    db.commit()
    return a


def apps(db, user):
    return {a.external_id: a for a in db.scalars(select(Application).where(Application.user_id == user.id))}


def test_discover_inserts_scores_and_dedupes(db, user, sites):
    sites["a"] = Fake("a", postings=[job(1), job(2, title="Senior python developer")])
    acct = account(db, user, "a", secret_enc=crypto.encrypt(user.id, "pw"))
    assert worker.discover_site(db, user, acct) == 1  # "senior" is excluded: no row at all
    assert sites["a"].secrets == ["pw"]
    rows = apps(db, user)
    assert list(rows) == ["1"] and rows["1"].status == "found" and rows["1"].fit_score > 0 and rows["1"].fit_notes
    assert acct.last_run_at and acct.last_error == ""
    assert worker.discover_site(db, user, acct) == 0
    assert len(apps(db, user)) == 1


def test_filtered_postings_are_skipped_with_reason(db, user, sites):
    db.scalar(select(Criteria)).remote_only = True
    db.commit()
    sites["a"] = Fake("a", postings=[job(1, location="Austin, TX")])
    worker.discover_site(db, user, account(db, user, "a"))
    row = apps(db, user)["1"]
    assert row.status == "skipped" and row.error == "Not remote"


def test_one_broken_site_does_not_stop_others(db, user, sites):
    sites["bad"] = Fake("bad", boom=RuntimeError("site down"))
    sites["good"] = Fake("good", postings=[job(1)])
    bad, good = account(db, user, "bad"), account(db, user, "good")
    worker.run_user(db, user)
    assert bad.last_error == "site down" and bad.last_run_at
    assert good.last_error == "" and "1" in apps(db, user)


def seed(db, user, n, site="a", fit=80):
    for i in range(n):
        db.add(Application(user_id=user.id, site=site, external_id=str(i), title="t", fit_score=fit - i, status="found"))
    db.commit()


def test_apply_maps_results_and_respects_cap(db, user, sites):
    db.scalar(select(Criteria)).max_per_day = 5
    db.commit()
    sites["a"] = Fake("a", results=[ApplyResult("submitted"), ApplyResult("needs_manual", "Captcha"),
                                     ApplyResult("failed", "Closed"), RuntimeError("crash"), ApplyResult("weird")])
    account(db, user, "a", auto_submit=True, risk_ack=True)
    seed(db, user, 5)
    assert worker.apply_pending(db, user) == 1
    rows = apps(db, user)
    assert sites["a"].applied == ["0", "1", "2", "3", "4"]  # best fit first
    assert rows["0"].status == "submitted" and rows["0"].submitted_at and rows["0"].error == ""
    assert (rows["1"].status, rows["1"].error) == ("needs_manual", "Captcha")
    assert (rows["2"].status, rows["2"].error) == ("failed", "Closed")
    assert rows["3"].status == "failed" and "crash" in rows["3"].error
    assert rows["4"].status == "failed"
    assert all(not p.exists() for p in sites["a"].paths)  # temp resume copies cleaned up
    assert sites["a"].paths[0].name.startswith("Ann_Resume-") and sites["a"].paths[0].suffix == ".pdf"


def test_daily_cap_counts_todays_submissions(db, user, sites):
    sites["a"] = Fake("a", results=[ApplyResult("submitted")] * 5)
    account(db, user, "a", auto_submit=True, risk_ack=True)
    seed(db, user, 4)
    db.add(Application(user_id=user.id, site="a", external_id="old", status="submitted", submitted_at=now()))
    db.add(Application(user_id=user.id, site="a", external_id="older", status="submitted", submitted_at=now() - timedelta(days=2)))
    db.commit()
    assert worker.apply_pending(db, user) == 1  # cap 2, one already today
    assert worker.apply_pending(db, user) == 0
    assert sites["a"].applied == ["0"]


def test_min_fit_respected(db, user, sites):
    db.scalar(select(Criteria)).min_fit = 79
    db.commit()
    sites["a"] = Fake("a", results=[ApplyResult("submitted")] * 3)
    account(db, user, "a", auto_submit=True, risk_ack=True)
    seed(db, user, 3)
    worker.apply_pending(db, user)
    assert sites["a"].applied == ["0", "1"]


@pytest.mark.parametrize("flags", [dict(auto_submit=True, risk_ack=False), dict(auto_submit=False, risk_ack=True),
                                   dict(auto_submit=True, risk_ack=True, enabled=False)])
def test_no_apply_without_consent(db, user, sites, flags):
    sites["a"] = Fake("a", results=[ApplyResult("submitted")])
    sites["a"].risk_note = "Risky."
    account(db, user, "a", **flags)
    seed(db, user, 1)
    assert worker.apply_pending(db, user) == 0 and sites["a"].applied == []


def test_site_without_risk_note_needs_no_ack(db, user, sites):
    sites["a"] = Fake("a", results=[ApplyResult("submitted")])
    account(db, user, "a", auto_submit=True, risk_ack=False)
    seed(db, user, 1)
    assert worker.apply_pending(db, user) == 1


def test_no_apply_when_connector_cannot(db, user, sites):
    sites["a"] = Fake("a", results=[ApplyResult("submitted")])
    sites["a"].can_apply = False
    account(db, user, "a", auto_submit=True, risk_ack=True)
    seed(db, user, 1)
    assert worker.apply_pending(db, user) == 0


def test_automation_off_applies_nothing(db, user, sites):
    user.automation_enabled = False
    db.commit()
    sites["a"] = Fake("a", results=[ApplyResult("submitted")])
    account(db, user, "a", auto_submit=True, risk_ack=True)
    seed(db, user, 1)
    assert worker.apply_pending(db, user) == 0 and sites["a"].applied == []


def test_run_user_polls_mail_and_survives_mail_errors(db, user, sites, monkeypatch):
    import sys
    import types
    from jobpot.models import Mailbox
    polled = []
    fake_mail = types.SimpleNamespace(poll=lambda db, u: polled.append(u.id) or 0)
    monkeypatch.setitem(sys.modules, "jobpot.mail", fake_mail)
    monkeypatch.setattr("jobpot.mail", fake_mail, raising=False)
    worker.run_user(db, user)
    assert polled == []  # no mailbox
    db.add(Mailbox(user_id=user.id, enabled=True))
    db.commit()
    worker.run_user(db, user)
    assert polled == [user.id]
    fake_mail.poll = lambda db, u: 1 / 0
    worker.run_user(db, user)  # logged, not raised


def test_run_once_isolates_users(db, sites, monkeypatch):
    a = make_user("a1", automation_enabled=True)
    b = make_user("b1", automation_enabled=True)
    make_user("c1")
    seen = []

    def run(db, u):
        seen.append(u.id)
        if u.id == a:
            raise RuntimeError("bad user")
    monkeypatch.setattr(worker, "run_user", run)
    worker.run_once()
    assert seen == [a, b]


def test_build_profile_without_resume(db, user):
    p = worker.build_profile(user, None)
    assert p.resume_path is None and p.full_name == "Ann Example" and p.resume_text == ""


def test_hand_added_jobs_are_never_auto_applied(db, user, sites):
    sites["a"] = Fake("a", results=[ApplyResult("submitted")])
    account(db, user, "a", auto_submit=True, risk_ack=True)
    seed(db, user, 1)
    db.query(Application).update({Application.external_id: "manual-abc"})
    db.commit()
    assert worker.apply_pending(db, user) == 0 and sites["a"].applied == []


def test_discover_never_raises_on_duplicate_insert(db, user, sites, monkeypatch):
    from jobpot.db import SessionLocal
    sites["a"] = Fake("a", postings=[JobPosting("p1", "Python dev", "Acme", "Remote", "https://x")])
    acc = account(db, user, "a")
    real_add = db.add

    def racing_add(obj):  # another process inserts the same posting first
        if isinstance(obj, Application):
            with SessionLocal() as other:
                other.add(Application(user_id=obj.user_id, site=obj.site, external_id=obj.external_id, title="x", company="y"))
                other.commit()
        real_add(obj)
    monkeypatch.setattr(db, "add", racing_add)
    assert worker.discover_site(db, user, acc) == 0
    assert "UNIQUE" in acc.last_error
