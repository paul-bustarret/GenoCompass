"""Write side: LLM extraction of sourced claims from PubMed abstracts and web pages (spec §6).

Every claim the LLM proposes must pass code checks before it becomes an edge:
  invalid_type       relation / mechanism slug / effect / context outside the allowed values
  quote_check        the quote must appear verbatim (whitespace/case-normalised) in the fetched text
  relevance          the quote must name the disease (name, short, synonym, gene) or the claim's object
  unresolved_entity  caused_by gene must already be a gene node (studied_with creates an intervention)
Edges are appended to the existing graph (GraphBuilder.from_csv) and never overwrite an existing edge.

CLI:
  python -m atlas.extract papers|pages|all [--per-disease N] [--diseases ID,ID]
  python -m atlas.extract add --text TEXT | --url URL [--by NAME] [--diseases ID,ID]
  python -m atlas.extract refresh [--since YYYY-MM-DD] [--per-disease N] [--diseases ID,ID]
"""
import argparse
import hashlib
import html
import json
import os
import re
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote as urlquote

import requests
import yaml

from . import http as _http
from . import llm
from .graph import OUT, GraphBuilder
from .http import get_json
from .sources import EUTILS, _clip, slug

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
MECH_PATH = ROOT / "config" / "mechanisms.yaml"
SLICE_PATH = ROOT / "config" / "slice.yaml"
PAGES_PATH = ROOT / "config" / "pages.yaml"
WORKERS = 10
CHECKPOINT_EVERY = 50  # write the graph after every N documents so an interruption loses little
MAX_CONF = 0.8
OTHER_MAX_CONF = 0.5
PAGE_CHARS = 12000
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

RELATIONS = ("has_mechanism", "studied_with", "caused_by")
POLARITIES = ("supports", "contradicts")
EFFECTS = ("LoF", "GoF", "dominant_negative")
CONTEXTS = ("human", "animal_model", "cell_model", "review")
INTERVENTION_KINDS = ("drug", "biological", "genetic")
ORG_KINDS = ("patient_group", "company", "academic", "registry_host")
ASSET_KINDS = ("registry", "natural_history_study", "biobank", "animal_model", "cell_model", "biomarker")
DROP_REASONS = ("quote_check", "relevance", "unresolved_entity", "invalid_type")

COUNTRY_NAMES = {
    "US": "United States", "GB": "United Kingdom", "UK": "United Kingdom", "CA": "Canada", "AU": "Australia",
    "NZ": "New Zealand", "IE": "Ireland", "FR": "France", "DE": "Germany", "NL": "Netherlands",
    "BE": "Belgium", "ES": "Spain", "PT": "Portugal", "IT": "Italy", "CH": "Switzerland", "AT": "Austria",
    "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland", "PL": "Poland", "CZ": "Czechia",
    "GR": "Greece", "TR": "Turkey", "IL": "Israel", "IN": "India", "CN": "China", "JP": "Japan",
    "KR": "South Korea", "TW": "Taiwan", "SG": "Singapore", "BR": "Brazil", "AR": "Argentina",
    "MX": "Mexico", "CO": "Colombia", "CL": "Chile", "ZA": "South Africa", "EG": "Egypt", "RU": "Russia",
    "HU": "Hungary", "RO": "Romania", "SA": "Saudi Arabia", "AE": "United Arab Emirates",
}

_ncbi_lock = threading.Lock()


# ---------------------------------------------------------------- text helpers
def _norm(text: str) -> str:
    """Case/whitespace/punctuation-variant normalisation used by the quote check."""
    t = unicodedata.normalize("NFKC", text or "")
    t = re.sub(r"[‐-―−]", "-", t)
    t = re.sub(r"[‘’‛′]", "'", t)
    t = re.sub(r"[“”″]", '"', t)
    return re.sub(r"\s+", " ", t).strip().lower()


def _words(text: str) -> str:
    """Token-level normalisation used for name matching ('Tay-Sachs' == 'tay sachs')."""
    return " " + " ".join(re.findall(r"[^\W_]+", _norm(text))) + " "


def quote_ok(quote: str, text: str) -> bool:
    q = _norm(quote).rstrip(" .;,")
    return len(q) >= 15 and q in _norm(text)


def mentions_any(text: str, terms) -> bool:
    w = _words(text)
    for t in terms:
        tw = _words(t).strip()
        if len(tw) >= 2 and f" {tw} " in w:
            return True
    return False


def _name_key(name: str) -> str:
    return "".join(re.findall(r"[a-z0-9]+", _norm(name)))


def _strip_parens(name: str) -> str:
    return re.sub(r"\([^)]*\)", " ", name or "")


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# personal webmail is never published as an organisation's contact
WEBMAIL = re.compile(r"@(?:gmail|googlemail|yahoo|ymail|hotmail|outlook|live|msn|icloud|me|mac|aol|"
                     r"proton|protonmail|pm)\.", re.I)
# never chosen when the page has another address of the organisation
AVOID_LOCAL = {"fundraising", "donate", "donations", "press", "media", "careers", "jobs"}


def cf_decode(hexstr: str) -> str:
    """Cloudflare email obfuscation: first byte is the XOR key for the rest."""
    try:
        b = bytes.fromhex(hexstr)
        return "".join(chr(c ^ b[0]) for c in b[1:])
    except ValueError:
        return ""


def _cf_replace(raw: str) -> str:
    def elem(m):
        email = cf_decode(m.group(2))
        return f" {email} " if EMAIL_RE.fullmatch(email) else " "
    raw = re.sub(r"""(?is)<(a|span)\b[^>]*?data-cfemail=["']([0-9a-f]+)["'][^>]*>.*?</\1>""", elem, raw)
    # links whose href carries the encoded address: /cdn-cgi/l/email-protection#<hex>
    raw = re.sub(r"""(?i)/cdn-cgi/l/email-protection#([0-9a-f]+)""",
                 lambda m: "mailto:" + cf_decode(m.group(1)), raw)
    return raw


