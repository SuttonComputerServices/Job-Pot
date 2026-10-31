import re

import httpx

from . import browser
from .base import ApplyResult, Connector, JobPosting, Option

NAME = re.compile(r"^[A-Za-z0-9._-]+$")
API = "https://api.ashbyhq.com/posting-api/job-board/{}"
# shortcut: confirmation wording taken from Ashby's public pages, never seen after a real submit; check the first live one
DONE = r"thank you for (applying|your application)|application (was |has been )?(successfully )?(submitted|received)"


def boards(account) -> list[str]:
    names = browser.board_names((account.options or {}).get("boards", ""), "jobs.ashbyhq.com")
    if not names:
        raise ValueError("Add at least one company name, for example: linear")
    for n in names:
        if not NAME.match(n):
            raise ValueError(f"'{n}' isn't a company name. Use the part after jobs.ashbyhq.com/ (for example: linear), or paste a job link")
    return names


def parse_board(data: dict, board: str) -> list[JobPosting]:
    out = []
    for j in data.get("jobs") or []:
        if j.get("isListed") is False:
            continue
        where = j.get("location") or ""
        if j.get("isRemote") and "remote" not in where.lower():
            where = f"{where} (Remote)".strip()
        pay = j.get("compensation") or {}
        out.append(JobPosting(
            external_id=j["id"], title=(j.get("title") or "").strip(), company=board, location=where,
            url=j.get("jobUrl") or "", description=(j.get("descriptionPlain") or "").strip(),
            salary=pay.get("scrapeableCompensationSalarySummary") or pay.get("compensationTierSummary") or "",
        ))
    return out


def fill_form(page, url: str, profile, dry_run: bool = False) -> ApplyResult:
    browser.open_page(page, url)
    if page.locator("#_systemfield_name").count() == 0:
        return ApplyResult("failed", "The Ashby application form didn't load. The job may be closed.")
    filled = []
    for label, sel, value in [("name", "#_systemfield_name", profile.full_name),
                              ("email", "#_systemfield_email", profile.email)]:
        if browser.put(page.locator(sel), value):
            filled.append(label)
    if browser.upload(page.locator("input[type=file]#_systemfield_resume"), profile.resume_path):
        filled.append("resume")
    links = browser.links(profile.links)
    site = lambda d: next((l for l in links if d in l), "")
    others = [l for l in links if not any(d in l for d in ("linkedin.com", "github.com"))]
    # Everything else on an Ashby form is a question the company wrote; only fill the ones with an obvious answer.
    for label, pattern, value in [("phone", r"^phone", profile.phone), ("LinkedIn", r"^linkedin", site("linkedin.com")),
                                  ("GitHub", r"^github", site("github.com")),
                                  ("website", r"^(website|portfolio|personal (site|website))", others[0] if others else "")]:
        if browser.fill_if_empty(browser.by_label(page, pattern), value):
            filled.append(label)
    return browser.finish_form(page, filled, r"submit application", DONE, dry_run)


class Ashby(Connector):
    def discover(self, account, criteria, secret):
        out = []
        for name in boards(account):
            try:
                data = browser.get(API.format(name), includeCompensation="true").json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    raise ValueError(f"Ashby company '{name}' wasn't found. Check the name after jobs.ashbyhq.com/.") from None
                raise
            out += parse_board(data, name)
        return browser.relevant(out, criteria)

    def apply(self, account, application, profile, secret, dry_run: bool = False):
        if not application.url.startswith("https://jobs.ashbyhq.com/"):
            return ApplyResult("failed", "This job's link isn't an Ashby job page.")
        try:
            with browser.page() as page:
                return fill_form(page, application.url.rstrip("/") + "/application", profile, dry_run=dry_run)
        except Exception as e:
            return ApplyResult("failed", "Ashby form error: " + browser.short_error(e))


ASHBY = Ashby(
    key="ashby", label="Ashby", can_apply=True,
    risk_note="Applications go straight to the employer. Job Pot stops and leaves the job for you when the form asks "
              "questions it can't answer or shows a captcha.",
    options=[Option("boards", "Company names", "The part after jobs.ashbyhq.com/, for example: linear, ramp. Comma separated. You can also paste a job link.")],
)
