import { useEffect, useState } from "react";
import {
  diseases as mockDiseases,
  allEdges as mockEdges,
  clusters as mockClusters,
} from "@/lib/atlas";
import {
  loadAtlasGraph,
  type AtlasCluster,
  type AtlasDisease,
  type AtlasEdge,
} from "@/lib/atlas-db";

// Hand-placed coordinates for the original mock map. Only used when a record is
// not in the database (the `disease-z` gap demo, and older bookmarked ids).
const mockPositions: Record<string, [number, number]> = {
  "mps-iiic": [46, 44],
  "mps-iiia": [28, 25],
  "mps-iiib": [69, 28],
  "mps-iiid": [69, 66],
  "mps-i": [16, 69],
  "mps-ii": [26, 83],
  "mps-vii": [43, 76],
  msd: [14, 45],
  rett: [85, 43],
  "mecp2-dup": [88, 76],
  "disease-z": [50, 48],
};

export type AtlasSource = {
  loading: boolean;
  error: string | null;
  /** False when we fell back to the bundled mock records. */
  fromDatabase: boolean;
  diseases: AtlasDisease[];
  edges: AtlasEdge[];
  clusters: AtlasCluster[];
  positions: Record<string, [number, number]>;
};

const mockFallback = (): Omit<AtlasSource, "loading" | "error" | "fromDatabase"> => ({
  diseases: mockDiseases.map((d) => ({
    id: d.id,
    type: "disease",
    label: d.label,
    plain_label: d.plain_label,
    synonyms: [...d.synonyms],
    xrefs: { ...d.xrefs } as Record<string, string>,
    cluster: d.cluster ?? "unmapped",
    research_activity: d.research_activity,
    evidence_records: 0,
    attributes: {
      gene: d.attributes.gene ?? "",
      description: d.attributes.description ?? "",
      patient_group: d.attributes.patient_group ?? "",
    },
  })),
  edges: mockEdges
    .filter((e) => e.id !== "e-rett-counter")
    .map((e) => ({
      ...e,
      evidence_type: e.evidence_type as AtlasEdge["evidence_type"],
      evidence_level: e.evidence_level as AtlasEdge["evidence_level"],
      contradicted_by: [...e.contradicted_by],
      witnesses: [...e.witnesses],
      provenance: "mock" as const,
    })),
  clusters: mockClusters.map((c) => ({ id: c.id, label: c.label, plain_label: c.plain_label })),
  positions: mockPositions,
});

const empty: AtlasSource = {
  loading: true,
  error: null,
  fromDatabase: true,
  diseases: [],
  edges: [],
  clusters: [],
  positions: {},
};

/**
 * Loads the disease graph from Supabase. If `focusId` is not a database record,
 * serves the bundled mock set instead so the demonstration routes keep working.
 */
export function useAtlasGraph(focusId: string): AtlasSource {
  const [state, setState] = useState<AtlasSource>(empty);

  useEffect(() => {
    let active = true;
    setState((prev) => ({ ...prev, loading: true, error: null }));
    loadAtlasGraph()
      .then((graph) => {
        if (!active) return;
        if (graph.diseases.some((d) => d.id === focusId)) {
          setState({ loading: false, error: null, fromDatabase: true, ...graph });
        } else {
          setState({ loading: false, error: null, fromDatabase: false, ...mockFallback() });
        }
      })
      .catch((err: unknown) => {
        if (!active) return;
        const message = err instanceof Error ? err.message : String(err);
        // Still render something useful if the database is unreachable.
        setState({ loading: false, error: message, fromDatabase: false, ...mockFallback() });
      });
    return () => {
      active = false;
    };
  }, [focusId]);

  return state;
}
