"""
ICP Self-Tuner — adjust rule weights based on actual conversion data.

Inputs:
  - A baseline ICP preset (e.g. any PRESET_* from icp.py)
  - The tenant's outcome history (from lead_store)

Outputs:
  - A printed report : which rule features correlate with replies / meetings
    / won deals
  - An optional tuned preset (dict) where weights have been adjusted
  - Concrete recommendations the operator can apply by hand

The tuning uses a simple uplift formula per rule:
    uplift(rule) = positive_rate(leads_where_rule_matched) /
                    positive_rate(all_leads_with_outcome)
A rule with uplift > 1.5 should keep / boost its weight.
A rule with uplift < 0.7 should be REJECT-style or have its weight cut.

We need at least N=20 leads with outcomes to compute meaningful stats.
Below that, we print the data but refuse to suggest tuning ("not enough
signal yet").
"""
from __future__ import annotations

import json
from typing import Optional

from icp import evaluate_rule
from lead_store import outcome_stats
from triangulation import Lead, ScoredField


MIN_OUTCOMES_FOR_TUNING = 20
POSITIVE_OUTCOMES = ("replied", "meeting_booked", "closed_won")
NEGATIVE_OUTCOMES = ("bounced", "unsubscribed", "closed_lost")


def _dict_to_lead(payload: dict) -> Lead:
    """Rebuild a Lead instance from the payload_json stored in lead_store.
    Used so we can run evaluate_rule() on each historical lead.
    """
    # Strip ScoredField dicts back into ScoredField instances. They were
    # serialized via model_dump.
    sf_fields = (
        "company_linkedin", "company_instagram", "company_phone",
        "person_name", "person_role", "person_email",
        "person_phone", "person_linkedin", "person_instagram",
    )
    clean = {}
    for k, v in payload.items():
        if k in sf_fields and isinstance(v, dict):
            try:
                clean[k] = ScoredField(**v)
            except Exception:
                clean[k] = ScoredField.missing()
        else:
            clean[k] = v
    try:
        return Lead(**clean)
    except Exception:
        # Fall back to a minimal Lead with just the company_name
        return Lead(company_name=payload.get("company_name", "?"))


def analyze(icp: dict, tenant_id: Optional[str] = None) -> dict:
    """Per-rule uplift analysis of `icp` against the tenant's outcome history.

    Returns:
        {
            "n_leads_with_outcome": int,
            "n_positive": int,
            "n_negative": int,
            "positive_rate": float (0-1),
            "rules": [
                {
                    "name": str,
                    "weight": int,
                    "n_match": int,
                    "n_match_positive": int,
                    "uplift": float (positive_rate_when_matched / overall_positive_rate),
                    "verdict": "boost" | "keep" | "cut" | "needs more data",
                },
                ...
            ],
            "ready_for_tuning": bool,
            "recommendations": [str, ...],
        }
    """
    stats = outcome_stats(tenant_id=tenant_id)
    leads_data = stats["leads"]
    n_with_outcome = len(leads_data)
    n_positive = sum(1 for l in leads_data if l["outcome"] in POSITIVE_OUTCOMES)
    n_negative = sum(1 for l in leads_data if l["outcome"] in NEGATIVE_OUTCOMES)
    positive_rate = (n_positive / n_with_outcome) if n_with_outcome else 0.0

    rules_out = []
    recommendations = []
    ready = n_with_outcome >= MIN_OUTCOMES_FOR_TUNING

    for rule in icp.get("rules", []):
        n_match = 0
        n_match_pos = 0
        for ld in leads_data:
            try:
                lead = _dict_to_lead(ld["payload"])
                if evaluate_rule(lead, rule):
                    n_match += 1
                    if ld["outcome"] in POSITIVE_OUTCOMES:
                        n_match_pos += 1
            except Exception:
                continue
        if n_match == 0:
            uplift = None
            verdict = "needs more data"
        else:
            match_pos_rate = n_match_pos / n_match
            uplift = (match_pos_rate / positive_rate) if positive_rate > 0 else None
            if uplift is None or n_match < 5:
                verdict = "needs more data"
            elif uplift >= 1.5:
                verdict = "boost"
            elif uplift >= 0.8:
                verdict = "keep"
            else:
                verdict = "cut"
        rules_out.append({
            "name": rule.get("name", "?"),
            "weight": rule.get("weight", 0),
            "n_match": n_match,
            "n_match_positive": n_match_pos,
            "uplift": uplift,
            "verdict": verdict,
        })
        if ready and verdict == "boost":
            recommendations.append(
                f"BOOST '{rule.get('name')}' from weight {rule.get('weight')} to "
                f"{min(40, int(rule.get('weight', 0) * 1.5))} "
                f"(uplift {uplift:.2f}x vs avg)."
            )
        elif ready and verdict == "cut":
            recommendations.append(
                f"CUT '{rule.get('name')}' from weight {rule.get('weight')} to "
                f"{max(0, int(rule.get('weight', 0) * 0.5))} "
                f"(uplift {uplift:.2f}x — under-performing)."
            )

    return {
        "n_leads_with_outcome": n_with_outcome,
        "n_positive": n_positive,
        "n_negative": n_negative,
        "positive_rate": round(positive_rate, 3),
        "rules": rules_out,
        "ready_for_tuning": ready,
        "min_leads_required": MIN_OUTCOMES_FOR_TUNING,
        "recommendations": recommendations,
    }


