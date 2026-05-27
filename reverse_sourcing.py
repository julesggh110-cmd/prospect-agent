"""
Reverse sourcing — Google → URL → SIREN (au lieu de Sirene NAF → list).

Pourquoi ce module existe :
La pipeline classique part de Sirene (filtre NAF + dépt + tranche), ce qui
fait remonter beaucoup de faux positifs car le NAF est administratif, pas
business. Exemple : NAF 70.22Z "conseil aux affaires" = holding agricole,
freelance, cabinet RH, agence de pub… tous mélangés.

Reverse sourcing résout ça à la SOURCE :
  1. Tu décris ton ICP en langage naturel ("cabinet conseil SIRH Paris")
  2. On génère 3-5 variations de requête Google
  3. On hit Serper pour chacune → top 20 URLs
  4. On vire les agrégateurs (LinkedIn, Pappers, Pages Jaunes, etc.)
  5. Pour chaque URL business survivante, on extrait le nom de l'entreprise
     (og:site_name → og:title → <title>)
  6. On résout vers SIREN via recherche texte Pappers
  7. On retourne la liste compatible avec le reste du pipeline

Résultat : on attaque la pipeline avec un POOL DE VRAIES BOÎTES qui matchent
le business model voulu, pas un pool NAF-pollué.

À combiner idéalement avec --icp-strict-llm (v0.17.0) pour double filtre :
sourcing pertinent + analyse business sémantique = leads parfaits.

Coût :
  - Serper : 1 query par variation = 3-5 credits par campagne (≈ $0.005)
  - Pappers : 1 lookup par URL survivante (free tier 100/jour)
  - Anthropic Haiku (génération queries, optionnel) : ≈ $0.0003/campagne

Public API :
    from reverse_sourcing import reverse_source

    candidates = reverse_source(
        icp_description="cabinet conseil SIRH Paris 50-200 salariés",
        max_total=30,
    )
    # → [{nom_complet, siren, website, source_query, source_snippet}, ...]
"""
from __future__ import annotations

import os
import re
from typing import Optional
from urllib.parse import urlparse

import httpx

from http_safe import Throttle

_THROTTLE = Throttle(min_interval_s=0.3)


# Agrégateurs / annuaires / réseaux sociaux à VIRER des résultats Google
# (un agrégateur n'est pas une boîte cible — c'est un répertoire d'autres
# boîtes). Si on garde linkedin.com on aurait 1000 profils LinkedIn comme
# leads, ce qui n'a aucun sens.
_AGGREGATORS = {
    # Annuaires entreprises FR
    "pappers.fr", "societe.com", "infogreffe.fr", "verif.com",
    "kompass.com", "kompass.fr", "manageo.fr", "scores.io",
    "pages-jaunes.fr", "pagesjaunes.fr", "annuaire.fr", "118712.fr",
    "118000.fr", "yellowpages.fr", "annuaire-des-entreprises.fr",
    "data.gouv.fr", "recherche-entreprises.api.gouv.fr",
    "business-directory.fr", "europages.fr", "europages.com",
    "hoodspot.fr", "kalimedia.com",
    # Réseaux sociaux et plateformes
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com",
    "x.com", "youtube.com", "tiktok.com", "pinterest.com",
    "snapchat.com", "threads.net", "medium.com",
    # Job boards (= pas la boîte elle-même)
    "indeed.fr", "indeed.com", "welcometothejungle.com",
    "glassdoor.fr", "glassdoor.com", "monster.fr", "apec.fr",
    "pole-emploi.fr", "francetravail.fr", "linkedin.com/jobs",
    # Search engines / portals
    "google.com", "google.fr", "bing.com", "yahoo.com", "yahoo.fr",
    "duckduckgo.com", "qwant.com",
    # Generic platforms / e-commerce
    "wikipedia.org", "fr.wikipedia.org", "amazon.fr", "amazon.com",
    "ebay.fr", "leboncoin.fr",
    # Presse / news (rarement la boîte officielle)
    "lesechos.fr", "lefigaro.fr", "lemonde.fr", "20minutes.fr",
    "challenges.fr", "capital.fr", "bfmtv.com", "ladepeche.fr",
    "ouest-france.fr", "leparisien.fr", "liberation.fr",
    # Avis & comparateurs (= pas la boîte cible)
    "trustpilot.com", "fr.trustpilot.com", "avis-verifies.com",
    "google.fr/maps", "tripadvisor.fr", "yelp.fr",
    "logiciels.pro", "appvizer.fr", "appvizer.com", "g2.com",
    "capterra.com", "capterra.fr", "softwaresuggest.com",
    "selectra.com", "ultra-saas.com", "saasforge.fr",
    # GitHub, dev platforms
    "github.com", "gitlab.com", "bitbucket.org",
    # Blogs/aggregateurs marketing
    "siecledigital.fr", "frenchweb.fr", "blogdumoderateur.com",
    "clubic.com", "01net.com", "presse-citron.net",
}


