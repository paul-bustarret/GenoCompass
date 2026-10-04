// Local-demo contribute flow: submit a PubMed paper (or pasted abstract) and let the backend read it
// (POST /documents). No sign-in; used only when VITE_ATLAS_API_URL is set.
import { Link } from "@tanstack/react-router";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { ArrowRight, ArrowUpRight, CheckCircle2, Info, Loader2, TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  submitDocument,
  type ApiAddedLink,
  type ApiDocumentResult,
  type ApiRejectedClaim,
} from "@/lib/atlas-api";
import { invalidateAtlasCache, loadAtlasGraph, type AtlasDisease } from "@/lib/atlas-db";

const PMID_RE = /pubmed\.ncbi\.nlm\.nih\.gov\/(\d+)|^\s*(?:PMID:?\s*)?(\d{5,9})\s*$/i;

const STAGES = [
  { after: 0, text: "Fetching the abstract from PubMed…" },
  { after: 2, text: "Reading the paper with AI…" },
  { after: 14, text: "Checking every quote word-for-word against the paper…" },
  { after: 28, text: "Almost there — adding the new links to the atlas…" },
];

/** A plain-words phrase for each relation the extractor can add. */
function phrase(link: ApiAddedLink): string {
  if (link.relation === "has_mechanism") return "involves";
  if (link.relation === "caused_by") return "is caused by changes in the gene";
  if (link.relation === "studied_with")
    return link.polarity === "contradicts" ? "was tested with, without benefit:" : "has been studied with";
  return link.relation.replace(/_/g, " ");
}

const CONTEXT: Record<string, string> = {
  human: "in patients",
  animal_model: "in an animal model",
  cell_model: "in cells",
  review: "review article",
};

const REJECT_REASON: Record<string, string> = {
  quote_check: "The AI's supporting quote could not be found word-for-word in the paper",
  relevance: "The quoted sentence is off-topic: it names neither the condition nor the finding",
  unresolved_entity: "Names a gene or treatment the atlas could not identify",
  invalid_type: "Not a kind of finding the atlas records",
};

function friendlyReason(reason: string): string {
  if (reason.startsWith("no slice disease"))
    return "The paper doesn't mention any condition in the atlas by name, so nothing was added.";
  if (reason.startsWith("PubMed has no abstract"))
    return "PubMed has no abstract for that ID. Check the number, or paste the abstract text instead.";
  if (reason.startsWith("could not reach PubMed") || reason.startsWith("could not fetch"))
    return "We couldn't reach the source just now. Please try again in a moment.";
  if (reason.startsWith("the AI reader")) return "The AI reader couldn't be reached. Please try again.";
  if (reason.startsWith("no text")) return "Add a PubMed link, an ID, or the abstract text.";
  return reason;
}

