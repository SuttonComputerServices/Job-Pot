"""Resume text extraction and rule-based review. AI review is an optional extra on top."""
import io
import re
from dataclasses import dataclass
from pathlib import Path

from docx import Document
from docx.table import Table
from pypdf import PdfReader

from . import ai

MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
}
UNSUPPORTED = "Job Pot reads PDF, Word (.docx) and plain text (.txt, .md) files. Save your resume as one of those and upload it again."
UNREADABLE = "That file couldn't be read. It may be damaged or password protected. Save a fresh copy, or paste the text instead."

WEAK_PHRASES = ("responsible for", "duties included", "helped with", "worked on", "assisted with", "tasked with",
                "involved in", "participated in", "in charge of", "worked with")
SECTIONS = {
    "Experience": r"experience|employment|work history",
    "Education": r"education|academic",
    "Skills": r"skills|competencies|expertise",
}
BULLET = re.compile(r"^\s*([-*•▪‣◦·●]|\d+[.)])\s+")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"\+?\(?\d[\d ().-]{7,}\d")
LINK = re.compile(r"linkedin\.com|github\.com|https?://|www\.", re.I)
NUMBER = re.compile(r"\d|%|\$")
FIRST_PERSON = re.compile(r"\b(I|I'm|I've|[Mm]e|[Mm]y|[Mm]yself)\b")
LONG_LINE_WORDS = 40


@dataclass
class Suggestion:
    title: str
    detail: str
    severity: str  # fix | improve | ok


def _docx_text(data: bytes) -> str:
    lines = []
    for block in Document(io.BytesIO(data)).iter_inner_content():  # keeps paragraphs and tables in reading order
        if isinstance(block, Table):
            for row in block.rows:
                cells = dict.fromkeys(c.text.strip() for c in row.cells if c.text.strip())  # merged cells repeat
                lines.append(" | ".join(cells))
            continue
        ppr = block._p.pPr
        is_list = (block.style is not None and block.style.name.startswith("List")) or (ppr is not None and ppr.numPr is not None)
        lines.append(f"- {block.text}" if is_list and block.text.strip() else block.text)  # Word bullets are formatting, not text
    return "\n".join(lines)


def _pdf_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError(UNREADABLE)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def extract_text(filename: str, data: bytes) -> str:
    """Plain text of an uploaded resume. A scanned (image-only) PDF gives ""."""
    ext = Path(filename).suffix.lower()
    if ext not in MIME:
        raise ValueError(UNSUPPORTED)
    try:
        if ext == ".pdf":
            text = _pdf_text(data)
        elif ext == ".docx":
            text = _docx_text(data)
        else:
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = data.decode("cp1252", errors="replace")
    except Exception as e:  # untrusted file: any parser failure is the file's fault, not ours
        raise ValueError(UNREADABLE) from e
    return text.replace("\x00", "").strip()


def _has_phone(text: str) -> bool:
    # Digit count keeps date ranges like "2018 - 2022" from passing as a phone number.
    return any(10 <= sum(ch.isdigit() for ch in m) <= 15 for m in PHONE.findall(text))


def _terms(criteria) -> list[str]:
    if criteria is None:
        return []
    raw = f"{criteria.keywords},{criteria.titles}".split(",")
    return list({t.strip().lower(): t.strip() for t in raw if t.strip()}.values())


def _has(term: str, text: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.I) is not None


