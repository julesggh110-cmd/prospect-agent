"""
Web enrichment — extract contact info and social links from a company website.

Strategy:
1. Fetch the homepage (follow redirects, ignore SSL errors).
2. Parse HTML for:
   - emails (mailto: links + raw text matching)
   - phones (tel: links + raw text matching with FR/intl patterns)
   - LinkedIn URL (any linkedin.com/company/ or linkedin.com/in/ link)
   - Instagram URL (any instagram.com/* link)
   - Facebook, X/Twitter, YouTube (bonus)
   - candidate "team" / "about" page URLs (to fetch in a second pass)
3. If a team page is found, fetch it and extract again (emails+names often live there).

This module is a pure data extractor. Decision-making (which contact belongs to
which person, persona matching) is delegated to Claude in the orchestrator.

Triangulation note: every extracted value comes with its source URL so the
calling code can score confidence later.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
import tldextract
from pydantic import BaseModel, ConfigDict, Field
from selectolax.parser import HTMLParser

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 15.0

# --- regex ---
EMAIL_RX = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)
# FR-first phone patterns. We are STRICT to avoid matching dates, SIREN numbers,
# postal codes, year ranges, etc. Three accepted formats:
# - 0X XX XX XX XX  (FR national, 10 digits starting with 0[1-9])
# - +33 X XX XX XX XX  (FR international)
# - +XX XXX...  (other intl, +CC then 7-14 more digits)
PHONE_RX = re.compile(
    r"(?:"
    r"\b0[1-9](?:[\s.\-]?\d{2}){4}"                  # 0X XX XX XX XX
    r"|\+33[\s.\-]?[1-9](?:[\s.\-]?\d{2}){4}"        # +33 X XX XX XX XX
    r"|\+(?!33\b)\d{1,3}[\s.\-]?\d(?:[\s.\-]?\d{2,4}){2,5}"  # +CC ...
    r")\b"
)
# Generic / shared inbox local-parts — these are NOT a person's email.
GENERIC_LOCAL_PARTS = {
    "contact", "info", "infos", "hello", "bonjour", "welcome",
    "support", "help", "aide", "service-client", "serviceclient",
    "sales", "ventes", "commercial", "commerce",
    "marketing", "communication", "press", "presse", "media",
    "admin", "administration", "secretariat", "secretaire",
    "rh", "hr", "recrutement", "recruiting", "jobs", "carrieres",
    "comptabilite", "compta", "facturation", "billing", "finance",
    "direction", "office",
    "noreply", "no-reply", "donotreply",
    "webmaster", "postmaster", "abuse",
    "reservations", "reservation", "booking", "rsvp",
}
# Hints for team / about pages (FR + EN)
TEAM_HINTS = (
    "equipe", "équipe", "team", "about", "a-propos", "à-propos",
    "qui-sommes-nous", "qui-nous-sommes", "notre-equipe", "notre-équipe",
    "leadership", "direction", "dirigeants", "fondateurs", "founders",
    "people", "staff", "members",
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class WebEnrichment(BaseModel):
    """Aggregated info extracted from one or several pages of a company website."""
    model_config = ConfigDict(extra="allow")

    root_url: str
    pages_fetched: list[str] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)            # all (legacy)
    emails_generic: list[str] = Field(default_factory=list)    # contact@, info@, ...
    emails_personal: list[str] = Field(default_factory=list)   # prenom.nom@, ...
    phones: list[str] = Field(default_factory=list)
    linkedin_company: Optional[str] = None
    linkedin_profiles: list[str] = Field(default_factory=list)
    instagram_account: Optional[str] = None
    facebook: Optional[str] = None
    twitter: Optional[str] = None
    youtube: Optional[str] = None
    team_page_url: Optional[str] = None
    team_page_text: Optional[str] = None
    error: Optional[str] = None

    @property
    def root_domain(self) -> Optional[str]:
        ext = tldextract.extract(self.root_url)
        if not ext.domain:
            return None
        return f"{ext.domain}.{ext.suffix}" if ext.suffix else ext.domain


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def _clean_phone(raw: str) -> Optional[str]:
    """Normalize and validate a phone-like string. Returns None if it isn't really one.

    Rejects dates (DDMMYYYY), SIRENs (9 digits), SIRETs (14 digits matching SIREN),
    postal codes, year ranges. Accepts:
    - FR national: 10 digits starting with 0[1-9]
    - International with +: 8-15 digits after the +
    """
    digits = re.sub(r"[^\d+]", "", raw)
    digits_only = re.sub(r"\D", "", digits)
    n = len(digits_only)

    # Outright bad lengths
    if n < 8 or n > 15:
        return None

    # Common false positives:
    # - 8 digits looking like DDMMYYYY (01010000 .. 31129999)
    if n == 8 and digits_only.isdigit():
        try:
            d = int(digits_only[:2])
            m = int(digits_only[2:4])
            y = int(digits_only[4:8])
            if 1 <= d <= 31 and 1 <= m <= 12 and 1900 <= y <= 2099:
                return None  # it's a date
        except ValueError:
            pass

    # - 9 digits = exactly a SIREN
    if n == 9 and digits == digits_only:
        return None

    # - 14 digits = SIRET
    if n == 14 and digits == digits_only:
        return None

    # If FR national, must start with 0 and 2nd digit 1-9
    if n == 10 and digits == digits_only:
        if not (digits_only.startswith("0") and digits_only[1] in "123456789"):
            return None

    # Strip leading + and require a leading 0 or country code for everything else
    if digits.startswith("+"):
        return digits
    if n == 10 and digits_only.startswith("0"):
        return digits_only

    # Anything else (e.g. 11-13 raw digits not starting with 0) — probably noise
    return None


def _dedupe(seq: list[str]) -> list[str]:
    """Order-preserving dedupe."""
    seen: set[str] = set()
    out = []
    for x in seq:
        k = x.lower()
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


def _extract_from_html(html: str, base_url: str) -> dict:
    """Pull emails, phones, social links, and candidate team URLs from one page."""
    tree = HTMLParser(html)
    text = tree.text(separator=" ")

    emails = [m.group(0) for m in EMAIL_RX.finditer(text)]
    # Drop obviously-junk emails
    emails = [
        e for e in emails
        if not e.lower().endswith((".png", ".jpg", ".gif", ".webp", ".svg"))
        and "example.com" not in e.lower()
        and "sentry.io" not in e.lower()
        and "@2x" not in e.lower()
    ]
    # Separate generic ("contact@", "info@", ...) from personal-looking emails.
    generic_emails: list[str] = []
    personal_emails: list[str] = []
    for e in emails:
        local = e.split("@", 1)[0].lower()
        # local-part normalized for matching (strip dots/dashes/underscores)
        local_clean = re.sub(r"[._\-]", "", local)
        if local in GENERIC_LOCAL_PARTS or local_clean in GENERIC_LOCAL_PARTS:
            generic_emails.append(e)
        else:
            personal_emails.append(e)

    phones_raw = [m.group(0) for m in PHONE_RX.finditer(text)]
    phones = [p for p in (_clean_phone(p) for p in phones_raw) if p]

    linkedin_company: Optional[str] = None
    linkedin_profiles: list[str] = []
    instagram: Optional[str] = None
    facebook: Optional[str] = None
    twitter: Optional[str] = None
    youtube: Optional[str] = None
    team_candidates: list[str] = []

    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href:
            continue
        # Mailto / tel
        if href.startswith("mailto:"):
            addr = href[7:].split("?", 1)[0].strip()
            if EMAIL_RX.fullmatch(addr):
                emails.append(addr)
        elif href.startswith("tel:"):
            cleaned = _clean_phone(href[4:])
            if cleaned:
                phones.append(cleaned)
        else:
            absurl = urljoin(base_url, href)
            host = urlparse(absurl).hostname or ""
            host_l = host.lower()
            path_l = urlparse(absurl).path.lower()

            if "linkedin.com" in host_l:
                if "/company/" in path_l and not linkedin_company:
                    linkedin_company = absurl.split("?", 1)[0].rstrip("/")
                elif "/in/" in path_l:
                    linkedin_profiles.append(absurl.split("?", 1)[0].rstrip("/"))
                elif not linkedin_company:
                    linkedin_company = absurl.split("?", 1)[0].rstrip("/")
            elif "instagram.com" in host_l and not instagram:
                instagram = absurl.split("?", 1)[0].rstrip("/")
            elif "facebook.com" in host_l and not facebook:
                facebook = absurl.split("?", 1)[0].rstrip("/")
            elif ("twitter.com" in host_l or "x.com" in host_l) and not twitter:
                twitter = absurl.split("?", 1)[0].rstrip("/")
            elif "youtube.com" in host_l and not youtube:
                youtube = absurl.split("?", 1)[0].rstrip("/")

            # team page candidate (same-domain)
            base_host = urlparse(base_url).hostname or ""
            if host_l == base_host.lower():
                if any(h in path_l for h in TEAM_HINTS):
                    team_candidates.append(absurl.split("#", 1)[0])

    return {
        "emails": _dedupe(emails),
        "emails_generic": _dedupe(generic_emails),
        "emails_personal": _dedupe(personal_emails),
        "phones": _dedupe(phones),
        "linkedin_company": linkedin_company,
        "linkedin_profiles": _dedupe(linkedin_profiles),
        "instagram": instagram,
        "facebook": facebook,
        "twitter": twitter,
        "youtube": youtube,
        "team_candidates": _dedupe(team_candidates),
        "text": text,
    }


def enrich_company_from_website(url: str, *, fetch_team_page: bool = True) -> WebEnrichment:
    """Fetch a company homepage and (optionally) its team page; return aggregated info."""
    root_url = url if url.startswith(("http://", "https://")) else "https://" + url
    enrichment = WebEnrichment(root_url=root_url)

    try:
        with httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
            verify=False,  # many small business sites have broken certs
        ) as client:
            resp = client.get(root_url)
            resp.raise_for_status()
            home_url = str(resp.url)
            enrichment.pages_fetched.append(home_url)
            extracted = _extract_from_html(resp.text, home_url)

            enrichment.emails = extracted["emails"]
            enrichment.emails_generic = extracted["emails_generic"]
            enrichment.emails_personal = extracted["emails_personal"]
            enrichment.phones = extracted["phones"]
            enrichment.linkedin_company = extracted["linkedin_company"]
            enrichment.linkedin_profiles = extracted["linkedin_profiles"]
            enrichment.instagram_account = extracted["instagram"]
            enrichment.facebook = extracted["facebook"]
            enrichment.twitter = extracted["twitter"]
            enrichment.youtube = extracted["youtube"]

            # Try one team page (the first candidate that looks promising)
            if fetch_team_page and extracted["team_candidates"]:
                team_url = extracted["team_candidates"][0]
                try:
                    tresp = client.get(team_url)
                    tresp.raise_for_status()
                    enrichment.pages_fetched.append(str(tresp.url))
                    team_extracted = _extract_from_html(tresp.text, str(tresp.url))
                    enrichment.team_page_url = str(tresp.url)
                    # cap text at 20k chars so Claude doesn't choke on giant pages
                    enrichment.team_page_text = team_extracted["text"][:20_000]
                    # merge new emails/phones/socials
                    enrichment.emails = _dedupe(enrichment.emails + team_extracted["emails"])
                    enrichment.emails_generic = _dedupe(
                        enrichment.emails_generic + team_extracted["emails_generic"]
                    )
                    enrichment.emails_personal = _dedupe(
                        enrichment.emails_personal + team_extracted["emails_personal"]
                    )
                    enrichment.phones = _dedupe(enrichment.phones + team_extracted["phones"])
                    enrichment.linkedin_profiles = _dedupe(
                        enrichment.linkedin_profiles + team_extracted["linkedin_profiles"]
                    )
                    if not enrichment.linkedin_company:
                        enrichment.linkedin_company = team_extracted["linkedin_company"]
                    if not enrichment.instagram_account:
                        enrichment.instagram_account = team_extracted["instagram"]
                except Exception:
                    pass  # team page is best-effort

    except httpx.HTTPError as exc:
        enrichment.error = f"HTTPError: {exc}"
    except Exception as exc:
        enrichment.error = f"{type(exc).__name__}: {exc}"

    return enrichment


# ---------------------------------------------------------------------------
# CLI for manual testing
# ---------------------------------------------------------------------------

def _cli() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Scrape a company website for contact info.")
    parser.add_argument("url", help="The website URL (with or without https://)")
    parser.add_argument("--no-team", action="store_true", help="Skip the team-page fetch")
    args = parser.parse_args()

    result = enrich_company_from_website(args.url, fetch_team_page=not args.no_team)
    print(json.dumps(result.model_dump(exclude={"team_page_text"}), indent=2, ensure_ascii=False))
    if result.team_page_text:
        print(f"\n--- team_page_text (truncated, {len(result.team_page_text)} chars) ---")
        print(result.team_page_text[:500] + "...")


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    _cli()
