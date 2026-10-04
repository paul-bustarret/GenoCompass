"""One fetcher per public API. Each adds sourced nodes/edges to the GraphBuilder and logs coverage."""
import re
import unicodedata
from urllib.parse import quote, urlencode

from .graph import GraphBuilder
from .http import get_json

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def slug(text: str) -> str:
    return re.sub(r"\W+", "_", text.strip())[:40].strip("_")


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower())) - {"disease", "syndrome", "type", "the", "of", "and"}


# ---------- genes: mygene.info (symbol -> NCBI gene, HGNC, UniProt) ----------
def gene(gb: GraphBuilder, symbol: str) -> dict:
    url = f"https://mygene.info/v3/query?q=symbol:{symbol}&species=human&fields=entrezgene,HGNC,uniprot.Swiss-Prot,name"
    hit = get_json("mygene", url)["hits"][0]
    up = hit.get("uniprot", {}).get("Swiss-Prot")
    info = {"ncbi": f"NCBIGene:{hit['entrezgene']}", "uniprot": up[0] if isinstance(up, list) else up}
    gb.node(info["ncbi"], "gene", symbol, synonyms=[hit.get("name", "")],
            hgnc=f"HGNC:{hit.get('HGNC')}", uniprot=info["uniprot"])
    return info


# ---------- HPO (JAX ontology API): disease, its MONDO id, phenotypes, genes ----------
def hpo_disease(gb: GraphBuilder, d: dict, gene_id: str) -> str | None:
    url = f"https://ontology.jax.org/api/network/annotation/{d['omim']}"
    data = get_json("hpo", url)
    if not data:
        gb.covered(d["omim"], "hpo", d["omim"], -1, 0)
        return None
    dis = data["disease"]
    did = dis.get("mondoId") or d["omim"]
    gb.node(did, "disease", dis["name"], synonyms=[d["short"]], omim=d["omim"], role=d["role"],
            short=d["short"], group=d.get("group", ""), description=dis.get("description") or "")
    gb.edge(did, gene_id, "caused_by", "hpo", url)
    phenos = [p for ps in data["categories"].values() for p in ps]
    for p in phenos:
        gb.node(p["id"], "phenotype", p["name"])
        meta = p.get("metadata", {})
        gb.edge(did, p["id"], "has_phenotype", "hpo", url, frequency=meta.get("frequency", ""),
                quote="; ".join(meta.get("sources", [])))
    gb.covered(did, "hpo", d["omim"], len(phenos), len(phenos))
    return did


# ---------- MONDO (OLS API): label + synonyms for name search ----------
def mondo(gb: GraphBuilder, did: str):
    if not did.startswith("MONDO:"):
        return
    url = f"https://www.ebi.ac.uk/ols4/api/ontologies/mondo/terms?obo_id={did}"
    data = get_json("mondo", url)
    terms = (data or {}).get("_embedded", {}).get("terms", [])
    if terms:
        n = gb.nodes[did]
        syn = set(filter(None, n["synonyms"].split("|"))) | set(terms[0].get("synonyms") or [])
        n["synonyms"] = "|".join(sorted(syn))
        n["attrs"]["mondo_label"] = terms[0]["label"]
    gb.covered(did, "mondo", did, len(terms), len(terms))


# ---------- Reactome: gene -> lowest-level pathways ----------
def reactome(gb: GraphBuilder, gene_id: str, uniprot: str):
    if not uniprot:
        return
    url = f"https://reactome.org/ContentService/data/mapping/UniProt/{uniprot}/pathways?species=9606"
    pws = get_json("reactome", url) or []
    for p in pws:
        gb.node(p["stId"], "pathway", p["displayName"])
        gb.edge(gene_id, p["stId"], "in_pathway", "reactome", url)
        # one level up the Reactome hierarchy, so sibling pathways (e.g. MPS IIIA..D) can meet
        aurl = f"https://reactome.org/ContentService/data/event/{p['stId']}/ancestors"
        for chain in get_json("reactome", aurl) or []:
            # chain = [leaf, parent, ..., top-level]; skip parents in the top two levels
            # (e.g. "Innate Immune System") — they are hubs that link unrelated diseases
            if len(chain) >= 4:
                parent = chain[1]
                gb.node(parent["stId"], "pathway", parent["displayName"])
                gb.edge(p["stId"], parent["stId"], "part_of", "reactome", aurl)
    gb.covered(gene_id, "reactome", uniprot, len(pws), len(pws))


