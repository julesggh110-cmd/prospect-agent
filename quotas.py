"""
Quotas — centralised API-credit tracker.

Why this module exists:
The agent consumes credits across 9 different services, each with its own
free-tier limit and reset period. Without central tracking, an employee can
silently exhaust a free tier mid-campaign and the agent will start returning
empty results — looking broken when it's actually just out of credits.

This module:
1. Records every successful API call in a local SQLite (`data/quotas.db`)
   per (service, period_start, period_type).
2. Knows the FREE-tier limits + RESET periods for every service we use.
3. Provides `remaining(service)` and `can_call(service)` to query before a call.
4. Provides a `summary()` that returns a clean dict for CLI / Multica display.
5. Supports a USER-CONFIGURED `daily_cap_leads` ceiling to prevent burning
   the entire monthly free tier in one run.

CLI:
  python quotas.py                    # human-readable status table
  python quotas.py json               # machine-readable JSON for Multica
  python quotas.py reset <service>    # reset a service's counter (admin)
  python quotas.py set-cap 50         # set daily cap to 50 leads max

Integration:
  from quotas import mark_used, can_call, remaining
  if not can_call("dropcontact"):
      return None
  ...do API call...
  mark_used("dropcontact", count=1)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

DB_DEFAULT = Path(__file__).resolve().parent / "data" / "quotas.db"


# ---------------------------------------------------------------------------
# Service catalogue — limits + reset periods
# Period types:
#   "day"        rolling 24h (resets at UTC midnight)
#   "month"      calendar month (resets 1st of each month)
#   "oneshot"    one-time grant at signup; never resets unless admin does it
# ---------------------------------------------------------------------------

SERVICES: dict[str, dict] = {
    "sirene": {
        "label": "Sirene (api.gouv.fr)",
        "free_limit": 999_999,    # effectively unlimited
        "period": "month",
        "credits_per_lead": 1,
    },
    "pappers": {
        "label": "Pappers (FR companies)",
        "free_limit": 100,
        "period": "day",
        "credits_per_lead": 1,
    },
    "serper": {
        "label": "Serper.dev (Google search)",
        "free_limit": 2500,
        "period": "oneshot",
        "credits_per_lead": 5,     # ~5 queries per lead (website + LinkedIn + Insta)
    },
    "brave_search": {
        "label": "Brave Search",
        "free_limit": 2000,
        "period": "month",
        "credits_per_lead": 5,
    },
    "here_maps": {
        "label": "HERE Maps Places",
        "free_limit": 250_000,
        "period": "month",
        "credits_per_lead": 2,     # 1 geocode (cached) + 1 discover per lead
    },
    "google_places": {              # via Serper /places — counts as Serper too
        "label": "Google Places (via Serper)",
        "free_limit": 0,            # shares Serper quota; tracked for analytics
        "period": "month",
        "credits_per_lead": 0,
    },
    "dropcontact": {
        "label": "Dropcontact",
        "free_limit": 50,
        "period": "oneshot",
        "credits_per_lead": 1,
    },
    "hunter": {
        "label": "Hunter.io",
        "free_limit": 50,           # combined search+verify, ~25 searches
        "period": "month",
        "credits_per_lead": 1,
    },
    "datagma": {
        "label": "Datagma (FR specialist)",
        "free_limit": 50,           # base credits; mobile lookup = 30 credits
        "period": "oneshot",
        "credits_per_lead": 1,      # email only; mobile is 30
    },
    "bettercontact": {
        "label": "BetterContact (waterfall)",
        "free_limit": 50,           # pay-per-valid; only counted when match found
        "period": "oneshot",
        "credits_per_lead": 1,
    },
    "anthropic": {
        "label": "Anthropic Haiku (cold emails)",
        "free_limit": 0,            # no free tier; pay-per-use
        "period": "month",
        "credits_per_lead": 1,      # 1 Haiku call per email generation
    },
    "osm": {
        "label": "OpenStreetMap (Overpass + Nominatim)",
        "free_limit": 10_000,       # ~10k/day per IP
        "period": "day",
        "credits_per_lead": 2,      # 1 geocode + 1 overpass per lead
    },
}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_TABLE = """
CREATE TABLE IF NOT EXISTS api_usage (
    service        TEXT NOT NULL,
    period_start   TEXT NOT NULL,   -- ISO date (YYYY-MM-DD for day, YYYY-MM-01 for month, "oneshot" for oneshot)
    period_type    TEXT NOT NULL,   -- 'day' | 'month' | 'oneshot'
    used           INTEGER NOT NULL DEFAULT 0,
    last_used_at   TEXT,
    PRIMARY KEY (service, period_start, period_type)
);

CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@contextmanager
def _conn(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    p = db_path or DB_DEFAULT
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p)
    c.executescript(_TABLE)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _period_key(period_type: str) -> str:
    now = datetime.now(timezone.utc)
    if period_type == "day":
        return now.strftime("%Y-%m-%d")
    if period_type == "month":
        return now.strftime("%Y-%m-01")
    return "oneshot"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def mark_used(service: str, count: int = 1, db_path: Optional[Path] = None) -> None:
    """Increment the counter for `service` by `count`.

    Called by API clients AFTER a successful request. If the service isn't
    in our catalogue, the call is silently no-op (so adding a new client
    doesn't crash if quotas.py wasn't updated yet).
    """
    if service not in SERVICES:
        return
    period_type = SERVICES[service]["period"]
    period_start = _period_key(period_type)
    now_iso = datetime.now(timezone.utc).isoformat()
    with _conn(db_path) as c:
        existing = c.execute(
            "SELECT used FROM api_usage WHERE service=? AND period_start=? AND period_type=?",
            (service, period_start, period_type),
        ).fetchone()
        if existing:
            c.execute(
                "UPDATE api_usage SET used=used+?, last_used_at=? "
                "WHERE service=? AND period_start=? AND period_type=?",
                (count, now_iso, service, period_start, period_type),
            )
        else:
            c.execute(
                "INSERT INTO api_usage (service, period_start, period_type, used, last_used_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (service, period_start, period_type, count, now_iso),
            )


def used(service: str, db_path: Optional[Path] = None) -> int:
    """Return credits consumed in the current period for `service`."""
    if service not in SERVICES:
        return 0
    period_type = SERVICES[service]["period"]
    period_start = _period_key(period_type)
    with _conn(db_path) as c:
        row = c.execute(
            "SELECT used FROM api_usage WHERE service=? AND period_start=? AND period_type=?",
            (service, period_start, period_type),
        ).fetchone()
        return row["used"] if row else 0


def remaining(service: str, db_path: Optional[Path] = None) -> int:
    """How many credits are left in the current period."""
    if service not in SERVICES:
        return 999_999
    return max(0, SERVICES[service]["free_limit"] - used(service, db_path))


def can_call(service: str, count: int = 1, db_path: Optional[Path] = None) -> bool:
    """Is there enough budget to make `count` more calls right now?"""
    return remaining(service, db_path) >= count


def reset(service: str, db_path: Optional[Path] = None) -> int:
    """Reset (delete) the current-period counter for `service`. Admin only.

    Useful when:
    - You upgraded to a paid plan (your real limit is now higher)
    - You want to start fresh after a free-tier rollover that we missed
    """
    if service not in SERVICES:
        return 0
    period_type = SERVICES[service]["period"]
    period_start = _period_key(period_type)
    with _conn(db_path) as c:
        cur = c.execute(
            "DELETE FROM api_usage WHERE service=? AND period_start=? AND period_type=?",
            (service, period_start, period_type),
        )
        return cur.rowcount


def set_daily_cap(n: int, db_path: Optional[Path] = None) -> None:
    """Set the max number of leads allowed per day (across all services).

    Stored in config table; read by run_campaign to refuse runs that exceed.
    Set to 0 to disable the cap.
    """
    with _conn(db_path) as c:
        c.execute(
            "INSERT OR REPLACE INTO config(key, value) VALUES (?, ?)",
            ("daily_cap_leads", str(int(n))),
        )


def get_daily_cap(db_path: Optional[Path] = None) -> int:
    """Returns the configured daily lead cap, or 0 if disabled."""
    with _conn(db_path) as c:
        row = c.execute("SELECT value FROM config WHERE key='daily_cap_leads'").fetchone()
        try:
            return int(row["value"]) if row else 0
        except Exception:
            return 0


def daily_leads_used(db_path: Optional[Path] = None) -> int:
    """Heuristic: estimate leads processed today.

    We don't track 'leads' directly because the same lead consumes credits
    across many services. The closest proxy is the SERPER counter (5 queries
    per lead, day-tracked-equivalent) since Serper is the dominant per-lead
    consumer.
    """
    # Use the daily count of dropcontact + bettercontact (1 credit per lead each)
    # as a better proxy than serper (which is shared across many sub-tasks).
    today = _period_key("day")
    with _conn(db_path) as c:
        # Count distinct days isn't useful here — just sum today's usage of
        # the per-lead services.
        row = c.execute(
            "SELECT COALESCE(SUM(used), 0) AS s FROM api_usage "
            "WHERE period_start=? AND period_type='day' AND service IN ('dropcontact','bettercontact','datagma','pappers')",
            (today,),
        ).fetchone()
        # Each lead consumes ~1 credit on Pappers; using max(pappers) is the
        # cleanest 'leads' proxy. But if Pappers free-tier daily resets
        # before SQL day rollover, we may miss some — fine for a soft cap.
        return int(row["s"]) if row else 0


