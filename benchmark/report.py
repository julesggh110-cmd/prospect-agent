"""
Rendering helpers for benchmark results — pretty CLI tables + Markdown export.

Both coverage.py and precision.py call into here so the look-and-feel is
consistent.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table


console = Console()


def _color(pct: float) -> str:
    """Color a percentage based on standard 'red/yellow/green' bands."""
    if pct >= 70:
        return "bold green"
    if pct >= 40:
        return "yellow"
    return "red"


def render_coverage_table(rows: list[dict], *, total: int) -> None:
    """Render the per-field coverage table.

    `rows` items: {field, with_value, with_high_conf, by_source: {name: count}}.
    """
    t = Table(title=f"Coverage benchmark — {total} companies", show_lines=False)
    t.add_column("Field")
    t.add_column("Has value", justify="right")
    t.add_column("Coverage %", justify="right")
    t.add_column("Conf ≥ 60", justify="right")
    t.add_column("High-conf %", justify="right")
    t.add_column("Top sources", justify="left")
    for r in rows:
        pct = 100 * r["with_value"] / total if total else 0
        pct_hi = 100 * r["with_high_conf"] / total if total else 0
        sources = r.get("by_source") or {}
        top = ", ".join(f"{k}({v})" for k, v in sorted(sources.items(), key=lambda kv: -kv[1])[:3])
        t.add_row(
            r["field"],
            str(r["with_value"]),
            f"[{_color(pct)}]{pct:.0f}%[/]",
            str(r["with_high_conf"]),
            f"[{_color(pct_hi)}]{pct_hi:.0f}%[/]",
            top or "—",
        )
    console.print(t)


def render_precision_table(rows: list[dict]) -> None:
    """Render the per-field precision table.

    `rows` items: {field, n_checked, n_match, n_mismatch, n_missing_in_agent,
                   n_missing_in_truth, precision, recall}.
    """
    t = Table(title="Precision benchmark — agent vs golden truth", show_lines=False)
    t.add_column("Field")
    t.add_column("Checked", justify="right")
    t.add_column("Match", justify="right")
    t.add_column("Mismatch", justify="right")
    t.add_column("Missing (agent)", justify="right")
    t.add_column("Precision %", justify="right")
    t.add_column("Recall %", justify="right")
    for r in rows:
        prec = r.get("precision", 0)
        rec = r.get("recall", 0)
        t.add_row(
            r["field"],
            str(r["n_checked"]),
            str(r["n_match"]),
            str(r["n_mismatch"]),
            str(r["n_missing_in_agent"]),
            f"[{_color(prec)}]{prec:.0f}%[/]" if r["n_checked"] else "—",
            f"[{_color(rec)}]{rec:.0f}%[/]" if r["n_checked"] else "—",
        )
    console.print(t)


def render_per_company_breakdown(per_company: list[dict]) -> None:
    """One row per company showing match/mismatch/missing across all fields."""
    t = Table(title="Per-company breakdown", show_lines=False)
    t.add_column("Company")
    t.add_column("City")
    t.add_column("✓ Match", justify="right")
    t.add_column("✗ Mismatch", justify="right")
    t.add_column("- Missing", justify="right")
    t.add_column("Score %", justify="right")
    for r in per_company:
        score = r.get("score", 0)
        t.add_row(
            r["company"][:35],
            r.get("city", "") or "",
            str(r["matches"]),
            str(r["mismatches"]),
            str(r["missing"]),
            f"[{_color(score)}]{score:.0f}%[/]",
        )
    console.print(t)


def write_markdown_report(
    out_path: Path,
    *,
    title: str,
    summary: str,
    coverage_rows: Optional[list[dict]] = None,
    precision_rows: Optional[list[dict]] = None,
    per_company: Optional[list[dict]] = None,
    total: int = 0,
) -> None:
    """Persist a clean Markdown report to disk."""
    lines = [f"# {title}", "", summary, ""]

    if coverage_rows:
        lines += [
            f"## Coverage benchmark — {total} companies",
            "",
            "| Field | Has value | Coverage % | Conf ≥ 60 | High-conf % | Top sources |",
            "|---|---:|---:|---:|---:|---|",
        ]
        for r in coverage_rows:
            pct = 100 * r["with_value"] / total if total else 0
            pct_hi = 100 * r["with_high_conf"] / total if total else 0
            sources = r.get("by_source") or {}
            top = ", ".join(f"{k}({v})" for k, v in sorted(sources.items(), key=lambda kv: -kv[1])[:3])
            lines.append(
                f"| {r['field']} | {r['with_value']} | {pct:.0f}% | "
                f"{r['with_high_conf']} | {pct_hi:.0f}% | {top or '—'} |"
            )
        lines.append("")

    if precision_rows:
        lines += [
            "## Precision benchmark — agent vs golden truth",
            "",
            "| Field | Checked | Match | Mismatch | Missing (agent) | Precision % | Recall % |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for r in precision_rows:
            prec = r.get("precision", 0)
            rec = r.get("recall", 0)
            lines.append(
                f"| {r['field']} | {r['n_checked']} | {r['n_match']} | "
                f"{r['n_mismatch']} | {r['n_missing_in_agent']} | "
                f"{prec:.0f}% | {rec:.0f}% |"
            )
        lines.append("")

    if per_company:
        lines += [
            "## Per-company breakdown",
            "",
            "| Company | City | Match | Mismatch | Missing | Score % |",
            "|---|---|---:|---:|---:|---:|",
        ]
        for r in per_company:
            lines.append(
                f"| {r['company']} | {r.get('city', '') or ''} | "
                f"{r['matches']} | {r['mismatches']} | {r['missing']} | "
                f"{r.get('score', 0):.0f}% |"
            )
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"[dim]Report saved to:[/] {out_path}")
