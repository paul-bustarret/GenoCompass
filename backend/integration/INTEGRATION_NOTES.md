# Backend integration notes

The UI can read from the Rare Disease Atlas FastAPI backend (`rare_disease_atlas/docs/contract.md`).
Every call still goes through `src/lib/atlas.ts`. Turn the switch off and the mocks behave exactly as before.

## Files
- `src/lib/atlas-api.ts`: typed fetch client with contract types, timeouts (20 s by default, 120 s for LLM endpoints) and `AtlasApiError` (`kind`: http, timeout, network or parse).
- `src/lib/atlas-adapter.ts`: pure converters from backend responses to UI shapes. These are `toUiDisease`, `toUiSimilarEdges`, `toUiEvidence`, `toPartner`, `stripCitations` and `citationIds`.
- `src/lib/atlas.ts`: the `USE_API` switch. API mode has no 180 ms delay. On API errors it logs `console.warn` and returns an empty result, so routes never hang.

## Env vars
| var | effect |
|---|---|
| `VITE_ATLAS_API_URL` | base URL. A non-empty value turns API mode on |
| `VITE_USE_ATLAS_API=true` | turns API mode on with the default URL `http://localhost:8000` |
| `VITE_ATLAS_USE_RECOMMEND=true` | profiles use `/recommend` (LLM cards, so the description comes from `whats_going_on`) instead of `/journey` |

## Run
```sh
# backend (in rare_disease_atlas)
uv run python -m atlas.server            # http://localhost:8000; needs CORS for the Vite origin
# UI (in this repo)
bun install   # or npm install
VITE_ATLAS_API_URL=http://localhost:8000 bun run dev   # or: npx vite dev
```
The browser calls the backend directly. The FastAPI app must allow the dev origin through `CORSMiddleware`, for example `http://localhost:8080` or `*`. If it does not, add a Vite `server.proxy` instead.

## Endpoint → function
| atlas.ts function | backend |
|---|---|
| `searchEntities(q)` | `GET /search?q=`, then `GET /node/{id}` for each disease hit (max 10) |
| `getDiseaseProfile(id)` | `GET /journey/{id}` (or `/recommend`) + `GET /subgraph/{id}` |
| `getGraph(id)`, `getSimilarDiseases`, `getConnectionPath` | journey + subgraph, using `similar[]` and `lookalikes[]` |
| `getNextSteps(id)`, `getPeople(id)` | `journey.organizations` (only `for_disease === id`), plus `GET /edge/{id}` when the cited edges are missing |
| `getPatientGroups(id)` | `journey.organizations`, kind `patient_group` |
| `getEvidence(ids)` | `GET /edge/{id}` (cached similarity edges are returned locally) |
| `getSearchCoverage(q)` | `/search`, then `journey.searched` + `journey.missing` |
| `askAtlas(id, messages)` *(new)* | `POST /chat` |
| `draftOutreachEmail(id, sender, orgId?)` *(new)* | `POST /email` (`org_id` null lets the backend choose the recipient) |
| `getRecommendation`, `addResearchDocument`, `getAtlasHealth` *(new)* | `/recommend`, `/documents`, `/health` |
| `getClusterAssets`, `getFasterRoute`, `rankClustersByApproach` | unchanged (no backend equivalent) |

Results are cached in memory per disease: journey, subgraph and graph. API mode fills the synchronous exports `diseases`, `clusters` and `allEdges` in place, so `getNode`, `edgesFor` and `clusterFor` work after the first async call.

## Field mapping
| UI | backend |
|---|---|
| Disease `label` / `plain_label` | `attrs.short` (falls back to `name`) / `name` |
| `synonyms`, `xrefs.OMIM` | `synonyms`, `attrs.omim` without the `OMIM:` prefix (`""` if missing) |
| `cluster` | slug of `attrs.group`, else `cluster-<n>`, else `unmapped`. A cluster node is created with `label` = group |
| `attributes.gene` | `profile.genes[0].name`, else the neighbour's `caused_by` gene in the subgraph |
| `attributes.description` | `whats_going_on` card with `[E…]` stripped, else `attrs.description`, else `""` |
| `attributes.patient_group` | first `patient_group` org for the disease |
| `research_activity` | 0–100 = `100·(1−e^(−n/15))`, where n = number of trial + grant + paper nodes in the subgraph |
| similar edge `id` | `sim:<anchor>:<other>`, `type: similar_to` |
| `score`, `*_score` | `score`, `components.{mechanism,pathway,phenotype}` (plus `gene_score`) |
| `witnesses` | witness node ids. `evidence_edges` = all witness edge ids |
| `negated` / `effect_compatible` | `lookalike` / `!lookalike`. Lookalike edges get the label "Symptom overlap only" |
| similar `evidence_type` | strongest witness edge in the subgraph (observed → curated), else inferred |
| evidence `evidence_type` | observed → curated, extracted → extracted, inferred → inferred |
| `evidence_level` | trial relations/source → clinical_trial; context human or review → clinical_observation; animal or cell → preclinical; null → clinical_observation if observed, else hypothesis |
| `plain_explanation` | `quote` in quotation marks, else a generated relation sentence |
| `source_id` / extra `source_url` | `source_url` (falls back to the edge id) |
| `negated` / `contradicted_by` | `polarity === contradicts` / ids of contradicting edges on the same endpoints |
| Partner `region` | `country` → full name via `Intl.DisplayNames` |
| `email` | `contact_email` (`""` if missing) |
| `sourceUrl` | `website`, else the source_url of the first cited edge |
| `contactSourceUrl` | source_url of a cited edge |
| `approach` | quote from a cited `studies` edge, else a generic sentence |
| `stage`, `question` | generated defaults |

