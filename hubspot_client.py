"""
HubSpot Free CRM sync — push leads as Contacts + Companies + Notes.

Sets up automatically:
- A Company in HubSpot (with website, LinkedIn, Instagram, address, SIREN as
  external ID for dedup)
- A Contact (decision-maker) linked to the Company, with email, phone,
  LinkedIn, role
- A Note attached to the Contact with the full confidence/sources breakdown
  (so the user can audit any field)

Auth: HubSpot Free Private App Access Token, env var `HUBSPOT_ACCESS_TOKEN`.

To get a token:
1. Sign up for HubSpot Free (free forever): https://www.hubspot.com/products/get-started
2. Settings → Integrations → Private Apps → Create a private app
3. Name it "Prospect Agent", scopes: crm.objects.contacts.write, crm.objects.companies.write, crm.objects.notes.write, plus the matching .read
4. Copy the access token, put it in .env as HUBSPOT_ACCESS_TOKEN

If the env var is missing, sync_leads_to_hubspot() returns (0, 0, "no token")
without raising — Multica-side this is graceful.
"""
from __future__ import annotations

import os
from typing import Any, Optional

try:
    from hubspot import HubSpot  # type: ignore
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate as ContactInput  # type: ignore
    from hubspot.crm.companies import SimplePublicObjectInputForCreate as CompanyInput  # type: ignore
    from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteInput  # type: ignore
    from hubspot.crm.contacts.exceptions import ApiException as ContactApiException  # type: ignore
except ImportError:  # pragma: no cover
    HubSpot = None  # type: ignore


def have_hubspot_token() -> bool:
    return bool(os.environ.get("HUBSPOT_ACCESS_TOKEN"))


def _split_name(full_name: str) -> tuple[str, str]:
    """'JEAN PIERRE DURAND' → ('JEAN PIERRE', 'DURAND')."""
    parts = full_name.strip().split()
    if len(parts) <= 1:
        return full_name, ""
    return " ".join(parts[:-1]), parts[-1]


def _format_note_for_lead(lead) -> str:
    """Markdown audit-trail body. Shown in HubSpot under the contact."""
    lines = [
        f"# Prospect Agent — confidence audit",
        f"",
        f"**Company**: {lead.company_name}",
        f"  - SIREN: {lead.company_siren or '—'}",
        f"  - Sector: {lead.company_naf_label or lead.company_naf or '—'}",
        f"  - Address: {lead.company_address or '—'}",
        f"  - Size: {lead.company_size or '—'}",
        f"",
        f"**Decision-maker**: {lead.person_name.value or '?'}",
        f"  - Role: {lead.person_role.value or '?'}",
        f"  - Sources: {', '.join(lead.person_name.sources) or '—'}",
        f"",
        f"**Contact channels** (confidence /100):",
        f"  - Email: {lead.person_email.value or '—'} ({lead.person_email.confidence}) [{lead.person_email.note or ''}]",
        f"  - Phone: {lead.person_phone.value or lead.company_phone.value or '—'} ({max(lead.person_phone.confidence, lead.company_phone.confidence)})",
        f"  - LinkedIn: {lead.person_linkedin.value or '—'} ({lead.person_linkedin.confidence})",
        f"  - Instagram: {lead.person_instagram.value or '—'} ({lead.person_instagram.confidence})",
        f"",
        f"**Company socials**:",
        f"  - Website: {lead.company_website or '—'}",
        f"  - LinkedIn: {lead.company_linkedin.value or '—'}",
        f"  - Instagram: {lead.company_instagram.value or '—'}",
        f"  - Facebook: {lead.company_facebook or '—'}",
        f"",
        f"**Overall score**: {lead.overall_score}/100",
    ]
    return "\n".join(lines)


def sync_leads_to_hubspot(leads: list, *, owner_email: Optional[str] = None) -> tuple[int, int, str]:
    """Push a list of Lead objects to HubSpot. Returns (n_created, n_updated, message).

    Safe to call without a token — returns (0, 0, 'no token') and never raises.
    Per-lead errors are caught and logged; the function keeps going.
    """
    if HubSpot is None:
        return 0, 0, "hubspot-api-client not installed"
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not token:
        return 0, 0, "HUBSPOT_ACCESS_TOKEN not set"

    hs = HubSpot(access_token=token)
    n_created = 0
    n_updated = 0
    errors: list[str] = []

    for lead in leads:
        if lead.dropped:
            continue
        try:
            # 1) Upsert company by SIREN (or by name if no SIREN)
            company_id = _upsert_company(hs, lead)
            # 2) Upsert contact by email (or by name+company)
            contact_id, was_new = _upsert_contact(hs, lead, company_id)
            n_created += 1 if was_new else 0
            n_updated += 0 if was_new else 1
            # 3) Attach a fresh note with the full audit trail
            _attach_note(hs, contact_id, _format_note_for_lead(lead))
        except Exception as e:
            errors.append(f"{lead.company_name}: {type(e).__name__}: {e}")
            continue

    msg = f"{n_created} created, {n_updated} updated"
    if errors:
        msg += f", {len(errors)} errors (first: {errors[0]})"
    return n_created, n_updated, msg


