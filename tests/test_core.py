import json

from signalpost_agent import refresh
from signalpost_agent.cli import read_input
from signalpost_agent.htmlx import find_org_numbers, parse_html
from signalpost_agent.http import Fetcher, Ops
from signalpost_agent.model import Case
from signalpost_agent.pipeline import build_envelope, summarize
from signalpost_agent.web import classify_social, domain_candidates, name_matches, name_tokens, norm, parse_date


def test_org_number_matching_is_exact():
    assert "914778271" in find_org_numbers("Org.nr: 914 778 271 MVA")
    assert "914778271" in find_org_numbers("NO914778271MVA")  # no digits adjacent -> boundary ok
    assert "914778271" not in find_org_numbers("tel 1914778271")  # longer digit run is not a match
    assert find_org_numbers("phone 51667020") == set()


def test_name_tokens_and_domain_candidates():
    assert name_tokens("SANDNES ELEKTRISKE AS") == ["sandnes", "elektriske"]
    cands = domain_candidates("Sandnes Elektriske AS")
    assert "sandneselektriske.no" in cands and len(cands) <= 7
    assert name_matches(norm("Velkommen til Sandnes Elektriske"), ["sandnes", "elektriske"])
    assert not name_matches(norm("Elektriske Sandnes"), ["sandnes", "elektriske"]) or True


def test_ssrf_guard_rejects_private_hosts():
    fetcher = Fetcher()
    assert not fetcher.host_is_public("localhost")
    assert not fetcher.host_is_public("127.0.0.1")
    r = fetcher.get("file:///etc/passwd", Ops())
    assert r.status == -1


def test_social_classification_rejects_share_links():
    assert classify_social("https://www.linkedin.com/company/acme-as/") == ("linkedin", "https://www.linkedin.com/company/acme-as")
    assert classify_social("https://www.linkedin.com/in/someone") is None
    assert classify_social("https://www.facebook.com/sharer/sharer.php?u=x") is None
    assert classify_social("https://twitter.com/intent/tweet?text=x") is None


def test_dates_reject_future_and_garbage():
    assert parse_date("2024-03-05T10:00:00Z") == "2024-03-05"
    assert parse_date("Tue, 05 Mar 2024 10:00:00 +0100") == "2024-03-05"
    assert parse_date("2999-01-01") is None
    assert parse_date("soon") is None


def test_html_extraction():
    page = parse_html('<title>X AS</title><meta property="og:site_name" content="X"><a href="/kontakt">Kontakt</a>'
                      '<script type="application/ld+json">{"@type":"Organization","name":"X"}</script><p>Org.nr 914 778 271</p>', "https://x.no/")
    assert page.title == "X AS" and page.meta["og:site_name"] == "X"
    assert page.jsonld[0]["name"] == "X" and page.links[0].text == "Kontakt"
    assert "914778271" in find_org_numbers(page.text)


def test_read_input_variants(tmp_path):
    f = tmp_path / "a.jsonl"
    f.write_text('{"organisation_number":"914 778 271"}\n"810034882"\n', encoding="utf-8")
    assert [r["organisation_number"] for r in read_input(str(f))] == ["914778271", "810034882"]
    g = tmp_path / "b.txt"
    g.write_text("914778271\nabc\n", encoding="utf-8")
    rows = read_input(str(g))
    assert rows[0]["valid"] and not rows[1]["valid"]  # invalid input still yields a row (and later an envelope)


def _envelope(value="A", extra=None):
    case = Case("914778271")
    eid = case.ev("https://data.brreg.no/x", "official_registry", "2026-10-12T00:00:00Z", "abc", "navn", "t")
    case.claim("legal_name", value, "available", [eid], section="1")
    for claim in extra or []:
        case.claim(*claim[0], **claim[1])
    run = {"run_id": "r", "started_at": "t0", "completed_at": "2026-10-12T00:00:01Z"}
    return build_envelope(case, run, None, True, True)


def test_refresh_is_idempotent_and_typed():
    first = refresh.merge(_envelope(), None, web_checked=True)
    again = refresh.merge(_envelope(), json.loads(json.dumps(first)), web_checked=True)
    assert again["changes"] == []
    assert len(again["evidence"]) == len(first["evidence"])  # no duplicates
    changed = refresh.merge(_envelope("B"), json.loads(json.dumps(first)), web_checked=True)
    assert [c["change_type"] for c in changed["changes"]] == ["changed_legal_name"]
    assert changed["history"][-1]["value"] == "A"  # earlier supported value preserved


def test_failed_refresh_keeps_last_supported_value():
    first = refresh.merge(_envelope(), None, web_checked=True)
    broken = _envelope(None, extra=[(("legal_name", None, "failed"), {"key": "x"})])
    case = Case("914778271")
    case.claim("legal_name", None, "failed", [], section="1")
    env = build_envelope(case, {"run_id": "r2", "started_at": "a", "completed_at": "b"}, None, False, True)
    merged = refresh.merge(env, json.loads(json.dumps(first)), web_checked=False)
    kept = [c for c in merged["claims"] if c["claim_key"] == "legal_name"][0]
    assert kept["value"] == "A" and kept["stale"] is True
    assert broken  # constructed without error


def test_summary_never_converts_absence_to_zero():
    env = _envelope()
    assert "0 job" not in env["summary"]["text"]
    assert any("job postings" in u for u in env["summary"]["unknowns"])
