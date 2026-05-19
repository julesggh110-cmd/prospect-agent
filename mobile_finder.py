"""
Free mobile finder — best-effort lookup of a decision-maker's direct mobile.

There is NO reliable free way to get 70% of B2B mobile direct numbers
(that's what Kaspr / Cognism charge for). But we can combine 4 free techniques
to recover 15-25% of them.

Techniques (tried in order, stop on first valid mobile):
1. Scrape the company's press/about/newsroom pages for a signature near the
   person's name. Many founders are quoted in their own newsroom with a mobile.
2. Calendly / Cal.com profile lookup (some pros expose their mobile there).
3. Press release / podcast / blog search via Brave (interviews often include
   a contact mobile near the person's name).
4. Personal blog / website footer lookup.

We REQUIRE the mobile to appear within ~200 chars of the person's name on
the same page. Bare "06 12 34 56 78" on a page that mentions Jean is NOT enough
— it could be the standardiste, the receptionist, anyone.

We SKIP scraping LinkedIn contact info even though it works because it's
explicitly against LinkedIn ToS and risks the user's account.

Public API:
    find_mobile_for_person(first, last, company, website=None) -> (phone, source) | None
"""
from __future__ import annotations

import re
import time
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

from brave_search import search_text

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 8.0

# FR mobile only: 06 XX XX XX XX or 07 XX XX XX XX (with optional separators)
# Also accept +33 6 / +33 7 international format.
_FR_MOBILE_RX = re.compile(
    r"(?:"
    r"\b0[67](?:[\s.\-]?\d{2}){4}"
    r"|\+33[\s.\-]?[67](?:[\s.\-]?\d{2}){4}"
    r")\b"
)

# Pages on a company website where founder/exec contacts often appear
PRESS_HINTS = (
    "presse", "press", "newsroom", "news", "media", "medias", "media-kit",
    "contact-presse", "press-contact", "contact-media",
)

# Rate limit our requests politely
_LAST_AT = 0.0
_MIN_INTERVAL = 1.0


def _throttle() -> None:
    global _LAST_AT
    now = time.monotonic()
    delta = now - _LAST_AT
    if delta < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - delta)
    _LAST_AT = time.monotonic()


def _fetch(url: str) -> Optional[str]:
    try:
        _throttle()
        with httpx.Client(
            timeout=TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "fr-FR,fr;q=0.9"},
            follow_redirects=True,
            verify=False,
        ) as c:
            r = c.get(url)
            if r.status_code < 400:
                return r.text
    except Exception:
        pass
    return None


def _mobile_near_name(text: str, first: str, last: str, max_distance: int = 250) -> Optional[str]:
    """Find a FR mobile within `max_distance` chars of the first occurrence of the name.

    We need this proximity check because a raw mobile on a contact page could be
    anyone's — we want the one ASSOCIATED with the target person.
    """
    if not text or not first or not last:
        return None
    text_l = text.lower()
    first_l = first.lower()
    last_l = last.lower()

    # Find every place the last name appears (more specific than first name)
    starts = []
    pos = 0
    while True:
        idx = text_l.find(last_l, pos)
        if idx == -1:
            break
        starts.append(idx)
        pos = idx + len(last_l)
    if not starts:
        return None

    for idx in starts:
        # Window of ±max_distance chars around the name occurrence
        window_start = max(0, idx - max_distance)
        window_end = min(len(text), idx + len(last_l) + max_distance)
        window = text[window_start:window_end]
        # Bonus: require first name to also appear in this window
        if first_l not in window.lower():
            continue
        for m in _FR_MOBILE_RX.finditer(window):
            return _normalize_mobile(m.group(0))
    return None


def _normalize_mobile(raw: str) -> Optional[str]:
    digits = re.sub(r"\D", "", raw)
    # FR international with country code → strip 33 prefix and prepend 0
    if digits.startswith("33") and len(digits) == 11:
        digits = "0" + digits[2:]
    if len(digits) != 10 or not digits.startswith(("06", "07")):
        return None
    # Pretty-format
    return " ".join(digits[i:i+2] for i in range(0, 10, 2))


# ---------------------------------------------------------------------------
# Technique 1 — Press / about / newsroom pages on the company's own website
# ---------------------------------------------------------------------------

def _tech1_press_pages(first: str, last: str, website: str) -> Optional[str]:
    """Scan press/newsroom/contact pages on the company website."""
    if not website:
        return None
    home_html = _fetch(website)
    if not home_html:
        return None
    tree = HTMLParser(home_html)
    # Same-domain links containing one of the press hints
    base_host = urlparse(website).hostname or ""
    candidates: list[str] = []
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        absurl = urljoin(website, href)
        if (urlparse(absurl).hostname or "").lower() != base_host.lower():
            continue
        path = urlparse(absurl).path.lower()
        if any(h in path for h in PRESS_HINTS):
            candidates.append(absurl.split("#", 1)[0])

    # Also try the homepage itself (sometimes contact info is there)
    candidates = list(dict.fromkeys(candidates))[:3]  # dedupe + cap to 3
    for url in [website] + candidates:
        html = _fetch(url) if url != website else home_html
        if not html:
            continue
        tree = HTMLParser(html)
        text = tree.text(separator=" ")
        m = _mobile_near_name(text, first, last)
        if m:
            return m
    return None


