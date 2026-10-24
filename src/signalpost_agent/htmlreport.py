"""Static, responsive profile browser: one card per company, every fact linked to its source, and a side-by-side comparison."""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

CSS = """
:root{color-scheme:light dark;--bg:#fafaf7;--fg:#1c1c1a;--mut:#666;--card:#fff;--line:#ddd;--ok:#1b6b3a;--warn:#8a5a00}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#eeeeea;--mut:#a0a09a;--card:#1f1f1c;--line:#34342f;--ok:#6fcf8f;--warn:#e0b050}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:56rem;margin:0 auto;padding:1rem}
input[type=search]{width:100%;box-sizing:border-box;padding:.6rem;font:inherit;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
article{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:1rem 0;padding:1rem}
h2{margin:.1rem 0;font-size:1.15rem}h3{font-size:.95rem;margin:1rem 0 .3rem;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.mut{color:var(--mut)}table{width:100%;border-collapse:collapse;font-size:.92rem}td,th{padding:.3rem .4rem;border-top:1px solid var(--line);text-align:left;vertical-align:top}
.s-available{color:var(--ok)}.s-ambiguous,.s-blocked,.s-failed{color:var(--warn)}a{color:inherit;overflow-wrap:anywhere}
.unk li{color:var(--mut)}.warn{color:var(--warn)}.num{text-align:right;font-variant-numeric:tabular-nums}
#cmp{position:sticky;top:0;z-index:2;background:var(--bg);padding:.5rem 0;border-bottom:1px solid var(--line)}
#cmp[hidden]{display:none}#cmp .scroll{overflow-x:auto}label.pick{font-size:.9rem;color:var(--mut);cursor:pointer;float:right}
button{font:inherit;padding:.3rem .7rem;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);cursor:pointer}
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


def _link(source: str, when: str = "", extra: str = "") -> str:
    label = f'<span class="mut">{html.escape(source)}</span>'
    if source.startswith("http"):
        label = f'<a href="{html.escape(source)}" rel="noopener">source</a>'
    tail = html.escape(" · ".join(x for x in (when[:10], extra) if x))
    return f'{label} <span class="mut">({tail})</span>' if tail else label


def _first(env: dict, field: str, available: bool = True) -> dict | None:
    return next((c for c in env.get("claims", []) if c.get("field") == field and (c.get("availability") == "available" or not available)), None)


def compare_row(env: dict) -> dict[str, Any]:
    """Flat, display-ready facts for one company. Missing stays 'n/a'; nothing is converted to zero."""
    evidence = {e["id"]: e for e in env.get("evidence", [])}
    name = (_first(env, "legal_name") or {}).get("value") or env.get("organisation_number")
    accounts = sorted((c for c in env.get("claims", []) if c.get("field") == "annual_accounts" and c.get("availability") == "available"
                       and isinstance(c.get("value"), dict) and c["value"].get("period_to")),
                      key=lambda c: (c["value"]["period_to"], c["value"].get("accounts_type") != "KONSERN"), reverse=True)
    fresh = ((_first(env, "accounts_freshness") or {}).get("value") or {})
    emp = _first(env, "registered_employees")
    row: dict[str, Any] = {"Company": str(name), "Organisation number": env.get("organisation_number", ""), "Registry status": env.get("state", "")}
    if accounts:
        v = accounts[0]["value"]
        status = fresh.get("status")
        note = {"current": "latest filing", "lagging": f"NOT latest: registry reports FY{fresh.get('registry_latest_filing_year')} filed"}.get(status, "")
        row["Accounts year"] = f"FY{v['period_to'][:4]}" + (f" ({note})" if note else "")
        row["Accounts type"] = v.get("accounts_type") or "n/a"
        for key, label in (("revenue", "Operating revenue"), ("operating_result", "Operating result"), ("annual_result", "Annual result"),
                           ("total_assets", "Total assets"), ("equity", "Equity")):
            row[label] = f"{v.get('currency') or 'NOK'} {_num(v.get(key))}" if v.get(key) is not None else "n/a"
        ev = next((evidence[i] for i in accounts[0].get("evidence_ids", []) if i in evidence), None)
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
    return row


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
            f'<div style="overflow-x:auto"><table><tr><th>Metric</th><th class="num">{prev}</th><th class="num">{cur}</th><th class="num">Change</th></tr>{rows}</table></div>')


def _card(env: dict) -> str:
    evidence = {e["id"]: e for e in env.get("evidence", [])}
    row = compare_row(env)
    name, org = row["Company"], str(env.get("organisation_number", ""))
    table = []
    for c in env.get("claims", []):
        links = " ".join(
            _link(evidence[i]["source_url"], evidence[i].get("retrieved_at", ""),
                  ("period " + evidence[i]["reporting_period"]) if evidence[i].get("reporting_period") else "")
            for i in c.get("evidence_ids", []) if i in evidence and evidence[i].get("source_url"))
        value = html.escape(_value(c["value"])) if c.get("value") is not None else f'<span class="mut">{html.escape(c.get("note") or "")}</span>'
        state = c.get("availability", "")
        table.append(f'<tr><td>{html.escape(str(c.get("field", "")))}</td><td>{value}</td>'
                     f'<td class="s-{html.escape(state)}">{html.escape(state)}{" (stale)" if c.get("stale") else ""}</td><td>{links}</td></tr>')
    summary = env.get("summary") or {}
    sentences = []
    for s in summary.get("sentences", []):
        src = " ".join(_link(x.get("source", ""), x.get("retrieved_at", "")) for x in s.get("sources", []))
        sentences.append(f'<p>{html.escape(s.get("text", ""))} {src}</p>')
    unknowns = "".join(f"<li>{html.escape(u)}</li>" for u in summary.get("unknowns", []))
    return (f'<article data-q="{html.escape((str(name) + " " + org).lower())}" data-org="{html.escape(org)}">'
            f'<label class="pick"><input type="checkbox" class="cmpbox" value="{html.escape(org)}"> Compare</label>'
            f'<h2>{html.escape(str(name))}</h2><div class="mut">Organisation number {html.escape(org)} · state {html.escape(str(env.get("state", "")))}</div>'
            f'{"".join(sentences)}'
            f'{"<h3>What we could not establish</h3><ul class=unk>" + unknowns + "</ul>" if unknowns else ""}'
            f'{_yoy_table(env)}'
            f'<h3>Facts and evidence</h3><div style="overflow-x:auto"><table><tr><th>Field</th><th>Value</th><th>State</th><th>Evidence</th></tr>'
            + "".join(table) + "</table></div></article>")


def _error_card(env: Any, exc: Exception) -> str:
    org = html.escape(str(env.get("organisation_number", "?")) if isinstance(env, dict) else "?")
    return (f'<article data-q="{org}"><h2>{org}</h2><p class="warn">This profile could not be displayed '
            f'({html.escape(type(exc).__name__)}). The envelope is still present in the JSONL output.</p></article>')


SCRIPT = """
const rows = JSON.parse(document.getElementById('cmpdata').textContent), MAX = %d;
const panel = document.getElementById('cmp'), out = document.getElementById('cmpout'), note = document.getElementById('cmpnote');
function render() {
  const picked = [...document.querySelectorAll('.cmpbox:checked')].map(b => b.value);
  panel.hidden = picked.length < 1;
  out.replaceChildren();
  if (!picked.length) return;
  note.textContent = picked.length < 2 ? 'Tick at least one more company to compare.' : '';
  const sel = picked.map(o => rows[o]).filter(Boolean);
  const keys = [];
  sel.forEach(r => Object.keys(r).forEach(k => { if (!keys.includes(k)) keys.push(k); }));
  const t = document.createElement('table');
  keys.forEach(k => {
    const tr = t.insertRow(), th = document.createElement('th');
    th.textContent = k; tr.appendChild(th);
    sel.forEach(r => {
      const td = tr.insertCell(), v = r[k] === undefined ? 'n/a' : String(r[k]);
      if (/^https?:\\/\\//.test(v)) { const a = document.createElement('a'); a.href = v; a.rel = 'noopener'; a.textContent = k === 'Accounts source' ? 'source' : v; td.appendChild(a); }
      else td.textContent = v;
    });
  });
  out.appendChild(t);
}
document.addEventListener('change', e => {
  if (!e.target.classList.contains('cmpbox')) return;
  if (document.querySelectorAll('.cmpbox:checked').length > MAX) { e.target.checked = false; note.textContent = 'At most ' + MAX + ' companies can be compared.'; return; }
  render();
});
document.getElementById('cmpclear').addEventListener('click', () => { document.querySelectorAll('.cmpbox').forEach(b => b.checked = false); render(); });
document.getElementById('q').addEventListener('input', e => {
  const v = e.target.value.toLowerCase();
  document.querySelectorAll('article').forEach(a => a.hidden = !(a.dataset.q || '').includes(v));
});
""" % MAX_COMPARE


def write_html(directory: Path, envelopes: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    cards, data = [], {}
    for env in envelopes:
        try:
            cards.append(_card(env))
            data[str(env.get("organisation_number", ""))] = compare_row(env)
        except Exception as exc:  # noqa: BLE001 - one odd envelope must not take down the whole report
            cards.append(_error_card(env, exc))
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    page = (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Signalpost profiles</title><style>{CSS}</style><main><h1>Company profiles</h1>'
            f'<input id="q" type="search" placeholder="Filter by name or organisation number" aria-label="Filter">'
            f'<section id="cmp" hidden><strong>Comparison</strong> <button id="cmpclear" type="button">Clear</button> '
            f'<span id="cmpnote" class="mut"></span><div class="scroll" id="cmpout"></div></section>'
            + "".join(cards) +
            f'</main><script type="application/json" id="cmpdata">{payload}</script><script>{SCRIPT}</script></html>')
    (directory / "index.html").write_text(page, encoding="utf-8")