def choose_email(proposed: str | None, text: str, website: str | None = None) -> tuple[str | None, str | None]:
    """(contact_email, note). The address must be a strict email that literally appears in the page text;
    personal webmail is withheld; fundraising/press/jobs addresses are replaced by another address of the
    same organisation (same domain or the website's domain) when the page has one."""
    email = (proposed or "").strip().strip(".")
    if not email or not EMAIL_RE.fullmatch(email):
        return None, None
    on_page = list(dict.fromkeys(m.group(0).strip(".") for m in EMAIL_RE.finditer(text or "")))
    if email.lower() not in {e.lower() for e in on_page}:
        return None, None
    if WEBMAIL.search(email):
        return None, "personal address on page withheld"
    if email.split("@")[0].lower() in AVOID_LOCAL:
        domains = {email.split("@")[1].lower()}
        if website:
            host = re.sub(r"^(?:https?://)?(?:www\.)?", "", website.lower()).split("/")[0]
            domains.add(host)
        alts = [e for e in on_page if e.lower() != email.lower() and e.split("@")[1].lower() in domains
                and e.split("@")[0].lower() not in AVOID_LOCAL and not WEBMAIL.search(e)]
        if alts:
            return alts[0], None
        if any(e.lower() != email.lower() for e in on_page):
            return None, "only a fundraising/press/jobs address of this organisation on page"
    return email, None


def html_to_text(raw: str) -> str:
    raw = _cf_replace(raw or "")
    emails = sorted(set(re.findall(r"mailto:([\w.+-]+@[\w-]+(?:\.[\w-]+)+)", raw or "")))
    t = re.sub(r"(?is)<(script|style|noscript|svg|template)\b.*?</\1>", " ", raw or "")
    t = re.sub(r"(?s)<!--.*?-->", " ", t)
    t = re.sub(r"""<(?:"[^"]*"|'[^']*'|[^'">])*>""", " ", t)
    t = re.sub(r"\s+", " ", html.unescape(t)).strip()
    t = re.sub(r"\[email\s*protected\]", " ", t, flags=re.I)
    if emails:  # mailto: links are page content too; append them so they can be verified literally
        t += " Emails on page: " + " ".join(emails)
    return t


# ---------------------------------------------------------------- fetching (cached under data/raw/)
def _cache_path(sub: str, key: str, ext: str) -> Path:
    return RAW / sub / f"{hashlib.sha1(key.encode()).hexdigest()[:16]}.{ext}"


def fetch_abstract(pmid: str) -> str:
    """PubMed abstract as plain text (efetch rettype=abstract), cached in data/raw/pubmed_abstracts/."""
    url = f"{EUTILS}/efetch.fcgi?db=pubmed&id={pmid}&rettype=abstract&retmode=text"
    path = _cache_path("pubmed_abstracts", url, "txt")
    if path.exists():
        return path.read_text(encoding="utf-8")
    for attempt in range(3):
        with _ncbi_lock:
            wait = 0.34 - (time.time() - _http._last_ncbi[0])
            if wait > 0:
                time.sleep(wait)
            _http._last_ncbi[0] = time.time()
        try:
            r = requests.get(url, headers=_http.UA, timeout=40)
            r.raise_for_status()
            break
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(r.text, encoding="utf-8")
    return r.text


def fetch_page(url: str) -> str:
    """Raw HTML of a web page (browser UA, 30 s timeout), cached in data/raw/pages/."""
    path = _cache_path("pages", url, "html")
    if path.exists():
        return path.read_text(encoding="utf-8")
    r = requests.get(url, headers=BROWSER_UA, timeout=30)
    r.raise_for_status()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(r.text, encoding="utf-8")
    return r.text


def pubmed_summaries(pmids: list[str]) -> dict:
    if not pmids:
        return {}
    return get_json("pubmed", f"{EUTILS}/esummary.fcgi?db=pubmed&retmode=json&id={','.join(pmids)}")["result"]


# ---------------------------------------------------------------- vocabulary + disease context
def load_mechanisms(path: Path = MECH_PATH) -> dict:
    return yaml.safe_load(open(path, encoding="utf-8"))["mechanisms"]


def _slice_by_omim(path: Path = SLICE_PATH) -> dict:
    try:
        return {d["omim"]: d for d in yaml.safe_load(open(path, encoding="utf-8"))["diseases"]}
    except FileNotFoundError:
        return {}


_GENERIC_FIRST = {"neuronal", "juvenile", "hypertrophic", "late", "early", "infantile", "adult", "classic",
                  "congenital", "glycogen", "acid", "gba", "lysosomal", "type", "the", "disease", "syndrome",
                  "parkinson", "muscular", "progressive"}


def disease_contexts(gb: GraphBuilder, disease_ids=None) -> dict[str, dict]:
    """Per disease: names for prompts/relevance, causal gene symbols, slice entry (PubMed query)."""
    slice_ = _slice_by_omim()
    genes: dict[str, list[str]] = {}
    for e in gb.edges.values():
        if e["relation"] == "caused_by" and e["dst"] in gb.nodes:
            genes.setdefault(e["src"], [])
            sym = gb.nodes[e["dst"]]["name"]
            if sym not in genes[e["src"]]:
                genes[e["src"]].append(sym)
    out = {}
    for n in gb.nodes.values():
        if n["type"] != "disease" or (disease_ids and n["id"] not in disease_ids):
            continue
        a = n["attrs"]
        entry = slice_.get(a.get("omim"), {})
        syns = [s for s in (n.get("synonyms") or "").split("|") if s]
        query_terms = [q.strip().strip('"') for q in entry.get("query", "").split(" OR ") if q.strip()]
        terms = [n["name"], a.get("short") or "", a.get("mondo_label") or ""] + syns + query_terms + genes.get(n["id"], [])
        # family words ("Sanfilippo", "MPS", "Pompe") used only to verify page-level disease coverage
        family = {_words(t).split()[0] for t in [n["name"], a.get("short") or ""] + query_terms if _words(t).split()}
        family = sorted(f for f in family if len(f) >= 3 and f not in _GENERIC_FIRST)
        out[n["id"]] = {"id": n["id"], "name": n["name"], "short": a.get("short") or n["name"],
                        "synonyms": syns, "genes": genes.get(n["id"], []), "slice": entry,
                        "terms": [t for t in dict.fromkeys(terms) if t], "family": family}
    return out


