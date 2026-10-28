"""Dedicated mailbox: read employer replies over IMAP, answer them over SMTP. Stdlib only."""
import email
import imaplib
import ipaddress
import logging
import socket
import re
import smtplib
import ssl
from datetime import timezone
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import formataddr, formatdate, make_msgid, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from sqlalchemy import select

from . import ai, config, crypto
from .models import Application, Mailbox, Reply, Response, now

MAX_BODY = 20000
FIRST_RUN = 50  # first poll imports only the newest messages, not a whole inbox
LABELS = ("interview", "rejection", "offer", "info", "other")
log = logging.getLogger("jobpot.mail")
MAIL_ERRORS = (imaplib.IMAP4.error, OSError, UnicodeError, crypto.InvalidToken)  # smtplib/ssl errors are OSErrors
# Checked in order: a rejection often also says "thank you for applying".
RULES = (
    ("offer", ("offer letter", "pleased to offer", "happy to offer", "extend an offer", "job offer")),
    ("rejection", ("unfortunately", "not moving forward", "not be moving forward", "other candidates",
                   "position has been filled", "decided not to proceed", "not been selected", "regret to inform")),
    ("interview", ("interview", "schedule a call", "availability", "phone screen", "available for a call", "next steps")),
    ("info", ("received your application", "thank you for applying", "thanks for applying", "application received",
              "application has been received")),
)
SUFFIX = re.compile(r"[\s,]+(inc|llc|ltd|limited|corp|corporation|co|gmbh|plc|company|group|ag|sa)\.?$", re.I)


# ---------- connections (tests monkeypatch these two) ----------

