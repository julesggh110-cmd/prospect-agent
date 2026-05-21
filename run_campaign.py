"""
run_campaign.py — ONE function that runs the full prospection campaign end to end.

Why: when Claude (in Multica or Claude Code) drives the pipeline tool-by-tool, every
intermediate state is shipped back through the model. With Sonnet that's ~$0.40/lead.
With this script, Claude makes ONE Bash call instead of 30+, saving 90%+ of tokens.

Usage from a shell:
    python run_campaign.py \\
        --query "cabinet dentaire" --code-postal 69001 --volume 10 \\
        --persona "associé" --output prospects-dentistes-lyon

Usage from Claude (the typical Multica flow):
    1. Pick query, geo, persona from the user request
    2. Call this script ONCE via Bash
    3. Read the printed summary + the produced .xlsx
    4. Report back to the user

The script:
- Searches Sirene
- Runs enrich_company_partial in parallel (Pappers + Brave + cache)
- Picks the legal director as decision-maker (Phase 1 default). For Phase 2 with
  Claude-in-the-loop persona disambiguation, use --interactive or call the lower-level
  pipeline functions yourself.
- Finalizes each lead (email, LinkedIn, Insta)
- Exports both .csv (Excel-FR) and .xlsx via sheets_export.export_leads
- Prints a 1-screen summary
"""
from __future__ import annotations

import argparse
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

# Make sure relative imports work whether we are run as a script or a module
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _summary(leads, kept_path: str, elapsed: float) -> None:
    kept = [l for l in leads if not l.dropped]
    dropped = [l for l in leads if l.dropped]
    print()
    print(f"=== {len(leads)} leads enriched in {elapsed:.1f}s "
          f"(~{elapsed/max(1,len(leads)):.1f}s/lead) ===")
    print(f"  Kept:    {len(kept)}")
    print(f"  Dropped: {len(dropped)}")

    if dropped:
        from collections import Counter
        reasons = Counter(l.drop_reason for l in dropped)
        for reason, n in reasons.most_common(3):
            print(f"    [{n}x] {reason}")

    print()
    print(f"=== Sample of top {min(3, len(kept))} kept leads ===")
    for l in sorted(kept, key=lambda x: -x.overall_score)[:3]:
        print(f"  {l.company_name} (score {l.overall_score})")
        print(f"    {l.person_name.value} · {l.person_role.value or '?'}")
        print(f"    email:    {l.person_email.value or '—'} (conf {l.person_email.confidence})")
        print(f"    linkedin: {l.person_linkedin.value or '—'} (conf {l.person_linkedin.confidence})")
        print(f"    phone:    {l.person_phone.value or l.company_phone.value or '—'}")
    print()
    print(f"=== Output ===")
    print(f"  CSV:  {kept_path}")
    xlsx = kept_path.replace('.csv', '.xlsx')
    if Path(xlsx).exists():
        print(f"  XLSX: {xlsx}")


