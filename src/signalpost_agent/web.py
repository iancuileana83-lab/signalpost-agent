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

from . import official
from .htmlx import Page, find_org_numbers, labelled_org_numbers, ld_types, parse_html
from .http import Fetcher, Response
from .model import Case

LEGAL_SUFFIXES = {"as", "asa", "ans", "da", "sa", "ks", "nuf", "enk", "iks", "sf", "ba", "stiftelse", "foretak", "hf", "kf", "fkf", "sa"}
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
             "lever.co", "workable.com", "greenhouse.io", "smartrecruiters", "jobbnorge", "lyse.no", "personio", "hire.", "recruitee", "ashbyhq",
             "myworkdayjobs", "emply.", "talentech", "join.com", "breezy.hr")
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


def parse_date(value: str | None, *, future: bool = False) -> str | None:
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
    if parsed.year < 2000 or (not future and parsed > datetime.now(timezone.utc) + timedelta(days=1)):
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


# ----- whose site is it? ---------------------------------------------------------
_W = r"[A-ZÆØÅ0-9][\w&'’.\-]*"
ENTITY_RE = re.compile(rf"({_W}(?: (?:{_W}|og|and|&)){{0,4}}) (AS|ASA|GmbH|AB|A/S|ApS|Oy|Oyj|Ltd|Limited|Inc|B\.V\.|BV|S\.A\.|AG|LLC|SE|NUF)(?!\w)")
COPYRIGHT_RE = re.compile(r"(?:©|\(c\)|copyright)\s*(?:\d{4}\s*(?:[-–]\s*\d{4})?\s*[,.:\-]?\s*)?(?:by\s+)?(.{0,90})", re.I)
DOMAIN_LIKE = re.compile(r"^(?:www\.)?[\w-]+(?:\.[\w-]+)+$")


def _org_near(text: str, start: int, end: int, org: str) -> bool:
    return org in find_org_numbers(text[max(0, start - 200): end + 200])


OWNER_CONTEXT_RE = re.compile(r"behandlingsansvarlig|dataansvarlig|data controller|controller|ansvarlig utgiver|utgiver|publisher|operated by|"
                              r"drevet av|driftet av|eies av|eid av|owned by|company information|selskapsinformasjon|impressum|"
                              r"legal notice|provider|tjenesteleverandør|nettstedet (?:eies|drives)|this (?:web)?site is", re.I)


def foreign_owner(page: Page, legal_name: str, org: str) -> tuple[str, str] | None:
    """Another legal entity that this page names as its owner/publisher and that shares the company's first name word
    (REMA 1000 AS vs REMA 1000 NORGE AS, Color Line GmbH vs Color Line AS, Aker Solutions ASA vs AS).
    Evidence: a © line (any page), or a legal page (impressum, privacy, contact) naming it in an owner / controller / publisher
    context. A mere mention does not count, nor does a longer name that starts with ours (a subsidiary), nor a mention that
    sits next to our own organisation number."""
    ours = norm(legal_name)
    own_tokens = name_tokens(legal_name)
    first = (own_tokens or [""])[0]
    if len(first) < 3:
        return None
    text = page.text
    found: list[tuple[str, int, bool]] = []  # (entity name, absolute start, is a © statement)
    for m in COPYRIGHT_RE.finditer(text):
        lead = len(m.group(1)) - len(m.group(1).lstrip())
        e = ENTITY_RE.match(m.group(1).lstrip())
        if e:
            found.append((e.group(0), m.start(1) + lead, True))
    if LEGAL_RE.search(page.url) or LEGAL_RE.search(page.title):
        found += [(e.group(0), e.start(), False) for e in ENTITY_RE.finditer(text)
                  if OWNER_CONTEXT_RE.search(text[max(0, e.start() - 100): e.start()])]
    for raw, start, is_copyright in found:
        name = raw.strip(" .,")
        core = name_tokens(ENTITY_RE.match(name).group(1)) if ENTITY_RE.match(name) else name_tokens(name)  # without the legal form
        if norm(name) == ours or not core or core[0] != first:
            continue
        if not is_copyright and len(core) > len(own_tokens) and core[:len(own_tokens)] == own_tokens:
            continue  # "Equinor Asset Management AS" on the Equinor ASA site: a subsidiary, not the owner
        if _org_near(text, start, start + len(name), org):
            continue
        return name, re.sub(r"\s+", " ", text[max(0, start - 40): start + len(name) + 40]).strip()
    return None


def site_brand_tokens(ctx: "SiteContext", legal_name: str) -> list[str]:
    """How the site names itself, when that name is (part of) the legal name; otherwise []."""
    home = ctx.home
    candidates = [home.meta.get("og:site_name", "")]
    candidates += [str(i.get("name") or "") for i in home.jsonld if ld_types(i) & {"Organization", "Corporation", "LocalBusiness"}]
    candidates += [seg for seg in re.split(r"\s[|\-–—·]\s", home.title)]
    candidates.append(ctx.host.split(".")[0].replace("-", " "))
    ours = "".join(name_tokens(legal_name))
    for raw in candidates:
        raw = raw.strip()
        if not raw or DOMAIN_LIKE.match(raw):
            continue
        tokens = name_tokens(raw)
        joined = "".join(tokens)
        if len(joined) >= 4 and joined in ours:
            return tokens
    return []


