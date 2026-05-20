#!/usr/bin/env bash
cd "$(dirname "$0")/.."
for p in "06 60 19 05 01" "+33 6 60 19 05 01" "01 42 59 69 31" "+33 5 61 22 49 25" "+14055947026" "08 92 70 12 39" "09 70 80 12 34" "0033612345678"; do
    echo "=== '$p' ==="
    .venv/bin/python phone_utils.py "$p" 2>&1
done
