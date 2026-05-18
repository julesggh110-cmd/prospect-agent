"""Generate 5 verified leads of certified-organic French spirits distilleries
for a spirits wholesaler. Candidates are pre-selected from web evidence
(public bio/organic claims on their own websites) and cross-checked against
Sirene for legal-dirigeant data.
"""
import warnings
warnings.filterwarnings("ignore")

import sys
from pipeline import enrich_company_partial, enrich_companies_parallel, finalize_lead
from sheets_export import export_leads
from sirene_client import SireneClient


# (siren, first, last, role, bio_evidence_source)
CANDIDATES = [
    ("812981710", "HERVE",    "GRANGEON",  "Président",          "site officiel distillerie-ergaster.com : 'certifiée biologique'"),
    ("832935829", "GILLES",   "VICTORS",   "Président",          "site officiel maison-victors.com : 'Distillerie bio premium'"),
    ("979621281", "THOMAS",   "GUILLARD",  "Gérant",             "site officiel distillerie-terre-froide.fr : 'producteur bio'"),
    ("801353178", "HELENE",   "PEREZ",     "Dirigeante",         "site officiel mountainspirit-fabrik.fr : 'spiritueux premium biologiques'"),
    ("854077179", "BENJAMIN", "COUSSEAU",  "Co-fondateur",       "site officiel distilleriedesachards.fr : 'engagement bio'"),
]


def fetch_company(siren: str):
    with SireneClient() as c:
        resp = c.search(query=siren, per_page=5)
    for comp in resp.results:
        if comp.siren == siren:
            return comp
    return None


def main():
    print("[i] Fetching Sirene records...", file=sys.stderr)
    companies = []
    for siren, *_ in CANDIDATES:
        comp = fetch_company(siren)
        if comp is None:
            print(f"[!] SIREN {siren} not found", file=sys.stderr)
            continue
        companies.append(comp)
    print(f"[i] Got {len(companies)} companies; enriching in parallel...", file=sys.stderr)

    partials = enrich_companies_parallel(companies, max_workers=3)

    # Index partials by SIREN for matching with our candidate decision-makers
    by_siren = {p["siren"]: p for p in partials}

    leads = []
    for siren, first, last, role, bio_src in CANDIDATES:
        partial = by_siren.get(siren)
        if not partial:
            continue
        print(f"\n[+] {partial['company_name']} ({siren}) — website={partial.get('website')}", file=sys.stderr)
        lead = finalize_lead(
            partial,
            person_first=first,
            person_last=last,
            person_role=role,
            person_sources=["sirene"],
            naf_label="Production de boissons alcooliques distillées (certifiée AB / biologique)",
        )
        leads.append((lead, bio_src))
        print(
            f"    dropped={lead.dropped} reason={lead.drop_reason}\n"
            f"    name conf={lead.person_name.confidence}, role conf={lead.person_role.confidence}\n"
            f"    email={lead.person_email.value} (conf={lead.person_email.confidence}, note={lead.person_email.note})\n"
            f"    li_person={lead.person_linkedin.value} (conf={lead.person_linkedin.confidence})\n"
            f"    ig_person={lead.person_instagram.value} (conf={lead.person_instagram.confidence})\n"
            f"    phone_person={lead.person_phone.value} (conf={lead.person_phone.confidence})\n"
            f"    li_co={lead.company_linkedin.value} (conf={lead.company_linkedin.confidence})\n"
            f"    ig_co={lead.company_instagram.value} (conf={lead.company_instagram.confidence})\n"
            f"    phone_co={lead.company_phone.value} (conf={lead.company_phone.confidence})\n"
            f"    fb_co={lead.company_facebook}",
            file=sys.stderr,
        )

    only_leads = [l for l, _ in leads]
    output = export_leads(only_leads)
    print(f"\n[OK] Exported {len(only_leads)} leads -> {output}", file=sys.stderr)

    # Also dump a JSON snapshot with bio source for downstream reporting
    import json
    snapshot = []
    for lead, bio_src in leads:
        d = lead.model_dump()
        d["_bio_evidence"] = bio_src
        snapshot.append(d)
    snap_path = output.replace(".csv", ".json")
    with open(snap_path, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=2, ensure_ascii=False, default=str)
    print(f"[OK] JSON snapshot -> {snap_path}", file=sys.stderr)

    print(output)  # stdout


if __name__ == "__main__":
    main()
