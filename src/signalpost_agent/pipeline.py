"""Per-company research pipeline and envelope assembly."""
from __future__ import annotations

import time
from datetime import date
from typing import Any

from . import external, official, web
from .http import Fetcher, utc_now
from .model import Case

SECTION_TITLES = {
    "1": "Legal identity and public brand",
    "2": "Latest annual accounts and available history",
    "3": "Leadership and registered workplaces",
    "4": "Verified official website and company-owned profiles",
    "5": "Hiring and dated public activity",
    "6": "Claim-level evidence and availability state",
    "7": "Refresh metadata and material changes",
}
STATE_ORDER = ["available", "ambiguous", "blocked", "failed", "not_available", "not_applicable"]


def official_phase(fetcher: Fetcher, case: Case, snapshot_row: dict | None) -> dict | None:
    """Brønnøysund modules. Returns the live entity (or None)."""
    entity = official.fetch_entity(fetcher, case)
    if entity is None:
        if snapshot_row:
            _snapshot_fallback(case, snapshot_row)
        return None
    official.fetch_accounts(fetcher, case)
    official.fetch_roles(fetcher, case)
    official.fetch_workplaces(fetcher, case)
    official.fetch_updates(fetcher, case)
    return entity


def _snapshot_fallback(case: Case, row: dict) -> None:
    """Live registry unreachable: use the frozen snapshot row for identity only, clearly labelled."""
    case.claims[:] = [c for c in case.claims if c["field"] != "legal_identity"]
    eid = case.ev("builderr://frozen-registry-snapshot", "frozen_registry_snapshot", None, None,
                  f"{row.get('organisation_number')} {row.get('name')}", "snapshot_row")
    if row.get("name"):
        case.claim("legal_name", row["name"], "available", [eid], confidence=0.95, section="1",
                   note="From the frozen registry snapshot because the live registry call failed")
    if row.get("legal_form"):
        case.claim("legal_form", {"code": row["legal_form"]}, "available", [eid], confidence=0.95, section="1")
    if row.get("industry_code"):
        case.claim("industry", {"code": row["industry_code"], "description": row.get("industry_label"), "rank": 1}, "available", [eid],
                   key="1", confidence=0.95, section="1")
    if row.get("website"):
        case.claim("registry_declared_website", row["website"], "available", [eid], confidence=0.95, section="4")
    if row.get("employees"):  # a snapshot 0 cannot be told apart from "not registered", so it is never asserted
        case.claim("registered_employees", row["employees"], "available", [eid], confidence=0.95, section="3")
    else:
        case.claim("registered_employees", None, "not_available", [], section="3",
                   note="Not registered, or the frozen snapshot has no confirmed count (never read as zero)")


def legal_name_of(case: Case) -> str | None:
    for claim in case.claims:
        if claim["field"] == "legal_name" and claim["availability"] == "available":
            return claim["value"]
    return None


