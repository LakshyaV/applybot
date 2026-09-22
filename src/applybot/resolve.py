"""Answer resolution: profile facts + an exact-hash answer bank. There is NO fuzzy matching here.

A question is identified by sha256(normalized label | field type | sorted normalized options). A lookup
is an exact hit or a miss. Bank entries never store "Yes"/"No"; they store how each option maps onto a
profile *fact*, so polarity ("Will you NOT require sponsorship?") lives in the reviewed mapping and the
same entry answers correctly on a Canadian and a US posting.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date

# --- questions ---------------------------------------------------------------------------------

FIELD_TYPES = {"text", "textarea", "select", "multiselect", "checkbox", "file"}


@dataclass
class Question:
    id: str  # ATS field name / DOM id
    label: str
    type: str
    required: bool = False
    options: list[str] = field(default_factory=list)
    section: str = ""  # "", "eeo", "demographic"
    company: str = ""  # only used to template the employer's name out of the label before hashing

    @property
    def template(self) -> str:
        return templated(self.label, self.company)

    @property
    def qhash(self) -> str:
        return question_hash(self.template, self.type, self.options)


def normalize(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "").lower().replace("’", "'")
    text = re.sub(r"[\s ]+", " ", text)
    return text.strip(" *:\t\n")


CORPORATE_SUFFIX_RE = re.compile(r",?\s+(inc|llc|ltd|corp|corporation|co|company|technologies|labs|group|holdings)\.?$", re.I)


def templated(label: str, company: str) -> str:
    """Replace the employer's own name with {company} so one reviewed entry serves every employer that
    uses the same ATS template. Deterministic string substitution — not similarity matching."""
    names = {company.strip(), CORPORATE_SUFFIX_RE.sub("", company.strip())}
    for name in sorted((n for n in names if len(n) >= 3), key=len, reverse=True):
        label = re.sub(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", "{company}", label, flags=re.I)
    return label


def question_hash(label: str, field_type: str, options: list[str]) -> str:
    payload = "|".join([normalize(label), field_type, *sorted(normalize(o) for o in options)])
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


# High-recall trigger list. Anything matching is high-stakes: it resolves only through a bank entry the
# user has approved. False positives cost one extra approval; false negatives are the dangerous direction.
HIGH_STAKES_RE = re.compile(
    r"sponsor|visa|authoriz|authoris|eligible to work|right to work|work permit|citizen|national(ity)?\b|"
    r"resident|u\.?s\.? person|itar|export control|\bear\b|clearance|convict|criminal|felony|misdemeanor|"
    r"background check|drug|non-?compete|restrictive covenant|agreement|obligation|conflict of interest|"
    r"(previously|ever|currently|formerly)[^.?]{0,30}(employed|worked|work for|applied|interviewed|contractor)|"
    r"employed by|former employee|relative|family member|familial|relationship|government|public official|"
    r"own, operate|outside (business|activit)|certif|attest|acknowledg|i agree|i understand|consent|"
    r"licen[cs]e|18 years|legal age|relocat|salary|compensation|pay expectation|hourly rate|rate requirement|"
    r"graduat|gpa|grade point|primary residence|reside|eligib|"
    r"artificial intelligence|\bai\b|chatgpt|generative|completed (this|the) application (yourself|myself)",
    re.I,
)
# Never automated regardless of bank contents (CLAUDE.md rules 5 and 6).
HUMAN_ONLY_RE = re.compile(
    r"social security|\bssn\b|social insurance|\bsin\b number|date of birth|\bdob\b|passport|driver'?s licen|"
    r"bank account|arbitration|artificial intelligence|\bai\b (tool|assist)|chatgpt|generative ai|"
    r"without (the )?(use|help|assistance) of|completed (this|the) application (yourself|myself)",
    re.I,
)


# Critical: a wrong answer is a false legal statement. Cleared only by the USER, once per template.
CRITICAL_RE = re.compile(
    r"sponsor|visa|authoriz|authoris|eligible to work|right to work|work permit|citizen|national(ity)?\b|"
    r"permanent resident|u\.?s\.? person|itar|export control|clearance|convict|criminal|felony|misdemeanor|"
    r"background check|non-?compete|restrictive covenant|arbitrat|certif|attest|acknowledg|i agree|"
    r"i understand|consent|government|public official|conflict of interest",
    re.I,
)
EEO_RE = re.compile(
    r"gender|\bsex\b|race|ethnic|hispanic|latin[oax]|veteran|disabilit|lgbt|sexual orientation|transgender", re.I
)
# The user's standing policy is "decline": choose the form's own decline option, matched exactly against this list.
DECLINE_PHRASES = {
    "decline to self identify", "decline to self-identify", "i decline to self identify", "i decline to self-identify",
    "i don't wish to answer", "i do not wish to answer", "i do not want to answer", "i don't want to answer",
    "prefer not to say", "i prefer not to say", "prefer not to answer", "i prefer not to answer",
    "prefer not to disclose", "i prefer not to disclose", "choose not to disclose", "i choose not to disclose",
    "decline to answer", "decline to state", "i do not wish to self-identify", "i do not wish to disclose",
    "i do not wish to provide this information", "do not wish to answer", "not declared", "prefer not to respond",
    "prefer not to state", "i prefer not to state", "prefer not to identify", "prefer not to self-identify",
    "i'd rather not say", "i would rather not say", "rather not say", "i'd rather not disclose",
    "i do not wish to identify", "i choose not to self-identify", "choose not to self-identify", "choose not to identify",
}  # fmt: skip
LOW, VERIFY, CRITICAL = 0, 1, 2


def is_eeo(question: Question) -> bool:
    return question.section in ("eeo", "demographic") or bool(EEO_RE.search(question.label))


def tier(question: Question) -> int:
    """CRITICAL → user approval · VERIFY → independent truth-verifier pass · LOW → validated proposal."""
    if CRITICAL_RE.search(question.label):
        return CRITICAL
    if HIGH_STAKES_RE.search(question.label) or is_eeo(question):
        return VERIFY
    return LOW


def is_high_stakes(question: Question) -> bool:
    return tier(question) > LOW


def eeo_decline_option(question: Question) -> str | None:
    matches = [o for o in question.options if normalize(o).rstrip(".") in DECLINE_PHRASES]
    return matches[0] if len(matches) == 1 else None


# --- facts -------------------------------------------------------------------------------------


@dataclass
class JobContext:
    company: str
    countries: list[str]  # from normalize.countries_of, refined by the ATS at apply time

    @property
    def country(self) -> str | None:
        """The single country whose work-authorization facts apply, or None when ambiguous."""
        real = [c for c in self.countries if c in ("CA", "US")] + [c for c in self.countries if c == "OTHER"]
        return real[0] if len(set(real)) == 1 else None


class Unknown(Exception):
    """The profile does not contain this fact (null) — only the user can supply it."""


class Ambiguous(Exception):
    """A country-dependent fact was needed but the job's country is unknown or mixed."""


MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
               "October", "November", "December"]  # fmt: skip
DEMONYMS = {"Canada": "Canadian", "United States": "U.S.", "United Kingdom": "British", "India": "Indian"}
EU_MEMBERS = {"Austria", "Belgium", "Bulgaria", "Croatia", "Cyprus", "Czechia", "Denmark", "Estonia", "Finland", "France",
              "Germany", "Greece", "Hungary", "Ireland", "Italy", "Latvia", "Lithuania", "Luxembourg", "Malta",
              "Netherlands", "Poland", "Portugal", "Romania", "Slovakia", "Slovenia", "Spain", "Sweden"}  # fmt: skip
COUNTRY_PARAM_FACTS = {"work_authorized", "requires_sponsorship"}
DECLINE = "decline"


class Facts:
    """Read-only view of profile.md front-matter, exposed as flat fact keys."""

    def __init__(self, profile: dict, ctx: JobContext):
        self.p, self.ctx = profile, ctx

    def get(self, key: str):
        value = self._lookup(key)
        if value is None:
            raise Unknown(key)
        return value

    def _lookup(self, key: str):
        if key in COUNTRY_PARAM_FACTS:
            country = self.ctx.country
            if country is None:
                raise Ambiguous(key)
            auth = self.p["work_authorization"]
            field_name = "authorized" if key == "work_authorized" else key
            return auth["by_country"].get(country, auth["default"])[field_name]
        if key.startswith("eeo_"):
            return self.p["eeo"].get(key[4:], DECLINE)
        simple = self._simple()
        if key not in simple:
            raise KeyError(f"unknown fact key: {key}")
        return simple[key]

    def _simple(self) -> dict:
        p = self.p
        ident, links, addr = p["identity"], p["links"], p["address"]
        edu, avail, hist = p["education"][0], p["availability"], p["history"]
        auth = p["work_authorization"]
        end = edu.get("end")  # "2030-04"
        month = MONTH_NAMES[int(end[5:7]) - 1] if end else None
        company = normalize(self.ctx.company)
        worked_here = any(company in normalize(past) or normalize(past) in company
                          for past in hist.get("previously_employed_at", [])) if company else None  # fmt: skip
        return {
            "first_name": ident["first_name"], "last_name": ident["last_name"], "full_name": ident["full_name"],
            "preferred_name": ident["preferred_name"], "email": ident["email"], "phone": ident["phone_display"],
            "phone_e164": ident["phone_e164"], "phone_national": ident["phone_national"],
            "pronouns": ident.get("pronouns"), "over_18": ident.get("over_18"),
            "linkedin": links["linkedin"], "github": links["github"], "website": links["website"],
            "address_line1": addr.get("line1"), "city": addr.get("city"), "province_state": addr.get("province_state"),
            "postal_code": addr.get("postal_code"), "country_of_residence": addr.get("country"),
            "province_code": addr.get("province_code"),
            "location_full": ", ".join(x for x in (addr.get("city"), addr.get("province_state"), addr.get("country")) if x)
            if addr.get("city") else None,
            "us_resident": addr.get("country") == "United States" if addr.get("country") else None,
            "citizenship_country": auth["citizenships"][0] if auth["citizenships"] else None,
            # Questions that NAME a country must use these, never the per-job-country facts: "authorized to work
            # in the United States?" is false for this user even on a Canadian posting.
            "us_work_authorized": auth["by_country"].get("US", auth["default"]).get("authorized"),
            "us_requires_sponsorship": auth["by_country"].get("US", auth["default"]).get("requires_sponsorship"),
            "ca_work_authorized": auth["by_country"].get("CA", auth["default"]).get("authorized"),
            "ca_requires_sponsorship": auth["by_country"].get("CA", auth["default"]).get("requires_sponsorship"),
            "intl_work_authorized": auth["default"].get("authorized"),  # any country other than CA / US
            "intl_requires_sponsorship": auth["default"].get("requires_sponsorship"),
            "address_full": ", ".join(x for x in (addr.get("line1"), addr.get("city"), addr.get("province_state"),
                                                  addr.get("postal_code"), addr.get("country")) if x)
            if addr.get("line1") else None,
            "earliest_start_month": MONTH_NAMES[int(avail["earliest_start"][5:7]) - 1] if avail.get("earliest_start") else None,
            "most_recent_employer": hist.get("most_recent_employer"),
            "citizenship_statement": " and ".join(f"{DEMONYMS.get(c, c)} citizen" for c in auth["citizenships"]) or None,
            "eu_citizen": any(c in EU_MEMBERS for c in auth["citizenships"]) if auth["citizenships"] else None,
            "availability_text": (f"{avail['duration_weeks']} weeks ({avail['earliest_start']} to {avail['latest_end']})"
                                  if avail.get("duration_weeks") and avail.get("earliest_start") and avail.get("latest_end")
                                  else None),
            "currently_employed_here": any(company in normalize(cur) or normalize(cur) in company
                                           for cur in hist.get("currently_employed_at", [])) if company else None,
            "government_employee_or_official": hist.get("government_employee_or_official"),
            "outside_business_activities": hist.get("outside_business_activities"),
            "school": edu["school"], "degree": edu["degree"], "degree_level": edu["degree_level"], "major": edu["major"],
            "gpa": None if edu.get("gpa") in (None, "do_not_disclose") else str(edu["gpa"]),
            "grad_year": end[:4] if end else None, "grad_month": month,
            "grad_date": f"{month} {end[:4]}" if end else None,
            "school_start": edu.get("start"), "currently_enrolled": edu.get("currently_enrolled"),
            "school_start_year": (edu.get("start") or "")[:4] or None,
            "school_start_month": MONTH_NAMES[int(edu["start"][5:7]) - 1] if edu.get("start") else None,
            "coop_program": edu.get("coop_program"),
            "class_standing": edu.get("class_standing_fall_2026"),
            "high_school_grad_year": edu.get("high_school_grad_year"),
            # The visa the applicant would need for THIS posting: the US one on US postings, none at all in the
            # country of citizenship, unknown elsewhere (so the question stops for a human instead of guessing).
            "visa_type_needed": (auth["by_country"].get("US", {}).get("visa_type_needed") if self.ctx.country == "US"
                                 else "None" if self.ctx.country == "CA" else None),
            "export_license_required": auth["by_country"].get("US", {}).get("export_license_required"),
            "may_contact_current_employer": p["consents"].get("may_contact_current_employer"),
            "can_perform_essential_functions": avail.get("can_perform_essential_functions"),
            "french_proficient": p["identity"].get("french_proficient"),
            "attends_university_in_canada": ("canada" in (edu.get("location") or "").lower()) if edu.get("location") else None,
            # "Confirm RECEIPT of the privacy notice and arbitration agreement" — confirmed for NAMED employers only, on the
            # user's explicit instruction. Never generalizes: arbitration terms elsewhere still stop for the user.
            "arbitration_receipt_confirmed": True if company and any(
                normalize(c) in company for c in p["consents"].get("arbitration_receipt_companies", [])) else None,
            # "I understand that <employer> may use AI tools in its hiring process" — acknowledged for NAMED employers only.
            "employer_ai_use_acknowledged": True if company and any(
                normalize(c) in company for c in p["consents"].get("employer_ai_use_ack_companies", [])) else None,
            # The user's promise to follow an employer's interview-conduct policy (e.g. no unauthorized AI help in
            # interviews) — given for NAMED employers only.
            "interview_policy_acknowledged": True if company and any(
                normalize(c) in company for c in p["consents"].get("interview_policy_ack_companies", [])) else None,
            # A consent the user gave for NAMED employers only (investigation authorization + liability release):
            # elsewhere the same wording still pauses for the user.
            "investigation_release_signature": ident["full_name"] if company and any(
                normalize(c) in company for c in p["consents"].get("investigation_release_companies", [])) else None,
            "conflict_of_interest_any": hist.get("conflict_of_interest_any"),
            "government_official_ties": hist.get("government_official_ties"),
            "pending_criminal_charges": hist.get("pending_criminal_charges"),
            # for questions that ask about "convictions OR pending charges" in one breath; unknown if either is
            "criminal_conviction_or_charges": (
                None if None in (hist.get("criminal_conviction"), hist.get("pending_criminal_charges"))
                else bool(hist.get("criminal_conviction") or hist.get("pending_criminal_charges"))
            ),
            "has_outstanding_offers": hist.get("outstanding_offers_or_deadlines"),
            "finance_licences": hist.get("finance_licences"),
            "military_service": hist.get("military_service"),
            "earliest_start": avail.get("earliest_start"), "latest_end": avail.get("latest_end"),
            "duration_weeks": str(avail["duration_weeks"]) if avail.get("duration_weeks") else None,
            "willing_to_relocate": avail.get("willing_to_relocate"), "full_time_available": avail.get("full_time"),
            "travel_any_amount": (avail.get("travel_willingness") == "any") if avail.get("travel_willingness") else None,
            "any_posted_pay_ok": avail.get("any_posted_pay_ok"),
            "any_office_location_ok": avail.get("any_office_location_ok"),
            "interested_in_full_time_after": avail.get("interested_in_full_time_after"),
            "further_education_planned": avail.get("further_education_planned"),
            "citizenship": ", ".join(auth["citizenships"]),
            "us_citizen": "United States" in auth["citizenships"], "canadian_citizen": "Canada" in auth["citizenships"],
            "us_person_itar": auth.get("us_person_itar"), "work_auth_note": auth["by_country"]["US"].get("note"),
            "has_security_clearance": auth.get("security_clearance") not in (None, "none"),
            "criminal_conviction": hist.get("criminal_conviction"),
            "non_compete": hist.get("non_compete_or_restrictive_agreement"),
            "relatives_at_company": hist.get("relatives_at_company"),
            "previously_applied": hist.get("previously_applied_default"),
            "previously_employed_here": worked_here,
            "how_did_you_hear": p["defaults"].get("how_did_you_hear"),
            "salary_expectation": p["defaults"].get("salary_expectation"),
            "certify_truthful": True if p["consents"].get("agent_may_certify_truthfulness") else None,
            "acknowledge_privacy_notice": True if p["consents"].get("agent_may_acknowledge_privacy_notice") else None,
            "sms_opt_in": p["consents"].get("sms_marketing_opt_in"),
            "talent_community_opt_in": p["consents"].get("talent_community_opt_in"),
            "sms_updates_opt_in": p["consents"].get("sms_updates_opt_in", False),  # text-message updates: off unless the user opts in
            "today": date.today().isoformat(),
        }  # fmt: skip


