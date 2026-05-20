"""
Direct domain guess — when no source confirms a website, just guess it.

For a French SMB called "L'Escargot" in Toulouse, the website is very often
`l-escargot.fr`, `restaurant-lescargot.fr`, or `lescargot-toulouse.fr`.

We generate plausible candidates from:
- The company name (slugified, with / without article, with / without sector word)
- The TLD list (.fr first — the obvious one for FR businesses, then .com, .eu)
- A few naming conventions ("restaurant-X", "X-toulouse", "le-X")

Then we HEAD-check each candidate in parallel-ish (just sequential 2 sec each)
and return the first one that:
- Responds with 2xx / 3xx
- Whose final page mentions the company name OR the city (sanity check)

This is BEST-EFFORT and CAN return None. When it works it's pure gold for
SMBs whose Sirene record has no website.

Why this is worth the effort:
- Sirene has website for ~20% of SMBs
- Pappers has it for maybe ~40%
- Brave Search can't always find it
- But domain guess at .fr hits often for shop/resto names
- Zero cost, zero rate limit

Public API:
    guess_website(name, city=None, sector_hint=None) -> Optional[str]
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Optional

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 5.0


def _strip_diacritics(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def _slugify(s: str, *, keep_hyphen: bool = True) -> str:
    s = _strip_diacritics(s).lower()
    # Drop apostrophes ("l'escargot" -> "lescargot")
    s = s.replace("'", "").replace("’", "").replace("`", "")
    if keep_hyphen:
        s = re.sub(r"[^a-z0-9-]+", "-", s)
        s = re.sub(r"-+", "-", s).strip("-")
    else:
        s = re.sub(r"[^a-z0-9]+", "", s)
    return s


_LEAD_ARTICLES = (
    "le ", "la ", "les ", "l ", "l'", "l’", "un ", "une ", "des ",
    "au ", "aux ", "chez ",
)

_GENERIC_SECTOR_WORDS = (
    # Resto/bar/CHR — covers most Bear Brothers prospects
    "restaurant", "resto", "bar", "cafe", "café", "bistro", "bistrot",
    "brasserie", "pub", "wine", "cave", "caviste",
    # Hospitality
    "hotel", "hôtel", "auberge",
    # Beauty / services
    "salon", "coiffeur", "esthetique", "esthétique", "spa", "barbier",
)

_PRIMARY_TLDS = (".fr", ".com")
_SECONDARY_TLDS = (".eu", ".net")


def _drop_leading_article(name: str) -> str:
    nl = name.lower()
    for art in _LEAD_ARTICLES:
        if nl.startswith(art):
            return name[len(art):].strip()
    return name


def _name_variants(name: str) -> list[str]:
    """Return slug variants for the company name.

    Handles Sirene-style names like 'STEVO\\'S DINING EMPORIUM (LA FAIM DES HARICOTS)'
    by also exposing the parenthesised trade name (often the real public-facing brand).
    """
    out: list[str] = []
    base = name.strip()

    # Pull out the parenthesised brand name if any — it's usually the real
    # public-facing name for restaurants ('STEVO'S DINING EMPORIUM (LA FAIM DES HARICOTS)').
    paren = re.findall(r"\(([^)]+)\)", base)
    base_no_paren = re.sub(r"\([^)]*\)", "", base).strip()

    candidates: list[str] = []
    for raw in (base_no_paren, base, *paren):
        if not raw:
            continue
        candidates.append(raw)
        # Also try without leading article
        candidates.append(_drop_leading_article(raw))

    for n in candidates:
        if not n:
            continue
        # With hyphens preserved
        out.append(_slugify(n, keep_hyphen=True))
        # Without any separator (lescargot)
        out.append(_slugify(n, keep_hyphen=False))

    # Dedup, drop empties and very short ones (< 3 chars are too noisy)
    seen = set()
    result = []
    for s in out:
        if not s or len(s) < 3 or s in seen:
            continue
        seen.add(s)
        result.append(s)
    return result


def _candidate_domains(
    name: str,
    *,
    city: Optional[str] = None,
    sector_hint: Optional[str] = None,
) -> list[str]:
    """Return a ranked list of candidate domains, most likely first."""
    names = _name_variants(name)
    city_slug = _slugify(city, keep_hyphen=False) if city else None
    sector_slug = None
    if sector_hint:
        # Use first known word that's in our generic list
        for word in sector_hint.lower().split():
            stripped = _strip_diacritics(word)
            if stripped in _GENERIC_SECTOR_WORDS or word in _GENERIC_SECTOR_WORDS:
                sector_slug = stripped
                break

    out: list[str] = []

    # Strategy 1: bare name on .fr / .com — the highest signal
    for n in names:
        for tld in _PRIMARY_TLDS:
            out.append(n + tld)

    # Strategy 2: prefixed with sector word (restaurant-X.fr)
    if sector_slug:
        for n in names:
            for tld in _PRIMARY_TLDS:
                out.append(f"{sector_slug}-{n}{tld}")

    # Strategy 3: suffixed with city (X-toulouse.fr)
    if city_slug and len(city_slug) <= 15:
        for n in names:
            for tld in _PRIMARY_TLDS:
                out.append(f"{n}-{city_slug}{tld}")

    # Strategy 4: bare name on secondary TLDs (lower priority)
    for n in names:
        for tld in _SECONDARY_TLDS:
            out.append(n + tld)

    # Dedup while preserving order
    seen = set()
    result = []
    for d in out:
        if d not in seen and len(d) <= 60:
            seen.add(d)
            result.append(d)
    return result


def _is_real_site(url: str, name: str, city: Optional[str] = None) -> bool:
    """HEAD then GET-prefix; True iff response looks like a real business site.

    We are intentionally STRICT here because the candidate list contains short,
    plausible-looking domains (`navarre.fr`, `septime.fr`) that almost always
    belong to unrelated businesses. Requirements to accept:

    1. HTTP 2xx/3xx with a real (>500 char) HTML body.
    2. The page must mention the distinguishing slug of the brand name in
       either its <title> OR within the first 4 KB of body text — French
       generic signals alone ("menu", "réservation") are not enough.
    3. We additionally require either:
        - the city name to appear somewhere on the page, OR
        - all multi-token name pieces to co-occur (so "Le Bibent" → both
          "bibent" must appear; "Chez Navarre" → "navarre"+city or
          "chez navarre"); single-word brands need city corroboration.

    These rules eliminate parked domains and unrelated namesakes at the cost
    of missing a few legit-but-bare landing pages. For a prospection use case,
    missing is always better than wrong.
    """
    try:
        with httpx.Client(
            timeout=TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            verify=False,
            http2=False,
        ) as c:
            r = c.head(url)
            if r.status_code >= 400 or r.status_code == 0:
                return False
            r2 = c.get(url)
            if r2.status_code >= 400:
                return False
            html = (r2.text or "")
            if len(html) < 500:
                return False
    except Exception:
        return False

    # Lowercase + accent-strip the full page once
    page = _strip_diacritics(html).lower()

    # Extract the <title> tag for stronger signal
    title_match = re.search(r"<title[^>]*>(.*?)</title>", page, re.DOTALL)
    title = (title_match.group(1) if title_match else "")[:300]
    page_slug = _slugify(page[:4000], keep_hyphen=False)
    title_slug = _slugify(title, keep_hyphen=False)
    city_slug = _slugify(city, keep_hyphen=False) if city else None

    # Build candidate name forms — both the parent name AND any parenthesised
    # trade name (Sirene records like "STEVO'S DINING (LA FAIM DES HARICOTS)").
    parens = re.findall(r"\(([^)]+)\)", name)
    no_paren = re.sub(r"\([^)]*\)", "", name).strip()
    candidates_for_name = [_drop_leading_article(no_paren), name, *parens]

    # Build candidate name forms — both the parent name AND any parenthesised
    # trade name. We accept a match against ANY of them.
    for cand in candidates_for_name:
        cand = (cand or "").strip()
        if not cand:
            continue
        slug = _slugify(cand, keep_hyphen=False)
        words = [
            _slugify(w, keep_hyphen=False)
            for w in re.split(r"\W+", cand)
            if len(w) > 2
        ]
        if not slug or len(slug) < 4:
            continue

        # Rule 2: name presence in title OR first 4 KB
        name_present = slug in title_slug or slug in page_slug
        if not name_present:
            continue

        # Rule 3: corroboration. Either city on page, or all multi-word tokens
        # present. Single-word brands without city → REJECT (high false-positive
        # rate from registered namesakes / domain squatters).
        if city_slug and city_slug in page_slug:
            return True
        if len(words) >= 2 and all(w in page_slug for w in words):
            return True

    return False


# Parking / domainer placeholder hosts we want to skip
_PARKED_HOSTS = {
    "sedoparking.com", "afternic.com", "godaddy.com", "dan.com", "uniregistry.com",
}


def guess_website(
    name: str,
    *,
    city: Optional[str] = None,
    sector_hint: Optional[str] = None,
    max_attempts: int = 6,
) -> Optional[str]:
    """Try a handful of obvious .fr/.com domains for `name`.

    Returns the first plausible URL that responds and looks like a real business
    site, or None.
    """
    if not name:
        return None
    candidates = _candidate_domains(name, city=city, sector_hint=sector_hint)
    candidates = candidates[:max_attempts]
    for domain in candidates:
        for scheme in ("https://", "http://"):
            url = scheme + domain
            try:
                with httpx.Client(
                    timeout=TIMEOUT,
                    headers={"User-Agent": USER_AGENT},
                    follow_redirects=True,
                    verify=False,
                    http2=False,
                ) as c:
                    r = c.head(url)
                    if r.status_code == 0 or r.status_code >= 400:
                        continue
                    final_host = (r.url.host or "").lower()
                    if any(p in final_host for p in _PARKED_HOSTS):
                        continue
            except Exception:
                continue

            # Now do a GET-based sanity check
            if _is_real_site(url, name, city=city):
                return f"https://{domain}"
            # Don't double-check with http:// if https already failed sanity
            break
    return None


def _cli() -> None:
    import argparse
    import warnings
    warnings.filterwarnings("ignore")

    p = argparse.ArgumentParser(description="Guess a French SMB website from name.")
    p.add_argument("name", help="Company name (quoted)")
    p.add_argument("--city", help="City hint")
    p.add_argument("--sector", help="Sector keyword (e.g. 'restaurant')")
    p.add_argument("--show", action="store_true", help="Show full candidate list")
    args = p.parse_args()
    if args.show:
        for c in _candidate_domains(args.name, city=args.city, sector_hint=args.sector):
            print(c)
        return
    site = guess_website(args.name, city=args.city, sector_hint=args.sector)
    print(site or "(no guess hit)")


if __name__ == "__main__":
    _cli()
