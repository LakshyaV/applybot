"""SQLite tracker: idempotent ingest, atomic claim with leases, write-ahead submit intent."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import models as m
from .config import DB_PATH

LEASE_SECONDS = 30 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  key TEXT UNIQUE NOT NULL,
  key2 TEXT NOT NULL,
  company TEXT NOT NULL,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  raw_url TEXT,
  locations TEXT NOT NULL DEFAULT '[]',
  countries TEXT NOT NULL DEFAULT '[]',
  ats TEXT NOT NULL,
  board TEXT,
  ats_job_id TEXT,
  category TEXT,
  sponsorship TEXT,
  terms TEXT NOT NULL DEFAULT '[]',
  date_posted INTEGER NOT NULL DEFAULT 0,
  source TEXT,
  source_id TEXT,
  status TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  priority INTEGER NOT NULL DEFAULT 0,
  lease_until INTEGER NOT NULL DEFAULT 0,
  claimed_by TEXT NOT NULL DEFAULT '',
  first_seen INTEGER NOT NULL,
  last_seen INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, priority, date_posted DESC);
CREATE INDEX IF NOT EXISTS jobs_key2 ON jobs(key2);

-- one row per attempt; `intent_at` is written before the submit click
CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  mode TEXT NOT NULL,
  lane TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  intent_at INTEGER,
  finished_at INTEGER,
  outcome TEXT,
  confirmation TEXT,
  run_dir TEXT,
  error TEXT
);

-- exact-hash answer bank; `approved` gates high-stakes entries
CREATE TABLE IF NOT EXISTS answer_bank (
  qhash TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  field_type TEXT NOT NULL,
  options TEXT NOT NULL DEFAULT '[]',
  high_stakes INTEGER NOT NULL DEFAULT 0,
  resolution TEXT NOT NULL,
  approved INTEGER NOT NULL DEFAULT 0,
  origin TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

-- unresolved questions waiting for Claude or the user
CREATE TABLE IF NOT EXISTS questions (
  qhash TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  field_type TEXT NOT NULL,
  options TEXT NOT NULL DEFAULT '[]',
  high_stakes INTEGER NOT NULL DEFAULT 0,
  needs TEXT NOT NULL DEFAULT 'llm',
  example_job_id INTEGER,
  job_count INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS job_questions (
  job_id INTEGER NOT NULL,
  qhash TEXT NOT NULL,
  PRIMARY KEY (job_id, qhash)
);

-- free-text answers written for ONE job (never banked: "Why {company}?" differs per employer)
CREATE TABLE IF NOT EXISTS essays (
  job_id INTEGER NOT NULL,
  question_id TEXT NOT NULL,
  label TEXT NOT NULL,
  text TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | written
  created_at INTEGER NOT NULL,
  PRIMARY KEY (job_id, question_id)
);

CREATE TABLE IF NOT EXISTS accounts (
  ats TEXT NOT NULL,
  tenant TEXT NOT NULL,
  email TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  verified INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (ats, tenant)
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    try:  # migration: the field's character limit, captured from the live form
        conn.execute("ALTER TABLE essays ADD COLUMN max_chars INTEGER")
    except sqlite3.OperationalError:
        pass  # column already exists
    return conn


def upsert_job(conn: sqlite3.Connection, job: m.Job, status: str, reason: str, priority: int) -> str:
    """Insert or refresh a job. Returns 'new' | 'seen' | 'duplicate' | 'closed'."""
    now = int(time.time())
    row = conn.execute("SELECT id, status FROM jobs WHERE key = ?", (job.key,)).fetchone()
    if row:
        conn.execute("UPDATE jobs SET last_seen = ? WHERE id = ?", (now, row["id"]))
        # A listing that closed upstream is withdrawn only if we have not touched it yet.
        if status == m.CLOSED and row["status"] in (m.QUEUED, m.DISCOVERED, m.NEEDS_ANSWERS, m.NEEDS_INPUT):
            set_status(conn, row["id"], m.CLOSED, reason)
            return "closed"
        return "seen"

    outcome = "new"
    if status == m.QUEUED:
        twin = conn.execute(
            "SELECT id FROM jobs WHERE key2 = ? AND status NOT IN (?, ?, ?) LIMIT 1",
            (job.key2, m.DUPLICATE, m.CLOSED, m.OFF_SEASON),
        ).fetchone()
        if twin:
            status, reason, outcome = m.DUPLICATE, f"same posting as job {twin['id']}", "duplicate"
    conn.execute(
        """INSERT INTO jobs (key, key2, company, title, url, raw_url, locations, countries, ats, board,
               ats_job_id, category, sponsorship, terms, date_posted, source, source_id, status, reason,
               priority, first_seen, last_seen, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            job.key, job.key2, job.company, job.title, job.url, job.raw_url, json.dumps(job.locations),
            json.dumps(job.countries), job.ats, job.board, job.ats_job_id, job.category, job.sponsorship,
            json.dumps(job.terms), job.date_posted, job.source, job.source_id, status, reason, priority,
            now, now, now,
        ),
    )  # fmt: skip
    return outcome


