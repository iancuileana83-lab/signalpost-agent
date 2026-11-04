"""Static, responsive profile browser: search, filter and sort, one card per company with every fact dated and linked to its
source, and a side-by-side comparison. One self-contained HTML file; works on a laptop and on a phone."""
from __future__ import annotations

import html
import json
import urllib.parse
from pathlib import Path
from typing import Any

from .web import ats_host

CSS = """
:root{color-scheme:light dark;--bg:#fafaf7;--fg:#1c1c1a;--mut:#5f5f5a;--card:#fff;--line:#d8d8d2;--ok:#1b6b3a;--warn:#8a5a00;--acc:#1f4fa3;--accfg:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#eeeeea;--mut:#a8a8a2;--card:#1f1f1c;--line:#37372f;--ok:#6fcf8f;--warn:#e0b050;--acc:#7aa5f0;--accfg:#10131a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-text-size-adjust:100%}
body.lock{overflow:hidden}
main{max-width:60rem;margin:0 auto;padding:.75rem 1rem 6rem}
h1{font-size:1.35rem;margin:.4rem 0 .6rem}h2{margin:.1rem 0;font-size:1.15rem;overflow-wrap:anywhere}
h3{font-size:.8rem;margin:1.1rem 0 .3rem;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}
a{color:inherit;overflow-wrap:anywhere}.mut{color:var(--mut)}.warn{color:var(--warn)}.num{text-align:right;font-variant-numeric:tabular-nums}
:focus-visible{outline:3px solid var(--acc);outline-offset:2px}
button,select,input[type=search]{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:8px;min-height:44px}
button{padding:.4rem 1rem;cursor:pointer}button.primary{background:var(--acc);color:var(--accfg);border-color:var(--acc);font-weight:600}
button:disabled{opacity:.5;cursor:not-allowed}
input[type=search]{width:100%;padding:.5rem .8rem}select{padding:.4rem .6rem;max-width:100%}
#bar{position:sticky;top:0;z-index:5;background:var(--bg);padding:.5rem 0 .6rem;border-bottom:1px solid var(--line)}
#bar details{margin-top:.5rem}#bar summary{cursor:pointer;min-height:44px;display:flex;align-items:center;font-weight:600}
.opts{display:flex;flex-wrap:wrap;gap:.6rem 1rem;align-items:center;padding:.2rem 0 .4rem}
.opts label{display:inline-flex;align-items:center;gap:.4rem;min-height:44px;cursor:pointer}
.opts input[type=checkbox]{width:1.2rem;height:1.2rem}
#count{margin:.5rem 0 0;font-size:.9rem;color:var(--mut)}
#empty{background:var(--card);border:1px dashed var(--line);border-radius:8px;padding:1.2rem;margin:1rem 0;text-align:center}
#empty strong{display:block;font-size:1.05rem;margin-bottom:.5rem}
article{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:1rem 0;padding:1rem}
article[hidden]{display:none}
.head{display:flex;gap:.75rem;justify-content:space-between;align-items:flex-start}
label.pick{flex:none;display:inline-flex;align-items:center;gap:.45rem;min-height:44px;padding:0 .7rem;border:1px solid var(--line);border-radius:8px;cursor:pointer;font-size:.95rem;white-space:nowrap}
label.pick input{width:1.25rem;height:1.25rem}
.badges{display:flex;flex-wrap:wrap;gap:.35rem;margin:.5rem 0 0}
.badge{font-size:.8rem;border:1px solid var(--ok);border-radius:999px;padding:.05rem .65rem;color:var(--ok);background:color-mix(in srgb,var(--ok) 10%,transparent)}
.badge.no{border:1px dashed var(--warn);color:var(--warn);background:transparent}
.src{display:inline-flex;flex-direction:column;justify-content:center;min-height:44px;padding:.2rem .8rem;margin:.1rem .25rem .1rem 0;border:1px solid var(--line);border-radius:14px;font-size:.85rem;line-height:1.25;text-decoration:none;color:var(--acc)}
.src .k{font-weight:600}.src .m{font-size:.75rem;color:var(--mut)}
.src:hover{background:color-mix(in srgb,var(--acc) 12%,transparent)}.src.none{color:var(--mut);border-style:dashed}
.sent{margin:.55rem 0}.sent p{margin:0}
table{width:100%;border-collapse:collapse;font-size:.92rem}td,th{padding:.35rem .45rem;border-top:1px solid var(--line);text-align:left;vertical-align:top}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.s-available{color:var(--ok)}.s-ambiguous,.s-blocked,.s-failed{color:var(--warn)}
.unk li{color:var(--mut)}
.feed{list-style:none;padding:0;margin:0}.feed li{display:flex;gap:.75rem;padding:.4rem 0;border-top:1px solid var(--line)}
.feed time{flex:none;min-width:6.4rem;color:var(--mut);font-variant-numeric:tabular-nums}
.kfh{margin:.7rem 0 .2rem;font-size:.8rem;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}.kfh .warn{text-transform:none;letter-spacing:0}
dl.kf{display:grid;grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr));gap:.4rem .9rem;margin:0}dl.kf dt{font-size:.75rem;color:var(--mut)}dl.kf dd{margin:0;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.actions{margin-top:.7rem}button.toggle{min-width:11rem}.more{margin-top:.4rem}.more[hidden]{display:none}
details.facts{margin-top:1rem}details.facts>summary{cursor:pointer;min-height:44px;display:flex;align-items:center;font-weight:600}
#tray{position:fixed;left:0;right:0;bottom:0;z-index:10;background:var(--card);border-top:1px solid var(--line);padding:.55rem .9rem calc(.55rem + env(safe-area-inset-bottom));display:flex;gap:.6rem;align-items:center;flex-wrap:wrap}
#tray[hidden],#cmp[hidden],#empty[hidden]{display:none}
#traytext{flex:1 1 12rem;font-size:.92rem;overflow-wrap:anywhere}
#cmp{position:fixed;inset:0;z-index:20;background:var(--bg);overflow:auto;-webkit-overflow-scrolling:touch}
#cmp .top{position:sticky;top:0;z-index:3;background:var(--bg);border-bottom:1px solid var(--line);padding:.6rem 1rem;display:flex;gap:.8rem;align-items:center;flex-wrap:wrap}
#cmp .top strong{flex:1 1 auto;font-size:1.1rem}
#cmpout{padding:.5rem 1rem 3rem}
#cmpout table{min-width:100%;width:max-content}#cmpout th,#cmpout td{min-width:9.5rem;max-width:20rem}
#cmpout td:first-child{position:sticky;left:0;background:var(--bg);min-width:7.5rem;max-width:9rem;z-index:1;font-weight:600}
#cmpout tr.diff td:not(:first-child){background:color-mix(in srgb,var(--warn) 9%,transparent)}
#cmpout .src{min-height:36px}
#cmpout .cur{display:inline-block;font-weight:700;font-size:.8em;padding:0 .4rem;margin-right:.3rem;border-radius:6px;background:color-mix(in srgb,var(--acc) 16%,transparent);color:var(--acc)}
#cmpnote{margin:.6rem 0;padding:.6rem .8rem;border:1px solid var(--warn);border-radius:8px;color:var(--warn);font-weight:600}
#cmpout a.lnk{color:var(--acc)}
@media (max-width:640px){
  main{padding:.5rem .6rem 7rem}article{padding:.8rem;border-radius:8px}
  .head{flex-direction:column;gap:.4rem}label.pick{align-self:stretch;justify-content:center}
  table.facts thead{display:none}table.facts,table.facts tbody,table.facts tr,table.facts td{display:block;width:100%}
  table.facts tr{border-top:1px solid var(--line);padding:.5rem 0}table.facts td{border:0;padding:.1rem 0}
  table.facts td::before{content:attr(data-label);display:block;font-size:.72rem;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}
  table.facts td.fld{font-weight:600}table.facts td.fld::before{display:none}
  .feed li{flex-direction:column;gap:0}
  #traytext{flex:1 1 100%}#tray button{flex:1 1 0}button.toggle{width:100%}dl.kf{grid-template-columns:1fr 1fr}
  #cmpout{padding:.4rem .5rem 3rem}#cmpout table{width:100%;font-size:.86rem}#cmpout th,#cmpout td{min-width:6.6rem;max-width:11rem;padding:.35rem .3rem}
  #cmpout td:first-child{min-width:5.2rem;max-width:5.6rem}#cmpout .src{padding:0 .5rem;font-size:.8rem}
}
@media print{#bar,#tray,#cmp,label.pick,button.toggle{display:none}.more[hidden]{display:block}}
"""