def assess_site_scope(fetcher: Fetcher, case: Case, ctx: "SiteContext", legal_name: str, proof: str, source: str,
                      branded: bool) -> dict[str, Any]:
    """Is this the company's own site, or the site of a parent group / brand owner? Any negative sign means 'group'."""
    for url, (page, resp) in ctx.pages.items():
        hit = foreign_owner(page, legal_name, case.org)
        if hit:
            name, span = hit
            return {"scope": "group", "owner": name, "reason": f'the site names another legal entity, "{name}", as its owner or publisher',
                    "evidence": (url, "company_owned", resp.sha256, span, "other_legal_owner_named_on_site")}
    if proof == "legal_name_and_address_match" and not branded:
        return {"scope": "group", "owner": None, "reason": "the site's own name does not carry the company name, so it is a brand or group site",
                "evidence": (ctx.home_resp.final_url, "company_owned", ctx.home_resp.sha256, ctx.home.title[:200], "site_brand_differs_from_legal_name")}
    if proof != "org_number_on_site":  # registry-declared too: orkla.com for ORKLA HOME & PERSONAL CARE AS is ORKLA ASA's site
        # The site brand is only part of the legal name ("Statkraft" vs "Statkraft Energi AS"): who else is registered under that name?
        brand = site_brand_tokens(ctx, legal_name)
        if brand and brand != name_tokens(legal_name) and len("".join(brand)) >= 4:
            for other_org, other_name in official.search_names(fetcher, case, " ".join(brand)):
                if other_org != case.org and name_tokens(other_name) == brand:
                    return {"scope": "group", "owner": f"{other_name} ({other_org})",
                            "reason": f'the site is branded "{" ".join(brand)}", the exact name of another registered entity, {other_name} ({other_org})',
                            "evidence": (official.NAME_SEARCH.split("?")[0], "official_registry", None, f"{other_org} {other_name}",
                                         "brreg_exact_name_of_other_entity")}
    basis = {"org_number_on_site": "organisation number shown on the site", "registry_declared_website": "declared in the registry"}.get(
        proof, "exact name and registered address shown on the site, and the site is branded with the company name")
    return {"scope": "own", "basis": basis + "; no other legal entity named as the site's owner"}


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
    others = set().union(*(labelled_org_numbers(p.text) for p, _r in ctx.pages.values())) - {org}
    if others and not proof:
        # The site states another entity's organisation number and never ours: it is that entity's site (the manager's, the
        # parent's or a sister company's), even when the registry declares it.
        result.update(state="other_entity", reason=f"the site states organisation number {sorted(others)[0]} of another entity, and not {org}")
        return result
    first = next((t for t in tokens if t not in GENERIC_NAME_WORDS), "")
    named = (site_names_company(ctx, tokens) or brand_is_ours(ctx, tokens)
             or (len(first) >= 4 and (first in re.sub(r"[^a-z0-9]", "", ctx.host.split(".")[0]) or first in norm(page.title).split())))
    head = norm(page.title + " " + page.meta.get("og:site_name", ""))
    label = re.sub(r"[^a-z0-9]", "", norm(ctx.host.rsplit(".", 1)[0]))
    joined = "".join(tokens)
    branded = name_phrase_in(head, tokens) or (len(joined) >= 4 and len(label) >= 4 and joined == label) or brand_is_ours(ctx, tokens)
    if proof and named:
        # The organisation number alone could sit in a list of clients or managed associations: the site must also name the company.
        result.update(state="verified", proof="org_number_on_site", span=proof, proof_url=proof_url, branded=branded)
        return result
    if source == "registry" and branded:
        # Registry-declared and the site calls itself by the company's name. A declared site that does not (the housing
        # manager's usbl.no / obos.no for a borettslag, a parent group's site) is not published: it is someone else's site.
        result.update(state="verified", proof="registry_declared_website", span=f"hjemmeside: {url}; site title: {page.title[:120]}",
                      proof_url=response.final_url, branded=True)
        return result
    address = _address_match(ctx, entity, tokens)
    if address:
        result.update(state="verified", proof="legal_name_and_address_match", span=address[0], proof_url=address[1], branded=branded)
        return result
    if named or proof:
        result.update(state="ambiguous", reason="the site does not prove it is this entity's own site (organisation number with name, "
                                                 "registry declaration with matching site name, or exact name with the registered address)")
    return result


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def name_phrase_in(text_norm: str, tokens: list[str]) -> bool:
    """The company's name (legal name without the legal form) as one contiguous phrase, not words scattered over a page."""
    if not tokens or len("".join(tokens)) < 4:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(" ".join(tokens)) + r"(?![a-z0-9])", text_norm) is not None


GENERIC_NAME_WORDS = {"borettslag", "sameie", "sameiet", "eiendom", "holding", "invest", "consulting", "gruppen", "group", "bygg", "service",
                      "services", "norge", "norway", "norsk", "regnskap", "transport", "boligbyggelag", "forening", "stiftelse", "kommune"}


def brand_is_ours(ctx: SiteContext, tokens: list[str]) -> bool:
    """The site's own name (og:site_name, Organization name, a <title> segment) is a contiguous run of the legal name's words
    that is not just a generic word: "KONGSBERG" for KONGSBERG GRUPPEN ASA, "Hydro" for Norsk Hydro ASA; not "OBOS" or
    "Borettslag" for ABELLUND BORETTSLAG."""
    home = ctx.home
    names = [home.meta.get("og:site_name", "")] + [seg for seg in re.split(r"\s[|\-–—·:]\s", home.title)]
    names += [str(i.get("name") or "") for i in home.jsonld if ld_types(i) & {"Organization", "Corporation", "LocalBusiness"}]
    for raw in names:
        cand = name_tokens(raw.strip()) if raw.strip() and not DOMAIN_LIKE.match(raw.strip()) else []
        if not cand or len("".join(cand)) < 4 or all(t in GENERIC_NAME_WORDS for t in cand):
            continue
        k = len(cand)
        if any(tokens[i:i + k] == cand for i in range(len(tokens) - k + 1)):
            return True
    return False


def site_names_company(ctx: SiteContext, tokens: list[str]) -> bool:
    return any(name_phrase_in(norm(p.title + " " + p.text), tokens) for p, _r in ctx.pages.values())


