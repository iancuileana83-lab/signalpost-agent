"""Per-company claim/evidence builder producing the terminal envelope pieces."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .http import Ops, utc_now

STATES = ("available", "not_available", "blocked", "not_applicable", "ambiguous", "failed")


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def short_hash(*parts: str, size: int = 12) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:size]


class Case:
    """Collects claims, evidence and errors for one organisation number."""

    def __init__(self, org: str):
        self.org = org
        self.claims: list[dict[str, Any]] = []
        self.evidence: dict[str, dict[str, Any]] = {}
        self.errors: list[dict[str, Any]] = []
        self.ops = Ops()
        self.notes: dict[str, Any] = {}

    # ----- evidence -------------------------------------------------------
    def ev(self, source_url: str, source_class: str, retrieved_at: str | None, sha256: str | None, span: str,
           method: str, *, reporting_period: str | None = None, effective_date: str | None = None,
           http_status: int | None = None, final_url: str | None = None, license: str | None = None) -> str:
        span = (span or "")[:400]
        eid = "ev-" + short_hash(source_url, method, span)
        record = {
            "id": eid, "source_url": source_url, "source_class": source_class,
            "retrieved_at": retrieved_at or utc_now(), "content_sha256": sha256 or "",
            "claim_span": span, "extraction_method": method,
        }
        if reporting_period:
            record["reporting_period"] = reporting_period
        if effective_date:
            record["effective_date"] = effective_date
        if http_status is not None:
            record["http_status"] = http_status
        if final_url and final_url != source_url:
            record["final_url"] = final_url
        if license:
            record["license"] = license
        self.evidence[eid] = record
        return eid

    # ----- claims ---------------------------------------------------------
    def claim(self, field: str, value: Any, availability: str, evidence_ids: list[str] | tuple[str, ...] = (), *,
              key: str | None = None, confidence: float | None = None, note: str | None = None,
              reporting_period: str | None = None, effective_date: str | None = None,
              section: str | None = None) -> dict[str, Any]:
        assert availability in STATES, availability
        claim: dict[str, Any] = {
            "claim_key": field if key is None else f"{field}:{key}",
            "field": field, "value": value, "availability": availability,
            "evidence_ids": list(dict.fromkeys(evidence_ids)),
        }
        if confidence is not None:
            claim["confidence"] = confidence
        if note:
            claim["note"] = note
        if reporting_period:
            claim["reporting_period"] = reporting_period
        if effective_date:
            claim["effective_date"] = effective_date
        if section:
            claim["section"] = section
        self.claims.append(claim)
        return claim

    def error(self, source: str, message: str, *, severity: str = "error") -> None:
        self.errors.append({"source": source, "message": message[:300], "severity": severity, "at": utc_now()})
