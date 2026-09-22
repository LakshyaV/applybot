"""Ashby application forms (jobs.ashbyhq.com/<board>/<posting>/application).

Schema: the same public GraphQL query Ashby's own page issues (`ApiJobPosting`) returns every field entry with
its type, options and requiredness — so, as with Greenhouse, the form is known before the browser opens.
Filling and submitting happen in the real browser. Ashby scores every submit with invisible reCAPTCHA v3;
a rejection is reported as a challenge for the human lane, never worked around.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import httpx

from ..normalize import countries_of
from ..resolve import Question
from .greenhouse import CHALLENGE, CONFIRMED, FillError, INVALID, UNKNOWN

GRAPHQL = "https://jobs.ashbyhq.com/api/non-user-graphql"
QUERY = (Path(__file__).with_name("ashby_posting.graphql")).read_text()
HEADERS = {"content-type": "application/json", "apollographql-client-name": "frontend_non_user",
           "User-Agent": "applybot (personal internship tracker)"}  # fmt: skip

# Ashby field type → the resolver's field type. Anything else (e.g. Number, SocialLink) is text.
FIELD_TYPES = {
    "String": "text", "Email": "text", "Phone": "text", "LongText": "textarea", "File": "file", "Date": "text",
    "Location": "text", "ValueSelect": "select", "MultiValueSelect": "multiselect", "Boolean": "select",
    "Number": "text", "SocialLink": "text", "Url": "text",
}  # fmt: skip
# System fields answered straight from the profile (identified by their stable path, not by label).
PRESET_FACTS = {"_systemfield_name": "full_name", "_systemfield_email": "email"}
# Most entries carry data-field-path; radio/checkbox groups are <fieldset>s whose title label points at the path.
ENTRY = (".ashby-application-form-field-entry[data-field-path=\"{path}\"], "
         "fieldset[class*=fieldEntry]:has(> label.ashby-application-form-question-title[for=\"{path}\"])")  # fmt: skip
ALL_ENTRIES = ".ashby-application-form-field-entry[data-field-path], fieldset[class*=fieldEntry]"


class Gone(Exception):
    """The posting no longer exists or is unlisted."""


def fetch(board: str, posting_id: str, client: httpx.Client | None = None) -> dict:
    client = client or httpx.Client(timeout=30, headers=HEADERS)
    body = {"operationName": "ApiJobPosting", "query": QUERY,
            "variables": {"organizationHostedJobsPageName": board, "jobPostingId": posting_id}}  # fmt: skip
    resp = client.post(GRAPHQL, params={"op": "ApiJobPosting"}, json=body)
    resp.raise_for_status()
    data = resp.json()
    posting = (data.get("data") or {}).get("jobPosting")
    if not posting:
        raise Gone(f"{board}/{posting_id}: {json.dumps(data.get('errors', ''))[:120]}")
    return posting


def _entries(form: dict | None, section: str = "") -> list[Question]:
    out = []
    for sec in (form or {}).get("sections") or []:
        if sec.get("isHidden"):
            continue
        for entry in sec.get("fieldEntries") or []:
            if entry.get("isHidden"):
                continue
            fld = entry["field"]
            ftype = fld.get("type", "String")
            options = [html.unescape(v.get("label", "")).strip() for v in fld.get("selectableValues") or []]
            if ftype == "Boolean":
                options = ["Yes", "No"]
            out.append(
                Question(
                    id=fld["path"], label=html.unescape(fld.get("title") or "").strip(),
                    type=FIELD_TYPES.get(ftype, "text"), required=bool(entry.get("isRequired")),
                    options=options, section=section or ("eeo" if fld["path"].startswith("_systemfield_eeoc") else ""),
                )  # fmt: skip
            )
    return out


def parse(posting: dict) -> tuple[list[Question], dict]:
    """→ (questions, meta). The EEO survey renders on the same page, so its fields are included, tagged 'eeo'."""
    questions = _entries(posting.get("applicationForm"))
    for survey in posting.get("surveyForms") or []:
        questions += _entries(survey, "eeo")
    locations = [posting.get("locationName") or "", *(posting.get("secondaryLocationNames") or [])]
    if (posting.get("workplaceType") or "").lower() == "remote":
        locations.append("Remote")
    content = html.unescape(posting.get("descriptionHtml") or "")
    meta = {
        "title": posting.get("title", ""), "location": locations[0], "countries": countries_of([l for l in locations if l]),
        "description": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", content)).strip(),
        "employment_type": posting.get("employmentType"), "application_limit": posting.get("applicationLimitCalloutHtml"),
        "automated_processing_notice": bool(posting.get("automatedProcessingLegalNotice")),
    }  # fmt: skip
    return questions, meta


def preset_answers(questions: list[Question], facts, resume_path: str) -> dict[str, object]:
    preset: dict[str, object] = {}
    for q in questions:
        if q.id in PRESET_FACTS:
            preset[q.id] = str(facts.get(PRESET_FACTS[q.id]))
        elif q.id == "_systemfield_resume":
            preset[q.id] = resume_path
        elif q.id == "_systemfield_location":
            preset[q.id] = str(facts.get("location_full"))
        elif q.label.strip().lower() == "phone" and q.type == "text":  # the phone field has a random path
            preset[q.id] = str(facts.get("phone"))
    return preset


# --- browser side ------------------------------------------------------------------------------


def open_form(page, board: str, posting_id: str) -> bool:
    try:
        page.goto(f"https://jobs.ashbyhq.com/{board}/{posting_id}/application", wait_until="domcontentloaded", timeout=45_000)
        page.locator(ENTRY.format(path="_systemfield_name")).wait_for(state="visible", timeout=20_000)
        page.wait_for_timeout(1_000)
        return True
    except Exception:  # noqa: BLE001
        return False


def _entry(page, path: str):
    return page.locator(ENTRY.format(path=path))


def _kind(entry) -> str:
    """How the entry is rendered: text | textarea | file | yesno | radio | checkbox | date | autocomplete | select."""
    if entry.locator("input.ashby-application-form-input-date").count():
        return "date"
    if entry.locator("input[type=file]").count():
        return "file"
    if entry.locator(".ashby-application-form-input-yesno").count():
        return "yesno"
    if entry.locator("input[type=radio]").count():
        return "radio"
    if entry.locator("input[type=checkbox]").count():
        return "checkbox"
    if entry.locator("input[role=combobox]").count():
        return "autocomplete"
    if entry.locator("textarea").count():
        return "textarea"
    if entry.locator("input[type=text], input[type=email], input[type=tel], input[type=url], input[type=number]").count():
        return "text"
    return "unknown"


def _held(entry, kind: str) -> object:
    if kind == "yesno":
        pressed = entry.locator("button[aria-pressed=true]")
        return pressed.first.get_attribute("data-option").capitalize() if pressed.count() else ""
    if kind == "radio":
        checked = entry.locator("input[type=radio]:checked")
        return entry.locator(f'label[for="{checked.first.get_attribute("id")}"]').inner_text().strip() if checked.count() else ""
    if kind == "checkbox":
        return sorted(b.get_attribute("name") or "" for b in entry.locator("input[type=checkbox]:checked").all())
    if kind == "file":
        # Ashby swaps the input for a filename chip once the upload is accepted
        text = entry.inner_text()
        m_ = re.search(r"[\w\-. ]+\.(pdf|docx?)", text, re.I)
        return m_.group(0).strip() if m_ else ""
    if kind == "autocomplete":
        return entry.locator("input[role=combobox]").input_value()
    if kind in ("text", "textarea", "date"):
        return entry.locator("input, textarea").first.input_value()
    return ""


def _apply(page, path: str, value: object, kind: str) -> None:
    entry = _entry(page, path)
    if kind == "file":
        entry.locator("input[type=file]").set_input_files(str(value))
        name = str(value).rsplit("/", 1)[-1]
        entry.get_by_text(name, exact=False).first.wait_for(state="visible", timeout=30_000)
    elif kind == "yesno":
        option = str(value).strip().lower()
        if option not in ("yes", "no"):
            raise FillError(f"{path}: a yes/no field cannot hold {value!r}")
        entry.locator(f'button[data-option="{option}"]').click()
        if _held(entry, kind).lower() != option:
            raise FillError(f"{path}: {option} did not commit")
    elif kind == "radio":
        wanted = str(value).strip()
        for radio in entry.locator("input[type=radio]").all():
            label = entry.locator(f'label[for="{radio.get_attribute("id")}"]').inner_text().strip()
            if label == wanted:
                radio.check(force=True)  # the native input sits under a styled label
                break
        else:
            raise FillError(f"{path}: option {wanted!r} is not on the form")
        if _held(entry, kind) != wanted:
            raise FillError(f"{path}: {wanted!r} did not commit")
    elif kind == "checkbox":
        wanted = [str(v).strip() for v in (value if isinstance(value, list) else [value])]
        present = {b.get_attribute("name") for b in entry.locator("input[type=checkbox]").all()}
        missing = [w for w in wanted if w not in present]
        if missing:
            raise FillError(f"{path}: options {missing} are not on the form")
        for box in entry.locator("input[type=checkbox]").all():
            box.set_checked(box.get_attribute("name") in wanted, force=True)
        if _held(entry, kind) != sorted(wanted):
            raise FillError(f"{path}: wanted {sorted(wanted)} but the form holds {_held(entry, kind)}")
    elif kind == "autocomplete":
        choose_location(page, entry, str(value))
    elif kind == "date":
        fill_date(entry, str(value))
    else:
        box = entry.locator("input, textarea").first
        limit = box.evaluate("el => el.maxLength > 0 ? el.maxLength : null")
        if limit and len(str(value)) > limit:
            raise FillError(f"{path}: answer is {len(str(value))} characters but the field allows {limit}")
        box.fill(str(value))


def choose_location(page, entry, location: str) -> None:
    """Type the applicant's own city and pick the geocoder suggestion that names that city and country."""
    parts = [p.strip() for p in location.split(",")]
    city, country = parts[0], parts[-1]
    box = entry.locator("input[role=combobox]")
    box.click()
    box.fill(city)
    options = page.locator("[role=listbox] [role=option], [role=option]")
    options.first.wait_for(state="visible", timeout=10_000)
    page.wait_for_timeout(600)
    for option in options.all():
        text = option.inner_text().strip()
        bits = [b.strip().lower() for b in text.split(",")]
        if bits[0] == city.lower() and any(country.lower() == b or country.lower() in b for b in bits[1:]):
            option.click()
            for _ in range(10):  # the chosen suggestion is written into the input asynchronously
                if city.lower() in box.input_value().lower():
                    return
                page.wait_for_timeout(300)
            raise FillError("location did not commit")
    raise FillError(f"no location suggestion for {location!r}")


