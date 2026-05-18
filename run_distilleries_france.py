"""Generate 5 verified leads of French distilleries (NAF 11.01Z) for a spirits wholesaler."""
import warnings
warnings.filterwarnings("ignore")

import json
import sys
from pipeline import enrich_company_partial, enrich_companies_parallel, finalize_lead
from sheets_export import export_leads
from sirene_client import SireneClient


# 8 active candidates with named legal dirigeants (picked from Sirene NAF 11.01Z)
CANDIDATES = {
    "675850242": ("BERNARD", "BAUD", "Président"),                  # Grandes Distilleries Peureux
    "351427604": ("PIERRE JEAN", "NAUD", "Président"),               # Distillerie de la Tour / Naud
    "335481040": ("PASCAL", "HAMON", "Président"),                   # Distillerie Jean Goyard
    "466203338": ("PASCAL", "BARAT", "Président"),                   # Distillerie Dillon
    "905420295": ("LILIAN", "TESSENDIER", "Président"),              # Distillerie Tessendier (Cognac Park)
    "528785637": ("SEBASTIEN", "CASTAN", "Président"),               # Distillerie Castan
    "881373310": ("SARAH", "GAUTHIER", "Présidente"),                # Distillerie des Aravis
    "436150049": ("GILLES", "LEIZOUR", "Président"),                 # Distillerie Warenghem
}

def main():
    with SireneClient() as c:
        resp = c.search(query="distillerie", naf="11.01Z", per_page=25)
    selected = [comp for comp in resp.results if comp.siren in CANDIDATES]
    print(f"[i] Enriching {len(selected)} candidates...", file=sys.stderr)

    partials = enrich_companies_parallel(selected, max_workers=3)

    leads = []
    for partial in partials:
        siren = partial["siren"]
        first, last, role = CANDIDATES[siren]
        print(f"\n[+] {partial['company_name']} ({siren}) — website={partial.get('website')}", file=sys.stderr)
        lead = finalize_lead(
            partial,
            person_first=first,
            person_last=last,
            person_role=role,
            person_sources=["sirene"],
            naf_label="Production de boissons alcooliques distillées",
        )
        leads.append(lead)
        print(
            f"    dropped={lead.dropped} reason={lead.drop_reason}\n"
            f"    email={lead.person_email.value} (conf={lead.person_email.confidence})\n"
            f"    li_co={lead.company_linkedin.value} (conf={lead.company_linkedin.confidence})\n"
            f"    ig_co={lead.company_instagram.value} (conf={lead.company_instagram.confidence})\n"
            f"    phone_co={lead.company_phone.value} (conf={lead.company_phone.confidence})",
            file=sys.stderr,
        )

    kept = [l for l in leads if not l.dropped]
    dropped = [l for l in leads if l.dropped]
    print(f"\n[=] kept={len(kept)} dropped={len(dropped)}", file=sys.stderr)
    for d in dropped:
        print(f"    DROPPED {d.company_name}: {d.drop_reason}", file=sys.stderr)

    # Keep at most 5 strongest
    kept.sort(key=lambda l: l.lead_score or 0, reverse=True)
    final = kept[:5]
    output = export_leads(final, csv_path="data/leads-distilleries-france.csv")
    print(f"\n[OK] {len(final)} leads exported -> {output}", file=sys.stderr)
    print(output)  # stdout

if __name__ == "__main__":
    main()
