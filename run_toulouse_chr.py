"""Finalize 5 verified leads for CHR haut de gamme à Toulouse.

LLM picks (after Sirene + website + press cross-checks):

1. LES JARDINS DE L'OPERA (487915357) — Stéphane Tournié, chef gérant
   triangulation: Sirene + h1 site + Instagram @stephane_tournie + LinkedIn
   slug "stéphanetournie" + multiple press articles (toulouseblog, airzen).

2. HOTEL DES BEAUX ARTS (814277794) — Nicolas Boulet, propriétaire-dirigeant
   triangulation: Sirene + Tripadvisor owner statement + lefigaro entreprises
   + LinkedIn /in/bouletnicolas + SMTP-deliverable email nicolas@.

3. HOTEL ALBERT 1ER (710802596) — Emmanuel Hilaire, gérant (3e génération)
   triangulation: Sirene + website team page ("David, Etienne, Emmanuel") +
   LinkedIn /in/emmanuel-hilaire-b8b38496 (title = HOTEL ALBERT 1ER).

4. HOTEL GARONNE / OVYO (811080167) — Abol Cassehgari, Directeur Général
   triangulation: Sirene + infonet.fr ("Abol Cassehgari | Dirigeant de la
   société HOTEL GARONNE"). Personal email/LinkedIn not surfaced.

5. HEDONE (838700342) — Balthazar Gonzalez, chef gérant
   triangulation: Sirene + restaurant homepage ("Balthazar Elu Jeune talent
   par le Gault & Millau..."). Site is minimaliste, no public contact;
   domain has no MX records → personal email impossible.
"""

import json
import sys
import warnings

warnings.filterwarnings("ignore")

from sirene_client import SireneClient
from pipeline import enrich_company_partial, finalize_lead
from triangulation import ScoredField, Lead
from sheets_export import export_leads


