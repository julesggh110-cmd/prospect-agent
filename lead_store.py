"""
Lead store — persistent SQLite database of every lead ever generated.

Purpose:
- Dedup across runs (don't re-prospect the same SIREN twice).
- Track lifecycle: when was each lead first seen? Has it been pushed to CRM?
- Power "show me only NEW leads since last campaign" workflows.

Schema:
- leads(siren PK, company_name, person_name, person_email, overall_score,
        first_seen_at, last_seen_at, campaigns INT, last_campaign_id,
        pushed_to_hubspot BOOL, hubspot_contact_id, dropped BOOL,
        drop_reason, payload_json)

DB file: data/leads.db (per-project; for multi-tenant later we'll add user_id).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Optional


DB_DEFAULT = Path(__file__).resolve().parent / "data" / "leads.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    siren                TEXT PRIMARY KEY,
    company_name         TEXT NOT NULL,
    person_name          TEXT,
    person_email         TEXT,
    overall_score        INTEGER,
    first_seen_at        TEXT NOT NULL,
    last_seen_at         TEXT NOT NULL,
    campaigns            INTEGER NOT NULL DEFAULT 1,
    last_campaign_id     TEXT,
    pushed_to_hubspot    INTEGER NOT NULL DEFAULT 0,
    hubspot_contact_id   TEXT,
    dropped              INTEGER NOT NULL DEFAULT 0,
    drop_reason          TEXT,
    icp_score            INTEGER,
    payload_json         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_company_name ON leads(company_name);
CREATE INDEX IF NOT EXISTS idx_leads_last_seen ON leads(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_leads_last_campaign ON leads(last_campaign_id);
"""


@contextmanager
def _conn(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    p = db_path or DB_DEFAULT
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p)
    c.executescript(SCHEMA)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def upsert_leads(leads: Iterable, campaign_id: str,
                 db_path: Optional[Path] = None) -> tuple[int, int]:
    """Insert new leads, update existing ones. Returns (n_new, n_existing).

    `leads` is an iterable of triangulation.Lead instances.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    n_new = 0
    n_existing = 0
    with _conn(db_path) as c:
        for lead in leads:
            siren = lead.company_siren or f"NO-SIREN/{lead.company_name}"
            existing = c.execute("SELECT siren FROM leads WHERE siren=?", (siren,)).fetchone()
            payload = json.dumps(lead.model_dump(), default=str)
            if existing:
                c.execute(
                    """UPDATE leads SET
                         company_name=?, person_name=?, person_email=?,
                         overall_score=?, last_seen_at=?, campaigns=campaigns+1,
                         last_campaign_id=?, dropped=?, drop_reason=?,
                         icp_score=?, payload_json=?
                       WHERE siren=?""",
                    (lead.company_name, lead.person_name.value, lead.person_email.value,
                     lead.overall_score, now, campaign_id, int(lead.dropped),
                     lead.drop_reason, getattr(lead, 'icp_score', None),
                     payload, siren),
                )
                n_existing += 1
            else:
                c.execute(
                    """INSERT INTO leads
                       (siren, company_name, person_name, person_email,
                        overall_score, first_seen_at, last_seen_at,
                        last_campaign_id, dropped, drop_reason, icp_score, payload_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (siren, lead.company_name, lead.person_name.value,
                     lead.person_email.value, lead.overall_score, now, now,
                     campaign_id, int(lead.dropped), lead.drop_reason,
                     getattr(lead, 'icp_score', None), payload),
                )
                n_new += 1
    return n_new, n_existing


def already_seen_sirens(sirens: Iterable[str], db_path: Optional[Path] = None) -> set[str]:
    """Return the subset of `sirens` that already exist in the store."""
    sirens = [s for s in sirens if s]
    if not sirens:
        return set()
    with _conn(db_path) as c:
        placeholders = ",".join("?" for _ in sirens)
        rows = c.execute(
            f"SELECT siren FROM leads WHERE siren IN ({placeholders})",
            list(sirens),
        ).fetchall()
        return {r["siren"] for r in rows}


def mark_pushed_to_hubspot(siren: str, contact_id: str,
                            db_path: Optional[Path] = None) -> None:
    with _conn(db_path) as c:
        c.execute(
            "UPDATE leads SET pushed_to_hubspot=1, hubspot_contact_id=? WHERE siren=?",
            (contact_id, siren),
        )


def list_recent(limit: int = 20, db_path: Optional[Path] = None) -> list[dict]:
    """Return the most recently seen leads (for quick CLI inspection)."""
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT siren, company_name, person_name, person_email, overall_score, "
            "       last_seen_at, campaigns, pushed_to_hubspot, dropped "
            "FROM leads ORDER BY last_seen_at DESC LIMIT ?", (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def stats(db_path: Optional[Path] = None) -> dict:
    with _conn(db_path) as c:
        total = c.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"]
        kept = c.execute("SELECT COUNT(*) AS n FROM leads WHERE dropped=0").fetchone()["n"]
        pushed = c.execute(
            "SELECT COUNT(*) AS n FROM leads WHERE pushed_to_hubspot=1",
        ).fetchone()["n"]
        campaigns = c.execute(
            "SELECT COUNT(DISTINCT last_campaign_id) AS n FROM leads",
        ).fetchone()["n"]
    return {"total": total, "kept": kept, "pushed_to_hubspot": pushed, "campaigns": campaigns}


def _cli() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Lead store inspector")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats", help="Print store statistics")
    rl = sub.add_parser("recent", help="List recent leads")
    rl.add_argument("--limit", type=int, default=20)
    args = p.parse_args()

    if args.cmd == "stats":
        print(json.dumps(stats(), indent=2))
    elif args.cmd == "recent":
        for row in list_recent(args.limit):
            print(f"  [{row['overall_score']:3d}] {row['company_name']:30s} | "
                  f"{row['person_name'] or '—':25s} | seen {row['campaigns']}x | "
                  f"{'HS' if row['pushed_to_hubspot'] else '  '} {'DROP' if row['dropped'] else ''}")


if __name__ == "__main__":
    _cli()
