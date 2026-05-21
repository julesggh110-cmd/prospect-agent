"""
BODACC — Bulletin Officiel des Annonces Civiles et Commerciales.

FREE PUBLIC API (no auth, no quota). Returns the legal announcements
published in the BODACC for a given SIREN. These are GOLD signals for
B2B prospect qualification:

  ✅ "AUGMENTATION DE CAPITAL"     → company is growing, has cash → BOOST
  ✅ "MODIFICATION OBJET SOCIAL"   → expanding scope → POSSIBLE BOOST
  ✅ "CREATION ETABLISSEMENT"      → new establishment → growing → BOOST
  ❌ "REDRESSEMENT JUDICIAIRE"     → financial trouble → HARD DROP
  ❌ "LIQUIDATION JUDICIAIRE"      → company is dying → HARD DROP
  ❌ "RADIATION"                   → company is gone → HARD DROP
  ⚠️  "DEPOT DES COMPTES"          → neutral, recurring
  ⚠️  "JUGEMENT DE CLOTURE"        → outcome of redressement → CONTEXT

This is the kind of intelligence a paid Pappers / Societé.com gives you,
served FOR FREE by the French government via opendatasoft.

API base: https://bodacc-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets/annonces-commerciales/records
Quota: opendatasoft has soft limits (~10k/day per IP, plenty for us).
Setup: ZERO — no key needed, no signup.

Public API:
    fetch_announcements(siren, *, since_days=730) → list[dict]
    qualify_from_bodacc(siren) → dict with classification + flags
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from http_safe import Throttle

API_URL = (
    "https://bodacc-datadila.opendatasoft.com/"
    "api/explore/v2.1/catalog/datasets/annonces-commerciales/records"
)
DEFAULT_TIMEOUT = 10.0
_THROTTLE = Throttle(min_interval_s=0.3)


# Patterns we look for in the BODACC text. Each pattern → category + impact.
# Impact = "boost" / "drop" / "neutral" / "watchout".
# Patterns are case-insensitive substring matches against the concatenated
# blob of (familleavis_lib, modificationsgenerales, jugement, depot, etc.).
# Categories aligned with BODACC's actual taxonomy (familleavis_lib values).
_PATTERNS: list[tuple[str, str, str]] = [
    # BODACC familleavis_lib values (the main signal — coarse but reliable)
    ("trouble-procedure", "procedures collectives",         "drop"),
    ("trouble-procedure", "procédures collectives",         "drop"),
    ("trouble-redress",   "redressement",                   "drop"),
    ("trouble-liquid",    "liquidation",                    "drop"),
    ("trouble-cessat",    "cessation",                      "drop"),
    ("growth-creation",   "creations",                      "boost"),
    ("growth-creation",   "créations",                      "boost"),
    ("growth-modif",      "modifications diverses",         "watchout"),
    ("dead-radiation",    "radiations",                     "drop"),
    ("dead-radiation",    "radiation",                      "drop"),
    ("sale-transfer",     "ventes et cessions",             "watchout"),
    ("recurring",         "depots des comptes",             "neutral"),
    ("recurring",         "dépôts des comptes",             "neutral"),
    # Fine-grained patterns from modificationsgenerales / jugement text
    ("growth-capital",  "augmentation de capital",          "boost"),
    ("growth-capital",  "augmentation du capital",          "boost"),
    ("growth-scope",    "modification de l'objet",          "boost"),
    ("growth-scope",    "extension de l'objet",             "boost"),
    ("growth-estab",    "creation d'etablissement",         "boost"),
    ("growth-estab",    "ouverture d'etablissement",        "boost"),
    ("growth-merger",   "fusion",                           "watchout"),
    ("growth-transfer", "transfert de siege",               "neutral"),
    ("trouble-judg",    "ouverture d'une procédure",        "drop"),
    ("trouble-judg",    "jugement de redressement",         "drop"),
    ("trouble-judg",    "jugement de liquidation",          "drop"),
    ("trouble-judg",    "jugement prononçant",              "drop"),
    ("recovery",        "plan de redressement adopté",      "watchout"),
    ("recovery",        "jugement de clôture",              "neutral"),
    ("change-dir",      "changement de gérant",             "neutral"),
    ("change-dir",      "nomination de gérant",             "neutral"),
    ("change-dir",      "nomination de l'administrateur",   "neutral"),
]


def fetch_announcements(siren: str, *, since_days: int = 730) -> list[dict]:
    """Fetch BODACC announcements for `siren` from the last `since_days`.

    Default window: 730 days (~2 years) — captures the relevant business-life
    events for prospect qualification without flooding the response.

    Returns a list of dicts: [{date, type, content_text, type_avis, ...}, ...]
    Newest first. Empty list on any failure.
    """
    if not siren or len(siren) != 9 or not siren.isdigit():
        return []

    _THROTTLE.acquire()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).date().isoformat()
    # opendatasoft v2.1: `registre` is an array; exact-match works on either
    # spaced or unspaced form. Date filter uses plain ISO string (the
    # `date'YYYY-MM-DD'` literal also works).
    params = {
        "where": f'registre="{siren}" AND dateparution>="{cutoff}"',
        "limit": 50,
        "order_by": "dateparution DESC",
    }
    try:
        with httpx.Client(
            timeout=DEFAULT_TIMEOUT,
            headers={"User-Agent": "ProspectAgent/0.12 (gouv.fr opendatasoft client)"},
        ) as c:
            r = c.get(API_URL, params=params)
            if r.status_code != 200:
                return []
            data = r.json()
            try:
                from quotas import mark_used
                mark_used("bodacc")
            except Exception:
                pass
            return data.get("results") or []
    except Exception:
        return []


def qualify_from_bodacc(siren: str, *, since_days: int = 730) -> dict:
    """Classify a SIREN's recent BODACC activity into a single verdict.

    Returns:
        {
            "siren": str,
            "n_announcements": int,
            "categories_found": [str, ...],     # unique
            "highest_impact": "drop" | "boost" | "watchout" | "neutral" | "none",
            "verdict": "HARD_DROP" | "QUALITY_BOOST" | "NEUTRAL" | "WATCHOUT",
            "reason": str,                       # human-readable
            "icp_modifier": int,                 # -100, +10, 0, etc.
            "latest_event_date": str | None,
        }
    """
    out = {
        "siren": siren,
        "n_announcements": 0,
        "categories_found": [],
        "highest_impact": "none",
        "verdict": "NEUTRAL",
        "reason": "no recent BODACC activity",
        "icp_modifier": 0,
        "latest_event_date": None,
    }
    if not siren:
        return out
    announcements = fetch_announcements(siren, since_days=since_days)
    if not announcements:
        return out
    out["n_announcements"] = len(announcements)
    out["latest_event_date"] = announcements[0].get("dateparution")

    impact_priority = {"drop": 4, "boost": 3, "watchout": 2, "neutral": 1, "none": 0}
    best_impact = "none"
    best_category = None
    categories_found: list[str] = []

    for a in announcements:
        # The actual classified content lives in several optional fields —
        # we concatenate everything text-like and run regexes once. The key
        # fields per opendatasoft schema: familleavis_lib, modificationsgenerales,
        # jugement, depot, listepersonnes (for administration changes).
        blob_parts = []
        for k in ("familleavis_lib", "typeavis_lib", "modificationsgenerales",
                   "jugement", "depot", "listepersonnes", "acte"):
            v = a.get(k)
            if v:
                blob_parts.append(str(v).lower())
        blob = " | ".join(blob_parts)
        if not blob:
            continue
        for category, pattern, impact in _PATTERNS:
            if pattern in blob:
                if category not in categories_found:
                    categories_found.append(category)
                if impact_priority[impact] > impact_priority[best_impact]:
                    best_impact = impact
                    best_category = category

    out["categories_found"] = categories_found
    out["highest_impact"] = best_impact

    # Convert impact → verdict + ICP modifier + reason
    if best_impact == "drop":
        out["verdict"] = "HARD_DROP"
        out["icp_modifier"] = -100
        out["reason"] = (
            f"BODACC: {best_category} detected → company in trouble or dissolved. Drop."
        )
    elif best_impact == "boost":
        out["verdict"] = "QUALITY_BOOST"
        out["icp_modifier"] = 10
        out["reason"] = (
            f"BODACC: {best_category} in last 2 years → company growing. Boost."
        )
    elif best_impact == "watchout":
        out["verdict"] = "WATCHOUT"
        out["icp_modifier"] = -5
        out["reason"] = (
            f"BODACC: {best_category} → ownership/structure shift, verify before pitch."
        )
    else:
        out["verdict"] = "NEUTRAL"
        out["reason"] = "BODACC activity is routine (depot des comptes, etc.)"
    return out


def _cli() -> None:
    import argparse
    import json
    p = argparse.ArgumentParser(description="BODACC announcement classifier")
    p.add_argument("siren", help="9-digit SIREN")
    p.add_argument("--since-days", type=int, default=730, help="Look-back window (default 730)")
    p.add_argument("--raw", action="store_true", help="Dump raw API response")
    args = p.parse_args()
    if args.raw:
        anns = fetch_announcements(args.siren, since_days=args.since_days)
        print(json.dumps(anns, indent=2, ensure_ascii=False)[:6000])
        return
    q = qualify_from_bodacc(args.siren, since_days=args.since_days)
    print(json.dumps(q, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
