"""
Email Pattern Engine — last-resort email guesser when all verification fails.

When the cascade Dropcontact → mentions_legales → SMTP-verify all return
nothing, we still want to give the salesperson SOMETHING to test in their
1-to-1 outreach. This module:

1. Resolves the most likely email DOMAIN for a company (3-tier waterfall):
   a. The verified website passed by the caller (highest trust)
   b. A hardcoded mapping of well-known FR groups (CAC40/SBF120 + tech leaders)
   c. LLM inference (Claude Haiku) GUARDED by a DNS+MX existence check

2. Generates ranked email pattern candidates from (first, last, domain):
   - prenom.nom@         (most common across FR B2B)
   - prenomnom@
   - initiale.nom@
   - prenom@             (smaller companies / founders)
   - prenom_nom@
   - prenom-nom@
   - nom.prenom@         (some legacy groups)

3. Returns the LIST without SMTP verification, each entry tagged with:
   - "pattern-guess" source
   - confidence capped at 40 (low — explicit "not verified")
   - note "PATTERN GUESS: TEST BEFORE SENDING"

The output is designed for 1-to-1 outreach where the salesperson tests
deliverability manually. It's NOT for mass campaigns (high bounce rate).

Public API:
    guess_emails(first, last, company, *, website=None, role=None) -> dict
        returns {
            "domain": "...",
            "domain_source": "website|known-fr-group|llm-inferred|none",
            "patterns": [{"email": "...", "confidence": 35, "note": "..."}, ...],
            "warning": "...",
        }
"""
from __future__ import annotations

import os
import re
import socket
import unicodedata
from typing import Optional

import httpx

try:
    import dns.resolver
    _HAS_DNS = True
except ImportError:
    _HAS_DNS = False

try:
    from anthropic import Anthropic  # type: ignore
except ImportError:
    Anthropic = None  # type: ignore

DEFAULT_TIMEOUT = 6.0


# ---------------------------------------------------------------------------
# Name normalization (FR-aware)
# ---------------------------------------------------------------------------

def _strip_accents(s: str) -> str:
    """'Frédéric' → 'frederic', 'Laëtitia' → 'laetitia'."""
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def _normalize_token(s: str) -> str:
    """Lower + ASCII + strip apostrophes/spaces. Keep hyphens (composite names)."""
    s = _strip_accents(s).lower()
    # Drop apostrophes ("d'Arcy" → "darcy"), keep hyphens
    s = re.sub(r"['`’]", "", s)
    # Collapse internal spaces (composite last names without hyphens)
    s = re.sub(r"\s+", "", s)
    # Strip anything that's not a-z, 0-9, hyphen
    s = re.sub(r"[^a-z0-9-]", "", s)
    return s


def _no_hyphen(s: str) -> str:
    return s.replace("-", "")


# ---------------------------------------------------------------------------
# Domain resolution
# ---------------------------------------------------------------------------

