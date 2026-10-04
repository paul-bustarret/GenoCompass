# Rare Disease Atlas — Pipeline Design

*Date: 2026-10-03 (revised after the first build). Brief: `HackNation/challenge7.pdf`
(Hack-Nation × OpenAI × Buffalo Initiative, Challenge 05).*

## 0. Status

| Part | Status |
|---|---|
| Write side: API fetchers, graph build, coverage log (§5) | **Built** — 21 diseases, 1,052 nodes, 1,582 edges, all sourced |
| Similarity, clusters, figures (§8, `figures/`) | **Built** — 3 clusters, lookalikes isolated, 9 offline tests pass |
| Write side: LLM extraction + `add_document` (§6) | To build — placeholder until an OpenAI key arrives |
| Read side: search, subgraph, journey queries, explainer (§7–§9) | To build |
| UI | Out of scope (teammate) — consumes the contract in §9 |

## 1. Goal and scope

Build the data pipeline and query layer for the "AI Atlas for the World's Rare Diseases":
a small, evidence-backed knowledge graph for one disease cluster, plus the queries that
carry a patient-group leader (Maria) from her disease to a similar disease, a reusable
asset, a collaborator, and a concrete next step — or to an honest "nothing supported".

**In scope:** API fetching, LLM extraction (behind a placeholder), graph construction,
similarity/clustering, journey queries, cited explanations, a JSON contract for the UI.

**Out of scope:** the UI, scaling beyond the slice, user accounts, any patient-level data.

**Constraints:** under 8 hours of build time; no OpenAI key yet; Python; no graph database.

**Success test:** `journey("MPS IIIA")` returns profile, similar diseases with witnesses,
assets across the cluster, network overlap, and 1–3 gap-based next steps, every item citing
edge IDs; Rett is never placed in the Sanfilippo cluster; a query with no support returns the
honest-gap result with coverage; `add_document(<abstract>)` adds sourced edges that the next
journey picks up.

## 2. Architecture: two systems, one graph

```
WRITE SIDE — graph manager                         READ SIDE — question answering
  inputs: API fetchers (built), a researcher         input: Maria's text
          uploading a paper, a patient group         search → anchor node
          submitting their registry                  → filtered k-step subgraph (§7)
  fetch/extract → match to existing nodes            → fixed query per question (§8)
  → quote + relevance check → add edges              → LLM writes cited answer; code
    with provenance (never overwrite)                  drops uncited sentences
                 │                                              ▲
                 └────────────▶  THE GRAPH (evidence only) ─────┘
                                 nodes.csv / edges.csv
```

- The read side never writes to the graph. The write side never answers questions.
- The initial build and "a researcher adds a paper" use the same write path (§6).

### What lives in the graph and what does not

The graph holds **evidence only**: facts about the world, each with a source. Three other kinds
of information sit beside it, deliberately, so anything in the graph can be cited:

| Kind | Example | Where | Why not in the graph |
|---|---|---|---|
| Rules | treatment-readiness checklist, 10× timelines | `config/*.yaml` | our judgement, not a sourced fact |
| Search records | "searched RePORTER for 'Krabbe', 5 kept of 41" | `data/graph/coverage.csv` | records what *we did*; lets us tell "none exists" from "never searched" |
| Computed results | similarity scores, clusters | `similarity.csv`, `clusters.json`, or at query time | our arithmetic; goes stale when evidence changes |

## 3. Disease slice (verified)

21 diseases, each verified against HPO (gene → disease annotation). Defined in
`config/slice.yaml` by OMIM ID + gene, with per-source search terms.

