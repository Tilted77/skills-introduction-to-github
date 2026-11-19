"""SQLite tracker: one row per job, with its status, score and where the drafts live.

Status flow:  new -> scored -> drafted -> applied
                        \\-> skipped (low score, or you skipped it)
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

from .sources import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, source TEXT, company TEXT, company_name TEXT, title TEXT,
    location TEXT, remote INTEGER, url TEXT, apply_url TEXT, description TEXT, posted_at TEXT,
    fetched_at TEXT, status TEXT NOT NULL DEFAULT 'new',
    score INTEGER, verdict TEXT, score_detail TEXT, materials_dir TEXT, applied_at TEXT, note TEXT
)
"""
JOB_FIELDS = ("source", "company", "company_name", "title", "location", "remote",
              "url", "apply_url", "description", "posted_at")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute(SCHEMA)
        self.db.commit()

    def add_jobs(self, jobs: list[Job]) -> tuple[int, int]:
        """Insert new jobs; refresh the posting text of known ones without touching their status.

        A company often posts one role several times (once per office). Only the first copy is
        tracked, so you are never queued to apply to the same role twice. Returns (added, duplicates).
        """
        added = duplicates = 0
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for job in jobs:
            row = asdict(job) | {"remote": int(job.remote), "fetched_at": now}
            if self.db.execute("SELECT 1 FROM jobs WHERE id = ?", (job.id,)).fetchone():
                sets = ", ".join(f"{f} = :{f}" for f in JOB_FIELDS)
                self.db.execute(f"UPDATE jobs SET {sets} WHERE id = :id", row)
            elif self.db.execute("SELECT 1 FROM jobs WHERE source = ? AND company = ? AND lower(title) = lower(?)",
                                 (job.source, job.company, job.title)).fetchone():
                duplicates += 1
            else:
                cols = ("id",) + JOB_FIELDS + ("fetched_at",)
                self.db.execute(f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})", row)
                added += 1
        self.db.commit()
        return added, duplicates

    def by_status(self, *statuses: str) -> list[sqlite3.Row]:
        marks = ", ".join("?" * len(statuses))
        return self.db.execute(
            f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY score DESC, posted_at DESC, id", statuses
        ).fetchall()

    def all(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM jobs ORDER BY score DESC, posted_at DESC, id").fetchall()

    def find(self, fragment: str) -> list[sqlite3.Row]:
        """Look a job up by full id or by any unique-enough part of it."""
        return self.db.execute("SELECT * FROM jobs WHERE id = ? OR id LIKE ?", (fragment, f"%{fragment}%")).fetchall()

    def set_score(self, job_id: str, score: int, verdict: str, detail: dict, min_score: int) -> str:
        status = "scored" if score >= min_score else "skipped"
        note = None if status == "scored" else f"score below {min_score}"
        self.db.execute("UPDATE jobs SET status = ?, score = ?, verdict = ?, score_detail = ?, note = ? WHERE id = ?",
                        (status, score, verdict, json.dumps(detail), note, job_id))
        self.db.commit()
        return status

    def set_drafted(self, job_id: str, materials_dir: Path) -> None:
        self.db.execute("UPDATE jobs SET status = 'drafted', materials_dir = ? WHERE id = ?",
                        (str(materials_dir), job_id))
        self.db.commit()

    def set_applied(self, job_id: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.db.execute("UPDATE jobs SET status = 'applied', applied_at = ? WHERE id = ?", (now, job_id))
        self.db.commit()

    def set_skipped(self, job_id: str, note: str) -> None:
        self.db.execute("UPDATE jobs SET status = 'skipped', note = ? WHERE id = ?", (note, job_id))
        self.db.commit()

    def applied_today(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM jobs WHERE status = 'applied' AND applied_at LIKE ?",
                               (date.today().isoformat() + "%",)).fetchone()[0]

    def counts(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
