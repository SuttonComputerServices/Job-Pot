import json
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
import pytest

from jobpot.connectors import browser, greenhouse, lever, linkedin
from jobpot.connectors.registry import REGISTRY

FIX = Path(__file__).parent / "fixtures"


def crit(**kw):
    base = dict(titles="", keywords="", locations="", remote_only=False, exclude_keywords="", min_salary=0)
    return NS(**{**base, **kw})


class FakeResp:
    def __init__(self, text="", status=200):
        self.text, self.status_code = text, status

    def json(self):
        return json.loads(self.text)


def fake_get(routes, calls=None):
    def get(url, **params):
        if calls is not None:
            calls.append((url, params))
        for part, body in routes.items():
            if part in url:
                if isinstance(body, int):
                    raise httpx.HTTPStatusError("x", request=None, response=FakeResp(status=body))
                return FakeResp(body)
        raise AssertionError(f"unexpected request {url}")
    return get


def test_registry_order_and_flags():
    assert list(REGISTRY) == ["linkedin", "greenhouse", "lever", "ziprecruiter"]
    z = REGISTRY["ziprecruiter"]
    assert not z.can_discover and not z.can_apply and "blocks automated" in z.risk_note
    assert z.discover(None, None, None) == []
    assert REGISTRY["linkedin"].needs_login and [o.name for o in REGISTRY["linkedin"].options] == ["location"]


def test_html_to_text():
    assert browser.html_to_text("<p>Hi&amp;bye</p><ul><li><p>one</p></li><li>two</li></ul><script>x()</script>") == \
        "Hi&bye\n\n- one\n\n- two"


def test_greenhouse_parse_fixture():
    data = json.loads((FIX / "greenhouse_discord.json").read_text())
    jobs = greenhouse.parse_board(data, "discord")
    assert [j.external_id for j in jobs] == ["8806482002", "8840756002", "8722538002"]
    j = jobs[1]
    assert (j.title, j.company, j.location) == ("Data Scientist - Client Platform", "Discord", "San Francisco Bay Area")
    assert j.url.startswith("https://job-boards.greenhouse.io/discord/jobs/")
    assert "<" not in j.description and "&lt;" not in j.description and len(j.description) > 200


def test_greenhouse_discover_prefilters_and_validates(monkeypatch):
    calls = []
    monkeypatch.setattr(browser, "get", fake_get({"/boards/discord/": (FIX / "greenhouse_discord.json").read_text()}, calls))
    acct = NS(options={"boards": "Discord"})
    assert len(greenhouse.GREENHOUSE.discover(acct, None, None)) == 3
    assert calls[0][1] == {"content": "true"}
    found = greenhouse.GREENHOUSE.discover(acct, crit(titles="data scientist"), None)
    titles = [p.title for p in found]  # title or description may match
    assert "Data Scientist - Client Platform" in titles and "Commercial Policy Lead" not in titles
    with pytest.raises(ValueError, match="isn't a board name"):
        greenhouse.GREENHOUSE.discover(NS(options={"boards": "../etc"}), None, None)
    with pytest.raises(ValueError, match="Add at least one"):
        greenhouse.GREENHOUSE.discover(NS(options={}), None, None)


def test_greenhouse_unknown_board(monkeypatch):
    monkeypatch.setattr(browser, "get", fake_get({"/boards/nope/": 404}))
    with pytest.raises(ValueError, match="wasn't found"):
        greenhouse.GREENHOUSE.discover(NS(options={"boards": "nope"}), None, None)


def test_lever_parse_fixture():
    data = json.loads((FIX / "lever_matchgroup.json").read_text())
    jobs = lever.parse_postings(data, "matchgroup")
    assert len(jobs) == 3
    a, b, c = jobs
    assert a.salary == "CAD 35-38 per hour"
    assert b.salary == "USD 150,000-180,000 per year" and b.location == "New York, New York"
    assert c.salary == ""
    assert b.url == "https://jobs.lever.co/matchgroup/cb977666-1e41-4098-ad9c-6ab387e56a7a"
    assert "<" not in b.description and len(b.description) > 200


def test_lever_discover(monkeypatch):
    monkeypatch.setattr(browser, "get", fake_get({"/postings/matchgroup": (FIX / "lever_matchgroup.json").read_text()}))
    found = lever.LEVER.discover(NS(options={"companies": "matchgroup"}), crit(keywords="android"), None)
    assert [p.title for p in found] == ["Android Engineer III"]
    with pytest.raises(ValueError):
        lever.LEVER.discover(NS(options={"companies": "a b"}), None, None)


