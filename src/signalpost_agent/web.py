"""Official-website discovery with exact-entity verification, plus company-owned extraction.

Identity gate: a website is published only when (a) the registry itself declares it and the
page is reachable, or (b) the exact organisation number appears in the page text/JSON-LD of the
site (homepage or a legal/contact page). Name similarity alone only produces `ambiguous`.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from .htmlx import Page, find_org_numbers, ld_types, parse_html
from .http import Fetcher, Response
from .model import Case

LEGAL_SUFFIXES = {"as", "asa", "ans", "da", "sa", "ks", "nuf", "enk", "iks", "sf", "ba", "stiftelse", "foretak"}
FREE_MAIL = {"gmail.com", "hotmail.com", "hotmail.no", "outlook.com", "outlook.no", "live.com", "live.no", "yahoo.com", "yahoo.no",
             "icloud.com", "me.com", "online.no", "broadpark.no", "getmail.no", "start.no", "c2i.net", "frisurf.no", "powertech.no",
             "lyse.net", "altibox.no", "telenor.com", "telenormail.no", "msn.com", "protonmail.com", "proton.me", "gmx.com", "hotmail.se",
             "bbnett.no", "tiscali.no", "chello.no", "combitel.no", "mac.com", "aol.com", "mail.com", "t-online.de", "web.de", "ntlworld.com",
             "mimer.no", "nordnet.no", "nett.no", "sensewave.no", "vikenfiber.no", "haugnett.no", "ebnett.no", "outlook.sg"}
ACCOUNTANT_HINT = re.compile(r"regnskap|revisjon|accounting|advokat|bank")  # shared mail domains of service providers
SOCIAL = {
    "linkedin": (re.compile(r"^(?:[a-z]{2,3}\.)?linkedin\.com$"), re.compile(r"^/company/[^/?#]+")),
    "facebook": (re.compile(r"^(?:www\.|m\.|nb-no\.)?facebook\.com$"), re.compile(r"^/(?!sharer|share|plugins|dialog|tr|policies|login|groups|events|watch|pages/category|hashtag|photo|permalink|profile\.php\?)[A-Za-z0-9.\-_]{3,}/?$")),
    "instagram": (re.compile(r"^(?:www\.)?instagram\.com$"), re.compile(r"^/(?!p/|reel/|explore|accounts|about|direct)[A-Za-z0-9._]{2,}/?$")),
    "youtube": (re.compile(r"^(?:www\.|m\.)?youtube\.com$"), re.compile(r"^/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+)")),
    "x": (re.compile(r"^(?:www\.)?(?:twitter|x)\.com$"), re.compile(r"^/(?!intent|share|home|i/|search|hashtag)[A-Za-z0-9_]{2,15}/?$")),
    "tiktok": (re.compile(r"^(?:www\.)?tiktok\.com$"), re.compile(r"^/@[^/?#]+")),
}
ATS_HOSTS = ("webcruiter", "easycruit", "teamtailor", "recman", "reachmee", "varbi", "hrmanager", "jobylon", "cvpartner",
             "lever.co", "workable.com", "greenhouse.io", "smartrecruiters", "jobbnorge", "lyse.no", "personio", "hire.", "recruitee", "ashbyhq")
CAREERS_RE = re.compile(r"karriere|ledige[-_ ]?stilling|stillinger|\bjobb|career|\bjobs?\b|rekrutter|join[-_ ]us|work[-_ ]with|bli[-_ ]med|vacanc", re.I)
NEWS_RE = re.compile(r"nyhet|news|aktuelt|blogg|blog|presse|pressemelding|artikkel|innlegg|meldinger", re.I)
LEGAL_RE = re.compile(r"kontakt|contact|om[-_ ]oss|about|personvern|privacy|vilkar|vilkår|terms|impressum|betingelser|handelsbetingelser|cookie|selskap", re.I)
JOB_STOP = re.compile(r"søk jobb|apply now|les mer|read more|alle stillinger|se alle|vis alle|kontakt|cookie|personvern|logg inn|login|facebook|"
                      r"linkedin|instagram|tilbake|hjem|meny|menu|jobb hos oss|^karriere$|ledige stillinger|open positions|^jobs?$|^careers?$|"
                      r"initiativsøknad|spontan", re.I)
JOB_PATH_RE = re.compile(r"stilling|jobb|job|position|vacanc|career|karriere|rekrutt|/ad/|/ads/|apply", re.I)
NOT_JOB_RE = re.compile(r"lokal|bolig|leilighet|tomt|plass|parkering|garasje|utleie|bil|biler|eiendom|produkt|vare", re.I)


def norm(text: str) -> str:
    text = text.casefold()
    for a, b in (("æ", "ae"), ("ø", "o"), ("å", "a"), ("é", "e"), ("è", "e"), ("ü", "u"), ("ö", "o"), ("ä", "a")):
        text = text.replace(a, b)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def name_tokens(legal_name: str) -> list[str]:
    tokens = norm(legal_name).split()
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return [t for t in tokens if t not in ("og", "i", "v", "avd")] or tokens


def domain_candidates(legal_name: str) -> list[str]:
    tokens = name_tokens(legal_name)
    if not tokens:
        return []
    base = norm(legal_name).split()
    variants: list[str] = []
    alt = legal_name.casefold().replace("ø", "oe").replace("å", "aa").replace("æ", "ae")
    alt_tokens = [t for t in norm(alt).split() if t not in LEGAL_SUFFIXES and t not in ("og",)]
    for toks in (tokens, alt_tokens):
        if not toks:
            continue
        joined = "".join(toks)
        if len(joined) >= 4:
            variants.append(joined)
        if len(toks) >= 2:
            variants.append("-".join(toks))
        if len(toks) >= 3:
            variants.append("".join(toks[:2]))
        if len(toks[0]) >= 6 and len(toks) >= 2:
            variants.append(toks[0])
    del base
    out: list[str] = []
    for v in variants:
        v = v[:60].strip("-")
        if v and v not in out:
            out.append(v)
    domains = [f"{v}.no" for v in out[:5]] + [f"{v}.com" for v in out[:2]]
    return domains[:7]


def registered_host(host: str) -> str:
    host = host.lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def clean_site_url(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip().split()[0].strip(",;")
    if not raw:
        return None
    if "@" in raw and "/" not in raw:
        return None
    if not re.match(r"^[a-z]+://", raw, re.I):
        raw = "https://" + raw
    parts = urllib.parse.urlsplit(raw)
    host = (parts.hostname or "").lower()
    if "." not in host or any(s in host for s in ("facebook.com", "linkedin.com", "instagram.com")):
        return None
    return f"{parts.scheme}://{parts.netloc}/"


def name_matches(text_norm: str, tokens: list[str]) -> bool:
    if not tokens:
        return False
    words = set(text_norm.split())
    if len(tokens) == 1:
        return tokens[0] in words and len(tokens[0]) >= 4
    phrase = " ".join(tokens)
    return phrase in text_norm or all(t in words for t in tokens)


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
            if m:
                try:
                    parsed = datetime(int(m[1]), int(m[2]), int(m[3]))
                except ValueError:
                    parsed = None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed.year < 2000 or parsed > datetime.now(timezone.utc) + timedelta(days=1):
        return None
    return parsed.date().isoformat()


class SiteContext:
    def __init__(self, origin: str, home: Page, home_resp: Response):
        self.origin = origin
        self.host = registered_host(urllib.parse.urlsplit(origin).hostname or "")
        self.home = home
        self.home_resp = home_resp
        self.pages: dict[str, tuple[Page, Response]] = {home_resp.final_url: (home, home_resp)}

    def same_site(self, url: str) -> bool:
        return registered_host(urllib.parse.urlsplit(url).hostname or "") == self.host


def _html_page(response: Response) -> Page | None:
    if response.status != 200:
        return None
    if response.content_type and "html" not in response.content_type.lower() and "xml" not in response.content_type.lower():
        return None
    return parse_html(response.text, response.final_url)


def _snippet(text: str, needle_re: re.Pattern, width: int = 60) -> str:
    m = needle_re.search(text)
    if not m:
        return ""
    return re.sub(r"\s+", " ", text[max(0, m.start() - width): m.end() + width]).strip()


def _page_proof(page: Page, org: str) -> str | None:
    haystack = page.text + "\n" + json.dumps(page.jsonld, ensure_ascii=False)
    if org in find_org_numbers(haystack):
        pattern = re.compile(r"(?<!\d)" + r"[ . ]?".join([org[:3], org[3:6], org[6:]]) + r"(?!\d)")
        return _snippet(haystack, pattern) or f"organisation number {org}"
    return None


def verify_candidate(fetcher: Fetcher, case: Case, url: str, org: str, tokens: list[str], source: str,
                     entity: dict | None = None) -> dict[str, Any]:
    """Fetch a candidate and decide identity. Returns {state, ctx, proof, ...}."""
    response = fetcher.get(url, case.ops)
    if response.status == 0 and url.startswith("https://") and not response.redirects:
        alt = fetcher.get("http://" + url[len("https://"):], case.ops)
        if alt.status != 0:
            response = alt
    result: dict[str, Any] = {"url": url, "source": source, "state": "none", "response": response}
    if response.blocked:
        result["state"] = "blocked"
        result["reason"] = response.error
        return result
    if response.status in (401, 403, 429):
        result["state"] = "blocked"
        result["reason"] = f"HTTP {response.status}"
        return result
    page = _html_page(response)
    if page is None:
        result["reason"] = f"HTTP {response.status or response.error}"
        return result
    origin_parts = urllib.parse.urlsplit(response.final_url)
    origin = f"{origin_parts.scheme}://{origin_parts.netloc}/"
    ctx = SiteContext(origin, page, response)
    result["ctx"] = ctx
    proof = _page_proof(page, org)
    proof_url = response.final_url
    if not proof:
        tried = 0
        legal = [link for link in page.links if LEGAL_RE.search(link.href) or LEGAL_RE.search(link.text)]
        cands: list[str] = []
        for link in legal:
            absu = page.abs(link.href).split("#")[0]
            if ctx.same_site(absu) and absu not in cands and absu != response.final_url:
                cands.append(absu)
        for path in ("kontakt", "kontakt-oss", "om-oss", "contact", "about", "personvern"):
            absu = urllib.parse.urljoin(origin, path)
            if len(cands) < 6 and absu not in cands:
                cands.append(absu)
        for absu in cands:
            if tried >= 4:
                break
            tried += 1
            sub = fetcher.get(absu, case.ops)
            sub_page = _html_page(sub)
            if sub_page is None:
                continue
            ctx.pages[sub.final_url] = (sub_page, sub)
            proof = _page_proof(sub_page, org)
            if proof:
                proof_url = sub.final_url
                break
    if proof:
        result.update(state="verified", proof="org_number_on_site", span=proof, proof_url=proof_url)
        return result
    # No org number. Registry-declared sites are accepted: the registry itself ties the URL to the entity.
    if source == "registry":
        result.update(state="verified", proof="registry_declared_website", span=f"hjemmeside: {url}", proof_url=response.final_url)
        return result
    text_norm = norm(page.title + " " + page.meta.get("og:site_name", "") + " " + page.text[:6000])
    if name_matches(text_norm, tokens):
        head = norm(page.title + " " + page.meta.get("og:site_name", ""))
        label = re.sub(r"[^a-z0-9]", "", norm(ctx.host.rsplit(".", 1)[0]))
        joined = "".join(tokens)
        branded = name_matches(head, tokens) or (len(joined) >= 4 and (joined in label or label in joined) and len(label) >= 4)
        corroboration = _corroborate(ctx, entity, source, branded)
        if corroboration:
            result.update(state="verified", proof="registry_contact_details_match", span=corroboration[0], proof_url=corroboration[1],
                          branded=branded)
            return result
        result.update(state="ambiguous", reason="name matches but the organisation number is not shown on the site")
    return result


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _corroborate(ctx: SiteContext, entity: dict | None, source: str, branded: bool = True) -> tuple[str, str] | None:
    """Name matched; accept only with entity-specific registry details: phone, or street AND postcode (or e-mail domain)."""
    if not entity:
        return None
    for url, (page, _resp) in ctx.pages.items():
        text = page.text
        digits = _digits(text)
        for key in ("telefon", "mobil"):
            phone = _digits(str(entity.get(key) or ""))[-8:]
            if len(phone) == 8 and phone in digits:
                return f"registry {key} {entity[key]} shown on the site", url
        addr = entity.get("forretningsadresse") or {}
        street = (addr.get("adresse") or [""])[0] if isinstance(addr.get("adresse"), list) and addr.get("adresse") else ""
        post = str(addr.get("postnummer") or "")
        tnorm = norm(text)
        if street and post and len(norm(street)) >= 6 and norm(street) in tnorm and re.search(rf"(?<!\d){post}(?!\d)", text):
            return f"registry address {street}, {post} shown on the site", url
        for leader in ((entity.get("_leader_names") or []) if branded else []):
            parts = norm(leader).split()
            if len(parts) >= 2 and len(norm(leader)) >= 9 and norm(leader) in tnorm:
                return f"registered leader {leader} named on the site", url
        email = str(entity.get("epostadresse") or "")
        if source == "registry_email_domain" and "@" in email:
            return f"site domain equals registry e-mail domain ({email.rsplit('@', 1)[1]})", url
    return None


def resolve_website(fetcher: Fetcher, case: Case, entity: dict | None, legal_name: str, wikidata: dict | None,
                    max_guesses: int = 7) -> dict[str, Any] | None:
    """Find and verify the official website; emits the official_website claim; returns context or None."""
    org = case.org
    tokens = name_tokens(legal_name)
    candidates: list[tuple[str, str]] = []
    seen_hosts: set[str] = set()

    def add(source: str, url: str | None) -> None:
        url = clean_site_url(url)
        if not url:
            return
        host = registered_host(urllib.parse.urlsplit(url).hostname or "")
        if host in seen_hosts:
            return
        seen_hosts.add(host)
        candidates.append((source, url))

    if entity:
        add("registry", entity.get("hjemmeside"))
    if wikidata and wikidata.get("website"):
        add("wikidata", wikidata["website"])
    if entity and entity.get("epostadresse") and "@" in entity["epostadresse"]:
        domain = entity["epostadresse"].rsplit("@", 1)[1].strip().lower()
        if domain and domain not in FREE_MAIL and not ACCOUNTANT_HINT.search(domain.split(".")[0]):
            add("registry_email_domain", domain)
    pre = len(candidates)
    for domain in domain_candidates(legal_name)[:max_guesses]:
        if registered_host(domain) in seen_hosts:
            continue
        if fetcher.host_is_public(domain) or fetcher.host_is_public("www." + domain):
            add("name_guess", domain)
    del pre

    best_ambiguous: dict[str, Any] | None = None
    blocked: dict[str, Any] | None = None
    attempts: list[str] = []
    for source, url in candidates:
        result = verify_candidate(fetcher, case, url, org, tokens, source, entity)
        attempts.append(f"{source}:{urllib.parse.urlsplit(url).hostname}:{result['state']}")
        if result["state"] == "verified":
            response: Response = result["response"]
            ctx: SiteContext = result["ctx"]
            methods = {"org_number_on_site": "org_number_exact_match_in_page_text",
                       "registry_declared_website": "registry_declared_website",
                       "registry_contact_details_match": "name_plus_registry_contact_details_match"}
            ev_ids = [case.ev(result["proof_url"], "company_owned", response.retrieved_at,
                              (ctx.pages.get(result["proof_url"], (None, response))[1]).sha256,
                              result["span"], methods[result["proof"]], http_status=200)]
            if entity and entity.get("hjemmeside") and source == "registry":
                ev_ids.append(case.ev(f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}", "official_registry",
                                      response.retrieved_at, None, f"hjemmeside: {entity['hjemmeside']}",
                                      "brreg_enheter_json_field", license="NLOD 2.0"))
            value = {"url": ctx.origin, "candidate_source": source, "identity_proof": result["proof"]}
            if result["proof"] == "registry_contact_details_match" and not result.get("branded", True):
                value["scope_note"] = "site name does not clearly carry the legal name; may be a group or brand site"
            case.claim("official_website", value, "available", ev_ids, confidence={"org_number_on_site": 0.99, "registry_declared_website": 0.9}.get(result["proof"], 0.85 if result.get("branded", True) else 0.7),
                       section="4")
            ctx.proof = result["proof"]  # type: ignore[attr-defined]
            return {"ctx": ctx, "source": source}
        if result["state"] == "blocked" and blocked is None:
            blocked = result
        if result["state"] == "ambiguous" and best_ambiguous is None:
            best_ambiguous = result
    if best_ambiguous:
        case.claim("official_website", None, "ambiguous", [],
                   note=f"Candidate {best_ambiguous['url']} resembles the company name but does not show organisation number {org}; not published",
                   section="4")
    elif blocked and blocked["source"] != "name_guess":
        case.claim("official_website", None, "blocked", [], note=f"Declared site {blocked['url']} could not be read: {blocked.get('reason')}", section="4")
    else:
        note = "No website declared in the registry; checked " + (", ".join(attempts) if attempts else "no resolvable name-derived domains")
        case.claim("official_website", None, "not_available", [], note=note[:300], section="4")
    return None


# ----- extraction from a verified site -----------------------------------------
def extract_brand(case: Case, ctx: SiteContext, legal_name: str) -> None:
    home, resp = ctx.home, ctx.home_resp
    brand = None
    method = ""
    for item in home.jsonld:
        if ld_types(item) & {"Organization", "LocalBusiness", "Corporation", "Store", "ProfessionalService"}:
            name = item.get("name") or item.get("legalName")
            if isinstance(name, str) and 2 <= len(name) <= 80:
                brand, method = name.strip(), "json_ld_organization_name"
                break
    if not brand and home.meta.get("og:site_name"):
        brand, method = home.meta["og:site_name"].strip()[:80], "og_site_name"
    if not brand and home.title:
        parts = [p.strip() for p in re.split(r"\s[|\-–—·]\s", home.title) if p.strip()]
        tokens = name_tokens(legal_name)
        for part in parts:
            if name_matches(norm(part), tokens) or (tokens and norm(part) and norm(part).split()[0] == tokens[0]):
                brand, method = part[:80], "title_segment"
                break
    if brand:
        segments = [seg.strip(" 	|-–—·:") for seg in re.split(r"\s[|\-–—·]\s|\s[|]|[|]\s", brand)]
        segments = [seg for seg in segments if len(re.findall(r"[A-Za-zÆØÅæøå0-9]", seg)) >= 3]
        tokens = name_tokens(legal_name)
        pick = next((seg for seg in segments if name_matches(norm(seg), tokens)), segments[0] if segments else None)
        brand = pick[:80] if pick else None
    if brand:
        eid = case.ev(resp.final_url, "company_owned", resp.retrieved_at, resp.sha256, brand, method, http_status=200)
        same = norm(brand) == norm(legal_name) or norm(brand) == " ".join(name_tokens(legal_name))
        case.claim("public_brand", {"name": brand, "differs_from_legal_name": not same}, "available", [eid],
                   confidence=0.8, section="1", note="Company's own self-description")
    else:
        case.claim("public_brand", None, "not_available", [], note="No brand name found in structured data or title", section="1")
    desc = home.meta.get("og:description") or home.meta.get("description")
    if desc and len(desc) >= 30:
        eid = case.ev(resp.final_url, "company_owned", resp.retrieved_at, resp.sha256, desc[:300], "meta_description", http_status=200)
        case.claim("company_description", desc[:400], "available", [eid], confidence=0.7, section="1",
                   note="Company-owned promotional copy: describes the business, not independent evidence")


def classify_social(href: str) -> tuple[str, str] | None:
    parts = urllib.parse.urlsplit(href)
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    if parts.query and "profile.php" in path:
        return None
    for platform, (host_re, path_re) in SOCIAL.items():
        if host_re.match(host) and path_re.match(path):
            clean = f"https://{host if platform != 'linkedin' else 'www.linkedin.com'}{path.rstrip('/')}"
            if platform == "linkedin":
                clean = "https://www.linkedin.com/" + "/".join(path.strip("/").split("/")[:2])
            if platform == "youtube" and path.startswith(("/channel/", "/c/", "/user/")):
                clean = f"https://www.youtube.com/" + "/".join(path.strip("/").split("/")[:2])
            if platform == "facebook":
                clean = f"https://www.facebook.com{path.rstrip('/')}"
            return platform, clean
    return None


def extract_social(case: Case, ctx: SiteContext) -> None:
    found: dict[str, dict[str, list[tuple[str, str, Response]]]] = {}
    for url, (page, resp) in ctx.pages.items():
        for link in page.links:
            cls = classify_social(page.abs(link.href))
            if cls:
                platform, clean = cls
                found.setdefault(platform, {}).setdefault(clean.lower(), []).append((clean, url, resp))
    for platform in SOCIAL:
        options = found.get(platform)
        if not options:
            continue
        ranked = sorted(options.items(), key=lambda kv: -len(kv[1]))
        top_url, top_hits = ranked[0]
        if len(ranked) > 1 and len(ranked[1][1]) == len(top_hits):
            case.claim("company_profile", {"platform": platform, "candidates": [o[1][0][0] for o in ranked[:3]]}, "ambiguous", [],
                       key=platform, note="Several different profiles linked with equal prominence", section="4")
            continue
        clean, page_url, resp = top_hits[0]
        eid = case.ev(page_url, "company_owned", resp.retrieved_at, resp.sha256, f"link to {clean}", "outbound_link_from_verified_site", http_status=200)
        case.claim("company_profile", {"platform": platform, "url": clean, "linked_from": page_url}, "available", [eid],
                   key=platform, confidence=0.9, section="4",
                   note="Profile URL taken from the verified company site; the platform itself was not scraped")


def _job_from_ld(item: dict, page: Page) -> dict | None:
    title = item.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    org = item.get("hiringOrganization")
    org_name = org.get("name") if isinstance(org, dict) else (org if isinstance(org, str) else None)
    loc = item.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    place = None
    if isinstance(loc, dict):
        addr = loc.get("address")
        if isinstance(addr, dict):
            place = ", ".join(filter(None, [addr.get("streetAddress"), addr.get("postalCode"), addr.get("addressLocality")])) or None
        elif isinstance(addr, str):
            place = addr
    return {"title": title.strip()[:160], "date_posted": parse_date(item.get("datePosted")), "valid_through": parse_date(item.get("validThrough")),
            "hiring_organization": org_name, "location": place, "url": item.get("url") or page.url}


def extract_jobs(fetcher: Fetcher, case: Case, ctx: SiteContext, legal_name: str) -> dict[str, Any]:
    """Careers page of the verified site. Returns {'checked': bool, 'count': n, 'page': url}."""
    home = ctx.home
    candidates: list[str] = []
    for link in home.links:
        absu = home.abs(link.href).split("#")[0]
        host = (urllib.parse.urlsplit(absu).hostname or "").lower()
        if CAREERS_RE.search(urllib.parse.urlsplit(absu).path) or CAREERS_RE.search(link.text):
            if ctx.same_site(absu) or any(h in host for h in ATS_HOSTS):
                if absu not in candidates:
                    candidates.append(absu)
    if not candidates:
        candidates = [urllib.parse.urljoin(ctx.origin, p) for p in ("karriere", "ledige-stillinger", "jobb", "careers")]
    candidates = sorted(candidates, key=lambda u: len(u))[:3]
    tokens = name_tokens(legal_name)
    for url in candidates:
        resp = fetcher.get(url, case.ops)
        page = _html_page(resp)
        if page is None:
            continue
        ctx.pages[resp.final_url] = (page, resp)
        jobs: list[dict] = []
        for item in page.jsonld:
            if "JobPosting" in ld_types(item):
                job = _job_from_ld(item, page)
                if job:
                    org_name = job.get("hiring_organization")
                    if org_name and not name_matches(norm(org_name), tokens) and norm(org_name) not in norm(legal_name):
                        continue
                    jobs.append({**job, "method": "json_ld_jobposting", "confidence": 0.95})
        if not jobs:
            cap = 0
            for link in page.links:
                text = link.text.strip()
                absu = page.abs(link.href).split("#")[0]
                host = (urllib.parse.urlsplit(absu).hostname or "").lower()
                if not (8 <= len(text) <= 90) or JOB_STOP.search(text) or absu == resp.final_url or NOT_JOB_RE.search(text):
                    continue
                ats = any(h in host for h in ATS_HOSTS)
                if not (ats or (ctx.same_site(absu) and JOB_PATH_RE.search(urllib.parse.urlsplit(absu).path))):
                    continue
                if urllib.parse.urlsplit(absu).path.rstrip("/") == urllib.parse.urlsplit(resp.final_url).path.rstrip("/") and not ats:
                    continue
                if any(j["url"] == absu for j in jobs):
                    continue
                jobs.append({"title": text, "date_posted": None, "valid_through": None, "hiring_organization": None,
                             "location": None, "url": absu, "method": "careers_page_link", "confidence": 0.6})
                cap += 1
                if cap >= 12:
                    break
        for job in jobs[:15]:
            span = job["title"] + (f" (datePosted {job['date_posted']})" if job["date_posted"] else "")
            eid = case.ev(resp.final_url, "company_owned", resp.retrieved_at, resp.sha256, span, job["method"], http_status=200,
                          effective_date=job["date_posted"])
            value = {k: v for k, v in job.items() if k not in ("method", "confidence")}
            case.claim("job_posting", value, "available", [eid], key=job["url"], confidence=job["confidence"],
                       effective_date=job["date_posted"], section="5", note="Posting listed on the company's own careers page")
        return {"checked": True, "count": len(jobs[:15]), "page": resp.final_url}
    return {"checked": False, "count": 0, "page": None}


def _parse_feed(text: str) -> list[dict]:
    items: list[dict] = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return items
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        d: dict[str, str] = {}
        for child in el:
            ctag = child.tag.rsplit("}", 1)[-1]
            if ctag == "link":
                d["link"] = child.attrib.get("href") or (child.text or "")
            elif ctag in ("title", "pubDate", "published", "updated", "date", "guid"):
                d.setdefault(ctag, (child.text or "").strip())
        date = parse_date(d.get("pubDate") or d.get("published") or d.get("updated") or d.get("date"))
        if d.get("title") and date:
            items.append({"title": re.sub(r"\s+", " ", d["title"])[:200], "date": date, "url": (d.get("link") or d.get("guid") or "").strip()})
    return items


def extract_activity(fetcher: Fetcher, case: Case, ctx: SiteContext) -> dict[str, Any]:
    """Dated company news from RSS/Atom, then sitemap lastmod, then <time> tags. Returns {'checked','count'}."""
    origin = ctx.origin
    feeds: list[str] = []
    for _t, href, _title in ctx.home.alternates:
        feeds.append(ctx.home.abs(href))
    checked = False
    if not feeds:
        feeds = [urllib.parse.urljoin(origin, p) for p in ("feed/", "rss.xml", "feed.xml")]
    emitted = 0
    for url in feeds[:3]:
        if not ctx.same_site(url):
            continue
        resp = fetcher.get(url, case.ops, accept="application/rss+xml,application/atom+xml,application/xml,text/xml;q=0.9,*/*;q=0.5")
        if resp.status != 200:
            continue
        checked = True
        items = _parse_feed(resp.text)
        for item in sorted(items, key=lambda i: i["date"], reverse=True)[:6]:
            eid = case.ev(resp.final_url, "company_owned", resp.retrieved_at, resp.sha256, f"{item['date']} {item['title']}",
                          "rss_atom_item", http_status=200, effective_date=item["date"])
            case.claim("public_activity", {"kind": "company_news", "title": item["title"], "date": item["date"], "url": item["url"] or resp.final_url},
                       "available", [eid], key="news:" + (item["url"] or item["title"]), confidence=0.95,
                       effective_date=item["date"], section="5")
            emitted += 1
        if emitted:
            return {"checked": True, "count": emitted}
    # sitemap lastmod
    sitemap_urls = fetcher.robots_sitemaps(origin) or [urllib.parse.urljoin(origin, "sitemap.xml")]
    entries: list[tuple[str, str]] = []
    queue = sitemap_urls[:2]
    fetched = 0
    while queue and fetched < 3:
        sm = queue.pop(0)
        if not ctx.same_site(sm):
            continue
        resp = fetcher.get(sm, case.ops, accept="application/xml,text/xml;q=0.9,*/*;q=0.5", max_bytes=2_000_000)
        fetched += 1
        if resp.status != 200:
            continue
        checked = True
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError:
            continue
        for el in root.iter():
            tag = el.tag.rsplit("}", 1)[-1]
            if tag == "sitemap":
                loc = next((c.text for c in el if c.tag.rsplit("}", 1)[-1] == "loc"), None)
                if loc and NEWS_RE.search(loc):
                    queue.append(loc.strip())
            elif tag == "url":
                loc = next((c.text for c in el if c.tag.rsplit("}", 1)[-1] == "loc"), None)
                mod = next((c.text for c in el if c.tag.rsplit("}", 1)[-1] == "lastmod"), None)
                if loc and mod and NEWS_RE.search(urllib.parse.urlsplit(loc).path) and urllib.parse.urlsplit(loc).path.count("/") >= 2:
                    date = parse_date(mod)
                    if date:
                        entries.append((date, loc.strip()))
    for date, loc in sorted(set(entries), reverse=True)[:4]:
        if not ctx.same_site(loc):
            continue
        resp = fetcher.get(loc, case.ops)
        page = _html_page(resp)
        if page is None:
            continue
        title = page.meta.get("og:title") or page.title
        published = parse_date(page.meta.get("article:published_time") or page.meta.get("datepublished"))
        for item in page.jsonld:
            if not published and item.get("datePublished"):
                published = parse_date(str(item["datePublished"]))
        if not title:
            continue
        use_date = published or date
        eid = case.ev(resp.final_url, "company_owned", resp.retrieved_at, resp.sha256, f"{use_date} {title[:150]}",
                      "page_published_time" if published else "sitemap_lastmod", http_status=200, effective_date=use_date)
        case.claim("public_activity", {"kind": "company_news", "title": title[:200], "date": use_date, "url": resp.final_url,
                                       "date_basis": "page_published_time" if published else "sitemap_lastmod"},
                   "available", [eid], key="news:" + resp.final_url, confidence=0.85 if published else 0.6,
                   effective_date=use_date, section="5")
        emitted += 1
    return {"checked": checked, "count": emitted}