MAX_COMPARE = 4


def _num(v: Any) -> str:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return "n/a" if v is None else str(v)
    return f"{v:,.0f}".replace(",", " ")


def _value(v: Any) -> str:
    if isinstance(v, dict):
        return "; ".join(f"{k}: {x}" for k, x in v.items() if x not in (None, "", []))
    if isinstance(v, list):
        return ", ".join(map(str, v))
    return str(v)


def _host(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or url).lower()
    return host[4:] if host.startswith("www.") else host


def _source_kind(url: str) -> str:
    """What a source is, in plain words, so two pills from the same site and date can be told apart."""
    parts = urllib.parse.urlsplit(url)
    host, path = (parts.hostname or "").lower(), parts.path.lower()
    if host == "data.brreg.no":
        for needle, label in (("/regnskapsregisteret/", "annual accounts"), ("/roller", "roles"), ("/underenheter", "workplaces"),
                              ("/oppdateringer", "register updates"), ("/enhetsregisteret/api/enheter", "register entry")):
            if needle in path:
                return label
        return "register"
    if host.endswith("wikidata.org") or host.endswith("wikidata.org."):
        return "Wikidata"
    if ats_host(host):
        return "job platform"
    if any(w in path for w in ("/karriere", "/career", "/jobb", "/stilling", "/jobs")):
        return "careers page"
    if any(w in path for w in ("/nyhet", "/news", "/blogg", "/blog", "/presse", "/aktuelt")):
        return "news article"
    if path.rstrip("/").endswith(("feed", "rss.xml", "feed.xml", "sitemap.xml")):
        return "news feed"
    return "company website"


