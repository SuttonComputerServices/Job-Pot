"""Fit score 0-100: 60 keyword coverage + 30 resume/job vocabulary overlap + 10 title match."""
import re
from collections import Counter

STOP = set("""a about above after again all also an and any are as at be because been before being below between both but by
can could did do does doing down during each few for from further had has have having he her here hers him his how i if in
into is it its just me more most my no nor not now of off on once only or other our out over own same she should so some
such than that the their them then there these they this those through to too under until up very was we were what when where
which while who whom why will with would you your yours etc job jobs role team work working company within across including
experience years year strong ability skills using use well new help""".split())
WORD = re.compile(r"[a-z][a-z0-9+#]*(?:[.\-][a-z0-9+#]+)*")
REMOTE = re.compile(r"\b(remote|work from home|wfh|anywhere)\b", re.I)


def split_list(text: str) -> list[str]:
    return [p.strip().lower() for p in re.split(r"[,\n]", text or "") if p.strip()]


def terms(text: str) -> list[str]:
    return [w for w in WORD.findall((text or "").lower()) if len(w) > 2 and w not in STOP]


def _has(phrase: str, text: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


def _job_text(p) -> str:
    return f"{p.title}\n{p.description}".lower()


def excluded(criteria, posting) -> bool:
    if criteria is None:
        return False
    text = _job_text(posting) + " " + (posting.company or "").lower()
    return any(_has(w, text) for w in split_list(criteria.exclude_keywords))


def _salary_max(text: str) -> int | None:
    nums = []
    for n, k in re.findall(r"\$?\s?(\d{2,3}(?:,\d{3})+|\d{2,3})(\s?[kK])?", text or ""):
        v = int(n.replace(",", "")) * (1000 if k else 1)
        if v >= 10000:  # ignore hourly rates and stray numbers
            nums.append(v)
    return max(nums) if nums else None


def filter_reason(criteria, posting) -> str | None:
    if criteria is None:
        return None
    where = f"{posting.location} {posting.title}"
    remote = bool(REMOTE.search(where) or REMOTE.search(posting.description[:600] or ""))
    if criteria.remote_only and not remote:
        return "Not remote"
    places = split_list(criteria.locations)
    if places and not remote and not any(_has(p, posting.location.lower()) for p in places):
        return f"Location {posting.location or 'not given'} isn't in your list"
    top = _salary_max(posting.salary)
    if criteria.min_salary and top is not None and top < criteria.min_salary:
        return f"Pay tops out below {criteria.min_salary:,}"
    return None


def score(resume_text: str, criteria, posting) -> tuple[int, str]:
    text = _job_text(posting)
    keywords = split_list(criteria.keywords) if criteria else []
    found = [k for k in keywords if _has(k, text)]
    # No keywords set: count keyword coverage as neutral rather than zero.
    kw_points = 60 * len(found) / len(keywords) if keywords else 30
    job_top = [w for w, _ in Counter(terms(text)).most_common(40)]
    resume_terms = set(terms(resume_text))
    overlap = [w for w in job_top if w in resume_terms]
    vocab_points = 30 * len(overlap) / len(job_top) if job_top else 0
    titles = split_list(criteria.titles) if criteria else []
    title_hit = any(_has(t, posting.title.lower()) for t in titles)
    total = round(kw_points + vocab_points + (10 if title_hit else 0))
    notes = []
    if keywords:
        notes.append(f"Keywords {len(found)}/{len(keywords)}" + (f": {', '.join(found)}" if found else ""))
        if missing := [k for k in keywords if k not in found]:
            notes.append(f"missing: {', '.join(missing[:6])}")
    notes.append(f"resume overlap {len(overlap)}/{len(job_top)}" if job_top else "no description to compare")
    if title_hit:
        notes.append("title matches")
    return max(0, min(100, total)), "; ".join(notes) + "."