| Role | Diseases (gene) | What the data showed |
|---|---|---|
| Hero | MPS IIIA (SGSH), IIIB (NAGLU), IIIC (HGSNAT), IIID (GNS) | A–C cluster with MPS I/II; IIID just misses (0.108 < 0.12, Reactome curation gap) |
| Lysosomal context | MPS I (IDUA), MPS II (IDS), Pompe (GAA), Gaucher (GBA1), Fabry (GLA), Krabbe (GALC), MLD (ARSA), Tay-Sachs (HEXA), Sandhoff (HEXB), Niemann-Pick C (NPC1), CLN2 (TPP1), CLN3 (CLN3) | sphingolipid cluster (Fabry, Gaucher, Krabbe, MLD); GM2 cluster (Tay-Sachs, Sandhoff) |
| Bridge | Late-onset Parkinson (GBA1) ↔ Gaucher; Danon (LAMP2) ↔ Pompe | Parkinson joins the sphingolipid cluster via GBA1 (0.70); Danon–Pompe not supported (0.07) |
| Lookalike | Rett (MECP2), Duchenne (DMD), HCM (MYH7) | all stay unclustered; CLN2 ~ CLN3 flagged as lookalike |

## 4. Data model

Two CSVs, loaded into a `networkx.MultiDiGraph` (parallel edges = several sources for one fact).

### `nodes.csv`

| column | meaning |
|---|---|
| `id` | public ID with prefix: `MONDO:`, `NCBIGene:`, `ClinVar:`, `HP:`, `R-HSA-`, `PMID:`, `NCT…`, `RePORTER:`, `PERSON:` (RePORTER profile), `ORG:`, `INTERVENTION:`, `ASSET:`, `MECH:` |
| `type` | built: `disease`, `gene`, `variant`, `phenotype`, `pathway`, `paper`, `trial`, `intervention`, `grant`, `person`, `organization` · planned: `mechanism`, `asset` |
| `name`, `synonyms` | display name; `|`-separated synonyms (MONDO via OLS) used by search |
| `attrs` | JSON: disease `omim`, `role`, `short`; gene `hgnc`, `uniprot`; variant `significance`, `consequence`; trial `status`, `phase`; org `kind`; paper `authors` |

`mechanism` nodes (planned) use a small controlled vocabulary (e.g. `lysosomal_enzyme_deficiency`,
`autophagy_defect`, `sarcomere_defect`) and are assigned only by LLM extraction from cited text.

### `edges.csv`

| column | meaning |
|---|---|
| `edge_id` | `E` + 7-hex hash of (src, dst, relation, source_url) — used for citations |
| `src`, `dst`, `relation` | see relations below |
| `source`, `source_url`, `retrieved_at` | API name, exact URL, ISO date |
| `confidence` | 0–1 (APIs 0.95; name-matched author links 0.6; LLM extraction capped at 0.8) |
| `evidence` | `observed` (database / registry record) · `extracted` (LLM from cited text) · `inferred` (our matching) · `fixture` (placeholder LLM — never shown as real) |
| `polarity` | `supports` or `contradicts` |
| `effect` | `LoF`, `GoF`, `dominant_negative`, or empty (= unknown) |
| `quote` | supporting text (ClinVar classification + traits, trial conditions, grant title, LLM quote) |
| `frequency` | disease→phenotype frequency from HPO |

### Relations

| relation | src → dst | source | status |
|---|---|---|---|
| `caused_by` | disease → gene | HPO | built |
| `has_phenotype` | disease → phenotype | HPO | built |
| `has_variant` | gene → variant | ClinVar | built |
| `pathogenic_for` | variant → disease | ClinVar, only when its trait names match the disease | built |
| `in_pathway` | gene → pathway | Reactome (lowest level) | built |
| `part_of` | pathway → parent pathway | Reactome, one level up, top two levels skipped | built |
| `studies` | trial / grant → disease | ClinicalTrials.gov, RePORTER | built |
| `runs` | organization → trial | ClinicalTrials.gov sponsor | built |
| `tests` | trial → intervention | ClinicalTrials.gov (drug/biologic/genetic) | built |
| `pi_of`, `funds` | person → grant, organization → grant | RePORTER | built |
| `mentions` | paper → disease | PubMed | built |
| `authored` | person → paper | PubMed author ↔ RePORTER PI by last name + initial (`inferred`, 0.6) | built |
| `has_mechanism` | disease → mechanism | LLM on PubMed abstracts | planned |
| `studies` / `runs` | organization / asset → disease | LLM on patient-group pages | planned |
| `requires` | trial → gene/variant | LLM on eligibility text | planned |