def _link(source: str, when: str = "", extra: str = "") -> str:
    """A source as a large, tappable pill: what it is, where it was read and when."""
    when = " · ".join(x for x in (when[:10], extra) if x)
    if source.startswith("http"):
        meta = html.escape(_host(source) + (f" · {when}" if when else ""))
        return (f'<a class="src" href="{html.escape(source)}" target="_blank" rel="noopener" title="{html.escape(source)}">'
                f'<span class="k">{html.escape(_source_kind(source))}</span><span class="m">{meta} <span aria-hidden="true">↗</span></span></a>')
    return f'<span class="src none"><span class="k">{html.escape(source)}</span><span class="m">{html.escape(when)}</span></span>'


def _first(env: dict, field: str, available: bool = True) -> dict | None:
    return next((c for c in env.get("claims", []) if c.get("field") == field and (c.get("availability") == "available" or not available)), None)


def _latest_accounts(env: dict) -> dict | None:
    accounts = sorted((c for c in env.get("claims", []) if c.get("field") == "annual_accounts" and c.get("availability") == "available"
                       and isinstance(c.get("value"), dict) and c["value"].get("period_to")),
                      key=lambda c: (c["value"]["period_to"], c["value"].get("accounts_type") != "KONSERN"), reverse=True)
    return accounts[0] if accounts else None


def compare_row(env: dict) -> dict[str, Any]:
    """Flat, display-ready facts for one company. Missing stays 'n/a'; nothing is converted to zero."""
    evidence = {e["id"]: e for e in env.get("evidence", [])}
    name = (_first(env, "legal_name") or {}).get("value") or env.get("organisation_number")
    latest = _latest_accounts(env)
    fresh = ((_first(env, "accounts_freshness") or {}).get("value") or {})
    emp = _first(env, "registered_employees")
    row: dict[str, Any] = {"Company": str(name), "Organisation number": env.get("organisation_number", ""), "Registry status": env.get("state", "")}
    if latest:
        v = latest["value"]
        status = fresh.get("status")
        note = {"current": "latest filing", "lagging": f"NOT latest: registry reports FY{fresh.get('registry_latest_filing_year')} filed"}.get(status, "")
        row["Accounts year"] = f"FY{v['period_to'][:4]}" + (f" ({note})" if note else "")
        row["Accounts type"] = v.get("accounts_type") or "n/a"
        for key, label in (("revenue", "Operating revenue"), ("operating_result", "Operating result"), ("annual_result", "Annual result"),
                           ("total_assets", "Total assets"), ("equity", "Equity")):
            row[label] = f"{v.get('currency') or 'NOK'} {_num(v.get(key))}" if v.get(key) is not None else "n/a"
        ev = next((evidence[i] for i in latest.get("evidence_ids", []) if i in evidence), None)
        row["Accounts source"] = ev["source_url"] if ev else ""
        row["Retrieved"] = (ev or {}).get("retrieved_at", "")[:10]
    else:
        row["Accounts year"] = "no accounts figures in the register"
    row["Registered employees"] = _num(emp["value"]) if emp else "not registered (not zero)"
    site = _first(env, "official_website")
    group = next((c for c in env.get("claims", []) if c.get("field") == "official_website" and isinstance(c.get("value"), dict)
                  and c["value"].get("site_scope") == "group_site"), None)
    row["Verified website"] = (site.get("value") or {}).get("url", "") if site else (
        f"none verified (group site {group['value'].get('candidate_url')} withheld)" if group else "none verified")
    row["Latest news"] = _latest_news_text(env)
    jobs = _job_values(env)
    row["Job postings"] = f"{len(jobs)} posting{'s' if len(jobs) != 1 else ''}" if jobs else "none found"
    return row


