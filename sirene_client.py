"""
Sirene client — search French companies via recherche-entreprises.api.gouv.fr

This is the FREE official wrapper around INSEE's Sirene database, maintained
by data.gouv.fr. No authentication required, no rate limit for reasonable use.

API docs: https://recherche-entreprises.api.gouv.fr/

This module exposes:
- Pydantic models (Company, Dirigeant, Siege, SearchResponse)
- SireneClient: a thin synchronous client
- A CLI entry point for manual testing

Usage as a library:
    from scripts.sirene_client import SireneClient
    with SireneClient() as c:
        resp = c.search("cabinet dentaire", code_postal="69001", per_page=10)
        for company in resp.results:
            print(company.name, company.dirigeants)

Usage from CLI:
    python scripts/sirene_client.py "cabinet dentaire" --code-postal 69001 --limit 10
"""
from __future__ import annotations

from typing import Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field
from rich.console import Console
from rich.table import Table

API_BASE = "https://recherche-entreprises.api.gouv.fr"
DEFAULT_TIMEOUT = 30.0
USER_AGENT = "prospect-agent/0.1 (+https://github.com/julesggh110-cmd/prospect-agent)"

console = Console()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class Dirigeant(BaseModel):
    """A legal director (gérant, président, etc.) registered at INSEE Sirene.

    Note: this is the LEGAL director, not necessarily the operational
    decision-maker. For SaaS or larger companies, the operational decider
    (VP Sales, Head of X, etc.) must be found via additional sources.
    """
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    nom: Optional[str] = None
    prenoms: Optional[str] = None
    qualite: Optional[str] = None
    type_dirigeant: Optional[str] = None
    annee_de_naissance: Optional[str] = None
    nationalite: Optional[str] = None
    # When the dirigeant is itself a company (PM)
    siren: Optional[str] = None
    denomination: Optional[str] = None

    @property
    def full_name(self) -> Optional[str]:
        if self.nom and self.prenoms:
            return f"{self.prenoms} {self.nom}".strip()
        return self.nom or self.denomination


class Siege(BaseModel):
    """Headquarters address."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    siret: Optional[str] = None
    adresse: Optional[str] = None
    code_postal: Optional[str] = None
    libelle_commune: Optional[str] = None
    departement: Optional[str] = None
    region: Optional[str] = None
    latitude: Optional[str] = None
    longitude: Optional[str] = None


class Company(BaseModel):
    """A company result from Sirene."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    siren: str
    nom_complet: Optional[str] = None
    nom_raison_sociale: Optional[str] = None
    nature_juridique: Optional[str] = None
    activite_principale: Optional[str] = None
    section_activite_principale: Optional[str] = None
    tranche_effectif_salarie: Optional[str] = None
    date_creation: Optional[str] = None
    etat_administratif: Optional[str] = None
    siege: Optional[Siege] = None
    dirigeants: list[Dirigeant] = Field(default_factory=list)

    @property
    def name(self) -> str:
        return self.nom_complet or self.nom_raison_sociale or f"SIREN {self.siren}"

    @property
    def city(self) -> Optional[str]:
        return self.siege.libelle_commune if self.siege else None

    @property
    def address_short(self) -> str:
        if not self.siege:
            return ""
        parts = [self.siege.adresse, self.siege.code_postal, self.siege.libelle_commune]
        return ", ".join(p for p in parts if p)


class SearchResponse(BaseModel):
    """A paginated Sirene search response."""
    model_config = ConfigDict(extra="allow")

    results: list[Company]
    total_results: int
    page: int
    per_page: int
    total_pages: int


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class SireneClient:
    """Thin synchronous client for recherche-entreprises.api.gouv.fr.

    Use as a context manager to ensure the underlying HTTP client closes:
        with SireneClient() as c:
            resp = c.search("...")
    """

    def __init__(self, base_url: str = API_BASE, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )

    def search(
        self,
        query: Optional[str] = None,
        *,
        naf: Optional[str] = None,
        code_postal: Optional[str] = None,
        departement: Optional[str] = None,
        region: Optional[str] = None,
        tranche_effectif: Optional[str] = None,
        page: int = 1,
        per_page: int = 25,
    ) -> SearchResponse:
        """Search active French companies. Returns the first page by default.

        At least one filter must be provided. Iterate pages with `page=` if
        you need more than `per_page` results.
        """
        params: dict[str, str | int] = {"page": page, "per_page": per_page}
        if query:
            params["q"] = query
        if naf:
            params["activite_principale"] = naf
        if code_postal:
            params["code_postal"] = code_postal
        if departement:
            params["departement"] = departement
        if region:
            params["region"] = region
        if tranche_effectif:
            params["tranche_effectif_salarie"] = tranche_effectif

        resp = self._client.get("/search", params=params)
        resp.raise_for_status()
        return SearchResponse.model_validate(resp.json())

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SireneClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# CLI entry point (for manual testing)
# ---------------------------------------------------------------------------

def _print_results(resp: SearchResponse) -> None:
    table = Table(
        title=f"Sirene — {resp.total_results} résultats "
              f"(page {resp.page}/{resp.total_pages})"
    )
    table.add_column("SIREN", style="dim")
    table.add_column("Nom", style="bold", max_width=40)
    table.add_column("NAF", style="cyan")
    table.add_column("Ville")
    table.add_column("Effectif")
    table.add_column("Dirigeant(s)", max_width=40)

    for c in resp.results:
        dirigeants = "\n".join(
            (
                f"{d.full_name} ({d.qualite})"
                if d.qualite and d.full_name
                else (d.full_name or "—")
            )
            for d in c.dirigeants[:3]
        )
        if len(c.dirigeants) > 3:
            dirigeants += f"\n+{len(c.dirigeants) - 3} autres"

        table.add_row(
            c.siren,
            c.name,
            c.activite_principale or "—",
            c.city or "—",
            c.tranche_effectif_salarie or "—",
            dirigeants or "—",
        )
    console.print(table)


def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Search French companies via the public Sirene API."
    )
    parser.add_argument("query", nargs="?", help="Free-text query")
    parser.add_argument("--naf", help="NAF code filter (e.g., 86.23Z)")
    parser.add_argument("--code-postal", help="Postal code (e.g., 69001)")
    parser.add_argument("--departement", help="Département code (e.g., 69)")
    parser.add_argument("--region", help="Région code")
    parser.add_argument("--limit", type=int, default=10, help="Results per page (max 25)")
    args = parser.parse_args()

    if not any([args.query, args.naf, args.code_postal, args.departement, args.region]):
        parser.error("provide at least one filter (query / --naf / --code-postal / ...)")

    with SireneClient() as client:
        resp = client.search(
            query=args.query,
            naf=args.naf,
            code_postal=args.code_postal,
            departement=args.departement,
            region=args.region,
            per_page=min(args.limit, 25),
        )
        _print_results(resp)


if __name__ == "__main__":
    _cli()
