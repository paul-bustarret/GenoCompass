"""Offline checks on the slice (25 diseases, display groups) and trial site countries."""
import csv
import json
import re
from collections import defaultdict
from pathlib import Path

import pytest
import yaml

from atlas.graph import OUT

GROUPS = {"Mucopolysaccharidoses", "Glycogen & autophagy (Pompe, Danon)", "Sphingolipidoses",
          "GM2 & GM1 gangliosidoses", "Niemann-Pick", "Neuronal ceroid lipofuscinoses", "Bridge",
          "Lookalike control"}


@pytest.fixture(scope="module")
def nodes():
    with open(OUT / "nodes.csv") as f:
        return [{**n, "attrs": json.loads(n["attrs"])} for n in csv.DictReader(f)]


@pytest.fixture(scope="module")
def edges():
    with open(OUT / "edges.csv") as f:
        return list(csv.DictReader(f))


def test_slice_config_has_name_and_groups():
    cfg = yaml.safe_load(open(Path(__file__).resolve().parent.parent / "config" / "slice.yaml"))
    assert cfg["name"] == "Lysosomal storage diseases (20) + controls (5)"
    assert len(cfg["diseases"]) == 25
    assert all(d["group"] in GROUPS for d in cfg["diseases"])


def test_25_diseases(nodes):
    assert sum(n["type"] == "disease" for n in nodes) == 25


def test_every_disease_has_group(nodes):
    for n in nodes:
        if n["type"] == "disease":
            assert n["attrs"].get("group") in GROUPS, n["id"]


def test_trial_countries_match_site_edges(nodes, edges):
    sites = defaultdict(set)
    for e in edges:
        if e["relation"] == "has_site_in":
            assert e["source"] == "ctgov" and e["source_url"] == f"https://clinicaltrials.gov/study/{e['src']}"
            sites[e["src"]].add(e["dst"])
    trials = [n for n in nodes if n["type"] == "trial"]
    assert sum(bool(n["attrs"].get("countries")) for n in trials) > len(trials) / 2
    for n in trials:
        countries = n["attrs"].get("countries", [])
        assert countries == sorted(countries), n["id"]
        assert {f"COUNTRY:{c}" for c in countries} == sites.get(n["id"], set()), n["id"]


def test_country_ids_are_iso2(nodes):
    countries = [n for n in nodes if n["type"] == "country"]
    assert countries
    for n in countries:
        assert re.fullmatch(r"COUNTRY:[A-Z]{2}", n["id"]), n["id"]
        assert n["name"]
