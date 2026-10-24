"""Revision 2: website ownership (own vs parent group), social-link ownership, real dated news."""
import json

from signalpost_agent.htmlx import parse_html
from signalpost_agent.http import Response
from signalpost_agent.model import Case
from signalpost_agent.pipeline import summarize, web_phase
from signalpost_agent.web import (SiteContext, _parse_feed, clean_headline, extract_activity, extract_social, foreign_owner,
                                  page_published, resolve_website, social_mismatch)

ORG = "914778271"
NOW = "2026-10-20T10:00:00Z"


class SiteFake:
    """Serves fixed pages/feeds by exact URL; everything else is 404. Records every URL asked for."""

    def __init__(self, routes, sitemaps=()):
        self.routes, self.sitemaps, self.asked = routes, list(sitemaps), []

    def get(self, url, ops, **kwargs):
        self.asked.append(url)
        body = self.routes.get(url)
        if body is None:
            return Response(url, url, 404, retrieved_at=NOW)
        kind = "application/xml" if body.lstrip().startswith("<?xml") else "text/html"
        return Response(url, url, 200, body.encode(), content_type=kind, retrieved_at=NOW, sha256="h")

    def host_is_public(self, host):
        return True

    def robots_sitemaps(self, url):
        return self.sitemaps


def html(title="", body="", head=""):
    return f"<html><head><title>{title}</title>{head}</head><body>{body}</body></html>"


def ctx_for(url, page_html):
    page = parse_html(page_html, url)
    resp = Response(url, url, 200, page_html.encode(), content_type="text/html", retrieved_at=NOW, sha256="h")
    return SiteContext(url, page, resp)


def case_named(name):
    case = Case(ORG)
    eid = case.ev("https://data.brreg.no/x", "official_registry", NOW, "h", name, "t")
    case.claim("legal_name", name, "available", [eid], section="1")
    return case


def claims(case, field, availability="available"):
    return [c for c in case.claims if c["field"] == field and c["availability"] == availability]


# ----- 1. website: the company's own site, or a parent group's ----------------------------------------------
def test_copyright_naming_the_parent_entity_makes_it_a_group_site():
    page = parse_html(html("REMA 1000", "<p>Velkommen</p><footer>© 2026 REMA 1000 AS</footer>"), "https://rema.example/")
    owner, _span = foreign_owner(page, "REMA 1000 NORGE AS", ORG)
    assert owner == "REMA 1000 AS"
    assert foreign_owner(page, "REMA 1000 AS", ORG) is None  # the parent's own site is its own


def test_foreign_legal_form_on_impressum_page_is_another_owner_but_a_subsidiary_mention_is_not():
    impressum = parse_html(html("Legal notice", "<h1>Legal notice</h1><p>Company information</p><p>Color Line GmbH</p><p>Kiel</p>"),
                           "https://colorline.example/about-us/legal-notice")
    assert foreign_owner(impressum, "COLOR LINE AS", ORG)[0] == "Color Line GmbH"
    mention = parse_html(html("Contact", "<p>Our unit Color Line GmbH sells tickets.</p>"), "https://colorline.example/contact")
    assert foreign_owner(mention, "COLOR LINE AS", ORG) is None  # mentioned, not named as owner/publisher
    subsidiary = parse_html(html("Privacy", "<p>Data controller: Equinor Asset Management AS</p>"), "https://equinor.example/privacy")
    assert foreign_owner(subsidiary, "EQUINOR ASA", ORG) is None  # a longer name that starts with ours is a subsidiary


def test_own_organisation_number_next_to_the_name_keeps_the_site_own():
    page = parse_html(html("Personvern", "<p>Behandlingsansvarlig er KIWI AS, org.nr 914 778 271.</p>"), "https://kiwi.example/personvern")
    assert foreign_owner(page, "KIWI NORGE AS", ORG) is None


def _acme(page_body, *, routes=None, entity=None, name="ACME NORGE AS"):
    home = html("Acme", page_body, '<meta property="og:site_name" content="Acme Norge">')
    fetcher = SiteFake({"https://acme.example/": home, **(routes or {})})
    case = case_named(name)
    found = resolve_website(fetcher, case, {"hjemmeside": "acme.example", **(entity or {})}, name, None, max_guesses=0)
    return fetcher, case, found


