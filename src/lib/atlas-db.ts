// Supabase-backed atlas data. Returns the same record shapes as the local mock in
// `atlas.ts`, so the screens render either source without changes (see AGENTS.md).
import type { SupabaseClient } from "@supabase/supabase-js";
import { supabase } from "@/integrations/supabase/client";

type GraphDatabase = {
  public: {
    Tables: {
      nodes: {
        Row: {
          id: string;
          type: string;
          name: string | null;
          synonyms: string | null;
          attrs: Record<string, unknown>;
        };
        Insert: never;
        Update: never;
        Relationships: [];
      };
      edges: {
        Row: {
          edge_id: string;
          src: string;
          dst: string;
          relation: string;
          source: string | null;
          source_url: string | null;
          retrieved_at: string | null;
          confidence: number | null;
          evidence: string | null;
          polarity: string | null;
          effect: number | null;
          quote: string | null;
          frequency: string | null;
        };
        Insert: never;
        Update: never;
        Relationships: [];
      };
      similarity: {
        Row: {
          a: string;
          b: string;
          therapeutic: number | null;
          phenotype_view: number | null;
          gene: number | null;
          pathway: number | null;
          phenotype: number | null;
          lookalike: boolean | null;
          top_witnesses: string | null;
        };
        Insert: never;
        Update: never;
        Relationships: [];
      };
      clusters: {
        Row: { cluster: number; disease_id: string };
        Insert: never;
        Update: never;
        Relationships: [];
      };
      coverage: {
        Row: {
          disease_id: string;
          source: string;
          query: string;
          n_results: number | null;
          n_kept: number | null;
          retrieved_at: string | null;
        };
        Insert: never;
        Update: never;
        Relationships: [];
      };
    };
    Views: Record<string, never>;
    Functions: Record<string, never>;
    Enums: Record<string, never>;
    CompositeTypes: Record<string, never>;
  };
};

// Same client instance (and session); the generated Database type just doesn't
// describe the knowledge-graph tables.
const db = supabase as unknown as SupabaseClient<GraphDatabase>;

export type AtlasDisease = {
  id: string;
  type: "disease";
  label: string;
  plain_label: string;
  synonyms: string[];
  xrefs: Record<string, string>;
  cluster: string;
  research_activity: number;
  /** Trials, grants and papers linked to this disease. 0 when unknown. */
  evidence_records: number;
  attributes: { gene: string; description: string; patient_group: string };
};

export type AtlasEdge = {
  id: string;
  source: string;
  target: string;
  type: string;
  plain_explanation: string;
  evidence_type: "curated" | "extracted" | "inferred";
  evidence_level: "hypothesis" | "preclinical" | "clinical_observation" | "clinical_trial";
  source_name: string;
  source_id: string;
  retrieved_at: string;
  confidence: number;
  negated: boolean;
  contradicted_by: string[];
  score: number;
  mechanism_score: number;
  pathway_score: number;
  phenotype_score: number;
  effect_compatible: boolean;
  witnesses: string[];
  relationship_label: string;
  /** Where the record came from, so the UI can describe it honestly. */
  provenance: "database" | "mock";
};

export type AtlasCluster = { id: string; label: string; plain_label: string };

export type AtlasGraph = {
  diseases: AtlasDisease[];
  edges: AtlasEdge[];
  clusters: AtlasCluster[];
  positions: Record<string, [number, number]>;
};

// How many similarity links each disease keeps. The similarity table is complete
// pairwise (21 diseases -> 210 rows) and heavily skewed, so a fixed score cutoff
// yields either nothing or everything. Top-K per node keeps the map readable and
// guarantees every disease stays connected.
const LINKS_PER_DISEASE = 3;

const num = (v: number | null | undefined) => (typeof v === "number" ? v : 0);
const splitList = (v: string | null, sep: string) =>
  v
    ? v
        .split(sep)
        .map((s) => s.trim())
        .filter(Boolean)
    : [];

/** Cluster number -> slug used for the colour classes in styles.css. */
export const clusterSlug = (n: number) => `c${n}`;