## 5. Data acquisition (built)

`atlas/sources.py`, one function per API; `atlas/http.py` caches every response in
`data/raw/<source>/` (gitignored) and rate-limits NCBI to 3 req/s.

| source | endpoint | gives |
|---|---|---|
| HPO (JAX) | `ontology.jax.org/api/network/annotation/{OMIM}` | disease, its MONDO ID, phenotypes with frequency, gene |
| MONDO (OLS) | `ebi.ac.uk/ols4/api/ontologies/mondo/terms` | label + synonyms |
| mygene.info | `mygene.info/v3/query` | symbol → NCBI Gene, HGNC, UniProt |
| Reactome | `ContentService/data/mapping/UniProt/{id}/pathways`, `/data/event/{id}/ancestors` | pathways + parents |
| ClinVar | E-utilities `esearch`/`esummary` | top pathogenic variants per gene |
| ClinicalTrials.gov | `api/v2/studies` | 5 most recently updated trials per disease |
| NIH RePORTER | `v2/projects/search` | 5 grants per disease, FY2024–26, PIs, institute |
| PubMed | E-utilities `esearch`/`esummary` | 5 most recent papers per disease |

**Not used:** OMIM (key + licence for redisplay; IDs come via HPO); the Orphanet patient-organisation
directory (needs a data transfer agreement); Orphadata's gene endpoint (404 — cross-referencing works).

**Lessons from the build, now part of the design:**

1. **ID matching is unavoidable.** MONDO search and HPO disagree for Pompe (`MONDO:0009290` vs
   `MONDO:0017694`). We take the MONDO ID that HPO assigns, keep the OMIM ID as an attribute,
   and record mismatches.
2. **Reactome needs one parent level.** Leaf pathways alone split siblings apart (MPS IIIA–D each
   have their own leaf). Parents in Reactome's top two levels (e.g. "Innate Immune System") are
   skipped because they link unrelated diseases.
3. **Search terms are per-source.** Phrase searches returned nothing for GBA-Parkinson and MYH7
   cardiomyopathy; `slice.yaml` has an optional raw PubMed query.

## 6. Write side: LLM extraction and contributions (to build)

`atlas/llm/` exposes three functions; only this package knows which backend is used.

```python
extract_edges(text: str, source_url: str, task: str) -> list[EdgeDraft]
    # task ∈ {"mechanism", "eligibility", "patient_group"}; each draft carries a verbatim quote
resolve_name(name: str, candidates: list[Node]) -> str | None
    # choose among existing nodes only; called after exact/synonym matching fails
explain(subgraph: list[Edge], question: str, audience: str) -> str
    # plain-language answer; every sentence ends with [E…] citations
```

Backends via `ATLAS_LLM`: `stub` (default; hand-written outputs from `fixtures/llm/`, all marked
`evidence = fixture`) and `openai` (structured outputs; filled in when the key arrives).

**Entry point for new evidence:**

```python
add_document(text_or_url: str, submitted_by: str, kind: "paper" | "patient_group_page") -> AddReport
```

1. Fetch and store the raw text with URL + date.
2. `extract_edges` → drafts.
3. **Match** each entity to an existing node (exact ID → synonym → `resolve_name` over candidates);
   a new node is created only when nothing matches, and is reported.
4. **Quote check:** the quote must appear verbatim (whitespace-normalised) in the text.
5. **Relevance check:** the quote must mention the edge's subject or object by name or synonym.
   The spike showed why: a quote from NORD's "assistance programs" box passed the quote check but
   named an organisation unrelated to the disease.
6. Add edges as `extracted`, with `submitted_by` in attrs and confidence ≤ 0.8. Never overwrite;
   disagreement becomes a `polarity = contradicts` edge.
7. Append to the coverage log and return counts (added, dropped by reason, new nodes).

## 7. Read side: search and subgraph (to build)

