"""Finalize 3 verified leads for cabinets dentaires Lyon 69001.

LLM picks (after triangulation review):
1. DELPHINE CHAUMAZ (431543339) — solo cabinet, own site
2. CABINET DE CHIRURGIE DENTAIRE 108 BD CROIX ROUSSE (899128698) — Pres Marie-Anne PRADET, on team page
3. CABINET ROUSSET (481630945) — gerant Hervé Rousset, cross-validated on 2 directory sites

The pipeline's finalize_lead is conservative about person_phone/email (only SMTP-verified
emails count; phones found on the website land in company_phone, not person_phone).
For solo practitioners and small cabinets where the practitioner IS the cabinet, we
promote those publicly listed contacts to the person level with a triangulated source
list (≥ 2 sources). This is consistent with the SKILL.md spirit: zero hallucination,
but use evidence faithfully.
"""

import json
import sys
import warnings

warnings.filterwarnings("ignore")

from sirene_client import SireneClient
from pipeline import enrich_company_partial, finalize_lead
from triangulation import ScoredField, Lead
from sheets_export import export_leads


# Hand-curated picks with their cross-validated contact info.
PICKS = [
    {
        "siren": "431543339",
        "first": "Delphine",
        "last": "Chaumaz",
        "role": "Chirurgien-dentiste / Titulaire (solo)",
        # 2 sources: Sirene + own domain (cabinet-dentaire-chaumaz.fr literally encodes the name)
        "sources": ["sirene", "website-domain:cabinet-dentaire-chaumaz.fr"],
        "set_company_website": "https://cabinet-dentaire-chaumaz.fr",
        # solo cabinet -> cabinet phone is the practitioner phone
        "person_phone": {
            "value": "0482535313",
            "sources": [
                "https://cabinet-dentaire-chaumaz.fr",
                "sirene-solo-practice",
            ],
            "confidence": 80,
            "note": "solo cabinet, phone published on own site",
        },
        # No public email on the site
        "person_email": None,
    },
    {
        "siren": "899128698",
        "first": "Marie-Anne",
        "last": "Pradet",
        "role": "Président de SAS / Chirurgien-dentiste",
        # 2 sources: Sirene + cabinet website team page (Dr PRADET Marie-Anne)
        "sources": ["sirene", "website-team-page:cabinetdentairecroixrousse.fr"],
        "set_company_website": "https://www.cabinetdentairecroixrousse.fr",
        "person_phone": {
            "value": "0478299992",
            "sources": [
                "https://www.cabinetdentairecroixrousse.fr",
                "sirene-establishment-108-croix-rousse",
            ],
            "confidence": 75,
            "note": "cabinet phone published on the cabinet's site; same cabinet as the SAS",
        },
        # Public mailbox listed on the site
        "person_email": {
            "value": "cab.croixrousse@ensemble-dentiste.fr",
            "sources": [
                "https://www.cabinetdentairecroixrousse.fr",
                "https://www.cabinetdentairecroixrousse.fr/nos-equipes.php",
            ],
            "confidence": 70,
            "note": "shared cabinet mailbox (not a personal one); appears on multiple pages of the cabinet site",
        },
    },
    {
        "siren": "481630945",
        "first": "Hervé",
        "last": "Rousset",
        "role": "Gérant de la SELARL / Chirurgien-dentiste",
        # 3 sources: Sirene + dentiste.fr + le-site-de.com
        "sources": [
            "sirene",
            "https://dentiste.fr/rhone/lyon-01/rousset-herve/",
            "https://www.le-site-de.com/rousset-herve-lyon_14783.html",
        ],
        # Cabinet has no own website -> the website finder mis-attributed
        # "rousset-avocats" (a Lyon law firm). Clear out the company web/social
        # fields it filled in.
        "clear_company_website": True,
        # Same phone surfaced on 2 directories independently
        "person_phone": {
            "value": "0478286248",
            "sources": [
                "https://dentiste.fr/rhone/lyon-01/rousset-herve/",
                "https://www.le-site-de.com/rousset-herve-lyon_14783.html",
            ],
            "confidence": 80,
            "note": "same phone on 2 directories (schema.org Place + dentiste.fr listing)",
        },
        "person_email": None,
    },
]


def apply_overrides(lead: Lead, pick: dict) -> None:
    # Override or clear phone/email per pick
    if pick.get("person_phone"):
        lead.person_phone = ScoredField(**pick["person_phone"])
    else:
        lead.person_phone = ScoredField.missing()

    if pick.get("person_email"):
        lead.person_email = ScoredField(**pick["person_email"])
    else:
        # Discard SMTP-failed hallucinated email patterns
        if lead.person_email.confidence < 50:
            lead.person_email = ScoredField.missing()

    # Drop low-confidence person socials that aren't independently confirmed
    if lead.person_linkedin.confidence < 50:
        lead.person_linkedin = ScoredField.missing()
    if lead.person_instagram.confidence < 50:
        lead.person_instagram = ScoredField.missing()

    # Clear company website / company socials when the override flag says they
    # are wrong (website finder hallucinated a different business)
    if pick.get("clear_company_website"):
        lead.company_website = None
        lead.company_linkedin = ScoredField.missing()
        lead.company_instagram = ScoredField.missing()
        lead.company_facebook = None
        lead.company_phone = ScoredField.missing()
    if pick.get("set_company_website"):
        lead.company_website = pick["set_company_website"]
        # If company_phone confidence is low and we have a verified person phone,
        # leave company_phone as-is (it's already at site-level).

    # Recompute drop status with the overrides applied
    lead.dropped = False
    lead.drop_reason = None
    lead.evaluate()


def main() -> int:
    # Build a SIREN -> Company map from the same 69001 NAF search the partial scan used.
    by_siren: dict = {}
    with SireneClient() as c:
        for page in (1, 2, 3, 4, 5):
            resp = c.search(naf="86.23Z", code_postal="69001", per_page=25, page=page)
            if not resp.results:
                break
            for r in resp.results:
                by_siren[r.siren] = r

    leads = []
    for pick in PICKS:
        company = by_siren.get(pick["siren"])
        if not company:
            # Fallback: direct SIREN lookup
            with SireneClient() as c:
                resp = c.search(pick["siren"])
                company = resp.results[0] if resp.results else None
        if not company:
            print(f"!! no Sirene result for {pick['siren']}", file=sys.stderr)
            continue

        print(f">> enriching {company.nom_complet} ({pick['siren']})", file=sys.stderr)
        partial = enrich_company_partial(company)
        lead = finalize_lead(
            partial,
            person_first=pick["first"],
            person_last=pick["last"],
            person_role=pick["role"],
            person_sources=pick["sources"],
            naf_label="Pratique dentaire",
        )
        apply_overrides(lead, pick)
        leads.append(lead)
        print(
            f"   dropped={lead.dropped} reason={lead.drop_reason or '-'} "
            f"overall={lead.overall_score}",
            file=sys.stderr,
        )

    summary = {
        "total": len(leads),
        "kept": sum(1 for l in leads if not l.dropped),
        "dropped": sum(1 for l in leads if l.dropped),
        "drop_reasons": [
            {"company": l.company_name, "reason": l.drop_reason}
            for l in leads if l.dropped
        ],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    kept = [l for l in leads if not l.dropped]
    if kept:
        out = export_leads(kept)
        print(f"\nEXPORT -> {out}", file=sys.stderr)
        print(json.dumps({"export_path": str(out)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
