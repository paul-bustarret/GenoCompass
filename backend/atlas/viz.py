"""Pictures of the graph: python -m atlas.viz

figures/disease_map.png      diseases only, coloured by computed cluster, edges = similarity with top witness
figures/knowledge_graph.png  the full typed graph around the Sanfilippo (MPS III) cluster
figures/atlas.html           interactive view of the whole graph; click an edge to see its source
Also writes the computed (non-evidence) results: data/graph/similarity.csv, clusters.json.
"""
import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx

from . import similarity as S
from .graph import OUT, load

FIG = Path(__file__).resolve().parent.parent / "figures"
TYPE_COLORS = {"disease": "#d62728", "gene": "#1f77b4", "variant": "#17becf", "phenotype": "#ff9f43",
               "pathway": "#2ca02c", "mechanism": "#9467bd", "paper": "#8c564b", "trial": "#e377c2",
               "intervention": "#f368e0", "grant": "#bcbd22", "person": "#7f7f7f",
               "organization": "#3c40c6"}
CLUSTER_COLORS = ["#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#76b7b2", "#edc948", "#b07aa1"]
ROLE_EDGE = {"hero": "#000000", "lysosomal": "#555555", "bridge": "#9467bd", "lookalike": "#d62728"}


def short(g, n):
    return g.nodes[n]["attrs"].get("short") or g.nodes[n]["name"]


def save_computed(g, pairs, comms):
    with open(OUT / "similarity.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["a", "b", "therapeutic", "phenotype_view", "gene", "pathway", "phenotype",
                    "lookalike", "top_witnesses"])
        for (a, b), s in sorted(pairs.items(), key=lambda kv: -kv[1]["therapeutic"]):
            wit = [g.nodes[x]["name"] for k in ("gene", "pathway", "phenotype") for x in s["witnesses"][k][:2]]
            c = s["components"]
            w.writerow([a, b, s["therapeutic"], s["phenotype"], c["gene"], c["pathway"], c["phenotype"],
                        s["lookalike"], " | ".join(wit)])
    (OUT / "clusters.json").write_text(json.dumps(
        [{"cluster": i, "diseases": sorted(c), "names": sorted(short(g, d) for d in c)} for i, c in enumerate(comms)],
        indent=2), encoding="utf-8")


def disease_map(g, pairs, comms):
    dis = [n for n, d in g.nodes(data=True) if d["type"] == "disease"]
    color = {}
    for i, c in enumerate(comms):
        for d in c:
            color[d] = CLUSTER_COLORS[i] if len(c) > 1 else "#d9d9d9"
    h = nx.Graph()
    h.add_nodes_from(dis)
    strong = {k: s for k, s in pairs.items() if s["therapeutic"] >= S.CLUSTER_MIN}
    looks = {k: s for k, s in pairs.items() if s["lookalike"]}
    for (a, b), s in strong.items():
        h.add_edge(a, b, weight=s["therapeutic"])
    # clustered diseases get a force layout; unclustered ones go in their own row below,
    # so no lone disease is drawn on top of an edge it doesn't belong to
    linked = [n for n in dis if h.degree(n)]
    lone = sorted((n for n in dis if not h.degree(n)), key=lambda n: (g.nodes[n]["attrs"].get("role", ""), short(g, n)))
    pos = nx.spring_layout(h.subgraph(linked), weight="weight", seed=4, k=0.6)
    xs = [p[0] for p in pos.values()]
    y_row = min(p[1] for p in pos.values()) - 0.45
    for i, n in enumerate(lone):
        pos[n] = (min(xs) + i * (max(xs) - min(xs)) / max(len(lone) - 1, 1), y_row)
    fig, ax = plt.subplots(figsize=(18, 11))
    ax.text(min(xs), y_row + 0.14, "no supported cluster (score < %.2f to every other disease):" % S.CLUSTER_MIN,
            fontsize=9, color="#555")
    nx.draw_networkx_edges(h, pos, ax=ax, edgelist=list(strong), edge_color="#888",
                           width=[1 + 9 * s["therapeutic"] for s in strong.values()])
    nx.draw_networkx_edges(h, pos, ax=ax, edgelist=list(looks), edge_color="#d62728", style="dashed", width=2.5,
                           connectionstyle="arc3,rad=0.35", arrows=True, arrowstyle="-", node_size=1500)
    for n in dis:
        role = g.nodes[n]["attrs"].get("role", "")
        ax.scatter(*pos[n], s=1500, c=color[n], edgecolors=ROLE_EDGE.get(role, "#555"),
                   linewidths=3 if role in ("hero", "bridge", "lookalike") else 1, zorder=3)
        gene = next((g.nodes[v]["name"] for _, v, e in g.out_edges(n, data=True) if e["relation"] == "caused_by"), "")
        ax.annotate(f"{short(g, n)}\n{gene}", pos[n], ha="center", va="center", fontsize=8, zorder=4,
                    fontweight="bold" if role == "hero" else "normal")
    labels = {}
    for (a, b), s in strong.items():
        for kind in ("gene", "pathway", "phenotype"):
            if s["witnesses"][kind]:
                labels[(a, b)] = f"{s['therapeutic']:.2f} · {g.nodes[s['witnesses'][kind][0]]['name'][:20]}"
                break
    for (a, b), s in looks.items():
        ax.annotate(f"lookalike: {g.nodes[s['witnesses']['phenotype'][0]]['name'][:30]}",
                    ((pos[a][0] + pos[b][0]) / 2, pos[a][1] - 0.12), ha="center", fontsize=7, color="#d62728")
    nx.draw_networkx_edge_labels(h, pos, ax=ax, edge_labels=labels, font_size=6.5)
    for i, c in enumerate(comms):
        if len(c) > 1:
            ax.scatter([], [], c=CLUSTER_COLORS[i], s=120, label=f"cluster {i + 1}: " + ", ".join(sorted(short(g, d) for d in c)))
    ax.scatter([], [], c="#d9d9d9", s=120, label="no supported cluster")
    ax.plot([], [], c="#d62728", ls="--", label="lookalike: similar symptoms, no shared biology")
    for role, col in ROLE_EDGE.items():
        ax.scatter([], [], facecolors="white", edgecolors=col, linewidths=3, s=120, label=f"role: {role}")
    ax.legend(loc="upper right", fontsize=7.5, frameon=False)
    ax.set_title("Rare Disease Atlas — disease map (21 diseases)\n"
                 "edge = computed similarity (rarity-weighted shared genes, Reactome pathways, HPO symptoms); "
                 "label = score · strongest shared witness", fontsize=11)
    ax.axis("off")
    fig.tight_layout()
    fig.savefig(FIG / "disease_map.png", dpi=140)
    plt.close(fig)


