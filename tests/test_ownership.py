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
        if kwargs.get("data") is not None:
            self.posted = getattr(self, "posted", []) + [(url, kwargs["data"])]
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


def test_registry_declared_group_site_is_kept_but_marked_shared():
    _f, case, found = _acme("<p>Acme</p><footer>© 2026 Acme AS</footer>")
    assert found["shared"] is True
    value = claims(case, "official_website")[0]["value"]
    assert value["url"] == "https://acme.example/" and value["site_scope"] == "registry_declared_shared"
    assert "Acme AS" in value["scope_basis"]


def test_group_site_found_without_registry_declaration_is_not_published():
    home = html("Acme", "<p>Acme Norge AS, Storgata 1, 0155 Oslo</p><footer>© 2026 Acme AS</footer>")
    fetcher = SiteFake({"https://acme.example/": home})
    case = case_named("ACME NORGE AS")
    entity = {"epostadresse": "post@acme.example", "forretningsadresse": {"adresse": ["Storgata 1"], "postnummer": "0155"}}
    assert resolve_website(fetcher, case, entity, "ACME NORGE AS", None, max_guesses=0) is None
    assert not claims(case, "official_website")


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
    claim = claims(case, "official_website", "ambiguous")[0]
    assert claim["value"] is None and "not this company's own site" in claim["note"]


def test_registry_group_site_yields_no_brand_social_news_or_jobs_that_do_not_name_the_company():
    feed = ('<?xml version="1.0"?><rss><channel>' + "".join(
        f"<item><title>Acme wins contract number {i}</title><link>https://acme.example/news/post-{i}/</link>"
        f"<pubDate>Mon, 0{i} Sep 2024 10:00:00 +0200</pubDate></item>" for i in (1, 2, 3)) + "</channel></rss>")
    body = '<footer>© 2026 Acme AS <a href="https://www.facebook.com/acme">f</a> <a href="/karriere">Karriere</a></footer>'
    fetcher = SiteFake({"https://acme.example/": html("Acme", body, '<link rel="alternate" type="application/rss+xml" href="/feed/">'),
                        "https://acme.example/feed/": feed})
    case = case_named("ACME NORGE AS")
    web_phase(fetcher, case, {"hjemmeside": "acme.example"}, None, max_guesses=0)
    assert claims(case, "official_website")[0]["value"]["site_scope"] == "registry_declared_shared"
    assert not claims(case, "company_profile") and not claims(case, "job_posting") and not claims(case, "public_activity")
    assert not claims(case, "public_brand") and not claims(case, "company_description")


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
    ctx = ctx_for("https://fjordbygg.example/", html("Fjord Bygg", "<footer>" + footer + "</footer>"))
    case = case_named("FJORD BYGG AS")
    extract_social(case, ctx, "FJORD BYGG AS")
    got = {c["value"]["platform"]: c["value"]["url"] for c in claims(case, "company_profile")}
    assert got == {"facebook": "https://www.facebook.com/fjordbygg", "linkedin": "https://www.linkedin.com/company/fjordbygg"}


def test_all_links_rejected_leaves_the_field_empty_and_says_why():
    ctx = ctx_for("https://fjordbygg.example/", html("Fjord Bygg", '<footer><a href="https://www.instagram.com/fjordbygg_deutschland">i</a></footer>'))
    case = case_named("FJORD BYGG AS")
    extract_social(case, ctx, "FJORD BYGG AS")
    assert not claims(case, "company_profile")
    note = claims(case, "company_profile", "not_available")[0]["note"]
    assert "fjordbygg_deutschland" in note and "deutschland" in note and note.startswith("Social links on the site were not published")


def test_site_declared_profile_is_accepted_even_without_the_name_in_the_handle():
    ld = ('<script type="application/ld+json">{"@type":"Organization","name":"Fjord Bygg","sameAs":'
          '["https://www.facebook.com/byggmesterne.i.fjorden"]}</script>')
    page = html("Fjord Bygg", '<footer><a href="https://www.facebook.com/byggmesterne.i.fjorden">f</a></footer>' + ld)
    ctx = ctx_for("https://fjordbygg.example/", page)
    case = case_named("FJORD BYGG AS")
    extract_social(case, ctx, "FJORD BYGG AS")
    assert [c["value"]["url"] for c in claims(case, "company_profile")] == ["https://www.facebook.com/byggmesterne.i.fjorden"]


