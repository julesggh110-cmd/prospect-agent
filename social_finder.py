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

def find_linkedin_for_person(
    name: str,
    company: str,
    *,
    city: Optional[str] = None,
    role: Optional[str] = None,
) -> Optional[str]:
    """Search for `name` LinkedIn profile mentioning `company`. Returns URL or None.

    Multi-query: tries several combinations to maximise hit rate. The first
    `linkedin.com/in/` URL that matches the person's name (in the slug or
    snippet) wins. For FR SMB gérants, the role keyword ("gérant", "président")
    in the query often pushes the right profile to position 1.
    """
    if not name or not company:
        return None

    # Normalize for slug matching at the end. CRITICAL: we strip accents
    # because LinkedIn slugs strip them too ("Hervé Sichel-Dulong" →
    # "herve-sichel-dulong-XXXXX"), and we previously matched against the
    # accented form, causing every accented French name to be rejected.
    import unicodedata
    def _strip_accents(s: str) -> str:
        return "".join(c for c in unicodedata.normalize("NFKD", s)
                       if not unicodedata.combining(c))

    name_ascii = _strip_accents(name)
    name_tokens = [t for t in re.split(r"\s+", name_ascii) if t]
    name_slug_check = re.sub(r"[^a-z]+", "", name_ascii.lower())

    # Generate query variants — most specific first
    bits_base = f'"{name}" "{company}"'
    queries = [
        f'site:linkedin.com/in {bits_base}',                # exact match both
        f'site:linkedin.com/in "{name}" {company}',         # company unquoted
        f'site:linkedin.com/in "{name}"' + (f' "{city}"' if city else ""),
        f'"{name}" {company} linkedin',                     # broader, hope LI URL in results
    ]
    if role:
        queries.insert(1, f'site:linkedin.com/in "{name}" "{role}"')

    seen_urls: set[str] = set()
    for q in queries:
        results = _ddg_search(q, max_results=6)
        for r in results:
            url = r.get("href") or r.get("url") or ""
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            host = (urlparse(url).hostname or "").lower()
            if not (host.endswith("linkedin.com") or host.endswith(".linkedin.com")):
                continue
            path = urlparse(url).path.lower()
            if "/in/" not in path:
                continue
            # Sanity: the slug must contain the person's name.
            # Strip accents from the slug FIRST (URL-decoded LinkedIn slugs
            # like "hervé-sichel-dulong" should compare against "herve...").
            raw_slug = path.split("/in/")[-1].split("/")[0]
            slug_ascii = _strip_accents(raw_slug)
            slug = re.sub(r"[^a-z]+", "", slug_ascii.lower())
            if not name_slug_check:
                return url.split("?", 1)[0].rstrip("/")

            # Distinguishing tokens — only keep ones that are long enough to
            # rule out common-name false-positives (e.g. "alain-navarre"
            # matching "Alain Audiau" because "alain" alone is in the slug).
            distinguishing = [t.lower() for t in name_tokens if len(t) >= 4]
            if not distinguishing:
                # Name was too short/non-distinguishing — skip
                continue

            # STRICT match: require ALL distinguishing tokens to appear in slug.
            # This kills "alain-navarre" matching "Alain Audiau" because "audiau"
            # is missing. For multi-word last names like "Sichel-Dulong", we also
            # accept matching the joined form ("sicheldulong").
            all_present = all(t in slug for t in distinguishing)
            joined_present = name_slug_check in slug or slug in name_slug_check

            if all_present or joined_present:
                return url.split("?", 1)[0].rstrip("/")
    return None


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


def find_instagram_for_person(
    name: str,
    company: Optional[str] = None,
    *,
    city: Optional[str] = None,
) -> Optional[str]:
    """Find an Instagram account for a person (best-effort, often empty).

    Tries a few query variants — bare name + city is often the only one that
    surfaces hospitality chefs' personal accounts.
    """
    if not name:
        return None
    queries = []
    if company:
        queries.append(f'"{name}" "{company}" site:instagram.com')
    queries.append(f'"{name}" {city or ""} site:instagram.com'.strip())
    queries.append(f'"{name}" chef site:instagram.com')

    seen_urls: set[str] = set()
    for q in queries:
        results = _ddg_search(q, max_results=5)
        for r in results:
            url = r.get("href") or r.get("url") or ""
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            host = (urlparse(url).hostname or "").lower()
            if not (host.endswith("instagram.com") or host.endswith(".instagram.com")):
                continue
            valid = _valid_instagram_account(url)
            if valid:
                return valid
    return None


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
