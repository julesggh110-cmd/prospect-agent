---
name: prospect-agent
description: Generate verified B2B prospect lists from a natural-language request. Returns decision-maker + email + phone + LinkedIn + Instagram per company, with confidence scores and provenance. Never invents data. Use when the user asks to find leads, prospects, decision-makers, "trouve-moi des contacts", "génère une liste de prospects B2B".
version: 0.15.0
triggers:
  - prospection
  - prospects
  - leads
  - décideurs
  - trouve-moi
  - liste d'entreprises
  - dirigeants
  - cold email
  - cold emails
---

# Prospect Agent

Verified B2B prospect lists from natural-language requests.

## 🚨 The ONE thing to do (90% of cases)

Run `run_campaign.py` once. It handles the full pipeline (Sirene → Pappers → Brave → SMTP verify → ICP scoring → dedup vs lead_store → optional HubSpot sync → export). Example:

```bash
python run_campaign.py \
  --query "caviste premium" --code-postal 75001 --volume 20 \
  --icp-preset cavistes-paris \
  --only-new \
  --push-to-hubspot \
  --generate-emails --sender-company "Bear Brothers" \
  --output cavistes-paris-1
```

Key flags:
- `--icp-preset {cavistes-paris,palaces-paris}`: score each lead 0-100 vs an ideal profile; sorted by score in the output.
- `--only-new`: skip companies already in `data/leads.db` (dedup across runs).
- `--push-to-hubspot`: create Contacts + Companies + audit Notes in HubSpot (needs `HUBSPOT_ACCESS_TOKEN`).
- `--generate-emails`: draft a personalized FR cold email per kept lead with Claude Haiku (~$0.0015/lead). Email subject + body land in the XLSX as new columns. Requires `ANTHROPIC_API_KEY`.
- `--sender-offer` / `--sender-company`: one-line description of what you sell + your brand name. Customize per campaign.
- `--tranche-effectif {00,01,02,03,11,12,...}`: filter SMB size (use `11` = 10-19 emp to skip chains/groups).

The script writes `output/<stem>.csv` (Excel-FR) + `output/<stem>.xlsx` (premium: hyperlinks active, color-graded confidence cells, filterable header). **Do not write your own CSV** — Excel FR reads comma-CSVs as a single column.

## Non-negotiable rules

1. **Triangulation** — every field needs ≥2 sources OR 1 source + active verification (SMTP, HTTP). Otherwise it lands `unverified`.
2. **Zero hallucination** — empty field beats wrong field. Always.
3. **Drop, don't guess** — leads with no reliable decision-maker name OR no contact >60% confidence are auto-dropped with `drop_reason`.
4. **NEVER scrape LinkedIn/Insta profile pages.** Only discover URLs via search.

## When to NOT use `run_campaign.py` (the 10% case)

Use the modules directly when you need:
- **Custom persona detection** (sector ≠ legal director) — call `pipeline.enrich_company_partial` per company, read `team_page_text` + `legal_dirigeants`, pick the right person yourself, then `pipeline.finalize_lead`.
- **Multi-pass enrichment** (re-run dropped leads with relaxed thresholds).
- **Non-FR companies** — Sirene is France-only. International is Phase 2 (TBD).

See `DETAILS.md` for the full workflow, module reference, and examples.

## Environment

Required at runtime: nothing — the agent works with zero keys (just slower).
**All free tiers below are no-card-required.** Recommended setup:

**Search & business data**:
- `SERPER_API_KEY` → search backend (LinkedIn/Insta). 2,500 free. https://serper.dev
- `HERE_MAPS_API_KEY` → company phone + cuisine type. **250,000 free/month.** https://platform.here.com
- `PAPPERS_API_KEY` → website/email/phone for FR companies. 100/day free.

**Person enrichment waterfall** (cascaded in this order):
- `DROPCONTACT_API_KEY` → email + phone, French + GDPR. 50 free credits.
- `HUNTER_API_KEY` → email finder/verifier. 50 free credits/month.
- `DATAGMA_API_KEY` → French specialist (95% accuracy). 50 credits + 160 API matches free.
- `BETTERCONTACT_API_KEY` → 20-provider waterfall, PAY-PER-VALID. 50 free credits.