## Routes that still bypass the async API (minimal fixes)
- `src/routes/atlas.$id.tsx`
  - `mapEdges = allEdges.filter(...)` runs once at module load, so the map has no lines in API mode. Fix: compute it inside `Atlas()`, e.g. `const mapEdges = allEdges.filter(...)` after `loading` turns false, or store `getGraph()`'s returned `edges` in state.
  - `positions` is keyed by mock ids, so every real node lands at (50,50). Fix: when there is no fixed position, fall back to a radial layout around the focus node, e.g. `(50+35cos(2πi/n), 50+35sin(2πi/n))`.
  - `isGap = id === "disease-z"`. Fix: use `journey.status === "no_supported_link"`; expose it from `getGraph`.
  - `plainDescriptions` is mock-only. It already falls back to `attributes.description`.
  - Cluster CSS classes (`cluster-waste`, etc.) do not exist for the slugs. Nodes render without colour. Add a fallback class or map `cluster-*` to a palette.
  - `focus = diseases.find(...)` shows "Record not found" until `getGraph` resolves. Show the loading state when `loading` is true.
- `src/routes/disease.$id.tsx`: `diseases.find` is fine after `getNextSteps` hydrates the cache, but "Record not found" flashes first. `draftFor` hard-codes "(MPS IIIC)" and "Type C", so drop those strings or call `draftOutreachEmail`. Partners with `email: ""` produce an empty `mailto:`.
- `src/routes/search.tsx`: text and examples are tuned to the mocks (Sanfilippo, HGSNAT). Gene and approach searches return no matches because only `type: disease` hits are shown. `diseases.length` counts only cached records. The honest-gap button links to `disease-z`.
- `src/routes/contribute.tsx`: the condition `<select>` is empty until diseases are cached. Optionally wire it to `addResearchDocument`.
- `src/components/atlas/atlas-chat-demo.tsx`: `answer()` is synchronous over `allEdges`/`diseases`.
- `src/components/atlas/evidence-drawer.tsx`: shows `source_id`, now the URL, as text. Make it a link and resolve `evidence_edges` with `getEvidence` to list the quotes.
- `src/lib/ai/atlas-context.server.ts`: reads `diseases`/`allEdges` on the server, which are empty in API mode.
- `src/components/atlas/atlas-shell.tsx`: uses `diseases.slice(0,10)` only for the decorative canvas. Harmless.

## Chat plan
`POST /chat` is stateless JSON and does not stream. The live `AtlasChat` uses AI SDK streaming through `/api/chat` with Supabase threads. The simplest path:
1. In `AtlasChatDemo.ask()`, push the user turn, call `askAtlas(diseaseId, turns.map(t => ({ role: t.role, content: t.text })))`, then push `answer`. Show a "Reviewing the chart…" placeholder while it waits.
2. Render `[E…]` markers as chips. Split the answer on `/\[(E[0-9a-f]+(?:,\s*E[0-9a-f]+)*)\]/`. Use `citationIds` from the adapter, and on click call `getEvidence([id])` and open the existing `EvidenceDrawer`. Show a "not grounded" note when `grounded === false`.
3. Leave `/api/chat` and the OpenAI attribution untouched for the live mode. If needed later, `/api/chat` could proxy to `POST /chat` and send the answer back as one text part.

## Known gaps
- There is no backend endpoint that lists diseases, so `diseases` starts empty. `exports/json/index.json` could seed it.
- Similar-disease neighbours only have a gene when their `caused_by` edge is in the focus subgraph, and they have no description until they are opened.
- `journey` (the default) has `cards: []`, so descriptions come from `attrs.description`. Use `VITE_ATLAS_USE_RECOMMEND=true` for card text. The first uncached call is slow.
- `getClusterAssets`, `getFasterRoute` and `rankClustersByApproach` have no backend mapping.
- Not tested against a running backend. Only type-checked (`tsc --noEmit` passes).
