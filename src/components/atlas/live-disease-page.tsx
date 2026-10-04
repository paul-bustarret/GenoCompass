// Disease next-steps page served by the local backend (VITE_ATLAS_API_URL): partners and
// next steps from GET /journey, the outreach draft from POST /email. Nothing is ever sent.
import { Link } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  Check,
  ExternalLink,
  FlaskConical,
  HeartHandshake,
  Landmark,
  Loader2,
  Mail,
  MapPin,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { useAtlas } from "@/components/atlas/atlas-shell";
import { loadAtlasGraph, type AtlasDisease } from "@/lib/atlas-db";
import {
  draftEmail,
  getEdge,
  getJourney,
  stripCitations,
  type ApiEmailDraft,
  type ApiJourney,
  type ApiNextStep,
  type ApiOrganization,
} from "@/lib/atlas-api";
import {
  CATEGORY_INFO,
  CATEGORY_ORDER,
  partnersFor,
  roleOf,
  type PartnerCategory,
} from "@/lib/partner-category";

const countryName = (() => {
  let names: Intl.DisplayNames | null = null;
  try {
    names = new Intl.DisplayNames(["en"], { type: "region" });
  } catch {
    names = null;
  }
  return (code: string | null) => {
    if (!code) return "Country not recorded";
    try {
      return names?.of(code) ?? code;
    } catch {
      return code;
    }
  };
})();

const categoryIcon: Record<PartnerCategory, typeof HeartHandshake> = {
  families: HeartHandshake,
  therapies: FlaskConical,
  institutions: Landmark,
};

const stepTitle: Record<ApiNextStep["kind"], string> = {
  contact: "Reach out",
  join: "Join what already exists",
  reuse: "Reuse from a related condition",
  build: "Build what is missing",
};

type Step = {
  title: string;
  text: string;
  review: boolean;
  org?: ApiOrganization | undefined;
  edges: string[];
};

/** Three next steps: the journey's own, or (if it has none) contacting the linked organisations. */
function planFor(j: ApiJourney): Step[] {
  const orgs = new Map(j.organizations.map((o) => [o.id, o]));
  const steps: Step[] = j.next_steps.slice(0, 3).map((s) => ({
    title: stepTitle[s.kind],
    text: s.text,
    review: s.kind === "reuse" || s.kind === "build",
    org: s.target_id ? orgs.get(s.target_id) : undefined,
    edges: s.edges,
  }));
  if (steps.length) return steps;
  return j.organizations.slice(0, 3).map((o) => ({
    title: stepTitle.contact,
    text: `Contact ${o.name} (${roleOf(o, j).toLowerCase()}, ${countryName(o.country)}), which the atlas links to ${j.disease.short ?? j.disease.name}.`,
    review: false,
    org: o,
    edges: o.edges,
  }));
}

/** "Why?" link: the source page of the step's first sourced record. */
function WhyLink({ edges }: { edges: string[] }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    const first = edges[0];
    if (first)
      getEdge(first)
        .then((e) => {
          if (active) setUrl(e.source_url);
        })
        .catch(() => {});
    return () => {
      active = false;
    };
  }, [edges]);
  if (!url) return null;
  return (
    <a href={url} target="_blank" rel="noopener noreferrer" className="why-link">
      Why?
    </a>
  );
}

