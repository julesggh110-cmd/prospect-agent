"""
Mentions Légales scraper — the highest-ROI free source for FR SMB owner contacts.

French law (LCEN 2004) REQUIRES every commercial website to publish a "Mentions
légales" page containing:
- Editor name (= dirigeant or company)
- Publication director (often the gérant by name)
- Contact (often a direct email or phone)
- RCS registration, SIRET, share capital, host

Penalty for non-compliance: up to €75,000 (individual) or €375,000 (company).
Compliance is VERY high — most legit FR sites have this page.

The page is usually at one of these URLs:
- /mentions-legales
- /mentions-legales-rgpd
- /mentions
- /legal
- /legal-notice
- /cgv (CGV often include them)
- /cgu
- /privacy-policy
- /politique-confidentialite

We probe these paths, parse the page, and extract:
- Email near "directeur de publication", "responsable de publication", "gérant"
- Phone near same keywords
- SIRET/RCS for cross-validation

Public API:
    extract_legal_contacts(website) -> dict
"""
from __future__ import annotations

import re
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 8.0

# Standard paths to probe — ordered most-likely-first to short-circuit on hit
_LEGAL_PATHS = (
    "/mentions-legales",
    "/mentions-legales/",
    "/mentions-legales-rgpd",
    "/mentions",
    "/legal",
    "/legal-notice",
    "/legal-notice/",
    "/legals",
    "/cgv",
    "/cgu",
    "/cgv-cgu",
    "/conditions-generales",
    "/politique-confidentialite",
    "/privacy",
    "/privacy-policy",
    "/about",
    "/a-propos",
    "/contact",
)

# Keywords near which we look for contact info — these signal "publisher info"
_PUBLISHER_KW = (
    "directeur de la publication",
    "directrice de la publication",
    "directeur de publication",
    "directrice de publication",
    "responsable de publication",
    "responsable de la publication",
    "gerant", "gérant", "gérante", "gerante",
    "exploitant", "exploitante",
    "editeur", "éditeur",
    "publication",
)

# Regex
_EMAIL_RX = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_FR_PHONE_RX = re.compile(
    r"(?:\+33[\s.\-]?\d|0\d)(?:[\s.\-]?\d{2}){4}\b"
)
_SIRET_RX = re.compile(r"\b\d{14}\b")
_SIREN_RX = re.compile(r"\b\d{9}\b")
# Full RCS pattern: "RCS Paris 123 456 789" or "RCS de Bordeaux numéro ..."
_RCS_RX = re.compile(
    r"R\.?C\.?S\.?(?:\s+(?:de|du|d')?\s*)?([A-ZÀ-Ÿ][a-zà-ÿ\-]+(?:\s+[A-ZÀ-Ÿ][a-zà-ÿ\-]+)?)"
    r"(?:\s+(?:n[°ºo]\s*)?\s*([\d\s]{9,}))?",
    re.IGNORECASE,
)

_LAST_AT = 0.0
_MIN_INTERVAL = 0.6


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
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "fr-FR,fr;q=0.9",
            },
            follow_redirects=True,
            verify=False,
        ) as c:
            r = c.get(url)
            if r.status_code == 200 and len(r.text) > 200:
                return r.text
    except Exception:
        pass
    return None


def _is_mentions_page(html: str) -> bool:
    """Heuristic: does this page look like a Mentions Légales page?"""
    if not html:
        return False
    lower = html.lower()
    signals = (
        "mentions légales", "mentions legales",
        "directeur de publication", "directeur de la publication",
        "rcs ", "r.c.s ", "siret",
        "loi pour la confiance dans l'économie numérique",
        "loi n° 2004-575",
        "responsable de publication",
    )
    hits = sum(1 for s in signals if s in lower)
    return hits >= 2


def _find_paths_via_homepage(homepage_html: str, base_url: str) -> list[str]:
    """Look at the homepage's footer for explicit links to legal/contact pages."""
    if not homepage_html:
        return []
    tree = HTMLParser(homepage_html)
    base_host = (urlparse(base_url).hostname or "").lower()
    candidates: list[str] = []
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        text = a.text(strip=True).lower()
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        absurl = urljoin(base_url, href)
        if (urlparse(absurl).hostname or "").lower() != base_host:
            continue
        path_low = urlparse(absurl).path.lower()
        if any(kw in text for kw in (
            "mention", "legal", "cgv", "cgu", "confidentialité", "privacy",
            "à propos", "a propos", "qui sommes", "contact",
        )):
            candidates.append(absurl.split("#", 1)[0])
        elif any(kw in path_low for kw in (
            "mention", "legal", "cgv", "cgu", "confidential", "privacy",
            "about", "apropos",
        )):
            candidates.append(absurl.split("#", 1)[0])
    return list(dict.fromkeys(candidates))


def _proximity_match(text: str, anchor_lower: str,
                     pattern: re.Pattern, window: int = 400) -> Optional[str]:
    """Find pattern within `window` chars of any anchor keyword occurrence."""
    text_low = text.lower()
    pos = 0
    matches: list[str] = []
    while True:
        idx = text_low.find(anchor_lower, pos)
        if idx == -1:
            break
        window_start = max(0, idx - window)
        window_end = min(len(text), idx + len(anchor_lower) + window)
        snippet = text[window_start:window_end]
        for m in pattern.finditer(snippet):
            matches.append(m.group(0))
        pos = idx + len(anchor_lower)
    if matches:
        # Return the closest one (first match is closest to first anchor)
        return matches[0]
    return None


