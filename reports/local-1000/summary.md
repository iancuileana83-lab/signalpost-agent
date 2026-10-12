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