**Search** (`atlas/query/search.py`): exact ID → exact name/synonym → token fuzzy match →
`llm.resolve_name` for leftovers. Accepts disease, gene, symptom, mechanism or organisation text.
Two or more candidates within 0.05 of the top score → `ambiguous` with the candidates.

**Subgraph:** all nodes within k steps (k ≤ 2) of the anchor, filtered so it stays small and relevant:

- **Relations per question**: Q1 follows `caused_by`, `in_pathway`, `part_of`, `has_phenotype`;
  Q2 follows `studies`, `runs`, `tests`; overlap follows `pi_of`, `funds`, `authored`.
- **Hub filtering**: features are weighted by rarity (IDF); nodes with weight below a floor are not
  expanded (e.g. "Childhood onset", "Neutrophil degranulation").
- **Top N per step**, ranked by that weight.

The LLM receives only this subgraph, with edge IDs.

## 8. Analytics and queries

### Similarity (built, `atlas/similarity.py`)

Features per disease: genes, pathways (leaf + parent), phenotypes. Weight = slice IDF
`log(N / diseases_with_feature)`. `component = Σ w(shared) / Σ w(union)`.

| view | weights (mechanism pending → folded into pathway) |
|---|---|
| therapeutic (Maria, Priya) | pathway 0.5, phenotype 0.3, gene 0.2 |
| phenotype (Devon) | phenotype 0.8, pathway 0.2 |

When `effect` data exists: drop a therapeutic pair whose known effects differ (LoF vs GoF).
**Lookalike** = phenotype ≥ 0.15 and pathway + gene ≤ 0.02. **Clusters** = Louvain on pairs with
therapeutic ≥ 0.12. Every score keeps its **witnesses** (shared nodes, rarest first).

### Query functions (to build, `atlas/query/journey.py`)

| function | answers |
|---|---|
| `profile(disease_id)` | genes, variants (ClinVar status), pathways, top phenotypes by IDF, mechanism if known |
| `similar(disease_id, view, k=5)` | Q1: neighbours with components, witnesses, lookalike flag (built) |
| `assets(disease_id)` | Q2: trials, interventions, grants, (later) patient groups/registries on the disease and its cluster |
| `next_steps(disease_id)` | Q3: readiness steps the disease lacks that a neighbour has ("reuse"), and steps nobody has ("build"); 10× table for the top item |
| `overlap(cluster_a, cluster_b)` | people, funders, sponsors linked to both |
| `by_mechanism(id)` | Priya: clusters ranked by share of diseases with that mechanism/pathway |
| `who_works_on(gene_or_mechanism_id)` | Dr. Osei: PIs/authors linked to any disease sharing it |
| `journey(text)` | search → profile → similar → assets → next_steps → explanation, or honest gap |

**Readiness checklist** (`config/readiness.yaml`): `mechanism_known`, `patient_group`, `registry`,
`natural_history_study`, `model`, `biomarker`, `trial`, `funding`, each defined as a node/edge pattern.
Today `trial` and `funding` can be filled from the graph; the rest need the LLM write path.

**Honest gap:** no neighbour ≥ 0.12, or empty assets and next steps → `{"status":
"no_supported_link", "searched": <coverage rows>, "missing": <sources not yet searched or empty>,
"next_question": …}`. Coverage distinguishes `n_results = 0` (searched, none) from `-1` (not searched).

**10×:** for the top "reuse" step, a table of from-scratch vs. reuse duration from
`config/timelines.yaml` (cited estimates; uncited ones labelled "assumption").

**Explanation:** `llm.explain` gets the subgraph; code removes sentences without a valid `[E…]`.

## 9. Output contract for the UI

`journey(text)` returns JSON (served by a thin FastAPI app if the UI needs HTTP:
`GET /search?q=`, `GET /journey?q=`, `GET /edge/{edge_id}`, `GET /node/{id}`):