def compare_meta(env: dict) -> dict[str, Any]:
    """What the comparison needs besides the display text: the accounts currency, the link of the latest news article and whether
    there is a list of postings to open, so every row can be checked at its source."""
    latest = _latest_accounts(env)
    news = _news_values(env)
    url = str(news[0].get("url", "")) if news else ""
    return {"currency": ((latest or {}).get("value", {}).get("currency") or "NOK") if latest else "",
            "news_url": url if url.startswith("http") else "", "jobs": len(_job_values(env))}


def _news_values(env: dict) -> list[dict]:
    return sorted((c["value"] for c in env.get("claims", [])
                   if c.get("field") == "public_activity" and c.get("availability") == "available" and isinstance(c.get("value"), dict)
                   and c["value"].get("kind") == "company_news" and c["value"].get("date") and c["value"].get("title")),
                  key=lambda v: v["date"], reverse=True)


def _job_values(env: dict) -> list[dict]:
    return sorted((c["value"] for c in env.get("claims", [])
                   if c.get("field") == "job_posting" and c.get("availability") == "available" and isinstance(c.get("value"), dict)
                   and c["value"].get("title")), key=lambda v: v.get("date_posted") or "", reverse=True)


def _latest_news_text(env: dict) -> str:
    news = _news_values(env)
    return f"{news[0]['date']} · {news[0]['title']}" if news else "none found"


def _sort_keys(env: dict) -> dict[str, str]:
    """Numbers the viewer sorts and filters on, as data attributes. Empty string = unknown (always sorted last, never 0)."""
    latest = _latest_accounts(env)
    fresh = ((_first(env, "accounts_freshness") or {}).get("value") or {})
    emp = _first(env, "registered_employees")
    revenue = (latest or {}).get("value", {}).get("revenue")
    news, jobs = len(_news_values(env)), len(_job_values(env))
    # "Most information first": website, news, jobs and accounts each count once when present (accounts count half when they are
    # not the latest); ties are broken by how many news items and postings there are.
    info = (1 if _first(env, "official_website") else 0) + (news > 0) + (jobs > 0) + (1 if fresh.get("status") == "current" else 0.5 if latest else 0)
    return {"info": str(info), "rev": "" if revenue is None else str(revenue),
            "year": str(latest["value"]["period_to"][:4]) if latest else "",
            "emp": str(emp["value"]) if emp and isinstance(emp["value"], (int, float)) else "",
            "site": "1" if _first(env, "official_website") else "0",
            "news": str(len(_news_values(env))), "jobs": str(len(_job_values(env))),
            "latest": "1" if fresh.get("status") == "current" else "0"}


def _badges(keys: dict[str, str]) -> str:
    """Present things are green with a tick; missing things say so ('no website'), never a neutral grey that reads as 'has it'."""
    def badge(on: bool, yes: str, no: str) -> str:
        return f'<span class="badge">✓ {yes}</span>' if on else f'<span class="badge no">✕ {no}</span>'
    news, jobs = int(keys["news"]), int(keys["jobs"])
    if keys["latest"] == "1":
        accounts = badge(True, "latest accounts", "")
    elif keys["year"]:
        accounts = f'<span class="badge no">✕ accounts not the latest (FY{html.escape(keys["year"])})</span>'
    else:
        accounts = badge(False, "", "no accounts")
    return "".join((badge(keys["site"] == "1", "website", "no website"), badge(news > 0, f"{news} news", "no news"),
                    badge(jobs > 0, f"{jobs} job{'s' if jobs != 1 else ''}", "no jobs"), accounts))


