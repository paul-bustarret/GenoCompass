"""Build the knowledge graph for the slice: python -m atlas.build"""
import json
import re
from collections import Counter
from pathlib import Path

import yaml

from . import sources
from .graph import OUT, GraphBuilder

ROOT = Path(__file__).resolve().parent.parent


def link_authors_to_pis(gb: GraphBuilder):
    """PubMed authors are only strings; link a paper to a RePORTER PI when 'Last F' matches."""
    pis = {}
    for n in gb.nodes.values():
        if n["type"] == "person":
            a = n["attrs"]
            pis[(a["last"].lower(), a["first"][:1].lower())] = n["id"]
    for n in list(gb.nodes.values()):
        if n["type"] != "paper":
            continue
        for author in n["attrs"].get("authors", []):
            m = re.match(r"(.+?)\s+([A-Z])[A-Z]*$", author)
            if m and (key := (m.group(1).lower(), m.group(2).lower())) in pis:
                gb.edge(pis[key], n["id"], "authored", "pubmed",
                        f"https://pubmed.ncbi.nlm.nih.gov/{n['id'].split(':')[1]}/",
                        evidence="inferred", confidence=0.6,
                        quote=f"Author '{author}' matched RePORTER PI by last name + initial")


def build(config=ROOT / "config" / "slice.yaml"):
    cfg = yaml.safe_load(open(config, encoding="utf-8"))
    lim = cfg["limits"]
    gb = GraphBuilder()
    diseases = {}  # did -> slice entry
    genes = {}
    for d in cfg["diseases"]:
        g = genes.setdefault(d["gene"], sources.gene(gb, d["gene"]))
        did = sources.hpo_disease(gb, d, g["ncbi"])
        if did is None:
            print(f"  ! dropped {d['short']}: {d['omim']} not found in HPO")
            continue
        diseases[did] = d
        sources.mondo(gb, did)
        print(f"  {d['short']:24} {did}")

    names = {did: gb.nodes[did]["name"] for did in diseases}
    for sym, g in genes.items():
        sources.reactome(gb, g["ncbi"], g["uniprot"])
        gene_diseases = {did: names[did] for did, d in diseases.items() if d["gene"] == sym}
        sources.clinvar(gb, sym, g["ncbi"], gene_diseases, lim["variants_per_gene"])

    for did, d in diseases.items():
        sources.trials(gb, did, d["query"], lim["trials_per_disease"])
        sources.grants(gb, did, d["reporter"], lim["grants_per_disease"], lim["grant_fiscal_years"])
        sources.papers(gb, did, d["query"], lim["papers_per_disease"], d.get("pubmed"))
        # Patient groups / registries need web pages + LLM extraction (placeholder until key arrives)
        gb.covered(did, "patient_groups", "web + LLM", -1, 0)

    link_authors_to_pis(gb)
    gb.write()

    report = {
        "diseases": len(diseases),
        "nodes": len(gb.nodes),
        "edges": len(gb.edges),
        "nodes_by_type": dict(Counter(n["type"] for n in gb.nodes.values())),
        "edges_by_relation": dict(Counter(e["relation"] for e in gb.edges.values())),
        "edges_by_source": dict(Counter(e["source"] for e in gb.edges.values())),
        "not_yet_covered": ["patient groups / registries (web + LLM)", "mechanism + LoF/GoF (PubMed + LLM)"],
    }
    (OUT / "build_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(build(), indent=2))
