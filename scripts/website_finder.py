"""
Website finder — given a company name (+ optional location), find its
official website via DuckDuckGo search.

Strategy:
1. Build a query: "{company_name}" "{city}" site officiel
2. Take the top non-aggregator result
3. Validate via a HEAD request (status < 400)

Blacklist of aggregators we explicitly skip (they ARE the official "results"
for thousands of small companies but they're not the company's own site):
- pappers.fr, societe.com, infogreffe.fr, verif.com, manageo.fr
- pagesjaunes.fr, hoodspot.fr, scores.io, b-reputation.com
- linkedin.com, facebook.com, instagram.com, twitter.com, x.com, youtube.com
- google.com, doctolib.fr (for medical), tripadvisor.fr, yelp.fr
"""
from __future__ import annotations

import time
from typing import Optional
from urllib.parse import urlparse

import httpx

try:
    from ddgs import DDGS  # type: ignore
except ImportError:  # pragma: no cover
    DDGS = None  # type: ignore

AGGREGATOR_HOSTS = {
    "pappers.fr", "societe.com", "infogreffe.fr", "verif.com", "manageo.fr",
    "pagesjaunes.fr", "hoodspot.fr", "scores.io", "b-reputation.com",
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com",
    "x.com", "youtube.com", "google.com", "google.fr",
    "tripadvisor.fr", "yelp.fr", "yelp.com", "doctolib.fr",
    "wikipedia.org", "fr.wikipedia.org", "amazon.fr", "amazon.com",
}

_LAST_AT = 0.0
_MIN_INTERVAL = 2.5
_HTTP_TIMEOUT = 8.0
_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 prospect-agent/0.2"}


def _throttle() -> None:
    global _LAST_AT
    now = time.monotonic()
    delta = now - _LAST_AT
    if delta < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - delta)
    _LAST_AT = time.monotonic()


def _host(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _looks_alive(url: str) -> bool:
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, headers=_HTTP_HEADERS,
                          follow_redirects=True, verify=False) as c:
            resp = c.head(url)
            if resp.status_code >= 400:
                # some servers reject HEAD; try GET as a fallback
                resp = c.get(url)
            return resp.status_code < 400
    except Exception:
        return False


def find_company_website(name: str, city: Optional[str] = None,
                          *, validate: bool = True) -> Optional[str]:
    """Return the best guess of the company's official website URL, or None."""
    if not name or DDGS is None:
        return None
    bits = [f'"{name}"']
    if city:
        bits.append(f'"{city}"')
    bits.append("site officiel")
    query = " ".join(bits)

    _throttle()
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=10)) or []
    except Exception:
        return None

    for r in results:
        url = r.get("href") or r.get("url") or ""
        if not url:
            continue
        host = _host(url)
        if not host or host in AGGREGATOR_HOSTS:
            continue
        # Skip subdomains of aggregators
        if any(host.endswith("." + agg) for agg in AGGREGATOR_HOSTS):
            continue
        if validate and not _looks_alive(url):
            continue
        # Return the root of the site, not a deep page
        parsed = urlparse(url)
        root = f"{parsed.scheme}://{parsed.hostname}"
        return root

    return None


def _cli() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Discover a company's website.")
    parser.add_argument("name", help="Company name (quote it)")
    parser.add_argument("--city", help="Optional city")
    parser.add_argument("--no-validate", action="store_true",
                        help="Skip HTTP liveness check (faster, less accurate)")
    args = parser.parse_args()
    url = find_company_website(args.name, args.city, validate=not args.no_validate)
    print(url or "(not found)")


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    _cli()