def _address_match(ctx: SiteContext, entity: dict | None, tokens: list[str]) -> tuple[str, str] | None:
    """The exact company name AND the registered Norwegian street address with postcode on the same page. Phone numbers,
    e-mail domains and people's names are not accepted: they are shared by accountants, managers and namesakes."""
    if not entity:
        return None
    addresses = []
    for key in ("forretningsadresse", "postadresse"):
        addr = entity.get(key) or {}
        street = (addr.get("adresse") or [""])[0] if isinstance(addr.get("adresse"), list) and addr.get("adresse") else ""
        post = str(addr.get("postnummer") or "")
        if street and post and len(norm(street)) >= 6 and re.search(r"\d", street) and (addr.get("landkode") or "NO") == "NO":
            addresses.append((street, post))
    for url, (page, _resp) in ctx.pages.items():
        tnorm = norm(page.title + " " + page.text)
        if not name_phrase_in(tnorm, tokens):
            continue
        for street, post in addresses:
            if re.search(r"(?<![a-z0-9])" + re.escape(norm(street)) + r"(?![a-z0-9])", tnorm) and re.search(rf"(?<!\d){post}(?!\d)", page.text):
                return f"name and registered address {street}, {post} shown on the site", url
    return None


def _publish_registry_shared(case: Case, entity: dict | None, ctx: "SiteContext", response: Response, org: str, reason: str) -> dict[str, Any]:
    """A homepage the registry declares for this organisation number is always published, also when it is a group, manager or
    shared site (hydro.com, orkla.com, usbl.no). Such a site is read strictly: no brand or description (they describe the site
    owner), social profiles only with the exact legal name as handle, news only naming the full legal name, jobs only when the
    posting names this exact company as the employer."""
    ev_ids = [case.ev(f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}", "official_registry", response.retrieved_at, None,
                      f"hjemmeside: {(entity or {}).get('hjemmeside')}", "brreg_enheter_json_field", license="NLOD 2.0"),
              case.ev(response.final_url, "company_owned", response.retrieved_at, response.sha256, ctx.home.title[:200],
                      "registry_declared_website_reachable", http_status=200)]
    case.claim("official_website", {"url": ctx.origin, "candidate_source": "registry", "identity_proof": "registry_declared_website",
                                    "site_scope": "registry_declared_shared", "scope_basis": f"declared in the registry for {org}; {reason}"},
               "available", ev_ids, confidence=0.9, section="4",
               note="Declared in Enhetsregisteret for this organisation number. The site may belong to a group or manager, so only "
                    "facts that name this exact company are read from it")
    ctx.proof = "registry_declared_website"  # type: ignore[attr-defined]
    ctx.shared = True  # type: ignore[attr-defined]
    return {"ctx": ctx, "source": "registry", "shared": True}


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
    raw_declared = str((entity or {}).get("hjemmeside") or "").strip()
    _parts = urllib.parse.urlsplit(raw_declared if re.match(r"^[a-z]+://", raw_declared, re.I) else "https://" + raw_declared)
    declared_host = registered_host(_parts.hostname or "")
    declared_path = _parts.path.strip("/")
    if wikidata and wikidata.get("website"):
        add("wikidata", wikidata["website"])
    if entity and entity.get("epostadresse") and "@" in entity["epostadresse"]:
        domain = entity["epostadresse"].rsplit("@", 1)[1].strip().lower()
        if domain and domain not in FREE_MAIL and not ACCOUNTANT_HINT.search(domain.split(".")[0]):
            add("registry_email_domain", domain)
    # No domain is ever guessed from the company name (max_guesses is ignored): a name-shaped domain is how a cafe in
    # Illinois became the website of a Norwegian ISABELLA CAFE AS. Candidates come only from the registry and Wikidata (P2333).

    best_ambiguous: dict[str, Any] | None = None
    best_group: dict[str, Any] | None = None
    blocked: dict[str, Any] | None = None
    attempts: list[str] = []
    for source, url in candidates:
        result = verify_candidate(fetcher, case, url, org, tokens, source, entity)
        attempts.append(f"{source}:{urllib.parse.urlsplit(url).hostname}:{result['state']}")
        if result["state"] == "verified":
            response: Response = result["response"]
            ctx: SiteContext = result["ctx"]
            scope = assess_site_scope(fetcher, case, ctx, legal_name, result["proof"], source, result.get("branded", True))
            if (scope["scope"] == "own" and source == "registry" and declared_path and result["proof"] != "org_number_on_site"
                    and ctx.host == declared_host):
                # The registry points at a sub-page (orkla.no/home-personal-care/, festivaldrift.no/countryfestivalen): the domain is
                # shared with a group or service provider, so its homepage, profiles, news and jobs are not this company's.
                scope = {"scope": "group", "owner": None,
                         "reason": f"the registry declares a sub-page (/{declared_path}) of a shared site, and the site does not show organisation number {org}"}
            if scope["scope"] == "group" and source == "registry":
                return _publish_registry_shared(case, entity, ctx, response, org, scope["reason"])
            if scope["scope"] == "group":
                attempts[-1] += ":group_site"
                if best_group is None:
                    best_group = {**scope, "origin": ctx.origin, "retrieved_at": response.retrieved_at}
                continue
            methods = {"org_number_on_site": "org_number_exact_match_in_page_text",
                       "registry_declared_website": "registry_declared_website",
                       "legal_name_and_address_match": "exact_name_plus_registered_address_match"}
            ev_ids = [case.ev(result["proof_url"], "company_owned", response.retrieved_at,
                              (ctx.pages.get(result["proof_url"], (None, response))[1]).sha256,
                              result["span"], methods[result["proof"]], http_status=200)]
            if entity and entity.get("hjemmeside") and source == "registry":
                ev_ids.append(case.ev(f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}", "official_registry",
                                      response.retrieved_at, None, f"hjemmeside: {entity['hjemmeside']}",
                                      "brreg_enheter_json_field", license="NLOD 2.0"))
            value = {"url": ctx.origin, "candidate_source": source, "identity_proof": result["proof"],
                     "site_scope": "own", "scope_basis": scope["basis"]}
            case.claim("official_website", value, "available", ev_ids, confidence={"org_number_on_site": 0.99, "registry_declared_website": 0.95}.get(result["proof"], 0.95),
                       section="4")
            ctx.proof = result["proof"]  # type: ignore[attr-defined]
            return {"ctx": ctx, "source": source}
        if source == "registry" and result.get("ctx") is not None and result["state"] in ("ambiguous", "other_entity", "none"):
            # Declared in Brønnøysund for this exact organisation number: always published, read strictly (see below).
            return _publish_registry_shared(case, entity, result["ctx"], result["response"], org,
                                            result.get("reason") or "the site does not itself name the company")
        if result["state"] == "other_entity" and best_group is None:
            best_group = {"reason": result["reason"]}
        if result["state"] == "blocked" and blocked is None:
            blocked = result
        if result["state"] == "ambiguous" and best_ambiguous is None:
            best_ambiguous = result
    # Nothing verified. Nothing about any candidate site is published, not even its URL: when in doubt, leave it out.
    if best_group:
        case.claim("official_website", None, "ambiguous", [], section="4",
                   note=f"A candidate site was found but it is not this company's own site ({best_group['reason']}); not published, "
                        "and neither are its social links, news or jobs")
    elif best_ambiguous:
        case.claim("official_website", None, "ambiguous", [], section="4",
                   note=f"A candidate site could not be proven to be this entity's own site (organisation number {org} with the name, "
                        "a registry declaration with matching site name, or the exact name with the registered address); not published")
    elif blocked:
        case.claim("official_website", None, "blocked", [], note=f"The declared site could not be read ({blocked.get('reason')}); not published", section="4")
    else:
        case.claim("official_website", None, "not_available", [], section="4",
                   note="No website is declared in the registry or Wikidata (by organisation number) that could be verified; no domain is guessed from the name")
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
        joined_legal = "".join(name_tokens(legal_name))
        joined_brand = "".join(name_tokens(brand))
        if not (len(joined_brand) >= 3 and (joined_brand in joined_legal or name_phrase_in(norm(brand), name_tokens(legal_name)))):
            brand = None  # a brand that is not (part of) the legal name may be the manager's or the group's
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


