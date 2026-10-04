import { describe, expect, it } from "vitest";
import { loadAtlasGraph, searchDiseases } from "@/lib/atlas-db";

// Hits the live Supabase project: this is the check that the mapping matches the
// real data, not a fixture of what we assume it looks like.
describe("atlas graph from Supabase", () => {
  it("loads diseases with labels, clusters and positions", async () => {
    const g = await loadAtlasGraph();
    expect(g.diseases.length).toBeGreaterThan(10);
    for (const d of g.diseases) {
      expect(d.label).toBeTruthy();
      expect(d.plain_label).toBeTruthy();
      expect(g.positions[d.id]).toBeDefined();
      const [x, y] = g.positions[d.id]!;
      // Stays inside the stage, so no node or its label is clipped.
      expect(x).toBeGreaterThanOrEqual(8);
      expect(x).toBeLessThanOrEqual(92);
      expect(y).toBeGreaterThanOrEqual(8);
      expect(y).toBeLessThanOrEqual(92);
    }
  }, 30000);

  it("builds edges that reference real nodes and carry an explanation", async () => {
    const g = await loadAtlasGraph();
    const ids = new Set(g.diseases.map((d) => d.id));
    expect(g.edges.length).toBeGreaterThan(0);
    for (const e of g.edges) {
      expect(ids.has(e.source)).toBe(true);
      expect(ids.has(e.target)).toBe(true);
      expect(e.plain_explanation).toBeTruthy();
      expect(e.relationship_label).toBeTruthy();
      expect(["curated", "extracted", "inferred"]).toContain(e.evidence_type);
    }
  }, 30000);

  it("leaves no disease stranded without a connection", async () => {
    const g = await loadAtlasGraph();
    const linked = new Set(g.edges.flatMap((e) => [e.source, e.target]));
    expect(g.diseases.filter((d) => !linked.has(d.id))).toEqual([]);
  }, 30000);

  it("never places two diseases on top of each other", async () => {
    const g = await loadAtlasGraph();
    const seen = new Map<string, string>();
    for (const d of g.diseases) {
      const [x, y] = g.positions[d.id]!;
      for (const [other, pos] of seen) {
        const [ox, oy] = pos.split(",").map(Number) as [number, number];
        // Labels are roughly 8% wide and 18% tall of the stage.
        const tooClose = Math.abs(x - ox) < 7 && Math.abs(y - oy) < 16;
        expect(tooClose, `${d.plain_label} overlaps ${other}`).toBe(false);
      }
      seen.set(d.plain_label, `${x},${y}`);
    }
  }, 30000);

  it("finds a disease by gene symbol and by synonym", async () => {
    const byGene = await searchDiseases("HGSNAT");
    expect(byGene.length).toBeGreaterThan(0);
    expect(byGene[0]!.attributes.gene).toBe("HGSNAT");
    const bySynonym = await searchDiseases("Sanfilippo");
    expect(bySynonym.length).toBeGreaterThan(0);
  }, 30000);
});