def _pubmed_term(ctx: dict) -> str:
    entry = ctx["slice"]
    if entry.get("pubmed"):
        return entry["pubmed"]
    if entry.get("query"):
        return " OR ".join(f'"{q.strip()}"[tiab]' for q in entry["query"].split(" OR "))
    return f'"{ctx["name"]}"[tiab]'


# ---------------------------------------------------------------- LLM prompts + schemas
def _nullable_enum(values):
    return {"type": ["string", "null"], "enum": list(values) + [None]}


CLAIM_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["claims"],
    "properties": {"claims": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["relation", "object", "polarity", "effect", "context", "intervention_kind", "quote", "confidence"],
        "properties": {
            "relation": {"type": "string", "enum": list(RELATIONS)},
            "object": {"type": "string"},
            "polarity": {"type": "string", "enum": list(POLARITIES)},
            "effect": _nullable_enum(EFFECTS),
            "context": _nullable_enum(CONTEXTS),
            "intervention_kind": _nullable_enum(INTERVENTION_KINDS),
            "quote": {"type": "string"},
            "confidence": {"type": "number"},
        }}}},
}

PAPER_SYSTEM = """You extract sourced claims about ONE target disease from a PubMed abstract for a rare-disease knowledge graph.

Relations (subject is always the target disease):
- has_mechanism: object = ONE slug from the mechanism list below that the abstract states is part of the disease's mechanism.
- studied_with: object = the name of a specific therapy or intervention (drug, enzyme replacement, gene therapy, transplant, diet...) tested or used in the target disease, as named in the abstract.
- caused_by: object = HGNC gene symbol whose variants cause the disease.

Fields:
- polarity: "supports", or "contradicts" ONLY when the abstract explicitly reports a negative or contradicting finding (e.g. the therapy showed no benefit, the mechanism was not observed / not required).
- effect: "LoF", "GoF", "dominant_negative" when the abstract states the variant effect; otherwise null.
- context: "human" (patients), "animal_model", "cell_model", or "review"; null if unclear.
- intervention_kind: for studied_with only: "drug", "biological", or "genetic"; null otherwise.
- quote: ONE sentence copied EXACTLY, character for character, from the abstract that states the claim. Prefer a sentence that names the disease or the object. Never paraphrase, never join sentences.
- confidence: 0-1, how directly the quoted sentence states the claim.

Rules: only claims stated in the abstract; no background knowledge. Use "other" only if no slug fits. Return at most 8 claims; return {"claims": []} if nothing qualifies.

Mechanism slugs:
{mechanisms}

Worked example.
Target disease: Krabbe disease (gene GALC)
Abstract: "Krabbe disease is a leukodystrophy caused by deficiency of galactocerebrosidase (GALC). Psychosine accumulation drives oligodendrocyte death and demyelination in the twitcher mouse. AAV gene therapy combined with bone marrow transplant extended survival of twitcher mice. In contrast, substrate reduction with L-cycloserine alone did not improve survival."
Output:
{"claims": [
 {"relation": "has_mechanism", "object": "lysosomal_enzyme_deficiency", "polarity": "supports", "effect": "LoF", "context": "review", "intervention_kind": null, "quote": "Krabbe disease is a leukodystrophy caused by deficiency of galactocerebrosidase (GALC).", "confidence": 0.9},
 {"relation": "caused_by", "object": "GALC", "polarity": "supports", "effect": "LoF", "context": "review", "intervention_kind": null, "quote": "Krabbe disease is a leukodystrophy caused by deficiency of galactocerebrosidase (GALC).", "confidence": 0.9},
 {"relation": "has_mechanism", "object": "demyelination", "polarity": "supports", "effect": null, "context": "animal_model", "intervention_kind": null, "quote": "Psychosine accumulation drives oligodendrocyte death and demyelination in the twitcher mouse.", "confidence": 0.8},
 {"relation": "studied_with", "object": "AAV gene therapy", "polarity": "supports", "effect": null, "context": "animal_model", "intervention_kind": "genetic", "quote": "AAV gene therapy combined with bone marrow transplant extended survival of twitcher mice.", "confidence": 0.8},
 {"relation": "studied_with", "object": "L-cycloserine", "polarity": "contradicts", "effect": null, "context": "animal_model", "intervention_kind": "drug", "quote": "In contrast, substrate reduction with L-cycloserine alone did not improve survival.", "confidence": 0.7}
]}"""


def paper_system(mechs: dict) -> str:
    lines = "\n".join(f"- {k}: {v['definition']}" for k, v in mechs.items())
    return PAPER_SYSTEM.replace("{mechanisms}", lines)


def paper_user(ctx: dict, text: str) -> str:
    syn = "; ".join(ctx["synonyms"][:8])
    return (f"Target disease: {ctx['name']} (short: {ctx['short']}; gene: {', '.join(ctx['genes']) or 'unknown'}; "
            f"also called: {syn})\n\nAbstract:\n{text.strip()}")


PAGE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["organizations"],
    "properties": {"organizations": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["name", "kind", "country", "website", "contact_email", "diseases", "assets", "quote"],
        "properties": {
            "name": {"type": "string"},
            "kind": {"type": "string", "enum": list(ORG_KINDS)},
            "country": {"type": ["string", "null"]},
            "website": {"type": ["string", "null"]},
            "contact_email": {"type": ["string", "null"]},
            "diseases": {"type": "array", "items": {"type": "string"}},
            "quote": {"type": "string"},
            "assets": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["name", "kind", "quote"],
                "properties": {"name": {"type": "string"}, "kind": {"type": "string", "enum": list(ASSET_KINDS)},
                               "quote": {"type": "string"}}}},
        }}}},
}