# A handle that carries the company name is still not the company's when it also carries a country, group or foreign
# legal-form marker (colorline_deutschland, ColorLineGmbH, orkla-group). Short markers only count as the whole remainder.
FOREIGN_MARKERS = ("deutschland", "germany", "tyskland", "sverige", "sweden", "danmark", "denmark", "suomi", "finland", "global",
                   "group", "gruppen", "international", "holding", "konsern", "europe", "gmbh", "corporate")
SHORT_FOREIGN_MARKERS = {"ab", "oy", "sa", "ag", "uk", "usa", "se", "dk", "de", "fi", "inc", "ltd", "aps", "bv"}
NEUTRAL_REMAINDER = ("norge", "norway", "norsk", "norwegian", "official", "offisiell", "offisielle", "asa", "as", "nor", "no")
# Housing-cooperative managers, property managers and parent organisations whose name may sit next to ours in a handle
MANAGER_MARKERS = ("usbl", "obos", "bbl", "boligbyggelag", "forvaltning", "eiendom", "regnskap", "revisjon", "advokat", "konsern", "gruppen",
                   "holding", "forening", "forbund", "kjede", "franchise")
# Social links that belong to the web agency or the site theme, never to the company
THIRD_PARTY_HANDLES = {"wordpress", "wix", "squarespace", "shopify", "webflow", "elementor", "themeforest", "envato", "facebook", "meta",
                       "instagram", "linkedin", "youtube", "twitter", "x", "tiktok", "google", "visma", "mailchimp", "hubspot"}


def _handle(platform: str, clean: str) -> str:
    path = urllib.parse.urlsplit(clean).path.strip("/")
    parts = path.split("/")
    if platform == "linkedin" and len(parts) >= 2:
        return parts[1]
    if platform in ("youtube",) and parts and parts[0] in ("channel", "c", "user") and len(parts) >= 2:
        return ("channel:" if parts[0] == "channel" else "") + parts[1]
    return parts[0].lstrip("@") if parts else ""


def social_mismatch(platform: str, clean: str, anchors: list[str], declared: set[str]) -> str | None:
    """None when the profile can be tied to the company, else the reason it is not published."""
    handle = _handle(platform, clean)
    key = re.sub(r"[^a-z0-9]", "", norm(handle))
    if not key or handle.startswith("channel:") or key.isdigit():
        return "opaque profile id, cannot be tied to the company"
    if key in THIRD_PARTY_HANDLES:
        return "profile of a platform, theme vendor or agency"
    anchor = next((a for a in sorted(anchors, key=len, reverse=True) if a in key), None)
    if anchor is None and clean.lower() not in declared:
        return "profile name does not carry the company name"
    rest = key.replace(anchor, "", 1) if anchor else key
    if anchor and rest.strip("0123456789") and rest not in LEGAL_SUFFIXES | {"official", "offisiell", "no"}:
        return f'profile name adds "{rest}" to the company name: it may belong to a sister, country or parent entity'
    for word in NEUTRAL_REMAINDER:
        rest = rest.replace(word, "") if len(word) > 3 else ("" if rest == word else rest)
    manager = next((m for m in MANAGER_MARKERS if m in rest), None)
    if manager:
        return f'profile name carries "{manager}": it belongs to a manager, parent or related organisation'
    marker = next((m for m in FOREIGN_MARKERS if m in rest), None) or (rest if rest in SHORT_FOREIGN_MARKERS else None)
    if marker:
        return f'profile name carries "{marker}": it belongs to another entity, a country branch or the group'
    return None


