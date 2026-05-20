"""
Precision benchmark — measure correctness per field vs a golden-truth CSV.

For each row in golden_truth.csv:
1. Run the agent on (name, city, siren).
2. Compare the agent's value for each field to the truth value, with
   field-aware normalization (phone strips formatting, URLs strip
   www/https/trailing slash, names case-fold + accent-strip).
3. Aggregate to precision/recall per field + an overall per-company score.

Why both precision AND recall?
- Precision: of values the agent RETURNED, how many were correct?
- Recall: of values that EXIST in the truth, how many did the agent find?
A low recall but high precision means "rarely wrong, but misses a lot".
A high recall but low precision means "finds a lot, but often wrong" — the
worst failure mode for a prospection agent (we'd rather miss than mislead).

Usage:
    python -m benchmark.precision \\
        --truth benchmark/golden_truth.csv \\
        --output bench-precision.md
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
import unicodedata
import warnings
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

warnings.filterwarnings("ignore")
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from rich.console import Console

from benchmark.report import (
    render_per_company_breakdown,
    render_precision_table,
    write_markdown_report,
)


console = Console()


# ---------------------------------------------------------------------------
# Field-aware comparators — return True if `agent` matches `truth`
# ---------------------------------------------------------------------------

def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _norm_phone(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    d = re.sub(r"\D", "", s)
    if d.startswith("33") and len(d) >= 11:
        d = "0" + d[2:]
    if not d:
        return None
    return d


def _norm_url(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = s.strip().lower()
    if not s.startswith("http"):
        s = "https://" + s
    h = (urlparse(s).hostname or "").lower()
    if h.startswith("www."):
        h = h[4:]
    return h or None


def _norm_text(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    return re.sub(r"\s+", " ", _strip_accents(s).lower().strip()) or None


def _norm_email(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = s.strip().lower()
    return s or None


def _eq_phone(a, b) -> bool:
    return _norm_phone(a) == _norm_phone(b) and _norm_phone(a) is not None


def _eq_url(a, b) -> bool:
    return _norm_url(a) == _norm_url(b) and _norm_url(a) is not None


def _eq_email(a, b) -> bool:
    return _norm_email(a) == _norm_email(b) and _norm_email(a) is not None


def _eq_name(a, b) -> bool:
    """Match if the truth name appears in the agent name (or vice versa).

    A clean French resto name like 'Bibent' should match 'Le Bibent' from the
    agent. We compare normalized substrings both ways.
    """
    na = _norm_text(a)
    nb = _norm_text(b)
    if not na or not nb:
        return False
    return na in nb or nb in na


# Field config: which agent attribute, which truth column, which comparator.
FIELDS = [
    ("website",         "company_website",        lambda l: l.company_website,                _eq_url),
    ("company_phone",   "company_phone",          lambda l: l.company_phone.value,            _eq_phone),
    ("company_email",   "company_email",          lambda l: l.company_email,                  _eq_email),
    ("person_first",    "person_first",
        lambda l: (l.person_name.value or "").split()[0] if l.person_name.value else None,
        _eq_name),
    ("person_last",     "person_last",
        lambda l: (l.person_name.value or "").split()[-1] if l.person_name.value else None,
        _eq_name),
]


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run(*, truth_csv: Path, output_md: Optional[Path] = None) -> dict:
    from sirene_client import SireneClient
    from pipeline import enrich_company_partial, finalize_lead

    if not truth_csv.exists():
        console.print(f"[red]Golden truth file not found: {truth_csv}[/red]")
        sys.exit(1)

    truth_rows: list[dict] = []
    with truth_csv.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            truth_rows.append({k.strip(): (v or "").strip() for k, v in r.items()})
    console.print(f"[dim]Loaded {len(truth_rows)} golden-truth companies[/dim]")

    per_field = {label: {"n_checked": 0, "n_match": 0, "n_mismatch": 0,
                         "n_missing_in_agent": 0, "n_missing_in_truth": 0}
                 for label, _, _, _ in FIELDS}
    per_company: list[dict] = []
    t0 = time.time()

    with SireneClient() as sclient:
        for i, t in enumerate(truth_rows, 1):
            company_name = t.get("company_name") or t.get("name") or ""
            city = t.get("city") or ""
            siren = t.get("siren") or ""
            console.print(f"[bold]\\[{i}/{len(truth_rows)}][/bold] {company_name} — {city}")

            sirene_co = None
            if siren:
                resp = sclient.search(siren)
                if resp.results:
                    sirene_co = resp.results[0]
            if sirene_co is None:
                # Fallback: name + city search
                resp = sclient.search(query=company_name, per_page=3)
                # Pick first result whose city matches if possible
                for r in resp.results:
                    if not city or (r.city or "").lower() == city.lower():
                        sirene_co = r
                        break
                if sirene_co is None and resp.results:
                    sirene_co = resp.results[0]
            if sirene_co is None:
                console.print("  [red]× could not find in Sirene[/red]")
                continue

            partial = enrich_company_partial(sirene_co)
            dirs = partial.get("legal_dirigeants") or []
            if dirs:
                d = dirs[0]
                first = d.get("first") or ""
                last = d.get("last") or ""
                if not first or not last:
                    parts = (d.get("name") or "").split()
                    if len(parts) >= 2:
                        first = first or parts[0]
                        last = last or parts[-1]
                lead = finalize_lead(
                    partial,
                    person_first=first or "",
                    person_last=last or "",
                    person_role=d.get("role") or "",
                    person_sources=["sirene"],
                )
            else:
                # No dirigeants — build a thin Lead just from the partial
                from triangulation import Lead, ScoredField
                lead = Lead(
                    company_name=partial["company_name"],
                    company_siren=partial.get("siren"),
                    company_website=partial.get("website"),
                    company_email=partial.get("company_email"),
                    company_phone=__import__("triangulation").ScoredField(**partial["company_phone"]),
                )

            # Per-company tally
            matches = 0
            mismatches = 0
            missing = 0
            for label, truth_col, getter, eq in FIELDS:
                truth_v = t.get(truth_col) or None
                agent_v = getter(lead)
                pf = per_field[label]
                if not truth_v:
                    if agent_v:
                        # Truth has nothing, agent found something — not really evaluable;
                        # log it but don't count as wrong.
                        pf["n_missing_in_truth"] += 1
                    continue
                pf["n_checked"] += 1
                if not agent_v:
                    pf["n_missing_in_agent"] += 1
                    missing += 1
                    console.print(f"    [yellow]- {label}: missing[/yellow] (truth={truth_v})")
                elif eq(agent_v, truth_v):
                    pf["n_match"] += 1
                    matches += 1
                    console.print(f"    [green]✓ {label}[/green]")
                else:
                    pf["n_mismatch"] += 1
                    mismatches += 1
                    console.print(f"    [red]✗ {label}[/red] (agent={agent_v} · truth={truth_v})")

            total = matches + mismatches + missing
            score = (100 * matches / total) if total else 0
            per_company.append({
                "company": company_name,
                "city": city,
                "matches": matches,
                "mismatches": mismatches,
                "missing": missing,
                "score": score,
            })

    # Aggregate
    rows = []
    for label, _, _, _ in FIELDS:
        pf = per_field[label]
        returned = pf["n_match"] + pf["n_mismatch"]
        prec = 100 * pf["n_match"] / returned if returned else 0
        rec = 100 * pf["n_match"] / pf["n_checked"] if pf["n_checked"] else 0
        rows.append({"field": label, **pf, "precision": prec, "recall": rec})

    elapsed = time.time() - t0
    console.print()
    console.print(f"[bold]Done in {elapsed:.1f}s on {len(truth_rows)} companies[/bold]")
    console.print()
    render_precision_table(rows)
    console.print()
    render_per_company_breakdown(per_company)

    if output_md:
        avg_score = sum(c["score"] for c in per_company) / len(per_company) if per_company else 0
        summary = (
            f"- Golden truth: `{truth_csv.name}` ({len(truth_rows)} companies)\n"
            f"- Elapsed: {elapsed:.1f}s\n"
            f"- Mean per-company score: **{avg_score:.0f}%**\n"
        )
        write_markdown_report(
            output_md,
            title="Precision benchmark — agent vs golden truth",
            summary=summary,
            precision_rows=rows,
            per_company=per_company,
        )

    return {"rows": rows, "per_company": per_company, "elapsed": elapsed}


def _cli() -> None:
    p = argparse.ArgumentParser(description="Precision benchmark (correctness vs golden truth).")
    p.add_argument("--truth", default="benchmark/golden_truth.csv",
                   help="Path to golden-truth CSV (default benchmark/golden_truth.csv)")
    p.add_argument("--output", help="Markdown report path (e.g. bench-precision.md)")
    args = p.parse_args()

    truth = Path(args.truth)
    if not truth.is_absolute():
        truth = Path(__file__).resolve().parent.parent / truth

    out_md = Path(args.output) if args.output else None
    if out_md and not out_md.is_absolute():
        out_md = Path(__file__).resolve().parent / out_md

    run(truth_csv=truth, output_md=out_md)


if __name__ == "__main__":
    _cli()
