#!/usr/bin/env bash
# Upload v0.15.0 files to Multica skill
set -e
SKILL_ID=82758c2e-70dd-4b5b-82a3-93f6bab3a2da
cd "$(dirname "$0")/.."
for f in SKILL.md pagesjaunes_client.py pipeline.py run_campaign.py sheets_export.py triangulation.py cold_email.py domain_guess.py name_utils.py osm_finder.py research_urls.py social_finder.py mentions_legales.py google_cse.py brave_search.py serper_search.py dropcontact_client.py http_safe.py email_finder.py lead_store.py sirene_client.py mobile_finder.py email_pattern_engine.py phone_utils.py google_places.py icp.py here_maps.py hunter_client.py datagma_client.py bettercontact_client.py pappers_client.py quotas.py icp_tuner.py bodacc_client.py web_enrichment.py icp_from_nl.py tech_stack.py francetravail_client.py appels_offres.py careers_page.py reverse_sourcing.py lead_reasoner.py; do
  echo ">>> Upserting $f"
  multica skill files upsert "$SKILL_ID" --path "$f" --content "$(cat "$f")" 2>&1 | tail -3
  echo
done
echo "Done."
