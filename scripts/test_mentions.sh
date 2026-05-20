#!/usr/bin/env bash
cd "$(dirname "$0")/.."
for site in 'https://chez-navarre.fr' 'https://bibent.fr' 'https://www.fontaine-de-mars.com' 'https://lamijean.fr' 'https://www.michel-sarran.com' 'https://bouillonlesite.com'; do
  echo "=== $site ==="
  .venv/bin/python mentions_legales.py "$site" 2>&1 | head -10
  echo
done
