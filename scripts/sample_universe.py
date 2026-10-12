#!/usr/bin/env python3
"""Draw a reproducible random sample of organisation numbers from the public universe (.jsonl.gz)."""
import argparse
import gzip
import json
import random

p = argparse.ArgumentParser()
p.add_argument("--universe", required=True)
p.add_argument("--count", type=int, default=100)
p.add_argument("--seed", type=int, default=20261012)
p.add_argument("--output", required=True)
a = p.parse_args()
with gzip.open(a.universe, "rt", encoding="utf-8") as fh:
    orgs = [json.loads(line)["organisation_number"] for line in fh if line.strip()]
rng = random.Random(a.seed)
chosen = rng.sample(orgs, a.count)
with open(a.output, "w", encoding="utf-8") as out:
    for org in chosen:
        out.write(json.dumps({"organisation_number": org}) + "\n")
print(f"wrote {len(chosen)} of {len(orgs)} to {a.output}")
