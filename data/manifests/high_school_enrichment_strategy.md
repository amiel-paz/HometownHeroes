# High School Enrichment Strategy

This is an engineering/data-governance plan, not legal advice.

## Goal

Add high school associations for professional athletes in a way that is useful, reproducible, and conservative enough to support a public project.

The high school fact itself is usually simple factual data, for example:

- `Steven Kwan attended Washington High School in Fremont, California.`

The risk is not the fact in isolation. The risk is the acquisition and redistribution path: source terms, copied expression, bulk extraction, and whether a derived dataset is redistributed.

## Source Lanes

### Lane A: Clean Automated Ingest

Use these as normal database inputs.

- Wikidata `P69` educated at, plus school coordinates when present.
  - License posture: CC0.
  - Best use: first-pass automated enrichment.
- NCES CCD / EDGE public school locations.
  - License posture: U.S. government public-domain style data.
  - Best use: geocoding and canonicalizing U.S. public high schools.
- College Scorecard / IPEDS.
  - Best use: college geocoding.
- Lahman, Chadwick, nflverse, Retrosheet-style open/bulk sports identity data.
  - Best use: IDs, player universe, birthplaces, college fields where present.

### Lane B: Wikimedia Enrichment

Use for missing facts, with attribution and source separation.

- Wikipedia article text, retrieved by dump/API rather than browser scraping.
  - License posture: generally CC BY-SA, not CC0.
  - Best use: extracting high-confidence school candidates from sections like `Early life`, `Amateur career`, `High school`, and `College career`.
- DBpedia or other Wikipedia-derived structured extracts.
  - License posture: generally CC BY-SA or equivalent inherited obligations.
  - Best use: supplemental structured extraction when it provides school facts.

Rows from this lane should not be merged invisibly into the clean CC0/open layer. Store `source_layer = wikipedia_cc_by_sa`, article URL, revision ID, extraction rule, and attribution metadata.

### Lane C: Community / Manual Curation

Use for facts that are widely verifiable but absent from clean automated sources.

- Maintainer or user submits a normalized fact.
- Submitter grants the normalized fact contribution to the project under the project data license.
- Submission includes one or more source URLs for verification.
- The stored value is a normalized fact, not copied prose.

This lane is the best way to handle facts like Kwan's high school when Wikidata is missing the row but multiple public sources agree.

### Lane D: Local-Only Research Runners

Allow pybaseball-style local enrichment tooling, but do not redistribute the bulk output by default.

- Sports Reference / Baseball Reference / Pro Football Reference.
- School athletic department biographies.
- MaxPreps, Perfect Game, recruiting/scouting sites.
- Other sources with unclear or restrictive reuse terms.

These can be useful as personal research tools or verification leads. Their bulk outputs should remain ignored/untracked unless a source license or explicit permission allows redistribution.

## Recommended Data Model

High school rows should carry provenance at the fact level:

- `player_id`
- `sport`
- `event_type`: `attended_high_school`, `played_high_school`, or `attended_school`
- `school_name`
- `school_location_id`
- `city`
- `state`
- `country`
- `latitude`
- `longitude`
- `source_layer`
- `source_url`
- `source_revision_or_snapshot`
- `source_license`
- `extraction_method`: `wikidata_p69`, `wikipedia_rule`, `manual_curated`, `local_runner`
- `confidence`
- `curation_status`: `candidate`, `accepted`, `rejected`, `needs_review`
- `reviewed_by`
- `reviewed_at`

## Automated Workflow

1. Pull the player universe from existing MLB/NFL tables.
2. Match each player to Wikidata where possible.
3. Import Wikidata `P69` education rows.
4. Classify education institutions:
   - high school
   - college/university
   - prep/JUCO/other school
5. Resolve U.S. high schools against NCES CCD / EDGE.
6. For players still missing high school:
   - fetch Wikipedia article content by API/dump;
   - extract school candidates only from relevant sections;
   - keep revision ID and extraction rule;
   - resolve linked or named school against Wikidata and NCES;
   - write candidates with `curation_status = candidate`.
7. Promote high-confidence candidates automatically only when:
   - the school is linked to a school entity or matches NCES strongly;
   - location context matches the article sentence or school entity;
   - no conflicting school candidate exists.
8. Send the rest to manual review.

## Manual Review Rules

Accept a high school fact when at least one of these is true:

- It comes from Wikidata.
- It comes from Wikipedia and the article sentence is specific enough to identify the school and city/state.
- It is manually submitted with one official source or two independent public sources.

Reject or hold when:

- The school name is ambiguous and cannot be resolved to a specific campus.
- The source only says hometown, not school attended.
- The row appears to describe a camp, showcase, academy, club, or travel team rather than a school.
- Sources conflict.

## Display / Export Rules

- Display normalized facts, not copied biographies.
- Link to source attribution for Wikipedia-derived rows.
- Keep Wikipedia-derived rows in a visibly separate source layer.
- Do not ship bulk exports of Lane D local-runner output unless source permissions are cleared.
- Prefer contributing missing facts back to Wikidata when they are well sourced.

## Practical Recommendation

Build high school coverage in this order:

1. Wikidata `P69` plus school geocoding.
2. Wikipedia candidate extraction with attribution.
3. Community/manual curation table for missing well-known facts.
4. Optional local-only runners for restricted sports sites, ignored by Git and excluded from public data exports.

This gives the project a defensible public dataset while still allowing practical enrichment for gaps.
