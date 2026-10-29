import re
from datetime import datetime, timezone
from hashlib import sha256

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select

from .. import ai, crypto, matching
from ..connectors.base import JobPosting
from ..connectors.registry import REGISTRY
from ..db import get_db
from ..models import Application, Criteria, Resume, SiteAccount
from ..security import get_owned
from .deps import current_user, flash, redirect, render

router = APIRouter()
STATUSES = {"found": "Found", "submitted": "Applied", "needs_manual": "Needs you", "failed": "Failed", "skipped": "Skipped"}
MANUAL_SITES = {"manual": "Other site", "ziprecruiter": "ZipRecruiter", "linkedin": "LinkedIn", "indeed": "Indeed",
                "monster": "Monster"}


def criteria_for(db, user) -> Criteria:
    c = db.scalar(select(Criteria).where(Criteria.user_id == user.id))
    if c is None:
        c = Criteria(user_id=user.id)
        db.add(c)
        db.flush()
    return c


def resume_text(db, user) -> str:
    r = db.scalar(select(Resume).where(Resume.user_id == user.id))
    return r.text if r else ""


def _posting(a: Application) -> JobPosting:
    return JobPosting(a.external_id, a.title, a.company, a.location, a.url, a.description, a.salary)


def _site_label(key: str) -> str:
    return REGISTRY[key].label if key in REGISTRY else MANUAL_SITES.get(key, key)


# ---------- Applications ----------

@router.get("/")
def applications(request: Request, status: str = "", user=Depends(current_user), db=Depends(get_db)):
    q = select(Application).where(Application.user_id == user.id)
    if status in STATUSES:
        q = q.where(Application.status == status)
    elif status != "all":
        q = q.where(Application.status != "skipped")
    apps = db.scalars(q.order_by(Application.fit_score.desc(), Application.created_at.desc()).limit(300)).all()
    counts = {k: 0 for k in STATUSES}
    for s in db.scalars(select(Application.status).where(Application.user_id == user.id)):
        counts[s] = counts.get(s, 0) + 1
    return render(request, "applications.html", user, apps=apps, counts=counts, statuses=STATUSES, current=status,
                  site_label=_site_label, manual_sites=MANUAL_SITES, ai_on=ai.ai_enabled(db))


@router.post("/applications")
def add_application(request: Request, site: str = Form("manual"), title: str = Form(...), company: str = Form(...),
                    url: str = Form(""), location: str = Form(""), description: str = Form(""), applied: str = Form(""),
                    user=Depends(current_user), db=Depends(get_db)):
    url = url.strip()
    if url and not re.match(r"^https?://", url):
        flash(request, "The job link must start with http:// or https://", "problem")
        return redirect("/")
    site = site if site in MANUAL_SITES else "manual"
    ext = "manual-" + sha256(f"{url or title}|{company}".lower().encode()).hexdigest()[:16]
    if db.scalar(select(Application).where(Application.user_id == user.id, Application.site == site, Application.external_id == ext)):
        flash(request, "That job is already on your list.", "problem")
        return redirect("/")
    a = Application(user_id=user.id, site=site, external_id=ext, title=title.strip()[:300], company=company.strip()[:300],
                    url=url, location=location.strip()[:300], description=description.strip()[:50000])
    a.fit_score, a.fit_notes = matching.score(resume_text(db, user), criteria_for(db, user), _posting(a))
    if applied == "1":
        a.status, a.submitted_at = "submitted", datetime.now(timezone.utc)
    db.add(a)
    db.commit()
    flash(request, f"Added {a.title} at {a.company}.")
    return redirect(f"/#app-{a.id}")


@router.post("/applications/{aid}/status")
def set_status(request: Request, aid: int, status: str = Form(...), user=Depends(current_user), db=Depends(get_db)):
    a = get_owned(db, Application, aid, user)
    if status not in STATUSES:
        raise HTTPException(400, "Unknown status.")
    a.status = status
    if status == "submitted" and not a.submitted_at:
        a.submitted_at = datetime.now(timezone.utc)
    elif status == "found":
        a.submitted_at = None
    db.commit()
    return redirect(f"/#app-{a.id}")


@router.post("/applications/{aid}/delete")
def delete_application(request: Request, aid: int, user=Depends(current_user), db=Depends(get_db)):
    db.delete(get_owned(db, Application, aid, user))
    db.commit()
    flash(request, "Removed from your list.")
    return redirect("/")


