"""
Pappers API client — direct website / email / phone / dirigeants for FR companies.

Pappers wraps the official French business register (Greffe + INSEE). It gives
us the ONE field Sirene doesn't: the company's official website. Plus a more
complete dirigeants list including birth dates, addresses, etc.

Two endpoints we use:
- `/v2/entreprise?siren=XXX` — company detail (website, email, phone, dirigeants)
- `/v2/recherche?q=XXX` — text search (fallback when SIREN unknown)

Free tier: 100 requests/day. Plenty for testing.
Set env var `PAPPERS_API_KEY`. If missing, every function returns None and
the pipeline silently falls back to DDG/scraping.

API docs: https://www.pappers.fr/api/documentation
"""
from __future__ import annotations

import os
from typing import Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field

API_BASE = "https://api.pappers.fr/v2"
DEFAULT_TIMEOUT = 15.0


# ---------------------------------------------------------------------------
# Models — kept loose so we never break on API field renames
# ---------------------------------------------------------------------------

class PappersDirigeant(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    nom: Optional[str] = None
    prenom: Optional[str] = None
    qualite: Optional[str] = None
    date_de_naissance: Optional[str] = None
    nationalite: Optional[str] = None


class PappersCompany(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    siren: Optional[str] = None
    nom_entreprise: Optional[str] = None
    site_web: Optional[str] = None
    email: Optional[str] = None
    telephone: Optional[str] = None
    capital: Optional[float] = None
    effectif: Optional[str] = None
    code_naf: Optional[str] = None
    libelle_code_naf: Optional[str] = None
    representants: list[PappersDirigeant] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

def have_pappers_key() -> bool:
    return bool(os.environ.get("PAPPERS_API_KEY"))


class PappersClient:
    """Thin sync client for the bits we need."""

    def __init__(self, api_key: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.api_key = api_key or os.environ.get("PAPPERS_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "PAPPERS_API_KEY is not set. "
                "Get one for free at https://www.pappers.fr/api"
            )
        self._client = httpx.Client(
            base_url=API_BASE,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )

    def get_by_siren(self, siren: str) -> Optional[PappersCompany]:
        """Fetch detailed info for one SIREN. None on any error / not-found."""
        if not siren:
            return None
        try:
            resp = self._client.get(
                "/entreprise",
                params={"siren": siren, "api_token": self.api_key},
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return PappersCompany.model_validate(resp.json())
        except (httpx.HTTPError, ValueError):
            return None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PappersClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Convenience helper — safe to call without checking the key first
# ---------------------------------------------------------------------------

def enrich_with_pappers(siren: Optional[str]) -> Optional[PappersCompany]:
    """Best-effort enrichment. Returns None if no key, no SIREN, or any error."""
    if not siren or not have_pappers_key():
        return None
    try:
        with PappersClient() as c:
            return c.get_by_siren(siren)
    except Exception:
        return None


def _cli() -> None:
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Test Pappers API.")
    parser.add_argument("siren", help="SIREN to look up")
    args = parser.parse_args()
    result = enrich_with_pappers(args.siren)
    if result is None:
        print("(no result or no API key)")
        return
    print(json.dumps(result.model_dump(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
