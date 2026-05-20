"""
BetterContact — waterfall enrichment over 20+ providers. PAY-PER-VALID.

The unique selling point: you're only charged a credit when a contact comes
back VERIFIED (email passes SMTP/MX checks, mobile passes carrier lookup).
Lookups that find nothing cost 0 credits. So the 50 free-tier credits don't
disappear into junk attempts.

Behind the scenes BetterContact cascades through: Apollo, Lusha, ZoomInfo,
Hunter, Datagma, ContactOut, Kaspr, Dropcontact, etc. Returns the first
verified hit. Their published benchmark: 60-70% email find-rate, 88% phone
discovery — significantly higher than any single source.

Plans (2026):
  Free trial : 50 credits, no card
  Starter    : $15/mo, 200 credits, 10+ data sources
  Pro        : $49/mo, 1 000 credits, 20+ data sources, mobile geo-match

API endpoints:
  POST /api/v1.1/enrich       single enrich (sync)
  POST /api/v1.1/enrich-bulk  batch (async; poll with /jobs/{id})

Setup:
  1. https://app.bettercontact.rocks/sign-up
  2. Settings → API → copy key
  3. .env: BETTERCONTACT_API_KEY=...
"""
from __future__ import annotations

import os
import time
from typing import Optional

import httpx

from http_safe import Throttle

API_BASE = "https://app.bettercontact.rocks/api/v2"
DEFAULT_TIMEOUT = 30.0
_THROTTLE = Throttle(min_interval_s=0.5)
_POLL_INTERVAL_S = 2.0
_MAX_POLL_S = 90.0


def have_bettercontact_key() -> bool:
    return bool(os.environ.get("BETTERCONTACT_API_KEY"))


def _client(timeout: float = DEFAULT_TIMEOUT) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )


def enrich_person(
    first: str,
    last: str,
    company: str,
    *,
    linkedin_url: Optional[str] = None,
    company_domain: Optional[str] = None,
    enrich_email: bool = True,
    enrich_phone: bool = True,
) -> Optional[dict]:
    """Submit one contact + poll until done. Returns flat dict or None.

    Pay-per-valid: this only costs credits if a verified email/phone is found.
    The pipeline calls this AFTER Dropcontact (which may have already returned
    an email) — we use BetterContact to fill the remaining gaps.
    """
    if not have_bettercontact_key() or not first or not last:
        return None
    key = os.environ["BETTERCONTACT_API_KEY"]
    payload: dict = {
        "api_key": key,
        "data": [{
            "first_name": first,
            "last_name": last,
            "company": company or "",
            "linkedin_url": linkedin_url or None,
            "company_domain": company_domain or None,
        }],
        "enrich_email_address": enrich_email,
        "enrich_phone_number": enrich_phone,
    }
    _THROTTLE.acquire()
    try:
        with _client() as c:
            r = c.post(f"{API_BASE}/async", json=payload)
            if r.status_code >= 400:
                return None
            rid = r.json().get("id")
            if not rid:
                return None
            # Poll
            elapsed = 0.0
            while elapsed < _MAX_POLL_S:
                time.sleep(_POLL_INTERVAL_S)
                elapsed += _POLL_INTERVAL_S
                r2 = c.get(f"{API_BASE}/async/{rid}", params={"api_key": key})
                if r2.status_code != 200:
                    continue
                data = r2.json()
                if data.get("status") != "completed":
                    continue
                items = data.get("data") or []
                if not items:
                    return None
                item = items[0]
                email = item.get("enriched_email") or item.get("email")
                phone = (item.get("enriched_phone_number")
                         or item.get("phone_number")
                         or item.get("phone"))
                return {
                    "email": (email or "").lower() or None,
                    "phone": phone,
                    "linkedin": item.get("linkedin_url"),
                    "source": "bettercontact",
                    "providers_tried": item.get("providers_tried"),
                    "providers_matched": item.get("providers_matched"),
                }
    except Exception:
        return None
    return None


def _cli() -> None:
    import argparse
    import json
    import warnings
    warnings.filterwarnings("ignore")
    p = argparse.ArgumentParser(description="BetterContact waterfall enrichment")
    p.add_argument("first")
    p.add_argument("last")
    p.add_argument("company")
    p.add_argument("--domain", help="Company domain (improves accuracy)")
    p.add_argument("--linkedin", help="LinkedIn URL of the person if known")
    p.add_argument("--no-phone", action="store_true")
    args = p.parse_args()
    if not have_bettercontact_key():
        print("BETTERCONTACT_API_KEY not set. Sign up free (50 pay-per-valid credits) at https://app.bettercontact.rocks/sign-up")
        return
    res = enrich_person(
        args.first, args.last, args.company,
        company_domain=args.domain, linkedin_url=args.linkedin,
        enrich_phone=not args.no_phone,
    )
    print(json.dumps(res, indent=2, ensure_ascii=False) if res else "(no match)")


if __name__ == "__main__":
    _cli()
