"""
ICP scoring — give each lead a 0-100 score against the user's Ideal Customer Profile.

Approach: a declarative ICP profile (JSON / Python dict) with weighted rules.
The score is the weighted average of rule matches. Drop-from-deliverable
threshold is decided by the caller (typical: keep top 80, nurture rest).

ICP profile schema:
{
    "name": "Cavistes premium Paris",
    "rules": [
        {"name": "Sector match",
         "weight": 30,
         "naf_starts_with": ["47.25Z"]},                # caviste
        {"name": "Right size",
         "weight": 25,
         "size_in": ["02", "03", "11"]},                # 1-19 employees
        {"name": "Has website",
         "weight": 15,
         "has_field": "company_website"},
        {"name": "Has decision-maker email",
         "weight": 20,
         "min_field_confidence": {"person_email": 60}},
        {"name": "Has LinkedIn",
         "weight": 10,
         "has_field_confidence_above": {"person_linkedin": 60}},
    ]
}

Rule types implemented:
- `naf_starts_with`: list of NAF code prefixes to match (any-of)
- `naf_in`: exact NAF codes to match
- `city_in`: exact cities to match (case-insensitive)
- `size_in`: list of Sirene size codes
- `has_field`: lead field must be non-empty (e.g., "company_website")
- `has_field_confidence_above`: {"person_email": 60} → email confidence ≥ 60
- `min_field_confidence`: alias of the above
- `cuisine_type_in`: lead.cuisine_type matches any of these (substring, case-insensitive)
- `cuisine_type_not_in`: REJECT if lead.cuisine_type matches any (drops vegan/halal)
- `gmb_rating_above`: float, lead.gmb_rating must be >= this (premium signal)
- `gmb_review_count_above`: int, established business signal

All rules are evaluated; their weights sum to 100 (caller should ensure that).
A rule that matches contributes its weight; otherwise contributes 0.
"""
from __future__ import annotations

from typing import Any, Optional


def _get_field(lead, field: str):
    """Resolve 'company_website', 'person_email.value', etc."""
    obj: Any = lead
    for part in field.split("."):
        obj = getattr(obj, part, None) if obj is not None else None
    return obj


def _confidence_of(lead, field: str) -> int:
    """For a scored field name like 'person_email', return its confidence."""
    f = getattr(lead, field, None)
    return getattr(f, "confidence", 0) if f is not None else 0


