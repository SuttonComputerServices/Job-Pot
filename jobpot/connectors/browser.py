"""Shared connector helpers: HTTP, HTML to text, and Playwright form filling.
Playwright is imported lazily so discovery, the web app and tests never need a browser."""
import os
import re
import time
from contextlib import contextmanager
from html.parser import HTMLParser

import httpx

from ..matching import split_list
from .base import ApplyResult

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
TIMEOUT_MS = 30_000


def get(url: str, **params) -> httpx.Response:
    r = httpx.get(url, params=params or None, headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
    r.raise_for_status()
    return r


_BLOCK = {"p", "div", "br", "ul", "ol", "li", "tr", "section", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "table"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in _BLOCK and not (self.out and self.out[-1] == "\n- "):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html or "")
    p.close()
    lines = [re.sub(r"[ \t\r\f\v\xa0]+", " ", line).strip() for line in "".join(p.out).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def wanted_terms(criteria) -> list[str]:
    return (split_list(criteria.titles) + split_list(criteria.keywords)) if criteria else []


def relevant(postings: list, criteria) -> list:
    """Cheap pre-filter for whole-board APIs; real scoring happens in the worker."""
    terms = wanted_terms(criteria)
    if not terms:
        return postings
    from ..matching import has_phrase  # whole words: "rest" must not match "interest"
    return [p for p in postings if any(has_phrase(t, f"{p.title}\n{p.description}".lower()) for t in terms)]


def links(text: str) -> list[str]:
    return re.findall(r"https?://[^\s,]+", text or "")


def short_error(e: Exception, secret: str | None = None) -> str:
    msg = (str(e).strip().splitlines() or [type(e).__name__])[0][:200]
    return msg.replace(secret, "***") if secret else msg


@contextmanager
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=os.environ.get("JOBPOT_HEADLESS", "1") != "0")
        try:
            ctx = b.new_context(user_agent=UA, locale="en-US", viewport={"width": 1280, "height": 900})
            ctx.set_default_timeout(TIMEOUT_MS)
            yield ctx.new_page()
        finally:
            b.close()


def open_page(page, url: str):
    """Waits for the app to hydrate: before that, React forms silently ignore file uploads."""
    resp = page.goto(url, wait_until="load")
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass  # some pages poll forever; "load" is enough then
    return resp


def fill_if_empty(locator, value: str) -> bool:
    """True when this call put the value in. Leaves fields the site (or a resume parser) already filled."""
    if not value or locator.count() == 0:
        return False
    el = locator.first
    if not el.is_visible() or not el.is_editable() or el.input_value().strip():
        return False
    el.fill(value)
    return True


def put(locator, value: str) -> bool:
    """Sets the field even if filled: profile contact details beat whatever a resume parser guessed."""
    if not value or locator.count() == 0 or not locator.first.is_editable():
        return False
    locator.first.fill(value)
    return True


def by_label(scope, pattern: str):
    return scope.get_by_label(re.compile(pattern, re.I))


def upload(locator, path) -> bool:
    if not path or locator.count() == 0:
        return False
    locator.first.set_input_files(str(path))
    return True


def click_button(scope, pattern: str) -> bool:
    """Clicks the first visible, enabled button whose accessible name matches."""
    buttons = scope.get_by_role("button", name=re.compile(pattern, re.I))
    for i in range(buttons.count()):
        b = buttons.nth(i)
        if b.is_visible() and b.is_enabled():
            b.click()
            return True
    return False


_MISSING_JS = r"""(root) => {
  root = root || document;
  const out = [];
  const shown = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  for (const e of root.querySelectorAll('input, select, textarea')) {
    if (!(e.required || e.getAttribute('aria-required') === 'true') || e.type === 'hidden' || e.disabled) continue;
    if (e.getAttribute('aria-hidden') === 'true') continue;  // react-select's mirror input; its visible combobox is checked instead
    if (e.type !== 'file' && !shown(e)) continue;  // file inputs often sit hidden behind an Attach button
    let empty;
    if (e.type === 'radio' || e.type === 'checkbox') empty = !document.querySelector(`input[name="${CSS.escape(e.name)}"]:checked`);
    else if (e.type === 'file') empty = !e.files.length;
    else empty = !String(e.value).trim();
    // react-select keeps its text input empty after a choice; the choice lives in a sibling
    const ctl = e.closest('[class*="select__control"]');
    if (!empty || (ctl && ctl.querySelector('[class*="single-value"], [class*="multi-value"]'))) continue;
    const q = ['.application-question', 'fieldset', '.fb-dash-form-element', '.field', 'li'].map(s => e.closest(s)).find(Boolean);
    const qLabel = q && q.querySelector('legend, .application-label, label');
    const own = (e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label');
    const choice = e.type === 'radio' || e.type === 'checkbox';  // their own label is the option, not the question
    let label = (choice ? (qLabel && qLabel.innerText) || own : own || (qLabel && qLabel.innerText)) || e.name || e.id || 'Unnamed question';
    label = label.trim().split('\n')[0].replace(/[*✱]/g, '').trim();
    if (!out.includes(label)) out.push(label);
  }
  return out;
}"""


def missing_required(scope) -> list[str]:
    """Labels of required fields still empty. scope: a Page or a Locator (for example a modal)."""
    return scope.evaluate(_MISSING_JS)


def captcha_visible(page) -> bool:
    """Invisible score-based captchas load on every Greenhouse and Lever form; only a shown widget or challenge blocks us."""
    for f in page.frames:
        if "captcha" not in f.url or "size=invisible" in f.url:
            continue
        try:
            el = f.frame_element()
            box = el.bounding_box()
            if el.is_visible() and box and box["width"] > 10 and box["y"] >= 0:
                return True
        except Exception:
            continue  # frame went away while we looked
    return False


def wait_for_text(page, pattern: str, seconds: float = 15) -> bool:
    rx = re.compile(pattern, re.I)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            if rx.search(page.inner_text("body")):
                return True
        except Exception:
            pass  # page navigating
        page.wait_for_timeout(500)
    return False


def finish_form(page, filled: list[str], submit: str, done: str, dry_run: bool) -> ApplyResult:
    """Shared tail of a one-page form: stop on unanswered questions or a captcha, else submit and look for confirmation."""
    page.wait_for_timeout(1500)  # let uploads and autocomplete settle before checking
    missing = missing_required(page)
    if dry_run:
        return ApplyResult("needs_manual", f"Dry run, not submitted. Filled: {', '.join(filled) or 'nothing'}. "
                           f"Still empty: {'; '.join(missing) or 'nothing'}. Captcha shown: {'yes' if captcha_visible(page) else 'no'}.")
    if missing:
        return ApplyResult("needs_manual", "Required questions Job Pot can't answer: " + "; ".join(missing[:10]))
    if captcha_visible(page):
        return ApplyResult("needs_manual", "The form shows a captcha. Open the job and apply yourself.")
    if not click_button(page, submit):
        return ApplyResult("failed", "Couldn't find the submit button on the form.")
    if wait_for_text(page, done):
        return ApplyResult("submitted")
    if captcha_visible(page):
        return ApplyResult("needs_manual", "The site asked for a captcha after submit. Open the job and apply yourself.")
    missing = missing_required(page)
    if missing:
        return ApplyResult("needs_manual", "The site wants answers to: " + "; ".join(missing[:10]))
    # Not "failed": it may have gone through, and a retry could apply twice.
    return ApplyResult("needs_manual", "Job Pot pressed submit but saw no confirmation. Check your email or the job page before applying again.")