EEO_KEYS = ("gender", "race_ethnicity", "hispanic_latino", "veteran_status", "disability", "lgbtq")
# Minimal profile used only to enumerate fact keys, so validation can never drift from Facts._simple.
_PROBE_PROFILE = {
    "identity": dict.fromkeys(("first_name", "last_name", "full_name", "preferred_name", "email", "phone_display",
                               "phone_e164", "phone_national"), ""),
    "links": {"linkedin": "", "github": "", "website": ""}, "address": {},
    "education": [{"school": "", "degree": "", "degree_level": "", "major": "", "end": "2030-04"}],
    "availability": {}, "history": {}, "defaults": {}, "consents": {}, "eeo": {},
    "work_authorization": {"citizenships": [], "by_country": {"US": {}}, "default": {}},
}  # fmt: skip


def fact_keys() -> set[str]:
    simple = Facts(_PROBE_PROFILE, JobContext("", ["CA"]))._simple()
    return set(simple) | COUNTRY_PARAM_FACTS | {f"eeo_{k}" for k in EEO_KEYS}


# --- resolutions -------------------------------------------------------------------------------
#
# {"kind": "fact_text",   "fact": "linkedin"}                      fill the fact's text value
# {"kind": "fact_option", "fact": "requires_sponsorship",          choose the option whose mapped value
#                         "options": {"Yes": true, "No": false}}   equals the fact (country-aware)
# {"kind": "fact_checkbox","fact": "certify_truthful"}              lone checkbox: ticked iff the fact is true
# {"kind": "decline",     "option": "I don't wish to answer"}      EEO: always this option
# {"kind": "literal",     "value": "…"}                            fixed text, or an option label
# {"kind": "skip"}                                                 leave an optional field blank
# {"kind": "per_job"}                                              essay written per job from the JD
# {"kind": "human"}                                                never automated

