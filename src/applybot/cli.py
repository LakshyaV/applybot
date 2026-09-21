from __future__ import annotations

import json
import os
import re
import time
from datetime import date
from collections import Counter

import typer

from . import db, filters, preflight, resolve as rs
from . import models as m
from .config import DATA_DIR, ROOT, load_config, load_profile
from .forms import greenhouse
from .normalize import is_tracker
from .sources import github_repo

GENERIC_OPTION_RE = (r"bachelor|undergrad|software|computer|engineering|english|other|none|not applicable|n/a|"
                     r"prefer not|decline|job board|github|online")


def _relevant_option_re(profile: dict) -> re.Pattern:
    """Options worth showing an answerer from a 200-entry picker: generic ones plus the user's own values
    (taken from the profile at runtime so nothing personal is hard-coded in this public repo)."""
    addr, edu = profile["address"], profile["education"][0]
    own = [addr.get("city"), addr.get("province_state"), addr.get("country"), edu["school"].split()[-1],
           (edu.get("end") or "")[:4], profile["availability"]["season"][-4:], *profile["work_authorization"]["citizenships"]]
    return re.compile("|".join([GENERIC_OPTION_RE, *(re.escape(v) for v in own if v)]), re.I)


app = typer.Typer(no_args_is_help=True, add_completion=False, help="Internship discovery + application tracker.")


def _apply_company_cap(conn, cap: int | None) -> int:
    """Keep only the `cap` best-ranked roles per company; the rest wait as 'discovered'.

    Roles already applied to (or mid-flight) at that company use up the cap too.
    """
    if not cap:
        return 0
    used = (m.IN_PROGRESS, m.NEEDS_ANSWERS, m.NEEDS_INPUT, m.SUBMITTING, m.VERIFY, m.SUBMITTED, m.ALREADY_APPLIED)
    return conn.execute(
        f"""UPDATE jobs SET status = ?, reason = 'over per_company_cap' WHERE id IN (
             SELECT id FROM (
               SELECT j.id,
                      ROW_NUMBER() OVER (PARTITION BY lower(j.company)
                                         ORDER BY j.priority, j.date_posted DESC, j.id) AS rank,
                      (SELECT COUNT(*) FROM jobs u WHERE lower(u.company) = lower(j.company)
                          AND u.status IN ({",".join("?" * len(used))})) AS used
               FROM jobs j WHERE j.status = ?)
             WHERE rank + used > ?)""",
        (m.DISCOVERED, *used, m.QUEUED, cap),
    ).rowcount


@app.command("ingest-repo")
def ingest_repo(
    url: str,
    relevant_only: bool = typer.Option(False, help="Keep only SWE/ML/data/quant intern titles (discovery mode)."),
    as_json: bool = typer.Option(False, "--json"),
):
    """Scrape every job in a GitHub internship-list repo into the tracker (idempotent)."""
    cfg = load_config()
    conn = db.connect()
    known = frozenset(r[0] for r in conn.execute("SELECT raw_url FROM jobs WHERE raw_url != url"))
    jobs, adapter = github_repo.ingest(url, cfg["target"]["season"], known)
    outcomes, queued_by_ats, reasons = Counter(), Counter(), Counter()
    for job in jobs:
        status, reason = filters.eligibility(job, cfg)
        if status == m.QUEUED and is_tracker(job.url):
            status, reason = m.FAILED, "unresolved tracker link"
        if status == m.QUEUED and relevant_only and (
            not filters.is_relevant_title(job.title) or job.category not in cfg["discovery_categories"]
        ):
            status, reason = m.DISCOVERED, "outside discovery filter"
        outcome = db.upsert_job(conn, job, status, reason, filters.priority(job, cfg))
        outcomes[outcome] += 1
        if outcome == "new":
            if status == m.QUEUED:
                queued_by_ats[job.ats] += 1
            else:
                reasons[f"{status}: {reason.split(' [')[0][:40]}"] += 1
    capped = _apply_company_cap(conn, cfg.get("per_company_cap"))

    report = {
        "adapter": adapter, "rows": len(jobs), **outcomes, "newly_queued": sum(queued_by_ats.values()) - capped,
        "queued_by_ats": dict(queued_by_ats.most_common()), "not_queued": dict(reasons.most_common(8)),
    }  # fmt: skip
    if as_json:
        typer.echo(json.dumps(report))
        return
    typer.echo(f"adapter={adapter}  rows={len(jobs)}  new={outcomes['new']}  seen={outcomes['seen']}  "
               f"duplicate={outcomes['duplicate']}  closed={outcomes['closed']}")  # fmt: skip
    typer.echo(f"newly queued: {report['newly_queued']}  by ATS: {report['queued_by_ats']}")
    if reasons:
        typer.echo("not queued: " + "; ".join(f"{k} ×{v}" for k, v in reasons.most_common(8)))


