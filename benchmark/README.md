# Benchmark suite — how good is the agent?

Two complementary harnesses, both run-once-and-read-the-report.

## A. Coverage benchmark — hit-rate per field

Answers: **"sur N entreprises Sirene, combien d'infos l'agent récupère ?"**
No ground truth needed.

```bash
python -m benchmark.coverage \
  --naf 56.10A --departement 31 --tranche-effectif 11 \
  --volume 30 --output bench-toulouse-chr.md
```

The harness queries Sirene, runs the full enrichment, then tallies for each field:
- **Has value**: % of leads where the agent returned something
- **Coverage %**: same, as a percentage
- **Conf ≥ 60**: % of leads with that value at usable confidence
- **Top sources**: which fallback provided the value (pappers / ddg / osm / domain-guess / website-scrape)

Run this when you change the agent (e.g. add a new fallback) to see how
coverage moved.

## B. Precision benchmark — correctness vs golden truth

Answers: **"quand l'agent retourne une valeur, est-ce qu'elle est juste ?"**
Needs a labelled CSV.

```bash
python -m benchmark.precision \
  --truth benchmark/golden_truth.csv \
  --output bench-precision.md
```

For each row in `golden_truth.csv`, the harness:
1. Runs the agent on (name, city, siren if known).
2. Compares per field with field-aware normalization (phones strip formatting,
   URLs strip www/scheme, names case-fold).
3. Aggregates into **precision** (of values returned, % correct) and
   **recall** (of values that exist in truth, % found).

A LOW recall + HIGH precision = "rarely wrong, misses a lot" — safe.
A HIGH recall + LOW precision = "finds a lot but often wrong" — the worst
failure mode for a prospection agent. We want precision > recall.

## golden_truth.csv

A hand-curated CSV. Columns:
- `company_name` (required)
- `city` (recommended)
- `siren` (highly recommended — without it we fall back to fuzzy Sirene
  search, which returns the wrong entity for many famous restos)
- `company_website`, `company_phone`, `company_email`,
  `person_first`, `person_last` — fill in only what you can verify from
  an INDEPENDENT source (the resto's own homepage, TripAdvisor, Yelp,
  Google business listing). Empty cells are not penalized.

Add rows for entreprises you know the truth about. The more rows, the
more meaningful the precision numbers.

## Reading the results

Both harnesses print a rich CLI table and write a clean Markdown report
(`--output bench-name.md`) that you can paste into a doc or share with
the team.

The report's per-company breakdown is the most actionable: it shows
exactly which boîtes the agent nails (100%) and which it whiffs on (0%) —
that's where you'd go improve the fallbacks.
