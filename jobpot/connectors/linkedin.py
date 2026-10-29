import html
import random
import re
import time

from . import browser
from ..matching import split_list
from .base import ApplyResult, Connector, JobPosting, Option

SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
POSTING = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"
PAGE_SIZE = 10  # the guest endpoint returns 10 cards per call today, so page 2 starts at 10
MAX_DESCRIPTIONS = 15
MAX_STEPS = 8
SECURITY = "LinkedIn asked for a security check; sign in once in a normal browser, then Job Pot can try again."
SENT = r"application was sent|your application was sent"


def pause():
    time.sleep(random.uniform(1, 3))


def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _grab(card: str, cls: str) -> str:
    m = re.search(rf'class="[^"]*\b{cls}\b[^"]*"[^>]*>(.*?)</(?:h3|h4|span|a|div)>', card, re.S)
    return _text(m.group(1)) if m else ""


def parse_search(page_html: str) -> list[JobPosting]:
    out = []
    for card in re.split(r"<li[\s>]", page_html)[1:]:
        m = re.search(r"urn:li:jobPosting:(\d+)", card)
        if not m:
            continue
        out.append(JobPosting(
            external_id=m.group(1),
            title=_grab(card, "base-search-card__title"),
            company=_grab(card, "base-search-card__subtitle"),
            location=_grab(card, "job-search-card__location"),
            url=f"https://www.linkedin.com/jobs/view/{m.group(1)}/",
            salary=_grab(card, "job-search-card__salary-info"),
        ))
    return out


def parse_description(page_html: str) -> str:
    m = (re.search(r'show-more-less-html__markup[^>]*>(.*?)<button', page_html, re.S)
         or re.search(r'description__text[^>]*>(.*?)</section>', page_html, re.S))
    return browser.html_to_text(m.group(1)) if m else ""


class LinkedIn(Connector):
    def discover(self, account, criteria, secret):
        phrases = (split_list(criteria.titles) or split_list(criteria.keywords)) if criteria else []
        if not phrases:
            raise ValueError("Add job titles or keywords in your criteria so Job Pot knows what to search for.")
        location = (account.options or {}).get("location", "").strip() or next(iter(split_list(criteria.locations)), "")
        found, error = {}, None
        for phrase in phrases:
            for start in (0, PAGE_SIZE):
                params = {"keywords": phrase, "location": location, "f_AL": "true", "start": start}
                if criteria.remote_only:
                    params["f_WT"] = "2"
                try:
                    cards = parse_search(browser.get(SEARCH, **params).text)
                except Exception as e:  # one blocked page (429) shouldn't lose what we already have
                    error = e
                    break
                finally:
                    pause()
                for p in cards:
                    found.setdefault(p.external_id, p)
                if len(cards) < PAGE_SIZE:
                    break
        if error and not found:
            raise error
        for p in list(found.values())[:MAX_DESCRIPTIONS]:
            try:
                p.description = parse_description(browser.get(POSTING.format(p.external_id)).text)
            except Exception:
                pass  # scoring still works on the title
            pause()
        return list(found.values())

    def apply(self, account, application, profile, secret):
        if not account.login or not secret:
            return ApplyResult("needs_manual", "Add your LinkedIn email and password on the Job sites page.")
        try:
            with browser.page() as page:
                result = _sign_in(page, account.login, secret)
                return result or _easy_apply(page, application.url, profile)
        except Exception as e:
            return ApplyResult("failed", "LinkedIn apply stopped: " + browser.short_error(e, secret))


def _sign_in(page, login: str, secret: str) -> ApplyResult | None:
    browser.open_page(page, "https://www.linkedin.com/login")
    page.locator("#username").fill(login)
    page.locator("#password").fill(secret)
    page.locator("button[type=submit]").first.click()
    try:
        page.wait_for_url(lambda u: "/login" not in u and "/uas/login" not in u, timeout=20_000)
    except Exception:
        pass
    # A wrong password also lands on a /checkpoint/ URL, so look for the field errors first.
    if page.locator("#error-for-password, #error-for-username").filter(has_text=re.compile(r"\S")).count():
        return ApplyResult("failed", "LinkedIn didn't accept the email or password. Update them on the Job sites page.")
    if any(x in page.url for x in ("checkpoint", "challenge", "authwall", "/login")) or browser.captcha_visible(page):
        return ApplyResult("needs_manual", SECURITY)
    return None