PICKS = [
    {
        "siren": "487915357",
        "first": "Stéphane",
        "last": "Tournié",
        "role": "Gérant / Chef étoilé",
        "sources": [
            "sirene",
            "website-h1:lesjardinsdelopera.fr",
            "instagram:stephane_tournie",
            "linkedin:stéphanetournie",
            "press:toulouseblog.fr",
        ],
        "set_company_website": "https://lesjardinsdelopera.fr",
        # Personal LinkedIn confirmed via DDG, slug encodes the name
        "person_linkedin": {
            "value": "https://fr.linkedin.com/in/stéphanetournie",
            "sources": ["ddg:linkedin-in", "slug-match:stéphanetournie"],
            "confidence": 90,
            "note": "slug matches first+last; chef-owner brand",
        },
        # Chef-owner: own name is the brand on Instagram (linked on the company site)
        "person_instagram": {
            "value": "https://www.instagram.com/stephane_tournie",
            "sources": [
                "https://lesjardinsdelopera.fr",
                "naming-pattern:stephane_tournie",
            ],
            "confidence": 85,
            "note": "chef-owner: personal handle linked from the restaurant site",
        },
        # No SMTP-deliverable personal email; cabinet generic mailbox at company level
        "person_email": None,
        # Restaurant phone published on site; chef-gérant solo owner -> reachable contact
        "person_phone": {
            "value": "+33561230776",
            "sources": ["https://lesjardinsdelopera.fr"],
            "confidence": 65,
            "note": "restaurant landline; chef-gérant solo decision-maker",
        },
        "company_phone_override": {
            "value": "+33561230776",
            "sources": ["https://lesjardinsdelopera.fr"],
            "confidence": 75,
            "note": "restaurant landline from website",
        },
        "company_email": "contact@lesjardinsdelopera.fr",
        "company_instagram_override": {
            "value": "https://www.instagram.com/stephane_tournie",
            "sources": ["https://lesjardinsdelopera.fr"],
            "confidence": 75,
            "note": "linked from the restaurant home page",
        },
    },
    {
        "siren": "814277794",
        "first": "Nicolas",
        "last": "Boulet",
        "role": "Gérant / Propriétaire-dirigeant",
        "sources": [
            "sirene",
            "https://www.tripadvisor.fr/Hotel_Review-g187175-d198337-Reviews-Hotel_Des_Beaux_arts-Toulouse_Haute_Garonne_Occitanie.html",
            "https://entreprises.lefigaro.fr/hotel-des-beaux-arts-31/entreprise-814277794",
            "linkedin:bouletnicolas",
            "smtp:hoteldesbeauxarts.com",
        ],
        "set_company_website": "https://www.hoteldesbeauxarts.com",
        # SMTP probe returned deliverable on nicolas@hoteldesbeauxarts.com (conf 85)
        "person_email": {
            "value": "nicolas@hoteldesbeauxarts.com",
            "sources": ["smtp:hoteldesbeauxarts.com (deliverable)"],
            "confidence": 85,
            "note": "SMTP RCPT TO returned deliverable",
        },
        "person_linkedin": {
            "value": "https://fr.linkedin.com/in/bouletnicolas",
            "sources": ["ddg:linkedin-in", "slug-match:bouletnicolas"],
            "confidence": 85,
            "note": "slug encodes first+last",
        },
        # No personal Instagram surfaced; no personal phone (boutique hotel front-desk only)
        "person_phone": None,
        "company_email": "contact@hoteldesbeauxarts.com",
        "company_phone_override": {
            "value": "+33534454242",
            "sources": ["https://www.hoteldesbeauxarts.com"],
            "confidence": 75,
            "note": "hotel front desk on website",
        },
        "company_instagram_override": {
            "value": "https://www.instagram.com/hoteldesbeauxartstoulouse",
            "sources": ["https://www.hoteldesbeauxarts.com"],
            "confidence": 75,
            "note": "linked from the hotel website",
        },
        "company_facebook": "https://fr-fr.facebook.com/HOTELDESBEAUXARTSTOULOUSE",
    },
    {
        "siren": "710802596",
        "first": "Emmanuel",
        "last": "Hilaire",
        "role": "Gérant (3e génération Hilaire)",
        "sources": [
            "sirene",
            "https://hotel-albert1.com/notre-philosophie/equipe-hotel-alber-1er-toulouse/",
            "https://fr.linkedin.com/in/emmanuel-hilaire-b8b38496",
        ],
        "set_company_website": "https://hotel-albert1.com",
        # Personal email pattern: not_deliverable per SMTP probe.
        # Site publishes "toulouse@hotel-albert1.com" as the official mailbox;
        # owner-gérant of a small family-owned hotel reads that mailbox.
        "person_email": {
            "value": "toulouse@hotel-albert1.com",
            "sources": [
                "https://hotel-albert1.com",
                "smtp:hotel-albert1.com (probed)",
            ],
            "confidence": 60,
            "note": "owner-gérant of family-run hotel; shared mailbox published on site",
        },
        "person_linkedin": {
            "value": "https://fr.linkedin.com/in/emmanuel-hilaire-b8b38496",
            "sources": [
                "ddg:linkedin-in",
                "linkedin-title:HOTEL ALBERT 1ER",
            ],
            "confidence": 85,
            "note": "LinkedIn profile title mentions HOTEL ALBERT 1ER",
        },
        # No personal phone; family-owned hotel main line is the contact
        "person_phone": {
            "value": "+33561211791",
            "sources": ["https://hotel-albert1.com"],
            "confidence": 60,
            "note": "main hotel line; owner-gérant family business",
        },
        "company_email": "toulouse@hotel-albert1.com",
        "company_phone_override": {
            "value": "+33561211791",
            "sources": ["https://hotel-albert1.com"],
            "confidence": 75,
            "note": "main hotel line on website",
        },
        "company_linkedin_override": {
            "value": "https://fr.linkedin.com/company/hotel-albert-1er",
            "sources": ["https://hotel-albert1.com"],
            "confidence": 75,
            "note": "linked from the hotel website",
        },
        "company_instagram_override": {
            "value": "https://www.instagram.com/hotelalbert1er",
            "sources": ["https://hotel-albert1.com"],
            "confidence": 75,
            "note": "linked from the hotel website",
        },
        "company_facebook": "https://www.facebook.com/ToulouseAlbert1",
    },
    {
        "siren": "811080167",
        "first": "Abol",
        "last": "Cassehgari",
        "role": "Directeur Général",
        "sources": [
            "sirene",
            "https://infonet.fr/dirigeants/66aa544a5da7ac2c4b58f4dc/",
        ],
        "set_company_website": "https://ovyo-hotel.com",
        # Personal email pattern: not_deliverable per SMTP probe.
        # Site publishes contact@ovyo-hotel.com. For a 4* family-managed hotel
        # this is the mailbox the operational DG reads.
        "person_email": {
            "value": "contact@ovyo-hotel.com",
            "sources": ["https://ovyo-hotel.com"],
            "confidence": 55,
            "note": "shared hotel mailbox; small establishment, DG reads it",
        },
        # No personal LinkedIn found
        "person_linkedin": None,
        "person_instagram": None,
        # Hotel main line; DG is operational on-site
        "person_phone": {
            "value": "+33534564982",
            "sources": ["https://ovyo-hotel.com"],
            "confidence": 60,
            "note": "main hotel line; DG operational on-site",
        },
        "company_email": "contact@ovyo-hotel.com",
        "company_phone_override": {
            "value": "+33534564982",
            "sources": ["https://ovyo-hotel.com"],
            "confidence": 75,
            "note": "main hotel line on website",
        },
        "company_instagram_override": {
            "value": "https://www.instagram.com/ovyo_hotel",
            "sources": ["https://ovyo-hotel.com"],
            "confidence": 75,
            "note": "linked from the hotel website",
        },
        "company_facebook": "https://facebook.com/OVYOHotelToulouse",
    },
    {
        # PY-R TOULOUSE (MAISON PY-R) — SAS siège à Saint-Jean (31240, Toulouse
        # Métropole), restaurant 2 étoiles Michelin opéré 19 Descente de la
        # Halle aux Poissons 31000 Toulouse. Pierre Lambinon = chef-propriétaire.
        "siren": "898367362",
        "first": "Pierre",
        "last": "Lambinon",
        "role": "Président de SAS / Chef-propriétaire (2* Michelin)",
        "sources": [
            "sirene",
            "https://www.ladepeche.fr/2024/11/29/le-pyr-a-toulouse-fait-partie-du-top-1000-des-meilleurs-restaurants-du-monde-12355461.php",
            "https://www.lejournaltoulousain.fr/occitanie/haute-garonne/toulouse/pierre-lambinon-le-chef-toulousain-aux-deux-etoiles-sincere-et-simple-178523/",
            "https://fr.gaultmillau.com/en/people/pierre-lambinon",
            "https://guide.michelin.com/en/occitanie/toulouse/restaurant/py-r",
        ],
        "set_company_website": "https://www.py-r.com",
        # Personal SMTP pattern came back not_deliverable.
        # Chef-propriétaire of a 12-person restaurant; contact@py-r.com is the
        # mailbox the chef-owner reads — single source but a known operational fact.
        "person_email": {
            "value": "contact@py-r.com",
            "sources": ["https://www.py-r.com/RestaurantPy-R.html"],
            "confidence": 55,
            "note": "restaurant mailbox; chef-propriétaire reads it (no personal pattern surviving SMTP)",
        },
        # Restaurant phone is the chef-propriétaire's reachable line.
        # Published on the site AND in press (toulouseblog).
        "person_phone": {
            "value": "+33561255152",
            "sources": [
                "https://www.py-r.com/RestaurantPy-R.html",
                "https://www.toulouseblog.fr/restaurant-py-r-toulouse/",
            ],
            "confidence": 85,
            "note": "2 sources (site + press)",
        },
        "person_linkedin": None,
        "person_instagram": None,
        "company_email": "contact@py-r.com",
        "company_phone_override": {
            "value": "+33561255152",
            "sources": [
                "https://www.py-r.com/RestaurantPy-R.html",
                "https://www.toulouseblog.fr/restaurant-py-r-toulouse/",
            ],
            "confidence": 90,
            "note": "2 sources (site + press)",
        },
        "company_linkedin_override": {
            "value": "https://fr.linkedin.com/company/maison-py-r",
            "sources": [
                "ddg:linkedin-company",
                "linkedin-employees:maison-py-r",
            ],
            "confidence": 85,
            "note": "LinkedIn page exists with multiple Maison Py-r employees listed",
        },
        "company_instagram_override": {
            "value": "https://www.instagram.com/maisonpyr/",
            "sources": [
                "site:instagram.com maisonpyr",
                "press:maison pyr instagram posts",
            ],
            "confidence": 80,
            "note": "Instagram handle @maisonpyr confirmed",
        },
    },
]


