# Prospect-Agent — Roadmap d'amélioration

> État actuel (v0.2.2) : pipeline fonctionnel end-to-end, ~30-60s par société,
> 30-50% de leads "gardés" (les autres droppent honnêtement faute de
> triangulation). Architecture validée dans Multica Cloud.
>
> Cette roadmap classe les améliorations par **ratio impact / effort**.
> Chaque ligne précise : ce qu'on gagne, ce que ça coûte (temps de dev),
> ce qu'il faut payer (API), et le bug actuel qu'elle résout.

---

## 🟢 SPRINT 1 — Quick wins (1 semaine, gros ROI)

Ces 4 améliorations multiplient la qualité par 2-3× sans changer l'archi.

### 1.1 — Intégrer Pappers API (FR companies → website direct)
**Bug actuel** : Sirene ne donne pas le site web. On le devine via DDG, qui est lent et imprécis (~30% de faux positifs sur les petites boîtes).
**Fix** : [Pappers API](https://www.pappers.fr/api) free tier = 100 requêtes/jour. Endpoint `/entreprise?siren=XXX` retourne `site_web` directement + `email` + `telephone` officiels.
**Gain attendu** : -10s par lookup (skip 1 DDG), +40% précision sur le website, +20% sur le téléphone.
**Effort** : 3h (nouveau module `pappers_client.py`, intégration dans `pipeline.enrich_company_partial`).
**Coût** : 0€ (free tier suffit pour < 100 leads/jour ; ensuite 19€/mois).

### 1.2 — Pipeline parallèle (3 sociétés en simultané)
**Bug actuel** : Chaque société est traitée séquentiellement → 30-60s × N companies.
**Fix** : `concurrent.futures.ThreadPoolExecutor(max_workers=3)` autour de `enrich_company_partial` et `finalize_lead`. Le throttle DDG reste global (verrou).
**Gain** : 3× plus rapide pour 30+ leads (15 min → 5 min pour 30 boîtes).
**Effort** : 2h (ajout `pipeline.run_batch(companies)` + ajustement du throttle DDG en process-safe).
**Coût** : 0€.

### 1.3 — Remplacer DDG par Brave Search API
**Bug actuel** : DDG est instable (mêmes requêtes → résultats différents), throttle agressif (1.5s minimum), parfois retourne 0 résultats.
**Fix** : [Brave Search API](https://brave.com/search/api/) free tier = 2 000 requêtes/mois. JSON propre, pas de throttle, résultats stables.
**Gain** : 0 retry inutile, +30% taux de réussite sur les LinkedIn/website lookups, 2× plus rapide en clock-time.
**Effort** : 4h (wrapper compatible avec l'interface DDG actuelle, fallback DDG si quota épuisé).
**Coût** : 0€ jusqu'à 2k/mois ; ensuite 3€/1000 requêtes.

### 1.4 — Cache persistant SQLite
**Bug actuel** : Si Claude relance la même requête (même secteur+géo), tout est re-calculé.
**Fix** : SQLite `data/cache.db` avec tables `companies`, `websites`, `socials`, `emails`. TTL 30j par défaut.
**Gain** : 10× plus rapide sur les re-runs, économise les API quotas.
**Effort** : 4h.
**Coût** : 0€.

**Total Sprint 1** : ~13h de dev, livrable en ~1 semaine, **résultat = pipeline 5× plus rapide + 2× plus fiable.**

---

## 🟡 SPRINT 2 — Qualité décideurs & enrichissement avancé (2-3 semaines)

### 2.1 — Intégrer Hunter.io pour les emails
**Bug actuel** : Notre SMTP probe est fragile (firewalls bloquent port 25 sur ~40% des domaines, catch-all → faux positifs).
**Fix** : [Hunter.io](https://hunter.io/api) free tier = 25 vérifications/mois + 25 recherches "domain → emails connus". Endpoint `/email-finder?first_name=X&last_name=Y&domain=Z` renvoie l'email vérifié directement.
**Gain** : Taux de délivrabilité passe de ~30% à 85%+ sur les emails retournés.
**Effort** : 3h.
**Coût** : 0€ jusqu'à 25 ; ensuite 49€/mois pour 500 verifications.

### 2.2 — Persona detection LLM dynamique
**Bug actuel** : Le mapping secteur→persona dans SKILL.md est statique (cabinets → gérant, SaaS → VP Sales). Pour les secteurs hybrides ou exotiques (ex : "studio de design produit"), Claude se rabat sur le dirigeant légal.
**Fix** : Avant le batch, faire 1 appel Claude avec le secteur + 3 exemples d'entreprises Sirene pour qu'il propose le persona idéal + 2-3 fallbacks. Le résultat est mémorisé pour le batch.
**Gain** : Bons décideurs au lieu de "gérant SAS" partout.
**Effort** : 4h.
**Coût** : ~0,01€ par batch (1 appel Sonnet).

### 2.3 — Web Archive fallback
**Bug actuel** : Si le site officiel est down (~10% des petites boîtes), on perd toute l'enrichissement web.
**Fix** : Si HEAD échoue, retry sur `https://web.archive.org/web/2024/<url>`.
**Gain** : Récupère 50% des cas perdus.
**Effort** : 2h.
**Coût** : 0€.

### 2.4 — Phone normalization & dedup intelligente
**Bug actuel** : Le regex phone récupère beaucoup de bruit (dates, IDs). On garde le premier match.
**Fix** : Utiliser `phonenumbers` lib pour parser, normaliser, dédupliquer par numéro. Garder le numéro le PLUS FRÉQUENT dans la page.
**Gain** : +80% précision sur le téléphone.
**Effort** : 2h.
**Coût** : 0€ (lib open-source).

### 2.5 — Email patterns par domaine (catch-all detection)
**Bug actuel** : Pour les domaines catch-all (tous les emails sont "deliverable"), on retourne le premier, souvent faux.
**Fix** : Détecter catch-all (probe 1 fake@domain). Si catch-all, ne PAS retourner d'email (confidence trop basse). Alternative : feed les emails déjà connus du domaine à Claude pour qu'il devine le pattern dominant.
**Gain** : Élimine les faux positifs catch-all.
**Effort** : 3h.
**Coût** : 0€.

### 2.6 — Streaming output vers Sheets
**Bug actuel** : Tout est exporté à la fin. Si le run plante au milieu, on perd tout.
**Fix** : `export_leads` accepte un mode "append" qui pousse chaque lead dès qu'il est finalisé.
**Gain** : Robustesse + UX (l'utilisateur voit ses leads arriver en live).
**Effort** : 3h.
**Coût** : 0€.

**Total Sprint 2** : ~17h, **résultat = qualité décideurs niveau Apollo, robustesse production.**

---

## 🔵 SPRINT 3 — International + go-to-market (1 mois)

### 3.1 — UK : Companies House API
**Pourquoi** : 1ère extension géo logique. API officielle, gratuite, exhaustive.
**Effort** : 6h (nouveau module `companies_house_client.py`, adapter le triangulation flow).
**Coût** : 0€.

### 3.2 — Germany : Bundesanzeiger + OpenCorporates
**Effort** : 8h (deux sources hétérogènes à fusionner).
**Coût** : 0€ (OpenCorporates free tier 500/jour).

### 3.3 — Apollo API pour décideurs déjà mappés
**Pourquoi** : Apollo a la majorité des décideurs SaaS B2B déjà identifiés avec emails vérifiés. Court-circuite tout notre pipeline pour ces cas.
**Fix** : Avant SMTP probe, query Apollo `/people/match`. Si trouvé avec verified email → confidence 90.
**Gain** : 80% des leads SaaS d'un coup.
**Effort** : 6h.
**Coût** : Free tier 60 credits/mois ; ensuite 49€/mois.

### 3.4 — Setup wizard Google Sheets OAuth (vs service account)
**Pourquoi** : Plus simple pour les clients non-techniques. Service account demande de partager le sheet à un email cryptique.
**Effort** : 8h (flow OAuth installé localement avec navigation web).
**Coût** : 0€.

### 3.5 — Slack / Discord notifications
**Effort** : 3h.

**Total Sprint 3** : ~31h, **résultat = produit international + intégrations enterprise.**

---

## 🟣 LONG TERME — Vision produit (3-6 mois)

### Niveau "compétitif Apollo/Clay" :

| Feature | Effort | Pourquoi c'est différenciant |
|---|---|---|
| **Web UI propre** (Next.js + FastAPI) | 3 sem | Permet de vendre à des non-tech sans passer par Multica |
| **Bright Data proxies pour LinkedIn Sales Nav scraping** | 1 sem + ~200€/mois | Récupère les décideurs précis qu'Apollo n'a pas |
| **Intent signals** (job changes, funding, hiring) via NewsAPI + Clearbit | 2 sem | Lead scoring "qui est CHAUD" |
| **Auto-séquence d'outreach** (intégration Lemlist / Smartlead) | 2 sem | One-stop-shop : prospect → email envoyé |
| **CRM bi-directionnel** (HubSpot, Pipedrive, Salesforce) | 1 sem chacun | Push leads + statut |
| **Live LinkedIn Connect** (Phantombuster / La Growth Machine) | 1 sem | Demande de connexion auto sur lead chaud |
| **Score de qualité empirique** (track ouvertures / réponses) | 1 sem | Boucle d'amélioration continue |

### Niveau "10× la concurrence" — bets risqués :

1. **Agent qui négocie le RDV** — détecter une réponse positive et envoyer un Calendly auto. (Demande très bien gérer la sécu et la conformité.)
2. **Prédiction "qui va churn" sur les comptes B2B existants** — modèle ML sur les signaux publics (équipe qui change, posts négatifs).
3. **Marketplace de campagnes** — autres utilisateurs publient leurs requêtes performantes, monétisable.

---

## 📊 Métriques à instrumenter dès maintenant

Pour piloter la roadmap, ajouter dans le pipeline (`data/metrics.db`) :

| Métrique | Pourquoi |
|---|---|
| `time_per_company_seconds` (p50, p95) | Détecter régressions perf |
| `dropped_pct` & top 5 `drop_reason` | Voir où la triangulation est trop stricte |
| `field_confidence` distribution (par champ) | Cibler les sources à améliorer |
| `email_deliverability_rate` (après campagne réelle) | Calibrer le SMTP probe |
| `api_quota_used_pct` (Pappers, Brave, Hunter, Apollo) | Anticiper le passage en payant |

---

## 🎯 Priorisation recommandée

Si tu n'as que **1 semaine** : Sprint 1 (1.1 + 1.2 + 1.3).
Si tu as **1 mois** : Sprint 1 + Sprint 2.
Si tu as **3 mois** : tout sauf Long Terme.
Si tu vises **revenue** : Sprint 1 puis ouvrir UN secteur vertical (ex: cabinets RH) à fond, livrer 50 leads ultra-qualifiés à 2-3 vrais clients pour valider le pricing.

## 🚧 Anti-patterns à éviter

- ❌ Ajouter **plus de sources** sans améliorer la triangulation. Plus de sources noisy = pire qualité.
- ❌ Scraper LinkedIn directement. **Ban LinkedIn = mort du produit.**
- ❌ Promettre un volume X/jour avant d'avoir mesuré la délivrabilité réelle.
- ❌ Coder le multi-tenant avant d'avoir 3 clients qui utilisent vraiment l'outil.
- ❌ Faire confiance aux emails "catch-all" sans le flag.

---

*Roadmap rédigée pour prospect-agent v0.2.2 — à actualiser à chaque sprint clos.*
