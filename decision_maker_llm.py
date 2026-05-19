"""
Decision-maker LLM picker — for big companies where the Sirene legal director
is NOT the operational buyer.

Examples where this matters:
- Hotel chain → Sirene says "Dorchester Holding Ltd CEO" but the buyer at the
  Plaza Athénée is the F&B Manager of that specific property.
- SaaS B2B → Sirene says "Président SAS" but the actual buyer of your SDR tool
  is the VP Sales.
- Distillerie B2B → Sirene says "Président" but the buyer of bottling equipment
  is the Directeur des opérations.

The LLM reads:
- The team page text (if scraped)
- The list of legal dirigeants from Sirene
- The persona we're looking for (passed in by the campaign)

And returns: first name, last name, role, confidence, reasoning.

Cost: 1 Claude Haiku call per company. ~$0.001 each. Negligible at scale.

Activated by:
- `run_campaign.py --llm-decider`
- or programmatically: `decision_maker_llm.pick(...)` then feed result into
  `pipeline.finalize_lead`.
"""
from __future__ import annotations

import json
import os
from typing import Optional

try:
    from anthropic import Anthropic  # type: ignore
except ImportError:  # pragma: no cover
    Anthropic = None  # type: ignore


def have_anthropic_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


_SYSTEM_PROMPT = """You pick the right decision-maker to contact in a B2B prospection campaign.

You receive:
- The company name + sector
- A short description of the persona the user wants to reach (role + seniority)
- The list of legal directors from the official French register (Sirene)
- (Optional) Raw text scraped from the company's team / about page

You return ONE person who is the most likely operational buyer for that persona.
Rules:
- Use ONLY names that appear in the inputs. NEVER invent a name.
- If the team page lists a person matching the persona (e.g. "VP Sales", "F&B Manager", "Directeur achats"), prefer them over the legal director.
- If the team page lists no good match AND the legal director's role matches the persona OR the company is clearly small (one-person shop), use the legal director.
- If nothing matches and you cannot find a credible person, return person_name="" — DO NOT GUESS.

Always return JSON only, no prose, no preamble. Schema:
{
  "person_first": "Marie",
  "person_last": "Dupont",
  "person_role": "VP Sales France",
  "person_sources": ["website-team-page", "sirene"],
  "confidence": 85,
  "reasoning": "She is listed on the team page as VP Sales France, which directly matches the requested persona."
}
"""


def pick(
    company_name: str,
    sector_hint: str,
    persona_hint: str,
    legal_dirigeants: list[dict],
    team_page_text: Optional[str] = None,
    model: str = "claude-haiku-4-5",
) -> Optional[dict]:
    """Ask the LLM to pick the right decision-maker. Returns None on any failure."""
    if Anthropic is None or not have_anthropic_key():
        return None

    # Cap team_page_text to keep token cost low (~5k chars = ~1.5k tokens)
    if team_page_text and len(team_page_text) > 5000:
        team_page_text = team_page_text[:5000] + "...(truncated)"

    user_msg = json.dumps(
        {
            "company_name": company_name,
            "sector": sector_hint or "unknown",
            "persona_we_want": persona_hint or "the operational decision-maker for this kind of company",
            "legal_dirigeants": legal_dirigeants[:5],
            "team_page_text": team_page_text or None,
        },
        ensure_ascii=False,
    )

    try:
        client = Anthropic()
        resp = client.messages.create(
            model=model,
            max_tokens=400,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = resp.content[0].text if resp.content else ""
        # Sometimes the model wraps in ```json fences; strip them
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].lstrip()
        return json.loads(text)
    except Exception:
        return None


def _cli() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Test LLM decision-maker picker.")
    p.add_argument("company_name")
    p.add_argument("--sector", default="")
    p.add_argument("--persona", default="")
    p.add_argument("--dirigeants", default="[]",
                   help='JSON list e.g. \'[{"name":"Jean Dupont","role":"Gérant"}]\'')
    p.add_argument("--team-text", default=None)
    args = p.parse_args()

    dirigeants = json.loads(args.dirigeants)
    res = pick(args.company_name, args.sector, args.persona, dirigeants, args.team_text)
    print(json.dumps(res, indent=2, ensure_ascii=False) if res else "(no result)")


if __name__ == "__main__":
    _cli()
