"""
Coverage benchmark — measure hit-rate per field on random Sirene companies.

No ground-truth needed: this answers "what fraction of leads come back with
a website, a phone, a personal email, a LinkedIn, a verified decision-maker?"
plus a breakdown of WHICH source (Pappers / DDG / OSM / domain_guess / website
scrape) provided the value.

The point: prove the agent EXTRACTS info reliably from a representative
sample. Then run `precision.py` on a labelled sample to prove the info is
CORRECT.

Usage:
    python -m benchmark.coverage --naf 56.10A --departement 31 \\
        --tranche-effectif 11 --volume 30 --output bench-toulouse-chr.md
"""
from __future__ import annotations

import argparse
import sys
import time
import warnings
from pathlib import Path
from typing import Optional

# Allow running as a script (`python benchmark/coverage.py ...`) by adding the
# project root to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

warnings.filterwarnings("ignore")
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from rich.console import Console

from benchmark.report import render_coverage_table, write_markdown_report

console = Console()


# Fields we care about. Each entry is (label, getter, source_getter).
# `getter(lead, partial)` returns the value to inspect (may be None / empty).
# `source_getter(lead, partial)` returns a string identifying the source bucket
# (e.g. "pappers", "ddg-search", "osm", "domain-guess").
def _website_source(lead, partial) -> Optional[str]:
    if not lead.company_website:
        return None
    # We tagged the source in the partial dict via `source_for_website` if we did it.
    # Otherwise we inspect the URL host or the cache.
    we = partial.get("web_enrichment") or {}
    if partial.get("company_official_email"):  # Pappers gave both
        # heuristic: if Pappers gave the email we likely also got the website
        pass
    # Fall back to broad bucket: we just say "found"
    return partial.get("website_source") or "unknown"


def _bucket_phone_source(lead) -> Optional[str]:
    if not lead.company_phone.value:
        return None
    srcs = lead.company_phone.sources or []
    for s in srcs:
        sl = s.lower()
        if "pappers" in sl: return "pappers"
        if "osm" in sl: return "osm"
        if "pagesjaunes" in sl: return "pagesjaunes"
        if any(host in sl for host in ("http", ".fr", ".com", "website")):
            return "website-scrape"
    return srcs[0] if srcs else "unknown"


def _bucket_url_source(field) -> Optional[str]:
    if not field.value:
        return None
    for s in field.sources or []:
        sl = s.lower()
        if "osm" in sl: return "osm"
        if "search" in sl or "ddg" in sl: return "search"
        if any(h in sl for h in ("http", ".fr", ".com", "website")):
            return "website-scrape"
    return (field.sources or ["unknown"])[0]


def _bucket_email_source(lead) -> Optional[str]:
    if not lead.person_email.value:
        return None
    for s in lead.person_email.sources or []:
        sl = s.lower()
        if "smtp-deliverable" in sl: return "smtp-deliverable"
        if "website-personal" in sl: return "website-personal"
        if "pattern" in sl: return "pattern-guess"
    return "unknown"


