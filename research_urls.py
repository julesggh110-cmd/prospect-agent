"""
Research URLs — pre-built one-click links per lead.

When the agent can't auto-find a personal LinkedIn, Instagram, or mobile,
the salesperson still needs to do the lookup themselves. This module pre-
builds the exact search URLs so they click once and see the right SERP.

Why this is valuable: on FR SMB gérants, ~50% of personal data is simply
not auto-discoverable from free sources (no public LinkedIn, mobile not
indexed). The other 50% IS findable but takes 30 seconds of manual work
per lead. Pre-built URLs reduce that to 5 seconds (one click).

Each URL is targeted to land on the most useful SERP for that purpose:
- Google "<name> <company> linkedin"  → finds LinkedIn profile fast
- LinkedIn people search              → backup for above
- Instagram search                    → personal Insta
- Pages Jaunes business search        → phone if CF lets the user through
- Société.com                         → SIREN-based dirigeant cross-check

Public API:
    research_urls_for_lead(lead) -> dict[str, str]
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote_plus


def _google_search(query: str) -> str:
    return f"https://www.google.com/search?q={quote_plus(query)}"


def _bing_search(query: str) -> str:
    return f"https://www.bing.com/search?q={quote_plus(query)}"


def research_urls_for_lead(lead) -> dict[str, str]:
    """Build a dict of {label: url} for one Lead — ready to embed in the XLSX.

    Always returns 5 keys (some may be empty strings if input is too thin).
    """
    name = (lead.person_name.value or "").strip() if hasattr(lead, "person_name") else ""
    company = (getattr(lead, "company_name", "") or "").strip()
    city = (getattr(lead, "company_city", "") or "").strip()
    siren = (getattr(lead, "company_siren", "") or "").strip()

    out: dict[str, str] = {
        "linkedin_search": "",
        "instagram_search": "",
        "google_lookup": "",
        "pagesjaunes": "",
        "societe_com": "",
    }

    if name and company:
        # Targeted Google query — almost always surfaces the LinkedIn /in/ URL
        # at position 1 when one exists.
        out["google_lookup"] = _google_search(f'{name} {company} {city} linkedin'.strip())
        # LinkedIn's own people search (we use Google site-restrict because the
        # native LinkedIn search URL no longer works for logged-out visitors).
        out["linkedin_search"] = _google_search(f'"{name}" {company} site:linkedin.com/in')
        # Instagram: search Google site-restricted (Insta's own search is broken
        # for non-logged-in users too).
        out["instagram_search"] = _google_search(f'"{company}" {city} site:instagram.com')
    elif company:
        out["google_lookup"] = _google_search(f'{company} {city} contact'.strip())
        out["instagram_search"] = _google_search(f'{company} {city} site:instagram.com'.strip())

    if company:
        out["pagesjaunes"] = (
            f"https://www.pagesjaunes.fr/recherche/{quote_plus(city or 'France')}/"
            f"{quote_plus(company)}"
        )

    if siren:
        # Société.com is the open dirigeants/financials registry — alternative
        # cross-check to Pappers, free, no rate-limit, indexed by SIREN.
        out["societe_com"] = f"https://www.societe.com/societe/-{siren}.html"
    elif company:
        out["societe_com"] = _google_search(f'{company} {city} site:societe.com'.strip())

    return out


def _cli() -> None:
    """Print sample URLs for a few hard-coded test leads."""
    class FakeLead:
        def __init__(self, name, company, city, siren):
            from triangulation import ScoredField
            self.person_name = ScoredField(value=name, sources=["test"], confidence=70)
            self.company_name = company
            self.company_city = city
            self.company_siren = siren

    samples = [
        FakeLead("Stephen Chong", "La Faim des Haricots", "Toulouse", "408400547"),
        FakeLead("Bertrand Grébaut", "Septime", "Paris", ""),
    ]
    import json
    for l in samples:
        print(f"--- {l.person_name.value} @ {l.company_name}")
        print(json.dumps(research_urls_for_lead(l), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
