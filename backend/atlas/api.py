"""Read side: search, subgraph, journey, recommend, draft_email, chat (docs/contract.md §2-§3).

Everything here only reads data/graph/. The graph is reloaded automatically when its CSVs change,
so nodes/edges added by the write side (mechanisms, patient groups, assets, countries) show up
without code changes; every lookup tolerates their absence.
"""
import csv
import difflib
import json
import math
import re
from pathlib import Path

from . import llm
from . import similarity as S
from .graph import OUT, load

# ---------------------------------------------------------------------------------------------
# Rules (our judgement, not evidence): treatment-readiness checklist.
# A step is "present" for a disease when any pattern matches. Pattern fields:
#   relation   edge relation
#   direction  "in"  = other node -> disease,  "out" = disease -> other node
#   via        optional second hop: (relation, direction) from the matched node to the disease,
#              e.g. organization -runs-> asset -studies-> disease
#   node_type  type of the matched node; attrs = required attr values; name_regex on its name
# ---------------------------------------------------------------------------------------------
READINESS = {
    "mechanism_known": {
        "label": "a described disease mechanism",
        "patterns": [{"relation": "has_mechanism", "direction": "out", "node_type": "mechanism"}],
    },
    "patient_group": {
        "label": "a patient group",
        "patterns": [{"relation": "studies", "direction": "in", "node_type": "organization",
                      "attrs": {"kind": "patient_group"}}],
    },
    "registry": {
        "label": "a patient registry",
        "patterns": [{"relation": "studies", "direction": "in", "node_type": "asset",
                      "attrs": {"kind": "registry"}},
                     {"relation": "studies", "direction": "in", "node_type": "trial",
                      "name_regex": r"\bregistry\b"}],
    },
    "natural_history_study": {
        "label": "a natural history study",
        "patterns": [{"relation": "studies", "direction": "in", "node_type": "asset",
                      "attrs": {"kind": "natural_history_study"}},
                     {"relation": "studies", "direction": "in", "node_type": "trial",
                      "name_regex": r"natural history|disease monitoring"}],
    },
    "trial": {
        "label": "a clinical trial",
        "patterns": [{"relation": "studies", "direction": "in", "node_type": "trial"}],
    },
    "funding": {
        "label": "research funding (an NIH grant)",
        "patterns": [{"relation": "studies", "direction": "in", "node_type": "grant"}],
    },
}

FOCUS_RELATIONS = {
    "similar": {"caused_by", "in_pathway", "part_of", "has_phenotype", "has_mechanism"},
    "assets": {"studies", "runs", "tests", "has_site_in", "based_in", "studied_with"},
    # studies/mentions link the disease to the grants/papers that people and funders hang off.
    "people": {"pi_of", "funds", "authored", "runs", "studies", "mentions"},
}
FOCUS_RELATIONS["all"] = set().union(*FOCUS_RELATIONS.values()) | {"has_variant", "pathogenic_for"}
HUB_TYPES = {"phenotype", "pathway"}
HUB_IDF_FLOOR = 0.5
SEARCH_TYPES = {"disease", "gene", "phenotype", "mechanism", "organization"}
AMBIGUOUS_WITHIN = 0.05
CITE_RE = re.compile(r"\[([^\[\]]*)\]")
EID_RE = re.compile(r"\bE[0-9A-Za-z]{4,}\b")
ABBREV_RE = re.compile(r"^(?:(?:[A-Za-z]\.){2,}|(?:Inc|Ltd|Co|Corp|Dr|Mr|Mrs|Ms|Prof|St|vs|No|Jr|Sr)\.)$")


class NotFound(KeyError):
    """Unknown node id (the server maps this to HTTP 404)."""


# ---------------------------------------------------------------------------------------------
# Graph state (reloaded when the CSVs change)
# ---------------------------------------------------------------------------------------------
_STATE: dict = {}


def _state(out: Path = OUT) -> dict:
    files = [out / f for f in ("nodes.csv", "edges.csv", "coverage.csv")]
    stamp = tuple(f.stat().st_mtime_ns if f.exists() else 0 for f in files)
    if _STATE.get("stamp") == stamp and _STATE.get("out") == out:
        return _STATE
    g = load(out)
    feats = S.features(g)
    st = {"stamp": stamp, "out": out, "g": g, "feats": feats, "idf": S.idf(feats),
          "edges": {k: (u, v, d) for u, v, k, d in g.edges(keys=True, data=True)},
          "coverage": []}
    if (out / "coverage.csv").exists():
        with open(out / "coverage.csv") as f:
            st["coverage"] = list(csv.DictReader(f))
    st["pairs"] = None  # computed lazily
    st["names"] = _name_index(g)
    _STATE.clear()
    _STATE.update(st)
    return _STATE


def _pairs(st) -> dict:
    if st["pairs"] is None:
        st["pairs"] = S.all_pairs(st["g"])
    return st["pairs"]


def _nz(v):
    """'' -> None (contract: nulls, never empty strings)."""
    if isinstance(v, str) and v == "":
        return None
    if isinstance(v, dict):
        return {k: _nz(x) for k, x in v.items()}
    return v


def node_json(g, n) -> dict:
    d = g.nodes[n]
    syn = d.get("synonyms") or ""
    return {"id": n, "type": d["type"], "name": d["name"],
            "synonyms": [s for s in syn.split("|") if s] if isinstance(syn, str) else list(syn),
            "attrs": _nz(dict(d.get("attrs") or {}))}


def edge_json(e: dict) -> dict:
    conf = e.get("confidence")
    try:
        conf = float(conf) if conf not in (None, "") else None
    except ValueError:
        conf = None
    return {"id": e["edge_id"], "source": e["src"], "target": e["dst"], "relation": e["relation"],
            "evidence": _nz(e.get("evidence")), "confidence": conf,
            "polarity": _nz(e.get("polarity")) or "supports", "effect": _nz(e.get("effect")),
            "source_name": _nz(e.get("source")), "source_url": _nz(e.get("source_url")),
            "quote": _nz(e.get("quote")), "frequency": _nz(e.get("frequency")),
            "context": _nz(e.get("context")), "retrieved_at": _nz(e.get("retrieved_at")),
            "submitted_by": _nz(e.get("submitted_by"))}


def _require(st, node_id, type_=None):
    g = st["g"]
    if node_id not in g or (type_ and g.nodes[node_id]["type"] != type_):
        raise NotFound(f"unknown {type_ or 'node'} id: {node_id}")


def _name(g, n) -> str:
    d = g.nodes[n]
    return (d.get("attrs") or {}).get("short") or d["name"]


def _attr(g, n, key):
    return _nz((g.nodes[n].get("attrs") or {}).get(key))


def _out(g, n, rel=None):
    """[(target, edge_data)] for out-edges (supports polarity only for has_mechanism)."""
    return [(v, e) for _, v, e in g.out_edges(n, data=True) if rel is None or e["relation"] == rel]


def _in(g, n, rel=None):
    return [(u, e) for u, _, e in g.in_edges(n, data=True) if rel is None or e["relation"] == rel]


def _supports(e) -> bool:
    return (e.get("polarity") or "supports") == "supports"


def _disease_terms(g, d) -> list[str]:
    """Lower-cased names a quote must contain to count as naming disease d (name, short, synonyms, genes)."""
    nd = g.nodes[d]
    terms = [nd["name"], (nd.get("attrs") or {}).get("short") or ""]
    syn = nd.get("synonyms") or ""
    terms += syn.split("|") if isinstance(syn, str) else list(syn)
    terms += [g.nodes[v]["name"] for v, _ in _out(g, d, "caused_by")]
    return [t.lower() for t in terms if t and len(t) >= 3]


