"""
HERE Maps Geocoding & Search — 250k free transactions/month, has PHONES.

THE big free win for FR SMB CHR phone coverage. Where Serper /places returns
no phone in its basic response, HERE's /discover endpoint returns:
    - phone numbers (the real listed line on Google/HERE/Yelp database union)
    - opening hours
    - category (cuisine type — already get from Google Places too)
    - address + lat/lng
    - website

Free tier: 250 000 transactions/month, no credit card required. Setup is
slightly involved (OAuth API key) but only once.

Setup (10 min):
  1. https://platform.here.com → sign up free
  2. Console → Apps → Create app → "Project type: REST" → Create
  3. Note your "API Key" (NOT the OAuth bearer, just the simple key)
  4. Add to .env:
        HERE_MAPS_API_KEY=xxxxxxxx

API endpoints we use:
  /v1/discover    : free-text search for a business at coords
  /v1/browse      : browse-by-category at coords (cuisine type filter)

Per-call cost on free tier: 1 transaction. Even at 5 calls/lead × 100
leads/day, that's 500/day = 15 000/month → well under 250k free.

Public API:
    find_business_here(name, city) -> Optional[dict]
        returns {
            "name": ...,
            "phone": ...,        ← THE KEY VALUE-ADD
            "address": ...,
            "category": ...,
            "website": ...,
            "lat": ..., "lng": ...,
            "opening_hours": [...],
            "source": "here_maps",
        }
"""
from __future__ import annotations

import os
import re
import time
import unicodedata
from typing import Optional

import httpx

from http_safe import Throttle

API_BASE = "https://discover.search.hereapi.com/v1"
GEOCODER_URL = "https://geocode.search.hereapi.com/v1/geocode"
DEFAULT_TIMEOUT = 10.0
_THROTTLE = Throttle(min_interval_s=0.1)

# Cache the geocoded centre of each city to avoid spending 1 transaction per
# lead just resolving the city → (lat, lng).
_CITY_CACHE: dict[str, tuple[float, float]] = {}


def have_here_key() -> bool:
    return bool(os.environ.get("HERE_MAPS_API_KEY"))


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                   if not unicodedata.combining(c))


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _strip_accents(s).lower())


def _client(timeout: float = DEFAULT_TIMEOUT) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        headers={"Accept": "application/json"},
    )


def _geocode_city(city: str) -> Optional[tuple[float, float]]:
    """Resolve a FR city name → (lat, lng). Cached."""
    if not city:
        return None
    key = city.strip().lower()
    if key in _CITY_CACHE:
        return _CITY_CACHE[key]
    if not have_here_key():
        return None
    _THROTTLE.acquire()
    try:
        with _client() as c:
            r = c.get(GEOCODER_URL, params={
                "q": f"{city}, France",
                "in": "countryCode:FRA",
                "limit": 1,
                "apiKey": os.environ["HERE_MAPS_API_KEY"],
            })
            if r.status_code != 200:
                return None
            data = r.json()
            # Successful geocode = 1 HERE transaction
            try:
                from quotas import mark_used
                mark_used("here_maps")
            except Exception:
                pass
            items = data.get("items") or []
            if not items:
                return None
            pos = items[0].get("position") or {}
            lat, lng = pos.get("lat"), pos.get("lng")
            if lat is None or lng is None:
                return None
            _CITY_CACHE[key] = (float(lat), float(lng))
            return _CITY_CACHE[key]
    except Exception:
        return None


def _discover_business(name: str, lat: float, lng: float,
                        radius_m: int = 10000) -> list[dict]:
    """Run /v1/discover at given coords with a free-text query."""
    if not have_here_key():
        return []
    _THROTTLE.acquire()
    try:
        with _client() as c:
            r = c.get(f"{API_BASE}/discover", params={
                "q": name,
                "in": f"circle:{lat},{lng};r={radius_m}",
                "limit": 5,
                "lang": "fr-FR",
                "apiKey": os.environ["HERE_MAPS_API_KEY"],
            })
            if r.status_code != 200:
                return []
            data = r.json()
            try:
                from quotas import mark_used
                mark_used("here_maps")
            except Exception:
                pass
            return data.get("items") or []
    except Exception:
        return []