# ---------- ClinVar: top pathogenic variants of the gene ----------
def clinvar(gb: GraphBuilder, symbol: str, gene_id: str, diseases: dict[str, str], limit: int):
    term = f"{symbol}[gene] AND clinsig_pathogenic[prop]"
    s = get_json("clinvar", f"{EUTILS}/esearch.fcgi?db=clinvar&retmode=json&retmax={limit}&term={quote(term)}")
    ids = s["esearchresult"]["idlist"]
    if ids:
        summ = get_json("clinvar", f"{EUTILS}/esummary.fcgi?db=clinvar&retmode=json&id={','.join(ids)}")["result"]
        for vid in ids:
            v = summ[vid]
            cls = v.get("germline_classification", {})
            vurl = f"https://www.ncbi.nlm.nih.gov/clinvar/variation/{vid}/"
            gb.node(f"ClinVar:{vid}", "variant", v["title"], significance=cls.get("description", ""),
                    consequence=", ".join(v.get("molecular_consequence_list", [])))
            gb.edge(gene_id, f"ClinVar:{vid}", "has_variant", "clinvar", vurl)
            traits = [t.get("trait_name", "") for t in cls.get("trait_set", [])]
            for did, dname in diseases.items():  # link only to the disease ClinVar names
                if any(len(words(t) & words(dname)) >= 2 or words(dname) <= words(t) for t in traits):
                    gb.edge(f"ClinVar:{vid}", did, "pathogenic_for", "clinvar", vurl,
                            quote=f"{cls.get('description', '')}: {'; '.join(traits)}")
    gb.covered(gene_id, "clinvar", term, int(s["esearchresult"]["count"]), len(ids))


# ---------- ClinicalTrials.gov v2: trials, sponsors, interventions, site countries ----------
# ClinicalTrials.gov location country names -> ISO 3166-1 alpha-2 (covers the names seen in the slice
# plus common trial-site countries). Unknown names fall back to an upper-case slug (see iso2()).
ISO2 = {
    "Argentina": "AR", "Australia": "AU", "Austria": "AT", "Belgium": "BE", "Brazil": "BR", "Bulgaria": "BG",
    "Canada": "CA", "Chile": "CL", "China": "CN", "Colombia": "CO", "Croatia": "HR", "Czechia": "CZ",
    "Czech Republic": "CZ", "Denmark": "DK", "Egypt": "EG", "Estonia": "EE", "Finland": "FI", "France": "FR",
    "Germany": "DE", "Greece": "GR", "Hong Kong": "HK", "Hungary": "HU", "India": "IN", "Indonesia": "ID",
    "Iran": "IR", "Iran, Islamic Republic of": "IR", "Ireland": "IE", "Israel": "IL", "Italy": "IT",
    "Japan": "JP", "Jordan": "JO", "Korea, Republic of": "KR", "Kuwait": "KW", "Latvia": "LV", "Lebanon": "LB",
    "Lithuania": "LT", "Malaysia": "MY", "Mexico": "MX", "Morocco": "MA", "Netherlands": "NL",
    "New Zealand": "NZ", "Norway": "NO", "Pakistan": "PK", "Peru": "PE", "Philippines": "PH", "Poland": "PL",
    "Portugal": "PT", "Qatar": "QA", "Romania": "RO", "Russia": "RU", "Russian Federation": "RU",
    "Saudi Arabia": "SA", "Serbia": "RS", "Singapore": "SG", "Slovakia": "SK", "Slovenia": "SI",
    "South Africa": "ZA", "South Korea": "KR", "Spain": "ES", "Sri Lanka": "LK", "Sweden": "SE",
    "Switzerland": "CH", "Taiwan": "TW", "Thailand": "TH", "Tunisia": "TN", "Turkey": "TR",
    "Turkey (Türkiye)": "TR", "Türkiye": "TR", "Ukraine": "UA", "United Arab Emirates": "AE",
    "United Kingdom": "GB", "United States": "US", "Vietnam": "VN",
}


