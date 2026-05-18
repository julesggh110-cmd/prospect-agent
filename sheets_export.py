"""
Sheets export — push a list of Leads to a Google Sheet, with CSV fallback.

Two auth modes:
1. Service account JSON file (path in env var GOOGLE_SERVICE_ACCOUNT_JSON)
   → simplest for automation. The user creates a Cloud project, enables the
     Sheets API, downloads a service account JSON, shares the target sheet
     with the service account email.
2. (TBD) OAuth user flow — not implemented yet because it requires
   interactive consent that's awkward inside Claude/Multica.

If GOOGLE_SERVICE_ACCOUNT_JSON is not set OR DEFAULT_SHEET_ID is empty, we
write a CSV to ./data/leads-<timestamp>.csv and return the file path.
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
    out_dir = out_dir or Path(__file__).resolve().parent.parent / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = out_dir / time.strftime("leads-%Y-%m-%d-%H%M%S.csv")
    with fname.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADERS)
        for lead in leads:
            writer.writerow(_row_for(lead))
    return str(fname)
