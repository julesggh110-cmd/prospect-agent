"""
Lead store — persistent SQLite database of every lead ever generated.

Purpose:
- Dedup across runs (don't re-prospect the same SIREN twice).
- Track lifecycle: when was each lead first seen? Has it been pushed to CRM?
- Power "show me only NEW leads since last campaign" workflows.
- **Multi-tenant**: every row carries a `tenant_id`. When the agent is sold to
  multiple clients (Bear Brothers + Comeos + resold further), each tenant
  sees only its own leads.
- **GDPR**: ships with `delete_for_subject(person_email)` (right-to-erasure)
  and `purge_older_than(days)` (data minimisation).

Schema (v2 — adds tenant_id, created_at, gdpr_deleted columns):
- leads(siren+tenant_id PK, company_name, person_name, person_email,
        overall_score, first_seen_at, last_seen_at, campaigns INT,
        last_campaign_id, pushed_to_hubspot BOOL, hubspot_contact_id,
        dropped BOOL, drop_reason, icp_score INT, tenant_id TEXT,
        gdpr_deleted BOOL, payload_json TEXT)

DB file: data/leads.db (single-file SQLite, isolates tenants by tenant_id col).

Default tenant id (when none set) = "default" — keeps single-user setups
backward compatible.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Optional


DB_DEFAULT = Path(__file__).resolve().parent / "data" / "leads.db"
DEFAULT_TENANT = "default"


def _current_tenant() -> str:
    """Tenant id for the current process. Override via PROSPECT_AGENT_TENANT env."""
    return os.environ.get("PROSPECT_AGENT_TENANT", DEFAULT_TENANT).strip() or DEFAULT_TENANT


# Base table creation — idempotent. New installs get tenant_id and gdpr_deleted
# directly. Old installs that pre-date these columns are upgraded via _MIGRATIONS
# BEFORE indexes are created (because the indexes reference the new columns).
_TABLE = """
CREATE TABLE IF NOT EXISTS leads (
    siren                TEXT NOT NULL,
    tenant_id            TEXT NOT NULL DEFAULT 'default',
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
    gdpr_deleted         INTEGER NOT NULL DEFAULT 0,
    payload_json         TEXT NOT NULL,
    PRIMARY KEY (siren, tenant_id)
);
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_leads_tenant ON leads(tenant_id);
CREATE INDEX IF NOT EXISTS idx_leads_company_name ON leads(tenant_id, company_name);
CREATE INDEX IF NOT EXISTS idx_leads_last_seen ON leads(tenant_id, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_leads_last_campaign ON leads(tenant_id, last_campaign_id);
CREATE INDEX IF NOT EXISTS idx_leads_person_email ON leads(person_email);
"""

# Idempotent migrations from v1 (no tenant_id, no gdpr_deleted) → v2.
# We swallow the "duplicate column" error for installs already on v2.
_MIGRATIONS = [
    "ALTER TABLE leads ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
    "ALTER TABLE leads ADD COLUMN gdpr_deleted INTEGER NOT NULL DEFAULT 0",
]


@contextmanager
def _conn(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    p = db_path or DB_DEFAULT
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p)
    # 1. Make sure the table exists (with new schema on fresh installs)
    c.executescript(_TABLE)
    # 2. Migrate older installs in place — adds missing columns before indexes
    for stmt in _MIGRATIONS:
        try:
            c.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
    # 3. Create indexes (now safe because all columns exist)
    c.executescript(_INDEXES)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def upsert_leads(leads: Iterable, campaign_id: str,
                 db_path: Optional[Path] = None,
                 tenant_id: Optional[str] = None) -> tuple[int, int]:
    """Insert new leads, update existing ones. Returns (n_new, n_existing).

    `leads` is an iterable of triangulation.Lead instances.
    `tenant_id` defaults to env PROSPECT_AGENT_TENANT or "default".
    """
    from datetime import datetime, timezone
    tenant = tenant_id or _current_tenant()
    now = datetime.now(timezone.utc).isoformat()
    n_new = 0
    n_existing = 0
    with _conn(db_path) as c:
        for lead in leads:
            siren = lead.company_siren or f"NO-SIREN/{lead.company_name}"
            existing = c.execute(
                "SELECT siren FROM leads WHERE siren=? AND tenant_id=?",
                (siren, tenant),
            ).fetchone()
            payload = json.dumps(lead.model_dump(), default=str)
            if existing:
                c.execute(
                    """UPDATE leads SET
                         company_name=?, person_name=?, person_email=?,
                         overall_score=?, last_seen_at=?, campaigns=campaigns+1,
                         last_campaign_id=?, dropped=?, drop_reason=?,
                         icp_score=?, payload_json=?
                       WHERE siren=? AND tenant_id=?""",
                    (lead.company_name, lead.person_name.value, lead.person_email.value,
                     lead.overall_score, now, campaign_id, int(lead.dropped),
                     lead.drop_reason, getattr(lead, 'icp_score', None),
                     payload, siren, tenant),
                )
                n_existing += 1
            else:
                c.execute(
                    """INSERT INTO leads
                       (siren, tenant_id, company_name, person_name, person_email,
                        overall_score, first_seen_at, last_seen_at,
                        last_campaign_id, dropped, drop_reason, icp_score, payload_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (siren, tenant, lead.company_name, lead.person_name.value,
                     lead.person_email.value, lead.overall_score, now, now,
                     campaign_id, int(lead.dropped), lead.drop_reason,
                     getattr(lead, 'icp_score', None), payload),
                )
                n_new += 1
    return n_new, n_existing


def already_seen_sirens(sirens: Iterable[str],
                        db_path: Optional[Path] = None,
                        tenant_id: Optional[str] = None) -> set[str]:
    """Return the subset of `sirens` already in the store FOR THIS TENANT."""
    sirens = [s for s in sirens if s]
    if not sirens:
        return set()
    tenant = tenant_id or _current_tenant()
    with _conn(db_path) as c:
        placeholders = ",".join("?" for _ in sirens)
        rows = c.execute(
            f"SELECT siren FROM leads WHERE tenant_id=? AND siren IN ({placeholders})",
            [tenant] + list(sirens),
        ).fetchall()
        return {r["siren"] for r in rows}


def mark_pushed_to_hubspot(siren: str, contact_id: str,
                            db_path: Optional[Path] = None,
                            tenant_id: Optional[str] = None) -> None:
    tenant = tenant_id or _current_tenant()
    with _conn(db_path) as c:
        c.execute(
            "UPDATE leads SET pushed_to_hubspot=1, hubspot_contact_id=? "
            "WHERE siren=? AND tenant_id=?",
            (contact_id, siren, tenant),
        )


def list_recent(limit: int = 20, db_path: Optional[Path] = None,
                tenant_id: Optional[str] = None) -> list[dict]:
    """Return the most recently seen leads for the current tenant."""
    tenant = tenant_id or _current_tenant()
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT siren, company_name, person_name, person_email, overall_score, "
            "       last_seen_at, campaigns, pushed_to_hubspot, dropped "
            "FROM leads WHERE tenant_id=? AND gdpr_deleted=0 "
            "ORDER BY last_seen_at DESC LIMIT ?",
            (tenant, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def stats(db_path: Optional[Path] = None,
          tenant_id: Optional[str] = None) -> dict:
    tenant = tenant_id or _current_tenant()
    with _conn(db_path) as c:
        total = c.execute(
            "SELECT COUNT(*) AS n FROM leads WHERE tenant_id=? AND gdpr_deleted=0",
            (tenant,)).fetchone()["n"]
        kept = c.execute(
            "SELECT COUNT(*) AS n FROM leads WHERE tenant_id=? AND dropped=0 AND gdpr_deleted=0",
            (tenant,)).fetchone()["n"]
        pushed = c.execute(
            "SELECT COUNT(*) AS n FROM leads WHERE tenant_id=? AND pushed_to_hubspot=1 AND gdpr_deleted=0",
            (tenant,)).fetchone()["n"]
        campaigns = c.execute(
            "SELECT COUNT(DISTINCT last_campaign_id) AS n FROM leads WHERE tenant_id=?",
            (tenant,)).fetchone()["n"]
    return {"tenant": tenant, "total": total, "kept": kept,
            "pushed_to_hubspot": pushed, "campaigns": campaigns}


# ---------------------------------------------------------------------------
# GDPR / data minimisation
# ---------------------------------------------------------------------------

def delete_for_subject(person_email: str, *,
                       db_path: Optional[Path] = None,
                       tenant_id: Optional[str] = None,
                       hard: bool = False) -> int:
    """Right-to-erasure (GDPR Art. 17).

    Marks every row whose `person_email` matches as `gdpr_deleted=1` (soft
    delete — keeps a stub for de-duplication & audit). When `hard=True`,
    physically removes the row.

    Cross-tenant: by default scoped to `tenant_id`. Pass `tenant_id=None` and
    `hard=True` for an unconditional purge (rare; usually one tenant asks).

    Returns the number of rows affected.
    """
    if not person_email:
        return 0
    person_email = person_email.lower().strip()
    tenant = tenant_id or _current_tenant()
    with _conn(db_path) as c:
        if hard:
            cur = c.execute(
                "DELETE FROM leads WHERE LOWER(person_email)=? AND tenant_id=?",
                (person_email, tenant),
            )
        else:
            # Soft delete: clear personal fields, keep the SIREN+tenant key for
            # future dedup so we never re-prospect this person.
            cur = c.execute(
                """UPDATE leads
                   SET person_name=NULL, person_email=NULL, payload_json='{}',
                       gdpr_deleted=1
                   WHERE LOWER(person_email)=? AND tenant_id=?""",
                (person_email, tenant),
            )
        return cur.rowcount


def purge_older_than(days: int, *,
                     db_path: Optional[Path] = None,
                     tenant_id: Optional[str] = None) -> int:
    """Data minimisation: physically delete leads not seen in `days` days.

    For B2B prospection in France the CNIL recommends ~3 years max retention
    on prospect data unless renewed contact. Default is conservative — caller
    sets the cadence.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    tenant = tenant_id or _current_tenant()
    with _conn(db_path) as c:
        cur = c.execute(
            "DELETE FROM leads WHERE tenant_id=? AND last_seen_at < ?",
            (tenant, cutoff),
        )
        return cur.rowcount


def list_tenants(db_path: Optional[Path] = None) -> list[dict]:
    """Inspect tenants (admin function — bypasses tenant filter intentionally)."""
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT tenant_id, COUNT(*) AS n_leads, "
            "       MAX(last_seen_at) AS most_recent "
            "FROM leads GROUP BY tenant_id ORDER BY n_leads DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def _cli() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Lead store inspector + GDPR ops")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats", help="Print store statistics (current tenant)")
    rl = sub.add_parser("recent", help="List recent leads (current tenant)")
    rl.add_argument("--limit", type=int, default=20)

    sub.add_parser("tenants", help="List all tenants (admin)")

    dl = sub.add_parser("delete", help="GDPR: erase all rows for a person email")
    dl.add_argument("email", help="Person email (case-insensitive)")
    dl.add_argument("--hard", action="store_true",
                     help="Physically delete the row (default: soft delete)")

    pg = sub.add_parser("purge", help="GDPR: hard-delete leads older than N days")
    pg.add_argument("days", type=int, help="Retention window in days")

    args = p.parse_args()

    if args.cmd == "stats":
        print(json.dumps(stats(), indent=2))
    elif args.cmd == "recent":
        for row in list_recent(args.limit):
            print(f"  [{row['overall_score']:3d}] {row['company_name']:30s} | "
                  f"{row['person_name'] or '—':25s} | seen {row['campaigns']}x | "
                  f"{'HS' if row['pushed_to_hubspot'] else '  '} {'DROP' if row['dropped'] else ''}")
    elif args.cmd == "tenants":
        for t in list_tenants():
            print(f"  {t['tenant_id']:20s} | {t['n_leads']:>6d} leads | last seen {t['most_recent']}")
    elif args.cmd == "delete":
        n = delete_for_subject(args.email, hard=args.hard)
        print(f"GDPR delete: {n} row(s) affected for {args.email} (hard={args.hard})")
    elif args.cmd == "purge":
        n = purge_older_than(args.days)
        print(f"Purged {n} lead(s) older than {args.days} days")


if __name__ == "__main__":
    _cli()
