import html
import re
from urllib.parse import urlparse

import httpx

from . import browser
from ..matching import split_list
from .base import ApplyResult, Connector, JobPosting, Option

TOKEN = re.compile(r"^[a-z0-9_-]+$")
API = "https://boards-api.greenhouse.io/v1/boards/{}/jobs"
# The embed form works for every board, including companies whose job pages live on their own site.
FORM = "https://job-boards.greenhouse.io/embed/job_app?for={}&token={}"
NO_FORM = "The Greenhouse application form didn't load. The job may be closed."
DONE = r"thank you for applying|application (has been |was )?(submitted|received)|we('ve| have) received your application"


def board_tokens(account) -> list[str]:
    tokens = split_list((account.options or {}).get("boards", ""))
    if not tokens:
        raise ValueError("Add at least one company board name, for example: gitlab")
    for t in tokens:
        if not TOKEN.match(t):
            raise ValueError(f"'{t}' isn't a board name. Use only the part after boards.greenhouse.io/, for example: gitlab")
    return tokens


def parse_board(data: dict, token: str) -> list[JobPosting]:
    return [JobPosting(
        external_id=str(j["id"]),
        title=j.get("title") or "",
        company=j.get("company_name") or token,
        location=(j.get("location") or {}).get("name") or "",
        url=j.get("absolute_url") or "",
        # content arrives HTML-escaped: unescape once to get HTML, then strip tags
        description=browser.html_to_text(html.unescape(j.get("content") or "")),
    ) for j in data.get("jobs", [])]


def _split_name(full: str) -> tuple[str, str]:
    first, _, last = (full or "").strip().partition(" ")
    return first, last.strip()


def _choose(page, input_id: str, *values: str) -> bool:
    """New-layout dropdowns are react-selects: type, then take the first suggestion. Tries each value in turn."""
    box = page.locator(f"#{input_id}")
    if box.count() == 0 or not box.first.is_visible():
        return False
    if page.locator(f"#{input_id}").locator("xpath=ancestor::*[contains(@class,'select__control')]").locator(
            "[class*=single-value]").count():
        return False  # already chosen
    options = page.locator(f"#react-select-{input_id}-listbox [role=option]")
    for value in filter(None, values):
        box.click()
        box.fill("")
        page.wait_for_timeout(300)  # typing right after focus drops the first keys
        box.press_sequentially(value, delay=40)
        try:
            options.first.wait_for(timeout=5000)
        except Exception:
            continue
        options.first.click()
        return True
    box.fill("")
    return False


def _country(profile) -> str:
    # shortcut: guesses the phone country from the location and +1 numbers only; add a profile country field if users abroad need it
    if "canada" in (profile.location or "").lower():
        return "Canada"
    phone = (profile.phone or "").strip()
    return "United States" if phone and (not phone.startswith("+") or phone.startswith("+1")) else ""


def fill_form(page, url: str, profile, dry_run: bool = False) -> ApplyResult:
    browser.open_page(page, url)
    if page.locator("#first_name").count() == 0:
        return ApplyResult("failed", NO_FORM)
    first, last = _split_name(profile.full_name)
    filled = []
    # Upload first: Greenhouse parses the resume and overwrites the name and email fields with what it read.
    if browser.upload(page.locator("input[type=file]#resume"), profile.resume_path):
        page.wait_for_timeout(3000)
        if page.get_by_text(profile.resume_path.name).count() == 0:
            return ApplyResult("needs_manual", "Greenhouse didn't accept the resume file. Open the job and apply yourself.")
        filled.append("resume")
    fields = [("first name", "#first_name", first), ("last name", "#last_name", last),
              ("email", "#email", profile.email), ("phone", "#phone", profile.phone)]
    for label, sel, value in fields:
        if browser.put(page.locator(sel), value):
            filled.append(label)
    if _choose(page, "country", _country(profile)):
        filled.append("phone country")
    loc = profile.location or ""
    if _choose(page, "candidate-location", loc, loc.split(",")[0]):
        filled.append("location")
    linkedin = next((u for u in browser.links(profile.links) if "linkedin.com" in u), "")
    if browser.fill_if_empty(browser.by_label(page, r"linkedin"), linkedin):
        filled.append("LinkedIn")
    return browser.finish_form(page, filled, r"submit application|^submit$", DONE, dry_run)


class Greenhouse(Connector):
    def discover(self, account, criteria, secret):
        out = []
        for token in board_tokens(account):
            try:
                data = browser.get(API.format(token), content="true").json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    raise ValueError(f"Greenhouse board '{token}' wasn't found. Check the name after boards.greenhouse.io/.") from None
                raise
            out += parse_board(data, token)
        return browser.relevant(out, criteria)

    def apply(self, account, application, profile, secret, dry_run: bool = False):
        u = urlparse(application.url)
        own = u.path.strip("/").split("/")[0] if u.hostname and u.hostname.endswith("greenhouse.io") else ""
        tokens = [t for t in dict.fromkeys([own, *split_list((account.options or {}).get("boards", ""))]) if TOKEN.match(t)]
        try:
            with browser.page() as page:
                result = ApplyResult("failed", "No board name to open the form with. Check the board names on the Job sites page.")
                for token in tokens:  # shortcut: the job id alone doesn't say which board; try each, fine for a few boards
                    result = fill_form(page, FORM.format(token, application.external_id), profile, dry_run=dry_run)
                    if result.message != NO_FORM:
                        break
                return result
        except Exception as e:
            return ApplyResult("failed", "Greenhouse form error: " + browser.short_error(e))


GREENHOUSE = Greenhouse(
    key="greenhouse", label="Greenhouse", can_apply=True,
    risk_note="Applications go straight to the employer. Job Pot stops and leaves the job for you when the form asks "
              "questions it can't answer or shows a captcha.",
    options=[Option("boards", "Company board names",
                    "The part after boards.greenhouse.io/ — for example: gitlab, airbnb. Comma separated.")],
)