def _is_business_url(url: str) -> bool:
    """True si l'URL ressemble à un VRAI site d'entreprise (pas aggregator)."""
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        return False
    if not host or "." not in host:
        return False
    # Reject if host matches any aggregator (exact or subdomain)
    for agg in _AGGREGATORS:
        if host == agg or host.endswith("." + agg):
            return False
    return True


def _extract_company_name(url: str) -> Optional[str]:
    """Fetch la page et extrait le nom de la boîte via :
      1. og:site_name (meta — le plus fiable)
      2. og:title (meta — souvent "Nom - Tagline")
      3. <title> (fallback)
    Retourne None si rien d'utilisable.
    """
    if not url:
        return None
    try:
        with httpx.Client(timeout=8.0, follow_redirects=True, verify=False,
                          headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = c.get(url)
            if r.status_code >= 400 or len(r.text) < 200:
                return None
            html = r.text
    except Exception:
        return None
    # og:site_name (le plus fiable)
    m = re.search(
        r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)',
        html, re.IGNORECASE,
    )
    if m:
        n = m.group(1).strip()
        if 2 < len(n) < 80:
            return n
    # og:title
    m = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        html, re.IGNORECASE,
    )
    if m:
        t = m.group(1).strip()
        # Couper après " - " ou " | " — souvent "Nom - Tagline"
        for sep in [" — ", " - ", " | ", " : "]:
            if sep in t:
                t = t.split(sep)[0].strip()
                break
        if 2 < len(t) < 80:
            return t
    # <title>
    m = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE | re.DOTALL)
    if m:
        t = m.group(1).strip()
        for sep in [" — ", " - ", " | ", " : "]:
            if sep in t:
                t = t.split(sep)[0].strip()
                break
        if 2 < len(t) < 80:
            return t
    return None


def _resolve_siren_from_name(name: str) -> Optional[str]:
    """Resolve a company name to SIREN. Tries Pappers first (richer match),
    falls back to Sirene API (free + unlimited but stricter match).
    """
    if not name or len(name) < 3:
        return None

    # === Tier 1 — Pappers (riche, fuzzy match) ===
    try:
        from pappers_client import have_pappers_key
        if have_pappers_key():
            api_key = os.environ.get("PAPPERS_API_KEY", "")
            _THROTTLE.acquire()
            try:
                with httpx.Client(timeout=10) as c:
                    r = c.get(
                        "https://api.pappers.fr/v2/recherche",
                        params={"q": name, "per_page": 3, "api_token": api_key},
                    )
                    if r.status_code == 200:
                        data = r.json()
                        results = data.get("resultats") or []
                        for entry in results:
                            siren = entry.get("siren")
                            if siren and len(siren) == 9:
                                try:
                                    from quotas import mark_used
                                    mark_used("pappers")
                                except Exception:
                                    pass
                                return siren
                    # status 401/402 = quota dead → fall through to Sirene
            except Exception:
                pass
    except Exception:
        pass

    # === Tier 2 — Sirene API (free, illimité, fallback safe) ===
    try:
        from sirene_client import SireneClient
        with SireneClient() as c:
            resp = c.search(name, per_page=3)
            if resp and resp.results:
                for co in resp.results:
                    siren = getattr(co, "siren", None) or (
                        co.get("siren") if isinstance(co, dict) else None
                    )
                    if siren and len(str(siren)) == 9:
                        try:
                            from quotas import mark_used
                            mark_used("sirene")
                        except Exception:
                            pass
                        return str(siren)
    except Exception:
        pass

    return None


def _generate_queries_heuristic(icp_description: str, n: int = 5) -> list[str]:
    """Fallback heuristique (sans LLM) pour générer N variations de requête.

    Stratégie : extrait les mots-clés saillants de la description ICP et
    génère des combinaisons avec préfixes (cabinet/société/intégrateur)
    + suffixes ("France", "Paris", "PME").
    """
    desc = icp_description.lower()
    queries = []
    # 1. La description brute (max 80 chars Google)
    base = re.sub(r"\s+", " ", icp_description.strip())[:80]
    queries.append(base)
    # 2. Avec "France"
    if "france" not in desc and "fr" not in desc:
        queries.append(f"{base} France")
    # 3. Préfixes business
    for prefix in ("cabinet", "société", "intégrateur"):
        if prefix not in desc:
            queries.append(f"{prefix} {base}"[:80])
    # 4. Variation site:linkedin (mais on rejette linkedin → ça crée du contexte
    # mais on ne pourra pas l'utiliser → skip)
    # Dedup
    seen = set()
    uniq = []
    for q in queries:
        if q and q not in seen:
            seen.add(q)
            uniq.append(q)
    return uniq[:n]