def summary(db_path: Optional[Path] = None) -> dict:
    """Return a clean dict of all service usage for CLI / Multica display."""
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "daily_cap_leads": get_daily_cap(db_path),
        "leads_used_today_approx": daily_leads_used(db_path),
        "services": [],
    }
    for service, meta in SERVICES.items():
        u = used(service, db_path)
        limit = meta["free_limit"]
        r = max(0, limit - u)
        pct_used = round(100 * u / limit, 1) if limit > 0 else None
        leads_left = (r // meta["credits_per_lead"]) if meta["credits_per_lead"] > 0 else None
        status = "ok"
        if limit > 0 and pct_used is not None:
            if pct_used >= 90:
                status = "critical"
            elif pct_used >= 70:
                status = "warn"
        out["services"].append({
            "service": service,
            "label": meta["label"],
            "free_limit": limit,
            "used": u,
            "remaining": r,
            "percent_used": pct_used,
            "period": meta["period"],
            "credits_per_lead": meta["credits_per_lead"],
            "leads_capacity": leads_left,
            "status": status,
        })
    # Bottleneck = the service that limits the smallest number of additional
    # leads. We only count services that are ACTUALLY hit per-lead and have
    # a meaningful free limit (>0). Services with free_limit=0 (Anthropic
    # pay-per-use, Google Places via shared Serper) are excluded — they
    # aren't quota-bound for our purposes.
    relevant = [
        s for s in out["services"]
        if s["leads_capacity"] is not None
        and s["free_limit"] > 0
        and s["credits_per_lead"] > 0
    ]
    if relevant:
        bottleneck = min(relevant, key=lambda s: s["leads_capacity"])
        out["bottleneck_service"] = bottleneck["service"]
        out["bottleneck_service_label"] = bottleneck["label"]
        out["bottleneck_leads_remaining"] = bottleneck["leads_capacity"]
    else:
        out["bottleneck_service"] = None
        out["bottleneck_service_label"] = None
        out["bottleneck_leads_remaining"] = None
    return out


# ---------------------------------------------------------------------------
# CLI — table and json output for Multica + employees
# ---------------------------------------------------------------------------

def _format_table(s: dict) -> str:
    """Pretty 1-screen status table with traffic-light icons."""
    lines = []
    lines.append("=" * 80)
    lines.append(f"PROSPECT-AGENT QUOTAS — {s['generated_at']}")
    if s["daily_cap_leads"] > 0:
        lines.append(
            f"  Daily cap: {s['leads_used_today_approx']}/{s['daily_cap_leads']} leads used today"
        )
    else:
        lines.append("  Daily cap: not set (use `python quotas.py set-cap N` to add one)")
    if s["bottleneck_leads_remaining"] is not None:
        lines.append(
            f"  → MAX REMAINING LEADS POSSIBLE: ~{s['bottleneck_leads_remaining']} "
            f"(bottleneck: {s.get('bottleneck_service_label') or s['bottleneck_service']})"
        )
    lines.append("=" * 80)
    lines.append(f"{'Service':<32} {'Used':>8} {'Free':>8} {'Left':>8} {'Period':>8} {'Leads≈':>8} Status")
    lines.append("-" * 80)
    for svc in s["services"]:
        icon = {"ok": "✓", "warn": "⚠", "critical": "✗"}.get(svc["status"], "?")
        leads_cap = "—" if svc["leads_capacity"] is None else str(svc["leads_capacity"])
        free_str = "∞" if svc["free_limit"] >= 999_999 else str(svc["free_limit"])
        lines.append(
            f"{svc['label'][:32]:<32} {svc['used']:>8} {free_str:>8} "
            f"{svc['remaining']:>8} {svc['period']:>8} {leads_cap:>8}  {icon} {svc['status']}"
        )
    lines.append("=" * 80)
    # Action hints
    crit = [s for s in s["services"] if s["status"] == "critical"]
    if crit:
        lines.append("")
        lines.append("⚠  CRITICAL — these services are >90% consumed:")
        for c in crit:
            lines.append(f"   - {c['label']}: {c['used']}/{c['free_limit']} ({c['percent_used']}%)")
            lines.append(f"     → Upgrade or stop campaigns until {c['period']} reset.")
    return "\n".join(lines)


def _cli() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Prospect-agent quota tracker")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("table", help="Pretty status table (default)")
    sub.add_parser("json", help="Machine-readable JSON status")
    r = sub.add_parser("reset", help="Reset a service's current-period counter")
    r.add_argument("service", choices=list(SERVICES.keys()))
    c = sub.add_parser("set-cap", help="Set the daily lead cap (0 to disable)")
    c.add_argument("n", type=int)
    sub.add_parser("get-cap", help="Show the current daily cap")
    sub.add_parser("services", help="List all known services + their free limits")
    args = p.parse_args()
    cmd = args.cmd or "table"

    if cmd == "table":
        print(_format_table(summary()))
    elif cmd == "json":
        print(json.dumps(summary(), indent=2, ensure_ascii=False))
    elif cmd == "reset":
        n = reset(args.service)
        print(f"Reset {args.service}: cleared {n} row(s) for the current period.")
    elif cmd == "set-cap":
        set_daily_cap(args.n)
        print(f"Daily cap set to {args.n} leads (0 = disabled).")
    elif cmd == "get-cap":
        print(get_daily_cap())
    elif cmd == "services":
        for svc, meta in SERVICES.items():
            print(f"  {svc:20s} {meta['label']:35s} {meta['free_limit']:>10}/{meta['period']}")


if __name__ == "__main__":
    _cli()