PAGE_SYSTEM = """You read the text of a web page (patient organisation, registry, company or academic site) and list the organisations it describes that work on the listed rare diseases, and the research assets they run.

For each organisation:
- name: as written on the page.
- kind: patient_group | company | academic | registry_host.
- country: ISO 3166-1 alpha-2 code of where it is based, if the page states or clearly implies it (address, phone prefix, ".org.uk"); else null.
- website: its URL if shown; else null.
- contact_email: ONLY an email address that literally appears in the page text; else null. Never guess.
- diseases: which of the provided slice diseases it covers, copied exactly from the provided list. Empty if none.
- assets: research assets it runs: registry, natural_history_study, biobank, animal_model, cell_model, biomarker; each with name and a verbatim quote (page language).
- quote: ONE sentence copied EXACTLY, character for character, from the page text (in the page's own language; never translate) naming the organisation or the disease and showing what it does.

A page may describe several organisations (e.g. a directory of national groups): list each one, with its own quote and country.
Only include organisations that actually work on one of the listed diseases (ignore sponsors, footers, unrelated "other programs" boxes). Return {"organizations": []} if none."""


def save_graph(gb: GraphBuilder, out: Path = OUT) -> None:
    """gb.write() into a temp dir, then atomically swap each CSV in (a crash never leaves a half file)."""
    out = Path(out)
    tmp = out / ".tmp_write"
    gb.write(tmp)
    for name in ("nodes.csv", "edges.csv", "coverage.csv"):
        os.replace(tmp / name, out / name)
    tmp.rmdir()


# ---------------------------------------------------------------- stats
class Stats:
    def __init__(self):
        self.c = Counter()
        self.dropped = Counter({r: 0 for r in DROP_REASONS})
        self.kept_examples, self.drop_examples = [], []
        self.added_edges, self.new_nodes, self.contradictions, self.kept = [], [], [], []

    def drop(self, reason, **ex):
        self.dropped[reason] += 1
        if len(self.drop_examples) < 25:
            self.drop_examples.append({"reason": reason, **ex})

    def as_dict(self):
        return {**dict(self.c), "kept": len(self.kept), "edges_added_total": len(self.added_edges), "dropped": dict(self.dropped),
                "new_nodes": len(self.new_nodes), "contradictions": len(self.contradictions),
                "kept_examples": self.kept_examples[:25], "drop_examples": self.drop_examples}


def _eid(src, dst, relation, source_url) -> str:  # mirrors GraphBuilder.edge
    return "E" + hashlib.sha1(f"{src}|{dst}|{relation}|{source_url}".encode()).hexdigest()[:7]


def _add_edge(gb, st: Stats, src, dst, relation, source, source_url, **kw) -> str | None:
    """Append an edge unless one with the same id exists (never overwrite)."""
    eid = _eid(src, dst, relation, source_url)
    if eid in gb.edges:
        st.c["already_present"] += 1
        return None
    gb.edge(src, dst, relation, source, source_url, **kw)
    st.added_edges.append(eid)
    if kw.get("polarity") == "contradicts":
        st.contradictions.append(eid)
    if kw.get("evidence") == "extracted":
        st.kept.append(eid)
    if len(st.kept_examples) < 25 and kw.get("evidence") == "extracted":
        st.kept_examples.append({"id": eid, "src": src, "relation": relation, "dst": dst,
                                 "polarity": kw.get("polarity"), "quote": (kw.get("quote") or "")[:160]})
    return eid


def _add_node(gb, st: Stats, id_, type_, name, **attrs) -> str:
    if id_ not in gb.nodes:
        st.new_nodes.append(id_)
    return gb.node(id_, type_, name, **attrs)


# ---------------------------------------------------------------- claim pipeline (papers / documents)
def _match_intervention(gb, name: str) -> str | None:
    keys = {_name_key(name), _name_key(_strip_parens(name))} - {""}
    for n in gb.nodes.values():
        if n["type"] != "intervention":
            continue
        for cand in [n["name"]] + (n.get("synonyms") or "").split("|"):
            if cand and ({_name_key(cand), _name_key(_strip_parens(cand))} & keys):
                return n["id"]
    return None


def _match_gene(gb, symbol: str) -> str | None:
    s = symbol.strip().upper()
    for n in gb.nodes.values():
        if n["type"] == "gene" and n["name"].upper() == s:
            return n["id"]
    return None


def _confirm_contradictions(ctx: dict, claims: list[dict], text: str) -> dict[int, bool]:
    """Second-pass checks for every `contradicts` claim whose quote is genuine, run in parallel
    (index -> confirmed), so a document costs one extraction call plus one round of checks."""
    idx = [i for i, c in enumerate(claims) if c.get("polarity") == "contradicts"
           and (c.get("object") or "").strip() and quote_ok(c.get("quote") or "", text)]
    if not idx:
        return {}
    args = [(ctx["name"], claims[i].get("relation"), (claims[i].get("object") or "").strip(),
             (claims[i].get("quote") or "").strip()) for i in idx]
    with ThreadPoolExecutor(max_workers=min(len(idx), WORKERS)) as ex:
        return dict(zip(idx, ex.map(lambda a: confirm_contradiction(*a), args)))


def apply_claims(gb, st: Stats, ctx: dict, claims: list[dict], text: str, source: str, source_url: str,
                 mechs: dict, submitted_by: str = "") -> None:
    """Run the four checks over LLM claims and append the survivors as extracted edges."""
    did = ctx["id"]
    confirmed = _confirm_contradictions(ctx, claims, text)
    for i, c in enumerate(claims):
        st.c["claims_proposed"] += 1
        rel, obj, q = c.get("relation"), (c.get("object") or "").strip(), c.get("quote") or ""
        ex = {"disease": ctx["short"], "relation": rel, "object": obj, "quote": q[:160], "source_url": source_url}
        effect, context, pol = c.get("effect"), c.get("context"), c.get("polarity")
        if (rel not in RELATIONS or pol not in POLARITIES or effect not in (None, *EFFECTS)
                or context not in (None, *CONTEXTS) or not obj
                or (rel == "has_mechanism" and obj not in mechs)):
            st.drop("invalid_type", **ex)
            continue
        if not quote_ok(q, text):
            st.drop("quote_check", **ex)
            continue
        obj_terms = [obj]
        if rel == "has_mechanism":
            m = mechs[obj]
            obj_terms = [m["label"]] + list(m.get("keywords") or [])
        if not mentions_any(q, ctx["terms"] + obj_terms):
            st.drop("relevance", **ex)
            continue
        conf = min(float(c.get("confidence") or 0), MAX_CONF)
        if pol == "contradicts" and not confirmed.get(i, False):
            pol, conf = "supports", min(conf, 0.5)
            st.c["contradictions_downgraded"] += 1
        if rel == "has_mechanism":
            if obj == "other":
                conf = min(conf, OTHER_MAX_CONF)
            dst = _add_node(gb, st, f"MECH:{obj}", "mechanism", mechs[obj]["label"], label=mechs[obj]["label"])
        elif rel == "caused_by":
            dst = _match_gene(gb, obj)
            if dst is None:
                st.drop("unresolved_entity", **ex)
                continue
        else:  # studied_with
            dst = _match_intervention(gb, obj)
            if dst is None:
                iid = f"INTERVENTION:{slug(obj)}"
                if not slug(obj):
                    st.drop("unresolved_entity", **ex)
                    continue
                dst = _add_node(gb, st, iid, "intervention", obj, kind=c.get("intervention_kind"))
        _add_edge(gb, st, did, dst, rel, source, source_url, evidence="extracted", confidence=round(conf, 2),
                  quote=_clip(q.strip()), effect=effect or "", polarity=pol, context=context or "",
                  submitted_by=submitted_by)