def _generate_queries_llm(icp_description: str, n: int = 5) -> Optional[list[str]]:
    """Génération de queries via Claude Haiku (préféré quand dispo)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        from anthropic import Anthropic
        client = Anthropic()
        prompt = (
            f"Voici la description d'un ICP (Ideal Customer Profile) en français :\n"
            f"\"{icp_description}\"\n\n"
            f"Génère exactement {n} requêtes Google différentes qui aideraient "
            f"à trouver des entreprises matchant cet ICP. Chaque requête doit :\n"
            f"- être en français\n"
            f"- faire moins de 80 caractères\n"
            f"- utiliser des termes business spécifiques (pas génériques)\n"
            f"- viser de vraies sociétés (pas des annuaires)\n\n"
            f"Réponds avec UNE requête par ligne, sans numérotation, sans guillemets."
        )
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = (resp.content[0].text if resp.content else "").strip()
        queries = [q.strip(" -*\"'") for q in text.split("\n") if q.strip()]
        queries = [q for q in queries if 5 < len(q) <= 80]
        return queries[:n] if queries else None
    except Exception:
        return None


def reverse_source(
    icp_description: str,
    *,
    n_queries: int = 5,
    max_results_per_query: int = 15,
    max_total: int = 40,
) -> list[dict]:
    """Sourcing inversé : ICP NL → URLs Google → SIREN.

    Args:
        icp_description: description du profil cible (NL FR)
        n_queries: nombre de variations de requête à générer
        max_results_per_query: top N par requête (15 = balance qualité/coût)
        max_total: stop dès qu'on a N candidats résolus

    Returns:
        list of {nom_complet, siren, website, source_query, source_snippet}
        Le pipeline en aval s'attend à ce schéma (compat Sirene dict).
    """
    if not icp_description or not icp_description.strip():
        return []
    # v0.18.0 — utilise search_text qui fait fallback Serper→CSE→Brave→DDG
    # automatique. Quand Serper est épuisé, Brave (2000/mo free) prend la
    # relève sans rien faire.
    try:
        from brave_search import search_text
    except Exception:
        print("[ReverseSource] brave_search module missing — abort")
        return []

    # 1) Génération des queries (LLM préféré, fallback heuristique)
    queries = _generate_queries_llm(icp_description, n=n_queries)
    if not queries:
        queries = _generate_queries_heuristic(icp_description, n=n_queries)
    print(f"[ReverseSource] {len(queries)} queries générées :")
    for q in queries:
        print(f"  → {q}")

    # 2) Hit Serper pour chaque query, dédupe les URLs au passage
    seen_urls: set[str] = set()
    seen_sirens: set[str] = set()
    candidates: list[dict] = []
    n_serper_hits = 0
    n_url_kept = 0
    n_url_dropped_agg = 0
    n_resolved = 0
    n_unresolved = 0

    for q in queries:
        results = search_text(q, max_results=max_results_per_query)
        n_serper_hits += 1
        for r in results:
            # search_text returns 'href' (DDG/Brave), serper returns 'url'/'link'
            url = r.get("url") or r.get("link") or r.get("href")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            if not _is_business_url(url):
                n_url_dropped_agg += 1
                continue
            n_url_kept += 1
            # 3) Extraire nom de la boîte depuis la page
            name = _extract_company_name(url)
            if not name:
                # fallback: utiliser le title Google si on l'a
                title = (r.get("title") or "").strip()
                for sep in [" - ", " | ", " — "]:
                    if sep in title:
                        title = title.split(sep)[0].strip()
                        break
                name = title if 2 < len(title) < 80 else None
            if not name:
                n_unresolved += 1
                continue
            # 4) Résoudre vers SIREN via Pappers
            siren = _resolve_siren_from_name(name)
            if not siren or siren in seen_sirens:
                if not siren:
                    n_unresolved += 1
                continue
            seen_sirens.add(siren)
            candidates.append({
                "nom_complet": name,
                "siren": siren,
                "website": url.split("?")[0].rstrip("/"),
                "source_query": q,
                # search_text returns 'body' (DDG/Brave), serper returns 'snippet'
                "source_snippet": (r.get("snippet") or r.get("body") or "")[:200],
            })
            n_resolved += 1
            if len(candidates) >= max_total:
                break
        if len(candidates) >= max_total:
            break

    print(f"[ReverseSource] Done: {n_serper_hits} queries → "
          f"{n_url_kept} URLs kept ({n_url_dropped_agg} agg dropped) → "
          f"{n_resolved} resolved to SIREN ({n_unresolved} unresolved).")
    return candidates


def _cli() -> None:
    """CLI test : reverse-source from an ICP description."""
    import argparse
    import json
    p = argparse.ArgumentParser(description="Reverse source — ICP NL → URLs → SIREN")
    p.add_argument("--icp", required=True, help="Description ICP en français")
    p.add_argument("--n-queries", type=int, default=5)
    p.add_argument("--max-total", type=int, default=20)
    args = p.parse_args()

    candidates = reverse_source(
        args.icp,
        n_queries=args.n_queries,
        max_total=args.max_total,
    )
    print()
    print(f"=== {len(candidates)} candidates ===")
    for c in candidates:
        print(f"  • {c['nom_complet']:40s} SIREN={c['siren']} | {c['website']}")
    print()
    print(json.dumps(candidates, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