export function PaperContribute() {
  const [diseases, setDiseases] = useState<AtlasDisease[]>([]);
  const [mode, setMode] = useState<"pubmed" | "text">("pubmed");
  const [link, setLink] = useState("");
  const [text, setText] = useState("");
  const [name, setName] = useState("");
  const [disease, setDisease] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [took, setTook] = useState(0);
  const [result, setResult] = useState<ApiDocumentResult | null>(null);
  const resultRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    loadAtlasGraph()
      .then((g) => setDiseases(g.diseases.slice().sort((a, b) => a.label.localeCompare(b.label))))
      .catch(() => setDiseases([]));
  }, []);

  useEffect(() => {
    if (!busy) return;
    const start = Date.now();
    setElapsed(0);
    const t = setInterval(() => setElapsed(Math.floor((Date.now() - start) / 1000)), 500);
    return () => clearInterval(t);
  }, [busy]);

  useEffect(() => {
    if (result) resultRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [result]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setResult(null);
    const by = name.trim();
    if (!by) return setError("Tell us your name or role so the new links are credited to you.");
    let body: Parameters<typeof submitDocument>[0];
    if (mode === "pubmed") {
      const m = PMID_RE.exec(link.trim());
      if (!m) return setError("Paste a PubMed link (pubmed.ncbi.nlm.nih.gov/…) or a PubMed ID.");
      body = { url: `https://pubmed.ncbi.nlm.nih.gov/${m[1] ?? m[2]}/`, submitted_by: by };
    } else {
      if (text.trim().length < 200)
        return setError("Paste the full abstract (at least a few sentences).");
      body = { text: text.trim(), submitted_by: by };
    }
    if (disease) body.disease_ids = [disease];
    setBusy(true);
    const t0 = Date.now();
    try {
      const res = await submitDocument(body);
      if (res.error) throw new Error(res.error);
      setTook(Math.round((Date.now() - t0) / 1000));
      setResult(res);
      const touched = (res.matched_diseases ?? []).map((d) => d.id);
      if (res.added_edges.length > 0) invalidateAtlasCache(touched);
    } catch (err) {
      setError(
        err instanceof Error && err.message.includes("took too long")
          ? "Reading the paper took too long. Please try again."
          : "The atlas server couldn't read this paper. Is the local backend running?",
      );
    } finally {
      setBusy(false);
    }
  }

  const stage = [...STAGES].reverse().find((s) => elapsed >= s.after) ?? STAGES[0]!;

  return (
    <div className="contribute-grid paper-contribute">
      <form className="contribute-form" onSubmit={submit} aria-busy={busy}>
        <div className="paper-mode" role="tablist" aria-label="How to share the paper">
          <button
            type="button"
            role="tab"
            aria-selected={mode === "pubmed"}
            onClick={() => setMode("pubmed")}
          >
            PubMed link or ID
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === "text"}
            onClick={() => setMode("text")}
          >
            Paste the abstract
          </button>
        </div>
        {mode === "pubmed" ? (
          <label>
            PubMed link or ID
            <input
              name="pubmed"
              value={link}
              onChange={(e) => setLink(e.target.value)}
              placeholder="https://pubmed.ncbi.nlm.nih.gov/41819452/ or 41819452"
              autoComplete="off"
            />
          </label>
        ) : (
          <label>
            Abstract text
            <textarea
              name="abstract"
              value={text}
              onChange={(e) => setText(e.target.value)}
              maxLength={12000}
              placeholder="Paste the title and abstract of the paper…"
            />
          </label>
        )}
        <div className="contribute-row">
          <label>
            Your name / role
            <input
              name="submitted_by"
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={120}
              placeholder="e.g. Dr Lee, paediatric neurologist"
            />
          </label>
          <label>
            Condition
            <select name="disease" value={disease} onChange={(e) => setDisease(e.target.value)}>
              <option value="">Detect automatically</option>
              {diseases.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.label}
                </option>
              ))}
            </select>
          </label>
        </div>
        {error && <p className="contribute-error">{error}</p>}
        <Button type="submit" disabled={busy}>
          {busy ? (
            <>
              <Loader2 size={15} className="animate-spin" /> Reading…
            </>
          ) : (
            <>
              Add to the atlas <ArrowRight size={15} />
            </>
          )}
        </Button>
        <p className="mock-note">
          <Info size={14} /> AI reads the abstract and proposes findings. Each one is kept only if its
          supporting sentence appears word-for-word in the paper and names the condition.
        </p>
      </form>

      <aside className="paper-result" ref={resultRef} aria-live="polite">
        {busy ? (
          <div className="paper-progress" role="status">
            <Loader2 size={22} className="animate-spin" />
            <strong>{stage.text}</strong>
            <small>{elapsed}s · this usually takes under 30 seconds</small>
          </div>
        ) : result ? (
          <PaperResult result={result} took={took} />
        ) : (
          <div className="paper-empty">
            <span className="eyebrow">WHAT HAPPENS NEXT</span>
            <ol>
              <li>We fetch the abstract from PubMed.</li>
              <li>AI reads it and proposes links: mechanisms, treatments studied, causal genes.</li>
              <li>Every link must quote the paper exactly — anything else is left out.</li>
              <li>Kept links appear on the map right away, credited to you.</li>
            </ol>
          </div>
        )}
      </aside>
    </div>
  );
}

