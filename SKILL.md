---
name: prospect-agent
description: Generate verified B2B prospect lists from a natural-language request. Returns decision-maker + email + phone + LinkedIn + Instagram per company, with confidence scores and provenance. Never invents data. Use when the user asks to find leads, prospects, decision-makers, "trouve-moi des contacts", "génère une liste de prospects B2B".
version: 0.8.0
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

Run `python setup_wizard.py --check` to confirm. If a key is missing, tell the user the URL to get one (in `setup_wizard.py`).

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
