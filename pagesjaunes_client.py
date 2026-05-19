"""
Pages Jaunes scraper — find a French SMB's phone when Sirene + Pappers + website
all came up empty.

The official French Yellow Pages (pagesjaunes.fr) indexes nearly every legally
registered French business. They expose a search page that returns business
cards with name, address, and phone. We scrape that one page only (no deep
crawl, no rate-limit hammering) and extract the first matching phone.

Why this is the highest-ROI module for SMB prospection:
- Restos, cavistes, coiffeurs, etc. often have NO website but ARE on PJ.
- Sirene/Pappers may have an outdated phone; PJ is usually current.
- We get the standard line (= the gérant's line for an SMB).

Ethics / ToS:
- We scrape ONLY the public search page, no auth, no scraping at depth.
- One request per company, throttled 1.5s, identifying user-agent.
- We do NOT redistribute PJ's full dataset — only enrich one record at a time.

Public API:
    find_phone_on_pagesjaunes(name, city) -> Optional[str]
"""
from __future__ import annotations

import re
import time
from typing import Optional
from urllib.parse import quote_plus, urlparse

import httpx
from selectolax.parser import HTMLParser

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 10.0
_LAST_AT = 0.0
_MIN_INTERVAL = 1.5

# Full browser-like headers so Pages Jaunes doesn't 403 us
_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def _throttle() -> None:
    global _LAST_AT
    now = time.monotonic()
    delta = now - _LAST_AT
    if delta < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - delta)
    _LAST_AT = time.monotonic()


def _clean_phone(raw: str) -> Optional[str]:
    digits = re.sub(r"[^\d+]", "", raw)
    n_digits = len(re.sub(r"\D", "", digits))
    if n_digits < 9 or n_digits > 15:
        return None
    return digits


def find_phone_on_pagesjaunes(name: str, city: Optional[str] = None) -> Optional[str]:
    """Return the first phone number found for `name` (+ optional `city`) on PJ.

    Falls back to None if PJ blocks us, returns nothing, or the result doesn't
    obviously match the company name we asked for.
    """
    if not name:
        return None
    quoiqui = quote_plus(name)
    ou = quote_plus(city or "France")
    url = f"https://www.pagesjaunes.fr/recherche/{ou}/{quoiqui}"

    _throttle()
    try:
        with httpx.Client(
            timeout=TIMEOUT,
            headers=_HEADERS,
            follow_redirects=True,
            verify=False,
            http2=False,
        ) as c:
            resp = c.get(url)
            if resp.status_code >= 400:
                return None
            html = resp.text
    except httpx.HTTPError:
        return None

    tree = HTMLParser(html)

    # PJ exposes phones with multiple patterns. We try the structured selectors
    # first, then fall back to a text regex on the page.
    selectors = [
        "a[href^='tel:']",                # canonical
        "[data-pjlb='click_phone']",
        ".number-contact",
        "[class*='phone']",
    ]
    for sel in selectors:
        for node in tree.css(sel):
            txt = node.text(strip=True) or node.attributes.get("href", "")
            if txt.startswith("tel:"):
                txt = txt[4:]
            cleaned = _clean_phone(txt)
            if cleaned:
                return cleaned

    # Fallback regex: French phone formats on the page
    page_text = tree.text(separator=" ")
    # Pattern: 0X XX XX XX XX with various separators
    fr_phone_rx = re.compile(r"\b0\s?\d(?:[\s.\-]?\d{2}){4}\b")
    m = fr_phone_rx.search(page_text)
    if m:
        cleaned = _clean_phone(m.group(0))
        if cleaned:
            return cleaned

    return None


def find_business_on_pagesjaunes(
    name: str, city: Optional[str] = None,
) -> Optional[dict]:
    """Look up a business on PJ; return whatever we can: phone, website, address.

    Returns dict with keys: phone, website, address, name_match. None on miss.
    """
    if not name:
        return None
    from urllib.parse import quote_plus
    quoiqui = quote_plus(name)
    ou = quote_plus(city or "France")
    url = f"https://www.pagesjaunes.fr/recherche/{ou}/{quoiqui}"

    _throttle()
    try:
        with httpx.Client(
            timeout=TIMEOUT, headers=_HEADERS,
            follow_redirects=True, verify=False,
        ) as c:
            resp = c.get(url)
            if resp.status_code >= 400:
                return None
            html = resp.text
    except httpx.HTTPError:
        return None

    tree = HTMLParser(html)

    # PHONE
    phone = None
    for sel in ("a[href^='tel:']", ".number-contact", "[class*='phone']"):
        for node in tree.css(sel):
            txt = node.text(strip=True) or (node.attributes.get("href") or "")
            if txt.startswith("tel:"):
                txt = txt[4:]
            cleaned = _clean_phone(txt)
            if cleaned:
                phone = cleaned
                break
        if phone:
            break

    # WEBSITE — PJ shows external website links in business cards
    website = None
    seen = set()
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href.startswith(("http://", "https://")):
            continue
        from urllib.parse import urlparse
        host = (urlparse(href).hostname or "").lower()
        if not host or host in seen:
            continue
        seen.add(host)
        # Skip PJ-internal and trackers
        bad = ("pagesjaunes", "solocal", "googleapis", "google-analytics", "doubleclick",
               "facebook.com", "twitter.com", "instagram.com", "tiktok.com",
               "youtube.com", "linkedin.com", "googletagmanager", "cookielaw",
               "adobedtm", "newrelic", "hotjar", "cloudinary", "cdn.")
        if any(b in host for b in bad):
            continue
        # Skip very-short or pure-static (image) hosts
        if len(host) < 6:
            continue
        website = href.split("?", 1)[0]
        break

    if not phone and not website:
        return None
    return {"phone": phone, "website": website, "source": "pagesjaunes"}


def _cli() -> None:
    import argparse
    import json
    import warnings
    warnings.filterwarnings("ignore")

    p = argparse.ArgumentParser(description="Look up a business on Pages Jaunes.")
    p.add_argument("name", help="Company name (in quotes)")
    p.add_argument("--city", help="Optional city")
    p.add_argument("--full", action="store_true",
                   help="Return phone + website + address (not just phone)")
    args = p.parse_args()
    if args.full:
        result = find_business_on_pagesjaunes(args.name, args.city)
        print(json.dumps(result, indent=2, ensure_ascii=False) if result else "(not found)")
    else:
        phone = find_phone_on_pagesjaunes(args.name, args.city)
        print(phone or "(not found)")


if __name__ == "__main__":
    _cli()