def iso2(name: str) -> str:
    return ISO2.get(name.strip()) or slug(name).upper()


def trials(gb: GraphBuilder, did: str, query: str, limit: int):
    url = "https://clinicaltrials.gov/api/v2/studies?" + urlencode(
        {"query.cond": query, "pageSize": limit, "countTotal": "true", "sort": "LastUpdatePostDate:desc"})
    data = get_json("ctgov", url)
    studies = data.get("studies", [])
    for st in studies:
        p = st["protocolSection"]
        nct = p["identificationModule"]["nctId"]
        turl = f"https://clinicaltrials.gov/study/{nct}"
        gb.node(nct, "trial", p["identificationModule"]["briefTitle"],
                status=p.get("statusModule", {}).get("overallStatus", ""),
                phase=",".join(p.get("designModule", {}).get("phases", []) or []))
        locs = p.get("contactsLocationsModule", {}).get("locations", []) or []
        names = {iso2(l["country"]): l["country"] for l in locs if l.get("country")}
        if names:
            gb.nodes[nct]["attrs"]["countries"] = sorted(names)
            for code, cname in names.items():
                cid = gb.node(f"COUNTRY:{code}", "country", cname)
                gb.edge(nct, cid, "has_site_in", "ctgov", turl)
        conds = p.get("conditionsModule", {}).get("conditions", [])
        gb.edge(nct, did, "studies", "ctgov", turl, quote="Conditions: " + "; ".join(conds))
        sp = p.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {})
        if sp.get("name"):
            oid = gb.node("ORG:" + slug(sp["name"]), "organization", sp["name"], kind=sp.get("class", "").lower())
            gb.edge(oid, nct, "runs", "ctgov", turl)
        for iv in p.get("armsInterventionsModule", {}).get("interventions", []) or []:
            if iv.get("type") in ("DRUG", "BIOLOGICAL", "GENETIC"):
                iid = gb.node("INTERVENTION:" + slug(iv["name"]), "intervention", iv["name"], kind=iv["type"].lower())
                gb.edge(nct, iid, "tests", "ctgov", turl)
    gb.covered(did, "ctgov", query, data.get("totalCount", len(studies)), len(studies))


# ---------- NIH RePORTER: grants, PIs, funders ----------
# RePORTER's text search is loose (stems, ORs words, matches physics "Fabry-Perot"), so every hit is
# re-checked in code: the title or abstract must name the disease by a specific term (see grant_match).
GRANT_CANDIDATES = 25        # results requested; the top `limit` that pass the filter are kept
COMMON_WORDS = {"hunter", "parkinson", "disease", "syndrome", "deficiency"}
PHYSICS_SENSES = re.compile(r"Fabry[\W_]*P[eé]rot", re.I)
# sentences in which a term means something else (yeast G1 cyclins CLN1/2/3 are not Batten disease)
WRONG_SENSE = [(re.compile(r"^CLN\d", re.I), re.compile(r"yeast|cerevisiae|cyclin", re.I))]
# Sanfilippo subtypes: a generic mention (no subtype letter) links to all four only when no subtype /
# subtype gene is named; otherwise only the named subtypes are linked.
SANFILIPPO_GENES = {"SGSH": "A", "NAGLU": "B", "HGSNAT": "C", "GNS": "D"}
SANFILIPPO_GENERIC = [r"\bSanfilippo\b", r"\bMPS[\s-]?(?:III|3)(?![A-Da-d0-9IVX])",
                      r"\bmucopolysaccharidos[ie]s?[\s,-]+(?:type\s+)?(?:III|3)(?![A-Da-d0-9IVX])"]


def _fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in t if not unicodedata.combining(c))