def declared_profiles(ctx: SiteContext, legal_name: str) -> set[str]:
    """Profiles the site itself declares as its organisation's identity: sameAs of an Organization whose name is the company's."""
    out: set[str] = set()
    tokens = name_tokens(legal_name)
    for item in ctx.home.jsonld:
        if not ld_types(item) & {"Organization", "Corporation", "LocalBusiness", "Store", "ProfessionalService"}:
            continue
        if not name_matches(norm(str(item.get("name") or item.get("legalName") or "")), tokens):
            continue
        same_as = item.get("sameAs")
        for link in ([same_as] if isinstance(same_as, str) else same_as or []):
            cls = classify_social(str(link))
            if cls:
                out.add(cls[1].lower())
    return out


def extract_social(case: Case, ctx: SiteContext, legal_name: str) -> None:
    """Social profiles linked from the footer of the company's confirmed own site (homepage), kept only when each one can be tied
    to this company. Header, body, legal-page and other-page links are not read. Says so when nothing is found."""
    # The handle must carry the company's own name. The site's domain label is NOT an anchor: on a manager's or group's site it is
    # the manager's name (usbl -> instagram.com/boligbyggelaget_usbl published for HOVLY BORETTSLAG).
    joined_legal = "".join(name_tokens(legal_name))
    anchors = [joined_legal] if len(joined_legal) >= 4 else []
    declared = set() if getattr(ctx, "shared", False) else declared_profiles(ctx, legal_name)
    found: dict[str, dict[str, list[tuple[str, str, Response]]]] = {}
    rejected: dict[str, str] = {}
    home_url = ctx.home_resp.final_url
    page, resp = ctx.home, ctx.home_resp
    seen_in_footer = 0
    for url in (home_url,):
        for link in page.links:
            cls = classify_social(page.abs(link.href))
            if not cls:
                continue
            seen_in_footer += 1
            platform, clean = cls
            why = social_mismatch(platform, clean, anchors, declared)
            if why:
                rejected.setdefault(clean.lower(), f"{clean}: {why}")
                continue
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
                   note="Linked from the company's own verified site; the handle carries the company's legal name and no group, manager, country or agency marker. The platform itself was not scraped")
    if not found:
        if rejected:
            note = ("Social links on the site were not published because they cannot be tied to this company: "
                    + "; ".join(sorted(rejected.values())[:6]))
        else:
            note = f"No social media links found on the homepage of {ctx.origin}"
        case.claim("company_profile", None, "not_available", [], key="any", section="4", note=note[:400])


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
    return {"title": title.strip()[:160], "date_posted": parse_date(item.get("datePosted")), "valid_through": parse_date(item.get("validThrough"), future=True),
            "hiring_organization": org_name, "location": place, "url": item.get("url") or page.url}


def ats_host(host: str) -> bool:
    return any(h in host for h in ATS_HOSTS)


def ats_ad_like(url: str) -> bool:
    """An ad URL on a job platform (/jobs/123-title, /Public/4711, ?jobid=9), not the platform's or the company's root page."""
    parts = urllib.parse.urlsplit(url)
    if (parts.hostname or "").endswith("myworkdayjobs.com"):
        return "/job/" in parts.path  # /en-US/SiteName is the career site, /job/... is one posting
    return len([seg for seg in parts.path.split("/") if seg]) >= 2 or bool(re.search(r"(?:^|[?&])\w*(?:id|job|ad)\w*=", parts.query, re.I))


WORKDAY_LOCALE = re.compile(r"^[a-z]{2}(?:-[A-Za-z]{2})?$")


def workday_jobs(fetcher: Fetcher, case: Case, careers_url: str, legal_name: str, max_postings: int = 12) -> list[dict]:
    """Postings of a Workday career site through its public JSON endpoint (the one the site's own page calls), no scripts run.
    The list gives titles and paths; each posting's detail gives the real start date, public URL and the hiring organisation, and
    only postings whose hiring organisation is this company are kept."""
    parts = urllib.parse.urlsplit(careers_url)
    segments = [seg for seg in parts.path.split("/") if seg and not WORKDAY_LOCALE.match(seg)]
    host = parts.hostname or ""
    if not host.endswith("myworkdayjobs.com") or not segments:
        return []
    tenant, site = host.split(".")[0], segments[0]
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    body = json.dumps({"appliedFacets": {}, "limit": max_postings, "offset": 0, "searchText": ""}).encode()
    listing = fetcher.get(base + "/jobs", case.ops, accept="application/json", data=body, headers={"Content-Type": "application/json"})
    try:
        rows = json.loads(listing.text)["jobPostings"] if listing.status == 200 else []
    except (ValueError, KeyError, TypeError):
        rows = []
    out: list[dict] = []
    for row in rows[:max_postings]:
        path = row.get("externalPath") if isinstance(row, dict) else None
        if not path or not row.get("title"):
            continue
        detail = fetcher.get(base + path, case.ops, accept="application/json")
        try:
            data = json.loads(detail.text) if detail.status == 200 else {}
            info = data.get("jobPostingInfo") or {}
        except (ValueError, AttributeError):
            continue
        org = (data.get("hiringOrganization") or {}).get("name")
        job = {"title": str(info.get("title") or row["title"]).strip()[:160], "date_posted": parse_date(info.get("startDate")),
               "valid_through": parse_date(info.get("endDate"), future=True), "hiring_organization": org,
               "location": info.get("location") or row.get("locationsText"), "url": info.get("externalUrl") or f"https://{host}/{site}{path}"}
        own = name_tokens(legal_name)
        shown = [t for t in name_tokens(org or "") if not t.isdigit()]  # "1200 Equinor ASA" is Equinor ASA
        if org and shown == own:  # the employer itself: not a sister company, and no organisation shown means unproven
            out.append({**job, "method": "workday_cxs_json", "confidence": 0.95, "_resp": detail})
    return out


