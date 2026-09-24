"""IBM careers portal (careers.ibm.com): IBMid account + application lane.

Apply → login.ibm.com (IBMid). The account is created once in the persistent automation profile (email, names,
country, password from the Keychain, emailed verification code from the user); its cookies keep later runs signed
in. Only account terms / privacy-statement boxes are ticked, never marketing opt-ins.
"""

from __future__ import annotations

import re
from pathlib import Path

CAREERS = "https://careers.ibm.com/en_US/careers"
CODE_RE = re.compile(r"verification code|verify your email|enter the (\d-digit )?code|code (we )?sent|one-time code", re.I)
MARKETING_RE = re.compile(r"marketing|communications|newsletter|offers|promotions|keep me informed|updates about", re.I)
ACCOUNT_TERMS_RE = re.compile(r"privacy statement|terms of use|terms and conditions|account privacy|i accept|i agree", re.I)


def signed_in(page, job_id: str = "129661") -> bool:
    """Signed in = the careers login URL no longer bounces to login.ibm.com."""
    page.goto(f"{CAREERS}/Login?jobId={job_id}", wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(6_000)
    return "login.ibm.com" not in page.url and "ibm.com/account/reg" not in page.url


def _text(page) -> str:
    return re.sub(r"\s+", " ", page.locator("body").inner_text())[:3000]


def _click_next(page) -> bool:
    for name in (r"^continue$", r"^create (account|ibmid)$", r"^verify$", r"^submit$", r"^next$", r"^log ?in$", r"^sign in$", r"^proceed$"):
        btn = page.get_by_role("button", name=re.compile(name, re.I))
        for i in range(btn.count()):
            if btn.nth(i).is_visible() and btn.nth(i).is_enabled():
                btn.nth(i).click()
                return True
    return False


def _code_inputs(page):
    return page.locator("input[autocomplete='one-time-code'], input[id*='code' i], input[name*='code' i], "
                        "input[placeholder*='code' i], input[maxlength='1'], input[id*='otp' i]")


def _tick_account_terms(page) -> None:
    for box in page.locator("input[type=checkbox]").all():
        if not box.is_visible() or box.is_checked():
            continue
        label = page.locator(f'label[for="{box.get_attribute("id")}"]')
        text = (label.text_content() if label.count() else box.evaluate("el => el.closest('label')?.innerText || ''")) or ""
        if ACCOUNT_TERMS_RE.search(text) and not MARKETING_RE.search(text):
            box.check(force=True)


def run_account(page, profile: dict, password: str, get_code, shots: Path, log) -> str:
    """Drive the IBMid sign-in / registration until the careers site accepts the session. → 'signed_in' or a
    short description of where it stopped (for the human)."""
    ident, addr = profile["identity"], profile["address"]
    if signed_in(page):
        return "signed_in"
    for step in range(10):
        page.wait_for_timeout(1_500)
        page.screenshot(path=str(shots / f"ibm_step{step}.png"), full_page=True)
        url, text = page.url, _text(page)
        log({"step": step, "url": url[:100]})
        if "careers.ibm.com" in url and "login.ibm.com" not in url and "/account/reg" not in url:
            return "signed_in"
        if re.search(r"incorrect ibmid or password|account (is )?locked|too many attempts", text, re.I):
            return "existing IBMid, wrong password: an IBMid already exists for this email and the shared ATS password is not its password"
        # IBMid login: username → (continue) → password
        user = page.locator("#username")
        if user.count() and user.first.is_visible() and not user.first.input_value():
            user.first.fill(ident["email"])
            _click_next(page)
            page.wait_for_timeout(4_000)
            # an unknown IBMid is offered a registration link; a known one gets the password box
            if page.locator("#password").count() == 0 and page.get_by_role("link", name=re.compile(r"create an ibmid", re.I)).count():
                page.get_by_role("link", name=re.compile(r"create an ibmid", re.I)).first.click()
                page.wait_for_timeout(5_000)
            continue
        pwd = page.locator("#password, input[type=password]")
        if pwd.count() and pwd.first.is_visible() and not _code_inputs(page).count():
            for i in range(pwd.count()):  # password + confirm-password
                if pwd.nth(i).is_visible():
                    pwd.nth(i).fill(password)
            _tick_account_terms(page)
            _click_next(page)
            page.wait_for_timeout(5_000)
            continue
        # registration form: email / names / country
        email = page.locator("#email")
        if email.count() and email.first.is_visible():
            if not email.first.input_value():
                email.first.fill(ident["email"])
            if page.locator("#firstName").count():
                page.locator("#firstName").fill(ident["first_name"])
                page.locator("#lastName").fill(ident["last_name"])
            if page.locator("select#country").count():
                page.locator("select#country").select_option(label=addr["country"])
            _tick_account_terms(page)
            _click_next(page)
            page.wait_for_timeout(5_000)
            continue
        codes = _code_inputs(page)
        if codes.count() and codes.first.is_visible() or CODE_RE.search(text):
            code = get_code()
            if not code:
                return "no code within 10 min"
            boxes = _code_inputs(page)
            if boxes.count() >= len(code) and boxes.first.get_attribute("maxlength") == "1":
                for i, ch in enumerate(code):
                    boxes.nth(i).fill(ch)
            else:
                boxes.first.fill(code)
            _tick_account_terms(page)
            _click_next(page)
            page.wait_for_timeout(6_000)
            continue
        _tick_account_terms(page)
        if _click_next(page):
            page.wait_for_timeout(5_000)
            continue
        return f"stopped at {url[:80]}: {text[:300]}"
    return f"gave up after 10 steps at {page.url[:80]}"


# --- application wizard (Avature) -------------------------------------------------------------------------------
import json  # noqa: E402
from ..resolve import Question  # noqa: E402
from .greenhouse import CONFIRMED, FillError, INVALID, UNKNOWN  # noqa: E402

# Standard fields on the "Personal information" page, identified by their labels → profile facts / literals.
LABEL_FACTS = {
    "legal first name": ("fact", "first_name"), "legal last name": ("fact", "last_name"),
    "address line 1": ("fact", "address_line1"), "city": ("fact", "city"), "zip code/postal code": ("fact", "postal_code"),
    "home email": ("fact", "email"), "phone number": ("fact", "phone"),
    "country": ("fact", "country_of_residence"), "state/province": ("fact", "province_state"),
    "degree name": ("fact", "degree"), "university": ("fact", "school"),
}
EDU_TYPE_PREFERENCE = ["Bachelor's Degree", "Bachelors", "Bachelor", "Undergraduate", "Bachelor's"]


class Gone(Exception):
    pass


SEARCH_API = "https://www-api.ibm.com/search/api/v2"
SEARCH_HEADERS = {"content-type": "application/json", "referer": "https://www.ibm.com/", "origin": "https://www.ibm.com",
                  "user-agent": "Mozilla/5.0"}  # fmt: skip
_INDEX: dict[str, dict] = {}
_INDEX_AT = 0.0


def _search(country: str, offset: int, size: int = 100) -> list[dict]:
    import httpx

    f = [{"term": {"field_keyword_18": "Internship"}}, {"term": {"field_keyword_05": country}}]
    body = {"appId": "careers", "scopes": ["careers2"], "query": {"bool": {"must": []}}, "post_filter": {"bool": {"must": f}},
            "aggs": {"field_keyword_083": {"filter": {"bool": {"must": f}}, "aggs": {"field_keyword_08": {"terms": {"field": "field_keyword_08", "size": 6}}}}},
            "from": offset, "size": size, "sort": [{"_score": "desc"}, {"pageviews": "desc"}], "lang": "zz", "localeSelector": {},
            "sm": {"query": "", "lang": "zz"},
            "_source": ["_id", "title", "url", "description", "field_keyword_05", "field_keyword_08", "field_keyword_17", "field_keyword_19"]}  # fmt: skip
    resp = httpx.post(SEARCH_API, json=body, headers=SEARCH_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()["hits"]["hits"]


def index(force: bool = False) -> dict[str, dict]:
    """IBM's own careers search index (public, JSON): every internship with title, location, country, description.
    The job pages themselves sit behind a JS challenge for plain HTTP clients, so this is the schema source."""
    global _INDEX, _INDEX_AT
    import time as _time

    if _INDEX and not force and _time.time() - _INDEX_AT < 3600:
        return _INDEX
    out: dict[str, dict] = {}
    for country in ("United States", "Canada"):
        for offset in range(0, 600, 100):
            hits = _search(country, offset)
            for h in hits:
                src = h["_source"]
                m_ = re.search(r"jobId=(\d+)", src.get("url", ""))
                if m_:
                    out[m_.group(1)] = src
            if len(hits) < 100:
                break
    _INDEX, _INDEX_AT = out, _time.time()
    return out


def fetch(board: str, job_id: str, client=None) -> dict:
    src = index().get(job_id) or index(force=True).get(job_id)
    if not src:
        raise Gone(job_id)
    location = (src.get("field_keyword_19") or "").strip()
    if location.lower() in ("multiple cities", "multiple locations"):
        location = ""
    country = src.get("field_keyword_05") or ""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", src.get("description") or ""))
    return {"title": src.get("title", ""), "location": location, "country": country, "description": text[:6000],
            "workplace": src.get("field_keyword_17") or "", "job_id": job_id}  # fmt: skip


def parse(data: dict) -> tuple[list[Question], dict]:
    """The wizard's questions are only visible page by page after login, so parse() returns none here; the
    runner asks `page_questions` on each page instead."""
    from ..normalize import countries_of

    location = data.get("location") or ""
    country = {"United States": "USA", "Canada": "Canada"}.get(data.get("country") or "", data.get("country") or "")
    countries = countries_of([f"{location}, {country}" if location else country]) if (location or country) else ["UNKNOWN"]
    desc = data.get("description", "")
    # "Program Duration: 12-16 Months" — a term longer than the applicant's availability is a stated requirement
    dur = re.search(r"program duration:\s*(\d+)\s*(?:-|–|to)?\s*(\d+)?\s*months?", desc, re.I)
    return [], {"title": data.get("title", ""), "location": location, "countries": countries, "description": desc,
                "country": data.get("country", ""), "program_months_min": int(dur.group(1)) if dur else None}  # fmt: skip


def preset_answers(questions, facts, resume_path: str) -> dict:
    return {}


def read_posting(page, job_id: str) -> dict:
    """The full job description (the search index only carries a teaser): text plus the stated program duration."""
    page.goto(f"{CAREERS}/JobDetail?jobId={job_id}", wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(6_000)
    text = re.sub(r"\s+", " ", page.locator("body").inner_text())
    dur = re.search(r"program duration:\s*(\d+)\s*(?:-|–|to)?\s*(\d+)?\s*months?", text, re.I)
    return {"description": text[:12000], "program_months_min": int(dur.group(1)) if dur else None,
            "already_applied": applied_marker(page)}  # fmt: skip


def open_form(page, board: str, job_id: str, resume_path: str = "") -> bool:
    """Login?jobId → ApplicationMethods (upload the resume) → talent-network gate (declined) → the wizard."""
    page.goto(f"{CAREERS}/Login?jobId={job_id}", wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(6_000)
    if "login.ibm.com" in page.url:
        return False
    if "ApplicationMethods" in page.url and resume_path:
        page.get_by_text(re.compile(r"^From Device", re.I)).first.click()
        page.wait_for_timeout(1_500)
        page.locator("#resumeFile").set_input_files(resume_path)
        page.wait_for_timeout(2_500)
        btn = page.locator("#uploadFileResume")
        (btn if btn.is_visible() else page.get_by_role("button", name=re.compile(r"^continue$", re.I)).filter(visible=True).first).click()
        page.wait_for_timeout(8_000)
    nothanks = page.get_by_label(re.compile(r"no thanks", re.I))
    if nothanks.count():
        nothanks.first.check(force=True)  # talent-network marketing opt-in: declined
        _continue(page)
    return "JobApplication" in page.url


def _continue(page) -> None:
    page.get_by_role("button", name=re.compile(r"^continue$", re.I)).filter(visible=True).first.click()
    page.wait_for_timeout(7_000)


def _label_for(page, el_id: str) -> str:
    lab = page.locator(f'label[for="{el_id}"]')
    return re.sub(r"\s+", " ", (lab.first.text_content() or "")).replace("Select an option", "").strip(" *") if lab.count() else ""


def dom_census(page) -> list[dict]:
    """Every control on the current wizard page: id, kind, label (radio groups collapse to one entry)."""
    return page.evaluate("""() => {
      const out = []; const seen = new Set();
      const clean = s => (s || '').replace(/\\s+/g, ' ').replace('Select an option', '').trim().replace(/\\s*\\*$/, '');
      for (const el of document.querySelectorAll('form input, form select, form textarea')) {
        if (el.type === 'hidden' || el.id.endsWith('-sample') || el.id.endsWith('search__field')) continue;
        if (el.classList.contains('select2-hidden-accessible')) {
          const box = el.parentElement.querySelector('.select2-container'); if (!box || box.offsetParent === null) continue;
        } else if (el.offsetParent === null && el.type !== 'radio') continue;
        const lab = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
        const fs = el.closest('fieldset'); const legend = fs ? fs.querySelector('legend') : null;
        if (el.type === 'radio') {
          const fsv = el.closest('fieldset'); if (fsv && fsv.offsetParent === null) continue;
          if (seen.has(el.name)) { const g = out.find(o => o.id === el.name); g.options.push(clean(lab ? lab.innerText : el.value)); continue; }
          seen.add(el.name);
          out.push({id: el.name, kind: 'radio', label: clean(legend ? legend.innerText : ''), required: !!(legend && /\\*/.test(legend.innerText)), options: [clean(lab ? lab.innerText : el.value)]});
          continue;
        }
        const kind = el.tagName === 'SELECT' ? (el.classList.contains('select2-hidden-accessible') ? 'select2' : 'select')
                   : el.tagName === 'TEXTAREA' ? 'textarea' : el.type === 'checkbox' ? 'checkbox' : el.type === 'date' ? 'date' : el.type === 'file' ? 'file' : 'text';
        const combo = kind === 'select2' ? el.parentElement.querySelector('[role=combobox]') : null;
        const required = el.required || el.getAttribute('aria-required') === 'true' || (lab && /\\*/.test(lab.innerText)) || (combo && combo.getAttribute('aria-required') === 'true');
        out.push({id: el.id, kind, label: clean(lab ? lab.innerText : (legend ? legend.innerText : '')), required: !!required,
                  options: el.tagName === 'SELECT' ? [...el.options].map(o => o.text.trim()).filter(t => t && t !== 'Select an option') : [],
                  value: el.type === 'checkbox' ? el.checked : (el.value || '')});
      }
      return out;
    }""")


def choose2(page, el_id: str, text: str, attempts: int = 3) -> None:
    """Pick an option in a select2 combobox by typing and choosing the exact match. The option list is fetched
    over the network and sometimes fails to load or lags the keystrokes; each attempt reopens the box."""
    listed: list[str] = []
    for attempt in range(attempts):
        page.locator(f'[id="{el_id}"]').locator("xpath=following-sibling::span[contains(@class,'select2')]//span[@role='combobox']").first.click()
        page.wait_for_timeout(500)
        page.keyboard.type(text[:40])
        options = page.locator(".select2-results__option")
        for _ in range(20):  # "Searching…" until the list lands
            page.wait_for_timeout(500)
            listed = [options.nth(i).inner_text().strip() for i in range(min(options.count(), 12))]
            if listed and not any(t.startswith("Searching") for t in listed):
                break
        for i in range(options.count()):
            if options.nth(i).inner_text().strip().lower() == text.lower():
                options.nth(i).click()
                for _ in range(8):  # the rendered selection updates asynchronously
                    page.wait_for_timeout(400)
                    if held2(page, el_id).strip().lower() == text.lower():
                        return
                break  # clicked but did not commit: retry
        page.keyboard.press("Escape")
        if not any("could not be loaded" in t or t.startswith("Searching") or t.startswith("Loading") for t in listed):
            break  # the list loaded and the option is simply not there
        page.wait_for_timeout(1_500)
    raise FillError(f"{el_id}: no option {text!r}; offered {listed[:8]}")


def held2(page, el_id: str) -> str:
    box = page.locator(f'[id="select2-{el_id}-container"]').first
    if not box.count():
        return ""
    return (box.get_attribute("title") or box.text_content() or "").strip()


def set_radio(page, name: str, label: str) -> bool:
    """Tick the radio whose label matches. → False when the group is hidden (a conditional block that is not shown)."""
    group = page.locator(f'input[type=radio][name="{name}"]')
    if not group.count():
        raise FillError(f"radio {name}: not on the page")
    fieldset = group.first.locator("xpath=ancestor::fieldset[1]")
    if (fieldset.count() and not fieldset.first.is_visible()) or not (group.first.is_visible() or
            page.locator(f'label[for="{group.first.get_attribute("id")}"]').first.is_visible()):
        return False
    for radio in group.all():
        rid = radio.get_attribute("id") or ""
        lab = _label_for(page, rid)
        if lab.lower() == label.lower():
            page.locator(f'label[for="{rid}"]').first.click()  # the styled label drives the (visually replaced) input
            page.wait_for_timeout(300)
            if not (radio.is_checked() or radio.get_attribute("aria-checked") == "true"):
                radio.check(force=True)
            if not (radio.is_checked() or radio.get_attribute("aria-checked") == "true"):
                raise FillError(f"radio {name}: {label!r} did not commit")
            return True
    raise FillError(f"radio {name}: no option {label!r}")


def fill_text(page, el_id: str, value: str) -> None:
    box = page.locator(f'[id="{el_id}"]')
    box.fill(value)
    if box.input_value().strip() != value.strip():
        raise FillError(f"{el_id}: value did not commit")


def _fill_consents(page, census: list[dict], facts, filled: dict) -> None:
    for c in census:
        if c["kind"] != "radio" or c["id"] in filled:
            continue
        opts = [o.lower() for o in c["options"]]
        # the Talent Acquisition Privacy Notice statement sits outside the radio's own fieldset, so its legend is bare
        if opts == ["i agree"] and facts.get("acknowledge_privacy_notice") is True and re.search(
                r"talent acquisition privacy notice", page.locator("body").inner_text(), re.I):
            if set_radio(page, c["id"], "I agree"):
                filled[c["id"]] = "I agree"
        elif any(o.startswith("i consent to ibm processing") for o in opts) and facts.get("acknowledge_privacy_notice") is True:
            label = next(o for o in c["options"] if o.lower().startswith("i consent"))
            if set_radio(page, c["id"], label):
                filled[c["id"]] = label
        elif re.search(r"preferred name", c["label"], re.I) and "no" in opts:
            if set_radio(page, c["id"], "No"):
                filled[c["id"]] = "No"


def _fill_conditionals(page, census: list[dict], facts, filled: dict) -> None:
    for c in census:
        if c["id"] in filled:
            continue
        if c["kind"] == "select" and re.search(r"resident of china or south korea", c["label"], re.I):
            page.locator(f'[id="{c["id"]}"]').select_option(label="No")  # Canadian resident (profile address)
            filled[c["id"]] = "No"
        elif c["kind"] == "select2" and re.search(r"source of candidate", c["label"], re.I):
            try:
                choose2(page, c["id"], "Job Board")
            except FillError:
                choose2(page, c["id"], "Other")
            filled[c["id"]] = held2(page, c["id"])


def fill_personal_page(page, profile: dict, facts) -> dict:
    """The 'Personal information' page: names, address, contact, education, work history and the standard
    privacy / processing consents. Returns what was filled by field id."""
    filled: dict[str, object] = {}
    census = dom_census(page)
    by_label = {c["label"].lower(): c for c in census if c["label"]}

    def want(label: str):
        return by_label.get(label)

    for label, (kind, fact) in LABEL_FACTS.items():
        c = want(label)
        if not c:
            continue
        value = facts.get(fact)
        if value is None:
            continue
        if c["kind"] == "select2":
            if held2(page, c["id"]).lower() != str(value).lower():
                choose2(page, c["id"], str(value))
                page.wait_for_timeout(1_500)  # choosing a country reloads the state/province list
            filled[c["id"]] = str(value)
        elif c["kind"] in ("text", "textarea"):
            fill_text(page, c["id"], str(value))
            filled[c["id"]] = str(value)
    # consents: privacy notice acknowledgement + processing consent (standard application attestations).
    # Ticking the acknowledgement reveals further fields, so the census is taken again afterwards.
    for _pass in range(2):
        census = dom_census(page)
        _fill_consents(page, census, facts, filled)
        page.wait_for_timeout(800)
    census = dom_census(page)
    _fill_conditionals(page, census, facts, filled)
    # education row 0
    edu = profile["education"][0]
    if page.locator('[id="9014-1-0"]').count():
        fill_text(page, "9014-1-0", edu["degree"]); fill_text(page, "9014-3-0", edu["school"])
        if edu.get("start"):
            page.locator('[id="9014-5-0"]').fill(f"{edu['start']}-01" if len(edu["start"]) == 7 else edu["start"])
        if edu.get("end"):
            page.locator('[id="9014-4-0"]').fill(f"{edu['end']}-30" if len(edu["end"]) == 7 else edu["end"])
        for name in EDU_TYPE_PREFERENCE:
            try:
                choose2(page, "9014-2-0", name); break
            except FillError:
                continue
        filled["education"] = f"{edu['degree']} @ {edu['school']}"
    # work history: "Do you have past working experience?" + one row per experience
    exp = profile.get("experience") or []
    if page.locator('[id="9016"]').count():
        choose2(page, "9016", "Yes" if exp else "No")
        page.wait_for_timeout(1_500)
        for i, role in enumerate(exp[:3]):
            if not page.locator(f'[id="9017-1-{i}"]').count():
                add = page.get_by_role("button", name=re.compile(r"add another", re.I)).filter(visible=True)
                if not add.count():
                    break
                add.last.click(); page.wait_for_timeout(1_500)
            fill_text(page, f"9017-1-{i}", role["company"]); fill_text(page, f"9017-2-{i}", role["title"])
            page.locator(f'[id="9017-4-{i}"]').fill(f"{role['start']}-01" if len(role["start"]) == 7 else role["start"])
            choose2(page, f"9017-3-{i}", "No" if role.get("end") else "Yes")
            if role.get("end") and page.locator(f'[id="9017-5-{i}"]').count():
                page.locator(f'[id="9017-5-{i}"]').fill(f"{role['end']}-28" if len(role["end"]) == 7 else role["end"])
            filled[f"experience_{i}"] = f"{role['title']} @ {role['company']}"
    return filled


def page_questions(page, company: str, census: list[dict] | None = None) -> list[Question]:
    """The current page's controls as resolver Questions (used on the question pages after personal info)."""
    out = []
    for c in census if census is not None else dom_census(page):
        if not c["label"]:
            continue
        qtype = {"radio": "select", "select": "select", "select2": "select", "textarea": "textarea",
                 "checkbox": "checkbox", "file": "file"}.get(c["kind"], "text")
        options = c["options"] if c["kind"] in ("radio", "select") else []
        out.append(Question(id=c["id"], label=c["label"], type=qtype, required=c["required"], options=options, company=company))
    return out


def apply_answers(page, answers: dict, census: list[dict]) -> None:
    kinds = {c["id"]: c for c in census}
    for el_id, value in answers.items():
        c = kinds.get(el_id)
        if not c or value is None:
            continue
        if c["kind"] == "radio":
            set_radio(page, el_id, str(value))
        elif c["kind"] == "select":
            page.locator(f'[id="{el_id}"]').select_option(label=str(value))
        elif c["kind"] == "select2":
            choose2(page, el_id, str(value))
        elif c["kind"] == "checkbox":
            page.locator(f'[id="{el_id}"]').set_checked(bool(value), force=True)
        elif c["kind"] in ("text", "textarea", "date"):
            fill_text(page, el_id, str(value))


def page_errors(page) -> str:
    err = page.locator(".error, [class*=error]:visible, [role=alert]").filter(has_text=re.compile(r"required|invalid|correct", re.I))
    try:
        return err.first.inner_text().strip()[:160] if err.count() else ""
    except Exception:  # noqa: BLE001
        return ""


def at_submit(page) -> bool:
    btn = page.get_by_role("button", name=re.compile(r"^submit( application)?$", re.I))
    return bool(btn.count() and btn.first.is_visible())


def submit(page, wait_ms: int = 30_000) -> tuple[str, str]:
    page.get_by_role("button", name=re.compile(r"^submit( application)?$", re.I)).first.click()
    waited = 0
    while waited < wait_ms:
        page.wait_for_timeout(1_000)
        waited += 1_000
        text = re.sub(r"\s+", " ", page.locator("body").inner_text())[:3000]
        if re.search(r"thank you for applying|application (has been |was )?(submitted|received)|successfully submitted", text, re.I):
            return CONFIRMED, f"url={page.url}"
        if applied_marker(page):
            return CONFIRMED, f"job page shows Applied; url={page.url}"
        if page_errors(page):
            return INVALID, page_errors(page)
    return UNKNOWN, f"url={page.url}"


def applied_marker(page) -> bool:
    """After a submit IBM returns to the job page with its Apply button replaced by a disabled 'Applied'."""
    if "JobDetail" not in page.url:
        return False
    return bool(page.locator("a.button--disabled, button.button--disabled").filter(has_text=re.compile(r"^\s*applied\s*$", re.I)).count())


def already_applied(page, job_id: str) -> bool:
    """Reconciliation for a job left in `verify`: the job page says Applied."""
    page.goto(f"{CAREERS}/JobDetail?jobId={job_id}", wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(7_000)
    return applied_marker(page)


# Questionnaire fields the lane answers itself: their options are loaded lazily (select2), so the bank cannot hold a
# checked literal; each entry lists acceptable option texts in order of preference, or a fact name.
PAGE_PRESETS = [
    (re.compile(r"^how did you hear about this opportunity", re.I), ["Job Board", "Online Job Board", "Internet Job Board", "Other"]),
    (re.compile(r"^what is your preferred internship duration", re.I), ["4 months", "4 Months", "4-month", "4 month", "Four months"]),
    (re.compile(r"^correspondence language", re.I), ["English"]),
    (re.compile(r"^do you need to complete an internship as part of your degree", re.I), ["Yes"]),
    (re.compile(r"^please state your ethnic group", re.I), ["Do not want to declare", "Do not wish to declare", "Do not wish to respond", "Prefer not to say", "I do not wish to answer"]),
    (re.compile(r"^please select the country where your university", re.I), ["Canada"]),
    (re.compile(r"^please select the university or higher-education institution", re.I), ["University of Waterloo", "Waterloo, University of", "University Of Waterloo"]),
    (re.compile(r"^degree obtained or currently in progress", re.I), ["Bachelor's Degree", "Bachelors", "Bachelor", "Bachelor's", "Undergraduate"]),
    (re.compile(r"^graduation date \(completed", re.I), "grad_date"),
    (re.compile(r"^study/specialization of degree", re.I), ["Software Engineering", "Computer Science", "Engineering", "Computer Engineering", "Other"]),
    # revealed after the sponsorship answer; the option texts are IBM's own, tried per the applicant's status
    (re.compile(r"^please specify your current work authorization", re.I), "WORK_AUTH"),
    (re.compile(r"^month$", re.I), "birth_month_name"),
    (re.compile(r"^day$", re.I), "birth_day"),
    (re.compile(r"^name$", re.I), "full_name"),
    (re.compile(r"^date$", re.I), "signature_date_today"),
]


def preset_page_answers(page, census: list[dict], facts, meta: dict, resume_path: str) -> dict:
    """Answers for the questionnaire fields the lane owns. Returns {field id: value | [candidates]}."""
    out: dict[str, object] = {}
    city = (meta.get("location") or "").split(",")[0].strip().title()  # "POUGHKEEPSIE, US" → "Poughkeepsie"
    for c in census:
        label = c["label"]
        if c["kind"] == "file" and re.search(r"resume|cv", label, re.I):
            out[c["id"]] = resume_path
            continue
        if re.search(r"^what is your preferred work location", label, re.I):
            # the list holds IBM's main sites; a posting in a suburb maps to its metro (Markham → Toronto)
            metro = {"Markham": "Toronto", "Mississauga": "Toronto", "Brampton": "Toronto", "Kanata": "Ottawa", "Bromont": "Montreal",
                     "Poughkeepsie": "Poughkeepsie", "Rtp": "Research Triangle Park", "Research Triangle Park": "Research Triangle Park"}
            out[c["id"]] = [x for x in [city, metro.get(city, ""), "Toronto" if meta.get("country") == "Canada" else "",
                                        "Any", "Any location", "No preference", "Multiple locations", "Flexible"] if x]
            continue
        for rx, value in PAGE_PRESETS:
            if rx.search(label):
                if value == "WORK_AUTH":
                    if facts.get("work_authorized") is True:      # Canada: citizen, no sponsorship
                        out[c["id"]] = ["Canadian Citizen / Canadian Permanent Residency", "Canadian Citizen", "Citizen",
                                        "Canadian Citizen or Permanent Resident", "Citizen / Permanent Resident"]
                    elif facts.get("requires_sponsorship") is True:  # US: no status yet; the honest pick is the sponsorship one
                        out[c["id"]] = ["Require sponsorship", "Requires sponsorship", "I require sponsorship", "Will require sponsorship",
                                        "Need sponsorship", "Not authorized - require sponsorship", "Sponsorship required", "Other"]
                    else:
                        out[c["id"]] = []
                elif isinstance(value, list):
                    out[c["id"]] = value
                else:
                    fact = facts.get(value)
                    if fact is not None:
                        out[c["id"]] = str(fact)
                break
    return out


def apply_presets(page, presets: dict, census: list[dict]) -> dict:
    """Fill lane presets; a list value is tried in order until one option exists. Returns what was chosen."""
    kinds = {c["id"]: c for c in census}
    chosen: dict[str, object] = {}
    for el_id, value in presets.items():
        c = kinds.get(el_id)
        if not c:
            continue
        if c["kind"] == "file":
            page.locator(f'[id="{el_id}"]').set_input_files(str(value))
            chosen[el_id] = str(value).rsplit("/", 1)[-1]
            continue
        candidates = value if isinstance(value, list) else [value]
        if c["kind"] == "date":  # native date inputs take ISO only: "April 2029" → 2029-04-01
            iso = []
            for cand in candidates:
                m_ = re.fullmatch(r"([A-Za-z]+) (\d{4})", str(cand))
                if m_:
                    months = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
                    iso.append(f"{m_.group(2)}-{months.index(m_.group(1).lower()) + 1:02d}-01")
                else:
                    iso.append(str(cand))
            candidates = iso
        last = None
        for cand in candidates:
            try:
                if c["kind"] == "select2":
                    choose2(page, el_id, str(cand))
                elif c["kind"] == "select":
                    page.locator(f'[id="{el_id}"]').select_option(label=str(cand))
                elif c["kind"] == "radio":
                    set_radio(page, el_id, str(cand))
                else:
                    fill_text(page, el_id, str(cand))
                chosen[el_id] = str(cand)
                break
            except Exception as err:  # noqa: BLE001 — try the next candidate
                last = err
        else:
            offered = []
            if c["kind"] == "select2":  # list what the dropdown actually offers so the preset can be fixed
                try:
                    page.locator(f'[id="{el_id}"]').locator("xpath=following-sibling::span[contains(@class,'select2')]//span[@role='combobox']").first.click()
                    page.wait_for_timeout(1_500)
                    offered = [o.inner_text().strip() for o in page.locator(".select2-results__option").all()][:15]
                    page.keyboard.press("Escape")
                except Exception:  # noqa: BLE001
                    pass
            raise FillError(f"{el_id} ({c['label'][:40]}): none of {candidates} could be chosen; offered {offered}; last error: {str(last)[:160]}")
    return chosen
