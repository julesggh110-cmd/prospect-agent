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
    volume: int = 10,
    persona_role_hint: str | None = None,
    output_stem: str | None = None,
    max_workers: int = 3,
) -> str:
    """End-to-end campaign. Returns the path of the produced CSV."""
    from sirene_client import SireneClient
    from pipeline import enrich_companies_parallel, finalize_lead
    from sheets_export import export_leads

    t0 = time.time()

    # 1. Source via Sirene
    with SireneClient() as c:
        resp = c.search(
            query=query,
            naf=naf,
            code_postal=code_postal,
            departement=departement,
            region=region,
            per_page=min(volume, 25),
        )
    companies = resp.results[:volume]
    if not companies:
        print("No companies matched the query.")
        sys.exit(1)
    print(f"[Sirene] {len(companies)} companies")

    # 2. Parallel partial enrichment (Pappers + Brave + cache)
    partials = enrich_companies_parallel(companies, max_workers=max_workers)
    with_site = sum(1 for p in partials if p.get('website'))
    print(f"[Enrich] {with_site}/{len(partials)} with website")

    # 3. Finalize each lead — Phase-1 default: take the first legal director.
    leads = []
    for p in partials:
        dirs = p.get("legal_dirigeants") or []
        if not dirs:
            continue
        d = dirs[0]
        parts = d.get("name", "").split()
        if len(parts) < 2:
            continue
        first, last = parts[0], parts[-1]
        role = persona_role_hint or d.get("role") or ""
        lead = finalize_lead(
            p,
            person_first=first,
            person_last=last,
            person_role=role,
            person_sources=["sirene"],
        )
        leads.append(lead)

    # 4. Export both CSV (Excel-FR) and XLSX side by side
    if output_stem:
        # Override the default leads-<timestamp>
        out_dir = Path("output")
        out_dir.mkdir(exist_ok=True)
        csv_path = out_dir / f"{output_stem}.csv"
        xlsx_path = out_dir / f"{output_stem}.xlsx"
        import csv as _csv
        from sheets_export import HEADERS, _row_for
        rows = [HEADERS] + [_row_for(l) for l in leads if not l.dropped]
        with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
            _csv.writer(fh, delimiter=";", quoting=_csv.QUOTE_MINIMAL).writerows(rows)
        try:
            from openpyxl import Workbook
            wb = Workbook(); ws = wb.active; ws.title = "leads"
            for r in rows:
                ws.append([("" if v is None else v) for v in r])
            ws.freeze_panes = "A2"
            wb.save(xlsx_path)
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
    p.add_argument("--volume", type=int, default=10, help="Number of leads to target (default 10)")
    p.add_argument("--persona", dest="persona_role_hint",
                   help="Hint for the role label in the output (e.g., 'Gérant', 'DRH')")
    p.add_argument("--output", dest="output_stem",
                   help="Output filename stem (e.g., 'prospects-dentistes-lyon')")
    p.add_argument("--max-workers", type=int, default=3,
                   help="Parallel enrichment workers (default 3)")
    args = p.parse_args()

    if not any([args.query, args.naf, args.code_postal, args.departement, args.region]):
        p.error("provide at least one filter (--query / --naf / --code-postal / ...)")

    run(
        query=args.query,
        naf=args.naf,
        code_postal=args.code_postal,
        departement=args.departement,
        region=args.region,
        volume=args.volume,
        persona_role_hint=args.persona_role_hint,
        output_stem=args.output_stem,
        max_workers=args.max_workers,
    )


if __name__ == "__main__":
    _cli()
