"""Precompute static exports for the UI and Supabase (docs/contract.md §5).

Run: `uv run python -m atlas.export [--no-llm]`

  exports/json/subgraph/<id>.json   subgraph(id, focus="all")
  exports/json/journey/<id>.json    recommend(id)  (journey() with cards [] on LLM failure or --no-llm)
  exports/json/email/<id>.json      draft_email() to the best organisation for the disease
  exports/json/index.json           slice name, counts, sources, diseases, clusters
  exports/json/export_report.json   what fell back / was skipped, and why
  exports/examples/*.json           recommend(Tay-Sachs, IN), its email, one chat answer (same code path)
  exports/supabase/schema.sql + <table>.csv   Postgres tables, loadable with \\copy (scripts/push_supabase.py)

LLM calls go through atlas.llm, so they are cached in data/llm_cache/ and reruns are free.
"""
import argparse
import csv
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import api, llm
from .graph import EDGE_FIELDS, OUT

ROOT = Path(__file__).resolve().parent.parent
EXPORTS = ROOT / "exports"
JSON_DIR = EXPORTS / "json"
SUPA_DIR = EXPORTS / "supabase"
CONCURRENCY = 5


def fname(node_id: str) -> str:
    return node_id.replace(":", "_") + ".json"


def sender_for(short: str) -> dict:
    return {"name": "Maria", "role": "patient group leader",
            "context": f"Parent leading a patient group for {short}"}


def _dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------------------------
# per-disease pieces
# ---------------------------------------------------------------------------------------------
def diseases() -> list[str]:
    st = api._state()
    g = st["g"]
    order = {d["omim"]: i for i, d in enumerate(_slice()["diseases"])}
    ids = [n for n, d in g.nodes(data=True) if d["type"] == "disease"]
    return sorted(ids, key=lambda n: (order.get((g.nodes[n].get("attrs") or {}).get("omim"), 999), n))


def _slice() -> dict:
    return yaml.safe_load((ROOT / "config" / "slice.yaml").read_text(encoding="utf-8"))


def make_journey(disease_id: str, use_llm: bool) -> tuple[dict, str | None]:
    """recommend(), falling back to journey() with cards [] (returns (data, error_or_None))."""
    if not use_llm:
        return api.journey(disease_id), "skipped: --no-llm"
    try:
        j = api.recommend(disease_id)
    except llm.LLMError as e:  # recommend catches this itself; kept as a safety net
        j, err = api.journey(disease_id), str(e)
        j["generated_by"] = {**j["generated_by"], "error": err}
        return j, err
    err = j["generated_by"].get("error")
    if err:  # recommend fell back internally
        return j, err
    return j, None


def best_org(j: dict) -> tuple[dict | None, str]:
    """The organisation to email for this journey, and why (same rule as api.draft_email; contract §3.4)."""
    return api.choose_recipient(j)


def make_email(j: dict, use_llm: bool) -> tuple[dict | None, dict]:
    d = j["disease"]
    org, why = best_org(j)
    note = {"org_id": org["id"] if org else None, "why": why}
    if org is None:
        return None, note
    if not use_llm:
        note["skipped"] = "--no-llm"
        return None, note
    e = api.draft_email(org["id"], d["id"], sender_for(d["short"]))
    if e.get("body") is None:
        note["error"] = next((w for w in e["warnings"] if w.startswith("LLM failed")), "LLM failed")
    return e, note


EXAMPLE_DISEASE = "MONDO:0010100"  # Tay-Sachs
EXAMPLE_COUNTRY = "IN"
EXAMPLE_QUESTION = "Is there a trial my child could join outside the US?"


def write_examples() -> dict:
    """Regenerate exports/examples/* through the same code path as exports/json (never hand-edited)."""
    out = EXPORTS / "examples"
    j = api.recommend(EXAMPLE_DISEASE, EXAMPLE_COUNTRY)
    _dump(out / "recommend_tay_sachs.json", j)
    e, note = make_email(j, True)
    if e is not None:
        _dump(out / "email_example.json", e)
    req = {"disease_id": EXAMPLE_DISEASE, "messages": [{"role": "user", "content": EXAMPLE_QUESTION}]}
    _dump(out / "chat_example.json", {"request": req, "response": api.chat(**req)})
    _log(f"examples written ({len(j['cards'])} cards, email to {note.get('org_id')})")
    return {"cards": len(j["cards"]), "email": note}


# ---------------------------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------------------------
def _clusters() -> list:
    p = OUT / "clusters.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def counts(st) -> dict:
    g = st["g"]
    types = Counter(d["type"] for _, d in g.nodes(data=True))
    ev = Counter(d[2].get("evidence") or "unknown" for d in st["edges"].values())
    return {"nodes": g.number_of_nodes(),
            "edges": {"total": len(st["edges"]), **dict(sorted(ev.items()))},
            "papers": types["paper"], "organizations": types["organization"],
            "countries": types["country"], "trials": types["trial"], "diseases": types["disease"]}