def web_phase(fetcher: Fetcher, case: Case, entity: dict | None, wikidata: dict | None, *, use_nav: bool, max_guesses: int) -> dict[str, Any]:
    """Website resolution + company-owned and public-portal facts. Never raises."""
    outcome: dict[str, Any] = {"site": False, "jobs_checked": [], "activity_checked": []}
    name = legal_name_of(case)
    if not name:
        for f, sec in (("official_website", "4"), ("job_posting", "5"), ("public_activity", "5")):
            case.claim(f, None, "failed", [], note="Legal name unavailable; web research skipped", section=sec)
        return outcome
    external.emit_wikidata(case, wikidata)
    status = next((c["value"] for c in case.claims if c["field"] == "registry_status"), "active")
    if entity is not None:
        entity = dict(entity)
        entity["_leader_names"] = [c["value"]["name"] for c in case.claims
                                   if c["field"] == "leadership" and c["availability"] == "available"
                                   and c["value"].get("role_holder_type") == "person"]
    try:
        found = web.resolve_website(fetcher, case, entity, name, wikidata, max_guesses=max_guesses)
    except Exception as exc:  # noqa: BLE001 - a crawl bug must not drop the envelope
        case.error("website", f"{type(exc).__name__}: {exc}")
        found = None
        case.claim("official_website", None, "failed", [], note="Unexpected error while resolving the website", section="4")
    have_profiles: set[str] = set()
    if found:
        ctx = found["ctx"]
        outcome["site"] = True
        for step in (lambda: web.extract_brand(case, ctx, name), lambda: web.extract_social(case, ctx)):
            try:
                step()
            except Exception as exc:  # noqa: BLE001
                case.error("website_extract", f"{type(exc).__name__}: {exc}")
        try:
            jobs = web.extract_jobs(fetcher, case, ctx, name)
            if jobs["checked"]:
                outcome["jobs_checked"].append(f"company careers page {jobs['page']}")
        except Exception as exc:  # noqa: BLE001
            case.error("careers", f"{type(exc).__name__}: {exc}")
        try:
            act = web.extract_activity(fetcher, case, ctx)
            if act["checked"]:
                outcome["activity_checked"].append("company RSS/Atom feed or sitemap")
        except Exception as exc:  # noqa: BLE001
            case.error("activity", f"{type(exc).__name__}: {exc}")
        have_profiles = {c["value"]["platform"] for c in case.claims
                         if c["field"] == "company_profile" and c["availability"] == "available"}
    elif entity is not None:
        case.claim("public_brand", None, "not_available", [], note="No verified website to read a brand name from", section="1")
    if wikidata:
        external.wikidata_profiles(case, wikidata, have_profiles)
    employees = next((c["value"] for c in case.claims if c["field"] == "registered_employees" and c["availability"] == "available"), None)
    if use_nav and status == "active" and employees:
        try:
            nav = external.nav_jobs(fetcher, case, name)
            if nav["checked"]:
                outcome["jobs_checked"].append("NAV Arbeidsplassen public ads (exact employer org number)")
            else:
                outcome["jobs_unavailable"] = f"NAV Arbeidsplassen could not be queried ({nav.get('state')}, rate limited or unavailable)"
        except Exception as exc:  # noqa: BLE001
            case.error("nav_jobs", f"{type(exc).__name__}: {exc}")
    # Summary claims: absence is stated as not_available, never as zero.
    postings = [c for c in case.claims if c["field"] == "job_posting" and c["availability"] == "available"]
    if postings:
        case.claim("hiring_status", {"postings_found": len(postings), "sources": outcome["jobs_checked"]}, "available",
                   sorted({e for c in postings for e in c["evidence_ids"]})[:10], confidence=0.9, section="5",
                   note="Number of verified postings found in the checked sources; not a complete count of the company's vacancies")
    else:
        checked = outcome["jobs_checked"]
        extra = ("; " + outcome["jobs_unavailable"]) if outcome.get("jobs_unavailable") else ""
        case.claim("hiring_status", None, "not_available", [],
                   note=(("Checked, no postings found: " + "; ".join(checked) + extra) if checked else "No job source could be checked (no verified company website)" + extra),
                   section="5")
    if not any(c["field"] == "public_activity" and c["availability"] == "available" and c["claim_key"].startswith("public_activity:news:")
               for c in case.claims):
        checked = outcome["activity_checked"]
        case.claim("public_activity", None, "not_available", [], key="news",
                   note=("Checked, no dated company news found in: " + "; ".join(checked)) if checked else "No verified website to read news from",
                   section="5")
    if not have_profiles and not any(c["field"] == "company_profile" and c["availability"] in ("ambiguous",) for c in case.claims):
        case.claim("company_profile", None, "not_available", [], key="any",
                   note="No company-owned social profile links found on a verified site or Wikidata item", section="4")
    return outcome