```json
{
  "status": "ok | ambiguous | no_supported_link",
  "query": "sanfilipo",
  "anchor": {"id": "MONDO:0009655", "name": "MPS IIIA"},
  "candidates": [],
  "profile": {...},
  "similar": [{"id": "MONDO:0009656", "name": "MPS IIIB", "score": 0.37,
               "components": {"gene": 0.0, "pathway": 0.40, "phenotype": 0.55},
               "witnesses": [{"node": "R-HSA-2024096", "name": "HS-GAG degradation", "edges": ["E…"]}],
               "lookalike": false}],
  "assets": [...],
  "overlap": [...],
  "next_steps": [{"kind": "reuse | build", "step": "natural_history_study",
                  "from": "MONDO:…", "edges": ["E…"], "tenx": {...}}],
  "explanation": {"cards": {"whats_going_on": "...", "families_like_yours": "...",
                            "what_exists": "...", "next_step": "..."}},
  "searched": [...]
}
```

A frozen `fixtures/journey_mps3a.json` is produced first so the UI can be built in parallel.
`figures/atlas.html` already lets anyone explore the graph and click through to each edge's source.

## 10. Layout

```
rare_disease_atlas/
  config/        slice.yaml (built) · readiness.yaml, timelines.yaml (planned)
  atlas/
    http.py      cached HTTP, NCBI rate limit                       (built)
    graph.py     GraphBuilder, write/load CSVs → MultiDiGraph       (built)
    sources.py   one function per API                               (built)
    build.py     slice → all sources → data/graph/                  (built)
    similarity.py  IDF similarity, witnesses, lookalikes, clusters  (built)
    viz.py, atlas_template.html  figures + interactive explorer     (built)
    llm/         base.py, stub.py, openai_impl.py                   (planned)
    ingest.py    add_document: extract → match → checks → add       (planned)
    query/       search.py, subgraph.py, journey.py, explain.py     (planned)
    api.py       thin FastAPI wrapper                               (planned, optional)
  data/raw/      API cache (gitignored)
  data/graph/    nodes, edges, coverage, build_report, similarity, clusters (committed)
  figures/       disease_map.png, knowledge_graph.png, atlas.html
  spike_sample/  throwaway single-disease spike (kept for reference)
  tests/
```

## 11. Error handling

- API failure: 3 retries with backoff; 404 returns nothing and is logged as `n_results = -1`.
- NCBI requests share a 3 req/s limiter.
- Entities that cannot be matched (ClinVar traits, LLM drafts) are dropped and counted in
  `build_report.json`.

## 12. Testing

Offline `pytest` on the committed graph. **Passing now (9):** every edge has a source URL;
no similarity stored as evidence; Sanfilippo A–C share a cluster; Parkinson joins Gaucher via
GBA1; Rett, Duchenne and HCM stay unclustered; a feature shared by all diseases has zero weight;
coverage logs every disease for every source.

**To add with the read/write sides:** quote check and relevance check drop bad drafts;
`add_document` on a fixture abstract adds edges and a contradiction without overwriting;
a LoF/GoF pair is excluded from the therapeutic view (synthetic graph); `journey` on an isolated
disease returns `no_supported_link` with coverage; `explain` output has no uncited sentences;
`search("MPS 3")` returns `ambiguous` with the four subtypes.

## 13. Risks and known gaps

| risk / gap | handling |
|---|---|
| No OpenAI key before submission | Stub keeps everything runnable; fixtures are labelled; one real extraction run is needed to claim the OpenAI track. |
| LLM invents or misattributes | Quote check + relevance check; LLM only reads fetched text. |
| MPS IIID misses its cluster (Reactome files GNS under keratan sulfate) | Shown honestly; mechanism extraction should close it. Cutoff not tuned to force it. |
| Gaucher–Parkinson rests only on the shared gene | Needs `effect` data before any therapy claim; flagged in the explanation. |
| Danon–Pompe bridge unsupported | Reported as a gap with the next question to test. |
| Broad pathways as weak witnesses ("Neutrophil degranulation") | IDF keeps their weight low; subgraph hub floor (§7). |
| Website terms of service | Few pages, stored with URL + date; production would use partner agreements. |
| RePORTER search on "Sanfilippo" links the same grants to all four subtypes | Quote records the matched term; grants are not used in similarity. |