function describeLink(row: GraphDatabase["public"]["Tables"]["similarity"]["Row"]) {
  const gene = num(row.gene),
    pathway = num(row.pathway),
    phenotype = num(row.phenotype);
  if (row.lookalike)
    return { label: "Look-alike · different cause", evidence_type: "inferred" as const };
  if (gene >= 1) return { label: "Shared causal gene", evidence_type: "curated" as const };
  if (pathway >= 0.5) return { label: "Shared pathway", evidence_type: "curated" as const };
  if (pathway > 0) return { label: "Partial pathway overlap", evidence_type: "extracted" as const };
  if (phenotype >= 0.2)
    return { label: "Overlapping symptoms", evidence_type: "extracted" as const };
  return { label: "Weak overall similarity", evidence_type: "inferred" as const };
}

function explain(row: GraphDatabase["public"]["Tables"]["similarity"]["Row"], witnesses: string[]) {
  if (row.lookalike)
    return witnesses.length
      ? `These conditions look alike in the clinic, but the shared signals (${witnesses.slice(0, 2).join(", ")}) do not point to a shared cause.`
      : "These conditions look alike in the clinic, but the atlas found no shared cause.";
  if (!witnesses.length)
    return "Scored as similar, but the atlas has no shared gene, pathway or symptom to name yet.";
  return `Linked by ${witnesses.slice(0, 3).join(", ")}.`;
}

/** Stage box the layout is fitted into, in the SVG's 0-100 coordinate space. */
const STAGE = { left: 9, right: 91, top: 12, bottom: 88 };
/** Clear space a node label needs, as a share of the stage. Labels are wide and short. */
const LABEL = { x: 7.5, y: 16.5 };

type Point = { x: number; y: number };

/**
 * Force-directed layout (Fruchterman-Reingold): linked diseases attract,
 * every pair repels. Unlike a grid, position carries meaning here — diseases
 * that share genes and pathways settle into visible clumps, and the long
 * edges left over are the genuinely distant relationships.
 *
 * Deterministic: seeded from a golden-angle spiral by index, never at random,
 * so the same data always produces the same map.
 */
function layout(diseases: AtlasDisease[], links: AtlasEdge[]): Record<string, [number, number]> {
  const n = diseases.length;
  if (n === 0) return {};

  // Stable ordering, grouped by cluster, so the seed never depends on row order.
  const ordered = diseases
    .slice()
    .sort((a, b) => a.cluster.localeCompare(b.cluster) || a.id.localeCompare(b.id));
  const index = new Map(ordered.map((d, i) => [d.id, i]));

  const pos: Point[] = ordered.map((_, i) => {
    const angle = i * 2.399963229728653; // golden angle: spreads evenly, no clumped seed
    const radius = Math.sqrt((i + 0.5) / n);
    return { x: radius * Math.cos(angle), y: radius * Math.sin(angle) };
  });

  // Stronger similarity pulls harder. Scores are mostly small, so lift the floor
  // or weak links would contribute nothing at all.
  const springs = links
    .map((e) => ({ a: index.get(e.source), b: index.get(e.target), weight: 0.5 + 3 * e.score }))
    .filter(
      (e): e is { a: number; b: number; weight: number } => e.a !== undefined && e.b !== undefined,
    );

  const k = Math.sqrt(1 / n); // ideal edge length in the unit working space
  let temp = 0.35;

  for (let step = 0; step < 500; step++) {
    const disp: Point[] = pos.map(() => ({ x: 0, y: 0 }));

    for (let i = 0; i < n; i++) {
      const pi = pos[i]!;
      for (let j = i + 1; j < n; j++) {
        const pj = pos[j]!;
        const dx = pi.x - pj.x,
          dy = pi.y - pj.y;
        const dist = Math.hypot(dx, dy) || 1e-4;
        const force = (k * k) / dist;
        const ux = (dx / dist) * force,
          uy = (dy / dist) * force;
        disp[i]!.x += ux;
        disp[i]!.y += uy;
        disp[j]!.x -= ux;
        disp[j]!.y -= uy;
      }
    }

    for (const spring of springs) {
      const pa = pos[spring.a]!,
        pb = pos[spring.b]!;
      const dx = pa.x - pb.x,
        dy = pa.y - pb.y;
      const dist = Math.hypot(dx, dy) || 1e-4;
      const force = ((dist * dist) / k) * spring.weight;
      const ux = (dx / dist) * force,
        uy = (dy / dist) * force;
      disp[spring.a]!.x -= ux;
      disp[spring.a]!.y -= uy;
      disp[spring.b]!.x += ux;
      disp[spring.b]!.y += uy;
    }

    for (let i = 0; i < n; i++) {
      const pi = pos[i]!,
        di = disp[i]!;
      di.x -= pi.x * 0.03; // keep unlinked nodes from drifting away
      di.y -= pi.y * 0.03;
      const length = Math.hypot(di.x, di.y) || 1e-4;
      const capped = Math.min(length, temp);
      pi.x += (di.x / length) * capped;
      pi.y += (di.y / length) * capped;
    }
    temp *= 0.985; // cool down so late steps only fine-tune
  }

  // Fit to the stage, each axis independently: the stage is far wider than it
  // is tall, and stretching into it buys horizontal room for the labels.
  const xs = pos.map((p) => p.x),
    ys = pos.map((p) => p.y);
  const spread = (values: number[], lo: number, hi: number) => {
    const min = Math.min(...values),
      max = Math.max(...values);
    const range = max - min || 1;
    return (v: number) => lo + ((v - min) / range) * (hi - lo);
  };
  const toX = spread(xs, STAGE.left, STAGE.right);
  const toY = spread(ys, STAGE.top, STAGE.bottom);
  const placed: Point[] = pos.map((p) => ({ x: toX(p.x), y: toY(p.y) }));

  separateLabels(placed);

  const positions: Record<string, [number, number]> = {};
  ordered.forEach((d, i) => {
    const p = placed[i]!;
    positions[d.id] = [p.x, p.y];
  });
  return positions;
}

