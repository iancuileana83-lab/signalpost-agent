"""Idempotent refresh: merge a new envelope with the previous one, preserving evidence and typing changes."""
from __future__ import annotations

from typing import Any

from .http import utc_now
from .model import stable_json

WEB_FIELDS = {"official_website", "public_brand", "company_description", "company_profile", "job_posting", "public_activity",
              "hiring_status", "wikidata_entity"}
CHANGE_NAMES = {
    ("leadership", "new"): "new_role", ("leadership", "removed"): "role_ended",
    ("job_posting", "new"): "new_job", ("job_posting", "removed"): "closed_job",
    ("registered_workplace", "new"): "new_location", ("registered_workplace", "removed"): "closed_location",
    ("annual_accounts", "new"): "new_filing", ("annual_accounts", "changed"): "restated_filing",
    ("company_description", "changed"): "changed_description",
    ("public_activity", "new"): "new_activity",
}
MATERIAL = {"legal_name", "legal_form", "registry_status", "annual_accounts", "leadership", "official_website", "registered_address"}
MAX_HISTORY = 60


def _sig(claim: dict) -> str:
    return stable_json({"v": claim.get("value"), "a": claim.get("availability")})


def _name(field: str, kind: str) -> str:
    return CHANGE_NAMES.get((field, kind), f"{kind}_{field}")


def merge(current: dict[str, Any], previous: dict[str, Any] | None, *, web_checked: bool) -> dict[str, Any]:
    now = current["run"]["completed_at"]
    if not previous:
        for claim in current["claims"]:
            claim["first_observed_at"] = claim["last_observed_at"] = now
        current["changes"] = []
        current["history"] = []
        current["refresh"] = {"previous_run_id": None, "first_run": True}
        return current

    prev_claims = {c["claim_key"]: c for c in previous.get("claims", [])}
    new_claims = {c["claim_key"]: c for c in current["claims"]}
    changes: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = list(previous.get("history", []))
    unchanged = 0

    failed_fields = {c["field"] for c in current["claims"] if c["availability"] in ("failed", "blocked")}
    evidence = dict(previous.get("evidence_by_id", {}))
    prev_evidence = {e["id"]: e for e in previous.get("evidence", [])}
    merged_evidence = {**prev_evidence}

    for key, claim in new_claims.items():
        old = prev_claims.get(key)
        claim["first_observed_at"] = (old or {}).get("first_observed_at", now) if old and old.get("availability") == claim["availability"] else now
        claim["last_observed_at"] = now
        if not old:
            if claim["availability"] == "available":
                changes.append({"change_type": _name(claim["field"], "new"), "claim_key": key, "field": claim["field"],
                                "previous_value": None, "new_value": claim.get("value"),
                                "materiality": "high" if claim["field"] in MATERIAL else "normal",
                                "current_evidence_ids": claim["evidence_ids"], "previous_evidence_ids": [], "detected_at": now})
            continue
        if _sig(old) == _sig(claim):
            unchanged += 1
            claim["first_observed_at"] = old.get("first_observed_at", now)
            continue
        if old["availability"] == "available" and claim["availability"] in ("failed", "blocked"):
            # A failed refresh must not erase the last supported value.
            kept = dict(old)
            kept["stale"] = True
            kept["stale_reason"] = f"refresh {claim['availability']}: {claim.get('note') or 'source unavailable'}"[:200]
            current["claims"][current["claims"].index(claim)] = kept
            continue
        kind = "changed" if claim["availability"] == "available" and old["availability"] == "available" else \
            ("new" if claim["availability"] == "available" else "removed")
        changes.append({"change_type": _name(claim["field"], kind), "claim_key": key, "field": claim["field"],
                        "previous_value": old.get("value"), "new_value": claim.get("value"),
                        "materiality": "high" if claim["field"] in MATERIAL else "normal",
                        "current_evidence_ids": claim["evidence_ids"], "previous_evidence_ids": old.get("evidence_ids", []),
                        "detected_at": now})
        if old["availability"] == "available":
            history.append({**old, "superseded_at": now})

    for key, old in prev_claims.items():
        if key in new_claims or old.get("availability") != "available":
            continue
        field = old["field"]
        cannot_tell = field in failed_fields or (field in WEB_FIELDS and not web_checked)
        if cannot_tell:
            kept = dict(old)
            kept["stale"] = True
            kept["stale_reason"] = "not re-checked or source unavailable in this run"
            current["claims"].append(kept)
            continue
        changes.append({"change_type": _name(field, "removed"), "claim_key": key, "field": field, "previous_value": old.get("value"),
                        "new_value": None, "materiality": "high" if field in MATERIAL else "normal",
                        "current_evidence_ids": [], "previous_evidence_ids": old.get("evidence_ids", []), "detected_at": now})
        history.append({**old, "superseded_at": now})

    # Preserve every earlier evidence record (deduplicated by deterministic id).
    for e in current["evidence"]:
        old_e = prev_evidence.get(e["id"])
        if old_e:
            e["first_retrieved_at"] = old_e.get("first_retrieved_at", old_e.get("retrieved_at"))
        merged_evidence[e["id"]] = e
    referenced = {i for c in current["claims"] for i in c["evidence_ids"]}
    for eid, e in merged_evidence.items():
        if eid not in referenced:
            e = dict(e)
            e["superseded"] = True
            merged_evidence[eid] = e
    del evidence
    current["evidence"] = [merged_evidence[k] for k in sorted(merged_evidence)]
    current["claims"].sort(key=lambda c: (c.get("section") or "9", c["claim_key"]))
    current["changes"] = sorted(changes, key=lambda c: (c["claim_key"], c["change_type"]))
    current["history"] = history[-MAX_HISTORY:]
    current["refresh"] = {"previous_run_id": previous.get("run", {}).get("run_id"),
                          "previous_completed_at": previous.get("run", {}).get("completed_at"),
                          "claims_unchanged": unchanged, "changes": len(changes), "first_run": False, "computed_at": utc_now()}
    return current
