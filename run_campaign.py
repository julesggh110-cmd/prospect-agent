"""
run_campaign.py — ONE function that runs the full prospection campaign end to end.

Why: when Claude (in Multica or Claude Code) drives the pipeline tool-by-tool, every
intermediate state is shipped back through the model. With Sonnet that's ~$0.40/lead.
With this script, Claude makes ONE Bash call instead of 30+, saving 90%+ of tokens.

Usage from a shell:
    python run_campaign.py \\
        --query "cabinet dentaire" --code-postal 69001 --volume 10 \\
        --persona "associé" --output prospects-dentistes-lyon

Usage from Claude (the typical Multica flow):
    1. Pick query, geo, persona from the user request
    2. Call this script ONCE via Bash
    3. Read the printed summary + the produced .xlsx
    4. Report back to the user

The script:
- Searches Sirene
- Runs enrich_company_partial in parallel (Pappers + Brave + cache)
- Picks the legal director as decision-maker (Phase 1 default). For Phase 2 with
  Claude-in-the-loop persona disambiguation, use --interactive or call the lower-level
  pipeline functions yourself.
- Finalizes each lead (email, LinkedIn, Insta)
- Exports both .csv (Excel-FR) and .xlsx via sheets_export.export_leads
- Prints a 1-screen summary
"""
from __future__ import annotations

import argparse
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

# Make sure relative imports work whether we are run as a script or a module
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _summary(leads, kept_path: str, elapsed: float) -> None:
    kept = [l for l in leads if not l.dropped]
    dropped = [l for l in leads if l.dropped]
    print()
    print(f"=== {len(leads)} leads enriched in {elapsed:.1f}s "
          f"(~{elapsed/max(1,len(leads)):.1f}s/lead) ===")
    print(f"  Kept:    {len(kept)}")
    print(f"  Dropped: {len(dropped)}")

    if dropped:
        from collections import Counter
        reasons = Counter(l.drop_reason for l in dropped)
        for reason, n in reasons.most_common(3):
            print(f"    [{n}x] {reason}")

    print()
    print(f"=== Sample of top {min(3, len(kept))} kept leads ===")
    for l in sorted(kept, key=lambda x: -x.overall_score)[:3]:
        print(f"  {l.company_name} (score {l.overall_score})")
        print(f"    {l.person_name.value} · {l.person_role.value or '?'}")
        print(f"    email:    {l.person_email.value or '—'} (conf {l.person_email.confidence})")
        print(f"    linkedin: {l.person_linkedin.value or '—'} (conf {l.person_linkedin.confidence})")
        print(f"    phone:    {l.person_phone.value or l.company_phone.value or '—'}")
    print()
    print(f"=== Output ===")
    print(f"  CSV:  {kept_path}")
    xlsx = kept_path.replace('.csv', '.xlsx')
    if Path(xlsx).exists():
        print(f"  XLSX: {xlsx}")