@router.post("/applications/{aid}/ai")
def ai_fit(request: Request, aid: int, user=Depends(current_user), db=Depends(get_db)):
    a = get_owned(db, Application, aid, user)
    out = ai.generate(db, (
        "You compare a resume with a job posting. Reply in exactly two lines:\n"
        "SCORE: <0-100 fit>\nREASON: <one sentence>\n\n"
        f"JOB: {a.title} at {a.company}\n{a.description[:6000]}\n\nRESUME:\n{resume_text(db, user)[:6000]}"))
    m = re.search(r"SCORE:\s*(\d{1,3}).*?REASON:\s*(.+)", out or "", re.S)
    if not m:
        flash(request, "The AI model is off or didn't answer, so the score is unchanged.", "problem")
        return redirect(f"/#app-{a.id}")
    rule, _ = matching.score(resume_text(db, user), criteria_for(db, user), _posting(a))
    # The model may only nudge the rule-based score, so one odd answer can't reorder everything.
    a.fit_score = max(0, min(100, rule + max(-15, min(15, int(m.group(1)) - rule))))
    a.fit_notes = f"{a.fit_notes.split(' AI:')[0]} AI: {m.group(2).strip()[:300]}"
    db.commit()
    return redirect(f"/#app-{a.id}")


# ---------- Job sites and criteria ----------

@router.get("/sites")
def sites(request: Request, user=Depends(current_user), db=Depends(get_db)):
    accounts = {a.site: a for a in db.scalars(select(SiteAccount).where(SiteAccount.user_id == user.id))}
    c = criteria_for(db, user)
    db.commit()
    return render(request, "sites.html", user, criteria=c, connectors=list(REGISTRY.values()), accounts=accounts)


@router.post("/sites/criteria")
def save_criteria(request: Request, keywords: str = Form(""), exclude_keywords: str = Form(""), titles: str = Form(""),
                  locations: str = Form(""), remote_only: str = Form(""), min_salary: int = Form(0),
                  max_per_day: int = Form(10), min_fit: int = Form(60), user=Depends(current_user), db=Depends(get_db)):
    c = criteria_for(db, user)
    c.keywords, c.exclude_keywords = keywords.strip()[:2000], exclude_keywords.strip()[:2000]
    c.titles, c.locations, c.remote_only = titles.strip()[:2000], locations.strip()[:2000], remote_only == "1"
    c.min_salary = max(0, min(min_salary, 10_000_000))
    c.max_per_day = max(0, min(max_per_day, 100))
    c.min_fit = max(0, min(min_fit, 100))
    db.commit()
    flash(request, "Criteria saved. New postings are scored with them from now on.")
    return redirect("/sites")


def _account(db, user, key: str) -> SiteAccount:
    if key not in REGISTRY:
        raise HTTPException(404)
    acc = db.scalar(select(SiteAccount).where(SiteAccount.user_id == user.id, SiteAccount.site == key))
    if acc is None:
        acc = SiteAccount(user_id=user.id, site=key, options={})
        db.add(acc)
    return acc


async def _save_site(request: Request, key: str, user, db) -> SiteAccount:
    form = await request.form()
    conn, acc = REGISTRY.get(key), _account(db, user, key)
    acc.enabled = form.get("enabled") == "1"
    acc.risk_ack = form.get("risk_ack") == "1"
    acc.auto_submit = conn.can_apply and form.get("auto_submit") == "1"
    if acc.auto_submit and conn.risk_note and not acc.risk_ack:
        acc.auto_submit = False
        flash(request, f"Automatic applying on {conn.label} stays off until you tick the acknowledgement.", "problem")
    if conn.needs_login:
        acc.login = str(form.get("login", "")).strip()[:200]
        if password := str(form.get("password", "")):
            acc.secret_enc = crypto.encrypt(user.id, password)
        if form.get("forget") == "1":
            acc.secret_enc = None
    acc.options = {o.name: str(form.get(o.name, "")).strip()[:2000] for o in conn.options}
    db.commit()
    return acc


@router.post("/sites/{key}")
async def save_site(request: Request, key: str, user=Depends(current_user), db=Depends(get_db)):
    await _save_site(request, key, user, db)
    flash(request, f"{REGISTRY[key].label} settings saved.")
    return redirect(f"/sites#site-{key}")


@router.post("/sites/{key}/search")
async def search_now(request: Request, key: str, user=Depends(current_user), db=Depends(get_db)):
    from starlette.concurrency import run_in_threadpool

    from ..worker import discover_site  # worker imports connectors + Playwright; keep it off the import path
    acc = await _save_site(request, key, user, db)  # search with what's on screen, not the last saved form
    if not acc.enabled:
        flash(request, "Tick the box to search this site, then press Search now.", "problem")
        return redirect(f"/sites#site-{key}")
    n = await run_in_threadpool(discover_site, db, user, acc)  # network calls; keep the event loop free
    if acc.last_error:
        flash(request, f"{REGISTRY[key].label}: {acc.last_error}", "problem")
    else:
        flash(request, f"{REGISTRY[key].label}: {n} new posting{'s' if n != 1 else ''} added to Applications.")
    return redirect(f"/sites#site-{key}")