@app.command()
def status(by: str = typer.Option("status", help="status | ats | source | category")):
    """Counts only — safe to read into an LLM context."""
    conn = db.connect()
    for row in db.counts(conn, by):
        typer.echo(f"{row['n']:>6}  {row['k']}")


@app.command("list")
def list_jobs(status: str = m.QUEUED, limit: int = 15, ats: str = ""):
    """Show the next jobs in claim order."""
    conn = db.connect()
    clause, params = ("AND ats = ?", [ats]) if ats else ("", [])
    rows = conn.execute(
        f"SELECT id, priority, ats, company, title, countries FROM jobs WHERE status = ? {clause} "
        "ORDER BY priority, date_posted DESC, id LIMIT ?",
        [status, *params, limit],
    ).fetchall()
    for r in rows:
        typer.echo(f"{r['id']:>6} p{r['priority']:<3} {r['ats']:<16} {r['company'][:24]:<24} {r['title'][:60]}  {r['countries']}")


@app.command()
def next(n: int = 3, ats: str = typer.Option("", help="Comma-separated ATS filter")):
    """Atomically lease the next n queued jobs and print them as JSON lines."""
    conn = db.connect()
    db.recover(conn)
    worker = f"cli-{os.getpid()}"
    for r in db.claim(conn, n, worker, [a for a in ats.split(",") if a] or None):
        typer.echo(json.dumps({k: r[k] for k in ("id", "company", "title", "url", "ats", "board", "ats_job_id", "countries")}))


@app.command()
def mark(job_id: int, status: str, reason: str = ""):
    """Set a job's status (releases its lease)."""
    valid = {v for k, v in vars(m).items() if k.isupper() and isinstance(v, str)}
    if status not in valid:
        raise typer.BadParameter(f"unknown status; one of {sorted(valid)}")
    conn = db.connect()
    current = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if current is None:
        raise typer.BadParameter(f"no job {job_id}")
    if current["status"] == m.SUBMITTED and status != m.SUBMITTED:
        raise typer.BadParameter("a submitted job is final — one application per job, ever")
    db.set_status(conn, job_id, status, reason)