function PaperResult({ result, took }: { result: ApiDocumentResult; took: number }) {
  const added = result.added ?? [];
  const rejected = result.rejected ?? [];
  const matched = result.matched_diseases ?? [];
  const contradicting = added.filter((a) => a.polarity === "contradicts");
  const droppedTotal = Object.values(result.dropped ?? {}).reduce((a, b) => a + b, 0);
  const isLink = result.source_url?.startsWith("http");
  return (
    <div className="paper-result-body">
      <span className="eyebrow">
        {added.length > 0 ? <CheckCircle2 size={13} /> : <Info size={13} />} RESULT · {took}s
      </span>
      <h2>
        {added.length > 0
          ? `${added.length} new link${added.length === 1 ? "" : "s"} added to the atlas`
          : "Nothing new was added"}
      </h2>
      {result.title && (
        <p className="paper-title">
          {isLink ? (
            <a href={result.source_url!} target="_blank" rel="noreferrer">
              {result.title} <ArrowUpRight size={13} />
            </a>
          ) : (
            result.title
          )}
        </p>
      )}
      {result.reason && <p className="paper-reason">{friendlyReason(result.reason)}</p>}
      {matched.length > 0 && (
        <p className="paper-matched">
          Matched to <strong>{matched.map((d) => d.name).join(", ")}</strong>
        </p>
      )}

      {added.length > 0 && (
        <ul className="paper-links">
          {added.map((a) => (
            <li key={a.id} className={a.polarity === "contradicts" ? "contradicts" : undefined}>
              <span className="paper-link-line">
                {a.source_name} <em>{phrase(a)}</em> <strong>{a.target_name}</strong>
              </span>
              {(a.context || a.polarity === "contradicts") && (
                <small>
                  {a.polarity === "contradicts" && (
                    <span className="paper-flag">
                      <TriangleAlert size={12} /> Negative finding
                    </span>
                  )}
                  {a.context ? CONTEXT[a.context] ?? a.context : ""}
                </small>
              )}
              <blockquote>“{a.quote}”</blockquote>
            </li>
          ))}
        </ul>
      )}

      {contradicting.length > 0 && (
        <p className="paper-note warn">
          <TriangleAlert size={14} /> {contradicting.length} finding
          {contradicting.length === 1 ? " reports" : "s report"} a negative result. It is shown on the
          map as contradicting evidence, not as support.
        </p>
      )}
      {(result.already_present ?? 0) > 0 && (
        <p className="paper-note">
          {result.already_present} finding{result.already_present === 1 ? " was" : "s were"} already in
          the atlas from this paper.
        </p>
      )}

      {droppedTotal > 0 && (
        <details className="paper-rejected" open={added.length === 0}>
          <summary>
            {droppedTotal} proposed finding{droppedTotal === 1 ? " was" : "s were"} left out
          </summary>
          <ul>
            {rejected.map((r: ApiRejectedClaim, i) => (
              <li key={i}>
                <strong>{REJECT_REASON[r.reason] ?? r.reason}</strong>
                {r.object && <small>Proposed: {r.object.replace(/_/g, " ")}</small>}
                {r.quote && <blockquote>“{r.quote}”</blockquote>}
              </li>
            ))}
          </ul>
        </details>
      )}

      {matched.length > 0 && (
        <div className="paper-actions">
          {matched.map((d) => (
            <Button asChild key={d.id}>
              <Link to="/atlas/$id" params={{ id: d.id }}>
                See it on the map{matched.length > 1 ? ` · ${d.short}` : ""} <ArrowRight size={15} />
              </Link>
            </Button>
          ))}
        </div>
      )}
      <p className="mock-note">
        <Info size={14} /> Credited to {result.submitted_by}. AI-extracted links are research leads, marked
        as such on the map.
      </p>
    </div>
  );
}
