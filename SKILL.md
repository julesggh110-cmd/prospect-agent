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
Strongly recommended (free tiers cover all testing):
- `PAPPERS_API_KEY` → direct website/email/phone for FR companies (100/day free)
- `BRAVE_SEARCH_API_KEY` → stable search backend (2k/month free)
- `HUBSPOT_ACCESS_TOKEN` → push leads straight into HubSpot CRM
- `ANTHROPIC_API_KEY` → only needed for `--generate-emails` (cold email drafting). Your Claude Code MAX subscription does NOT cover this — it's a separate API key.

Run `python setup_wizard.py --check` to confirm. If a key is missing, tell the user the URL to get one (in `setup_wizard.py`).

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