def _modal(page):
    return page.locator(".jobs-easy-apply-modal, [data-test-modal][role=dialog], [role=dialog]").first


def _fill_step(modal, profile):
    for pattern, value in ((r"phone|mobile", profile.phone), (r"^\s*(email|e-mail)", profile.email)):
        try:
            browser.fill_if_empty(browser.by_label(modal, pattern).and_(modal.locator("input")), value)
        except Exception:
            pass  # a label match that isn't a text box
    selects = modal.locator("select")
    for i in range(selects.count()):
        s = selects.nth(i)
        try:
            if s.input_value() and not s.input_value().lower().startswith("select"):
                continue
            options = [o.strip() for o in s.locator("option").all_inner_texts()]
            pick = next((o for o in options if profile.email and profile.email.lower() in o.lower()), None)
            if pick:  # only the email chooser is safe to answer; other dropdowns are real questions
                s.select_option(label=pick)
        except Exception:
            continue
    files = modal.locator("input[type=file]")
    has_resume = modal.locator("[class*=document-upload][class*=selected], [aria-checked=true][class*=resume]").count()
    if files.count() and profile.resume_path and not has_resume:
        try:
            files.first.set_input_files(str(profile.resume_path))
            modal.page.wait_for_timeout(2000)
        except Exception:
            pass


def _easy_apply(page, url: str, profile) -> ApplyResult:
    if not url.startswith("https://www.linkedin.com/"):
        return ApplyResult("failed", "This job's link isn't a LinkedIn job page.")
    browser.open_page(page, url)
    if "checkpoint" in page.url or "authwall" in page.url:
        return ApplyResult("needs_manual", SECURITY)
    if not browser.click_button(page, r"easy apply"):
        return ApplyResult("needs_manual", "LinkedIn shows no Easy Apply button for this job. It may be closed, "
                                           "already applied, or apply on the company's site.")
    modal = _modal(page)
    modal.wait_for(timeout=15_000)
    for _ in range(MAX_STEPS):
        _fill_step(modal, profile)
        missing = browser.missing_required(modal)
        if missing:
            return ApplyResult("needs_manual", "LinkedIn asked questions Job Pot can't answer: " + "; ".join(missing[:10]))
        if browser.click_button(modal, r"submit application"):
            if browser.wait_for_text(page, SENT, 20):
                return ApplyResult("submitted")
            return ApplyResult("needs_manual", "Job Pot pressed submit but LinkedIn showed no confirmation. "
                                               "Check My Jobs on LinkedIn before applying again.")
        if not (browser.click_button(modal, r"^review|review your application")
                or browser.click_button(modal, r"^next$|continue to next step")):
            return ApplyResult("needs_manual", "LinkedIn showed a step Job Pot doesn't recognize.")
        page.wait_for_timeout(1500)
        errors = modal.locator(".artdeco-inline-feedback--error")
        if errors.count() and any(t.strip() for t in errors.all_inner_texts()):
            missing = browser.missing_required(modal)
            return ApplyResult("needs_manual", "LinkedIn wants answers Job Pot can't give: "
                               + ("; ".join(missing[:10]) or "; ".join(t.strip() for t in errors.all_inner_texts()[:5])))
    return ApplyResult("needs_manual", f"LinkedIn's form had more than {MAX_STEPS} steps.")


LINKEDIN = LinkedIn(
    key="linkedin", label="LinkedIn", needs_login=True, can_apply=True,
    risk_note="LinkedIn's user agreement forbids automated tools and may restrict accounts; keep the daily limit low.",
    options=[Option("location", "Search location", "For example: Remote, or Austin, Texas", required=False)],
)
