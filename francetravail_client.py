"""
France Travail (ex-Pôle Emploi) API — hiring signals per SIREN.

FREE official API. Used to detect "boîtes qui recrutent" = boîtes en
croissance = budget formation/IA dispo. THE most underused signal in
French B2B prospection.

SETUP (10 min):
  1. Sign up at https://francetravail.io/data (free, no card)
  2. Create an app → request access to "Offres d'emploi v2"
  3. Note your client_id + client_secret
  4. Add to .env:
       FRANCETRAVAIL_CLIENT_ID=PAR_yourapp_xxxxx
       FRANCETRAVAIL_CLIENT_SECRET=xxxxx

Once activated, every campaign automatically queries each candidate's
hiring activity from the last 30 days.

Endpoint used:
  GET /partenaire/offresdemploi/v2/offres/search?entreprise.siret=...

Returns per company:
  - n_offres_30d: how many active openings in last 30 days
  - n_offres_total: cumulative count across the search window
  - hiring_intensity: "high" / "medium" / "low" / "none"
  - top_jobs: most frequent ROME codes (job categories)
  - signal_for_icp: "growing-team" / "stable" / "shrinking" / None

This signal is gold for selling AI-training to companies that are scaling
their team (= they need to onboard new people fast).
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from http_safe import Throttle

AUTH_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token"
API_BASE = "https://api.francetravail.io/partenaire/offresdemploi/v2"
TIMEOUT = 15.0
_THROTTLE = Throttle(min_interval_s=0.3)

# Token cache (process-local). Tokens last 25 min per docs.
_TOKEN: Optional[str] = None
_TOKEN_EXP: float = 0.0


def have_francetravail_keys() -> bool:
    return bool(
        os.environ.get("FRANCETRAVAIL_CLIENT_ID")
        and os.environ.get("FRANCETRAVAIL_CLIENT_SECRET")
    )


def _get_token() -> Optional[str]:
    """OAuth2 client_credentials flow. Token cached for 24min."""
    global _TOKEN, _TOKEN_EXP
    if _TOKEN and time.monotonic() < _TOKEN_EXP:
        return _TOKEN
    if not have_francetravail_keys():
        return None
    cid = os.environ["FRANCETRAVAIL_CLIENT_ID"]
    secret = os.environ["FRANCETRAVAIL_CLIENT_SECRET"]
    try:
        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.post(
                AUTH_URL,
                params={"realm": "/partenaire"},
                data={
                    "grant_type": "client_credentials",
                    "client_id": cid,
                    "client_secret": secret,
                    "scope": f"application_{cid} api_offresdemploiv2 o2dsoffre",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if r.status_code != 200:
                return None
            data = r.json()
            _TOKEN = data.get("access_token")
            _TOKEN_EXP = time.monotonic() + (data.get("expires_in", 1500) - 60)
            return _TOKEN
    except Exception:
        return None


def fetch_offres_by_siret(siret: str, *, since_days: int = 30) -> Optional[dict]:
    """Return active job offers for a given SIRET + the REAL total count.

    The API filters by ESTABLISHMENT (SIRET), not just company (SIREN), so
    we can target the local outlet of a chain. Pass the SIRET from
    Sirene's matching_etablissement.

    Returns:
        {"resultats": [...], "total_real": int}  on success
        None on API error / no key

    `total_real` comes from the Content-Range header and reflects the actual
    matching count (not the capped page size). Used to detect "saturated"
    HQ-aggregation cases (v0.15.1 fix).
    """
    if not siret or len(siret) != 14 or not siret.isdigit():
        return None
    token = _get_token()
    if not token:
        return None
    _THROTTLE.acquire()
    # France Travail exige minCreationDate ET maxCreationDate ensemble
    # (sinon HTTP 400 codeErreur 1779370566030). On encadre la fenêtre
    # [now - since_days, now].
    now = datetime.now(timezone.utc)
    since_dt = now - timedelta(days=since_days)
    min_date = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    max_date = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.get(
                f"{API_BASE}/offres/search",
                params={
                    "entreprise.siret": siret,
                    "minCreationDate": min_date,
                    "maxCreationDate": max_date,
                    # We ask for a higher range to discriminate truly large vs
                    # 50-saturated. The Content-Range header gives the real
                    # total regardless of what we return here.
                    "range": "0-149",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code == 204:   # no offers
                return {"resultats": [], "total_real": 0}
            # France Travail returns 206 Partial Content for paginated lists
            # (Content-Range header), even when ALL results fit. So accept
            # both 200 and 206 as success. Anything else is a real error.
            if r.status_code not in (200, 206):
                return None
            try:
                from quotas import mark_used
                mark_used("francetravail")
            except Exception:
                pass
            resultats = r.json().get("resultats") or []
            # Parse Content-Range header: "offres 0-49/152" → total = 152
            total_real = len(resultats)
            cr = r.headers.get("Content-Range") or r.headers.get("content-range")
            if cr and "/" in cr:
                try:
                    total_real = int(cr.split("/")[-1])
                except Exception:
                    pass
            return {"resultats": resultats, "total_real": total_real}
    except Exception:
        return None


def hiring_signal_for_siret(siret: str, *, since_days: int = 30, naf: Optional[str] = None) -> dict:
    """v0.16.0 — DÉSACTIVÉ. L'API France Travail v2 n'accepte AUCUN filtre par
    SIRET ou SIREN (param `entreprise.siret` est ignoré, retourne toute la base
    nationale ~424k offres). Tous les leads recevaient donc faussement
    n_offres=150 + intensity=saturated.

    Vérifié live 2026-05-26 : 3 SIRETs différents (Carrefour, Le Procope, Ritz)
    → résultats strictement identiques. Le filtre `entreprise.siret` est mort.

    Pour v0.17 : envisager le sourcing INVERSE (récupérer les SIRENs des
    employeurs qui ont publié récemment dans un secteur+région) — c'est un
    autre paradigme (sourcing vs enrichment).

    Pour l'instant : retourne toujours intensity='unavailable' avec
    icp_modifier=0 pour ne plus polluer le scoring.
    """
    return {
        "siret": siret,
        "n_offres": 0,
        "n_offres_total": 0,
        "hiring_intensity": "unavailable",
        "is_saturated": False,
        "top_rome_codes": [],
        "top_titles": [],
        "icp_modifier": 0,
        "since_days": since_days,
        "reason": "FT API v2 ne supporte pas le filtre par SIRET (disabled v0.16.0)",
    }


def _legacy_hiring_signal_disabled(siret: str, *, since_days: int = 30, naf: Optional[str] = None) -> dict:
    """Classify hiring activity for a SIRET into a single signal dict.

    v0.15.1 — detects "HQ aggregation": when the API returns a very large
    n_offres (>= 100) for a single SIRET, it usually means the API is
    aggregating all the group's subsidiaries' jobs (we saw this on banks
    where every Caisse d'Épargne SIREN returned 50+ Optic-2000 / boucherie
    offers from unrelated subsidiaries). In that case we DOWNGRADE the
    icp_modifier to avoid false "hyper-croissance" boosts on legacy ETIs.

    Returns:
        {
            "siret": str,
            "n_offres": int,                     # number returned in this page
            "n_offres_total": int,                # real total from Content-Range
            "hiring_intensity": "high" | "medium" | "low" | "none" | "unknown",
            "is_saturated": bool,                # likely HQ-aggregation artifact
            "top_rome_codes": [str, ...],         # e.g. ["M1502"]
            "top_titles": [str, ...],             # short job titles
            "icp_modifier": int,                  # +20 high, +10 medium, 0 low, -5 none
            "since_days": int,
            "reason": str,                        # human readable
        }
    """
    out = {
        "siret": siret,
        "n_offres": 0,
        "n_offres_total": 0,
        "hiring_intensity": "unknown",
        "is_saturated": False,
        "top_rome_codes": [],
        "top_titles": [],
        "icp_modifier": 0,
        "since_days": since_days,
        "reason": "API not configured or no SIRET",
    }
    if not siret:
        return out
    raw = fetch_offres_by_siret(siret, since_days=since_days)
    if raw is None:
        return out
    offres = raw.get("resultats") or []
    n_real = raw.get("total_real") or len(offres)
    n_page = len(offres)
    out["n_offres"] = n_page
    out["n_offres_total"] = n_real
    # Aggregate the top ROME categories + job titles
    from collections import Counter
    romes = Counter()
    titles = []
    for o in offres:
        rc = o.get("romeCode")
        if rc:
            romes[rc] += 1
        intitule = o.get("intitule")
        if intitule and len(titles) < 5:
            titles.append(intitule)
    out["top_rome_codes"] = [rc for rc, _ in romes.most_common(3)]
    out["top_titles"] = titles

    # v0.15.1 — HQ saturation detection
    # Heuristic: more than 100 jobs on a single SIRET in 30 days is
    # vanishingly rare for a real local establishment. It's almost always
    # the API treating the SIREN HQ as the aggregator for ALL the group's
    # subsidiaries. In that case the "intensity" signal is noise, not gold.
    is_saturated = n_real >= 100
    # Also detect: heterogeneous top_titles (banque + boucherie + maçon) —
    # if 3+ distinct ROME categories AND >30 offers, almost certainly mixed
    # subsidiary pool.
    if n_real >= 30 and len(out["top_rome_codes"]) >= 3:
        is_saturated = True
    # v0.15.2 — durcir pour les groupes massifs d'énergie / transport /
    # logistique : ces secteurs ont des SIREN tête-de-réseau qui agrègent
    # toutes leurs filiales (cas réel Q ENERGY FRANCE : 50 offres avec
    # ROME = "Chef de partie" / "Préparateur livreur" → manifestement
    # une autre filiale du groupe). Seuil abaissé à 2 ROME distincts.
    naf_prefix = (naf or "")[:3]
    if (n_real >= 50 and len(out["top_rome_codes"]) >= 2
            and naf_prefix in ("35.", "49.", "52.", "30.")):
        # 35.=énergie · 49.=transport · 52.=logistique · 30.=fab transport
        is_saturated = True
    out["is_saturated"] = is_saturated

    # Intensity buckets — use REAL total (Content-Range), not the page size.
    if is_saturated:
        # We DON'T trust the count → label "unknown-aggregated" + 0 modifier
        out["hiring_intensity"] = "saturated"
        out["icp_modifier"] = 0
        out["reason"] = (
            f"{n_real} offres remontées — probablement agrégation HQ "
            f"(toutes filiales du groupe). Signal non discriminant pour ce SIRET."
        )
    elif n_real >= 10:
        out["hiring_intensity"] = "high"
        out["icp_modifier"] = 20
        out["reason"] = (
            f"{n_real} offres en {since_days}j — hyper-croissance (équipe en "
            f"expansion forte)"
        )
    elif n_real >= 4:
        out["hiring_intensity"] = "medium"
        out["icp_modifier"] = 10
        out["reason"] = f"{n_real} offres en {since_days}j — croissance soutenue"
    elif n_real >= 1:
        out["hiring_intensity"] = "low"
        out["icp_modifier"] = 5
        out["reason"] = f"{n_real} offre(s) — recrutement modéré"
    else:
        out["hiring_intensity"] = "none"
        out["icp_modifier"] = -5
        out["reason"] = "0 offre — peut-être en gel d'embauche"
    return out


def _cli() -> None:
    import argparse
    import json
    p = argparse.ArgumentParser(description="France Travail hiring signal")
    p.add_argument("siret", help="14-digit SIRET (établissement)")
    p.add_argument("--since-days", type=int, default=30)
    args = p.parse_args()
    if not have_francetravail_keys():
        print("FRANCETRAVAIL_CLIENT_ID / FRANCETRAVAIL_CLIENT_SECRET not set.")
        print("Sign up free at https://francetravail.io/data")
        return
    sig = hiring_signal_for_siret(args.siret, since_days=args.since_days)
    print(json.dumps(sig, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
