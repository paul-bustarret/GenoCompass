"""THROWAWAY SPIKE: fetch one record per source for Pompe disease, build a tiny graph, draw it.

Run:  uv run --with requests --with networkx --with matplotlib python build_sample.py
"""
import csv
import hashlib
import html
import json
import re
from datetime import date
from pathlib import Path

import requests

HERE = Path(__file__).parent
RAW = HERE / "raw"
RAW.mkdir(exist_ok=True)
TODAY = date.today().isoformat()
UA = {"User-Agent": "rare-disease-atlas-spike/0.1 (hackathon test)"}

nodes: dict[str, dict] = {}
edges: list[dict] = []
coverage: list[dict] = []
findings: list[str] = []


# ---------- helpers ----------
def fetch(name, url, method="GET", body=None, as_json=True):
    path = RAW / (name + (".json" if as_json else ".html"))
    if path.exists():
        text = path.read_text()
    else:
        r = (requests.post(url, json=body, headers=UA, timeout=30) if method == "POST"
             else requests.get(url, headers=UA, timeout=30))
        r.raise_for_status()
        text = r.text
        path.write_text(text)
    return json.loads(text) if as_json else text


def node(id_, type_, name, **attrs):
    nodes.setdefault(id_, {"id": id_, "type": type_, "name": name, "attrs": json.dumps(attrs)})
    return id_


def edge(src, dst, relation, source, url, evidence="observed", confidence=0.95, quote="", effect=""):
    eid = "E" + hashlib.sha1(f"{src}|{dst}|{relation}|{url}".encode()).hexdigest()[:6]
    edges.append(dict(edge_id=eid, src=src, dst=dst, relation=relation, source=source,
                      source_url=url, retrieved_at=TODAY, confidence=confidence,
                      evidence=evidence, polarity="supports", effect=effect, quote=quote))


def cover(source, query, n):
    coverage.append(dict(disease="Pompe", source=source, query=query, n_results=n, retrieved_at=TODAY))


def norm(s):
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_ok(quote, text):
    return bool(quote) and norm(quote) in norm(text)


def stub_llm_extract(text, pattern):
    """Placeholder for the LLM: returns the first sentence matching `pattern` as the 'quote'."""
    for sent in re.split(r"(?<=[.!?])\s+", text):
        if re.search(pattern, sent, re.I):
            return sent.strip()
    return None


# ---------- 1. MONDO (OLS API) ----------
url = "https://www.ebi.ac.uk/ols4/api/search?q=Pompe%20disease&ontology=mondo&rows=1&exact=true"
doc = fetch("mondo", url)["response"]["docs"][0]
DIS = node(doc["obo_id"], "disease", doc["label"], synonyms="Pompe disease")
cover("mondo", "Pompe disease", 1)

# ---------- 2. HPO (JAX API) ----------
url = "https://ontology.jax.org/api/network/annotation/OMIM:232300"
hpo = fetch("hpo", url)
if hpo["disease"].get("mondoId") != DIS:
    findings.append(f"ID mismatch: MONDO search -> {DIS}, HPO/JAX -> {hpo['disease'].get('mondoId')} for the same disease")
phenos = [p for ps in hpo["categories"].values() for p in ps][:3]
for p in phenos:
    node(p["id"], "phenotype", p["name"])
    edge(DIS, p["id"], "has_phenotype", "hpo", url, quote="")
for g in hpo.get("genes", [])[:1]:
    GENE = node(g["id"], "gene", g["name"])
    edge(DIS, GENE, "caused_by", "hpo", url)
cover("hpo", "OMIM:232300", len(phenos))

# ---------- 3. Orphadata (cross-referencing API) ----------
url = "https://api.orphadata.com/rd-cross-referencing/orphacodes/365?lang=en"
orpha = fetch("orphadata", url)
res = orpha.get("data", {}).get("results", {})
ORPHA = node("ORPHA:365", "xref", res.get("Preferred term", "ORPHA:365"))
edge(DIS, ORPHA, "same_as", "orphadata", url)
cover("orphadata", "ORPHA:365", 1)