@app.command()
def survey(ats: str = "greenhouse", limit: int = 50):
    """Read-only census: fetch form schemas via the ATS API, preflight the JD, record unanswered questions."""
    import httpx

    if ats != "greenhouse":
        raise typer.BadParameter("only greenhouse exposes its form schema through a public API")
    conn, profile = db.connect(), load_profile()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = ? AND ats = ? ORDER BY priority, date_posted DESC, id LIMIT ?",
        (m.QUEUED, ats, limit),
    ).fetchall()
    tally = Counter()
    with httpx.Client(timeout=30) as client:
        for row in rows:
            try:
                questions, meta = greenhouse.parse(greenhouse.fetch(row["board"], row["ats_job_id"], client))
            except greenhouse.Gone:
                db.set_status(conn, row["id"], m.CLOSED, "posting removed (ATS API 404)")
                tally["gone"] += 1
                continue
            except httpx.HTTPError as err:
                tally[f"error {type(err).__name__}"] += 1
                continue
            for q in questions:
                q.company = row["company"]
            countries = meta["countries"] if meta["countries"] != ["UNKNOWN"] else json.loads(row["countries"])
            conn.execute("UPDATE jobs SET countries = ? WHERE id = ?", (json.dumps(countries), row["id"]))
            blocked, reason = preflight.check(meta["description"], countries)
            if blocked:
                db.set_status(conn, row["id"], blocked, reason)
                tally[blocked] += 1
                continue
            run_dir = DATA_DIR / "runs" / str(row["id"])
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "schema.json").write_text(json.dumps(
                {"meta": meta, "questions": [vars(q) for q in questions]}, indent=1, ensure_ascii=False))
            ctx = rs.JobContext(row["company"], countries)
            preset = greenhouse.preset_answers(questions, rs.Facts(profile, ctx), profile["resume_path"])
            result = rs.resolve(conn, questions, profile, ctx, preset)
            for q in result.misses:
                rs.record_miss(conn, q, row["id"])
            tally["ready" if result.ready else "blocked_on_answers"] += 1
    typer.echo(f"surveyed={len(rows)}  " + "  ".join(f"{k}={v}" for k, v in tally.most_common()))
    by_tier = dict(conn.execute("SELECT high_stakes, COUNT(*) FROM questions GROUP BY high_stakes").fetchall())
    typer.echo(f"distinct unanswered questions: {sum(by_tier.values())}  (critical → you: {by_tier.get(2, 0)}, "
               f"verify: {by_tier.get(1, 0)}, low: {by_tier.get(0, 0)})")


@app.command()
def questions(limit: int = 20, offset: int = 0, as_json: bool = typer.Option(False, "--json"), tier: str = ""):
    """Unanswered questions, most common first (deduped across jobs). Input for the `answerer`."""
    conn = db.connect()
    tiers = ["low", "verify", "critical"]
    relevant_re = _relevant_option_re(load_profile())
    clause = f"WHERE high_stakes = {tiers.index(tier)}" if tier else ""
    rows = conn.execute(f"SELECT * FROM questions {clause} ORDER BY job_count DESC, qhash LIMIT ? OFFSET ?",
                        (limit, offset)).fetchall()  # fmt: skip
    for r in rows:
        options = json.loads(r["options"])
        item = {"qhash": r["qhash"], "jobs": r["job_count"], "tier": tiers[r["high_stakes"]], "type": r["field_type"],
                "label": r["label"], "options": options}  # fmt: skip
        if as_json and len(options) > 25:  # country/school pickers: show only options that could matter
            relevant = [o for o in options if relevant_re.search(o)]
            item["options"] = options[:4] + relevant
            item["options_note"] = f"{len(options)} options in total; showing the first 4 plus profile-relevant ones"
        if as_json:
            typer.echo(json.dumps(item, ensure_ascii=False))
        else:
            flag = {"low": " ", "verify": "?", "critical": "!"}[item["tier"]]
            opts = f"  {item['options'][:6]}" if item["options"] else ""
            typer.echo(f"{item['qhash']} x{item['jobs']:<3}{flag} [{item['type']}] {item['label'][:110]}{opts}")


