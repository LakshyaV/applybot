import json
from pathlib import Path

import pytest

from applybot import db, filters
from applybot import models as m
from applybot.config import load_config
from applybot.normalize import canonical_url, countries_of, job_key, secondary_key
from applybot.sources import github_repo as gr

FIX = Path(__file__).parent / "fixtures"
CFG = load_config()
SEASON = "Summer 2027"


def read(name: str) -> str:
    return (FIX / name).read_text()


# --- normalize ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, key",
    [
        ("https://job-boards.greenhouse.io/doordashusa/jobs/8171041?utm_source=Simplify&amp;ref=Simplify",
         "greenhouse:doordashusa:8171041"),
        ("https://boards.greenhouse.io/figma/jobs/6143238004?gh_jid=6143238004", "greenhouse:figma:6143238004"),
        ("https://app.careerpuck.com/job-board/lyft/job/8767726002?gh_jid=8767726002",
         "greenhouse_embed:app.careerpuck.com:8767726002"),
        ("https://jobs.ashbyhq.com/mercor/de3025e5-10ca-4d55-b688-eff0e647ac8d/application",
         "ashby:mercor:de3025e5-10ca-4d55-b688-eff0e647ac8d"),
        ("https://jobs.lever.co/palantir/abc-123/apply", "lever:palantir:abc-123"),
        ("https://marmon.wd501.myworkdayjobs.com/Marmon_MSIP_Internships/job/Milwaukee-WI/Data-Engineering-Intern_JR0000037453",
         "workday:marmon/marmon_msip_internships:jr0000037453"),
        ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA/Intern_JR123/apply",
         "workday:nvidia/nvidiaexternalcareersite:jr123"),
        ("https://egup.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/20278933",
         "oracle:egup/cx:20278933"),
        ("https://qualcomm.eightfold.ai/careers/job/446721143440", "eightfold:qualcomm:446721143440"),
        ("https://careers.trccompanies.com/jobs/26840?icims=1", "icims:careers.trccompanies.com:26840"),
    ],
)  # fmt: skip
def test_job_key(url, key):
    assert job_key(canonical_url(url))[0] == key


def test_canonical_url_strips_tracking_but_keeps_job_params():
    url = canonical_url("https://Example.com/careers/?gh_jid=42&utm_source=Simplify&amp;ref=Simplify#apply")
    assert url == "https://example.com/careers?gh_jid=42"


@pytest.mark.parametrize(
    "locations, expected",
    [
        (["Toronto, ON"], ["CA"]),
        (["Milwaukee, WI"], ["US"]),
        (["SF"], ["US"]),
        (["MD-ANNAPOLIS"], ["US"]),
        (["Remote in Canada"], ["CA"]),
        (["Remote"], ["REMOTE"]),
        (["London, UK"], ["OTHER"]),
        (["Vancouver, BC", "Seattle, WA"], ["CA", "US"]),
        (["Singapore"], ["OTHER"]),  # bare country name, no comma
        (["London, ON"], ["CA"]),
        (["Hong Kong +2"], ["UNKNOWN"]),  # the other two locations are not named
        ([], ["UNKNOWN"]),
    ],
)
def test_countries(locations, expected):
    assert countries_of(locations) == expected


def test_secondary_key_ignores_corporate_suffixes_and_case():
    assert secondary_key("Interac Corp.", "SWE Intern", ["Toronto, ON"]) == secondary_key("interac", "swe intern", ["Toronto, ON"])


# --- adapters ----------------------------------------------------------------------------------


def test_simplify_listings_json():
    jobs = gr.from_listings_json(read("simplify_listings.json"), "github:x", "2027")
    statuses = [filters.eligibility(j, CFG)[0] for j in jobs]
    assert statuses.count(m.QUEUED) >= 3
    assert m.CLOSED in statuses and m.OFF_SEASON in statuses and m.SKIPPED_INELIGIBLE in statuses
    evergreen = next(j for j in jobs if len(j.terms) > 8)
    fresh = next(j for j in jobs if j.terms == [SEASON] and j.active)
    assert filters.priority(evergreen, CFG) > filters.priority(fresh, CFG) or evergreen.category != fresh.category
    assert all("utm_source" not in j.url and "&amp;" not in j.url for j in jobs)


