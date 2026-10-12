#!/usr/bin/env python3
"""Turn a run report JSON into a Markdown summary (used for the README / submission)."""
import json
import sys
sys.stdout.reconfigure(encoding="utf-8")

report = json.load(open(sys.argv[1], encoding="utf-8"))
n = report["envelopes"]
ops = report["operations"]
print(f"- Inputs: {report['inputs']} -> envelopes: {n} (one per distinct input); envelope states: {report['envelope_state_counts']}")
print(f"- Wall clock: {ops['wall_clock_s']} s with {ops['workers']} workers; {ops['requests']} HTTP requests "
      f"({ops['requests_per_company_mean']}/company), {ops['bytes'] / 1e6:.0f} MB downloaded")
rt = ops["per_company_runtime_ms"]
print(f"- Per-company active time: p50 {rt['p50'] / 1000:.1f} s, p95 {rt['p95'] / 1000:.1f} s, max {rt['max'] / 1000:.1f} s")
print(f"- Third-party cost: ${ops['third_party_cost_usd']:.2f}; companies with at least one logged source error: {report['errors']['envelopes_with_errors']}")
print(f"- Claim states overall: {report['claim_availability_totals']}")
print(f"- Validation: {'PASSED' if report['validation']['passed'] else 'FAILED'} {report['validation']['checks']}")
print()
print("| Information type | Companies with verified data | Coverage | Claims | Other states |")
print("|---|---:|---:|---:|---|")
for family, v in report["coverage_by_information_type"].items():
    others = {k: c for k, c in v["state_counts"].items() if k != "available"}
    print(f"| {family} | {v['companies_with_available']} / {n} | {v['company_coverage_pct']}% | {v['claims']} | "
          f"{', '.join(f'{k} {c}' for k, c in others.items()) or '-'} |")
