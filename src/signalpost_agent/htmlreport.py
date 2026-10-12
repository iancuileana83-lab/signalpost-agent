"""Static, responsive profile browser: one page with a card per company, every fact linked to its source."""
from __future__ import annotations

import html
from pathlib import Path
from typing import Any

CSS = """
:root{color-scheme:light dark;--bg:#fafaf7;--fg:#1c1c1a;--mut:#666;--card:#fff;--line:#ddd;--ok:#1b6b3a;--warn:#8a5a00}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#eeeeea;--mut:#a0a09a;--card:#1f1f1c;--line:#34342f;--ok:#6fcf8f;--warn:#e0b050}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:56rem;margin:0 auto;padding:1rem}
input{width:100%;box-sizing:border-box;padding:.6rem;font:inherit;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
article{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:1rem 0;padding:1rem}
h2{margin:.1rem 0;font-size:1.15rem}h3{font-size:.95rem;margin:1rem 0 .3rem;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.mut{color:var(--mut)}table{width:100%;border-collapse:collapse;font-size:.92rem}td,th{padding:.3rem .4rem;border-top:1px solid var(--line);text-align:left;vertical-align:top}
.s-available{color:var(--ok)}.s-ambiguous,.s-blocked,.s-failed{color:var(--warn)}a{color:inherit;overflow-wrap:anywhere}
.unk li{color:var(--mut)}
"""


def _value(v: Any) -> str:
    if isinstance(v, dict):
        return "; ".join(f"{k}: {x}" for k, x in v.items() if x not in (None, "", []))
    if isinstance(v, list):
        return ", ".join(map(str, v))
    return str(v)


def write_html(directory: Path, envelopes: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    cards = []
    for env in envelopes:
        evidence = {e["id"]: e for e in env["evidence"]}
        name = next((c["value"] for c in env["claims"] if c["field"] == "legal_name" and c["availability"] == "available"), env["organisation_number"])
        rows = []
        for c in env["claims"]:
            links = " ".join(
                f'<a href="{html.escape(evidence[i]["source_url"])}" rel="noopener">source</a> '
                f'<span class="mut">({html.escape(evidence[i]["retrieved_at"][:10])}'
                f'{", period " + html.escape(evidence[i]["reporting_period"]) if evidence[i].get("reporting_period") else ""})</span>'
                for i in c["evidence_ids"] if i in evidence and evidence[i]["source_url"].startswith("http"))
            value = html.escape(_value(c["value"])) if c["value"] is not None else f'<span class="mut">{html.escape(c.get("note") or "")}</span>'
            rows.append(f'<tr><td>{html.escape(c["field"])}</td><td>{value}</td>'
                        f'<td class="s-{c["availability"]}">{c["availability"]}{" (stale)" if c.get("stale") else ""}</td><td>{links}</td></tr>')
        unknowns = "".join(f"<li>{html.escape(u)}</li>" for u in env.get("summary", {}).get("unknowns", []))
        cards.append(
            f'<article data-q="{html.escape((str(name) + " " + env["organisation_number"]).lower())}">'
            f'<h2>{html.escape(str(name))}</h2><div class="mut">Organisation number {env["organisation_number"]} · state {env["state"]}</div>'
            f'<p>{html.escape(env.get("summary", {}).get("text", ""))}</p>'
            f'{"<h3>What we could not establish</h3><ul class=unk>" + unknowns + "</ul>" if unknowns else ""}'
            f'<h3>Facts and evidence</h3><div style="overflow-x:auto"><table><tr><th>Field</th><th>Value</th><th>State</th><th>Evidence</th></tr>'
            + "".join(rows) + "</table></div></article>")
    page = (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Signalpost profiles</title><style>{CSS}</style><main><h1>Company profiles</h1>'
            f'<input id="q" type="search" placeholder="Filter by name or organisation number" aria-label="Filter">'
            + "".join(cards) +
            '</main><script>document.getElementById("q").addEventListener("input",e=>{const v=e.target.value.toLowerCase();'
            'document.querySelectorAll("article").forEach(a=>a.hidden=!a.dataset.q.includes(v))})</script></html>')
    (directory / "index.html").write_text(page, encoding="utf-8")
