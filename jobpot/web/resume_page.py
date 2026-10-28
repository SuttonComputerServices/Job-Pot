import re
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select

from .. import ai
from ..db import get_db
from ..models import Criteria, Resume
from ..resume import MIME, ai_review, extract_text, review
from .deps import current_user, flash, redirect, render

router = APIRouter()
MAX_BYTES = 5 * 1024 * 1024
SEVERITIES = (("fix", "Fix these first"), ("improve", "Worth improving"), ("ok", "Looks good"))


def _resume(db, user) -> Resume | None:
    return db.scalar(select(Resume).where(Resume.user_id == user.id))


def _criteria(db, user) -> Criteria | None:
    return db.scalar(select(Criteria).where(Criteria.user_id == user.id))


def _page(request: Request, user, db, status: int = 200, **ctx):
    res = _resume(db, user)
    found = review(res.text, _criteria(db, user)) if res and res.text.strip() else []
    groups = [(label, [s for s in found if s.severity == sev]) for sev, label in SEVERITIES]
    return render(request, "resume.html", user, status=status, resume=res, groups=[g for g in groups if g[1]],
                  ai_on=ai.ai_enabled(db), **ctx)


def _safe_name(name: str) -> str:
    # Header injection and path tricks: keep only plain filename characters.
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name).strip("._")
    return clean[:100] or "resume"


@router.get("/resume")
def resume_page(request: Request, user=Depends(current_user), db=Depends(get_db)):
    return _page(request, user, db)


@router.post("/resume/upload")
def resume_upload(request: Request, file: UploadFile = File(...), user=Depends(current_user), db=Depends(get_db)):
    # shortcut: the size cap is checked after Starlette has spooled the upload, add a proxy body limit if abuse matters
    data = file.file.read(MAX_BYTES + 1)
    if not file.filename or not data:
        return _page(request, user, db, status=400, error="Choose a resume file to upload.")
    if len(data) > MAX_BYTES:
        return _page(request, user, db, status=400, error="That file is over 5 MB. Save a smaller copy, for example without images, and try again.")
    try:
        text = extract_text(file.filename, data)
    except ValueError as e:
        return _page(request, user, db, status=400, error=str(e))
    res = _resume(db, user) or Resume(user_id=user.id)
    res.filename, res.data = Path(file.filename).name[:255], data
    res.mime = MIME[Path(file.filename).suffix.lower()]  # from the extension we parsed, not the browser's claim
    if text:
        res.text = text
        flash(request, f"Uploaded {res.filename}. Review the text below and fix anything that came out wrong.")
    else:
        # Keep any earlier text rather than wiping it with nothing.
        flash(request, "This file looks like a scan, so no text could be read from it. Paste your resume text into the box below and save it.", "problem")
    db.add(res)
    db.commit()
    return redirect("/resume")


@router.post("/resume/text")
def resume_text(request: Request, text: str = Form(""), user=Depends(current_user), db=Depends(get_db)):
    res = _resume(db, user) or Resume(user_id=user.id)
    res.text = text.replace("\r\n", "\n").replace("\x00", "").strip()
    db.add(res)
    db.commit()
    flash(request, "Resume text saved. Job Pot uses this text to score jobs.")
    return redirect("/resume")


@router.post("/resume/ai")
def resume_ai(request: Request, user=Depends(current_user), db=Depends(get_db)):
    res = _resume(db, user)
    if not res or not res.text.strip():
        return _page(request, user, db, status=400, error="Add your resume text first, then ask for an AI review.")
    result = ai_review(db, res.text, _criteria(db, user))
    if result is None:
        return _page(request, user, db, error="The AI model is off or unreachable, so there's no AI review. The checks below still apply. An administrator can check the AI settings.")
    return _page(request, user, db, ai_result=result)


@router.get("/resume/download")
def resume_download(user=Depends(current_user), db=Depends(get_db)):
    res = _resume(db, user)
    if not res or not res.data:
        raise HTTPException(404)
    return Response(res.data, media_type=res.mime or "application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{_safe_name(res.filename)}"'})


@router.get("/resume/download.txt")
def resume_download_text(user=Depends(current_user), db=Depends(get_db)):
    res = _resume(db, user)
    if not res or not res.text:
        raise HTTPException(404)
    name = Path(_safe_name(res.filename)).stem or "resume"
    return Response(res.text, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}.txt"'})
