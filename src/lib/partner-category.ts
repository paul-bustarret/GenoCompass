// Sorts the journey's organisations into the three partner categories of the design:
// families (patient groups), therapies (treatment developers / trial sponsors) and
// institutions (academic, hospitals, NIH and other funders, registry hosts).
import type { ApiJourney, ApiOrganization, ApiTrial } from "@/lib/atlas-api";

export type PartnerCategory = "families" | "therapies" | "institutions";

export const CATEGORY_ORDER: PartnerCategory[] = ["families", "therapies", "institutions"];

export const CATEGORY_INFO: Record<
  PartnerCategory,
  { label: string; blurb: string; stage: string; accent: number }
> = {
  families: {
    label: "Families",
    blurb: "Patient groups and family foundations",
    stage: "Family support / community",
    accent: 1,
  },
  therapies: {
    label: "Therapies",
    blurb: "Companies and sponsors developing treatments",
    stage: "Treatment development / research-stage",
    accent: 2,
  },
  institutions: {
    label: "Institutions",
    blurb: "Research centres, hospitals, funders and registries",
    stage: "Research / clinical / public",
    accent: 3,
  },
};

export type Partner = {
  org: ApiOrganization;
  category: PartnerCategory;
  /** Specific role shown as the card eyebrow, e.g. "Patient group", "Company", "NIH institute". */
  role: string;
  /** One-line description consistent with the category. */
  description: string;
  /** Linked trials this organisation sponsors or runs (by shared source edge). */
  trials: ApiTrial[];
};

const INSTITUTION_RE =
  /\b(universit|institut|hospital|h[oô]pita|college|cent(re|er)\b|clinic|NIH\b|national institutes?|academy|medical|school of|assistance publique|ministry|department of|RTI\b)|\bMD\b|\bPhD\b/i;
const HOSPITAL_RE = /hospital|h[oô]pita|clinic|medical cent/i;
const UNIVERSITY_RE = /universit|college|school of/i;
const COMPANY_RE =
  /\b(inc|ltd|llc|gmbh|b\.?v|corp|corporation|plc|ag|s\.?a|s\.?p\.?a|limited|pharma\w*|therapeutics|biotech\w*|bio\w*|biosciences|biopharma|genetics|medicines|ao)\b/i;
const FAMILY_RE =
  /foundation|association|society|alliance|fund\b|support|famil|parents|patients?|cure|hope|friends|network|stichting|fundaci[oó]n|asociaci[oó]n|e\.?\s?v\.?$|ets\b|project/i;

/** True when the trial tests an actual treatment (not only placebo / samples / observational). */
const isTreatmentTrial = (t: ApiTrial) =>
  t.interventions.some((i) => !/placebo|sample|questionnaire|observ|standard of care/i.test(i));

function trialsOf(org: ApiOrganization, trials: ApiTrial[]): ApiTrial[] {
  const mine = new Set(org.edges);
  return trials.filter((t) => t.edges.some((e) => mine.has(e)));
}

function classify(
  org: ApiOrganization,
  trials: ApiTrial[],
): { category: PartnerCategory; role: string } {
  const name = org.name;
  const sponsorsTreatment = trials.some(isTreatmentTrial);
  switch (org.kind) {
    case "patient_group":
      return { category: "families", role: "Patient group" };
    case "company":
    case "industry":
      return { category: "therapies", role: "Company" };
    case "sponsor":
      if (sponsorsTreatment || COMPANY_RE.test(name))
        return { category: "therapies", role: "Trial sponsor" };
      return { category: "institutions", role: "Trial sponsor" };
    case "nih":
      return { category: "institutions", role: "NIH institute" };
    case "funder":
      return {
        category: "institutions",
        role: /\bNIH\b|national institute/i.test(name) ? "NIH funder" : "Funder",
      };
    case "academic":
      return { category: "institutions", role: "Research centre" };
    case "registry_host":
      return { category: "institutions", role: "Registry host" };
  }
  // kind is "other", null or unknown: fall back to the name and the trials it is linked to.
  if (INSTITUTION_RE.test(name))
    return {
      category: "institutions",
      role: HOSPITAL_RE.test(name)
        ? "Hospital"
        : UNIVERSITY_RE.test(name)
          ? "University"
          : /\bMD\b|\bPhD\b/.test(name)
            ? "Clinical investigator"
            : "Research institute",
    };
  if (COMPANY_RE.test(name)) return { category: "therapies", role: "Company" };
  if (sponsorsTreatment) return { category: "therapies", role: "Trial sponsor" };
  if (FAMILY_RE.test(name)) return { category: "families", role: "Family foundation" };
  return { category: "institutions", role: "Organisation" };
}

function describe(category: PartnerCategory, role: string, trials: ApiTrial[], disease: string) {
  const drugs = [
    ...new Set(
      trials
        .filter(isTreatmentTrial)
        .flatMap((t) => t.interventions)
        .filter((i) => !/placebo|prophyla|immunomodulat|immunosuppress/i.test(i)),
    ),
  ].slice(0, 3);
  const n = trials.length;
  const trialNote = n ? ` Linked to ${n} clinical ${n === 1 ? "study" : "studies"} in the atlas.` : "";
  switch (category) {
    case "families":
      return `Run by and for families affected by ${disease}; can connect you with other families, registries and research news.`;
    case "therapies":
      return drugs.length
        ? `Developing or sponsoring treatment studies for ${disease} (${drugs.join(", ")}). Ask about programme status and trial eligibility.`
        : `Works on treatments relevant to ${disease}; ask about programme status and any open studies.${trialNote}`;
    case "institutions":
      if (role === "Registry host")
        return `Hosts a registry or data resource covering ${disease}; ask how to contribute or access data.`;
      if (role === "NIH institute")
        return `US National Institutes of Health institute linked to ${disease}; a route to intramural researchers and funded programmes.${trialNote}`;
      if (/funder|NIH/i.test(role))
        return `Public or charitable research funder linked to ${disease}; a route to funded investigators and programmes.${trialNote}`;
      return `Research or clinical institution linked to ${disease}; a route to specialist investigators and care.${trialNote}`;
  }
}

/**
 * Partners for the disease page: the disease's own organisations, grouped by category and
 * ordered families → therapies → institutions. Within a category, organisations named by the
 * backend's next_steps come first (in that order), then the rest in backend order.
 */
export function partnersFor(j: ApiJourney, diseaseId: string, perCategory = 3): Partner[] {
  const disease = j.disease.short ?? j.disease.name;
  const stepRank = new Map<string, number>();
  j.next_steps.forEach((s, i) => {
    if (s.target_id && !stepRank.has(s.target_id)) stepRank.set(s.target_id, i);
  });
  const own = j.organizations.filter((o) => o.for_disease === diseaseId);
  const partners = own.map((org, i) => {
    const trials = trialsOf(org, j.trials);
    const { category, role } = classify(org, trials);
    return {
      partner: { org, category, role, trials, description: describe(category, role, trials, disease) },
      rank: stepRank.get(org.id) ?? j.next_steps.length + i,
    };
  });
  return CATEGORY_ORDER.flatMap((c) =>
    partners
      .filter((p) => p.partner.category === c)
      .sort((a, b) => a.rank - b.rank)
      .slice(0, perCategory)
      .map((p) => p.partner),
  );
}

/** Role label for any organisation (used in fallback step text). */
export function roleOf(org: ApiOrganization, j: ApiJourney): string {
  return classify(org, trialsOf(org, j.trials)).role;
}
