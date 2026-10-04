"""Disease-disease similarity computed from the graph (never stored as evidence), plus clustering.

Features per disease: genes (caused_by), pathways (gene -> in_pathway, plus each pathway's Reactome
parent via part_of), phenotypes (has_phenotype), mechanisms (has_mechanism, polarity=supports; extracted).
Each shared feature is weighted by rarity across the slice: idf = log(N / diseases_with_feature),
so a symptom every disease has counts ~0 and a rare shared one counts a lot.
"""
import math
from pathlib import Path
from itertools import combinations

import networkx as nx
import yaml

HUB_PATH = Path(__file__).resolve().parent.parent / "config" / "hub_pathways.yaml"

KINDS = ("gene", "pathway", "phenotype", "mechanism")
MECH_MIN_SOURCES = 2  # distinct supporting papers needed before a mechanism enters similarity
# Without mechanism evidence for either disease, mechanism's weight is folded into pathway.
VIEWS = {
    "therapeutic": {"pathway": 0.5, "phenotype": 0.3, "gene": 0.2},
    "phenotype": {"phenotype": 0.8, "pathway": 0.2},
}
# Used for the therapeutic view when at least one disease of the pair has mechanism edges.
THERAPEUTIC_WITH_MECHANISM = {"mechanism": 0.3, "pathway": 0.35, "phenotype": 0.2, "gene": 0.15}
CLUSTER_MIN = 0.12      # therapeutic score needed for an edge in the clustering graph
LOOKALIKE_PHENO = 0.15  # similar symptoms ...
LOOKALIKE_BIO = 0.02    # ... but (almost) no shared pathway/gene


def hub_pathways(path: Path = HUB_PATH) -> set[str]:
    """Generic cell-biology hub pathways (config/hub_pathways.yaml) excluded from similarity features."""
    try:
        return {p["id"] for p in yaml.safe_load(open(path, encoding="utf-8")).get("hub_pathways") or []}
    except FileNotFoundError:
        return set()


def features(g: nx.MultiDiGraph) -> dict[str, dict[str, set]]:
    hubs = hub_pathways()
    feats = {}
    for d, data in g.nodes(data=True):
        if data["type"] != "disease":
            continue
        genes = {v for _, v, e in g.out_edges(d, data=True) if e["relation"] == "caused_by"}
        leaf = {p for gene in genes for _, p, e in g.out_edges(gene, data=True) if e["relation"] == "in_pathway"}
        parents = {q for p in leaf for _, q, e in g.out_edges(p, data=True) if e["relation"] == "part_of"}
        pathways = (leaf | parents) - hubs
        phenos = {v for _, v, e in g.out_edges(d, data=True) if e["relation"] == "has_phenotype"}
        # A mechanism counts only when >= MECH_MIN_SOURCES distinct papers support it for this disease
        # (single-paper claims stay in the graph as evidence but don't drive clustering); MECH:other never counts.
        mech_sources = {}
        for _, v, e in g.out_edges(d, data=True):
            if (e["relation"] == "has_mechanism" and (e.get("polarity") or "supports") == "supports"
                    and v != "MECH:other"):
                mech_sources.setdefault(v, set()).add(e.get("source_url"))
        mechs = {v for v, srcs in mech_sources.items() if len(srcs) >= MECH_MIN_SOURCES}
        feats[d] = {"gene": genes, "pathway": pathways, "phenotype": phenos, "mechanism": mechs}
    return feats


def idf(feats) -> dict[str, float]:
    n = len(feats)
    df = {}
    for f in feats.values():
        for kind in f.values():
            for x in kind:
                df[x] = df.get(x, 0) + 1
    return {x: math.log(n / c) for x, c in df.items()}


def compare(a: dict, b: dict, w: dict) -> dict:
    out = {"components": {}, "witnesses": {}}
    for kind in KINDS:
        shared, union = a.get(kind, set()) & b.get(kind, set()), a.get(kind, set()) | b.get(kind, set())
        den = sum(w[x] for x in union)
        out["components"][kind] = round(sum(w[x] for x in shared) / den, 3) if den else 0.0
        out["witnesses"][kind] = sorted(shared, key=lambda x: -w[x])
    has_mech = bool(a.get("mechanism") or b.get("mechanism"))
    for view, weights in VIEWS.items():
        if view == "therapeutic" and has_mech:
            weights = THERAPEUTIC_WITH_MECHANISM
        out[view] = round(sum(out["components"][k] * wt for k, wt in weights.items()), 3)
    c = out["components"]
    out["lookalike"] = c["phenotype"] >= LOOKALIKE_PHENO and c["pathway"] + c["gene"] <= LOOKALIKE_BIO
    return out


def all_pairs(g: nx.MultiDiGraph) -> dict[tuple[str, str], dict]:
    feats = features(g)
    w = idf(feats)
    return {(a, b): compare(feats[a], feats[b], w) for a, b in combinations(sorted(feats), 2)}


def clusters(pairs: dict, diseases) -> list[set]:
    h = nx.Graph()
    h.add_nodes_from(diseases)
    for (a, b), s in pairs.items():
        if s["therapeutic"] >= CLUSTER_MIN:
            h.add_edge(a, b, weight=s["therapeutic"])
    comms = nx.community.louvain_communities(h, weight="weight", seed=0)
    return sorted(comms, key=len, reverse=True)


def similar(g, disease_id: str, view="therapeutic", k=5, pairs=None) -> list[dict]:
    pairs = pairs or all_pairs(g)
    rows = []
    for (a, b), s in pairs.items():
        if disease_id in (a, b):
            other = b if a == disease_id else a
            rows.append({"id": other, "name": g.nodes[other]["attrs"].get("short", g.nodes[other]["name"]),
                         "score": s[view], **s})
    return sorted(rows, key=lambda r: -r["score"])[:k]
