#!/usr/bin/env bash
cd "$(dirname "$0")/.."
set -a; source .env; set +a
for name_city in "MOOD|Toulouse" "FLAVEURS|Toulouse" "L Escargot|Toulouse" "VINTO|Toulouse" "LE HARICOT TOULOUSE|Toulouse" "STEVO'S DINING EMPORIUM|Toulouse" "GALA|Toulouse" "EL NASSER|Toulouse" "CAPOHALLES 21|Labège"; do
    name="${name_city%|*}"
    city="${name_city#*|}"
    echo "=== $name ($city) ==="
    .venv/bin/python google_places.py "$name" --city "$city" 2>&1 | head -15
    echo
done
