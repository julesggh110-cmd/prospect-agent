"""
Cold email generator — turns a lead into an actionable outreach message.

The agent's biggest value-add: don't just deliver "30 contacts" → deliver
"30 personalized cold emails ready to send". The salesperson clicks
"Copy → Send" and that's it.

How it works:
1. Read the lead (company, sector, decision-maker, website if any).
2. Briefly scrape the company's homepage / about page to ground the email
   in REAL context (no generic fluff).
3. Call Claude Haiku with a focused prompt: write a 2-3 sentence opener
   that demonstrates "I actually researched you", then a clear CTA.
4. Return: subject line + body, both in French (configurable).

Personalization principles:
- Reference something SPECIFIC about their boîte (recent menu, location, story)
- Don't lie or invent → if no specific context, fall back to a clean generic
- Short. 80-120 words. People delete walls of text.
- Mobile-friendly (60-char subject, no bullet hell)
- Ask for ONE thing (15-min call), not 5

Cost: 1 Haiku call per lead ~$0.0015. For 100 leads = $0.15.

Auth: needs ANTHROPIC_API_KEY (already in env for the project).
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

import httpx
from selectolax.parser import HTMLParser

try:
    from anthropic import Anthropic  # type: ignore
except ImportError:  # pragma: no cover
    Anthropic = None  # type: ignore

DEFAULT_TIMEOUT = 8.0
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class ColdEmail:
    """Output: a ready-to-send cold email."""
    subject: str
    body: str
    angle: str             # 1-line summary of why this opener works
    context_signals: list[str]  # what we found that we leveraged
    sender_offer: str      # what your value prop was framed as


# ---------------------------------------------------------------------------
# Quick context scrape — feed the LLM something real
# ---------------------------------------------------------------------------

def _fetch_context(website: Optional[str]) -> str:
    """Pull a short, factual context blob from the company website."""
    if not website:
        return ""
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT,
                          headers={"User-Agent": USER_AGENT},
                          follow_redirects=True, verify=False) as c:
            r = c.get(website)
            if r.status_code >= 400:
                return ""
            tree = HTMLParser(r.text)
            # Extract meta description + first paragraph + first H1
            meta = ""
            for tag in tree.css("meta[name='description']") + tree.css("meta[property='og:description']"):
                content = tag.attributes.get("content") or ""
                if content and len(content) > len(meta):
                    meta = content
            h1 = ""
            h1_node = tree.css_first("h1")
            if h1_node:
                h1 = h1_node.text(strip=True)
            # First substantive paragraph
            first_p = ""
            for p in tree.css("p"):
                t = p.text(strip=True)
                if 50 < len(t) < 400:
                    first_p = t
                    break
            ctx = " · ".join(x for x in [h1, meta, first_p] if x)
            return ctx[:1200]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Cold email generation
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_FR = """Tu es un commercial B2B français spécialiste de l'outreach personnalisé.

Tu rédiges un cold email court (80-120 mots) en français pour un décideur d'une PME française.
Tu reçois :
- Le profil du décideur (nom, rôle, entreprise, secteur)
- Le contexte réel scrapé de leur site (si dispo)
- L'offre du vendeur (ce qu'il vend et pourquoi c'est pertinent pour cette boîte)

Règles non-négociables :
- Tu écris en FRANÇAIS naturel, pas anglais, pas robot.
- Tu personnalises avec au moins UN détail spécifique de leur site/secteur (jamais générique "j'ai vu votre site").
- Tu N'INVENTES JAMAIS de fait sur la boîte. Si le contexte est vide, écris un message clean sans fausse personnalisation.
- Maximum 120 mots dans le body.
- Subject line max 50 caractères, sans CAPS ni emojis ni "[URGENT]" ni clickbait.
- Tu finis par UNE seule demande claire : un RDV téléphonique de 15 min, ou un essai produit, etc.
- Pas de "Bonjour," sec — utilise "Bonjour {Prénom},".
- Pas de signature : on ajoutera ça côté outil.

