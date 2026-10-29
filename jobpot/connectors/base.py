"""Contract every job-site connector follows. The worker and the Job sites page only talk to this."""
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class JobPosting:
    external_id: str
    title: str
    company: str
    location: str
    url: str
    description: str = ""
    salary: str = ""


@dataclass
class ApplyResult:
    status: str  # "submitted" | "needs_manual" | "failed"
    message: str = ""


@dataclass
class Profile:
    full_name: str
    email: str
    phone: str
    location: str
    links: str
    resume_text: str
    resume_path: Path | None  # a temp copy of the uploaded file, for upload fields


@dataclass
class Option:
    name: str   # key in SiteAccount.options
    label: str
    hint: str = ""
    required: bool = True  # searching can't start while a required option is empty


@dataclass
class Connector:
    key: str
    label: str
    needs_login: bool = False
    can_discover: bool = True
    can_apply: bool = False
    risk_note: str = ""
    options: list[Option] = field(default_factory=list)

    def ready_to_search(self, account) -> bool:
        """Switched on, searchable, and every required option filled in."""
        opts = (account.options or {}) if account else {}
        return bool(account and account.enabled and self.can_discover
                    and all(str(opts.get(o.name, "")).strip() for o in self.options if o.required))

    def discover(self, account, criteria, secret: str | None) -> list[JobPosting]:
        """account: SiteAccount, criteria: Criteria, secret: decrypted site password or None."""
        return []

    def apply(self, account, application, profile: Profile, secret: str | None) -> ApplyResult:
        """application: Application row. Must never raise for site problems; return failed/needs_manual."""
        return ApplyResult("needs_manual", "This site doesn't support automatic applying.")
