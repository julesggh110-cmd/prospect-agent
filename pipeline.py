"""
Pipeline glue — high-level helpers that compose the building blocks.

This module is intentionally THIN. The bulk of the intelligence lives in:
- Claude (who reads SKILL.md and orchestrates)
- Each individual script

The two functions here exist to save Claude from re-implementing glue:

1. `enrich_company_partial(sirene_company)` — runs all the deterministic
   enrichment (find website, scrape it, search company LinkedIn/Instagram).
   Returns a dict that Claude can read.

2. `finalize_lead(partial, person_first, person_last, person_role,
   person_sources)` — given Claude's choice of decision-maker, generate +
   verify their email, search their LinkedIn, build the Lead.

Both functions are also exposed as CLI commands for manual testing.
"""
from __future__ import annotations

import json
import sys
import warnings
from typing import Optional

from rich.console import Console

from email_finder import find_best_email
from social_finder import (
    find_instagram_for_company,
    find_instagram_for_person,
    find_linkedin_for_company,
    find_linkedin_for_person,
)
from triangulation import Lead, ScoredField, triangulate_phone, triangulate_url
from web_enrichment import enrich_company_from_website
from website_finder import find_company_website

console = Console()


# ---------------------------------------------------------------------------
# Phase 1: enrich what we can from a Sirene record alone
# ---------------------------------------------------------------------------

def enrich_company_partial(sirene_company) -> dict:
    """Take a SireneClient Company (or dict), return enriched partial data.

    Output dict keys:
        company_name, siren, naf, city, address, size, legal_dirigeants,
        website, web_enrichment (emails, phones, socials, team_page_text),
        company_linkedin (ScoredField as dict),
        company_instagram (ScoredField as dict),
        company_phone (ScoredField as dict)
    """
    if hasattr(sirene_company, "model_dump"):
        c = sirene_company
        name = c.name
        siren = c.siren
        naf = c.activite_principale
        city = c.city
        address = c.address_short
        size = c.tranche_effectif_salarie
        dirs = [
            {
                "name": (d.full_name or "").strip(),
                "role": d.qualite or d.type_dirigeant or "",
            }
            for d in c.dirigeants
            if d.full_name
        ]
    else:
        c = sirene_company
        name = c.get("nom_complet") or c.get("nom_raison_sociale") or "?"
        siren = c.get("siren")
        naf = c.get("activite_principale")
        siege = c.get("siege") or {}
        city = siege.get("libelle_commune")
        address = ", ".join(
            x for x in [siege.get("adresse"), siege.get("code_postal"), siege.get("libelle_commune")] if x
        )
        size = c.get("tranche_effectif_salarie")
        dirs = [
            {"name": f"{d.get('prenoms','')} {d.get('nom','')}".strip(),
             "role": d.get("qualite") or d.get("type_dirigeant") or ""}
            for d in (c.get("dirigeants") or [])
            if d.get("nom")
        ]

    # 1. Find website (Sirene doesn't have it)
    website = find_company_website(name, city)

    # 2. Scrape website
    web = None
    if website:
        web = enrich_company_from_website(website, fetch_team_page=True)

    # 3. Search company LinkedIn / Instagram independently (for triangulation)
    li_search = find_linkedin_for_company(name, city)
    ig_search = find_instagram_for_company(name, city)

    # 4. Triangulate company-level fields
    li_field = triangulate_url(
        [(web.linkedin_company if web else None, website or "website"),
         (li_search, "ddg:linkedin-company")],
    )
    ig_field = triangulate_url(
        [(web.instagram_account if web else None, website or "website"),
         (ig_search, "ddg:instagram-company")],
    )
    phone_field = ScoredField.missing()
    if web and web.phones:
        phone_field = triangulate_phone([(p, website or "website") for p in web.phones[:3]])

    return {
        "company_name": name,
        "siren": siren,
        "naf": naf,
        "city": city,
        "address": address,
        "size": size,
        "legal_dirigeants": dirs,
        "website": website,
        "web_enrichment": web.model_dump() if web else None,
        "team_page_text": web.team_page_text if web else None,
        "company_linkedin": li_field.model_dump(),
        "company_instagram": ig_field.model_dump(),
        "company_phone": phone_field.model_dump(),
    }


# ---------------------------------------------------------------------------
# Phase 2: build a final Lead given a decided decision-maker
# ---------------------------------------------------------------------------

def _name_in_text(first: str, last: str, text: Optional[str]) -> bool:
    """Case-insensitive 'is this person mentioned in this blob'."""
    if not text or not first or not last:
        return False
    text_l = text.lower()
    return first.lower() in text_l and last.lower() in text_l


def _name_in_linkedin_url(first: str, last: str, url: Optional[str]) -> bool:
    """LinkedIn /in/<slug> slugs usually contain firstlast or first-last."""
    if not url or not first or not last:
        return False
    import re as _re
    slug = url.rstrip("/").rsplit("/", 1)[-1].lower()
    slug = _re.sub(r"[^a-z]+", "", slug)
    return (first.lower() in slug) and (last.lower() in slug)