# Hand-curated mapping of well-known FR groups → known email domain.
# Strict rule: keys are the brand string in LOWERCASE, ACCENT-STRIPPED;
# we match if any key is a substring of the slugified company name.
# Source: 2024-2026 publicly-observed email patterns from press releases,
# LinkedIn corporate emails, and signature footers on official sites.
KNOWN_FR_DOMAINS: dict[str, str] = {
    # --- CAC40 / big French groups ---
    "lvmh": "lvmh.fr",
    "totalenergies": "totalenergies.com",
    "total ": "totalenergies.com",  # legacy "Total"
    "lor": "loreal.com",
    "loreal": "loreal.com",
    "sanofi": "sanofi.com",
    "airbus": "airbus.com",
    "schneider electric": "se.com",
    "axa": "axa.com",
    "bnp paribas": "bnpparibas.com",
    "credit agricole": "credit-agricole-sa.fr",
    "societe generale": "socgen.com",
    "renault": "renault.com",
    "stellantis": "stellantis.com",
    "danone": "danone.com",
    "carrefour": "carrefour.com",
    "saint-gobain": "saint-gobain.com",
    "saint gobain": "saint-gobain.com",
    "michelin": "michelin.com",
    "publicis": "publicisgroupe.com",
    "vinci": "vinci.com",
    "bouygues": "bouygues.com",
    "engie": "engie.com",
    "veolia": "veolia.com",
    "suez": "suez.com",
    "essilor": "essilorluxottica.com",
    "luxottica": "essilorluxottica.com",
    "kering": "kering.com",
    "hermes": "hermes.com",
    "hermès": "hermes.com",
    "pernod ricard": "pernod-ricard.com",
    "edf": "edf.fr",
    "orange": "orange.com",
    "alstom": "alstomgroup.com",
    "capgemini": "capgemini.com",
    "thales": "thalesgroup.com",
    "thales alenia": "thalesaleniaspace.com",
    "dassault": "dassault-aviation.com",
    "dassault systemes": "3ds.com",
    # --- SBF120 / tech / industry ---
    "atos": "atos.net",
    "sopra steria": "soprasteria.com",
    "sopra": "soprasteria.com",
    "ubisoft": "ubisoft.com",
    "ovhcloud": "ovhcloud.com",
    "ovh": "ovhcloud.com",
    "iliad": "iliad.fr",
    "free": "free.fr",
    "sfr": "sfr.com",
    "altice": "altice.net",
    "bouygues telecom": "bouyguestelecom.fr",
    "blablacar": "blablacar.com",
    "doctolib": "doctolib.com",
    "back market": "backmarket.com",
    "veepee": "veepee.com",
    "vente-privee": "veepee.com",
    "criteo": "criteo.com",
    "deezer": "deezer.com",
    "dailymotion": "dailymotion.com",
    "showroomprive": "showroomprive.com",
    # --- Banque / assurance / retail ---
    "bpce": "bpce.fr",
    "lcl": "lcl.fr",
    "mma": "mma.fr",
    "macif": "macif.fr",
    "maif": "maif.fr",
    "matmut": "matmut.fr",
    "generali": "generali.com",
    "allianz": "allianz.fr",
    "boursorama": "boursorama.fr",
    "fnac": "fnacdarty.com",
    "darty": "fnacdarty.com",
    "decathlon": "decathlon.com",
    "leclerc": "e-leclerc.com",
    "auchan": "auchan.fr",
    "intermarche": "mousquetaires.com",
    # --- Auto / industrial ---
    "peugeot": "stellantis.com",
    "citroen": "stellantis.com",
    "citroën": "stellantis.com",
    "ds automobiles": "stellantis.com",
    "valeo": "valeo.com",
    "continental": "continental.com",
    "continental automotive": "continental.com",
    "faurecia": "forvia.com",
    "forvia": "forvia.com",
    "plastic omnium": "plasticomnium.com",
    # --- Energy / utilities ---
    "engie": "engie.com",
    "rte": "rte-france.com",
    "enedis": "enedis.fr",
    "grdf": "grdf.fr",
    # --- Aerospace / defense ---
    "safran": "safrangroup.com",
    "naval group": "naval-group.com",
    "mbda": "mbda-systems.com",
    "arianespace": "arianespace.com",
    # --- Pharma / chemicals ---
    "servier": "servier.com",
    "ipsen": "ipsen.com",
    "air liquide": "airliquide.com",
    "arkema": "arkema.com",
    "solvay": "syensqo.com",  # Solvay split → Syensqo
    "syensqo": "syensqo.com",
}


def _company_slug(name: str) -> str:
    """For matching against KNOWN_FR_DOMAINS keys."""
    return _strip_accents(name).lower().strip()


def _domain_from_known_group(company: str) -> Optional[str]:
    """Match against the curated FR group mapping. Substring match (case-insensitive,
    accent-stripped). Returns the domain or None.
    """
    if not company:
        return None
    slug = _company_slug(company)
    # Order matters when multiple keys could match (e.g. "thales alenia" before "thales")
    for key in sorted(KNOWN_FR_DOMAINS.keys(), key=lambda k: -len(k)):
        if key in slug:
            return KNOWN_FR_DOMAINS[key]
    return None


