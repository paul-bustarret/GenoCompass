# Demo script — Rare Disease Atlas

**Story:** a mother in India learns her baby has **Tay-Sachs disease**. There is no cure, no local
support group, and she doesn't know where to start. In about a minute the atlas shows her the
families, researchers and a clinical trial that can help — including a trial with a site in India.

## Before you start (2 min)

```bash
# terminal 1 — backend
cd rare-diseases-atlas/backend && uv run python -m atlas.server
# terminal 2 — website
cd rare-diseases-atlas && VITE_ATLAS_API_URL=http://localhost:8000 npx vite dev
```

Open **http://localhost:8080**. Chat answers are generated live every time (Claude Sonnet reads the
disease's evidence graph, ~5–10 seconds) — say so while it thinks.

## The walkthrough (about 90 seconds)

| # | Do | Say |
|---|---|---|
| 1 | Open `/search`, type **Tay-Sachs** | "Tay-Sachs is a rare inherited disease. Babies are born healthy, then lose abilities they had learned, because their nerve cells can't clear a fatty waste product. There is no cure. Most parents have never heard of it the day they're told." |
| 2 | Open the result → connection map (`/atlas/MONDO:0010100`) | "The atlas doesn't just look up the name. It looks at *how the disease works* — the broken gene, what builds up in the cells, the symptoms — and finds other diseases that break in the same way." |
| 3 | Point at **Sandhoff**, **GM2 AB variant**, **GM1** | "Sandhoff has a different name and a different gene, but the same waste builds up in the same cells. For a family, that means: these communities are working on *your* problem too." |
| 4 | Click a line → evidence panel | "Every line has a source. This one comes from a published paper — here is the exact sentence and the link. If the atlas can't show a source, it doesn't draw the line." |
| 5 | Open the disease page (`/disease/MONDO:0010100`) | "Now: what can she *do* this week? First, her own community — the National Tay-Sachs & Allied Diseases Association in the US and the CATS Foundation in the UK. Then the Tay-Sachs and Sandhoff Disease Registry, so her child's data counts in research." |
| 6 | Show the draft email (to `info@ntsad.org`) | "The atlas drafts a first email for her, with the sources attached. It never sends anything — she reads it and decides." |
| 7 | Open the chat, type: **Is there a clinical trial for Tay-Sachs outside the US?** | "She can ask follow-up questions in plain words." *(Answer: a trial of an oral medicine, Nizubaglustat, for GM2 gangliosidosis — which includes Tay-Sachs — recruiting in Argentina, Australia, Brazil, Canada, France, Germany and India.)* "That's a trial she can ask her doctor about, in her own country." |
| 8 | Close | "Something that normally takes a family months of searching, phone calls and luck — in one minute, with every step backed by a source." |

**Backup chat questions:** `Which patient groups work on Tay-Sachs?` ·
`How is Tay-Sachs related to Sandhoff disease?` · `Is there a cure for Tay-Sachs?`

## Honest moments worth showing (if time)

- **Lookalike, not a match:** Canavan disease also causes early loss of skills in babies, and the same
  US patient group covers it — but the atlas does **not** group it with Tay-Sachs, because the
  underlying cause is different. Similar symptoms ≠ same treatment.
- **An honest gap:** Niemann-Pick type C stays on its own. Nothing in our data shares its biology,
  so the atlas says so instead of guessing.

## Numbers for the pitch

25 rare diseases (20 lysosomal storage diseases + 5 controls) · 8 public sources (PubMed,
ClinicalTrials.gov, NIH RePORTER, ClinVar, HPO, MONDO, Reactome, patient-group websites) ·
**1,734 entities and 3,492 links, each with its source** · 361 papers read · 226 organisations in
56 countries · 88 clinical trials.

## If someone asks…

- **"How is AI used?"** It reads papers and patient-group websites and turns them into links in the
  map; it explains the map in plain language; it answers follow-up questions and drafts emails.
- **"How do you stop it making things up?"** The AI may only add a link if it copies the exact
  sentence from the source; our code checks that sentence is really there and is about the disease.
  Every answer must cite its sources, and sentences without a source are removed.
- **"Why not just ask ChatGPT?"** A chatbot answers from memory and can't show where a fact came
  from. The atlas only says what its sources say, links each claim to the record, and shows
  connections between diseases that no single search would reveal.
- **"Is this medical advice?"** No. It points families to people, studies and questions to bring to
  their care team.
