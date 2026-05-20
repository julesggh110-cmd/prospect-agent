"""
Hunter.io — email finder + verifier. Free tier: 25 search + 50 verify per month.

Use cases in our pipeline:
  - find_email_by_domain(first, last, domain)  → 1 credit, returns pattern + confidence
  - verify_email(email)                          → 0.5 credit, returns deliverability

Hunter is COMPLEMENTARY to Dropcontact (different data source). The waterfall
strategy: if Dropcontact returned nothing → try Hunter. Combined hit-rate is
significantly higher than either alone.

Setup:
  1. https://hunter.io/users/sign_up (free, no card)
  2. Dashboard → API → copy your key
  3. .env: HUNTER_API_KEY=...
"""
from __future__ import annotations

import os
from typing import Optional

import httpx

from http_safe import Throttle

API_BASE = "https://api.hunter.io/v2"
DEFAULT_TIMEOUT = 10.0
_THROTTLE = Throttle(min_interval_s=0.3)


def have_hunter_key() -> bool:
    return bool(os.environ.get("HUNTER_API_KEY"))


def find_email_by_domain(first: str, last: str, domain: str) -> Optional[dict]:
    """Hunter's /email-finder endpoint. 1 credit per call.

    Returns: {email, score (0-100), sources [...]} or None.
    """
    if not have_hunter_key() or not first or not last or not domain:
        return None
    key = os.environ["HUNTER_API_KEY"]
    _THROTTLE.acquire()
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as c:
            r = c.get(f"{API_BASE}/email-finder", params={
                "domain": domain,
                "first_name": first,
                "last_name": last,
                "api_key": key,
            })
            if r.status_code != 200:
                return None
            try:
                from quotas import mark_used
                mark_used("hunter")
            except Exception:
                pass
            d = r.json().get("data") or {}
            email = d.get("email")
            if not email:
                return None
            return {
                "email": email.lower(),
                "score": d.get("score"),          # 0-100 confidence Hunter own
                "verification": (d.get("verification") or {}).get("status"),
                "sources": [s.get("uri") for s in (d.get("sources") or [])][:3],
                "source": "hunter",
            }
    except Exception:
        return None


def verify_email(email: str) -> Optional[dict]:
    """Hunter's /email-verifier endpoint. 0.5 credit per call.

    Returns: {status, score, accept_all, ...} or None.
    """
    if not have_hunter_key() or not email:
        return None
    key = os.environ["HUNTER_API_KEY"]
    _THROTTLE.acquire()
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as c:
            r = c.get(f"{API_BASE}/email-verifier", params={
                "email": email,
                "api_key": key,
            })
            if r.status_code != 200:
                return None
            try:
                from quotas import mark_used
                mark_used("hunter")
            except Exception:
                pass
            d = r.json().get("data") or {}
            return {
                "email": email.lower(),
                "status": d.get("status"),              # 'valid', 'invalid', ...
                "score": d.get("score"),                # 0-100
                "accept_all": d.get("accept_all"),
                "regexp": d.get("regexp"),
                "gibberish": d.get("gibberish"),
                "disposable": d.get("disposable"),
                "webmail": d.get("webmail"),
                "mx_records": d.get("mx_records"),
                "smtp_check": d.get("smtp_check"),
                "source": "hunter",
            }
    except Exception:
        return None


def domain_search(domain: str, limit: int = 5) -> Optional[list[dict]]:
    """Hunter's /domain-search — returns known emails at this domain.

    Useful when we know the company has a website but not the person.
    1 credit per call.
    """
    if not have_hunter_key() or not domain:
        return None
    key = os.environ["HUNTER_API_KEY"]
    _THROTTLE.acquire()
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as c:
            r = c.get(f"{API_BASE}/domain-search", params={
                "domain": domain,
                "limit": limit,
                "api_key": key,
            })
            if r.status_code != 200:
                return None
            try:
                from quotas import mark_used
                mark_used("hunter")
            except Exception:
                pass
            d = r.json().get("data") or {}
            emails = d.get("emails") or []
            return [{
                "email": (e.get("value") or "").lower(),
                "first_name": e.get("first_name"),
                "last_name": e.get("last_name"),
                "position": e.get("position"),
                "confidence": e.get("confidence"),
                "type": e.get("type"),
            } for e in emails if e.get("value")]
    except Exception:
        return None


def _cli() -> None:
    import argparse
    import json
    import warnings
    warnings.filterwarnings("ignore")
    p = argparse.ArgumentParser(description="Hunter.io email finder/verifier")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("find")
    f.add_argument("first")
    f.add_argument("last")
    f.add_argument("domain")
    v = sub.add_parser("verify")
    v.add_argument("email")
    d = sub.add_parser("domain")
    d.add_argument("domain")
    d.add_argument("--limit", type=int, default=5)
    args = p.parse_args()
    if not have_hunter_key():
        print("HUNTER_API_KEY not set. Sign up free at https://hunter.io")
        return
    if args.cmd == "find":
        res = find_email_by_domain(args.first, args.last, args.domain)
    elif args.cmd == "verify":
        res = verify_email(args.email)
    elif args.cmd == "domain":
        res = domain_search(args.domain, limit=args.limit)
    print(json.dumps(res, indent=2, ensure_ascii=False) if res else "(no result)")


if __name__ == "__main__":
    _cli()