CONTRA_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["is_contradiction", "reason"],
                 "properties": {"is_contradiction": {"type": "boolean"}, "reason": {"type": "string"}}}
CONTRA_SYSTEM = """You check one claim proposed for a rare-disease knowledge graph.
Answer is_contradiction=true ONLY if the quoted sentence explicitly reports a negative or refuting finding for the stated relation: a negative trial result, "no effect", "did not improve", "not associated", "was not observed", a failure to replicate.
Answer false if the sentence only describes limitations, side effects, incomplete efficacy, uncertainty, unclear or insufficient evidence, unmet need, or that a treatment is "not curative" — those are not contradictions.
Give a one-sentence reason."""


def confirm_contradiction(disease: str, relation: str, obj: str, quote: str) -> bool:
    """Second pass for every proposed `contradicts` claim; an LLM failure counts as 'not confirmed'."""
    user = f"Disease: {disease}\nRelation: {relation}\nObject: {obj}\nQuoted sentence: {quote}"
    try:
        out = llm.complete(CONTRA_SYSTEM, user, schema=CONTRA_SCHEMA, tier="fast")
    except llm.LLMError:
        return False
    return isinstance(out, dict) and out.get("is_contradiction") is True


def _llm_claims(ctx: dict, text: str, mechs: dict) -> list[dict] | None:
    try:
        out = llm.complete(paper_system(mechs), paper_user(ctx, text), schema=CLAIM_SCHEMA, tier="fast")
        return out.get("claims", []) if isinstance(out, dict) else []
    except llm.LLMError:
        return None


def _paper_node(gb, st, pmid: str, meta: dict, did: str | None):
    url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    _add_node(gb, st, f"PMID:{pmid}", "paper", meta.get("title", "") or f"PMID {pmid}",
              journal=meta.get("source", ""), pubdate=meta.get("pubdate", ""),
              authors=[a["name"] for a in meta.get("authors", [])])
    if did:
        _add_edge(gb, st, f"PMID:{pmid}", did, "mentions", "pubmed", url, quote=meta.get("title", ""))


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _safe_abstract(pmid: str) -> str | None:
    try:
        return fetch_abstract(pmid)
    except requests.RequestException:
        return None


