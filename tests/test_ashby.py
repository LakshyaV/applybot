from __future__ import annotations

import json
from pathlib import Path

from applybot.forms import ashby

FIXTURE = json.load(open(Path(__file__).parent / "fixtures" / "ashby_posting.json"))["data"]["jobPosting"]


def test_parse_types_options_and_required():
    questions, meta = ashby.parse(FIXTURE)
    by_id = {q.id: q for q in questions}
    assert by_id["_systemfield_name"].type == "text" and by_id["_systemfield_name"].required
    assert by_id["_systemfield_resume"].type == "file"
    assert by_id["a05e892e-4a9f-4431-b491-013d6a3f804a"].options[:2] == ["He/Him", "She/Her"]
    boolean = by_id["30fcdc85-13b8-43a4-9fb4-0ea387642215"]
    assert boolean.type == "select" and boolean.options == ["Yes", "No"]
    assert by_id["2ff3be8b-ab56-445f-ad6c-8753234d2a91"].type == "multiselect"
    assert by_id["1c25bf6e-4a26-4d56-a5d0-89cd3b75d28a"].type == "textarea"
    assert by_id["_systemfield_eeoc_gender"].section == "eeo" and not by_id["_systemfield_eeoc_gender"].required
    assert meta["countries"] == ["US"] and meta["employment_type"] == "Intern"


def test_preset_answers_cover_system_fields():
    class Facts:
        def get(self, key):
            return {"full_name": "A B", "email": "a@b.c", "phone": "+1 555", "location_full": "X, Y, Z"}[key]

    questions, _ = ashby.parse(FIXTURE)
    preset = ashby.preset_answers(questions, Facts(), "/r.pdf")
    assert preset["_systemfield_name"] == "A B" and preset["_systemfield_resume"] == "/r.pdf"
    assert preset["d58506bf-393d-4769-a6ec-bdebe07843ed"] == "+1 555"  # the phone field has a random path
    assert preset["_systemfield_location"] == "X, Y, Z"


def test_gone_when_posting_missing():
    import httpx
    import pytest

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"jobPosting": None}, "errors": [{"message": "not found"}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ashby.Gone):
            ashby.fetch("acme", "nope", client)
