# Rare Disease Atlas — data & API contract

This file is the single source of truth for every shape the UI (and Supabase) consumes.
All JSON is UTF-8, keys are `snake_case`, IDs are strings, missing values are `null` (never `""`
in JSON output; CSVs use empty cells).

## 1. Graph records

### Node
```json
{"id": "MONDO:0010100", "type": "disease", "name": "Tay-Sachs disease",
 "synonyms": ["TSD", "GM2 gangliosidosis type B"], "attrs": {"short": "Tay-Sachs", "omim": "OMIM:272800", "role": "lysosomal"}}
```

| `type` | ID pattern | notable `attrs` |
|---|---|---|
| disease | `MONDO:…` | `short`, `omim`, `role` (`hero`/`lysosomal`/`bridge`/`lookalike`), `group` (display group) |
| gene | `NCBIGene:…` | `hgnc`, `uniprot` |
| variant | `ClinVar:…` | `significance`, `consequence` |
| phenotype | `HP:…` | — |
| pathway | `R-HSA-…` | — |
| mechanism | `MECH:<slug>` | `label`; slug from `config/mechanisms.yaml` |
| paper | `PMID:…` | `journal`, `pubdate`, `authors` |
| trial | `NCT…` | `status`, `phase`, `countries` |
| intervention | `INTERVENTION:<slug>` | `kind` (`drug`/`biological`/`genetic`) |
| grant | `RePORTER:…` | `fiscal_year`, `org` |
| person | `PERSON:<reporter_profile_id>` | `first`, `last` |
| organization | `ORG:<slug>` | `kind` (`patient_group`/`sponsor`/`funder`/`company`/`academic`/`registry_host`; the current graph also has `industry`, `nih`, `other`), `website`, `contact_email` (only if printed on a fetched page), `country` |
| asset | `ASSET:<slug>` | `kind` (`registry`/`natural_history_study`/`biobank`/`animal_model`/`cell_model`/`biomarker`), `website` |
| country | `COUNTRY:<ISO2>` | `name` |

### Edge (one sourced claim)
```json
{"id": "E1a2b3c4", "source": "MONDO:0010100", "target": "MECH:lysosomal_enzyme_deficiency",
 "relation": "has_mechanism", "evidence": "extracted", "confidence": 0.8, "polarity": "supports",
 "effect": "LoF", "source_name": "pubmed", "source_url": "https://pubmed.ncbi.nlm.nih.gov/42511849/",
 "quote": "Tay-Sachs disease is … deficient β-hexosaminidase A (HexA) activity …",
 "frequency": null, "context": "human", "retrieved_at": "2026-10-03", "submitted_by": null}
```
- `frequency`: phenotype frequency from HPO for `has_phenotype` edges (e.g. `"76/79"`, `"Occasional"`), else `null`.

- `evidence`: `observed` (database / registry record) · `extracted` (LLM, verbatim `quote` verified) · `inferred` (our matching, e.g. author↔PI)
- `polarity`: `supports` | `contradicts`
- `effect`: `LoF` | `GoF` | `dominant_negative` | `null` (= unknown)
- `context` (extracted edges): `human` | `animal_model` | `cell_model` | `review` | `null`
- CSV column names in `data/graph/edges.csv`: `edge_id, src, dst, relation, source, source_url, retrieved_at, confidence, evidence, polarity, effect, quote, frequency, context, submitted_by`. JSON output renames `edge_id→id, src→source, dst→target, source→source_name`.

### Relations
`caused_by` disease→gene · `has_phenotype` disease→phenotype · `has_variant` gene→variant ·
`pathogenic_for` variant→disease · `in_pathway` gene→pathway · `part_of` pathway→pathway ·
`has_mechanism` disease→mechanism · `studied_with` disease→intervention (extracted from papers) ·
`studies` trial|grant|organization|asset→disease · `runs` organization→trial|asset ·
`tests` trial→intervention · `pi_of` person→grant · `funds` organization→grant ·
`mentions` paper→disease · `authored` person→paper · `has_site_in` trial→country ·
`based_in` organization→country

## 2. Python API (`atlas.api`) — every function returns JSON-serialisable dicts

