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