def test_registry_declared_site_that_belongs_to_the_group_is_not_published():
    _f, case, found = _acme("<p>Acme</p><footer>© 2026 Acme AS</footer>")
    assert found is None
    claim = claims(case, "official_website", "ambiguous")[0]
    assert claim["value"]["site_scope"] == "group_site" and claim["value"]["owner"] == "Acme AS"
    assert claim["evidence_ids"] and not claims(case, "official_website")
    assert "not this company's own site" in claim["note"]


def test_site_with_own_org_number_and_no_other_owner_is_published_as_own():
    _f, case, found = _acme("<p>Org.nr 914 778 271</p><footer>© 2026 Acme Norge AS</footer>")
    assert found is not None
    value = claims(case, "official_website")[0]["value"]
    assert value["site_scope"] == "own" and value["identity_proof"] == "org_number_on_site"


def test_brand_owned_by_another_registered_entity_is_a_group_site():
    # No org number on the site, name + address match; the site calls itself "Acme", the exact name of another entity.
    home = html("Acme", "<p>Acme Norge, Storgata 1, 0155 Oslo</p>", '<meta property="og:site_name" content="Acme">')
    search = json.dumps({"_embedded": {"enheter": [{"organisasjonsnummer": "999888777", "navn": "ACME AS"},
                                                  {"organisasjonsnummer": ORG, "navn": "ACME NORGE AS"}]}})
    fetcher = SiteFake({"https://acme.example/": home, "https://data.brreg.no/enhetsregisteret/api/enheter?navn=acme&size=20": search})
    case = case_named("ACME NORGE AS")
    entity = {"epostadresse": "post@acme.example", "forretningsadresse": {"adresse": ["Storgata 1"], "postnummer": "0155"}}
    assert resolve_website(fetcher, case, entity, "ACME NORGE AS", None, max_guesses=0) is None
    value = claims(case, "official_website", "ambiguous")[0]["value"]
    assert value["site_scope"] == "group_site" and "999888777" in value["owner"]


def test_group_site_yields_no_social_news_or_jobs_for_the_company():
    feed = ('<?xml version="1.0"?><rss><channel>' + "".join(
        f"<item><title>Acme wins contract number {i}</title><link>https://acme.example/news/post-{i}/</link>"
        f"<pubDate>Mon, 0{i} Sep 2024 10:00:00 +0200</pubDate></item>" for i in (1, 2, 3)) + "</channel></rss>")
    body = '<footer>© 2026 Acme AS <a href="https://www.facebook.com/acme">f</a> <a href="/karriere">Karriere</a></footer>'
    fetcher = SiteFake({"https://acme.example/": html("Acme", body, '<link rel="alternate" type="application/rss+xml" href="/feed/">'),
                        "https://acme.example/feed/": feed})
    case = case_named("ACME NORGE AS")
    web_phase(fetcher, case, {"hjemmeside": "acme.example"}, None, use_nav=False, max_guesses=0)
    assert not claims(case, "company_profile") and not claims(case, "job_posting") and not claims(case, "public_activity")
    assert "https://acme.example/feed/" not in fetcher.asked and "https://acme.example/karriere" not in fetcher.asked
    text = " ".join(summarize(case)["unknowns"])
    assert "not this company's own site" in text and "https://acme.example/" in text


# ----- 2. social links ------------------------------------------------------------------------------------
def test_social_handles_must_carry_the_company_name_and_no_group_or_country_marker():
    anchors = ["colorline"]
    assert social_mismatch("facebook", "https://www.facebook.com/colorline", anchors, set()) is None
    assert social_mismatch("facebook", "https://www.facebook.com/colorlineno", anchors, set()) is None  # neutral country word
    assert "deutschland" in social_mismatch("instagram", "https://www.instagram.com/colorline_deutschland", anchors, set())
    assert "gmbh" in social_mismatch("youtube", "https://www.youtube.com/user/ColorLineGmbH", anchors, set())
    assert "group" in social_mismatch("linkedin", "https://www.linkedin.com/company/colorline-group", anchors, set())
    assert social_mismatch("linkedin", "https://www.linkedin.com/company/orkla-group", ["orklafoods"], set())  # the group's page
    assert social_mismatch("facebook", "https://www.facebook.com/webbyraaet", anchors, set())  # agency: no company name
    assert "opaque" in social_mismatch("linkedin", "https://www.linkedin.com/company/1234567", anchors, set())
    assert "theme" in social_mismatch("facebook", "https://www.facebook.com/wordpress", anchors, set())


