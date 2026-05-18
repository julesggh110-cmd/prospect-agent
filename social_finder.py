"""
Social finder — discover LinkedIn / Instagram URLs via web search.

We DO NOT scrape LinkedIn or Instagram profile pages directly:
- LinkedIn aggressively blocks bots; scraping violates their ToS and can get
  the user's account banned. We only discover the public URL via search.
- Instagram blocks too. We discover URLs only; for richer data the user must
  click through.

Search backend: DuckDuckGo HTML (via the `ddgs` library — no API key required).

Public API:
    find_linkedin_for_person(name, company) -> Optional[str]
    find_linkedin_for_company(company, location=None) -> Optional[str]
    find_instagram_for_company(company, location=None) -> Optional[str]
"""
from __future__ import annotations

import re
import time
from typing import Iterable, Optional
from urllib.parse import urlparse

from brave_search import search_text  # auto-routes Brave→DDG with fallback

# Safety throttle (mostly relevant when falling back to DDG).
_LAST_QUERY_AT = 0.0
_MIN_INTERVAL_S = 1.2


def _throttle() -> None:
    global _LAST_QUERY_AT
    now = time.monotonic()
    delta = now - _LAST_QUERY_AT
    if delta < _MIN_INTERVAL_S:
        time.sleep(_MIN_INTERVAL_S - delta)
    _LAST_QUERY_AT = time.monotonic()


def _ddg_search(query: str, *, max_results: int = 8) -> list[dict]:
    """Run one search via the active backend (Brave or DDG fallback)."""
    _throttle()
    return search_text(query, max_results=max_results)


def _first_matching(results: Iterable[dict], host_pred) -> Optional[str]:
    for r in results:
        url = r.get("href") or r.get("url") or ""
        if not url:
            continue
        host = (urlparse(url).hostname or "").lower()
        if host_pred(host):
            # strip query string / fragment / trailing slash
            return url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return None


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def find_linkedin_for_person(name: str, company: str) -> Optional[str]:
    """Search for `name` LinkedIn profile mentioning `company`. Returns URL or None."""
    if not name or not company:
        return None
    query = f'site:linkedin.com/in/ "{name}" "{company}"'
    results = _ddg_search(query, max_results=5)
    return _first_matching(
        results,
        lambda h: h.endswith("linkedin.com") or h.endswith(".linkedin.com"),
    )


def _company_slug(name: str) -> str:
    """Lowercase ASCII slug for company-name matching against URL paths."""
    import unicodedata
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", "", s)


def find_linkedin_for_company(company: str, location: Optional[str] = None) -> Optional[str]:
    """Find the LinkedIn company page for the given company name.

    Sanity check: the URL slug must contain (a chunk of) the company name.
    Without this filter, DDG sometimes returns wildly unrelated companies.
    """
    if not company:
        return None
    bits = [f'"{company}"', "site:linkedin.com/company/"]
    if location:
        bits.append(f'"{location}"')
    query = " ".join(bits)
    results = _ddg_search(query, max_results=8)

    company_s = _company_slug(company)
    if not company_s:
        return None
    key = company_s[: min(len(company_s), 8)]  # first 8 chars of slug

    for r in results:
        url = r.get("href") or r.get("url") or ""
        if not url:
            continue
        host = (urlparse(url).hostname or "").lower()
        if not (host.endswith("linkedin.com") or host.endswith(".linkedin.com")):
            continue
        path = urlparse(url).path.lower()
        if "/company/" not in path:
            continue
        # The /company/<slug> must include part of our company name
        url_slug = re.sub(r"[^a-z0-9]+", "", path.split("/company/")[-1])
        if key in url_slug or url_slug in company_s:
            return url.split("?", 1)[0].rstrip("/")
    return None


def _valid_instagram_account(url: Optional[str]) -> Optional[str]:
    """Return the URL only if it's a real account page (not home, not a post)."""
    if not url:
        return None
    if re.search(r"instagram\.com/(p|reel|stories|tv|explore)/", url):
        return None
    # Must have a username segment after the domain
    parsed = urlparse(url)
    segments = [s for s in parsed.path.split("/") if s]
    if not segments:
        return None  # root URL like https://www.instagram.com
    # Skip Instagram's own product pages
    if segments[0] in {"about", "accounts", "developer", "directory",
                         "legal", "press", "privacy", "terms"}:
        return None
    return url


def find_instagram_for_company(company: str, location: Optional[str] = None) -> Optional[str]:
    """Find the Instagram account for a company. None if not found."""
    if not company:
        return None
    bits = [f'"{company}"', "site:instagram.com"]
    if location:
        bits.append(f'"{location}"')
    query = " ".join(bits)
    results = _ddg_search(query, max_results=5)
    url = _first_matching(
        results,
        lambda h: h.endswith("instagram.com") or h.endswith(".instagram.com"),
    )
    return _valid_instagram_account(url)


def find_instagram_for_person(name: str, company: Optional[str] = None) -> Optional[str]:
    """Find an Instagram account for a person (best-effort, often empty)."""
    if not name:
        return None
    bits = [f'"{name}"', "site:instagram.com"]
    if company:
        bits.append(f'"{company}"')
    results = _ddg_search(" ".join(bits), max_results=5)
    url = _first_matching(
        results,
        lambda h: h.endswith("instagram.com") or h.endswith(".instagram.com"),
    )
    return _valid_instagram_account(url)


# ---------------------------------------------------------------------------
# CLI for manual testing
# ---------------------------------------------------------------------------

def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Find LinkedIn/Instagram URLs via search.")
    parser.add_argument("kind", choices=["person-linkedin", "company-linkedin",
                                          "company-instagram", "person-instagram"])
    parser.add_argument("query", help="Name (for person) or company name")
    parser.add_argument("--company", help="Company name (for person searches)")
    parser.add_argument("--location", help="Optional location (city) for company searches")
    args = parser.parse_args()

    fn_map = {
        "person-linkedin": lambda: find_linkedin_for_person(args.query, args.company or ""),
        "company-linkedin": lambda: find_linkedin_for_company(args.query, args.location),
        "company-instagram": lambda: find_instagram_for_company(args.query, args.location),
        "person-instagram": lambda: find_instagram_for_person(args.query, args.company),
    }
    result = fn_map[args.kind]()
    print(result or "(not found)")


if __name__ == "__main__":
    _cli()
