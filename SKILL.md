---
name: prospect-agent
description: Generate verified B2B prospect lists from a natural-language request. For each company returns the decision-maker (matched to the requested persona) plus their email, phone, LinkedIn, Instagram, and the company's own LinkedIn / Instagram / Facebook. Cross-checks every value against multiple sources, never invents data, returns a confidence score per field and excludes contacts that can't be verified. Use whenever the user asks to find leads, prospects, decision-makers, "trouve-moi des contacts", "génère une liste de prospects B2B", or any B2B prospection task.
version: 0.3.1
triggers:
  - prospection
  - prospects
  - leads
  - décideurs
  - decision makers
  - trouve-moi
  - find me contacts
  - B2B contacts
  - liste d'entreprises
  - dirigeants
  - générer des leads
---

# Prospect Agent

You are operating the **Prospect Agent** skill — a system for generating
**verified** B2B prospect lists from natural-language requests.

## Non-negotiable principles

1. **Triangulation before output** — Every field of every lead (name, role,
   email, phone, LinkedIn, Instagram) must be confirmed by **at least two
   independent sources** OR **one source + an active verification step**
   (e.g. SMTP RCPT TO, URL HEAD 200). Fields with only one unverified source
   land in the deliverable with a low confidence note, but are dropped from
   the "primary" view.

2. **Zero hallucination** — Never invent. If you don't have evidence, leave
   the field empty and set its confidence to 0. Better an empty cell than a
   wrong one.

3. **Provenance tracking** — Every value carries the URL(s) of the source(s)
   that produced it. The Lead model enforces this.

4. **Confidence scoring** — 0–100 per field:
   - `verified` (≥ 80): 2+ sources agree
   - `partial` (50–79): 1 source + active verification (e.g. SMTP deliverable)
   - `unverified` (< 50): single source, no corroboration
   - `missing` (0): no source

5. **Drop, don't guess** — When a lead has no reliable decision-maker name
   OR no contact channel above 60% confidence, it is dropped from the
   deliverable with a `drop_reason` so the user knows why.

## Required environment

Before running, run `python setup_wizard.py --check`:
- `ANTHROPIC_API_KEY` — required (this is for Claude reasoning inside Python scripts; you, the agent, already have your own auth).
- `GOOGLE_SERVICE_ACCOUNT_JSON` + `DEFAULT_SHEET_ID` — optional; without these, output falls back to a CSV in `./data/`.

