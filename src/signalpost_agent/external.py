"""Keyless external sources whose records can be tied to the exact organisation number."""
from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

from .http import Fetcher, Ops, Response
from .model import Case
from .web import name_tokens, norm, parse_date

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
NAV_SEARCH = "https://arbeidsplassen.nav.no/stillinger/api/search"
NAV_AD = "https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"
NAV_ORGNR_RE = re.compile(r'"employer\\*"\s*:\s*\{\s*\\*"orgnr\\*"\s*:\s*\\*"(\d{9})')

_WD_SOCIAL = {"P4264": "linkedin_company_id", "P2013": "facebook_id", "P2003": "instagram_id", "P2002": "x_id", "P2397": "youtube_channel_id"}


def wikidata_batch(fetcher: Fetcher, orgs: list[str], chunk: int = 60) -> tuple[dict[str, dict], list[str]]:
    """One SPARQL query per chunk, keyed by exact Norwegian organisation number (P2333)."""
    out: dict[str, dict] = {}
    errors: list[str] = []
    ops = Ops()
    for start in range(0, len(orgs), chunk):
        part = orgs[start:start + chunk]
        values = " ".join(f'"{o}"' for o in part)
        optional = " ".join(f"OPTIONAL {{ ?c wdt:{p} ?{p} . }}" for p in _WD_SOCIAL)
        query = (f"SELECT ?org ?c ?cLabel ?web {' '.join('?' + p for p in _WD_SOCIAL)} WHERE {{ VALUES ?org {{ {values} }} "
                 f"?c wdt:P2333 ?org . OPTIONAL {{ ?c wdt:P856 ?web . }} {optional} "
                 f'SERVICE wikibase:label {{ bd:serviceParam wikibase:language "nb,en". }} }}')
        response = fetcher.get(WIKIDATA_SPARQL, ops, robots=False, accept="application/sparql-results+json",
                               data=urllib.parse.urlencode({"query": query}).encode(),
                               headers={"Content-Type": "application/x-www-form-urlencoded"})
        if response.status != 200:
            errors.append(f"wikidata chunk {start}: {response.status} {response.error}")
            continue
        try:
            rows = json.loads(response.text)["results"]["bindings"]
        except (ValueError, KeyError):
            errors.append(f"wikidata chunk {start}: unparsable response")
            continue
        for row in rows:
            org = row.get("org", {}).get("value")
            if org not in part:
                continue
            record = out.setdefault(org, {"entity": row["c"]["value"], "label": row.get("cLabel", {}).get("value"),
                                          "websites": set(), "social": {}, "retrieved_at": response.retrieved_at,
                                          "sha256": response.sha256})
            if record["entity"] != row["c"]["value"]:
                record["conflict"] = True  # two items share one org number: refuse to use
            if row.get("web", {}).get("value"):
                record["websites"].add(row["web"]["value"])
            for prop, key in _WD_SOCIAL.items():
                if row.get(prop, {}).get("value"):
                    record["social"].setdefault(key, set()).add(row[prop]["value"])
    for record in out.values():
        sites = sorted(record.pop("websites"))
        record["website"] = sites[0] if len(sites) == 1 else None
        record["social"] = {k: sorted(v)[0] for k, v in record["social"].items() if len(v) == 1}
    return {o: r for o, r in out.items() if not r.get("conflict")}, errors


def emit_wikidata(case: Case, record: dict | None) -> None:
    if not record:
        return
    qid = record["entity"].rsplit("/", 1)[-1]
    url = f"https://www.wikidata.org/wiki/{qid}"
    eid = case.ev(url, "open_knowledge_base", record["retrieved_at"], record["sha256"],
                  f"{qid} {record.get('label') or ''}: Norwegian organisation number (P2333) = {case.org}",
                  "wikidata_p2333_exact_match", license="CC0 1.0")
    case.claim("wikidata_entity", {"qid": qid, "label": record.get("label")}, "available", [eid], confidence=0.97, section="1",
               note="Matched by exact organisation number statement (P2333)")


