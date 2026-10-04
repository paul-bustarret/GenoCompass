"""Offline checks on the files produced by `python -m atlas.export` (exports/json, exports/supabase)."""
import csv
import json
import re
import sys
from pathlib import Path

import pytest

from atlas.export import TABLES, pg_array, schema_sql

ROOT = Path(__file__).resolve().parent.parent
JSON_DIR = ROOT / "exports" / "json"
SUPA = ROOT / "exports" / "supabase"
JOURNEY_KEYS = {"status", "disease", "user_country", "profile", "similar", "lookalikes", "organizations",
                "trials", "assets", "researchers", "next_steps", "cards", "searched", "missing",
                "generated_by"}
CITE = re.compile(r"\[([^\[\]]*)\]")
EID = re.compile(r"\bE[0-9A-Za-z]{4,}\b")
csv.field_size_limit(sys.maxsize)  # subgraph/journey jsonb cells are large

pytestmark = pytest.mark.skipif(not (JSON_DIR / "index.json").exists(),
                                reason="run `uv run python -m atlas.export` first")


@pytest.fixture(scope="module")
def index():
    return json.loads((JSON_DIR / "index.json").read_text())


@pytest.fixture(scope="module")
def edge_ids():
    with open(ROOT / "data" / "graph" / "edges.csv") as f:
        return {r["edge_id"] for r in csv.DictReader(f)}


def test_index_lists_25_diseases_and_files_exist(index):
    assert len(index["diseases"]) == 25
    assert index["counts"]["diseases"] == 25
    assert index["name"]
    for d in index["diseases"]:
        assert {"id", "name", "short", "group", "role", "cluster_id", "files"} <= set(d)
        for kind in ("journey", "subgraph"):
            assert (JSON_DIR / d["files"][kind]).is_file(), d["files"][kind]
        if d["files"]["email"] is not None:
            assert (JSON_DIR / d["files"]["email"]).is_file()
        assert ":" not in d["files"]["journey"]


def test_index_counts_and_sources(index):
    c = index["counts"]
    assert c["edges"]["total"] == sum(v for k, v in c["edges"].items() if k != "total")
    assert all({"name", "records_available", "records_kept"} == set(s) for s in index["sources"])
    assert {d for cl in index["clusters"] for d in cl["diseases"]} == {d["id"] for d in index["diseases"]}


def test_journeys_have_contract_keys_and_valid_citations(index, edge_ids):
    for d in index["diseases"]:
        j = json.loads((JSON_DIR / d["files"]["journey"]).read_text())
        assert JOURNEY_KEYS <= set(j), d["id"]
        assert j["disease"]["id"] == d["id"]
        for card in j["cards"]:
            assert card["key"] in {"whats_going_on", "similar", "what_exists", "next_step"}
            cited = [e for m in CITE.findall(card["text"]) for e in EID.findall(m)]
            # uncited sentences are removed in code, which can leave a card empty
            if "status" in card:  # exports written before card status existed may still hold "" cards
                assert card["text"].strip(), f"empty card text in {d['id']}"
                assert cited or card["status"] == "no_supported_evidence", d["id"]
            else:
                assert cited or not card["text"].strip(), d["id"]
            assert set(cited) <= edge_ids, set(cited) - edge_ids
            assert set(card["citations"]) <= edge_ids


def test_subgraphs_reference_real_edges(index, edge_ids):
    for d in index["diseases"]:
        sg = json.loads((JSON_DIR / d["files"]["subgraph"]).read_text())
        assert sg["anchor"] == d["id"] and sg["focus"] == "all"
        nodes = {n["id"] for n in sg["nodes"]}
        for e in sg["edges"]:
            assert e["id"] in edge_ids and e["source"] in nodes and e["target"] in nodes


def test_emails_are_drafts_with_valid_citations(index, edge_ids):
    for d in index["diseases"]:
        if d["files"]["email"] is None:
            continue
        e = json.loads((JSON_DIR / d["files"]["email"]).read_text())
        assert e["disease_id"] == d["id"] and e["requires_human_review"] is True
        assert set(e["citations"]) <= edge_ids


def _create_tables(sql: str) -> dict[str, list[str]]:
    out = {}
    for name, body in re.findall(r"create table if not exists public\.(\w+) \((.*?)\n\);", sql, re.S):
        cols = [line.strip().rstrip(",").split()[0] for line in body.strip().splitlines()]
        out[name] = [c for c in cols if c != "primary"]
    return out


def test_supabase_csvs_match_schema():
    sql = (SUPA / "schema.sql").read_text()
    assert sql == schema_sql()
    tables = _create_tables(sql)
    assert set(tables) == {t for t, _, _ in TABLES}
    for t in tables:
        assert f"alter table public.{t} enable row level security" in sql and f"public.{t} for select to anon, authenticated" in sql
    for t, cols in tables.items():
        with open(SUPA / f"{t}.csv", newline="") as f:
            rows = list(csv.reader(f))
        assert rows[0] == cols, t
        assert all(len(r) == len(cols) for r in rows[1:]), t
    with open(SUPA / "journeys.csv", newline="") as f:
        for r in csv.DictReader(f):
            json.loads(r["data"])


def test_pg_array_literal():
    assert pg_array([]) == "{}"
    assert pg_array(['a "b"', "c\\d", "e,f"]) == '{"a \\"b\\"","c\\\\d","e,f"}'


def test_write_examples_uses_api(monkeypatch, tmp_path):
    from atlas import api, export
    monkeypatch.setattr(export, "EXPORTS", tmp_path)
    j = {"disease": {"id": "MONDO:0010100", "short": "Tay-Sachs"}, "cards": [], "organizations": []}
    calls = {}
    monkeypatch.setattr(api, "recommend", lambda d, c=None: calls.setdefault("rec", (d, c)) and j)
    monkeypatch.setattr(api, "choose_recipient", lambda jj: ({"id": "ORG:x"}, "why"))
    monkeypatch.setattr(api, "draft_email", lambda o, d, s: {"org_id": o, "body": "b", "warnings": []})
    monkeypatch.setattr(api, "chat", lambda disease_id, messages: {"answer": "a"})
    export.write_examples()
    assert calls["rec"] == ("MONDO:0010100", "IN")
    assert json.loads((tmp_path / "examples" / "email_example.json").read_text())["org_id"] == "ORG:x"
    chat = json.loads((tmp_path / "examples" / "chat_example.json").read_text())
    assert chat["request"]["disease_id"] == "MONDO:0010100" and chat["response"] == {"answer": "a"}
    assert (tmp_path / "examples" / "recommend_tay_sachs.json").is_file()