def _run_papers(gb, jobs: list[tuple[dict, str]], st: Stats, mechs: dict, submitted_by: str = "",
                checkpoint=None):
    """jobs: (disease ctx, pmid). Per chunk of CHECKPOINT_EVERY: fetch abstracts sequentially (NCBI rate
    limit), LLM in parallel, apply in order, then call `checkpoint()` (e.g. gb.write) so finished work is kept."""
    pmids = list(dict.fromkeys(p for _, p in jobs))
    meta = {}
    for i in range(0, len(pmids), 200):
        meta.update(pubmed_summaries(pmids[i:i + 200]))
    texts: dict[str, str | None] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for chunk in _chunks(jobs, CHECKPOINT_EVERY):
            for _, p in chunk:
                if p not in texts:
                    texts[p] = _safe_abstract(p)
            results = list(ex.map(lambda j: _llm_claims(j[0], texts[j[1]], mechs) if texts[j[1]] else None,
                                  chunk))
            for (ctx, pmid), claims in zip(chunk, results):
                if not texts[pmid]:
                    st.c["fetch_errors"] += 1
                    continue
                st.c["papers_read"] += 1
                _paper_node(gb, st, pmid, meta.get(pmid, {}), ctx["id"])
                if claims is None:
                    st.c["llm_errors"] += 1
                    continue
                apply_claims(gb, st, ctx, claims, texts[pmid], "pubmed",
                             f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", mechs, submitted_by)
            if checkpoint:
                checkpoint()


def _esearch(term: str, retmax: int, extra: str = "", sort: str = "relevance") -> tuple[int, list[str]]:
    s = get_json("pubmed", f"{EUTILS}/esearch.fcgi?db=pubmed&retmode=json&sort={sort}&retmax={retmax}"
                           f"{extra}&term={urlquote(term)}")
    return int(s["esearchresult"]["count"]), s["esearchresult"]["idlist"]


def _on_topic(pmids: list[str], ctx: dict) -> list[str]:
    """Best-match order, but papers whose title names the disease (or its gene) come first."""
    meta = pubmed_summaries(pmids)
    return sorted(pmids, key=lambda p: not mentions_any(meta.get(p, {}).get("title", ""), ctx["terms"]))


def extract_papers(gb: GraphBuilder, disease_ids=None, per_disease: int = 10, checkpoint=None) -> dict:
    mechs = load_mechanisms()
    ctxs = disease_contexts(gb, disease_ids)
    st, jobs, searched = Stats(), [], {}
    for did, ctx in ctxs.items():
        term = f"({_pubmed_term(ctx)}) AND hasabstract"
        n, ids = _esearch(term, per_disease * 3)
        searched[did] = (term, n)
        jobs += [(ctx, p) for p in _on_topic(ids, ctx)[:per_disease]]
    before = Counter()
    _run_papers_counted(gb, jobs, st, mechs, before, checkpoint=checkpoint)
    for did, (term, n) in searched.items():
        gb.covered(did, "pubmed_llm", term, n, before[did])
    return st.as_dict()


def _run_papers_counted(gb, jobs, st, mechs, per_disease_kept: Counter, submitted_by="", checkpoint=None):
    start = len(st.added_edges)
    _run_papers(gb, jobs, st, mechs, submitted_by, checkpoint)
    for eid in st.added_edges[start:]:
        e = gb.edges[eid]
        if e["evidence"] == "extracted":
            per_disease_kept[e["src"]] += 1


# ---------------------------------------------------------------- pages (patient groups / registries)
_STOP = {"disease", "syndrome", "type", "the", "of", "and", "deficiency", "s"}


def _toks(text: str) -> set[str]:
    return set(_words(_strip_parens(text) + " " + " ".join(re.findall(r"\(([^)]*)\)", text or ""))).split()) - _STOP


def _match_disease_by_name(ctxs: dict, name: str) -> str | None:
    """Exact name/short/synonym match, else the disease whose name shares the most (>=2) distinctive words."""
    k, k2 = _name_key(name), _name_key(_strip_parens(name))
    for did, c in ctxs.items():
        if any(_name_key(t) in (k, k2) for t in [c["name"], c["short"]] + c["synonyms"]):
            return did
    q = _toks(name)
    best, score, need, tie = None, 0, min(2, len(q)), False
    for did, c in ctxs.items():
        s = max(len(q & _toks(t)) for t in [c["name"], c["short"]] + c["synonyms"])
        if s >= need and s > score:
            best, score, tie = did, s, False
        elif s >= need and s == score:
            tie = True
    return None if best is None or tie else best


def _match_org(gb, name: str) -> str | None:
    k = _name_key(name)
    for n in gb.nodes.values():
        if n["type"] == "organization" and (_name_key(n["name"]) == k or n["id"] == f"ORG:{slug(name)}"):
            return n["id"]
    return None


def _country(gb, st, iso: str | None) -> str | None:
    iso = (iso or "").strip().upper()
    iso = "GB" if iso == "UK" else iso
    if not re.fullmatch(r"[A-Z]{2}", iso):
        return None
    cid = f"COUNTRY:{iso}"
    if cid not in gb.nodes:
        st.new_nodes.append(cid)
        gb.nodes[cid] = {"id": cid, "type": "country", "name": COUNTRY_NAMES.get(iso, iso), "synonyms": "",
                         "attrs": {"name": COUNTRY_NAMES.get(iso, iso)}}
    return cid


def apply_orgs(gb, st: Stats, orgs: list[dict], text: str, url: str, ctxs: dict,
               country_hint: str | None = None, submitted_by: str = "") -> None:
    labels = {f"{c['short']} — {c['name']}": did for did, c in ctxs.items()}
    common = dict(evidence="extracted", confidence=0.7, submitted_by=submitted_by)
    for o in orgs:
        st.c["orgs_proposed"] += 1
        name, q = (o.get("name") or "").strip(), o.get("quote") or ""
        ex = {"org": name, "quote": q[:160], "source_url": url}
        if not name or o.get("kind") not in ORG_KINDS:
            st.drop("invalid_type", **ex)
            continue
        if not quote_ok(q, text):
            st.drop("quote_check", **ex)
            continue
        dids = []
        for d in o.get("diseases") or []:
            did = labels.get(d) or _match_disease_by_name(ctxs, d) or _match_disease_by_name(ctxs, d.split(" — ")[0])
            # the disease must also be named somewhere on the page
            if did and did not in dids and mentions_any(text, ctxs[did]["terms"] + ctxs[did]["family"]):
                dids.append(did)
        dis_terms = [t for did in dids for t in ctxs[did]["terms"] + ctxs[did]["family"]]
        if not dids or not mentions_any(q, [name] + dis_terms):
            st.drop("relevance", **ex)
            continue
        email, email_note = choose_email(o.get("contact_email"), text, o.get("website"))
        if (o.get("contact_email") or "").strip() and email is None:
            st.c["emails_nulled"] += 1
        oid = _match_org(gb, name)
        if oid is None:
            oid = _add_node(gb, st, f"ORG:{slug(name)}", "organization", name, kind=o["kind"],
                            website=o.get("website"), contact_email=email, country=None)
        attrs = gb.nodes[oid]["attrs"]
        for k, v in (("website", o.get("website")), ("contact_email", email)):
            if v and not attrs.get(k):
                attrs[k] = v
        if email_note and not attrs.get("contact_email"):
            attrs["contact_email_note"] = email_note
        st.c["orgs_kept"] += 1
        for did in dids:
            _add_edge(gb, st, oid, did, "studies", "web", url, quote=q.strip(), **common)
        cid = _country(gb, st, o.get("country") or country_hint)
        if cid:
            attrs.setdefault("country", None)
            attrs["country"] = attrs["country"] or cid.split(":")[1]
            _add_edge(gb, st, oid, cid, "based_in", "web", url, quote=q.strip(), **common)
        for a in o.get("assets") or []:
            st.c["assets_proposed"] += 1
            aname, aq = (a.get("name") or "").strip(), a.get("quote") or ""
            aex = {"org": name, "asset": aname, "quote": aq[:160], "source_url": url}
            if not aname or a.get("kind") not in ASSET_KINDS or not slug(aname):
                st.drop("invalid_type", **aex)
                continue
            if not quote_ok(aq, text):
                st.drop("quote_check", **aex)
                continue
            if not mentions_any(aq, [aname, name] + dis_terms):
                st.drop("relevance", **aex)
                continue
            aid = _add_node(gb, st, f"ASSET:{slug(aname)}", "asset", aname, kind=a["kind"], website=o.get("website"))
            _add_edge(gb, st, oid, aid, "runs", "web", url, quote=aq.strip(), **common)
            # asset -> disease only for diseases its own quote names (or the org's single disease)
            a_dids = [d for d in dids if mentions_any(aq, ctxs[d]["terms"])] or (dids if len(dids) == 1 else [])
            for did in a_dids:
                _add_edge(gb, st, aid, did, "studies", "web", url, quote=aq.strip(), **common)


def _llm_orgs(text: str, ctxs: dict, note: str = "") -> list[dict] | None:
    listing = "\n".join(f"- {c['short']} — {c['name']}" for c in ctxs.values())
    user = (f"Slice diseases (copy names exactly from this list):\n{listing}\n\n"
            + (f"Curator note: {note}\n\n" if note else "") + f"Page text:\n{text}")
    try:
        out = llm.complete(PAGE_SYSTEM, user, schema=PAGE_SCHEMA, tier="fast")
        return out.get("organizations", []) if isinstance(out, dict) else []
    except llm.LLMError:
        return None


def page_text(url: str) -> str:
    return html_to_text(fetch_page(url))[:PAGE_CHARS]


def extract_pages(gb: GraphBuilder, pages_path=PAGES_PATH, checkpoint=None) -> dict:
    st = Stats()
    pages_path = Path(pages_path)
    if not pages_path.exists():
        st.c["missing_pages_file"] = 1
        return st.as_dict()
    pages = yaml.safe_load(open(pages_path, encoding="utf-8")).get("pages") or []
    ctxs = disease_contexts(gb)
    by_omim = {gb.nodes[d]["attrs"].get("omim"): d for d in ctxs if gb.nodes[d]["attrs"].get("omim")}

    def work(p):
        try:
            text = page_text(p["url"])
        except requests.RequestException as e:
            return p, None, None, str(e)[:120]
        if len(text) < 200:
            return p, None, None, f"page has no readable text ({len(text)} chars; JS-rendered?)"
        return p, text, _llm_orgs(text, ctxs, p.get("note") or ""), None

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for chunk in _chunks(pages, CHECKPOINT_EVERY):
            for p, text, orgs, err in ex.map(work, chunk):
                did = (by_omim.get(p.get("omim")) if p.get("omim") else None) \
                    or _match_disease_by_name(ctxs, p.get("disease_name") or "")
                st.c["pages_read"] += 1
                if err or orgs is None:
                    st.c["fetch_errors" if err else "llm_errors"] += 1
                    if err and len(st.drop_examples) < 25:
                        st.drop_examples.append({"reason": "fetch_error", "source_url": p["url"], "error": err})
                    if did:
                        gb.covered(did, "patient_groups", p["url"], -1, 0)
                    continue
                start = len(st.added_edges)
                apply_orgs(gb, st, orgs, text, p["url"], ctxs, p.get("country_hint"))
                kept = sum(1 for e in st.added_edges[start:] if gb.edges[e]["relation"] == "studies"
                           and gb.edges[e]["src"].startswith("ORG:"))
                if did:
                    gb.covered(did, "patient_groups", p["url"], len(orgs), kept)
            if checkpoint:
                checkpoint()
    return st.as_dict()


# ---------------------------------------------------------------- contributions
PMID_URL = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)|^\s*(?:PMID:?\s*)?(\d{5,9})\s*$", re.I)


