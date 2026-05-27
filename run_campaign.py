"""
run_campaign.py — ONE function that runs the full prospection campaign end to end.

Why: when Claude (in Multica or Claude Code) drives the pipeline tool-by-tool, every
intermediate state is shipped back through the model. With Sonnet that's ~$0.40/lead.
With this script, Claude makes ONE Bash call instead of 30+, saving 90%+ of tokens.

Usage from a shell:
    python run_campaign.py \\
        --query "cabinet dentaire" --code-postal 69001 --volume 10 \\
        --persona "associé" --output prospects-dentistes-lyon

Usage from Claude (the typical Multica flow):
    1. Pick query, geo, persona from the user request
    2. Call this script ONCE via Bash
    3. Read the printed summary + the produced .xlsx
    4. Report back to the user

The script:
- Searches Sirene
- Runs enrich_company_partial in parallel (Pappers + Brave + cache)
- Picks the legal director as decision-maker (Phase 1 default). For Phase 2 with
  Claude-in-the-loop persona disambiguation, use --interactive or call the lower-level
  pipeline functions yourself.
- Finalizes each lead (email, LinkedIn, Insta)
- Exports both .csv (Excel-FR) and .xlsx via sheets_export.export_leads
- Prints a 1-screen summary
"""
from __future__ import annotations

import argparse
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

# Make sure relative imports work whether we are run as a script or a module
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# v0.15.2 — re-order dirigeants so OPERATIONAL roles come first, statutory/
# audit roles (commissaire, censeur, administrateur sans fonction) come last.
# Without this, Sirene's order (alphabetical or random) often puts a
# "Commissaire aux comptes suppléant" at index 0 and we pick him as decideur
# instead of the real President / DG / Gérant.
#
# Tiers (higher = better):
#   3 = operational chiefs (Président, DG, DGD, Gérant, Fondateur, Directeur*)
#   2 = directors with portfolio (DRH, DAF, Directeur Formation, DSI…)
#   1 = co-gérant, associé unique
#   0 = administrator, board member (no formal exec function)
#  -1 = auditor / statutory roles (commissaire, censeur, représentant permanent)
_ROLE_PRIORITY_PATTERNS: list[tuple[int, tuple[str, ...]]] = [
    (3, (
        "president", "président",
        "directeur general", "directeur général", "directrice generale", "directrice générale",
        "directeur general delegue", "directeur général délégué",
        "gerant", "gérant", "co-gerant", "co-gérant",
        "fondateur", "fondatrice", "founder",
        "ceo", "chief executive",
    )),
    (2, (
        "directeur des ressources humaines", "drh",
        "directeur formation", "directrice formation",
        "directeur administratif et financier", "daf",
        "directeur des systemes d'information", "directeur des systèmes d'information", "dsi",
        "chief digital officer", "cdo",
        "directeur transformation", "directeur innovation",
        "directeur commercial", "directeur marketing", "directeur des opérations",
        "directeur", "directrice",   # generic catch-all
    )),
    (1, (
        "associe unique", "associé unique",
        "entrepreneur individuel",
    )),
    (-1, (
        "commissaire aux comptes", "commissaire au compte",
        "commissaire aux comptes suppleant", "commissaire aux comptes suppléant",
        "censeur",
        "representant permanent", "représentant permanent",
        "membre du conseil", "administrateur",   # board only, not exec
    )),
]


def _role_priority(role: str) -> int:
    """Return a priority tier for a Sirene role string. Higher = better
    decision-maker. 0 if no match (unknown role)."""
    if not role:
        return 0
    r = role.lower().strip()
    # Sort patterns by specificity (longest first) so "directeur général
    # délégué" matches before "directeur"
    flat = [(tier, kw) for tier, kws in _ROLE_PRIORITY_PATTERNS for kw in kws]
    flat.sort(key=lambda x: -len(x[1]))
    for tier, kw in flat:
        if kw in r:
            return tier
    return 0


# v0.16.0 — Cabinets d'audit / commissariat aux comptes connus.
# Quand un nom de dirigeant matche cette liste, c'est une personne morale
# CAC (KPMG SA, Mazars, Deloitte, etc.) — pas un humain joignable.
# Cas réel BBW-29: THERMADOUR avait person_name="Kpmg S.a".
_KNOWN_AUDIT_FIRMS = {
    "kpmg", "mazars", "deloitte", "ey", "ernst", "pwc", "pricewaterhouse",
    "grant thornton", "bdo", "rsm", "bm&a", "exco", "fiducial",
    "in extenso", "sefac", "advance", "axion", "barale", "becouze",
    "calan ramolino", "cofineg", "compagnie fiduciaire", "constantin",
    "exponens", "fidulor", "fitexco", "michel creuzot", "mga", "ofica",
    "orfis", "ratp dev", "sec ouest", "sefiges", "tgs france",
}


