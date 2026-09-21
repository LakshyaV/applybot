"""Safety invariants of the answer engine. If one of these fails, do not submit anything."""

import copy
from pathlib import Path

import pytest

from applybot import db
from applybot.config import load_profile
from applybot.resolve import (
    JobContext, Question, assertion_sentence, bank_put, fact_keys, is_high_stakes, record_miss, resolve,
    validate_resolution,
)  # fmt: skip

PROFILE = load_profile(Path(__file__).parent / "fixtures" / "profile.md")
US, CA = JobContext("Acme", ["US"]), JobContext("Acme", ["CA"])

SPONSOR = Question("q1", "Will you now or in the future require sponsorship for employment visa status?",
                   "select", True, ["Yes", "No"])  # fmt: skip
SPONSOR_NEGATED = Question("q2", "I will NOT require visa sponsorship now or in the future", "select", True,
                           ["True", "False"])  # fmt: skip
AUTHORIZED = Question("q3", "Are you legally authorized to work in the country where this job is located?",
                      "select", True, ["Yes", "No"])  # fmt: skip
LINKEDIN = Question("q4", "LinkedIn Profile", "text")


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


def test_high_stakes_lexicon_is_high_recall():
    for label in [SPONSOR.label, SPONSOR_NEGATED.label, AUTHORIZED.label, "Are you a U.S. Person under ITAR?",
                  "Have you ever been convicted of a felony?", "Are you bound by a non-compete?",
                  "I certify the above is true", "Expected graduation date", "Are you at least 18 years of age?",
                  "Have you previously been employed by Acme?", "Desired salary", "What is your GPA?"]:  # fmt: skip
        assert is_high_stakes(Question("x", label, "select")), label
    assert not is_high_stakes(LINKEDIN)
    assert is_high_stakes(Question("g", "Gender", "select", section="eeo"))


def test_polarity_lives_in_the_mapping_not_in_yes_no(conn):
    bank_put(conn, SPONSOR, {"kind": "fact_option", "fact": "requires_sponsorship",
                             "options": {"Yes": True, "No": False}}, "user", approved=True)  # fmt: skip
    bank_put(conn, SPONSOR_NEGATED, {"kind": "fact_option", "fact": "requires_sponsorship",
                                     "options": {"True": False, "False": True}}, "user", approved=True)  # fmt: skip
    us = resolve(conn, [SPONSOR, SPONSOR_NEGATED], PROFILE, US)
    assert us.ready and us.answers == {"q1": "Yes", "q2": "False"}
    ca = resolve(conn, [SPONSOR, SPONSOR_NEGATED], PROFILE, CA)
    assert ca.answers == {"q1": "No", "q2": "True"}


def test_same_question_different_truth_per_country(conn):
    bank_put(conn, AUTHORIZED, {"kind": "fact_option", "fact": "work_authorized",
                                "options": {"Yes": True, "No": False}}, "user", approved=True)  # fmt: skip
    assert resolve(conn, [AUTHORIZED], PROFILE, CA).answers == {"q3": "Yes"}
    assert resolve(conn, [AUTHORIZED], PROFILE, US).answers == {"q3": "No"}
    assert resolve(conn, [AUTHORIZED], PROFILE, JobContext("Acme", ["OTHER"])).answers == {"q3": "No"}


@pytest.mark.parametrize("countries", [["CA", "US"], ["UNKNOWN"], ["REMOTE"], []])
def test_ambiguous_country_never_answers_work_authorization(conn, countries):
    bank_put(conn, AUTHORIZED, {"kind": "fact_option", "fact": "work_authorized",
                                "options": {"Yes": True, "No": False}}, "user", approved=True)  # fmt: skip
    out = resolve(conn, [AUTHORIZED], PROFILE, JobContext("Acme", countries))
    assert not out.answers and out.human and not out.ready


def test_unapproved_high_stakes_entry_is_a_miss(conn):
    bank_put(conn, SPONSOR, {"kind": "fact_option", "fact": "requires_sponsorship",
                             "options": {"Yes": True, "No": False}}, "llm", approved=False)  # fmt: skip
    out = resolve(conn, [SPONSOR], PROFILE, US)
    assert out.answers == {} and out.misses == [SPONSOR]


def test_unapproved_low_stakes_entry_is_usable(conn):
    bank_put(conn, LINKEDIN, {"kind": "fact_text", "fact": "linkedin"}, "llm", approved=False)
    assert resolve(conn, [LINKEDIN], PROFILE, US).answers == {"q4": "https://www.linkedin.com/in/janedoe"}


def test_changed_wording_or_option_set_is_a_miss_not_a_near_match(conn):
    bank_put(conn, SPONSOR, {"kind": "fact_option", "fact": "requires_sponsorship",
                             "options": {"Yes": True, "No": False}}, "user", approved=True)  # fmt: skip
    reworded = Question("q", SPONSOR.label.replace("require", "not require"), "select", True, ["Yes", "No"])
    more_options = Question("q", SPONSOR.label, "select", True, ["Yes", "No", "Unsure"])
    out = resolve(conn, [reworded, more_options], PROFILE, US)
    assert out.answers == {} and len(out.misses) == 2


