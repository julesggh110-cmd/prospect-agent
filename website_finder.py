"""
Website finder — given a company name (+ optional location), find its
official website via DuckDuckGo search.

Strategy:
1. Build a query: "{company_name}" "{city}" site officiel
2. Score every candidate: penalise aggregators, bonus for company-name match
3. FETCH the top candidate and VERIFY it's actually a FR business site
   (mentions the city, has +33/0X phone, has SIRET/RCS, etc.)
4. Return the best-scoring result that passes FR-validation (or None)

This is the highest-leverage module for accuracy — picking the wrong website
poisons the whole pipeline (wrong emails, wrong contact info). Without
FR-validation, "L'Escargot Toulouse" was matching `lescargot.co.uk` (UK!)
and "MOOD Toulouse" was matching `mood.com` (US).
"""
from __future__ import annotations

import re
import time
import unicodedata
from typing import Optional
from urllib.parse import urlparse

import httpx
from selectolax.parser import HTMLParser

from brave_search import search_text  # auto-routes Brave→DDG with fallback

# Domains that aggregate companies but aren't the official site.
# Anything ending in these (incl. subdomains) is rejected.
AGGREGATOR_HOSTS = {
    # FR business registries / scoring
    "pappers.fr", "societe.com", "infogreffe.fr", "verif.com", "manageo.fr",
    "scores.io", "b-reputation.com", "verif-siren.com", "kompass.com",
    "kompass.fr", "annuaire-des-entreprises.fr", "data.gouv.fr",
    "recherche-entreprises.api.gouv.fr",
    # FR local directories
    "pagesjaunes.fr", "hoodspot.fr", "pages-jaunes.fr", "118712.fr",
    "118000.fr", "annuaire.fr", "yellowpages.fr",
    "business-directory.fr", "europages.fr", "europages.com",
    # social networks
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com",
    "x.com", "youtube.com", "tiktok.com", "pinterest.com",
    "snapchat.com", "threads.net",
    # search / portals
    "google.com", "google.fr", "bing.com", "yahoo.com", "yahoo.fr",
    "duckduckgo.com", "qwant.com",
    # generic platforms
    "wikipedia.org", "fr.wikipedia.org", "amazon.fr", "amazon.com",
    "ebay.fr", "leboncoin.fr",
    # vertical aggregators (medical, restaurant, etc.)
    "doctolib.fr", "doctolib.com", "lemedecin.fr", "ameli.fr",
    "tripadvisor.fr", "tripadvisor.com", "yelp.fr", "yelp.com",
    "lafourchette.com", "thefork.fr", "thefork.com",
    "trustpilot.com", "fr.trustpilot.com", "avis-verifies.com",
    # press / news (rarely the official site)
    "lesechos.fr", "lefigaro.fr", "lemonde.fr", "20minutes.fr",
    "challenges.fr", "capital.fr", "bfmtv.com",
    # github / dev
    "github.com", "gitlab.com", "bitbucket.org", "stackoverflow.com",
}

_LAST_AT = 0.0
_MIN_INTERVAL = 1.5
_HTTP_TIMEOUT = 6.0
_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


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


