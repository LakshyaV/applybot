from __future__ import annotations

import json
import os
from collections import Counter

import typer

from . import db, filters
from . import models as m
from .config import load_config
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
def recover():
    """Requeue expired leases; move interrupted submits to 'verify' (never retried)."""
    typer.echo(json.dumps(db.recover(db.connect())))


def main() -> None:
    app()