def infer_diseases(text: str, ctxs: dict, top: int = 3) -> list[str]:
    w = _words(text)
    scores = {}
    for did, c in ctxs.items():
        s = sum(w.count(f" {_words(t).strip()} ") for t in c["terms"] if len(_words(t).strip()) >= 3)
        if s:
            scores[did] = s
    return sorted(scores, key=lambda d: -scores[d])[:top]


def _report(source_url, submitted_by, st: Stats, papers_checked: int, reason: str | None = None) -> dict:
    out = {"source_url": source_url, "submitted_by": submitted_by, "added_edges": st.added_edges,
           "new_nodes": st.new_nodes, "contradictions": st.contradictions,
           "dropped": {r: st.dropped[r] for r in DROP_REASONS}, "papers_checked": papers_checked}
    if reason:
        out["reason"] = reason
    return out


def _details(gb, st: Stats, ctxs: dict, dids: list[str], title: str | None) -> dict:
    """What a person who submitted the document needs to see: names, not ids (UI contribute page)."""
    def name(n):
        node = gb.nodes.get(n) or {}
        return (node.get("attrs") or {}).get("short") or node.get("name") or n

    added = []
    for eid in st.added_edges:
        e = gb.edges[eid]
        if e["evidence"] != "extracted":
            continue  # the paper -> disease 'mentions' record is bookkeeping, not a finding
        added.append({"id": eid, "relation": e["relation"], "source": e["src"], "source_name": name(e["src"]),
                      "target": e["dst"], "target_name": name(e["dst"]),
                      "target_type": (gb.nodes.get(e["dst"]) or {}).get("type"),
                      "polarity": e["polarity"] or "supports", "confidence": e["confidence"],
                      "context": e["context"] or None, "quote": e["quote"]})
    return {"title": title or None,
            "matched_diseases": [{"id": d, "name": ctxs[d]["name"], "short": ctxs[d]["short"]} for d in dids],
            "added": added,
            "new_node_details": [{"id": n, "type": gb.nodes[n]["type"], "name": gb.nodes[n]["name"]}
                                 for n in st.new_nodes if n in gb.nodes],
            "rejected": [{k: d.get(k) for k in ("reason", "relation", "object", "quote")}
                         for d in st.drop_examples],
            "claims_proposed": st.c["claims_proposed"], "already_present": st.c["already_present"],
            "llm_errors": st.c["llm_errors"]}


def parse_pmid(url: str | None = None, text: str | None = None) -> str | None:
    """PMID from a PubMed URL or a bare 'PMID 12345678' / '12345678' (in `url`, or as the whole `text`)."""
    m = PMID_URL.search(url or "") or (PMID_URL.search(text) if text and not url else None)
    return (m.group(1) or m.group(2)) if m else None


