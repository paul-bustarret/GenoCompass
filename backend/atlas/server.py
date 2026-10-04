"""Local HTTP server for the UI (docs/contract.md §2). Run: `uv run python -m atlas.server`."""
import csv
import json

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import api
from .graph import OUT

app = FastAPI(title="Rare Disease Atlas")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(api.NotFound)
async def _not_found(_: Request, exc: api.NotFound):
    return JSONResponse(status_code=404, content={"error": str(exc.args[0] if exc.args else exc)})


@app.exception_handler(ValueError)
async def _bad_request(_: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"error": str(exc)})


class EmailRequest(BaseModel):
    org_id: str | None = None  # None: pick the best recipient automatically
    disease_id: str
    sender: dict = {}


class ChatRequest(BaseModel):
    disease_id: str
    messages: list[dict]


class DocumentRequest(BaseModel):
    text: str | None = None
    url: str | None = None
    submitted_by: str = "anonymous"
    disease_ids: list[str] | None = None


class RefreshRequest(BaseModel):
    since: str | None = None
    disease_ids: list[str] | None = None
    per_disease: int = 10


@app.get("/health")
def health():
    st = api._state()
    return {"status": "ok", "nodes": st["g"].number_of_nodes(), "edges": st["g"].number_of_edges()}


@app.get("/stats")
def stats():
    return api.stats()


@app.get("/search")
def search(q: str = ""):
    return api.search(q)


@app.get("/subgraph/{disease_id}")
def subgraph(disease_id: str, focus: str = "all", k: int = 2, max_nodes: int = 150):
    return api.subgraph(disease_id, focus=focus, k=k, max_nodes=max_nodes)


@app.get("/journey/{disease_id}")
def journey(disease_id: str, country: str | None = None):
    return api.journey(disease_id, country=country)


@app.get("/recommend/{disease_id}")
def recommend(disease_id: str, country: str | None = None):
    return api.recommend(disease_id, country=country)


@app.post("/email")
def email(req: EmailRequest):
    return api.draft_email(req.org_id, req.disease_id, req.sender)


@app.post("/chat")
def chat(req: ChatRequest):
    return api.chat(req.disease_id, req.messages)


@app.post("/documents")
def documents(req: DocumentRequest):
    return api.add_document(text=req.text, url=req.url, submitted_by=req.submitted_by,
                            disease_ids=req.disease_ids)


@app.post("/refresh")
def refresh(req: RefreshRequest):
    return api.refresh_papers(since=req.since, disease_ids=req.disease_ids, per_disease=req.per_disease)


@app.get("/edge/{edge_id}")
def edge(edge_id: str):
    return api.edge(edge_id)


@app.get("/node/{node_id}")
def node(node_id: str):
    return api.node(node_id)


# ---- raw tables, in the row shape of the UI's Supabase schema --------------------------------
# (supabase/migrations/*_knowledge_graph.sql in the UI repo: the column names of data/graph/*.csv).
# Lets the UI's existing table loader run against this server instead of Supabase.
_FLOAT = {"edges": {"confidence"},
          "similarity": {"therapeutic", "phenotype_view", "gene", "pathway", "phenotype"}}
_INT = {"coverage": {"n_results", "n_kept"}}
_COLUMNS = {
    "nodes": ["id", "type", "name", "synonyms", "attrs"],
    "edges": ["edge_id", "src", "dst", "relation", "source", "source_url", "retrieved_at", "confidence",
              "evidence", "polarity", "effect", "quote", "frequency"],
    "similarity": ["a", "b", "therapeutic", "phenotype_view", "gene", "pathway", "phenotype", "lookalike",
                   "top_witnesses"],
    "coverage": ["disease_id", "source", "query", "n_results", "n_kept", "retrieved_at"],
    "clusters": ["cluster", "disease_id"],
}
_table_cache: dict[str, tuple[float, list[dict]]] = {}


def _cell(table: str, col: str, v):
    if v in ("", None):
        return None
    if col in _FLOAT.get(table, ()):
        return float(v)
    if col in _INT.get(table, ()):
        return int(float(v))
    if table == "nodes" and col == "attrs":
        return json.loads(v)
    if table == "similarity" and col == "lookalike":
        return str(v).lower() == "true"
    return v


def table_rows(name: str) -> list[dict]:
    if name not in _COLUMNS:
        raise api.NotFound(f"unknown table {name}")
    path = OUT / ("clusters.json" if name == "clusters" else f"{name}.csv")
    mtime = path.stat().st_mtime if path.exists() else -1.0
    hit = _table_cache.get(name)
    if hit and hit[0] == mtime:
        return hit[1]
    if mtime < 0:
        rows = []
    elif name == "clusters":
        rows = [{"cluster": c["cluster"], "disease_id": d}
                for c in json.loads(path.read_text()) for d in c["diseases"]]
    else:
        with open(path, newline="") as f:
            rows = [{c: _cell(name, c, r.get(c)) for c in _COLUMNS[name]} for r in csv.DictReader(f)]
    _table_cache[name] = (mtime, rows)
    return rows


@app.get("/tables/{name}")
def tables(name: str, request: Request):
    """All rows of one table. Any query param naming a column filters it (comma-separated = any of)."""
    rows = table_rows(name)
    for col, val in request.query_params.items():
        if col not in _COLUMNS[name]:
            raise ValueError(f"unknown column {col} for table {name}")
        allowed = set(val.split(","))
        rows = [r for r in rows if str(r[col]) in allowed]
    return rows


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