def extract_jobs(fetcher: Fetcher, case: Case, ctx: SiteContext, legal_name: str) -> dict[str, Any]:
    """Jobs from the company's own careers/jobs page: a page on the verified site, or the page on a job platform (Teamtailor,
    Webcruiter, Jobylon, ReachMee, ...) that the verified site links to as its careers page. Nothing else is a source.
    Returns {'checked': bool, 'count': n, 'page': url}; checked is False when the site has no such page."""
    home = ctx.home
    own: list[str] = []
    platform: list[str] = []
    for link in home.links:
        absu = home.abs(link.href).split("#")[0]
        parts = urllib.parse.urlsplit(absu)
        if CAREERS_RE.search(parts.path) or CAREERS_RE.search(link.text) or (ats_host(parts.hostname or "") and not parts.path.strip("/")):
            if ctx.same_site(absu) and absu not in own:
                own.append(absu)
            elif ats_host((parts.hostname or "").lower()) and absu not in platform:
                platform.append(absu)
    candidates = sorted(own, key=len)[:2] + sorted(platform, key=len)[:2]
    if not candidates:
        candidates = [urllib.parse.urljoin(ctx.origin, p) for p in ("karriere", "ledige-stillinger", "jobb", "careers")]
    tokens = name_tokens(legal_name)

    shared = getattr(ctx, "shared", False)

    def org_ok(job: dict) -> bool:
        org_name = job.get("hiring_organization")
        if shared:  # group / manager site: the posting itself must name this exact company as the employer
            return bool(org_name) and [t for t in name_tokens(org_name) if not t.isdigit()] == tokens
        return not (org_name and not name_matches(norm(org_name), tokens) and norm(org_name) not in norm(legal_name))

    queue, visited, checked_pages = candidates[:4], set(), []
    while queue and len(visited) < 6:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        resp = fetcher.get(url, case.ops)
        page = _html_page(resp)
        final_host = (urllib.parse.urlsplit(resp.final_url).hostname or "").lower()
        on_site = ctx.same_site(resp.final_url)
        if page is None or not (on_site or ats_host(final_host)):  # a redirect to some other host is not the careers page
            continue
        if on_site:
            ctx.pages[resp.final_url] = (page, resp)
        checked_pages.append(resp.final_url)
        jobs: list[dict] = []
        for item in page.jsonld:
            if "JobPosting" in ld_types(item):
                job = _job_from_ld(item, page)
                if job and org_ok(job):
                    jobs.append({**job, "method": "json_ld_jobposting", "confidence": 0.95})
        if not jobs and final_host.endswith("myworkdayjobs.com"):
            jobs = workday_jobs(fetcher, case, resp.final_url, legal_name)
        if not jobs:
            # A careers page is full of menu links ("What we offer", "Graduate programme", "FAQ"): a link is never a posting by
            # itself. Only an ad on a job platform, or a same-site page that itself carries JobPosting JSON-LD, is one.
            opened = 0
            for link in page.links:
                text = link.text.strip()
                absu = page.abs(link.href).split("#")[0]
                parts = urllib.parse.urlsplit(absu)
                host = (parts.hostname or "").lower()
                if not (8 <= len(text) <= 90) or JOB_STOP.search(text) or absu == resp.final_url or any(j["url"] == absu for j in jobs):
                    continue
                if ats_host(host):
                    if (not on_site and host != final_host) or not ats_ad_like(absu):
                        continue  # on a platform page, only ads on that same platform host (not its "powered by" links)
                    job, foreign = None, False
                    if opened < 12:
                        opened += 1
                        ad = fetcher.get(absu, case.ops)
                        ad_page = _html_page(ad)
                        if ad_page is not None:
                            for item in ad_page.jsonld:
                                cand = _job_from_ld(item, ad_page) if "JobPosting" in ld_types(item) else None
                                if cand and org_ok(cand):
                                    job = {**cand, "url": ad.final_url, "method": "platform_ad_page_json_ld", "confidence": 0.9, "_resp": ad}
                                    break
                                foreign = foreign or bool(cand)  # the ad itself names another employer
                            if job is None:
                                shown = next((parse_date(t.datetime) for t in ad_page.times if PUBLISHED_TIME_HINT.search(t.hint)), None)
                                if shown:
                                    job = {"title": text, "date_posted": shown, "valid_through": None, "hiring_organization": None, "location": None,
                                           "url": absu, "method": "platform_ad_page_time", "confidence": 0.7, "_resp": ad}
                    if foreign and job is None:
                        continue
                    jobs.append(job or {"title": text, "date_posted": None, "valid_through": None, "hiring_organization": None,
                                        "location": None, "url": absu, "method": "platform_ad_link", "confidence": 0.6})
                elif on_site and ctx.same_site(absu) and JOB_PATH_RE.search(parts.path) and not NOT_JOB_RE.search(text) and opened < 8:
                    opened += 1
                    ad = fetcher.get(absu, case.ops)
                    ad_page = _html_page(ad)
                    if ad_page is None:
                        continue
                    for item in ad_page.jsonld:
                        job = _job_from_ld(item, ad_page) if "JobPosting" in ld_types(item) else None
                        if job and org_ok(job):
                            jobs.append({**job, "url": ad.final_url, "method": "linked_ad_page_json_ld", "confidence": 0.9, "_resp": ad})
                            break
                if len(jobs) >= 12:
                    break
            if not jobs and on_site:
                # The own careers page may only point to the job platform ("See our open positions"): follow that one hop
                for link in page.links:
                    absu = page.abs(link.href).split("#")[0]
                    if ats_host((urllib.parse.urlsplit(absu).hostname or "").lower()) and not ats_ad_like(absu) and absu not in visited                             and absu not in queue and len(queue) < 2:
                        queue.append(absu)
        today = datetime.now(timezone.utc).date().isoformat()
        jobs = [j for j in jobs if j.get("date_posted") and j.get("url") and (j.get("valid_through") or today) >= today  # dated, open
                and (not shared or org_ok(j))]  # on a group/manager site the posting must name this exact employer
        for job in jobs[:15]:
            span = job["title"] + (f" (datePosted {job['date_posted']})" if job["date_posted"] else "")
            src = job.get("_resp", resp)
            eid = case.ev(src.final_url, "company_owned", src.retrieved_at, src.sha256, span, job["method"], http_status=200,
                          effective_date=job["date_posted"])
            value = {k: v for k, v in job.items() if k not in ("method", "confidence", "_resp")}
            case.claim("job_posting", value, "available", [eid], key=job["url"], confidence=job["confidence"],
                       effective_date=job["date_posted"], section="5", note="Posting listed on the company's own careers page, or on the job platform page the company links to as its careers page")
        if jobs:
            return {"checked": True, "count": len(jobs[:15]), "page": resp.final_url, "pages": checked_pages}
    return {"checked": bool(checked_pages), "count": 0, "page": checked_pages[0] if checked_pages else None, "pages": checked_pages}


