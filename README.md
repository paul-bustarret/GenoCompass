# Rare Disease Atlas

An evidence-led rare-disease map prototype. The current review includes the introduction, four perspectives, search, constellation map, evidence inspection, an honest-gap example, and a Type C partner and outreach page. Contact details and research programs are public-source references, not a live directory; the email draft opens in the visitor’s email app and is never sent automatically.

## Run locally

```sh
bun install && bun run dev
```

## Architecture

TanStack Start provides the routes. `src/components/atlas/atlas-shell.tsx` holds the shared background and selected persona. The `?as=maria|devon|priya|osei` query makes perspectives shareable. `src/data/nodes.json` and `src/data/edges.json` contain illustrative records accessed through `src/lib/atlas.ts`. The search and connection map can load the knowledge graph from Supabase through `src/lib/atlas-db.ts`; the map falls back to the local mock records when that database is unavailable.

## Data contract and future sources

Nodes encode diseases, clusters and organizations with IDs, names, synonyms, cross-references, activity and attributes. Edges encode relationships with plain explanations, evidence type and level, source information, confidence and contradictions. Real data may later come from OMIM (disease/gene), HPO (phenotypes), ClinVar (variants), PubMed (publications), ClinicalTrials.gov (trials), NIH RePORTER (grants) and NORD/Orphanet (disease/community information). Placeholder IDs (`TBD-*`) are deliberately not real citations.

Database migrations are in `supabase/migrations`. Computed graph links are research leads, not verified clinical claims. The model-powered atlas assistant runs on the server and keeps conversations under the signed-in user's account. The chart assistant test mode uses labeled sample answers in the browser.

## Contributors

See [CONTRIBUTORS.md](CONTRIBUTORS.md) for contributor credits.