def _slug(s: str) -> str:
    """Lowercase ASCII slug — for fuzzy matching company name in domains."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", "", s)


def _is_aggregator(host: str) -> bool:
    if host in AGGREGATOR_HOSTS:
        return True
    return any(host.endswith("." + agg) for agg in AGGREGATOR_HOSTS)


def _score_candidate(url: str, company_name: str, city: Optional[str]) -> int:
    """Score a URL as a likely official site. Higher = better."""
    host = _host(url)
    if not host:
        return -100
    if _is_aggregator(host):
        return -100

    score = 0
    company_slug = _slug(company_name)
    host_slug = _slug(host.split(".")[0])  # second-level domain only

    # Strong signal: company name fully present in domain
    if company_slug and host_slug.startswith(company_slug[:12]):
        score += 60
    elif company_slug and len(company_slug) >= 4 and company_slug[:4] in host_slug:
        score += 30
    # Common French TLD
    if host.endswith(".fr"):
        score += 8
    elif host.endswith((".com", ".net", ".org", ".io", ".co")):
        score += 5
    # Penalty for very long domains (often subdomains/affiliates)
    if len(host) > 30:
        score -= 15
    # Penalty for hyphens (often affiliate / SEO domains)
    score -= host.count("-") * 3
    # Penalty for digits (rarely official)
    if re.search(r"\d", host_slug):
        score -= 5
    # Penalty for deep URL paths (we want a homepage)
    path = urlparse(url).path or "/"
    if path.count("/") > 2:
        score -= 5
    # Slight bonus if city appears in URL (small businesses)
    if city and _slug(city) in url.lower():
        score += 5
    return score


def _looks_alive(url: str) -> bool:
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, headers=_HTTP_HEADERS,
                          follow_redirects=True, verify=False) as c:
            resp = c.head(url)
            if resp.status_code >= 400:
                resp = c.get(url)
            return resp.status_code < 400
    except Exception:
        return False


# Regexes for FR-context detection on a candidate page.
_FR_PHONE_RX = re.compile(r"(?:\+33[\s.\-]?\d|\b0\d)(?:[\s.\-]?\d{2}){4}\b")
_US_PHONE_RX = re.compile(r"\+1[\s.\-]?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b")
_SIRET_RX = re.compile(r"\b\d{14}\b")
_SIREN_RX = re.compile(r"\b\d{9}\b")
_FR_LEGAL_RX = re.compile(
    r"mentions[\s\-]l[ée]gales|siren|siret|r\.?c\.?s\.?|tva\s+intracommunautaire|"
    r"num[ée]ro\s+t\.?v\.?a\.?",
    re.IGNORECASE,
)

# Top-level domains that strongly suggest a foreign business.
_FOREIGN_TLDS = (
    ".co.uk", ".uk", ".us", ".ca", ".au", ".com.au", ".de", ".es", ".it",
    ".nl", ".be", ".pt", ".ru", ".cn", ".jp", ".kr", ".in", ".br", ".mx",
)


def _is_fr_business_site(url: str, city: Optional[str] = None,
                          *, must_match_city: bool = False) -> tuple[bool, str]:
    """Fetch the URL and verify it looks like a French business site.

    Returns (is_fr, reason) — the reason string is useful for logging.

    A site is FR if at least ONE of:
      - hostname ends in .fr
      - page contains a FR phone (+33 or 0X XX XX XX XX)
      - page contains SIRET / SIREN / RCS / TVA mention
      - page contains the city name (if provided AND must_match_city=True)
      - page has 'Mentions Légales' link

    A site is REJECTED (returns False) if:
      - hostname is in _FOREIGN_TLDS AND no FR phone / SIRET on page
      - page contains a US phone format AND no FR phone
      - if must_match_city: the city does NOT appear on the page
    """
    host = _host(url)

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, headers=_HTTP_HEADERS,
                          follow_redirects=True, verify=False) as c:
            r = c.get(url)
            if r.status_code >= 400:
                return False, f"http {r.status_code}"
            html = r.text or ""
    except Exception as e:
        return False, f"fetch_error {type(e).__name__}"

    if len(html) < 300:
        return False, "page too short"

    # Strip diacritics + lowercase for matching
    low = unicodedata.normalize("NFKD", html.lower())
    low = "".join(c for c in low if not unicodedata.combining(c))

    # --- POSITIVE FR signals ---
    fr_signals: list[str] = []
    if host.endswith(".fr"):
        fr_signals.append("tld:.fr")
    if _FR_PHONE_RX.search(html):
        fr_signals.append("fr-phone")
    if _FR_LEGAL_RX.search(html):
        fr_signals.append("fr-legal")
    if "mentions legales" in low or "mentions-legales" in low:
        fr_signals.append("mentions-legales")
    city_present = bool(city) and _slug(city) in _slug(html[:6000])
    if city_present:
        fr_signals.append(f"city:{city}")

    # --- NEGATIVE / FOREIGN signals ---
    foreign_signals: list[str] = []
    if any(host.endswith(t) for t in _FOREIGN_TLDS):
        foreign_signals.append(f"tld:{host}")
    if _US_PHONE_RX.search(html) and not _FR_PHONE_RX.search(html):
        foreign_signals.append("us-phone-only")
    # Hard country mentions in the address area (footer / legal)
    for marker in (
        "united kingdom", "united states", "usa,", "u.s.a.",
        ", canada", "australia", "deutschland", "españa",
    ):
        if marker in low:
            foreign_signals.append(f"foreign:{marker.strip(', .')}")
            break

    # Decision:
    # - must_match_city forces city presence
    if must_match_city and not city_present:
        return False, f"city {city!r} not on page; signals={fr_signals or 'none'}"
    # - any foreign signal + no FR rebuttal = reject
    if foreign_signals and not fr_signals:
        return False, f"foreign signals {foreign_signals}, no FR rebuttal"
    # - foreign TLD but FR phone present → accept (FR business with .com)
    if fr_signals:
        return True, f"fr signals: {fr_signals}"
    # - no signal either way: be conservative — reject unless very common .com
    if host.endswith(".com") and not foreign_signals:
        return True, "neutral .com, no foreign signals"
    return False, f"no FR signals (signals={fr_signals}, foreign={foreign_signals})"


_WEBSITE_CACHE: dict[str, Optional[str]] = {}


def _query_search(query: str, max_results: int = 10) -> list[dict]:
    # brave_search.search_text has its own throttle when using Brave; the local
    # _throttle is kept only as a safety net for DDG fallback. Brave's wrapper
    # handles its own 1s pacing, so we don't double-sleep when it's active.
    _throttle()
    return search_text(query, max_results=max_results)


def find_company_website(
    name: str,
    city: Optional[str] = None,
    *,
    validate: bool = True,
    min_score: int = 25,
    strict_fr: bool = True,
) -> Optional[str]:
    """Return the best guess of the company's official website URL, or None.

    `min_score`: refuse to return anything below this URL-structure score.
    `strict_fr`: when True (default), FETCH the candidate and verify it looks
        like a French business site (TLD .fr, FR phone, city mention, SIRET, etc.).
        This kills the "MOOD Toulouse → mood.com (US)" and
        "L'Escargot Toulouse → lescargot.co.uk (UK)" false positives.

    Strategy: build several queries (with and without city / with "site officiel")
    and merge candidates. Caches the result per (name, city) for the process
    lifetime to avoid re-querying when Claude iterates.
    """
    if not name:
        return None
    cache_key = f"{name}|{city or ''}|{min_score}|{int(validate)}|{int(strict_fr)}"
    if cache_key in _WEBSITE_CACHE:
        return _WEBSITE_CACHE[cache_key]

    queries = [f'"{name}"']
    if city:
        queries.append(f'"{name}" "{city}"')
    queries.append(f'"{name}" official website')

    scored: dict[str, int] = {}
    for q in queries:
        for r in _query_search(q, max_results=10):
            url = r.get("href") or r.get("url") or ""
            if not url:
                continue
            s = _score_candidate(url, name, city)
            if s > 0:
                # keep highest score across queries
                key = url
                if s > scored.get(key, -999):
                    scored[key] = s
        # stop early if we already have a high-quality candidate
        if scored and max(scored.values()) >= 60:
            break

    sorted_urls = sorted(scored.items(), key=lambda kv: kv[1], reverse=True)
    for url, s in sorted_urls:
        if s < min_score:
            break
        if validate and not _looks_alive(url):
            continue
        # STRICT FR validation: actually fetch the page and verify FR signals.
        # When city is provided, REQUIRE the city name to appear on the page —
        # this is the cheapest, most reliable way to reject foreign squatters
        # that share the brand name (mood.com US, lescargot.co.uk UK, etc.).
        if strict_fr:
            is_fr, _reason = _is_fr_business_site(
                url, city, must_match_city=bool(city),
            )
            if not is_fr:
                continue
        parsed = urlparse(url)
        result = f"{parsed.scheme}://{parsed.hostname}"
        _WEBSITE_CACHE[cache_key] = result
        return result

    _WEBSITE_CACHE[cache_key] = None
    return None


def _cli() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Discover a company's website.")
    parser.add_argument("name", help="Company name (quote it)")
    parser.add_argument("--city", help="Optional city")
    parser.add_argument("--no-validate", action="store_true",
                        help="Skip HTTP liveness check (faster, less accurate)")
    parser.add_argument("--min-score", type=int, default=25,
                        help="Minimum score to accept (default 25)")
    parser.add_argument("--debug", action="store_true",
                        help="Show all candidate scores")
    args = parser.parse_args()

    if args.debug:
        query = f'"{args.name}"' + (f' "{args.city}"' if args.city else "")
        results = _query_search(query, max_results=10)
        print(f"--- {len(results)} candidates ---")
        for r in results:
            url = r.get("href") or r.get("url") or ""
            print(f"  {_score_candidate(url, args.name, args.city):+4d}  {url}")
        return

    url = find_company_website(args.name, args.city,
                                validate=not args.no_validate,
                                min_score=args.min_score)
    print(url or "(not found)")


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    _cli()