def check_host(host: str, port: int):
    """Users choose these hosts, so refuse internal addresses unless the admin allowed them."""
    if config.ALLOW_PRIVATE_MAIL_HOSTS:
        return
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise OSError(f"can't find the server {host}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise OSError(f"{host} is a private or local address. The server admin can allow these with JOBPOT_ALLOW_PRIVATE_MAIL_HOSTS=1")


def _connect(mb: Mailbox, password: str):
    check_host(mb.imap_host, mb.imap_port)
    ctx = ssl.create_default_context()
    if mb.imap_port == 143:
        conn = imaplib.IMAP4(mb.imap_host, 143, timeout=30)
        conn.starttls(ctx)
    else:
        conn = imaplib.IMAP4_SSL(mb.imap_host, mb.imap_port, ssl_context=ctx, timeout=30)
    conn.login(mb.login or mb.address, password)
    return conn


def _smtp(mb: Mailbox, password: str):
    check_host(mb.smtp_host, mb.smtp_port)
    ctx = ssl.create_default_context()
    if mb.smtp_starttls:
        s = smtplib.SMTP(mb.smtp_host, mb.smtp_port, timeout=30)
        s.starttls(context=ctx)
    else:
        s = smtplib.SMTP_SSL(mb.smtp_host, mb.smtp_port, context=ctx, timeout=30)
    s.login(mb.login or mb.address, password)
    return s


def _why(e: Exception) -> str:
    if isinstance(e, crypto.InvalidToken):
        return "the saved password can't be read, so enter it again"
    detail = getattr(e, "smtp_error", None) or (e.args[0] if isinstance(e, imaplib.IMAP4.error) and e.args else None) or str(e)
    if isinstance(detail, bytes):
        detail = detail.decode(errors="replace")
    return " ".join(str(detail).split())[:200] or e.__class__.__name__


def mailbox_of(db, user) -> Mailbox | None:
    return db.scalar(select(Mailbox).where(Mailbox.user_id == user.id))


def test_connection(mb: Mailbox, password: str) -> str | None:
    try:
        _connect(mb, password).logout()
    except MAIL_ERRORS as e:
        return f"Incoming mail (IMAP) failed: {_why(e)}."
    try:
        _smtp(mb, password).quit()
    except MAIL_ERRORS as e:
        return f"Outgoing mail (SMTP) failed: {_why(e)}."
    return None



# ---------- reading ----------

class _Text(HTMLParser):
    SKIP = {"script", "style", "head", "title"}
    BLOCK = {"p", "div", "br", "li", "tr", "table", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "hr"}

    def __init__(self):
        super().__init__()  # convert_charrefs=True unescapes entities for us
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    p.close()
    lines = (" ".join(line.split()) for line in "".join(p.out).splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _decode(part: Message) -> str:
    data = part.get_payload(decode=True) or b""
    charset = part.get_content_charset()
    if not charset:  # undeclared 8bit: usually UTF-8, otherwise Latin-1 never fails
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("latin-1")
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def extract_body(msg: Message) -> str:
    plain = html = None
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() == "text/plain" and plain is None:
            plain = _decode(part)
        elif part.get_content_type() == "text/html" and html is None:
            html = _decode(part)
    text = plain if plain and plain.strip() else html_to_text(html or "")
    return text.replace("\r\n", "\n").strip()[:MAX_BODY]


def _header(msg: Message, name: str) -> str:
    raw = msg.get(name, "")
    try:
        value = str(make_header(decode_header(raw)))
    except (LookupError, UnicodeError, ValueError):
        value = str(raw)
    return " ".join(value.split())


def _received(msg: Message):
    try:
        dt = parsedate_to_datetime(str(msg.get("Date") or ""))
    except (TypeError, ValueError):
        return now()
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _core(company: str) -> str:
    name = company.strip()
    while (stripped := SUFFIX.sub("", name)) != name:
        name = stripped
    return name.strip(" ,.")


def _matches(app: Application, name: str, labels: set[str], subject: str, body: str) -> bool:
    core = _core(app.company)
    # shortcut: names under 3 letters skipped to avoid false hits; a word-boundary match can still hit common-word names like "Square".
    if len(core) >= 3:
        if re.search(rf"\b{re.escape(core)}\b", f"{name}\n{subject}\n{body}", re.I):
            return True
        if re.sub(r"[^a-z0-9]", "", core.lower()) in labels:
            return True
    return bool(app.title) and re.search(rf"(?<!\w){re.escape(app.title)}(?!\w)", subject, re.I) is not None


def match_application(db, user, from_addr: str, subject: str, body: str) -> Application | None:
    name, addr = parseaddr(from_addr)
    labels = {label.replace("-", "") for label in addr.rpartition("@")[2].lower().split(".") if label}
    newest = (Application.submitted_at.desc().nulls_last(), Application.id.desc())
    for status in (Application.status == "submitted", Application.status.notin_(("submitted", "skipped"))):
        for app in db.scalars(select(Application).where(Application.user_id == user.id, status).order_by(*newest)):
            if _matches(app, name, labels, subject, body):
                return app
    return None


def categorise(subject: str, body: str, db=None) -> str:
    if db is not None and ai.ai_enabled(db):
        answer = ai.generate(db, "Classify this email from an employer about a job application. Answer with exactly one word: "
                                 f"interview, rejection, offer, info or other.\n\nSubject: {subject}\n\n{body[:3000]}")
        word = re.findall(r"[a-z]+", (answer or "").lower())
        if word and word[0] in LABELS:
            return word[0]
    text = f"{subject}\n{body}".lower()
    return next((label for label, words in RULES if any(w in text for w in words)), "other")


def suggest_replies(db, user, response: Response, application: Application | None) -> list[str]:
    name = user.full_name or user.username
    role = application.title if application and application.title else "the role"
    company = application.company if application and application.company else "your team"
    sign = f"\n\nBest regards,\n{name}"
    templates = {
        "rejection": [
            f"Hello,\n\nThank you for letting me know, and for considering me for {role}. I enjoyed learning about {company}. "
            f"If anything similar opens up, I'd be glad to be kept in mind.{sign}",
            f"Hello,\n\nThanks for the update. I'm disappointed, but I appreciate your time. "
            f"Please keep my details on file for future roles at {company}.{sign}",
        ],
        "interview": [
            f"Hello,\n\nThank you for the invitation to interview for {role}. I'd be glad to talk. "
            f"I'm available at these times: [your availability]. Let me know what suits you.{sign}",
            f"Hello,\n\nThanks for getting in touch. I'm very interested in {role} at {company} and happy to schedule a conversation. "
            f"These times work for me: [your availability].{sign}",
        ],
        "offer": [
            f"Hello,\n\nThank you so much for the offer for {role}. I'm excited about joining {company}. "
            f"Could you send the full details in writing, including salary, start date and benefits, so I can review them?{sign}",
            f"Hello,\n\nThank you for the offer. I'd like to review everything carefully. "
            f"Could you send the written offer with the full terms?{sign}",
        ],
        "info": [
            f"Hello,\n\nThank you for confirming you received my application for {role}. I look forward to hearing from you.{sign}",
            "No reply is needed for this message. It only confirms your application arrived.",
        ],
        "other": [
            f"Hello,\n\nThank you for your message about {role}. I'll get back to you shortly.{sign}",
            f"Hello,\n\nThanks for reaching out. Could you tell me a little more about what you need from me?{sign}",
        ],
    }
    drafts = list(templates.get(response.category, templates["other"]))
    draft = ai.generate(db, f"Write a short, polite, plain-text email reply from {name}, who applied for {role} at {company}. "
                            f"Reply only with the email body, signed by {name}. Use [your availability] for any times.\n\n"
                            f"Their email, subject: {response.subject}\n\n{response.body[:3000]}")
    return ([draft[:4000]] if draft else []) + drafts


def _store(db, user, msg: Message) -> int:
    message_id = _header(msg, "Message-ID")[:500]
    if message_id and db.scalar(select(Response.id).where(Response.user_id == user.id, Response.message_id == message_id)):
        return 0
    r = Response(user_id=user.id, message_id=message_id, from_addr=_header(msg, "From")[:500],
                 subject=_header(msg, "Subject"), body=extract_body(msg), received_at=_received(msg))
    app = match_application(db, user, r.from_addr, r.subject, r.body)
    r.application_id = app.id if app else None
    r.category = categorise(r.subject, r.body, db)
    r.suggestions = suggest_replies(db, user, r, app)
    db.add(r)
    db.flush()  # so a duplicate later in the same batch is seen
    return 1


def poll(db, user) -> int:
    """Import new INBOX messages as Responses. Mail-server problems land in mb.last_error, never raised."""
    mb = mailbox_of(db, user)
    if not mb or not mb.enabled:
        return 0
    new = 0
    try:
        if not mb.password_enc:
            raise imaplib.IMAP4.error("no password is saved")
        conn = _connect(mb, crypto.decrypt(user.id, mb.password_enc))
        try:
            conn.select("INBOX", readonly=True)
            # "n:*" always returns the newest UID even when it is below n, so filter again.
            _, data = conn.uid("SEARCH", None, f"UID {mb.last_uid + 1}:*" if mb.last_uid else "ALL")
            uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > mb.last_uid)
            if not mb.last_uid:
                uids = uids[-FIRST_RUN:]
            for uid in uids:
                _, parts = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
                raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
                if raw:
                    try:
                        new += _store(db, user, email.message_from_bytes(raw))
                    except Exception:  # one malformed message must not block every later one
                        db.rollback()
                        log.exception("skipped unreadable message uid %s for user %s", uid, user.id)
                mb.last_uid = uid
                db.commit()  # per message, so a later failure keeps what was already imported
        finally:
            try:
                conn.logout()
            except MAIL_ERRORS:
                pass
        mb.last_error = ""
    except MAIL_ERRORS as e:
        mb.last_error = f"Couldn't read the mailbox: {_why(e)}."
    mb.last_checked_at = now()
    db.commit()
    return new


# ---------- sending ----------

def send_reply(db, user, response: Response, body: str) -> None:
    body = body.strip()
    if not body:
        raise ValueError("The reply is empty. Write something, then send it.")
    mb = mailbox_of(db, user)
    if not mb or not mb.smtp_host or not mb.address or not mb.password_enc:
        raise ValueError("Your mailbox isn't set up for sending. Add the outgoing mail settings on the Mailbox page.")
    to = parseaddr(response.from_addr)[1]
    if "@" not in to:
        raise ValueError("This message has no sender address, so there's nobody to reply to.")
    subject = response.subject.strip()
    msg = EmailMessage()  # default policy rejects newlines in headers
    msg["From"] = formataddr((" ".join(user.full_name.split()), mb.address))
    msg["To"] = to
    msg["Subject"] = subject if re.match(r"re:", subject, re.I) else f"Re: {subject}"
    if response.message_id:
        msg["In-Reply-To"] = msg["References"] = response.message_id
    msg["Message-ID"] = make_msgid(domain=mb.address.rpartition("@")[2])
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)
    try:
        s = _smtp(mb, crypto.decrypt(user.id, mb.password_enc))
        try:
            s.send_message(msg)
        finally:
            try:
                s.quit()
            except MAIL_ERRORS:
                pass
    except MAIL_ERRORS as e:
        raise ValueError(f"The reply wasn't sent: {_why(e)}. Check the outgoing mail settings on the Mailbox page and try again.") from None
    db.add(Reply(user_id=user.id, response_id=response.id, body=body))
    db.commit()