@app.command("answers-import")
def answers_import(path: str, origin: str = "llm", replace: bool = typer.Option(False, help="Also overwrite existing UNCLEARED bank entries")):
    """Load proposed resolutions [{qhash, resolution}] into the bank. Every entry is validated; high-stakes
    entries stay unusable until cleared. Requiredness is per form, so it is enforced at fill time, not here."""
    conn = db.connect()
    ok, rejected = 0, []
    for item in json.loads(open(path).read()):
        if len(item["qhash"]) < 20:  # unique-prefix shorthand, like git
            matches = conn.execute("SELECT qhash FROM questions WHERE qhash LIKE ? UNION SELECT qhash FROM answer_bank "
                                   "WHERE qhash LIKE ?", (item["qhash"] + "%",) * 2).fetchall()  # fmt: skip
            if len(matches) != 1:
                rejected.append((item["qhash"], f"prefix matches {len(matches)} questions"))
                continue
            item["qhash"] = matches[0]["qhash"]
        banked = conn.execute("SELECT approved FROM answer_bank WHERE qhash = ?", (item["qhash"],)).fetchone()
        if banked and (banked["approved"] or not replace):  # never clobber a cleared entry; uncleared needs --replace
            rejected.append((item["qhash"], "already in the bank" + (" and cleared" if banked["approved"] else "")))
            continue
        row = conn.execute("SELECT * FROM questions WHERE qhash = ?", (item["qhash"],)).fetchone()
        if row is None and replace:
            row = conn.execute("SELECT * FROM answer_bank WHERE qhash = ? AND approved = 0", (item["qhash"],)).fetchone()
        if row is None:
            rejected.append((item["qhash"], "not a pending question (or already cleared)"))
            continue
        q = rs.Question("", row["label"], row["field_type"], False, json.loads(row["options"]))
        try:
            rs.bank_put(conn, q, item["resolution"], origin, approved=False)
            ok += 1
        except ValueError as err:
            rejected.append((item["qhash"], str(err)))
    typer.echo(f"imported={ok} rejected={len(rejected)}")
    for qhash, why in rejected:
        typer.echo(f"  {qhash}: {why}")