def _domain_has_mx(domain: str, timeout_s: float = 4.0) -> bool:
    """Check that a domain has at least one MX record. Used as a hallucination
    guard around LLM-inferred domains."""
    if not domain:
        return False
    if _HAS_DNS:
        try:
            r = dns.resolver.Resolver()
            r.lifetime = timeout_s
            answers = r.resolve(domain, "MX")
            return len(list(answers)) > 0
        except Exception:
            return False
    # Fallback if dnspython missing: a simple A-record lookup
    try:
        socket.gethostbyname(domain)
        return True
    except Exception:
        return False


def _domain_from_llm(company: str, *, role: Optional[str] = None) -> Optional[str]:
    """Ask Claude Haiku for the official email domain of a French company.

    GUARDED: we DNS-verify the returned domain has MX records before using it.
    Without this guard, Claude could hallucinate plausible but non-existent
    domains. Costs ~$0.0003/call.
    """
    if Anthropic is None or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    if not company:
        return None
    try:
        client = Anthropic()
        prompt = (
            f"Pour l'entreprise française \"{company}\""
            + (f" (rôle ciblé : {role})" if role else "")
            + ", quel est le domaine email officiel utilisé par les employés ?\n\n"
            "Réponds UNIQUEMENT avec le domaine, sans http://, sans www, sans /. "
            "Exemple: thalesgroup.com\n"
            "Si tu n'es pas SÛR à 90%, réponds NONE."
        )
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=30,
            messages=[{"role": "user", "content": prompt}],
        )
        text = (resp.content[0].text if resp.content else "").strip()
        # Sanitise
        if not text or text.upper().startswith("NONE"):
            return None
        text = text.lower().split()[0].strip(",./;\"'`")
        text = re.sub(r"^(?:https?://)?(?:www\.)?", "", text)
        if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", text):
            return None
        # DNS-MX guard — kills hallucinations
        if not _domain_has_mx(text):
            return None
        return text
    except Exception:
        return None


def resolve_domain(
    company: str,
    *,
    website: Optional[str] = None,
    role: Optional[str] = None,
    use_llm: bool = True,
) -> tuple[Optional[str], str]:
    """3-tier waterfall to resolve a company's email domain.

    Returns (domain, source) where source is one of:
        "website" | "known-fr-group" | "llm-inferred" | "none"
    """
    # Tier 1: verified website (the caller already passed strict FR checks)
    if website:
        from urllib.parse import urlparse
        host = (urlparse(website).hostname or "").lower()
        host = host.removeprefix("www.")
        if host and "." in host:
            return host, "website"

    # Tier 2: hardcoded FR groups (zero hallucination risk)
    known = _domain_from_known_group(company)
    if known:
        return known, "known-fr-group"

    # Tier 3: LLM inference with DNS+MX guard
    if use_llm:
        llm = _domain_from_llm(company, role=role)
        if llm:
            return llm, "llm-inferred"

    return None, "none"


# ---------------------------------------------------------------------------
# Pattern generation (FR-aware, composite-name-aware)
# ---------------------------------------------------------------------------

# Pattern catalogue, ordered by empirical frequency in FR B2B.
# Each entry: (template, baseline_confidence)
# Baselines are CAPPED at 40 because no SMTP verification was done.
_PATTERNS: list[tuple[str, int]] = [
    ("{first}.{last}@{domain}",     40),  # most common — Thales, BNP, Sopra
    ("{first}{last}@{domain}",      30),
    ("{f}.{last}@{domain}",         28),
    ("{first}@{domain}",            18),  # founders / small companies
    ("{first}_{last}@{domain}",     15),
    ("{first}-{last}@{domain}",     15),
    ("{last}.{first}@{domain}",     12),  # rare but exists (some legacy)
    ("{f}{last}@{domain}",          10),
]


def _expand_composite(first_n: str, last_n: str) -> list[tuple[str, str]]:
    """Build (first, last) variant pairs to handle composite names properly.

    For 'Jean-Michel Soler' we want both:
        ('jean-michel', 'soler')   ← keep hyphen (priority)
        ('jeanmichel', 'soler')    ← no hyphen fallback
    For 'Marie-Pierre Sans-Fabre':
        ('marie-pierre', 'sans-fabre')
        ('mariepierre', 'sansfabre')
    Last name with internal spaces: '(d)e Lapeyre' → 'delapeyre' (no hyphen).
    """
    variants: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _push(f: str, l: str) -> None:
        key = (f, l)
        if key not in seen and f and l:
            seen.add(key)
            variants.append(key)

    _push(first_n, last_n)
    if "-" in first_n or "-" in last_n:
        _push(_no_hyphen(first_n), last_n)
        _push(first_n, _no_hyphen(last_n))
        _push(_no_hyphen(first_n), _no_hyphen(last_n))
    return variants