def _key_figures(row: dict) -> str:
    """The few numbers worth scanning, labelled with the accounts year they belong to."""
    year = str(row.get("Accounts year", ""))
    if not year.startswith("FY"):
        head, cells = f'Key figures: <span class="warn">{html.escape(year)}</span>', [("Registered employees", row.get("Registered employees", "n/a"))]
    else:
        flag = f' <span class="warn">({html.escape(year.split("(", 1)[1].rstrip(")"))})</span>' if "NOT latest" in year else ""
        head = f'Key figures, {html.escape(year.split(" (")[0])}{flag}'
        cells = [("Operating revenue", row.get("Operating revenue", "n/a")), ("Annual result", row.get("Annual result", "n/a")),
                 ("Registered employees", row.get("Registered employees", "n/a"))]
    return (f'<div class="kfh">{head}</div><dl class="kf">'
            + "".join(f"<div><dt>{html.escape(k)}</dt><dd>{html.escape(str(v))}</dd></div>" for k, v in cells) + "</dl>")


def _yoy_table(env: dict) -> str:
    yoy = env.get("year_over_year") or {}
    if not yoy:
        return ""
    if not yoy.get("comparable"):
        return f'<p class="mut">Year-over-year comparison not shown: {html.escape(str(yoy.get("reason") or "not available"))}.</p>'
    cur = html.escape(str(yoy.get("current_period", "")))
    prev = html.escape(str(yoy.get("previous_period", "")))
    rows = "".join(
        f'<tr><td>{html.escape(r["metric"])}</td><td class="num">{_num(r["previous"])}</td><td class="num">{_num(r["current"])}</td>'
        f'<td class="num">{_num(r["change"]) if r["change"] is not None else "n/a"}'
        f'{" (%+.1f%%)" % r["change_pct"] if r.get("change_pct") is not None else ""}</td></tr>' for r in yoy.get("rows", []))
    return (f'<h3>Year over year (figures only, {html.escape(str(yoy.get("accounts_type") or ""))} accounts, {html.escape(str(yoy.get("currency") or ""))})</h3>'
            f'<div class="scroll"><table><tr><th>Metric</th><th class="num">{prev}</th><th class="num">{cur}</th><th class="num">Change</th></tr>{rows}</table></div>')


def _news_list(env: dict) -> str:
    """Dated news articles only: the date sits next to a headline that links to the article."""
    items = _news_values(env)
    if not items:
        return ""
    rows = "".join(f'<li><time datetime="{html.escape(v["date"])}">{html.escape(v["date"])}</time> '
                   + (f'<a href="{html.escape(v["url"])}" target="_blank" rel="noopener">{html.escape(v["title"])}</a>' if str(v.get("url", "")).startswith("http")
                      else html.escape(v["title"])) + "</li>" for v in items)
    return f"<h3>Company news</h3><ul class=feed>{rows}</ul>"


def _jobs_list(env: dict) -> str:
    items = _job_values(env)
    if not items:
        return ""
    rows = "".join(f'<li><time>{html.escape(v["date_posted"]) if v.get("date_posted") else "date not shown"}</time> '
                   + (f'<a href="{html.escape(v["url"])}" target="_blank" rel="noopener">{html.escape(v["title"])}</a>' if str(v.get("url", "")).startswith("http")
                      else html.escape(v["title"])) + "</li>" for v in items)
    return f"<h3 class=\"jobs-h\">Job postings</h3><ul class=feed>{rows}</ul>"


def _claim_date(c: dict, evidence: dict, fallback: str) -> tuple[str, str]:
    """(label, date) for any fact: its own date, its reporting period, else when the source was read. Never empty."""
    if c.get("effective_date"):
        return "dated", str(c["effective_date"])[:10]
    if c.get("reporting_period"):
        return "period", str(c["reporting_period"])
    reads = [evidence[i].get("retrieved_at", "") for i in c.get("evidence_ids", []) if i in evidence]
    if any(reads):
        return "read", max(reads)[:10]
    return "checked", fallback