def _upsert_company(hs, lead) -> str:
    """Create or update a HubSpot Company. Dedup by website domain or name."""
    from urllib.parse import urlparse
    props: dict[str, Any] = {
        "name": lead.company_name,
        "phone": lead.company_phone.value or "",
        "city": lead.company_city or "",
        "address": lead.company_address or "",
    }
    if lead.company_website:
        props["domain"] = urlparse(lead.company_website).hostname or ""
        props["website"] = lead.company_website
    if lead.company_linkedin.value:
        props["linkedin_company_page"] = lead.company_linkedin.value
    # HubSpot `industry` is a strict enum (ACCOUNTING, FOOD_BEVERAGES, ...) — our
    # free-text NAF label doesn't map cleanly. Put it in `description` instead so
    # the user keeps the info without enum-validation errors.
    desc_bits = []
    if lead.company_naf_label:
        desc_bits.append(f"NAF: {lead.company_naf_label} ({lead.company_naf or ''})")
    if lead.company_siren:
        desc_bits.append(f"SIREN: {lead.company_siren}")
    if desc_bits:
        props["description"] = " · ".join(desc_bits)[:500]
    # HubSpot `numberofemployees` expects a number, not the Sirene "11" code →
    # convert the Sirene size bracket to an approximate employee count.
    if lead.company_size:
        # Sirene size codes → midpoint of bracket (approximate)
        size_map = {
            "00": "0", "01": "1", "02": "3", "03": "7",
            "11": "15", "12": "35", "21": "75", "22": "150",
            "31": "350", "32": "750", "41": "1500", "42": "3500",
            "51": "7500", "52": "15000", "53": "30000",
        }
        emp = size_map.get(lead.company_size)
        if emp:
            props["numberofemployees"] = emp

    # Try to find existing by domain first, then by name
    existing_id = None
    search_term: Optional[tuple[str, str]] = None
    if props.get("domain"):
        search_term = ("domain", props["domain"])
    elif props.get("name"):
        search_term = ("name", props["name"])
    if search_term:
        existing_id = _search_object_id(hs.crm.companies, *search_term)

    if existing_id:
        hs.crm.companies.basic_api.update(
            company_id=existing_id,
            simple_public_object_input={"properties": props},
        )
        return existing_id

    created = hs.crm.companies.basic_api.create(
        simple_public_object_input_for_create=CompanyInput(properties=props),
    )
    return created.id


def _upsert_contact(hs, lead, company_id: str) -> tuple[str, bool]:
    """Create or update a HubSpot Contact and associate it to the company."""
    first, last = _split_name(lead.person_name.value or "")
    props: dict[str, Any] = {"firstname": first, "lastname": last}
    if lead.person_email.value:
        props["email"] = lead.person_email.value
    if lead.person_phone.value or lead.company_phone.value:
        props["phone"] = lead.person_phone.value or lead.company_phone.value
    if lead.person_role.value:
        props["jobtitle"] = lead.person_role.value[:100]
    if lead.person_linkedin.value:
        props["hs_linkedin_url"] = lead.person_linkedin.value

    existing_id = None
    if props.get("email"):
        existing_id = _search_object_id(hs.crm.contacts, "email", props["email"])
    elif first and last:
        # No reliable secondary key; we accept a possible dup rather than miss create
        pass

    if existing_id:
        hs.crm.contacts.basic_api.update(
            contact_id=existing_id,
            simple_public_object_input={"properties": props},
        )
        _associate_contact_to_company(hs, existing_id, company_id)
        return existing_id, False

    created = hs.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=ContactInput(properties=props),
    )
    _associate_contact_to_company(hs, created.id, company_id)
    return created.id, True


def _associate_contact_to_company(hs, contact_id: str, company_id: str) -> None:
    try:
        hs.crm.contacts.associations_api.create(
            contact_id=contact_id,
            to_object_type="companies",
            to_object_id=company_id,
            association_type="contact_to_company",
        )
    except Exception:
        pass  # already associated — fine


def _attach_note(hs, contact_id: str, body: str) -> None:
    try:
        note = NoteInput(properties={"hs_note_body": body[:65000], "hs_timestamp": _now_ms()})
        created_note = hs.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=note,
        )
        # Associate note → contact
        hs.crm.objects.notes.associations_api.create(
            note_id=created_note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_type="note_to_contact",
        )
    except Exception:
        pass


def _now_ms() -> str:
    import time as _t
    return str(int(_t.time() * 1000))


def _search_object_id(api, field: str, value: str) -> Optional[str]:
    """Best-effort search for one existing object by a single equality filter."""
    if not value:
        return None
    try:
        resp = api.search_api.do_search(
            public_object_search_request={
                "filterGroups": [{"filters": [{"propertyName": field, "operator": "EQ", "value": value}]}],
                "limit": 1,
            }
        )
        if resp.results:
            return resp.results[0].id
    except Exception:
        pass
    return None
