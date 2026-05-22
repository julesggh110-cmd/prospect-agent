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
    # v0.15.0 — multi-touch sequence (J+4 follow-up + J+10 break-up)
    followup_subject: Optional[str] = None
    followup_body: Optional[str] = None
    breakup_subject: Optional[str] = None
    breakup_body: Optional[str] = None


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
- Si `entreprise.appel_offres_actif` est fourni, c'est de TRÈS LOIN le signal le plus fort : la boîte cherche ACTIVEMENT à acheter quelque chose qui matche notre offre. Tu DOIS y faire référence dès la première phrase, de manière factuelle (« Vu votre récent appel à projets sur X »). Cadre tout le pitch comme une proposition de réponse.
- Si `entreprise.signal_recrutement_site` est fourni, c'est un signal très fort (un poste tech/IA précis ouvert sur leur site). Référence-le directement (« vu que vous recrutez un AI Engineer »).
- Si `entreprise.tech_stack_pitch_hint` est fourni, utilise-le pour cadrer l'offre — c'est un signal FACTUEL détecté sur leur site (ex: "vous utilisez déjà Zapier"). N'invente jamais un outil qui n'est pas dans le hint.
- Si `entreprise.signal_recrutement_FT` est fourni, c'est un timing trigger (ex: "12 offres en 30j"). Tu peux y faire référence subtilement ("vu votre rythme de recrutement actuel") mais sans citer le chiffre brut.
- Hiérarchie d'usage des hooks : RFP > rôles tech ouverts > tech stack > FT global. N'en utilise pas plus de 2 par email pour rester sobre.
- Tu N'INVENTES JAMAIS de fait sur la boîte. Si le contexte est vide, écris un message clean sans fausse personnalisation.
- Maximum 120 mots dans le body.
- Subject line max 50 caractères, sans CAPS ni emojis ni "[URGENT]" ni clickbait.
- Tu finis par UNE seule demande claire : un RDV téléphonique de 15 min, ou un essai produit, etc.
- Pas de "Bonjour," sec — utilise "Bonjour {Prénom},".
- Pas de signature : on ajoutera ça côté outil.

CONFORMITÉ GDPR / CNIL (à inclure côté outil avant envoi — pas dans ton body) :
- L'identité du démarcheur doit être claire (signature ajoutée par l'outil).
- Un lien d'opt-out / désinscription doit figurer en pied de mail.
- La source de la donnée doit pouvoir être indiquée si le destinataire la demande.
- Pour B2B FR : la prospection est légale sans opt-in préalable SI : (a) le poste
  pro est ciblé en rapport avec ton offre, (b) un opt-out est disponible.
  Source : CNIL, lignes directrices prospection commerciale.

Tu retournes UN JSON strict :
{
  "subject": "...",
  "body": "Bonjour Prénom,\\n\\n...\\n\\n...",
  "angle": "1 phrase sur pourquoi cette accroche marche",
  "context_signals": ["signal 1 utilisé", "signal 2 utilisé"],
  "sender_offer": "la value prop reformulée pour ce lead"
}
"""


# v0.15.0 — Follow-up (J+4) et Break-up (J+10) prompts
_SYSTEM_PROMPT_FOLLOWUP_FR = """Tu es un commercial B2B FR qui rédige une RELANCE courte (J+4) à un email cold non répondu.

Le contexte original (sujet, body, angle) t'est fourni. Tu dois :
- Garder le MÊME sujet, préfixé par "Re: " (l'email part dans le même thread).
- Body très court (40-60 mots MAX). 3 lignes grand max.
- Référence très subtilement l'email précédent (« je voulais m'assurer que mon message de la semaine dernière était bien passé »).
- Ajoute UN angle complémentaire (un bénéfice qu'on n'avait pas mis dans le premier, ou un mini-social-proof).
- Termine par UNE question simple/binaire (« est-ce que ça résonne ? » / « pertinent ou pas le moment ? »).
- PAS de pitch détaillé — c'est juste un bump amical.

Retourne JSON strict :
{ "subject": "...", "body": "Bonjour Prénom,\\n\\n..." }
"""

_SYSTEM_PROMPT_BREAKUP_FR = """Tu es un commercial B2B FR qui rédige un email BREAK-UP (J+10), dernière relance avant d'archiver le contact.

L'objectif paradoxal : provoquer une réponse en signalant qu'on lâche.

