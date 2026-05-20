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
import os
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable, Optional

from rich.console import Console

from email_finder import find_best_email
from name_utils import clean_person_name
from pappers_client import enrich_with_pappers, have_pappers_key
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
# Persistent cache for expensive lookups (website/social) across runs
# ---------------------------------------------------------------------------
try:
    import diskcache  # type: ignore
    _CACHE_DIR = Path(__file__).resolve().parent / "data" / "cache"
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _CACHE = diskcache.Cache(str(_CACHE_DIR))
    _CACHE_TTL = 60 * 60 * 24 * 30  # 30 days
except ImportError:
    _CACHE = None
    _CACHE_TTL = 0


def _cached(namespace: str, key: str):
    """Decorator factory for caching a function's result by (namespace, key)."""
    def wrap(fn):
        def inner(*args, **kwargs):
            if _CACHE is None:
                return fn(*args, **kwargs)
            ck = f"{namespace}:{key}"
            hit = _CACHE.get(ck, default=None)
            if hit is not None:
                return hit
            val = fn(*args, **kwargs)
            if val is not None:
                _CACHE.set(ck, val, expire=_CACHE_TTL)
            return val
        return inner
    return wrap


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
        dirs = []
        for d in c.dirigeants:
            if not d.full_name:
                continue
            cleaned = clean_person_name(d.full_name)
            dirs.append({
                "name": cleaned.display or d.full_name.strip(),
                "raw_name": d.full_name.strip(),
                "first": cleaned.first,
                "last": cleaned.last,
                "role": d.qualite or d.type_dirigeant or "",
            })
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
        dirs = []
        for d in (c.get("dirigeants") or []):
            if not d.get("nom"):
                continue
            raw = f"{d.get('prenoms','')} {d.get('nom','')}".strip()
            cleaned = clean_person_name(raw)
            dirs.append({
                "name": cleaned.display or raw,
                "raw_name": raw,
                "first": cleaned.first,
                "last": cleaned.last,
                "role": d.get("qualite") or d.get("type_dirigeant") or "",
            })

    # 1. Try Pappers FIRST — direct website, email, phone (skip the DDG guess).
    pappers_site: Optional[str] = None
    pappers_email: Optional[str] = None
    pappers_phone: Optional[str] = None
    if have_pappers_key() and siren:
        # cache Pappers per SIREN (rarely changes)
        @_cached("pappers", siren)
        def _fetch():
            p = enrich_with_pappers(siren)
            return p.model_dump() if p else None
        pdata = _fetch()
        if pdata:
            pappers_site = pdata.get("site_web") or None
            pappers_email = pdata.get("email") or None
            pappers_phone = pdata.get("telephone") or None
            # Pappers can give richer dirigeants — merge if Sirene was empty
            if not dirs:
                dirs = [
                    {"name": f"{d.get('prenom','')} {d.get('nom','')}".strip(),
                     "role": d.get("qualite") or ""}
                    for d in (pdata.get("representants") or [])
                    if d.get("nom")
                ]

    # 2. Website: Pappers > DDG search > OSM tag > direct .fr/.com domain guess.
    @_cached("website", f"{name}|{city or ''}")
    def _find_site():
        if pappers_site:
            return pappers_site
        ddg = find_company_website(name, city)
        if ddg:
            return ddg
        return None
    website = _find_site()

    # 2b. OSM fallback — for shop/resto/bar storefronts, OSM tags are gold.
    # We also pull the OSM phone and Instagram/Facebook handles since we have them.
    osm_data: Optional[dict] = None
    if not website or not pappers_phone:
        @_cached("osm", f"{name}|{city or ''}")
        def _osm():
            try:
                from osm_finder import find_business_on_osm
                return find_business_on_osm(name, city)
            except Exception:
                return None
        osm_data = _osm()
        if osm_data and not website and osm_data.get("website"):
            website = osm_data["website"]

    # 2c. Direct domain-guess fallback (e.g. lebibent.com). Free, fast, and works
    # for ~30% of FR SMBs whose Sirene/Pappers record has no website.
    if not website:
        @_cached("guess", f"{name}|{city or ''}|{naf or ''}")
        def _guess():
            try:
                from domain_guess import guess_website
                return guess_website(name, city=city, sector_hint=naf)
            except Exception:
                return None
        guess = _guess()
        if guess:
            website = guess

    # 3. Scrape the website (cached per URL).
    @_cached("web_enrichment", website or "no_site")
    def _scrape():
        if not website:
            return None
        we = enrich_company_from_website(website, fetch_team_page=True)
        return we.model_dump() if we else None
    web_dict = _scrape()

    # Pull the bits we need out of the scraped dict (lazy DDG: skip search if already known)
    web_li = (web_dict or {}).get("linkedin_company")
    web_ig = (web_dict or {}).get("instagram_account")
    web_phones = (web_dict or {}).get("phones") or []

    # 4. Search company LinkedIn ONLY if website didn't give it (lazy DDG).
    if web_li:
        li_search = None
    else:
        @_cached("li_co", f"{name}|{city or ''}")
        def _li_search():
            return find_linkedin_for_company(name, city)
        li_search = _li_search()

    if web_ig:
        ig_search = None
    else:
        @_cached("ig_co", f"{name}|{city or ''}")
        def _ig_search():
            return find_instagram_for_company(name, city)
        ig_search = _ig_search()

    # 5. Triangulate company-level fields
    li_field = triangulate_url(
        [(web_li, website or "website"),
         (li_search, "search:linkedin-company")],
    )
    ig_field = triangulate_url(
        [(web_ig, website or "website"),
         (ig_search, "search:instagram-company")],
    )
    # Phone: combine Pappers + website-scraped + Pages Jaunes fallback.
    phone_sources: list[tuple[Optional[str], str]] = []
    if pappers_phone:
        phone_sources.append((pappers_phone, "pappers"))
    for p in web_phones[:3]:
        phone_sources.append((p, website or "website"))

    # SMB fallback: if we still have no phone, use OSM's phone tag (free,
    # no anti-bot). PJ is now blocked behind Cloudflare so we only attempt it
    # as a last resort and accept silent failures.
    if not phone_sources and osm_data and osm_data.get("phone"):
        phone_sources.append((osm_data["phone"], "osm"))

    if not phone_sources:
        @_cached("pagesjaunes", f"{name}|{city or ''}")
        def _pj():
            try:
                from pagesjaunes_client import find_phone_on_pagesjaunes
                return find_phone_on_pagesjaunes(name, city)
            except Exception:
                return None
        pj_phone = _pj()
        if pj_phone:
            phone_sources.append((pj_phone, "pagesjaunes"))

    phone_field = triangulate_phone(phone_sources) if phone_sources else ScoredField.missing()
    # If Pappers / OSM / Pages Jaunes is the source (one alone is enough), boost to 70.
    if phone_field.value and phone_field.confidence < 70:
        srcs = phone_field.sources or []
        if any(s in ("pappers", "pagesjaunes", "osm") for s in srcs):
            phone_field.confidence = 70
            phone_field.note = (phone_field.note or "") + " · authoritative-source"

    # Company email candidates: Pappers (official greffe) + generic emails scraped
    # from the website (contact@, info@, ...) + OSM email tag. These are NOT
    # the decision-maker personal email — they go in the `company_email` field,
    # kept separate so the user is never confused about what they actually have.
    company_email_candidates: list[str] = []
    if pappers_email:
        company_email_candidates.append(pappers_email)
    for e in ((web_dict or {}).get("emails_generic") or []):
        if e not in company_email_candidates:
            company_email_candidates.append(e)
    if osm_data and osm_data.get("email") and osm_data["email"] not in company_email_candidates:
        company_email_candidates.append(osm_data["email"])
    company_email = company_email_candidates[0] if company_email_candidates else None

    # Promote OSM-discovered Instagram / Facebook handles if we got them and the
    # website scrape didn't.
    if osm_data:
        osm_ig = osm_data.get("instagram")
        if osm_ig and not web_ig:
            # Turn '@bibent' or 'bibent' into a full URL for consistency
            handle = osm_ig.lstrip("@").strip()
            if handle and not handle.startswith("http"):
                osm_ig_url = f"https://instagram.com/{handle}"
            else:
                osm_ig_url = handle
            ig_field = triangulate_url(
                [(osm_ig_url, "osm"),
                 (ig_field.value, ig_field.sources[0] if ig_field.sources else "ig")],
            )

    return {
        "company_name": name,
        "siren": siren,
        "naf": naf,
        "city": city,
        "address": address,
        "size": size,
        "legal_dirigeants": dirs,
        "website": website,
        "company_email": company_email,
        "company_email_all": company_email_candidates,
        "company_official_email": pappers_email,  # legacy alias
        "web_enrichment": web_dict,
        "team_page_text": (web_dict or {}).get("team_page_text"),
        "company_linkedin": li_field.model_dump(),
        "company_instagram": ig_field.model_dump(),
        "company_phone": phone_field.model_dump(),
    }


