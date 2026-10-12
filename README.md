# signalpost-agent

An agent for the Builderr **Signalpost** challenge: give it Norwegian organisation numbers, get back one
terminal envelope per company with legal identity, annual accounts, leadership, workplaces, a verified
official website, company-owned profiles, hiring signals and dated public activity. Every fact carries its
source URL, retrieval time, content hash, extraction method and (where relevant) reporting period.
When the company match is uncertain it answers `ambiguous` or `not_available`; it never guesses.

Standard library only (Python 3.10+). No API keys, no accounts, no LLM, no paid service.

## Run it (one command)

```bash
python -m signalpost_agent run --input companies.jsonl --output out/envelopes.jsonl
```

`companies.jsonl` may be JSONL (objects with `organisation_number`, or bare numbers), a JSON array or one
number per line. Useful options: `--report out/report.json` (machine-readable run report),
`--previous earlier-envelopes.jsonl` (idempotent refresh + change detection), `--html out/html` (static,
mobile-friendly profile browser), `--deadline-seconds N` (soft time budget),
`--registry-snapshot FILE` (frozen universe `.jsonl.gz` or bulk `.csv`, used only if the live registry
fails), `--workers`, `--no-web`, `--nav-search` (see Sources).

## Clean-machine install

```bash
git clone https://github.com/iancuileana83-lab/signalpost-agent && cd signalpost-agent
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install .                                      # no third-party dependencies are pulled in
python -m signalpost_agent run --input companies.jsonl --output out/envelopes.jsonl
```

Tests: `pip install pytest && pytest -q` (10 offline tests).

## Output contract

One JSON object per distinct input organisation number, in input order, always (also when nothing is found
or a source is down). Top-level keys: `organisation_number`, `run`, `state`, `sections` (the seven
required profile sections), `summary` (claim-backed text plus an explicit list of unknowns), `claims`,
`evidence`, `changes`, `history`, `refresh`, `errors`, `operations`.
Claim availability is one of `available`, `not_available`, `blocked`, `not_applicable`, `ambiguous`,
`failed`. Absence is never turned into zero. See `docs/DESIGN.md`.

## Sources and licences

| Source | Used for | Access | Licence / terms |
|---|---|---|---|
| Enhetsregisteret open API (data.brreg.no) | identity, status, industry, address, employees, roles, subunits, registry updates | keyless JSON API, ~12 req/s global cap | NLOD 2.0 |
| Regnskapsregisteret open API (data.brreg.no) | normalised annual accounts, filing years | keyless JSON API | NLOD 2.0 |
| Wikidata SPARQL (query.wikidata.org) | item matched by exact org-number statement P2333: website, social identifiers | keyless, 1 batched query per 60 companies, descriptive User-Agent | CC0 |
| The company's own website | org-number proof, brand, description, outbound profile links, careers page, news feed / sitemap | only after identity is verified; robots.txt honoured; 1 req/s per host | company copyright; only short facts and links are kept |
| NAV Arbeidsplassen public ad search (opt-in `--nav-search`) | job ads whose ad page shows the employer org number | **off by default**: undocumented endpoint that rate-limited us after ~100 requests | NLOD 2.0 data |

Not used: LinkedIn, Meta, Indeed, Glassdoor, Google or any search engine, any paid or keyed API. Social
profile URLs are taken only from links on the verified company site (or from Wikidata identifiers); the
platforms themselves are never fetched.

Safety: only http(s); every host must resolve to a public address (SSRF guard, checked on each redirect);
1.5 MB response cap; per-host rate limits; retries with back-off only for 429/5xx/connection errors; a
per-company web budget (60 s).

## Identity rule (why wrong-company matches are unlikely)

