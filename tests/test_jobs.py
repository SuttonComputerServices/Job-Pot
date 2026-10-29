from jobpot import crypto, matching
from jobpot.connectors.base import Connector, JobPosting
from jobpot.connectors.registry import REGISTRY
from jobpot.db import SessionLocal
from jobpot.models import Application, Criteria, SiteAccount

from .helpers import client, csrf, login, make_user


def crit(**kw):
    base = dict(keywords="", exclude_keywords="", titles="", locations="", remote_only=False, min_salary=0)
    return Criteria(**{**base, **kw})


def post(title="Python Developer", desc="", location="Austin, TX", salary=""):
    return JobPosting("1", title, "Acme", location, "https://x", desc, salary)


def test_score_weights():
    c = crit(keywords="python, django, kubernetes, go", titles="python developer")
    p = post(desc="We use Python and Django daily.")
    s, notes = matching.score("python django", c, p)
    assert 30 + 10 <= s <= 100  # 2/4 keywords = 30, title = 10, plus overlap
    assert "Keywords 2/4" in notes and "missing: kubernetes, go" in notes
    low, _ = matching.score("", crit(keywords="rust"), post(title="Chef", desc="cooking"))
    assert low == 0


def test_keywords_match_whole_words():
    c = crit(keywords="go")
    assert matching.score("", c, post(desc="good google"))[0] < matching.score("", c, post(desc="written in go."))[0]


def test_exclude_and_filters():
    assert matching.excluded(crit(exclude_keywords="senior"), post(title="Senior Python Developer"))
    assert not matching.excluded(crit(exclude_keywords="senior"), post())
    assert matching.filter_reason(crit(remote_only=True), post()) == "Not remote"
    assert matching.filter_reason(crit(remote_only=True), post(location="Remote - US")) is None
    assert "isn't in your list" in matching.filter_reason(crit(locations="denver"), post())
    assert matching.filter_reason(crit(locations="austin"), post()) is None
    assert "below" in matching.filter_reason(crit(min_salary=120000), post(salary="$80,000 - $95,000"))
    assert matching.filter_reason(crit(min_salary=120000), post(salary="$110k-$150k")) is None
    assert "below" in matching.filter_reason(crit(min_salary=300000), post(salary="$211.4K - $290.6K"))
    assert matching.filter_reason(crit(min_salary=290000), post(salary="$211.4K - $290.6K")) is None
    assert matching.filter_reason(crit(min_salary=120000), post(salary="")) is None


def _add(uid, title, fit, status="found"):
    with SessionLocal() as db:
        a = Application(user_id=uid, site="manual", external_id=title, title=title, company="Acme", fit_score=fit, status=status)
        db.add(a)
        db.commit()
        return a.id


def test_applications_ordered_best_fit_first_and_private():
    alice, bob = make_user(), make_user("bob")
    _add(alice, "Middling job", 50)
    _add(alice, "Great job", 90)
    _add(alice, "Weak job", 10)
    _add(alice, "Hidden skipped", 99, "skipped")
    bobs = _add(bob, "Bob only", 70)
    with client() as c:
        login(c)
        html = c.get("/").text
        assert html.index("Great job") < html.index("Middling job") < html.index("Weak job")
        assert "Hidden skipped" not in html and "Bob only" not in html
        assert "Hidden skipped" in c.get("/?status=skipped").text
        r = c.post(f"/applications/{bobs}/status", data={"status": "submitted", "csrf": csrf(c, "/")})
        assert r.status_code == 404
        assert c.post(f"/applications/{bobs}/delete", data={"csrf": csrf(c, "/")}).status_code == 404


def test_add_manual_job_scores_and_dedupes():
    make_user()
    with client() as c:
        login(c)
        data = {"title": "Data Analyst", "company": "Zip Co", "site": "ziprecruiter", "url": "https://example.com/j/1", "description": "SQL", "applied": "1"}
        c.post("/applications", data={**data, "csrf": csrf(c, "/")})
        r = c.post("/applications", data={**data, "csrf": csrf(c, "/")})
        assert "already on your list" in r.text
        bad = c.post("/applications", data={**data, "url": "javascript:alert(1)", "csrf": csrf(c, "/")})
        assert "must start with http" in bad.text
    with SessionLocal() as db:
        a = db.query(Application).one()
        assert a.status == "submitted" and a.submitted_at and a.site == "ziprecruiter"