def _card(env: dict) -> str:
    evidence = {e["id"]: e for e in env.get("evidence", [])}
    row = compare_row(env)
    name, org = row["Company"], str(env.get("organisation_number", ""))
    checked_on = str((env.get("run") or {}).get("completed_at", ""))[:10]
    keys = _sort_keys(env)
    table = []
    for c in env.get("claims", []):
        links = " ".join(_link(evidence[i]["source_url"], evidence[i].get("retrieved_at", "")) for i in c.get("evidence_ids", [])
                         if i in evidence and evidence[i].get("source_url"))
        value = html.escape(_value(c["value"])) if c.get("value") is not None else f'<span class="mut">{html.escape(c.get("note") or "")}</span>'
        state = c.get("availability", "")
        label, when = _claim_date(c, evidence, checked_on)
        table.append(f'<tr><td class="fld" data-label="Field">{html.escape(str(c.get("field", "")))}</td><td data-label="Value">{value}</td>'
                     f'<td data-label="Date"><span class="mut">{label}</span> {html.escape(when)}</td>'
                     f'<td data-label="State" class="s-{html.escape(state)}">{html.escape(state)}{" (stale)" if c.get("stale") else ""}</td>'
                     f'<td data-label="Source">{links or "<span class=mut>no source (nothing to cite)</span>"}</td></tr>')
    summary = env.get("summary") or {}
    sentences = []
    for s in summary.get("sentences", []):
        src = " ".join(_link(x.get("source", ""), x.get("retrieved_at", "")) for x in s.get("sources", []))
        sentences.append(f'<div class="sent"><p>{html.escape(s.get("text", ""))}</p><div>{src}</div></div>')
    unknowns = "".join(f"<li>{html.escape(u)}</li>" for u in summary.get("unknowns", []))
    attrs = "".join(f' data-{k}="{html.escape(v)}"' for k, v in keys.items())
    more_id = "more-" + "".join(ch for ch in org if ch.isalnum())
    return (f'<article data-q="{html.escape((str(name) + " " + org).lower())}" data-org="{html.escape(org)}" data-name="{html.escape(str(name).lower())}"'
            f' data-display="{html.escape(str(name))}"{attrs}>'
            f'<div class="head"><div><h2>{html.escape(str(name))}</h2><div class="mut">Organisation number {html.escape(org)} · state {html.escape(str(env.get("state", "")))}</div>'
            f'<div class="badges">{_badges(keys)}</div></div>'
            f'<label class="pick"><input type="checkbox" class="cmpbox" value="{html.escape(org)}" aria-label="Compare {html.escape(str(name))}"> Compare</label></div>'
            f'{_key_figures(row)}'
            f'<div class="actions"><button type="button" class="toggle" aria-expanded="false" aria-controls="{more_id}">Show details</button></div>'
            f'<div class="more" id="{more_id}" hidden>{"".join(sentences)}'
            f'{"<h3>What we could not establish</h3><ul class=unk>" + unknowns + "</ul>" if unknowns else ""}'
            f'{_yoy_table(env)}{_news_list(env)}{_jobs_list(env)}'
            f'<details class="facts"><summary>All facts, dates and sources ({len(table)})</summary><div class="scroll"><table class="facts">'
            f'<thead><tr><th>Field</th><th>Value</th><th>Date</th><th>State</th><th>Source</th></tr></thead><tbody>'
            + "".join(table) + "</tbody></table></div></details></div></article>")


def _error_card(env: Any, exc: Exception) -> str:
    org = html.escape(str(env.get("organisation_number", "?")) if isinstance(env, dict) else "?")
    return (f'<article data-q="{org}" data-name="{org}"><h2>{org}</h2><p class="warn">This profile could not be displayed '
            f'({html.escape(type(exc).__name__)}). The envelope is still present in the JSONL output.</p></article>')