**Other**:
- `HUBSPOT_ACCESS_TOKEN` → push leads to HubSpot CRM.
- `ANTHROPIC_API_KEY` → only for `--generate-emails` (cold email Haiku, ~$0.0015/lead).
- `BRAVE_SEARCH_API_KEY` → legacy search backend (2k/month).
- `FRANCETRAVAIL_CLIENT_ID` + `FRANCETRAVAIL_CLIENT_SECRET` → **100% free**. Hiring signals per SIRET (n_offres + intensity high/medium/low/none). When set, each lead is auto-scored on hiring activity (+20 ICP if high) — best timing trigger for AI-training / outsourcing pitches. https://francetravail.io/data

## v0.14.0 — Tech stack + hiring signals (TIER 1)

Two free signals now feed `preliminary_score` + `cold_email` personalisation:

1. **Wappalyzer-LITE** (`tech_stack.py`) — pure-Python regex detection over the homepage HTML we already fetched. Detects ~50 tools (HubSpot, Stripe, Shopify, WordPress, Zapier/Make/n8n, Calendly, etc.) and surfaces categories: `has-crm`, `has-automation`, `has-payment`, etc. `pitch_hint_from_tech()` returns a 1-line opener Claude injects into cold emails ("Vous utilisez déjà Zapier : niveau 3 IA…").
2. **France Travail** (`francetravail_client.py`) — official OAuth2 partner API, free. Returns active job offers per SIRET → classified into `high` (≥10), `medium` (4-9), `low` (1-3), `none`. Boîtes qui recrutent = budget formation IA disponible.

New ICP rule types (use in `--icp-description` / preset rules):
- `tech_signals_any: ["has-automation","has-crm"]`
- `tech_signals_all: ["has-crm","has-analytics"]`
- `tech_maturity_above: 40`
- `hiring_intensity_min: "medium"`

New XLSX columns: `ft_hiring_intensity`, `ft_n_offres`, `ft_top_titles`, `tech_stack`, `tech_maturity`, `primary_cms`.

## v0.15.0 — Intent signals + multi-touch + RFP-mode

### Appels d'offres BOAMP (signal d'intention max) 🔥

Source prospects directly from active public RFPs matching your offer.
LE signal d'intention d'achat le plus fort qui existe.

```bash
python run_campaign.py \
  --rfp-cpv-preset formation_ia \
  --rfp-regions "Occitanie,Nouvelle-Aquitaine" \
  --rfp-days 90 --rfp-montant-min 10000 \
  --volume 20 \
  --generate-emails --multi-touch \
  --sender-company "Comeos" \
  --output comeos-rfp-formation-ia
```