| function | returns |
|---|---|
| `search(text)` | §3.1 |
| `subgraph(disease_id, focus="all", k=2, max_nodes=150)` | §3.2 |
| `journey(disease_id, country=None)` | §3.3 (no LLM; deterministic) |
| `recommend(disease_id, country=None)` | §3.3 with `cards` filled by the LLM |
| `draft_email(org_id, disease_id, sender={"name":…, "role":…, "context":…})` | §3.4 |
| `chat(disease_id, messages)` | §3.5 |
| `add_document(text=None, url=None, submitted_by="anonymous", disease_ids=None)` | §3.6 |
| `refresh_papers(since=None, disease_ids=None, per_disease=10)` | §3.6 |

Local HTTP server (`python -m atlas.server`, FastAPI on `http://localhost:8000`):
`GET /search?q=` · `GET /subgraph/{id}?focus=&k=` · `GET /journey/{id}?country=` ·
`GET /recommend/{id}?country=` · `POST /email` · `POST /chat` · `POST /documents` · `POST /refresh` ·
`GET /edge/{edge_id}` (one Edge, §1) · `GET /node/{node_id}` (one Node, §1) · `GET /health`
(`{"status": "ok", "nodes": n, "edges": n}`). Request bodies mirror the function arguments.
Unknown ids → HTTP 404 `{"error": "…"}`; invalid arguments (e.g. bad `focus`) → HTTP 400.
`api.edge(edge_id)` and `api.node(node_id)` back the two lookup routes.
`GET /tables/{nodes|edges|similarity|clusters|coverage}` returns every row of `data/graph/` as a JSON
array in the row shape of the UI's Supabase tables (`supabase/migrations/*_knowledge_graph.sql`: CSV column
names, `attrs` as an object, numbers as numbers, `lookalike` as bool, `clusters` = `{cluster, disease_id}`
from `clusters.json`); any query param naming a column filters it (`?relation=studies,mentions`). The UI
uses it as its local graph source when `VITE_ATLAS_API_URL` is set.

## 3. Response shapes

### 3.1 search
```json
{"query": "sanfilipo", "status": "ok | ambiguous | not_found",
 "matches": [{"id": "MONDO:0009655", "type": "disease", "name": "MPS IIIA", "score": 0.92, "matched_on": "synonym"}]}
```

### 3.2 subgraph
```json
{"anchor": "MONDO:0010100", "focus": "all | similar | assets | people", "k": 2,
 "nodes": [Node], "edges": [Edge], "truncated": false}
```

### 3.3 journey / recommend
```json
{"status": "ok | no_supported_link",
 "disease": {"id": "MONDO:0010100", "name": "Tay-Sachs disease", "short": "Tay-Sachs"},
 "user_country": "IN",
 "profile": {"genes": [...], "mechanisms": [...], "pathways": [...], "top_phenotypes": [...], "variants": [...]},
 "similar": [{"id": "MONDO:0010006", "name": "Sandhoff", "score": 0.37, "lookalike": false,
              "components": {"gene": 0.0, "pathway": 0.70, "phenotype": 0.09, "mechanism": 0.5},
              "witnesses": [{"id": "R-HSA-…", "name": "…", "kind": "pathway", "edges": ["E…"]}]}],
 "lookalikes": [...same shape...],
 "organizations": [{"id": "ORG:…", "name": "…", "kind": "patient_group", "country": "GB",
                    "website": "…", "contact_email": null, "for_disease": "MONDO:…", "edges": ["E…"]}],
 "trials": [{"id": "NCT…", "title": "…", "status": "RECRUITING", "phase": "PHASE2", "for_disease": "MONDO:…",
             "countries": ["US", "NL"], "near_user": false, "interventions": ["…"], "edges": ["E…"]}],
 "assets": [{"id": "ASSET:…", "name": "…", "kind": "registry", "for_disease": "MONDO:…", "edges": ["E…"]}],
 "researchers": [{"id": "PERSON:…", "name": "…", "grants": ["RePORTER:…"], "diseases": ["MONDO:…"], "edges": ["E…"]}],
 "next_steps": [{"kind": "contact | join | reuse | build", "step": "natural_history_study", "text": "…",
                 "from_disease": "MONDO:…", "target_id": "ASSET:…", "edges": ["E…"]}],
 "cards": [{"key": "whats_going_on | similar | what_exists | next_step", "title": "…",
            "text": "Sentence one [E1a2b3c4]. Sentence two [E5d6e7f8].", "citations": ["E1a2b3c4", "E5d6e7f8"],
            "status": "ok | no_supported_evidence"}],
 "searched": [{"source": "pubmed", "query": "…", "n_results": 412, "n_kept": 10}],
 "missing": ["patient_groups: no page found"],
 "generated_by": {"backend": "claude", "model": "sonnet"}}
```
`journey` returns `cards: []` and `generated_by: {"backend": "deterministic", "model": null}`;
`recommend` fills `cards` and sets `generated_by` to the LLM backend and model (`claude`/`sonnet` or
`ollama`/`qwen3:30b-a3b`). If the LLM call fails, `recommend` returns `cards: []` and
`generated_by` gains an `"error": "…"` key. Every sentence in a card ends with ≥1 `[E…]`
citation that exists in the response's edges; uncited sentences are removed in code, as are sentences
whose only citations are grant `studies` edges whose quote does not name the disease (name, short name,
synonym or gene). A card's `text` is never `""`: if every sentence was dropped, `text` is
`"The atlas has no cited evidence for this yet."`, `citations: []` and `status: "no_supported_evidence"`;
otherwise `status: "ok"`.

