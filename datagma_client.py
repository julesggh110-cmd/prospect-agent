"""
Datagma — French B2B enrichment specialist. 50 free credits + 160 free API matches.

Strength: built BY French folks FOR French B2B, 95% accuracy on FR contacts
(per their Feb-2026 benchmark vs ContactOut's 92%). Particularly strong on
French SMB gérants where US-built tools (Apollo, ZoomInfo) are weak.

Credit model:
  - 1 credit = 1 email lookup
  - 30 credits = 1 mobile phone lookup
  - 160 API matches free (separate from credits — same idea, lookup quota)

Plans (2026):
  Free       : 50 credits + 160 API matches  (no card)
  Discover   : $39/mo, 1 000 credits (~33 mobiles)
  Growth     : $99/mo, 5 000 credits

API endpoints used:
  POST /full-finder            person enrichment (email + mobile + linkedin)
  POST /email-finder           email-only (1 credit)

Setup:
  1. https://app.datagma.com → sign up free
  2. Settings → API → copy your token
  3. .env: DATAGMA_API_KEY=...
"""
from __future__ import annotations

import os
from typing import Optional

import httpx

from http_safe import Throttle

API_BASE = "https://gateway.datagma.net"
DEFAULT_TIMEOUT = 15.0
_THROTTLE = Throttle(min_interval_s=0.3)


def have_datagma_key() -> bool:
    return bool(os.environ.get("DATAGMA_API_KEY"))


def find_full(first: str, last: str, company: str,
              *, want_phone: bool = True) -> Optional[dict]:
    """Datagma /full-finder — returns email + mobile + LinkedIn from name + company.

    Cost: 1 credit + 30 credits if a mobile is returned and want_phone=True.
    On the free plan you get ~33 mobile lookups before running out.
    """
    if not have_datagma_key() or not first or not last or not company:
        return None
    key = os.environ["DATAGMA_API_KEY"]
    _THROTTLE.acquire()
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as c:
            r = c.get(f"{API_BASE}/api/v2/full", params={
                "apiId": key,
                "firstName": first,
                "lastName": last,
                "company": company,
                "phone": "true" if want_phone else "false",
            })
            if r.status_code != 200:
                return None
            d = r.json()
            # Datagma's response is nested; flatten what we use.
            person = d.get("person") or {}
            email = person.get("email") or (d.get("email") or {}).get("email")
            phones = person.get("phones") or []
            mobile = None
            for ph in phones:
                if ph.get("type", "").lower() == "mobile" and ph.get("number"):
                    mobile = ph["number"]
                    break
            if not mobile and phones:
                # Fall back to whatever phone is there
                mobile = phones[0].get("number")
            linkedin = person.get("linkedinUrl") or person.get("linkedin")
            if not (email or mobile or linkedin):
                return None
            return {
                "email": (email or "").lower() or None,
                "phone": mobile,
                "linkedin": linkedin,
                "raw_person": person,
                "source": "datagma",
            }
    except Exception:
        return None


def find_email(first: str, last: str, company: str) -> Optional[str]:
    """Lower-cost variant: just the email (1 credit, no mobile lookup)."""
    res = find_full(first, last, company, want_phone=False)
    return res.get("email") if res else None


def _cli() -> None:
    import argparse
    import json
    import warnings
    warnings.filterwarnings("ignore")
    p = argparse.ArgumentParser(description="Datagma person enrichment")
    p.add_argument("first")
    p.add_argument("last")
    p.add_argument("company")
    p.add_argument("--no-phone", action="store_true", help="Skip the 30-credit mobile lookup")
    args = p.parse_args()
    if not have_datagma_key():
        print("DATAGMA_API_KEY not set. Sign up free (50 credits + 160 API) at https://app.datagma.com")
        return
    res = find_full(args.first, args.last, args.company, want_phone=not args.no_phone)
    print(json.dumps(res, indent=2, ensure_ascii=False) if res else "(no match)")


if __name__ == "__main__":
    _cli()
