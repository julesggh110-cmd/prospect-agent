"""
Google My Business via Serper /places — the missing CHR qualification layer.

WHY this matters for FR SMB CHR prospection:

The Sirene + Pappers + scraping cascade gives us legal data (raison sociale,
SIREN, dirigeant) but is BLIND on the operational reality:
  - Is "LE HARICOT TOULOUSE" actually a vegan healthy spot? (yes → 0 fit for
    a spirits brand)
  - Is "FLAVEURS" really a restaurant? (no, it's a magazine — bad NAF tag)
  - Is "GALA" a gastro? (no, it's the INSA student canteen)

Google My Business pages have this answer in their `type` field. They also
have the REAL listed phone number, the official website (as Google sees it,
which beats our domain_guess), opening hours, and rating signals.

Serper's /places endpoint returns this data with the same API key, billing
and free tier (2 500 queries) as the regular /search endpoint. Returned per
query:
    [
      {
        "title": "Le Bibent",
        "address": "5 Place du Capitole, 31000 Toulouse",
        "phoneNumber": "05 61 23 89 03",
        "website": "https://bibent.fr/",
        "rating": 4.0,
        "ratingCount": 1234,
        "type": "Restaurant français",
        "category": "Restaurant",
        "latitude": 43.604,
        "longitude": 1.443,
      },
      ...
    ]

We use this to:
  1. Resolve a REAL company_phone (Google's listing beats anything we scrape)
  2. Resolve the OFFICIAL website (Google has done the disambiguation for us)
  3. Surface a cuisine TYPE — wired into ICP qualification downstream
     (e.g. for an alcohol-compatible CHR ICP: "bar à cocktails" 4 stars
      = strong fit, "restaurant végétarien" = drop)
  4. Validate the match is the right business via geo (city present) +
     name overlap.

Public API:
    find_business_place(name, city) -> Optional[dict]
"""
from __future__ import annotations

import os
import unicodedata
from typing import Optional

import httpx

from http_safe import Throttle


API_URL = "https://google.serper.dev/places"
DEFAULT_TIMEOUT = 10.0
_THROTTLE = Throttle(min_interval_s=0.2)


def have_serper_key() -> bool:
    return bool(os.environ.get("SERPER_API_KEY"))


def _strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))


def _slug(s: str) -> str:
    """Lowercase ASCII slug for fuzzy matching."""
    import re
    return re.sub(r"[^a-z0-9]+", "", _strip_accents(s).lower())


def _query_places(query: str, max_results: int = 5) -> list[dict]:
    """Query Serper /places. CACHED with 24h TTL (Google Business listings
    don't change daily). Cache hit = 0 quota + 0 network.
    """
    if not have_serper_key():
        return []
    # Cache lookup first
    cache_key = f"serper-places:{query}"
    try:
        from http_safe import _HTTP_CACHE
        import json as _json
        if _HTTP_CACHE is not None:
            hit = _HTTP_CACHE.get(cache_key, default=None)
            if hit is not None:
                return _json.loads(hit)[:max_results]
    except Exception:
        pass

    key = os.environ.get("SERPER_API_KEY") or ""
    _THROTTLE.acquire()
    try:
        with httpx.Client(
            timeout=DEFAULT_TIMEOUT,
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
        ) as c:
            r = c.post(
                API_URL,
                json={"q": query, "gl": "fr", "hl": "fr"},
            )
            if r.status_code == 429:
                return []
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, ValueError):
        return []
    try:
        from quotas import mark_used
        mark_used("serper")
    except Exception:
        pass
    out = data.get("places") or []
    # Save to cache for warm runs
    try:
        from http_safe import _HTTP_CACHE
        import json as _json
        if _HTTP_CACHE is not None:
            _HTTP_CACHE.set(cache_key, _json.dumps(out), expire=24 * 3600)
    except Exception:
        pass
    return out[:max_results]


# Approximate lat/lng of major FR cities — used to reject cross-city false
# positives like "L'Escargot Montorgueil" (Paris) returned for "L'Escargot Toulouse".
# Square bbox of ~50 km around each city centre.
_FR_CITY_GEO: dict[str, tuple[float, float]] = {
    "paris": (48.8566, 2.3522),
    "lyon": (45.7640, 4.8357),
    "marseille": (43.2965, 5.3698),
    "toulouse": (43.6047, 1.4442),
    "nice": (43.7102, 7.2620),
    "nantes": (47.2184, -1.5536),
    "montpellier": (43.6108, 3.8767),
    "strasbourg": (48.5734, 7.7521),
    "bordeaux": (44.8378, -0.5792),
    "lille": (50.6292, 3.0573),
    "rennes": (48.1173, -1.6778),
    "reims": (49.2583, 4.0317),
    "le havre": (49.4944, 0.1079),
    "saint-etienne": (45.4397, 4.3872),
    "toulon": (43.1242, 5.9280),
    "grenoble": (45.1885, 5.7245),
    "dijon": (47.3220, 5.0415),
    "angers": (47.4784, -0.5632),
    "nimes": (43.8367, 4.3601),
    "villeurbanne": (45.7665, 4.8795),
    "labege": (43.5476, 1.5256),
    "blagnac": (43.6358, 1.3895),
}


