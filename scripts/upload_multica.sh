#!/usr/bin/env bash
# Upload v0.8.0 files to Multica skill
set -e
SKILL_ID=82758c2e-70dd-4b5b-82a3-93f6bab3a2da
cd "$(dirname "$0")/.."
for f in SKILL.md pagesjaunes_client.py pipeline.py run_campaign.py sheets_export.py triangulation.py cold_email.py domain_guess.py name_utils.py osm_finder.py research_urls.py social_finder.py; do
  echo ">>> Upserting $f"
  multica skill files upsert "$SKILL_ID" --path "$f" --content "$(cat "$f")" 2>&1 | tail -3
  echo
done
echo "Done."
