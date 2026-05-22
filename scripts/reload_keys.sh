#!/usr/bin/env bash
# reload_keys.sh — à lancer après avoir édité .env pour ajouter des clés API.
# Restart le daemon Multica avec les nouvelles clés chargées + vérifie ce qui marche.
#
# Usage :
#   bash scripts/reload_keys.sh
set -e

cd "$(dirname "$0")/.."

echo "=== 1/3 — Stop daemon Multica ==="
multica daemon stop 2>&1 | tail -2

echo
echo "=== 2/3 — Restart daemon avec .env chargé ==="
sleep 1
set -a; source .env; set +a
multica daemon start 2>&1 | tail -3

echo
echo "=== 3/3 — Vérification quelles clés sont actives ==="
python3 - <<'PY'
import os, sys
sys.path.insert(0, ".")
from pappers_client import have_pappers_key
from dropcontact_client import have_dropcontact_key
from hunter_client import have_hunter_key
from datagma_client import have_datagma_key
from bettercontact_client import have_bettercontact_key
from here_maps import have_here_key
from francetravail_client import have_francetravail_keys

icon = lambda b: "✅" if b else "❌"
checks = [
    ("PAPPERS_API_KEY",       have_pappers_key()),
    ("HERE_MAPS_API_KEY",     have_here_key()),
    ("DROPCONTACT_API_KEY",   have_dropcontact_key()),
    ("HUNTER_API_KEY",        have_hunter_key()),
    ("DATAGMA_API_KEY",       have_datagma_key()),
    ("BETTERCONTACT_API_KEY", have_bettercontact_key()),
    ("FRANCETRAVAIL keys",    have_francetravail_keys()),
    ("SERPER_API_KEY",        bool(os.environ.get("SERPER_API_KEY"))),
    ("ANTHROPIC_API_KEY",     bool(os.environ.get("ANTHROPIC_API_KEY"))),
]
print()
for name, ok in checks:
    print(f"  {icon(ok)}  {name}")
n_ok = sum(1 for _, b in checks if b)
print(f"\n{n_ok}/{len(checks)} clés actives.")
print()
missing = [n for n, b in checks if not b]
if missing:
    print("Toujours manquantes :")
    for m in missing:
        print(f"  - {m}")
    print()
    print("Signups :")
    urls = {
        "HERE_MAPS_API_KEY":     "https://platform.here.com (250 000/mois free)",
        "HUNTER_API_KEY":        "https://hunter.io/users/sign_up (50/mois free)",
        "DATAGMA_API_KEY":       "https://app.datagma.com (50 credits free)",
        "BETTERCONTACT_API_KEY": "https://app.bettercontact.rocks/sign-up (50 pay-per-valid)",
        "ANTHROPIC_API_KEY":     "https://console.anthropic.com/settings/keys (5$ credit signup)",
    }
    for m in missing:
        if m in urls:
            print(f"  {m:25s} → {urls[m]}")
else:
    print("🎉 Toutes les clés sont actives. Tu peux lancer une campagne pleine.")
PY
