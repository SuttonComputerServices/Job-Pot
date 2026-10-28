"""The 24/7 loop: for each user with automation on, discover > filter > score > apply (within caps) > poll mail."""
import logging
import random
import re
import signal
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select

from . import config, crypto, matching
from .connectors.base import ApplyResult, Profile
from .connectors.registry import REGISTRY
from .db import SessionLocal, init_db
from .models import Application, Criteria, Mailbox, Resume, SiteAccount, User, now

log = logging.getLogger("jobpot.worker")
STATUSES = {"submitted", "needs_manual", "failed"}
_stop = False


def pause():
    """Between submissions, so sites don't see a burst. Tests patch this."""
    time.sleep(random.uniform(20, 60))


def _short(e: Exception) -> str:
    return (str(e).strip() or type(e).__name__)[:300]


def _criteria(db, user):
    return db.scalar(select(Criteria).where(Criteria.user_id == user.id))


def _resume(db, user):
    return db.scalar(select(Resume).where(Resume.user_id == user.id))


def _secret(user, account) -> str | None:
    return crypto.decrypt(user.id, account.secret_enc) if account.secret_enc else None


def build_profile(user, resume) -> Profile:
    """The caller deletes profile.resume_path."""
    path = None
    if resume is not None and resume.data:
        name = Path(resume.filename or "resume")
        # Employers see the uploaded file name, so keep the user's own name in front of the random part.
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", name.stem)[:60] or "resume"
        with tempfile.NamedTemporaryFile(prefix=stem + "-", suffix=name.suffix, delete=False) as f:
            f.write(resume.data)
        path = Path(f.name)
    return Profile(full_name=user.full_name, email=user.email, phone=user.phone, location=user.location,
                   links=user.links, resume_text=resume.text if resume else "", resume_path=path)


def discover_site(db, user, account) -> int:
    """Adds new postings for one site. Never raises: problems land in account.last_error."""
    new = 0
    try:
        connector = REGISTRY.get(account.site)
        if connector is None:
            raise ValueError(f"Job Pot doesn't know the site '{account.site}'.")
        criteria = _criteria(db, user)
        resume = _resume(db, user)
        postings = connector.discover(account, criteria, _secret(user, account))
        seen = set(db.scalars(select(Application.external_id).where(
            Application.user_id == user.id, Application.site == account.site)))
        for p in postings:
            if p.external_id in seen or matching.excluded(criteria, p):
                continue
            seen.add(p.external_id)
            reason = matching.filter_reason(criteria, p)
            fit, notes = matching.score(resume.text if resume else "", criteria, p)
            db.add(Application(
                user_id=user.id, site=account.site, external_id=p.external_id[:300], title=p.title[:300],
                company=p.company[:300], location=p.location[:300], url=p.url, description=p.description,
                salary=p.salary[:100], fit_score=fit, fit_notes=notes,
                status="skipped" if reason else "found", error=reason or "",
            ))
            new += 1
        account.last_error = ""
    except Exception as e:
        db.rollback()
        new = 0
        log.warning("discover failed for user %s site %s: %s", user.id, account.site, _short(e))
        account.last_error = _short(e)
    account.last_run_at = now()
    db.commit()
    return new


def apply_pending(db, user) -> int:
    """Applies to the best "found" jobs within the daily cap. Returns how many were submitted."""
    if not user.automation_enabled:
        return 0
    criteria = _criteria(db, user)
    cap = criteria.max_per_day if criteria else 10
    min_fit = criteria.min_fit if criteria else 60
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    done = db.scalar(select(func.count()).select_from(Application).where(
        Application.user_id == user.id, Application.submitted_at >= midnight))
    accounts = {a.site: a for a in db.scalars(select(SiteAccount).where(
        SiteAccount.user_id == user.id, SiteAccount.enabled, SiteAccount.auto_submit))
        if a.site in REGISTRY and REGISTRY[a.site].can_apply and (a.risk_ack or not REGISTRY[a.site].risk_note)}
    if not accounts or done >= cap:
        return 0
    # Attempts per run are capped too, so a run of needs_manual results can't hold the loop for hours.
    apps = db.scalars(select(Application).where(
        Application.user_id == user.id, Application.status == "found", Application.fit_score >= min_fit,
        Application.site.in_(accounts)).order_by(Application.fit_score.desc(), Application.id).limit(cap - done)).all()
    resume = _resume(db, user)
    submitted = 0
    for i, app in enumerate(apps):
        if _stop or done >= cap:
            break
        if i:
            pause()
        account = accounts[app.site]
        profile = build_profile(user, resume)
        try:
            result = REGISTRY[app.site].apply(account, app, profile, _secret(user, account))
        except Exception as e:
            log.warning("apply failed for application %s: %s", app.id, _short(e))
            result = ApplyResult("failed", "Job Pot hit an error while applying: " + _short(e))
        finally:
            if profile.resume_path:
                profile.resume_path.unlink(missing_ok=True)
        app.status = result.status if result.status in STATUSES else "failed"
        app.error = "" if app.status == "submitted" else (result.message or "No reason given.")
        if app.status == "submitted":
            app.submitted_at = now()
            done += 1
            submitted += 1
        db.commit()
    return submitted


def run_user(db, user):
    for account in db.scalars(select(SiteAccount).where(SiteAccount.user_id == user.id, SiteAccount.enabled)).all():
        connector = REGISTRY.get(account.site)
        if connector and connector.can_discover:
            discover_site(db, user, account)
    try:
        apply_pending(db, user)
    except Exception:
        db.rollback()
        log.exception("applying failed for user %s", user.id)
    if db.scalar(select(Mailbox.id).where(Mailbox.user_id == user.id, Mailbox.enabled)):
        try:
            from . import mail  # lazy: the worker must still run if mail setup is broken
            mail.poll(db, user)
        except Exception:
            db.rollback()
            log.exception("mail poll failed for user %s", user.id)


def _on_term(*_):
    global _stop
    _stop = True
    log.info("Stopping after the current user")


def run_once():
    with SessionLocal() as db:
        ids = db.scalars(select(User.id).where(User.automation_enabled).order_by(User.id)).all()
    for uid in ids:
        if _stop:
            return
        with SessionLocal() as db:
            try:
                user = db.get(User, uid)
                if user and user.automation_enabled:
                    run_user(db, user)
            except Exception:
                log.exception("worker run failed for user %s", uid)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, _on_term)
    init_db()
    log.info("Worker started; checking every %s seconds", config.WORKER_INTERVAL)
    while not _stop:
        try:
            run_once()
        except Exception:
            log.exception("worker pass failed")
        end = time.monotonic() + config.WORKER_INTERVAL
        while not _stop and time.monotonic() < end:
            time.sleep(1)  # short steps so SIGTERM is honoured quickly


if __name__ == "__main__":
    main()
