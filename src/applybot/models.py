from __future__ import annotations

from dataclasses import dataclass, field

# Job lifecycle. `submitting` is write-ahead intent: set before the submit click and
# never auto-retried after a crash (see db.recover).
DISCOVERED = "discovered"
QUEUED = "queued"
IN_PROGRESS = "in_progress"
NEEDS_ANSWERS = "needs_answers"  # unresolved questions waiting on the answer bank
NEEDS_INPUT = "needs_input"  # a personal fact only the user knows
NEEDS_HUMAN = "needs_human"  # captcha, SSO, attestation, blocked host, ...
SUBMITTING = "submitting"
VERIFY = "verify"  # crashed mid-submit; reconcile before anything else
SUBMITTED = "submitted"
DRY_RUN_DONE = "dry_run_done"
FAILED = "failed"
SKIPPED_INELIGIBLE = "skipped_ineligible"
OFF_SEASON = "off_season"
CLOSED = "closed"
DUPLICATE = "duplicate"
ALREADY_APPLIED = "already_applied"

TERMINAL = {SUBMITTED, SKIPPED_INELIGIBLE, OFF_SEASON, CLOSED, DUPLICATE, ALREADY_APPLIED}

# Sponsorship signal carried by the source list (not the applicant's status).
SPONSOR_OFFERS = "offers"
SPONSOR_NONE = "none"
SPONSOR_CITIZEN = "citizenship_required"
SPONSOR_UNKNOWN = "unknown"


@dataclass
class Job:
    company: str
    title: str
    url: str  # canonical
    raw_url: str = ""
    locations: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)  # CA | US | REMOTE | OTHER | UNKNOWN
    ats: str = "custom"
    board: str = ""
    ats_job_id: str = ""
    key: str = ""  # primary dedupe key: ats:board:job_id
    key2: str = ""  # secondary dedupe key: company|title|first location
    category: str = "Other"
    sponsorship: str = SPONSOR_UNKNOWN
    advanced_degree_only: bool = False
    terms: list[str] = field(default_factory=list)
    season_inferred: bool = False
    active: bool = True
    date_posted: int = 0  # epoch seconds, 0 = unknown
    source: str = ""  # e.g. github:SimplifyJobs/Summer2027-Internships
    source_id: str = ""
