# Revision 2: own website, own profiles, real news

Evaluator feedback on commit `23c3509`: (1) the website must be the company's own, not a parent group's; (2) each social
link must really belong to the company; (3) news must be real, dated posts about the company.
Principle: **an empty field is better than a wrong one.** Every withheld item says why in the claim `note`.

## How version 1 behaved (the gaps)

| Area | Version 1 |
|---|---|
| Website | Verified identity only (registry field, org number on the site, or name + one registry detail). Never asked whose site it is, so a group site that shows a subsidiary's number or phone was published as the company's. Group / brand sites only got a lower confidence and a note. Jobs, news, social, brand and description were then read from it. |
| Social | Every Facebook / Instagram / LinkedIn / YouTube / X / TikTok profile link on the homepage, legal pages and careers page was kept; the most-linked won. No check that the profile is the company's: footer links of the group, a country branch, the agency or the theme were published. |
| News | RSS/Atom items were dated by `pubDate`, Atom `published` **or `updated`**; any item with a title counted. Without a feed, **sitemap `lastmod` was used as the publish date** and menu / section pages ("All News") were emitted as news. Placeholder posts and comment feeds passed. |

## Rules in version 2

**Website scope** (`assess_site_scope` in `web.py`). A verified site is the company's own unless one of these holds:

1. Another legal entity is named as owner/publisher: a `©` line on any fetched page, or an owner / controller / publisher /
   impressum statement on a legal page, naming an entity (`AS`, `ASA`, `GmbH`, `AB`, `A/S`, ...) that shares our first name
   word but is not our exact legal name. Ignored: our own organisation number within 200 characters of the mention, and (legal
   pages only) a longer name that starts with ours, which is a subsidiary.
2. The proof was name + registry contact details and the site's own name does not carry the company name.
3. The proof was not an organisation number or a registry declaration, the site brand is only part of the legal name, and the
   registry holds another entity with exactly that brand name (`Statkraft` -> `STATKRAFT AS`).

If it fails, `official_website` is `ambiguous`, value `{candidate_url, site_scope: "group_site", owner, reason}`, with an
evidence record; the pipeline then skips that site's brand, description, social links, jobs and news. The summary lists it
under *unknowns*. A published site now carries `site_scope: "own"` and a `scope_basis`.

**Social profiles** (`social_mismatch`, `extract_social`). From a verified own site only. Accepted when the handle contains the
domain label or the joined legal name and the remainder holds no country / group / legal-form marker; rejected when it is a
platform, theme vendor or agency profile, or an opaque id (`/company/123`, `/channel/UC...`). A profile in the `sameAs` of the
site's own Organization JSON-LD is accepted without the name test. When every link is rejected the claim is `not_available`
and the note lists each rejected link with its reason.

**News** (`extract_activity`). Dated only from the item's own publish date; `lastmod`, Atom `updated`, `dateModified` and a
feed whose items all carry one timestamp are not dates. Headline must not be a menu label, the site name, a theme placeholder
("Hello world!") or a comment; URL must be on the company's site and not a section index, listing, product or `#comment` URL.
Sitemap entries only say where to look: each article page must itself carry `og:type=article` (or an Article JSON-LD) **and** a
publish date, otherwise nothing is emitted.

Known limits: a site that publishes its date only as visible text yields no news (by design); the handle test cannot accept a
genuine profile whose name does not contain the company name unless the site declares it in JSON-LD; the job-posting extractor
is unchanged and still lists some careers-page menu labels as postings.

## Before / after on real companies (live run, 2026-10-24)

`before` = commit `23c3509`, `after` = this revision, same organisation numbers.

| Company | Group relation | Website before -> after | Social before -> after | News before -> after |
|---|---|---|---|---|
| Statkraft Energi AS (987059729) | subsidiary of Statkraft AS | statkraft.com -> withheld (site names / is branded as Statkraft AS) | 5 group profiles -> none | 4 group news items -> none |
| REMA 1000 Norge AS (982254604) | subsidiary of REMA 1000 AS | rema.no -> withheld (`© 2026 REMA 1000 AS`) | 5 -> none | 4 items all dated 2026-10-19 from sitemap `lastmod` -> none |
| Color Line AS (970903968) | German branch Color Line GmbH on same site | colorline.com -> withheld (legal notice is Color Line GmbH) | facebook + `colorline_deutschland` + `ColorLineGmbH` -> none | none |
| Aker Solutions AS (929877950) | subsidiary of Aker Solutions ASA | akersolutions.com -> withheld (names Aker Solutions ASA as publisher) | 2 -> none | 4 `lastmod` items incl. the menu page "All News" -> none |
| Orkla Foods Norge AS (916170858) | Orkla ASA group | orklafoods.no kept (own) | facebook `orklanorge`, instagram `orklanorge`, linkedin `orkla-group` -> none (none carries the company name) | 6 posts -> same 6, now explicitly dated by feed `pubDate` |
| Equinor ASA (923609016) | itself the parent | equinor.com kept (a subsidiary named on a legal page is not an owner) | unchanged: facebook, instagram, linkedin | none -> none |
| KIWI Norge AS (975959171) | NorgesGruppen | kiwi.no kept (own org number on the site) | unchanged (4 profiles, all carry "kiwi") | none -> none |

Wider check, first 60 companies of `reports/local-1000`: 10 -> 11 websites published (the extra one, mamastapas.dk, shows the exact
organisation number; the earlier run hit a transient fetch failure); Kunst og Kaos AS 3 `lastmod` "news" items (page names such as
"Kunstnere") -> none; Heim Skiservice AS 4 theme-demo posts ("Hello world!", "Crafting Captivating Headlines...") -> none; Step
Trafikkskole AS a WordPress comment-feed item -> none; Lerøy Seafood AS 4+ `lastmod`-dated items ("Annual report ... 2019" dated
2026-10-13) -> articles with their own publish dates (2021-2025); Melbu Hotell AS unchanged (feed `pubDate`).

Tests: `tests/test_ownership.py` (18 tests, fake fetcher, no network): copyright / impressum owners, subsidiary and org-number
exceptions, brand-owner lookup, group site yields no social / news / jobs, social handle rules, `sameAs`, Atom `updated`,
identical-timestamp feeds, comment feeds, sitemap `lastmod`, undated and listing pages, menu-label headlines.