# ----- news: only real, dated posts ---------------------------------------------------
# Menu labels and section names are not headlines; a sitemap lastmod, an Atom <updated> or a feed build date is not a publish date.
GENERIC_TITLES = {"news", "nyheter", "aktuelt", "blogg", "blog", "presse", "press", "pressemeldinger", "pressemelding", "press releases",
                  "press release", "pressrom", "presserom", "newsroom", "media", "mediesenter", "arkiv", "archive", "news archive",
                  "nyhetsarkiv", "all news", "alle nyheter", "latest news", "siste nyheter", "stories", "artikler", "articles",
                  "home", "hjem", "forside", "les mer", "read more", "se alle", "vis alle", "om oss", "about us", "kontakt", "contact"}
LISTING_SEGMENTS = {"news", "nyheter", "aktuelt", "blog", "blogg", "presse", "press", "pressemeldinger", "pressemelding", "arkiv", "archive",
                    "news-archive", "nyhetsarkiv", "newsroom", "pressrom", "presserom", "media", "artikler", "articles", "stories",
                    "nyhetsbrev", "page"}
NOT_NEWS_PATH = re.compile(r"/(?:produkt|produkter|product|products|shop|nettbutikk|butikk|tilbud|vare|varer|kategori|category|tag|pris|priser|bestill|kampanje)(?:/|$)", re.I)
ARTICLE_LD = {"Article", "NewsArticle", "BlogPosting", "Report", "TechArticle", "ScholarlyArticle"}
PUBLISHED_META = ("article:published_time", "og:article:published_time", "datepublished", "pubdate", "dc.date.issued", "dcterms.created",
                  "dcterms.issued", "sailthru.date")
PUBLISHED_TIME_HINT = re.compile(r"datepublished|publish|posted|entry-date|post-date|pubdate")
# Theme demo posts and comment-feed items are not company news
PLACEHOLDER_RE = re.compile(r"hello,? world|hei,? verden|hallo,? verden|lorem ipsum|your (?:awesome|attractive|intriguing|[a-z]+) post title|"
                            r"sample (?:post|page)|eksempel ?(?:innlegg|side)|just another wordpress|^kommentar til |^comment on |^svar til ", re.I)
SITE_SUFFIX_RE = re.compile(r"\s[|·]\s|\s[–—-]\s(?=[^–—-]*$)")


def clean_headline(title: str, host_label: str) -> str:
    """Drop a trailing ' | Site name' and return the headline, or '' when it is only a menu label or the site name."""
    title = re.sub(r"\s+", " ", title or "").strip()
    parts = [p for p in SITE_SUFFIX_RE.split(title) if p.strip()]
    if len(parts) > 1 and len(parts[-1]) <= 25 and norm(parts[-1]).replace(" ", "") in host_label:
        title = " | ".join(parts[:-1])
    words = norm(title).split()
    if not words or norm(title) in GENERIC_TITLES or norm(title).replace(" ", "") == host_label:
        return ""
    if len(words) < 2 and len(title) < 12 or PLACEHOLDER_RE.search(title):
        return ""
    return title[:200]


def news_url_ok(url: str, ctx: SiteContext) -> bool:
    """A post lives on the company's own site and is not the homepage, a section index, a listing page or a product page."""
    parts = urllib.parse.urlsplit(url)
    segments = [seg for seg in parts.path.split("/") if seg]
    if not url or not ctx.same_site(url) or not segments or "page=" in parts.query or "paged=" in parts.query or "comment" in parts.fragment:
        return False
    return segments[-1].lower() not in LISTING_SEGMENTS and not NOT_NEWS_PATH.search(parts.path)


def page_published(page: Page) -> tuple[str | None, bool]:
    """(publish date, is_article) read from the article page itself. dateModified, <updated> and sitemap lastmod are never used."""
    is_article = page.meta.get("og:type", "").lower() == "article"
    date = None
    for key in PUBLISHED_META:
        date = date or parse_date(page.meta.get(key))
    for item in page.jsonld:
        if ld_types(item) & ARTICLE_LD:
            is_article = True
            date = date or parse_date(str(item.get("datePublished") or ""))
    for tag in page.times:
        if PUBLISHED_TIME_HINT.search(tag.hint):
            date = date or parse_date(tag.datetime)
    return date, is_article