def test_extract_social_keeps_only_profiles_that_belong_to_the_company():
    footer = ('<a href="https://www.facebook.com/fjordbygg">f</a> <a href="https://www.instagram.com/fjordbygg_sverige">i</a>'
              '<a href="https://www.linkedin.com/company/fjord-group-holding">l</a> <a href="https://www.facebook.com/webbyraaet">agency</a>'
              '<a href="https://www.linkedin.com/company/fjordbygg">l2</a> <a href="https://www.youtube.com/channel/UCabc123">y</a>')
    ctx = ctx_for("https://fjordbygg.example/", html("Fjord Bygg", footer))
    case = case_named("FJORD BYGG AS")
    extract_social(case, ctx, "FJORD BYGG AS")
    got = {c["value"]["platform"]: c["value"]["url"] for c in claims(case, "company_profile")}
    assert got == {"facebook": "https://www.facebook.com/fjordbygg", "linkedin": "https://www.linkedin.com/company/fjordbygg"}


def test_all_links_rejected_leaves_the_field_empty_and_says_why():
    ctx = ctx_for("https://fjordbygg.example/", html("Fjord Bygg", '<a href="https://www.instagram.com/fjordbygg_deutschland">i</a>'))
    case = case_named("FJORD BYGG AS")
    extract_social(case, ctx, "FJORD BYGG AS")
    assert not claims(case, "company_profile")
    note = claims(case, "company_profile", "not_available")[0]["note"]
    assert "fjordbygg_deutschland" in note and "deutschland" in note


def test_site_declared_profile_is_accepted_even_without_the_name_in_the_handle():
    ld = ('<script type="application/ld+json">{"@type":"Organization","name":"Fjord Bygg","sameAs":'
          '["https://www.facebook.com/byggmesterne.i.fjorden"]}</script>')
    page = html("Fjord Bygg", '<a href="https://www.facebook.com/byggmesterne.i.fjorden">f</a>' + ld)
    ctx = ctx_for("https://fjordbygg.example/", page)
    case = case_named("FJORD BYGG AS")
    extract_social(case, ctx, "FJORD BYGG AS")
    assert [c["value"]["url"] for c in claims(case, "company_profile")] == ["https://www.facebook.com/byggmesterne.i.fjorden"]


# ----- 3. news ------------------------------------------------------------------------------------------------
def _rss(*items):
    return '<?xml version="1.0"?><rss><channel><lastBuildDate>Tue, 20 Oct 2026 08:00:00 GMT</lastBuildDate>' + "".join(items) + "</channel></rss>"


def _item(title, link, date="Mon, 01 Sep 2025 10:00:00 +0200"):
    return f"<item><title>{title}</title><link>{link}</link><pubDate>{date}</pubDate></item>"


def _news(routes, home_head='<link rel="alternate" type="application/rss+xml" href="/feed/">', sitemaps=()):
    home = html("Fjord Bygg", "<p>hei</p>", home_head)
    fetcher = SiteFake({"https://fjordbygg.example/": home, **routes}, sitemaps)
    ctx = ctx_for("https://fjordbygg.example/", home)
    case = case_named("FJORD BYGG AS")
    result = extract_activity(fetcher, case, ctx)
    return case, result


