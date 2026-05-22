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
import re
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable, Optional

from rich.console import Console

from bettercontact_client import enrich_person as bettercontact_enrich, have_bettercontact_key
from bodacc_client import qualify_from_bodacc
from francetravail_client import (
    have_francetravail_keys,
    hiring_signal_for_siret,
)
from tech_stack import (
    detect_tech_from_html,
    maturity_score as tech_maturity_score,
    pitch_hint_from_tech,
)
from careers_page import scan_careers_for
from datagma_client import find_full as datagma_find, have_datagma_key
from dropcontact_client import enrich_person as dropcontact_enrich, have_dropcontact_key
from email_finder import find_best_email
from email_pattern_engine import guess_emails as guess_email_patterns
from google_places import find_business_place, normalize_place
from here_maps import find_business_here, have_here_key
from hunter_client import (
    find_email_by_domain as hunter_find_email,
    have_hunter_key,
    verify_email as hunter_verify,
)
from phone_utils import classify as classify_phone, normalize_fr_phone
from mentions_legales import extract_legal_contacts
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
        date_creation = getattr(c, "date_creation", None)
        # Pydantic model: original siege might be in extra dict
        original_siege = (c.model_dump().get("_original_siege") or {}) if hasattr(c, 'model_dump') else {}
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
        # ORIGINAL siege (before sirene_client rewrote it to the local
        # establishment) — used to detect subsidiaries of out-of-area HQs.
        original_siege = c.get("_original_siege") or {}
        city = siege.get("libelle_commune")
        address = ", ".join(
            x for x in [siege.get("adresse"), siege.get("code_postal"), siege.get("libelle_commune")] if x
        )
        size = c.get("tranche_effectif_salarie")
        date_creation = c.get("date_creation")
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
    # For chains (size ≥ 50 employees = code 21+) we ALSO fetch the local
    # SIRET to get the actual local phone/address (which differs from the HQ
    # that Sirene's SIREN points to).
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

    # 1-bis. Chain detection: when the company has 50+ employees (size code
    # 21+) we're likely looking at a chain HQ. Pull the LOCAL SIRET data to
    # get the actual establishment phone/address — not the chain's siège.
    LARGE_SIZES = {"21", "22", "31", "32", "41", "42", "51", "52", "53"}
    is_chain_local = (size or "") in LARGE_SIZES
    if is_chain_local and have_pappers_key():
        # Get SIRET from the Sirene siege (already collected)
        siret = None
        if hasattr(sirene_company, "model_dump"):
            siege = getattr(c, "siege", None)
            if siege:
                siret = getattr(siege, "siret", None) if not isinstance(siege, dict) else siege.get("siret")
        else:
            siret = ((sirene_company.get("siege") or {}).get("siret")
                     if isinstance(sirene_company, dict) else None)
        if siret and len(siret) == 14:
            @_cached("pappers_siret", siret)
            def _fetch_siret():
                from pappers_client import PappersClient
                with PappersClient() as pc:
                    return pc.get_by_siret(siret)
            site_data = _fetch_siret() or {}
            # Local phone from the establishment beats the HQ phone we got via SIREN
            local_phone = (site_data.get("telephone") or
                           (site_data.get("etablissement") or {}).get("telephone"))
            if local_phone and not pappers_phone:
                pappers_phone = local_phone

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

    # Quality flags accumulated during enrichment — surfaced on the final Lead
    quality_flags: list[str] = []

    # BODACC qualification — free public API, returns recent legal events.
    # Hard-drops leads in redressement/liquidation/radiation; boosts those
    # with augmentation de capital (= growing). Cached per SIREN.
    bodacc_result: dict = {}
    if siren:
        @_cached("bodacc", siren)
        def _bodacc():
            try:
                return qualify_from_bodacc(siren)
            except Exception:
                return None
        bodacc_result = _bodacc() or {}
        verdict = bodacc_result.get("verdict")
        if verdict == "HARD_DROP":
            quality_flags.append(f"bodacc-drop:{bodacc_result.get('categories_found', [''])[0]}")
        elif verdict == "QUALITY_BOOST":
            quality_flags.append(f"bodacc-boost:{bodacc_result.get('categories_found', [''])[0]}")
        elif verdict == "WATCHOUT":
            quality_flags.append("bodacc-watchout")

    # FRANCE TRAVAIL hiring signal — boîte qui recrute = budget formation IA.
    # Requires SIRET (establishment), so we pull from siege.siret which has
    # been rewritten to the local matching_etablissement by sirene_client.
    ft_signal: dict = {}
    if have_francetravail_keys():
        siret = None
        if isinstance(siege, dict):
            siret = siege.get("siret")
        elif hasattr(siege, "siret"):
            siret = getattr(siege, "siret", None)
        if siret:
            @_cached("francetravail", siret)
            def _ft():
                try:
                    return hiring_signal_for_siret(siret)
                except Exception:
                    return None
            ft_signal = _ft() or {}
            intensity = ft_signal.get("hiring_intensity")
            if intensity == "high":
                quality_flags.append(f"hiring-high:{ft_signal.get('n_offres')}")
            elif intensity == "medium":
                quality_flags.append(f"hiring-medium:{ft_signal.get('n_offres')}")
            elif intensity == "none":
                quality_flags.append("hiring-none")

    # SUBSIDIARY DETECTION: if Sirene's ORIGINAL siege (HQ before local_only
    # rewrote it) is in a different department than the matched establishment,
    # this lead is a SUBSIDIARY of a company headquartered elsewhere.
    # Decision-makers for AI/training budgets are typically at HQ, not in
    # the local branch.
    try:
        local_dept = (siege.get("code_postal") or "")[:2] if isinstance(siege, dict) else ""
        original_dept = (original_siege.get("code_postal") or "")[:2] if isinstance(original_siege, dict) else ""
        if original_dept and local_dept and original_dept != local_dept:
            hq_city = original_siege.get("libelle_commune") or original_dept
            quality_flags.append(f"subsidiary-hq:{hq_city}")
    except Exception:
        pass

    # 2b-d. OSM + GMB + HERE — these 3 sources are INDEPENDENT (none reads
    # the others' results). Run them in parallel via a small thread pool —
    # cuts wall-time from ~6s sequential to ~2s parallel.
    @_cached("osm", f"{name}|{city or ''}")
    def _osm():
        try:
            from osm_finder import find_business_on_osm
            return find_business_on_osm(name, city)
        except Exception:
            return None

    @_cached("gmb", f"{name}|{city or ''}")
    def _gmb_lookup():
        try:
            p = find_business_place(name, city)
            return normalize_place(p) if p else None
        except Exception:
            return None

    @_cached("here", f"{name}|{city or ''}")
    def _here_lookup():
        if not have_here_key():
            return None
        try:
            return find_business_here(name, city)
        except Exception:
            return None

    osm_data: Optional[dict] = None
    gmb_data: dict = {}
    here_data: dict = {}
    from concurrent.futures import ThreadPoolExecutor as _PartialTPE
    with _PartialTPE(max_workers=3) as exe:
        futs = {
            "osm":  exe.submit(_osm),
            "gmb":  exe.submit(_gmb_lookup),
            "here": exe.submit(_here_lookup),
        }
        osm_data = futs["osm"].result()
        gmb_data = futs["gmb"].result() or {}
        here_data = futs["here"].result() or {}

    # Promote website from any of the 3 sources if we don't have one
    if not website:
        if osm_data and osm_data.get("website"):
            website = osm_data["website"]
        elif gmb_data.get("website"):
            website = gmb_data["website"].rstrip("/")
        elif here_data.get("website"):
            website = here_data["website"].rstrip("/")

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

    # 3+4. Run web_enrichment, mentions_legales, LinkedIn co, Insta co IN
    # PARALLEL. These were previously sequential; the search calls don't
    # depend on the scrape results. Cuts another ~3-5s/lead on wall-time.
    @_cached("web_enrichment", website or "no_site")
    def _scrape():
        if not website:
            return None
        we = enrich_company_from_website(website, fetch_team_page=True)
        return we.model_dump() if we else None

    @_cached("mentions_legales", website or "no_site")
    def _mentions_fn():
        if not website:
            return None
        try:
            return extract_legal_contacts(website)
        except Exception:
            return None

    @_cached("li_co", f"{name}|{city or ''}")
    def _li_search_fn():
        return find_linkedin_for_company(name, city)

    @_cached("ig_co", f"{name}|{city or ''}")
    def _ig_search_fn():
        return find_instagram_for_company(name, city)

    # v0.15.0 — careers page scan (free, parallel with the other web fetches)
    @_cached("careers", website or "no_site")
    def _careers_fn():
        if not website:
            return None
        try:
            return scan_careers_for(website)
        except Exception:
            return None

    web_dict: Optional[dict] = None
    mentions: dict = {}
    li_search: Optional[str] = None
    ig_search: Optional[str] = None
    careers_data: Optional[dict] = None
    with _PartialTPE(max_workers=5) as exe:
        futs = {
            "web":      exe.submit(_scrape),
            "mentions": exe.submit(_mentions_fn),
            "li_co":    exe.submit(_li_search_fn),
            "ig_co":    exe.submit(_ig_search_fn),
            "careers":  exe.submit(_careers_fn),
        }
        web_dict = futs["web"].result()
        mentions = futs["mentions"].result() or {}
        li_search = futs["li_co"].result()
        ig_search = futs["ig_co"].result()
        careers_data = futs["careers"].result()

    # v0.15.0 — surface careers signal as a quality_flag for visibility
    if careers_data and careers_data.get("tilt_categories"):
        tcats = careers_data["tilt_categories"]
        if any(c in ("ai", "data", "automation") for c in tcats):
            quality_flags.append(f"tilt:tech-hiring:{','.join(tcats)}")

    # Pull the bits we need out of the scraped dict
    web_li = (web_dict or {}).get("linkedin_company")
    web_ig = (web_dict or {}).get("instagram_account")
    web_phones = (web_dict or {}).get("phones") or []

    # Search results take priority only if the website didn't yield them
    if web_li:
        li_search = None
    if web_ig:
        ig_search = None

    # 5. Triangulate company-level fields
    li_field = triangulate_url(
        [(web_li, website or "website"),
         (li_search, "search:linkedin-company")],
    )
    # Cross-company DETECTION for LinkedIn entreprise: when a LinkedIn URL
    # was found, check the /company/<slug> matches the Sirene company name.
    # If not (e.g. 'flaveurs' → linkedin.com/company/magazine-flaveurs which
    # is a different business), downgrade confidence + add a CROSS-COMPANY
    # note so the salesperson knows.
    if li_field.value:
        from urllib.parse import urlparse as _urlp
        path = (_urlp(li_field.value).path or "").lower()
        if "/company/" in path:
            li_slug = path.split("/company/")[-1].split("/")[0]
            li_slug_clean = re.sub(r"[^a-z0-9]+", "", li_slug)
            import unicodedata as _ud
            def _norm(s):
                s = _ud.normalize("NFKD", s or "")
                s = "".join(c for c in s if not _ud.combining(c)).lower()
                return re.sub(r"[^a-z0-9]+", "", s)
            co_slug = _norm(name)
            # Strip common FR corp prefixes (SARL, SAS, EURL...) before compare
            co_slug_clean = re.sub(
                r"^(?:sas|sarl|eurl|sasu|sa|snc|scop|scic|scp|selas|selarl)",
                "",
                co_slug,
            )
            li_matches = bool(li_slug_clean and (
                li_slug_clean == co_slug
                or li_slug_clean == co_slug_clean
                or (len(li_slug_clean) >= 4 and len(co_slug_clean) >= 4
                    and (li_slug_clean in co_slug_clean or co_slug_clean in li_slug_clean))
            ))
            if not li_matches:
                # Keep the URL (still useful info), but lower confidence and flag
                li_field.confidence = min(li_field.confidence, 35)
                li_field.note = (
                    f"CROSS-COMPANY: LinkedIn slug '{li_slug}' "
                    f"does not match Sirene name '{name}'. Verify before use."
                )
        # Foreign-subsidiary detection: linkedin.com host like
        # 'uk.linkedin.com', 'us.linkedin.com', 'in.linkedin.com' means the
        # company is the LOCAL French presence of a foreign multinational
        # → decision-makers (DRH, etc.) are NOT in Toulouse, they're in
        # London/NYC/Bangalore. Flag and downgrade.
        from urllib.parse import urlparse as _urlp2
        host = (_urlp2(li_field.value).hostname or "").lower()
        if host.endswith(".linkedin.com") and host != "www.linkedin.com":
            tld_prefix = host.split(".linkedin.com")[0].split(".")[-1]
            if tld_prefix not in ("fr", "www", "", "media"):
                li_field.confidence = min(li_field.confidence, 25)
                prev_note = li_field.note + " · " if li_field.note else ""
                li_field.note = (
                    f"{prev_note}FOREIGN SUBSIDIARY: LinkedIn TLD '{tld_prefix}' "
                    f"= the French entity is a subsidiary of a foreign group. "
                    f"Decision-maker is NOT in France."
                )
                quality_flags.append(f"foreign-subsidiary:{tld_prefix}")
    ig_field = triangulate_url(
        [(web_ig, website or "website"),
         (ig_search, "search:instagram-company")],
    )
    # Company phone: gather EVERY plausible FR phone from all sources, then
    # triangulate. Foreign / invalid numbers are filtered out (eg the bogus
    # +14055947026 we saw for MOOD via OSM-tag-of-different-business).
    from phone_utils import classify as _classify_phone, normalize_fr_phone as _norm_fr
    phone_sources: list[tuple[Optional[str], str]] = []

    def _push_phone(raw: Optional[str], source: str) -> None:
        if not raw:
            return
        kind = _classify_phone(raw)
        if kind in ("foreign", "invalid", "special"):
            return
        norm = _norm_fr(raw) or raw
        phone_sources.append((norm, source))

    if pappers_phone:
        _push_phone(pappers_phone, "pappers")
    for p in web_phones[:5]:
        _push_phone(p, website or "website")
    # Mentions Légales phone — high-trust (legally mandated)
    _push_phone(mentions.get("company_phone"), "mentions-legales")
    _push_phone(mentions.get("director_phone"), "mentions-legales")
    # OSM tag — ALWAYS try it, not just when phone_sources is empty.
    # Multiple sources strengthen triangulation confidence (90 vs 35).
    if osm_data and osm_data.get("phone"):
        _push_phone(osm_data["phone"], "osm")
    # Google My Business listed phone — Google's source of truth for what
    # this business publishes publicly.
    if gmb_data.get("phone"):
        _push_phone(gmb_data["phone"], "google-places")
    # HERE Maps listed phone — usually present even when Google's isn't,
    # since HERE aggregates Tripadvisor + Yelp + carrier directories.
    if here_data.get("phone"):
        _push_phone(here_data["phone"], "here-maps")
    # PJ behind Cloudflare — best-effort, fails silently
    if not phone_sources:
        @_cached("pagesjaunes", f"{name}|{city or ''}")
        def _pj():
            try:
                from pagesjaunes_client import find_phone_on_pagesjaunes
                return find_phone_on_pagesjaunes(name, city)
            except Exception:
                return None
        pj_phone = _pj()
        _push_phone(pj_phone, "pagesjaunes")

    phone_field = triangulate_phone(phone_sources) if phone_sources else ScoredField.missing()
    # If Pappers / OSM / Pages Jaunes / Mentions Légales is the source (one
    # alone is enough), boost to 70 — these are authoritative.
    if phone_field.value and phone_field.confidence < 70:
        srcs = phone_field.sources or []
        if any(s in ("pappers", "pagesjaunes", "osm", "mentions-legales") for s in srcs):
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
    # Mentions Légales: director_email + company_email from legal page
    for k in ("director_email", "company_email"):
        v = mentions.get(k)
        if v and v not in company_email_candidates:
            company_email_candidates.append(v)

    # Last-resort company_email: when no source gave us anything, try the
    # standard "contact@{domain}" / "info@{domain}" / "hello@{domain}" patterns
    # on the verified website domain. Tagged as pattern-guess with low conf,
    # only if it has a working MX record (kills hallucinations).
    if not company_email_candidates and website:
        from urllib.parse import urlparse as _urlparse
        host = (_urlparse(website).hostname or "").lower().removeprefix("www.")
        if host and "." in host:
            try:
                from email_pattern_engine import _domain_has_mx
                if _domain_has_mx(host):
                    for prefix in ("contact", "info", "hello", "bonjour"):
                        company_email_candidates.append(f"{prefix}@{host}")
                        break  # one is enough at this stage
            except Exception:
                pass

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

    # v0.15.0 — lifecycle stage from Sirene date_creation
    company_age_months: Optional[int] = None
    lifecycle_stage: Optional[str] = None
    if date_creation:
        try:
            from datetime import datetime as _dt
            d = _dt.strptime(date_creation[:10], "%Y-%m-%d")
            now = _dt.now()
            months = (now.year - d.year) * 12 + (now.month - d.month)
            company_age_months = max(0, months)
            if months < 6:
                lifecycle_stage = "very-early"          # < 6 mois : trop tôt
            elif months < 24:
                lifecycle_stage = "scaling"             # 6-24 mois : TILT
            elif months < 60:
                lifecycle_stage = "mature"              # 2-5 ans
            else:
                lifecycle_stage = "legacy"              # > 5 ans
        except Exception:
            pass

    return {
        "company_name": name,
        "siren": siren,
        "naf": naf,
        "city": city,
        "address": address,
        "size": size,
        "date_creation": date_creation,
        "company_age_months": company_age_months,
        "lifecycle_stage": lifecycle_stage,
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
        # Mentions légales — used by finalize_lead for person-level enrichment
        "mentions_legales": mentions,
        # Google My Business — cuisine type, rating, photos. Used downstream
        # for ICP scoring (a "Végétarienne" is dropped from a spirits-brand
        # campaign, a "Bar à cocktails" is boosted).
        "gmb": gmb_data,
        "cuisine_type": gmb_data.get("type") or gmb_data.get("category"),
        "gmb_rating": gmb_data.get("rating"),
        "gmb_rating_count": gmb_data.get("rating_count"),
        "is_operating": gmb_data.get("is_operating"),
        "permanently_closed": gmb_data.get("permanently_closed"),
        # Quality flags surfaced during partial enrichment
        "quality_flags": quality_flags,
        # BODACC qualification (free public API — recent legal events)
        "bodacc_verdict": bodacc_result.get("verdict"),
        "bodacc_reason": bodacc_result.get("reason"),
        "bodacc_categories": bodacc_result.get("categories_found"),
        "bodacc_modifier": bodacc_result.get("icp_modifier") or 0,
        # France Travail hiring signal (when API key set)
        "ft_hiring_intensity": ft_signal.get("hiring_intensity"),
        "ft_n_offres": ft_signal.get("n_offres"),
        "ft_top_titles": ft_signal.get("top_titles"),
        "ft_reason": ft_signal.get("reason"),
        # Tech stack (always-on, from web_dict — Wappalyzer-LITE)
        "tech_stack": (web_dict or {}).get("tech_stack"),
        "tech_signals": (web_dict or {}).get("tech_signals"),
        "tech_categories": (web_dict or {}).get("tech_categories"),
        "primary_cms": (web_dict or {}).get("primary_cms"),
        # v0.15.0 — Careers page (job offers scraped directly from the site)
        "careers_url": (careers_data or {}).get("careers_url"),
        "careers_n_jobs": (careers_data or {}).get("n_jobs") or 0,
        "careers_top_titles": (careers_data or {}).get("top_titles") or [],
        "careers_tech_signals": (careers_data or {}).get("tech_signals") or {},
        "careers_tilt_categories": (careers_data or {}).get("tilt_categories") or [],
        "careers_icp_modifier": (careers_data or {}).get("icp_modifier") or 0,
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
    """LinkedIn /in/<slug> slugs usually contain firstlast or first-last.

    CRITICAL: we strip accents before comparing — French names like 'Hervé'
    become 'herve' in the slug, and the previous code was failing on every
    accented gérant name.
    """
    if not url or not first or not last:
        return False
    import re as _re
    import unicodedata as _ud
    def _strip(s: str) -> str:
        s = _ud.normalize("NFKD", s)
        return "".join(c for c in s if not _ud.combining(c)).lower()
    slug_raw = url.rstrip("/").rsplit("/", 1)[-1]
    slug = _re.sub(r"[^a-z]+", "", _strip(slug_raw))
    first_s = _re.sub(r"[^a-z]+", "", _strip(first))
    last_s = _re.sub(r"[^a-z]+", "", _strip(last))
    # Accept if BOTH first and last appear in slug, OR if just last (long
    # enough to be distinctive) appears — some slugs use only the last name
    # plus a numeric suffix.
    if first_s and last_s:
        if first_s in slug and last_s in slug:
            return True
    if len(last_s) >= 6 and last_s in slug:
        return True
    return False


# Companies whose Sirene name matches one of these patterns are NOT real
# private B2B prospects for AI training / consulting. They land in the NAF
# 70.22Z bucket by accident (mis-classified), but are:
#   - public agencies (ADEROC, agence de développement, agence économique)
#   - unions / syndicats / federations professionnelles
#   - mutuelles d'assurance (GALIAN, MAIF...)
#   - peps syndicats salariés
#   - chambres de commerce / d'agriculture / des métiers
# Drop them via preliminary_score → cheap pass only → low ICP → bottom of list.
_JUNK_NAME_RX = __import__("re").compile(
    r"\b("
    r"syndicat|union\s+(?:des|de|nationale|professionnelle)|"
    r"f[eé]d[eé]ration|conf[eé]d[eé]ration|"
    # Mutuelles d'assurance — by name or known abbreviations
    r"mutuelle|maif|macif|matmut|harmonie|smabtp|sma\s|smacl|groupama|generali|"
    # Public agencies / EPIC / EPA
    r"agence\s+(?:de\s+)?(?:d[eé]veloppement|publique|r[eé]gionale|locale)|"
    r"chambre\s+(?:de\s+commerce|d[''']agriculture|des?\s+m[eé]tiers)|"
    r"ordre\s+des?\s+(?:experts?|m[eé]decins|avocats|architectes)|"
    r"udife|adero?c|peps|"
    r"comit[eé]\s+(?:r[eé]gional|d[eé]partemental|territorial)|"
    r"^association\b|association\s+(?:nationale|fran[cç]aise|professionnelle)|"
    r"office\s+(?:public|de\s+tourisme)|"
    r"epic|epa|epcc|sem\s|sad\s"
    r")\b",
    __import__("re").IGNORECASE,
)


def is_junk_company_name(name: str) -> bool:
    """True when the name strongly suggests this isn't a real private B2B
    prospect (union, mutuelle, agence publique, etc.).

    Used by preliminary_score to drop to cheap-pass + by ICP scoring to
    apply a hard penalty.
    """
    if not name:
        return False
    return bool(_JUNK_NAME_RX.search(name))


# NAF prefixes for sectors where GMB cuisine_type / rating are meaningful
# signals (consumer-facing businesses with Google reviews). For B2B services,
# manufacturing, SaaS, etc., these signals are usually absent / irrelevant.
_GMB_RELEVANT_NAF_PREFIXES = (
    "55", "56",  # hôtels / restaurants / cafés
    "47",        # commerce de détail
    "96",        # services personnels (coiffeur, esthéticienne, etc.)
    "86", "87",  # santé (cabinet médical, EHPAD)
    "85.5",      # formation continue (visible auprès du grand public)
    "93",        # sport / loisirs
    "79",        # agences de voyages
)


def _is_gmb_relevant_sector(naf: str) -> bool:
    """Should we expect (and weight) GMB cuisine_type + rating signals?

    Returns True for consumer-facing sectors (CHR, retail, healthcare,
    consumer services). Returns False for pure B2B (SaaS, conseil, industrie)
    where GMB listings are rare/absent and shouldn't drive the score.
    """
    if not naf:
        return False
    return any(naf.startswith(p) for p in _GMB_RELEVANT_NAF_PREFIXES)


def preliminary_score(partial: dict) -> int:
    """Cheap-source-only 'is this lead worth paid enrichment' score (0-100).

    Computed from the FREE-tier data already collected in `partial` (Sirene +
    Pappers free + scraping + OSM + GMB + HERE). Used by run_campaign's
    two-pass strategy: if score < threshold, skip the paid waterfall
    (Dropcontact + Hunter + Datagma + BetterContact) to save credits.

    SECTOR-AWARE: for B2B-only NAFs (SaaS, conseil, industrie), GMB signals
    are irrelevant — we don't penalise missing cuisine_type. For
    consumer-facing NAFs (CHR, retail), GMB signals are required.

    Rules (additive, capped at 100):
      -100 if junk name (syndicat / mutuelle / agence publique / etc.)
      +20 has a verified company website (strict-FR-filter passed)
      +20 has a phone (any source)
      +15 has company LinkedIn entreprise
      +15 has at least one dirigeant nominatif
      +10 has BODACC growth signal (augmentation capital, créations, etc.)
      +20 France Travail hiring intensity = high (≥10 offres/30j)
      +10 France Travail hiring intensity = medium (4-9 offres/30j)
      +5  France Travail hiring intensity = low (1-3 offres/30j)
      -5  France Travail hiring intensity = none (gel d'embauche)
      +15 tech_stack has has-automation (Zapier/Make/n8n)
      +10 tech_stack has has-crm (HubSpot/Salesforce/Pipedrive)
      +5  tech_stack has has-framework (Next.js/React/Vue)
      +2  per soft signal (payment, analytics, chat, compliance — cap +6)
      +30 careers page open with AI/data/automation roles (cap +30)
      +15 careers page open with tech roles
      +15 lifecycle_stage = scaling (Sirene age 6-24 mois)
      -5  lifecycle_stage = very-early (< 6 mois)
      +40 ACTIVE matching RFP (BOAMP) — strongest possible signal
      [If GMB-relevant NAF only:]
      +20 has GMB cuisine_type
      +15 has GMB rating >= 4.0
      +10 has GMB rating_count >= 20
    """
    # Hard reject: junk names are mis-classified entities. No paid enrichment.
    if is_junk_company_name(partial.get("company_name", "")):
        return 0
    score = 0
    naf = partial.get("naf") or ""
    gmb_relevant = _is_gmb_relevant_sector(naf)

    # Universal signals (apply to all sectors)
    if partial.get("website"):
        score += 20
    co_phone = (partial.get("company_phone") or {}).get("value")
    if co_phone:
        score += 20
    co_li = (partial.get("company_linkedin") or {}).get("value")
    if co_li:
        score += 15
    dirs = partial.get("legal_dirigeants") or []
    if dirs and dirs[0].get("first") and dirs[0].get("last"):
        score += 15
    # BODACC growth signals = company investing → good for any pitch
    bodacc_verdict = partial.get("bodacc_verdict")
    if bodacc_verdict == "QUALITY_BOOST":
        score += 10

    # FRANCE TRAVAIL hiring signal — boîte qui recrute = budget formation IA
    # disponible. Strongest free signal we have for AI/training upsells.
    ft_intensity = partial.get("ft_hiring_intensity")
    if ft_intensity == "high":
        score += 20      # 10+ offres → hyper-croissance
    elif ft_intensity == "medium":
        score += 10      # 4-9 offres → croissance soutenue
    elif ft_intensity == "low":
        score += 5       # 1-3 offres → recrutement modéré
    elif ft_intensity == "none":
        score -= 5       # 0 offre 30j → peut-être gel d'embauche

    # TECH STACK signals — has-automation = AI-ready pour le niveau 3
    tech_signals = set(partial.get("tech_signals") or [])
    if "has-automation" in tech_signals:
        score += 15      # Zapier/Make/n8n → équipe déjà sur l'automatisation
    if "has-crm" in tech_signals:
        score += 10      # HubSpot/Salesforce/Pipedrive → maturité B2B
    if "has-framework" in tech_signals:
        score += 5       # Next.js/React → équipe tech moderne
    # has-payment / has-analytics / has-chat / has-compliance => +2 each (cap 6)
    soft_signals = {"has-payment", "has-analytics", "has-chat", "has-compliance"}
    score += min(6, 2 * len(tech_signals & soft_signals))

    # v0.15.0 — CAREERS PAGE signals (recrutement direct sur le site)
    # Complète FT: capture les rôles cadres / tech que FT rate.
    careers_mod = partial.get("careers_icp_modifier") or 0
    score += min(30, int(careers_mod))   # +30 max si AI/data/automation ouverts

    # v0.15.0 — LIFECYCLE STAGE (Sirene date_creation)
    stage = partial.get("lifecycle_stage")
    if stage == "scaling":
        score += 15      # 6-24 mois = besoin de structurer = budget IA/CRM/auto
    elif stage == "very-early":
        score -= 5       # < 6 mois = trop tôt, pas budget
    # mature/legacy = neutre

    # v0.15.0 — APPEL D'OFFRES ACTIF (signal d'intention d'achat MAXIMUM)
    # Une boîte avec un AO actif matchant notre offre = LITTÉRALEMENT en train
    # de chercher à acheter. Boost massif, plafonné par le cap à 100.
    if partial.get("rfp_active"):
        score += 40

    # CHR/retail-only signals
    if gmb_relevant:
        if partial.get("cuisine_type"):
            score += 20
        r = partial.get("gmb_rating")
        if r is not None:
            try:
                if float(r) >= 4.0:
                    score += 15
            except Exception:
                pass
        rc = partial.get("gmb_rating_count")
        if rc is not None:
            try:
                if int(rc) >= 20:
                    score += 10
            except Exception:
                pass
    else:
        # For B2B-only sectors, GMB signals are usually absent. Substitute
        # 'company has 50+ employees per Sirene' as a quality proxy (= real
        # mature company, not a one-person SCI / shell).
        size = partial.get("size") or ""
        if size in ("21", "22", "31", "32", "41", "42", "51", "52", "53"):
            score += 15

    return min(100, score)


def finalize_lead(
    partial: dict,
    *,
    person_first: str,
    person_last: str,
    person_role: str,
    person_sources: list[str],
    naf_label: Optional[str] = None,
    use_paid_sources: bool = True,
) -> Lead:
    """Build a triangulated Lead from a partial + Claude's decision-maker pick.

    Auto-triangulates the person's name against the website team page, the
    discovered email, and the LinkedIn profile slug. So a name initially backed
    by one source (Sirene) can climb to high confidence if the website team
    page mentions it AND the LinkedIn URL contains it AND the SMTP probe
    accepts the matching email pattern.

    `use_paid_sources`: when False, skips the entire paid waterfall
    (Dropcontact + Hunter + Datagma + BetterContact). Used by the two-pass
    strategy in run_campaign to spend paid credits only on high-value leads.
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
    # Person enrichment WATERFALL: Dropcontact → Hunter → Datagma → BetterContact.
    # We stop early once we have both email AND mobile (no need to spend more
    # credits if we got everything). Each source has its own free tier so the
    # default config is "all-free, cumulative coverage".
    #
    # Two-pass gate: when use_paid_sources=False, skip the entire waterfall.
    # run_campaign sets this based on a preliminary_score(partial) check —
    # leads scoring < threshold don't get paid enrichment, saving credits.
    dc_result = None
    if use_paid_sources and have_dropcontact_key() and person_first and person_last:
        @_cached("dropcontact", f"{person_first}|{person_last}|{partial.get('company_name', '')}")
        def _dc():
            try:
                return dropcontact_enrich(
                    person_first, person_last,
                    partial.get("company_name") or "",
                    website=partial.get("website"),
                )
            except Exception:
                return None
        dc_result = _dc()

    # Aggregate the waterfall results in a single dict (later cascade sources
    # fill in what earlier ones missed). Each layer is OPT-IN by key presence.
    waterfall_email: Optional[str] = (dc_result or {}).get("email")
    waterfall_phone: Optional[str] = (dc_result or {}).get("phone")
    waterfall_linkedin: Optional[str] = (dc_result or {}).get("linkedin")
    waterfall_sources: list[str] = ["dropcontact"] if dc_result else []

    # 2nd source: Hunter (50 free credits/mo) — try when we don't have an
    # email yet AND we have a website domain to query.
    if use_paid_sources and not waterfall_email and have_hunter_key() and partial.get("website") and person_first and person_last:
        from urllib.parse import urlparse as _urlp
        host = (_urlp(partial["website"]).hostname or "").lower().removeprefix("www.")
        if host:
            @_cached("hunter", f"{person_first}|{person_last}|{host}")
            def _hunter():
                try:
                    return hunter_find_email(person_first, person_last, host)
                except Exception:
                    return None
            h = _hunter() or {}
            if h.get("email"):
                waterfall_email = h["email"]
                waterfall_sources.append(f"hunter(score={h.get('score')})")

    # SHORT-CIRCUIT: if we already have email + mobile + LinkedIn from
    # Dropcontact + Hunter, no need to spend Datagma (expensive: 30 credits
    # per mobile lookup) or BetterContact (slow: 30s polling).
    waterfall_good_enough = bool(
        waterfall_email and waterfall_phone and waterfall_linkedin
    )

    # 3rd source: Datagma (50 free credits + 160 API matches) — French
    # specialist. Try when we still need email OR mobile AND we don't already
    # have the full set.
    if use_paid_sources and not waterfall_good_enough and (not waterfall_email or not waterfall_phone) and have_datagma_key() and person_first and person_last:
        @_cached("datagma", f"{person_first}|{person_last}|{partial.get('company_name', '')}")
        def _dg():
            try:
                # If we have the email already, skip the mobile lookup to save 30 credits
                return datagma_find(person_first, person_last,
                                     partial.get("company_name") or "",
                                     want_phone=not waterfall_phone)
            except Exception:
                return None
        dg = _dg() or {}
        if dg.get("email") and not waterfall_email:
            waterfall_email = dg["email"]
            waterfall_sources.append("datagma")
        if dg.get("phone") and not waterfall_phone:
            waterfall_phone = dg["phone"]
            waterfall_sources.append("datagma-phone")
        if dg.get("linkedin") and not waterfall_linkedin:
            waterfall_linkedin = dg["linkedin"]

    # Re-check after Datagma: maybe we're now good enough.
    waterfall_good_enough = bool(
        waterfall_email and waterfall_phone and waterfall_linkedin
    )

    # 4th source: BetterContact (50 free credits, PAY-PER-VALID, 20+ providers).
    # Last resort because the polling is slow (~30s). Skip entirely when we
    # have a "good enough" set or when we have just email+phone (no need to
    # pay the BC poll for marginal upside).
    if use_paid_sources and not waterfall_good_enough and (not waterfall_email or not waterfall_phone) and have_bettercontact_key() and person_first and person_last:
        from urllib.parse import urlparse as _urlp
        host = ""
        if partial.get("website"):
            host = (_urlp(partial["website"]).hostname or "").lower().removeprefix("www.")
        @_cached("bettercontact", f"{person_first}|{person_last}|{partial.get('company_name', '')}|{host}")
        def _bc():
            try:
                return bettercontact_enrich(
                    person_first, person_last,
                    partial.get("company_name") or "",
                    linkedin_url=waterfall_linkedin,
                    company_domain=host or None,
                    enrich_email=not waterfall_email,
                    enrich_phone=not waterfall_phone,
                )
            except Exception:
                return None
        bc = _bc() or {}
        if bc.get("email") and not waterfall_email:
            waterfall_email = bc["email"]
            waterfall_sources.append("bettercontact")
        if bc.get("phone") and not waterfall_phone:
            waterfall_phone = bc["phone"]
            waterfall_sources.append("bettercontact-phone")
        if bc.get("linkedin") and not waterfall_linkedin:
            waterfall_linkedin = bc["linkedin"]

    # Overwrite dc_result with the consolidated waterfall result so the
    # downstream code paths (cross-company detection, email/phone assignment)
    # see the best value found across all sources.
    if waterfall_email or waterfall_phone or waterfall_linkedin:
        dc_result = {
            "email": waterfall_email,
            "phone": waterfall_phone,
            "linkedin": waterfall_linkedin,
            "email_qualification": (dc_result or {}).get("email_qualification"),
            "company_siren": (dc_result or {}).get("company_siren"),
            "_waterfall_sources": waterfall_sources,
        }

    # CROSS-COMPANY DETECTION on Dropcontact result. Dropcontact returns the
    # person's CURRENT email, which may be at a DIFFERENT employer than the
    # SMB we just looked up in Sirene. Examples:
    #   - Julien Zuccarelli is gérant of TABLAPIZZA per Sirene, but Dropcontact
    #     returns zuccarelli.julien@tastycloud.fr (his SaaS employer)
    #   - Hervé Sichel-Dulong is gérant of FLAVEURS per Sirene, but Dropcontact
    #     returns herve@flaveursdurocher.com (a DIFFERENT 'Flaveurs' business)
    # We KEEP the value (it's still a valid email for the person) but flag it
    # so the salesperson knows their pitch needs to address this.
    dc_cross_company = False
    if dc_result and dc_result.get("email"):
        import unicodedata as _ud
        def _slug(s: str) -> str:
            s = _ud.normalize("NFKD", s or "")
            s = "".join(c for c in s if not _ud.combining(c)).lower()
            return re.sub(r"[^a-z0-9]+", "", s)
        email_domain = dc_result["email"].split("@", 1)[-1].lower()
        email_host_slug = _slug(email_domain.split(".")[0])
        company_slug = _slug(partial.get("company_name") or "")
        website_host_slug = ""
        if partial.get("website"):
            from urllib.parse import urlparse as _urlparse
            wh = (_urlparse(partial["website"]).hostname or "").lower()
            wh = wh.removeprefix("www.")
            website_host_slug = _slug(wh.split(".")[0])

        # STRICT match: email host must EQUAL the company slug (or the verified
        # website host slug), OR be within 3 characters of length, OR Dropcontact
        # has returned a SIREN that matches the company we asked about (strongest
        # cross-validation). We do NOT accept arbitrary prefix matches because
        # those let "flaveursdurocher" pass for "flaveurs" (different business).
        #
        # Pre-strip common French corporate prefixes ("SAS", "SARL", "EURL"...)
        # before comparing — Sirene names like "SAS BIBENT" should match
        # bibent.fr even though the literal slugs differ.
        def _strip_corp_prefixes(s: str) -> str:
            return re.sub(
                r"^(?:sas|sarl|eurl|sasu|sa|snc|scop|scic|scp|selas|selarl|"
                r"holding|groupe|group|cie|compagnie)",
                "",
                s,
            )
        company_slug_clean = _strip_corp_prefixes(company_slug)
        match = False
        if email_host_slug and company_slug:
            # Rule 1: exact match (raw or after stripping corp prefix)
            if email_host_slug in (company_slug, company_slug_clean):
                match = True
            # Rule 2: within 3 chars length diff AND one is a suffix of the
            # other (article stripped or corp prefix stripped on one side).
            elif (abs(len(email_host_slug) - len(company_slug_clean)) <= 3
                    and (email_host_slug.endswith(company_slug_clean)
                         or company_slug_clean.endswith(email_host_slug))):
                match = True
            # Rule 3: verified website host matches email host. Strong signal
            # since the website itself passed the strict FR / city-match filter.
            elif website_host_slug and email_host_slug == website_host_slug:
                match = True
            # Rule 4: Dropcontact returned a SIREN that matches ours. This is
            # the ultimate ground-truth — Dropcontact has cross-verified the
            # contact against the legal company registry.
            elif (dc_result.get("company_siren")
                  and partial.get("siren")
                  and str(dc_result["company_siren"]) == str(partial["siren"])):
                match = True
        if not match:
            dc_cross_company = True

    # Pre-compute mentions-légales person matches so the email block below
    # can use them before we hit the slower mobile/SMTP/LinkedIn steps.
    mentions = partial.get("mentions_legales") or {}
    director_name_low = (mentions.get("director_name") or "").lower()
    director_is_target = bool(
        director_name_low
        and person_first.lower() in director_name_low
        and person_last.lower() in director_name_low
    )
    if director_is_target:
        name_sources.append("mentions-legales:director-name")
    person_email_supplement = (
        mentions.get("director_email")
        if (mentions.get("director_email")
            and (director_is_target or not director_name_low))
        else None
    )

    email_field = ScoredField.missing()

    # PRIORITY -1: Dropcontact returned a verified email.
    # Highest trust — they actively probe SMTP and qualify as nominative vs pro.
    # Cross-company: if the email domain does NOT match the company we asked
    # about, mark as 'cross-company' and lower confidence — the person uses
    # this email but at a DIFFERENT employer, so the pitch will be off.
    if dc_result and dc_result.get("email"):
        qual = dc_result.get("email_qualification") or ""
        if dc_cross_company:
            email_domain = dc_result["email"].split("@", 1)[-1]
            email_field = ScoredField(
                value=dc_result["email"],
                sources=["dropcontact"],
                confidence=40,  # valid email but WRONG company → low confidence
                note=f"CROSS-COMPANY: email @{email_domain} ≠ {partial.get('company_name')}",
            )
        else:
            conf = 92 if qual.startswith("nominative") else 80
            email_field = ScoredField(
                value=dc_result["email"],
                sources=["dropcontact"],
                confidence=conf,
                note=f"dropcontact:{qual}" if qual else "dropcontact",
            )
        name_sources.append(f"dropcontact-email:{dc_result['email']}")

    # PRIORITY 0: Mentions Légales explicitly named the director's email.
    # This is the strongest possible signal — legally published, attached
    # to the publisher of the site.
    if not email_field.value and person_email_supplement:
        email_field = ScoredField(
            value=person_email_supplement,
            sources=["mentions-legales"],
            confidence=90 if director_is_target else 70,
            note="legal-publisher email" + (" (name matched)" if director_is_target else ""),
        )
        name_sources.append(f"mentions-email:{person_email_supplement}")

    if not email_field.value and domain and person_first and person_last:
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

    # PRIORITY 99 (LAST RESORT): no source found us a verified email. Generate
    # the most likely pattern from (first, last, company) using the dedicated
    # email_pattern_engine. Confidence is capped at 40 and the note is loud
    # — meant for 1-to-1 outreach where the salesperson tests deliverability
    # manually. Skipped if we already have something (any earlier source wins).
    if not email_field.value and person_first and person_last:
        try:
            pe = guess_email_patterns(
                person_first, person_last,
                partial.get("company_name") or "",
                website=partial.get("website"),
                role=person_role or None,
                use_llm_for_domain=True,  # Haiku-guarded by DNS-MX check
                max_variants=5,
            )
            if pe.get("primary_email"):
                source_tag = f"pattern-guess:{pe.get('domain_source', 'unknown')}"
                email_field = ScoredField(
                    value=pe["primary_email"],
                    sources=[source_tag],
                    confidence=pe["patterns"][0]["confidence"],
                    note=(
                        f"PATTERN GUESS — domain via {pe.get('domain_source')}. "
                        f"NOT verified. Test before sending. "
                        f"Alt: {', '.join(p['email'] for p in pe['patterns'][1:3])}"
                    ),
                )
                name_sources.append(f"pattern-email:{pe['primary_email']}")
        except Exception:
            pass

    # 3a-bis. Phone routing: person_phone = MOBILE ONLY (06/07).
    # Anything else (fixed line, foreign, special) is downgraded — it routes
    # through a switchboard, not the dirigeant's pocket. Those go to
    # company_phone instead (handled below via the partial dict).
    person_phone_field = ScoredField.missing()
    person_phone_demoted_fixed: Optional[str] = None  # collected for company_phone

    def _accept_as_person_phone(raw: str) -> bool:
        """Return True iff the phone is a FR mobile worth showing as person_phone.
        Fixed lines / foreign / special numbers get demoted to company_phone."""
        return classify_phone(raw) == "mobile"

    if dc_result and dc_result.get("phone"):
        dc_phone = dc_result["phone"]
        if _accept_as_person_phone(dc_phone):
            normalized = normalize_fr_phone(dc_phone) or dc_phone
            if dc_cross_company:
                person_phone_field = ScoredField(
                    value=normalized,
                    sources=["dropcontact"],
                    confidence=45,
                    note=f"CROSS-COMPANY mobile: at different employer than {partial.get('company_name')}",
                )
            else:
                person_phone_field = ScoredField(
                    value=normalized,
                    sources=["dropcontact"],
                    confidence=88,
                    note="dropcontact (mobile-verified)",
                )
        else:
            # Demote: not a mobile → company_phone candidate
            person_phone_demoted_fixed = normalize_fr_phone(dc_phone) or dc_phone

    if (not person_phone_field.value
            and mentions.get("director_phone")
            and (director_is_target or not director_name_low)):
        ml_phone = mentions["director_phone"]
        if _accept_as_person_phone(ml_phone):
            person_phone_field = ScoredField(
                value=normalize_fr_phone(ml_phone) or ml_phone,
                sources=["mentions-legales"],
                confidence=85 if director_is_target else 65,
                note="legal-publisher mobile"
                     + (" (name matched)" if director_is_target else ""),
            )
        elif not person_phone_demoted_fixed:
            person_phone_demoted_fixed = normalize_fr_phone(ml_phone) or ml_phone

    # 3b. Free Mobile Finder — fallback if mentions_legales didn't give a phone.
    # Skip entirely when Dropcontact already returned a phone (we save 10-15s
    # of sequential scraping per lead).
    if not person_phone_field.value and not (dc_result and dc_result.get("phone")):
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
                # mobile_finder already filters on 06/07 via its FR_MOBILE_RX,
                # but double-check with the centralised classifier in case the
                # regex captured something edge-case.
                if classify_phone(mobile) == "mobile":
                    person_phone_field = ScoredField(
                        value=normalize_fr_phone(mobile) or mobile,
                        sources=[f"mobile-finder:{source}"],
                        confidence=75,
                        note=f"mobile found via {source}",
                    )
                    name_sources.append(f"mobile-near-name:{source}")
        except Exception:
            pass

    # 4. Person LinkedIn — Dropcontact > Serper search.
    li_field = ScoredField.missing()
    if dc_result and dc_result.get("linkedin"):
        li_url = dc_result["linkedin"]
        if _name_in_linkedin_url(person_first, person_last, li_url):
            li_field = ScoredField(
                value=li_url,
                sources=["dropcontact"],
                confidence=90,
                note="dropcontact",
            )
            name_sources.append(f"dropcontact-linkedin:{li_url}")
    if not li_field.value:
        # Fall back to search (Serper > Brave > DDG). Strict slug validation.
        li_raw = find_linkedin_for_person(
            full_name,
            partial.get("company_name", ""),
            city=partial.get("city"),
            role=person_role or None,
        )
        if _name_in_linkedin_url(person_first, person_last, li_raw):
            li_field = ScoredField.from_single(li_raw, "search:linkedin-in", verified=True)
            name_sources.append(f"linkedin:{li_raw}")

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

    # If person_phone got demoted (Dropcontact returned a fixed line, not a
    # mobile), promote it into company_phone if it's not already there.
    company_phone_field = ScoredField(**partial["company_phone"])
    if person_phone_demoted_fixed:
        existing_digits = re.sub(r"\D", "", company_phone_field.value or "")
        new_digits = re.sub(r"\D", "", person_phone_demoted_fixed)
        if not existing_digits or existing_digits != new_digits:
            # Promote when company_phone was empty, OR triangulate when both
            # exist (different sources agreeing on the same line = bonus).
            if not company_phone_field.value:
                company_phone_field = ScoredField(
                    value=person_phone_demoted_fixed,
                    sources=["dropcontact:fixed-line"],
                    confidence=75,
                    note="Dropcontact returned a fixed line, not a mobile; "
                         "treated as company switchboard.",
                )
            elif company_phone_field.confidence < 90:
                company_phone_field.confidence = min(90, company_phone_field.confidence + 10)
                company_phone_field.sources = list(company_phone_field.sources) + ["dropcontact"]

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
        cuisine_type=partial.get("cuisine_type"),
        gmb_rating=partial.get("gmb_rating"),
        gmb_rating_count=partial.get("gmb_rating_count"),
        is_operating=partial.get("is_operating"),
        permanently_closed=partial.get("permanently_closed"),
        quality_flags=list(partial.get("quality_flags") or []),
        company_linkedin=ScoredField(**partial["company_linkedin"]),
        company_instagram=ScoredField(**partial["company_instagram"]),
        company_phone=company_phone_field,
        person_name=name_field,
        person_role=role_field,
        person_email=email_field,
        person_phone=person_phone_field,
        person_linkedin=li_field,
        person_instagram=ig_field,
        # v0.14.0: extras (Lead.model_config = extra='allow') — surfaced in
        # the XLSX export and used by cold_email.pitch_hint + ICP scoring.
        tech_stack=partial.get("tech_stack") or [],
        tech_signals=partial.get("tech_signals") or [],
        tech_categories=partial.get("tech_categories") or {},
        primary_cms=partial.get("primary_cms"),
        tech_maturity=tech_maturity_score({
            "stack": partial.get("tech_stack") or [],
            "signals": partial.get("tech_signals") or [],
        }) if (partial.get("tech_stack") or partial.get("tech_signals")) else 0,
        ft_hiring_intensity=partial.get("ft_hiring_intensity"),
        ft_n_offres=partial.get("ft_n_offres"),
        ft_top_titles=partial.get("ft_top_titles") or [],
        ft_reason=partial.get("ft_reason"),
        bodacc_verdict=partial.get("bodacc_verdict"),
        bodacc_reason=partial.get("bodacc_reason"),
        # v0.15.0 extras
        date_creation=partial.get("date_creation"),
        company_age_months=partial.get("company_age_months"),
        lifecycle_stage=partial.get("lifecycle_stage"),
        careers_url=partial.get("careers_url"),
        careers_n_jobs=partial.get("careers_n_jobs") or 0,
        careers_top_titles=partial.get("careers_top_titles") or [],
        careers_tech_signals=partial.get("careers_tech_signals") or {},
        careers_tilt_categories=partial.get("careers_tilt_categories") or [],
        rfp_active=partial.get("rfp_active"),
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
