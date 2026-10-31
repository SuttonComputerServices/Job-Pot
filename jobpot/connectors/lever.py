import re

import httpx

from . import browser
from .base import ApplyResult, Connector, JobPosting, Option

NAME = re.compile(r"^[a-z0-9_-]+$")
API = "https://api.lever.co/v0/postings/{}"
DONE = r"application submitted|thanks? (you )?for applying|we('ve| have) received your application"
URL_FIELDS = {"linkedin.com": "LinkedIn", "github.com": "GitHub", "twitter.com": "Twitter", "x.com": "Twitter"}


def companies(account) -> list[str]:
    names = browser.board_names((account.options or {}).get("companies", ""), "jobs.lever.co")
    if not names:
        raise ValueError("Add at least one company name, for example: spotify")
    for n in names:
        if not NAME.match(n):
            raise ValueError(f"'{n}' isn't a company name. Use the part after jobs.lever.co/ (for example: spotify), or paste a job link")
    return names


def _salary(r: dict | None) -> str:
    if not r or r.get("min") is None:
        return ""
    per = " ".join((r.get("interval") or "").split("-")[:2])  # "per-year-salary" -> "per year"
    return f"{r.get('currency', '')} {r['min']:,}-{(r.get('max') or r['min']):,} {per}".strip()


def parse_postings(data: list, company: str) -> list[JobPosting]:
    out = []
    for p in data:
        cats = p.get("categories") or {}
        parts = [p.get("descriptionPlain") or ""]
        for lst in p.get("lists") or []:
            parts.append(f"{lst.get('text', '')}\n{browser.html_to_text(lst.get('content') or '')}")
        parts.append(p.get("additionalPlain") or "")
        where = cats.get("location") or ""
        if p.get("workplaceType") == "remote" and "remote" not in where.lower():
            where = f"{where} (Remote)".strip()
        out.append(JobPosting(
            external_id=p["id"], title=p.get("text") or "", company=company, location=where,
            url=p.get("hostedUrl") or "", description="\n\n".join(x.strip() for x in parts if x.strip()),
            salary=_salary(p.get("salaryRange")),
        ))
    return out


def fill_form(page, url: str, profile, dry_run: bool = False) -> ApplyResult:
    browser.open_page(page, url)
    if page.locator("input[name=name]").count() == 0:
        return ApplyResult("failed", "The Lever application form didn't load. The job may be closed.")
    filled = []
    # Upload first: Lever parses the resume and fills empty fields from it, which we then correct.
    if browser.upload(page.locator("input[type=file][name=resume]"), profile.resume_path):
        try:
            page.locator("span.filename", has_text=profile.resume_path.name).wait_for(timeout=15000)
            page.locator(".resume-upload-working").wait_for(state="hidden", timeout=20000)
        except Exception:
            return ApplyResult("needs_manual", "Lever didn't accept the resume file. Open the job and apply yourself.")
        filled.append("resume")
    for label, name, value in [("name", "name", profile.full_name), ("email", "email", profile.email),
                               ("phone", "phone", profile.phone)]:
        if browser.put(page.locator(f"input[name={name}]"), value):
            filled.append(label)
    loc = page.locator("input[name=location]")
    if profile.location and loc.count() and loc.first.is_visible() and not loc.first.input_value().strip():
        # An autocomplete that only searches on key presses; the first suggestion sets the hidden selectedLocation.
        loc.first.click()
        page.wait_for_timeout(300)
        loc.first.press_sequentially(profile.location, delay=40)
        try:
            page.locator(".dropdown-results .dropdown-location").first.click(timeout=8000)
        except Exception:
            pass  # no suggestion; the typed text stays
        filled.append("location")
    others = []
    for link in browser.links(profile.links):
        field = next((f for d, f in URL_FIELDS.items() if d in link), None)
        if field and browser.fill_if_empty(page.locator(f'input[name="urls[{field}]"]'), link):
            filled.append(field)
        elif not field:
            others.append(link)
    for field, link in zip(("Portfolio", "Other"), others):
        if browser.fill_if_empty(page.locator(f'input[name="urls[{field}]"]'), link):
            filled.append(field)
    return browser.finish_form(page, filled, r"submit application", DONE, dry_run)


class Lever(Connector):
    def discover(self, account, criteria, secret):
        out = []
        for name in companies(account):
            try:
                data = browser.get(API.format(name), mode="json").json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    raise ValueError(f"Lever company '{name}' wasn't found. Check the name after jobs.lever.co/.") from None
                raise
            out += parse_postings(data, name)
        return browser.relevant(out, criteria)

    def apply(self, account, application, profile, secret, dry_run: bool = False):
        if not application.url.startswith("https://jobs.lever.co/"):
            return ApplyResult("failed", "This job's link isn't a Lever job page.")
        try:
            with browser.page() as page:
                return fill_form(page, application.url.rstrip("/") + "/apply", profile, dry_run=dry_run)
        except Exception as e:
            return ApplyResult("failed", "Lever form error: " + browser.short_error(e))


LEVER = Lever(
    key="lever", label="Lever", can_apply=True,
    risk_note="Applications go straight to the employer. Job Pot stops and leaves the job for you when the form asks "
              "questions it can't answer or shows a captcha.",
    options=[Option("companies", "Company names", "The part after jobs.lever.co/, for example: spotify. Comma separated. You can also paste a job link.")],
)
