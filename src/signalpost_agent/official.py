"""Brønnøysund Register Centre open APIs (NLOD 2.0): identity, accounts, roles, workplaces, updates."""
from __future__ import annotations

import json
import urllib.parse
from typing import Any

from .http import Fetcher, Response
from .model import Case

BASE = "https://data.brreg.no"
ENTITY = BASE + "/enhetsregisteret/api/enheter/{org}"
ROLES = ENTITY + "/roller"
SUBUNITS = BASE + "/enhetsregisteret/api/underenheter?overordnetEnhet={org}&size=100"
UPDATES = BASE + "/enhetsregisteret/api/oppdateringer/enheter?organisasjonsnummer={org}&size=5"
ACCOUNTS = BASE + "/regnskapsregisteret/regnskap/{org}"
NAME_SEARCH = BASE + "/enhetsregisteret/api/enheter?navn={name}&size=20"
LICENSE = "NLOD 2.0"
JSON_ACCEPT = "application/json"


def _get(value: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _json(response: Response) -> Any:
    try:
        return json.loads(response.text)
    except ValueError:
        return None


def _address(a: dict | None) -> str | None:
    if not isinstance(a, dict):
        return None
    parts = list(a.get("adresse") or []) + [" ".join(filter(None, [a.get("postnummer"), a.get("poststed")])), a.get("land")]
    text = ", ".join(p for p in parts if p)
    return text or None


def fetch(fetcher: Fetcher, case: Case, url: str) -> Response:
    return fetcher.get(url, case.ops, robots=False, accept=JSON_ACCEPT)


def status_claim_state(response: Response) -> str:
    if response.status == 200:
        return "available"
    if response.status in (404, 410):
        return "not_available"
    if response.status in (401, 403, -1):
        return "blocked"
    return "failed"


def search_names(fetcher: Fetcher, case: Case, name: str) -> list[tuple[str, str]]:
    """(organisation number, legal name) of registry entities whose name resembles `name`; [] when the search fails."""
    response = fetch(fetcher, case, NAME_SEARCH.format(name=urllib.parse.quote(name)))
    body = _json(response) if response.status == 200 else None
    rows = _get(body, "_embedded", "enheter") if isinstance(body, dict) else None
    return [(str(r.get("organisasjonsnummer")), str(r.get("navn"))) for r in rows or [] if isinstance(r, dict) and r.get("navn")]


def fetch_entity(fetcher: Fetcher, case: Case) -> dict | None:
    """Fetch the live entity record; returns the JSON body or None. Emits the identity claims."""
    url = ENTITY.format(org=case.org)
    response = fetch(fetcher, case, url)
    if response.status != 200:
        state = status_claim_state(response)
        case.claim("legal_identity", None, state, [], note=f"Enhetsregisteret returned {response.status or response.error}", section="1")
        case.error("brreg_entity", f"{url}: {response.status} {response.error}")
        return None
    body = _json(response)
    if not isinstance(body, dict) or body.get("organisasjonsnummer") != case.org:
        case.claim("legal_identity", None, "failed", [], note="Entity response did not match the requested organisation number", section="1")
        case.error("brreg_entity", "organisation number mismatch in response")
        return None

    def evid(span: str) -> str:
        return case.ev(url, "official_registry", response.retrieved_at, response.sha256, span,
                       "brreg_enheter_json_field", license=LICENSE, http_status=200)

    name = body.get("navn")
    case.claim("legal_name", name, "available", [evid(f'navn: "{name}"')], confidence=1.0, section="1")
    form = _get(body, "organisasjonsform", "kode")
    case.claim("legal_form", {"code": form, "description": _get(body, "organisasjonsform", "beskrivelse")}, "available",
               [evid(f"organisasjonsform: {form}")], confidence=1.0, section="1")
    status = "active"
    if body.get("konkurs"):
        status = "bankrupt"
    elif body.get("underAvvikling") or body.get("underTvangsavviklingEllerTvangsopploesning"):
        status = "in_liquidation"
    case.claim("registry_status", status, "available",
               [evid(f"konkurs: {body.get('konkurs')}; underAvvikling: {body.get('underAvvikling')}")], confidence=1.0, section="1")
    for i in (1, 2, 3):
        code = body.get(f"naeringskode{i}")
        if isinstance(code, dict) and code.get("kode"):
            case.claim("industry", {"code": code.get("kode"), "description": code.get("beskrivelse"), "rank": i}, "available",
                       [evid(f"naeringskode{i}: {code.get('kode')} {code.get('beskrivelse')}")], key=str(i), confidence=1.0, section="1")
    address = _address(body.get("forretningsadresse")) or _address(body.get("postadresse"))
    if address:
        case.claim("registered_address", address, "available", [evid(f"forretningsadresse: {address}")], confidence=1.0, section="1")
    else:
        case.claim("registered_address", None, "not_available", [], note="No address in the registry record", section="1")
    employees = body.get("antallAnsatte")
    registered_flag = body.get("harRegistrertAntallAnsatte")
    if employees is not None and registered_flag is not False:
        case.claim("registered_employees", employees, "available",
                   [evid(f"antallAnsatte: {employees}; harRegistrertAntallAnsatte: {registered_flag}")], confidence=1.0, section="3",
                   effective_date=body.get("registreringsdatoAntallAnsatteEnhetsregisteret") or None)
    else:
        case.claim("registered_employees", None, "not_available", [],
                   note="Employee count not registered in Enhetsregisteret (unregistered is not the same as zero)", section="3")
    if body.get("registreringsdatoEnhetsregisteret"):
        case.claim("registration_date", body["registreringsdatoEnhetsregisteret"], "available",
                   [evid(f"registreringsdatoEnhetsregisteret: {body['registreringsdatoEnhetsregisteret']}")], confidence=1.0, section="1")
    if body.get("stiftelsesdato"):
        case.claim("founded_date", body["stiftelsesdato"], "available", [evid(f"stiftelsesdato: {body['stiftelsesdato']}")], confidence=1.0, section="1")
    if "erIKonsern" in body:
        case.claim("group_member", bool(body["erIKonsern"]), "available",
                   [evid(f"erIKonsern: {body['erIKonsern']}")], confidence=1.0, section="1",
                   note="Registered group (konsern) flag only; parent/subsidiary relations are not asserted here")
    if body.get("hjemmeside"):
        case.claim("registry_declared_website", body["hjemmeside"], "available", [evid(f"hjemmeside: {body['hjemmeside']}")],
                   confidence=1.0, section="4")
    declared = body.get("sisteInnsendteAarsregnskap")
    if declared:
        case.notes["latest_filing_year"] = str(declared)
        case.notes["latest_filing_evidence"] = evid(f"sisteInnsendteAarsregnskap: {declared}")
        case.claim("latest_filing_year", str(declared), "available", [case.notes["latest_filing_evidence"]], confidence=1.0, section="2",
                   note="Year of the latest annual accounts the registry says were filed")
    return body


def _freshness(case: Case, newest: str | None, evidence: list[str]) -> None:
    """State whether the figures we show are the latest filing the registry knows about."""
    declared = case.notes.get("latest_filing_year")
    ids = [e for e in [case.notes.get("latest_filing_evidence"), *evidence] if e]
    if not declared and not newest:
        return
    if not declared:
        status = "unverified"
    elif not newest:
        status = "no_figures_in_register"
    elif newest >= declared:
        status = "current"
    else:
        status = "lagging"
    case.claim("accounts_freshness", {"status": status, "newest_figures_year": newest, "registry_latest_filing_year": declared},
               "available", ids, confidence=1.0, section="2",
               note="current = figures cover the latest filing year; lagging = a newer filing exists but its figures are not in the normalised register")


def fetch_accounts(fetcher: Fetcher, case: Case) -> None:
    url = ACCOUNTS.format(org=case.org)
    response = fetch(fetcher, case, url)
    if response.status != 200:
        state = status_claim_state(response)
        case.claim("annual_accounts", None, state, [], note=f"Regnskapsregisteret returned {response.status or response.error}", section="2")
        if state == "failed":
            case.error("brreg_accounts", f"{url}: {response.status} {response.error}")
        return
    body = _json(response)
    records = body if isinstance(body, list) else []
    if not records:
        case.claim("annual_accounts", None, "not_available", [], note="No normalised annual accounts returned for this entity", section="2")
        _freshness(case, None, [])
        return
    seen: dict[str, dict] = {}
    for record in records:
        period = record.get("regnskapsperiode") or {}
        end = period.get("tilDato")
        if not end:
            continue
        slot = f"{end}:{record.get('regnskapstype') or ''}"
        previous = seen.get(slot)
        if previous is None or (record.get("id") or 0) > (previous.get("id") or 0):
            seen[slot] = record
    ordered = sorted(seen.values(), key=lambda r: (r["regnskapsperiode"]["tilDato"], r.get("id") or 0), reverse=True)
    years = []
    for record in ordered[:5]:
        period = record["regnskapsperiode"]
        label = f"{period.get('fraDato')}..{period['tilDato']}"
        value = {
            "accounts_type": record.get("regnskapstype"),
            "currency": record.get("valuta"),
            "period_from": period.get("fraDato"), "period_to": period["tilDato"],
            "revenue": _get(record, "resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter"),
            "operating_result": _get(record, "resultatregnskapResultat", "driftsresultat", "driftsresultat"),
            "profit_before_tax": _get(record, "resultatregnskapResultat", "ordinaertResultatFoerSkattekostnad"),
            "annual_result": _get(record, "resultatregnskapResultat", "aarsresultat"),
            "total_assets": _get(record, "eiendeler", "sumEiendeler"),
            "equity": _get(record, "egenkapitalGjeld", "egenkapital", "sumEgenkapital"),
            "debt": _get(record, "egenkapitalGjeld", "gjeldOversikt", "sumGjeld"),
            "audited": (None if not isinstance(record.get("revisjon"), dict) else
                        (not record["revisjon"].get("ikkeRevidertAarsregnskap") if "ikkeRevidertAarsregnskap" in record["revisjon"] else None)),
        }
        span = "; ".join(f"{k}={v}" for k, v in value.items() if v is not None and k not in ("period_from",))
        eid = case.ev(url, "official_annual_accounts", response.retrieved_at, response.sha256, span,
                      "brreg_regnskap_json_field", reporting_period=label, license=LICENSE, http_status=200)
        case.claim("annual_accounts", value, "available", [eid], key=f"{period['tilDato']}:{record.get('regnskapstype') or ''}",
                   confidence=1.0, reporting_period=label, section="2")
        years.append(period["tilDato"][:4])
    history_ev = case.ev(url, "official_annual_accounts", response.retrieved_at, response.sha256, f"periods: {sorted(seen)}",
                         "brreg_regnskap_periods", license=LICENSE, http_status=200)
    case.claim("account_history_years", sorted(set(years), reverse=True), "available", [history_ev],
               confidence=1.0, section="2", note="Filing years returned by the normalised accounts endpoint (up to 5 kept)")
    _freshness(case, max(years) if years else None, [history_ev])


def fetch_roles(fetcher: Fetcher, case: Case) -> None:
    url = ROLES.format(org=case.org)
    response = fetch(fetcher, case, url)
    if response.status != 200:
        state = status_claim_state(response)
        case.claim("leadership", None, state, [], note=f"Roles endpoint returned {response.status or response.error}", section="3")
        if state == "failed":
            case.error("brreg_roles", f"{url}: {response.status} {response.error}")
        return
    body = _json(response)
    groups = body.get("rollegrupper", []) if isinstance(body, dict) else []
    emitted = 0
    for group in groups:
        for item in group.get("roller", []):
            if item.get("avregistrert"):
                continue
            person = item.get("person") or {}
            pname = person.get("navn") or {}
            entity = item.get("enhet") or {}
            entity_name = entity.get("navn")
            if isinstance(entity_name, list):
                entity_name = " ".join(entity_name)
            name = " ".join(filter(None, [pname.get("fornavn"), pname.get("mellomnavn"), pname.get("etternavn")])) or entity_name
            if not name or person.get("erDoed"):
                continue
            code = _get(item, "type", "kode") or "?"
            description = _get(item, "type", "beskrivelse")
            value = {"name": name, "role_code": code, "role": description,
                     "role_holder_type": "organisation" if entity else "person",
                     "organisation_number": entity.get("organisasjonsnummer") if entity else None,
                     "registry_group_last_changed": group.get("sistEndret")}
            eid = case.ev(url, "official_roles", response.retrieved_at, response.sha256,
                          f"{description}: {name}", "brreg_roller_json_field", license=LICENSE, http_status=200,
                          effective_date=group.get("sistEndret"))
            case.claim("leadership", value, "available", [eid], key=f"{code}:{name.lower()}", confidence=1.0,
                       effective_date=group.get("sistEndret"), section="3")
            emitted += 1
            if emitted >= 25:
                break
        if emitted >= 25:
            break
    if emitted == 0:
        case.claim("leadership", None, "not_available", [], note="No active role holders in the registry", section="3")


def fetch_workplaces(fetcher: Fetcher, case: Case) -> None:
    url = SUBUNITS.format(org=case.org)
    response = fetch(fetcher, case, url)
    if response.status != 200:
        state = status_claim_state(response)
        if response.status in (404, 410):
            state = "not_available"
        case.claim("registered_workplace", None, state, [], note=f"Subunit endpoint returned {response.status or response.error}", section="3")
        if state == "failed":
            case.error("brreg_subunits", f"{url}: {response.status} {response.error}")
        return
    body = _json(response)
    rows = _get(body, "_embedded", "underenheter") or []
    total = _get(body, "page", "totalElements")
    if not rows:
        case.claim("registered_workplace", None, "not_available", [],
                   note="Checked: no registered subunits (establishments) for this entity", section="3")
        return
    for row in rows[:30]:
        addr = _address(row.get("beliggenhetsadresse")) or _address(row.get("postadresse"))
        value = {"organisation_number": row.get("organisasjonsnummer"), "name": row.get("navn"), "address": addr,
                 "employees": row.get("antallAnsatte"), "industry": _get(row, "naeringskode1", "kode"),
                 "started": row.get("oppstartsdato")}
        eid = case.ev(url, "official_subunits", response.retrieved_at, response.sha256,
                      f"{row.get('organisasjonsnummer')} {row.get('navn')}: {addr}", "brreg_underenheter_json_field",
                      license=LICENSE, http_status=200)
        case.claim("registered_workplace", value, "available", [eid], key=str(row.get("organisasjonsnummer")), confidence=1.0, section="3")
    if total and total > 30:
        case.claim("registered_workplace_total", total, "available",
                   [case.ev(url, "official_subunits", response.retrieved_at, response.sha256, f"totalElements: {total}",
                            "brreg_underenheter_page", license=LICENSE, http_status=200)], confidence=1.0, section="3",
                   note="Only the first 30 workplaces are listed as claims")


def fetch_updates(fetcher: Fetcher, case: Case) -> None:
    """Dated registry changes (public activity from an official source)."""
    url = UPDATES.format(org=case.org)
    response = fetch(fetcher, case, url)
    if response.status != 200:
        if response.status not in (404, 410):
            case.error("brreg_updates", f"{url}: {response.status} {response.error}", severity="warning")
        return
    body = _json(response)
    pages = _get(body, "page", "totalPages") or 1
    if pages > 1:  # entries are ascending by id: the newest are on the last page
        last = fetch(fetcher, case, url + f"&page={pages - 1}")
        if last.status == 200:
            response, body = last, _json(last)
            url = url + f"&page={pages - 1}"
    rows = _get(body, "_embedded", "oppdaterteEnheter") or []
    for row in sorted(rows, key=lambda r: r.get("dato") or "", reverse=True)[:5]:
        if row.get("organisasjonsnummer") != case.org or not row.get("dato"):
            continue
        value = {"kind": "registry_update", "change_type": row.get("endringstype"), "date": row["dato"],
                 "update_id": row.get("oppdateringsid"), "title": f"Register entry {row.get('endringstype') or 'update'}"}
        eid = case.ev(url, "official_registry", response.retrieved_at, response.sha256,
                      f"oppdateringsid {row.get('oppdateringsid')} {row.get('dato')} {row.get('endringstype')}",
                      "brreg_oppdateringer_json_field", effective_date=row["dato"], license=LICENSE, http_status=200)
        case.claim("public_activity", value, "available", [eid], key=f"registry:{row.get('oppdateringsid')}",
                   confidence=1.0, effective_date=row["dato"], section="5")
