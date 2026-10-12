"""Evaluator entry point: `python -m signalpost_agent run --input companies.jsonl --output out/envelopes.jsonl`."""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import external, refresh
from .http import Fetcher, utc_now
from .model import Case
from .pipeline import build_envelope, official_phase, web_phase

FAMILIES = {
    "legal_identity": "legal_name", "annual_accounts": "annual_accounts", "leadership": "leadership",
    "registered_workplace": "registered_workplace", "official_website": "official_website", "public_brand": "public_brand",
    "company_description": "company_description", "company_profile": "company_profile", "job_posting": "job_posting",
    "company_news": "public_activity:company_news", "registry_update": "public_activity:registry_update",
}


# ----- input -----------------------------------------------------------------
def read_input(path: str) -> list[dict[str, Any]]:
    raw = Path(path).read_bytes()
    text = (gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw).decode("utf-8-sig")
    stripped = text.lstrip()
    values: list[Any]
    if stripped.startswith("["):
        values = json.loads(stripped)
    elif stripped.startswith("{") and "\n" not in stripped.strip():
        body = json.loads(stripped)
        values = body.get("organisation_numbers") or body.get("companies") or [body]
    else:
        values = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            values.append(json.loads(line) if line.startswith(("{", "[", '"')) else line)
    out = []
    for value in values:
        raw_org = value
        if isinstance(value, dict):
            raw_org = next((value[k] for k in ("organisation_number", "organisationNumber", "orgnr", "org_number", "organisasjonsnummer", "id")
                            if k in value), "")
        digits = "".join(ch for ch in str(raw_org) if ch.isdigit())
        out.append({"organisation_number": digits if len(digits) == 9 else str(raw_org).strip(), "valid": len(digits) == 9})
    return out


def load_snapshot(path: str | None, wanted: set[str]) -> dict[str, dict]:
    """Frozen registry rows (Builderr universe .jsonl[.gz] or brreg bulk .csv[.gz]) for the wanted organisations only."""
    if not path:
        return {}
    raw = Path(path).read_bytes()
    text = (gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw).decode("utf-8-sig", "replace")
    rows: dict[str, dict] = {}
    if text.lstrip().startswith("{"):
        for line in text.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("organisation_number") in wanted:
                rows[row["organisation_number"]] = row
    else:
        for row in csv.DictReader(io.StringIO(text)):
            org = row.get("organisasjonsnummer")
            if org in wanted:
                rows[org] = {"organisation_number": org, "name": row.get("navn"), "legal_form": row.get("organisasjonsform.kode"),
                             "industry_code": row.get("naeringskode1.kode"), "industry_label": row.get("naeringskode1.beskrivelse"),
                             "employees": int(row["antallAnsatte"]) if (row.get("antallAnsatte") or "").isdigit() else None,
                             "website": row.get("hjemmeside")}
    return rows


def load_previous(path: str | None) -> dict[str, dict]:
    if not path or not Path(path).exists():
        return {}
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            env = json.loads(line)
            out[env["organisation_number"]] = env
    return out