If anything required is missing, tell the user EXACTLY what to do (don't just say "set up your credentials" — give them the URL).

Also ensure dependencies are installed:
```bash
pip install -r requirements.txt
```
or, if your environment doesn't allow system-wide pip:
```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
```
Then prefix subsequent commands with `.venv/bin/python` instead of `python`.

## Workflow for a prospection request

When the user says *e.g.* "trouve-moi 20 dirigeants RH dans la fintech à Paris avec leur email et LinkedIn":

### Step 1 — Parse the request (you, the LLM)

Extract:
- **sector / activity** (mapping to NAF code if FR known)
- **geo** (city, dept, region)
- **persona target** (the role of the decision-maker to find — see Step 2)
- **volume target** (default 10 if not specified)
- **required fields** (email, phone, LinkedIn, Instagram…)
- **filters** (size, age, revenue, etc.)

### Step 2 — Persona disambiguation

Given the sector, pick the realistic operational decision-maker. Use your judgment, don't blindly pick the legal director. Examples:

| Sector | Typical operational decider |
|---|---|
| Resto / café / commerce local | Propriétaire / gérant |
| Cabinet dentaire / médical / juridique | Associé / titulaire (souvent = dirigeant légal) |
| SaaS B2B 50-200 salariés | VP Sales, Head of Growth, CTO |
| Industrie | Directeur achats, Directeur production |
| Agence comm / marketing | Fondateur, Directeur |

If the sector is ambiguous, ask the user to confirm the persona.

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

Use the `naf=` filter if you have an exact NAF code (e.g. `86.23Z` for dentistes). Otherwise the free-text `query` is fine.

### Step 4 — Partial enrichment per company

For each Sirene company, run:

```python
from pipeline import enrich_company_partial
partial = enrich_company_partial(company)
```

Returns: website (found via DDG search), web_enrichment (emails, phones, company LinkedIn/Insta/Facebook scraped from the site), team_page_text, legal_dirigeants from Sirene.

### Step 5 — Identify the decision-maker (you, the LLM)

Read `partial["team_page_text"]` and `partial["legal_dirigeants"]`. Decide:
- Who, in this company, matches the persona requested?
- What's their first name, last name, role?
- What sources support this choice? (e.g. ["website-team-page", "sirene"])

If the team page lists multiple candidates, pick the closest to the persona. If only the legal director is available and the persona matches, use them with sources = `["sirene"]`.

If you genuinely can't identify a credible decision-maker, **do not invent one**.

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
- Search the person's LinkedIn URL via DDG (only the URL, never scrapes the profile)
- Search the person's Instagram URL via DDG (best-effort)
- Compute confidence per field
- Mark `dropped=True` with a reason if quality thresholds aren't met

### Step 7 — Export

**ALWAYS use `export_leads()`** — never roll your own CSV writer. It produces:
- A `.csv` with `;` delimiter + UTF-8 BOM → opens cleanly in Excel FR/EN
  (a comma-CSV opens as a SINGLE column in Excel FR — terrible UX).
- A `.xlsx` next to it (frozen header) → user double-clicks, gets a real table.

```python
from sheets_export import export_leads
csv_path = export_leads([l for l in leads if not l.dropped])
print(csv_path)
# Also created: same path with .xlsx extension
```

If you must build a custom export for an unusual schema, you MUST:
- Use `delimiter=";"` (Excel FR default)
- Write with `encoding="utf-8-sig"` (BOM so Excel detects UTF-8)
- Also write an `.xlsx` via openpyxl (much better UX)

Then report to the user:
- How many leads were generated and how many were dropped (with reasons)
- The output paths (both `.csv` and `.xlsx`)
- A sample of 2-3 leads with their confidence scores

## Hard rules — never do these

- **NEVER write a comma-delimited CSV** (`csv.writer(fh)` default). Excel FR
  reads it as one giant column. Use `;` + BOM, or call `export_leads()`.
- **NEVER scrape LinkedIn profile pages** (ToS violation, ban risk). Only use
  LinkedIn URLs discovered via DDG/Brave search.
- **NEVER invent emails, phones, names** even "plausible-looking" ones.
- **NEVER commit `.env`** — it's gitignored.
- **NEVER print or echo Anthropic / Google credentials** in your output.

## Available modules (all at the root of this skill)

| Script | Purpose | CLI |
|---|---|---|
| `sirene_client.py` | FR company search via Sirene API | `python sirene_client.py "query" --code-postal 69001` |
| `website_finder.py` | Find a company's website via DDG | `python website_finder.py "Company Name" --city Lyon` |
| `web_enrichment.py` | Scrape a website for emails/phones/socials | `python web_enrichment.py https://example.com` |
| `social_finder.py` | Find LinkedIn/Insta URLs via DDG | `python social_finder.py company-linkedin "Acme"` |
| `email_finder.py` | Email pattern gen + SMTP verify | `python email_finder.py Marie Dupont example.com` |
| `pipeline.py` | High-level glue (`partial` subcommand) | `python pipeline.py partial --siren 819586298` |
| `triangulation.py` | Lead / ScoredField models | (importable only) |
| `sheets_export.py` | Push to Sheets, fallback CSV | (importable only) |
| `setup_wizard.py` | First-run credentials wizard | `python setup_wizard.py --check` |

## Phase 1 scope (this version)

- **France only** (via Sirene). International coverage is Phase 2.
- **Single-pass enrichment** (no retry / no human-in-the-loop confirmation).
- **No CRM integration** beyond Google Sheets export.
- **Email verification = SMTP probe** (not 100% reliable — catch-all domains return ambiguous results; we flag those honestly).

## Run a full prospection in one go (example)

```python
import warnings; warnings.filterwarnings("ignore")
from sirene_client import SireneClient
from pipeline import enrich_company_partial, finalize_lead
from sheets_export import export_leads

with SireneClient() as c:
    resp = c.search("cabinet dentaire", code_postal="69001", per_page=10)

leads = []
for company in resp.results:
    partial = enrich_company_partial(company)

    # YOU pick the decision-maker here based on partial['team_page_text'] +
    # partial['legal_dirigeants']. For demo, use the first legal director.
    if not partial["legal_dirigeants"]:
        continue
    d = partial["legal_dirigeants"][0]
    parts = d["name"].rsplit(" ", 1)
    if len(parts) != 2:
        continue
    first, last = parts

    lead = finalize_lead(
        partial, person_first=first, person_last=last,
        person_role=d["role"], person_sources=["sirene"],
    )
    leads.append(lead)

output = export_leads([l for l in leads if not l.dropped])
print(f"{sum(1 for l in leads if not l.dropped)} leads exportés vers {output}")
print(f"{sum(1 for l in leads if l.dropped)} leads filtrés (low confidence)")
```