def fill_date(entry, value: str) -> None:
    """Date fields are a react-datepicker text input. `value` is 'YYYY-MM' or 'YYYY-MM-DD' from the profile; the
    typed text is read back so a wrong picker format can never pass silently."""
    m_ = re.fullmatch(r"(\d{4})-(\d{2})(?:-(\d{2}))?", value)
    if not m_:
        raise FillError(f"date {value!r} must be YYYY-MM or YYYY-MM-DD")
    year, month, day = m_.group(1), m_.group(2), m_.group(3) or "01"
    box = entry.locator("input.ashby-application-form-input-date")
    box.click()
    box.fill(f"{month}/{day}/{year}")
    box.press("Enter")
    held = box.input_value()
    if not (year in held and (month.lstrip("0") in re.split(r"\D+", held) or month in held)):
        raise FillError(f"date {value!r} typed as {month}/{day}/{year} but the field shows {held!r}")


def dom_census(page) -> list[dict]:
    """Every field entry actually rendered: path, kind, required, label."""
    out = []
    for entry in page.locator(ALL_ENTRIES).all():
        label = entry.locator("label.ashby-application-form-question-title").first
        path = entry.get_attribute("data-field-path") or (label.get_attribute("for") if label.count() else "") or ""
        if not path:
            continue
        out.append({
            "id": path, "kind": _kind(entry),
            "required": bool(entry.locator("label.ashby-application-form-question-title[class*=required]").count()),
            "label": label.inner_text().strip() if label.count() else "",
        })  # fmt: skip
    return out


