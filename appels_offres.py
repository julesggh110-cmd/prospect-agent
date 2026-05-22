"""
Appels d'offres — détecte les organismes/entreprises qui PUBLIENT activement
un AO matchant notre offre. C'est LE signal d'intention d'achat le plus fort
qui existe : « j'ai un budget, envoyez-moi des propositions ».

Source : BOAMP (Bulletin Officiel des Annonces des Marchés Publics) via
opendatasoft. 100% open data, gratuit, pas de quota dur.

Endpoint : https://boamp-datadila.opendatasoft.com/api/explore/v2.1/...

Pour chaque AO actif (deadline future) matchant les keywords, on extrait :
  - idweb, objet (titre du marché)
  - nomacheteur + siret_acheteur → lien direct vers Sirene/Pappers
  - contact (nom + email + tel) extraits du JSON 'donnees'
  - CPV code (catégorie achat)
  - dateparution + datelimitereponse
  - montant estimé (VALEUR_MAX si dispo)
  - région, département
  - url_avis (BOAMP)

Usage :
    from appels_offres import search_boamp
    rfps = search_boamp(
        keywords=["formation IA", "intelligence artificielle"],
        regions=["Occitanie"],
        days=90,
        only_active=True,
    )
    for r in rfps:
        print(r["objet"], r["organisme"], r["deadline"], r["siret"])

Le pipeline aval (run_campaign --rfp-keywords) prend ces SIRET, les enrichit
normalement (Sirene → web → tech stack), et génère des cold emails qui
mentionnent EXPLICITEMENT l'AO (« vu votre AO 25-XXXX, voici pourquoi… »).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional

import httpx

from http_safe import Throttle

API_BASE = "https://boamp-datadila.opendatasoft.com/api/explore/v2.1"
DATASET = "boamp"
TIMEOUT = 20.0
_THROTTLE = Throttle(min_interval_s=0.4)


# ---------------------------------------------------------------------------
# CPV code presets — mapping offer category → most relevant CPV codes
# ---------------------------------------------------------------------------
# CPV = Common Procurement Vocabulary (Vocabulaire commun pour les marchés
# publics). Standard EU codes. Always 8 digits. The first 2 digits = division.
# https://simap.ted.europa.eu/cpv (référence officielle)
CPV_PRESETS: dict[str, list[str]] = {
    # === Formation / Conseil IA ===
    "formation_ia": [
        "80500000",  # Services de formation
        "80510000",  # Services de formation spécialisée
        "80531200",  # Services de formation IT
        "80533000",  # Services de familiarisation avec l'informatique et formation
        "73000000",  # Services de R&D et conseil connexes
        "73220000",  # Services de conseil en développement
        "79410000",  # Services de conseil en gestion
        "79411000",  # Services généraux de conseil en gestion
    ],
    # === Transformation numérique / IT ===
    "transformation_numerique": [
        "72000000",  # Services de TI
        "72200000",  # Services de programmation et de conseil en logiciel
        "72224000",  # Services de conseil en gestion de projet
        "72600000",  # Services d'assistance informatique
        "79400000",  # Services de conseil commercial et gestion
    ],
    # === Spiritueux / Boissons (Bear Brothers) ===
    "boissons_alcoolisees": [
        "15910000",  # Boissons alcoolisées distillées
        "15911000",  # Boissons spiritueuses
        "15920000",  # Vins
        "15931000",  # Vins de table
        "15940000",  # Cidre et autres vins de fruits
    ],
    # === Marketing / Communication ===
    "marketing": [
        "79340000",  # Services de publicité et de marketing
        "79341000",  # Services de publicité
        "79342000",  # Services de marketing
        "79822500",  # Services de conception graphique
    ],
}


# ---------------------------------------------------------------------------
# Department code → region (France métropolitaine + DROM)
# ---------------------------------------------------------------------------
_REGION_TO_DEPTS: dict[str, list[str]] = {
    "Auvergne-Rhône-Alpes": ["01", "03", "07", "15", "26", "38", "42", "43", "63", "69", "73", "74"],
    "Bourgogne-Franche-Comté": ["21", "25", "39", "58", "70", "71", "89", "90"],
    "Bretagne": ["22", "29", "35", "56"],
    "Centre-Val de Loire": ["18", "28", "36", "37", "41", "45"],
    "Corse": ["2A", "2B"],
    "Grand Est": ["08", "10", "51", "52", "54", "55", "57", "67", "68", "88"],
    "Hauts-de-France": ["02", "59", "60", "62", "80"],
    "Île-de-France": ["75", "77", "78", "91", "92", "93", "94", "95"],
    "Normandie": ["14", "27", "50", "61", "76"],
    "Nouvelle-Aquitaine": ["16", "17", "19", "23", "24", "33", "40", "47", "64", "79", "86", "87"],
    "Occitanie": ["09", "11", "12", "30", "31", "32", "34", "46", "48", "65", "66", "81", "82"],
    "Pays de la Loire": ["44", "49", "53", "72", "85"],
    "Provence-Alpes-Côte d'Azur": ["04", "05", "06", "13", "83", "84"],
}


def regions_to_depts(regions: list[str]) -> list[str]:
    """Expand region names to a flat list of dept codes (e.g. 'Occitanie' → ['09', '11', …])."""
    out: list[str] = []
    for r in regions:
        # Tolerate case + accents differences
        match = next((k for k in _REGION_TO_DEPTS if k.lower() == r.lower().strip()), None)
        if match:
            out.extend(_REGION_TO_DEPTS[match])
    return out


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _build_where_clause(
    keywords: Optional[list[str]],
    cpv_codes: Optional[list[str]],
    departments: Optional[list[str]],
    since_days: int,
    only_active: bool,
) -> str:
    """Build the opendatasoft `where` ODSQL clause."""
    parts: list[str] = []
    if keywords:
        # search() does fuzzy match on the field, multi-term = OR
        kw_or = " OR ".join(f"search(objet, \"{k}\")" for k in keywords)
        parts.append(f"({kw_or})")
    if cpv_codes:
        # CPV is buried in the `donnees` JSON string — we search-match on it
        cpv_or = " OR ".join(f"search(donnees, \"{c}\")" for c in cpv_codes)
        parts.append(f"({cpv_or})")
    if departments:
        # `code_departement` is an array — use `in` operator
        depts = ",".join(f'"{d}"' for d in departments)
        parts.append(f"code_departement in ({depts})")
    if since_days:
        from datetime import datetime as _dt, timedelta as _td
        cutoff = (_dt.now(timezone.utc) - _td(days=since_days)).date().isoformat()
        parts.append(f"dateparution >= \"{cutoff}\"")
    if only_active:
        today = datetime.now(timezone.utc).date().isoformat()
        parts.append(f"datelimitereponse > \"{today}\"")
    return " AND ".join(parts) if parts else ""


# Extract contact + SIRET + CPV + amount from the JSON-stringified `donnees`
# field. BOAMP's schema is messy (sub-keys vary per period), so be defensive.
def _parse_donnees(raw: Optional[str]) -> dict:
    if not raw or not isinstance(raw, str):
        return {}
    try:
        d = json.loads(raw)
    except Exception:
        return {}
    out: dict = {}
    identite = d.get("IDENTITE") or {}
    if isinstance(identite, dict):
        out["siret"] = (identite.get("CODE_IDENT_NATIONAL") or "").strip() or None
        out["contact_name"] = (identite.get("CORRESPONDANT") or "").strip() or None
        out["contact_email"] = (identite.get("MEL") or "").strip().lower() or None
        out["contact_phone"] = (identite.get("TEL") or "").strip() or None
        out["organisme_full"] = (identite.get("DENOMINATION") or "").strip() or None
        out["organisme_address"] = (identite.get("ADRESSE") or "").strip() or None
        out["organisme_cp"] = (identite.get("CP") or "").strip() or None
        out["organisme_ville"] = (identite.get("VILLE") or "").strip() or None
    objet = d.get("OBJET") or {}
    if isinstance(objet, dict):
        cpv = objet.get("CPV") or {}
        if isinstance(cpv, dict):
            out["cpv_principal"] = (cpv.get("PRINCIPAL") or "").strip() or None
        out["titre_marche"] = (objet.get("TITRE_MARCHE") or "").strip() or None
        out["objet_complet"] = (objet.get("OBJET_COMPLET") or "").strip() or None
        # Estimated monetary value — try the most common nesting paths
        for path in (
            ["CARACTERISTIQUES", "VALEUR_MAX"],
            ["ACCORD_CADRE", "VALEUR_MAX"],
            ["CARACTERISTIQUES", "VALEUR"],
        ):
            v = objet
            try:
                for p in path:
                    v = v.get(p) or {}
                if isinstance(v, dict) and v.get("#text"):
                    out["montant_estime_eur"] = int(float(v["#text"]))
                    break
                if isinstance(v, (int, float, str)) and str(v).replace(".", "").isdigit():
                    out["montant_estime_eur"] = int(float(v))
                    break
            except Exception:
                continue
    return out


def _normalize_rfp(raw: dict) -> dict:
    """Flatten one BOAMP record into a tidy dict used downstream."""
    parsed = _parse_donnees(raw.get("donnees"))
    # SIRET prefers the official identite field; fall back to a regex grab from
    # the raw string if needed.
    siret = parsed.get("siret")
    if not siret and isinstance(raw.get("gestion"), str):
        m = re.search(r"\"CODE_IDENT_NATIONAL\":\s*\"(\d{14})\"", raw["gestion"])
        if m:
            siret = m.group(1)
    deadline_iso = raw.get("datelimitereponse") or ""
    return {
        "idweb": raw.get("idweb"),
        "objet": raw.get("objet"),
        "titre_marche": parsed.get("titre_marche"),
        "objet_complet": parsed.get("objet_complet"),
        "organisme": raw.get("nomacheteur") or parsed.get("organisme_full"),
        "siret": siret,
        "siren": (siret or "")[:9] if siret and len(siret) >= 9 else None,
        "contact_name": parsed.get("contact_name"),
        "contact_email": parsed.get("contact_email"),
        "contact_phone": parsed.get("contact_phone"),
        "address": parsed.get("organisme_address"),
        "cp": parsed.get("organisme_cp"),
        "ville": parsed.get("organisme_ville"),
        "code_departement": (raw.get("code_departement") or [None])[0],
        "cpv_principal": parsed.get("cpv_principal"),
        "descripteur_libelle": raw.get("descripteur_libelle") or [],
        "type_marche": (raw.get("type_marche") or [None])[0],
        "procedure_libelle": raw.get("procedure_libelle"),
        "nature_libelle": raw.get("nature_libelle"),
        "dateparution": raw.get("dateparution"),
        "deadline": deadline_iso,
        "deadline_date": deadline_iso[:10] if deadline_iso else None,
        "montant_estime_eur": parsed.get("montant_estime_eur"),
        "url_avis": raw.get("url_avis"),
        "source": "boamp",
    }


def search_boamp(
    keywords: Optional[list[str]] = None,
    *,
    cpv_codes: Optional[list[str]] = None,
    cpv_preset: Optional[str] = None,
    regions: Optional[list[str]] = None,
    departments: Optional[list[str]] = None,
    days: int = 90,
    only_active: bool = True,
    limit: int = 100,
    montant_min: Optional[int] = None,
) -> list[dict]:
    """Search BOAMP for active RFPs matching the criteria.

    Parameters:
        keywords: free-text terms searched in `objet`. OR-combined.
        cpv_codes: explicit 8-digit CPV codes. OR-combined.
        cpv_preset: shortcut to one of CPV_PRESETS keys (e.g. 'formation_ia').
        regions: French region names (e.g. ['Occitanie']) auto-expanded to depts.
        departments: explicit dept codes (overrides regions).
        days: lookback window from publication date.
        only_active: drop RFPs whose deadline already passed.
        limit: max results (BOAMP per-page cap ~100; we'd paginate if needed).
        montant_min: post-filter on estimated amount (€). RFPs with no amount
            are KEPT (could be missing field, not low value).

    Returns:
        List of normalized RFP dicts. Empty list = no match (or API error).
    """
    # Build final cpv list from preset + explicit
    cpv = list(cpv_codes or [])
    if cpv_preset and cpv_preset in CPV_PRESETS:
        cpv.extend(CPV_PRESETS[cpv_preset])
    cpv = list(dict.fromkeys(cpv)) or None  # dedupe, keep order
    # Build dept list from regions + explicit
    depts = list(departments or [])
    if regions:
        depts.extend(regions_to_depts(regions))
    depts = list(dict.fromkeys(depts)) or None

    where = _build_where_clause(keywords, cpv, depts, days, only_active)
    params = {
        "limit": min(int(limit), 100),
        "order_by": "dateparution desc",
    }
    if where:
        params["where"] = where

    _THROTTLE.acquire()
    try:
        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.get(
                f"{API_BASE}/catalog/datasets/{DATASET}/records",
                params=params,
            )
            if r.status_code != 200:
                return []
            try:
                from quotas import mark_used
                mark_used("boamp")
            except Exception:
                pass
            data = r.json()
            results = data.get("results") or []
    except Exception:
        return []

    out = [_normalize_rfp(rec) for rec in results]

    if montant_min is not None:
        out = [
            r for r in out
            if r.get("montant_estime_eur") is None
            or r["montant_estime_eur"] >= montant_min
        ]
    return out


# ---------------------------------------------------------------------------
# Public helper: build a 1-liner the cold email generator can inject
# ---------------------------------------------------------------------------

def rfp_pitch_hint(rfp: dict, *, max_chars: int = 220) -> Optional[str]:
    """Return a short FR string the cold email can reference verbatim.

    The hint is a FACT (never invented): the RFP exists, here's its title and
    deadline. Claude is instructed to use this discreetly (« vu votre récent
    appel à projet sur X »), not to copy-paste it.
    """
    if not rfp:
        return None
    title = (rfp.get("titre_marche") or rfp.get("objet") or "").strip()
    if not title:
        return None
    deadline = rfp.get("deadline_date") or ""
    organisme = rfp.get("organisme") or ""
    montant = rfp.get("montant_estime_eur")
    bits = [f"AO actif « {title[:120]} »"]
    if organisme:
        bits.append(f"publié par {organisme}")
    if deadline:
        bits.append(f"deadline {deadline}")
    if montant:
        bits.append(f"~{montant:,}€".replace(",", " "))
    hint = " — ".join(bits)
    return hint[:max_chars]


# ---------------------------------------------------------------------------
# CLI for manual testing
# ---------------------------------------------------------------------------

def _cli() -> None:
    import argparse
    p = argparse.ArgumentParser(description="BOAMP appels d'offres search")
    p.add_argument("--keywords", nargs="*", help='Mots-clés ("formation IA" …)')
    p.add_argument("--cpv-preset", choices=list(CPV_PRESETS.keys()))
    p.add_argument("--cpv", nargs="*", help="Codes CPV explicites (8 chiffres)")
    p.add_argument("--regions", nargs="*", help="Noms de régions FR (Occitanie …)")
    p.add_argument("--depts", nargs="*", help="Codes départements (31, 75 …)")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--montant-min", type=int, default=None)
    p.add_argument("--all", action="store_true",
                   help="Include expired RFPs (default: active only)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    rfps = search_boamp(
        keywords=args.keywords,
        cpv_preset=args.cpv_preset,
        cpv_codes=args.cpv,
        regions=args.regions,
        departments=args.depts,
        days=args.days,
        only_active=not args.all,
        limit=args.limit,
        montant_min=args.montant_min,
    )

    if args.json:
        print(json.dumps(rfps, indent=2, ensure_ascii=False))
        return

    if not rfps:
        print("Aucun AO trouvé pour ces critères.")
        return

    print(f"=== {len(rfps)} AO trouvés ===\n")
    for r in rfps:
        print(f"📋 {r['idweb']}  ·  {r['nature_libelle'] or '?'}")
        print(f"   {r['objet'][:120] if r['objet'] else '(sans titre)'}")
        print(f"   🏢 {r['organisme']}  ({r['ville'] or '?'} — dept {r['code_departement']})")
        print(f"   💼 SIRET={r['siret'] or '—'}  ·  CPV={r['cpv_principal'] or '—'}")
        if r["contact_name"] or r["contact_email"]:
            print(f"   👤 {r['contact_name'] or '?'}  ✉ {r['contact_email'] or '—'}  ☎ {r['contact_phone'] or '—'}")
        montant = f"{r['montant_estime_eur']:,}€".replace(",", " ") if r['montant_estime_eur'] else "non précisé"
        print(f"   💰 montant: {montant}  ·  deadline: {r['deadline_date'] or '—'}")
        print(f"   🔗 {r['url_avis']}")
        print()


if __name__ == "__main__":
    _cli()