# ---------------------------------------------------------------------------
# Phase 1.5: parallel batch enrichment
# ---------------------------------------------------------------------------

def enrich_companies_parallel(companies: Iterable, *, max_workers: int = 3) -> list[dict]:
    """Run enrich_company_partial on many companies concurrently.

    Throttling on shared resources (Brave/DDG search, DNS) is enforced by the
    per-module global locks. With 3 workers and ~1.5s throttle, we still respect
    rate limits while overlapping the slow I/O across companies.
    """
    companies = list(companies)
    with ThreadPoolExecutor(max_workers=max_workers) as exe:
        return list(exe.map(enrich_company_partial, companies))


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

    # Authoritative shortcut: for small/micro businesses (Sirene size codes
    # 00..03 = 0–9 employees), the legal director registered at Sirene IS the
    # operational decision-maker — there's nobody else to triangulate against.
    # Treat that as a strong signal so the lead is not dropped just because we
    # couldn't find a website to corroborate.
    SMALL_BIZ_SIZES = {"00", "01", "02", "03", "11"}  # 0-19 employees
    AUTHORITATIVE_ROLES = (
        "gérant", "gerant", "président", "president", "co-gérant", "co-gerant",
        "directeur", "directrice", "fondateur", "fondatrice",
        "associé unique", "associee unique", "entrepreneur",
    )
    is_small_biz = (partial.get("size") or "") in SMALL_BIZ_SIZES
    role_is_authoritative = any(
        kw in (person_role or "").lower() for kw in AUTHORITATIVE_ROLES
    )
    if is_small_biz and role_is_authoritative and "sirene" in name_sources:
        name_sources.append("sirene-authoritative-sme")

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

    # 3. Email pattern + SMTP verify. We REFUSE to return a fake personal email
    # if SMTP couldn't actually confirm it AND we don't have an independent
    # corroborating source (the personal pattern email also appearing scraped on
    # the website). Otherwise the user thinks they have Marie's email when in
    # reality it bounces.
    email_field = ScoredField.missing()
    if domain and person_first and person_last:
        best, _ = find_best_email(person_first, person_last, domain)
        if best and best.email:
            # Does the pattern email appear on the company website? If yes,
            # that's an INDEPENDENT confirmation (not just an SMTP guess).
            personal_emails_on_site = (
                (partial.get("web_enrichment") or {}).get("emails_personal") or []
            )
            confirmed_on_site = best.email.lower() in {e.lower() for e in personal_emails_on_site}

            if best.status == "deliverable":
                # SMTP confirmed it routes — high confidence
                email_field = ScoredField(
                    value=best.email,
                    sources=[f"smtp-deliverable:{domain}"],
                    confidence=85 if not confirmed_on_site else 95,
                    note="smtp-deliverable" + (" + on-site" if confirmed_on_site else ""),
                )
                name_sources.append(f"smtp:{best.email}")
            elif confirmed_on_site:
                # Pattern is a guess via SMTP, but the SAME email appears on the
                # company website — strong independent signal, keep with medium conf.
                email_field = ScoredField(
                    value=best.email,
                    sources=["website-personal-email", f"pattern:{domain}"],
                    confidence=80,
                    note="confirmed on company website",
                )
                name_sources.append(f"website-email:{best.email}")
            elif best.status == "catch_all":
                # Catch-all server accepts anything — the pattern is a GUESS.
                # We expose it but with low confidence + crystal-clear note so
                # the user doesn't believe it's verified.
                email_field = ScoredField(
                    value=best.email,
                    sources=["pattern-guess:" + domain],
                    confidence=35,
                    note="catch-all domain — email is a likely pattern, NOT verified",
                )
            # Otherwise (not_deliverable, no_mx, smtp_unreachable without corroboration)
            # we leave email_field as missing — better empty than wrong.

    # 3b. Free Mobile Finder — try to find a personal mobile (06/07)
    # near the person's name in press pages, Calendly, articles, blogs.
    # Best-effort: ~15-25% hit rate (vs 70% with paid Kaspr). Always free.
    person_phone_field = ScoredField.missing()
    try:
        from mobile_finder import find_mobile_for_person
        @_cached("mobile", f"{person_first}|{person_last}|{partial.get('company_name', '')}")
        def _mobile():
            return find_mobile_for_person(
                person_first, person_last,
                partial.get("company_name") or "",
                website=partial.get("website"),
            )
        mobile_result = _mobile()
        if mobile_result:
            mobile, source = mobile_result
            person_phone_field = ScoredField(
                value=mobile,
                sources=[f"mobile-finder:{source}"],
                confidence=75,
                note=f"mobile found via {source}",
            )
            name_sources.append(f"mobile-near-name:{source}")
    except Exception:
        pass

    # 4. Person LinkedIn (filter: slug must contain first+last). The new
    # social_finder takes city + role hints to bump query precision.
    li_raw = find_linkedin_for_person(
        full_name,
        partial.get("company_name", ""),
        city=partial.get("city"),
        role=person_role or None,
    )
    if _name_in_linkedin_url(person_first, person_last, li_raw):
        li_field = ScoredField.from_single(li_raw, "ddg:linkedin-in", verified=True)
        name_sources.append(f"linkedin:{li_raw}")
    else:
        li_field = ScoredField.missing()

    # 5. Person Instagram (best-effort, weak signal)
    ig_raw = find_instagram_for_person(
        full_name,
        partial.get("company_name", ""),
        city=partial.get("city"),
    )
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
    # Sirene-sourced names are authoritative for FR companies — never DROP a
    # lead just because we couldn't triangulate the name. We can still drop
    # later if there's no contact channel at all.
    if name_field.value and "sirene" in name_sources and name_field.confidence < 70:
        name_field.confidence = 70
        name_field.note = (name_field.note or "") + " · sirene-authoritative"

    lead = Lead(
        company_name=partial["company_name"],
        company_siren=partial.get("siren"),
        company_naf=partial.get("naf"),
        company_naf_label=naf_label,
        company_city=partial.get("city"),
        company_address=partial.get("address"),
        company_size=partial.get("size"),
        company_website=partial.get("website"),
        company_email=partial.get("company_email"),
        company_linkedin=ScoredField(**partial["company_linkedin"]),
        company_instagram=ScoredField(**partial["company_instagram"]),
        company_phone=ScoredField(**partial["company_phone"]),
        person_name=name_field,
        person_role=role_field,
        person_email=email_field,
        person_phone=person_phone_field,
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