def fill(page, questions: list[Question], answers: dict[str, object], facts) -> dict:
    """Fill every answered field, then read the committed values back. Never clicks submit."""
    dom = {f["id"]: f for f in dom_census(page)}
    pending = {k: v for k, v in answers.items() if v is not None}
    not_on_form = []
    for path in list(pending):
        if path not in dom:
            not_on_form.append(path)
            continue
        value = pending.pop(path)
        kind = dom[path]["kind"]
        if kind == "unknown":
            raise FillError(f"{path}: control type not recognised")
        if kind == "date" and re.fullmatch(r"[A-Za-z]+ \d{4}", str(value)):  # "April 2029" from grad_date
            month = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
                     "october", "november", "december"].index(str(value).split()[0].lower()) + 1  # fmt: skip
            value = f"{str(value).split()[1]}-{month:02d}"
        _apply(page, path, value, kind)
    report = readback(page, answers)
    report["not_on_form"] = sorted(not_on_form)
    return report


def readback(page, answers: dict[str, object]) -> dict:
    held, empty_required, mismatches = {}, [], {}
    for f in dom_census(page):
        entry = _entry(page, f["id"])
        current = _held(entry, f["kind"])
        held[f["id"]] = current
        if f["required"] and current in ("", [], None):
            empty_required.append(f["id"])
        wanted = answers.get(f["id"])
        if wanted is None or f["kind"] in ("file", "date", "autocomplete"):
            continue  # verified at fill time by name chip / typed-value check / committed suggestion
        if isinstance(wanted, list):
            if sorted(str(w) for w in wanted) != current:
                mismatches[f["id"]] = {"wanted": wanted, "held": current}
        elif str(wanted).strip() != str(current).strip():
            mismatches[f["id"]] = {"wanted": wanted, "held": current}
    return {"held": held, "empty_required": empty_required, "mismatches": mismatches}