def add_document(text=None, url=None, submitted_by="anonymous", disease_ids=None,
                 gb: GraphBuilder | None = None, out: Path = OUT) -> dict:
    """One document (raw text, PubMed URL/PMID, or any URL) through the same extraction + checks.

    One extraction LLM call per target disease (plus parallel second-pass checks of any proposed
    contradictions). Without `disease_ids` the single best-matching slice disease is the target."""
    own = gb is None
    gb = gb or GraphBuilder.from_csv(out)
    mechs = load_mechanisms()
    st = Stats()
    submitted_by = (submitted_by or "").strip() or "anonymous"
    pmid = parse_pmid(url, text)
    title = None
    if pmid:
        source, source_url = "pubmed", f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        try:
            meta = pubmed_summaries([pmid]).get(pmid, {}) or {}
            body = fetch_abstract(pmid)
        except requests.RequestException as e:
            return _report(source_url, submitted_by, st, 0, f"could not reach PubMed: {str(e)[:120]}")
        if meta.get("error") or len((body or "").strip()) < 80:
            return _report(source_url, submitted_by, st, 0, f"PubMed has no abstract for PMID {pmid}")
        title = meta.get("title")
    elif url:
        try:
            body = page_text(url)
        except requests.RequestException as e:
            return _report(url, submitted_by, st, 0, f"could not fetch url: {str(e)[:120]}")
        source, source_url, meta = "web", url, None
    elif text and text.strip():
        body = text
        source, source_url, meta = "submitted", f"submitted:sha1:{hashlib.sha1(text.encode()).hexdigest()[:12]}", None
        title = text.strip().split("\n", 1)[0][:160]
    else:
        return _report(None, submitted_by, st, 0, "no text or url given")
    ctxs = disease_contexts(gb)
    dids = [d for d in (disease_ids or []) if d in ctxs] if disease_ids else infer_diseases(body, ctxs, top=1)
    if not dids:
        res = _report(source_url, submitted_by, st, 0,
                      "no slice disease (name, synonym or gene) is mentioned in the document; nothing added")
        return {**res, **_details(gb, st, ctxs, [], title)}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(lambda d: _llm_claims(ctxs[d], body, mechs), dids))
    for did, claims in zip(dids, results):
        if pmid:
            _paper_node(gb, st, pmid, meta, did)
        if claims is None:
            st.c["llm_errors"] += 1
            continue
        start = len(st.added_edges)
        apply_claims(gb, st, ctxs[did], claims, body, source, source_url, mechs, submitted_by)
        gb.covered(did, "pubmed_llm" if pmid else "submitted", source_url, len(claims),
                   len(st.added_edges) - start)
    if own:
        save_graph(gb, out)
        _write_report({"add_document": st.as_dict()}, out)
    reason = "the AI reader could not be reached; nothing added" if st.c["llm_errors"] == len(dids) else None
    return {**_report(source_url, submitted_by, st, 1, reason), **_details(gb, st, ctxs, dids, title)}


def refresh_papers(since=None, disease_ids=None, per_disease: int = 10,
                   gb: GraphBuilder | None = None, out: Path = OUT) -> dict:
    """New PubMed papers (pub date >= since, PMID not yet in the graph) through the paper pipeline."""
    own = gb is None
    gb = gb or GraphBuilder.from_csv(out)
    if since is None:
        prev = [r["retrieved_at"] for r in gb.coverage if r.get("source") == "pubmed_llm" and r.get("retrieved_at")]
        since = max(prev) if prev else (date.today() - timedelta(days=30)).isoformat()
    mechs = load_mechanisms()
    ctxs = disease_contexts(gb, disease_ids)
    st, jobs, searched = Stats(), [], {}
    window = f"&datetype=pdat&mindate={since.replace('-', '/')}&maxdate={date.today().strftime('%Y/%m/%d')}"
    for did, ctx in ctxs.items():
        term = f"({_pubmed_term(ctx)}) AND hasabstract"
        n, ids = _esearch(term, per_disease * 3, window, sort="pub_date")
        new = [p for p in ids if f"PMID:{p}" not in gb.nodes][:per_disease]
        searched[did] = (f"{term} [pdat >= {since}]", n)
        jobs += [(ctx, p) for p in new]
    kept = Counter()
    _run_papers_counted(gb, jobs, st, mechs, kept, submitted_by="refresh",
                        checkpoint=(lambda: save_graph(gb, out)) if own else None)
    for did, (term, n) in searched.items():
        gb.covered(did, "pubmed_llm", term, n, kept[did])
    if own:
        save_graph(gb, out)
        _write_report({"refresh": st.as_dict()}, out)
    res = _report("https://pubmed.ncbi.nlm.nih.gov/", "refresh", st, len({p for _, p in jobs}))
    res["since"] = since
    return res


# ---------------------------------------------------------------- report + CLI
def _write_report(parts: dict, out: Path = OUT):
    path = out / "extract_report.json"
    rep = {}
    if path.exists():
        try:
            rep = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            rep = {}
    for k, v in parts.items():
        rep[k] = {**v, "run_date": date.today().isoformat()}
    path.write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")


def _summary(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in ("kept_examples", "drop_examples")}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m atlas.extract")
    ap.add_argument("cmd", choices=["papers", "pages", "all", "add", "refresh"])
    ap.add_argument("--per-disease", type=int, default=10)
    ap.add_argument("--diseases", default=None, help="comma-separated disease ids")
    ap.add_argument("--pages", default=str(PAGES_PATH))
    ap.add_argument("--text", default=None)
    ap.add_argument("--url", default=None)
    ap.add_argument("--by", default="anonymous")
    ap.add_argument("--since", default=None)
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)
    out = Path(a.out)
    dids = a.diseases.split(",") if a.diseases else None
    if a.cmd == "add":
        res = add_document(text=a.text, url=a.url, submitted_by=a.by, disease_ids=dids, out=out)
    elif a.cmd == "refresh":
        res = refresh_papers(since=a.since, disease_ids=dids, per_disease=a.per_disease, out=out)
    else:
        gb = GraphBuilder.from_csv(out)
        checkpoint = lambda: save_graph(gb, out)  # noqa: E731 — crash safety: finished work is on disk
        parts = {}
        if a.cmd in ("papers", "all"):
            parts["pubmed_llm"] = extract_papers(gb, dids, a.per_disease, checkpoint=checkpoint)
            save_graph(gb, out)
            _write_report({"pubmed_llm": parts["pubmed_llm"]}, out)
        if a.cmd in ("pages", "all"):
            parts["patient_groups"] = extract_pages(gb, a.pages, checkpoint=checkpoint)
            save_graph(gb, out)
            _write_report({"patient_groups": parts["patient_groups"]}, out)
        res = {k: _summary(v) for k, v in parts.items()}
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return res


if __name__ == "__main__":
    main()
