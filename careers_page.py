"""
Careers page scraper — détecte la page "carrière" du prospect et extrait
les rôles ouverts, en complément de France Travail (qui rate ~70% des
postes cadres SaaS / conseil publiés directement sur le site).

Pourquoi ce signal est gold pour la prospection :
- Une PME qui RECRUTE un AI Engineer, Data Scientist, Product, Sales =
  TILT MAX pour Comeos (formation IA / automatisation).
- Les rôles tech/cadre sont sur le site, PAS sur France Travail (qui
  héberge ~80% d'ouvriers, employés, agents publics).
- Les rôles ouverts donnent une idée précise de la phase de croissance
  (scaling rapide, internationalisation, restructuration...).

Comment ça marche :
1. À partir du website du prospect, on teste une liste de chemins courants
   (/careers, /jobs, /recrutement, /rejoignez-nous, /carriere, …).
2. Le premier qui renvoie une 200 et contient du texte de type emploi est
   gardé.
3. On extrait :
   - tous les titres d'offre (h2/h3/article title patterns)
   - les keywords tech qui matchent un dico "TILT IA/automation"
4. On renvoie un dict avec n_offres, top_titles, tech_role_signals.

Le résultat alimente :
- ICP scoring : `careers_tech_signals_any: ["ai", "data", "automation"]`
- Cold email : "vu votre annonce d'AI Engineer..." (perso fort)
- Quality flag : `tilt:tech-hiring` (+30 ICP boost)

Module 100% gratuit, pas d'API tierce. ~50 lignes de scraping minimal.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 10.0

# Standard FR + EN career page paths to probe (in order of priority).
_CAREER_PATHS = [
    "/carrieres", "/carriere", "/careers", "/career",
    "/rejoignez-nous", "/rejoindre-nous", "/nous-rejoindre",
    "/recrutement", "/recrutements", "/jobs", "/job",
    "/offres-emploi", "/offres-d-emploi", "/offre-emploi",
    "/emplois", "/emploi", "/nos-offres", "/postes",
    "/join-us", "/work-with-us", "/we-are-hiring", "/hiring",
    "/team", "/equipe", "/nos-equipes",
]

# Keywords that say "this page is a job listing", to filter false positives
# (e.g. /carrieres → page corporate sans listings réels).
_JOB_PAGE_HINTS = re.compile(
    r"\b(?:CDI|CDD|stage|alternance|freelance|temps\s+plein|"
    r"full[\-\s]?time|part[\-\s]?time|"
    r"voir l[''']offre|postuler|candidat(?:er|ure)|apply|"
    r"open positions?|nos\s+offres|join\s+our\s+team|"
    r"rejoindre l[''']équipe|nous recrutons)\b",
    re.IGNORECASE,
)

# Tech / IA roles that trigger the TILT for Comeos pitch
_TECH_ROLE_PATTERNS: dict[str, re.Pattern] = {
    "ai": re.compile(
        r"\b(?:ai\s+engineer|ml\s+engineer|machine\s+learning|"
        r"intelligence\s+artificielle|ing[ée]nieur\s+ia|ia\s+engineer|"
        r"ia\s+architect|prompt\s+engineer|llm\s+engineer|"
        r"chercheur\s+ia|chief\s+ai\s+officer|caio)\b",
        re.IGNORECASE,
    ),
    "data": re.compile(
        r"\b(?:data\s+(?:scientist|engineer|analyst|architect|"
        r"steward|ops?|product\s+manager)|"
        r"analyste\s+(?:donn[ée]es|big\s+data)|"
        r"ing[ée]nieur\s+(?:data|donn[ée]es))\b",
        re.IGNORECASE,
    ),
    "automation": re.compile(
        r"\b(?:automation\s+engineer|rpa\s+(?:developer|engineer)|"
        r"workflow\s+(?:engineer|specialist)|"
        r"automatisation|n8n|zapier|make\.com|integromat|"
        r"low[\-\s]?code|no[\-\s]?code)\b",
        re.IGNORECASE,
    ),
    "tech": re.compile(
        r"\b(?:software\s+engineer|d[ée]veloppeur|developer|"
        r"full[\-\s]?stack|back[\-\s]?end|front[\-\s]?end|"
        r"devops|sre|platform\s+engineer|cloud\s+architect|"
        r"tech\s+lead|cto|chief\s+technology)\b",
        re.IGNORECASE,
    ),
    "sales": re.compile(
        r"\b(?:account\s+executive|sales\s+(?:dev|representative|manager)|"
        r"business\s+developer|sdr|bdr|"
        r"commercial\s+(?:terrain|grand\s+compte|b2b|saas)|"
        r"ing[ée]nieur\s+commercial|cso|chief\s+sales)\b",
        re.IGNORECASE,
    ),
    "marketing": re.compile(
        r"\b(?:growth\s+(?:hacker|marketer|manager)|"
        r"performance\s+marketing|cmo|chief\s+marketing|"
        r"chef\s+de\s+produit|product\s+marketing\s+manager|pmm)\b",
        re.IGNORECASE,
    ),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_careers_page(website: Optional[str]) -> Optional[str]:
    """Probe known paths to find the prospect's career page. Returns the URL.

    None if no page that looks like a job listing was found.
    """
    if not website:
        return None
    base = website.rstrip("/")
    try:
        with httpx.Client(
            timeout=TIMEOUT, follow_redirects=True, verify=False,
            headers={"User-Agent": USER_AGENT},
        ) as c:
            for path in _CAREER_PATHS:
                url = base + path
                try:
                    r = c.get(url)
                    if r.status_code != 200:
                        continue
                    if len(r.text) < 500:
                        continue
                    # Sanity check: page looks like a careers listing?
                    if _JOB_PAGE_HINTS.search(r.text):
                        return str(r.url)
                except Exception:
                    continue
    except Exception:
        return None
    return None


def extract_job_titles(html: str, *, max_titles: int = 15) -> list[str]:
    """Extract distinct candidate job titles from a careers page HTML."""
    if not html:
        return []
    titles: list[str] = []
    seen: set[str] = set()

    tree = HTMLParser(html)
    # Heading-based extraction — most career pages use h2/h3 per offer
    candidates: list[str] = []
    for sel in ("h1", "h2", "h3", "h4",
                "a.job-title", ".offer-title", ".vacancy-title",
                "[class*='job']", "[class*='offer']", "[class*='offre']"):
        for n in tree.css(sel):
            t = n.text(strip=True)
            if t and 5 < len(t) < 140:
                candidates.append(t)
    # Anchor texts that look like job postings ("Voir l'offre — ROLE")
    for a in tree.css("a"):
        t = a.text(strip=True)
        if t and 8 < len(t) < 140 and _looks_like_role(t):
            candidates.append(t)

    for c in candidates:
        norm = re.sub(r"\s+", " ", c).strip()
        # Filter obvious garbage
        if not _looks_like_role(norm):
            continue
        key = norm.lower()
        if key in seen:
            continue
        seen.add(key)
        titles.append(norm)
        if len(titles) >= max_titles:
            break
    return titles


def _looks_like_role(text: str) -> bool:
    """Heuristic: is this text a plausible job title?"""
    if not text:
        return False
    lower = text.lower()
    # Strong positive signals
    role_kw = (
        " h/f", "(h/f)", "f/h", "(f/h)",
        " cdi", " cdd", " stage", "alternance", "internship",
        "engineer", "ingénieur", "ingenieur",
        "developer", "développeur", "developpeur",
        "manager", "directeur", "directrice", "responsable",
        "consultant", "analyst", "analyste",
        "officer", "lead", "head of",
        "commercial", "sales", "marketing",
        "data", "devops", "designer", "architect", "architecte",
    )
    if any(k in lower for k in role_kw):
        return True
    # Reject very short and very long lines
    if len(text) < 8 or len(text) > 140:
        return False
    # Reject navigation / generic
    if re.search(r"\b(?:accueil|home|contact|about|menu|cookies?|"
                 r"voir plus|read more|en savoir plus)\b", lower):
        return False
    return False


def detect_tech_role_signals(titles: list[str]) -> dict[str, list[str]]:
    """Classify job titles into TILT categories (ai/data/automation/…).

    Returns a dict { category: [matching titles, …] } only for categories
    that actually have at least one match.
    """
    out: dict[str, list[str]] = {}
    for t in titles:
        for cat, rx in _TECH_ROLE_PATTERNS.items():
            if rx.search(t):
                out.setdefault(cat, []).append(t)
    return out


def scan_careers_for(website: Optional[str]) -> dict:
    """All-in-one: detect career page → extract titles → classify signals.

    Returns:
        {
            "careers_url": str | None,
            "n_jobs": int,
            "top_titles": [str, ...],
            "tech_signals": {"ai": [...], "data": [...], ...},
            "tilt_categories": ["ai", "data", "automation"],  # for ICP rule
            "icp_modifier": int,  # +30 if any tilt fires, 0 else
        }
    """
    out = {
        "careers_url": None,
        "n_jobs": 0,
        "top_titles": [],
        "tech_signals": {},
        "tilt_categories": [],
        "icp_modifier": 0,
    }
    url = detect_careers_page(website)
    if not url:
        return out
    out["careers_url"] = url
    try:
        with httpx.Client(
            timeout=TIMEOUT, follow_redirects=True, verify=False,
            headers={"User-Agent": USER_AGENT},
        ) as c:
            r = c.get(url)
            if r.status_code != 200:
                return out
            titles = extract_job_titles(r.text)
            out["n_jobs"] = len(titles)
            out["top_titles"] = titles[:10]
            sigs = detect_tech_role_signals(titles)
            out["tech_signals"] = sigs
            out["tilt_categories"] = sorted(sigs.keys())
            # Strong boost when AI/data/automation/tech roles open — that's
            # the strongest "formation IA" trigger we have.
            high_tilt = {"ai", "data", "automation"}
            if any(c in out["tilt_categories"] for c in high_tilt):
                out["icp_modifier"] = 30
            elif "tech" in out["tilt_categories"]:
                out["icp_modifier"] = 15
            elif sigs:
                out["icp_modifier"] = 5
    except Exception:
        pass
    return out


def _cli() -> None:
    import argparse
    import json as _json
    import warnings
    warnings.filterwarnings("ignore")
    p = argparse.ArgumentParser(description="Detect & scan the careers page of a website")
    p.add_argument("website")
    args = p.parse_args()
    result = scan_careers_for(args.website)
    print(_json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