def test_vansh_season_gets_year_from_repo_name():
    row = {"company_name": "IMC", "title": "Quant Research Intern", "url": "https://imc.com/j/1", "locations": ["Chicago"],
           "sponsorship": "Offers Sponsorship", "active": True, "season": "Spring/Summer", "is_visible": True}  # fmt: skip
    (job,) = gr.from_listings_json(json.dumps([row]), "github:v", "2027")
    assert job.terms == ["Spring 2027", "Summer 2027"] and job.sponsorship == m.SPONSOR_OFFERS
    assert job.category == "Quant" and filters.eligibility(job, CFG)[0] == m.QUEUED


def test_sponsorship_rules():
    base = {"company_name": "Acme", "url": "https://acme.com/j/1", "locations": ["Austin, TX"], "active": True,
            "terms": [SEASON], "is_visible": True, "degrees": ["Bachelor's"]}  # fmt: skip
    rows = [
        {**base, "title": "SWE Intern", "sponsorship": "U.S. Citizenship is Required"},
        {**base, "title": "SWE Intern II", "url": "https://acme.com/j/2", "sponsorship": "Does Not Offer Sponsorship"},
        {**base, "title": "SWE Intern III", "url": "https://acme.com/j/3", "sponsorship": "Other"},
    ]
    citizen, none, other = gr.from_listings_json(json.dumps(rows), "github:x", "2027")
    assert filters.eligibility(citizen, CFG)[0] == m.SKIPPED_INELIGIBLE
    assert filters.eligibility(none, CFG)[0] == m.QUEUED  # still applied to…
    assert filters.priority(none, CFG) > filters.priority(other, CFG)  # …but last


def test_markdown_table_vansh_flags_and_links():
    jobs = gr.from_tables(read("vansh.md"), "github:v", SEASON)
    assert jobs and jobs[0].company == "Vertiv" and jobs[0].ats == "oracle"
    assert jobs[0].title == "Product Management Intern" and jobs[0].sponsorship == m.SPONSOR_NONE
    assert all("utm_source" not in j.url for j in jobs)


def test_markdown_table_continuation_rows_and_badge_links():
    jobs = gr.from_tables(read("negar.md"), "github:n", SEASON)
    by_title = {j.title: j for j in jobs}
    assert by_title["Silicon Validation Intern"].company == "Qualcomm"  # ↳ carries the company forward
    assert by_title["Silicon Validation Intern"].ats == "eightfold"
    assert all("shields.io" not in j.url for j in jobs)
    assert by_title["Software Development Engineer Intern"].countries == ["CA"]
    assert by_title["Software Development Engineer Intern"].date_posted > 0


def test_markdown_table_with_salary_column_and_linked_company():
    jobs = gr.from_tables(read("speedy.md"), "github:s", SEASON)
    assert [j.company for j in jobs[:2]] == ["Microsoft", "DoorDash"]
    assert jobs[1].key == "greenhouse:doordashusa:8171041"
    assert "microsoft.com/careers" in jobs[0].url  # apply link, not the company homepage


def test_tracker_links_are_flagged_until_resolved():
    jobs = gr.from_tables(read("zapply.md"), "github:z", SEASON)
    assert jobs and all("zapply.jobs" in j.url for j in jobs)
    assert jobs[0].company == "RTX" and jobs[0].sponsorship == m.SPONSOR_OFFERS and jobs[0].countries == ["US"]


def test_html_table_simplify_readme():
    jobs = gr.from_tables(read("simplify_readme.html.md"), "github:s", SEASON)
    assert len(jobs) == 4
    assert jobs[3].company == jobs[2].company  # ↳ row
    assert all("simplify.jobs" not in j.url for j in jobs)


def test_quant_yaml():
    jobs = gr.from_firm_yaml(read("jane-street.yaml"), "github:nu", SEASON)
    titles = [j.title for j in jobs]
    assert len(jobs) == 5 and "Quantitative Researcher Intern (ML)" in titles
    assert all(j.company == "Jane Street" and j.category == "Quant" for j in jobs)


@pytest.mark.parametrize("title, status", [
    ("Software Engineer Intern - Fall 2026", m.OFF_SEASON),
    ("Software Engineer Intern, Summer 2027", m.QUEUED),
    ("ML Research Intern (PhD)", m.SKIPPED_INELIGIBLE),
    ("Software Intern (Bachelor's/Master's)", m.QUEUED),
])  # fmt: skip
def test_title_rules(title, status):
    job = gr.build_job(company="A", title=title, raw_url="https://a.com/1", locations=["Toronto, ON"], source="t",
                       terms=[SEASON], season_inferred=True)  # fmt: skip
    assert filters.eligibility(job, CFG)[0] == status


