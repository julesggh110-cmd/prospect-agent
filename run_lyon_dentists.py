"""Enrich Sirene candidates for cabinets dentaires Lyon 69001 (partial only)."""

import json
import sys
import warnings

warnings.filterwarnings("ignore")

from sirene_client import SireneClient
from pipeline import enrich_company_partial


DIRECTORY_DOMAINS = {
    "rdvdentiste.net",
    "sante.fr",
    "lemedecin.fr",
    "doctolib.fr",
    "archive.org",
    "chirurgiens-dentistes-en-france.fr",
    "selarl-cabinet-dentaire-didier-villemey.chirurgiens-dentistes-en-france.fr",
    "pagesjaunes.fr",
    "facebook.com",
    "instagram.com",
}


def is_directory(url: str | None) -> bool:
    if not url:
        return True
    low = url.lower()
    for d in DIRECTORY_DOMAINS:
        if d in low:
            return True
    return False


def main() -> int:
    with SireneClient() as client:
        resp = client.search(
            naf="86.23Z", code_postal="69001", per_page=25
        )

    out = []
    for company in resp.results:
        try:
            partial = enrich_company_partial(company)
        except Exception as e:
            out.append({
                "siren": company.siren,
                "name": company.nom_complet,
                "error": f"{type(e).__name__}: {e}",
            })
            continue

        web = partial.get("web_enrichment") or {}
        out.append({
            "siren": company.siren,
            "name": company.nom_complet,
            "website": partial.get("website"),
            "website_is_directory": is_directory(partial.get("website")),
            "legal_dirigeants": partial.get("legal_dirigeants"),
            "team_page_text_len": len(partial.get("team_page_text") or ""),
            "team_page_text_preview": (partial.get("team_page_text") or "")[:400],
            "emails": web.get("emails"),
            "phones": web.get("phones"),
            "linkedin": web.get("linkedin"),
            "instagram": web.get("instagram"),
            "facebook": web.get("facebook"),
        })

    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