# ---------- 4. ClinVar (E-utilities) ----------
q = "GAA[gene] AND clinsig_pathogenic[prop]"
s = fetch("clinvar_search", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=clinvar&retmode=json&retmax=1&term=" + requests.utils.quote(q))
vid = s["esearchresult"]["idlist"][0]
url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=clinvar&retmode=json&id={vid}"
v = fetch("clinvar_summary", url)["result"][vid]
sig = (v.get("germline_classification") or v.get("clinical_significance") or {}).get("description", "")
VAR = node(f"ClinVar:{vid}", "variant", v["title"][:40], significance=sig)
edge(GENE, VAR, "has_variant", "clinvar", f"https://www.ncbi.nlm.nih.gov/clinvar/variation/{vid}/")
edge(VAR, DIS, "pathogenic_for", "clinvar", f"https://www.ncbi.nlm.nih.gov/clinvar/variation/{vid}/", quote=sig)
cover("clinvar", q, int(s["esearchresult"]["count"]))

# ---------- 5. Reactome (content service) ----------
url = "https://reactome.org/ContentService/data/mapping/UniProt/P10253/pathways?species=9606"
pw = fetch("reactome", url)
findings.append("Reactome: GAA is in %d pathways incl. broad ones (%s); naive pick is misleading -> need hub filtering" % (len(pw), sorted(pw, key=lambda p: len(p["displayName"]))[0]["displayName"]))
pw = next((p for p in pw if "lycogen" in p["displayName"]), pw[0])
PW = node(pw["stId"], "pathway", pw["displayName"])
edge(GENE, PW, "in_pathway", "reactome", url)
cover("reactome", "UniProt:P10253", 1)

# ---------- 6. ClinicalTrials.gov (API v2) ----------
url = "https://clinicaltrials.gov/api/v2/studies?query.cond=Pompe%20disease&pageSize=1"
ct = fetch("ctgov", url)
st = ct["studies"][0]["protocolSection"]
nct = st["identificationModule"]["nctId"]
TRIAL = node(nct, "trial", st["identificationModule"]["briefTitle"][:45])
edge(TRIAL, DIS, "studies", "ctgov", f"https://clinicaltrials.gov/study/{nct}")
sponsor = st.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name")
if sponsor:
    SP = node("ORG:" + re.sub(r"\W+", "_", sponsor)[:30], "organization", sponsor, kind="sponsor")
    edge(SP, TRIAL, "runs", "ctgov", f"https://clinicaltrials.gov/study/{nct}")
cover("ctgov", "cond=Pompe disease", ct.get("totalCount", 1))

# ---------- 7. NIH RePORTER (API) ----------
url = "https://api.reporter.nih.gov/v2/projects/search"
body = {"criteria": {"advanced_text_search": {"operator": "and", "search_field": "projecttitle,terms",
                                              "search_text": "Pompe"}}, "limit": 1}
rp = fetch("reporter", url, method="POST", body=body)
pj = rp["results"][0]
GRANT = node(f"RePORTER:{pj['project_num']}", "grant", pj["project_title"][:45])
edge(GRANT, DIS, "studies", "reporter", pj.get("project_detail_url") or url, quote=pj["project_title"])
for pi in pj.get("principal_investigators", [])[:1]:
    P = node(f"PERSON:{pi['profile_id']}", "person", pi["full_name"])
    edge(P, GRANT, "pi_of", "reporter", pj.get("project_detail_url") or url)
agency = pj.get("agency_ic_admin", {}).get("abbreviation")
if agency:
    F = node(f"ORG:{agency}", "organization", agency, kind="funder")
    edge(F, GRANT, "funds", "reporter", pj.get("project_detail_url") or url)
cover("reporter", "Pompe", rp["meta"]["total"])

# ---------- 8. PubMed (E-utilities) + stub LLM extraction ----------
q = "Pompe disease[MeSH] AND acid alpha-glucosidase AND hasabstract"
s = fetch("pubmed_search", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&retmode=json&retmax=1&term=" + requests.utils.quote(q))
pmid = s["esearchresult"]["idlist"][0]
abstract = fetch("pubmed_abstract", f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id={pmid}&rettype=abstract&retmode=text", as_json=False)
purl = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
PAPER = node(f"PMID:{pmid}", "paper", f"PMID {pmid}")
edge(PAPER, DIS, "mentions", "pubmed", purl)
quote = stub_llm_extract(abstract, r"deficien")
if quote and quote_ok(quote, abstract):
    MECH = node("MECH:lysosomal_enzyme_deficiency", "mechanism", "lysosomal enzyme deficiency")
    edge(DIS, MECH, "has_mechanism", "pubmed", purl, evidence="fixture", confidence=0.5, quote=quote[:200], effect="LoF")
else:
    findings.append("PubMed stub extraction found no quotable mechanism sentence")
cover("pubmed", q, int(s["esearchresult"]["count"]))

# ---------- 9. NORD web page + stub LLM extraction ----------
url = "https://rarediseases.org/rare-diseases/pompe-disease/"
page = fetch("nord", url, as_json=False)
text = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<(script|style).*?</\1>", " ", page)))
quote = stub_llm_extract(text, r"(Association|Alliance|Foundation|Society)")
m = quote and re.search(r"((?:[A-Z][\w'&-]+\s){1,6}(?:Association|Alliance|Foundation|Society)(?:\s(?:of|for)\s(?:[A-Z][\w-]+\s?){1,4})?)", quote)
if m and quote_ok(quote, text):
    org = m.group(1).strip()
    ORG = node("ORG:" + re.sub(r"\W+", "_", org)[:30], "organization", org, kind="patient_group?")
    edge(ORG, DIS, "studies", "web", url, evidence="fixture", confidence=0.4, quote=quote[:200])
    cover("nord", url, 1)
    findings.append(f"NORD: stub extracted '{org}' — quote passes verbatim check but org is not a Pompe group; quote check stops invention, not irrelevance")
else:
    findings.append("NORD page: stub extractor found no organisation name (real LLM needed)")
    cover("nord", url, 0)


# ---------- write ----------
def write(name, rows):
    with open(HERE / name, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


write("nodes.csv", list(nodes.values()))
write("edges.csv", edges)
write("coverage.csv", coverage)
(HERE / "findings.txt").write_text("\n".join(findings) + "\n")

# ---------- draw ----------
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx

G = nx.MultiDiGraph()
for n in nodes.values():
    G.add_node(n["id"], **n)
for e in edges:
    G.add_edge(e["src"], e["dst"], **e)

COLORS = {"disease": "#d62728", "gene": "#1f77b4", "variant": "#17becf", "phenotype": "#ff7f0e",
          "pathway": "#2ca02c", "mechanism": "#9467bd", "paper": "#8c564b", "trial": "#e377c2",
          "grant": "#bcbd22", "person": "#7f7f7f", "organization": "#393b79", "xref": "#c7c7c7"}
pos = nx.kamada_kawai_layout(nx.Graph(G))
fig, ax = plt.subplots(figsize=(15, 11))
solid = [(u, v) for u, v, d in G.edges(data=True) if d["evidence"] != "fixture"]
dashed = [(u, v) for u, v, d in G.edges(data=True) if d["evidence"] == "fixture"]
nx.draw_networkx_edges(G, pos, edgelist=solid, ax=ax, edge_color="#555", arrows=True, arrowsize=12)
nx.draw_networkx_edges(G, pos, edgelist=dashed, ax=ax, edge_color="#9467bd", style="dashed", arrows=True, arrowsize=12)
nx.draw_networkx_nodes(G, pos, ax=ax, node_size=900,
                       node_color=[COLORS[G.nodes[n]["type"]] for n in G.nodes])
nx.draw_networkx_labels(G, pos, ax=ax, font_size=7,
                        labels={n: f"{G.nodes[n]['name'][:28]}\n{n}" for n in G.nodes})
nx.draw_networkx_edge_labels(G, pos, ax=ax, font_size=6,
                             edge_labels={(u, v): f"{d['relation']} [{d['source']}]" for u, v, d in G.edges(data=True)})
for t, c in COLORS.items():
    if any(n["type"] == t for n in nodes.values()):
        ax.scatter([], [], c=c, label=t, s=80)
ax.plot([], [], ls="--", c="#9467bd", label="fixture (stub LLM) edge")
ax.legend(loc="lower left", fontsize=8, frameon=False)
ax.set_title(f"Spike: Pompe disease sample graph — {len(nodes)} nodes, {len(edges)} edges, 9 sources", fontsize=12)
ax.axis("off")
fig.tight_layout()
fig.savefig(HERE / "graph.png", dpi=130)

print(f"nodes={len(nodes)} edges={len(edges)}")
for c in coverage:
    print(f"  {c['source']:10} n={c['n_results']}")
print("findings:", *findings, sep="\n  ")