Règles :
- Sujet très court (« on lâche ? » / « dernière tentative » / « je clôture »).
- Body 50-80 mots MAX.
- Ton calme, pas agressif, pas victimaire. Style « no hard feelings ».
- Structure : (a) reconnaître le silence sans insister, (b) résumer en 1 phrase ce qu'on apportait, (c) annoncer la fermeture du dossier, (d) laisser la porte ouverte (« si vous changez d'avis... »).
- PAS de relance future, PAS de pression.

Retourne JSON strict :
{ "subject": "...", "body": "Bonjour Prénom,\\n\\n..." }
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
    sender_pitch: Optional[str] = None,
    target_icp_description: Optional[str] = None,
    tech_pitch_hint: Optional[str] = None,
    ft_hiring_reason: Optional[str] = None,
    careers_tilt_hint: Optional[str] = None,
    rfp_hint: Optional[str] = None,
    multi_touch: bool = False,
    model: str = "claude-haiku-4-5",
) -> Optional[ColdEmail]:
    """Generate a personalized cold email for one lead. None on failure.

    Parameters:
        sender_offer: 1-line value prop (e.g. "logiciel ERP cloud no-code").
        sender_company: brand name (signed in the email body).
        sender_pitch: OPTIONAL multi-line pitch the user wrote — adds detail
            (use cases, differentiators, references). When provided, Claude
            adapts the message tone and angle to match.
        target_icp_description: OPTIONAL full ICP description the user gave
            to icp_from_nl. Used to remind Claude what kind of prospect this is
            (B2B SaaS vs CHR vs santé etc.) → adapts the tone accordingly.
        tech_pitch_hint: OPTIONAL one-liner from pitch_hint_from_tech() — uses
            the prospect's detected stack (Zapier, HubSpot, Next.js…) to
            personalise the opener without fabricating context.
        ft_hiring_reason: OPTIONAL France Travail hiring-signal reason
            ("12 offres en 30j — hyper-croissance"). Powerful timing trigger
            for AI-training / outsourcing pitches.
        careers_tilt_hint: OPTIONAL string from careers_page.scan_careers_for
            ("recrute Data Scientist + AI Engineer"). Stronger than FT for
            tech roles. Claude can reference a SPECIFIC role.
        rfp_hint: OPTIONAL string from appels_offres.rfp_pitch_hint — when
            present, the prospect has an ACTIVE matching RFP. Claude is told
            to reference it explicitly (« vu votre récent appel à projet… »).
            STRONGEST signal possible (intention d'achat directe).
        multi_touch: When True, also generate a J+4 follow-up email and a
            J+10 break-up email. Returns 3 emails in ONE ColdEmail object
            (subject/body + followup_subject/body + breakup_subject/body).
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
                # v0.14.0+ — extra hooks (Claude doit les utiliser SI pertinents,
                # jamais les inventer)
                "tech_stack_pitch_hint": tech_pitch_hint or None,
                "signal_recrutement_FT": ft_hiring_reason or None,
                "signal_recrutement_site": careers_tilt_hint or None,
                "appel_offres_actif": rfp_hint or None,
            },
            "vendeur": {
                "nom_entreprise": sender_company,
                "offre": sender_offer,
                "pitch_complet": sender_pitch or None,
                "icp_cible_global": target_icp_description or None,
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
        try:
            from quotas import mark_used
            mark_used("anthropic")
        except Exception:
            pass
        text = resp.content[0].text if resp.content else ""
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].lstrip()
        data = json.loads(text)
        email = ColdEmail(
            subject=(data.get("subject") or "").strip()[:60],
            body=(data.get("body") or "").strip(),
            angle=(data.get("angle") or "").strip(),
            context_signals=list(data.get("context_signals") or []),
            sender_offer=(data.get("sender_offer") or sender_offer),
        )
        # v0.15.0 — multi-touch: générer J+4 + J+10 dans la foulée
        if multi_touch and email.subject and email.body:
            email.followup_subject, email.followup_body = _generate_followup(
                email, person_first, model
            )
            email.breakup_subject, email.breakup_body = _generate_breakup(
                email, person_first, model
            )
        return email
    except Exception:
        return None


def _generate_followup(
    cold: ColdEmail, person_first: str, model: str
) -> tuple[Optional[str], Optional[str]]:
    """J+4 follow-up — short bump on the original thread."""
    try:
        client = Anthropic()
        payload = json.dumps(
            {
                "decideur_prenom": person_first,
                "email_initial_subject": cold.subject,
                "email_initial_body": cold.body,
                "angle_initial": cold.angle,
            },
            ensure_ascii=False,
        )
        resp = client.messages.create(
            model=model,
            max_tokens=300,
            system=_SYSTEM_PROMPT_FOLLOWUP_FR,
            messages=[{"role": "user", "content": payload}],
        )
        try:
            from quotas import mark_used
            mark_used("anthropic")
        except Exception:
            pass
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].lstrip()
        data = json.loads(text)
        subj = (data.get("subject") or "").strip()[:80]
        body = (data.get("body") or "").strip()
        if subj and not subj.lower().startswith("re:"):
            subj = f"Re: {cold.subject}"
        return subj or f"Re: {cold.subject}", body or None
    except Exception:
        return None, None


def _generate_breakup(
    cold: ColdEmail, person_first: str, model: str
) -> tuple[Optional[str], Optional[str]]:
    """J+10 break-up — calm, no-pressure final touch."""
    try:
        client = Anthropic()
        payload = json.dumps(
            {
                "decideur_prenom": person_first,
                "email_initial_subject": cold.subject,
                "email_initial_body": cold.body,
                "value_prop": cold.sender_offer,
            },
            ensure_ascii=False,
        )
        resp = client.messages.create(
            model=model,
            max_tokens=300,
            system=_SYSTEM_PROMPT_BREAKUP_FR,
            messages=[{"role": "user", "content": payload}],
        )
        try:
            from quotas import mark_used
            mark_used("anthropic")
        except Exception:
            pass
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].lstrip()
        data = json.loads(text)
        return (
            (data.get("subject") or "").strip()[:60] or None,
            (data.get("body") or "").strip() or None,
        )
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Batch generation for a list of Lead objects (returns dict by SIREN)
# ---------------------------------------------------------------------------

def generate_for_leads(
    leads: list,
    *,
    sender_offer: str = "spiritueux premium français pour cartes bars et restaurants",
    sender_company: str = "Bear Brothers",
    sender_pitch: Optional[str] = None,
    target_icp_description: Optional[str] = None,
    multi_touch: bool = False,
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
    # Lazy imports to avoid circular dep at module load time
    try:
        from tech_stack import pitch_hint_from_tech
    except Exception:
        pitch_hint_from_tech = None  # type: ignore
    try:
        from appels_offres import rfp_pitch_hint
    except Exception:
        rfp_pitch_hint = None  # type: ignore

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

        # v0.14.0+ — extract personalisation hooks from the lead extras
        tech_hint = None
        if pitch_hint_from_tech:
            try:
                tech_hint = pitch_hint_from_tech({
                    "stack": getattr(lead, "tech_stack", []) or [],
                    "categories": getattr(lead, "tech_categories", {}) or {},
                    "signals": getattr(lead, "tech_signals", []) or [],
                    "primary_cms": getattr(lead, "primary_cms", None),
                })
            except Exception:
                tech_hint = None
        ft_reason = getattr(lead, "ft_reason", None)

        # v0.15.0 — careers signal (recrutement direct sur le site)
        careers_hint = None
        tilt_cats = getattr(lead, "careers_tilt_categories", None) or []
        top_titles = getattr(lead, "careers_top_titles", None) or []
        if tilt_cats and top_titles:
            # Compact, factual: "recrute Data Scientist + AI Engineer"
            cats_str = " + ".join(tilt_cats[:2])
            sample = " / ".join(top_titles[:2])
            careers_hint = f"recrute actuellement ({cats_str}) : {sample}"

        # v0.15.0 — appel d'offres actif (signal d'intention max)
        rfp_active = getattr(lead, "rfp_active", None)
        rfp_hint = None
        if rfp_active and rfp_pitch_hint:
            try:
                rfp_hint = rfp_pitch_hint(rfp_active)
            except Exception:
                rfp_hint = None

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
            sender_pitch=sender_pitch,
            target_icp_description=target_icp_description,
            tech_pitch_hint=tech_hint,
            ft_hiring_reason=ft_reason,
            careers_tilt_hint=careers_hint,
            rfp_hint=rfp_hint,
            multi_touch=multi_touch,
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