def _name_looks_like_audit_firm(name: str) -> bool:
    """True if the cleaned name matches a known FR audit / CAC firm OR a
    generic pattern ('Cabinet X', 'X Audit', 'X Conseil', 'X & Associés', ...).

    v0.16.2 — Cas réel BBW-31: LIVINPARIS avait person_name='Cabinet Audit'
    qui ne matchait aucune entrée hard-codée. On élargit aux patterns
    génériques très typiques des CAC indépendants français.
    """
    if not name:
        return False
    import re, unicodedata
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    n = re.sub(r"\b(s\.?a\.?|sas|sasu|sarl|eurl|sca|llp|inc|gmbh|ltd)\b\.?", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    # Hard-coded firm names (gros cabinets)
    if any(firm in n for firm in _KNOWN_AUDIT_FIRMS):
        return True
    # Generic CAC / audit / conseil firm patterns
    _GENERIC_AUDIT_PATTERNS = [
        r"^cabinet\b",            # "Cabinet Audit", "Cabinet Martin"
        r"\baudit\b",              # "X Audit", "Audit Y"
        r"\bconseil(s)?\b",        # "X Conseil", "Cabinet Conseil"
        r"\bcommissariat\b",       # "X Commissariat aux comptes"
        r"(?:\bet\s+|&\s*)associes?\b", # "Martin & Associés", "Dupont et Associés"
        r"\bexpertise\b",          # "X Expertise Comptable"
        r"\bexperts?\s+comptables?\b",
        r"\bcomptables?\s+associes?\b",
        r"\bfiduciaire\b",         # "Compagnie Fiduciaire"
    ]
    for pat in _GENERIC_AUDIT_PATTERNS:
        if re.search(pat, n):
            return True
    return False


# v0.16.3 — Cohérence NAF vs cuisine_type (HERE/GMB).
# Cas réel BBW-33: une entreprise déclarée NAF 47.25Z (caviste) mais HERE
# la classe comme "Bed & Breakfast" → activité réelle ≠ déclaration Sirene.
# On rejette ces incohérences pour éviter les faux positifs en CHR.
#
# Pour chaque famille NAF, on définit:
#  - require_any: au moins UN de ces mots doit être dans cuisine_type
#  - forbid_any:  AUCUN de ces mots ne doit y être (signal contradictoire)
# Si cuisine_type est vide / inconnu → on passe (pas de signal contraire).
_NAF_CUISINE_RULES: dict[str, dict[str, list[str]]] = {
    # Cavistes (47.25Z)
    "47.25": {
        "require_any": ["vin", "spiritueux", "domaine", "caviste", "cave",
                         "alcool", "champagne", "wine"],
        "forbid_any":  ["b&b", "bed and breakfast", "bed & breakfast",
                         "hôtel", "hotel", "restaurant", "café", "cafe"],
    },
    # Restauration traditionnelle (56.10A)
    "56.10": {
        "require_any": ["restaurant", "brasserie", "bistro", "gastrono",
                         "café", "cafe", "italien", "français", "francais"],
        "forbid_any":  ["hôtel", "b&b", "bed and breakfast", "boulang",
                         "patisser", "boucher", "supermarché", "épicerie"],
    },
    # Hôtels (55.10Z)
    "55.10": {
        "require_any": ["hôtel", "hotel", "auberge", "lodge", "palace",
                         "b&b", "bed and breakfast"],
        "forbid_any":  ["restaurant", "boulang", "supermarché", "épicerie"],
    },
    # Bars / débits de boissons (56.30Z)
    "56.30": {
        "require_any": ["bar", "café", "cafe", "pub", "cocktail", "brasserie",
                         "lounge", "wine"],
        "forbid_any":  ["restaurant gastrono", "boulang", "supermarché"],
    },
}


def _naf_cuisine_consistent(naf: str | None, cuisine_type: str | None) -> bool:
    """Return False if the NAF and GMB/HERE cuisine_type are CONTRADICTORY.
    Return True if consistent or if we have no signal to judge."""
    if not naf or not cuisine_type:
        return True  # no signal, accept
    c = (cuisine_type or "").lower()
    for prefix, rules in _NAF_CUISINE_RULES.items():
        if not naf.startswith(prefix):
            continue
        # Forbid wins immediately: contradiction caught
        if any(kw in c for kw in rules.get("forbid_any", [])):
            return False
        # Require: if cuisine_type mentions none of the expected keywords AND
        # mentions something else, flag mismatch. If cuisine_type is super
        # generic (e.g. "Établissement", "Entreprise"), let it pass.
        if rules.get("require_any"):
            if not any(kw in c for kw in rules["require_any"]):
                # Cuisine talks about something else (not generic) → mismatch
                # Generic terms that we let pass:
                generic = ["établissement", "etablissement", "entreprise",
                           "commerce", "société", "societe", "magasin"]
                if not any(g in c for g in generic):
                    return False
        return True  # passed
    return True  # NAF not in our coherence map → accept


def _sort_dirigeants_by_decisionmaker_priority(dirs: list[dict]) -> list[dict]:
    """Stable-sort the dirigeants list so the highest-tier decision-makers
    come first. Auditors and censeurs are pushed to the bottom (tier -1).

    v0.15.3 — personnes morales (holdings qui président une SAS, ex: "Groupe
    Moovéus" président d'AKSIS) sont reléguées tout en bas (tier -2) parce
    qu'elles ne sont pas un humain joignable en cold outreach. Si seules des
    PM existent, on les garde quand même (mieux que rien).
    """
    def _key(d: dict) -> tuple[int, int]:
        # We sort ASCENDING. So smaller keys come FIRST.
        # → real humans: key = (-tier, 0). Higher tier = smaller key = first.
        # → personnes morales: key = (+infinity, 0) → always last.
        # v0.16.0 → audit firms (KPMG, Mazars…): key = (+infinity*2, 0) → after PM.
        full_name = d.get("name") or d.get("raw_name") or ""
        if _name_looks_like_audit_firm(full_name):
            return (20_000, 0)
        if d.get("is_personne_morale"):
            return (10_000, 0)
        return (-_role_priority(d.get("role", "")), 0)
    return sorted(dirs, key=_key)


def _summary(leads, kept_path: str, elapsed: float) -> None:
    kept = [l for l in leads if not l.dropped]
    dropped = [l for l in leads if l.dropped]
    print()
    print(f"=== {len(leads)} leads enriched in {elapsed:.1f}s "
          f"(~{elapsed/max(1,len(leads)):.1f}s/lead) ===")
    print(f"  Kept:    {len(kept)}")
    print(f"  Dropped: {len(dropped)}")

    if dropped:
        from collections import Counter
        reasons = Counter(l.drop_reason for l in dropped)
        for reason, n in reasons.most_common(3):
            print(f"    [{n}x] {reason}")

    print()
    print(f"=== Sample of top {min(3, len(kept))} kept leads ===")
    for l in sorted(kept, key=lambda x: -x.overall_score)[:3]:
        print(f"  {l.company_name} (score {l.overall_score})")
        print(f"    {l.person_name.value} · {l.person_role.value or '?'}")
        print(f"    email:    {l.person_email.value or '—'} (conf {l.person_email.confidence})")
        print(f"    linkedin: {l.person_linkedin.value or '—'} (conf {l.person_linkedin.confidence})")
        print(f"    phone:    {l.person_phone.value or l.company_phone.value or '—'}")
    print()
    print(f"=== Output ===")
    print(f"  CSV:  {kept_path}")
    xlsx = kept_path.replace('.csv', '.xlsx')
    if Path(xlsx).exists():
        print(f"  XLSX: {xlsx}")


def run(
    *,
    query: str | None = None,
    naf: str | None = None,
    code_postal: str | None = None,
    departement: str | None = None,
    region: str | None = None,
    tranche_effectif: str | None = None,
    volume: int = 10,
    persona_role_hint: str | None = None,
    output_stem: str | None = None,
    max_workers: int = 8,
    icp: dict | None = None,
    only_new: bool = True,
    include_outcomes: list[str] | None = None,
    push_to_hubspot: bool = False,
    campaign_id: str | None = None,
    llm_decider: bool = False,
    retry_dropped: bool = False,
    generate_emails: bool = False,
    sender_offer: str = "",
    sender_company: str = "",
    sender_pitch: str | None = None,
    target_icp_description: str | None = None,
    paid_threshold: int = 40,
    max_candidates: int | None = None,
    raw_mode: bool = False,
    # v0.15.0
    multi_touch: bool = False,
    rfp_keywords: list[str] | None = None,
    rfp_cpv_preset: str | None = None,
    rfp_regions: list[str] | None = None,
    rfp_days: int = 90,
    rfp_montant_min: int | None = None,
    # v0.16.0 — strict premium gates (BBW-30 hotfix: wire from CLI)
    require_gmb: bool = False,
    min_gmb_rating: float | None = None,
    min_gmb_reviews: int | None = None,
    # v0.16.3 — strict variant (rating-only, not just cuisine)
    require_gmb_rating: bool = False,
    # v0.17.0 — LLM lead reasoner (analyse business sémantique par lead)
    icp_strict_llm: bool = False,
    # v0.18.0 — reverse sourcing (Google → URL → SIREN, contourne le NAF)
    reverse_source_mode: bool = False,
) -> str:
    """End-to-end campaign. Returns the path of the produced CSV.

    New in v0.5.0:
    - `icp`: dict from icp.py (e.g., PRESET_CAVISTES_PREMIUM_PARIS). Annotates
      each lead with an `icp_score` 0-100. Use `icp.PRESET_*` for ready-made.
    - `only_new`: skip companies already in lead_store (dedup across runs).
    - `push_to_hubspot`: also sync kept leads to HubSpot (needs HUBSPOT_ACCESS_TOKEN).
    - `campaign_id`: tag the lead_store rows so you can list "leads from campaign X".
    """
    import time as _time
    from sirene_client import SireneClient
    from pipeline import enrich_companies_parallel, finalize_lead
    from sheets_export import export_leads
    from lead_store import already_seen_sirens, upsert_leads

    t0 = time.time()
    campaign_id = campaign_id or _time.strftime("campaign-%Y%m%d-%H%M%S")

    # 0. Sanity check: outbound SMTP. Many cloud VPS block port 25 by default,
    # which silently zeroes-out email verification. We warn LOUDLY so the user
    # knows why emails come back as "not verified" pattern guesses instead of
    # blaming the pipeline.
    from email_finder import smtp_outbound_available
    if not smtp_outbound_available():
        print("[WARN] Outbound SMTP (port 25) is BLOCKED on this host.")
        print("       Emails will fall back to pattern-guess at low confidence.")
        print("       If you need verified emails, run on a host with port 25 open,")
        print("       or rely on DROPCONTACT_API_KEY for verified results.")

    # 0-bis. QUOTAS — print the remaining capacity + check daily cap.
    # We want the operator (or Multica) to see at a glance:
    #   - how many leads they can still process today
    #   - if any free tier is at risk of running out mid-run
    from quotas import summary as _quota_summary, get_daily_cap, daily_leads_used
    qs = _quota_summary()
    bottleneck = qs.get("bottleneck_leads_remaining")
    bottleneck_svc = qs.get("bottleneck_service_label") or qs.get("bottleneck_service")
    if bottleneck is not None:
        print(f"[Quotas] {bottleneck} leads remaining (bottleneck: {bottleneck_svc})")
        if bottleneck < volume:
            print(f"[Quotas] WARN: you asked for {volume} leads but only ~{bottleneck} "
                  f"can be enriched before {bottleneck_svc} runs out.")
    # Daily cap enforcement
    cap = get_daily_cap()
    if cap > 0:
        used_today = daily_leads_used()
        if used_today + volume > cap:
            allowed = max(0, cap - used_today)
            print(f"[Quotas] DAILY CAP HIT — already used {used_today}/{cap} today, "
                  f"asked for {volume}. Limiting this run to {allowed} leads.")
            if allowed == 0:
                print("[Quotas] Refusing to run. Reset via `python quotas.py set-cap 0` "
                      "or wait until tomorrow.")
                sys.exit(2)
            volume = allowed

    # 1. Source via Sirene — PERFECT MODE: iterate pages, filter early,
    # enrich the survivors, stop when `volume` QUALIFIED leads collected.
    # Old behavior (--raw-mode) just fetches `volume` candidates and enriches
    # all of them, regardless of quality.
    from pipeline import is_junk_company_name, preliminary_score

    # v0.15.0 — RFP-FIRST MODE: source prospects from BOAMP appels d'offres
    # matching our offer. Strongest possible intent signal.
    # Note: this REPLACES the Sirene sourcing — the orgs come from RFPs.
    if rfp_keywords or rfp_cpv_preset:
        from appels_offres import search_boamp
        print(f"[BOAMP] searching active RFPs · keywords={rfp_keywords} · "
              f"cpv_preset={rfp_cpv_preset} · regions={rfp_regions} · "
              f"days={rfp_days} · montant_min={rfp_montant_min}")
        rfps = search_boamp(
            keywords=rfp_keywords,
            cpv_preset=rfp_cpv_preset,
            regions=rfp_regions,
            days=rfp_days,
            only_active=True,
            limit=100,
            montant_min=rfp_montant_min,
        )
        print(f"[BOAMP] {len(rfps)} active RFPs matched")
        if not rfps:
            print("[BOAMP] No active RFPs. Widen keywords / region / days.")
            sys.exit(1)

        # Dedupe by SIREN (one org can publish several RFPs — keep the
        # most recent/highest-value one as the canonical signal).
        by_siren: dict[str, dict] = {}
        for r in rfps:
            s = r.get("siren")
            if not s:
                continue  # no SIREN = can't enrich via Sirene
            existing = by_siren.get(s)
            if not existing:
                by_siren[s] = r
            else:
                # Keep the one with the highest amount (or most recent if none)
                a = (r.get("montant_estime_eur") or 0)
                b = (existing.get("montant_estime_eur") or 0)
                if a > b:
                    by_siren[s] = r
        unique_sirens = list(by_siren.keys())
        print(f"[BOAMP] {len(unique_sirens)} unique SIREN organisations to enrich")

        # Dedup vs lead_store (only_new)
        if only_new:
            seen = already_seen_sirens(unique_sirens)
            unique_sirens = [s for s in unique_sirens if s not in seen]
            print(f"[Dedup] {len(seen)} already prospected, {len(unique_sirens)} new")
        if not unique_sirens:
            print("[BOAMP] all matching orgs already prospected. Use --include-seen "
                  "or widen the BOAMP filters.")
            sys.exit(1)

        # Fetch Sirene companies for these SIRENs (one search per SIREN)
        from sirene_client import SireneClient
        companies = []
        with SireneClient() as cli:
            for s in unique_sirens[:max(volume * 3, 50)]:
                try:
                    resp = cli.search(s)
                    if resp.results:
                        companies.append(resp.results[0])
                except Exception:
                    continue
        print(f"[Sirene] {len(companies)}/{len(unique_sirens)} companies hydrated from RFP SIRENs")

        # Enrich in parallel
        partials = enrich_companies_parallel(companies, max_workers=max_workers)
        # Attach the RFP onto each partial (lookup by SIREN)
        for p in partials:
            s = p.get("siren")
            if s and s in by_siren:
                p["rfp_active"] = by_siren[s]
                # Quality flag for visibility in the XLSX
                rfp_id = by_siren[s].get("idweb") or "?"
                p.setdefault("quality_flags", []).append(f"rfp-active:{rfp_id}")
        with_site = sum(1 for p in partials if p.get('website'))
        print(f"[BOAMP-Enrich] {with_site}/{len(partials)} with website · "
              f"{sum(1 for p in partials if p.get('rfp_active'))} RFP-linked")

    elif reverse_source_mode:
        # v0.18.0 — Reverse sourcing: ICP NL → Google → URLs → SIREN
        # Au lieu de partir de Sirene NAF (administratif), on part d'une
        # vraie recherche sémantique pour trouver les boîtes qui MATCHENT
        # le profil business voulu, pas seulement le code NAF.
        #
        # v0.19.2 — fix BBW-38: si pas de --icp-description (Anthropic absent),
        # synthétiser une mini-ICP depuis NAF + dept pour ne pas bloquer.
        effective_icp = target_icp_description
        if not effective_icp:
            from reverse_sourcing import synthesize_icp_from_filters
            effective_icp = synthesize_icp_from_filters(
                naf=naf, departement=departement, region=region,
                tranche_effectif=tranche_effectif, query=query,
            )
            if not effective_icp:
                print("[ReverseSource] --reverse-source requires either "
                      "--icp-description, or at least --naf / --query for "
                      "fallback synthesis. None provided.")
                sys.exit(1)
            print(f"[ReverseSource] Synthesized ICP from filters:\n  '{effective_icp}'")
        from reverse_sourcing import reverse_source
        candidates = reverse_source(
            effective_icp,
            n_queries=5,
            max_results_per_query=15,
            max_total=max(volume * 3, 30),
        )
        if not candidates:
            print("[ReverseSource] No candidates found. Refine --icp-description "
                  "or check SERPER_API_KEY / PAPPERS_API_KEY.")
            sys.exit(1)

        # Hydrater via Sirene pour avoir la structure complete (dirigeants, etc.)
        from sirene_client import SireneClient
        companies = []
        with SireneClient() as cli:
            for c in candidates:
                s = c.get("siren")
                if not s:
                    continue
                try:
                    resp = cli.search(s)
                    if resp.results:
                        co = resp.results[0]
                        # Forcer le website depuis le reverse-source (souvent
                        # plus à jour que celui de Sirene)
                        if hasattr(co, "model_dump"):
                            co_dict = co.model_dump()
                            co_dict["_reverse_source_website"] = c["website"]
                            co_dict["_reverse_source_query"] = c.get("source_query")
                            co_dict["_reverse_source_snippet"] = c.get("source_snippet")
                            companies.append(co_dict)
                        else:
                            companies.append(co)
                except Exception:
                    continue
        print(f"[ReverseSource→Sirene] {len(companies)}/{len(candidates)} "
              f"hydrated from SIRENs")

        if not companies:
            print("[ReverseSource] Aucune entreprise hydratée — abort.")
            sys.exit(1)

        # Enrich + on flag tous comme reverse-sourced (pour traçabilité XLSX)
        partials = enrich_companies_parallel(companies, max_workers=max_workers)
        for p in partials:
            p.setdefault("quality_flags", []).append("reverse-sourced")
        with_site = sum(1 for p in partials if p.get('website'))
        print(f"[ReverseSource-Enrich] {with_site}/{len(partials)} with website")

    elif raw_mode:
        # Legacy: fetch volume candidates, no filtering
        with SireneClient() as c:
            resp = c.search_many(
                target=volume, query=query, naf=naf,
                code_postal=code_postal, departement=departement, region=region,
                tranche_effectif=tranche_effectif,
            )
        companies = resp.results[:volume]
        if not companies:
            print("No companies matched the query.")
            sys.exit(1)
        print(f"[Sirene] {len(companies)} companies (raw mode — no quality gate)")
        if only_new:
            seen = already_seen_sirens([c.siren for c in companies])
            companies = [c for c in companies if c.siren not in seen]
            print(f"[Dedup] {len(seen)} already prospected, {len(companies)} new")
        partials = enrich_companies_parallel(companies, max_workers=max_workers)
        with_site = sum(1 for p in partials if p.get('website'))
        print(f"[Enrich] {with_site}/{len(partials)} with website")
    else:
        # PERFECT MODE — iterate Sirene pages, gate by quality, stop at target
        cap = max_candidates if max_candidates is not None else max(50, volume * 5)
        print(f"[Perfect] target={volume} qualified leads · max candidates={cap}")

        # Quality gate operating on partial enrichment data (cheap-source only).
        # A "perfect" lead must pass ALL these:
        #   - Not junk name
        #   - Not permanently_closed (GMB)
        #   - Has at least one nominatif dirigeant
        #   - preliminary_score ≥ paid_threshold (or 40 by default)
        # We DO NOT gate on foreign-subsidiary yet because that signal comes
        # from company LinkedIn which is fetched in partial — but we keep
        # those leads with low ICP score; the user sees them flagged.
        def _is_qualified(p: dict) -> tuple[bool, str]:
            if not p:
                return False, "no partial"
            if is_junk_company_name(p.get("company_name", "")):
                return False, "junk-name"
            if p.get("permanently_closed"):
                return False, "GMB permanently_closed"
            # BODACC hard-drop: company in redressement / liquidation / radiation
            if p.get("bodacc_verdict") == "HARD_DROP":
                return False, f"BODACC: {p.get('bodacc_reason', 'in trouble')}"
            dirs = p.get("legal_dirigeants") or []
            real_dirs = [d for d in dirs if d.get("first") and d.get("last")]
            if not real_dirs:
                return False, "no nominatif dirigeant"
            score = preliminary_score(p)
            if score < paid_threshold:
                return False, f"preliminary_score {score} < {paid_threshold}"
            # Reject leads whose company LinkedIn flags foreign subsidiary
            flags = p.get("quality_flags") or []
            if any(f.startswith("foreign-subsidiary:") for f in flags):
                return False, "foreign-subsidiary"
            # v0.16.0 — strict premium gates (CHR mode)
            if require_gmb:
                gmb = p.get("gmb") or {}
                if not (gmb.get("rating") or p.get("cuisine_type")):
                    return False, "no GMB enrichment (--require-gmb)"
            # v0.16.3 — strict variant: rating ONLY (cuisine_type insuffisant)
            if require_gmb_rating:
                if not p.get("gmb_rating"):
                    return False, "no GMB rating (--require-gmb-rating)"
            if min_gmb_rating is not None:
                r = p.get("gmb_rating")
                if r is None or float(r) < float(min_gmb_rating):
                    return False, f"GMB rating {r} < {min_gmb_rating}"
            if min_gmb_reviews is not None:
                rc = p.get("gmb_rating_count")
                if rc is None or int(rc) < int(min_gmb_reviews):
                    return False, f"GMB reviews {rc} < {min_gmb_reviews}"
            # v0.16.3 — NAF/cuisine_type coherence guardrail
            # Cas réel BBW-33: ROXANE ET CYRANO listé NAF 47.25Z (caviste)
            # mais cuisine_type HERE = "Bed & Breakfast" → activité réelle ≠ NAF.
            # On rejette si le NAF dit "X" et le cuisine_type dit franchement
            # autre chose (B&B alors qu on cherche un caviste, par exemple).
            if not _naf_cuisine_consistent(p.get("naf"), p.get("cuisine_type")):
                return False, (
                    f"naf/cuisine mismatch: NAF={p.get('naf')} "
                    f"vs cuisine={p.get('cuisine_type')}"
                )
            return True, "ok"

        partials: list = []
        n_seen = 0
        n_junk_pre = 0
        n_failed_gate = 0
        # BBW-30 diag: track enrichment + reason breakdown on scanned leads
        _diag_stats = {
            "scanned": 0,
            "with_cuisine_type": 0,
            "with_gmb_rating": 0,
            "with_gmb_rating_count_50plus": 0,
            "with_gmb_rating_4plus": 0,
            "ft_intensities": {},
            "kpmg_in_dirigeants": 0,
            "reasons": {},
        }
        def _track(bp: dict, reason: str) -> None:
            _diag_stats["scanned"] += 1
            if bp.get("cuisine_type"):
                _diag_stats["with_cuisine_type"] += 1
            r = bp.get("gmb_rating")
            if r is not None:
                _diag_stats["with_gmb_rating"] += 1
                try:
                    if float(r) >= 4.0:
                        _diag_stats["with_gmb_rating_4plus"] += 1
                except Exception:
                    pass
            rc = bp.get("gmb_rating_count")
            try:
                if rc is not None and int(rc) >= 50:
                    _diag_stats["with_gmb_rating_count_50plus"] += 1
            except Exception:
                pass
            ft = bp.get("ft_hiring_intensity")
            if ft is not None:
                _diag_stats["ft_intensities"][ft] = (
                    _diag_stats["ft_intensities"].get(ft, 0) + 1
                )
            dirs = bp.get("legal_dirigeants") or []
            for d in dirs:
                nm = (d.get("name") or d.get("raw_name") or "").lower()
                if "kpmg" in nm or "mazars" in nm or "deloitte" in nm:
                    _diag_stats["kpmg_in_dirigeants"] += 1
                    break
            key = reason.split(":")[0].split("<")[0].strip()[:60]
            _diag_stats["reasons"][key] = _diag_stats["reasons"].get(key, 0) + 1
        with SireneClient() as c:
            for page_resp in c.iter_pages(
                query=query, naf=naf,
                code_postal=code_postal, departement=departement, region=region,
                tranche_effectif=tranche_effectif,
            ):
                if n_seen >= cap:
                    print(f"[Perfect] hit max_candidates ({cap}) — stopping")
                    break
                page_companies = page_resp.results
                if not page_companies:
                    break
                # Pre-filter: junk names (no API cost)
                pre_filtered = []
                for sc in page_companies:
                    n_seen += 1
                    name = getattr(sc, "name", None) or (
                        sc.get("nom_complet") if isinstance(sc, dict) else "?"
                    )
                    if is_junk_company_name(name):
                        n_junk_pre += 1
                        continue
                    pre_filtered.append(sc)
                # Dedup vs lead_store
                if only_new and pre_filtered:
                    sirens = [getattr(c0, "siren", None) or (
                        c0.get("siren") if isinstance(c0, dict) else None
                    ) for c0 in pre_filtered]
                    seen = already_seen_sirens([s for s in sirens if s])
                    pre_filtered = [c0 for c0, s in zip(pre_filtered, sirens)
                                     if s not in seen]
                if not pre_filtered:
                    continue
                # Enrich the survivors in parallel
                batch_partials = enrich_companies_parallel(
                    pre_filtered, max_workers=max_workers,
                )
                # Apply post-enrichment quality gate
                for bp in batch_partials:
                    ok, reason = _is_qualified(bp)
                    _track(bp or {}, reason if not ok else "ok")
                    if not ok:
                        n_failed_gate += 1
                        continue
                    partials.append(bp)
                    if len(partials) >= volume:
                        break
                print(f"[Perfect] page {page_resp.page}: "
                      f"{len(partials)}/{volume} qualified "
                      f"(seen {n_seen}, junk-pre {n_junk_pre}, "
                      f"failed gate {n_failed_gate})")
                if len(partials) >= volume:
                    break
                if n_seen >= cap:
                    break
        # BBW-30 diag dump (always, even if no partials)
        import json as _json
        print(f"[BBW-30 DIAG] {_json.dumps(_diag_stats, ensure_ascii=False)}")
        if not partials:
            print("[Perfect] No qualified candidates found. Try widening filters.")
            sys.exit(1)
        with_site = sum(1 for p in partials if p.get('website'))
        print(f"[Perfect FINAL] {len(partials)}/{volume} qualified leads kept "
              f"({with_site} with website) · "
              f"{n_seen} candidates scanned · "
              f"{n_junk_pre} junk-name dropped early · "
              f"{n_failed_gate} failed quality gate")

    # 3. Finalize each lead — Phase-1 default: take the first legal director.
    leads = []
    # Source SIRENs from the partials (works for both raw and perfect mode
    # since partials always have the 'siren' key from Sirene).
    sirens = [p.get("siren") for p in partials if p.get("siren")]
    if only_new:
        # We already filtered out seen ones above — all current ones are "new" by construction
        new_sirens = set(sirens)
    else:
        new_sirens = set(sirens) - already_seen_sirens(sirens)

    # Build the (partial, person_first, person_last, role, sources) tuples
    # FIRST (no I/O), then dispatch them all to a thread pool. This is the
    # single biggest speedup: finalize_lead does many slow network calls per
    # lead (LinkedIn search, Insta search, Dropcontact poll, mobile finder,
    # SMTP) — running them in parallel collapses the wall-clock time from
    # N × (slowest_per_lead) to roughly (slowest_per_lead).
    #
    # TWO-PASS strategy: compute a preliminary_score from the FREE-tier data
    # already in `partial`. Leads scoring below paid_threshold (default 40)
    # get the cheap path (no Dropcontact/Hunter/Datagma/BC) — saves credits
    # on low-quality leads we'd probably drop anyway.
    from pipeline import preliminary_score
    finalize_args = []
    prelim_scores: list[tuple[int, str]] = []
    n_paid = 0
    n_cheap = 0
    # v0.16.3 — track silent drops between qualif and finalize stage
    # (BBW-33: 1 lead lost between "8/8 qualified" and "7 stored").
    n_skipped_no_dirs = 0
    n_skipped_no_name = 0
    for p in partials:
        dirs = p.get("legal_dirigeants") or []
        if not dirs:
            n_skipped_no_dirs += 1
            continue

        chosen_first, chosen_last, chosen_role = None, None, None
        chosen_sources = ["sirene"]

        if llm_decider:
            from decision_maker_llm import pick as llm_pick
            decision = llm_pick(
                company_name=p["company_name"],
                sector_hint=p.get("naf") or "",
                persona_hint=persona_role_hint or "operational decision-maker",
                legal_dirigeants=dirs,
                team_page_text=p.get("team_page_text"),
            )
            if decision and decision.get("person_first") and decision.get("person_last"):
                chosen_first = decision["person_first"]
                chosen_last = decision["person_last"]
                chosen_role = decision.get("person_role") or persona_role_hint or ""
                chosen_sources = decision.get("person_sources") or ["sirene", "llm-decider"]

        if not chosen_first or not chosen_last:
            # v0.15.2 — reorder dirs so statutory/audit roles come LAST
            # (Sirene listed "Commissaire aux comptes suppléant" first for
            # AKSIS/Ladurée → we picked an auditor instead of the real boss).
            dirs = _sort_dirigeants_by_decisionmaker_priority(dirs)
            d = dirs[0]
            chosen_first = d.get("first") or ""
            chosen_last = d.get("last") or ""
            if not chosen_first or not chosen_last:
                parts = (d.get("name") or "").split()
                if len(parts) < 2:
                    n_skipped_no_name += 1   # v0.16.3 — track silent drop
                    continue
                chosen_first = chosen_first or parts[0]
                chosen_last = chosen_last or parts[-1]
            # CRITICAL: the REAL Sirene role wins. persona_role_hint is the
            # role the user WANTED to find (e.g. "DRH") but when we fall back
            # to the legal dirigeant, we must label them with their ACTUAL
            # role (Président, Gérant, etc.) — NOT the requested persona.
            # The persona hint is now used only as a last-resort label when
            # Sirene gave no role at all.
            real_role = d.get("role") or ""
            chosen_role = real_role or persona_role_hint or ""
            # If the user asked for a specific persona that we couldn't
            # confirm, annotate sources so finalize_lead's downstream code
            # knows this isn't a verified DRH/etc.
            if persona_role_hint and real_role and real_role.lower() != persona_role_hint.lower():
                chosen_sources = list(chosen_sources) + [
                    f"role-fallback:{real_role.lower()}-not-{persona_role_hint.lower()}"
                ]

        prelim = preliminary_score(p)
        prelim_scores.append((prelim, p["company_name"]))
        use_paid = prelim >= paid_threshold
        if use_paid:
            n_paid += 1
        else:
            n_cheap += 1
        finalize_args.append((p, chosen_first, chosen_last, chosen_role, chosen_sources, use_paid))

    if paid_threshold > 0 and (n_paid + n_cheap) > 0:
        print(f"[Two-pass] {n_paid}/{n_paid + n_cheap} leads above paid threshold "
              f"(>={paid_threshold}) → will hit paid waterfall; "
              f"{n_cheap} get cheap pass only.")
    # v0.16.3 — report silent drops between qualif and finalize (BBW-33)
    if n_skipped_no_dirs or n_skipped_no_name:
        print(f"[Pre-finalize] {n_skipped_no_dirs + n_skipped_no_name} qualified "
              f"leads SKIPPED before finalize: "
              f"{n_skipped_no_dirs} had no dirigeants · "
              f"{n_skipped_no_name} had no nominatif name "
              f"(résolution: ces leads passaient le quality gate mais sont "
              f"impossibles à finaliser sans personne identifiée).")

    # LAUNCH Dropcontact batch IN BACKGROUND, overlap with parallel finalize.
    # Previously the DC batch was BLOCKING (~30s polling), then finalize ran
    # (~30s). Now they run in parallel → wall-time drops by ~25s.
    # Race-safe because finalize_lead's DC step reads the diskcache; if the
    # background task hasn't primed it yet, finalize falls through to the
    # other waterfall sources (Hunter/Datagma/BetterContact). When DC IS
    # ready in time, every worker gets a clean cache hit.
    from concurrent.futures import ThreadPoolExecutor as _BgTPE
    _dc_executor = _BgTPE(max_workers=1)
    _dc_future = None
    try:
        from dropcontact_client import enrich_batch, have_dropcontact_key
        paid_args = [a for a in finalize_args if a[5]]   # a[5] = use_paid
        if have_dropcontact_key() and paid_args:
            print(f"[Dropcontact] batch-enriching {len(paid_args)} paid-tier leads "
                  f"in BACKGROUND (skipped {len(finalize_args) - len(paid_args)} cheap-only)…")

            def _dc_background_task():
                batch_rows = [
                    {"first": f, "last": l, "company": p.get("company_name", ""),
                     "website": p.get("website")}
                    for (p, f, l, r, srcs, _up) in paid_args
                ]
                batch_result = enrich_batch(batch_rows)
                try:
                    from pipeline import _CACHE, _CACHE_TTL
                    if _CACHE is not None:
                        for (p, f, l, r, srcs, _up) in paid_args:
                            key = f"dropcontact:{f}|{l}|{p.get('company_name', '')}"
                            lookup_key = (f.lower(), l.lower(),
                                           p.get("company_name", "").lower())
                            result = batch_result.get(lookup_key)
                            if result is not None:
                                _CACHE.set(key, result, expire=_CACHE_TTL)
                except Exception:
                    pass
                return batch_result

            _dc_future = _dc_executor.submit(_dc_background_task)
    except Exception as e:
        print(f"[Dropcontact] background batch failed ({e})")

    # Parallel finalize. We use the same worker count as the partial phase
    # since the rate-limited APIs (Brave, Serper, OSM, Dropcontact) all have
    # thread-safe Throttle() locks that serialize correctly under concurrency.
    from concurrent.futures import ThreadPoolExecutor as _TPE

    def _one(args):
        p, f, l, r, srcs, use_paid = args
        return finalize_lead(p, person_first=f, person_last=l,
                             person_role=r, person_sources=srcs,
                             use_paid_sources=use_paid)

    with _TPE(max_workers=max_workers) as exe:
        for lead in exe.map(_one, finalize_args):
            setattr(lead, "is_new_lead", lead.company_siren in new_sirens)
            leads.append(lead)

    # Drain the background Dropcontact task (might still be running if it
    # was slower than parallel finalize). We need this to make stats
    # accurate and to clean up the executor.
    if _dc_future is not None:
        try:
            batch_result = _dc_future.result(timeout=60)
            n_with_data = sum(
                1 for v in batch_result.values()
                if v.get("email") or v.get("phone")
            )
            # v0.16.3 — clearer logging: distinguish "empty input" / "quota dead"
            # / "real 0 matches" cases so the user knows what's happening.
            n_input = len(batch_result)
            if n_input == 0:
                # Try to figure out why — check quota state
                try:
                    from quotas import can_call as _qcan, remaining as _qrem
                    if not _qcan("dropcontact", 1):
                        rem = _qrem("dropcontact")
                        print(f"[Dropcontact] background batch SKIPPED: "
                              f"quota exhausted ({rem} credits left)")
                    else:
                        print(f"[Dropcontact] background batch done: 0 leads "
                              f"sent (no eligible leads above paid_threshold or "
                              f"DC client unavailable)")
                except Exception:
                    print(f"[Dropcontact] background batch done: 0/0 returned data "
                          f"(0 leads sent)")
            else:
                print(f"[Dropcontact] background batch done: "
                      f"{n_with_data}/{n_input} returned data")
        except Exception as e:
            print(f"[Dropcontact] background batch error: {e}")
    _dc_executor.shutdown(wait=True)

    # 3a-bis. Self-critique multi-pass: retry dropped leads with relaxed thresholds.
    if retry_dropped:
        retried = 0
        for lead in leads:
            if not lead.dropped:
                continue
            # Re-evaluate with a relaxed contact threshold (30 vs 50)
            lead.dropped = False
            lead.drop_reason = None
            lead.evaluate(min_person_conf=60, min_contact_conf=30)
            if not lead.dropped:
                retried += 1
        if retried:
            print(f"[Self-critique] Recovered {retried} leads with relaxed thresholds")

    # 3b. ICP scoring (optional)
    if icp:
        from icp import annotate_leads
        annotate_leads(leads, icp)
        print(f"[ICP] '{icp.get('name','?')}' applied. Top score: "
              f"{max((l.icp_score for l in leads), default=0)}")

    # 3b-bis. v0.17.0 — LLM Lead Reasoner (analyse business sémantique)
    # Le scoring ICP statique mesure les CHAMPS. Le reasoner LLM mesure
    # le FIT BUSINESS RÉEL (lit le site, raisonne, drop les faux positifs).
    # Activé via --icp-strict-llm + nécessite ANTHROPIC_API_KEY.
    if icp_strict_llm and target_icp_description:
        from lead_reasoner import reason_about_lead, should_keep_lead
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("[Reasoner] ANTHROPIC_API_KEY missing — skipping LLM reasoning.")
        else:
            # v0.19.1 — Wire decision_trace + ReAct refinement
            from decision_trace import LeadTrace, TraceWriter
            from react_loop import refine_borderline_lead
            import time as _t

            trace_path = Path("output") / f"{output_stem or 'campaign'}-trace.jsonl"
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            print(f"[Reasoner] Analyzing {len([l for l in leads if not l.dropped])} "
                  f"kept leads with Claude Haiku (trace → {trace_path})...")
            n_dropped_by_llm = 0
            n_react_refined = 0
            n_react_flipped = 0
            n_analyzed = 0

            with TraceWriter(trace_path) as trace_writer:
                for lead in leads:
                    if lead.dropped:
                        continue
                    trace = LeadTrace(
                        siren=lead.company_siren,
                        company_name=lead.company_name,
                        campaign_id=campaign_id or "?",
                    )
                    # Reconstruire un partial-like dict depuis le lead
                    partial_like = {
                        "company_name": lead.company_name,
                        "naf": lead.company_naf,
                        "city": lead.company_city,
                        "size": lead.company_size,
                        "website": lead.company_website,
                        "address": lead.company_address,
                        "cuisine_type": getattr(lead, "cuisine_type", None),
                        "gmb_rating": getattr(lead, "gmb_rating", None),
                        "gmb_rating_count": getattr(lead, "gmb_rating_count", None),
                        "tech_stack": getattr(lead, "tech_stack", None),
                        "primary_cms": getattr(lead, "primary_cms", None),
                        "company_linkedin": lead.company_linkedin.model_dump() if lead.company_linkedin else None,
                        "legal_dirigeants": getattr(lead, "legal_dirigeants", None) or [],
                        "lifecycle_stage": getattr(lead, "lifecycle_stage", None),
                        "company_age_months": getattr(lead, "company_age_months", None),
                        "bodacc_verdict": getattr(lead, "bodacc_verdict", None),
                        "web_enrichment": getattr(lead, "web_enrichment_dump", None) or {},
                    }
                    # === Phase 1: LLM Reasoner ===
                    t0 = _t.time()
                    verdict = reason_about_lead(partial_like, target_icp_description)
                    n_analyzed += 1
                    trace.span(
                        "llm_reasoner",
                        duration_ms=int((_t.time() - t0) * 1000),
                        data={"verdict_obj": verdict} if verdict else {},
                        error=None if verdict else "no verdict (LLM error)",
                    )
                    if not verdict:
                        trace.final_verdict("kept", "no LLM verdict, safe fallback kept")
                        trace_writer.write(trace)
                        continue

                    # === Phase 2: ReAct refinement si borderline ===
                    is_borderline = (
                        verdict.get("verdict") == "POSSIBLE_FIT"
                        and verdict.get("confidence") in ("low", "medium")
                    )
                    if is_borderline:
                        trace.decision(
                            "borderline_detected",
                            decision="refine",
                            reason=f"verdict={verdict.get('verdict')} confidence={verdict.get('confidence')}",
                            score=verdict.get("fit_score"),
                        )
                        t1 = _t.time()
                        refined_verdict, react_steps = refine_borderline_lead(
                            partial_like, target_icp_description,
                            original_verdict=verdict, max_turns=2,
                        )
                        trace.span(
                            "react_loop",
                            duration_ms=int((_t.time() - t1) * 1000),
                            data={"n_steps": len(react_steps), "steps": react_steps[:10]},
                        )
                        n_react_refined += 1
                        if refined_verdict and refined_verdict != verdict:
                            old_v = verdict.get("verdict")
                            new_v = refined_verdict.get("verdict")
                            if old_v != new_v:
                                n_react_flipped += 1
                                trace.decision(
                                    "react_flipped",
                                    decision="flipped",
                                    reason=f"{old_v} → {new_v}",
                                    score=refined_verdict.get("fit_score"),
                                )
                            verdict = refined_verdict

                    # === Phase 3: stick verdict + decide keep/drop ===
                    setattr(lead, "llm_fit_score", verdict.get("fit_score"))
                    setattr(lead, "llm_verdict", verdict.get("verdict"))
                    setattr(lead, "llm_reasoning", verdict.get("reasoning"))
                    setattr(lead, "llm_red_flags", " · ".join(verdict.get("red_flags") or []))
                    setattr(lead, "llm_green_flags", " · ".join(verdict.get("green_flags") or []))
                    setattr(lead, "llm_persona_guess", verdict.get("best_persona_guess"))
                    setattr(lead, "llm_confidence", verdict.get("confidence"))

                    if not should_keep_lead(verdict, min_score=60):
                        lead.dropped = True
                        lead.drop_reason = (
                            f"LLM REASONER: {verdict.get('verdict')} "
                            f"(score {verdict.get('fit_score')}) — "
                            f"{verdict.get('reasoning', '')[:200]}"
                        )
                        n_dropped_by_llm += 1
                        trace.decision(
                            "llm_reasoner",
                            decision="drop",
                            reason=verdict.get("reasoning", "")[:200],
                            score=verdict.get("fit_score"),
                            data={"verdict": verdict.get("verdict")},
                        )
                        trace.final_verdict("dropped", lead.drop_reason[:300])
                    else:
                        trace.decision(
                            "llm_reasoner",
                            decision="keep",
                            reason=verdict.get("reasoning", "")[:200],
                            score=verdict.get("fit_score"),
                            data={"verdict": verdict.get("verdict")},
                        )
                        trace.final_verdict("kept", verdict.get("reasoning", "")[:300])
                    trace_writer.write(trace)

            kept_after_llm = len([l for l in leads if not l.dropped])
            print(f"[Reasoner] Done: {n_analyzed} analyzed, "
                  f"{n_react_refined} refined via ReAct "
                  f"({n_react_flipped} verdict flipped), "
                  f"{n_dropped_by_llm} dropped, {kept_after_llm} kept.")
            print(f"[Trace] decision audit trail written to {trace_path}")

    # 3c. Persist to lead_store (dedup history + ICP score saved)
    n_new, n_existing = upsert_leads(leads, campaign_id=campaign_id)
    print(f"[Store] +{n_new} new leads, {n_existing} re-seen "
          f"(campaign_id={campaign_id})")

    # 3d. Optional cold email generation (Haiku, ~$0.0015/lead, FR personalized)
    if generate_emails:
        from cold_email import generate_for_leads
        kept_for_email = [l for l in leads if not l.dropped]
        emails = generate_for_leads(
            kept_for_email,
            sender_offer=sender_offer,
            sender_company=sender_company,
            sender_pitch=sender_pitch,
            target_icp_description=target_icp_description,
            multi_touch=multi_touch,
        )
        # Attach the generated email body to the lead model as extra fields so
        # the XLSX export picks them up.
        for l in kept_for_email:
            key = l.company_siren or l.company_name
            ce = emails.get(key)
            if ce:
                setattr(l, "cold_email_subject", ce.subject)
                setattr(l, "cold_email_body", ce.body)
                setattr(l, "cold_email_angle", ce.angle)
                # v0.15.0 — multi-touch sequence (J+4 + J+10)
                if multi_touch:
                    setattr(l, "cold_email_followup_subject", ce.followup_subject or "")
                    setattr(l, "cold_email_followup_body", ce.followup_body or "")
                    setattr(l, "cold_email_breakup_subject", ce.breakup_subject or "")
                    setattr(l, "cold_email_breakup_body", ce.breakup_body or "")
        n_seq = sum(1 for e in emails.values() if e.followup_body) if multi_touch else 0
        print(f"[Cold-Email] {len(emails)}/{len(kept_for_email)} drafted"
              + (f" · {n_seq} full 3-touch sequences" if multi_touch else ""))

    # 3e. Optional HubSpot push (only kept leads)
    if push_to_hubspot:
        from hubspot_client import sync_leads_to_hubspot
        created, updated, msg = sync_leads_to_hubspot([l for l in leads if not l.dropped])
        print(f"[HubSpot] {msg}")

    # 4. Export both CSV (Excel-FR) and premium XLSX side by side
    if output_stem:
        out_dir = Path("output")
        out_dir.mkdir(exist_ok=True)
        csv_path = out_dir / f"{output_stem}.csv"
        xlsx_path = out_dir / f"{output_stem}.xlsx"
        import csv as _csv
        from sheets_export import HEADERS, _row_for, _write_premium_xlsx
        # Sort kept leads: best ICP first (if scored), else by overall_score
        kept = sorted(
            [l for l in leads if not l.dropped],
            key=lambda x: (getattr(x, "icp_score", 0) or 0, x.overall_score),
            reverse=True,
        )
        rows = [HEADERS] + [_row_for(l) for l in kept]
        with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
            _csv.writer(fh, delimiter=";", quoting=_csv.QUOTE_MINIMAL).writerows(rows)
        try:
            _write_premium_xlsx(rows, xlsx_path)
        except ImportError:
            pass
        kept_path = str(csv_path)
    else:
        kept_path = export_leads([l for l in leads if not l.dropped])

    _summary(leads, kept_path, time.time() - t0)

    # Final quota status — show what's left after this run
    try:
        from quotas import summary as _qs2
        s = _qs2()
        if s.get("bottleneck_leads_remaining") is not None:
            print()
            print(f"[Quotas] After this run: ~{s['bottleneck_leads_remaining']} "
                  f"leads still possible (bottleneck: "
                  f"{s.get('bottleneck_service_label')}).")
            crit = [x for x in s["services"] if x["status"] == "critical"]
            if crit:
                for c in crit:
                    print(f"[Quotas] ⚠ {c['label']} is {c['percent_used']}% used "
                          f"— upgrade or wait for {c['period']} reset.")
    except Exception:
        pass

    return kept_path


def _cli() -> None:
    p = argparse.ArgumentParser(description="Run a full prospection campaign in one call.")
    p.add_argument("--query", help="Free-text Sirene query (e.g., 'cabinet dentaire')")
    p.add_argument("--naf",
                   help="NAF code(s), comma-separated. Examples: "
                        "single='86.23Z'; multi='70.22Z,78.10Z,82.99Z'.")
    p.add_argument("--code-postal",
                   help="Postal code(s), comma-separated. e.g. '69001,69002'.")
    p.add_argument("--departement",
                   help="Département(s), comma-separated. e.g. '31,34,33' "
                        "for Toulouse+Montpellier+Bordeaux.")
    p.add_argument("--region", help="Région code")
    p.add_argument("--tranche-effectif", dest="tranche_effectif",
                   help="Sirene size code: 00=0 emp, 01=1-2, 02=3-5, 03=6-9, "
                        "11=10-19, 12=20-49, 21=50-99, 22=100-199, 31=200-249, "
                        "32=250-499 (use 11 or 12 to target true SMBs and skip chains)")
    p.add_argument("--volume", type=int, default=10,
                   help="Number of QUALIFIED leads to deliver (default 10). "
                        "PERFECT MODE: the agent iterates Sirene pages and "
                        "applies quality gates (junk-name, ICP, foreign-sub, "
                        "operational status) until VOLUME perfect leads are "
                        "collected — may scan up to --max-candidates Sirene "
                        "entries to find them.")
    p.add_argument("--persona", dest="persona_role_hint",
                   help="Hint for the role label in the output (e.g., 'Gérant', 'DRH')")
    p.add_argument("--output", dest="output_stem",
                   help="Output filename stem (e.g., 'prospects-dentistes-lyon')")
    p.add_argument("--max-workers", type=int, default=8,
                   help="Parallel enrichment workers (default 8)")
    p.add_argument("--icp-preset",
                   choices=["cavistes-paris", "palaces-paris", "chr-alcool-compatible",
                            "pme-formation-qse", "eti-b2b-formation"],
                   help="Apply a preset ICP profile and add icp_score column. "
                        "Presets are GENERIC templates — for client-specific "
                        "targeting, prefer --icp-description (natural language). "
                        "Available: "
                        "cavistes-paris (cavistes Paris), "
                        "palaces-paris (hôtellerie haut de gamme), "
                        "chr-alcool-compatible (CHR filtre cuisine_type alcool), "
                        "pme-formation-qse (PME 50-249 santé/BTP/industrie), "
                        "eti-b2b-formation (ETI 100-1999 services B2B générique, "
                        "exclut banques/assurances).")
    # Memory / dedup — ON BY DEFAULT in v0.12.3. Use --include-seen to override.
    p.add_argument("--only-new", action="store_true", default=True,
                   help="DEFAULT ON: skip SIRENs already in lead_store. "
                        "Inverted by --include-seen.")
    p.add_argument("--include-seen", dest="only_new", action="store_false",
                   help="OPT-OUT of dedup: bring back leads already seen in "
                        "previous campaigns. Useful for re-prospecting after "
                        "N months or for outcome-tracking workflows.")
    p.add_argument("--push-to-hubspot", action="store_true",
                   help="Sync kept leads to HubSpot CRM (needs HUBSPOT_ACCESS_TOKEN)")
    p.add_argument("--campaign-id", help="Tag this run in lead_store")
    p.add_argument("--llm-decider", action="store_true",
                   help="Use Claude (Haiku) to pick the operational decision-maker "
                        "from the team page instead of the legal director. ~$0.001/lead.")
    p.add_argument("--retry-dropped", action="store_true",
                   help="After the main pass, retry dropped leads with relaxed thresholds "
                        "(min_contact_conf=30). Self-critique multi-pass.")
    p.add_argument("--tenant", default=None,
                   help="Tenant ID for multi-tenant deployments (defaults to env "
                        "PROSPECT_AGENT_TENANT or 'default'). Leads are isolated "
                        "per tenant in the lead store.")
    p.add_argument("--daily-cap", type=int, default=None,
                   help="Set the daily lead cap before running. Equivalent to "
                        "`python quotas.py set-cap N`. Use 0 to disable.")
    p.add_argument("--paid-threshold", type=int, default=40,
                   help="Two-pass strategy: leads with preliminary_score below "
                        "this don't get the paid waterfall (Dropcontact, "
                        "Hunter, Datagma, BetterContact). 0 = always pay. "
                        "Default 40 = skip the bottom ~30%% which we'd drop "
                        "anyway. Saves ~50%% paid credits.")
    p.add_argument("--max-candidates", type=int, default=None,
                   help="PERFECT MODE: safety cap on Sirene candidates "
                        "scanned per run. Default = max(50, volume*5). "
                        "Higher = better chance of hitting --volume but "
                        "more API quota burned.")
    p.add_argument("--raw-mode", action="store_true",
                   help="DEBUG: disable PERFECT MODE quality gates. Returns "
                        "the first `--volume` Sirene candidates regardless "
                        "of quality. Use only to compare/debug.")
    p.add_argument("--icp-description",
                   help="ICP description in French. Claude Haiku will "
                        "generate the NAF / dept / size / persona "
                        "automatically. Example: 'Je vends du conseil RGPD "
                        "aux ETI industrielles 100-500 emp en AURA'. "
                        "When set, overrides --naf / --dept / --tranche-effectif "
                        "/ --persona (unless those are also explicitly set).")
    p.add_argument("--quotas", action="store_true",
                   help="Print the current quota status and exit (no campaign).")
    p.add_argument("--generate-emails", action="store_true",
                   help="Generate a personalized FR cold email per kept lead via "
                        "Claude Haiku. ~$0.0015/lead. Saved into the XLSX export.")
    p.add_argument("--sender-offer", default="",
                   help="One-line description of what you're selling (FR). "
                        "REQUIRED if --generate-emails is set.")
    p.add_argument("--sender-company", default="",
                   help="Your company name (signed/referenced in the email). "
                        "REQUIRED if --generate-emails is set.")
    p.add_argument("--sender-pitch",
                   help="Multi-line detailed pitch describing your offer "
                        "(value prop, use cases, differentiators). Passed to "
                        "Claude for richer email personalization.")
    # v0.15.0 — séquence multi-touch
    p.add_argument("--multi-touch", action="store_true",
                   help="Generate a 3-touch sequence (J0 cold + J+4 follow-up "
                        "+ J+10 break-up) per lead. Requires --generate-emails. "
                        "Multiplies response rate by 3-4×. ~$0.005/lead.")
    # v0.16.0 — strict premium mode for CHR
    p.add_argument("--require-gmb", action="store_true",
                   help="Drop any lead with NO GMB enrichment AT ALL (no rating "
                        "AND no cuisine_type). Permissive — passes via cuisine_type "
                        "alone (HERE fallback). Use for basic CHR validation.")
    # v0.16.3 — strict variant (rating only, not cuisine alone)
    p.add_argument("--require-gmb-rating", action="store_true",
                   help="STRICT premium: drop any lead without a GMB rating value "
                        "(not just cuisine_type). Requires Google Places quota to "
                        "work (HERE has no rating). Use for 'vraiment haut de gamme' "
                        "campaigns where rating discrimination is critical.")
    p.add_argument("--min-gmb-rating", type=float, default=None,
                   help="Minimum GMB rating (e.g. 4.3 for haut de gamme). Drops "
                        "leads below threshold. Use with --require-gmb-rating.")
    p.add_argument("--min-gmb-reviews", type=int, default=None,
                   help="Minimum GMB review count (e.g. 100 for premium signal). "
                        "Drops leads below threshold.")
    # v0.17.0 — LLM lead reasoner (analyse business sémantique par lead)
    p.add_argument("--icp-strict-llm", action="store_true",
                   help="🧠 GAME-CHANGER: après enrichissement, envoie chaque lead "
                        "à Claude Haiku qui RAISONNE sur le fit business réel "
                        "(lit le site, analyse en 4 étapes, drop les faux positifs "
                        "que le scoring statique laisse passer). Coût ~$0.0005/lead. "
                        "Requiert ANTHROPIC_API_KEY + --icp-description (en NL).")
    # v0.18.0 — reverse sourcing (Google → URL → SIREN au lieu de Sirene NAF)
    p.add_argument("--reverse-source", action="store_true",
                   help="🚀 STRUCTURAL FIX: contourne le sourcing Sirene NAF "
                        "(administratif, pollué). À la place, génère 5 requêtes "
                        "Google depuis --icp-description, scrape les URLs des "
                        "VRAIES boîtes qui matchent, résout vers SIREN via Pappers. "
                        "Coût ~$0.005 (Serper) + 1 Pappers/lead. Requiert SERPER_API_KEY "
                        "et PAPPERS_API_KEY. Combine avec --icp-strict-llm pour "
                        "qualité MAX (sourcing pertinent + analyse business).")
    # v0.15.0 — appels d'offres
    p.add_argument("--rfp-keywords",
                   help="Comma-separated FR keywords to search BOAMP (appels "
                        "d'offres publics). Ex: 'formation IA,conseil digital'. "
                        "When set, the campaign sources prospects FROM matching "
                        "RFPs instead of Sirene — strongest possible intent signal.")
    p.add_argument("--rfp-cpv-preset",
                   choices=["formation_ia", "transformation_numerique",
                            "boissons_alcoolisees", "marketing"],
                   help="Shortcut to a curated CPV codes bundle for BOAMP search.")
    p.add_argument("--rfp-regions",
                   help="Comma-separated FR region names for BOAMP filter "
                        "(ex: 'Occitanie,Nouvelle-Aquitaine'). Auto-expanded to depts.")
    p.add_argument("--rfp-days", type=int, default=90,
                   help="BOAMP lookback window in days (default 90).")
    p.add_argument("--rfp-montant-min", type=int, default=None,
                   help="Drop RFPs with estimated amount below this (€).")
    args = p.parse_args()

    # Quota helpers — let the operator inspect / set caps from the same CLI
    if args.quotas:
        from quotas import summary as _qs, _format_table  # type: ignore
        print(_format_table(_qs()))
        return
    if args.daily_cap is not None:
        from quotas import set_daily_cap
        set_daily_cap(args.daily_cap)
        print(f"[Quotas] Daily cap set to {args.daily_cap} leads.")

    # ICP from natural language — Haiku generates NAF/dept/size/persona
    # from a French description. Sets the args only when not already set
    # explicitly by the user.
    if args.icp_description:
        from icp_from_nl import generate_icp_from_description
        print(f"[ICP-NL] Generating ICP from: {args.icp_description[:80]}...")
        icp = generate_icp_from_description(args.icp_description) or {}
        if not icp:
            print("[ICP-NL] Generation failed. Check ANTHROPIC_API_KEY.")
            p.error("--icp-description failed to produce a valid ICP")
        # Apply ICP fields only where user didn't override
        if not args.naf and icp.get("naf_codes"):
            args.naf = ",".join(icp["naf_codes"])
            print(f"[ICP-NL] NAF set to: {args.naf}")
        if not args.departement and icp.get("departements"):
            args.departement = ",".join(icp["departements"])
            print(f"[ICP-NL] departement set to: {args.departement}")
        if not args.tranche_effectif and icp.get("tranches_effectif"):
            args.tranche_effectif = ",".join(icp["tranches_effectif"])
            print(f"[ICP-NL] tranche_effectif set to: {args.tranche_effectif}")
        if not args.persona_role_hint and icp.get("persona"):
            args.persona_role_hint = icp["persona"]
            print(f"[ICP-NL] persona set to: {args.persona_role_hint}")

    if not any([args.query, args.naf, args.code_postal, args.departement, args.region]):
        p.error("provide at least one filter (--query / --naf / --code-postal / --icp-description ...)")

    # Apply tenant flag for the duration of this process — lead_store reads it
    # from env. This way the rest of the code doesn't have to thread it through.
    if args.tenant:
        import os as _os
        _os.environ["PROSPECT_AGENT_TENANT"] = args.tenant

    icp_profile = None
    if args.icp_preset:
        from icp import (
            PRESET_CAVISTES_PREMIUM_PARIS,
            PRESET_CHR_ALCOOL_COMPATIBLE,
            PRESET_ETI_B2B_FORMATION,
            PRESET_PALACES_PARIS,
            PRESET_PME_FORMATION_QSE,
        )
        icp_profile = {
            "cavistes-paris": PRESET_CAVISTES_PREMIUM_PARIS,
            "palaces-paris": PRESET_PALACES_PARIS,
            "chr-alcool-compatible": PRESET_CHR_ALCOOL_COMPATIBLE,
            "pme-formation-qse": PRESET_PME_FORMATION_QSE,
            "eti-b2b-formation": PRESET_ETI_B2B_FORMATION,
        }[args.icp_preset]

    # v0.15.5 — generic agent: when --generate-emails is set, --sender-company
    # and --sender-offer are REQUIRED (no client-specific defaults).
    if args.generate_emails:
        if not args.sender_company or not args.sender_offer:
            p.error("--generate-emails requires both --sender-company and "
                    "--sender-offer (the agent has NO default sender — it must "
                    "be specified per campaign).")

    # v0.16.0 — Fail-fast Anthropic. 3 flags requièrent ANTHROPIC_API_KEY.
    # Sans la clé, autant abort dès maintenant plutôt que faire tourner
    # le pipeline pendant 10 min pour découvrir que l'email gen a échoué.
    import os as _os
    if (args.generate_emails or args.multi_touch or args.icp_description) \
            and not _os.environ.get("ANTHROPIC_API_KEY"):
        p.error(
            "ANTHROPIC_API_KEY missing from environment. The following flags "
            "REQUIRE Claude Haiku: --generate-emails, --multi-touch, "
            "--icp-description. Either add the key to .env (get one at "
            "https://console.anthropic.com/settings/keys, $5 free credit) "
            "or remove these flags.")

    # Parse v0.15 RFP flags
    rfp_keywords = (
        [k.strip() for k in args.rfp_keywords.split(",") if k.strip()]
        if args.rfp_keywords else None
    )
    rfp_regions = (
        [r.strip() for r in args.rfp_regions.split(",") if r.strip()]
        if args.rfp_regions else None
    )

    run(
        query=args.query,
        naf=args.naf,
        code_postal=args.code_postal,
        departement=args.departement,
        region=args.region,
        tranche_effectif=args.tranche_effectif,
        volume=args.volume,
        persona_role_hint=args.persona_role_hint,
        output_stem=args.output_stem,
        max_workers=args.max_workers,
        icp=icp_profile,
        only_new=args.only_new,
        push_to_hubspot=args.push_to_hubspot,
        campaign_id=args.campaign_id,
        llm_decider=args.llm_decider,
        retry_dropped=args.retry_dropped,
        generate_emails=args.generate_emails,
        sender_offer=args.sender_offer,
        sender_company=args.sender_company,
        sender_pitch=args.sender_pitch,
        target_icp_description=args.icp_description,
        paid_threshold=args.paid_threshold,
        max_candidates=args.max_candidates,
        raw_mode=args.raw_mode,
        multi_touch=args.multi_touch,
        rfp_keywords=rfp_keywords,
        rfp_cpv_preset=args.rfp_cpv_preset,
        rfp_regions=rfp_regions,
        rfp_days=args.rfp_days,
        rfp_montant_min=args.rfp_montant_min,
        require_gmb=args.require_gmb,
        min_gmb_rating=args.min_gmb_rating,
        min_gmb_reviews=args.min_gmb_reviews,
        require_gmb_rating=args.require_gmb_rating,
        icp_strict_llm=args.icp_strict_llm,
        reverse_source_mode=args.reverse_source,
    )


if __name__ == "__main__":
    _cli()