/**
 * The simulation treats nodes as points; on screen each one carries a label.
 * Separate any pair whose label boxes overlap, resolving along whichever axis
 * needs the smaller correction so the layout keeps the shape the forces found.
 */
function separateLabels(points: Point[]): void {
  for (let pass = 0; pass < 80; pass++) {
    let moved = false;
    for (let i = 0; i < points.length; i++) {
      for (let j = i + 1; j < points.length; j++) {
        const a = points[i]!,
          b = points[j]!;
        const dx = a.x - b.x,
          dy = a.y - b.y;
        const overlapX = LABEL.x - Math.abs(dx);
        const overlapY = LABEL.y - Math.abs(dy);
        if (overlapX <= 0 || overlapY <= 0) continue; // boxes already clear

        // Push along the axis of least penetration, in label-relative terms.
        if (overlapX / LABEL.x <= overlapY / LABEL.y) {
          const shift = (overlapX / 2 + 0.05) * (dx < 0 ? -1 : 1);
          a.x += shift;
          b.x -= shift;
        } else {
          const shift = (overlapY / 2 + 0.05) * (dy < 0 ? -1 : 1);
          a.y += shift;
          b.y -= shift;
        }
        moved = true;
      }
    }
    for (const p of points) {
      p.x = Math.min(STAGE.right, Math.max(STAGE.left, p.x));
      p.y = Math.min(STAGE.bottom, Math.max(STAGE.top, p.y));
    }
    if (!moved) break;
  }
}

let cached: Promise<AtlasGraph> | null = null;

/** Loads the whole disease-level graph. Cached for the life of the page. */
export function loadAtlasGraph(): Promise<AtlasGraph> {
  if (!cached)
    cached = fetchAtlasGraph().catch((err) => {
      cached = null;
      throw err;
    });
  return cached;
}

