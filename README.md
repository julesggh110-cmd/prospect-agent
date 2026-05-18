# Prospect Agent

A Multica skill that generates **verified B2B prospect lists** with decision-maker
contacts (email, phone, LinkedIn, Instagram) from a natural-language request.

## Philosophy

- **Triangulation first** — every piece of info is cross-checked against at least
  two independent sources before being kept.
- **Zero hallucination** — when in doubt, the field is left empty. Better an empty
  cell than a wrong one.
- **Confidence per field** — every value comes with a confidence score and a list
  of sources, so you can audit every lead.

## Status

Phase 1 — **France only** via INSEE Sirene + web scraping. International coverage
is planned (Phase 2).

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# First-run setup (will prompt for Anthropic API key + Google Sheets OAuth)
python setup_wizard.py

# Run a prospection request
python run.py "trouve-moi 20 dirigeants de cabinets dentaires à Lyon"
```

## Using as a Multica skill

1. In Multica UI: **Skills → Import → From GitHub URL**
2. Paste: `https://github.com/julesggh110-cmd/prospect-agent`
3. Attach the skill to an agent (Claude Code recommended)
4. Assign tasks to that agent in natural language

## Architecture

```
prospect-agent/
├── SKILL.md                # Multica skill manifest (entry point for Claude)
├──                 # Python modules
│   ├── sirene_client.py    # FR official company database
│   ├── web_enrichment.py   # Scrape company websites for contacts
│   ├── triangulation.py    # Cross-source verification + confidence
│   └── sheets_export.py    # Google Sheets push
├── prompts/                # Reusable prompt templates for Claude
├── tests/                  # Unit + integration tests
└── data/                   # Local cache + outputs (gitignored)
```

## License

TBD.