def run(
    *,
    query: str | None = None,
    naf: str | None = None,
    code_postal: str | None = None,
    departement: str | None = None,
    region: str | None = None,
    tranche_effectif: str | None = None,
    volume: int = 10,
    persona_role_hint: str | None = None,
    output_stem: str | None = None,
    max_workers: int = 8,
    icp: dict | None = None,
    only_new: bool = False,
    push_to_hubspot: bool = False,
    campaign_id: str | None = None,
    llm_decider: bool = False,
    retry_dropped: bool = False,
    generate_emails: bool = False,
    sender_offer: str = "spiritueux premium français pour cartes bars et restaurants",
    sender_company: str = "Bear Brothers",
    paid_threshold: int = 40,
    max_candidates: int | None = None,
    raw_mode: bool = False,
) -> str:
    """End-to-end campaign. Returns the path of the produced CSV.

    New in v0.5.0:
    - `icp`: dict from icp.py (e.g., PRESET_CAVISTES_PREMIUM_PARIS). Annotates
      each lead with an `icp_score` 0-100. Use `icp.PRESET_*` for ready-made.
    - `only_new`: skip companies already in lead_store (dedup across runs).
    - `push_to_hubspot`: also sync kept leads to HubSpot (needs HUBSPOT_ACCESS_TOKEN).
    - `campaign_id`: tag the lead_store rows so you can list "leads from campaign X".
    """
    import time as _time
    from sirene_client import SireneClient
    from pipeline import enrich_companies_parallel, finalize_lead
    from sheets_export import export_leads
    from lead_store import already_seen_sirens, upsert_leads

    t0 = time.time()
    campaign_id = campaign_id or _time.strftime("campaign-%Y%m%d-%H%M%S")

    # 0. Sanity check: outbound SMTP. Many cloud VPS block port 25 by default,
    # which silently zeroes-out email verification. We warn LOUDLY so the user
    # knows why emails come back as "not verified" pattern guesses instead of
    # blaming the pipeline.
    from email_finder import smtp_outbound_available
    if not smtp_outbound_available():
        print("[WARN] Outbound SMTP (port 25) is BLOCKED on this host.")
        print("       Emails will fall back to pattern-guess at low confidence.")
        print("       If you need verified emails, run on a host with port 25 open,")
        print("       or rely on DROPCONTACT_API_KEY for verified results.")

    # 0-bis. QUOTAS — print the remaining capacity + check daily cap.
    # We want the operator (or Multica) to see at a glance:
    #   - how many leads they can still process today
    #   - if any free tier is at risk of running out mid-run
    from quotas import summary as _quota_summary, get_daily_cap, daily_leads_used
    qs = _quota_summary()
    bottleneck = qs.get("bottleneck_leads_remaining")
    bottleneck_svc = qs.get("bottleneck_service_label") or qs.get("bottleneck_service")
    if bottleneck is not None:
        print(f"[Quotas] {bottleneck} leads remaining (bottleneck: {bottleneck_svc})")
        if bottleneck < volume:
            print(f"[Quotas] WARN: you asked for {volume} leads but only ~{bottleneck} "
                  f"can be enriched before {bottleneck_svc} runs out.")
    # Daily cap enforcement
    cap = get_daily_cap()
    if cap > 0:
        used_today = daily_leads_used()
        if used_today + volume > cap:
            allowed = max(0, cap - used_today)
            print(f"[Quotas] DAILY CAP HIT — already used {used_today}/{cap} today, "
                  f"asked for {volume}. Limiting this run to {allowed} leads.")
            if allowed == 0:
                print("[Quotas] Refusing to run. Reset via `python quotas.py set-cap 0` "
                      "or wait until tomorrow.")
                sys.exit(2)
            volume = allowed

    # 1. Source via Sirene — PERFECT MODE: iterate pages, filter early,
    # enrich the survivors, stop when `volume` QUALIFIED leads collected.
    # Old behavior (--raw-mode) just fetches `volume` candidates and enriches
    # all of them, regardless of quality.
    from pipeline import is_junk_company_name, preliminary_score

    if raw_mode:
        # Legacy: fetch volume candidates, no filtering
        with SireneClient() as c:
            resp = c.search_many(
                target=volume, query=query, naf=naf,
                code_postal=code_postal, departement=departement, region=region,
                tranche_effectif=tranche_effectif,
            )
        companies = resp.results[:volume]
        if not companies:
            print("No companies matched the query.")
            sys.exit(1)
        print(f"[Sirene] {len(companies)} companies (raw mode — no quality gate)")
        if only_new:
            seen = already_seen_sirens([c.siren for c in companies])
            companies = [c for c in companies if c.siren not in seen]
            print(f"[Dedup] {len(seen)} already prospected, {len(companies)} new")
        partials = enrich_companies_parallel(companies, max_workers=max_workers)
        with_site = sum(1 for p in partials if p.get('website'))
        print(f"[Enrich] {with_site}/{len(partials)} with website")
    else:
        # PERFECT MODE — iterate Sirene pages, gate by quality, stop at target
        cap = max_candidates if max_candidates is not None else max(50, volume * 5)
        print(f"[Perfect] target={volume} qualified leads · max candidates={cap}")

        # Quality gate operating on partial enrichment data (cheap-source only).
        # A "perfect" lead must pass ALL these:
        #   - Not junk name
        #   - Not permanently_closed (GMB)
        #   - Has at least one nominatif dirigeant
        #   - preliminary_score ≥ paid_threshold (or 40 by default)
        # We DO NOT gate on foreign-subsidiary yet because that signal comes
        # from company LinkedIn which is fetched in partial — but we keep
        # those leads with low ICP score; the user sees them flagged.
        def _is_qualified(p: dict) -> tuple[bool, str]:
            if not p:
                return False, "no partial"
            if is_junk_company_name(p.get("company_name", "")):
                return False, "junk-name"
            if p.get("permanently_closed"):
                return False, "GMB permanently_closed"
            # BODACC hard-drop: company in redressement / liquidation / radiation
            if p.get("bodacc_verdict") == "HARD_DROP":
                return False, f"BODACC: {p.get('bodacc_reason', 'in trouble')}"
            dirs = p.get("legal_dirigeants") or []
            real_dirs = [d for d in dirs if d.get("first") and d.get("last")]
            if not real_dirs:
                return False, "no nominatif dirigeant"
            score = preliminary_score(p)
            if score < paid_threshold:
                return False, f"preliminary_score {score} < {paid_threshold}"
            # Reject leads whose company LinkedIn flags foreign subsidiary
            flags = p.get("quality_flags") or []
            if any(f.startswith("foreign-subsidiary:") for f in flags):
                return False, "foreign-subsidiary"
            return True, "ok"

        partials: list = []
        n_seen = 0
        n_junk_pre = 0
        n_failed_gate = 0
        with SireneClient() as c:
            for page_resp in c.iter_pages(
                query=query, naf=naf,
                code_postal=code_postal, departement=departement, region=region,
                tranche_effectif=tranche_effectif,
            ):
                if n_seen >= cap:
                    print(f"[Perfect] hit max_candidates ({cap}) — stopping")
                    break
                page_companies = page_resp.results
                if not page_companies:
                    break
                # Pre-filter: junk names (no API cost)
                pre_filtered = []
                for sc in page_companies:
                    n_seen += 1
                    name = getattr(sc, "name", None) or (
                        sc.get("nom_complet") if isinstance(sc, dict) else "?"
                    )
                    if is_junk_company_name(name):
                        n_junk_pre += 1
                        continue
                    pre_filtered.append(sc)
                # Dedup vs lead_store
                if only_new and pre_filtered:
                    sirens = [getattr(c0, "siren", None) or (
                        c0.get("siren") if isinstance(c0, dict) else None
                    ) for c0 in pre_filtered]
                    seen = already_seen_sirens([s for s in sirens if s])
                    pre_filtered = [c0 for c0, s in zip(pre_filtered, sirens)
                                     if s not in seen]
                if not pre_filtered:
                    continue
                # Enrich the survivors in parallel
                batch_partials = enrich_companies_parallel(
                    pre_filtered, max_workers=max_workers,
                )
                # Apply post-enrichment quality gate
                for bp in batch_partials:
                    ok, reason = _is_qualified(bp)
                    if not ok:
                        n_failed_gate += 1
                        continue
                    partials.append(bp)
                    if len(partials) >= volume:
                        break
                print(f"[Perfect] page {page_resp.page}: "
                      f"{len(partials)}/{volume} qualified "
                      f"(seen {n_seen}, junk-pre {n_junk_pre}, "
                      f"failed gate {n_failed_gate})")
                if len(partials) >= volume:
                    break
                if n_seen >= cap:
                    break
        if not partials:
            print("[Perfect] No qualified candidates found. Try widening filters.")
            sys.exit(1)
        with_site = sum(1 for p in partials if p.get('website'))
        print(f"[Perfect FINAL] {len(partials)}/{volume} qualified leads kept "
              f"({with_site} with website) · "
              f"{n_seen} candidates scanned · "
              f"{n_junk_pre} junk-name dropped early · "
              f"{n_failed_gate} failed quality gate")

    # 3. Finalize each lead — Phase-1 default: take the first legal director.
    leads = []
    # Source SIRENs from the partials (works for both raw and perfect mode
    # since partials always have the 'siren' key from Sirene).
    sirens = [p.get("siren") for p in partials if p.get("siren")]
    if only_new:
        # We already filtered out seen ones above — all current ones are "new" by construction
        new_sirens = set(sirens)
    else:
        new_sirens = set(sirens) - already_seen_sirens(sirens)

    # Build the (partial, person_first, person_last, role, sources) tuples
    # FIRST (no I/O), then dispatch them all to a thread pool. This is the
    # single biggest speedup: finalize_lead does many slow network calls per
    # lead (LinkedIn search, Insta search, Dropcontact poll, mobile finder,
    # SMTP) — running them in parallel collapses the wall-clock time from
    # N × (slowest_per_lead) to roughly (slowest_per_lead).
    #
    # TWO-PASS strategy: compute a preliminary_score from the FREE-tier data
    # already in `partial`. Leads scoring below paid_threshold (default 40)
    # get the cheap path (no Dropcontact/Hunter/Datagma/BC) — saves credits
    # on low-quality leads we'd probably drop anyway.
    from pipeline import preliminary_score
    finalize_args = []
    prelim_scores: list[tuple[int, str]] = []
    n_paid = 0
    n_cheap = 0
    for p in partials:
        dirs = p.get("legal_dirigeants") or []
        if not dirs:
            continue

        chosen_first, chosen_last, chosen_role = None, None, None
        chosen_sources = ["sirene"]

        if llm_decider:
            from decision_maker_llm import pick as llm_pick
            decision = llm_pick(
                company_name=p["company_name"],
                sector_hint=p.get("naf") or "",
                persona_hint=persona_role_hint or "operational decision-maker",
                legal_dirigeants=dirs,
                team_page_text=p.get("team_page_text"),
            )
            if decision and decision.get("person_first") and decision.get("person_last"):
                chosen_first = decision["person_first"]
                chosen_last = decision["person_last"]
                chosen_role = decision.get("person_role") or persona_role_hint or ""
                chosen_sources = decision.get("person_sources") or ["sirene", "llm-decider"]

        if not chosen_first or not chosen_last:
            d = dirs[0]
            chosen_first = d.get("first") or ""
            chosen_last = d.get("last") or ""
            if not chosen_first or not chosen_last:
                parts = (d.get("name") or "").split()
                if len(parts) < 2:
                    continue
                chosen_first = chosen_first or parts[0]
                chosen_last = chosen_last or parts[-1]
            # CRITICAL: the REAL Sirene role wins. persona_role_hint is the
            # role the user WANTED to find (e.g. "DRH") but when we fall back
            # to the legal dirigeant, we must label them with their ACTUAL
            # role (Président, Gérant, etc.) — NOT the requested persona.
            # The persona hint is now used only as a last-resort label when
            # Sirene gave no role at all.
            real_role = d.get("role") or ""
            chosen_role = real_role or persona_role_hint or ""
            # If the user asked for a specific persona that we couldn't
            # confirm, annotate sources so finalize_lead's downstream code
            # knows this isn't a verified DRH/etc.
            if persona_role_hint and real_role and real_role.lower() != persona_role_hint.lower():
                chosen_sources = list(chosen_sources) + [
                    f"role-fallback:{real_role.lower()}-not-{persona_role_hint.lower()}"
                ]

        prelim = preliminary_score(p)
        prelim_scores.append((prelim, p["company_name"]))
        use_paid = prelim >= paid_threshold
        if use_paid:
            n_paid += 1
        else:
            n_cheap += 1
        finalize_args.append((p, chosen_first, chosen_last, chosen_role, chosen_sources, use_paid))

    if paid_threshold > 0 and (n_paid + n_cheap) > 0:
        print(f"[Two-pass] {n_paid}/{n_paid + n_cheap} leads above paid threshold "
              f"(>={paid_threshold}) → will hit paid waterfall; "
              f"{n_cheap} get cheap pass only.")

    # LAUNCH Dropcontact batch IN BACKGROUND, overlap with parallel finalize.
    # Previously the DC batch was BLOCKING (~30s polling), then finalize ran
    # (~30s). Now they run in parallel → wall-time drops by ~25s.
    # Race-safe because finalize_lead's DC step reads the diskcache; if the
    # background task hasn't primed it yet, finalize falls through to the
    # other waterfall sources (Hunter/Datagma/BetterContact). When DC IS
    # ready in time, every worker gets a clean cache hit.
    from concurrent.futures import ThreadPoolExecutor as _BgTPE
    _dc_executor = _BgTPE(max_workers=1)
    _dc_future = None
    try:
        from dropcontact_client import enrich_batch, have_dropcontact_key
        paid_args = [a for a in finalize_args if a[5]]   # a[5] = use_paid
        if have_dropcontact_key() and paid_args:
            print(f"[Dropcontact] batch-enriching {len(paid_args)} paid-tier leads "
                  f"in BACKGROUND (skipped {len(finalize_args) - len(paid_args)} cheap-only)…")

            def _dc_background_task():
                batch_rows = [
                    {"first": f, "last": l, "company": p.get("company_name", ""),
                     "website": p.get("website")}
                    for (p, f, l, r, srcs, _up) in paid_args
                ]
                batch_result = enrich_batch(batch_rows)
                try:
                    from pipeline import _CACHE, _CACHE_TTL
                    if _CACHE is not None:
                        for (p, f, l, r, srcs, _up) in paid_args:
                            key = f"dropcontact:{f}|{l}|{p.get('company_name', '')}"
                            lookup_key = (f.lower(), l.lower(),
                                           p.get("company_name", "").lower())
                            result = batch_result.get(lookup_key)
                            if result is not None:
                                _CACHE.set(key, result, expire=_CACHE_TTL)
                except Exception:
                    pass
                return batch_result

            _dc_future = _dc_executor.submit(_dc_background_task)
    except Exception as e:
        print(f"[Dropcontact] background batch failed ({e})")

    # Parallel finalize. We use the same worker count as the partial phase
    # since the rate-limited APIs (Brave, Serper, OSM, Dropcontact) all have
    # thread-safe Throttle() locks that serialize correctly under concurrency.
    from concurrent.futures import ThreadPoolExecutor as _TPE

    def _one(args):
        p, f, l, r, srcs, use_paid = args
        return finalize_lead(p, person_first=f, person_last=l,
                             person_role=r, person_sources=srcs,
                             use_paid_sources=use_paid)

    with _TPE(max_workers=max_workers) as exe:
        for lead in exe.map(_one, finalize_args):
            setattr(lead, "is_new_lead", lead.company_siren in new_sirens)
            leads.append(lead)

    # Drain the background Dropcontact task (might still be running if it
    # was slower than parallel finalize). We need this to make stats
    # accurate and to clean up the executor.
    if _dc_future is not None:
        try:
            batch_result = _dc_future.result(timeout=60)
            n_with_data = sum(
                1 for v in batch_result.values()
                if v.get("email") or v.get("phone")
            )
            print(f"[Dropcontact] background batch done: {n_with_data}/{len(batch_result)} returned data")
        except Exception as e:
            print(f"[Dropcontact] background batch error: {e}")
    _dc_executor.shutdown(wait=True)

    # 3a-bis. Self-critique multi-pass: retry dropped leads with relaxed thresholds.
    if retry_dropped:
        retried = 0
        for lead in leads:
            if not lead.dropped:
                continue
            # Re-evaluate with a relaxed contact threshold (30 vs 50)
            lead.dropped = False
            lead.drop_reason = None
            lead.evaluate(min_person_conf=60, min_contact_conf=30)
            if not lead.dropped:
                retried += 1
        if retried:
            print(f"[Self-critique] Recovered {retried} leads with relaxed thresholds")

    # 3b. ICP scoring (optional)
    if icp:
        from icp import annotate_leads
        annotate_leads(leads, icp)
        print(f"[ICP] '{icp.get('name','?')}' applied. Top score: "
              f"{max((l.icp_score for l in leads), default=0)}")

    # 3c. Persist to lead_store (dedup history + ICP score saved)
    n_new, n_existing = upsert_leads(leads, campaign_id=campaign_id)
    print(f"[Store] +{n_new} new leads, {n_existing} re-seen "
          f"(campaign_id={campaign_id})")

    # 3d. Optional cold email generation (Haiku, ~$0.0015/lead, FR personalized)
    if generate_emails:
        from cold_email import generate_for_leads
        kept_for_email = [l for l in leads if not l.dropped]
        emails = generate_for_leads(
            kept_for_email,
            sender_offer=sender_offer,
            sender_company=sender_company,
        )
        # Attach the generated email body to the lead model as extra fields so
        # the XLSX export picks them up.
        for l in kept_for_email:
            key = l.company_siren or l.company_name
            ce = emails.get(key)
            if ce:
                setattr(l, "cold_email_subject", ce.subject)
                setattr(l, "cold_email_body", ce.body)
                setattr(l, "cold_email_angle", ce.angle)
        print(f"[Cold-Email] {len(emails)}/{len(kept_for_email)} drafted")

    # 3e. Optional HubSpot push (only kept leads)
    if push_to_hubspot:
        from hubspot_client import sync_leads_to_hubspot
        created, updated, msg = sync_leads_to_hubspot([l for l in leads if not l.dropped])
        print(f"[HubSpot] {msg}")

    # 4. Export both CSV (Excel-FR) and premium XLSX side by side
    if output_stem:
        out_dir = Path("output")
        out_dir.mkdir(exist_ok=True)
        csv_path = out_dir / f"{output_stem}.csv"
        xlsx_path = out_dir / f"{output_stem}.xlsx"
        import csv as _csv
        from sheets_export import HEADERS, _row_for, _write_premium_xlsx
        # Sort kept leads: best ICP first (if scored), else by overall_score
        kept = sorted(
            [l for l in leads if not l.dropped],
            key=lambda x: (getattr(x, "icp_score", 0) or 0, x.overall_score),
            reverse=True,
        )
        rows = [HEADERS] + [_row_for(l) for l in kept]
        with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
            _csv.writer(fh, delimiter=";", quoting=_csv.QUOTE_MINIMAL).writerows(rows)
        try:
            _write_premium_xlsx(rows, xlsx_path)
        except ImportError:
            pass
        kept_path = str(csv_path)
    else:
        kept_path = export_leads([l for l in leads if not l.dropped])

    _summary(leads, kept_path, time.time() - t0)

    # Final quota status — show what's left after this run
    try:
        from quotas import summary as _qs2
        s = _qs2()
        if s.get("bottleneck_leads_remaining") is not None:
            print()
            print(f"[Quotas] After this run: ~{s['bottleneck_leads_remaining']} "
                  f"leads still possible (bottleneck: "
                  f"{s.get('bottleneck_service_label')}).")
            crit = [x for x in s["services"] if x["status"] == "critical"]
            if crit:
                for c in crit:
                    print(f"[Quotas] ⚠ {c['label']} is {c['percent_used']}% used "
                          f"— upgrade or wait for {c['period']} reset.")
    except Exception:
        pass

    return kept_path