SCRIPT = """
const rows = JSON.parse(document.getElementById('cmpdata').textContent), MAX = __MAX__;
const $ = id => document.getElementById(id);
const list = $('list'), cards = [...list.querySelectorAll('article')];
const q = $('q'), sortSel = $('sort'), count = $('count'), empty = $('empty'), flt = [...document.querySelectorAll('.flt')];
const nm = a => a.dataset.name || '';
const byName = (a, b) => nm(a).localeCompare(nm(b), 'nb');
const by = (key, dir) => (a, b) => {
  const x = a.dataset[key] === '' || a.dataset[key] === undefined ? null : Number(a.dataset[key]);
  const y = b.dataset[key] === '' || b.dataset[key] === undefined ? null : Number(b.dataset[key]);
  if (x === null && y === null) return byName(a, b);
  if (x === null) return 1;            // unknown is never treated as 0: it goes last in either direction
  if (y === null) return -1;
  return (x - y) * dir || byName(a, b);
};
const num = (a, k) => Number(a.dataset[k]) || 0;
const byInfo = (a, b) => (num(b, 'info') - num(a, 'info')) || (num(b, 'news') + num(b, 'jobs') - num(a, 'news') - num(a, 'jobs')) || byName(a, b);
const SORTS = {info: byInfo, name: byName, 'rev-desc': by('rev', -1), 'rev-asc': by('rev', 1), year: by('year', -1), emp: by('emp', -1)};
function apply() {
  const term = q.value.trim().toLowerCase(), on = flt.filter(f => f.checked).map(f => f.dataset.key);
  let shown = 0;
  cards.forEach(a => {
    const ok = (!term || (a.dataset.q || '').includes(term)) && on.every(k => Number(a.dataset[k]) > 0);
    a.hidden = !ok; if (ok) shown++;
  });
  cards.slice().sort(SORTS[sortSel.value]).forEach(a => list.appendChild(a));
  count.textContent = shown === cards.length ? cards.length + ' companies' : 'Showing ' + shown + ' of ' + cards.length + ' companies';
  empty.hidden = shown > 0 || cards.length === 0;
}
function reset() { q.value = ''; sortSel.value = 'info'; flt.forEach(f => f.checked = false); apply(); q.focus(); }
q.addEventListener('input', apply); sortSel.addEventListener('change', apply); flt.forEach(f => f.addEventListener('change', apply));
$('reset').addEventListener('click', reset); $('emptyreset').addEventListener('click', reset);
const opts = document.querySelector('#bar details'); if (opts && window.matchMedia('(min-width:700px)').matches) opts.open = true;

const tray = $('tray'), traytext = $('traytext'), open_ = $('cmpopen'), panel = $('cmp'), out = $('cmpout'), diff = $('diffonly');
const picked = () => [...document.querySelectorAll('.cmpbox:checked')].map(b => b.value);
const label = o => { const a = cards.find(c => c.dataset.org === o); return a ? a.dataset.display : o; };
function syncTray() {
  const p = picked(); tray.hidden = p.length === 0;
  traytext.textContent = p.length < 2 ? (p.length ? label(p[0]) + ' · tick one more company to compare' : '') : p.map(label).join(' vs ');
  open_.disabled = p.length < 2;
}
const MONEY = ['Operating revenue', 'Operating result', 'Annual result', 'Total assets', 'Equity'];
function link(td, text, href) {
  const a = document.createElement('a'); a.className = 'lnk'; a.href = href; a.target = '_blank'; a.rel = 'noopener';
  a.textContent = text + ' \u2197'; td.appendChild(a);
}
function renderCompare() {
  const sel = picked().map(o => rows[o]).filter(Boolean), keys = [];
  sel.forEach(r => Object.keys(r).forEach(k => { if (k !== '_meta' && !keys.includes(k)) keys.push(k); }));
  const cur = [...new Set(sel.map(r => (r._meta || {}).currency).filter(Boolean))];
  const t = document.createElement('table');
  keys.forEach(k => {
    const vals = sel.map(r => r[k] === undefined ? 'n/a' : String(r[k]));
    const differs = new Set(vals).size > 1;
    if (diff.checked && !differs && k !== 'Company') return;
    const tr = t.insertRow(); if (differs && k !== 'Company') tr.className = 'diff';
    const th = tr.insertCell(); th.textContent = k;
    vals.forEach((v, i) => {
      const td = tr.insertCell(), m = (sel[i]._meta || {}), org = picked()[i];
      const money = MONEY.includes(k) && /^[A-Z]{3} /.test(v);
      if (/^https?:\/\//.test(v)) {
        const a = document.createElement('a'); a.className = 'src'; a.href = v; a.target = '_blank'; a.rel = 'noopener';
        a.textContent = new URL(v).hostname.replace(/^www\./, '') + ' \u2197'; td.appendChild(a);
      } else if (money) {
        const c = document.createElement('span'); c.className = 'cur'; c.textContent = v.slice(0, 3); td.append(c, v.slice(4));
      } else if (k === 'Latest news' && m.news_url) {
        link(td, v, m.news_url);
      } else if (k === 'Job postings' && m.jobs) {
        const a = document.createElement('a'); a.className = 'lnk'; a.href = '#'; a.textContent = v + ' \u2013 show the list';
        a.addEventListener('click', e => { e.preventDefault(); showJobs(org); }); td.appendChild(a);
      } else td.textContent = v;
    });
  });
  const wrap = document.createElement('div'); wrap.className = 'scroll'; wrap.appendChild(t);
  const parts = [];
  if (cur.length > 1) {
    const n = document.createElement('div'); n.id = 'cmpnote'; n.setAttribute('role', 'note');
    n.textContent = 'Figures are in different currencies (' + cur.join(' vs ') + ') and are not directly comparable.'; parts.push(n);
  }
  out.replaceChildren(...parts, wrap);
}
function showJobs(org) {
  closeCompare();
  const card = cards.find(c => c.dataset.org === org); if (!card) return;
  card.hidden = false;
  const btn = card.querySelector('.toggle'), more = document.getElementById(btn.getAttribute('aria-controls'));
  if (more.hidden) btn.click();
  (card.querySelector('.jobs-h') || card).scrollIntoView({block: 'start'});
}
function openCompare() { renderCompare(); panel.hidden = false; document.body.classList.add('lock'); $('cmpclose').focus(); }
function closeCompare() { panel.hidden = true; document.body.classList.remove('lock'); open_.focus(); }
document.addEventListener('change', e => {
  if (!e.target.classList.contains('cmpbox')) return;
  if (picked().length > MAX) { e.target.checked = false; traytext.textContent = 'At most ' + MAX + ' companies can be compared.'; return; }
  syncTray();
});
open_.addEventListener('click', openCompare); $('cmpclose').addEventListener('click', closeCompare);
diff.addEventListener('change', renderCompare);
$('cmpclear').addEventListener('click', () => { document.querySelectorAll('.cmpbox').forEach(b => b.checked = false); syncTray(); if (!panel.hidden) closeCompare(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !panel.hidden) closeCompare(); });
document.addEventListener('click', e => {
  const b = e.target.closest('.toggle'); if (!b) return;
  const more = document.getElementById(b.getAttribute('aria-controls')), open = more.hidden;
  more.hidden = !open; b.setAttribute('aria-expanded', String(open)); b.textContent = open ? 'Hide details' : 'Show details';
});
apply(); syncTray();
""".replace("__MAX__", str(MAX_COMPARE))