def run(
    *,
    query: str | None = None,
    naf: str | None = None,
    code_postal: str | None = None,
    departement: str | None = None,
    region: str | None = None,
    tranche_effectif: str | None = None,
    volume: int = 10,
    persona_role_hint: str | None = None,
    output_stem: str | None = None,
    max_workers: int = 8,
    icp: dict | None = None,
    only_new: bool = False,
    push_to_hubspot: bool = False,
    campaign_id: str | None = None,
    llm_decider: bool = False,
    retry_dropped: bool = False,
    generate_emails: bool = False,
    sender_offer: str = "spiritueux premium français pour cartes bars et restaurants",
    sender_company: str = "Bear Brothers",
) -> str:
    """End-to-end campaign. Returns the path of the produced CSV.

    New in v0.5.0:
    - `icp`: dict from icp.py (e.g., PRESET_CAVISTES_PREMIUM_PARIS). Annotates
      each lead with an `icp_score` 0-100. Use `icp.PRESET_*` for ready-made.
    - `only_new`: skip companies already in lead_store (dedup across runs).
    - `push_to_hubspot`: also sync kept leads to HubSpot (needs HUBSPOT_ACCESS_TOKEN).
    - `campaign_id`: tag the lead_store rows so you can list "leads from campaign X".
    """
    import time as _time
    from sirene_client import SireneClient
    from pipeline import enrich_companies_parallel, finalize_lead
    from sheets_export import export_leads
    from lead_store import already_seen_sirens, upsert_leads

    t0 = time.time()
    campaign_id = campaign_id or _time.strftime("campaign-%Y%m%d-%H%M%S")

    # 1. Source via Sirene
    with SireneClient() as c:
        resp = c.search(
            query=query,
            naf=naf,
            code_postal=code_postal,
            departement=departement,
            region=region,
            tranche_effectif=tranche_effectif,
            per_page=min(volume, 25),
        )
    companies = resp.results[:volume]
    if not companies:
        print("No companies matched the query.")
        sys.exit(1)
    print(f"[Sirene] {len(companies)} companies")

    # 1b. Dedup vs lead_store if requested
    if only_new:
        seen = already_seen_sirens([c.siren for c in companies])
        companies = [c for c in companies if c.siren not in seen]
        print(f"[Dedup] {len(seen)} already prospected, {len(companies)} new")

    # 2. Parallel partial enrichment (Pappers + Brave + cache)
    partials = enrich_companies_parallel(companies, max_workers=max_workers)
    with_site = sum(1 for p in partials if p.get('website'))
    print(f"[Enrich] {with_site}/{len(partials)} with website")

    # 3. Finalize each lead — Phase-1 default: take the first legal director.
    leads = []
    sirens = [c.siren for c in companies if c.siren]
    if only_new:
        # We already filtered out seen ones above — all current ones are "new" by construction
        new_sirens = set(sirens)
    else:
        new_sirens = set(sirens) - already_seen_sirens(sirens)

    for p in partials:
        dirs = p.get("legal_dirigeants") or []
        if not dirs:
            continue

        # Default: take the first legal director.
        chosen_first, chosen_last, chosen_role = None, None, None
        chosen_sources = ["sirene"]

        # Optional: LLM-driven decision-maker pick (great for big companies)
        if llm_decider:
            from decision_maker_llm import pick as llm_pick
            decision = llm_pick(
                company_name=p["company_name"],
                sector_hint=p.get("naf") or "",
                persona_hint=persona_role_hint or "operational decision-maker",
                legal_dirigeants=dirs,
                team_page_text=p.get("team_page_text"),
            )
            if decision and decision.get("person_first") and decision.get("person_last"):
                chosen_first = decision["person_first"]
                chosen_last = decision["person_last"]
                chosen_role = decision.get("person_role") or persona_role_hint or ""
                chosen_sources = decision.get("person_sources") or ["sirene", "llm-decider"]

        # Fallback: first legal dirigeant — prefer the cleaned first/last from
        # name_utils (handles 'DIDIER JACQUES EMMANUEL YVON VILLEMEY' → 'Didier'/'Villemey').
        if not chosen_first or not chosen_last:
            d = dirs[0]
            chosen_first = d.get("first") or ""
            chosen_last = d.get("last") or ""
            if not chosen_first or not chosen_last:
                parts = (d.get("name") or "").split()
                if len(parts) < 2:
                    continue
                chosen_first = chosen_first or parts[0]
                chosen_last = chosen_last or parts[-1]
            chosen_role = persona_role_hint or d.get("role") or ""

        lead = finalize_lead(
            p,
            person_first=chosen_first,
            person_last=chosen_last,
            person_role=chosen_role,
            person_sources=chosen_sources,
        )
        setattr(lead, "is_new_lead", lead.company_siren in new_sirens)
        leads.append(lead)

    # 3a-bis. Self-critique multi-pass: retry dropped leads with relaxed thresholds.
    if retry_dropped:
        retried = 0
        for lead in leads:
            if not lead.dropped:
                continue
            # Re-evaluate with a relaxed contact threshold (30 vs 50)
            lead.dropped = False
            lead.drop_reason = None
            lead.evaluate(min_person_conf=60, min_contact_conf=30)
            if not lead.dropped:
                retried += 1
        if retried:
            print(f"[Self-critique] Recovered {retried} leads with relaxed thresholds")

    # 3b. ICP scoring (optional)
    if icp:
        from icp import annotate_leads
        annotate_leads(leads, icp)
        print(f"[ICP] '{icp.get('name','?')}' applied. Top score: "
              f"{max((l.icp_score for l in leads), default=0)}")

    # 3c. Persist to lead_store (dedup history + ICP score saved)
    n_new, n_existing = upsert_leads(leads, campaign_id=campaign_id)
    print(f"[Store] +{n_new} new leads, {n_existing} re-seen "
          f"(campaign_id={campaign_id})")

    # 3d. Optional cold email generation (Haiku, ~$0.0015/lead, FR personalized)
    if generate_emails:
        from cold_email import generate_for_leads
        kept_for_email = [l for l in leads if not l.dropped]
        emails = generate_for_leads(
            kept_for_email,
            sender_offer=sender_offer,
            sender_company=sender_company,
        )
        # Attach the generated email body to the lead model as extra fields so
        # the XLSX export picks them up.
        for l in kept_for_email:
            key = l.company_siren or l.company_name
            ce = emails.get(key)
            if ce:
                setattr(l, "cold_email_subject", ce.subject)
                setattr(l, "cold_email_body", ce.body)
                setattr(l, "cold_email_angle", ce.angle)
        print(f"[Cold-Email] {len(emails)}/{len(kept_for_email)} drafted")

    # 3e. Optional HubSpot push (only kept leads)
    if push_to_hubspot:
        from hubspot_client import sync_leads_to_hubspot
        created, updated, msg = sync_leads_to_hubspot([l for l in leads if not l.dropped])
        print(f"[HubSpot] {msg}")

    # 4. Export both CSV (Excel-FR) and premium XLSX side by side
    if output_stem:
        out_dir = Path("output")
        out_dir.mkdir(exist_ok=True)
        csv_path = out_dir / f"{output_stem}.csv"
        xlsx_path = out_dir / f"{output_stem}.xlsx"
        import csv as _csv
        from sheets_export import HEADERS, _row_for, _write_premium_xlsx
        # Sort kept leads: best ICP first (if scored), else by overall_score
        kept = sorted(
            [l for l in leads if not l.dropped],
            key=lambda x: (getattr(x, "icp_score", 0) or 0, x.overall_score),
            reverse=True,
        )
        rows = [HEADERS] + [_row_for(l) for l in kept]
        with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
            _csv.writer(fh, delimiter=";", quoting=_csv.QUOTE_MINIMAL).writerows(rows)
        try:
            _write_premium_xlsx(rows, xlsx_path)
        except ImportError:
            pass
        kept_path = str(csv_path)
    else:
        kept_path = export_leads([l for l in leads if not l.dropped])

    _summary(leads, kept_path, time.time() - t0)
    return kept_path


