"""
Dropcontact — French B2B contact enrichment via REST API.

Best free path (May 2026) to get personal email + business phone for FR SMB
gérants once we know the (firstname, lastname, company) tuple from Sirene.

Free tier: 50 credits one-shot at signup, no card required. Each credit = one
enriched contact (email + phone returned together). Beyond that, €24/mo for
1000 credits.

Architecture:
- POST {email, first_name, last_name, website?} to /batch → request_id
- Poll GET /batch/{request_id} every 5s until status == "done"
- Parse response: returns email(s) + qualified emails + phone(s)

Strengths for FR market:
- 100% GDPR compliant (zero database, real-time generation)
- 54.9% effective enrichment rate (Feb 2026 benchmark, 20k contacts)
- 0.9% hard-bounce rate (vs ~10% for catch-all SMTP guesses)

Setup (3 min):
  1. https://www.dropcontact.com/signup (email + password, no card)
  2. Dashboard → API → copy key
  3. Add to .env: DROPCONTACT_API_KEY=...

Public API:
    enrich_person(first, last, company, website=None) -> Optional[dict]
        returns {
            "email": "...",
            "email_qualification": "nominative_email" | "pro_email" | ...,
            "phone": "...",
            "linkedin": "...",       # sometimes returned
            "company_website": "...",
            "company_siren": "...",  # Pappers-style, sometimes returned
            ...
        }
"""
from __future__ import annotations

import os
import time
from typing import Optional

import httpx

API_BASE = "https://api.dropcontact.io"
DEFAULT_TIMEOUT = 15.0
# Polite throttle — Dropcontact processes async so we mostly wait on polling.
_MIN_INTERVAL_S = 0.3
_LAST_AT = 0.0
# Polling: shorter interval = faster turnaround, especially in batch mode
# where many leads finish on the SAME poll. Dropcontact tolerates ~30 polls/min.
_POLL_INTERVAL_S = 1.5
_MAX_POLL_S = 60.0


def _throttle() -> None:
    global _LAST_AT
    now = time.monotonic()
    delta = now - _LAST_AT
    if delta < _MIN_INTERVAL_S:
        time.sleep(_MIN_INTERVAL_S - delta)
    _LAST_AT = time.monotonic()


def have_dropcontact_key() -> bool:
    return bool(os.environ.get("DROPCONTACT_API_KEY"))