def _money(value: Any, currency: str | None) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{currency or 'NOK'} {float(value):,.0f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


YOY_METRICS = (("revenue", "Operating revenue"), ("operating_result", "Operating result"), ("annual_result", "Annual result"),
               ("total_assets", "Total assets"), ("equity", "Equity"))


def _months_between(start: str | None, end: str | None) -> float | None:
    try:
        a, b = date.fromisoformat(str(start)[:10]), date.fromisoformat(str(end)[:10])
    except ValueError:
        return None
    return (b - a).days / 30.4375


def year_over_year(case: Case, preferred_type: str | None = None) -> dict[str, Any]:
    """Figures-only comparison of the two latest filings. Never narrates: it only states numbers that are comparable."""
    accounts = [c for c in case.claims if c["field"] == "annual_accounts" and c["availability"] == "available"
                and isinstance(c.get("value"), dict) and c["value"].get("period_to")]
    kinds = sorted({c["value"].get("accounts_type") for c in accounts}, key=lambda k: (k != preferred_type, str(k)))
    if not kinds:
        return {"comparable": False, "reason": "no annual accounts available", "rows": [], "claims": []}
    kind = kinds[0]
    series = sorted((c for c in accounts if c["value"].get("accounts_type") == kind), key=lambda c: c["value"]["period_to"], reverse=True)
    if len(series) < 2:
        return {"comparable": False, "reason": "only one filing year available", "accounts_type": kind, "rows": [], "claims": []}
    cur, prev = series[0], series[1]
    cv, pv = cur["value"], prev["value"]
    base = {"accounts_type": kind, "current_period": f"{cv.get('period_from')}..{cv['period_to']}",
            "previous_period": f"{pv.get('period_from')}..{pv['period_to']}", "claims": [cur["claim_key"], prev["claim_key"]]}
    gap = _months_between(pv["period_to"], cv["period_to"])
    lengths = [_months_between(v.get("period_from"), v["period_to"]) for v in (cv, pv)]
    if cv.get("currency") != pv.get("currency"):
        return {**base, "comparable": False, "reason": "different currencies", "rows": []}
    if gap is None or not 11 <= gap <= 13 or any(n is None or not 11 <= n <= 13 for n in lengths):
        return {**base, "comparable": False, "reason": "the periods are not two consecutive 12-month years", "rows": []}
    rows = []
    for key, label in YOY_METRICS:
        a, b = cv.get(key), pv.get(key)
        if a is None or b is None:
            rows.append({"metric": label, "current": a, "previous": b, "change": None, "change_pct": None, "direction": None})
            continue
        change = a - b
        pct = round(100 * change / b, 1) if b > 0 and a >= 0 else None
        rows.append({"metric": label, "current": a, "previous": b, "change": change, "change_pct": pct,
                     "direction": "up" if change > 0 else "down" if change < 0 else "flat"})
    return {**base, "comparable": True, "reason": None, "currency": cv.get("currency"), "rows": rows}


def _preferred_type(case: Case) -> str | None:
    """Accounts type of the newest filing, company-level first (matches the summary)."""
    accounts = [c["value"] for c in case.claims if c["field"] == "annual_accounts" and c["availability"] == "available"
                and isinstance(c.get("value"), dict) and c["value"].get("period_to")]
    accounts.sort(key=lambda v: (v["period_to"], v.get("accounts_type") != "KONSERN"), reverse=True)
    return accounts[0].get("accounts_type") if accounts else None


def _sources(case: Case, claims: list[dict]) -> list[dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for claim in claims:
        for eid in claim.get("evidence_ids", []):
            ev = case.evidence.get(eid)
            if not ev:
                continue
            url = ev["source_url"]
            label = url if url.startswith("http") else "frozen registry snapshot supplied by Builderr"
            out.setdefault(label, {"source": label, "retrieved_at": ev.get("retrieved_at", "")})
    return list(out.values())


def summarize(case: Case) -> dict[str, Any]:
    """Deterministic, claim-backed profile summary: every sentence cites claim keys."""
    by_field: dict[str, list[dict]] = {}
    for c in case.claims:
        if c["availability"] == "available":
            by_field.setdefault(c["field"], []).append(c)
    sentences: list[dict[str, Any]] = []
    unknowns: list[str] = []

    def one(field: str) -> dict | None:
        return (by_field.get(field) or [None])[0]

    name, form, status = one("legal_name"), one("legal_form"), one("registry_status")
    if name:
        text = f"{name['value']} is registered in Enhetsregisteret under organisation number {case.org}"
        keys = [name["claim_key"]]
        if form:
            text += f" as {form['value'].get('description') or form['value'].get('code')}"
            keys.append(form["claim_key"])
        if status and status["value"] != "active":
            text += f"; registry status: {status['value'].replace('_', ' ')}"
            keys.append(status["claim_key"])
        sentences.append({"text": text + ".", "claims": keys})
    industries = sorted(by_field.get("industry", []), key=lambda c: c["value"].get("rank", 9))
    address, employees = one("registered_address"), one("registered_employees")
    if industries or address or employees:
        parts, keys = [], []
        if industries:
            parts.append(f"industry {industries[0]['value']['code']} {industries[0]['value'].get('description') or ''}".strip())
            keys.append(industries[0]["claim_key"])
        if address:
            parts.append(f"registered address {address['value']}")
            keys.append(address["claim_key"])
        if employees:
            parts.append(f"{employees['value']} employees as registered in Enhetsregisteret")
            keys.append(employees["claim_key"])
        sentences.append({"text": "It reports " + "; ".join(parts) + ".", "claims": keys})
    if not employees:
        unknowns.append("the employee count is not registered in Enhetsregisteret (unregistered, not zero)")
    # Same period: company-level accounts before consolidated group accounts.
    accounts = sorted(by_field.get("annual_accounts", []),
                      key=lambda c: (c["value"].get("period_to") or "", c["value"].get("accounts_type") != "KONSERN"), reverse=True)
    fresh, filing = one("accounts_freshness"), one("latest_filing_year")
    yoy = year_over_year(case, accounts[0]["value"].get("accounts_type") if accounts else None)
    if accounts:
        v = accounts[0]["value"]
        fy = v["period_to"][:4]
        status = (fresh or {}).get("value", {}).get("status", "unverified")
        declared = (fresh or {}).get("value", {}).get("registry_latest_filing_year")
        kind = " consolidated (group)" if v.get("accounts_type") == "KONSERN" else ""
        lead = {"current": f"Latest filed{kind} accounts (FY{fy}, matching the registry's latest filing year {declared})",
                "lagging": f"Most recent{kind} accounts in the normalised register are FY{fy}; the registry reports FY{declared} accounts as filed "
                           "but their figures are not available here, so these are not the latest",
                }.get(status, f"Most recent{kind} accounts in the normalised register (FY{fy}; the registry gave no latest filing year to check against)")
        if status == "lagging":
            unknowns.append(f"FY{declared} accounts are filed according to the registry, but the figures are not in the accounts register")
        bits = []
        if v.get("revenue") is not None:
            bits.append(f"operating revenue {_money(v['revenue'], v.get('currency'))}")
        if v.get("annual_result") is not None:
            bits.append(f"annual result {_money(v['annual_result'], v.get('currency'))}")
        if v.get("total_assets") is not None:
            bits.append(f"total assets {_money(v['total_assets'], v.get('currency'))}")
        if v.get("equity") is not None:
            bits.append(f"equity {_money(v['equity'], v.get('currency'))}")
        if bits:
            keys = [accounts[0]["claim_key"]] + [c["claim_key"] for c in (fresh, filing) if c]
            sentences.append({"text": f"{lead} (period {v['period_from']} to {v['period_to']}): " + ", ".join(bits) + ".", "claims": keys})
        rev = next((r for r in yoy["rows"] if r["metric"] == "Operating revenue" and r["change"] is not None), None) if yoy["comparable"] else None
        if rev:
            pct = f" ({rev['change_pct']:+.1f}%)" if rev["change_pct"] is not None else ""
            move = "unchanged" if rev["direction"] == "flat" else f"{rev['direction']} {_money(abs(rev['change']), yoy.get('currency'))}{pct}"
            sentences.append({"text": f"Operating revenue was {_money(rev['current'], yoy.get('currency'))} in the period ending {yoy['current_period'][-10:]} "
                                      f"against {_money(rev['previous'], yoy.get('currency'))} in the period ending {yoy['previous_period'][-10:]}: {move}.",
                              "claims": yoy["claims"]})
    else:
        unknowns.append("no annual accounts were returned by the accounts register")
        if fresh and fresh["value"]["status"] == "no_figures_in_register":
            unknowns.append(f"the registry reports FY{fresh['value']['registry_latest_filing_year']} accounts as filed, but no figures are in the accounts register")
    roles = by_field.get("leadership", [])
    ceo = [c for c in roles if c["value"]["role_code"] == "DAGL"]
    chair = [c for c in roles if c["value"]["role_code"] == "LEDE"]
    if ceo or chair:
        bits, keys = [], []
        if ceo:
            bits.append(f"{ceo[0]['value']['name']} is general manager (daglig leder)")
            keys.append(ceo[0]["claim_key"])
        if chair:
            bits.append(f"{chair[0]['value']['name']} chairs the board")
            keys.append(chair[0]["claim_key"])
        sentences.append({"text": "Registered leadership: " + " and ".join(bits) + ".", "claims": keys})
    else:
        unknowns.append("no general manager or board chair is registered")
    places = by_field.get("registered_workplace", [])
    if places:
        sentences.append({"text": f"{len(places)} registered workplace(s) (underenheter) are listed in the register.",
                          "claims": [p["claim_key"] for p in places[:5]]})
    site = one("official_website")
    if site:
        proof = {"org_number_on_site": "organisation number shown on the site", "registry_declared_website": "declared in the registry",
                 "registry_contact_details_match": "name and registry contact details match"}.get(
                     (site["value"] or {}).get("identity_proof"), "identity checked against the registry record")
        sentences.append({"text": f"Official website {site['value']['url']} ({proof}).", "claims": [site["claim_key"]]})
        brand = one("public_brand")
        if brand and brand["value"]["differs_from_legal_name"]:
            sentences.append({"text": f"The company presents itself as \"{brand['value']['name']}\" on its own site.", "claims": [brand["claim_key"]]})
    else:
        unknowns.append("no official website could be verified for this exact entity")
    profiles = by_field.get("company_profile", [])
    if profiles:
        sentences.append({"text": "Company-owned profiles linked from its site or Wikidata: "
                                  + ", ".join(sorted(p["value"]["platform"] for p in profiles)) + ".",
                          "claims": [p["claim_key"] for p in profiles]})
    jobs = by_field.get("job_posting", [])
    if jobs:
        sentences.append({"text": f"{len(jobs)} job posting(s) found in the checked sources (e.g. \"{jobs[0]['value'].get('title')}\"); "
                                  "this is not a complete count of the company's vacancies.", "claims": [j["claim_key"] for j in jobs[:5]]})
    else:
        unknowns.append("no job postings were found in the checked sources (this does not prove the company is not hiring)")
    activity = sorted(by_field.get("public_activity", []), key=lambda c: c.get("effective_date") or "", reverse=True)
    news = [a for a in activity if a["value"].get("kind") == "company_news"]
    if news:
        n = news[0]["value"]
        sentences.append({"text": f"Latest dated company news: \"{n['title']}\" ({n['date']}).", "claims": [news[0]["claim_key"]]})
    elif activity:
        v = activity[0]["value"]
        sentences.append({"text": f"Latest dated register activity: {v['title']} on {v['date'][:10]} (no company news found).",
                          "claims": [activity[0]["claim_key"]]})
    by_key = {c["claim_key"]: c for c in case.claims}
    for s in sentences:  # a receipt for every sentence: where each cited claim was read, and when
        s["sources"] = _sources(case, [by_key[k] for k in s["claims"] if k in by_key])
    return {"text": " ".join(s["text"] for s in sentences), "sentences": sentences, "unknowns": unknowns}


def build_envelope(case: Case, run: dict[str, Any], outcome: dict[str, Any] | None, entity_found: bool, org_known: bool) -> dict[str, Any]:
    claims = case.claims
    sections = []
    for sid in ("1", "2", "3", "4", "5"):
        sec = [c for c in claims if c.get("section") == sid]
        states = sorted({c["availability"] for c in sec}, key=STATE_ORDER.index)
        sections.append({"id": sid, "title": SECTION_TITLES[sid], "state": states[0] if states else "not_available",
                         "claim_count": sum(1 for c in sec if c["availability"] == "available")})
    sections.append({"id": "6", "title": SECTION_TITLES["6"], "state": "available" if case.evidence else "not_available",
                     "claim_count": len(case.evidence)})
    if entity_found:
        overall = "available"
    elif not org_known:
        overall = "not_available"
    else:
        overall = "failed"
    return {
        "schema_version": "signalpost-agent/1",
        "organisation_number": case.org,
        "run": {**run, "terminal_status": "completed"},
        "state": overall,
        "sections": sections,
        "summary": summarize(case),
        "year_over_year": year_over_year(case, _preferred_type(case)),
        "claims": sorted(claims, key=lambda c: (c.get("section") or "9", c["claim_key"])),
        "evidence": sorted(case.evidence.values(), key=lambda e: e["id"]),
        "changes": [], "history": [], "refresh": {},
        "errors": case.errors,
        "operations": {"requests": case.ops.requests, "bytes": case.ops.bytes, "runtime_ms": case.ops.runtime_ms(),
                       "third_party_cost_usd": 0.0},
    }