# {"kind": "skip_job",    "why": "requires a GPA"}                  user policy: if this is REQUIRED, skip the job
KINDS = {"fact_text", "fact_option", "fact_checkbox", "decline", "literal", "skip", "skip_job", "per_job", "human"}


def validate_resolution(question: Question, res: dict) -> list[str]:
    """Reject malformed or unsafe proposals (from an LLM or a human) before they reach the bank."""
    errors = []
    kind = res.get("kind")
    if kind not in KINDS:
        return [f"kind must be one of {sorted(KINDS)}"]
    options = {normalize(o): o for o in question.options}
    # A human-only question can be automated ONLY as an explicit, user-directed exception: the resolution must say
    # so ("user_override"), it still needs the user's clearance, and its fact should be scoped to named employers.
    if HUMAN_ONLY_RE.search(question.label) and kind != "human" and res.get("user_override") is not True:
        errors.append("this question is human-only (ID numbers / AI-use attestations / arbitration)")
    if kind in ("fact_text", "fact_option", "fact_checkbox") and res.get("fact") not in fact_keys():
        errors.append(f"unknown fact {res.get('fact')!r}")
    if kind == "fact_option":
        mapping = res.get("options") or {}
        if not mapping:
            errors.append("fact_option needs an options map")
        for label in mapping:
            if normalize(label) not in options:
                errors.append(f"option {label!r} is not on the form")
        if len({json.dumps(v) for v in mapping.values()}) != len(mapping):
            errors.append("two options map to the same fact value")
    if kind in ("decline", "literal") and question.options:
        chosen = res.get("option") if kind == "decline" else res.get("value")
        for item in chosen if isinstance(chosen, list) else [chosen]:
            if normalize(str(item)) not in options:
                errors.append(f"{item!r} is not one of the form's options")
        if isinstance(chosen, list) and question.type != "multiselect":
            errors.append("a list value is only valid for a multiselect")
    if kind == "fact_checkbox" and (question.type != "checkbox" or question.options):
        errors.append("fact_checkbox is only for a lone checkbox")
    if kind == "fact_text" and question.options:
        errors.append("fact_text cannot answer a choice question; use fact_option")
    if kind == "skip" and question.required:
        errors.append("cannot skip a required question")
    if kind == "literal" and is_high_stakes(question) and not question.options and len(str(res.get("value", ""))) > 200:
        errors.append("long literal on a high-stakes question")
    return errors


