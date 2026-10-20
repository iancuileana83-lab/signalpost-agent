- Inputs: 100 -> envelopes: 100 (one per distinct input); envelope states: {'available': 100}
- Wall clock: 140.1 s with 16 workers; 1390 HTTP requests (13.9/company), 38 MB downloaded
- Per-company active time: p50 15.7 s, p95 44.9 s, max 51.2 s
- Third-party cost: $0.00; companies with at least one logged source error: 0
- Claim states overall: {'available': 2258, 'not_available': 555, 'ambiguous': 11, 'blocked': 1}
- Validation: PASSED {'every_available_claim_has_resolvable_source_and_time': True, 'latest_label_matches_registry_filing_year': True, 'no_zero_employees_for_unregistered': True, 'one_envelope_per_distinct_input': True, 'all_terminal': True, 'every_available_claim_has_evidence': True, 'evidence_has_url_time_hash': True, 'only_six_states': True}

| Information type | Companies with verified data | Coverage | Claims | Other states |
|---|---:|---:|---:|---|
| legal_identity | 100 / 100 | 100.0% | 100 | - |
| annual_accounts | 100 / 100 | 100.0% | 273 | - |
| leadership | 100 / 100 | 100.0% | 389 | - |
| registered_workplace | 73 / 100 | 73.0% | 77 | not_available 27 |
| official_website | 17 / 100 | 17.0% | 17 | ambiguous 10, not_available 72, blocked 1 |
| public_brand | 15 / 100 | 15.0% | 15 | not_available 85 |
| company_description | 12 / 100 | 12.0% | 12 | not_available 88 |
| company_profile | 10 / 100 | 10.0% | 19 | not_available 90 |
| job_posting | 1 / 100 | 1.0% | 1 | not_available 99 |
| company_news | 7 / 100 | 7.0% | 23 | not_available 93 |
| registry_update | 100 / 100 | 100.0% | 306 | - |
