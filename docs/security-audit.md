# Security audit — 2026-10-28

Two independent reviews ran on the full code base (commit `8cd7b63`) before the first release. Fixes landed in the commit after this report.

## 1. `security-review` skill

One reviewer read every module (auth, crypto, all routes, mail, resume, connectors, worker, Docker files), looking for directly exploitable HIGH and MEDIUM issues. Each candidate then went to a separate false-positive filter, and anything scoring below 8/10 was dropped.

**Result: no findings.**

## 2. `claude-security` plugin scan (effort: medium)

The scan split the repository into components and threat-modelled each one. Researchers then checked every component against every vulnerability category, and a sweep covered whatever that grid missed. Each candidate went to a three-lens adversarial panel: reachability, impact and exploitability. 72 agents, 18 candidates, 15 after removing duplicates, 45 panel votes. Tests, docs and static assets were out of scope.

**Result: 10 verified findings (0 high, 3 medium, 7 low). All fixed or mitigated.**

| ID | Severity | Where | Finding | Fix |
|---|---|---|---|---|
| F1 | Medium | `resume.py` | The email regex took quadratic time on long resume text, so one user could freeze the server for everyone (CWE-1333). | Bounded regex. Resume text is capped at 100,000 characters on upload, on save and before review. Regression test runs on 5 MB. |
| F2 | Medium | `web/auth.py` | The lockout was keyed on the username only, so a stranger could keep any account, including admin, locked out, and could block first-run setup. | The lockout key is now username plus client address. Setup attempts are counted per client too. Test: a stranger's failures don't lock out the owner. |
| F3 | Medium | `compose.yaml` | The default setup published plain HTTP on every interface, so passwords and the setup token crossed the network unencrypted. | Published on `127.0.0.1` by default. `JOBPOT_BIND=0.0.0.0` is an explicit opt-in, with a warning in `.env.example` and the README. `JOBPOT_HTTPS=1` is still needed behind an HTTPS proxy, because the app can't tell. |
| F4, F8 | Low | `web/mailbox.py`, `mail.py` | A saved mailbox password was sent to whatever server was saved later, so someone holding the session could send it to their own server. | Changing the address, server, port or login now drops the saved password and asks for it again. Tested. |
| F5, F7 | Low | `mail.py` | Mailbox settings let any user make the server connect to internal hosts and ports (SSRF) and read back the error. | Mail hosts must resolve to public addresses. `JOBPOT_ALLOW_PRIVATE_MAIL_HOSTS=1` allows LAN mail servers. Tested. |
| F6 | Low | `web/auth.py` | A TOTP code could be reused within its window. | The last accepted time step is stored on the user, and the same or an older step is refused. Tested. |
| F9, F10 | Low | `security.py` | The attempt counter checked and then acted without a lock, so parallel guesses could get past the limit. | Each attempt is counted before the password is checked, under a lock. Test: 200 parallel attempts get exactly 5 through. |

## Known remaining risks (accepted)

- **LinkedIn automation** goes against LinkedIn's user agreement. It's off by default and needs an acknowledgement per site.
- **Lockout state is in memory**, so it resets on restart. A distributed attacker gets 5 guesses per address every 5 minutes. Argon2id, the 12-character minimum and optional 2FA limit how much that helps them.
- **Master key:** whoever can read `/data/secret.key` (or `JOBPOT_SECRET_KEY`) and the database can decrypt every saved site and mailbox password. Protect and back up the data volume.
- **Admin trust:** the server admin controls the master key, so "the admin can't read your saved passwords" only holds inside the app, not on the host.