Lists are deduplicated by `id` (edges merged). `assets` include assets linked by `asset -studies-> disease`
and by `organization -runs-> asset` where the organisation `studies` the disease (only when the asset is
not linked to another disease and either names the disease or the organisation studies only this disease).
`trials` are ordered per disease (this disease first) by recruiting status, then `near_user`.
`next_steps` order: the disease's own community first (`contact` its patient groups, `join` its
registries / natural history studies), then `reuse` from similar diseases, then `contact` organisations of
similar diseases, then `build`. `missing` is computed per coverage source: `"<source>: not searched yet"`
when this disease has no coverage row for it with `n_results >= 0`; `"<source>: searched, none found"`
when all such rows have `n_results == 0`.

### 3.4 draft_email
```json
{"org_id": "ORG:…", "disease_id": "MONDO:…", "to": "info@example.org | null",
 "recipient_reason": "patient group linked to this disease (studies edge) with a general contact email",
 "subject": "…", "body": "…", "citations": ["E…"],
 "warnings": ["No contact email found on the organisation's pages; use the website contact form."],
 "requires_human_review": true}
```
Never sent automatically. `org_id` may be `null`: the recipient is then chosen from the journey's
organisations by this order: (a) patient group linked to this disease by a `studies` edge with a general
role address (`info@`, `contact@`, …); (b) any organisation linked to this disease with a usable email;
(c) patient group of the most similar disease with a usable email — the email then explicitly addresses
it as a related community; then the same tiers with only a `fundraising@`/`donate@`/`donations@`/
`press@`/`media@`/`careers@`/`jobs@` address; then organisations without a usable email (`to: null`).
A `contact_email` that is not a valid address (e.g. `[email protected]`) or is personal webmail
(gmail/yahoo/hotmail/outlook/icloud/aol/proton) is treated as missing, with a warning.
`recipient_reason` states which rule applied.

### 3.5 chat
Request: `{"disease_id": "MONDO:…", "messages": [{"role": "user", "content": "…"}, {"role": "assistant", "content": "…"}]}`
```json
{"answer": "… [E1a2b3c4] …", "citations": ["E1a2b3c4"], "grounded": true,
 "out_of_scope": false}
```
Stateless: the UI sends the full history each time. Answers only from the disease's subgraph;
`grounded:false` / `out_of_scope:true` when the graph cannot answer.

### 3.6 add_document / refresh_papers
```json
{"source_url": "…", "submitted_by": "…", "added_edges": ["E…"], "new_nodes": ["MECH:…"],
 "contradictions": ["E…"],
 "dropped": {"quote_check": 2, "relevance": 1, "unresolved_entity": 0, "invalid_type": 0},
 "papers_checked": 1}
```

## 4. Files

| path | content |
|---|---|
| `data/graph/{nodes,edges,coverage}.csv` | the graph (evidence only) |
| `data/graph/{similarity.csv,clusters.json,build_report.json}` | computed results |
| `exports/json/**`, `exports/supabase/**` | precomputed exports, see §5 |

## 5. Exports

Produced by `uv run python -m atlas.export [--no-llm]` (`atlas/export.py`) from `data/graph/`. LLM
calls go through `atlas.llm` and are cached in `data/llm_cache/`, so a rerun is free. Filenames replace
`:` in IDs with `_` (`MONDO:0010100` → `MONDO_0010100.json`).