def evaluate_rule(lead, rule: dict) -> bool:
    """Return True if `lead` matches `rule`."""
    if "naf_starts_with" in rule:
        naf = lead.company_naf or ""
        if not any(naf.startswith(p) for p in rule["naf_starts_with"]):
            return False
    if "naf_in" in rule:
        if (lead.company_naf or "") not in rule["naf_in"]:
            return False
    # v0.15.1 — exclusion rules (used to drop entire sectors with their own
    # OPCO/budget formation interne — e.g. banks/insurances/mutuelles)
    if "naf_not_starts_with" in rule:
        naf = lead.company_naf or ""
        if any(naf.startswith(p) for p in rule["naf_not_starts_with"]):
            return False
    if "naf_not_in" in rule:
        if (lead.company_naf or "") in rule["naf_not_in"]:
            return False
    if "city_in" in rule:
        city = (lead.company_city or "").lower()
        if city not in {c.lower() for c in rule["city_in"]}:
            return False
    if "size_in" in rule:
        if (lead.company_size or "") not in rule["size_in"]:
            return False
    if "has_field" in rule:
        val = _get_field(lead, rule["has_field"])
        if not val:
            return False
    if "has_field_confidence_above" in rule:
        for fname, threshold in rule["has_field_confidence_above"].items():
            if _confidence_of(lead, fname) < threshold:
                return False
    if "min_field_confidence" in rule:
        for fname, threshold in rule["min_field_confidence"].items():
            if _confidence_of(lead, fname) < threshold:
                return False
    if "cuisine_type_in" in rule:
        ct = (getattr(lead, "cuisine_type", "") or "").lower()
        if not any(kw.lower() in ct for kw in rule["cuisine_type_in"]):
            return False
    if "cuisine_type_not_in" in rule:
        ct = (getattr(lead, "cuisine_type", "") or "").lower()
        if any(kw.lower() in ct for kw in rule["cuisine_type_not_in"]):
            return False
    if "gmb_rating_above" in rule:
        r = getattr(lead, "gmb_rating", None)
        if r is None or float(r) < float(rule["gmb_rating_above"]):
            return False
    if "gmb_review_count_above" in rule:
        rc = getattr(lead, "gmb_rating_count", None)
        if rc is None or int(rc) < int(rule["gmb_review_count_above"]):
            return False
    # v0.14.0 — tech_stack signals (set by Wappalyzer-LITE on the homepage)
    # tech_signals_any: matches if AT LEAST ONE of the listed signals is set
    # tech_signals_all: matches only when ALL listed signals are set
    # Examples: {"tech_signals_any": ["has-automation","has-crm"]}
    if "tech_signals_any" in rule:
        sigs = set(getattr(lead, "tech_signals", []) or [])
        if not (sigs & set(rule["tech_signals_any"])):
            return False
    if "tech_signals_all" in rule:
        sigs = set(getattr(lead, "tech_signals", []) or [])
        if not set(rule["tech_signals_all"]).issubset(sigs):
            return False
    if "tech_maturity_above" in rule:
        tm = getattr(lead, "tech_maturity", 0) or 0
        if int(tm) < int(rule["tech_maturity_above"]):
            return False
    # v0.14.0 — France Travail hiring signal (boîte qui recrute = budget IA)
    # hiring_intensity_min: "low" | "medium" | "high"
    if "hiring_intensity_min" in rule:
        order = {"none": 0, "low": 1, "medium": 2, "high": 3}
        threshold = order.get(rule["hiring_intensity_min"], 0)
        actual = order.get(getattr(lead, "ft_hiring_intensity", None) or "", -1)
        if actual < threshold:
            return False
    # v0.15.0 — Careers page TILT categories
    # Examples:
    #   {"careers_tilt_any": ["ai", "data", "automation"]}
    #   {"careers_tilt_all": ["data", "automation"]}
    if "careers_tilt_any" in rule:
        cats = set(getattr(lead, "careers_tilt_categories", []) or [])
        if not (cats & set(rule["careers_tilt_any"])):
            return False
    if "careers_tilt_all" in rule:
        cats = set(getattr(lead, "careers_tilt_categories", []) or [])
        if not set(rule["careers_tilt_all"]).issubset(cats):
            return False
    if "careers_min_jobs" in rule:
        n = getattr(lead, "careers_n_jobs", 0) or 0
        if int(n) < int(rule["careers_min_jobs"]):
            return False
    # v0.15.0 — Lifecycle stage (Sirene age)
    # Example: {"lifecycle_stage_in": ["scaling", "mature"]}
    if "lifecycle_stage_in" in rule:
        stage = getattr(lead, "lifecycle_stage", None)
        if stage not in rule["lifecycle_stage_in"]:
            return False
    if "company_age_max_months" in rule:
        m = getattr(lead, "company_age_months", None)
        if m is None or int(m) > int(rule["company_age_max_months"]):
            return False
    if "company_age_min_months" in rule:
        m = getattr(lead, "company_age_months", None)
        if m is None or int(m) < int(rule["company_age_min_months"]):
            return False
    # v0.15.0 — RFP active (appel d'offres matching)
    # Boolean rule: lead has an active matching RFP
    if "has_active_rfp" in rule and rule["has_active_rfp"]:
        if not getattr(lead, "rfp_active", None):
            return False
    return True


def score_lead(lead, icp: dict) -> int:
    """Return ICP fit score 0-100 (rounded to nearest int).

    v0.15.1 — Exclusion rules (those containing `naf_not_starts_with` or
    `naf_not_in`) are treated as HARD FILTERS: if they fail, the lead's
    final score is 0 regardless of other rules.
    """
    rules = icp.get("rules", [])
    if not rules:
        return 0
    # Hard-filter exclusions — if any "exclude" rule fails, kill the score
    for r in rules:
        if any(k in r for k in ("naf_not_starts_with", "naf_not_in")):
            if not evaluate_rule(lead, r):
                return 0
    # Standard weighted scoring on the rest (skip 0-weight rules)
    scoring_rules = [r for r in rules if r.get("weight", 0) > 0]
    total_weight = sum(r.get("weight", 0) for r in scoring_rules)
    if total_weight == 0:
        return 0
    earned = sum(r.get("weight", 0) for r in scoring_rules if evaluate_rule(lead, r))
    return int(round(100 * earned / total_weight))


def annotate_leads(leads: list, icp: dict) -> None:
    """Mutates each lead: adds an `icp_score` attribute."""
    for lead in leads:
        setattr(lead, "icp_score", score_lead(lead, icp))


# ---------------------------------------------------------------------------
# A few useful preset ICPs (more in docs/icp/*.json later)
# ---------------------------------------------------------------------------