def apply_recommendations(icp: dict, tenant_id: Optional[str] = None) -> dict:
    """Return a NEW ICP dict with rule weights adjusted per `analyze()`.

    Does not mutate the input. Only applies adjustments when ready_for_tuning
    is True.
    """
    rep = analyze(icp, tenant_id=tenant_id)
    if not rep["ready_for_tuning"]:
        return dict(icp)  # unchanged copy
    new_rules = []
    verdict_by_name = {r["name"]: r for r in rep["rules"]}
    for rule in icp.get("rules", []):
        new_rule = dict(rule)
        v = verdict_by_name.get(rule.get("name", "?"), {})
        if v.get("verdict") == "boost":
            new_rule["weight"] = min(40, int(rule.get("weight", 0) * 1.5))
            new_rule["_tuned"] = f"boosted from {rule.get('weight', 0)}"
        elif v.get("verdict") == "cut":
            new_rule["weight"] = max(0, int(rule.get("weight", 0) * 0.5))
            new_rule["_tuned"] = f"cut from {rule.get('weight', 0)}"
        new_rules.append(new_rule)
    out = dict(icp)
    out["rules"] = new_rules
    out["name"] = icp.get("name", "?") + " (tuned)"
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _format_report(rep: dict, icp_name: str = "?") -> str:
    lines = []
    lines.append("=" * 80)
    lines.append(f"ICP TUNING REPORT — {icp_name}")
    lines.append(f"Leads with outcome: {rep['n_leads_with_outcome']} "
                  f"(need ≥{rep['min_leads_required']} to tune)")
    lines.append(f"Positive: {rep['n_positive']} "
                  f"({rep['positive_rate']*100:.1f}% conversion)")
    lines.append(f"Negative: {rep['n_negative']}")
    if not rep["ready_for_tuning"]:
        lines.append("\nNot enough signal yet — keep running campaigns and marking outcomes.")
    lines.append("=" * 80)
    lines.append(f"{'Rule':<40} {'Weight':>7} {'Match':>6} {'+':>4} {'Uplift':>8} {'Verdict':>15}")
    lines.append("-" * 80)
    for r in rep["rules"]:
        uplift = "—" if r["uplift"] is None else f"{r['uplift']:.2f}x"
        lines.append(
            f"{r['name'][:40]:<40} {r['weight']:>7} {r['n_match']:>6} "
            f"{r['n_match_positive']:>4} {uplift:>8} {r['verdict']:>15}"
        )
    if rep["recommendations"]:
        lines.append("\nRECOMMENDATIONS:")
        for rec in rep["recommendations"]:
            lines.append(f"  • {rec}")
    return "\n".join(lines)


def _cli() -> None:
    import argparse
    p = argparse.ArgumentParser(description="ICP self-tuning based on outcomes")
    p.add_argument(
        "preset",
        choices=["cavistes-paris", "palaces-paris", "chr-alcool-compatible",
                 "pme-formation-qse", "eti-b2b-formation"],
        help="Which baseline ICP preset to analyze",
    )
    p.add_argument("--json", action="store_true",
                   help="Output JSON instead of human-readable table")
    p.add_argument("--apply", action="store_true",
                   help="Print the tuned ICP dict (ready to use)")
    args = p.parse_args()

    from icp import (
        PRESET_CAVISTES_PREMIUM_PARIS,
        PRESET_CHR_ALCOOL_COMPATIBLE,
        PRESET_ETI_B2B_FORMATION,
        PRESET_PALACES_PARIS,
        PRESET_PME_FORMATION_QSE,
    )
    icp = {
        "cavistes-paris": PRESET_CAVISTES_PREMIUM_PARIS,
        "palaces-paris": PRESET_PALACES_PARIS,
        "chr-alcool-compatible": PRESET_CHR_ALCOOL_COMPATIBLE,
        "pme-formation-qse": PRESET_PME_FORMATION_QSE,
        "eti-b2b-formation": PRESET_ETI_B2B_FORMATION,
    }[args.preset]

    rep = analyze(icp)
    if args.apply:
        tuned = apply_recommendations(icp)
        print(json.dumps(tuned, indent=2, ensure_ascii=False))
        return
    if args.json:
        print(json.dumps(rep, indent=2, ensure_ascii=False))
    else:
        print(_format_report(rep, icp_name=icp.get("name", args.preset)))


if __name__ == "__main__":
    _cli()