export function LiveDiseasePage({ id }: { id: string }) {
  const { persona } = useAtlas();
  const [disease, setDisease] = useState<AtlasDisease | null>(null);
  const [journey, setJourney] = useState<ApiJourney | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hovered, setHovered] = useState("");
  const [draftFor, setDraftFor] = useState<string | null | undefined>(undefined); // undefined = closed
  const [draft, setDraft] = useState<ApiEmailDraft | null>(null);
  const [drafting, setDrafting] = useState(false);
  const [draftError, setDraftError] = useState<string | null>(null);
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [opened, setOpened] = useState(false);

  useEffect(() => {
    let active = true;
    setJourney(null);
    setError(null);
    loadAtlasGraph()
      .then((g) => {
        if (active) setDisease(g.diseases.find((d) => d.id === id) ?? null);
      })
      .catch(() => {});
    getJourney(id)
      .then((j) => {
        if (active) setJourney(j);
      })
      .catch((e: unknown) => {
        if (active) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      active = false;
    };
  }, [id]);

  const plan = useMemo(() => (journey ? planFor(journey) : []), [journey]);
  const helpers = useMemo(() => (journey ? partnersFor(journey, id) : []), [journey, id]);

  async function openDraft(orgId: string | null) {
    setDraftFor(orgId);
    setDraft(null);
    setDraftError(null);
    setOpened(false);
    setDrafting(true);
    try {
      const d = await draftEmail(id, orgId, {
        name: "",
        role: persona === "maria" ? "patient group leader" : "",
        context: "",
      });
      setDraft(d);
      setSubject(d.subject);
      setBody(d.body);
    } catch (e) {
      setDraftError(e instanceof Error ? e.message : String(e));
    } finally {
      setDrafting(false);
    }
  }

  function openEmail() {
    if (!draft?.to || !subject.trim() || !body.trim()) return;
    window.location.href = `mailto:${draft.to}?subject=${encodeURIComponent(subject.trim().slice(0, 200))}&body=${encodeURIComponent(body.trim().slice(0, 4000))}`;
    setOpened(true);
  }

  if (error)
    return (
      <section className="content-width journey-page">
        <h1>Record not found</h1>
        <p>{error}</p>
        <Button asChild>
          <Link to="/search" search={{ as: persona } as never}>
            Search the atlas
          </Link>
        </Button>
      </section>
    );

  const name = journey?.disease.name ?? disease?.label ?? id;
  const recipientName =
    draft?.org_id && journey
      ? (journey.organizations.find((o) => o.id === draft.org_id)?.name ?? draft.org_id)
      : "the best-matched organisation";

  return (
    <section className="steps-page">
      <header className="steps-head">
        <Link
          className="back-link"
          to="/atlas/$id"
          params={{ id }}
          search={{ as: persona } as never}
        >
          <ArrowLeft size={14} /> Back to map
        </Link>
        <div>
          <h1>{name}</h1>
          <p>{disease?.attributes.description}</p>
        </div>
      </header>
      {!journey ? (
        <p className="partner-empty">Reviewing available contacts…</p>
      ) : plan.length === 0 ? (
        <div className="partner-empty">
          <h3>No verified partner records for this condition yet</h3>
          <p>
            The atlas has not linked an organisation or study to this condition. We won’t substitute
            contacts from another condition.
          </p>
          {journey.missing.length > 0 && <p>Not yet searched: {journey.missing.join("; ")}.</p>}
        </div>
      ) : (
        <>
          <h2 className="steps-section-title">Your {plan.length} next steps</h2>
          <div className="steps-grid">
            {plan.map((step, i) => (
              <article
                key={`${step.text}-${i}`}
                className={`step-card accent-${i + 1} ${step.org && hovered === step.org.id ? "linked" : ""}`}
                onMouseEnter={() => setHovered(step.org?.id ?? "")}
                onMouseLeave={() => setHovered("")}
                tabIndex={0}
              >
                <span className="step-num">{String(i + 1).padStart(2, "0")}</span>
                <h3>{step.title}</h3>
                <p>{stripCitations(step.text)}</p>
                <div className="step-meta">
                  {step.org && <span className="partner-chip">{step.org.name}</span>}
                  <span className="evidence-chip curated">Sourced</span>
                  <WhyLink edges={step.edges} />
                  {step.review && (
                    <span className="review-flag">
                      <AlertTriangle size={12} /> Needs expert review
                    </span>
                  )}
                </div>
              </article>
            ))}
          </div>
          <div className="party-actions" style={{ marginTop: 0, justifyContent: "flex-start" }}>
            <Button onClick={() => void openDraft(null)}>
              <Mail size={15} /> Draft an outreach email
            </Button>
          </div>
          <h2 className="steps-section-title">Who can help</h2>
          <div className="steps-grid">
            {CATEGORY_ORDER.map((cat) => {
              const info = CATEGORY_INFO[cat];
              const CatIcon = categoryIcon[cat];
              const items = helpers.filter((p) => p.category === cat);
              return (
                <section
                  key={cat}
                  className={`accent-${info.accent}`}
                  style={{ display: "flex", flexDirection: "column", gap: 10, minWidth: 0 }}
                  aria-label={info.label}
                >
                  <div className="party-top">
                    <span className="party-icon">
                      <CatIcon size={18} />
                    </span>
                    <div>
                      <span className="eyebrow">{info.label.toUpperCase()}</span>
                      <p className="party-loc" style={{ margin: 0 }}>
                        {info.blurb}
                      </p>
                    </div>
                  </div>
                  {items.length === 0 && (
                    <p className="party-loc">No {info.label.toLowerCase()} linked to this condition yet.</p>
                  )}
                  {items.map(({ org, role, description }) => (
                    <article
                      key={org.id}
                      data-category={cat}
                      className={`party-card accent-${info.accent} ${hovered === org.id ? "linked" : ""}`}
                    >
                      <div className="party-top">
                        <span className="party-icon">
                          <CatIcon size={18} />
                        </span>
                        <div style={{ minWidth: 0 }}>
                          <span className="eyebrow">
                            {info.label} · {role}
                          </span>
                          <h3>{org.name}</h3>
                        </div>
                      </div>
                      <p className="party-loc">
                        <MapPin size={12} /> {countryName(org.country)}
                      </p>
                      <p className="party-approach" title={description}>
                        {description}
                      </p>
                      <span className="stage-badge">{info.stage}</span>
                      <div className="party-actions">
                        {org.contact_email ? (
                          <a href={`mailto:${org.contact_email}`}>{org.contact_email}</a>
                        ) : org.website ? (
                          <a href={org.website} target="_blank" rel="noopener noreferrer">
                            Website <ExternalLink size={12} />
                          </a>
                        ) : (
                          <span className="muted">No public contact recorded</span>
                        )}
                        <Button size="sm" variant="outline" onClick={() => void openDraft(org.id)}>
                          <Mail size={14} /> Draft email
                        </Button>
                      </div>
                    </article>
                  ))}
                </section>
              );
            })}
          </div>
          <p className="steps-disclaimer">
            Public research contacts to discuss, not treatment recommendations or confirmed
            opportunities.
          </p>
        </>
      )}
      <Sheet
        open={draftFor !== undefined}
        onOpenChange={(o) => {
          if (!o) setDraftFor(undefined);
        }}
      >
        <SheetContent side="right" className="outreach-sheet">
          <SheetHeader>
            <SheetTitle>Write to {draft ? recipientName : "a partner"}</SheetTitle>
            <SheetDescription>
              Drafted from cited atlas records. Edit it; nothing is sent from the atlas.
            </SheetDescription>
          </SheetHeader>
          {drafting ? (
            <p className="partner-empty">
              <Loader2 size={16} className="animate-spin" /> Drafting from the atlas records…
            </p>
          ) : draftError ? (
            <p className="chat-error" role="alert">
              {draftError}
            </p>
          ) : draft ? (
            <div className="outreach-form">
              <div className="outreach-recipient">
                <span className="field-label">TO</span>
                <strong>{recipientName}</strong>
                <span>{draft.to ?? "No usable email address on record"}</span>
                <small>{draft.recipient_reason}</small>
              </div>
              {draft.warnings.length > 0 && (
                <ul className="outreach-warnings">
                  {draft.warnings.map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              )}
              <label htmlFor="outreach-subject">Subject</label>
              <input
                id="outreach-subject"
                value={subject}
                maxLength={200}
                onChange={(e) => {
                  setSubject(e.target.value);
                  setOpened(false);
                }}
              />
              <label htmlFor="outreach-message">Message</label>
              <textarea
                id="outreach-message"
                value={body}
                maxLength={6000}
                onChange={(e) => {
                  setBody(e.target.value);
                  setOpened(false);
                }}
              />
              <div className="outreach-actions">
                <Button onClick={openEmail} disabled={!draft.to || !subject.trim() || !body.trim()}>
                  <Mail size={15} /> Open in email app
                </Button>
                <span>
                  {opened ? (
                    <>
                      <Check size={14} /> Review and press Send there.
                    </>
                  ) : (
                    "Needs your review. You decide whether to send."
                  )}
                </span>
              </div>
            </div>
          ) : null}
        </SheetContent>
      </Sheet>
    </section>
  );
}