def write_html(directory: Path, envelopes: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    cards, data = [], {}
    for env in envelopes:
        try:
            cards.append(_card(env))
            data[str(env.get("organisation_number", ""))] = {**compare_row(env), "_meta": compare_meta(env)}
        except Exception as exc:  # noqa: BLE001 - one odd envelope must not take down the whole report
            cards.append(_error_card(env, exc))
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    controls = (
        '<div id="bar"><input id="q" type="search" placeholder="Search name or org. number" aria-label="Find a company" autocomplete="off">'
        '<details><summary>Sort and filter</summary><div class="opts">'
        '<label>Sort by <select id="sort" aria-label="Sort by"><option value="info">Most information first</option><option value="name">Name A–Z</option>'
        '<option value="rev-desc">Revenue, high to low</option><option value="rev-asc">Revenue, low to high</option>'
        '<option value="year">Accounts year, newest first</option><option value="emp">Employees, most first</option></select></label>'
        '<label><input type="checkbox" class="flt" data-key="site"> Has website</label>'
        '<label><input type="checkbox" class="flt" data-key="news"> Has news</label>'
        '<label><input type="checkbox" class="flt" data-key="jobs"> Has job postings</label>'
        '<label><input type="checkbox" class="flt" data-key="latest"> Latest accounts filed</label>'
        '<button type="button" id="reset">Reset</button></div></details>'
        '<div id="count" role="status" aria-live="polite"></div></div>')
    empty = ('<div id="empty" role="status" hidden><strong>No companies match your search.</strong>'
             '<span class="mut">Check the spelling, try a shorter name or an organisation number, or remove a filter.</span><br><br>'
             '<button type="button" id="emptyreset" class="primary">Clear search and filters</button></div>')
    tray = ('<div id="tray" hidden><span id="traytext"></span><button type="button" id="cmpopen" class="primary" disabled>Compare</button>'
            '<button type="button" id="cmpclear">Clear</button></div>')
    panel = ('<div id="cmp" role="dialog" aria-modal="true" aria-label="Comparison" hidden><div class="top"><strong>Comparison</strong>'
             '<label class="opts"><input type="checkbox" id="diffonly"> Differences only</label>'
             '<button type="button" id="cmpclose" class="primary">Close</button></div><div id="cmpout"></div></div>')
    page = (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Signalpost profiles</title><style>{CSS}</style><main><h1>Company profiles</h1>{controls}{empty}'
            f'<div id="list">' + "".join(cards) + f'</div></main>{tray}{panel}'
            f'<script type="application/json" id="cmpdata">{payload}</script><script>{SCRIPT}</script></html>')
    (directory / "index.html").write_text(page, encoding="utf-8")
