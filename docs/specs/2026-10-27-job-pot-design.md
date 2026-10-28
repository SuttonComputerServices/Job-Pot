# Job Pot — design spec (2026-10-27)

Source requirements: `AI/job finder/prompts/inception prompts/1.txt`, `2.txt`, `3.txt`.
Brainstorming approvals were not collected live (the owner asked for an unattended run with
questions written to `reports/<date>.txt`). Decisions below are mine; open questions are in the report.

## What the owner asked for (verbatim intent)
- Web app, self-hostable, light + dark mode, desktop + mobile.
- Multi-account database; accounts isolated from each other; all credentials encrypted.
- Base `admin` account with **no default password**; the host sets it on first login.
- Upload resume + personal info; connect to job sites (LinkedIn, ZipRecruiter, etc.);
  auto-submit applications meeting criteria 24/7.
- Dedicated mailbox: read employer responses, show them per application, suggested replies,
  write own reply.
- Single pages: Sites + criteria, Mailbox, Applications (best fit → worst), Resume builder.
- User-icon dropdown (top right): Profile, Settings, FAQ, About, Terms.
  Profile: change login username. Settings: TOTP 2FA.
- Optional local AI (Ollama). Containerized. Security-audited. README with screenshots. Push to `main`.
- Not the usual blue/purple/red palette; should not look AI-built.

## Hard facts found during exploration (2026-10-27, from this machine)
| Source | Result | Meaning |
|---|---|---|
| `boards-api.greenhouse.io/v1/boards/<co>/jobs` | 200, public JSON | Discovery works, no auth |
| `api.lever.co/v0/postings/<co>?mode=json` | 200, public JSON | Discovery works, no auth |
| `linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search` | 200, HTML cards | Discovery works without login |
| `ziprecruiter.com/jobs-search` | 403 (bot wall) | No scripted discovery possible from here |

No job site offers a public API that lets a job seeker's tool **submit** an application.
Submitting therefore means driving a real browser (Playwright) on the user's behalf.
LinkedIn's User Agreement forbids bots; accounts can be restricted. So auto-submit is
**off by default, per site, behind an explicit risk acknowledgement**.

## Stack (and why)
- **Python 3.12, FastAPI, Jinja2 server-rendered pages, plain CSS, ~no JS.** One language, small
  deps, works without a build step; server rendering + hand-written CSS avoids the "template
  dashboard" look.
- **SQLite via SQLAlchemy 2** in a Docker volume. `DATABASE_URL` can point at Postgres later
  without code changes (not tested).
- **Playwright (Chromium)** for browser-driven applying and for README screenshots.
- **imaplib / smtplib** (stdlib) for the mailbox.
- **argon2-cffi** password hashing, **cryptography (Fernet + HKDF)** for secrets,
  **pyotp + segno** for TOTP + QR (SVG, no image deps), **httpx** for HTTP/Ollama,
  **pypdf + python-docx** for resume text extraction.

## Processes
- `web`: uvicorn serving the UI.
- `worker`: same image, `python -m jobpot.worker`; loops every `JOBPOT_WORKER_INTERVAL` (default
  900 s): per user with automation on → discover → filter → score → apply (within caps) → poll mailbox.
- `ollama` (compose profile `ai`, optional).

## Security model
- **Passwords:** argon2id. Login lockout: 5 failures per username → 5 min (in-memory; per process).
- **Admin bootstrap:** `admin` row created with `password_hash = NULL`. Logging in as `admin`
  while unset redirects to `/setup`, which also requires a **one-time setup token** printed to the
  container log at startup (and written to `data/setup-token`, mode 0600). Prevents a stranger who
  reaches the URL first from claiming the instance. Token deleted after use.
- **Secrets at rest:** master key from `JOBPOT_SECRET_KEY` or generated once into
  `data/secret.key` (0600). Per-user key = HKDF-SHA256(master, info=`user:<id>`) → Fernet.
  Encrypted: site passwords/cookies, mailbox password, TOTP secret. A row copied to another user
  cannot be decrypted with that user's key (tested).
- **Isolation:** every data row has `user_id`; every lookup goes through `get_owned(db, Model, id,
  user)` which 404s on mismatch (tested).
- **Sessions:** signed cookie (Starlette SessionMiddleware, key derived from master), `SameSite=Lax`,
  `Secure` when `JOBPOT_HTTPS=1`. **CSRF** token per session, required on every POST.
