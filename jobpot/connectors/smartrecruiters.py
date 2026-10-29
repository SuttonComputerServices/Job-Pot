import re

from . import browser
from ..matching import split_list
from .base import Connector, JobPosting, Option

NAME = re.compile(r"^[A-Za-z0-9_-]+$")
API = "https://api.smartrecruiters.com/v1/companies/{}/postings"
# shortcut: reads at most this many full postings per company each run (one request each); raise it if users need more
MAX_DETAILS = 60


def companies(account) -> list[str]:
    names = split_list((account.options or {}).get("companies", ""))
    if not names:
        raise ValueError("Add at least one company name, for example: ServiceNow")
    for n in names:
        if not NAME.match(n):
            raise ValueError(f"'{n}' isn't a company name. Use only the part after jobs.smartrecruiters.com/, for example: ServiceNow")
    return names


def parse_posting(d: dict, company: str) -> JobPosting:
    loc = d.get("location") or {}
    where = loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("country")) if x)
    if loc.get("remote") and "remote" not in where.lower():
        where = f"{where} (Remote)".strip()
    sections = ((d.get("jobAd") or {}).get("sections") or {}).values()
    return JobPosting(
        external_id=str(d["id"]), title=(d.get("name") or "").strip(),
        company=(d.get("company") or {}).get("name") or company, location=where,
        url=d.get("postingUrl") or "",
        description="\n\n".join(t for t in (browser.html_to_text(s.get("text") or "").strip() for s in sections) if t),
    )


class SmartRecruiters(Connector):
    def discover(self, account, criteria, secret):
        out = []
        terms = browser.wanted_terms(criteria)
        for name in companies(account):
            # The list has no descriptions, so search it per term, then read each match in full.
            ids = []
            for q in terms or [None]:
                params = {"limit": 100, **({"q": q} if q else {})}
                for p in browser.get(API.format(name), **params).json().get("content") or []:
                    if p["id"] not in ids:
                        ids.append(p["id"])
            if not ids and not browser.get(API.format(name), limit=1).json().get("totalFound"):
                raise ValueError(f"SmartRecruiters company '{name}' has no open jobs, or the name is wrong. "
                                 "Check the name after jobs.smartrecruiters.com/.")
            for i in ids[:MAX_DETAILS]:
                out.append(parse_posting(browser.get(f"{API.format(name)}/{i}").json(), name))
        return browser.relevant(out, criteria)


SMARTRECRUITERS = SmartRecruiters(
    key="smartrecruiters", label="SmartRecruiters",
    options=[Option("companies", "Company names",
                    "The part after jobs.smartrecruiters.com/ — for example: ServiceNow. Comma separated.")],
)