def test_blocked_hosts_go_to_human():
    job = gr.build_job(company="A", title="SWE Intern", raw_url="https://www.linkedin.com/jobs/view/1", locations=[],
                       source="t", terms=[SEASON])  # fmt: skip
    assert filters.eligibility(job, CFG)[0] == m.NEEDS_HUMAN


# --- tracker -----------------------------------------------------------------------------------


def make(n: int, **kw) -> m.Job:
    return gr.build_job(company=f"Co{n}", title="SWE Intern", raw_url=f"https://co{n}.com/jobs/{n}",
                        locations=["Toronto, ON"], source="t", terms=[SEASON], **kw)  # fmt: skip


def test_ingest_is_idempotent_and_dedupes(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    assert db.upsert_job(conn, make(1), m.QUEUED, "", 0) == "new"
    assert db.upsert_job(conn, make(1), m.QUEUED, "", 0) == "seen"
    twin = make(1)
    twin.key = "other:key"  # same company/title/location reached through a different URL
    assert db.upsert_job(conn, twin, m.QUEUED, "", 0) == "duplicate"
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE status = ?", (m.QUEUED,)).fetchone()[0] == 1


def test_claim_is_exclusive_ordered_and_leases_expire(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    for n, pri in [(1, 10), (2, 0), (3, 5)]:
        db.upsert_job(conn, make(n), m.QUEUED, "", pri)
    first = db.claim(conn, 2, "w1")
    assert [r["company"] for r in first] == ["Co2", "Co3"]
    assert [r["company"] for r in db.claim(conn, 5, "w2")] == ["Co1"]
    assert db.claim(conn, 5, "w3") == []
    conn.execute("UPDATE jobs SET lease_until = 1 WHERE company = 'Co2'")
    assert db.recover(conn)["lease_expired"] == 1
    assert [r["company"] for r in db.claim(conn, 5, "w4")] == ["Co2"]


def test_interrupted_submit_is_never_requeued(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_job(conn, make(1), m.QUEUED, "", 0)
    (job,) = db.claim(conn, 1, "w1")
    app_id = conn.execute("INSERT INTO applications (job_id, mode, lane, started_at) VALUES (?, 'auto', 'gh', 0)",
                          (job["id"],)).lastrowid  # fmt: skip
    db.begin_submit(conn, job["id"], app_id)
    conn.execute("UPDATE jobs SET lease_until = 1")  # simulate a crash + long downtime
    assert db.recover(conn) == {"to_verify": 1, "lease_expired": 0}
    assert db.claim(conn, 5, "w2") == []
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == m.VERIFY


def test_upstream_close_withdraws_untouched_jobs_only(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_job(conn, make(1), m.QUEUED, "", 0)
    db.upsert_job(conn, make(2), m.QUEUED, "", 0)
    db.set_status(conn, 2, m.SUBMITTED)
    assert db.upsert_job(conn, make(1), m.CLOSED, "closed upstream", 0) == "closed"
    assert db.upsert_job(conn, make(2), m.CLOSED, "closed upstream", 0) == "seen"
    assert conn.execute("SELECT status FROM jobs WHERE id = 2").fetchone()[0] == m.SUBMITTED


@pytest.mark.parametrize("company, excluded", [
    ("Stripe", True), ("stripe", True), ("Amazon", True), ("Amazon Web Services", True), ("Amazon Robotics", True),
    ("Google", True), ("Google DeepMind", True), ("Microsoft", True), ("Microsoft Research", True),
    ("Amazonia Labs", False), ("Stripes & Co", False), ("Googleplex Tours", False), ("Robinhood", False),
])  # fmt: skip
def test_user_excluded_companies(company, excluded):
    cfg = {**CFG, "exclude_companies": ["Stripe", "Google", "Microsoft", "Amazon"]}
    assert filters.is_excluded_company(company, cfg) is excluded
    job = gr.build_job(company=company, title="SWE Intern", raw_url="https://x.com/1", locations=["Toronto, ON"],
                       source="t", terms=[SEASON])  # fmt: skip
    assert (filters.eligibility(job, cfg)[0] == m.ALREADY_APPLIED) is excluded
