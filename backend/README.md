# Rare Disease Atlas — knowledge graph

One evidence graph for a slice of 25 rare diseases — **Lysosomal storage diseases (20) + controls (5)**:
20 lysosomal diseases (the 4 Sanfilippo "hero" subtypes, 15 others, and Danon as a bridge) plus 5 controls
(GBA1 Parkinson as a bridge; Rett, Duchenne, HCM/MYH7 and Canavan as lookalikes). Built only from public APIs and public
web pages. Every edge is one claim with its source, URL, date, confidence and evidence type
(`observed` database record · `extracted` LLM claim with a verbatim, code-checked quote · `inferred` our matching).

The data and API shapes are defined in [`docs/contract.md`](docs/contract.md) (authoritative).

## Current graph (`data/graph/`)

| | count |
|---|---|
| nodes | 1,756 |
| edges | 3,513 — 2,335 observed · 1,153 extracted · 25 inferred |
| papers | 361 |
| organisations | 228 (143 patient groups) |
| countries | 56 |
| trials · grants · researchers | 88 · 80 · 104 |
| mechanisms · assets (registries, studies, models) | 24 · 28 |

## Pipeline: build → extract → viz → export

```bash
uv run python -m atlas.build            # public APIs (cached in data/raw/) -> data/graph/{nodes,edges,coverage}.csv
uv run python -m atlas.extract all      # LLM: mechanisms/LoF-GoF/interventions from PubMed abstracts,
                                        #      patient groups/registries from pages in config/pages.yaml
uv run python -m atlas.viz              # similarity + clusters -> data/graph/{similarity.csv,clusters.json}, figures/
uv run python -m atlas.export           # precomputed JSON + Supabase files -> exports/ (see contract §5)
uv run --group dev pytest -q            # offline checks (LLM always faked)
```

`atlas.extract` runs as `papers`, `pages` or `all` (`--per-disease N`, `--diseases ID,ID`).
`atlas.export --no-llm` skips the LLM (deterministic journeys with empty `cards`, no emails).
The slice is defined in `config/slice.yaml` (OMIM id + gene, each verified against HPO).

## Server

```bash
uv run python -m atlas.server           # FastAPI on http://localhost:8000  (docs at /docs)
```

`GET /search?q=` · `GET /subgraph/{id}` · `GET /journey/{id}` · `GET /recommend/{id}` · `POST /email` ·
`POST /chat` · `POST /documents` · `POST /refresh` · `GET /edge/{id}` · `GET /node/{id}` · `GET /health`.
The same functions are importable from `atlas.api` (contract §2–§3). The graph is reloaded when the CSVs change.

## LLM backend

All LLM calls go through `atlas/llm.py` and are cached on disk in `data/llm_cache/` (reruns are free;
`ATLAS_LLM_NOCACHE=1` bypasses the cache).

- default: `claude -p` headless (local Claude Code login, no API key): `haiku` for extraction, `sonnet` for writing.
- `ATLAS_LLM=ollama`: a local Ollama model (`qwen3:30b-a3b`) on `http://localhost:11434`.

## Adding evidence

```bash
# one document (pasted text or a URL); claims must quote the text verbatim to be kept
uv run python -m atlas.extract add --url https://example.org/news --by "Maria" --diseases MONDO:0009655
uv run python -m atlas.extract add --text "…" --by "Maria"
# new PubMed papers since a date
uv run python -m atlas.extract refresh --since 2026-09-01 --per-disease 10
```

Same from Python (`atlas.api.add_document(text=…, url=…, submitted_by=…, disease_ids=…)`,
`atlas.api.refresh_papers(since=…, disease_ids=…, per_disease=…)`) or over HTTP (`POST /documents`,
`POST /refresh`). New edges are appended, never overwrite existing ones, and carry `submitted_by`.

## Exports (`exports/`)

