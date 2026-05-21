"""
ICP from natural language — let any user describe their ICP in French and
get a structured config the agent can execute on.

This is the GAME-CHANGER for the agent's commercializability: instead of
needing to know NAF codes by heart and write Python ICP dicts, ANY client
can say:

  "Je vends du conseil RGPD aux ETI industrielles 100-500 employés en
   Auvergne-Rhône-Alpes. Mon décideur cible est le DPO ou le Directeur
   Juridique."

And the agent produces:

  {
    "naf_codes": ["20.XX", "22.XX", "25.XX", "28.XX", "29.XX"],
    "departements": ["01", "03", "07", "15", "26", "38", "42", "43", "63", "69", "73", "74"],
    "tranches_effectif": ["31", "32", "41"],
    "persona": "DPO",
    "persona_alternatives": ["Directeur Juridique", "RSSI", "DSI"],
    "exclude_keywords": ["filiale", "groupe étranger"],
    "icp_rules": [...]   # icp.py-compatible
  }

Cost: 1 Claude Haiku call per setup (~$0.001). Free if user uses Claude
Code Max sub (the LLM itself is the agent here).
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

try:
    from anthropic import Anthropic  # type: ignore
except ImportError:
    Anthropic = None  # type: ignore


_SYSTEM_PROMPT = """Tu es un expert en codification INSEE NAF et géographie française.

Ton rôle : transformer une description ICP en français langage naturel
(faite par un commercial / fondateur) en une configuration JSON exploitable
par le prospect-agent.

L'utilisateur va te décrire :
- Ce qu'il vend (offre, secteur)
- À qui il vend (taille, secteur cible, fonction du décideur)
- Où (géographie : ville, dept, région, France entière)
- Toute spécificité (industrie, B2B vs B2C, premium vs entry-level)

TU RETOURNES UN JSON STRICT avec :

{
  "naf_codes": ["XX.XXZ", ...],            // 1-15 codes NAF/APE pertinents
  "naf_rationale": "...",                  // 1 phrase expliquant ton choix
  "departements": ["XX", ...],             // 2-digit, FR mainland; vide = France entière
  "tranches_effectif": ["XX", ...],        // codes Sirene : 00,01,02,03,11,12,21,22,31,32,41,42,51,52,53
  "size_rationale": "...",                 // 1 phrase
  "persona": "...",                        // rôle principal (singulier)
  "persona_alternatives": ["...", "..."],  // 2-4 alternatives
  "junk_keywords_extra": ["..."],          // mots-clés exclusion supplémentaires
                                            // (en plus des syndicat/mutuelle/etc. déjà filtrés)
  "sector_fit_keywords": ["..."],          // mots positifs à booster
  "is_b2c": false,                         // true si le client final est consommateur (CHR, retail, sante)
  "gmb_relevance": "high|medium|low",      // pertinence des signaux Google My Business
  "sample_query_cli": "..."                // commande CLI prête à coller
}

EXEMPLES de codes Sirene tranches :
  00 = 0 emp, 01 = 1-2, 02 = 3-5, 03 = 6-9
  11 = 10-19, 12 = 20-49 (TPE)
  21 = 50-99, 22 = 100-199 (PME)
  31 = 200-249, 32 = 250-499 (ETI)
  41 = 500-999, 42 = 1000-1999, 51 = 2000-4999, 52 = 5000-9999, 53 = 10000+

CODES NAF clés (extraits) :
- 56.10A/B/C : restaurants / café-bar
- 47.XX : commerce détail (47.25Z = caviste, 47.71Z = vêtements)
- 55.10Z : hôtellerie
- 70.22Z : conseil pour affaires
- 78.10Z : agences placement
- 70.21Z : RP / communication
- 82.99Z : services aux entreprises divers
- 62.01Z : programmation informatique
- 62.02A : conseil systèmes informatiques
- 63.XX : services information (SaaS, hosting)
- 86.21Z / 86.22A : médecins
- 87.10A/B/C : EHPAD
- 41.XX-43.XX : BTP
- 10.XX-33.XX : industrie manufacturière

RÉPONDS UNIQUEMENT EN JSON valide, sans commentaire markdown ni ```json."""


def generate_icp_from_description(
    description: str,
    *,
    model: str = "claude-haiku-4-5",
) -> Optional[dict]:
    """Take a French ICP description, return a structured config dict.

    None on failure. Cost: ~$0.001 per call.
    """
    if Anthropic is None or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    if not description or len(description.strip()) < 20:
        return None
    try:
        client = Anthropic()
        resp = client.messages.create(
            model=model,
            max_tokens=1500,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": description.strip()}],
        )
        text = (resp.content[0].text if resp.content else "").strip()
        # Strip code fences if present
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].lstrip()
        try:
            from quotas import mark_used
            mark_used("anthropic")
        except Exception:
            pass
        return json.loads(text)
    except Exception:
        return None


def to_run_campaign_args(icp: dict) -> dict:
    """Translate an icp dict (from generate_icp_from_description) into kwargs
    suitable for run_campaign.run(). Caller can splat ** these.
    """
    if not icp:
        return {}
    out = {
        "naf": ",".join(icp.get("naf_codes") or []) or None,
        "departement": ",".join(icp.get("departements") or []) or None,
        "tranche_effectif": ",".join(icp.get("tranches_effectif") or []) or None,
        "persona_role_hint": icp.get("persona") or None,
    }
    # Filter None
    return {k: v for k, v in out.items() if v}


def _cli() -> None:
    import argparse
    p = argparse.ArgumentParser(
        description="ICP from natural language description"
    )
    p.add_argument("description",
                   help="French ICP description in quotes, e.g. "
                        "'Je vends du conseil RGPD aux ETI industrielles AURA'")
    p.add_argument("--apply", action="store_true",
                   help="Also print the run_campaign CLI command")
    args = p.parse_args()

    if Anthropic is None:
        print("ERROR: anthropic SDK not installed.")
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set in env.")
        return

    icp = generate_icp_from_description(args.description)
    if not icp:
        print("(generation failed)")
        return
    print(json.dumps(icp, indent=2, ensure_ascii=False))
    if args.apply:
        kwargs = to_run_campaign_args(icp)
        cmd = "python run_campaign.py"
        for k, v in kwargs.items():
            cli_flag = "--" + k.replace("_", "-").replace("persona-role-hint", "persona")
            cmd += f' {cli_flag} "{v}"'
        print("\n# Ready-to-run:")
        print(cmd + " --volume 20 --max-candidates 200 --output my-icp-run --retry-dropped")


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    _cli()