def sources(st) -> list[dict]:
    agg: dict[str, dict] = {}
    for r in st["coverage"]:
        a = agg.setdefault(r["source"], {"name": r["source"], "records_available": 0, "records_kept": 0})
        n, k = int(float(r["n_results"] or 0)), int(float(r["n_kept"] or 0))
        a["records_available"] += max(n, 0)  # -1 = not searched (placeholder row)
        a["records_kept"] += max(k, 0)
    return list(agg.values())


def build_index(st, ids, email_files) -> dict:
    g = st["g"]
    cl = {d: c["cluster"] for c in _clusters() for d in c["diseases"]}
    rows = []
    for d in ids:
        a = g.nodes[d].get("attrs") or {}
        rows.append({"id": d, "name": g.nodes[d]["name"], "short": a.get("short") or g.nodes[d]["name"],
                     "group": a.get("group"), "role": a.get("role"), "cluster_id": cl.get(d),
                     "files": {"journey": f"journey/{fname(d)}", "subgraph": f"subgraph/{fname(d)}",
                               "email": f"email/{fname(d)}" if d in email_files else None}})
    return {"name": _slice().get("name"),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "counts": counts(st), "sources": sources(st), "diseases": rows, "clusters": _clusters()}


# ---------------------------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------------------------
# (table, [(column, sql type)], primary key)
TABLES = [
    ("nodes", [("id", "text"), ("type", "text not null"), ("name", "text not null"),
               ("synonyms", "text[]"), ("attrs", "jsonb")], "id"),
    ("edges", [("id", "text"), ("source", "text not null"), ("target", "text not null"),
               ("relation", "text not null"), ("evidence", "text"), ("confidence", "numeric"),
               ("polarity", "text"), ("effect", "text"), ("source_name", "text"), ("source_url", "text"),
               ("quote", "text"), ("context", "text"), ("retrieved_at", "date"), ("submitted_by", "text"),
               ("frequency", "text")], "id"),
    ("coverage", [("disease_id", "text"), ("source", "text"), ("query", "text"), ("n_results", "integer"),
                  ("n_kept", "integer"), ("retrieved_at", "date")], "disease_id, source, query"),
    ("similarity", [("a", "text"), ("b", "text"), ("therapeutic", "numeric"), ("phenotype_view", "numeric"),
                    ("gene", "numeric"), ("pathway", "numeric"), ("phenotype", "numeric"),
                    ("lookalike", "boolean"), ("top_witnesses", "text[]")], "a, b"),
    ("clusters", [("cluster_id", "integer"), ("disease_id", "text")], "cluster_id, disease_id"),
    ("journeys", [("disease_id", "text"), ("data", "jsonb not null")], "disease_id"),
    ("subgraphs", [("disease_id", "text"), ("data", "jsonb not null")], "disease_id"),
    ("emails", [("disease_id", "text"), ("data", "jsonb not null")], "disease_id"),
]
INDEXES = [("edges", "source"), ("edges", "target"), ("edges", "relation"), ("nodes", "type")]


def schema_sql() -> str:
    out = ["-- Rare Disease Atlas: Postgres / Supabase schema (generated by atlas.export; idempotent).",
           "-- Load the matching CSVs with: \\copy <table> from '<table>.csv' with (format csv, header true)",
           ""]
    for t, cols, pk in TABLES:
        body = ",\n".join(f"  {c} {typ}" for c, typ in cols)
        out.append(f"create table if not exists public.{t} (\n{body},\n  primary key ({pk})\n);")
    out.append("")
    for t, c in INDEXES:
        out.append(f"create index if not exists {t}_{c}_idx on public.{t} ({c});")
    out.append("")
    out.append("-- Read-only public access: RLS on, one SELECT policy for anon + authenticated, no write policies.")
    for t, _, _ in TABLES:
        out += [f"alter table public.{t} enable row level security;",
                f"drop policy if exists {t}_read on public.{t};",
                f"create policy {t}_read on public.{t} for select to anon, authenticated using (true);",
                f"grant select on public.{t} to anon, authenticated;"]
    return "\n".join(out) + "\n"


def pg_array(items) -> str:
    """Postgres array literal: {"a","b \\"c\\""}."""
    def q(s):
        return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'
    return "{" + ",".join(q(s) for s in items) + "}"


def _write_csv(path: Path, header: list[str], rows) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            # None -> unquoted empty cell = NULL under COPY ... CSV
            w.writerow(["" if v is None else v for v in r])
            n += 1
    return n


def _nn(v):
    return None if v in ("", None) else v