def _best_match(items: list[dict], name: str, city: Optional[str]) -> Optional[dict]:
    """Pick the HERE item that best matches (name, city)."""
    if not items:
        return None
    city_slug = _slug(city) if city else ""
    name_tokens = [_slug(t) for t in name.split() if len(t) >= 3]

    best: Optional[dict] = None
    best_score = -1
    for it in items:
        title = it.get("title") or ""
        address = (it.get("address") or {}).get("label") or ""
        title_slug = _slug(title)
        address_slug = _slug(address)
        score = 0
        if city_slug and city_slug in address_slug:
            score += 10
        for tok in name_tokens:
            if tok and tok in title_slug:
                score += 5
        # Penalise foreign-country results (shouldn't happen with country
        # filter but defensive)
        if "FRA" not in (it.get("address") or {}).get("countryCode", "FRA"):
            score -= 50
        if score > best_score:
            best_score = score
            best = it
    return best if best_score >= 5 else None


def _normalize(item: dict) -> dict:
    """Flatten the HERE item into the keys we use downstream."""
    if not item:
        return {}
    address = item.get("address") or {}
    contacts = item.get("contacts") or []
    phone = None
    website = None
    for cset in contacts:
        for ph in (cset.get("phone") or []):
            if ph.get("value"):
                phone = ph["value"]
                break
        if phone:
            break
    for cset in contacts:
        for w in (cset.get("www") or []):
            if w.get("value"):
                website = w["value"]
                break
        if website:
            break
    cats = item.get("categories") or []
    primary_cat = cats[0].get("name") if cats else None
    hours_raw = (item.get("openingHours") or [{}])[0].get("text") or []
    pos = item.get("position") or {}
    return {
        "name": item.get("title"),
        "phone": phone,
        "address": address.get("label"),
        "category": primary_cat,
        "website": website,
        "lat": pos.get("lat"),
        "lng": pos.get("lng"),
        "opening_hours": hours_raw,
        "here_id": item.get("id"),
        "source": "here_maps",
    }


def find_business_here(name: str, city: Optional[str] = None, *, naf: Optional[str] = None) -> Optional[dict]:
    """Find the HERE Maps record for (name, city). Returns flat dict or None.

    v0.16.0 — Sector-aware query retry: when the legal name doesn't match
    (Sirene's "LEMON FORMATIONS"), retry with sector-prefixed clean_name
    ("hôtel lemon" matches "Lemon Hôtel Saint-Lazare").
    """
    if not name or not have_here_key():
        return None
    geo = _geocode_city(city) if city else None
    if not geo:
        return None
    # First attempt: standard name
    items = _discover_business(name, geo[0], geo[1])
    best = _best_match(items, name, city)
    if best:
        return _normalize(best)
    # v0.16.0 — Retry with cleaned name + sector prefix for CHR/retail
    try:
        from google_places import _strip_legal_suffix, _sector_keywords_for_naf
        clean = _strip_legal_suffix(name)
        sector_words = _sector_keywords_for_naf(naf)
        for word in sector_words:
            q = f"{word} {clean}"
            items = _discover_business(q, geo[0], geo[1])
            best = _best_match(items, clean, city)
            if best:
                return _normalize(best)
        # Try clean alone (without sector prefix)
        if clean != name:
            items = _discover_business(clean, geo[0], geo[1])
            best = _best_match(items, clean, city)
            if best:
                return _normalize(best)
    except Exception:
        pass
    return None


def _cli() -> None:
    import argparse
    import json
    import warnings
    warnings.filterwarnings("ignore")
    p = argparse.ArgumentParser(description="Look up a business via HERE Maps.")
    p.add_argument("name")
    p.add_argument("--city")
    args = p.parse_args()
    if not have_here_key():
        print("HERE_MAPS_API_KEY not set. Sign up free at https://platform.here.com")
        return
    res = find_business_here(args.name, args.city)
    print(json.dumps(res, indent=2, ensure_ascii=False) if res else "(no match)")


if __name__ == "__main__":
    _cli()
