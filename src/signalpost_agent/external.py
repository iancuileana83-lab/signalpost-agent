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