def test_feed_items_need_a_real_post_url_and_a_headline_and_their_own_publish_date():
    feed = _rss(_item("Fjord Bygg vinner kontrakt i Bergen", "https://fjordbygg.example/nyheter/vinner-kontrakt/", "Mon, 06 Oct 2025 09:00:00 +0200"),
                _item("Nyheter", "https://fjordbygg.example/nyheter/"),  # menu label and section page
                _item("Alle nyheter", "https://fjordbygg.example/aktuelt/arkiv/"),
                _item("Vår nye pose", "https://fjordbygg.example/produkter/pose/"),  # product page
                _item("Annen side", "https://other.example/nyheter/ekstern/"),  # not the company's site
                _item("Hei, verden!", "https://fjordbygg.example/hei-verden/"),
                _item("Kommentar til noe av Anonym", "https://fjordbygg.example/post/#comment-4"))
    case, result = _news({"https://fjordbygg.example/feed/": feed})
    got = claims(case, "public_activity")
    assert [c["value"]["title"] for c in got] == ["Fjord Bygg vinner kontrakt i Bergen"]
    assert got[0]["value"]["date"] == "2025-10-06" and got[0]["value"]["date_basis"] == "feed_publish_date"
    assert result == {"checked": True, "count": 1}


def test_atom_updated_is_not_a_publish_date():
    atom = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><updated>2026-10-20T08:00:00Z</updated>'
            '<entry><title>Fjord Bygg bygger nytt bygg</title><link href="https://fjordbygg.example/nyheter/nytt-bygg/"/>'
            "<updated>2026-10-19T08:00:00Z</updated></entry>"
            '<entry><title>Fjord Bygg flytter til nye lokaler</title><link href="https://fjordbygg.example/nyheter/flytter/"/>'
            "<published>2025-03-02T08:00:00Z</published><updated>2026-10-19T08:00:00Z</updated></entry></feed>")
    items = _parse_feed(atom)
    assert [(i["title"], i["date"]) for i in items] == [("Fjord Bygg flytter til nye lokaler", "2025-03-02")]


def test_one_identical_timestamp_on_every_item_is_a_feed_build_date():
    same = "Tue, 20 Oct 2026 08:00:00 GMT"
    feed = _rss(*[_item(f"Fjord Bygg nyhet nummer {i}", f"https://fjordbygg.example/nyheter/n{i}/", same) for i in range(4)])
    case, result = _news({"https://fjordbygg.example/feed/": feed})
    assert not claims(case, "public_activity") and result["count"] == 0


def test_comment_feeds_are_skipped():
    head = '<link rel="alternate" type="application/rss+xml" href="/comments/feed/">'
    feed = _rss(_item("Kommentar til noe viktig", "https://fjordbygg.example/viktig/"))
    case, _ = _news({"https://fjordbygg.example/comments/feed/": feed}, home_head=head)
    assert not claims(case, "public_activity")


SITEMAP = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(
    f"<url><loc>https://fjordbygg.example/{p}</loc><lastmod>2026-10-19</lastmod></url>" for p in (
        "nyheter/", "nyheter/ny-hall-i-bergen/", "nyheter/uten-dato/", "nyheter/arkiv/", "nyheter/om-oss-tekst/", "produkter/pose/")) + "</urlset>")


def test_sitemap_lastmod_is_never_a_publish_date():
    dated = html("Ny hall i Bergen | Fjord Bygg", "<p>x</p>", '<meta property="og:type" content="article">'
                 '<meta property="og:title" content="Ny hall i Bergen | Fjord Bygg">'
                 '<meta property="article:published_time" content="2025-08-14T10:00:00+02:00">'
                 '<meta property="article:modified_time" content="2026-10-19T10:00:00+02:00">')
    undated = html("Uten dato", "<p>x</p>", '<meta property="og:type" content="article"><meta property="og:title" content="Artikkel uten dato i det hele tatt">')
    archive = html("Arkiv over alle nyheter", "<p>x</p>", '<meta property="og:type" content="website">'
                   '<meta property="article:published_time" content="2025-01-01T10:00:00+02:00">')
    case, result = _news({"https://fjordbygg.example/sitemap.xml": SITEMAP, "https://fjordbygg.example/nyheter/ny-hall-i-bergen/": dated,
                          "https://fjordbygg.example/nyheter/uten-dato/": undated, "https://fjordbygg.example/nyheter/arkiv/": archive,
                          "https://fjordbygg.example/nyheter/om-oss-tekst/": archive}, home_head="")
    got = claims(case, "public_activity")
    assert [(c["value"]["title"], c["value"]["date"], c["value"]["date_basis"]) for c in got] == [
        ("Ny hall i Bergen", "2025-08-14", "article_publish_date")]  # modified time, lastmod 2026-10-19 and undated pages are not used
    assert result["count"] == 1