def _parse_feed(text: str) -> list[dict]:
    """Feed items with a real publish date: RSS pubDate, Atom published, dc:date. Atom <updated> is a modification date: ignored."""
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
                d.setdefault("link", child.attrib.get("href") or (child.text or "").strip())
            elif ctag in ("title", "pubDate", "published", "date", "guid", "description", "summary", "encoded"):
                d.setdefault(ctag, (child.text or "").strip())
        raw = d.get("pubDate") or d.get("published") or d.get("date")
        date = parse_date(raw)
        link = (d.get("link") or (d.get("guid") if str(d.get("guid", "")).startswith("http") else "") or "").strip()
        if d.get("title") and date and link:
            blurb = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", d.get("description") or d.get("summary") or d.get("encoded") or ""))
            items.append({"title": re.sub(r"\s+", " ", d["title"])[:200], "date": date, "url": link, "raw_date": raw, "blurb": blurb[:600]})
    return items


def extract_activity(fetcher: Fetcher, case: Case, ctx: SiteContext, legal_name: str) -> dict[str, Any]:
    """Dated news articles that name the company, from the site's own feed, else from article pages in its sitemap.
    Register change records are not news and are never emitted. Returns {'checked','count'}."""
    origin = ctx.origin
    label = re.sub(r"[^a-z0-9]", "", ctx.host.split(".")[0])
    legal_tokens = name_tokens(legal_name)
    brand_tokens = site_brand_tokens(ctx, legal_name)
    opened = 0
    chrome = {line.strip() for line in ctx.home.text.split("\n")}

    def article_text(page: Page) -> str:
        """The page's own text: lines that also sit on the homepage (menu, footer, <title> suffix) are site chrome, not the article."""
        return " ".join(line for line in page.text.split("\n") if line.strip() and line.strip() not in chrome and line.strip() != page.title)[:20000]

    shared = getattr(ctx, "shared", False)

    def names_company(text: str) -> bool:
        words = norm(text)
        if shared:  # group / manager site: only posts that name this exact company by its full legal name
            return name_phrase_in(words, legal_tokens) and len(legal_tokens) >= 2 or (len(legal_tokens) == 1 and norm(legal_name) in words)
        squashed = words.replace(" ", "")
        return (name_phrase_in(words, legal_tokens) or (bool(brand_tokens) and name_phrase_in(words, brand_tokens))
                or (len("".join(legal_tokens)) >= 5 and "".join(legal_tokens) in squashed))

    feeds: list[str] = []
    for _t, href, _title in ctx.home.alternates:
        feeds.append(ctx.home.abs(href))
    checked = False
    if not feeds:
        feeds = [urllib.parse.urljoin(origin, p) for p in ("feed/", "rss.xml", "feed.xml")]
    emitted = 0
    for url in feeds[:3]:
        if not ctx.same_site(url) or "comment" in urllib.parse.urlsplit(url).path.lower():
            continue
        resp = fetcher.get(url, case.ops, accept="application/rss+xml,application/atom+xml,application/xml,text/xml;q=0.9,*/*;q=0.5")
        if resp.status != 200:
            continue
        checked = True
        items = _parse_feed(resp.text)
        if len(items) >= 3 and len({i["raw_date"] for i in items}) == 1:
            continue  # one identical timestamp on every item is the feed's build time, not publish dates
        seen: set[str] = set()
        for item in sorted(items, key=lambda i: i["date"], reverse=True):
            title = clean_headline(item["title"], label)
            if not title or not news_url_ok(item["url"], ctx) or item["url"] in seen:
                continue
            if not names_company(title + " " + item["blurb"]):
                # the headline does not say; the article itself must (a bounded number of pages is opened for this)
                if opened >= 6:
                    continue
                opened += 1
                art = fetcher.get(item["url"], case.ops)
                art_page = _html_page(art)
                if art_page is None or not names_company(title + " " + article_text(art_page)):
                    continue
            seen.add(item["url"])
            eid = case.ev(resp.final_url, "company_owned", resp.retrieved_at, resp.sha256, f"{item['date']} {title}",
                          "rss_atom_item_publish_date", http_status=200, effective_date=item["date"])
            case.claim("public_activity", {"kind": "company_news", "title": title, "date": item["date"], "url": item["url"],
                                           "date_basis": "feed_publish_date"},
                       "available", [eid], key="news:" + item["url"], confidence=0.95, effective_date=item["date"], section="5")
            emitted += 1
            if emitted >= 6:
                break
        if emitted:
            return {"checked": True, "count": emitted}
    # Sitemap: it only says where articles are. Each article page must carry its own publish date and look like an article.
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
                if loc and NEWS_RE.search(urllib.parse.urlsplit(loc).path) and news_url_ok(loc.strip(), ctx):
                    entries.append((parse_date(mod) or "", loc.strip()))  # lastmod only orders which pages to open
    for _order, loc in sorted(set(entries), reverse=True)[:10]:
        if emitted >= 6:
            break
        resp = fetcher.get(loc, case.ops)
        page = _html_page(resp)
        if page is None:
            continue
        published, is_article = page_published(page)
        title = clean_headline(page.meta.get("og:title") or page.title, label)
        if not (published and is_article and title and names_company(title + " " + article_text(page))):
            continue
        eid = case.ev(resp.final_url, "company_owned", resp.retrieved_at, resp.sha256, f"{published} {title[:150]}",
                      "article_page_publish_date", http_status=200, effective_date=published)
        case.claim("public_activity", {"kind": "company_news", "title": title, "date": published, "url": resp.final_url,
                                       "date_basis": "article_publish_date"},
                   "available", [eid], key="news:" + resp.final_url, confidence=0.85, effective_date=published, section="5")
        emitted += 1
    return {"checked": checked, "count": emitted}