# ----- run -------------------------------------------------------------------
def run(args: argparse.Namespace) -> int:
    t0 = time.monotonic()
    started_at = utc_now()
    inputs = read_input(args.input)
    seen: set[str] = set()
    orgs: list[str] = []
    for item in inputs:
        if item["organisation_number"] not in seen:
            seen.add(item["organisation_number"])
            orgs.append(item["organisation_number"])
    valid = {item["organisation_number"] for item in inputs if item["valid"]}
    snapshot = load_snapshot(args.registry_snapshot, set(orgs))
    previous = load_previous(args.previous)
    deadline = t0 + args.deadline_seconds if args.deadline_seconds else None
    fetcher = Fetcher(default_interval=args.site_interval, retries=args.retries, timeout=args.timeout,
                      host_intervals={"data.brreg.no": args.brreg_interval, "query.wikidata.org": 1.5, "arbeidsplassen.nav.no": args.nav_interval})
    run_meta = {"run_id": args.run_id, "started_at": started_at}
    cases: dict[str, Case] = {org: Case(org) for org in orgs}
    entities: dict[str, dict | None] = {}
    phase_times: dict[str, float] = {}

    def log(msg: str) -> None:
        if not args.quiet:
            print(f"[{time.monotonic() - t0:7.1f}s] {msg}", file=sys.stderr, flush=True)

    # Phase A: official registers (always for every company).
    def phase_a(org: str) -> None:
        case = cases[org]
        t_start = time.monotonic()
        try:
            _phase_a(org, case)
        finally:
            case.ops.busy_s += time.monotonic() - t_start

    def _phase_a(org: str, case: Case) -> None:
        if org not in valid:
            case.claim("legal_identity", None, "not_available", [], note="Input is not a 9-digit organisation number", section="1")
            entities[org] = None
            return
        try:
            entities[org] = official_phase(fetcher, case, snapshot.get(org))
        except Exception as exc:  # noqa: BLE001 - one bad company must never drop the batch
            case.error("official", f"{type(exc).__name__}: {exc}")
            case.claim("legal_identity", None, "failed", [], note="Unexpected error in the official-register phase", section="1")
            entities[org] = None

    log(f"phase A: official registers for {len(orgs)} companies")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(phase_a, orgs))
    phase_times["official_s"] = round(time.monotonic() - t0, 1)

    # Phase B: web + external sources.
    wikidata: dict[str, dict] = {}
    if not args.no_web and not args.no_wikidata:
        log("phase B0: Wikidata batch lookup (exact organisation-number statements)")
        wikidata, wd_errors = external.wikidata_batch(fetcher, [o for o in orgs if o in valid])
        for message in wd_errors:
            log("wikidata warning: " + message)
    outcomes: dict[str, dict] = {}
    web_done: set[str] = set()

    def phase_b(org: str) -> None:
        case = cases[org]
        if args.no_web or org not in valid:
            return
        if deadline and time.monotonic() > deadline:
            for f, sec in (("official_website", "4"), ("job_posting", "5"), ("public_activity", "5")):
                case.claim(f, None, "failed", [], note="Time budget exhausted before web research reached this company", section=sec)
            case.error("budget", "web phase skipped: time budget exhausted", severity="warning")
            return
        if entities.get(org) is None and not snapshot.get(org):
            return
        t_start = time.monotonic()
        case.ops.deadline = t_start + args.company_budget_seconds
        try:
            outcomes[org] = web_phase(fetcher, case, entities.get(org), wikidata.get(org), use_nav=args.nav_search, max_guesses=args.max_guesses)
            web_done.add(org)
        finally:
            case.ops.busy_s += time.monotonic() - t_start

    log("phase B: websites, jobs, news")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for index, _ in enumerate(pool.map(phase_b, orgs), 1):
            if index % 50 == 0:
                log(f"  web phase {index}/{len(orgs)}")
    phase_times["total_s"] = round(time.monotonic() - t0, 1)

    # Envelopes: exactly one per distinct input organisation number, in input order.
    completed_at = utc_now()
    envelopes = []
    for org in orgs:
        case = cases[org]
        run = {**run_meta, "completed_at": completed_at}
        try:
            env = build_envelope(case, run, outcomes.get(org), entities.get(org) is not None or bool(snapshot.get(org)), org in valid)
            env = refresh.merge(env, previous.get(org), web_checked=org in web_done)
        except Exception as exc:  # noqa: BLE001 - last-resort terminal envelope
            env = {"schema_version": "signalpost-agent/1", "organisation_number": org, "run": {**run, "terminal_status": "completed"},
                   "state": "failed", "claims": [], "evidence": [], "changes": [], "history": [], "refresh": {},
                   "errors": [{"source": "assembly", "message": f"{type(exc).__name__}: {exc}"}],
                   "operations": {"requests": case.ops.requests, "bytes": case.ops.bytes, "runtime_ms": case.ops.runtime_ms(), "third_party_cost_usd": 0.0}}
        envelopes.append(env)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for env in envelopes:
            handle.write(json.dumps(env, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(out)

    report = make_report(args, envelopes, orgs, inputs, phase_times, fetcher, started_at, completed_at)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.html:
        from .htmlreport import write_html
        write_html(Path(args.html), envelopes)
    log(f"done: {len(envelopes)} envelopes, {report['operations']['requests']} requests, {phase_times['total_s']} s")
    return 0 if report["validation"]["passed"] else 1


def make_report(args, envelopes, orgs, inputs, phase_times, fetcher, started_at, completed_at) -> dict[str, Any]:
    n = len(envelopes)
    states = Counter(e["state"] for e in envelopes)
    claim_states: dict[str, Counter] = {}
    coverage: dict[str, dict[str, Any]] = {}
    for family, selector in FAMILIES.items():
        field, _, kind = selector.partition(":")
        companies = claims = 0
        states_c: Counter = Counter()
        for env in envelopes:
            mine = [c for c in env["claims"] if c["field"] == field and (not kind or (c.get("value") or {}).get("kind") == kind
                                                                           or (c["availability"] != "available" and not kind))]
            if kind:
                mine = [c for c in env["claims"] if c["field"] == field and (c.get("value") or {}).get("kind") == kind]
            avail = [c for c in mine if c["availability"] == "available"]
            if avail:
                companies += 1
                claims += len(avail)
                states_c["available"] += 1
            else:
                best = next((c["availability"] for c in env["claims"] if c["field"] == field and c["availability"] != "available"), "not_available")
                states_c[best if not kind else "not_available"] += 1
        coverage[family] = {"companies_with_available": companies, "company_coverage_pct": round(100 * companies / n, 1) if n else 0.0,
                            "claims": claims, "state_counts": dict(states_c)}
        claim_states[family] = states_c
    runtimes = sorted(e["operations"]["runtime_ms"] for e in envelopes)
    reqs = sum(e["operations"]["requests"] for e in envelopes)
    byts = sum(e["operations"]["bytes"] for e in envelopes)
    evidence_ok = all(ev.get("source_url") and ev.get("retrieved_at") and ev.get("content_sha256") is not None for e in envelopes for ev in e["evidence"])
    claims_with_evidence = all(c["evidence_ids"] for e in envelopes for c in e["claims"] if c["availability"] == "available" and not c.get("stale"))
    valid_states = all(c["availability"] in ("available", "not_available", "blocked", "not_applicable", "ambiguous", "failed")
                       for e in envelopes for c in e["claims"])
    checks = {
        "one_envelope_per_distinct_input": len(envelopes) == len(orgs) == len({e["organisation_number"] for e in envelopes}),
        "all_terminal": all(e["run"]["terminal_status"] == "completed" for e in envelopes),
        "every_available_claim_has_evidence": claims_with_evidence,
        "evidence_has_url_time_hash": evidence_ok,
        "only_six_states": valid_states,
    }
    return {
        "run_id": args.run_id, "started_at": started_at, "completed_at": completed_at,
        "inputs": len(inputs), "distinct_inputs": len(orgs), "envelopes": n,
        "envelope_state_counts": dict(states),
        "coverage_by_information_type": coverage,
        "claim_availability_totals": dict(Counter(c["availability"] for e in envelopes for c in e["claims"])),
        "operations": {"requests": reqs, "bytes": byts, "wall_clock_s": phase_times.get("total_s"), "phase_seconds": phase_times,
                       "per_company_runtime_ms": {"p50": runtimes[n // 2] if n else None, "p95": runtimes[min(n - 1, int(n * 0.95))] if n else None,
                                                  "max": runtimes[-1] if n else None},
                       "requests_per_company_mean": round(reqs / n, 1) if n else 0,
                       "workers": args.workers, "third_party_cost_usd": 0.0},
        "errors": {"envelopes_with_errors": sum(1 for e in envelopes if e["errors"]),
                   "by_source": dict(Counter(err["source"] for e in envelopes for err in e["errors"]))},
        "refresh": {"companies_with_previous": sum(1 for e in envelopes if not e["refresh"].get("first_run", True)),
                    "total_changes": sum(len(e["changes"]) for e in envelopes)},
        "validation": {"passed": all(checks.values()), "checks": checks},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="signalpost_agent", description="Signalpost company-research agent")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="research a batch of Norwegian organisation numbers")
    p.add_argument("--input", required=True, help="JSON / JSONL / text file of organisation numbers (or objects with organisation_number)")
    p.add_argument("--output", required=True, help="terminal envelopes, JSONL, one per distinct input organisation")
    p.add_argument("--previous", help="envelopes JSONL from an earlier run: enables idempotent refresh and change detection")
    p.add_argument("--report", help="machine-readable run report (JSON)")
    p.add_argument("--html", help="directory for a static, mobile-friendly profile browser")
    p.add_argument("--registry-snapshot", help="optional frozen registry snapshot (universe .jsonl.gz or bulk .csv[.gz]) used if the live registry fails")
    p.add_argument("--run-id", default=time.strftime("run-%Y%m%dT%H%M%SZ", time.gmtime()))
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--deadline-seconds", type=float, default=0, help="soft budget: web research is skipped (and stated) once exceeded")
    p.add_argument("--brreg-interval", type=float, default=0.08, help="seconds between requests to data.brreg.no")
    p.add_argument("--site-interval", type=float, default=1.0, help="seconds between requests to the same company host")
    p.add_argument("--nav-interval", type=float, default=1.0)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--max-guesses", type=int, default=7, help="name-derived domain candidates to try per company")
    p.add_argument("--company-budget-seconds", type=float, default=60.0, help="max wall time spent on web research per company")
    p.add_argument("--no-web", action="store_true", help="official registers only")
    p.add_argument("--nav-search", action="store_true",
                   help="also query NAV Arbeidsplassen's public ad search (off by default: the endpoint is undocumented and rate-limits hard)")
    p.add_argument("--no-wikidata", action="store_true")
    p.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
