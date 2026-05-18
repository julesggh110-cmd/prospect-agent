"""
Sheets export — push leads to Google Sheets, with Excel-FR-friendly CSV fallback.

CSV format choice matters:
- Excel FR opens comma-CSV as a single column (it expects `;`).
- Plain `,` CSVs with internal quotes can break older parsers.
- A UTF-8 BOM + `;` delimiter opens cleanly in Excel FR, Excel EN, Numbers,
  LibreOffice, and Google Sheets ("Import" → auto-detects).

Two auth modes for Sheets:
1. Service account JSON file (path in env var GOOGLE_SERVICE_ACCOUNT_JSON)
   → simplest for automation. Create a Cloud project, enable the Sheets API,
     download a service account JSON, share the target sheet (edit) with the
     service-account email.
2. (TBD) OAuth user flow — not implemented yet.

If Sheets isn't configured, we write **two** files side by side:
- `leads-<timestamp>.csv` (semicolon, UTF-8 BOM) — opens in Excel
- `leads-<timestamp>.xlsx` (real Excel binary) — opens in everything
"""
from __future__ import annotations

import csv
import os
import time
from pathlib import Path
from typing import Optional

try:
    import gspread  # type: ignore
    from google.oauth2.service_account import Credentials  # type: ignore
except ImportError:  # pragma: no cover
    gspread = None  # type: ignore
    Credentials = None  # type: ignore

# Imported lazily to keep this module light if Lead model isn't loaded
HEADERS = [
    "company_name", "company_siren", "company_naf_label",
    "company_city", "company_address", "company_size", "company_website",
    "company_phone", "company_phone_conf",
    "company_linkedin", "company_linkedin_conf",
    "company_instagram", "company_instagram_conf",
    "company_facebook",
    "person_name", "person_name_conf",
    "person_role", "person_role_conf",
    "person_email", "person_email_conf", "person_email_note",
    "person_phone", "person_phone_conf",
    "person_linkedin", "person_linkedin_conf",
    "person_instagram", "person_instagram_conf",
    "overall_score",
    "dropped", "drop_reason",
]


def _row_for(lead) -> list:  # `lead` is a triangulation.Lead but we keep this lazy
    def scored(field):
        return [field.value or "", field.confidence if field.value else ""]

    return [
        lead.company_name,
        lead.company_siren or "",
        lead.company_naf_label or "",
        lead.company_city or "",
        lead.company_address or "",
        lead.company_size or "",
        lead.company_website or "",
        *scored(lead.company_phone),
        *scored(lead.company_linkedin),
        *scored(lead.company_instagram),
        lead.company_facebook or "",
        *scored(lead.person_name),
        *scored(lead.person_role),
        lead.person_email.value or "",
        lead.person_email.confidence if lead.person_email.value else "",
        lead.person_email.note or "",
        *scored(lead.person_phone),
        *scored(lead.person_linkedin),
        *scored(lead.person_instagram),
        lead.overall_score,
        "yes" if lead.dropped else "",
        lead.drop_reason or "",
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def export_leads(leads: list, *, prefer_sheet: bool = True) -> str:
    """Push leads to Google Sheets if configured, else CSV. Returns URL or path."""
    if prefer_sheet:
        sheet_id = os.environ.get("DEFAULT_SHEET_ID", "").strip()
        sa_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        if sheet_id and sa_path and gspread is not None:
            try:
                return _push_to_sheet(leads, sheet_id, sa_path)
            except Exception as exc:
                print(f"[sheets] Push failed ({exc}); falling back to CSV.")
    return _write_csv(leads)


def _push_to_sheet(leads: list, sheet_id: str, sa_path: str) -> str:
    if gspread is None or Credentials is None:
        raise RuntimeError("gspread / google-auth not installed")
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(sa_path, scopes=scopes)
    client = gspread.authorize(creds)
    sh = client.open_by_key(sheet_id)
    ws_name = time.strftime("leads-%Y-%m-%d-%H%M%S")
    ws = sh.add_worksheet(title=ws_name, rows=max(2, len(leads) + 5), cols=len(HEADERS))
    rows = [HEADERS] + [_row_for(l) for l in leads]
    ws.update(values=rows, range_name="A1")
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#gid={ws.id}"


def _write_csv(leads: list, out_dir: Optional[Path] = None) -> str:
    """Write two artifacts: Excel-FR-friendly CSV (semicolon + BOM) AND XLSX.

    Returns the CSV path (the XLSX sits alongside with the same stem).
    """
    out_dir = out_dir or Path(__file__).resolve().parent / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = time.strftime("leads-%Y-%m-%d-%H%M%S")
    csv_path = out_dir / f"{stem}.csv"
    xlsx_path = out_dir / f"{stem}.xlsx"

    rows = [HEADERS] + [_row_for(l) for l in leads]

    # CSV with `;` + UTF-8 BOM (Excel FR opens it as a real table, no Import wizard)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writerows(rows)

    # XLSX (binary, no separator ambiguity ever, opens cleanly everywhere)
    try:
        from openpyxl import Workbook  # type: ignore
        wb = Workbook()
        ws = wb.active
        ws.title = "leads"
        for row in rows:
            ws.append([("" if v is None else v) for v in row])
        # Freeze the header row
        ws.freeze_panes = "A2"
        wb.save(xlsx_path)
    except ImportError:
        # openpyxl not installed → CSV only, still works
        pass

    return str(csv_path)
