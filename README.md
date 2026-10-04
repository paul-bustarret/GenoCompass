# Rare Disease Atlas

An evidence-led rare-disease map prototype. The current review includes the introduction, four perspectives, search, constellation map, evidence inspection, an honest-gap example, and a Type C partner and outreach page. Contact details and research programs are public-source references, not a live directory; the email draft opens in the visitor’s email app and is never sent automatically.

## Run locally (website + knowledge-graph backend)

**Needs once per computer**

| | macOS | Windows |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) (Python) | `brew install uv` | `winget install astral-sh.uv` |
| Node.js | `brew install node` | `winget install OpenJS.NodeJS.LTS` |
| Claude Code, logged in (chat, email drafts, adding papers) | [install](https://docs.claude.com/en/docs/claude-code) then run `claude` once | same |

Open a terminal **in the repo root** (the folder with `backend/` and `src/`). First time only:

```
npm install
```

**Every time — two terminals, both starting in the repo root.** The commands are the same on
macOS, Linux and Windows (PowerShell or Command Prompt); type them line by line.

Terminal 1 — backend (data + AI) on http://localhost:8000:

```
cd backend
uv run python -m atlas.server
```

Terminal 2 — website, connected to that backend:

```
npm run demo
```

Open **http://localhost:8080** (the address terminal 2 prints; if 8080 is busy it uses the next
free port — close old servers first). Stop both with `Ctrl+C`.

- Connected? The home page shows **"The atlas today"** with live numbers, and
  http://localhost:8000/health returns `"status": "ok"`.
- **Demo mode (default):** papers added on the "Add a paper" page are removed when the backend stops
  with `Ctrl+C`, so each run starts from the same data. To keep them, start the backend with
  `ATLAS_PERSIST=1 uv run python -m atlas.server` (macOS/Linux) or
  `$env:ATLAS_PERSIST=1; uv run python -m atlas.server` (PowerShell). Reset by hand:
  `git checkout -- backend/data/graph`.
- `npm run demo` reads `.env.demo` (`VITE_ATLAS_API_URL=http://localhost:8000`). Plain `npm run dev`
  runs the website without the backend (Supabase / sample data).

Website only (no backend): `bun install && bun run dev`, or `npm install && npm run dev`.
Full demo walkthrough: [DEMO.md](DEMO.md). Backend details: [backend/README.md](backend/README.md).

## Architecture

TanStack Start provides the routes. `src/components/atlas/atlas-shell.tsx` holds the shared background and selected persona. The `?as=maria|devon|priya|osei` query makes perspectives shareable. `src/data/nodes.json` and `src/data/edges.json` contain illustrative records accessed through `src/lib/atlas.ts`. The search and connection map can load the knowledge graph from Supabase through `src/lib/atlas-db.ts`; the map falls back to the local mock records when that database is unavailable.

## Data contract and future sources

Nodes encode diseases, clusters and organizations with IDs, names, synonyms, cross-references, activity and attributes. Edges encode relationships with plain explanations, evidence type and level, source information, confidence and contradictions. Real data may later come from OMIM (disease/gene), HPO (phenotypes), ClinVar (variants), PubMed (publications), ClinicalTrials.gov (trials), NIH RePORTER (grants) and NORD/Orphanet (disease/community information). Placeholder IDs (`TBD-*`) are deliberately not real citations.

Database migrations are in `supabase/migrations`. Computed graph links are research leads, not verified clinical claims. The model-powered atlas assistant runs on the server and keeps conversations under the signed-in user's account. The chart assistant test mode uses labeled sample answers in the browser.

## Contributors

See [CONTRIBUTORS.md](CONTRIBUTORS.md) for contributor credits.