def wikidata_profiles(case: Case, record: dict | None, have: set[str]) -> None:
    if not record:
        return
    qid = record["entity"].rsplit("/", 1)[-1]
    url = f"https://www.wikidata.org/wiki/{qid}"
    patterns = {
        "linkedin_company_id": ("linkedin", "https://www.linkedin.com/company/{}"),
        "facebook_id": ("facebook", "https://www.facebook.com/{}"),
        "instagram_id": ("instagram", "https://www.instagram.com/{}"),
        "x_id": ("x", "https://x.com/{}"),
        "youtube_channel_id": ("youtube", "https://www.youtube.com/channel/{}"),
    }
    for key, value in record.get("social", {}).items():
        platform, pattern = patterns[key]
        if platform in have:
            continue
        eid = case.ev(url, "open_knowledge_base", record["retrieved_at"], record["sha256"], f"{key} = {value}", "wikidata_property",
                      license="CC0 1.0")
        case.claim("company_profile", {"platform": platform, "url": pattern.format(value), "linked_from": url}, "available", [eid],
                   key=platform, confidence=0.9, section="4",
                   note="Identifier from the Wikidata item matched by organisation number; the platform itself was not scraped")


class NavBreaker:
    """Stops querying NAV for the rest of the run after repeated 429/403 responses (polite back-off)."""
    def __init__(self, limit: int = 3):
        self.limit, self.hits, self.tripped = limit, 0, False
        import threading
        self._lock = threading.Lock()

    def record(self, status: int) -> None:
        with self._lock:
            if status in (403, 429):
                self.hits += 1
                if self.hits >= self.limit:
                    self.tripped = True
            elif status == 200:
                self.hits = 0


NAV_BREAKER = NavBreaker()


def nav_jobs(fetcher: Fetcher, case: Case, legal_name: str, max_ads: int = 8) -> dict[str, Any]:
    """Public NAV Arbeidsplassen ads. An ad is accepted only when its page shows employer.orgnr == org."""
    if NAV_BREAKER.tripped:
        return {"checked": False, "count": 0, "state": "blocked", "skipped": "rate limited earlier in this run"}
    query = urllib.parse.urlencode({"q": legal_name, "size": 20})
    response = fetcher.get(f"{NAV_SEARCH}?{query}", case.ops, robots=True, accept="application/json")
    NAV_BREAKER.record(response.status)
    if response.status != 200:
        case.error("nav_arbeidsplassen", f"search: {response.status} {response.error}", severity="warning")
        return {"checked": False, "count": 0, "state": "failed" if not response.blocked else "blocked"}
    try:
        hits = json.loads(response.text)["hits"]["hits"]
    except (ValueError, KeyError):
        case.error("nav_arbeidsplassen", "unparsable search response", severity="warning")
        return {"checked": False, "count": 0, "state": "failed"}
    wanted = {norm(legal_name), " ".join(name_tokens(legal_name))}
    matched = []
    for hit in hits:
        src = hit.get("_source", {})
        names = {norm(str(src.get("businessName") or "")), norm(str((src.get("employer") or {}).get("name") or ""))}
        names |= {" ".join(name_tokens(n)) for n in list(names) if n}
        if names & wanted:
            matched.append(src)
    verified = 0
    mismatches = 0
    for src in matched[:max_ads]:
        uuid = src.get("uuid")
        if not uuid:
            continue
        page = fetcher.get(NAV_AD.format(uuid=uuid), case.ops, robots=True)
        NAV_BREAKER.record(page.status)
        if page.status != 200:
            continue
        orgs = set(NAV_ORGNR_RE.findall(page.text))
        if case.org not in orgs:
            mismatches += 1
            if mismatches >= 3:
                break
            continue
        title = str(src.get("title") or "").strip()
        locations = src.get("locationList") or []
        place = ", ".join(filter(None, [(locations[0] or {}).get("city"), (locations[0] or {}).get("municipal")])) if locations else None
        posted = parse_date(src.get("published"))
        expires = parse_date(src.get("expires"))
        eid = case.ev(page.final_url, "official_public_job_portal", page.retrieved_at, page.sha256,
                      f"employer.orgnr {case.org}; \"{title}\"; published {posted}", "nav_ad_page_employer_orgnr_exact_match",
                      http_status=200, effective_date=posted, license="NLOD 2.0")
        case.claim("job_posting", {"title": title[:160], "date_posted": posted, "valid_through": expires, "location": place,
                                   "url": page.final_url, "hiring_organization": src.get("businessName"),
                                   "portal": "NAV Arbeidsplassen", "original_source": src.get("source")},
                   "available", [eid], key="nav:" + str(uuid), confidence=0.98, effective_date=posted, section="5",
                   note="Employer organisation number shown on the ad page equals the entity's")
        verified += 1
    return {"checked": True, "count": verified, "state": "available" if verified else "not_available",
            "candidates": len(matched), "mismatches": mismatches}