Tu retournes UN JSON strict :
{
  "subject": "...",
  "body": "Bonjour Prénom,\\n\\n...\\n\\n...",
  "angle": "1 phrase sur pourquoi cette accroche marche",
  "context_signals": ["signal 1 utilisé", "signal 2 utilisé"],
  "sender_offer": "la value prop reformulée pour ce lead"
}
"""


def generate_cold_email(
    *,
    person_first: str,
    person_last: str,
    person_role: str,
    company_name: str,
    company_sector: str,
    company_city: Optional[str] = None,
    company_website: Optional[str] = None,
    sender_offer: str = "spiritueux premium français pour cartes bars et restaurants",
    sender_company: str = "Bear Brothers",
    model: str = "claude-haiku-4-5",
) -> Optional[ColdEmail]:
    """Generate a personalized cold email for one lead. None on failure.

    `sender_offer` is the one-line description of what you're selling.
    Default to Bear Brothers (spirits premium) — override per campaign.
    """
    if Anthropic is None or not os.environ.get("ANTHROPIC_API_KEY"):
        return None

    context = _fetch_context(company_website)

    user_payload = json.dumps(
        {
            "decideur": {
                "prenom": person_first,
                "nom": person_last,
                "role": person_role or "Décideur",
            },
            "entreprise": {
                "nom": company_name,
                "secteur": company_sector or "?",
                "ville": company_city or "?",
                "site_web": company_website or None,
                "contexte_scraping": context or None,
            },
            "vendeur": {
                "nom_entreprise": sender_company,
                "offre": sender_offer,
            },
        },
        ensure_ascii=False,
    )

    try:
        client = Anthropic()
        resp = client.messages.create(
            model=model,
            max_tokens=500,
            system=_SYSTEM_PROMPT_FR,
            messages=[{"role": "user", "content": user_payload}],
        )
        text = resp.content[0].text if resp.content else ""
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].lstrip()
        data = json.loads(text)
        return ColdEmail(
            subject=(data.get("subject") or "").strip()[:60],
            body=(data.get("body") or "").strip(),
            angle=(data.get("angle") or "").strip(),
            context_signals=list(data.get("context_signals") or []),
            sender_offer=(data.get("sender_offer") or sender_offer),
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Batch generation for a list of Lead objects (returns dict by SIREN)
# ---------------------------------------------------------------------------

def generate_for_leads(
    leads: list,
    *,
    sender_offer: str = "spiritueux premium français pour cartes bars et restaurants",
    sender_company: str = "Bear Brothers",
) -> dict[str, ColdEmail]:
    """Generate cold emails for a batch of Lead objects. Returns {siren: ColdEmail}.

    Loudly warns once if Anthropic auth is missing — silent failure here means
    the user thinks "--generate-emails" worked when nothing was produced.
    """
    if Anthropic is None:
        print("[Cold-Email] anthropic SDK not installed; skipping. Run `pip install anthropic`.")
        return {}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[Cold-Email] ANTHROPIC_API_KEY not set in env; skipping. "
              "Add it to your .env, or run from a shell that has it.")
        return {}
    out: dict[str, ColdEmail] = {}
    for lead in leads:
        if getattr(lead, "dropped", False):
            continue
        if not lead.person_name.value:
            continue
        parts = lead.person_name.value.split()
        if len(parts) < 2:
            continue
        first = parts[0]
        last = parts[-1]
        email = generate_cold_email(
            person_first=first,
            person_last=last,
            person_role=lead.person_role.value or "",
            company_name=lead.company_name,
            company_sector=lead.company_naf_label or lead.company_naf or "",
            company_city=lead.company_city,
            company_website=lead.company_website,
            sender_offer=sender_offer,
            sender_company=sender_company,
        )
        if email:
            out[lead.company_siren or lead.company_name] = email
    return out


def _cli() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Generate one cold email for a manual test.")
    p.add_argument("--first", required=True)
    p.add_argument("--last", required=True)
    p.add_argument("--role", default="Gérant")
    p.add_argument("--company", required=True)
    p.add_argument("--sector", default="Restauration")
    p.add_argument("--city")
    p.add_argument("--website")
    p.add_argument("--offer", default="spiritueux premium français pour cartes bars et restaurants")
    p.add_argument("--sender", default="Bear Brothers")
    args = p.parse_args()
    email = generate_cold_email(
        person_first=args.first, person_last=args.last,
        person_role=args.role, company_name=args.company,
        company_sector=args.sector, company_city=args.city,
        company_website=args.website,
        sender_offer=args.offer, sender_company=args.sender,
    )
    if not email:
        print("(generation failed — check ANTHROPIC_API_KEY)")
        return
    print(f"SUBJECT: {email.subject}")
    print()
    print(email.body)
    print()
    print(f"--- Angle: {email.angle}")
    print(f"--- Signals: {', '.join(email.context_signals) or 'generic'}")


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    _cli()