def assertion_sentence(question: Question, res: dict) -> str:
    """What the user is approving, phrased as the claim the form will make on their behalf."""
    kind = res["kind"]
    if kind == "fact_option":
        pairs = "; ".join(f"“{label}” ⇔ {res['fact']} = {json.dumps(value)}" for label, value in res["options"].items())
        try:
            used = effective_fact(question.label, res["fact"])
        except Ambiguous:
            used = "AMBIGUOUS (names several countries → always sent to the human)"
        if used != res["fact"]:
            scope = f" [evaluated as {used}: the question names that country]"
        else:
            scope = " (evaluated per job country)" if res["fact"] in COUNTRY_PARAM_FACTS else ""
        return f"“{question.label}” → {pairs}{scope}"
    if kind == "fact_checkbox":
        return f"“{question.label}” → ticked only while {res['fact']} = true"
    if kind == "fact_text":
        return f"“{question.label}” → filled with your {res['fact']}"
    if kind == "decline":
        return f"“{question.label}” → always “{res['option']}”"
    if kind == "literal":
        return f"“{question.label}” → always “{res['value']}”"
    return f"“{question.label}” → {kind}"


# --- bank --------------------------------------------------------------------------------------


def bank_put(conn: sqlite3.Connection, question: Question, res: dict, origin: str, approved: bool) -> None:
    errors = validate_resolution(question, res)
    if errors:
        raise ValueError("; ".join(errors))
    conn.execute(
        "INSERT OR REPLACE INTO answer_bank (qhash, label, field_type, options, high_stakes, resolution, approved,"
        " origin, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (question.qhash, question.template, question.type, json.dumps(question.options), tier(question),
         json.dumps(res), int(approved), origin, int(time.time())),
    )  # fmt: skip
    conn.execute("DELETE FROM questions WHERE qhash = ?", (question.qhash,))