def _cli() -> None:
    p = argparse.ArgumentParser(description="Run a full prospection campaign in one call.")
    p.add_argument("--query", help="Free-text Sirene query (e.g., 'cabinet dentaire')")
    p.add_argument("--naf", help="NAF code (e.g., 86.23Z)")
    p.add_argument("--code-postal", help="Postal code (e.g., 69001)")
    p.add_argument("--departement", help="Département (e.g., 69)")
    p.add_argument("--region", help="Région code")
    p.add_argument("--tranche-effectif", dest="tranche_effectif",
                   help="Sirene size code: 00=0 emp, 01=1-2, 02=3-5, 03=6-9, "
                        "11=10-19, 12=20-49, 21=50-99, 22=100-199, 31=200-249, "
                        "32=250-499 (use 11 or 12 to target true SMBs and skip chains)")
    p.add_argument("--volume", type=int, default=10, help="Number of leads to target (default 10)")
    p.add_argument("--persona", dest="persona_role_hint",
                   help="Hint for the role label in the output (e.g., 'Gérant', 'DRH')")
    p.add_argument("--output", dest="output_stem",
                   help="Output filename stem (e.g., 'prospects-dentistes-lyon')")
    p.add_argument("--max-workers", type=int, default=8,
                   help="Parallel enrichment workers (default 8)")
    p.add_argument("--icp-preset", choices=["cavistes-paris", "palaces-paris"],
                   help="Apply a preset ICP profile and add icp_score column")
    p.add_argument("--only-new", action="store_true",
                   help="Skip companies already in lead_store (dedup across runs)")
    p.add_argument("--push-to-hubspot", action="store_true",
                   help="Sync kept leads to HubSpot CRM (needs HUBSPOT_ACCESS_TOKEN)")
    p.add_argument("--campaign-id", help="Tag this run in lead_store")
    p.add_argument("--llm-decider", action="store_true",
                   help="Use Claude (Haiku) to pick the operational decision-maker "
                        "from the team page instead of the legal director. ~$0.001/lead.")
    p.add_argument("--retry-dropped", action="store_true",
                   help="After the main pass, retry dropped leads with relaxed thresholds "
                        "(min_contact_conf=30). Self-critique multi-pass.")
    p.add_argument("--generate-emails", action="store_true",
                   help="Generate a personalized FR cold email per kept lead via "
                        "Claude Haiku. ~$0.0015/lead. Saved into the XLSX export.")
    p.add_argument("--sender-offer", default="spiritueux premium français pour cartes bars et restaurants",
                   help="One-line description of what you're selling (FR)")
    p.add_argument("--sender-company", default="Bear Brothers",
                   help="Your company name (signed/referenced in the email)")
    args = p.parse_args()

    if not any([args.query, args.naf, args.code_postal, args.departement, args.region]):
        p.error("provide at least one filter (--query / --naf / --code-postal / ...)")

    icp_profile = None
    if args.icp_preset:
        from icp import PRESET_CAVISTES_PREMIUM_PARIS, PRESET_PALACES_PARIS
        icp_profile = {"cavistes-paris": PRESET_CAVISTES_PREMIUM_PARIS,
                       "palaces-paris": PRESET_PALACES_PARIS}[args.icp_preset]

    run(
        query=args.query,
        naf=args.naf,
        code_postal=args.code_postal,
        departement=args.departement,
        region=args.region,
        tranche_effectif=args.tranche_effectif,
        volume=args.volume,
        persona_role_hint=args.persona_role_hint,
        output_stem=args.output_stem,
        max_workers=args.max_workers,
        icp=icp_profile,
        only_new=args.only_new,
        push_to_hubspot=args.push_to_hubspot,
        campaign_id=args.campaign_id,
        llm_decider=args.llm_decider,
        retry_dropped=args.retry_dropped,
        generate_emails=args.generate_emails,
        sender_offer=args.sender_offer,
        sender_company=args.sender_company,
    )


if __name__ == "__main__":
    _cli()