def _weak_grant_edge(g, e) -> bool:
    """A grant `studies` edge whose quote (minus the 'Text search ... matched:' prefix) does not name the
    disease. Such edges are not evidence that the grant works on the disease (defence in depth)."""
    if e.get("relation") != "studies" or e["src"] not in g or g.nodes[e["src"]]["type"] != "grant":
        return False
    if e["dst"] not in g or g.nodes[e["dst"]]["type"] != "disease":
        return False
    q = re.sub(r"^\s*text search .*?matched:\s*", "", e.get("quote") or "", flags=re.I).lower()
    return not any(re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", q)
                   for t in _disease_terms(g, e["dst"]))


def _dedupe(rows: list[dict], key: str = "id") -> list[dict]:
    """Merge rows with the same id, keeping the first and unioning their `edges`."""
    seen: dict = {}
    out = []
    for r in rows:
        k = r.get(key)
        if k in seen:
            if "edges" in r:
                seen[k]["edges"] = list(dict.fromkeys(seen[k].get("edges", []) + r["edges"]))
            continue
        r = dict(r)
        if "edges" in r:
            r["edges"] = list(dict.fromkeys(r["edges"]))
        seen[k] = r
        out.append(r)
    return out


# ---------------------------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------------------------
ROMAN = {"1": "i", "2": "ii", "3": "iii", "4": "iv", "5": "v", "6": "vi", "7": "vii"}


def _norm(s: str) -> str:
    s = re.sub(r"[^0-9a-z]+", " ", s.lower()).strip()
    return " ".join(ROMAN.get(t, t) for t in s.split())


def _name_index(g) -> list[tuple[str, str, str]]:
    rows = []
    for n, d in g.nodes(data=True):
        if d["type"] not in SEARCH_TYPES:
            continue
        attrs = d.get("attrs") or {}
        cands = [("name", d["name"]), ("short", attrs.get("short")), ("label", attrs.get("label"))]
        cands += [("synonym", s) for s in (d.get("synonyms") or "").split("|")]
        seen = set()
        for kind, text in cands:
            t = _norm(text or "")
            if t and t not in seen:
                seen.add(t)
                rows.append((n, kind, t))
    return rows