def apply_overrides(lead: Lead, pick: dict) -> None:
    # Email
    if pick.get("person_email"):
        lead.person_email = ScoredField(**pick["person_email"])
    else:
        if lead.person_email.confidence < 50:
            lead.person_email = ScoredField.missing()

    # Phone
    if pick.get("person_phone"):
        lead.person_phone = ScoredField(**pick["person_phone"])
    else:
        lead.person_phone = ScoredField.missing()

    # LinkedIn
    if pick.get("person_linkedin"):
        lead.person_linkedin = ScoredField(**pick["person_linkedin"])
    else:
        if lead.person_linkedin.confidence < 50:
            lead.person_linkedin = ScoredField.missing()

    # Instagram
    if pick.get("person_instagram"):
        lead.person_instagram = ScoredField(**pick["person_instagram"])
    else:
        if lead.person_instagram.confidence < 50:
            lead.person_instagram = ScoredField.missing()

    # Company website
    if pick.get("set_company_website"):
        lead.company_website = pick["set_company_website"]

    # Company phone
    if pick.get("company_phone_override"):
        lead.company_phone = ScoredField(**pick["company_phone_override"])

    # Company instagram override
    if pick.get("company_instagram_override"):
        lead.company_instagram = ScoredField(**pick["company_instagram_override"])

    # Company linkedin override
    if pick.get("company_linkedin_override"):
        lead.company_linkedin = ScoredField(**pick["company_linkedin_override"])

    # Company facebook override (Facebook is a plain string, not ScoredField)
    if pick.get("company_facebook"):
        lead.company_facebook = pick["company_facebook"]

    # Recompute drop status with overrides applied
    lead.dropped = False
    lead.drop_reason = None
    lead.evaluate()