async function fetchAtlasGraph(): Promise<AtlasGraph> {
  const [nodesRes, clustersRes, causedByRes, genesRes, simRes, activityRes] = await Promise.all([
    db.from("nodes").select("id,name,synonyms,attrs").eq("type", "disease"),
    db.from("clusters").select("cluster,disease_id"),
    db.from("edges").select("src,dst").eq("relation", "caused_by"),
    db.from("nodes").select("id,name").eq("type", "gene"),
    db.from("similarity").select("*"),
    db.from("edges").select("dst,relation").in("relation", ["studies", "mentions"]),
  ]);

  for (const res of [nodesRes, clustersRes, causedByRes, genesRes, simRes, activityRes]) {
    if (res.error) throw new Error(`Atlas query failed: ${res.error.message}`);
  }

  const geneName = new Map((genesRes.data ?? []).map((g) => [g.id, g.name ?? g.id]));
  const geneFor = new Map((causedByRes.data ?? []).map((e) => [e.src, geneName.get(e.dst) ?? ""]));
  const clusterFor = new Map(
    (clustersRes.data ?? []).map((c) => [c.disease_id, clusterSlug(c.cluster)]),
  );

  const activity = new Map<string, number>();
  for (const e of activityRes.data ?? []) activity.set(e.dst, (activity.get(e.dst) ?? 0) + 1);
  const peak = Math.max(1, ...activity.values());

  const diseases: AtlasDisease[] = (nodesRes.data ?? []).map((n) => {
    const attrs = (n.attrs ?? {}) as Record<string, string | undefined>;
    const synonyms = splitList(n.synonyms, "|");
    const label = n.name ?? attrs["mondo_label"] ?? n.id;
    return {
      id: n.id,
      type: "disease",
      label,
      plain_label: attrs["short"] || label,
      synonyms,
      xrefs: attrs["omim"] ? { OMIM: attrs["omim"] } : {},
      cluster: clusterFor.get(n.id) ?? "unmapped",
      research_activity: Math.round(((activity.get(n.id) ?? 0) / peak) * 100),
      evidence_records: activity.get(n.id) ?? 0,
      attributes: {
        gene: geneFor.get(n.id) ?? "",
        description: attrs["description"] ?? "",
        patient_group: "",
      },
    };
  });

  const known = new Set(diseases.map((d) => d.id));
  const rows = (simRes.data ?? []).filter((r) => known.has(r.a) && known.has(r.b));

  // Keep each disease's strongest few links, plus every look-alike pair (those are
  // the cautionary ones the map is meant to surface even when scores are low).
  const keep = new Set<string>();
  const key = (a: string, b: string) => (a < b ? `${a}|${b}` : `${b}|${a}`);
  for (const d of diseases) {
    rows
      .filter((r) => r.a === d.id || r.b === d.id)
      .sort((x, y) => num(y.therapeutic) - num(x.therapeutic))
      .slice(0, LINKS_PER_DISEASE)
      .forEach((r) => keep.add(key(r.a, r.b)));
  }
  for (const r of rows) if (r.lookalike) keep.add(key(r.a, r.b));

  const edges: AtlasEdge[] = rows
    .filter((r) => keep.has(key(r.a, r.b)))
    .map((r) => {
      const witnesses = splitList(r.top_witnesses, "|");
      const { label, evidence_type } = describeLink(r);
      return {
        id: `sim-${r.a}-${r.b}`.replace(/[^\w-]/g, "_"),
        source: r.a,
        target: r.b,
        type: "similar_to",
        plain_explanation: explain(r, witnesses),
        evidence_type,
        // Similarity is computed from the graph, not observed in a study.
        evidence_level: "hypothesis",
        source_name: "Atlas similarity model",
        source_id: `similarity:${r.a}:${r.b}`,
        retrieved_at: "Computed from current atlas data",
        confidence: num(r.therapeutic),
        negated: Boolean(r.lookalike),
        contradicted_by: [],
        score: num(r.therapeutic),
        mechanism_score: num(r.gene),
        pathway_score: num(r.pathway),
        phenotype_score: num(r.phenotype),
        effect_compatible: !r.lookalike,
        witnesses,
        relationship_label: label,
        provenance: "database",
      };
    });

  const usedClusters = [...new Set(diseases.map((d) => d.cluster))].sort();
  const clusters: AtlasCluster[] = usedClusters.map((id) => {
    const members = diseases.filter((d) => d.cluster === id);
    // No cluster names in the database: name each one after the gene family or the
    // condition that anchors it, rather than inventing a mechanism label.
    const anchor = members.slice().sort((a, b) => b.research_activity - a.research_activity)[0];
    const name =
      id === "unmapped"
        ? "Not yet grouped"
        : anchor
          ? `${anchor.plain_label} group`
          : `Group ${id}`;
    return { id, label: name, plain_label: name };
  });

  return { diseases, edges, clusters, positions: layout(diseases, edges) };
}

export async function searchDiseases(query: string): Promise<AtlasDisease[]> {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const { diseases } = await loadAtlasGraph();
  const terms = q.split(/\s+/).filter((t) => t.length > 2);
  const haystack = (d: AtlasDisease) =>
    [d.label, d.plain_label, d.attributes.gene, ...d.synonyms]
      .filter(Boolean)
      .map((s) => s.toLowerCase());
  return diseases
    .filter((d) => {
      const fields = haystack(d);
      if (fields.some((s) => s.includes(q))) return true;
      return terms.length > 1 && terms.every((t) => fields.some((s) => s.includes(t)));
    })
    .sort((a, b) => {
      const exact = (d: AtlasDisease) =>
        haystack(d).some((s) => s === q || s.startsWith(q)) ? 0 : 1;
      return exact(a) - exact(b) || a.label.localeCompare(b.label);
    });
}