def set_status(conn: sqlite3.Connection, job_id: int, status: str, reason: str = "") -> None:
    conn.execute(
        "UPDATE jobs SET status = ?, reason = ?, lease_until = 0, claimed_by = '', updated_at = ? WHERE id = ?",
        (status, reason, int(time.time()), job_id),
    )


def recover(conn: sqlite3.Connection) -> dict[str, int]:
    """Run at startup. Expired leases go back to the queue; interrupted submits are NEVER retried."""
    now = int(time.time())
    stuck = conn.execute(
        "UPDATE jobs SET status = ?, reason = 'crashed mid-submit; reconcile before retrying', updated_at = ? "
        "WHERE status = ?",
        (m.VERIFY, now, m.SUBMITTING),
    ).rowcount
    expired = conn.execute(
        "UPDATE jobs SET status = ?, lease_until = 0, claimed_by = '', updated_at = ? "
        "WHERE status = ? AND lease_until < ?",
        (m.QUEUED, now, m.IN_PROGRESS, now),
    ).rowcount
    return {"to_verify": stuck, "lease_expired": expired}


def claim(conn: sqlite3.Connection, n: int, worker: str, ats: list[str] | None = None) -> list[sqlite3.Row]:
    """Atomically lease the n best queued jobs (single UPDATE … RETURNING)."""
    now = int(time.time())
    ats_clause, params = "", []
    if ats:
        ats_clause = f"AND ats IN ({','.join('?' * len(ats))})"
        params = list(ats)
    rows = conn.execute(
        f"""UPDATE jobs SET status = ?, lease_until = ?, claimed_by = ?, updated_at = ?
            WHERE id IN (SELECT id FROM jobs WHERE status = ? {ats_clause}
                         ORDER BY priority ASC, date_posted DESC, id ASC LIMIT ?)
            RETURNING *""",
        [m.IN_PROGRESS, now + LEASE_SECONDS, worker, now, m.QUEUED, *params, n],
    ).fetchall()
    return sorted(rows, key=lambda r: (r["priority"], -r["date_posted"], r["id"]))


def begin_submit(conn: sqlite3.Connection, job_id: int, application_id: int) -> None:
    """Write-ahead intent, in one transaction, BEFORE the submit click."""
    now = int(time.time())
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?", (m.SUBMITTING, now, job_id))
        conn.execute("UPDATE applications SET intent_at = ? WHERE id = ?", (now, application_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def counts(conn: sqlite3.Connection, by: str = "status") -> list[sqlite3.Row]:
    assert by in ("status", "ats", "source", "category")
    return conn.execute(f"SELECT {by} AS k, COUNT(*) AS n FROM jobs GROUP BY {by} ORDER BY n DESC").fetchall()
