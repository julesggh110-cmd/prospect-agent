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

def _etab_to_siege(etab: dict, fallback: Optional[dict] = None) -> dict:
    """Turn a matching_etablissement dict into a Siege-compatible dict."""
    fallback = fallback or {}
    return {
        "siret": etab.get("siret") or fallback.get("siret"),
        "adresse": etab.get("adresse") or fallback.get("adresse"),
        "code_postal": etab.get("code_postal") or fallback.get("code_postal"),
        "libelle_commune": etab.get("libelle_commune") or fallback.get("libelle_commune"),
        "departement": (etab.get("code_postal") or "")[:2] or fallback.get("departement"),
        "region": fallback.get("region"),
        "latitude": etab.get("latitude") or fallback.get("latitude"),
        "longitude": etab.get("longitude") or fallback.get("longitude"),
    }


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
        local_only: bool = True,
    ) -> SearchResponse:
        """Search active French companies. Returns the first page by default.

        IMPORTANT — `departement` / `code_postal` filter on ESTABLISHMENTS, not
        on the legal HQ. A chain like "B&B Hotels" whose HQ is in Brest will
        appear in a `departement=69` query because they have hotels in Lyon.
        The result rows nevertheless show the Brest HQ in `siege`.

        We use `inclure_etablissements=true` to get the matching establishments
        (the actual Lyon hotels) and, when `local_only=True` (default), we
        rewrite `siege` to point at the first matching establishment that's
        actually in the requested geo. This gives proper LOCAL prospection
        results: one Lyon hotel = one lead, even if the chain HQ is elsewhere.

        Pass `local_only=False` to keep the HQ unchanged (useful when you want
        to prospect the chain itself, not each location).
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
        if local_only and (code_postal or departement):
            params["inclure_etablissements"] = "true"

        resp = self._client.get("/search", params=params)
        resp.raise_for_status()
        data = resp.json()

        # Rewrite siege to the local establishment when local_only is True.
        # CRITICAL: preserve the original siege in _original_siege so the
        # pipeline can detect when a "local" lead is actually a subsidiary
        # of a company headquartered elsewhere (decision-maker is at HQ).
        # MULTI-DEPT support: code_postal and departement can be comma-separated
        # ("31,34,33"). We accept a matching_etablissement if its CP starts
        # with ANY of the requested codes / depts.
        if local_only and (code_postal or departement):
            cp_prefixes = [s.strip() for s in str(code_postal).split(",")] if code_postal else []
            dept_prefixes = [s.strip().zfill(2) for s in str(departement).split(",")] if departement else []
            for c in data.get("results", []):
                matches = c.get("matching_etablissements") or []
                if not matches:
                    continue
                for m in matches:
                    mcp = m.get("code_postal") or ""
                    accepted = False
                    if cp_prefixes and any(mcp.startswith(p) for p in cp_prefixes):
                        accepted = True
                    elif dept_prefixes and any(mcp.startswith(p) for p in dept_prefixes):
                        accepted = True
                    if accepted:
                        c.setdefault("_original_siege", dict(c.get("siege") or {}))
                        c["siege"] = _etab_to_siege(m, fallback=c.get("siege"))
                        c.setdefault("_local_etab", m)
                        break

        return SearchResponse.model_validate(data)

    def search_many(
        self,
        *,
        target: int,
        query: Optional[str] = None,
        naf: Optional[str] = None,
        code_postal: Optional[str] = None,
        departement: Optional[str] = None,
        region: Optional[str] = None,
        tranche_effectif: Optional[str] = None,
        local_only: bool = True,
        max_pages: int = 20,
    ) -> SearchResponse:
        """Paginated search — keeps fetching until `target` results are gathered
        OR the API runs out of pages.

        Why this exists: a single Sirene page caps at 25 results. Asking for
        `--volume 100` previously gave you 25 because `run_campaign` was calling
        `search(per_page=min(volume, 25))` and not iterating. This method does
        the iteration in one place, returning a SearchResponse whose `results`
        list holds up to `target` companies (truncated at the target).

        `max_pages` is a hard safety net — at per_page=25 that's 500 results.
        Sirene's free API tolerates this without rate-limiting in practice.
        """
        per_page = 25
        results: list = []
        merged: Optional[SearchResponse] = None
        for page in range(1, max_pages + 1):
            resp = self.search(
                query=query, naf=naf,
                code_postal=code_postal, departement=departement, region=region,
                tranche_effectif=tranche_effectif,
                page=page, per_page=per_page,
                local_only=local_only,
            )
            if merged is None:
                merged = resp
            results.extend(resp.results)
            if len(results) >= target:
                results = results[:target]
                break
            if page >= resp.total_pages or not resp.results:
                break
        if merged is None:
            return SearchResponse(results=[], total_results=0,
                                   page=1, per_page=per_page, total_pages=0)
        # Return a SearchResponse with the merged results — keep the original
        # total_results / total_pages from page 1 for visibility.
        merged.results = results
        return merged

    def iter_pages(
        self,
        *,
        query: Optional[str] = None,
        naf: Optional[str] = None,
        code_postal: Optional[str] = None,
        departement: Optional[str] = None,
        region: Optional[str] = None,
        tranche_effectif: Optional[str] = None,
        local_only: bool = True,
        max_pages: int = 20,
    ):
        """Generator: yields ONE Sirene page at a time.

        Used by run_campaign's 'perfect mode': process page 1, filter, score,
        if not enough qualified leads yet → process page 2, etc. Stops the
        moment the caller breaks out of the loop. Saves API calls vs
        search_many() which pulls everything before returning.
        """
        per_page = 25
        for page in range(1, max_pages + 1):
            resp = self.search(
                query=query, naf=naf,
                code_postal=code_postal, departement=departement, region=region,
                tranche_effectif=tranche_effectif,
                page=page, per_page=per_page,
                local_only=local_only,
            )
            yield resp
            if not resp.results or page >= resp.total_pages:
                break

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