def _tok_ratio(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if len(a) <= 3 or len(b) <= 3:
        return 0.0
    if b.startswith(a) and len(a) >= 4:
        return 0.9
    return difflib.SequenceMatcher(None, a, b).ratio()


def _score(q: str, qt: list[str], cand: str) -> float:
    if q == cand:
        return 1.0
    ct = cand.split()
    tok = 0.0
    if qt and ct:
        best = [max(_tok_ratio(a, b) for b in ct) for a in qt]
        if min(best) >= 0.8:
            cov = sum(best) / len(qt)
            tok = 0.55 + 0.35 * cov * len(qt) / max(len(ct), len(qt))
    full = difflib.SequenceMatcher(None, q, cand).ratio() ** 2 * 0.85
    return max(tok, full)


def search(text: str, limit: int = 10) -> dict:
    st = _state()
    g = st["g"]
    text = (text or "").strip()
    matches: dict[str, dict] = {}
    if text in g:
        matches[text] = {"score": 1.0, "matched_on": "id"}
    elif text.upper() in g:
        matches[text.upper()] = {"score": 1.0, "matched_on": "id"}
    q = _norm(text)
    qt = q.split()
    if q:
        for n, kind, cand in st["names"]:
            sc = _score(q, qt, cand)
            if sc >= 0.6 and sc > matches.get(n, {}).get("score", 0):
                matches[n] = {"score": sc, "matched_on": kind}
    type_rank = {"disease": 0, "mechanism": 1, "gene": 2, "organization": 3, "phenotype": 4}
    ranked = sorted(matches.items(),
                    key=lambda kv: (-kv[1]["score"], type_rank.get(g.nodes[kv[0]]["type"], 9), kv[0]))
    out = [{"id": n, "type": g.nodes[n]["type"], "name": _name(g, n), "score": round(m["score"], 3),
            "matched_on": m["matched_on"]} for n, m in ranked[:limit]]
    status = "not_found"
    if out:
        top = out[0]["score"]
        close = [m for m in out if m["type"] == "disease" and top - m["score"] <= AMBIGUOUS_WITHIN]
        status = "ambiguous" if len(close) >= 2 else "ok"
    return {"query": text, "status": status, "matches": out}


# ---------------------------------------------------------------------------------------------
# similarity helpers (witnesses with edge ids)
# ---------------------------------------------------------------------------------------------
def _genes(g, d):
    return [(v, e) for v, e in _out(g, d, "caused_by")]


def _feature_edges(g, d, kind, x) -> list[str]:
    """Edge ids that connect disease d to feature node x of the given kind."""
    if kind == "gene":
        return [e["edge_id"] for v, e in _out(g, d, "caused_by") if v == x]
    if kind == "phenotype":
        return [e["edge_id"] for v, e in _out(g, d, "has_phenotype") if v == x]
    if kind == "mechanism":
        return [e["edge_id"] for v, e in _out(g, d, "has_mechanism") if v == x and _supports(e)]
    if kind == "pathway":
        ids = []
        for gene, ce in _genes(g, d):
            for p, pe in _out(g, gene, "in_pathway"):
                if p == x:
                    ids += [ce["edge_id"], pe["edge_id"]]
                else:
                    for q, qe in _out(g, p, "part_of"):
                        if q == x:
                            ids += [ce["edge_id"], pe["edge_id"], qe["edge_id"]]
        return list(dict.fromkeys(ids))
    return []


def _similar_rows(st, disease_id, view="therapeutic") -> list[dict]:
    g = st["g"]
    rows = S.similar(g, disease_id, view=view, k=10 ** 6, pairs=_pairs(st))
    out = []
    for r in rows:
        wits = []
        for kind in ("mechanism", "gene", "pathway", "phenotype"):
            for x in r["witnesses"].get(kind, [])[:3]:
                eids = _feature_edges(g, disease_id, kind, x) + _feature_edges(g, r["id"], kind, x)
                wits.append({"id": x, "name": g.nodes[x]["name"], "kind": kind,
                             "edges": list(dict.fromkeys(eids))})
        comp = {k: r["components"].get(k, 0.0) for k in S.KINDS}
        out.append({"id": r["id"], "name": r["name"], "score": r["score"], "lookalike": r["lookalike"],
                    "components": comp, "witnesses": wits})
    return out


def _similar(st, disease_id, k=5) -> list[dict]:
    rows = _similar_rows(st, disease_id)
    return [r for r in rows if not r["lookalike"] and r["score"] >= S.CLUSTER_MIN][:k]


def _lookalikes(st, disease_id, k=5) -> list[dict]:
    rows = _similar_rows(st, disease_id, view="phenotype")
    return [r for r in rows if r["lookalike"]][:k]


# ---------------------------------------------------------------------------------------------
# subgraph
# ---------------------------------------------------------------------------------------------
def subgraph(disease_id: str, focus: str = "all", k: int = 2, max_nodes: int = 150) -> dict:
    st = _state()
    g = st["g"]
    _require(st, disease_id)
    if focus not in FOCUS_RELATIONS:
        raise ValueError(f"focus must be one of {sorted(FOCUS_RELATIONS)}")
    rels = FOCUS_RELATIONS[focus]
    w = st["idf"]
    n_dis = max(1, len(st["feats"]))
    default_w = math.log(n_dis) if n_dis > 1 else 1.0

    def weight(n):
        return w.get(n, default_w)

    def is_hub(n):
        t = g.nodes[n]["type"]
        return t == "country" or (t in HUB_TYPES and w.get(n, default_w) < HUB_IDF_FLOOR)

    dist = {disease_id: 0}
    frontier = [disease_id]
    for step in range(1, max(0, min(k, 3)) + 1):
        nxt = []
        for n in frontier:
            if n != disease_id and (is_hub(n) or g.nodes[n]["type"] == "disease"):
                continue  # do not expand hubs or other diseases
            nbrs = [v for v, e in _out(g, n) if e["relation"] in rels]
            nbrs += [u for u, e in _in(g, n) if e["relation"] in rels]
            for m in nbrs:
                if m not in dist:
                    dist[m] = step
                    nxt.append(m)
        frontier = nxt

    extra_edges: set[str] = set()
    sim_ids: list[str] = []
    if focus in ("similar", "all"):
        for r in _similar(st, disease_id):
            sim_ids.append(r["id"])
            for wt in r["witnesses"]:
                extra_edges.update(wt["edges"])
    pinned = {disease_id, *sim_ids}
    for eid in extra_edges:
        u, v, _ = st["edges"][eid]
        for x in (u, v):
            if x not in dist:
                dist[x] = 2
    for s in sim_ids:
        dist[s] = min(dist.get(s, 1), 1)

    ranked = sorted(dist, key=lambda n: (n not in pinned, dist[n], -weight(n), n))
    keep = ranked[:max_nodes]
    truncated = len(ranked) > max_nodes
    kept = set(keep)
    edges = []
    for n in keep:
        for v, e in _out(g, n):
            if v in kept and (e["relation"] in rels or e["edge_id"] in extra_edges):
                edges.append(edge_json(e))
    return {"anchor": disease_id, "focus": focus, "k": k,
            "nodes": [node_json(g, n) for n in keep], "edges": edges, "truncated": truncated}


# ---------------------------------------------------------------------------------------------
# journey
# ---------------------------------------------------------------------------------------------
def _match(g, n, pat) -> bool:
    d = g.nodes[n]
    if pat.get("node_type") and d["type"] != pat["node_type"]:
        return False
    attrs = d.get("attrs") or {}
    if any(attrs.get(k) != v for k, v in (pat.get("attrs") or {}).items()):
        return False
    if pat.get("name_regex") and not re.search(pat["name_regex"], d["name"], re.I):
        return False
    return True


def step_hits(g, disease_id, step) -> list[tuple[str, list[str]]]:
    """Nodes satisfying a readiness step for a disease, with the edges that show it."""
    hits = []
    for pat in READINESS[step]["patterns"]:
        if pat.get("node_type") == "asset" and pat["relation"] == "studies":
            for n, eids in _disease_assets(g, disease_id):
                if _match(g, n, pat):
                    hits.append((n, eids))
            continue
        pairs = (_out(g, disease_id, pat["relation"]) if pat["direction"] == "out"
                 else _in(g, disease_id, pat["relation"]))
        for n, e in pairs:
            if _supports(e) and _match(g, n, pat) and not _weak_grant_edge(g, e):
                hits.append((n, [e["edge_id"]]))
    seen, out = set(), []
    for n, eids in hits:
        if n not in seen:
            seen.add(n)
            out.append((n, eids))
    return out


def _disease_assets(g, d) -> list[tuple[str, list[str]]]:
    """Assets for disease d: asset -studies-> d, and org -runs-> asset where org -studies-> d and the asset
    is not linked to any other disease (so a group's registry for another disease is not attributed to d)."""
    out: dict[str, list[str]] = {}
    for a, e in _in(g, d, "studies"):
        if g.nodes[a]["type"] == "asset" and _supports(e):
            out.setdefault(a, []).append(e["edge_id"])
    for o, e in _in(g, d, "studies"):
        if g.nodes[o]["type"] != "organization" or not _supports(e):
            continue
        for a, re_ in _out(g, o, "runs"):
            if g.nodes[a]["type"] != "asset" or a in out:
                continue
            if any(g.nodes[x]["type"] == "disease" and x != d for x, _ in _out(g, a, "studies")):
                continue
            org_only_d = all(x == d for x, _ in _out(g, o, "studies") if g.nodes[x]["type"] == "disease")
            named = any(t in g.nodes[a]["name"].lower() for t in _disease_terms(g, d))
            if not (org_only_d or named):
                continue  # the group works on several diseases; do not guess which one the asset is for
            out[a] = [re_["edge_id"], e["edge_id"]]
    return list(out.items())


def _profile(st, d) -> dict:
    g, w = st["g"], st["idf"]
    genes = [{"id": v, "name": g.nodes[v]["name"], "edges": [e["edge_id"]]} for v, e in _genes(g, d)]
    mechs = [{"id": v, "name": _attr(g, v, "label") or g.nodes[v]["name"], "effect": _nz(e.get("effect")),
              "evidence": e.get("evidence"), "edges": [e["edge_id"]]}
             for v, e in _out(g, d, "has_mechanism") if _supports(e)]
    paths: dict[str, list[str]] = {}
    for gene, ce in _genes(g, d):
        for p, pe in _out(g, gene, "in_pathway"):
            paths.setdefault(p, []).extend([ce["edge_id"], pe["edge_id"]])
            for q, qe in _out(g, p, "part_of"):
                paths.setdefault(q, []).extend([ce["edge_id"], pe["edge_id"], qe["edge_id"]])
    pathways = [{"id": p, "name": g.nodes[p]["name"], "edges": list(dict.fromkeys(e))}
                for p, e in sorted(paths.items(), key=lambda kv: -w.get(kv[0], 0))]
    phen = sorted(_out(g, d, "has_phenotype"), key=lambda ve: (-w.get(ve[0], 0), ve[0]))
    top_ph = [{"id": v, "name": g.nodes[v]["name"], "weight": round(w.get(v, 0), 3),
               "frequency": _nz(e.get("frequency")), "edges": [e["edge_id"]]} for v, e in phen[:10]]
    variants = []
    for var, e in _in(g, d, "pathogenic_for"):
        variants.append({"id": var, "name": g.nodes[var]["name"],
                         "significance": _attr(g, var, "significance"),
                         "consequence": _attr(g, var, "consequence"), "edges": [e["edge_id"]]})
    # dedupe mechanisms
    mseen, mout = {}, []
    for m in mechs:
        if m["id"] in mseen:
            mseen[m["id"]]["edges"] += m["edges"]
        else:
            mseen[m["id"]] = m
            mout.append(m)
    return {"genes": _dedupe(genes), "mechanisms": _dedupe(mout), "pathways": _dedupe(pathways),
            "top_phenotypes": _dedupe(top_ph), "variants": _dedupe(variants)[:10]}


def _countries(g, trial) -> tuple[list[str], list[str]]:
    cs = _attr(g, trial, "countries") or []
    if isinstance(cs, str):
        cs = [c for c in re.split(r"[|,;]\s*", cs) if c]
    cs = [c.upper() for c in cs]
    eids = []
    for c, e in _out(g, trial, "has_site_in"):
        code = c.split(":", 1)[-1].upper()
        eids.append(e["edge_id"])
        if code not in cs:
            cs.append(code)
    return sorted(set(cs)), eids


def _org_country(g, org):
    c = _attr(g, org, "country")
    if c:
        return c.upper(), []
    for v, e in _out(g, org, "based_in"):
        return v.split(":", 1)[-1].upper(), [e["edge_id"]]
    return None, []


def _orgs_for(g, d) -> list[tuple[str, list[str]]]:
    """Organisations linked to a disease, directly or via a trial, grant or asset."""
    hits = []
    for o, e in _in(g, d, "studies"):
        if g.nodes[o]["type"] == "organization":
            hits.append((o, [e["edge_id"]]))
    for x, e in _in(g, d, "studies"):
        t = g.nodes[x]["type"]
        if t in ("trial", "asset"):
            hits += [(o, [e2["edge_id"], e["edge_id"]]) for o, e2 in _in(g, x, "runs")
                     if g.nodes[o]["type"] == "organization"]
        elif t == "grant" and not _weak_grant_edge(g, e):
            hits += [(o, [e2["edge_id"], e["edge_id"]]) for o, e2 in _in(g, x, "funds")
                     if g.nodes[o]["type"] == "organization"]
    merged: dict[str, list[str]] = {}
    for o, eids in hits:
        merged.setdefault(o, []).extend(eids)
    return [(o, list(dict.fromkeys(e))[:6]) for o, e in merged.items()]


KIND_ORDER = {"patient_group": 0, "registry_host": 1, "academic": 2, "funder": 3, "sponsor": 4,
              "company": 5, "industry": 5}


def _organizations(st, diseases, country) -> list[dict]:
    g = st["g"]
    out, seen = [], set()
    for i, d in enumerate(diseases):
        rows = []
        for o, eids in _orgs_for(g, d):
            if o in seen:
                continue
            c, ce = _org_country(g, o)
            rows.append({"id": o, "name": g.nodes[o]["name"], "kind": _attr(g, o, "kind"), "country": c,
                         "website": _attr(g, o, "website"), "contact_email": _attr(g, o, "contact_email"),
                         "for_disease": d, "edges": eids + ce})
        rows.sort(key=lambda r: (KIND_ORDER.get(r["kind"], 9), -len(r["edges"]), r["name"]))
        if i > 0:
            rows = rows[:6]
        for r in rows:
            seen.add(r["id"])
        out += rows
    return _dedupe(out)


def _trials(st, diseases, country) -> list[dict]:
    g = st["g"]
    out, seen = [], set()
    status_rank = {"RECRUITING": 0, "NOT_YET_RECRUITING": 1, "ENROLLING_BY_INVITATION": 2,
                   "ACTIVE_NOT_RECRUITING": 3}
    for i, d in enumerate(diseases):
        rows = []
        for t, e in _in(g, d, "studies"):
            if g.nodes[t]["type"] != "trial" or t in seen:
                continue
            cs, ce = _countries(g, t)
            ivs = _out(g, t, "tests")
            rows.append({"id": t, "title": g.nodes[t]["name"], "status": _attr(g, t, "status"),
                         "phase": _attr(g, t, "phase"), "for_disease": d, "countries": cs,
                         "near_user": bool(country) and country.upper() in cs,
                         "interventions": [g.nodes[v]["name"] for v, _ in ivs],
                         "edges": [e["edge_id"]] + [x["edge_id"] for _, x in ivs] + ce[:5]})
        rows.sort(key=lambda r: (status_rank.get(r["status"] or "", 9), not r["near_user"], r["id"]))
        if i > 0:
            rows = rows[:5]
        for r in rows:
            seen.add(r["id"])
        out += rows
    return _dedupe(out)


def _assets(st, diseases) -> list[dict]:
    g = st["g"]
    out, seen = [], set()
    for d in diseases:
        for a, link in _disease_assets(g, d):
            if a in seen:
                continue
            seen.add(a)
            eids = list(dict.fromkeys(link + [x["edge_id"] for _, x in _in(g, a, "runs")][:2]))
            out.append({"id": a, "name": g.nodes[a]["name"], "kind": _attr(g, a, "kind"),
                        "website": _attr(g, a, "website"), "for_disease": d, "edges": eids})
    return out


def _researchers(st, diseases, limit=15) -> list[dict]:
    g = st["g"]
    people: dict[str, dict] = {}
    for d in diseases:
        for x, e in _in(g, d, "studies"):
            if g.nodes[x]["type"] != "grant" or _weak_grant_edge(g, e):
                continue
            for p, pe in _in(g, x, "pi_of"):
                r = people.setdefault(p, {"grants": [], "diseases": [], "edges": []})
                r["grants"].append(x)
                r["diseases"].append(d)
                r["edges"] += [pe["edge_id"], e["edge_id"]]
        for x, e in _in(g, d, "mentions"):
            for p, pe in _in(g, x, "authored"):
                r = people.setdefault(p, {"grants": [], "diseases": [], "edges": []})
                r["diseases"].append(d)
                r["edges"] += [pe["edge_id"], e["edge_id"]]
    out = []
    for p, r in people.items():
        ds = list(dict.fromkeys(r["diseases"]))
        out.append({"id": p, "name": g.nodes[p]["name"], "grants": list(dict.fromkeys(r["grants"])),
                    "diseases": ds, "edges": list(dict.fromkeys(r["edges"]))[:8]})
    first = diseases[0] if diseases else None
    out.sort(key=lambda r: (-len(r["diseases"]), first not in r["diseases"], -len(r["grants"]), r["name"]))
    return out[:limit]


CONTACT_STEP = {"patient_group": "patient_group", "registry_host": "registry", "funder": "funding",
                "sponsor": "trial", "industry": "trial", "company": "trial", "academic": "natural_history_study"}


def _next_steps(st, d, similar, orgs, researchers, country, assets=()) -> list[dict]:
    """Ordered: the disease's OWN community first (contact its patient groups, join its registries /
    natural history studies), then reuse from similar diseases, then contacts from similar diseases,
    then what would have to be built."""
    g = st["g"]
    dname = _name(g, d)
    own, reuse, build = [], [], []
    for o in orgs:
        if o["for_disease"] == d and o["kind"] == "patient_group":
            where = f", based in {o['country']}" if o["country"] else ""
            own.append({"kind": "contact", "step": "patient_group",
                        "text": f"Contact {o['name']} (patient group{where}), which the atlas links to {dname}.",
                        "from_disease": d, "target_id": o["id"], "edges": o["edges"]})
    for a in assets:
        if a["for_disease"] == d and a["kind"] in ("registry", "natural_history_study"):
            label = "registry" if a["kind"] == "registry" else "natural history study"
            own.append({"kind": "join", "step": "registry" if a["kind"] == "registry" else "natural_history_study",
                        "text": f"Ask about joining the {label} {a['name']}, which the atlas links to {dname}.",
                        "from_disease": d, "target_id": a["id"], "edges": a["edges"]})
    for step, spec in READINESS.items():
        if step_hits(g, d, step):
            continue
        donor = None
        for s in similar:
            hits = step_hits(g, s["id"], step)
            if hits:
                donor = (s, hits[0])
                break
        if donor:
            s, (node, eids) = donor
            reuse.append({"kind": "reuse", "step": step,
                          "text": f"{s['name']} already has {spec['label']} ({g.nodes[node]['name']}); "
                                  f"{dname} does not yet. Ask whether it could include or be copied for {dname}.",
                          "from_disease": s["id"], "target_id": node, "edges": eids})
        else:
            build.append({"kind": "build", "step": step,
                          "text": f"No disease similar to {dname} in the atlas has {spec['label']} yet; "
                                  f"this would have to be built.",
                          "from_disease": None, "target_id": None, "edges": []})
    contacts = [o for o in orgs if o["for_disease"] != d]
    if country:
        contacts.sort(key=lambda o: (o["country"] is None or o["country"] != country.upper(),
                                     KIND_ORDER.get(o["kind"], 9)))
    else:
        contacts.sort(key=lambda o: KIND_ORDER.get(o["kind"], 9))
    names = {s["id"]: s["name"] for s in similar}
    other = []
    for o in contacts[:3]:
        where = f", based in {o['country']}" if o["country"] else ""
        other.append({"kind": "contact", "step": CONTACT_STEP.get(o["kind"], "trial"),
                      "text": f"Contact {o['name']} ({o['kind'] or 'organisation'}{where}), which the atlas links to "
                              f"{names.get(o['for_disease'], o['for_disease'])}, a related disease.",
                      "from_disease": o["for_disease"], "target_id": o["id"], "edges": o["edges"]})
    if len(contacts) < 3:
        for r in [r for r in researchers if any(x != d for x in r["diseases"])][:3 - len(contacts)]:
            oth = next(x for x in r["diseases"] if x != d)
            other.append({"kind": "contact", "step": "funding",
                          "text": f"Contact researcher {r['name']}, whom the atlas links to {names.get(oth, oth)}.",
                          "from_disease": oth, "target_id": r["id"], "edges": r["edges"]})
    groups = [x for x in own if x["kind"] == "contact"]
    joins = [x for x in own if x["kind"] == "join"]
    own = groups[:2] + joins[:3] + groups[2:4]
    return _dedupe(own, "target_id") + reuse + other + build


def _searched(st, d):
    rows = [r for r in st["coverage"] if r.get("disease_id") == d]
    searched = []
    for r in rows:
        def num(x):
            try:
                return int(float(x))
            except (TypeError, ValueError):
                return None
        searched.append({"source": r["source"], "query": _nz(r.get("query")),
                         "n_results": num(r.get("n_results")), "n_kept": num(r.get("n_kept"))})
    missing = []
    all_sources = list(dict.fromkeys(r["source"] for r in st["coverage"]))
    for src in all_sources:
        done = [r["n_results"] for r in searched
                if r["source"] == src and r["n_results"] is not None and r["n_results"] >= 0]
        if not done:
            missing.append(f"{src}: not searched yet")
        elif not any(n > 0 for n in done):
            missing.append(f"{src}: searched, none found")
    return searched, missing


def journey(disease_id: str, country: str | None = None) -> dict:
    st = _state()
    g = st["g"]
    _require(st, disease_id, "disease")
    country = country.upper() if country else None
    similar = _similar(st, disease_id)
    diseases = [disease_id] + [s["id"] for s in similar]
    orgs = _organizations(st, diseases, country)
    trials = _trials(st, diseases, country)
    assets = _assets(st, diseases)
    researchers = _dedupe(_researchers(st, diseases))
    searched, missing = _searched(st, disease_id)
    status = "ok" if (similar or orgs or trials or assets) else "no_supported_link"
    return {"status": status,
            "disease": {"id": disease_id, "name": g.nodes[disease_id]["name"], "short": _name(g, disease_id)},
            "user_country": country,
            "profile": _profile(st, disease_id),
            "similar": similar,
            "lookalikes": _lookalikes(st, disease_id),
            "organizations": orgs, "trials": trials, "assets": assets, "researchers": researchers,
            "next_steps": _next_steps(st, disease_id, similar, orgs, researchers, country, assets),
            "cards": [], "searched": searched, "missing": missing,
            "generated_by": {"backend": "deterministic", "model": None}}


# ---------------------------------------------------------------------------------------------
# citation validation (shared by recommend, draft_email, chat)
# ---------------------------------------------------------------------------------------------
def _split_sentences(text: str) -> list[str]:
    # move citations that follow the full stop in front of it: "Claim. [E1]" -> "Claim [E1]."
    # (unless the citations are already followed by punctuation: "B.V. [E1]." stays as is)
    text = re.sub(r"([.!?])((?:\s*\[[^\[\]]*\])+)(?!\s*[.!?\[])", r"\2\1", text.strip())
    out, start = [], 0
    for m in re.finditer(r"(?<=[.!?])\s+(?!\[)", text):
        last_word = text[start:m.start()].split()[-1] if text[start:m.start()].split() else ""
        if ABBREV_RE.search(last_word):
            continue  # "Azafaros B.V. runs" / "Dr. Smith" is not a sentence end
        out.append(text[start:m.start()])
        start = m.end()
    out.append(text[start:])
    return [x.strip() for x in out if x.strip()]


def _clean_citations(sentence: str, valid: set[str]) -> tuple[str, list[str], bool]:
    """Drop invalid ids from citation brackets. Returns (sentence, valid ids, had_any_bracket)."""
    found: list[str] = []
    had = False

    def repl(m):
        nonlocal had
        ids = EID_RE.findall(m.group(1))
        if not ids:
            return m.group(0)
        had = True
        ok = [i for i in ids if i in valid]
        found.extend(ok)
        return "[" + ", ".join(ok) + "]" if ok else ""

    s = CITE_RE.sub(repl, sentence)
    s = re.sub(r"\s+([.!?,;])", r"\1", re.sub(r"\s{2,}", " ", s)).strip()
    return s, list(dict.fromkeys(found)), had


def validate_cited(text: str, valid: set[str], keep_uncited: bool = False,
                   weak: set[str] = frozenset()) -> tuple[str, list[str]]:
    """Keep only sentences carrying >=1 valid [E…] citation (or, with keep_uncited, sentences that
    never claimed a citation). Invalid ids are removed. A sentence whose valid citations are all in
    `weak` (grant `studies` edges whose quote does not name the disease) is dropped."""
    kept, cites = [], []
    text = re.sub(r"</?[A-Za-z_][\w:-]*(?:\s[^<>]*)?>", "", text or "")  # stray markup from the model
    for s in _split_sentences(text):
        s2, ids, had = _clean_citations(s, valid)
        if ids and all(i in weak for i in ids):
            continue
        if ids or (keep_uncited and not had):
            kept.append(s2)
            cites += ids
    return " ".join(kept), list(dict.fromkeys(cites))


def validate_cited_lines(text: str, valid: set[str], weak: set[str] = frozenset()) -> tuple[str, list[str]]:
    """validate_cited applied line by line, so markdown bullets ("- ...") survive the filter."""
    lines, cites = [], []
    for raw in (text or "").splitlines():
        m = re.match(r"\s*(?:[-*•]|\d+[.)])\s+", raw)
        body = raw[m.end():] if m else raw
        kept, ids = validate_cited(body, valid, weak=weak)
        if kept:
            lines.append(("- " if m else "") + kept)
            cites += ids
    return "\n".join(lines), list(dict.fromkeys(cites))


def _edge_line(g, e) -> str:
    def label(n):
        if n not in g:
            return n
        d = g.nodes[n]
        bits = [f"{d['type']} '{_name(g, n)}'"]
        a = d.get("attrs") or {}
        if d["type"] == "trial":
            cs, _ = _countries(g, n)
            bits.append(f"(status {a.get('status') or '?'}, phase {a.get('phase') or '?'}"
                        + (f", countries {','.join(cs)}" if cs else "") + ")")
        elif d["type"] == "organization":
            extra = [x for x in (a.get("kind"), a.get("country")) if x]
            if extra:
                bits.append("(" + ", ".join(extra) + ")")
        return " ".join(bits)
    line = (f"{e['edge_id']}: {label(e['src'])} --{e['relation']}--> {label(e['dst'])}"
            f" [evidence={e.get('evidence')}, source={e.get('source')}")
    if e.get("effect"):
        line += f", effect={e['effect']}"
    if (e.get("polarity") or "supports") != "supports":
        line += ", polarity=contradicts"
    line += "]"
    if e.get("quote") and e.get("evidence") == "extracted":
        line += f' quote: "{e["quote"][:240]}"'
    return line


def _collect_edge_ids(obj) -> list[str]:
    ids = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "edges" and isinstance(v, list):
                ids += [x for x in v if isinstance(x, str)]
            else:
                ids += _collect_edge_ids(v)
    elif isinstance(obj, list):
        for v in obj:
            ids += _collect_edge_ids(v)
    return list(dict.fromkeys(ids))


def _weak_ids(st, ids) -> set[str]:
    g = st["g"]
    return {i for i in ids if i in st["edges"] and _weak_grant_edge(g, st["edges"][i][2])}


def _gen_by():
    be = llm.backend()
    return {"backend": be, "model": llm.MODELS.get(be, {}).get("smart")}


# ---------------------------------------------------------------------------------------------
# recommend
# ---------------------------------------------------------------------------------------------
CARD_KEYS = ["whats_going_on", "similar", "what_exists", "next_step"]
CARD_SCHEMA = {
    "type": "object",
    "properties": {"cards": {"type": "array", "minItems": 4, "maxItems": 4, "items": {
        "type": "object",
        "properties": {"key": {"type": "string", "enum": CARD_KEYS},
                       "title": {"type": "string"}, "text": {"type": "string"}},
        "required": ["key", "title", "text"]}}},
    "required": ["cards"],
}
RECOMMEND_SYSTEM = """You write four short cards for a parent whose child has a rare disease. The parent has no science background.
Use ONLY the facts and evidence edges given. Do not add outside knowledge.

Rules:
- Plain, warm, simple language. Explain any technical word in a few everyday words. Short sentences.
- EVERY sentence must end with one or more citations in square brackets using the edge ids given, e.g. "... enzyme [E1a2b3c4]." or "[E1a2b3c4, E5d6e7f8]". Only use ids from the list. A sentence you cannot cite must be left out.
- Never give medical advice, never recommend a treatment, never predict outcomes. Suggest talking to the child's care team where relevant.
- Only diseases listed under "lookalikes" are described as having "similar symptoms, different cause"; never use that phrase for diseases under "similar" (those share biology).
- Do not use abbreviations like "e.g." or "i.e.".
Faithfulness (strict):
- Each sentence may only state what its cited edges say: the relation between the two nodes, plus the quote if one is shown. Do not add clinical interpretation, consequences, explanations of why something matters, or how doctors use it (for example never write "a hallmark doctors look for" or "connected to low energy" unless an edge quote says exactly that).
- For evidence=extracted say "a paper reports ..."; for evidence=observed say "according to <source database>" (for example "according to ClinicalTrials.gov" or "according to HPO"); for evidence=inferred say "we matched by name".
- Never describe a grant or a researcher as working on the disease unless the cited edge's quote names the disease.

Cards (exactly these keys, in this order):
1. whats_going_on: what the disease is at the level of genes/mechanism, in everyday words.
2. similar: which other diseases are biologically similar and why (the shared evidence), plus lookalikes if any.
3. what_exists: trials, organisations, studies or researchers that exist for this disease or similar ones; mention trials in the user's country if any are marked near_user.
4. next_step: two or three concrete, non-medical next steps, taken from NEXT STEPS IN PRIORITY ORDER and kept in that order. The disease's OWN patient groups and registries (kind contact/join for this disease) always come first; only after them mention reusing what similar diseases have, and building what is missing last. Never put a similar disease's group before the disease's own groups."""


def _compact_journey(j: dict) -> dict:
    keep = {k: j[k] for k in ("status", "disease", "user_country", "profile", "similar", "lookalikes",
                              "organizations", "trials", "assets", "next_steps", "missing")}
    keep["researchers"] = j["researchers"][:6]
    keep["profile"] = {**j["profile"], "top_phenotypes": j["profile"]["top_phenotypes"][:6],
                       "variants": j["profile"]["variants"][:3]}
    return keep


def recommend(disease_id: str, country: str | None = None) -> dict:
    j = journey(disease_id, country)
    st = _state()
    g = st["g"]
    facts = _compact_journey(j)
    eids = [e for e in _collect_edge_ids(facts) if e in st["edges"]]
    lines = [_edge_line(g, st["edges"][e][2]) for e in eids[:250]]
    valid = {e for e in eids[:250]}
    order = "\n".join(f"{i + 1}. [{s['kind']}] {s['text']} (edges: {', '.join(s['edges']) or 'none'})"
                      for i, s in enumerate(j["next_steps"][:8]))
    user = ("JOURNEY FACTS (JSON):\n" + json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
            + "\n\nNEXT STEPS IN PRIORITY ORDER (own community first, then reuse, then build):\n" + order
            + "\n\nEVIDENCE EDGES (cite these ids):\n" + "\n".join(lines))
    try:
        res = llm.complete(RECOMMEND_SYSTEM, user, schema=CARD_SCHEMA, tier="smart")
    except llm.LLMError as e:
        j["cards"] = []
        j["generated_by"] = {**_gen_by(), "error": str(e)}
        return j
    by_key = {c.get("key"): c for c in (res or {}).get("cards", []) if isinstance(c, dict)}
    weak = _weak_ids(st, valid)
    cards = []
    for key in CARD_KEYS:
        c = by_key.get(key)
        if not c:
            continue
        text, cites = validate_cited(c.get("text", ""), valid, weak=weak)
        if key == "next_step":
            text, cites = _own_first(j, text, cites)
        cards.append(_card(key, c.get("title") or key, text, cites))
    j["cards"] = cards
    j["generated_by"] = _gen_by()
    return j


NO_EVIDENCE = "The atlas has no cited evidence for this yet."


def _card(key, title, text, cites) -> dict:
    """A card never has empty text: if the citation filter dropped every sentence, say so honestly."""
    if not text.strip():
        return {"key": key, "title": title, "text": NO_EVIDENCE, "citations": [],
                "status": "no_supported_evidence"}
    return {"key": key, "title": title, "text": text, "citations": cites, "status": "ok"}


def _own_first(j, text, cites) -> tuple[str, list[str]]:
    """If the disease has its own community steps but the model's next_step card cites none of them,
    prepend the deterministic own-community steps (with their citations)."""
    own = [s for s in j["next_steps"] if s["kind"] in ("contact", "join")
           and s["from_disease"] == j["disease"]["id"] and s["edges"]][:2]
    if not own or any(e in cites for s in own for e in s["edges"]):
        return text, cites
    lead = " ".join(f"{s['text'][:-1]} [{', '.join(s['edges'][:4])}]." for s in own)
    return (lead + (" " + text if text else "")).strip(), list(dict.fromkeys(
        [e for s in own for e in s["edges"][:4]] + cites))


# ---------------------------------------------------------------------------------------------
# draft_email
# ---------------------------------------------------------------------------------------------
EMAIL_SCHEMA = {"type": "object",
                "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
                "required": ["subject", "body"]}
EMAIL_SYSTEM = """You draft a short, respectful first email from a patient-group leader to an organisation, proposing a collaboration.
Use ONLY the evidence edges given. Do not invent facts, names, results, email addresses or promises.
- Every sentence that states a fact about the organisation, the diseases or their work must end with a citation like [E1a2b3c4] using only the ids given.
- Greeting, the request itself and the sign-off need no citation.
- Explain briefly why the organisation's work (on this disease or a biologically similar one) is relevant, and propose one concrete, modest next step (for example a short call).
- No medical claims or advice. Under 200 words. Plain text, no markdown. Do not add a sources list; it is added automatically.
- Sign with the sender's name and role as given.
- Each factual sentence may only state what its cited edges say (relation plus quote); no added interpretation. Say "a paper reports" for evidence=extracted and "according to <source database>" for evidence=observed.
- Never describe a grant or researcher as working on the disease unless the cited edge's quote names the disease.
- If RELATIONSHIP says the organisation works on a related disease, write to it explicitly as a related community: say that it works on the related disease (not on the sender's disease) and ask whether it would be open to connecting."""
ROLE_SKIP = ("fundraising", "donate", "donations", "press", "media", "careers", "jobs")
ROLE_GENERIC = ("info", "contact", "contactus", "hello", "office", "enquiries", "inquiries", "admin",
                "support", "team", "mail", "general", "help", "secretariat")
WEBMAIL = ("gmail", "googlemail", "yahoo", "hotmail", "outlook", "live", "icloud", "me", "aol", "proton",
           "protonmail")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")


def check_email(raw) -> tuple[str | None, str | None, bool]:
    """(usable address or None, warning or None, deprioritised?) for an organisation's contact_email.
    Invalid addresses (e.g. a scraped '[email protected]') and personal webmail are treated as missing."""
    cands = [c.strip() for c in re.split(r"[|,;\s]+", raw or "") if c.strip()]
    if not cands:
        return None, None, False
    good = []
    warn = None
    for c in cands:
        if not EMAIL_RE.match(c):
            warn = f"The contact email on file ({c!r}) is not a valid address; treated as missing."
            continue
        dom = c.split("@", 1)[1].lower().split(".")[0]
        if dom in WEBMAIL:
            warn = "The only contact email on file is a personal webmail address; treated as missing."
            continue
        good.append(c)
    if not good:
        return None, warn, False
    good.sort(key=lambda c: c.split("@")[0].lower() in ROLE_SKIP)
    best = good[0]
    return best, None, best.split("@")[0].lower() in ROLE_SKIP


def _direct_studies(g, org, d) -> bool:
    return d in g and any(o == org and _supports(e) for o, e in _in(g, d, "studies"))


def _recipient_rank(g, d, o: dict, sim_rank: dict) -> tuple[tuple, str, str | None, str | None]:
    """(sort key, reason, address, warning) for one journey organisation row as recipient for disease d."""
    to, warn, skip = check_email(o.get("contact_email"))
    pg = o.get("kind") == "patient_group"
    own = o.get("for_disease") == d
    direct = own and _direct_studies(g, o["id"], d)
    role = bool(to) and to.split("@")[0].lower() in ROLE_GENERIC
    srank = sim_rank.get(o.get("for_disease"), 99)
    if to:
        if pg and direct and role:
            tier, why = 0, "patient group linked to this disease (studies edge) with a general contact email"
        elif own:
            tier, why = (1 if pg and direct else 2 if pg else 3), \
                ("organisation linked to this disease with a contact email"
                 if not pg else "patient group linked to this disease with a contact email")
        elif pg:
            tier, why = 4, "patient group of the most similar disease with a contact email (a related community)"
        else:
            tier, why = 5, "organisation of a similar disease with a contact email (a related community)"
        if skip:
            tier += 10
            why += f" (only a {to.split('@')[0].lower()}@ address is on file)"
    else:
        if own and pg:
            tier, why = 20 + (0 if direct else 1), "patient group linked to this disease (no usable contact email)"
        elif own:
            tier, why = 22, "organisation linked to this disease (no usable contact email)"
        elif pg:
            tier, why = 23, "patient group of a similar disease, a related community (no usable contact email)"
        else:
            tier, why = 24, "organisation of a similar disease, a related community (no usable contact email)"
    return (tier, srank if not own else 0), why, to, warn


def choose_recipient(j: dict) -> tuple[dict | None, str]:
    """Best organisation from a journey to email, and why (contract §3.4 / §5)."""
    st = _state()
    g = st["g"]
    d = j["disease"]["id"]
    sim_rank = {s["id"]: i for i, s in enumerate(j.get("similar", []))}
    orgs = list(j.get("organizations", []))
    if not orgs:
        return None, "no organisation linked to this disease or a similar one in the atlas"
    ranked = sorted(((_recipient_rank(g, d, o, sim_rank), i, o) for i, o in enumerate(orgs)),
                    key=lambda x: (x[0][0], x[1]))
    (_, why, _, _), _, best = ranked[0]
    return best, why


def _org_links(st, org_id, diseases) -> list[str]:
    g = st["g"]
    eids = []
    for d in diseases:
        for o, ids in _orgs_for(g, d):
            if o == org_id:
                eids += ids
    return list(dict.fromkeys(eids))


def draft_email(org_id: str | None, disease_id: str, sender: dict | None = None) -> dict:
    """Draft an email to org_id (or, when org_id is None, to the organisation choose_recipient() picks)."""
    st = _state()
    g = st["g"]
    _require(st, disease_id, "disease")
    j = journey(disease_id)
    if org_id is None:
        best, _ = choose_recipient(j)
        if best is None:
            raise NotFound(f"no organisation linked to {disease_id} or a similar disease")
        org_id = best["id"]
    _require(st, org_id, "organization")
    sender = sender or {}
    similar = _similar(st, disease_id)
    sim_rank = {s["id"]: i for i, s in enumerate(similar)}
    row = next((o for o in j["organizations"] if o["id"] == org_id), None)
    if row is None:
        row = {"id": org_id, "kind": _attr(g, org_id, "kind"), "for_disease": None,
               "contact_email": _attr(g, org_id, "contact_email")}
    _, reason, to, mail_warn = _recipient_rank(g, disease_id, row, sim_rank)
    related = row["for_disease"] not in (None, disease_id)
    if row["for_disease"] is None:
        reason = "chosen by the caller; not linked to this disease or a similar one in the atlas"
    diseases = [disease_id] + [s["id"] for s in similar]
    warnings = ["Draft only: review and edit before sending; nothing is sent automatically."]
    link = _org_links(st, org_id, diseases)
    if not link:
        warnings.append("No evidence in the atlas links this organisation to the disease or a similar disease.")
        link = [e["edge_id"] for _, e in _out(g, org_id)][:8] + [e["edge_id"] for _, e in _in(g, org_id)][:4]
    # why the linked disease is similar
    linked_dis = {st["edges"][e][1] for e in link if st["edges"][e][1] in diseases}
    for s in similar:
        if s["id"] in linked_dis:
            for w in s["witnesses"][:3]:
                link += w["edges"]
    link = list(dict.fromkeys(link))[:40]
    valid = set(link)
    if mail_warn:
        warnings.append(mail_warn)
    if not to:
        to = None
        site = _attr(g, org_id, "website")
        warnings.append("No contact email found on the organisation's pages; "
                        + (f"use the contact form on {site}." if site else "find a contact route on their website."))
    org = g.nodes[org_id]
    user = (f"SENDER: {json.dumps(sender, ensure_ascii=False)}\n"
            f"ORGANISATION: {org['name']} (kind: {_attr(g, org_id, 'kind') or 'unknown'})\n"
            f"DISEASE OF THE SENDER'S GROUP: {g.nodes[disease_id]['name']} ({_name(g, disease_id)})\n"
            f"SIMILAR DISEASES: {', '.join(s['name'] + ' (score ' + str(s['score']) + ')' for s in similar) or 'none'}\n"
            + (f"RELATIONSHIP: this organisation works on {_name(g, row['for_disease'])}, a related disease, "
               f"not on {_name(g, disease_id)}. Write to it as a related community.\n" if related else
               f"RELATIONSHIP: the atlas links this organisation to {_name(g, disease_id)}.\n" if row["for_disease"] else "")
            + "\n"
            "EVIDENCE EDGES:\n" + "\n".join(_edge_line(g, st["edges"][e][2]) for e in link))
    try:
        res = llm.complete(EMAIL_SYSTEM, user, schema=EMAIL_SCHEMA, tier="smart")
    except llm.LLMError as e:
        return {"org_id": org_id, "disease_id": disease_id, "to": to, "recipient_reason": reason,
                "subject": None, "body": None,
                "citations": [], "warnings": warnings + [f"LLM failed: {e}"], "requires_human_review": True}
    weak = _weak_ids(st, valid)
    paras, cites = [], []
    for para in re.split(r"\n\s*\n", res.get("body", "")):
        lines = []
        for line in para.split("\n"):
            text, ids = validate_cited(line, valid, keep_uncited=True, weak=weak)
            if text:
                lines.append(text)
                cites += ids
        if lines:
            paras.append("\n".join(lines))
    cites = list(dict.fromkeys(cites))
    body = "\n\n".join(paras)
    if cites:
        body += "\n\nSources:\n" + "\n".join(
            f"[{c}] {st['edges'][c][2].get('source_url') or st['edges'][c][2].get('source')}" for c in cites)
    return {"org_id": org_id, "disease_id": disease_id, "to": to, "recipient_reason": reason,
            "subject": res.get("subject"),
            "body": body, "citations": cites, "warnings": warnings, "requires_human_review": True}


# ---------------------------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------------------------
CHAT_SCHEMA = {"type": "object",
               "properties": {"answer": {"type": "string"},
                              "answerable_from_graph": {"type": "boolean"}},
               "required": ["answer", "answerable_from_graph"]}
CHAT_SYSTEM = """You help a parent or patient-group leader with no science background understand a rare disease, using ONLY the evidence edges given (an excerpt of a knowledge graph).
- Answer the question directly, in warm, everyday words. Avoid jargon; if a medical word is unavoidable, explain it in a few plain words.
- Format: 2 to 5 short bullet points, each starting with "- " and each stating one fact. No headings, no long paragraphs.
- Every bullet must end with one or more citations like [E1a2b3c4] using only the ids given.
- Each bullet may only state what its cited edges say; add no clinical interpretation.
- Name sources simply: "a research paper" (evidence=extracted from pubmed), "a clinical-trial registry" (ctgov), "a patient group's website" (web), "a medical database" (other observed sources). Do not name database codes or ids in the text.
- Never describe a grant or researcher as working on the disease unless the cited edge's quote names the disease.
- Never give medical advice; where relevant, the last bullet can suggest discussing it with the care team (with a citation of the most relevant edge).
- If the edges do not contain the answer, set answerable_from_graph=false and say in one or two plain sentences, without citations, that the atlas does not have this information yet. Do not guess."""
CHAT_PRIORITY = {"has_mechanism": 0, "studied_with": 1, "based_in": 2, "has_site_in": 3, "runs": 4,
                 "studies": 5, "tests": 6, "caused_by": 7, "in_pathway": 8, "part_of": 9, "pi_of": 10,
                 "funds": 11, "pathogenic_for": 12, "has_phenotype": 13}


def _chat_edges(st, disease_id, cap=120) -> list[dict]:
    g = st["g"]
    sub = subgraph(disease_id, focus="all", k=2, max_nodes=400)
    sim = {s["id"] for s in _similar(st, disease_id)}
    wit = {e for s in _similar(st, disease_id) for w in s["witnesses"] for e in w["edges"]}

    def rank(e):
        rel = e["relation"]
        p = CHAT_PRIORITY.get(rel, 20)
        if g.nodes[e["source"]]["type"] == "organization" or g.nodes[e["target"]]["type"] == "organization":
            p = min(p, 2)
        if e["id"] in wit:
            p = min(p, 3)
        near = 0 if disease_id in (e["source"], e["target"]) else (1 if {e["source"], e["target"]} & sim else 2)
        return (p, near, e["evidence"] != "extracted")
    edges = sorted(sub["edges"], key=rank)
    # Per-relation quota so one plentiful relation (e.g. 177 has_mechanism edges) can't crowd out
    # trials, sites, organisations and people; leftovers fill any remaining room in rank order.
    quota = {"has_mechanism": 15, "has_phenotype": 8, "mentions": 4}
    picked, used, rest = [], {}, []
    for e in edges:
        rel = e["relation"]
        if rel in ("studies", "runs"):  # separate room for trials, organisations, grants, assets
            rel = f"{rel}:{g.nodes[e['source']]['type']}"
        if used.get(rel, 0) < quota.get(rel, 10):
            picked.append(e)
            used[rel] = used.get(rel, 0) + 1
        else:
            rest.append(e)
    picked += rest[: max(0, cap - len(picked))]
    return sorted(picked[:cap], key=rank)


def chat(disease_id: str, messages: list[dict]) -> dict:
    st = _state()
    g = st["g"]
    _require(st, disease_id, "disease")
    msgs = [m for m in (messages or []) if m.get("content")]
    last = next((m["content"] for m in reversed(msgs) if m.get("role") == "user"), None)
    if not last:
        return {"answer": "Please ask a question.", "citations": [], "grounded": False, "out_of_scope": True}
    edges = _chat_edges(st, disease_id)
    valid = {e["id"] for e in edges}
    history = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in msgs[:-1][-6:])
    user = (f"DISEASE: {g.nodes[disease_id]['name']} ({_name(g, disease_id)})\n\n"
            "EVIDENCE EDGES:\n" + "\n".join(_edge_line(g, st["edges"][e["id"]][2]) for e in edges)
            + (f"\n\nEARLIER CONVERSATION:\n{history}" if history else "")
            + f"\n\nQUESTION: {last}")
    try:
        res = llm.complete(CHAT_SYSTEM + "\nReply with ONLY a JSON object: "
                           '{"answer": "...", "answerable_from_graph": true|false}', user, tier="smart",
                           cache=False)  # live every time: chat is never served from the cache
        res = llm._parse_json(res) if isinstance(res, str) else res
    except llm.LLMError as e:
        return {"answer": f"The assistant is unavailable: {e}", "citations": [], "grounded": False,
                "out_of_scope": False}
    if not res.get("answerable_from_graph", False):
        text, _ = validate_cited(res.get("answer", ""), set(), keep_uncited=True)
        return {"answer": text or "The atlas does not contain this information yet.", "citations": [],
                "grounded": False, "out_of_scope": True}
    text, cites = validate_cited_lines(res.get("answer", ""), valid, weak=_weak_ids(st, valid))
    if not cites:
        return {"answer": "The atlas does not contain evidence that answers this question yet.",
                "citations": [], "grounded": False, "out_of_scope": True}
    return {"answer": text, "citations": cites, "grounded": True, "out_of_scope": False}


