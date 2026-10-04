// Client for the local Rare Disease Atlas backend (FastAPI, `backend/`, `python -m atlas.server`).
// Active only when VITE_ATLAS_API_URL is set; otherwise every screen keeps its Supabase / mock path.
// Shapes mirror backend/docs/contract.md.

const env = import.meta.env as Record<string, string | undefined>;

/** Base URL of the local backend, or null when the UI should use Supabase. */
export const ATLAS_API_URL: string | null = env["VITE_ATLAS_API_URL"]
  ? env["VITE_ATLAS_API_URL"].replace(/\/+$/, "")
  : null;
export const useLocalApi = ATLAS_API_URL !== null;

const FAST_MS = 20_000;
/** LLM-backed endpoints (email, chat) can be slow on a cold cache. */
const LLM_MS = 120_000;

async function request<T>(path: string, init: RequestInit = {}, timeout = FAST_MS): Promise<T> {
  if (!ATLAS_API_URL) throw new Error("VITE_ATLAS_API_URL is not set");
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  try {
    const res = await fetch(`${ATLAS_API_URL}${path}`, {
      ...init,
      signal: ctrl.signal,
      headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        detail = ((await res.json()) as { error?: string }).error ?? detail;
      } catch {
        /* keep statusText */
      }
      throw new Error(`Atlas API ${res.status}: ${detail}`);
    }
    return (await res.json()) as T;
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError")
      throw new Error("The atlas server took too long to answer.");
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

const enc = encodeURIComponent;
const post = <T>(path: string, body: unknown) =>
  request<T>(path, { method: "POST", body: JSON.stringify(body) }, LLM_MS);

// ---- §1 records ----------------------------------------------------------------------------
export type ApiEdge = {
  id: string;
  source: string;
  target: string;
  relation: string;
  evidence: string | null;
  confidence: number | null;
  polarity: string | null;
  effect: string | null;
  source_name: string | null;
  source_url: string | null;
  quote: string | null;
  frequency: string | null;
  context: string | null;
  retrieved_at: string | null;
  submitted_by: string | null;
};

// ---- §3.3 journey ----------------------------------------------------------------------------
export type ApiWitness = { id: string; name: string; kind: string; edges: string[] };
export type ApiSimilar = {
  id: string;
  name: string;
  score: number;
  lookalike: boolean;
  witnesses: ApiWitness[];
};
export type ApiOrganization = {
  id: string;
  name: string;
  kind: string | null;
  country: string | null;
  website: string | null;
  contact_email: string | null;
  for_disease: string;
  edges: string[];
};
export type ApiTrial = {
  id: string;
  title: string;
  status: string | null;
  phase: string | null;
  for_disease: string;
  countries: string[];
  near_user: boolean;
  interventions: string[];
  edges: string[];
};
export type ApiAsset = {
  id: string;
  name: string;
  kind: string;
  for_disease: string;
  edges: string[];
};
export type ApiNextStep = {
  kind: "contact" | "join" | "reuse" | "build";
  step: string;
  text: string;
  from_disease: string | null;
  target_id: string | null;
  edges: string[];
};
export type ApiJourney = {
  status: "ok" | "no_supported_link";
  disease: { id: string; name: string; short: string | null };
  similar: ApiSimilar[];
  lookalikes: ApiSimilar[];
  organizations: ApiOrganization[];
  trials: ApiTrial[];
  assets: ApiAsset[];
  next_steps: ApiNextStep[];
  searched: { source: string; query: string; n_results: number; n_kept: number }[];
  missing: string[];
};

export type ApiEmailDraft = {
  org_id: string | null;
  disease_id: string;
  to: string | null;
  recipient_reason: string;
  subject: string;
  body: string;
  citations: string[];
  warnings: string[];
  requires_human_review: boolean;
};

export type ApiChatMessage = { role: "user" | "assistant"; content: string };
export type ApiChatResponse = {
  answer: string;
  citations: string[];
  grounded: boolean;
  out_of_scope: boolean;
};

const journeys = new Map<string, Promise<ApiJourney>>();
export function getJourney(diseaseId: string): Promise<ApiJourney> {
  let hit = journeys.get(diseaseId);
  if (!hit) {
    hit = request<ApiJourney>(`/journey/${enc(diseaseId)}`);
    hit.catch(() => journeys.delete(diseaseId));
    journeys.set(diseaseId, hit);
  }
  return hit;
}
const edgeCache = new Map<string, Promise<ApiEdge>>();
export function getEdge(edgeId: string): Promise<ApiEdge> {
  let hit = edgeCache.get(edgeId);
  if (!hit) {
    hit = request<ApiEdge>(`/edge/${enc(edgeId)}`);
    hit.catch(() => edgeCache.delete(edgeId));
    edgeCache.set(edgeId, hit);
  }
  return hit;
}
export const draftEmail = (
  diseaseId: string,
  orgId: string | null,
  sender: { name?: string; role?: string; context?: string } = {},
) => post<ApiEmailDraft>("/email", { disease_id: diseaseId, org_id: orgId, sender });
export const askChat = (diseaseId: string, messages: ApiChatMessage[]) =>
  post<ApiChatResponse>("/chat", { disease_id: diseaseId, messages });

/** Removes `[E1a2b3c4]` / `[E1, E2]` citation markers, keeping line breaks (markdown bullets). */
export function stripCitations(text: string): string {
  return text
    .replace(/\s*\[(?:E[0-9a-f]+(?:\s*,\s*)?)+\]/gi, "")
    .replace(/[ \t]+([.,;:])/g, "$1")
    .replace(/[ \t]{2,}/g, " ")
    .trim();
}

// ---- evidence behind a computed similarity line -----------------------------------------------
export type LinkEvidence = {
  witness: string;
  kind: string;
  edges: ApiEdge[];
};

/**
 * The sourced records behind a similarity line: the shared gene / pathway / mechanism /
 * symptom ("witnesses") and the edges that support each, with their quotes and source links.
 */
export async function getLinkEvidence(a: string, b: string): Promise<LinkEvidence[]> {
  const find = async (from: string, to: string) => {
    const j = await getJourney(from);
    return [...j.similar, ...j.lookalikes].find((s) => s.id === to) ?? null;
  };
  const match = (await find(a, b).catch(() => null)) ?? (await find(b, a).catch(() => null));
  if (!match) return [];
  return Promise.all(
    match.witnesses.slice(0, 4).map(async (w) => ({
      witness: w.name,
      kind: w.kind,
      edges: (
        await Promise.all(w.edges.slice(0, 3).map((id) => getEdge(id).catch(() => null)))
      ).filter((e): e is ApiEdge => e !== null),
    })),
  );
}

// ---- table source for atlas-db.ts ------------------------------------------------------------
type Row = Record<string, unknown>;
type Result = { data: Row[] | null; error: { message: string } | null };

/**
 * The few Supabase query-builder calls `atlas-db.ts` makes (`from().select().eq()/.in()`),
 * served from `GET /tables/{name}` on the local backend. Rows have the Supabase table shape.
 */
class LocalQuery implements PromiseLike<Result> {
  private filters: [string, string[]][] = [];
  private columns: string[] | null = null;
  constructor(private table: string) {}
  select(cols = "*") {
    this.columns = cols.trim() === "*" ? null : cols.split(",").map((c) => c.trim());
    return this;
  }
  eq(col: string, value: unknown) {
    this.filters.push([col, [String(value)]]);
    return this;
  }
  in(col: string, values: unknown[]) {
    this.filters.push([col, values.map(String)]);
    return this;
  }
  private async run(): Promise<Result> {
    try {
      const qs = new URLSearchParams(this.filters.map(([c, v]) => [c, v.join(",")])).toString();
      const rows = await request<Row[]>(`/tables/${enc(this.table)}${qs ? `?${qs}` : ""}`);
      const cols = this.columns;
      const data = cols
        ? rows.map((r) => Object.fromEntries(cols.map((c) => [c, r[c] ?? null])))
        : rows;
      return { data, error: null };
    } catch (err) {
      return { data: null, error: { message: err instanceof Error ? err.message : String(err) } };
    }
  }
  then<A = Result, B = never>(
    ok?: ((value: Result) => A | PromiseLike<A>) | null,
    fail?: ((reason: unknown) => B | PromiseLike<B>) | null,
  ): PromiseLike<A | B> {
    return this.run().then(ok, fail);
  }
}

export const localTables = { from: (table: string) => new LocalQuery(table) };

/** Headline numbers for the home page (GET /stats). */
export type AtlasStats = {
  diseases: number;
  diseases_lysosomal: number;
  diseases_controls: number;
  papers: number;
  papers_read_by_ai: number;
  pages_read_by_ai: number;
  organizations: number;
  countries: number;
  trials: number;
  grants: number;
  researchers: number;
  nodes: number;
  edges: number;
  edges_by_evidence: Record<string, number>;
  records_searched: number;
  sources: string[];
};
export function getStats(): Promise<AtlasStats> {
  return request<AtlasStats>("/stats");
}