def _client(timeout: float = DEFAULT_TIMEOUT) -> httpx.Client:
    key = os.environ.get("DROPCONTACT_API_KEY") or ""
    return httpx.Client(
        timeout=timeout,
        headers={
            "X-Access-Token": key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )


def _submit_batch(rows: list[dict]) -> Optional[str]:
    """Submit a batch enrichment request. Returns the request_id, or None."""
    _throttle()
    try:
        with _client() as c:
            r = c.post(
                f"{API_BASE}/batch",
                json={
                    "data": rows,
                    "siren": True,   # ask for Sirene cross-check (FR boost)
                    "language": "fr",
                },
            )
            if r.status_code >= 400:
                return None
            data = r.json()
            # Mark quota: 1 credit per ROW (not per batch). Even if the row
            # returns nothing, Dropcontact counts it.
            try:
                from quotas import mark_used
                mark_used("dropcontact", count=len(rows))
            except Exception:
                pass
            return data.get("request_id")
    except Exception:
        return None


def _fetch_batch(request_id: str) -> Optional[dict]:
    """Fetch a batch result by ID. Returns the response JSON or None."""
    _throttle()
    try:
        with _client() as c:
            r = c.get(f"{API_BASE}/batch/{request_id}")
            if r.status_code >= 400:
                return None
            return r.json()
    except Exception:
        return None


def _poll_until_done(request_id: str) -> Optional[dict]:
    """Poll the batch endpoint until status == 'done' or we time out."""
    elapsed = 0.0
    while elapsed < _MAX_POLL_S:
        data = _fetch_batch(request_id)
        if data is None:
            return None
        # Dropcontact uses success: bool + data: [...] on done
        if data.get("success") is True and data.get("data"):
            return data
        # Or sometimes returns reason: "Enrichment in progress" while waiting
        time.sleep(_POLL_INTERVAL_S)
        elapsed += _POLL_INTERVAL_S
    return None


def enrich_person(
    first: str,
    last: str,
    company: str,
    *,
    website: Optional[str] = None,
) -> Optional[dict]:
    """Enrich one person via Dropcontact. Returns a flat dict or None on failure.

    Cost: 1 credit per request (returns email + phone together when found).

    On the FREE plan, you have 50 credits → enough to test ~50 leads.
    """
    if not have_dropcontact_key():
        return None
    if not first or not last or not company:
        return None

    row: dict = {
        "first_name": first,
        "last_name": last,
        "company": company,
    }
    if website:
        row["website"] = website

    rid = _submit_batch([row])
    if not rid:
        return None
    data = _poll_until_done(rid)
    if not data:
        return None

    items = data.get("data") or []
    if not items:
        return None
    item = items[0]

    # Dropcontact returns a flat structure with email, phone, qualifications
    emails = item.get("email") or []
    # `email` is a list of {email, qualification} dicts when found
    primary_email: Optional[str] = None
    email_qual: Optional[str] = None
    if emails and isinstance(emails, list):
        # Prefer nominative (firstname.lastname@) over pro / generic
        nominative = [e for e in emails if (e.get("qualification") or "").startswith("nominative")]
        chosen = (nominative or emails)[0]
        primary_email = (chosen.get("email") or "").lower() or None
        email_qual = chosen.get("qualification")

    phone: Optional[str] = None
    phones = item.get("phone") or []
    if isinstance(phones, list) and phones:
        # phones is a list of strings or {"number": ...}
        first_phone = phones[0]
        if isinstance(first_phone, dict):
            phone = first_phone.get("number") or first_phone.get("phone")
        else:
            phone = str(first_phone)
    elif isinstance(phones, str):
        phone = phones

    return {
        "email": primary_email,
        "email_qualification": email_qual,
        "phone": phone,
        "linkedin": item.get("linkedin"),
        "company_website": item.get("website"),
        "company_siren": item.get("siren"),
        "company_phone": item.get("company_phone"),
        "company_email": (
            (item.get("company_email") or [{}])[0].get("email")
            if isinstance(item.get("company_email"), list)
            else None
        ),
        "source": "dropcontact",
    }


def enrich_batch(
    rows: list[dict],
) -> dict[tuple[str, str, str], dict]:
    """Enrich up to ~250 contacts in a SINGLE Dropcontact API call.

    Each row must have keys: 'first', 'last', 'company', and optionally 'website'.
    Returns a dict keyed by (first.lower(), last.lower(), company.lower()) pointing
    to the same flat result shape as enrich_person().

    Why batch matters: the polling phase (~10-30s of wall-time per request)
    is amortised across ALL leads. Sequential: 10 leads × 20s polling = 200s.
    Batch:      10 leads × 1 submit + 1 poll = ~20s total. **10× speedup**
    on a 10-lead campaign.

    Dropcontact API supports up to 250 rows per batch (per their docs).
    """
    if not have_dropcontact_key() or not rows:
        return {}

    # Build API rows + remember the lookup key for each
    api_rows: list[dict] = []
    keys: list[tuple[str, str, str]] = []
    for r in rows:
        first = (r.get("first") or "").strip()
        last = (r.get("last") or "").strip()
        company = (r.get("company") or "").strip()
        if not first or not last or not company:
            continue
        row: dict = {"first_name": first, "last_name": last, "company": company}
        if r.get("website"):
            row["website"] = r["website"]
        api_rows.append(row)
        keys.append((first.lower(), last.lower(), company.lower()))

    if not api_rows:
        return {}

    rid = _submit_batch(api_rows)
    if not rid:
        return {}
    data = _poll_until_done(rid)
    if not data:
        return {}

    items = data.get("data") or []
    out: dict[tuple[str, str, str], dict] = {}
    for k, item in zip(keys, items):
        # Reuse the same parser as enrich_person()
        emails = item.get("email") or []
        primary_email: Optional[str] = None
        email_qual: Optional[str] = None
        if emails and isinstance(emails, list):
            nominative = [e for e in emails if (e.get("qualification") or "").startswith("nominative")]
            chosen = (nominative or emails)[0]
            primary_email = (chosen.get("email") or "").lower() or None
            email_qual = chosen.get("qualification")
        phone: Optional[str] = None
        phones = item.get("phone") or []
        if isinstance(phones, list) and phones:
            first_phone = phones[0]
            if isinstance(first_phone, dict):
                phone = first_phone.get("number") or first_phone.get("phone")
            else:
                phone = str(first_phone)
        elif isinstance(phones, str):
            phone = phones
        out[k] = {
            "email": primary_email,
            "email_qualification": email_qual,
            "phone": phone,
            "linkedin": item.get("linkedin"),
            "company_website": item.get("website"),
            "company_siren": item.get("siren"),
            "company_phone": item.get("company_phone"),
            "company_email": (
                (item.get("company_email") or [{}])[0].get("email")
                if isinstance(item.get("company_email"), list)
                else None
            ),
            "source": "dropcontact",
        }
    return out


def _cli() -> None:
    import argparse
    import json
    import warnings
    warnings.filterwarnings("ignore")

    p = argparse.ArgumentParser(description="Test Dropcontact person enrichment.")
    p.add_argument("first")
    p.add_argument("last")
    p.add_argument("company")
    p.add_argument("--website", help="Optional company website")
    args = p.parse_args()

    if not have_dropcontact_key():
        print("DROPCONTACT_API_KEY not set — sign up free at https://www.dropcontact.com/signup")
        return
    result = enrich_person(args.first, args.last, args.company, website=args.website)
    if not result:
        print("(no enrichment returned — could be credit limit or no match)")
        return
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