def test_lever_apply_rejects_foreign_url():
    r = lever.LEVER.apply(NS(options={}), NS(url="https://evil.example/job"), None, None)
    assert r.status == "failed"


def test_linkedin_parse_fixtures():
    cards = linkedin.parse_search((FIX / "linkedin_search.html").read_text())
    assert len(cards) == 10
    c = cards[0]
    assert (c.external_id, c.title, c.company, c.location) == ("4480989424", "Python Developer", "Virtusa", "New York, NY")
    assert c.url == "https://www.linkedin.com/jobs/view/4480989424/"
    text = linkedin.parse_description((FIX / "linkedin_posting.html").read_text())
    assert text.startswith("8+ years of professional experience in Python") and "<" not in text


def test_linkedin_discover(monkeypatch):
    calls = []
    search = (FIX / "linkedin_search.html").read_text()
    monkeypatch.setattr(browser, "get", fake_get({"seeMoreJobPostings": search, "jobPosting/": (FIX / "linkedin_posting.html").read_text()}, calls))
    monkeypatch.setattr(linkedin, "pause", lambda: None)
    found = linkedin.LINKEDIN.discover(NS(options={"location": "United States"}), crit(titles="python developer", keywords="ignored", remote_only=True), None)
    assert len(found) == 10  # both pages return the same cards; deduped
    searches = [p for u, p in calls if "seeMoreJobPostings" in u]
    assert [p["start"] for p in searches] == [0, 10]
    assert searches[0]["keywords"] == "python developer" and searches[0]["f_AL"] == "true" and searches[0]["f_WT"] == "2"
    assert sum(1 for u, _ in calls if "jobPosting/" in u) == 10
    assert found[0].description.startswith("8+ years")


def test_linkedin_discover_tolerates_late_failure(monkeypatch):
    search = (FIX / "linkedin_search.html").read_text()
    pages = iter([search, None])

    def get(url, **params):
        if "jobPosting/" in url:
            raise httpx.ConnectError("boom")
        body = next(pages)
        if body is None:
            raise httpx.ConnectError("429")
        return FakeResp(body)
    monkeypatch.setattr(browser, "get", get)
    monkeypatch.setattr(linkedin, "pause", lambda: None)
    found = linkedin.LINKEDIN.discover(NS(options={}), crit(keywords="python"), None)
    assert len(found) == 10 and found[0].description == ""


def test_linkedin_needs_criteria_and_login():
    with pytest.raises(ValueError, match="titles or keywords"):
        linkedin.LINKEDIN.discover(NS(options={}), crit(), None)
    r = linkedin.LINKEDIN.apply(NS(login="", options={}), NS(url="https://www.linkedin.com/jobs/view/1/"), None, None)
    assert r.status == "needs_manual"


def test_short_error_hides_secret():
    assert browser.short_error(RuntimeError("bad hunter2 here\ncall log"), "hunter2") == "bad *** here"


FORM = """<form>
<label for=a>First name*</label><input id=a required value="x">
<label for=b>Why us?*</label><textarea id=b required></textarea>
<li class="application-question"><div class="application-label">Authorized to work?<span>✱</span></div>
 <ul><li><label><input type=radio name=q required value=Yes>Yes</label></li>
 <li><label><input type=radio name=q required value=No>No</label></li></ul></li>
<input type=hidden name=h required>
<input aria-hidden="true" required>
<button type=button>Submit application</button></form>"""


def test_missing_required_in_real_browser():
    try:
        ctx = browser.page()
        page = ctx.__enter__()
    except Exception as e:  # no Chromium installed here
        pytest.skip(f"no browser: {e}")
    try:
        page.set_content(FORM)
        assert browser.missing_required(page) == ["Why us?", "Authorized to work?"]
        page.check("input[value=Yes]")
        page.fill("#b", "Because")
        assert browser.missing_required(page) == []
        r = browser.finish_form(page, ["first name"], r"submit application", r"thanks", dry_run=True)
        assert r.status == "needs_manual" and r.message.startswith("Dry run, not submitted. Filled: first name.")
    finally:
        ctx.__exit__(None, None, None)