# --- submit --------------------------------------------------------------------------------------

CONFIRM_TEXT_RE = re.compile(r"thank you for (applying|your application|your interest)|application (has been )?(submitted|received)|"
                             r"we('ve| have) received your application|successfully submitted", re.I)  # fmt: skip
CHALLENGE_SELECTOR = 'iframe[src*="recaptcha/api2/bframe"], iframe[src*="hcaptcha.com"], iframe[title*="challenge" i]'


def _classify(page, responses: list[tuple[int, str]]) -> tuple[str, str]:
    form_gone = page.locator(ENTRY.format(path="_systemfield_name")).count() == 0
    body = page.locator("body").inner_text()[:4000]
    if form_gone and CONFIRM_TEXT_RE.search(body):
        return CONFIRMED, ""
    for frame in page.locator(CHALLENGE_SELECTOR).all():
        if frame.is_visible():
            return CHALLENGE, ""
    for status, text in responses:
        if "submit" in text.lower() and "error" in text.lower():
            # Ashby answers 200 with an errors array when it rejects (validation, captcha score, closed posting)
            m_ = re.search(r'"message"\s*:\s*"([^"]{0,160})"', text)
            note = m_.group(1) if m_ else "server rejected the application"
            if re.search(r"captcha|robot|suspicious", note, re.I):
                return CHALLENGE, note
            if not any(s < 400 and "submit" in t.lower() and '"success":true' in t.replace(" ", "") for s, t in responses):
                return INVALID, note
    error = page.locator(".ashby-application-form-error, [class*=error]:visible").first
    try:
        if error.count() and (text := error.inner_text().strip()):
            if not any(s < 400 and '"success":true' in t.replace(" ", "") for s, t in responses):
                return INVALID, text[:120]
    except Exception:  # noqa: BLE001
        pass
    return UNKNOWN, ""


def submit(page, wait_ms: int = 30_000) -> tuple[str, str]:
    """Click submit exactly once and classify what happened. → (outcome, detail)."""
    responses: list[tuple[int, str]] = []

    def on_response(resp):
        if resp.request.method == "POST" and "ashbyhq.com" in resp.url and "graphql" in resp.url:
            try:
                responses.append((resp.status, resp.text()[:2000]))
            except Exception:  # noqa: BLE001
                responses.append((resp.status, ""))

    page.on("response", on_response)
    page.locator("button.ashby-application-form-submit-button, form button[type=submit]").first.click()
    waited, outcome, note = 0, UNKNOWN, ""
    while waited < wait_ms:
        page.wait_for_timeout(500)
        waited += 500
        if waited < 2_000:
            continue
        outcome, note = _classify(page, responses)
        if outcome != UNKNOWN:
            break
    statuses = [s for s, _ in responses][-4:]
    return outcome, f"{note} POST statuses={statuses} url={page.url}".strip()
