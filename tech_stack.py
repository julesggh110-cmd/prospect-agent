"""
Tech stack detector — Wappalyzer-LITE in pure Python (no Playwright).

Why a LITE version: the full Wappalyzer-Next requires Playwright + headless
Chromium, heavy for our use case. We already FETCH the company homepage in
web_enrichment.py — we just need to apply regex matches on the HTML we
already have. ~80% of Wappalyzer's coverage, ~0% of the overhead.

What we detect → why it matters for prospect qualification:

  CRM / Marketing Stack:
    - HubSpot, Salesforce, Pipedrive → mature B2B → good for AI/automation pitch
    - Mailchimp, SendinBlue, Brevo → email-marketing already → upsell automation
    - Zoho, Monday → mid-market business → fit conseil + RGPD

  E-commerce:
    - Shopify, WooCommerce, PrestaShop, Magento → online merchant → automation needs
    - Stripe → modern payment stack → tech-forward
    - PayPlug, Stancer → French stack → local buyer

  Analytics / Optim:
    - Google Analytics 4, GTM → data-aware
    - Hotjar, Crazy Egg → CRO mature

  Automation:
    - Zapier, Make (Integromat), n8n → ALREADY automating → READY for AI level 3
    - Pipedream → tech-savvy team

  CMS:
    - WordPress + WooCommerce → SMB / TPE
    - Webflow → design-forward, modern
    - Drupal → institutional / corporate

  Frameworks:
    - React, Next.js, Vue → SaaS / tech company
    - Symfony, Laravel → French dev shop / agency

Each detection gets a CATEGORY + a SCORE (1-5 confidence). Surfaced on the
Lead as `tech_stack` dict and `tech_signals` list (used by ICP scoring +
cold email personalization).

Public API:
    detect_tech_from_html(html: str) -> dict
"""
from __future__ import annotations

import re
from typing import Optional