def _geo_distance_km(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Approximate distance — flat-earth, fine for the 50-km radius we care about."""
    import math
    lat1, lon1 = p1
    lat2, lon2 = p2
    # Equirectangular approximation
    R = 6371.0
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return R * math.sqrt(x * x + y * y)


def _best_match(
    places: list[dict],
    name: str,
    city: Optional[str] = None,
) -> Optional[dict]:
    """Pick the place that best matches (name, city).

    Scoring:
      +10 if the city slug appears in the address
      +10 if lat/lng is within 50 km of the target city centre
      +5  per name-token (>=3 chars) found in the place title
      -50 if lat/lng > 100 km from target city (HARD reject — likely a
          cross-city false positive like Paris vs Toulouse)
      -20 if foreign-country wording appears in the address

    Returns the best place with score >= 5, else None.
    """
    if not places:
        return None
    city_slug = _slug(city) if city else ""
    target_geo = _FR_CITY_GEO.get(city_slug) if city else None
    name_tokens_raw = [_slug(t) for t in name.split() if len(t) >= 3]

    best: Optional[dict] = None
    best_score = -100
    for p in places:
        title = (p.get("title") or "")
        address = (p.get("address") or "")
        title_slug = _slug(title)
        address_slug = _slug(address)
        plat = p.get("latitude")
        plon = p.get("longitude")
        score = 0

        if city_slug and city_slug in address_slug:
            score += 10
        for tok in name_tokens_raw:
            if tok and tok in title_slug:
                score += 5
        if target_geo and plat is not None and plon is not None:
            d = _geo_distance_km(target_geo, (float(plat), float(plon)))
            if d <= 50:
                score += 10
            elif d > 100:
                score -= 50          # hard reject: wrong city entirely
        if address and any(c in address.lower() for c in (
            "united states", "united kingdom", "spain", "italy", "germany",
            ", uk", ", usa", ", us", ", de", ", es"
        )):
            score -= 20
        if score > best_score:
            best_score = score
            best = p
    if best_score < 5:
        return None
    return best


def find_business_place(
    name: str,
    city: Optional[str] = None,
) -> Optional[dict]:
    """Find the Google My Business entry for (name, city).

    Returns a dict (Serper place schema) or None on no confident match.
    Caller should expect any of these keys to be missing.
    """
    if not name:
        return None
    queries = []
    if city:
        queries.append(f"{name} {city}")
    queries.append(name)

    for q in queries:
        places = _query_places(q, max_results=5)
        match = _best_match(places, name, city)
        if match:
            return match
    return None


def normalize_place(place: dict) -> dict:
    """Flatten the Serper response + compute operational signals."""
    if not place:
        return {}
    # Operational signals — used by ICP to drop dead/inactive businesses.
    # Serper /places returns a `permanently_closed` bool when Google flags it.
    perm_closed = bool(place.get("permanentlyClosed") or place.get("permanently_closed"))
    temp_closed = bool(place.get("temporarilyClosed") or place.get("temporarily_closed"))
    # "Is operating" defaults to True unless Google says otherwise. Also flag
    # businesses with zero reviews as suspicious (could be a stale listing).
    is_operating = not (perm_closed or temp_closed)
    return {
        "name": place.get("title"),
        "address": place.get("address"),
        "phone": place.get("phoneNumber"),
        "website": place.get("website"),
        "rating": place.get("rating"),
        "rating_count": place.get("ratingCount"),
        "type": place.get("type"),          # localised: "Restaurant français"
        "category": place.get("category"),  # generic: "Restaurant"
        "latitude": place.get("latitude"),
        "longitude": place.get("longitude"),
        "cid": place.get("cid"),
        "place_id": place.get("placeId"),
        # Operational signals (CHR ICPs use these)
        "opening_hours": place.get("openingHours") or place.get("hours"),
        "permanently_closed": perm_closed,
        "temporarily_closed": temp_closed,
        "is_operating": is_operating,
        "source": "google_places",
    }


def _cli() -> None:
    import argparse
    import json
    import warnings
    warnings.filterwarnings("ignore")

    p = argparse.ArgumentParser(description="Look up a business on Google Places (Serper).")
    p.add_argument("name")
    p.add_argument("--city")
    args = p.parse_args()
    if not have_serper_key():
        print("SERPER_API_KEY not set. Sign up free at https://serper.dev")
        return
    place = find_business_place(args.name, args.city)
    if not place:
        print("(no match)")
        return
    print(json.dumps(normalize_place(place), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