def test_cosmetic_differences_still_hit(conn):
    bank_put(conn, SPONSOR, {"kind": "fact_option", "fact": "requires_sponsorship",
                             "options": {"Yes": True, "No": False}}, "user", approved=True)  # fmt: skip
    cosmetic = Question("z", "  Will you now or in the future require sponsorship for employment visa status? *",
                        "select", True, ["No", "Yes"])  # fmt: skip
    assert resolve(conn, [cosmetic], PROFILE, US).answers == {"z": "Yes"}


def test_null_fact_is_needs_input_when_required_and_blank_when_optional(conn):
    gpa_req = Question("g1", "Cumulative GPA", "text", True)
    gpa_opt = Question("g2", "Cumulative GPA (optional)", "text", False)
    for q in (gpa_req, gpa_opt):
        bank_put(conn, q, {"kind": "fact_text", "fact": "gpa"}, "user", approved=True)
    out = resolve(conn, [gpa_req, gpa_opt], PROFILE, US)
    assert out.answers == {} and [(q.id, fact) for q, fact in out.needs_input] == [("g1", "gpa")]


def test_unknown_optional_low_stakes_is_left_blank_but_required_is_a_miss(conn):
    optional = Question("o", "Anything else you'd like to share?", "textarea", False)
    required = Question("r", "Favourite programming language", "text", True)
    out = resolve(conn, [optional, required], PROFILE, US)
    assert out.misses == [required]


def test_human_only_questions_are_never_automated(conn):
    for label in ["Social Security Number", "Date of Birth", "Did you use AI tools or ChatGPT to complete this?",
                  "I completed this application myself without the use of generative AI"]:  # fmt: skip
        q = Question("h", label, "text", True)
        assert validate_resolution(q, {"kind": "literal", "value": "x"})
        out = resolve(conn, [q], PROFILE, US)
        assert out.human and not out.answers


def test_validation_rejects_unsafe_proposals():
    assert validate_resolution(SPONSOR, {"kind": "fact_option", "fact": "requires_sponsorship",
                                         "options": {"Yes": True, "Nope": False}})  # option not on the form
    assert validate_resolution(SPONSOR, {"kind": "fact_option", "fact": "requires_sponsorship",
                                         "options": {"Yes": True, "No": True}})  # both options → same value
    assert validate_resolution(SPONSOR, {"kind": "fact_option", "fact": "made_up", "options": {"Yes": True}})
    assert validate_resolution(SPONSOR, {"kind": "fact_text", "fact": "linkedin"})  # text on a choice question
    assert validate_resolution(SPONSOR, {"kind": "skip"})  # required
    assert validate_resolution(SPONSOR, {"kind": "literal", "value": "Maybe"})
    assert validate_resolution(SPONSOR, {"kind": "guess"})
    assert not validate_resolution(SPONSOR, {"kind": "fact_option", "fact": "requires_sponsorship",
                                             "options": {"Yes": True, "No": False}})  # fmt: skip


def test_eeo_declines_with_the_forms_own_wording(conn):
    gender = Question("e", "Gender", "select", False, ["Male", "Female", "Decline To Self Identify"], section="eeo")
    bank_put(conn, gender, {"kind": "decline", "option": "Decline To Self Identify"}, "user", approved=True)
    assert resolve(conn, [gender], PROFILE, US).answers == {"e": "Decline To Self Identify"}


def test_previous_employer_is_computed_per_company(conn):
    q = Question("p", "Have you previously been employed by this company?", "select", True, ["Yes", "No"])
    bank_put(conn, q, {"kind": "fact_option", "fact": "previously_employed_here",
                       "options": {"Yes": True, "No": False}}, "user", approved=True)  # fmt: skip
    assert resolve(conn, [q], PROFILE, JobContext("Initech Technologies", ["US"])).answers == {"p": "Yes"}
    assert resolve(conn, [q], PROFILE, JobContext("Stripe", ["US"])).answers == {"p": "No"}


def test_certification_checkbox_follows_consent(conn):
    box = Question("c", "I certify that the information provided is true and complete", "checkbox", True)
    assert validate_resolution(LINKEDIN, {"kind": "fact_checkbox", "fact": "certify_truthful"})  # not a checkbox
    bank_put(conn, box, {"kind": "fact_checkbox", "fact": "certify_truthful"}, "user", approved=True)
    assert resolve(conn, [box], PROFILE, US).answers == {"c": True}
    no_consent = copy.deepcopy(PROFILE)
    no_consent["consents"]["agent_may_certify_truthfulness"] = False
    out = resolve(conn, [box], no_consent, US)
    assert out.answers == {} and out.needs_input


def test_misses_are_deduped_across_jobs(conn):
    for job_id in (1, 2, 3):
        record_miss(conn, SPONSOR, job_id)
    row = conn.execute("SELECT job_count, high_stakes FROM questions").fetchone()
    assert (row["job_count"], row["high_stakes"]) == (3, 1)
    assert conn.execute("SELECT COUNT(*) FROM job_questions").fetchone()[0] == 3


def test_assertion_sentence_states_the_claim():
    sentence = assertion_sentence(SPONSOR_NEGATED, {"kind": "fact_option", "fact": "requires_sponsorship",
                                                    "options": {"True": False, "False": True}})  # fmt: skip
    assert "“False” ⇔ requires_sponsorship = true" in sentence and "per job country" in sentence


def test_fact_registry_covers_profile_driven_keys():
    assert {"work_authorized", "requires_sponsorship", "eeo_gender", "linkedin", "grad_date", "over_18"} <= fact_keys()
