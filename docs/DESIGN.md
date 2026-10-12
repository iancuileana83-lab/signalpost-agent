# Design notes

## Research and abstention policy

1. The organisation number is the only key. Everything else is a *candidate* until it resolves back to it.
2. A fact is published (`available`) only with at least one evidence record: source URL, retrieval time,
   content SHA-256, a claim span and the extraction method (plus reporting period / effective date where
   relevant).
3. When the match is uncertain the agent answers `ambiguous` (candidate named in the note, value withheld)
   or `not_available`. It never guesses and never converts absence into zero.
4. No LLM is used anywhere. Every step is deterministic, so a run can be reproduced and audited.
   (If an LLM is added later it must only summarise supported claims; the key would come from an
   environment variable supplied by the evaluator.)

## Identity resolution (website)

Candidates, in order: registry `hjemmeside`; Wikidata P856 of the item whose P2333 equals the organisation
number; the domain of the registry e-mail address (free-mail and accountant domains excluded); up to 7
name-derived domains (`.no`/`.com`) that resolve in DNS.

A candidate becomes the verified website only through one of these proofs:

| Proof | Rule |
|---|---|
| `org_number_on_site` | The exact 9-digit number appears in visible text or JSON-LD of the homepage or a contact/legal/about page. Works for any candidate source. |
| `registry_declared_website` | The registry itself lists the URL. |
| `registry_contact_details_match` | Legal-name tokens appear on the site **and** one entity-specific registry detail appears there: the registry phone number, the street address together with the postcode, a registered person's full name, or (for e-mail-domain candidates) the registry e-mail domain. |

Name similarity alone yields `ambiguous`. Parent/brand relations are not collapsed: the claim carries
the candidate source and proof, and a brand differing from the legal name is stated as such.

## Company-owned extraction (only after the site is verified)

Brand (JSON-LD, `og:site_name`, title), description (meta; labelled promotional), outbound profile links to
LinkedIn / Facebook / Instagram / YouTube / X / TikTok (profile URLs only; the platforms themselves are never
fetched), careers page (JSON-LD `JobPosting`, then job-looking links), dated news from RSS/Atom, then sitemap
`lastmod`.

## Evidence and refresh

Evidence ids are deterministic (hash of URL, method, span), so a re-run produces no duplicates. With
`--previous`, claims are merged by `claim_key`: unchanged claims keep `first_observed_at`; changed values emit a
typed change (`new_role`, `new_job`, `closed_job`, `new_filing`, `changed_description`, `changed_<field>`...),
and the superseded value moves to `history`. A refresh that fails or is blocked never erases the last supported
value: the old claim is kept with `stale: true`.

## Limitations

See the README. In short: tiny holding companies mostly have no public web presence, so web-derived fields are
sparse by nature; job coverage depends on the company's own careers page; NAV's public ad search is optional
because it rate-limits aggressively.
