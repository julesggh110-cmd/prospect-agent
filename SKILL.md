---
name: prospect-agent
description: Generate verified B2B prospect lists from a natural-language request. Returns decision-maker + email + phone + LinkedIn + Instagram per company, with confidence scores and provenance. Never invents data. Use when the user asks to find leads, prospects, decision-makers, "trouve-moi des contacts", "génère une liste de prospects B2B".
version: 0.7.0
triggers:
  - prospection
  - prospects
  - leads
  - décideurs
  - trouve-moi
  - liste d'entreprises
  - dirigeants
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
  --output cavistes-paris-1
```

Key flags:
- `--icp-preset {cavistes-paris,palaces-paris}`: score each lead 0-100 vs an ideal profile; sorted by score in the output.
- `--only-new`: skip companies already in `data/leads.db` (dedup across runs).
- `--push-to-hubspot`: create Contacts + Companies + audit Notes in HubSpot (needs `HUBSPOT_ACCESS_TOKEN`).

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

Required: `ANTHROPIC_API_KEY` (covered by your Claude Code auth).
Strongly recommended (free tiers cover all testing):
- `PAPPERS_API_KEY` → direct website/email/phone for FR companies (100/day free)
- `BRAVE_SEARCH_API_KEY` → stable search backend (2k/month free)

Run `python setup_wizard.py --check` to confirm. If a key is missing, tell the user the URL to get one (in `setup_wizard.py`).

## Output format

Both files are produced side by side:
- `.csv` with `;` delimiter + UTF-8 BOM → opens cleanly in Excel FR
- `.xlsx` with frozen header → real Excel table

Report back to the user: how many kept, how many dropped (with reasons), and the file paths.