def test_site_settings_encrypt_password_and_need_risk_ack(monkeypatch):
    monkeypatch.setitem(REGISTRY, "fake", Connector("fake", "Fake site", needs_login=True, can_apply=True, risk_note="Risky."))
    uid = make_user()
    with client() as c:
        login(c)
        page = c.get("/sites").text
        assert all(s in page for s in ("Ashby", "SmartRecruiters", "Indeed", "Monster", "jobs.ashbyhq.com/"))
        assert "Fake site" in page and "Risky." in page
        c.post("/sites/fake", data={"enabled": "1", "auto_submit": "1", "login": "me@x.com", "password": "pw-secret-123", "csrf": csrf(c, "/sites")})
        with SessionLocal() as db:
            acc = db.query(SiteAccount).one()
            assert acc.enabled and not acc.auto_submit  # no risk ack
            assert "pw-secret-123" not in acc.secret_enc and crypto.decrypt(uid, acc.secret_enc) == "pw-secret-123"
        c.post("/sites/fake", data={"enabled": "1", "auto_submit": "1", "risk_ack": "1", "csrf": csrf(c, "/sites")})
        with SessionLocal() as db:
            acc = db.query(SiteAccount).one()
            assert acc.auto_submit and crypto.decrypt(uid, acc.secret_enc) == "pw-secret-123"  # blank keeps it
        assert "pw-secret-123" not in c.get("/sites").text
        assert c.post("/sites/nope", data={"csrf": csrf(c, "/sites")}).status_code == 404


def test_criteria_saved_and_clamped():
    make_user()
    with client() as c:
        login(c)
        c.post("/sites/criteria", data={"keywords": "python", "min_fit": "500", "max_per_day": "-3", "csrf": csrf(c, "/sites")})
    with SessionLocal() as db:
        cr = db.query(Criteria).one()
        assert cr.keywords == "python" and cr.min_fit == 100 and cr.max_per_day == 0


def test_search_now_uses_the_form_on_screen(monkeypatch):
    from jobpot import worker
    seen = {}

    def fake_discover(db, user, acc):
        seen["boards"] = acc.options.get("boards")
        return 0
    monkeypatch.setattr(worker, "discover_site", fake_discover)
    make_user()
    with client() as c:
        login(c)
        c.post("/sites/greenhouse", data={"enabled": "1", "boards": "old", "csrf": csrf(c, "/sites")})
        r = c.post("/sites/greenhouse/search", data={"enabled": "1", "boards": "gitlab", "csrf": csrf(c, "/sites")})
        assert seen["boards"] == "gitlab" and "0 new postings" in r.text


def test_salary_without_commas_and_status_reset():
    assert "below" in matching.filter_reason(crit(min_salary=120000), post(salary="$90000 - $110000"))
    assert matching.filter_reason(crit(min_salary=120000), post(salary="USD 120000-150000")) is None
    uid = make_user()
    aid = _add(uid, "Job", 50, "submitted")
    with client() as c:
        login(c)
        c.post(f"/applications/{aid}/status", data={"status": "submitted", "csrf": csrf(c, "/")})
        c.post(f"/applications/{aid}/status", data={"status": "found", "csrf": csrf(c, "/")})
    with SessionLocal() as db:
        assert db.get(Application, aid).submitted_at is None


def test_theme_switch_after_post_page_goes_somewhere_valid():
    make_user()
    with client() as c:
        login(c)
        r = c.post("/profile/username", data={"new_username": "x", "password": "wrong", "csrf": csrf(c, "/profile")})
        assert 'name="back" value="/settings"' in r.text


def test_non_ascii_totp_code_is_rejected_cleanly():
    make_user()
    with client() as c:
        login(c)
        c.post("/settings/2fa/start", data={"csrf": csrf(c, "/settings")})
        r = c.post("/settings/2fa/confirm", data={"code": "１２３４５６", "csrf": csrf(c, "/settings")})
        assert r.status_code == 400


def test_search_all_only_runs_set_up_sites(monkeypatch):
    from jobpot import worker
    ran = []

    def fake_discover(db, user, acc):
        ran.append(acc.site)
        acc.last_error = "boom" if acc.site == "lever" else ""
        return 2
    monkeypatch.setattr(worker, "discover_site", fake_discover)
    uid = make_user()
    with SessionLocal() as db:
        db.add_all([
            SiteAccount(user_id=uid, site="greenhouse", enabled=True, options={"boards": "gitlab"}),
            SiteAccount(user_id=uid, site="lever", enabled=True, options={"companies": "x"}),
            SiteAccount(user_id=uid, site="ashby", enabled=True, options={"boards": " "}),       # not filled in
            SiteAccount(user_id=uid, site="smartrecruiters", enabled=False, options={"companies": "x"}),  # off
            SiteAccount(user_id=uid, site="linkedin", enabled=True, options={}),                 # location is optional
            SiteAccount(user_id=uid, site="ziprecruiter", enabled=True, options={}),             # can't search
        ])
        db.commit()
    with client() as c:
        login(c)
        page = c.post("/sites/search-all", data={"csrf": csrf(c, "/sites")}).text
    assert ran == ["linkedin", "greenhouse", "lever"]
    assert "Searched LinkedIn, Greenhouse: 4 new postings" in page and "Lever: boom" in page


def test_search_all_with_nothing_set_up():
    make_user()
    with client() as c:
        login(c)
        assert "No site is set up" in c.post("/sites/search-all", data={"csrf": csrf(c, "/sites")}).text