def test_page_published_reads_only_publish_markers():
    page = parse_html(html("x", '<time datetime="2024-01-01">Oppdatert</time><time itemprop="datePublished" datetime="2023-05-05">5. mai</time>',
                           '<meta property="article:modified_time" content="2026-01-01T00:00:00Z">'), "https://x.example/")
    assert page_published(page) == ("2023-05-05", False)
    assert page_published(parse_html(html("x", "<time datetime='2024-01-01'>x</time>"), "https://x.example/")) == (None, False)


def test_headlines_reject_menu_labels_and_strip_the_site_suffix():
    assert clean_headline("All News", "akersolutions") == "" and clean_headline("Nyheter", "x") == ""
    assert clean_headline("Aker Solutions", "akersolutions") == ""  # the site name alone
    assert clean_headline("RE-Source 2021 | Statkraft", "statkraft") == "RE-Source 2021"
    assert clean_headline("Endelig sertifisert", "melbuhotell") == "Endelig sertifisert"


# ----- 4. job postings: menu labels are not postings ------------------------------------------------------
def _jobs(routes, careers_body):
    from signalpost_agent.web import extract_jobs
    home = html("Fjord Bygg", '<a href="/karriere">Karriere</a>')
    fetcher = SiteFake({"https://fjordbygg.example/": home, "https://fjordbygg.example/karriere": html("Karriere", careers_body), **routes})
    case = case_named("FJORD BYGG AS")
    result = extract_jobs(fetcher, case, ctx_for("https://fjordbygg.example/", home), "FJORD BYGG AS")
    return case, result, fetcher


def test_careers_menu_links_are_not_job_postings():
    menu = ('<a href="/karriere/what-we-offer">What we offer</a> <a href="/karriere/graduate-programme">Graduate programme</a>'
            '<a href="/karriere/faq">Frequently asked questions</a> <a href="/karriere/stillinger/ansatte-historier">Misak ville jobbe gratis for oss</a>')
    plain = html("Graduate programme", "<p>Apply for our programme</p>")
    case, result, _ = _jobs({"https://fjordbygg.example/karriere/graduate-programme": plain,
                             "https://fjordbygg.example/karriere/stillinger/ansatte-historier": plain}, menu)
    assert not claims(case, "job_posting") and result["checked"] and result["count"] == 0


def test_linked_ad_page_with_jobposting_json_ld_is_a_posting():
    ad = html("Tømrer", '<script type="application/ld+json">{"@type":"JobPosting","title":"Tømrer","datePosted":"2026-09-01",'
                        '"hiringOrganization":{"name":"Fjord Bygg AS"}}</script>')
    other_org = html("Fremmed", '<script type="application/ld+json">{"@type":"JobPosting","title":"Sjåfør","hiringOrganization":{"name":"Annet Selskap AS"}}</script>')
    links = '<a href="/stillinger/tomrer">Tømrer til Bergen</a> <a href="/stillinger/sjafor">Sjåfør til Oslo</a>'
    case, _r, _f = _jobs({"https://fjordbygg.example/stillinger/tomrer": ad, "https://fjordbygg.example/stillinger/sjafor": other_org}, links)
    got = claims(case, "job_posting")
    assert [(c["value"]["title"], c["value"]["date_posted"]) for c in got] == [("Tømrer", "2026-09-01")]
    assert case.evidence[got[0]["evidence_ids"][0]]["source_url"] == "https://fjordbygg.example/stillinger/tomrer"


def test_ats_hosted_ad_links_count_but_the_ats_root_does_not():
    links = ('<a href="https://fjordbygg.webcruiter.no/">Se alle våre stillinger</a>'
             '<a href="https://fjordbygg.webcruiter.no/Main/Recruit/Public/4711">Prosjektleder bygg</a>')
    case, _r, _f = _jobs({}, links)
    assert [c["value"]["title"] for c in claims(case, "job_posting")] == ["Prosjektleder bygg"]