def generate_patterns(
    first: str,
    last: str,
    domain: str,
) -> list[dict]:
    """Generate ranked email pattern candidates for (first, last, domain).

    Returns a list of dicts: {email, confidence, note, template}, sorted by
    confidence descending. NO SMTP verification is performed — confidence is
    based on FR B2B pattern frequency only.
    """
    if not first or not last or not domain:
        return []

    first_n = _normalize_token(first)
    last_n = _normalize_token(last)
    if not first_n or not last_n:
        return []

    out: list[dict] = []
    seen_emails: set[str] = set()

    for first_var, last_var in _expand_composite(first_n, last_n):
        f_initial = first_var[0]
        for template, base_conf in _PATTERNS:
            email = template.format(
                first=first_var,
                last=last_var,
                f=f_initial,
                domain=domain,
            )
            email = email.lower()
            if email in seen_emails:
                continue
            seen_emails.add(email)
            # Small confidence penalty for the no-hyphen variant of a hyphenated name
            conf = base_conf
            if "-" in first or "-" in last:
                if "-" not in first_var and "-" not in last_var:
                    conf = max(5, conf - 5)
            out.append({
                "email": email,
                "confidence": conf,
                "note": "PATTERN GUESS — not SMTP verified. Test before sending.",
                "template": template,
            })

    # Stable sort: higher confidence first, then by template order
    out.sort(key=lambda d: -d["confidence"])
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def guess_emails(
    first: str,
    last: str,
    company: str,
    *,
    website: Optional[str] = None,
    role: Optional[str] = None,
    use_llm_for_domain: bool = True,
    max_variants: int = 5,
) -> dict:
    """Full pipeline: resolve domain + generate ranked patterns.

    Returns:
        {
            "domain": "thalesgroup.com" | None,
            "domain_source": "website|known-fr-group|llm-inferred|none",
            "primary_email": str | None,
            "patterns": [ {email, confidence, note, template}, ... ],
            "warning": str,
        }

    Use this as a LAST RESORT in the pipeline — after Dropcontact, mentions
    légales and SMTP probe have all failed. The salesperson is expected to
    manually test the primary_email before mass-sending.
    """
    domain, source = resolve_domain(
        company, website=website, role=role, use_llm=use_llm_for_domain,
    )

    if not domain:
        return {
            "domain": None,
            "domain_source": source,
            "primary_email": None,
            "patterns": [],
            "warning": (
                "No reliable email domain could be resolved for this company. "
                "The pattern engine cannot guess without a domain."
            ),
        }

    patterns = generate_patterns(first, last, domain)[:max_variants]
    primary = patterns[0]["email"] if patterns else None
    warning = (
        "Emails below are PATTERN GUESSES. None has been verified via SMTP. "
        f"Domain '{domain}' was resolved via {source}. "
        "Test the primary email with a 1-to-1 send before mass campaigns."
    )
    return {
        "domain": domain,
        "domain_source": source,
        "primary_email": primary,
        "patterns": patterns,
        "warning": warning,
    }


def _cli() -> None:
    import argparse
    import json
    import warnings as _w
    _w.filterwarnings("ignore")

    p = argparse.ArgumentParser(
        description="Last-resort email pattern guesser (no SMTP)."
    )
    p.add_argument("first")
    p.add_argument("last")
    p.add_argument("company")
    p.add_argument("--website", help="Verified company website (skip domain inference)")
    p.add_argument("--role", help="Optional role hint (for LLM domain inference)")
    p.add_argument("--no-llm", action="store_true",
                   help="Skip LLM inference; rely only on website + known-FR-groups")
    p.add_argument("--max", type=int, default=5)
    args = p.parse_args()

    result = guess_emails(
        args.first, args.last, args.company,
        website=args.website, role=args.role,
        use_llm_for_domain=not args.no_llm,
        max_variants=args.max,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
