"""Enrich the 16 siege-69001 dental cabinets we haven't enriched yet."""

import json
import sys
import warnings

warnings.filterwarnings("ignore")

from sirene_client import SireneClient
from pipeline import enrich_company_partial


WANT_SIRENS = {
    "344618772", "477652762", "481630945",
    "753179001", "402999742", "821532652", "910510254",
    "903759231", "904467081", "480396688",
    "840883979", "834994048", "850571910",
}


def main() -> int:
    by_siren: dict = {}
    with SireneClient() as c:
        for page in (1, 2):
            resp = c.search(naf="86.23Z", code_postal="69001", per_page=25, page=page)
            for r in resp.results:
                if r.siren in WANT_SIRENS and r.siege and r.siege.code_postal == "69001":
                    by_siren[r.siren] = r

    out = []
    for siren, company in by_siren.items():
        try:
            partial = enrich_company_partial(company)
        except Exception as e:
            out.append({"siren": siren, "name": company.nom_complet, "error": f"{type(e).__name__}: {e}"})
            continue
        web = partial.get("web_enrichment") or {}
        out.append({
            "siren": siren,
            "name": company.nom_complet,
            "website": partial.get("website"),
            "legal_dirigeants": partial.get("legal_dirigeants"),
            "team_page_text_len": len(partial.get("team_page_text") or ""),
            "team_page_preview": (partial.get("team_page_text") or "")[:300],
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