@app.command()
def approvals(
    approve: list[str] = typer.Option([], help="qhash to clear (repeatable)"),
    approve_file: str = typer.Option("", help="File of qhashes to clear, one per line"),
    tier: str = typer.Option("critical", help="critical = cleared by the USER only · verify = cleared by the verifier pass"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Bank entries awaiting clearance, phrased as the claim the form will make on the user's behalf."""
    level = {"critical": rs.CRITICAL, "verify": rs.VERIFY}[tier]
    conn = db.connect()
    if approve_file:  # one hash (or unique prefix) per line — lets the USER clear a list an agent has reviewed
        approve = [*approve, *(line.strip() for line in open(approve_file) if line.strip())]
    for qhash in approve:
        found = conn.execute("SELECT * FROM answer_bank WHERE qhash LIKE ? AND high_stakes = ? AND approved = 0",
                             (qhash + "%", level)).fetchall()  # fmt: skip
        if len(found) != 1:
            typer.echo(f"  {qhash}: matches {len(found)} uncleared {tier} entries — skipped")
            continue
        row, qhash = found[0], found[0]["qhash"]
        conn.execute("UPDATE answer_bank SET approved = 1, origin = origin || ? WHERE qhash = ?", (f"+{tier}", qhash))
        if level == rs.CRITICAL:  # audit trail the user can skim and veto
            q = rs.Question("", row["label"], row["field_type"], True, json.loads(row["options"]))
            log = ROOT / "profile" / "approved_claims.md"
            if not log.exists():
                log.write_text("# Critical answers cleared on my behalf\n\nTo veto one: tell the agent its id, or run "
                               "`uv run applybot bank-revoke <id>`.\n\n")
            with log.open("a") as fh:
                fh.write(f"- `{qhash}` {date.today().isoformat()} — {rs.assertion_sentence(q, json.loads(row['resolution']))}\n")
    rows = conn.execute("SELECT * FROM answer_bank WHERE high_stakes = ? AND approved = 0 ORDER BY created_at", (level,)).fetchall()
    for r in rows:
        q = rs.Question("", r["label"], r["field_type"], True, json.loads(r["options"]))
        sentence = rs.assertion_sentence(q, json.loads(r["resolution"]))
        typer.echo(json.dumps({"qhash": r["qhash"], "claim": sentence}, ensure_ascii=False) if as_json else f"{r['qhash']}  {sentence}")
    if not rows:
        typer.echo("nothing awaiting clearance")


def _process(conn, profile: dict, cfg: dict, context, row, submit: bool) -> dict:
    """One Greenhouse application: schema → preflight → resolve → fill → readback → (optionally) submit.
    Returns a small summary dict and leaves the job in its final status."""
    job_id = row["id"]
    run_dir = DATA_DIR / "runs" / str(job_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    summary = {"job": job_id, "company": row["company"], "title": row["title"][:60]}

    def finish(status: str, reason: str = "", **extra) -> dict:
        db.set_status(conn, job_id, status, reason)
        return {**summary, "status": status, "reason": reason[:160], **extra}

    try:
        questions, meta = greenhouse.parse(greenhouse.fetch(row["board"], row["ats_job_id"]))
    except greenhouse.Gone:
        return finish(m.CLOSED, "posting removed (ATS API 404)")
    countries = meta["countries"] if meta["countries"] != ["UNKNOWN"] else json.loads(row["countries"])
    blocked, reason = preflight.check(meta["description"], countries)
    if blocked:
        return finish(blocked, reason)
    ctx = rs.JobContext(row["company"], countries)
    facts = rs.Facts(profile, ctx)
    for q in questions:
        q.company = row["company"]

    page = context.new_page()
    try:
        page.goto(meta["url"] or row["url"], wait_until="networkidle", timeout=60_000)
        if not page.locator("form #first_name").count():
            return finish(m.NEEDS_HUMAN, "not the standard Greenhouse form layout (embedded/legacy board)")
        questions += greenhouse.dom_only_questions(page, questions, row["company"])
        preset = greenhouse.preset_answers(questions, facts, str(ROOT / profile["resume_path"]))
        on_form = {q.id for q in questions}
        for essay in conn.execute("SELECT question_id, text FROM essays WHERE job_id = ? AND status = 'written'", (job_id,)):
            if essay["question_id"] in on_form:
                preset[essay["question_id"]] = essay["text"]
        result = rs.resolve(conn, questions, profile, ctx, preset)
        for q in result.misses:
            rs.record_miss(conn, q, job_id)
        limits = {f["id"]: f.get("maxlength") for f in greenhouse.dom_census(page)}
        for q in result.essays:  # ask for exactly the essays this form requires, with the field's size limit
            conn.execute("INSERT OR IGNORE INTO essays (job_id, question_id, label, created_at) VALUES (?,?,?,?)",
                         (job_id, q.id, q.label, int(time.time())))  # fmt: skip
        for essay_id, limit in limits.items():
            conn.execute("UPDATE essays SET max_chars = ? WHERE job_id = ? AND question_id = ?", (limit, job_id, essay_id))
        too_long = conn.execute("SELECT question_id, max_chars FROM essays WHERE job_id = ? AND status = 'written' "
                                "AND max_chars IS NOT NULL AND length(text) > max_chars", (job_id,)).fetchall()  # fmt: skip
        if too_long:  # never let the browser truncate an answer mid-sentence
            for row_ in too_long:
                conn.execute("UPDATE essays SET status = 'pending' WHERE job_id = ? AND question_id = ?",
                             (job_id, row_["question_id"]))  # fmt: skip
            return finish(m.NEEDS_ANSWERS, f"essay longer than the field allows ({too_long[0]['max_chars']} characters): rewrite")
        (run_dir / "answers.json").write_text(json.dumps(result.answers, indent=1, ensure_ascii=False, default=str))
        if result.skip_job:
            return finish(m.SKIPPED_INELIGIBLE, "; ".join(f"{q.label[:60]} ({why})" for q, why in result.skip_job[:2]))
        if result.human:
            return finish(m.NEEDS_HUMAN, "; ".join(f"{q.label[:60]} ({why})" for q, why in result.human[:3]))
        if result.needs_input:
            return finish(m.NEEDS_INPUT, "; ".join(f"{q.label[:60]} (missing {fact})" for q, fact in result.needs_input[:3]))
        if result.misses or result.essays:
            pending = [q.label[:50] for q in (result.misses + result.essays)[:4]]
            return finish(m.NEEDS_ANSWERS, f"{len(result.misses)} unanswered, {len(result.essays)} essays: {pending}")

        report = greenhouse.fill(page, questions, dict(result.answers), facts)
        (run_dir / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str))
        page.screenshot(path=str(run_dir / "filled.png"), full_page=True)
        if report["mismatches"] or report["empty_required"]:
            return finish(m.FAILED, f"readback: mismatches={list(report['mismatches'])} empty={report['empty_required'][:5]}")
        if not submit:
            return finish(m.DRY_RUN_DONE, "filled and verified; not submitted (dry run)", filled=len(result.answers))

        application_id = conn.execute(
            "INSERT INTO applications (job_id, mode, lane, started_at, run_dir) VALUES (?,?,?,?,?)",
            (job_id, cfg["mode"], "greenhouse", int(time.time()), str(run_dir)),
        ).lastrowid
        db.begin_submit(conn, job_id, application_id)  # write-ahead: from here a crash means `verify`, never a retry
        outcome, detail = greenhouse.submit(page)
        if outcome == greenhouse.NEEDS_CODE:
            typer.echo(json.dumps({**summary, "event": "needs_code", "hint": f"uv run applybot code {job_id} <8-char code from email>"}))
            code = _wait_for_code(conn, job_id, timeout_s=600)
            if not code:
                # Greenhouse answered 428 and no code was ever entered, so the application was NOT accepted.
                # That is positively known, which makes a later retry safe (unlike an unidentified outcome).
                conn.execute("UPDATE applications SET finished_at = ?, outcome = 'code_not_provided' WHERE id = ?",
                             (int(time.time()), application_id))  # fmt: skip
                return finish(m.NEEDS_HUMAN, "verification code not provided within 10 min; Greenhouse did not accept the application")
            outcome, detail = greenhouse.enter_security_code(page, code)
        page.screenshot(path=str(run_dir / "after_submit.png"), full_page=True)
        final = {greenhouse.CONFIRMED: m.SUBMITTED, greenhouse.INVALID: m.FAILED,
                 greenhouse.CHALLENGE: m.NEEDS_HUMAN}.get(outcome, m.VERIFY)  # fmt: skip
        conn.execute("UPDATE applications SET finished_at = ?, outcome = ?, confirmation = ? WHERE id = ?",
                     (int(time.time()), outcome, detail, application_id))  # fmt: skip
        return finish(final, f"{outcome}: {detail}")
    except Exception as err:  # noqa: BLE001 — one bad form must not stop the queue
        current = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()["status"]
        if current == m.SUBMITTING:
            return finish(m.VERIFY, f"error after submit click: {type(err).__name__}: {str(err)[:120]}")
        return finish(m.FAILED, f"{type(err).__name__}: {str(err)[:160]}")
    finally:
        page.close()


def _wait_for_code(conn, job_id: int, timeout_s: int) -> str | None:
    conn.execute("CREATE TABLE IF NOT EXISTS codes (job_id INTEGER PRIMARY KEY, code TEXT NOT NULL)")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        row = conn.execute("SELECT code FROM codes WHERE job_id = ?", (job_id,)).fetchone()
        if row:
            conn.execute("DELETE FROM codes WHERE job_id = ?", (job_id,))
            return row["code"].strip()
        time.sleep(3)
    return None


@app.command()
def code(job_id: int, value: str):
    """Hand an emailed verification code to the running application (same browser session)."""
    conn = db.connect()
    conn.execute("CREATE TABLE IF NOT EXISTS codes (job_id INTEGER PRIMARY KEY, code TEXT NOT NULL)")
    conn.execute("INSERT OR REPLACE INTO codes (job_id, code) VALUES (?, ?)", (job_id, value))
    typer.echo("code queued")


@app.command()
def run(limit: int = 5, headless: bool = False, jobs: str = typer.Option("", help="Comma-separated job ids to run instead of the queue head")):
    """Work the Greenhouse queue. Whether anything is SUBMITTED is decided only by `mode` in config.yaml,
    which the user controls: dry_run (default) never submits."""
    from playwright.sync_api import sync_playwright

    from . import browser

    cfg, profile, conn = load_config(), load_profile(), db.connect()
    mode = cfg["mode"]
    if mode not in ("dry_run", "supervised", "auto"):
        raise typer.BadParameter(f"mode {mode!r} is not runnable here")
    submit = mode in ("supervised", "auto")
    if submit and profile.get("resume_needs_update"):
        typer.echo(json.dumps({"stopped": "resume_needs_update is set in profile.md — forms and resume disagree",
                               "detail": profile["resume_needs_update"]}))  # fmt: skip
        return
    db.recover(conn)
    if submit:  # forms that already passed a dry run are vetted: put them back at the front of the queue
        conn.execute("UPDATE jobs SET status = ?, reason = 'passed dry run' WHERE status = ?", (m.QUEUED, m.DRY_RUN_DONE))
        today = conn.execute("SELECT COUNT(*) FROM applications WHERE outcome = 'confirmed' AND finished_at > ?",
                             (int(time.time()) - 86400,)).fetchone()[0]  # fmt: skip
        limit = min(limit, cfg["pacing"]["daily_submit_cap"] - today)
        if mode == "supervised":
            done = conn.execute("SELECT COUNT(*) FROM applications WHERE mode = 'supervised' AND outcome = 'confirmed'").fetchone()[0]
            limit = min(limit, cfg["supervised_limit"] - done)
        if limit <= 0:
            typer.echo(json.dumps({"stopped": "submit cap reached for this mode; the user decides what happens next"}))
            return

    tally = Counter()
    with sync_playwright() as pw:
        context = browser.launch(pw, headless=headless)
        try:
            if jobs:  # specific jobs: only ones that are queued (never submitted / mid-submit / awaiting verify)
                ids = [int(j) for j in jobs.split(",") if j.strip().isdigit()][:limit]
                marks = ",".join("?" * len(ids))
                claimed = conn.execute(
                    f"UPDATE jobs SET status = ?, lease_until = ?, claimed_by = ? WHERE id IN ({marks}) AND status = ? "
                    "AND ats = 'greenhouse' RETURNING *",
                    (m.IN_PROGRESS, int(time.time()) + db.LEASE_SECONDS, f"run-{os.getpid()}", *ids, m.QUEUED),
                ).fetchall()
                claimed.sort(key=lambda r: ids.index(r["id"]))  # run them in the order they were asked for
            else:
                claimed = db.claim(conn, limit, f"run-{os.getpid()}", ["greenhouse"])
            for row in claimed:
                outcome = _process(conn, profile, cfg, context, row, submit)
                tally[outcome["status"]] += 1
                typer.echo(json.dumps(outcome, ensure_ascii=False))
                if submit and outcome["status"] == m.SUBMITTED:
                    time.sleep(cfg["pacing"]["submit_min_interval_seconds"])
        finally:
            context.close()
    typer.echo(json.dumps({"mode": mode, "summary": dict(tally)}))


PLACEHOLDER_RE = re.compile(r"\[[^\]]{2,40}\]|\{[a-z_ ]{2,30}\}|lorem ipsum|as an ai\b|language model", re.I)


@app.command()
def essays(limit: int = 10, as_json: bool = typer.Option(False, "--json")):
    """Essays still to be written, each with the job context needed to write it truthfully."""
    conn = db.connect()
    rows = conn.execute(
        "SELECT e.job_id, e.question_id, e.label, e.max_chars, j.company, j.title FROM essays e JOIN jobs j ON j.id = e.job_id "
        "WHERE e.status = 'pending' AND j.status IN (?, ?) ORDER BY j.priority, e.job_id LIMIT ?",
        (m.NEEDS_ANSWERS, m.QUEUED, limit),
    ).fetchall()
    for r in rows:
        schema_path = DATA_DIR / "runs" / str(r["job_id"]) / "schema.json"
        meta = json.loads(schema_path.read_text())["meta"] if schema_path.exists() else {}
        item = {"job_id": r["job_id"], "question_id": r["question_id"], "company": r["company"], "title": r["title"],
                "question": r["label"], "max_chars": r["max_chars"] or 1500,
                "job_description": (meta.get("description") or "")[:1800]}  # fmt: skip
        typer.echo(json.dumps(item, ensure_ascii=False) if as_json else
                   f"{r['job_id']:>6} {r['company'][:22]:<22} {r['title'][:34]:<34} :: {r['label'][:90]}")  # fmt: skip
    if not rows:
        typer.echo("no essays pending")


@app.command("essays-import")
def essays_import(path: str):
    """Load written essays [{job_id, question_id, text}]. Each is also saved to data/runs/<job>/essays.md so the
    user can read exactly what will be sent in their name."""
    conn = db.connect()
    ok, rejected = 0, []
    for item in json.loads(open(path).read()):
        text = (item.get("text") or "").strip()
        row = conn.execute("SELECT label, max_chars FROM essays WHERE job_id = ? AND question_id = ?",
                           (item["job_id"], item["question_id"])).fetchone()  # fmt: skip
        limit = (row["max_chars"] if row else None) or 1500
        problem = ("no such pending essay" if row is None else "too short" if len(text) < 40 else
                   f"too long: {len(text)} characters, the field allows {limit}" if len(text) > limit else
                   "contains a placeholder or AI boilerplate" if PLACEHOLDER_RE.search(text) else "")  # fmt: skip
        if problem:
            rejected.append((item["job_id"], item["question_id"], problem))
            continue
        conn.execute("UPDATE essays SET text = ?, status = 'written' WHERE job_id = ? AND question_id = ?",
                     (text, item["job_id"], item["question_id"]))  # fmt: skip
        run_dir = DATA_DIR / "runs" / str(item["job_id"])
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / "essays.md").open("a") as fh:
            fh.write(f"### {row['label']}\n\n{text}\n\n")
        ok += 1
    typer.echo(f"imported={ok} rejected={len(rejected)}")
    for job_id, question_id, why in rejected:
        typer.echo(f"  job {job_id} / {question_id}: {why}")


@app.command()
def requeue(statuses: str = "needs_answers,needs_input,needs_human,failed,dry_run_done", ats: str = "greenhouse"):
    """Put parked jobs back in the queue after the answer bank or profile changed. Never touches a job that
    was submitted, is mid-submit, or is awaiting reconciliation."""
    allowed = {m.NEEDS_ANSWERS, m.NEEDS_INPUT, m.NEEDS_HUMAN, m.FAILED, m.DRY_RUN_DONE}
    chosen = [s for s in statuses.split(",") if s in allowed]
    conn = db.connect()
    changed = conn.execute(
        f"UPDATE jobs SET status = ?, reason = 'requeued' WHERE ats = ? AND status IN ({','.join('?' * len(chosen))})",
        (m.QUEUED, ats, *chosen),
    ).rowcount
    typer.echo(f"requeued={changed}")


@app.command("bank-revoke")
def bank_revoke(qhash: str):
    """Un-clear a bank entry (the user's veto). Jobs that relied on it will pause at that question again."""
    changed = db.connect().execute("UPDATE answer_bank SET approved = 0, origin = origin || '+revoked' WHERE qhash = ?",
                                   (qhash,)).rowcount  # fmt: skip
    typer.echo("revoked" if changed else "no such entry")


@app.command()
def recover():
    """Requeue expired leases; move interrupted submits to 'verify' (never retried)."""
    typer.echo(json.dumps(db.recover(db.connect())))


def main() -> None:
    app()
