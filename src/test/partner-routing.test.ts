import { describe, expect, it } from "vitest";
import { getMockPartnerDisease, getNextSteps } from "@/lib/atlas";

describe("database disease links to local partner records", () => {
  it("opens the Type C plan for its database ID", async () => {
    const match = getMockPartnerDisease({
      id: "MONDO:example-type-c",
      label: "Mucopolysaccharidosis, type IIIC",
      synonyms: ["Sanfilippo syndrome type C"],
      attributes: { gene: "HGSNAT" },
    });

    expect(match?.id).toBe("mps-iiic");
    expect((await getNextSteps(match!.id, "devon")).map((partner) => partner.id)).toEqual([
      "phoenix-nest",
      "cure-sanfilippo",
      "sanfilippo-childrens",
    ]);
  });

  it("does not borrow Type C partners for another subtype", () => {
    expect(
      getMockPartnerDisease({
        id: "MONDO:example-type-a",
        label: "Mucopolysaccharidosis, type IIIA",
        synonyms: ["Sanfilippo syndrome type A"],
        attributes: { gene: "SGSH" },
      }),
    ).toBeUndefined();
  });
});