def run(
    *,
    query: Optional[str] = None,
    naf: Optional[str] = None,
    code_postal: Optional[str] = None,
    departement: Optional[str] = None,
    tranche_effectif: Optional[str] = None,
    volume: int = 30,
    max_workers: int = 6,
    persona_role_hint: str = "Gérant",
    output_md: Optional[Path] = None,
) -> dict:
    from sirene_client import SireneClient
    from pipeline import enrich_companies_parallel, finalize_lead

    t0 = time.time()

    with SireneClient() as c:
        resp = c.search(
            query=query, naf=naf, code_postal=code_postal,
            departement=departement, tranche_effectif=tranche_effectif,
            per_page=min(volume, 25),
        )
    companies = resp.results[:volume]
    if not companies:
        console.print("[red]No companies matched.[/red]")
        sys.exit(1)
    console.print(f"[dim]Sirene returned {len(companies)} companies[/dim]")

    partials = enrich_companies_parallel(companies, max_workers=max_workers)

    leads = []
    for p in partials:
        dirs = p.get("legal_dirigeants") or []
        if not dirs:
            continue
        d = dirs[0]
        first = d.get("first") or ""
        last = d.get("last") or ""
        if not first or not last:
            parts = (d.get("name") or "").split()
            if len(parts) < 2:
                continue
            first = first or parts[0]
            last = last or parts[-1]
        lead = finalize_lead(
            p,
            person_first=first,
            person_last=last,
            person_role=persona_role_hint or d.get("role") or "",
            person_sources=["sirene"],
        )
        leads.append((lead, p))

    total = len(leads)
    if total == 0:
        console.print("[red]No leads built — every partial was missing dirigeants[/red]")
        sys.exit(1)

    # Build per-field counters
    field_specs = [
        ("company_website",   lambda l, p: l.company_website,
                              lambda l, p: _website_source(l, p) if l.company_website else None),
        ("company_phone",     lambda l, p: l.company_phone.value,
                              lambda l, p: _bucket_phone_source(l)),
        ("company_email",     lambda l, p: l.company_email,
                              lambda l, p: "found" if l.company_email else None),
        ("company_linkedin",  lambda l, p: l.company_linkedin.value,
                              lambda l, p: _bucket_url_source(l.company_linkedin)),
        ("company_instagram", lambda l, p: l.company_instagram.value,
                              lambda l, p: _bucket_url_source(l.company_instagram)),
        ("person_name",       lambda l, p: l.person_name.value,
                              lambda l, p: "sirene" if l.person_name.value else None),
        ("person_email",      lambda l, p: l.person_email.value,
                              lambda l, p: _bucket_email_source(l)),
        ("person_phone",      lambda l, p: l.person_phone.value,
                              lambda l, p: "mobile-finder" if l.person_phone.value else None),
        ("person_linkedin",   lambda l, p: l.person_linkedin.value,
                              lambda l, p: _bucket_url_source(l.person_linkedin)),
    ]

    rows = []
    for field, getter, src_getter in field_specs:
        with_value = 0
        with_high_conf = 0
        by_source: dict[str, int] = {}
        for lead, partial in leads:
            v = getter(lead, partial)
            if v:
                with_value += 1
                src = src_getter(lead, partial)
                if src:
                    by_source[src] = by_source.get(src, 0) + 1
                # Confidence (only ScoredField has it)
                attr = getattr(lead, field, None)
                conf = getattr(attr, "confidence", 100) if attr else 100
                if conf >= 60:
                    with_high_conf += 1
        rows.append({
            "field": field,
            "with_value": with_value,
            "with_high_conf": with_high_conf,
            "by_source": by_source,
        })

    # Lead-level stats: kept vs dropped
    kept = sum(1 for l, _ in leads if not l.dropped)
    dropped = total - kept

    elapsed = time.time() - t0
    console.print()
    console.print(f"[bold]Done in {elapsed:.1f}s[/bold] · {kept}/{total} kept, {dropped} dropped")
    console.print()
    render_coverage_table(rows, total=total)

    if output_md:
        summary = (
            f"- Query: NAF={naf or query or '?'} · "
            f"dept={departement or code_postal or 'all'} · "
            f"tranche={tranche_effectif or 'all'}\n"
            f"- {total} companies enriched in {elapsed:.1f}s "
            f"(~{elapsed/total:.1f}s/lead)\n"
            f"- Kept: **{kept}** · Dropped: {dropped}\n"
        )
        write_markdown_report(
            output_md,
            title=f"Coverage benchmark — {total} CHR ({departement or code_postal or 'FR'})",
            summary=summary,
            coverage_rows=rows,
            total=total,
        )

    return {
        "total": total,
        "kept": kept,
        "dropped": dropped,
        "elapsed": elapsed,
        "rows": rows,
    }


def _cli() -> None:
    p = argparse.ArgumentParser(description="Coverage benchmark (hit-rate per field).")
    p.add_argument("--query", help="Free-text Sirene query")
    p.add_argument("--naf", help="NAF code (e.g. 56.10A for restaurants)")
    p.add_argument("--code-postal", help="Postal code")
    p.add_argument("--departement", help="Département (e.g. 31)")
    p.add_argument("--tranche-effectif", help="Sirene size code (e.g. 11 for 10-19 emp)")
    p.add_argument("--volume", type=int, default=30, help="Number of companies (default 30)")
    p.add_argument("--max-workers", type=int, default=6)
    p.add_argument("--persona", default="Gérant", help="Role label hint (default Gérant)")
    p.add_argument("--output", help="Markdown report path (e.g. bench-toulouse-chr.md)")
    args = p.parse_args()

    if not any([args.query, args.naf, args.code_postal, args.departement]):
        p.error("provide at least one filter (--query / --naf / --code-postal / --departement)")

    out_md = Path(args.output) if args.output else None
    if out_md and not out_md.is_absolute():
        out_md = Path(__file__).resolve().parent / out_md

    run(
        query=args.query, naf=args.naf, code_postal=args.code_postal,
        departement=args.departement, tranche_effectif=args.tranche_effectif,
        volume=args.volume, max_workers=args.max_workers,
        persona_role_hint=args.persona, output_md=out_md,
    )


if __name__ == "__main__":
    _cli()
