"""The one graph: nodes + edges (each edge = one sourced claim) + a coverage log of what was searched."""
import csv
import hashlib
import json
from datetime import date
from pathlib import Path

import networkx as nx

OUT = Path(__file__).resolve().parent.parent / "data" / "graph"

NODE_FIELDS = ["id", "type", "name", "synonyms", "attrs"]
EDGE_FIELDS = ["edge_id", "src", "dst", "relation", "source", "source_url", "retrieved_at",
               "confidence", "evidence", "polarity", "effect", "quote", "frequency", "context", "submitted_by"]
COVERAGE_FIELDS = ["disease_id", "source", "query", "n_results", "n_kept", "retrieved_at"]


class GraphBuilder:
    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.edges: dict[str, dict] = {}
        self.coverage: list[dict] = []
        self.today = date.today().isoformat()

    def node(self, id_: str, type_: str, name: str, synonyms=(), **attrs) -> str:
        if id_ not in self.nodes:
            self.nodes[id_] = {"id": id_, "type": type_, "name": name,
                               "synonyms": "|".join(synonyms), "attrs": attrs}
        else:
            self.nodes[id_]["attrs"].update(attrs)
        return id_

    def edge(self, src, dst, relation, source, source_url, evidence="observed", confidence=0.95,
             quote="", effect="", frequency="", polarity="supports", context="", submitted_by="") -> str:
        eid = "E" + hashlib.sha1(f"{src}|{dst}|{relation}|{source_url}".encode()).hexdigest()[:7]
        self.edges[eid] = dict(edge_id=eid, src=src, dst=dst, relation=relation, source=source,
                               source_url=source_url, retrieved_at=self.today, confidence=confidence,
                               evidence=evidence, polarity=polarity, effect=effect,
                               quote=quote[:300], frequency=frequency, context=context,
                               submitted_by=submitted_by)
        return eid

    def covered(self, disease_id, source, query, n_results, n_kept):
        self.coverage.append(dict(disease_id=disease_id, source=source, query=query,
                                  n_results=n_results, n_kept=n_kept, retrieved_at=self.today))

    @classmethod
    def from_csv(cls, out: Path = OUT) -> "GraphBuilder":
        """Reload a written graph so new evidence can be appended (add_document, refresh_papers)."""
        gb = cls()
        with open(out / "nodes.csv", encoding="utf-8") as f:
            for n in csv.DictReader(f):
                gb.nodes[n["id"]] = {**n, "attrs": json.loads(n["attrs"])}
        with open(out / "edges.csv", encoding="utf-8") as f:
            for e in csv.DictReader(f):
                gb.edges[e["edge_id"]] = {k: e.get(k, "") for k in EDGE_FIELDS}
        if (out / "coverage.csv").exists():
            with open(out / "coverage.csv", encoding="utf-8") as f:
                gb.coverage = list(csv.DictReader(f))
        return gb

    def write(self, out: Path = OUT):
        out.mkdir(parents=True, exist_ok=True)
        _write(out / "nodes.csv", NODE_FIELDS,
               [{**n, "attrs": json.dumps(n["attrs"])} for n in self.nodes.values()])
        _write(out / "edges.csv", EDGE_FIELDS, self.edges.values())
        _write(out / "coverage.csv", COVERAGE_FIELDS, self.coverage)


def _write(path, fields, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def load(out: Path = OUT) -> nx.MultiDiGraph:
    """Load the CSVs into a MultiDiGraph (parallel edges = several sources for one relation)."""
    g = nx.MultiDiGraph()
    with open(out / "nodes.csv", encoding="utf-8") as f:
        for n in csv.DictReader(f):
            g.add_node(n["id"], type=n["type"], name=n["name"], synonyms=n["synonyms"],
                       attrs=json.loads(n["attrs"]))
    with open(out / "edges.csv", encoding="utf-8") as f:
        for e in csv.DictReader(f):
            e["confidence"] = float(e["confidence"])
            g.add_edge(e["src"], e["dst"], key=e["edge_id"], **e)
    return g