- `exports/json/index.json` — slice name, counts, sources, diseases (with group, role, cluster, file paths), clusters.
- `exports/json/{journey,subgraph,email}/<id>.json` — per disease (`:` → `_` in filenames).
- `exports/supabase/schema.sql` + one CSV per table — load with
  `SUPABASE_DB_URL=… uv run --extra supabase python scripts/push_supabase.py` (one transaction: schema, truncate, copy).

## Sources

| Source | API | Gives |
|---|---|---|
| HPO (JAX ontology API) | `ontology.jax.org/api/network/annotation` | disease ↔ MONDO id, phenotypes (with frequency), causal gene |
| MONDO (EBI OLS) | `ebi.ac.uk/ols4/api` | disease labels + synonyms (for search) |
| mygene.info | `mygene.info/v3/query` | gene symbol → NCBI Gene, HGNC, UniProt |
| Reactome | `reactome.org/ContentService` | gene → pathways, plus one parent level (top two levels skipped as hubs) |
| ClinVar (NCBI E-utilities) | `eutils.ncbi.nlm.nih.gov` | pathogenic variants per gene, linked to a disease only when ClinVar's trait names match |
| ClinicalTrials.gov v2 | `clinicaltrials.gov/api/v2/studies` | trials, sponsors, interventions (drug/biologic/genetic) |
| NIH RePORTER | `api.reporter.nih.gov/v2/projects/search` | grants (FY2024–26), PIs, NIH funding institute |
| PubMed (NCBI E-utilities) | `eutils.ncbi.nlm.nih.gov` | recent papers per disease; authors linked to PIs by name match (`evidence=inferred`) |
| PubMed abstracts + LLM | (`atlas.extract papers`) | mechanisms, LoF/GoF, studied interventions, contradictions |
| Patient-group / registry pages + LLM | `config/pages.yaml` (`atlas.extract pages`) | patient groups, registries, natural-history studies, contact emails printed on the page |

## Layout

```
atlas/http.py        cached GET/POST (data/raw/), NCBI rate limit
atlas/graph.py       GraphBuilder (nodes, edges, coverage) + load() -> networkx.MultiDiGraph
atlas/sources.py     one function per API
atlas/build.py       runs the slice through all sources
atlas/llm.py         the single LLM entry point (claude -p / ollama, disk cache)
atlas/extract.py     write side: LLM claims with quote/relevance checks; add_document, refresh_papers
atlas/similarity.py  rarity-weighted disease similarity, witnesses, lookalikes, Louvain clusters
atlas/viz.py         figures/disease_map.png, figures/knowledge_graph.png, figures/atlas.html
atlas/api.py         read side: search, subgraph, journey, recommend, draft_email, chat
atlas/server.py      FastAPI wrapper around atlas.api
atlas/export.py      exports/json + exports/supabase
scripts/push_supabase.py  load exports/supabase into Postgres
```

Similarity and clusters are *computed* from the graph and written to separate files; they are never
stored as evidence edges.

## Known gaps

- Gaucher ↔ Parkinson (0.53) rests on the shared gene GBA1, shared Reactome pathways and one generic
  mechanism (lysosomal enzyme deficiency, LoF in both); there is no phenotype overlap, and the atlas cannot
  say whether the same therapy would apply.
- Pompe ↔ Danon (proposed bridge) is still not supported by current data (0.08); it needs mechanism evidence
  that links autophagy/glycogen storage in both.
- Broad pathways such as "Neutrophil degranulation" still appear as weak witnesses between lysosomal diseases.
- `data/graph/similarity.csv` lists gene/pathway/phenotype components but not the mechanism component
  that enters the therapeutic score.
- Organisation `kind` values from page extraction include `industry`, `nih` and `other` besides the
  contract's list; contact emails exist only where printed on a fetched page.
- (Closed) MPS IIID ↔ MPS IIIC was 0.108 before mechanism extraction; it is now 0.18 and MPS IIID clusters with
  the other mucopolysaccharidoses.
