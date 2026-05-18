# Prospect Agent — full reference (lazy-loaded by Claude)

> This file is **NOT** loaded into Claude's context by default. The agent reads
> it only when it needs deep details (custom persona, debugging, edge cases).
> Keeping it out of the hot path saves tokens.

## Full workflow (manual mode)

### Step 1 — Parse the request (you, the LLM)

Extract:
- **sector / activity** (mapping to NAF code if FR known)
- **geo** (city, dept, region)
- **persona target** (the role of the decision-maker to find — see Step 2)
- **volume target** (default 10 if not specified)
- **required fields** (email, phone, LinkedIn, Instagram…)
- **filters** (size, age, revenue, etc.)

### Step 2 — Persona disambiguation

Given the sector, pick the realistic operational decision-maker. Use your
judgment, don't blindly pick the legal director. Examples:

| Sector | Typical operational decider |
|---|---|
| Resto / café / commerce local | Propriétaire / gérant |
| Cabinet dentaire / médical / juridique | Associé / titulaire (souvent = dirigeant légal) |
| SaaS B2B 50-200 salariés | VP Sales, Head of Growth, CTO |
| Industrie | Directeur achats, Directeur production |
| Agence comm / marketing | Fondateur, Directeur |
| Hôtellerie de luxe / palace | Directeur général de l'établissement |

If the sector is ambiguous, ask the user to confirm.

### Step 3 — Source companies (FR via Sirene)

```python
from sirene_client import SireneClient
with SireneClient() as c:
    resp = c.search(
        query="cabinet dentaire",
        code_postal="69001",      # or departement/region
        per_page=20,              # max 25
    )
companies = resp.results
```

### Step 4 — Partial enrichment per company

```python
from pipeline import enrich_companies_parallel
partials = enrich_companies_parallel(companies, max_workers=3)
```

Each `partial` is a dict with: company_name, siren, naf, city, address, size,
legal_dirigeants, website, web_enrichment (emails/phones/socials),
team_page_text, company_linkedin/instagram/phone (ScoredField dicts),
company_official_email (from Pappers if available).

### Step 5 — Identify the decision-maker (you, the LLM)

Read `partial["team_page_text"]` and `partial["legal_dirigeants"]`. Decide:
- Who matches the persona requested?
- What's their first name, last name, role?
- What sources support this choice?

If only the legal director is available and the persona matches, use them with
`sources = ["sirene"]`. If you can't identify a credible decision-maker, **do
not invent one** — skip the lead.

### Step 6 — Finalize the lead

```python
from pipeline import finalize_lead
lead = finalize_lead(
    partial,
    person_first="Marie",
    person_last="Dupont",
    person_role="Gérante",
    person_sources=["website-team-page", "sirene"],
    naf_label="Pratique dentaire",
)
```

`finalize_lead` will:
- Generate email patterns + verify via SMTP
- Auto-triangulate the name against website team page, emails, LinkedIn slug
- Search the person's LinkedIn / Instagram URL via DDG/Brave
- Compute confidence per field
- Set `dropped=True` if quality thresholds aren't met

### Step 7 — Export

```python
from sheets_export import export_leads
csv_path = export_leads([l for l in leads if not l.dropped])
print(csv_path)
# Also created: same path with .xlsx extension
```

## Module reference

| Script | Purpose | CLI |
|---|---|---|
| `run_campaign.py` | Full pipeline end-to-end (the 90% path) | `python run_campaign.py --query "..." --output ...` |
| `sirene_client.py` | FR company search via Sirene API | `python sirene_client.py "query" --code-postal 69001` |
| `pappers_client.py` | Direct website/email/phone via Pappers | `python pappers_client.py 819586298` |
| `website_finder.py` | Find a company's website via Brave/DDG | `python website_finder.py "Acme" --city Lyon` |
| `web_enrichment.py` | Scrape a website for emails/phones/socials | `python web_enrichment.py https://example.com` |
| `social_finder.py` | Find LinkedIn/Insta URLs via search | `python social_finder.py company-linkedin "Acme"` |
| `email_finder.py` | Email pattern gen + SMTP verify | `python email_finder.py Marie Dupont example.com` |
| `pipeline.py` | Glue helpers | `python pipeline.py partial --siren 819586298` |
| `brave_search.py` | Brave Search wrapper with DDG fallback | `python brave_search.py "query"` |
| `triangulation.py` | Lead / ScoredField models | (importable only) |
| `sheets_export.py` | Excel-FR friendly CSV + XLSX | (importable only) |
| `setup_wizard.py` | First-run credentials check | `python setup_wizard.py --check` |

## Confidence scoring

- **verified (≥80)** : 2+ sources agree
- **partial (50-79)** : 1 source + active verification (e.g., SMTP deliverable)
- **unverified (<50)** : single source, no corroboration
- **missing (0)** : no source

Auto-drop thresholds (in `triangulation.Lead.evaluate`):
- `person_name.confidence < 60` → dropped
- Best contact channel (email/phone/linkedin) `< 60` → dropped

## Hard rules — never do these

- **NEVER write a comma-delimited CSV** (`csv.writer(fh)` default). Excel FR
  reads it as one giant column. Use `;` + BOM, or call `export_leads()`.
- **NEVER scrape LinkedIn profile pages** (ToS violation, ban risk). Only use
  LinkedIn URLs discovered via DDG/Brave search.
- **NEVER invent emails, phones, names** even "plausible-looking" ones.
- **NEVER commit `.env`** — it's gitignored.
- **NEVER print or echo Anthropic / Google credentials** in your output.

## Costs cheat sheet (per task)

With `run_campaign.py` (1 tool call) + Haiku:  ~$0.10
With `run_campaign.py` + Sonnet: ~$0.30
Per-step manual flow + Sonnet: $2-5 (heavy iteration cost)
Per-step manual flow + Opus: $10-20 (avoid)

## Phase 1 scope (this version)

- **France only** via Sirene. International is Phase 2.
- **Single-pass enrichment** (no human-in-the-loop confirmation).
- **No CRM integration** beyond Google Sheets / CSV / XLSX.
- **Email verification = SMTP probe** (catch-all domains flagged honestly).
