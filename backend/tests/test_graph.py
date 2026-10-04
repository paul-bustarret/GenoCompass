"""Offline tests on the committed graph (data/graph/) plus small synthetic checks."""
import csv
import json
from pathlib import Path

import networkx as nx
import pytest

from atlas import similarity as S
from atlas.graph import OUT, load


@pytest.fixture(scope="module")
def g():
    return load()


@pytest.fixture(scope="module")
def comms(g):
    pairs = S.all_pairs(g)
    return S.clusters(pairs, [n for n, d in g.nodes(data=True) if d["type"] == "disease"])


def by_short(g, name):
    return next(n for n, d in g.nodes(data=True) if d["type"] == "disease" and d["attrs"].get("short") == name)


def cluster_of(comms, node):
    return next(c for c in comms if node in c)


def test_every_edge_has_a_source(g):
    for _, _, e in g.edges(data=True):
        assert e["source"] and e["source_url"].startswith("http"), e["edge_id"]


def test_similarity_is_never_stored_as_evidence():
    with open(OUT / "edges.csv") as f:
        assert not {r["relation"] for r in csv.DictReader(f)} & {"similar_to", "SIMILAR_TO"}


def test_sanfilippo_subtypes_cluster_together(g, comms):
    a, b, c = (by_short(g, s) for s in ("MPS IIIA", "MPS IIIB", "MPS IIIC"))
    assert b in cluster_of(comms, a) and c in cluster_of(comms, a)


def test_shared_gene_bridge_is_found(g, comms):
    assert by_short(g, "Parkinson (late-onset)") in cluster_of(comms, by_short(g, "Gaucher"))


@pytest.mark.parametrize("lookalike", ["Rett", "Duchenne", "HCM (MYH7)"])
def test_lookalikes_do_not_join_a_cluster(g, comms, lookalike):
    assert len(cluster_of(comms, by_short(g, lookalike))) == 1


def test_feature_shared_by_all_diseases_has_zero_weight():
    feats = {d: {"gene": set(), "pathway": set(), "phenotype": {"HP:common", f"HP:{d}"}} for d in "abc"}
    w = S.idf(feats)
    assert w["HP:common"] == 0 and w["HP:a"] > 0


def test_coverage_logs_every_disease_and_source(g):
    with open(OUT / "coverage.csv") as f:
        rows = list(csv.DictReader(f))
    diseases = {n for n, d in g.nodes(data=True) if d["type"] == "disease"}
    for src in ("hpo", "ctgov", "reporter", "pubmed", "patient_groups"):
        assert {r["disease_id"] for r in rows if r["source"] == src} == diseases, src


def test_single_paper_mechanism_and_other_do_not_drive_similarity():
    g = nx.MultiDiGraph()
    for d in ("D1", "D2"):
        g.add_node(d, type="disease", name=d, attrs={})
    g.add_node("MECH:x", type="mechanism", name="x", attrs={})
    g.add_node("MECH:other", type="mechanism", name="other", attrs={})
    g.add_edge("D1", "MECH:x", relation="has_mechanism", polarity="supports", source_url="u1")
    g.add_edge("D1", "MECH:x", relation="has_mechanism", polarity="supports", source_url="u2")
    g.add_edge("D2", "MECH:x", relation="has_mechanism", polarity="supports", source_url="u3")
    g.add_edge("D1", "MECH:other", relation="has_mechanism", polarity="supports", source_url="u4")
    g.add_edge("D1", "MECH:other", relation="has_mechanism", polarity="supports", source_url="u5")
    f = S.features(g)
    assert f["D1"]["mechanism"] == {"MECH:x"}   # two papers; MECH:other never counts
    assert f["D2"]["mechanism"] == set()        # one paper only