def write_supabase(st, journeys: dict, subgraphs: dict, emails: dict) -> dict:
    SUPA_DIR.mkdir(parents=True, exist_ok=True)
    (SUPA_DIR / "schema.sql").write_text(schema_sql(), encoding="utf-8")
    cols = {t: [c for c, _ in cs] for t, cs, _ in TABLES}
    g = st["g"]
    n = {}
    n["nodes"] = _write_csv(SUPA_DIR / "nodes.csv", cols["nodes"], (
        [nid, d["type"], d["name"], pg_array([s for s in (d.get("synonyms") or "").split("|") if s]),
         json.dumps(d.get("attrs") or {}, ensure_ascii=False)] for nid, d in g.nodes(data=True)))
    with open(OUT / "edges.csv", encoding="utf-8") as f:
        raw = list(csv.DictReader(f))
    assert set(EDGE_FIELDS) <= set(raw[0]) if raw else True
    n["edges"] = _write_csv(SUPA_DIR / "edges.csv", cols["edges"], (
        [e["edge_id"], e["src"], e["dst"], e["relation"], _nn(e["evidence"]), _nn(e["confidence"]),
         _nn(e["polarity"]) or "supports", _nn(e["effect"]), _nn(e["source"]), _nn(e["source_url"]),
         _nn(e["quote"]), _nn(e["context"]), _nn(e["retrieved_at"]), _nn(e["submitted_by"]),
         _nn(e["frequency"])] for e in raw))
    n["coverage"] = _write_csv(SUPA_DIR / "coverage.csv", cols["coverage"], (
        [r["disease_id"], r["source"], r["query"], _nn(r["n_results"]), _nn(r["n_kept"]),
         _nn(r["retrieved_at"])] for r in st["coverage"]))
    sim = []
    if (OUT / "similarity.csv").exists():
        with open(OUT / "similarity.csv", encoding="utf-8") as f:
            sim = list(csv.DictReader(f))
    n["similarity"] = _write_csv(SUPA_DIR / "similarity.csv", cols["similarity"], (
        [r["a"], r["b"], r["therapeutic"], r["phenotype_view"], r["gene"], r["pathway"], r["phenotype"],
         "true" if r["lookalike"].lower() == "true" else "false",
         pg_array([w.strip() for w in r["top_witnesses"].split(" | ") if w.strip()])] for r in sim))
    n["clusters"] = _write_csv(SUPA_DIR / "clusters.csv", cols["clusters"], (
        [c["cluster"], d] for c in _clusters() for d in c["diseases"]))
    for t, data in (("journeys", journeys), ("subgraphs", subgraphs), ("emails", emails)):
        n[t] = _write_csv(SUPA_DIR / f"{t}.csv", cols[t], (
            [d, json.dumps(v, ensure_ascii=False)] for d, v in data.items()))
    return n


# ---------------------------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------------------------
def run(use_llm: bool = True) -> dict:
    t0 = time.time()
    st = api._state()
    api._pairs(st)  # warm shared caches before threads start
    ids = diseases()
    _log(f"{len(ids)} diseases, llm={'on (' + llm.backend() + ')' if use_llm else 'off'}")

    subgraphs = {}
    for d in ids:
        subgraphs[d] = api.subgraph(d, focus="all")
        _dump(JSON_DIR / "subgraph" / fname(d), subgraphs[d])
    _log(f"subgraphs written ({len(subgraphs)})")

    report = {"journeys": {}, "emails": {}}
    journeys: dict[str, dict] = {}

    def do_journey(d):
        j, err = make_journey(d, use_llm)
        _dump(JSON_DIR / "journey" / fname(d), j)
        _log(f"journey {d} {j['disease']['short']}: {len(j['cards'])} cards" + (f" (fallback: {err})" if err else ""))
        return d, j, err

    with ThreadPoolExecutor(CONCURRENCY) as ex:
        for d, j, err in ex.map(do_journey, ids):
            journeys[d] = j
            report["journeys"][d] = {"cards": len(j["cards"]), "fallback": err}

    emails: dict[str, dict] = {}

    def do_email(d):
        e, note = make_email(journeys[d], use_llm)
        if e is not None:
            _dump(JSON_DIR / "email" / fname(d), e)
        _log(f"email {d}: " + (f"to={e['to']!r} org={e['org_id']}" if e else f"skipped ({note['why']})"))
        return d, e, note

    stale = JSON_DIR / "email"
    if stale.exists():
        for p in stale.glob("*.json"):
            p.unlink()
    with ThreadPoolExecutor(CONCURRENCY) as ex:
        for d, e, note in ex.map(do_email, ids):
            if e is not None:
                emails[d] = e
            report["emails"][d] = note

    if use_llm:
        report["examples"] = write_examples()

    index = build_index(st, ids, emails)
    _dump(JSON_DIR / "index.json", index)
    rows = write_supabase(st, journeys, subgraphs, emails)
    report.update({"generated_at": index["generated_at"], "llm": use_llm, "supabase_rows": rows,
                   "seconds": round(time.time() - t0, 1)})
    _dump(JSON_DIR / "export_report.json", report)
    n_cards = sum(1 for r in report["journeys"].values() if r["cards"])
    _log(f"done in {report['seconds']}s: {n_cards}/{len(ids)} journeys with cards, "
         f"{len(emails)} emails ({sum(1 for e in emails.values() if e.get('to'))} with a 'to'), "
         f"supabase rows {rows}")
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-llm", action="store_true", help="deterministic journeys only; no emails")
    ap.add_argument("--examples-only", action="store_true", help="only regenerate exports/examples/*")
    args = ap.parse_args(argv)
    if args.examples_only:
        write_examples()
        return
    run(use_llm=not args.no_llm)


if __name__ == "__main__":
    sys.exit(main())