# Each entry: (technology_name, category, regex_pattern, confidence_1_to_5)
# Patterns are case-insensitive. Order matters only when multiple match the
# same category; we keep all matches.
_PATTERNS: list[tuple[str, str, str, int]] = [
    # === CRM / Marketing ===
    ("HubSpot",        "crm",       r"js\.hsforms\.net|track\.hubspot|hs-script", 5),
    ("Salesforce",     "crm",       r"force\.com|salesforce\.com/\.well-known|pardot", 5),
    ("Pipedrive",      "crm",       r"pipedrive\.com|leadbooster", 5),
    ("Zoho CRM",       "crm",       r"zoho\.com/crm|zohostatic", 4),
    ("Monday.com",     "crm",       r"monday\.com/embed|cdn\.monday\.com", 4),
    ("Mailchimp",      "email-mkt", r"mailchimp\.com|chimpstatic|mc\.us\d+\.list-manage", 5),
    ("Brevo (Sendinblue)", "email-mkt", r"sendinblue\.com|brevo\.com|sib-api", 5),
    ("Sarbacane",      "email-mkt", r"sarbacane\.com", 5),
    ("ActiveCampaign", "email-mkt", r"activehosted\.com|active-campaign", 5),
    ("Lemlist",        "email-mkt", r"lemlist\.com", 5),

    # === E-commerce ===
    ("Shopify",        "ecommerce", r"cdn\.shopify\.com|myshopify\.com", 5),
    ("WooCommerce",    "ecommerce", r"woocommerce|wc-block-grid|wp-content/plugins/woocommerce", 5),
    ("PrestaShop",     "ecommerce", r"prestashop|/themes/.+?/css/", 4),
    ("Magento",        "ecommerce", r"magento|/skin/frontend|Mage\.Cookies", 5),
    ("Wix",            "ecommerce", r"wixstatic\.com|wix\.com", 4),

    # === Payment ===
    ("Stripe",         "payment",   r"js\.stripe\.com|stripe\.network|sk_live_", 5),
    ("PayPlug",        "payment",   r"payplug\.com", 5),
    ("Stancer",        "payment",   r"stancer\.com", 5),
    ("Lemonway",       "payment",   r"lemonway\.com|api-rec\.lemonway", 5),
    ("Mollie",         "payment",   r"mollie\.com|js\.mollie", 5),

    # === Analytics & CRO ===
    ("Google Analytics 4", "analytics", r"googletagmanager\.com|gtag\(|G-[A-Z0-9]{10}", 4),
    ("Matomo",         "analytics", r"matomo\.org|/matomo\.js|piwik\.php", 4),
    ("Hotjar",         "analytics", r"static\.hotjar\.com|hjid", 5),
    ("Crazy Egg",      "analytics", r"crazyegg\.com|cetrk\.com", 5),
    ("Plausible",      "analytics", r"plausible\.io/js", 4),

    # === Automation ===
    ("Zapier",         "automation", r"zapier\.com/embed|zapierhooks", 5),
    ("Make.com (Integromat)", "automation", r"make\.com/embed|integromat\.com", 5),
    ("n8n",            "automation", r"n8n\.io|n8n-cloud", 5),
    ("Pipedream",      "automation", r"pipedream\.com/embed", 5),

    # === Live chat / customer support ===
    ("Intercom",       "chat",      r"intercom\.io|intercom-cdn|widget\.intercom", 5),
    ("Crisp Chat",     "chat",      r"crisp\.chat|client\.crisp\.chat", 5),
    ("Zendesk",        "chat",      r"zendesk\.com|zd-cdn", 5),
    ("Tawk.to",        "chat",      r"tawk\.to", 5),
    ("Drift",          "chat",      r"drift\.com|js\.driftt", 5),

    # === CMS ===
    ("WordPress",      "cms",       r"wp-content/|wp-includes/|generator.+wordpress", 5),
    ("Webflow",        "cms",       r"webflow\.com|w-mod-js", 5),
    ("Drupal",         "cms",       r"drupal\.js|sites/all/themes|generator.+drupal", 5),
    ("Squarespace",    "cms",       r"squarespace\.com|sqsp\.net", 5),
    ("Strikingly",     "cms",       r"strikingly\.com", 4),

    # === Frameworks ===
    ("React",          "framework", r"react\.production|/react/|reactdom", 4),
    ("Next.js",        "framework", r"_next/static|__NEXT_DATA__|next/router", 5),
    ("Vue.js",         "framework", r"vue\.runtime|__vue__", 4),
    ("Angular",        "framework", r"ng-version|angular\.io", 4),
    ("Symfony",        "framework", r"symfony|sf-toolbar", 4),
    ("Laravel",        "framework", r"laravel_session|/livewire/", 4),

    # === Booking / scheduling ===
    ("Calendly",       "booking",   r"calendly\.com/embed", 5),
    ("Cal.com",        "booking",   r"cal\.com/embed", 5),
    ("Doctolib",       "booking",   r"doctolib\.fr|doctolib\.com", 5),
    ("Acuity",         "booking",   r"acuityscheduling\.com", 5),

    # === Cloud infra hints ===
    ("Cloudflare",     "infra",     r"cdnjs\.cloudflare|__cf_chl|/cdn-cgi/", 3),
    ("AWS CloudFront", "infra",     r"cloudfront\.net", 3),
    ("Vercel",         "infra",     r"vercel\.com|/_vercel/", 4),
    ("Netlify",        "infra",     r"netlify\.app|netlifyusercontent", 4),

    # === French B2B specifics ===
    ("Axeptio (cookies)", "compliance", r"axept\.io|axeptio\.eu", 5),
    ("Tarteaucitron",     "compliance", r"tarteaucitron", 5),
    ("Didomi",            "compliance", r"didomi\.io|sdk\.privacy-center\.org", 5),
]