def _term_regex(term: str, gene: bool = False) -> re.Pattern:
    """Whole-token regex for a term; single-letter tokens (subtype letters) and gene symbols are case-sensitive."""
    if gene:
        return re.compile(rf"(?<![A-Za-z0-9-]){re.escape(term)}(?![A-Za-z0-9])")
    toks = re.findall(r"[^\W_]+", _fold(term))
    parts = [f"(?-i:{t.upper()})" if len(t) == 1 and t.isalpha() else re.escape(t) for t in toks]
    return re.compile(r"(?<![^\W_])" + r"[\W_]*".join(parts) + r"(?![^\W_])", re.I)


def _specific_terms(gb: GraphBuilder, did: str) -> tuple[list[str], list[str]]:
    """(name-like terms, gene symbols) that identify the disease: name, short, MONDO synonyms with >=2 words
    or >=5 chars that are not common words, causal gene symbol(s)."""
    n = gb.nodes[did]
    a = n["attrs"]
    syns = [x for x in (n.get("synonyms") or "").split("|") if x]
    syns = [x for x in syns if (len(x.split()) >= 2 or len(x) >= 5) and x.lower() not in COMMON_WORDS]
    names = [n["name"], a.get("short") or "", a.get("mondo_label") or ""] + syns
    genes = [gb.nodes[e["dst"]]["name"] for e in gb.edges.values()
             if e["relation"] == "caused_by" and e["src"] == did and e["dst"] in gb.nodes]
    return [t for t in dict.fromkeys(names) if t], list(dict.fromkeys(genes))


def grant_terms(gb: GraphBuilder) -> dict[str, dict]:
    """Per slice disease: specific term regexes (terms shared with another slice disease are dropped as
    ambiguous), the Sanfilippo subtype letter, and generic family regexes."""
    raw = {d: _specific_terms(gb, d) for d, n in gb.nodes.items() if n["type"] == "disease"}
    owners: dict[str, set] = {}
    for d, (names, genes) in raw.items():
        for t in names + genes:
            owners.setdefault(t.lower(), set()).add(d)
    out = {}
    for d, (names, genes) in raw.items():
        keep = [(t, False) for t in names if len(owners[t.lower()]) == 1]
        keep += [(t, True) for t in genes if len(owners[t.lower()]) == 1]
        sub = next((SANFILIPPO_GENES[g] for g in genes if g in SANFILIPPO_GENES), None)
        if sub:  # subtype letter forms not always among the MONDO synonyms
            keep += [(f"MPS III{sub}", False), (f"MPS3{sub}", False), (f"Sanfilippo {sub}", False),
                     (f"Sanfilippo type {sub}", False), (f"Sanfilippo syndrome type {sub}", False)]
        shared_gene = any(len(owners[g.lower()]) > 1 for g in genes)
        out[d] = {"specific": [(t, _term_regex(t, gene)) for t, gene in keep], "subtype": sub,
                  "shared_gene": shared_gene,
                  "generic": [re.compile(r, re.I) for r in SANFILIPPO_GENERIC] if sub else []}
    return out


def _sentences(text: str) -> list[str]:
    """Sentences of an abstract; hard line wraps inside a paragraph are joined first."""
    out = []
    for para in re.split(r"\n\s*\n", text or ""):
        para = re.sub(r"\s+", " ", para).strip()
        out += [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(])", para) if s.strip()]
    return out


def _seg_hits(spec, f: str) -> bool:
    for t, rx in spec:
        if rx.search(f) and not any(trx.search(t) and ctx.search(f) for trx, ctx in WRONG_SENSE):
            return True
    return False


def _clip(text: str, n: int = 300) -> str:
    """Cut at a word boundary so a quote never ends mid-word."""
    if len(text) <= n:
        return text
    cut = text[:n + 1].rsplit(" ", 1)[0].rstrip(" ,;:")
    return cut or text[:n]