PRESET_CAVISTES_PREMIUM_PARIS = {
    "name": "Cavistes premium Paris",
    "rules": [
        {"name": "Caviste (NAF 47.25Z)", "weight": 30, "naf_starts_with": ["47.25"]},
        {"name": "Paris intramuros", "weight": 20, "city_in": ["PARIS"]},
        {"name": "Petite/moyenne taille", "weight": 15, "size_in": ["02", "03", "11", "12"]},
        {"name": "A un site web", "weight": 15, "has_field": "company_website"},
        {"name": "Email décideur fiable", "weight": 15, "min_field_confidence": {"person_email": 60}},
        {"name": "LinkedIn entreprise présent", "weight": 5, "has_field_confidence_above": {"company_linkedin": 50}},
    ],
}

PRESET_PALACES_PARIS = {
    "name": "Palaces parisiens",
    "rules": [
        {"name": "Hôtellerie (NAF 55.10Z)", "weight": 25, "naf_starts_with": ["55.10"]},
        {"name": "Paris", "weight": 15, "city_in": ["PARIS"]},
        {"name": "Grande structure", "weight": 20, "size_in": ["31", "32", "41", "42", "51", "52"]},
        {"name": "A un site web premium", "weight": 15, "has_field": "company_website"},
        {"name": "Email décideur fiable", "weight": 15, "min_field_confidence": {"person_email": 60}},
        {"name": "LinkedIn entreprise", "weight": 10, "has_field_confidence_above": {"company_linkedin": 50}},
    ],
}

# Bear Brothers — premium spirits brand selling into CHR (Café/Hôtel/Restaurant).
# Uses Google My Business cuisine_type as the operational qualifier:
#   - BOOST cuisines that pair with spirits (gastro, brasserie, italian, bar)
#   - REJECT cuisines incompatible with alcohol (vegan, healthy, halal-only,
#     student canteen)
# This single ICP is what turns a generic "restos Toulouse" list into a
# pre-qualified Bear Brothers prospect list.
PRESET_BEAR_BROTHERS_CHR = {
    "name": "Bear Brothers CHR (spiritueux premium)",
    "rules": [
        # +30 if NAF is restaurant / café / bar
        {"name": "CHR (restos/bars)", "weight": 25,
         "naf_starts_with": ["56.10", "56.30", "55.10"]},
        # +20 if cuisine type pairs with spirits (Google My Business label)
        {"name": "Cuisine compatible spiritueux", "weight": 20,
         "cuisine_type_in": [
             "français", "francaise", "francais",
             "brasserie", "bistro", "gastrono",
             "italien", "italienne",
             "bar", "cocktail", "pub", "wine",
             "lounge", "tapas", "espagnol",
             "fine dining", "fusion",
         ]},
        # -20 if cuisine type is incompatible (vegan, halal, cantine)
        {"name": "Cuisine REJET (végé/halal/cantine)", "weight": 20,
         "cuisine_type_not_in": [
             "végétar", "vegetar", "vegan", "végan",
             "healthy", "salade", "salad",
             "halal", "kebab",
             "boulang", "patisser", "patiss",
             "cafétér", "cafeter", "fast food",
             "asiatique", "asian",  # often low-alcohol culture
         ]},
        # +10 if Google rating ≥ 4.0 (established, popular = budget for premium)
        {"name": "Bien noté (≥4.0)", "weight": 10, "gmb_rating_above": 4.0},
        # +10 if at least 50 reviews (real established business, not pop-up)
        {"name": "Établi (≥50 reviews)", "weight": 10, "gmb_review_count_above": 50},
        # +10 if a website is published (means investment in branding)
        {"name": "Site web actif", "weight": 5, "has_field": "company_website"},
        # +5 if person LinkedIn — enables a warm DM pre-meeting
        {"name": "LinkedIn décideur", "weight": 5,
         "has_field_confidence_above": {"person_linkedin": 60}},
        # +5 if person email — direct mail channel
        {"name": "Email décideur", "weight": 5,
         "has_field_confidence_above": {"person_email": 40}},
    ],
}