def finalize_lead(
    partial: dict,
    *,
    person_first: str,
    person_last: str,
    person_role: str,
    person_sources: list[str],
    naf_label: Optional[str] = None,
) -> Lead:
    """Build a triangulated Lead from a partial + Claude's decision-maker pick.

    Auto-triangulates the person's name against the website team page, the
    discovered email, and the LinkedIn profile slug. So a name initially backed
    by one source (Sirene) can climb to high confidence if the website team
    page mentions it AND the LinkedIn URL contains it AND the SMTP probe
    accepts the matching email pattern.
    """
    full_name = f"{person_first} {person_last}".strip()
    website = partial.get("website") or ""
    domain = ""
    if website:
        from urllib.parse import urlparse
        domain = (urlparse(website).hostname or "").removeprefix("www.")

    # We will gradually accumulate sources for the person name as we verify.
    name_sources = list(person_sources)

    # 1. Web team page corroborates the name?
    if _name_in_text(person_first, person_last, partial.get("team_page_text")):
        name_sources.append("website-team-page")

    # 2. Discovered emails on the website mention this person?
    web = partial.get("web_enrichment") or {}
    web_emails = web.get("emails") or []
    if any(
        person_first.lower() in e.lower() and person_last.lower() in e.lower()
        for e in web_emails
    ):
        name_sources.append("website-emails")

    # Role
    role_field = (
        ScoredField.from_single(person_role, person_sources[0] if person_sources else "claude", verified=False)
        if person_role else ScoredField.missing()
    )

    # 3. Email pattern + SMTP verify
    email_field = ScoredField.missing()
    if domain and person_first and person_last:
        best, _ = find_best_email(person_first, person_last, domain)
        if best and best.email:
            email_field = ScoredField(
                value=best.email,
                sources=[f"smtp:{domain}"],
                confidence=best.confidence,
                note=best.status,
            )
            # SMTP deliverable email is a strong corroboration of the person
            if best.status in ("deliverable", "catch_all"):
                name_sources.append(f"smtp:{best.email}")

    # 4. Person LinkedIn (filter: slug must contain first+last)
    li_raw = find_linkedin_for_person(full_name, partial.get("company_name", ""))
    if _name_in_linkedin_url(person_first, person_last, li_raw):
        li_field = ScoredField.from_single(li_raw, "ddg:linkedin-in", verified=True)
        name_sources.append(f"linkedin:{li_raw}")
    else:
        li_field = ScoredField.missing()

    # 5. Person Instagram (best-effort, weak signal)
    ig_raw = find_instagram_for_person(full_name, partial.get("company_name", ""))
    ig_field = (
        ScoredField.from_single(ig_raw, "ddg:instagram-person", verified=False)
        if ig_raw else ScoredField.missing()
    )

    # Now build name_field with all accumulated sources
    # Dedupe while preserving order
    seen = set()
    name_sources = [s for s in name_sources if not (s in seen or seen.add(s))]
    name_field = (
        ScoredField.from_multiple(full_name, name_sources)
        if len(name_sources) >= 2
        else ScoredField.from_single(full_name, name_sources[0] if name_sources else "claude")
    )

    lead = Lead(
        company_name=partial["company_name"],
        company_siren=partial.get("siren"),
        company_naf=partial.get("naf"),
        company_naf_label=naf_label,
        company_city=partial.get("city"),
        company_address=partial.get("address"),
        company_size=partial.get("size"),
        company_website=partial.get("website"),
        company_linkedin=ScoredField(**partial["company_linkedin"]),
        company_instagram=ScoredField(**partial["company_instagram"]),
        company_phone=ScoredField(**partial["company_phone"]),
        person_name=name_field,
        person_role=role_field,
        person_email=email_field,
        person_linkedin=li_field,
        person_instagram=ig_field,
    )
    lead.evaluate()
    return lead


# ---------------------------------------------------------------------------
# CLI for manual testing
# ---------------------------------------------------------------------------

def _cli() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Enrich one company end-to-end (partial phase only — Claude picks the decision-maker)."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("partial", help="Run partial enrichment for one company")
    p1.add_argument("--siren", help="Sirene SIREN to lookup")
    p1.add_argument("--name", help="Company name (if no SIREN)")
    p1.add_argument("--city", help="City")

    args = parser.parse_args()

    if args.cmd == "partial":
        from sirene_client import SireneClient
        if args.siren:
            with SireneClient() as c:
                resp = c.search(args.siren)
                if not resp.results:
                    console.print(f"[red]No company found for SIREN {args.siren}[/red]")
                    sys.exit(1)
                company = resp.results[0]
        elif args.name:
            with SireneClient() as c:
                resp = c.search(args.name, per_page=1)
                if not resp.results:
                    console.print(f"[red]No company found for name '{args.name}'[/red]")
                    sys.exit(1)
                company = resp.results[0]
        else:
            console.print("[red]Provide --siren or --name[/red]")
            sys.exit(1)

        partial = enrich_company_partial(company)
        # Drop the bulky team_page_text from the printed JSON
        printable = {**partial}
        if printable.get("web_enrichment"):
            printable["web_enrichment"] = {
                k: v for k, v in printable["web_enrichment"].items()
                if k != "team_page_text"
            }
        if printable.get("team_page_text"):
            printable["team_page_text"] = (
                printable["team_page_text"][:500] + "...(truncated)"
                if len(printable["team_page_text"]) > 500
                else printable["team_page_text"]
            )
        print(json.dumps(printable, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    warnings.filterwarnings("ignore")
    _cli()
