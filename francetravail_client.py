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


def fetch_offres_by_siret(siret: str, *, since_days: int = 30) -> Optional[list[dict]]:
    """Return active job offers for a given SIRET. Empty list = no offers.

    The API filters by ESTABLISHMENT (SIRET), not just company (SIREN), so
    we can target the local outlet of a chain. Pass the SIRET from
    Sirene's matching_etablissement.

    None = API error (no key, throttled, etc.). [] = no offers in window.
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
                    "range": "0-49",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code == 204:   # no offers
                return []
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
            return r.json().get("resultats") or []
    except Exception:
        return None


def hiring_signal_for_siret(siret: str, *, since_days: int = 30) -> dict:
    """Classify hiring activity for a SIRET into a single signal dict.

    Returns:
        {
            "siret": str,
            "n_offres": int,
            "hiring_intensity": "high" | "medium" | "low" | "none" | "unknown",
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
        "hiring_intensity": "unknown",
        "top_rome_codes": [],
        "top_titles": [],
        "icp_modifier": 0,
        "since_days": since_days,
        "reason": "API not configured or no SIRET",
    }
    if not siret:
        return out
    offres = fetch_offres_by_siret(siret, since_days=since_days)
    if offres is None:
        return out
    n = len(offres)
    out["n_offres"] = n
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
    # Intensity buckets
    if n >= 10:
        out["hiring_intensity"] = "high"
        out["icp_modifier"] = 20
        out["reason"] = (
            f"{n} offres en {since_days}j — hyper-croissance (équipe en "
            f"expansion forte)"
        )
    elif n >= 4:
        out["hiring_intensity"] = "medium"
        out["icp_modifier"] = 10
        out["reason"] = f"{n} offres en {since_days}j — croissance soutenue"
    elif n >= 1:
        out["hiring_intensity"] = "low"
        out["icp_modifier"] = 5
        out["reason"] = f"{n} offre(s) — recrutement modéré"
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