def review(text: str, criteria) -> list[Suggestion]:
    """Rule checks, each giving one suggestion. criteria is a models.Criteria or None."""
    text = text.strip()
    if not text:
        return [Suggestion("Add your resume", "Upload a file or paste your resume text, then save it.", "fix")]
    out = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    found = {"email address": EMAIL.search(text), "phone number": _has_phone(text), "LinkedIn or portfolio link": LINK.search(text)}
    missing = [name for name, ok in found.items() if not ok]
    if missing:
        sev = "fix" if len(missing) > 1 or missing[0] != "LinkedIn or portfolio link" else "improve"
        out.append(Suggestion("Add your contact details", f"Your resume is missing: {', '.join(missing)}. Put contact details at the top so employers can reach you.", sev))
    else:
        out.append(Suggestion("Contact details", "Email, phone and a link are all there.", "ok"))

    words = len(re.findall(r"\w+", text))
    if words < 250:
        out.append(Suggestion("Add more detail", f"Your resume is {words} words. Most strong resumes are 400 to 800. Describe what you did and what changed because of it.", "fix"))
    elif words > 1100:
        out.append(Suggestion("Cut it down", f"Your resume is {words} words. Aim for under 1,000 by dropping old or unrelated roles.", "improve"))
    else:
        out.append(Suggestion("Length", f"{words} words is a good length.", "ok"))

    bullets = [ln for ln in lines if BULLET.match(ln)]
    if not bullets:
        out.append(Suggestion("Use bullet points", "No bullet points found. List each role's achievements as short bullets that start with a verb.", "fix"))
    else:
        counted = sum(1 for b in bullets if NUMBER.search(b))
        if counted / len(bullets) < 0.3:
            out.append(Suggestion("Show results with numbers", f"Only {counted} of {len(bullets)} bullet points include a number. Add figures such as money saved, time cut, people led or percent growth.", "improve"))
        else:
            out.append(Suggestion("Quantified results", f"{counted} of {len(bullets)} bullet points include a number.", "ok"))

    weak = [p for p in WEAK_PHRASES if re.search(rf"\b{p}\b", text, re.I)]
    if weak:
        out.append(Suggestion("Replace weak phrases", f"Found: {', '.join(weak)}. Start with a strong verb instead, such as led, built, cut, grew or launched.", "improve"))
    else:
        out.append(Suggestion("Wording", "No weak phrases such as \"responsible for\".", "ok"))

    pronouns = FIRST_PERSON.findall(text)
    if pronouns:
        out.append(Suggestion("Drop first-person words", f"Found \"I\", \"me\" or \"my\" {len(pronouns)} times. Resumes read better without them: \"Led a team of 5\", not \"I led a team of 5\".", "improve"))
    else:
        out.append(Suggestion("Voice", "No first-person words.", "ok"))

    terms = _terms(criteria)
    if not terms:
        out.append(Suggestion("Set your search criteria", "Add keywords and job titles on the Job sites page so Job Pot can check your resume against them.", "improve"))
    elif absent := [t for t in terms if not _has(t, text)]:
        out.append(Suggestion("Add your target keywords", f"These words from your criteria aren't in your resume: {', '.join(absent)}. Add the ones that truly fit your experience, since they count toward each job's fit score.", "fix"))
    else:
        out.append(Suggestion("Target keywords", "Every keyword and title from your criteria appears in your resume.", "ok"))

    short = [ln for ln in lines if len(ln.split()) <= 5]  # headings are short lines
    gone = [name for name, rx in SECTIONS.items() if not any(re.search(rf"\b({rx})\b", ln, re.I) for ln in short)]
    if gone:
        out.append(Suggestion("Add section headings", f"No heading found for: {', '.join(gone)}. Clear headings help employers and job sites find each part.",
                              "fix" if "Experience" in gone else "improve"))
    else:
        out.append(Suggestion("Sections", "Experience, education and skills each have a heading.", "ok"))

    long = [ln for ln in lines if len(ln.split()) > LONG_LINE_WORDS]
    if long:
        out.append(Suggestion("Break up long paragraphs", f"{len(long)} paragraph{'s run' if len(long) > 1 else ' runs'} over {LONG_LINE_WORDS} words, starting \"{' '.join(long[0].split()[:8])}...\". Split long paragraphs into bullets of one or two lines.", "improve"))
    else:
        out.append(Suggestion("Paragraphs", "No overly long paragraphs.", "ok"))
    return out


def ai_review(db, text: str, criteria) -> str | None:
    """Free-text review from the local model, or None when AI is off or fails."""
    if not text.strip():
        return None
    goal = ""
    if criteria is not None and (criteria.titles.strip() or criteria.keywords.strip()):
        goal = f"The person is applying for: {criteria.titles or 'any role'}. Keywords they care about: {criteria.keywords or 'none given'}.\n"
    prompt = (
        "You are an experienced recruiter reviewing a resume.\n" + goal +
        "Give 5 to 8 specific improvements, most important first, as a plain-text numbered list. "
        "For each, quote the line it applies to and show a better version. "
        "Never invent experience, employers, dates or numbers; where a number would help, say what kind to add. "
        "Do not rewrite the whole resume. No markdown formatting.\n\n"
        # shortcut: long resumes are cut to 12k chars for the model's context, raise if models get bigger windows
        f"Resume:\n{text[:12000]}"
    )
    return ai.generate(db, prompt, timeout=120)