A website is published only when the registry itself declares it, or the exact 9-digit organisation number
is shown on the site, or the legal name is on the site **and** an entity-specific registry detail (phone,
street + postcode, registered person's full name, registry e-mail domain) is shown there. A name match alone
is reported as `ambiguous` and the value is withheld. Details in `docs/DESIGN.md`.

## Refresh

`--previous` merges by stable claim key with deterministic evidence ids: re-running on unchanged data yields
zero changes and no duplicate evidence (checked on 20 companies, below); changed values emit typed changes
and keep the superseded value in `history`; a failed or blocked source keeps the last supported value marked
`stale`.

## Cost per official run

Third-party cost is **$0.00** (no paid API, no LLM). Compute is the only cost: the 1,000-company run below
used 16 worker threads, about 21 minutes wall clock, ~14,000 requests and ~300 MB of transfer on a laptop,
with negligible CPU, so a 1,000-company official batch (the stated size) is on the order of one cent of cloud
compute. A 1,100-company batch scales linearly (~23 minutes). Use `--deadline-seconds` to bound it.

## 100-company smoke test (random sample of the public universe, seed 20261012)

- Inputs: 100 -> envelopes: 100 (one per distinct input); envelope states: {'available': 100}
- Wall clock: 128.0 s with 16 workers; 1373 HTTP requests (13.7/company), 37 MB downloaded
- Per-company active time: p50 15.7 s, p95 34.2 s, max 50.8 s
- Third-party cost: $0.00; companies with at least one logged source error: 0
- Claim states overall: {'available': 2051, 'not_available': 561, 'ambiguous': 11}
- Validation: PASSED {'one_envelope_per_distinct_input': True, 'all_terminal': True, 'every_available_claim_has_evidence': True, 'evidence_has_url_time_hash': True, 'only_six_states': True}

| Information type | Companies with verified data | Coverage | Claims | Other states |
|---|---:|---:|---:|---|
| legal_identity | 100 / 100 | 100.0% | 100 | - |
| annual_accounts | 100 / 100 | 100.0% | 273 | - |
| leadership | 100 / 100 | 100.0% | 389 | - |
| registered_workplace | 73 / 100 | 73.0% | 77 | not_available 27 |
| official_website | 16 / 100 | 16.0% | 16 | ambiguous 10, not_available 74 |
| public_brand | 14 / 100 | 14.0% | 14 | not_available 86 |
| company_description | 11 / 100 | 11.0% | 11 | not_available 89 |
| company_profile | 10 / 100 | 10.0% | 19 | not_available 90 |
| job_posting | 1 / 100 | 1.0% | 1 | not_available 99 |
| company_news | 6 / 100 | 6.0% | 22 | not_available 94 |
| registry_update | 100 / 100 | 100.0% | 305 | - |

Inputs, report, per-company table and example envelopes: `reports/smoke-100/`.
Refresh check: a second run on 20 of these companies with `--previous` produced 0 changes and 0 duplicate
evidence ids.

## 1,000-company local run (random sample, seed 777)

- Inputs: 1000 -> envelopes: 1000 (one per distinct input); envelope states: {'available': 1000}
- Wall clock: 1242.0 s with 16 workers; 13908 HTTP requests (13.9/company), 296 MB downloaded
- Per-company active time: p50 15.7 s, p95 40.0 s, max 77.3 s
- Third-party cost: $0.00; companies with at least one logged source error: 0
- Claim states overall: {'available': 20764, 'not_available': 5437, 'blocked': 12, 'ambiguous': 127}
- Validation: PASSED {'one_envelope_per_distinct_input': True, 'all_terminal': True, 'every_available_claim_has_evidence': True, 'evidence_has_url_time_hash': True, 'only_six_states': True}

| Information type | Companies with verified data | Coverage | Claims | Other states |
|---|---:|---:|---:|---|
| legal_identity | 1000 / 1000 | 100.0% | 1000 | - |
| annual_accounts | 1000 / 1000 | 100.0% | 2802 | - |
| leadership | 998 / 1000 | 99.8% | 3956 | not_available 2 |
| registered_workplace | 772 / 1000 | 77.2% | 820 | not_available 228 |
| official_website | 186 / 1000 | 18.6% | 186 | not_available 675, blocked 12, ambiguous 127 |
| public_brand | 160 / 1000 | 16.0% | 160 | not_available 840 |
| company_description | 115 / 1000 | 11.5% | 115 | not_available 885 |
| company_profile | 102 / 1000 | 10.2% | 207 | not_available 898 |
| job_posting | 13 / 1000 | 1.3% | 51 | not_available 987 |
| company_news | 67 / 1000 | 6.7% | 262 | not_available 933 |
| registry_update | 1000 / 1000 | 100.0% | 2959 | - |

Report: `reports/local-1000/`.

## Honest limits

- Most companies in the universe are tiny holding or property companies with no public web presence, so
  web-derived fields are sparse by nature (website 18.6%, news 6.7%, job postings 1.3% of companies).
  Official-register fields are near complete.
- Hiring coverage comes only from the company's own careers page; NAV's ad search is opt-in because it
  rate-limits; the official NAV feed needs a registered token, which this agent does not use.
- The registry's kunngjøringer (announcements) have no open API and are not covered.
- 127 of 1,000 websites remain `ambiguous` (name matches but no entity-specific proof); they are withheld on
  purpose to avoid wrong-company publications.
- Precision was checked by reading every verified website's evidence span in the 100-company and a part of
  the 1,000-company sample; there is no hand-labelled gold set, so precision is not measured here, only
  argued by the rules above.

## Licence

MIT, see `LICENSE`.
