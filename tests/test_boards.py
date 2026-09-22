from __future__ import annotations

import httpx
import pytest

from applybot import filters
from applybot import models as m
from applybot.config import load_config
from applybot.sources import boards

CFG = load_config()
TARGET = "Summer 2027"


@pytest.mark.parametrize(
    "title, description, terms, inferred",
    [
        ("Software Engineer Intern (Summer 2027)", "", ["Summer 2027"], False),
        ("Software Engineer Intern, Winter 2027", "also mentions Summer 2027", ["Winter 2027"], False),  # title wins
        ("2026 Software Engineering Intern", "", ["2026"], False),  # bare year in the title
        ("2027 Software Engineering Intern", "", [], True),
        ("Software Engineer Intern", "Our Summer 2027 program runs 12 weeks", ["Summer 2027"], False),
        ("Software Engineer Intern", "Join us for Fall 2026.", ["Fall 2026"], False),
        ("Software Engineer Intern", "For students graduating by Spring 2028", [], True),  # says nothing about the term
        ("Machine Learning Intern", "", [], True),
    ],
)
def test_season_terms(title, description, terms, inferred):
    assert boards.season_terms(title, description, TARGET) == (terms, inferred)


@pytest.mark.parametrize(
    "title, wanted",
    [
        ("Software Engineer Intern", True),
        ("Machine Learning Research Intern", True),
        ("Quantitative Trader Intern", True),
        ("Engineering Intern", True),  # no discipline named: startups title software roles this way
        ("Member of Technical Staff Intern", True),
        ("Software Engineer Intern - Hardware Test", True),  # a software word outranks the other discipline
        ("Civil Engineering Intern", False),
        ("Mechanical Engineering Intern (Robotics)", False),
        ("Water Resources Engineering Intern", False),
        ("Electrical Engineer Co-op", False),
        ("Senior Software Engineer", False),  # not an internship
        ("SWE Fellow - Human Frontier Collective (Canada)", False),  # gig work, not an internship
    ],
)
def test_board_title_filter(title, wanted):
    assert boards.is_target_title(title) is wanted


def test_off_season_board_rows_are_not_queued():
    terms, inferred = boards.season_terms("2026 Software Engineering Intern", "", TARGET)
    job = m.Job(company="Acme", title="2026 Software Engineering Intern", url="https://x.test/1", terms=terms,
                season_inferred=inferred, countries=["US"])  # fmt: skip
    assert filters.eligibility(job, CFG)[0] == m.OFF_SEASON


def test_greenhouse_board_is_filtered_to_intern_rows_and_keyed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": [
                {"id": 11, "title": "Software Engineer Intern", "location": {"name": "Toronto, ON"}, "company_name": "Acme",
                 "first_published": "2026-09-01T10:00:00-04:00"},
                {"id": 12, "title": "Senior Software Engineer", "location": {"name": "Toronto, ON"}},
            ]})  # fmt: skip
        return httpx.Response(200, json={"content": "&lt;p&gt;Summer 2027 internship&lt;/p&gt;"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        jobs = boards._greenhouse(client, "acme", None, TARGET, "board:greenhouse:acme")
    assert [(j.company, j.key, j.terms, j.countries) for j in jobs] == [("Acme", "greenhouse:acme:11", ["Summer 2027"], ["CA"])]


def test_missing_board_returns_none():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404))) as client:
        assert boards._ashby(client, "nope", None, TARGET, "s") is None
        assert boards._lever(client, "nope", None, TARGET, "s") is None


def test_company_bonus_takes_the_best_tier():
    cfg = {"priority_companies": ["Acme"], "priority_company_bonus": 40,
           "company_tiers": [{"bonus": 30, "companies": ["Beta Labs"]}, {"bonus": 10, "companies": ["Acme", "Gamma"]}]}  # fmt: skip
    assert filters.company_bonus("Acme", cfg) == 40
    assert filters.company_bonus("Beta Labs Inc", cfg) == 30
    assert filters.company_bonus("Gamma", cfg) == 10
    assert filters.company_bonus("Betamax", cfg) == 0


def test_curated_file_parses():
    curated = boards.curated_boards()
    assert curated[("greenhouse", "anthropic")] == "Anthropic"
    assert all(ats in boards.POLLED for ats, _ in curated)


def test_dated_non_summer_term_in_title_is_off_season():
    job = m.Job(company="Acme", title="Software Engineering Intern - Vehicle Controls (January - August 2027)",
                url="https://x.test/2", terms=["Summer 2027"], countries=["US"])  # fmt: skip
    assert filters.eligibility(job, CFG)[0] == m.OFF_SEASON
    summer = m.Job(company="Acme", title="Software Engineering Intern (May - August 2027)", url="https://x.test/3",
                   terms=["Summer 2027"], countries=["US"])  # fmt: skip
    assert filters.eligibility(summer, CFG)[0] == m.QUEUED
