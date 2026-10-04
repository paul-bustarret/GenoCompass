# Brief: refine the Geno Compass demo script

You are refining the demo for **Geno Compass**, a hackathon prototype ("AI Atlas for Rare Diseases",
Hack-Nation Challenge 05). The site runs locally; you can see it in Chrome. Your output is a new
script, **`DEMO_v2.md`** in this folder, built only from what you actually see on screen.

Read `DEMO.md` (the current script) first — `DEMO_v2.md` replaces it.

## Steps

1. **Confirm the site is live.** Open http://localhost:8080 (or the port the user gives you) and
   http://localhost:8000/health. Done when the home page shows the "The atlas today" numbers and
   `/health` returns `"status": "ok"`. If either fails, stop and ask the user to start the two
   servers (commands in `DEMO.md` → "Before you start").
2. **Walk the demo path and take notes.** Visit, in order, and note what is on screen, what is
   clickable, and anything slow, broken or confusing:
   - `/` → home (headline + "The atlas today")
   - `/who` → choose a perspective
   - `/search` → type `Tay-Sachs`
   - `/atlas/MONDO:0010100` → connection map; click the Tay-Sachs–Sandhoff line and open "Why?"
   - `/disease/MONDO:0010100` → next steps, "Who can help" (families / therapies / institutions),
     "Draft an outreach email"
   - the chart assistant (button on the map page): ask the questions listed under Reference
   - `/contribute` → if it shows a paper form, submit one PubMed link and watch the result
   - `/atlas/MONDO:0009757` → Niemann-Pick C, the honest-gap example
   Done when every page above has a note — including the ones that failed.
3. **Write `DEMO_v2.md`.** Done when it contains every section in "Output format" and every on-screen
   claim in it matches a note from step 2.
4. **Tell the user** what changed versus `DEMO.md` and list any bugs you hit, each with page + what
   you clicked.

## Output format (`DEMO_v2.md`)

1. **One-line story** (who the user is and what they need).
2. **Setup** — copy "Before you start" from `DEMO.md` unchanged.
3. **1-minute walkthrough** — a table: step · what to click · what to say (one or two sentences,
   spoken, plain English). This is the judged "1-minute walkthrough" video.
4. **3-minute version** — the same path plus the evidence drawer, the AI chat, the paper upload
   and the honest gap, with what to say while the AI is thinking.
5. **Lines for the pitch** — problem (one sentence), why AI and not a search engine, how hallucination
   is prevented, the numbers from the home page, the 10× claim.
6. **Q&A cheat sheet** — 5–8 likely judge questions with short answers.
7. **Known rough edges** — what to avoid clicking on camera.

## Reference

**Audience and voice.** Speaker and listeners have little biology background. Use everyday words;
when a term is unavoidable (gene, enzyme, clinical trial), explain it in a few words the first time.
Short spoken sentences. Warm, concrete, about a family.

**What judges score** (from the challenge brief): graph quality (meaningful links, counterexamples,
uncertainty shown), evidence integrity (sources, AI-extracted vs database, contradictions), patient
progress (diagnosis → a collaborator, a reusable asset, a next step — or an honest gap), the 10×
argument (a milestone reached faster, with stated assumptions), product craft.

**Story already chosen.** A mother in India; her baby has Tay-Sachs (inherited, nerve cells can't
clear a fatty waste, no cure). The atlas shows: diseases that break the same way (Sandhoff, GM2 AB
variant, GM1); her own patient groups (NTSAD in the US, CATS Foundation in the UK); the Tay-Sachs and
Sandhoff Disease Registry; a clinical trial of an oral medicine (Nizubaglustat, by Azafaros) with
sites in many countries including India; an email draft to `info@ntsad.org` that is never sent
automatically.

**Chat questions to try** (answers are generated live by Claude, ~5–10 s each — show that it is
working, don't cut it):
- `Is there a clinical trial for Tay-Sachs outside the US?`
- `Which patient groups work on Tay-Sachs?`
- `How is Tay-Sachs related to Sandhoff disease?`

**How the AI is used** (for the pitch): it reads research papers and patient-group websites and turns
them into links on the map; each link must quote the exact sentence from its source, and code checks
the quote is really there and on topic; it explains the map and answers questions only from those
sourced links; it drafts outreach emails for a person to review.

**Rules for the script.**
- Every number comes from the home page's "The atlas today" band, as displayed.
- Every feature claim is something you saw working in step 2.
- Medical framing stays at "questions to bring to your care team" — the site is a research navigation
  tool.
- Credit the AI as the site does (the footer reads "Powered by OpenAI"; keep that wording as is).

**Known rough edges** (verify, then list in the script): Google sign-in on some pages is not part of
the demo; Niemann-Pick C still shows faint dotted lines although its panel explains there is no
supported link; a brand-new chat question can occasionally take longer or fail once — asking again
works.