def bank_get(conn: sqlite3.Connection, question: Question) -> tuple[dict, bool] | None:
    row = conn.execute("SELECT resolution, approved FROM answer_bank WHERE qhash = ?", (question.qhash,)).fetchone()
    return (json.loads(row["resolution"]), bool(row["approved"])) if row else None


def record_miss(conn: sqlite3.Connection, question: Question, job_id: int, needs: str = "llm") -> None:
    conn.execute("INSERT OR IGNORE INTO job_questions (job_id, qhash) VALUES (?, ?)", (job_id, question.qhash))
    if conn.execute("SELECT 1 FROM answer_bank WHERE qhash = ?", (question.qhash,)).fetchone():
        return  # already answered, only awaiting clearance — not an unanswered question
    conn.execute(
        "INSERT INTO questions (qhash, label, field_type, options, high_stakes, needs, example_job_id, created_at)"
        " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(qhash) DO UPDATE SET job_count = job_count + 1",
        (question.qhash, question.template, question.type, json.dumps(question.options), tier(question),
         needs, job_id, int(time.time())),
    )  # fmt: skip
    conn.execute("INSERT OR IGNORE INTO job_questions (job_id, qhash) VALUES (?, ?)", (job_id, question.qhash))


# --- resolve -----------------------------------------------------------------------------------