Each RFP is auto-attached to the lead (`rfp_active` field). The cold-email
prompt then **references the RFP explicitly** ("vu votre récent appel à
projet sur X"). Pure gold for conversion.

### Multi-touch sequence (J0 + J+4 + J+10) 🚀

```bash
python run_campaign.py ... --generate-emails --multi-touch
```

Generates 3 emails per lead in one go: cold + follow-up + break-up.
Multiplies response rate by 3-4×. Cost: ~$0.005/lead instead of $0.0015.
XLSX gets 6 extra columns (subject + body per touch).

### Careers page scraping 🎯

Auto-detects `/careers`, `/jobs`, `/recrutement`, etc. on the prospect's
site. Extracts open job titles. Strong TILT if AI / Data / Automation
roles detected (+30 ICP boost) — captures cadre roles that France Travail
doesn't carry.

### Lifecycle stage (Sirene age) ⏱️

Auto-classifies into: `very-early` (<6mo), `scaling` (6-24mo, +15 ICP),
`mature` (2-5yr), `legacy` (>5yr). Filter via `lifecycle_stage_in` in ICP.

### New ICP rule types

- `careers_tilt_any: ["ai","data","automation"]`
- `careers_tilt_all: ["data","automation"]`
- `careers_min_jobs: 3`
- `lifecycle_stage_in: ["scaling"]`
- `company_age_max_months: 24`
- `has_active_rfp: true`

Run `python setup_wizard.py --check` to confirm. If a key is missing, tell the user the URL to get one (in `setup_wizard.py`).

## Outcome tracking & ICP self-tuning

After sending cold emails, mark what actually happened so the agent learns:

```bash
# When a lead replies / books a meeting / converts:
python lead_store.py mark 408400547 replied
python lead_store.py mark 408400547 meeting_booked --note "RDV pris 22/5"
python lead_store.py mark 408400547 closed_won

# When it bounces / unsubscribes:
python lead_store.py mark 408400547 bounced
python lead_store.py mark 408400547 unsubscribed

# Review outcome distribution + top-converting attributes:
python lead_store.py outcomes

# ICP self-tuning report (needs ≥20 outcomes to suggest changes):
python icp_tuner.py bear-brothers-chr

# Get the tuned ICP dict ready to use:
python icp_tuner.py bear-brothers-chr --apply
```

The tuner computes UPLIFT per rule: how much more likely a lead matching
this rule converts vs the average. Rules with uplift ≥1.5 are recommended
for BOOST; ≤0.7 for CUT. Below 20 marked outcomes, the tuner refuses to
suggest changes ("not enough signal").

## Quotas — track free-tier consumption

Every API call increments a local counter (`data/quotas.db`). Use these
commands to know what's left before launching a campaign:

```bash
python quotas.py                  # human-readable table (use this in Multica)
python quotas.py json             # machine-readable for automation
python quotas.py set-cap 50       # cap to 50 leads/day max
python quotas.py get-cap          # show current cap
python quotas.py reset dropcontact   # reset one service (admin)

# Quick inline from the main CLI:
python run_campaign.py --quotas
python run_campaign.py --naf 56.10A --departement 31 --volume 10 --daily-cap 50 ...
```

Every campaign run prints :
- Quota status BEFORE (max leads possible + warning if asking too much)
- Quota status AFTER (what's left + critical alerts)

The "MAX REMAINING LEADS POSSIBLE" line names the **bottleneck service** —
that's the one that will hit zero first. Upgrade it (or wait for its reset
period) to lift the ceiling.

## What v0.9.0 added (paid-layer ready)

- **Serper.dev integration** (`serper_search.py`) — drop-in search backend, **2,500 free queries one-shot**. Becomes the primary backend because Google CSE is closed to new customers since 2025 and Brave's free quota is small.
- **Dropcontact integration** (`dropcontact_client.py`) — French B2B contact enrichment via REST API. (firstname, lastname, company) → verified email + phone + sometimes LinkedIn. **50 free credits at signup, no card.** Wired as PRIORITY -1 in `finalize_lead`: when Dropcontact returns an email/phone/LinkedIn, it wins over all other sources (highest trust).
- **Search backend fallback chain**: Serper → Google CSE → Brave → DDG. First non-empty result wins.

## What v0.8.0 added

- **Smart name cleanup** (`name_utils.py`) — `DIDIER JACQUES EMMANUEL YVON VILLEMEY` → `Didier Villemey`. Handles all-caps Sirene strings, hyphens, particles (de la / van / ibn), apostrophes.
- **OpenStreetMap finder** (`osm_finder.py`) — free, no anti-bot, no rate-limit hell. Pulls phone / website / email / contact:instagram / contact:facebook tags from OSM nodes. Great for restos/bars/cafés.
- **Domain guess** (`domain_guess.py`) — tries `{slug}.fr`, `{slug}.com`, `restaurant-{slug}.fr`, `{slug}-{city}.fr` etc. with HEAD + sanity-GET. Recovers websites for ~30 % of SMBs whose Sirene/Pappers record is empty.
- **SMB-friendly thresholds** (`triangulation.py`) — for 0-49 emp companies, `company_phone` and `company_email` count as reachability channels (these literally route to the gérant). Leads with verified-name + postal address are kept even with no remote channel ("postal/visit only").
- **Cold email drafter** (`cold_email.py`) — scrapes the company homepage for real context, then calls Claude Haiku to write a 80-120 word FR cold email with a specific opener. Subject + body + angle land in the XLSX export.
- **Pages Jaunes deprecated** — site is now behind Cloudflare's full JS challenge. Kept as a best-effort last-resort; replaced everywhere by OSM + domain_guess.

## Output format

Both files are produced side by side:
- `.csv` with `;` delimiter + UTF-8 BOM → opens cleanly in Excel FR
- `.xlsx` with frozen header → real Excel table

Report back to the user: how many kept, how many dropped (with reasons), and the file paths.