# Comeos — Toulouse-based QSE consulting + training firm. Triple cert
# (management, RH, santé-sécurité au travail, qualité, pratiques pro santé).
# Target: French SMBs 50-249 employees whose business creates obligatory
# QSE / HSE / RH training spend.
# Prioritization order:
#   1. Santé / médico-social (Comeos' deepest expertise) — EHPAD, hébergement
#      social, cabinets médicaux. NAF 87.10*, 87.30*, 86.21Z, 86.22*.
#   2. Industrie — fab. métallique, machines, agro. NAF 25.*, 28.*, 10.*, 11.*.
#   3. BTP — formation sécurité obligatoire. NAF 41.20*, 43.*.
#   4. Services B2B — emploi/intérim (78.*), services bât. (81.*), SSII (62.*).
# Geo: Occitanie en priorité (31, 09, 11, 12, 30, 32, 34, 46, 48, 65, 66, 81,
# 82) puis grand sud-ouest. Bonus pour entreprise avec site/LinkedIn (maturité
# RH = budget formation existant).
PRESET_COMEOS_FORMATION = {
    "name": "Comeos — Formation/QSE PME 50-249",
    "rules": [
        # Sector fit: tiered weights, only the best-fit tier scores
        {"name": "Santé/médico-social (EHPAD, hébergement, cabinets)",
         "weight": 25,
         "naf_starts_with": [
             "87.10", "87.30",          # EHPAD + hébergement social
             "86.21", "86.22", "86.23", # cabinets médicaux + cliniques
             "86.90",                    # autres soins
             "88.10", "88.91", "88.99",  # action sociale sans hébergement
         ]},
        {"name": "Industrie (QSE/sécurité au travail)",
         "weight": 15,
         "naf_starts_with": [
             "10.", "11.",              # agro
             "20.", "21.", "22.",       # chimie/pharma/plastiques
             "23.", "24.", "25.",       # minéraux/métallurgie/produits métal
             "26.", "27.", "28.",       # électronique/machines
             "29.", "30.",              # automobile/transport
         ]},
        {"name": "BTP (formation sécurité obligatoire)",
         "weight": 12,
         "naf_starts_with": [
             "41.20", "42.",            # construction + génie civil
             "43.",                      # travaux spécialisés
         ]},
        {"name": "Services B2B (management + RH)",
         "weight": 8,
         "naf_starts_with": [
             "78.",                      # emploi/intérim
             "81.",                      # services aux bâtiments
             "62.", "63.",              # IT/SSII
         ]},
        # Right size: 50-249 employees (Sirene codes 21=50-99, 22=100-199, 31=200-249)
        {"name": "Taille 50-249 emp (cible PME ETI)",
         "weight": 15,
         "size_in": ["21", "22", "31"]},
        # Geo: Occitanie priority
        {"name": "Occitanie (priorité géo)",
         "weight": 10,
         "city_in": [
             # Major cities of Occitanie depts 31/09/11/12/30/32/34/46/48/65/66/81/82
             "TOULOUSE", "MONTPELLIER", "NIMES", "PERPIGNAN", "BEZIERS",
             "MONTAUBAN", "ALBI", "CARCASSONNE", "TARBES", "RODEZ", "AUCH",
             "CAHORS", "MENDE", "FOIX", "NARBONNE", "SETE", "BLAGNAC",
             "COLOMIERS", "TOURNEFEUILLE", "MURET", "BALMA", "LABEGE",
             "RAMONVILLE-SAINT-AGNE", "L'UNION", "PORTET-SUR-GARONNE",
             "CASTANET-TOLOSAN", "PLAISANCE-DU-TOUCH", "SAINT-ORENS-DE-GAMEVILLE",
             "FONSORBES", "CASTRES", "MAZAMET", "MILLAU", "VILLEFRANCHE-DE-ROUERGUE",
             "LOURDES", "AGDE", "FRONTIGNAC",
         ]},
        # Contactability: website + decision-maker
        {"name": "A un site web actif",
         "weight": 10,
         "has_field": "company_website"},
        {"name": "Email décideur identifié",
         "weight": 10,
         "has_field_confidence_above": {"person_email": 50}},
        {"name": "LinkedIn décideur",
         "weight": 5,
         "has_field_confidence_above": {"person_linkedin": 50}},
    ],
}


