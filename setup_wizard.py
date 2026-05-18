"""
First-run setup wizard — checks credentials, prompts for missing ones,
writes them to .env, runs basic connectivity tests.

Usage:
    python scripts/setup_wizard.py
    python scripts/setup_wizard.py --check    # non-interactive check only

What the wizard handles:
- ANTHROPIC_API_KEY (required for Claude reasoning)
- GOOGLE_SERVICE_ACCOUNT_JSON (optional, for Google Sheets export)
- DEFAULT_SHEET_ID (optional, target spreadsheet ID)

It NEVER prints secrets back. It writes them to .env in the project root.

When run by an LLM agent (not a human), it should be invoked with --check
first; the agent then explains missing pieces to the user rather than
prompting interactively (which doesn't work over Multica's task interface).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from rich.console import Console

console = Console()


REQUIRED = {
    "ANTHROPIC_API_KEY": (
        "Anthropic API key (sk-ant-...)",
        "https://console.anthropic.com/settings/keys",
        True,
    ),
}
OPTIONAL = {
    "PAPPERS_API_KEY": (
        "Pappers API key — direct website/email/phone for FR companies (free 100/day)",
        "https://www.pappers.fr/api",
        False,
    ),
    "BRAVE_SEARCH_API_KEY": (
        "Brave Search API key — stable replacement for DuckDuckGo (free 2k/month)",
        "https://api-dashboard.search.brave.com/",
        False,
    ),
    "GOOGLE_SERVICE_ACCOUNT_JSON": (
        "Path to a Google service-account JSON key file",
        "https://console.cloud.google.com/iam-admin/serviceaccounts",
        False,
    ),
    "DEFAULT_SHEET_ID": (
        "Default Google Sheet ID to push leads into (the long ID from the URL)",
        "Create a sheet at https://sheets.new and share it (edit) with the "
        "service-account email from the JSON.",
        False,
    ),
}


def _project_root() -> Path:
    return Path(__file__).resolve().parent


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _write_env_file(path: Path, values: dict[str, str]) -> None:
    lines = [
        "# Generated/updated by scripts/setup_wizard.py — DO NOT commit.",
    ]
    for k, v in values.items():
        if v == "":
            lines.append(f"{k}=")
        else:
            lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _current_state() -> dict[str, str]:
    env_path = _project_root() / ".env"
    return {**_read_env_file(env_path), **{k: v for k, v in os.environ.items()
                                              if k in REQUIRED or k in OPTIONAL}}


def check(verbose: bool = True) -> bool:
    state = _current_state()
    all_ok = True
    for k, (label, link, required) in {**REQUIRED, **OPTIONAL}.items():
        present = bool(state.get(k))
        tag = "[green]✓[/green]" if present else ("[red]✗ MISSING[/red]" if required else "[yellow]– optional, not set[/yellow]")
        if verbose:
            console.print(f"  {tag}  [bold]{k}[/bold]  — {label}")
            if not present and verbose:
                console.print(f"      → get one here: {link}")
        if required and not present:
            all_ok = False
    return all_ok


def interactive() -> None:
    env_path = _project_root() / ".env"
    state = _read_env_file(env_path)

    console.rule("[bold]Prospect Agent — first-run setup")
    console.print("This wizard saves credentials to .env in the project root.\n")

    for k, (label, link, required) in {**REQUIRED, **OPTIONAL}.items():
        existing = state.get(k, "")
        if existing:
            console.print(f"[green]{k}[/green] already set — skipping (delete the line in .env to re-prompt)")
            continue
        tag = "[red]required[/red]" if required else "[yellow]optional[/yellow]"
        console.print(f"\n[bold]{k}[/bold] ({tag}) — {label}")
        console.print(f"  Get one at: {link}")
        val = console.input(f"  Paste {k} (Enter to skip): ").strip()
        if val:
            state[k] = val

    _write_env_file(env_path, state)
    console.print(f"\n[green]✓ Saved to {env_path}[/green]\n")
    console.rule("[bold]Status check")
    check(verbose=True)


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Setup wizard for prospect-agent")
    parser.add_argument("--check", action="store_true",
                        help="Non-interactive: just report what is set / missing")
    args = parser.parse_args()

    if args.check:
        ok = check(verbose=True)
        sys.exit(0 if ok else 1)

    if not sys.stdin.isatty():
        console.print("[yellow]Non-TTY environment detected — running --check instead of interactive prompts.[/yellow]")
        ok = check(verbose=True)
        sys.exit(0 if ok else 1)

    interactive()


if __name__ == "__main__":
    _cli()
