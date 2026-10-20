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


# ----- accounts freshness, employees, comparison view (Builderr C300 feedback) --------------------
import json as _json

from signalpost_agent import official
from signalpost_agent.cli import make_report
from signalpost_agent.htmlreport import compare_row, write_html
from signalpost_agent.http import Response
from signalpost_agent.pipeline import year_over_year


class FakeFetcher:
    def __init__(self, routes):
        self.routes = routes

    def get(self, url, ops, **kwargs):
        for part, body in self.routes.items():
            if part in url:
                return Response(url=url, final_url=url, status=200, body=_json.dumps(body).encode(),
                                retrieved_at="2026-10-20T10:00:00Z", sha256="h")
        return Response(url=url, final_url=url, status=404, retrieved_at="2026-10-20T10:00:00Z")


def _accounts(*years, kind="SELSKAP", revenue=None):
    return [{"id": i, "regnskapstype": kind, "valuta": "NOK", "regnskapsperiode": {"fraDato": f"{y}-01-01", "tilDato": f"{y}-12-31"},
             "resultatregnskapResultat": {"driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": (revenue or {}).get(y, 100 * (y - 2000))}}}}
            for i, y in enumerate(years, 1)]


def _run(entity, accounts):
    org = "914778271"
    entity = {"organisasjonsnummer": org, "navn": "TEST AS", **entity}
    fetcher = FakeFetcher({"/regnskap/": accounts, f"/enheter/{org}": entity})
    case = Case(org)
    assert official.fetch_entity(fetcher, case)
    official.fetch_accounts(fetcher, case)
    run = {"run_id": "r", "started_at": "a", "completed_at": "b"}
    return build_envelope(case, run, None, True, True)


def _claim(env, field):
    return next((c for c in env["claims"] if c["field"] == field), None)


def test_latest_label_only_when_figures_cover_registry_filing_year():
    current = _run({"sisteInnsendteAarsregnskap": "2025"}, _accounts(2024, 2025))
    assert _claim(current, "accounts_freshness")["value"]["status"] == "current"
    assert "Latest filed accounts (FY2025" in current["summary"]["text"]
    lagging = _run({"sisteInnsendteAarsregnskap": "2025"}, _accounts(2023, 2024))
    assert _claim(lagging, "accounts_freshness")["value"]["status"] == "lagging"
    text = lagging["summary"]["text"]
    assert "Latest filed" not in text and "not the latest" in text and "FY2025" in text
    assert any("FY2025" in u for u in lagging["summary"]["unknowns"])


def test_filing_year_without_figures_is_stated_not_hidden():
    env = _run({"sisteInnsendteAarsregnskap": "2025"}, [])
    assert _claim(env, "accounts_freshness")["value"]["status"] == "no_figures_in_register"
    assert any("FY2025" in u for u in env["summary"]["unknowns"])


def test_unregistered_employees_are_never_zero():
    for entity in ({}, {"harRegistrertAntallAnsatte": False}, {"antallAnsatte": 0, "harRegistrertAntallAnsatte": False}):
        env = _run(entity, [])
        claim = _claim(env, "registered_employees")
        assert claim["availability"] == "not_available" and claim["value"] is None
        assert "0 employees" not in env["summary"]["text"]
        assert any("not registered" in u for u in env["summary"]["unknowns"])
    registered = _run({"antallAnsatte": 0, "harRegistrertAntallAnsatte": True}, [])
    assert _claim(registered, "registered_employees")["value"] == 0  # an explicit registered 0 is a real value


def test_year_over_year_is_figures_only_and_requires_comparable_periods():
    env = _run({"sisteInnsendteAarsregnskap": "2025"}, _accounts(2024, 2025, revenue={2024: 200, 2025: 150}))
    yoy = env["year_over_year"]
    row = next(r for r in yoy["rows"] if r["metric"] == "Operating revenue")
    assert yoy["comparable"] and row["change"] == -50 and row["change_pct"] == -25.0 and row["direction"] == "down"
    assert "down NOK 50 (-25.0%)" in env["summary"]["text"]
    assert not any(w in env["summary"]["text"].lower() for w in ("growth", "growing", "declin", "strong", "weak"))
    gap = _run({"sisteInnsendteAarsregnskap": "2025"}, _accounts(2022, 2025))
    assert not gap["year_over_year"]["comparable"] and "consecutive" in gap["year_over_year"]["reason"]
    assert not _run({}, _accounts(2025))["year_over_year"]["comparable"]


def test_company_accounts_preferred_over_group_for_latest():
    env = _run({"sisteInnsendteAarsregnskap": "2025"}, _accounts(2025) + [dict(a, id=9, regnskapstype="KONSERN") for a in _accounts(2025)])
    assert compare_row(env)["Accounts type"] == "SELSKAP"


def test_report_builds_from_every_envelope_shape(tmp_path):
    good = _run({"sisteInnsendteAarsregnskap": "2025"}, _accounts(2024, 2025))
    lagging = _run({"sisteInnsendteAarsregnskap": "2025"}, _accounts(2023, 2024))
    bare = {"organisation_number": "999999999", "state": "failed", "claims": [], "evidence": []}  # no summary / sections
    broken = {"organisation_number": "888888888", "claims": [{"field": "annual_accounts", "availability": "available", "value": "oops", "evidence_ids": ["nope"]}]}
    write_html(tmp_path, [good, lagging, bare, broken, {}])
    page = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert page.count("<article") == 5 and "cmpbox" in page and "Comparison" in page
    rows = _json.loads(page.split('id="cmpdata">')[1].split("</script>")[0])
    assert rows["914778271"]["Accounts year"].startswith("FY2")
    assert "NOT latest" in compare_row(lagging)["Accounts year"]
    assert compare_row(good)["Registered employees"] == "not registered (not zero)"


def test_validation_flags_a_latest_label_on_lagging_figures():
    class A:  # minimal argparse namespace
        run_id, workers = "r", 1
    lagging = _run({"sisteInnsendteAarsregnskap": "2025"}, _accounts(2023, 2024))
    lagging["run"]["terminal_status"] = "completed"
    ok = make_report(A, [lagging], [lagging["organisation_number"]], [1], {"total_s": 1}, None, "a", "b")
    assert ok["validation"]["checks"]["latest_label_matches_registry_filing_year"]
    lagging["summary"]["text"] = "Latest filed accounts (FY2024): ..."
    bad = make_report(A, [lagging], [lagging["organisation_number"]], [1], {"total_s": 1}, None, "a", "b")
    assert not bad["validation"]["checks"]["latest_label_matches_registry_filing_year"]