# ----- 3. news ------------------------------------------------------------------------------------------------
def _rss(*items):
    return '<?xml version="1.0"?><rss><channel><lastBuildDate>Tue, 20 Oct 2026 08:00:00 GMT</lastBuildDate>' + "".join(items) + "</channel></rss>"


def _item(title, link, date="Mon, 01 Sep 2025 10:00:00 +0200"):
    return f"<item><title>{title}</title><link>{link}</link><pubDate>{date}</pubDate></item>"


def _news(routes, home_head='<link rel="alternate" type="application/rss+xml" href="/feed/">', sitemaps=(), home_body="<p>hei</p>"):
    home = html("Fjord Bygg", home_body, home_head)
    fetcher = SiteFake({"https://fjordbygg.example/": home, **routes}, sitemaps)
    ctx = ctx_for("https://fjordbygg.example/", home)
    case = case_named("FJORD BYGG AS")
    result = extract_activity(fetcher, case, ctx, "FJORD BYGG AS")
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
    dated = html("Ny hall i Bergen | Fjord Bygg", "<p>Fjord Bygg har reist en ny hall i Bergen.</p>", '<meta property="og:type" content="article">'
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
    ad = html("Prosjektleder", '<time itemprop="datePosted" datetime="2026-09-20">20. sept</time>')
    case, _r, _f = _jobs({"https://fjordbygg.webcruiter.no/Main/Recruit/Public/4711": ad}, links)
    assert [c["value"]["title"] for c in claims(case, "job_posting")] == ["Prosjektleder bygg"]


# ----- 5. only articles that name the company; register change records are not news ---------------------------
def test_feed_post_must_name_the_company_in_headline_description_or_article():
    feed = _rss(
        _item("Fjord Bygg vinner kontrakt i Bergen", "https://fjordbygg.example/nyheter/a/"),  # headline names it
        "<item><title>Ny hall reist i havneomradet</title><link>https://fjordbygg.example/nyheter/b/</link><pubDate>Mon, 02 Jun 2025 10:00:00 +0200</pubDate>"
        "<description>&lt;p&gt;Fjord Bygg leverte hallen.&lt;/p&gt;</description></item>",  # description names it
        _item("Tips til varmepumpe", "https://fjordbygg.example/nyheter/c/", "Mon, 03 Jun 2025 10:00:00 +0200"),  # article body names it
        _item("Tips til sparing", "https://fjordbygg.example/nyheter/d/", "Mon, 04 Jun 2025 10:00:00 +0200"),  # names nobody
        _item("Hva er en kontrakt", "https://fjordbygg.example/nyheter/e/", "Mon, 05 Jun 2025 10:00:00 +0200"))  # only site chrome names it
    menu = "<nav>Fjord Bygg AS</nav>"
    case, _ = _news({"https://fjordbygg.example/feed/": feed,
                     "https://fjordbygg.example/nyheter/c/": html("Tips | Fjord Bygg", menu + "<p>Fjord Bygg anbefaler varmepumpe.</p>"),
                     "https://fjordbygg.example/nyheter/d/": html("Tips | Fjord Bygg", menu + "<p>Spar strom hjemme.</p>"),
                     "https://fjordbygg.example/nyheter/e/": html("Hva | Fjord Bygg", menu + "<p>Kontrakt betyr avtale.</p>")},
                    home_body=menu + "<p>hei</p>")
    titles = sorted(c["value"]["title"] for c in claims(case, "public_activity"))
    assert titles == ["Fjord Bygg vinner kontrakt i Bergen", "Ny hall reist i havneomradet", "Tips til varmepumpe"]


def test_sitemap_article_that_does_not_name_the_company_is_dropped():
    other = html("Generell artikkel | Fjord Bygg", "<p>Om vaer og vind.</p>", '<meta property="og:type" content="article">'
                 '<meta property="article:published_time" content="2025-08-14T10:00:00+02:00">')
    sm = SITEMAP.replace("nyheter/ny-hall-i-bergen/", "nyheter/generell/")
    case, result = _news({"https://fjordbygg.example/sitemap.xml": sm, "https://fjordbygg.example/nyheter/generell/": other}, home_head="")
    assert not claims(case, "public_activity") and result["count"] == 0


def test_register_change_records_are_never_emitted_and_viewer_shows_date_next_to_each_article():
    from signalpost_agent import official
    from signalpost_agent.htmlreport import _news_list
    from signalpost_agent.pipeline import build_envelope
    assert not hasattr(official, "fetch_updates")
    feed = _rss(_item("Fjord Bygg vinner kontrakt i Bergen", "https://fjordbygg.example/nyheter/a/", "Mon, 06 Oct 2025 09:00:00 +0200"))
    case, _ = _news({"https://fjordbygg.example/feed/": feed})
    env = build_envelope(case, {"run_id": "r", "started_at": "a", "completed_at": "b"}, None, True, True)
    assert {c["value"]["kind"] for c in env["claims"] if c["field"] == "public_activity" and c["availability"] == "available"} == {"company_news"}
    news = _news_list(env)
    assert '<time datetime="2025-10-06">2025-10-06</time> <a href="https://fjordbygg.example/nyheter/a/"' in news
    assert "Endring" not in news and "Ukjent" not in news and "Register entry" not in news
    assert _news_list({"claims": [{"field": "public_activity", "availability": "available",
                                   "value": {"kind": "registry_update", "title": "Register entry Endring", "date": "2025-01-01"}}]}) == ""


# ----- 6. step 2: social links only from the footer, jobs only from the site's own careers page ----------------
FB, IG = "https://www.facebook.com/fjordbygg", "https://www.instagram.com/fjordbygg"


def _social(page_body):
    ctx = ctx_for("https://fjordbygg.example/", html("Fjord Bygg", page_body))
    case = case_named("FJORD BYGG AS")
    extract_social(case, ctx, "FJORD BYGG AS")
    return case


def test_social_links_anywhere_on_the_verified_homepage_count_when_the_handle_is_the_company_name():
    case = _social(f'<header><a href="{IG}">ig</a></header><main><a href="{FB}">fb</a></main>'
                   '<footer><a href="https://www.linkedin.com/company/fjordbygg">in</a></footer>')
    assert sorted(c["value"]["platform"] for c in claims(case, "company_profile")) == ["facebook", "instagram", "linkedin"]


def test_manager_or_sister_handles_are_rejected_even_on_the_verified_site():
    case = _social('<a href="https://instagram.com/boligbyggelaget_usbl">i</a><a href="https://www.facebook.com/fjordbyggnorge">f</a>'
                   '<a href="https://www.linkedin.com/company/usbl">l</a>')
    assert not claims(case, "company_profile")


def test_nothing_found_says_so():
    none = claims(_social('<footer><p>Org.nr 914 778 271</p></footer>'), "company_profile", "not_available")
    assert len(none) == 1 and none[0]["note"].startswith("No social media links found on the homepage of https://fjordbygg.example/")


def test_wikidata_profiles_are_not_a_social_source():
    from signalpost_agent import external
    assert not hasattr(external, "wikidata_profiles")


def _phase(routes, name="FJORD BYGG AS", registry="fjordbygg.example"):
    fetcher = SiteFake(routes)
    case = case_named(name)
    web_phase(fetcher, case, {"hjemmeside": registry}, None, max_guesses=0)
    return case, fetcher


def test_no_careers_page_on_the_site_or_on_a_linked_job_platform_is_reported_honestly():
    home = html("Fjord Bygg", '<p>Org.nr 914 778 271</p><a href="https://careers.example.org/karriere">Karriere</a>')  # not a job platform
    case, fetcher = _phase({"https://fjordbygg.example/": home})
    assert "https://careers.example.org/karriere" not in fetcher.asked
    assert not claims(case, "job_posting")
    note = claims(case, "hiring_status", "not_available")[0]["note"]
    assert note == "No careers/jobs page was found on the company's own website https://fjordbygg.example/"


def test_careers_page_on_a_job_platform_linked_from_the_site_is_followed():
    home = html("Fjord Bygg", '<p>Org.nr 914 778 271</p><a href="https://fjordbygg.teamtailor.com/jobs">Ledige stillinger</a>')
    listing = html("Jobs", '<a href="/jobs/1001-tomrer">Tømrer til Bergen</a><a href="/jobs/1002-prosjektleder">Prosjektleder bygg</a>'
                           '<a href="/jobs">Se alle våre stillinger</a><a href="https://www.teamtailor.com/en/careers-site/">Powered by Teamtailor</a>'
                           '<a href="https://other.teamtailor.com/jobs/9-annen">Stilling hos en annen bedrift</a>')
    ad1 = html("Tømrer", '<script type="application/ld+json">{"@type":"JobPosting","title":"Tømrer til Bergen","datePosted":"2026-10-01",'
                         '"hiringOrganization":{"name":"Fjord Bygg AS"}}</script>')
    ad2 = html("Prosjektleder", '<time itemprop="datePosted" datetime="2026-09-20">20. sept</time><h1>Prosjektleder bygg</h1>')
    case, fetcher = _phase({"https://fjordbygg.example/": home, "https://fjordbygg.teamtailor.com/jobs": listing,
                            "https://fjordbygg.teamtailor.com/jobs/1001-tomrer": ad1, "https://fjordbygg.teamtailor.com/jobs/1002-prosjektleder": ad2})
    got = sorted((c["value"]["title"], c["value"]["date_posted"], c["value"]["url"]) for c in claims(case, "job_posting"))
    assert got == [("Prosjektleder bygg", "2026-09-20", "https://fjordbygg.teamtailor.com/jobs/1002-prosjektleder"),
                   ("Tømrer til Bergen", "2026-10-01", "https://fjordbygg.teamtailor.com/jobs/1001-tomrer")]
    assert not any("teamtailor.com/en/" in u or "other.teamtailor" in u for u in fetcher.asked)
    assert claims(case, "hiring_status")[0]["value"]["postings_found"] == 2


def test_platform_ad_without_date_is_not_published():
    home = html("Fjord Bygg", '<p>Org.nr 914 778 271</p><a href="https://fjordbygg.webcruiter.no/">Karriere</a>')
    listing = html("Ledige stillinger", '<a href="/Main/Recruit/Public/4711">Prosjektleder bygg</a><a href="/Main/Recruit/Public/4712">Bilmekaniker</a>')
    case, _ = _phase({"https://fjordbygg.example/": home, "https://fjordbygg.webcruiter.no/": listing})
    got = sorted((c["value"]["title"], c["value"]["date_posted"]) for c in claims(case, "job_posting"))
    assert got == []  # undated: not a verified open posting


def test_platform_ads_of_another_employer_are_not_published():
    home = html("Fjord Bygg", '<p>Org.nr 914 778 271</p><a href="https://jobs.jobylon.com/companies/fjordbygg">Karriere</a>')
    listing = html("Jobs", '<a href="/jobs/55-sjafor">Sjåfør til Oslo</a>')
    ad = html("Sjåfør", '<script type="application/ld+json">{"@type":"JobPosting","title":"Sjåfør","hiringOrganization":{"name":"Annet Selskap AS"}}</script>')
    case, _ = _phase({"https://fjordbygg.example/": home, "https://jobs.jobylon.com/companies/fjordbygg": listing, "https://jobs.jobylon.com/jobs/55-sjafor": ad})
    assert not claims(case, "job_posting")  # the ad's own structured data names another employer


def test_own_careers_page_is_used_and_an_empty_one_is_reported_as_checked():
    home = html("Fjord Bygg", '<p>Org.nr 914 778 271</p><a href="/karriere">Karriere</a>')
    ld = '<script type="application/ld+json">{"@type":"JobPosting","title":"Tømrer","datePosted":"2026-10-01","hiringOrganization":{"name":"Fjord Bygg AS"}}</script>'
    case, _ = _phase({"https://fjordbygg.example/": home, "https://fjordbygg.example/karriere": html("Karriere", ld)})
    assert [c["value"]["title"] for c in claims(case, "job_posting")] == ["Tømrer"]
    empty, _ = _phase({"https://fjordbygg.example/": home, "https://fjordbygg.example/karriere": html("Karriere", "<p>Ingen ledige stillinger</p>")})
    assert not claims(empty, "job_posting")
    note = claims(empty, "hiring_status", "not_available")[0]["note"]
    assert note.startswith("Checked, no job postings found on the company's careers page https://fjordbygg.example/karriere")


def test_no_confirmed_website_says_so_for_social_and_jobs():
    case = case_named("FJORD BYGG AS")
    web_phase(SiteFake({}), case, {}, None, max_guesses=0)
    assert "no footer to read" in claims(case, "company_profile", "not_available")[0]["note"]
    assert "no careers/jobs page could be checked" in claims(case, "hiring_status", "not_available")[0]["note"]


def test_own_careers_page_that_only_points_to_a_job_platform_is_followed_one_hop():
    home = html("Fjord Bygg", '<p>Org.nr 914 778 271</p><a href="/karriere">Karriere</a>')
    careers = html("Karriere", '<a href="https://fjordbygg.jobylon.com/jobs">See our open positions</a>')
    listing = html("Jobs", '<a href="/jobs/77-tomrer">Tømrer til Bergen</a>')
    ad = html("Tømrer", '<time itemprop="datePosted" datetime="2026-10-02">2. okt</time>')
    case, fetcher = _phase({"https://fjordbygg.example/": home, "https://fjordbygg.example/karriere": careers,
                            "https://fjordbygg.jobylon.com/jobs": listing, "https://fjordbygg.jobylon.com/jobs/77-tomrer": ad})
    assert [c["value"]["title"] for c in claims(case, "job_posting")] == ["Tømrer til Bergen"]
    # a platform page that shows no postings in plain HTML is reported as checked, naming both pages
    case2, _ = _phase({"https://fjordbygg.example/": home, "https://fjordbygg.example/karriere": careers,
                       "https://fjordbygg.jobylon.com/jobs": html("Jobs", "<div id=app></div>")})
    note = claims(case2, "hiring_status", "not_available")[0]["note"]
    assert "https://fjordbygg.example/karriere" in note and "https://fjordbygg.jobylon.com/jobs" in note


def test_workday_career_site_is_read_through_its_json_endpoint_for_the_employer_only():
    home = html("Fjord Bygg", '<p>Org.nr 914 778 271</p><a href="/karriere">Karriere</a>')
    careers = html("Karriere", '<a href="https://fjordbygg.wd3.myworkdayjobs.com/en-US/FB">See our open positions</a>')
    base = "https://fjordbygg.wd3.myworkdayjobs.com/wday/cxs/fjordbygg/FB"
    listing = json.dumps({"total": 3, "jobPostings": [
        {"title": "Tømrer", "externalPath": "/job/Bergen/Tomrer_JR1", "locationsText": "Bergen", "postedOn": "Posted 5 Days Ago"},
        {"title": "Sjåfør", "externalPath": "/job/Oslo/Sjafor_JR2", "locationsText": "Oslo", "postedOn": "Posted Today"},
        {"title": "Uten arbeidsgiver", "externalPath": "/job/Oslo/X_JR3", "locationsText": "Oslo", "postedOn": "Posted Today"}]})
    def detail(title, start, org):
        info = {"title": title, "startDate": start, "endDate": "2026-12-01", "externalUrl": f"https://fjordbygg.wd3.myworkdayjobs.com/FB/job/{title}"}
        return json.dumps({"jobPostingInfo": info, **({"hiringOrganization": {"name": org}} if org else {})})
    routes = {"https://fjordbygg.example/": home, "https://fjordbygg.example/karriere": careers,
              "https://fjordbygg.wd3.myworkdayjobs.com/en-US/FB": html("Workday", "<div id=app></div>"), base + "/jobs": listing,
              base + "/job/Bergen/Tomrer_JR1": detail("Tomrer", "2026-11-03", "1200 Fjord Bygg AS"),
              base + "/job/Oslo/Sjafor_JR2": detail("Sjafor", "2026-11-01", "Fjord Bygg Eiendom AS"),  # a sister company on the same tenant
              base + "/job/Oslo/X_JR3": detail("Uten", "2026-11-01", None)}
    case, fetcher = _phase(routes)
    got = [(c["value"]["title"], c["value"]["date_posted"], c["value"]["valid_through"], c["value"]["url"]) for c in claims(case, "job_posting")]
    assert got == [("Tomrer", "2026-11-03", "2026-12-01", "https://fjordbygg.wd3.myworkdayjobs.com/FB/job/Tomrer")]
    url, body = fetcher.posted[0]
    assert url == base + "/jobs" and json.loads(body)["searchText"] == ""  # tenant and site derived from the careers link (locale skipped)
    ev = case.evidence[claims(case, "job_posting")[0]["evidence_ids"][0]]
    assert ev["source_url"] == base + "/job/Bergen/Tomrer_JR1" and ev["extraction_method"] == "workday_cxs_json"


def test_workday_endpoint_failure_leaves_jobs_empty_and_reported():
    home = html("Fjord Bygg", '<p>Org.nr 914 778 271</p><a href="/karriere">Karriere</a>')
    careers = html("Karriere", '<a href="https://fjordbygg.wd3.myworkdayjobs.com/FB">See our open positions</a>')
    case, _ = _phase({"https://fjordbygg.example/": home, "https://fjordbygg.example/karriere": careers,
                      "https://fjordbygg.wd3.myworkdayjobs.com/FB": html("Workday", "<div id=app></div>")})
    assert not claims(case, "job_posting")
    assert "https://fjordbygg.wd3.myworkdayjobs.com/FB" in claims(case, "hiring_status", "not_available")[0]["note"]


# ----- 7. step 3: viewer (find, compare, check a source; laptop and phone) -----------------------------------
def _viewer_envs():
    from signalpost_agent.pipeline import build_envelope
    run = {"run_id": "r", "started_at": "a", "completed_at": "2026-11-04T10:00:00Z"}
    feed = _rss(_item("Fjord Bygg vinner kontrakt i Bergen", "https://fjordbygg.example/nyheter/a/", "Mon, 06 Oct 2025 09:00:00 +0200"))
    case, _ = _news({"https://fjordbygg.example/feed/": feed})
    news_env = build_envelope(case, run, None, True, True)
    bare = {"organisation_number": "999999999", "state": "failed", "claims": [], "evidence": []}
    return news_env, bare


def test_viewer_has_search_sort_filter_empty_message_and_phone_meta(tmp_path):
    from signalpost_agent.htmlreport import write_html
    news_env, bare = _viewer_envs()
    write_html(tmp_path, [news_env, bare])
    page = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert 'name="viewport"' in page and 'id="q"' in page and 'type="search"' in page
    for option in ("Name A–Z", "Revenue, high to low", "Revenue, low to high", "Accounts year, newest first", "Employees, most first"):
        assert option in page
    for key in ("site", "news", "jobs", "latest"):
        assert f'class="flt" data-key="{key}"' in page
    assert "No companies match your search." in page and 'id="emptyreset"' in page
    assert "@media (max-width:640px)" in page and "min-height:44px" in page  # phone layout and tap-sized controls
    assert 'id="tray"' in page and 'id="diffonly"' in page and 'role="dialog"' in page


def test_viewer_unknown_numbers_are_empty_never_zero():
    from signalpost_agent.htmlreport import _sort_keys
    news_env, bare = _viewer_envs()
    keys = _sort_keys(bare)
    assert keys["rev"] == "" and keys["year"] == "" and keys["emp"] == "" and keys["news"] == "0"
    assert _sort_keys(news_env)["news"] == "1"


def test_every_fact_row_has_a_date_and_a_tappable_source(tmp_path):
    from signalpost_agent.htmlreport import _card, _claim_date
    news_env, _ = _viewer_envs()
    card = _card(news_env)
    facts = card.split('<tbody>')[1].split("</tbody>")[0]
    assert facts.count("<tr>") == len(news_env["claims"]) == facts.count('data-label="Date"')
    ev = {"e1": {"retrieved_at": "2026-11-04T10:00:00Z"}}
    assert _claim_date({"effective_date": "2025-10-06T00:00:00Z", "evidence_ids": ["e1"]}, ev, "2026-01-01") == ("dated", "2025-10-06")
    assert _claim_date({"reporting_period": "2025-01-01..2025-12-31", "evidence_ids": ["e1"]}, ev, "2026-01-01") == ("period", "2025-01-01..2025-12-31")
    assert _claim_date({"evidence_ids": ["e1"]}, ev, "2026-01-01") == ("read", "2026-11-04")
    assert _claim_date({"evidence_ids": []}, ev, "2026-01-01") == ("checked", "2026-01-01")  # not available: still dated
    assert '<time datetime="2025-10-06">2025-10-06</time> <a href="https://fjordbygg.example/nyheter/a/" target="_blank" rel="noopener">' in card
    assert 'class="src" href="https://' in card and 'target="_blank"' in card


def test_viewer_jobs_list_shows_date_or_says_it_is_not_shown():
    from signalpost_agent.htmlreport import _jobs_list
    env = {"claims": [{"field": "job_posting", "availability": "available", "value": {"title": "Tømrer", "date_posted": "2026-11-03", "url": "https://x.example/j/1"}},
                      {"field": "job_posting", "availability": "available", "value": {"title": "Sjåfør", "date_posted": None, "url": "https://x.example/j/2"}}]}
    out = _jobs_list(env)
    assert "<time>2026-11-03</time>" in out and "<time>date not shown</time>" in out and out.index("Tømrer") < out.index("Sjåfør")


# ----- 8. viewer UX round: explicit "no ..." badges, compact cards, labelled sources ------------------------------
def test_badges_say_what_is_missing_and_only_present_things_are_green():
    from signalpost_agent.htmlreport import _badges
    none = _badges({"site": "0", "news": "0", "jobs": "0", "latest": "0", "year": "", "rev": "", "emp": ""})
    assert none.count("badge no") == 4 and "✕ no website" in none and "✕ no news" in none and "✕ no jobs" in none and "✕ no accounts" in none
    assert 'class="badge">' not in none  # nothing green when nothing is present
    some = _badges({"site": "1", "news": "3", "jobs": "1", "latest": "1", "year": "2025"})
    assert "✓ website" in some and "✓ 3 news" in some and "✓ 1 job<" in some and "✓ latest accounts" in some and "badge no" not in some
    lagging = _badges({"site": "1", "news": "0", "jobs": "2", "latest": "0", "year": "2024"})
    assert "✓ 2 jobs" in lagging and "✕ accounts not the latest (FY2024)" in lagging and "✕ no news" in lagging


def test_card_is_compact_by_default_with_details_behind_a_button():
    from test_core import _accounts, _run
    from signalpost_agent.htmlreport import _card
    env = _run({"sisteInnsendteAarsregnskap": "2025"}, _accounts(2024, 2025, revenue={2024: 200, 2025: 150}))
    card = _card(env)
    head, rest = card.split('<div class="actions">')
    assert "Key figures, FY2025" in head and "Operating revenue" in head and "Annual result" in head and "Registered employees" in head
    assert "Compare" in head and "✕ no website" in head
    assert 'class="toggle" aria-expanded="false" aria-controls="more-914778271">Show details</button>' in rest
    more = rest.split('<div class="more" id="more-914778271" hidden>')[1]
    assert "Year over year" in more and "All facts, dates and sources" in more and 'class="sent"' in more  # all of it is behind the button
    assert "Year over year" not in head and 'class="sent"' not in head and "<table" not in head


def test_not_latest_accounts_are_flagged_in_the_key_figures():
    from test_core import _accounts, _run
    from signalpost_agent.htmlreport import _card
    card = _card(_run({"sisteInnsendteAarsregnskap": "2025"}, _accounts(2023, 2024)))
    assert "Key figures, FY2024" in card and "NOT latest" in card.split('<div class="actions">')[0]


def test_sources_are_labelled_by_what_they_are():
    from signalpost_agent.htmlreport import _link, _source_kind
    assert _source_kind("https://data.brreg.no/enhetsregisteret/api/enheter/914778271") == "register entry"
    assert _source_kind("https://data.brreg.no/regnskapsregisteret/regnskap/914778271") == "annual accounts"
    assert _source_kind("https://data.brreg.no/enhetsregisteret/api/enheter/914778271/roller") == "roles"
    assert _source_kind("https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet=1") == "workplaces"
    assert _source_kind("https://www.wikidata.org/wiki/Q1") == "Wikidata"
    assert _source_kind("https://fjordbygg.teamtailor.com/jobs/1-x") == "job platform"
    assert _source_kind("https://fjordbygg.example/nyheter/a/") == "news article"
    assert _source_kind("https://fjordbygg.example/") == "company website"
    a = _link("https://data.brreg.no/enhetsregisteret/api/enheter/914778271", "2026-11-04T10:00:00Z")
    b = _link("https://data.brreg.no/regnskapsregisteret/regnskap/914778271", "2026-11-04T10:00:00Z")
    assert 'class="k">register entry</span>' in a and 'class="k">annual accounts</span>' in b
    assert "data.brreg.no · 2026-11-04" in a and "data.brreg.no · 2026-11-04" in b  # same site and date, different label


def test_comparison_meta_carries_currency_news_link_and_posting_count_and_viewer_script_uses_them(tmp_path):
    from test_core import _accounts, _run
    from signalpost_agent.htmlreport import compare_meta, compare_row, write_html
    env = _run({"sisteInnsendteAarsregnskap": "2025"}, _accounts(2024, 2025, revenue={2024: 200, 2025: 150}))
    meta = compare_meta(env)
    assert meta["currency"] == "NOK" and meta["news_url"] == "" and meta["jobs"] == 0
    assert compare_row(env)["Job postings"] == "none found"
    write_html(tmp_path, [env])
    page = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "are not directly comparable" in page and "different currencies" in page and 'id = \'cmpnote\'' in page
    assert "m.news_url" in page and "showJobs" in page and '"_meta"' in page


def test_comparison_panel_header_is_pinned_and_only_the_table_scrolls_sideways(tmp_path):
    from test_core import _accounts, _run
    from signalpost_agent.htmlreport import write_html
    write_html(tmp_path, [_run({"sisteInnsendteAarsregnskap": "2025"}, _accounts(2024, 2025, revenue={2024: 200, 2025: 150}))])
    page = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "#cmp{position:fixed;top:0;left:0;right:0;bottom:0;width:100%;max-width:100%" in page and "display:flex;flex-direction:column" in page
    assert "#cmp .top{flex:0 0 auto" in page and "#cmpout{flex:1 1 auto;min-height:0;min-width:0" in page and "#cmpout .scroll{max-width:100%;overflow-x:auto}" in page
    assert "#cmpnote{max-width:100%;overflow-wrap:anywhere" in page and "#traytext{flex:1 1 12rem;min-width:0;max-width:100%" in page
    assert "html,body{max-width:100%;overflow-x:hidden}" in page


# ----- 7. revision 4: when in doubt, leave it out ---------------------------------------------------------------
def test_registry_site_branded_with_the_parent_group_name_is_kept_as_shared_without_brand():
    # ORKLA HOME & PERSONAL CARE AS declares orkla.com; the site calls itself "Orkla", the exact name of ORKLA ASA.
    search = json.dumps({"_embedded": {"enheter": [{"organisasjonsnummer": "910747711", "navn": "ORKLA ASA"}]}})
    fetcher = SiteFake({"https://orkla.example/": html("Orkla", "<p>Orkla</p>", '<meta property="og:site_name" content="Orkla">'),
                        "https://data.brreg.no/enhetsregisteret/api/enheter?navn=orkla&size=20": search})
    case = case_named("ORKLA HOME & PERSONAL CARE AS")
    web_phase(fetcher, case, {"hjemmeside": "orkla.example"}, None, max_guesses=0)
    assert claims(case, "official_website")[0]["value"]["site_scope"] == "registry_declared_shared"
    assert not claims(case, "public_brand")  # "Orkla" is the parent's brand


def test_registry_sub_page_of_a_shared_site_is_kept_as_shared():
    fetcher = SiteFake({"https://acme.example/": html("Acme Norge", "<p>Acme Norge</p>")})
    case = case_named("ACME NORGE AS")
    web_phase(fetcher, case, {"hjemmeside": "acme.example/home-care/"}, None, max_guesses=0)
    assert claims(case, "official_website")[0]["value"]["site_scope"] == "registry_declared_shared"
    # ... unless the site shows our organisation number
    fetcher = SiteFake({"https://acme.example/": html("Acme Norge", f"<p>Acme Norge AS org.nr {ORG}</p>")})
    case = case_named("ACME NORGE AS")
    web_phase(fetcher, case, {"hjemmeside": "acme.example/home-care/"}, None, max_guesses=0)
    assert claims(case, "official_website")[0]["value"]["identity_proof"] == "org_number_on_site"


def test_site_stating_another_entitys_org_number_is_not_used():
    fetcher = SiteFake({"https://acme.example/": html("Acme Norge", "<footer>Acme Norge – Org.nr. 976 967 631</footer>")})
    case = case_named("ACME NORGE AS")
    web_phase(fetcher, case, {"hjemmeside": "acme.example"}, None, max_guesses=0)
    assert claims(case, "official_website")[0]["value"]["site_scope"] == "registry_declared_shared"  # declared: kept, read strictly
    case2 = case_named("ACME NORGE AS")
    web_phase(fetcher, case2, {"epostadresse": "post@acme.example"}, None, max_guesses=0)
    assert not claims(case2, "official_website")  # found by the agent itself: another entity's site is not used


def test_registry_extras_and_forced_liquidation_status():
    from signalpost_agent import official
    body = {"organisasjonsnummer": ORG, "navn": "ACME AS", "organisasjonsform": {"kode": "AS"}, "underTvangsavviklingEllerTvangsopplosning": True,
            "postadresse": {"adresse": ["Postboks 1"], "postnummer": "0101", "poststed": "OSLO", "land": "Norge"},
            "aktivitet": ["Handel med varer."], "historiskeNavn": [{"navn": "GAMMEL AS", "fraDato": "2001-01-01 00:00", "tilDato": "2010-01-01 00:00"}],
            "kapital": {"belop": 30000.0, "valuta": "NOK", "type": "Aksjekapital"}, "registrertIMvaregisteret": True}
    fetcher = SiteFake({f"https://data.brreg.no/enhetsregisteret/api/enheter/{ORG}": json.dumps(body)})
    case = Case(ORG)
    official.fetch_entity(fetcher, case)
    assert claims(case, "registry_status")[0]["value"] == "in_forced_liquidation"
    assert claims(case, "registered_activity")[0]["value"] == "Handel med varer."
    assert claims(case, "former_name")[0]["value"] == {"name": "GAMMEL AS", "from": "2001-01-01", "to": "2010-01-01"}
    assert claims(case, "share_capital")[0]["value"]["amount"] == 30000.0
    assert claims(case, "register_memberships")[0]["value"]["vat_register"]["registered"] is True
    assert claims(case, "postal_address")