| path | content |
|---|---|
| `exports/json/subgraph/<id>.json` | `subgraph(id, focus="all")` (§3.2) for every disease |
| `exports/json/journey/<id>.json` | `recommend(id)` (§3.3, no `country`). On LLM failure (or `--no-llm`) the `journey(id)` shape with `cards: []` |
| `exports/json/email/<id>.json` | `draft_email(org, id, sender)` (§3.4) to one organisation per disease; absent when the disease has no linked organisation (or with `--no-llm`) |
| `exports/json/index.json` | entry point for a static UI, below |
| `exports/json/export_report.json` | per disease: number of cards and fallback error; chosen email org and why; Supabase row counts |
| `exports/supabase/schema.sql` | Postgres DDL, idempotent (`create … if not exists`) |
| `exports/supabase/<table>.csv` | one CSV per table, header = column names, loadable with `\copy <table> from '<table>.csv' with (format csv, header true)` |

Email organisation choice: `api.choose_recipient(journey)`, the same rule as `draft_email(null, …)` (§3.4).
`exports/examples/{recommend_tay_sachs,email_example,chat_example}.json` are regenerated by the same code
(`recommend("MONDO:0010100", "IN")`, its email, one chat answer) on every LLM export, or alone with
`uv run python -m atlas.export --examples-only`. Sender:
`{"name": "Maria", "role": "patient group leader", "context": "Parent leading a patient group for <short name>"}`.

### index.json
```json
{"name": "Lysosomal storage diseases (20) + controls (5)",       // config/slice.yaml
 "generated_at": "2026-10-04T01:00:00+00:00",
 "counts": {"nodes": 1756, "edges": {"total": 3513, "observed": 2335, "extracted": 1153, "inferred": 25},
            "papers": 361, "organizations": 228, "countries": 56, "trials": 88, "diseases": 25},
 "sources": [{"name": "pubmed", "records_available": 412, "records_kept": 125}],  // summed from coverage.csv; n_results = -1 counts as 0
 "diseases": [{"id": "MONDO:0010100", "name": "Tay-Sachs disease", "short": "Tay-Sachs",
               "group": "GM2 & GM1 gangliosidoses", "role": "lysosomal", "cluster_id": 2,
               "files": {"journey": "journey/MONDO_0010100.json", "subgraph": "subgraph/MONDO_0010100.json",
                         "email": "email/MONDO_0010100.json"}}],   // paths relative to exports/json/; email may be null
 "clusters": [{"cluster": 2, "diseases": ["MONDO:…"], "names": ["Tay-Sachs"]}]}  // = data/graph/clusters.json
```
Diseases are listed in `config/slice.yaml` order.

### Supabase tables (`exports/supabase/`)

| table | columns (primary key first) |
|---|---|
| `nodes` | `id` text pk, `type`, `name`, `synonyms` text[], `attrs` jsonb |
| `edges` | `id` text pk, `source`, `target`, `relation`, `evidence`, `confidence` numeric, `polarity`, `effect`, `source_name`, `source_url`, `quote`, `context`, `retrieved_at` date, `submitted_by`, `frequency` |
| `coverage` | (`disease_id`, `source`, `query`) pk, `n_results` int, `n_kept` int, `retrieved_at` date |
| `similarity` | (`a`, `b`) pk, `therapeutic`, `phenotype_view`, `gene`, `pathway`, `phenotype` numeric, `lookalike` bool, `top_witnesses` text[] |
| `clusters` | (`cluster_id` int, `disease_id`) pk |
| `journeys` / `subgraphs` / `emails` | `disease_id` text pk, `data` jsonb (= the JSON file of the same disease) |

Edge columns use the JSON names of §1 (not the `edges.csv` names). Indexes: `edges(source)`,
`edges(target)`, `edges(relation)`, `nodes(type)`. No foreign keys. Every table has row-level security
enabled with one policy `<table>_read`: `SELECT` for `anon` and `authenticated` (read-only; no write
policies). CSV encoding: empty unquoted cell = `NULL`, arrays as Postgres literals (`{"a","b"}`), jsonb as
JSON text.

`scripts/push_supabase.py` (needs `SUPABASE_DB_URL`; `uv run --extra supabase python scripts/push_supabase.py`)
applies `schema.sql`, truncates all tables and `COPY`s every CSV in one transaction.