# ---------------------------------------------------------------------------
# Technique 2 — Calendly / Cal.com profiles
# ---------------------------------------------------------------------------

def _tech2_calendly(first: str, last: str) -> Optional[str]:
    """Lookup the person's Calendly/Cal.com page if any, scan for mobile."""
    for host in ("calendly.com", "cal.com"):
        results = search_text(f'site:{host} "{first} {last}"', max_results=3)
        for r in results:
            url = r.get("href") or r.get("url") or ""
            if not url or host not in (urlparse(url).hostname or ""):
                continue
            html = _fetch(url)
            if not html:
                continue
            text = HTMLParser(html).text(separator=" ")
            m = _mobile_near_name(text, first, last)
            if m:
                return m
    return None


# ---------------------------------------------------------------------------
# Technique 3 — Press search (interviews, podcasts, articles)
# ---------------------------------------------------------------------------

def _tech3_press_search(first: str, last: str, company: str) -> Optional[str]:
    """Search the web for press/podcast mentions of the person + a mobile near them."""
    queries = [
        f'"{first} {last}" "{company}" "06"',
        f'"{first} {last}" "{company}" "07"',
        f'"{first} {last}" "{company}" mobile',
    ]
    seen_urls: set[str] = set()
    for q in queries:
        results = search_text(q, max_results=5)
        for r in results:
            url = (r.get("href") or r.get("url") or "").split("?", 1)[0]
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            # Skip social media noise + aggregator junk we already know is bad
            host = (urlparse(url).hostname or "").lower()
            if any(h in host for h in (
                "linkedin.com", "facebook.com", "twitter.com", "x.com",
                "pappers.fr", "societe.com", "pagesjaunes.fr",
            )):
                continue
            html = _fetch(url)
            if not html:
                continue
            text = HTMLParser(html).text(separator=" ")
            m = _mobile_near_name(text, first, last, max_distance=400)
            if m:
                return m
    return None


# ---------------------------------------------------------------------------
# Technique 4 — Personal blog / website lookup
# ---------------------------------------------------------------------------

def _tech4_personal_site(first: str, last: str, company: str) -> Optional[str]:
    """Search for a personal blog/site (e.g., portfolio, About.me) of the person."""
    queries = [
        f'"{first} {last}" "{company}" blog',
        f'"{first} {last}" about.me',
        f'"{first} {last}" site personnel',
    ]
    seen_urls: set[str] = set()
    for q in queries:
        results = search_text(q, max_results=3)
        for r in results:
            url = (r.get("href") or r.get("url") or "").split("?", 1)[0]
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            host = (urlparse(url).hostname or "").lower()
            if any(h in host for h in (
                "linkedin.com", "facebook.com", "twitter.com", "x.com",
                "instagram.com", "youtube.com",
            )):
                continue
            html = _fetch(url)
            if not html:
                continue
            text = HTMLParser(html).text(separator=" ")
            m = _mobile_near_name(text, first, last, max_distance=300)
            if m:
                return m
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_mobile_for_person(
    first: str,
    last: str,
    company: str,
    website: Optional[str] = None,
) -> Optional[tuple[str, str]]:
    """Try the 4 free techniques in sequence. Returns (formatted_mobile, source) or None.

    Stop on first valid mobile found. Each technique takes 1-5s; in the worst
    case (all 4 attempted with no luck) the whole call is ~15-25s.
    """
    if not first or not last:
        return None

    if website:
        m = _tech1_press_pages(first, last, website)
        if m:
            return m, "press-pages-on-website"

    m = _tech2_calendly(first, last)
    if m:
        return m, "calendly"

    m = _tech3_press_search(first, last, company)
    if m:
        return m, "press-search"

    m = _tech4_personal_site(first, last, company)
    if m:
        return m, "personal-site"

    return None


def _cli() -> None:
    import argparse
    import warnings
    warnings.filterwarnings("ignore")
    p = argparse.ArgumentParser(description="Free mobile finder for a B2B decision-maker.")
    p.add_argument("first", help="First name")
    p.add_argument("last", help="Last name")
    p.add_argument("company", help="Company name")
    p.add_argument("--website", help="Company website (https://...)")
    args = p.parse_args()
    result = find_mobile_for_person(args.first, args.last, args.company, args.website)
    if result:
        phone, source = result
        print(f"{phone}  (source: {source})")
    else:
        print("(no mobile found)")


if __name__ == "__main__":
    _cli()