@dataclass
class Resolved:
    answers: dict[str, object] = field(default_factory=dict)  # question.id → text | option label | list | bool
    essays: list[Question] = field(default_factory=list)  # per-job free text still to be written
    misses: list[Question] = field(default_factory=list)  # not in the bank, or awaiting approval
    needs_input: list[tuple[Question, str]] = field(default_factory=list)  # (question, missing fact)
    human: list[tuple[Question, str]] = field(default_factory=list)
    skip_job: list[tuple[Question, str]] = field(default_factory=list)  # user policy says: don't apply to this one

    @property
    def ready(self) -> bool:
        return not (self.essays or self.misses or self.needs_input or self.human or self.skip_job)


def resolve(conn: sqlite3.Connection, questions: list[Question], profile: dict, ctx: JobContext,
            preset: dict[str, object] | None = None) -> Resolved:  # fmt: skip
    """preset: answers the ATS adapter already fixed by field id (first_name, resume, …)."""
    out = Resolved(answers=dict(preset or {}))
    facts = Facts(profile, ctx)
    for q in questions:
        if q.id in out.answers:
            continue
        if HUMAN_ONLY_RE.search(q.label):
            override = bank_get(conn, q)
            # only a CLEARED, explicitly user-directed entry may answer a human-only question
            if not (override and override[1] and override[0].get("user_override") is True):
                if q.required:
                    out.human.append((q, "human-only question"))
                continue
        if is_eeo(q) and all(profile["eeo"].get(k, DECLINE) == DECLINE for k in EEO_KEYS):
            option = eeo_decline_option(q)
            if option:
                out.answers[q.id] = option
                continue
            if not q.required:
                continue  # voluntary and no decline option offered → leave blank
        hit = bank_get(conn, q)
        # "human" and "skip" assert nothing on the user's behalf, so they need no clearance.
        asserts = hit is not None and hit[0]["kind"] not in ("human", "skip", "skip_job")
        if hit is None or (is_high_stakes(q) and asserts and not hit[1]):
            if q.required or hit is not None or is_high_stakes(q):
                out.misses.append(q)
            continue  # unknown optional low-stakes questions are left blank
        res = hit[0]
        try:
            answer = _apply(q, res, facts)
        except Unknown as missing:
            if q.required and str(missing) == "gpa" and profile["education"][0].get("gpa") == "do_not_disclose":
                out.skip_job.append((q, "form requires a GPA and the user does not disclose it"))
            elif q.required:
                out.needs_input.append((q, str(missing)))
            continue
        except Ambiguous:
            out.human.append((q, "job country unknown or mixed; work-authorization facts are per country"))
            continue
        if not q.required and res["kind"] in ("skip", "skip_job", "human", "per_job"):
            continue  # optional on THIS form → leave it blank instead of blocking the application
        if res["kind"] == "skip":
            out.human.append((q, "banked as skip but this form requires it"))
        elif res["kind"] == "skip_job":
            out.skip_job.append((q, res.get("why", "user policy")))
        elif res["kind"] == "per_job":
            out.essays.append(q)
        elif res["kind"] == "human":
            out.human.append((q, "marked human-only in the bank"))
        elif answer is not None:
            out.answers[q.id] = answer
    return out