# ---------------------------------------------------------------------------------------------
# write-side wrappers
# ---------------------------------------------------------------------------------------------
def _extract():
    try:
        from atlas import extract  # noqa: PLC0415  (lazy: written by another module)
        return extract
    except ImportError as e:
        return {"error": f"atlas.extract is not available: {e}"}


def add_document(text=None, url=None, submitted_by="anonymous", disease_ids=None) -> dict:
    ex = _extract()
    if isinstance(ex, dict):
        return ex
    if not hasattr(ex, "add_document"):
        return {"error": "atlas.extract has no add_document()"}
    return ex.add_document(text=text, url=url, submitted_by=submitted_by, disease_ids=disease_ids)


def refresh_papers(since=None, disease_ids=None, per_disease=10) -> dict:
    ex = _extract()
    if isinstance(ex, dict):
        return ex
    if not hasattr(ex, "refresh_papers"):
        return {"error": "atlas.extract has no refresh_papers()"}
    return ex.refresh_papers(since=since, disease_ids=disease_ids, per_disease=per_disease)


def edge(edge_id: str) -> dict:
    st = _state()
    if edge_id not in st["edges"]:
        raise NotFound(f"unknown edge id: {edge_id}")
    return edge_json(st["edges"][edge_id][2])


def node(node_id: str) -> dict:
    st = _state()
    _require(st, node_id)
    return node_json(st["g"], node_id)