def _cli() -> None:
    p = argparse.ArgumentParser(description="Run a full prospection campaign in one call.")
    p.add_argument("--query", help="Free-text Sirene query (e.g., 'cabinet dentaire')")
    p.add_argument("--naf", help="NAF code (e.g., 86.23Z)")
    p.add_argument("--code-postal", help="Postal code (e.g., 69001)")
    p.add_argument("--departement", help="Département (e.g., 69)")
    p.add_argument("--region", help="Région code")
    p.add_argument("--tranche-effectif", dest="tranche_effectif",
                   help="Sirene size code: 00=0 emp, 01=1-2, 02=3-5, 03=6-9, "
                        "11=10-19, 12=20-49, 21=50-99, 22=100-199, 31=200-249, "
                        "32=250-499 (use 11 or 12 to target true SMBs and skip chains)")
    p.add_argument("--volume", type=int, default=10,
                   help="Number of QUALIFIED leads to deliver (default 10). "
                        "PERFECT MODE: the agent iterates Sirene pages and "
                        "applies quality gates (junk-name, ICP, foreign-sub, "
                        "operational status) until VOLUME perfect leads are "
                        "collected — may scan up to --max-candidates Sirene "
                        "entries to find them.")
    p.add_argument("--persona", dest="persona_role_hint",
                   help="Hint for the role label in the output (e.g., 'Gérant', 'DRH')")
    p.add_argument("--output", dest="output_stem",
                   help="Output filename stem (e.g., 'prospects-dentistes-lyon')")
    p.add_argument("--max-workers", type=int, default=8,
                   help="Parallel enrichment workers (default 8)")
    p.add_argument("--icp-preset",
                   choices=["cavistes-paris", "palaces-paris", "bear-brothers-chr",
                            "comeos-formation"],
                   help="Apply a preset ICP profile and add icp_score column. "
                        "bear-brothers-chr uses GMB cuisine_type to filter "
                        "vegan/halal/cantine and boost gastro/bar/brasserie. "
                        "comeos-formation targets santé/médico-social/industrie/BTP "
                        "50-249 emp en Occitanie (Comeos QSE/RH training).")
    p.add_argument("--only-new", action="store_true",
                   help="Skip companies already in lead_store (dedup across runs)")
    p.add_argument("--push-to-hubspot", action="store_true",
                   help="Sync kept leads to HubSpot CRM (needs HUBSPOT_ACCESS_TOKEN)")
    p.add_argument("--campaign-id", help="Tag this run in lead_store")
    p.add_argument("--llm-decider", action="store_true",
                   help="Use Claude (Haiku) to pick the operational decision-maker "
                        "from the team page instead of the legal director. ~$0.001/lead.")
    p.add_argument("--retry-dropped", action="store_true",
                   help="After the main pass, retry dropped leads with relaxed thresholds "
                        "(min_contact_conf=30). Self-critique multi-pass.")
    p.add_argument("--tenant", default=None,
                   help="Tenant ID for multi-tenant deployments (defaults to env "
                        "PROSPECT_AGENT_TENANT or 'default'). Leads are isolated "
                        "per tenant in the lead store.")
    p.add_argument("--daily-cap", type=int, default=None,
                   help="Set the daily lead cap before running. Equivalent to "
                        "`python quotas.py set-cap N`. Use 0 to disable.")
    p.add_argument("--paid-threshold", type=int, default=40,
                   help="Two-pass strategy: leads with preliminary_score below "
                        "this don't get the paid waterfall (Dropcontact, "
                        "Hunter, Datagma, BetterContact). 0 = always pay. "
                        "Default 40 = skip the bottom ~30%% which we'd drop "
                        "anyway. Saves ~50%% paid credits.")
    p.add_argument("--max-candidates", type=int, default=None,
                   help="PERFECT MODE: safety cap on Sirene candidates "
                        "scanned per run. Default = max(50, volume*5). "
                        "Higher = better chance of hitting --volume but "
                        "more API quota burned.")
    p.add_argument("--raw-mode", action="store_true",
                   help="DEBUG: disable PERFECT MODE quality gates. Returns "
                        "the first `--volume` Sirene candidates regardless "
                        "of quality. Use only to compare/debug.")
    p.add_argument("--quotas", action="store_true",
                   help="Print the current quota status and exit (no campaign).")
    p.add_argument("--generate-emails", action="store_true",
                   help="Generate a personalized FR cold email per kept lead via "
                        "Claude Haiku. ~$0.0015/lead. Saved into the XLSX export.")
    p.add_argument("--sender-offer", default="spiritueux premium français pour cartes bars et restaurants",
                   help="One-line description of what you're selling (FR)")
    p.add_argument("--sender-company", default="Bear Brothers",
                   help="Your company name (signed/referenced in the email)")
    args = p.parse_args()

    # Quota helpers — let the operator inspect / set caps from the same CLI
    if args.quotas:
        from quotas import summary as _qs, _format_table  # type: ignore
        print(_format_table(_qs()))
        return
    if args.daily_cap is not None:
        from quotas import set_daily_cap
        set_daily_cap(args.daily_cap)
        print(f"[Quotas] Daily cap set to {args.daily_cap} leads.")

    if not any([args.query, args.naf, args.code_postal, args.departement, args.region]):
        p.error("provide at least one filter (--query / --naf / --code-postal / ...)")

    # Apply tenant flag for the duration of this process — lead_store reads it
    # from env. This way the rest of the code doesn't have to thread it through.
    if args.tenant:
        import os as _os
        _os.environ["PROSPECT_AGENT_TENANT"] = args.tenant

    icp_profile = None
    if args.icp_preset:
        from icp import (
            PRESET_BEAR_BROTHERS_CHR,
            PRESET_CAVISTES_PREMIUM_PARIS,
            PRESET_COMEOS_FORMATION,
            PRESET_PALACES_PARIS,
        )
        icp_profile = {
            "cavistes-paris": PRESET_CAVISTES_PREMIUM_PARIS,
            "palaces-paris": PRESET_PALACES_PARIS,
            "bear-brothers-chr": PRESET_BEAR_BROTHERS_CHR,
            "comeos-formation": PRESET_COMEOS_FORMATION,
        }[args.icp_preset]

    run(
        query=args.query,
        naf=args.naf,
        code_postal=args.code_postal,
        departement=args.departement,
        region=args.region,
        tranche_effectif=args.tranche_effectif,
        volume=args.volume,
        persona_role_hint=args.persona_role_hint,
        output_stem=args.output_stem,
        max_workers=args.max_workers,
        icp=icp_profile,
        only_new=args.only_new,
        push_to_hubspot=args.push_to_hubspot,
        campaign_id=args.campaign_id,
        llm_decider=args.llm_decider,
        retry_dropped=args.retry_dropped,
        generate_emails=args.generate_emails,
        sender_offer=args.sender_offer,
        sender_company=args.sender_company,
        paid_threshold=args.paid_threshold,
        max_candidates=args.max_candidates,
        raw_mode=args.raw_mode,
    )


if __name__ == "__main__":
    _cli()