# A question that NAMES a country is about that country, whatever the posting's location and whatever fact a
# proposer picked. "Are you authorized to work in the United States?" on a Toronto posting is still about the US.
# "US" must be upper-case to count (otherwise "tell us about…" would match); the spelled-out forms are case-blind.
NAMED_US_RE = re.compile(r"(?i:\bunited states\b|\bu\.s\b|\bu\.s\.a\b|\busa\b|\bamerica\b)|\bUS\b")
NAMED_CA_RE = re.compile(r"\bcanad(a|ian)\b", re.I)
NAMED_OTHER_RE = re.compile(
    r"\b(united kingdom|uk|u\.k|britain|british|england|english|ireland|irish|france|french|germany|german|"
    r"netherlands|dutch|spain|spanish|italy|italian|poland|polish|sweden|swedish|switzerland|swiss|europe|european|"
    r"eu|european union|schengen|india|indian|singapore|singaporean|hong kong|china|chinese|japan|japanese|korea|"
    r"korean|australia|australian|new zealand|brazil|brazilian|mexico|mexican|peru|argentina|israel|israeli|uae|"
    r"dubai|abu dhabi|serbia|serbian)\b",
    re.I,
)
_EXPLICIT = {"work_authorized": "{}_work_authorized", "requires_sponsorship": "{}_requires_sponsorship"}


def effective_fact(label: str, fact: str) -> str:
    """Swap a per-job-country fact for the named country's fact when the question names exactly one."""
    base = next((b for b in _EXPLICIT if fact == b or fact.endswith("_" + b)), None)
    if base is None:
        return fact
    named = [code for code, rx in (("us", NAMED_US_RE), ("ca", NAMED_CA_RE), ("intl", NAMED_OTHER_RE)) if rx.search(label)]
    if len(named) > 1:
        raise Ambiguous(f"question names several countries: {label[:80]}")
    return _EXPLICIT[base].format(named[0]) if named else fact


def _apply(q: Question, res: dict, facts: Facts):
    kind = res["kind"]
    if kind in ("skip", "skip_job", "per_job", "human"):
        return None
    if kind == "literal":
        return res["value"]
    if kind == "decline":
        return res["option"]
    value = facts.get(effective_fact(q.label, res["fact"]))
    if kind == "fact_text":
        return {True: "Yes", False: "No"}.get(value, str(value)) if isinstance(value, bool) else str(value)
    if kind == "fact_checkbox":
        if value is not True and q.required:
            raise Unknown(f"{res['fact']} is not true, so a required checkbox cannot be ticked")
        return value is True
    matches = [label for label, mapped in res["options"].items() if mapped == value]
    if len(matches) != 1:
        raise Unknown(f"{res['fact']}={value!r} has no matching option")
    return matches[0]
