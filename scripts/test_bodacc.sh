#!/usr/bin/env bash
cd "$(dirname "$0")/.."
for siren in 470501545 484975982 451814644 422399238 384721619 502095094; do
    echo "=== SIREN $siren ==="
    .venv/bin/python bodacc_client.py "$siren" --since-days 1095 2>&1 | tail -10
    echo
done
