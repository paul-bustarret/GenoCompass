import { ExternalLink, CircleAlert } from "lucide-react";
import { Sheet, SheetContent, SheetTitle, SheetDescription } from "@/components/ui/sheet";
import { allEdges } from "@/lib/atlas";
import type { AtlasEdge } from "@/lib/atlas-db";

export function EvidenceDrawer({ edge, onClose }: { edge: AtlasEdge | null; onClose: () => void }) {
  const fromDb = edge?.provenance === "database";
  const label =
    edge?.evidence_type === "curated"
      ? "Curated"
      : edge?.evidence_type === "extracted"
        ? "AI-extracted"
        : "Inferred, needs expert review";
  const level = edge
    ? ({
        hypothesis: "Idea, not yet tested",
        preclinical: "Seen in lab or animal studies",
        clinical_observation: "Seen in patients",
        clinical_trial: "Shown in a clinical trial",
      }[edge.evidence_level] ?? "Strength not recorded")
    : "";
  return (
    <Sheet
      open={Boolean(edge)}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <SheetContent
        side="right"
        className="evidence-drawer"
        aria-describedby="evidence-description"
      >
        <SheetTitle>Why this connection?</SheetTitle>
        <SheetDescription id="evidence-description">
          Source record and confidence details.
        </SheetDescription>
        {edge && (
          <>
            <div className="drawer-top">
              <span className="eyebrow">
                {fromDb ? "SOURCE RECORD / ATLAS DATABASE" : "SOURCE RECORD / DEMO"}
              </span>
            </div>
            <p className="drawer-lead">{edge.plain_explanation}</p>
            <div className="evidence-line">
              <span className={`line-sample ${edge.evidence_type}`} />
              <strong>{label}</strong>
              <span className="muted">· {level}</span>
            </div>
            <div className="drawer-rule" />
            <dl className="source-list">
              <div>
                <dt>Source</dt>
                <dd>{edge.source_name}</dd>
              </div>
              <div>
                <dt>Record ID</dt>
                <dd>
                  {edge.source_id} <ExternalLink size={13} />
                </dd>
              </div>
              <div>
                <dt>Retrieved</dt>
                <dd>{edge.retrieved_at}</dd>
              </div>
              <div>
                <dt>Confidence</dt>
                <dd>
                  {Math.round(edge.confidence * 100)}%
                  {fromDb ? " · similarity score" : " · illustrative"}
                </dd>
              </div>
              <div>
                <dt>Independent sources</dt>
                <dd>
                  {fromDb
                    ? `${edge.witnesses.length} shared signal${edge.witnesses.length === 1 ? "" : "s"}; not independently verified`
                    : "1 mock record; not independently verified"}
                </dd>
              </div>
            </dl>
            {edge.contradicted_by.length > 0 && (
              <section className="contradiction">
                <CircleAlert size={18} />
                <div>
                  <strong>Contradicting evidence</strong>
                  <p>
                    This link has a counterpoint in the dataset. It should not be treated as a
                    validated treatment connection.
                  </p>
                  {edge.contradicted_by.map((id) => (
                    <p key={id}>{allEdges.find((e) => e.id === id)?.plain_explanation}</p>
                  ))}
                </div>
              </section>
            )}
            <p className="drawer-note">
              {fromDb
                ? "Computed by the atlas from shared genes, pathways and symptoms in the connected database. It is a lead to investigate, not a verified scientific claim."
                : "This record is illustrative mock data, not a verified scientific citation. Source links will be added when real data is connected."}
            </p>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}