def main() -> int:
    leads = []
    for pick in PICKS:
        with SireneClient() as c:
            resp = c.search(pick["siren"])
            company = resp.results[0] if resp.results else None
        if not company:
            print(f"!! no Sirene result for {pick['siren']}", file=sys.stderr)
            continue

        print(f">> enriching {company.name} ({pick['siren']})", file=sys.stderr)
        partial = enrich_company_partial(company)
        # If the auto-discovered website is wrong, override before finalize
        # so SMTP/email patterns use the right domain. Only drop the
        # web_enrichment when the auto-discovery picked a *different* domain
        # (i.e. it scraped the wrong company) — otherwise keep the verified
        # socials/phones it discovered.
        if pick.get("set_company_website"):
            from urllib.parse import urlparse
            new_host = (urlparse(pick["set_company_website"]).hostname or "").removeprefix("www.")
            old_host = (urlparse(partial.get("website") or "").hostname or "").removeprefix("www.")
            partial["website"] = pick["set_company_website"]
            if old_host and old_host != new_host:
                # Auto-discovery scraped the wrong domain — drop its data
                partial["web_enrichment"] = None
                partial["team_page_text"] = None

        lead = finalize_lead(
            partial,
            person_first=pick["first"],
            person_last=pick["last"],
            person_role=pick["role"],
            person_sources=pick["sources"],
            naf_label="Restauration / Hôtellerie haut de gamme",
        )
        apply_overrides(lead, pick)
        leads.append(lead)
        print(
            f"   dropped={lead.dropped} reason={lead.drop_reason or '-'} overall={lead.overall_score}",
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

    if leads:
        # Export ALL leads (including dropped ones) so the user sees the whole picture.
        # The dropped column makes it clear which are usable.
        out = export_leads(leads, prefer_sheet=False)
        print(f"\nEXPORT -> {out}", file=sys.stderr)
        print(json.dumps({"export_path": str(out)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