# v0.15.1 — preset dédié aux grandes structures privées (ETI 200-1999 sal)
# Tiré de l'audit campagne "comeos-200plus-sud-1" (mai 2026) :
#   - 8/10 leads = banques (NAF 64.19Z) → ont leur OPCO BPCE/CA interne,
#     pas de budget formation externe. EXCLUSION DURE.
#   - 10/10 leads = lifecycle "legacy" (>5 ans) → à cette taille, c'est
#     systématique. Le signal lifecycle est mort, on ne l'utilise PAS.
#   - v0.15.2 — autotest Multica confirme: 6/6 leads en "legacy" pour ETI 100+.
#     Aucune règle lifecycle_* dans ce preset (vérifié = correct).
#   - 10/10 ft_hiring_intensity "high" (saturé HQ-agg). v0.15.1 met "saturated"
#     pour ces cas → on n'utilise QUE high/medium réels (donc seules les
#     vraies hyper-croissances ressortent).
#   - Tech maturity 0/10 > 25 → on baisse à 20 pour avoir un signal qui fire.
#
# Public cible Comeos formation IA :
#   - ETI mid-market 100-1999 salariés (tranche 22, 31, 32, 41, 42)
#   - Secteurs où la formation IA générique CONVERTIT : aéro, ESN, conseil,
#     ingénierie, expertise comptable, pharma, cosmétique, agro premium.
#   - Exclus : banque/assurance/mutuelle (OPCO interne), public, syndicats.
PRESET_COMEOS_ETI_FORMATION = {
    "name": "Comeos — Formation IA / ETI privées 100-1999 sal",
    "rules": [
        # === EXCLUSIONS DURES (hard filter — désactive le lead s'il match) ===
        {"name": "EXCLURE banque / assurance / mutuelle (OPCO interne)",
         "weight": 0,
         "naf_not_starts_with": [
             "64.",   # activités des services financiers (banques)
             "65.",   # assurances
             "66.",   # activités auxiliaires de services financiers et d'assurance
         ]},
        # === TAILLE — ETI mid-market (poids fort) ===
        {"name": "ETI 100-1999 salariés (vrais décideurs accessibles)",
         "weight": 25,
         "size_in": ["22", "31", "32", "41", "42"]},  # 100→1999 sal
        # === SECTEURS À FORT FIT (poids fort) ===
        {"name": "Tech tier 1 — aéro / ESN / conseil / ingénierie",
         "weight": 25,
         "naf_starts_with": [
             "30.30",  # aéronautique / spatial
             "62.",    # programmation / conseil informatique / SSII
             "70.22",  # conseil pour les affaires
             "71.12",  # ingénierie études techniques
             "73.",    # publicité / études de marché
             "72.",    # recherche-développement
         ]},
        {"name": "Tech tier 2 — pharma / cosmétique / agro premium / énergie",
         "weight": 15,
         "naf_starts_with": [
             "21.",    # pharma
             "20.42",  # cosmétiques
             "10.71",  # boulangerie indus
             "11.01",  # spiritueux
             "35.",    # énergie
             "69.20",  # expertise comptable / audit
             "69.10",  # avocats
         ]},
        # === CONTACTABILITÉ ===
        {"name": "A un site web actif",
         "weight": 10,
         "has_field": "company_website"},
        {"name": "Téléphone décideur identifié",
         "weight": 8,
         "has_field_confidence_above": {"person_phone": 50}},
        {"name": "Email décideur fiable",
         "weight": 7,
         "has_field_confidence_above": {"person_email": 60}},
        # === SIGNAUX v0.14/v0.15 ===
        # Stack moderne (CRM, automation, framework) — équipe déjà outillée
        {"name": "Stack tech mature (>= 20)",
         "weight": 10,
         "tech_maturity_above": 20},
        # Career page avec rôles IA/data/automation = TILT MAX
        {"name": "Recrute des rôles tech (AI/data/automation)",
         "weight": 15,
         "careers_tilt_any": ["ai", "data", "automation"]},
        # Recrutement réel via FT (NON saturé — v0.15.1)
        {"name": "FT hiring medium ou high (non-saturé)",
         "weight": 10,
         "hiring_intensity_min": "medium"},
        # BOAMP : si AO formation IA actif = signal d'intention max
        {"name": "AO formation IA actif (BOAMP)",
         "weight": 15,
         "has_active_rfp": True},
        # BODACC : augmentation capital récente = budget dispo
        # (note: pas de rule directe, surfacé via preliminary_score)
    ],
}


def _cli() -> None:
    import argparse
    import json
    p = argparse.ArgumentParser(description="Print a preset ICP profile or score one lead.")
    p.add_argument("preset", choices=[
        "cavistes-paris", "palaces-paris",
        "comeos-formation", "comeos-eti-formation",
    ])
    args = p.parse_args()
    presets = {
        "cavistes-paris": PRESET_CAVISTES_PREMIUM_PARIS,
        "palaces-paris": PRESET_PALACES_PARIS,
        "comeos-formation": PRESET_COMEOS_FORMATION,
        "comeos-eti-formation": PRESET_COMEOS_ETI_FORMATION,
    }
    print(json.dumps(presets[args.preset], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
