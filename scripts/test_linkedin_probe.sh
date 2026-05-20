#!/usr/bin/env bash
cd "$(dirname "$0")/.."
while IFS='|' read -r first last; do
  echo "=== $first $last ==="
  .venv/bin/python linkedin_probe.py "$first" "$last" 2>&1
done <<EOF
Stephen|Chong
Mickael|Odier
Franck|Bitbol
Herve|Sichel-Dulong
Bertrand|Grebaut
Alain|Audiau
EOF