def knowledge_graph(g, focus_short=("MPS IIIA", "MPS IIIB", "MPS IIIC", "MPS IIID")):
    """Typed subgraph around the focus diseases: everything 1 hop out, plus 1 more hop for
    genes/trials/grants (pathways, variants, sponsors, PIs). Phenotypes: only those shared by 2+ focus diseases."""
    focus = [n for n, d in g.nodes(data=True) if d["type"] == "disease" and d["attrs"].get("short") in focus_short]
    keep = set(focus)
    und = g.to_undirected(as_view=True)
    for d in focus:
        for n in und.neighbors(d):
            t = g.nodes[n]["type"]
            if t == "phenotype":
                if sum(g.has_edge(f, n) for f in focus) >= 2:
                    keep.add(n)
            elif t != "disease":
                keep.add(n)
    for n in list(keep):
        if g.nodes[n]["type"] in ("gene", "trial", "grant"):
            keep.update(m for m in und.neighbors(n) if g.nodes[m]["type"] != "disease")
    sub = nx.Graph(g.subgraph(keep))
    pos = nx.spring_layout(sub, seed=2, k=0.35, iterations=200)
    fig, ax = plt.subplots(figsize=(20, 15))
    evid = nx.get_edge_attributes(sub, "evidence")
    nx.draw_networkx_edges(sub, pos, ax=ax, edge_color=["#9467bd" if evid.get(e) == "inferred" else "#bbb" for e in sub.edges],
                           width=0.6)
    for t, col in TYPE_COLORS.items():
        ns = [n for n in sub if g.nodes[n]["type"] == t]
        if ns:
            nx.draw_networkx_nodes(sub, pos, nodelist=ns, ax=ax, node_color=col,
                                   node_size=900 if t == "disease" else 220 if t in ("gene", "pathway") else 70,
                                   label=f"{t} ({len(ns)})")
    big = {n: (short(g, n) if g.nodes[n]["type"] == "disease" else g.nodes[n]["name"][:32])
           for n in sub if g.nodes[n]["type"] in ("disease", "gene", "pathway", "organization", "intervention")}
    nx.draw_networkx_labels(sub, pos, labels=big, ax=ax, font_size=6.5)
    ax.plot([], [], c="#9467bd", label="inferred edge (author↔PI name match)")
    ax.legend(loc="lower left", fontsize=9, frameon=False, markerscale=1.2)
    ax.set_title(f"Rare Disease Atlas — knowledge graph around the Sanfilippo (MPS III) cluster: "
                 f"{sub.number_of_nodes()} nodes, {sub.number_of_edges()} edges\n"
                 "every edge carries source + URL (HPO, Reactome, ClinVar, ClinicalTrials.gov, NIH RePORTER, PubMed)",
                 fontsize=12)
    ax.axis("off")
    fig.tight_layout()
    fig.savefig(FIG / "knowledge_graph.png", dpi=130)
    plt.close(fig)


def html(g, comms):
    cluster_of = {d: i for i, c in enumerate(comms) for d in c if len(c) > 1}
    nodes = [{"id": n, "label": short(g, n) if d["type"] == "disease" else d["name"][:40], "group": d["type"],
              "color": TYPE_COLORS.get(d["type"], "#999"), "title": f"{d['type']}: {d['name']}\n{n}",
              "size": 28 if d["type"] == "disease" else 10, "cluster": cluster_of.get(n, -1)}
             for n, d in g.nodes(data=True)]
    edges = [{"from": u, "to": v, "label": e["relation"], "source": e["source"], "url": e["source_url"],
              "evidence": e["evidence"], "confidence": e["confidence"], "quote": e["quote"], "id": k}
             for u, v, k, e in g.edges(keys=True, data=True)]
    tpl = (Path(__file__).parent / "atlas_template.html").read_text(encoding="utf-8")
    out = tpl.replace("/*NODES*/[]", json.dumps(nodes)).replace("/*EDGES*/[]", json.dumps(edges))
    (FIG / "atlas.html").write_text(out, encoding="utf-8")


def main():
    FIG.mkdir(exist_ok=True)
    g = load()
    pairs = S.all_pairs(g)
    comms = S.clusters(pairs, [n for n, d in g.nodes(data=True) if d["type"] == "disease"])
    save_computed(g, pairs, comms)
    disease_map(g, pairs, comms)
    knowledge_graph(g)
    html(g, comms)
    print("wrote", *[p.name for p in sorted(FIG.iterdir())])


if __name__ == "__main__":
    main()