def grant_match(terms: dict[str, dict], did: str, title: str, abstract: str, query: str = "") -> str | None:
    """The title / abstract sentence that names `did` specifically, or None if the grant is not about it.
    `query` (the RePORTER term): when the disease's causal gene is shared with another slice disease
    (GBA1: Gaucher + Parkinson), a sentence naming every part of an "A AND B" query also counts."""
    segments = [title or ""] + _sentences(abstract)
    folded = [PHYSICS_SENSES.sub(" ", _fold(s)) for s in segments]
    own = terms[did]
    hit = next((seg for seg, f in zip(segments, folded) if _seg_hits(own["specific"], f)), None)
    if not hit and own.get("shared_gene") and " AND " in query:
        parts = [re.compile(r"(?<![A-Za-z0-9])" + re.escape(p.strip().strip('"')), re.I) for p in query.split(" AND ")]
        hit = next((seg for seg, f in zip(segments, folded) if all(rx.search(f) for rx in parts)), None)
    if hit or not own["generic"]:
        return _clip(hit) if hit else None
    # generic Sanfilippo / MPS III: only when no subtype (term or gene) is named anywhere
    siblings = [t for d, t in terms.items() if t["subtype"]]
    if any(_seg_hits(t["specific"], f) for t in siblings for f in folded):
        return None
    hit = next((seg for seg, f in zip(segments, folded) if any(rx.search(f) for rx in own["generic"])), None)
    return _clip(hit) if hit else None


def grants(gb: GraphBuilder, did: str, term: str, limit: int, years: list[int]) -> list[dict]:
    url = "https://api.reporter.nih.gov/v2/projects/search"
    body = {"criteria": {"fiscal_years": years, "advanced_text_search": {
        "operator": "advanced", "search_field": "projecttitle,abstracttext", "search_text": term}},
        "limit": max(GRANT_CANDIDATES, limit), "sort_field": "fiscal_year", "sort_order": "desc"}
    data = get_json("reporter", url, body=body)
    terms = grant_terms(gb)
    pis, kept = [], 0
    for pj in data.get("results", []):
        if kept >= limit:
            break
        quote_ = grant_match(terms, did, pj.get("project_title") or "", pj.get("abstract_text") or "", term)
        if not quote_:
            continue
        kept += 1
        gid = gb.node(f"RePORTER:{pj['core_project_num']}", "grant", pj["project_title"],
                      fiscal_year=pj.get("fiscal_year"), org=(pj.get("organization") or {}).get("org_name", ""))
        gurl = pj.get("project_detail_url") or url
        gb.edge(gid, did, "studies", "reporter", gurl, quote=quote_)
        for pi in pj.get("principal_investigators", []):
            pid = gb.node(f"PERSON:{pi['profile_id']}", "person", pi["full_name"].replace("  ", " ").title(),
                          last=pi["last_name"], first=pi["first_name"])
            gb.edge(pid, gid, "pi_of", "reporter", gurl)
            pis.append(pi)
        ic = (pj.get("agency_ic_admin") or {}).get("abbreviation")
        if ic:
            fid = gb.node(f"ORG:NIH_{ic}", "organization", f"NIH {ic}", kind="funder")
            gb.edge(fid, gid, "funds", "reporter", gurl)
    gb.covered(did, "reporter", term, data.get("meta", {}).get("total", 0), kept)
    return pis


# ---------- PubMed: recent papers mentioning the disease ----------
def papers(gb: GraphBuilder, did: str, query: str, limit: int, raw_term: str | None = None):
    term = raw_term or " OR ".join(f'"{q.strip()}"[tiab]' for q in query.split(" OR "))
    s = get_json("pubmed", f"{EUTILS}/esearch.fcgi?db=pubmed&retmode=json&sort=pub_date&retmax={limit}&term={quote(term)}")
    ids = s["esearchresult"]["idlist"]
    if ids:
        summ = get_json("pubmed", f"{EUTILS}/esummary.fcgi?db=pubmed&retmode=json&id={','.join(ids)}")["result"]
        for pmid in ids:
            p = summ[pmid]
            purl = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
            gb.node(f"PMID:{pmid}", "paper", p.get("title", ""), journal=p.get("source", ""),
                    pubdate=p.get("pubdate", ""), authors=[a["name"] for a in p.get("authors", [])])
            gb.edge(f"PMID:{pmid}", did, "mentions", "pubmed", purl, quote=p.get("title", ""))
    gb.covered(did, "pubmed", term, int(s["esearchresult"]["count"]), len(ids))
