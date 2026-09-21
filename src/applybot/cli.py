from __future__ import annotations

import json
import os
from collections import Counter

import typer

from . import db, filters, preflight, resolve as rs
from . import models as m
from .config import DATA_DIR, load_config, load_profile
from .forms import greenhouse
from .normalize import is_tracker
from .sources import github_repo

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
    pending = conn.execute("SELECT COUNT(*), SUM(high_stakes) FROM questions").fetchone()
    typer.echo(f"surveyed={len(rows)}  " + "  ".join(f"{k}={v}" for k, v in tally.most_common()))
    typer.echo(f"distinct unanswered questions: {pending[0]} (high-stakes: {pending[1] or 0})")


@app.command()
def questions(limit: int = 20, as_json: bool = typer.Option(False, "--json"), high_stakes: bool | None = None):
    """Unanswered questions, most common first (deduped across jobs). Input for the `answerer`."""
    conn = db.connect()
    clause = "" if high_stakes is None else f"WHERE high_stakes = {int(high_stakes)}"
    rows = conn.execute(f"SELECT * FROM questions {clause} ORDER BY job_count DESC, qhash LIMIT ?", (limit,)).fetchall()
    for r in rows:
        item = {"qhash": r["qhash"], "jobs": r["job_count"], "high_stakes": bool(r["high_stakes"]), "type": r["field_type"],
                "label": r["label"], "options": json.loads(r["options"])}  # fmt: skip
        if as_json:
            typer.echo(json.dumps(item, ensure_ascii=False))
        else:
            flag = "!" if item["high_stakes"] else " "
            opts = f"  {item['options'][:6]}" if item["options"] else ""
            typer.echo(f"{item['qhash']} x{item['jobs']:<3}{flag} [{item['type']}] {item['label'][:110]}{opts}")


@app.command("answers-import")
def answers_import(path: str, origin: str = "llm"):
    """Load proposed resolutions [{qhash, resolution}] into the bank. Every entry is validated; high-stakes
    entries stay unusable until the user approves them."""
    conn = db.connect()
    ok, rejected = 0, []
    for item in json.loads(open(path).read()):
        row = conn.execute("SELECT * FROM questions WHERE qhash = ?", (item["qhash"],)).fetchone()
        if row is None:
            rejected.append((item["qhash"], "not a pending question"))
            continue
        q = rs.Question("", row["label"], row["field_type"], True, json.loads(row["options"]))
        try:
            rs.bank_put(conn, q, item["resolution"], origin, approved=False)
            ok += 1
        except ValueError as err:
            rejected.append((item["qhash"], str(err)))
    typer.echo(f"imported={ok} rejected={len(rejected)}")
    for qhash, why in rejected:
        typer.echo(f"  {qhash}: {why}")


@app.command()
def approvals(approve: list[str] = typer.Option([], help="qhash to approve (repeatable)"), as_json: bool = typer.Option(False, "--json")):
    """High-stakes bank entries awaiting the USER's approval, phrased as the claim the form will make."""
    conn = db.connect()
    for qhash in approve:
        conn.execute("UPDATE answer_bank SET approved = 1, origin = origin || '+user' WHERE qhash = ?", (qhash,))
    rows = conn.execute("SELECT * FROM answer_bank WHERE high_stakes = 1 AND approved = 0 ORDER BY created_at").fetchall()
    for r in rows:
        q = rs.Question("", r["label"], r["field_type"], True, json.loads(r["options"]))
        sentence = rs.assertion_sentence(q, json.loads(r["resolution"]))
        typer.echo(json.dumps({"qhash": r["qhash"], "claim": sentence}, ensure_ascii=False) if as_json else f"{r['qhash']}  {sentence}")
    if not rows:
        typer.echo("nothing awaiting approval")


@app.command()
def recover():
    """Requeue expired leases; move interrupted submits to 'verify' (never retried)."""
    typer.echo(json.dumps(db.recover(db.connect())))


def main() -> None:
    app()
