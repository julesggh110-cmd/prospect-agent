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
    return True


def score_lead(lead, icp: dict) -> int:
    """Return ICP fit score 0-100 (rounded to nearest int)."""
    rules = icp.get("rules", [])
    if not rules:
        return 0
    total_weight = sum(r.get("weight", 0) for r in rules)
    if total_weight == 0:
        return 0
    earned = sum(r.get("weight", 0) for r in rules if evaluate_rule(lead, r))
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


def _cli() -> None:
    import argparse
    import json
    p = argparse.ArgumentParser(description="Print a preset ICP profile or score one lead.")
    p.add_argument("preset", choices=["cavistes-paris", "palaces-paris"])
    args = p.parse_args()
    presets = {
        "cavistes-paris": PRESET_CAVISTES_PREMIUM_PARIS,
        "palaces-paris": PRESET_PALACES_PARIS,
    }
    print(json.dumps(presets[args.preset], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
