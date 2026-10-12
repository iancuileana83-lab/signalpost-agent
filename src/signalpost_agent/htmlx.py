"""Small dependency-free HTML extraction: title, meta, links, JSON-LD, <time>, visible text."""
from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser

ORG_RE = re.compile(r"(?<!\d)(\d{3})[ . ]?(\d{3})[ . ]?(\d{3})(?!\d)")


def find_org_numbers(text: str) -> set[str]:
    return {"".join(m.groups()) for m in ORG_RE.finditer(text)}


@dataclass
class Link:
    href: str
    text: str
    rel: str = ""
    index: int = 0


@dataclass
class TimeTag:
    datetime: str
    index: int


@dataclass
class Page:
    url: str
    title: str = ""
    meta: dict[str, str] = field(default_factory=dict)
    links: list[Link] = field(default_factory=list)
    alternates: list[tuple[str, str, str]] = field(default_factory=list)  # (type, href, title)
    jsonld: list[dict] = field(default_factory=list)
    times: list[TimeTag] = field(default_factory=list)
    text: str = ""
    iframes: list[str] = field(default_factory=list)

    def abs(self, href: str) -> str:
        return urllib.parse.urljoin(self.url, href)


class _Extractor(HTMLParser):
    def __init__(self, url: str):
        super().__init__(convert_charrefs=True)
        self.page = Page(url)
        self._in_title = False
        self._skip = 0
        self._ld = False
        self._ld_buf: list[str] = []
        self._anchor: Link | None = None
        self._text: list[str] = []
        self._i = 0

    def handle_starttag(self, tag, attrs):
        self._i += 1
        a = {k: (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and "content" in a and key not in self.page.meta:
                self.page.meta[key] = a["content"].strip()
        elif tag == "link":
            rel = a.get("rel", "").lower()
            if "alternate" in rel and a.get("href") and a.get("type", "").lower() in (
                    "application/rss+xml", "application/atom+xml", "application/feed+json"):
                self.page.alternates.append((a["type"].lower(), a["href"], a.get("title", "")))
        elif tag == "a" and a.get("href"):
            self._anchor = Link(a["href"].strip(), "", a.get("rel", ""), self._i)
            self.page.links.append(self._anchor)
        elif tag == "time" and a.get("datetime"):
            self.page.times.append(TimeTag(a["datetime"].strip(), self._i))
        elif tag == "iframe" and a.get("src"):
            self.page.iframes.append(a["src"])
        elif tag == "script":
            if "ld+json" in a.get("type", "").lower():
                self._ld = True
                self._ld_buf = []
            else:
                self._skip += 1
        elif tag in ("style", "noscript"):
            self._skip += 1
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "section", "footer", "header"):
            self._text.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "a":
            self._anchor = None
        elif tag == "script":
            if self._ld:
                self._ld = False
                raw = "".join(self._ld_buf).strip()
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = None
                for item in _flatten_ld(data):
                    self.page.jsonld.append(item)
            elif self._skip:
                self._skip -= 1
        elif tag in ("style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._ld:
            self._ld_buf.append(data)
            return
        if self._skip:
            return
        if self._in_title:
            self.page.title += data
        if self._anchor is not None:
            self._anchor.text += data
        self._text.append(data)


def _flatten_ld(data) -> list[dict]:
    out: list[dict] = []
    if isinstance(data, list):
        for item in data:
            out.extend(_flatten_ld(item))
    elif isinstance(data, dict):
        out.append(data)
        graph = data.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                out.extend(_flatten_ld(item))
    return out


def parse_html(html: str, url: str) -> Page:
    extractor = _Extractor(url)
    try:
        extractor.feed(html)
        extractor.close()
    except Exception:  # noqa: BLE001 - malformed HTML must never abort a company
        pass
    page = extractor.page
    page.title = re.sub(r"\s+", " ", page.title).strip()
    for link in page.links:
        link.text = re.sub(r"\s+", " ", link.text).strip()
    page.text = re.sub(r"[ \t ]+", " ", "".join(extractor._text))
    page.text = re.sub(r"\n\s*\n+", "\n", page.text).strip()
    return page


def ld_types(item: dict) -> set[str]:
    t = item.get("@type")
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        return {x for x in t if isinstance(x, str)}
    return set()