def _extract_director_name(text: str) -> Optional[str]:
    """Extract the directeur de publication name.

    Typical patterns we handle:
      - "Directeur de publication : Jean Dupont"
      - "Directeur de la publication : Monsieur Alain AUDIAU"
      - "Le directeur de publication du site est Monsieur Alain Audiau"
      - "Responsable de la publication : Mme Marie DURAND"

    Returns the cleaned name (without "Monsieur"/"Mme"/etc. prefixes).
    """
    text_l = text.lower()
    for kw in ("directeur de la publication",
               "directrice de la publication",
               "directeur de publication",
               "directrice de publication",
               "responsable de publication",
               "responsable de la publication"):
        idx = text_l.find(kw)
        if idx == -1:
            continue
        # Look at the next ~150 chars after the keyword
        tail = text[idx + len(kw): idx + len(kw) + 200]
        # Drop leading punctuation/separators
        tail = re.sub(r"^[\s:.,\-–=]+", "", tail)
        # Drop common filler ("du site est ", "est ", "M. ", "Mr ", "Monsieur ", "Mme ")
        tail = re.sub(
            r"^(?:du\s+site\s+est\s+|est\s+|monsieur\s+|mme?\s+|m\.?\s+|mr\s+|"
            r"madame\s+|mademoiselle\s+|mlle\s+|dr\s+|me\s+)+",
            "",
            tail,
            flags=re.IGNORECASE,
        )
        # Cut at first sentence-ender or newline
        end = re.search(r"[.\n,;]", tail)
        if end:
            tail = tail[: end.start()]
        cleaned = re.sub(r"\s+", " ", tail).strip()
        # Must look like a name (2-5 words, only letters/spaces/hyphens/apostrophes)
        if not (4 < len(cleaned) < 70):
            continue
        if not re.match(r"^[A-Za-zÀ-ÿ'\-\s.]+$", cleaned):
            continue
        words = cleaned.split()
        if 2 <= len(words) <= 5:
            return cleaned
    return None


def extract_legal_contacts(website: str) -> dict:
    """Crawl a website's legal/about pages and extract dirigeant contact info.

    Returns dict (may have empty fields):
        {
            "director_name": "...",       # directeur de publication name
            "director_email": "...",       # email near publication keyword
            "director_phone": "...",       # phone near publication keyword
            "company_phone": "...",        # any FR phone on the page
            "company_email": "...",        # any generic email on the page
            "siret": "...",
            "rcs": "...",
            "source_url": "...",           # which page yielded the data
        }
    """
    out: dict = {
        "director_name": None,
        "director_email": None,
        "director_phone": None,
        "company_phone": None,
        "company_email": None,
        "siret": None,
        "rcs": None,
        "source_url": None,
    }
    if not website:
        return out

    # Step 1: fetch homepage to find legal-page links in the footer
    homepage_html = _fetch(website)
    candidate_urls = _find_paths_via_homepage(homepage_html or "", website)

    # Step 2: also try standard paths
    for path in _LEGAL_PATHS:
        candidate_urls.append(urljoin(website, path))
    candidate_urls = list(dict.fromkeys(candidate_urls))[:10]

    # Step 3: scan each candidate until we find a Mentions Légales page
    for url in candidate_urls:
        html = _fetch(url)
        if not html or not _is_mentions_page(html):
            continue
        tree = HTMLParser(html)
        text = tree.text(separator=" ")

        # Director name
        out["director_name"] = out["director_name"] or _extract_director_name(text)

        # Director email + phone — near publication keywords
        for kw in _PUBLISHER_KW:
            if not out["director_email"]:
                m = _proximity_match(text, kw, _EMAIL_RX, window=300)
                if m:
                    out["director_email"] = m.lower()
            if not out["director_phone"]:
                m = _proximity_match(text, kw, _FR_PHONE_RX, window=300)
                if m:
                    out["director_phone"] = m

        # Generic company contacts (any email/phone on the page)
        if not out["company_email"]:
            m = _EMAIL_RX.search(text)
            if m:
                out["company_email"] = m.group(0).lower()
        if not out["company_phone"]:
            m = _FR_PHONE_RX.search(text)
            if m:
                out["company_phone"] = m.group(0)

        # SIRET / RCS
        if not out["siret"]:
            m = _SIRET_RX.search(text)
            if m:
                out["siret"] = m.group(0)
        if not out["rcs"]:
            m = _RCS_RX.search(text)
            if m:
                city = (m.group(1) or "").strip()
                num = re.sub(r"\s+", "", (m.group(2) or "") or "")
                if city:
                    out["rcs"] = f"RCS {city}" + (f" {num}" if num else "")

        out["source_url"] = url
        # If we have at least 2 non-trivial fields, we're done
        non_empty = sum(1 for k in ("director_name", "director_email",
                                     "director_phone", "siret") if out[k])
        if non_empty >= 2:
            break

    return out


def _cli() -> None:
    import argparse
    import json
    import warnings
    warnings.filterwarnings("ignore")

    p = argparse.ArgumentParser(description="Scrape mentions légales for FR SMB contacts.")
    p.add_argument("website", help="Company website URL (https://...)")
    args = p.parse_args()
    result = extract_legal_contacts(args.website)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
