import re

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select

from .. import crypto, mail
from ..db import get_db
from ..models import Application, Mailbox, Response
from ..security import get_owned
from .deps import current_user, flash, redirect, render

router = APIRouter()
HOST_RE = re.compile(r"^[^\s/\\]+$")


def _mailbox(db, user) -> Mailbox:
    return mail.mailbox_of(db, user) or Mailbox(user_id=user.id, address="", imap_host="", imap_port=993, smtp_host="",
                                                smtp_port=587, smtp_starttls=True, login="", enabled=False, last_error="")


def _page(request, db, user, mb, error=None, status=200):
    unmatched = db.scalars(select(Response).where(Response.user_id == user.id, Response.application_id.is_(None))
                           .order_by(Response.received_at.desc()).limit(50)).all()
    choices = db.scalars(select(Application).where(Application.user_id == user.id, Application.status != "skipped")
                         .order_by(Application.submitted_at.desc().nulls_last(), Application.id.desc()).limit(300)).all()
    return render(request, "mailbox.html", user, mb=mb, unmatched=unmatched, attach_choices=choices, error=error, status=status)


def _app(db, user, r: Response) -> Application | None:
    return get_owned(db, Application, r.application_id, user) if r.application_id else None


def _back(r: Response):
    return redirect(f"/#app-{r.application_id}" if r.application_id else "/mailbox")


def _port(value: str) -> int | None:
    return int(value) if value.strip().isdigit() and 1 <= int(value) <= 65535 else None


@router.get("/mailbox")
def mailbox_page(request: Request, user=Depends(current_user), db=Depends(get_db)):
    return _page(request, db, user, _mailbox(db, user))


@router.post("/mailbox")
def mailbox_save(request: Request, address: str = Form(""), imap_host: str = Form(""), imap_port: str = Form("993"),
                 smtp_host: str = Form(""), smtp_port: str = Form("587"), smtp_starttls: str = Form(""),
                 login: str = Form(""), password: str = Form(""), enabled: str = Form(""),
                 user=Depends(current_user), db=Depends(get_db)):
    mb = _mailbox(db, user)
    on, ip, sp = enabled == "1", _port(imap_port), _port(smtp_port)
    address, imap_host, smtp_host = address.strip()[:200], imap_host.strip().lower()[:200], smtp_host.strip().lower()[:200]
    password = password.strip("\r\n")
    problem = None
    if ip is None or sp is None:
        problem = "Ports must be whole numbers from 1 to 65535. The usual ones are 993 for IMAP and 587 or 465 for SMTP."
    elif any(h and not HOST_RE.match(h) for h in (imap_host, smtp_host)):
        problem = "A server name can't contain spaces or slashes. Enter just the host, like imap.example.com."
    elif on and not (imap_host and smtp_host):
        problem = "Enter both the IMAP and SMTP server names, or untick the mailbox to save without them."
    elif on and "@" not in address:
        problem = "Enter the mailbox's full email address, like jobs@example.com."
    elif on and not (password or mb.password_enc):
        problem = "Enter the mailbox password. Gmail and Outlook need an app password here."
    if problem:
        if mb in db:
            db.expunge(mb)  # show what they typed without saving any of it
        mb.address, mb.imap_host, mb.smtp_host, mb.login, mb.enabled = address, imap_host, smtp_host, login.strip(), on
        mb.imap_port, mb.smtp_port, mb.smtp_starttls = imap_port, smtp_port, smtp_starttls == "1"
        return _page(request, db, user, mb, error=problem, status=400)
    mb.address, mb.imap_host, mb.imap_port, mb.smtp_host, mb.smtp_port = address, imap_host, ip, smtp_host, sp
    mb.smtp_starttls, mb.login, mb.enabled = smtp_starttls == "1", login.strip()[:200], on
    if password:
        mb.password_enc = crypto.encrypt(user.id, password)
    db.add(mb)
    db.commit()
    flash(request, "Mailbox saved." + (" Job Pot will check it for replies." if on else " It won't be checked until you turn it on."))
    return redirect("/mailbox")


@router.post("/mailbox/test")
def mailbox_test(request: Request, user=Depends(current_user), db=Depends(get_db)):
    mb = mail.mailbox_of(db, user)
    if not mb or not mb.password_enc or not (mb.imap_host and mb.smtp_host):
        flash(request, "Save the server names and password first, then test the connection.", "problem")
        return redirect("/mailbox")
    try:
        problem = mail.test_connection(mb, crypto.decrypt(user.id, mb.password_enc))
    except crypto.InvalidToken:
        problem = "The saved password can't be read. Enter it again and save."
    flash(request, problem or "Connected. Job Pot can read and send mail with these settings.", "problem" if problem else "ok")
    return redirect("/mailbox")


@router.post("/mailbox/check")
def mailbox_check(request: Request, user=Depends(current_user), db=Depends(get_db)):
    mb = mail.mailbox_of(db, user)
    if not mb or not mb.enabled:
        flash(request, "The mailbox is off. Tick the box to check it, save, then try again.", "problem")
        return redirect("/mailbox")
    n = mail.poll(db, user)
    if mb.last_error:
        flash(request, f"{mb.last_error} Check the settings, then use Test connection.", "problem")
    else:
        flash(request, "No new messages." if n == 0 else f"{n} new message{'s' if n != 1 else ''}.")
    return redirect("/mailbox")


@router.post("/responses/{rid}/reply")
def response_reply(request: Request, rid: int, body: str = Form(""), user=Depends(current_user), db=Depends(get_db)):
    r = get_owned(db, Response, rid, user)
    try:
        mail.send_reply(db, user, r, body)
        flash(request, "Reply sent.")
    except ValueError as e:
        # shortcut: the typed reply is lost on failure; keep a draft on the Response if people complain.
        flash(request, str(e), "problem")
    return _back(r)


@router.post("/responses/{rid}/suggest")
def response_suggest(request: Request, rid: int, user=Depends(current_user), db=Depends(get_db)):
    r = get_owned(db, Response, rid, user)
    r.suggestions = mail.suggest_replies(db, user, r, _app(db, user, r))
    db.commit()
    flash(request, "New reply suggestions are ready.")
    return _back(r)


@router.post("/responses/{rid}/attach")
def response_attach(request: Request, rid: int, application_id: int = Form(...), user=Depends(current_user), db=Depends(get_db)):
    r = get_owned(db, Response, rid, user)
    a = get_owned(db, Application, application_id, user)
    r.application_id = a.id
    r.category = mail.categorise(r.subject, r.body, db)
    r.suggestions = mail.suggest_replies(db, user, r, a)
    db.commit()
    flash(request, f"Message attached to {a.title or 'the application'}" + (f" at {a.company}." if a.company else "."))
    return _back(r)


@router.post("/responses/{rid}/delete")
def response_delete(request: Request, rid: int, user=Depends(current_user), db=Depends(get_db)):
    r = get_owned(db, Response, rid, user)
    back = _back(r)
    db.delete(r)
    db.commit()
    flash(request, "Message deleted.")
    return back