def detect_tech_from_html(html: str) -> dict:
    """Scan HTML for known tech stack signatures.

    Returns:
        {
            "stack": [
                {"name": "HubSpot", "category": "crm", "confidence": 5},
                {"name": "Stripe", "category": "payment", "confidence": 5},
                ...
            ],
            "categories": {
                "crm": ["HubSpot"],
                "payment": ["Stripe", "PayPlug"],
                ...
            },
            "signals": [
                "has-crm", "has-payment", "has-automation", ...
            ],
            "primary_cms": "WordPress",       # most reliable CMS match
        }
    """
    if not html:
        return {"stack": [], "categories": {}, "signals": [], "primary_cms": None}

    stack: list[dict] = []
    categories: dict[str, list[str]] = {}
    for name, category, pattern, conf in _PATTERNS:
        try:
            if re.search(pattern, html, re.IGNORECASE):
                stack.append({"name": name, "category": category, "confidence": conf})
                categories.setdefault(category, []).append(name)
        except re.error:
            continue

    # Derive coarse signals (used by ICP scoring)
    signals = sorted({f"has-{cat}" for cat in categories.keys()})

    # Primary CMS = highest-confidence CMS match
    primary_cms = None
    cms_matches = [s for s in stack if s["category"] == "cms"]
    if cms_matches:
        primary_cms = max(cms_matches, key=lambda s: s["confidence"])["name"]

    return {
        "stack": stack,
        "categories": categories,
        "signals": signals,
        "primary_cms": primary_cms,
    }


def maturity_score(tech_result: dict) -> int:
    """Compute a 0-100 'tech maturity' score from detected stack.

    Used by ICP scoring to identify boîtes "tech-ready" (good fit for AI
    automation pitches) vs "old-school" (good fit for level-1 discovery).
    """
    if not tech_result or not tech_result.get("stack"):
        return 0
    sig_weights = {
        "has-automation": 30,      # already using Zapier/Make/n8n → AI-ready
        "has-crm": 15,
        "has-email-mkt": 10,
        "has-payment": 10,
        "has-analytics": 10,
        "has-ecommerce": 10,
        "has-chat": 5,
        "has-framework": 10,        # modern stack → tech-forward
        "has-booking": 5,
        "has-compliance": 5,        # GDPR-aware = mature
    }
    signals = set(tech_result.get("signals") or [])
    score = sum(w for s, w in sig_weights.items() if s in signals)
    return min(100, score)


def pitch_hint_from_tech(tech_result: dict) -> Optional[str]:
    """Return a one-liner the cold email generator can use to personalise.

    Example outputs:
      "utilise déjà Zapier → notre niveau 3 montre comment connecter des
       agents IA à des workflows existants"
      "stack moderne (Next.js + Stripe) → niveau 2-3 pertinent"
      "WordPress only → niveau 1 découverte conseillé"
    """
    if not tech_result or not tech_result.get("stack"):
        return None
    cats = tech_result.get("categories") or {}
    if "automation" in cats:
        tool = cats["automation"][0]
        return (
            f"Vous utilisez déjà {tool} : notre niveau 3 montre comment "
            f"brancher des agents IA custom sur vos workflows existants."
        )
    if "crm" in cats and "automation" not in cats:
        tool = cats["crm"][0]
        return (
            f"Votre stack {tool} est mûre pour un branchement IA : notre "
            f"niveau 2 traite l'enrichissement automatique + scoring lead."
        )
    if tech_result.get("primary_cms") == "WordPress" and len(cats) <= 2:
        return (
            "Vous êtes encore sur une stack WordPress classique : commencer "
            "par notre niveau 1 (Découverte) est probablement le bon point "
            "d'entrée pour identifier les use-cases IA pertinents."
        )
    if any(f in cats.get("framework", []) for f in ("Next.js", "React", "Vue.js")):
        return (
            "Stack technique moderne — votre équipe peut directement adresser "
            "le niveau 3 (automatisations agents IA + intégrations API)."
        )
    return None


def _cli() -> None:
    import argparse
    import json
    import httpx
    import warnings
    warnings.filterwarnings("ignore")
    p = argparse.ArgumentParser(description="Tech stack detector (lite)")
    p.add_argument("url", help="URL to scan")
    args = p.parse_args()
    try:
        r = httpx.get(args.url, timeout=10, follow_redirects=True, verify=False,
                       headers={"User-Agent": "Mozilla/5.0"})
        html = r.text
    except Exception as e:
        print(f"fetch error: {e}")
        return
    result = detect_tech_from_html(html)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print()
    print(f"Maturity score: {maturity_score(result)}/100")
    hint = pitch_hint_from_tech(result)
    if hint:
        print(f"Pitch hint: {hint}")


if __name__ == "__main__":
    _cli()