- **2FA:** TOTP (RFC 6238), enabling requires a valid code; login asks for it after the password.
- **AI endpoint** is instance-wide and admin-only (avoids per-user SSRF to internal hosts).
- Security headers: CSP (`default-src 'self'`), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`.

## Data model (all user tables carry `user_id`, FK cascade on delete)
- `users`: username (unique), password_hash (nullable only for bootstrap admin), is_admin,
  must_change_password, totp_secret_enc, totp_enabled, full_name, email, phone, location, links,
  theme (`system|light|dark`), automation_enabled.
- `resumes`: one per user — filename, mime, file bytes, extracted text, edited text, updated_at.
- `criteria`: one per user — keywords, exclude_keywords, titles, locations, remote_only,
  min_salary, max_per_day, min_fit.
- `site_accounts`: site, enabled, auto_submit, risk_ack, login, secret_enc, options (JSON:
  board tokens / search location), last_run_at, last_error.
- `mailboxes`: one per user — address, imap host/port, smtp host/port/starttls, login,
  password_enc, last_uid, enabled, last_error.
- `applications`: site, external_id (unique per user+site), title, company, location, url,
  description, salary, fit_score, fit_notes, status (`found|submitted|needs_manual|failed|skipped`),
  submitted_at, error.
- `responses`: application_id (nullable), message_id, from_addr, subject, body, received_at,
  category (`interview|rejection|offer|info|other`), suggestions (JSON list).
- `replies`: response_id, body, sent_at.
- `app_settings`: key/value — `ai_enabled`, `ollama_url`, `ollama_model`.

## Fit score (0–100, deterministic; AI optional)
Tokenise resume text and job text (lowercase words, drop stop-words). Score =
60 × (share of criteria keywords found in job) + 30 × (overlap of job's top terms with resume
terms) + 10 if the title matches a desired title. Excluded keyword in title/description → skipped.
Applications page sorts by `fit_score desc`. With AI on, an Ollama prompt may adjust the score
± and return a one-line rationale (stored in `fit_notes`).

## Connectors (`jobpot/connectors/`)
Interface: `discover(account, criteria) -> list[JobPosting]`, `apply(account, posting, profile,
resume_path) -> ApplyResult` (`submitted|needs_manual|failed`, message).
| Site | Discover | Apply |
|---|---|---|
| LinkedIn | guest search API (verified) | Playwright Easy Apply, login with stored creds. **Unverified** (no test account). Stops at unknown required questions → `needs_manual` |
| Greenhouse | public board API per company token (verified) | Playwright fill of hosted form; fill verified in dry-run; real submit click unverified, captcha → `needs_manual` |
| Lever | public postings API per company (verified) | same as Greenhouse |
| ZipRecruiter | not possible from scripts (403) | `needs_manual` only; jobs can be added by URL+paste |
| Manual | "Add job" form on Applications page | user applies themselves, marks submitted |

## Mailbox
IMAP (SSL) poll of INBOX for UID > last_uid. Each message: match to an application (company name
or job title in From/Subject/body, most recent submitted wins), categorise by keyword rules
(AI if on), generate 2–3 suggested replies (templates; AI if on). Replies go out via SMTP with
`In-Reply-To`/`References` headers. "Test connection" button on the Mailbox page.

## Resume builder
Upload PDF/DOCX/TXT → text extracted. Checks: contact info present, length, bullet usage,
quantified achievements, weak phrases, action verbs, criteria keywords missing from resume,
date consistency. Each check → a suggestion. Text editor to revise; saved text is what scoring uses.
AI review button when AI on.

## UI
Pages: Login, Setup, 2FA, Applications (home), Sites, Mailbox, Resume, Profile, Settings, FAQ,
About, Terms, Admin (users + AI). Header: wordmark left, nav, user icon → `<details>` dropdown
(no JS). Theme: `prefers-color-scheme` default; user choice stored on the account.
Look: paper/ink palette, deep green + ochre accents, serif headings, monospace figures, hairline
rules, no gradients/emoji/glow. Mobile: nav collapses to a scrollable row; tables become stacked rows.

## Testing
pytest + FastAPI TestClient: crypto round-trip and cross-user failure, setup-token flow, CSRF
rejection, login lockout, TOTP, cross-user 404s, scoring/filtering, email matching/categorising,
resume checks, connector parsing against saved fixtures. Live checks run once by hand (recorded
in the report), not in CI.

## Out of scope (named, not built)
Indeed/Glassdoor connectors, LLM answering of custom application questions, Postgres testing,
multiple resumes per user, password-reset email, OAuth mail (Gmail needs an app password).
