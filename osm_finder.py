"""
OpenStreetMap finder — free, no auth, no anti-bot.

Pages Jaunes is now behind Cloudflare's full JS challenge. OSM is a great
fallback for FR SMBs because shopkeepers, restos, bars regularly add
`phone`, `website`, `email`, `contact:instagram`, `contact:facebook` tags
to their establishment node.

Coverage in France is high for the visible-storefront sectors that matter
for Bear Brothers: restaurants, bars, cafés, hôtels, cavistes, salons,
boulangeries, etc.

Approach:
1. Geocode the city (Nominatim) to get a bbox/lat-lon.
2. Run Overpass QL query for matching `name~"..."` within ~10km.
3. Parse the returned JSON for phone/website/email/instagram tags.

Throttling:
- Nominatim: 1 req/sec hard rule (we use 1.1s).
- Overpass: ~10 req/min recommended (we use 6s between calls).

Public API:
    find_business_on_osm(name, city) -> Optional[dict]
"""
from __future__ import annotations

import re
import time
from typing import Optional
from urllib.parse import quote_plus

import httpx

USER_AGENT = "ProspectAgent/0.9 (contact: prospect-agent@example.com)"
TIMEOUT = 25.0

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Thread-safe throttles (the previous `global _LAST_*` was racy under the
# parallel enricher and risked tripping Nominatim's 1-req/sec rule).
from http_safe import Throttle  # noqa: E402

_NOMINATIM_THROTTLE = Throttle(min_interval_s=1.1)  # Nominatim hard 1/sec rule
_OVERPASS_THROTTLE = Throttle(min_interval_s=6.0)   # Overpass ~10/min recommended

# Small in-process geocoding cache
_GEO_CACHE: dict[str, tuple[float, float]] = {}


def _throttle_nominatim() -> None:
    _NOMINATIM_THROTTLE.acquire()


def _throttle_overpass() -> None:
    _OVERPASS_THROTTLE.acquire()


def _geocode_city(city: str) -> Optional[tuple[float, float]]:
    """Return (lat, lon) for a French city. Cached. None on failure."""
    key = city.strip().lower()
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]
    _throttle_nominatim()
    try:
        with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}) as c:
            r = c.get(
                _NOMINATIM_URL,
                params={
                    "q": f"{city}, France",
                    "format": "json",
                    "limit": 1,
                    "countrycodes": "fr",
                },
            )
            if r.status_code != 200:
                return None
            data = r.json()
            if not data:
                return None
            lat = float(data[0]["lat"])
            lon = float(data[0]["lon"])
            _GEO_CACHE[key] = (lat, lon)
            return (lat, lon)
    except Exception:
        return None


def _clean_phone(raw: str) -> Optional[str]:
    """Validate FR phone string. Returns digits-only-ish form, or None."""
    if not raw:
        return None
    cleaned = re.sub(r"[^\d+]", "", raw)
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) < 9 or len(digits) > 15:
        return None
    return cleaned


def _normalize_website(raw: str) -> Optional[str]:
    if not raw:
        return None
    raw = raw.strip()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    # Strip trailing slash
    return raw.rstrip("/")


def _build_overpass_query(name: str, lat: float, lon: float, radius_m: int = 12000) -> str:
    """Search nodes/ways/relations whose `name` matches case-insensitively
    within a radius of the geocoded city."""
    # Escape any double-quote inside name for the regex
    safe_name = name.replace('"', '\\"')
    return f"""
[out:json][timeout:25];
(
  nwr["name"~"{safe_name}",i](around:{radius_m},{lat},{lon});
);
out tags 30;
""".strip()


def find_business_on_osm(name: str, city: Optional[str] = None) -> Optional[dict]:
    """Look up a business in OpenStreetMap by name + city.

    Returns dict with keys: phone, website, email, instagram, facebook,
    address, osm_name, osm_type, osm_id, source='osm'. None on no match.
    """
    if not name:
        return None
    target_city = city or "Paris"
    geo = _geocode_city(target_city)
    if geo is None:
        return None
    lat, lon = geo

    q = _build_overpass_query(name, lat, lon)
    _throttle_overpass()
    try:
        with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}) as c:
            r = c.post(_OVERPASS_URL, data={"data": q})
            if r.status_code != 200:
                return None
            data = r.json()
    except Exception:
        return None

    elements = data.get("elements") or []
    if not elements:
        return None

    # Score: prefer elements whose name contains all of the query tokens
    name_tokens = [t.lower() for t in re.split(r"\W+", name) if len(t) > 2]

    def score(elem: dict) -> int:
        tags = elem.get("tags") or {}
        ename = (tags.get("name") or "").lower()
        s = 0
        for tok in name_tokens:
            if tok in ename:
                s += 5
        # Prefer elements with rich contact info
        if tags.get("phone") or tags.get("contact:phone"):
            s += 3
        if tags.get("website") or tags.get("contact:website"):
            s += 3
        if tags.get("email") or tags.get("contact:email"):
            s += 1
        return s

    elements.sort(key=score, reverse=True)
    best = elements[0]
    tags = best.get("tags") or {}

    phone = _clean_phone(
        tags.get("phone") or tags.get("contact:phone") or tags.get("contact:mobile") or ""
    )
    website = _normalize_website(
        tags.get("website") or tags.get("contact:website") or tags.get("url") or ""
    )
    email = (tags.get("email") or tags.get("contact:email") or "").strip().lower() or None
    instagram = (tags.get("contact:instagram") or tags.get("instagram") or "").strip() or None
    facebook = (tags.get("contact:facebook") or tags.get("facebook") or "").strip() or None

    # Address — assemble what we have
    parts = []
    for k in ("addr:housenumber", "addr:street", "addr:postcode", "addr:city"):
        v = tags.get(k)
        if v:
            parts.append(v)
    address = " ".join(parts) if parts else None

    # Drop if no contact channels found
    if not any([phone, website, email, instagram, facebook]):
        return None

    return {
        "phone": phone,
        "website": website,
        "email": email,
        "instagram": instagram,
        "facebook": facebook,
        "address": address,
        "osm_name": tags.get("name"),
        "osm_type": best.get("type"),
        "osm_id": best.get("id"),
        "source": "osm",
    }


# ---------------------------------------------------------------------------
# CLI for manual test
# ---------------------------------------------------------------------------

def _cli() -> None:
    import argparse
    import json
    import warnings
    warnings.filterwarnings("ignore")

    p = argparse.ArgumentParser(description="Look up a business on OpenStreetMap.")
    p.add_argument("name", help="Business name (quoted)")
    p.add_argument("--city", help="City to constrain the search", default="Paris")
    args = p.parse_args()
    result = find_business_on_osm(args.name, args.city)
    if not result:
        print("(not found)")
        return
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
