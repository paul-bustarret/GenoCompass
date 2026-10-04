import { createFileRoute, Link } from "@tanstack/react-router";
import { ArrowRight, ArrowUpRight, Database, Network, MoveUpRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useEffect, useState } from "react";
import { useAtlas } from "@/components/atlas/atlas-shell";
import { getStats, useLocalApi, type AtlasStats } from "@/lib/atlas-api";

// Snapshot of the built graph, shown when the local backend isn't connected (live numbers replace it).
const SNAPSHOT: AtlasStats = {
  diseases: 25, diseases_lysosomal: 20, diseases_controls: 5, papers: 361, papers_read_by_ai: 224,
  pages_read_by_ai: 72, organizations: 226, countries: 56, trials: 88, grants: 75, researchers: 87,
  nodes: 1734, edges: 3492, edges_by_evidence: { observed: 2313, extracted: 1159, inferred: 20 },
  records_searched: 100926,
  sources: ["PubMed", "ClinicalTrials.gov", "NIH RePORTER", "ClinVar", "HPO", "MONDO", "Reactome", "Patient-group websites"],
};
const fmt = (n: number) => n.toLocaleString("en-US");

function AtlasStatsBand() {
  const [s, setS] = useState<AtlasStats>(SNAPSHOT);
  useEffect(() => {
    if (!useLocalApi) return;
    let active = true;
    getStats().then((x) => { if (active) setS(x); }).catch(() => {});
    return () => { active = false; };
  }, []);
  const items: [string, string, string][] = [
    [fmt(s.diseases), "rare diseases", `${s.diseases_lysosomal} lysosomal storage diseases + ${s.diseases_controls} controls`],
    [fmt(s.papers), "research papers", `${fmt(s.papers_read_by_ai)} read by AI, sentence by sentence`],
    [fmt(s.organizations), "organisations", `patient groups, sponsors and funders in ${s.countries} countries`],
    [fmt(s.trials), "clinical trials", `plus ${fmt(s.grants)} research grants and ${fmt(s.researchers)} researchers`],
    [fmt(s.nodes), "connected entities", "genes, symptoms, pathways, mechanisms, registries…"],
    [fmt(s.edges), "sourced links", `${fmt(s.edges_by_evidence["extracted"] ?? 0)} extracted by AI with a verified quote`],
    [fmt(s.records_searched), "records searched", "to choose what goes into the atlas"],
    [String(s.sources.length), "public sources", s.sources.join(" · ")],
  ];
  return <section className="atlas-stats-band"><div className="content-width"><div className="atlas-stats-head"><span className="eyebrow">THE ATLAS TODAY</span><p>One cluster, mapped in depth: <strong>lysosomal storage diseases.</strong> Every link carries its source.</p></div><div className="atlas-stats-grid">{items.map(([n, label, note]) => <div key={label} className="atlas-stat"><strong>{n}</strong><span>{label}</span><small>{note}</small></div>)}</div></div></section>;
}

export const Route = createFileRoute("/")({
 head: () => ({ meta: [{ title: "A Rare Disease Atlas — geno compass" }, { name: "description", content: "Explore an evidence-led map of rare-disease biology, research assets and potential collaborators." }, { property: "og:title", content: "A Rare Disease Atlas — geno compass" }, { property: "og:description", content: "An evidence-led map of rare-disease connections." }, { property: "og:type", content: "website" }, { name: "twitter:card", content: "summary" }] }),
 component: Home,
});
function Home() {
 const { persona } = useAtlas();
  return <><section className="hero content-width"><div className="hero-copy"><div className="section-kicker"><span className="kicker-line"/> A NEW PERSPECTIVE ON RARE DISEASE</div><h1>A Rare Disease<br/><em>Atlas.</em></h1><p className="hero-statement">The connections are there.<br/>We make them visible.</p><p className="hero-description">Find the diseases that share your family’s biology, the research that already exists, and the people who could help move it forward.</p><div className="hero-actions"><Button asChild size="lg" className="primary-cta"><Link to="/who" search={{ as: persona } as never}>Explore the atlas <ArrowRight/></Link></Button><span>Evidence-led. Human-centered.</span></div></div><div className="hero-visual" aria-hidden="true"><span className="visual-orbit orbit-a"/><span className="visual-orbit orbit-b"/><span className="visual-orbit orbit-c"/><span className="visual-node node-a"/><span className="visual-node node-b"/><span className="visual-node node-c"/><span className="visual-node node-d"/><span className="visual-node node-e"/><span className="visual-label label-a">MPS IIIC <small>HGSNAT</small></span><span className="visual-label label-b">MPS IIIA <small>SGSH</small></span><span className="visual-label label-c">MPS IIIB <small>NAGLU</small></span><span className="visual-annotation">FIG. 01 — SHARED BIOLOGY</span></div></section>
 <section className="proof-band"><div className="content-width proof-inner"><div className="proof-title"><span className="eyebrow">THE CHALLENGE</span><p>When knowledge is scattered,<br/><strong>progress is harder to see.</strong></p></div><div className="proof-stat"><strong>10,000<span>+</span></strong><span>known rare diseases</span></div><div className="proof-stat"><strong>350<span>M</span></strong><span>people affected worldwide</span></div><div className="proof-stat"><strong>&lt;5<span>%</span></strong><span>with an approved treatment</span></div></div></section>
 <AtlasStatsBand/>
 <section className="method-band content-width"><div><div className="section-kicker"><span className="kicker-line"/> THE ATLAS APPROACH</div><h2>From isolated facts<br/>to informed next steps.</h2></div><div className="method-items"><div><Network size={22}/><h3>See the connections</h3><p>Explore where diseases share mechanisms, research and communities.</p></div><div><Database size={22}/><h3>Inspect the evidence</h3><p>Every link tells you what supports it — and what remains uncertain.</p></div><div><MoveUpRight size={22}/><h3>Find a way forward</h3><p>Turn relevant work into a question worth asking together.</p></div></div><Button variant="link" asChild className="method-link"><Link to="/who" search={{ as: persona } as never}>Start with your perspective <ArrowUpRight/></Link></Button></section></>;
}
