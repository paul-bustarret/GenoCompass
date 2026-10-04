"""Offline tests for the LLM write side (atlas.extract); the LLM is monkeypatched."""
import csv

import pytest

import atlas.llm
from atlas import extract as X
from atlas.graph import GraphBuilder

DID = "MONDO:0009655"
ABSTRACT = (
    "Sanfilippo syndrome type A (MPS IIIA) is caused by a deficiency of the lysosomal enzyme "
    "heparan N-sulfatase encoded by SGSH. Accumulation of heparan sulfate drives microglial activation "
    "in the MPS IIIA mouse brain. In contrast, treatment with genistein did not reduce heparan sulfate "
    "storage in MPS IIIA patients. Unrelated sentence about the weather in Boston this spring."
)
CLAIMS = [
    {"relation": "has_mechanism", "object": "substrate_storage_heparan_sulfate", "polarity": "supports",
     "effect": None, "context": "animal_model", "intervention_kind": None, "confidence": 0.9,
     "quote": "Accumulation of heparan sulfate drives microglial activation in the MPS IIIA mouse brain."},
    {"relation": "studied_with", "object": "genistein", "polarity": "contradicts", "effect": None,
     "context": "human", "intervention_kind": "drug", "confidence": 0.7,
     "quote": "In contrast, treatment with genistein did not reduce heparan sulfate storage in MPS IIIA patients."},
    {"relation": "caused_by", "object": "SGSH", "polarity": "supports", "effect": "LoF", "context": "human",
     "intervention_kind": None, "confidence": 0.95,
     "quote": "Sanfilippo syndrome type A (MPS IIIA) is caused by a deficiency of the lysosomal enzyme "
              "heparan N-sulfatase encoded by SGSH."},
]


def tiny_graph() -> GraphBuilder:
    gb = GraphBuilder()
    gb.node(DID, "disease", "Mucopolysaccharidosis type IIIA", synonyms=["MPS IIIA", "Sanfilippo A"],
            short="MPS IIIA", omim="OMIM:252900", role="hero")
    gb.node("NCBIGene:6448", "gene", "SGSH")
    gb.edge(DID, "NCBIGene:6448", "caused_by", "hpo", "https://ontology.jax.org/x")
    gb.node("INTERVENTION:UX111", "intervention", "UX111", kind="biological")
    return gb


@pytest.fixture
def graph_dir(tmp_path):
    tiny_graph().write(tmp_path)
    return tmp_path


@pytest.fixture
def fake_llm(monkeypatch):
    calls = []

    def fake(system, user, schema=None, tier="fast", timeout=300):
        calls.append(user)
        if schema is X.CONTRA_SCHEMA:  # second-pass contradiction check
            return {"is_contradiction": "did not" in user, "reason": "test"}
        return {"claims": CLAIMS}
    monkeypatch.setattr(atlas.llm, "complete", fake)
    return calls


def ctx(gb):
    return X.disease_contexts(gb)[DID]


def test_quote_check_drops_fabricated_quote():
    gb, st = tiny_graph(), X.Stats()
    claim = {**CLAIMS[0], "quote": "Heparan sulfate storage in MPS IIIA is reversed by gene therapy in all patients."}
    X.apply_claims(gb, st, ctx(gb), [claim], ABSTRACT, "pubmed", "u", X.load_mechanisms())
    assert st.dropped["quote_check"] == 1 and not st.kept


def test_relevance_drops_off_topic_quote():
    gb, st = tiny_graph(), X.Stats()
    claim = {**CLAIMS[0], "object": "neuronal_loss",
             "quote": "Unrelated sentence about the weather in Boston this spring."}
    X.apply_claims(gb, st, ctx(gb), [claim], ABSTRACT, "pubmed", "u", X.load_mechanisms())
    assert st.dropped["relevance"] == 1 and not st.kept


def test_invalid_slug_and_unknown_gene_dropped():
    gb, st = tiny_graph(), X.Stats()
    bad = [{**CLAIMS[0], "object": "made_up_slug"}, {**CLAIMS[2], "object": "NOTAGENE"}]
    X.apply_claims(gb, st, ctx(gb), bad, ABSTRACT, "pubmed", "u", X.load_mechanisms())
    assert st.dropped["invalid_type"] == 1 and st.dropped["unresolved_entity"] == 1


def test_contact_email_nulled_when_not_in_page():
    gb, st = tiny_graph(), X.Stats()
    page = X.html_to_text("<html><script>var e='x@y.org'</script><body><p>The Sanfilippo Hope Foundation "
                          "supports families living with MPS IIIA in the UK.</p>"
                          "<a href='mailto:hello@sanhope.org.uk'>Email us</a></body></html>")
    orgs = [{"name": "Sanfilippo Hope Foundation", "kind": "patient_group", "country": "GB", "website": None,
             "contact_email": "info@sanhope.org", "diseases": ["MPS IIIA — Mucopolysaccharidosis type IIIA"],
             "assets": [], "quote": "The Sanfilippo Hope Foundation supports families living with MPS IIIA in the UK."}]
    X.apply_orgs(gb, st, orgs, page, "https://example.org", X.disease_contexts(gb))
    org = gb.nodes["ORG:Sanfilippo_Hope_Foundation"]
    assert org["attrs"]["contact_email"] is None
    assert "x@y.org" not in page  # scripts stripped
    # an email present via mailto is kept
    gb2, st2 = tiny_graph(), X.Stats()
    X.apply_orgs(gb2, st2, [{**orgs[0], "contact_email": "hello@sanhope.org.uk"}], page, "https://example.org",
                 X.disease_contexts(gb2))
    assert gb2.nodes["ORG:Sanfilippo_Hope_Foundation"]["attrs"]["contact_email"] == "hello@sanhope.org.uk"
    rels = {e["relation"] for e in gb2.edges.values() if e["src"].startswith("ORG:")}
    assert rels == {"studies", "based_in"}


def _edges(path):
    with open(path / "edges.csv") as f:
        return {r["edge_id"]: r for r in csv.DictReader(f)}


def test_add_document_adds_mechanism_and_contradiction_without_touching_existing(graph_dir, fake_llm):
    before = _edges(graph_dir)
    res = X.add_document(text=ABSTRACT, submitted_by="tester", out=graph_dir)
    after = _edges(graph_dir)
    assert all(after[k] == v for k, v in before.items())  # nothing overwritten
    added = [after[e] for e in res["added_edges"]]
    assert any(e["relation"] == "has_mechanism" and e["dst"] == "MECH:substrate_storage_heparan_sulfate"
               for e in added)
    assert len(res["contradictions"]) == 1
    contra = after[res["contradictions"][0]]
    assert contra["polarity"] == "contradicts" and contra["dst"] == "INTERVENTION:genistein"
    assert all(e["submitted_by"] == "tester" and e["evidence"] == "extracted" for e in added)
    assert all(float(e["confidence"]) <= 0.8 for e in added)
    assert res["dropped"] == {"quote_check": 0, "relevance": 0, "unresolved_entity": 0, "invalid_type": 0}
    assert "MECH:substrate_storage_heparan_sulfate" in res["new_nodes"]


def test_add_document_is_idempotent(graph_dir, fake_llm):
    first = X.add_document(text=ABSTRACT, submitted_by="tester", out=graph_dir)
    snap = _edges(graph_dir)
    second = X.add_document(text=ABSTRACT, submitted_by="tester", out=graph_dir)
    assert second["added_edges"] == [] and _edges(graph_dir) == snap
    ids = {X._eid(DID, e["dst"], e["relation"], e["source_url"]) for e in snap.values() if e["evidence"] == "extracted"}
    assert set(first["added_edges"]) == ids


def test_add_document_without_matching_disease(graph_dir, fake_llm):
    res = X.add_document(text="A study of cats and dogs in the park.", out=graph_dir)
    assert res["added_edges"] == [] and "no slice disease" in res["reason"] and not fake_llm


def test_paper_run_checkpoints_and_is_idempotent(graph_dir, fake_llm, monkeypatch):
    monkeypatch.setattr(X, "CHECKPOINT_EVERY", 2)
    monkeypatch.setattr(X, "fetch_abstract", lambda pmid: ABSTRACT)
    monkeypatch.setattr(X, "pubmed_summaries", lambda pmids: {p: {"title": f"MPS IIIA paper {p}"} for p in pmids})
    writes = []

    def run():
        gb = GraphBuilder.from_csv(graph_dir)
        c = X.disease_contexts(gb)[DID]
        st = X.Stats()
        X._run_papers(gb, [(c, "111"), (c, "222"), (c, "333")], st, X.load_mechanisms(),
                      checkpoint=lambda: (writes.append(1), X.save_graph(gb, graph_dir)))
        return st
    st1 = run()
    assert len(writes) == 2 and st1.c["papers_read"] == 3 and st1.kept
    snap = _edges(graph_dir)
    assert all(e["source_url"].startswith("https://pubmed.ncbi.nlm.nih.gov/") for e in snap.values()
               if e["evidence"] == "extracted")
    st2 = run()
    assert st2.added_edges == [] and _edges(graph_dir) == snap


def test_disease_matched_by_name_for_pages_without_omim():
    gb = tiny_graph()
    assert X._match_disease_by_name(X.disease_contexts(gb), "Mucopolysaccharidosis type IIIA (Sanfilippo A)") == DID


def test_unconfirmed_contradiction_becomes_low_confidence_support(monkeypatch):
    seen = []

    def fake(system, user, schema=None, tier="fast", timeout=300):
        seen.append(schema)
        return {"is_contradiction": False, "reason": "limitation, not a refutation"}
    monkeypatch.setattr(atlas.llm, "complete", fake)
    gb, st = tiny_graph(), X.Stats()
    text = ABSTRACT + " Genistein is not curative and has limitations in MPS IIIA patients."
    claim = {**CLAIMS[1], "quote": "Genistein is not curative and has limitations in MPS IIIA patients."}
    X.apply_claims(gb, st, ctx(gb), [claim], text, "pubmed", "u", X.load_mechanisms())
    e = gb.edges[st.kept[0]]
    assert seen == [X.CONTRA_SCHEMA] and e["polarity"] == "supports" and e["confidence"] <= 0.5
    assert not st.contradictions


def test_long_quote_is_cut_at_a_word_boundary(monkeypatch):
    gb, st = tiny_graph(), X.Stats()
    q = "Accumulation of heparan sulfate in MPS IIIA " + "drives microglial activation " * 12 + "in mice."
    claim = {**CLAIMS[0], "quote": q}
    X.apply_claims(gb, st, ctx(gb), [claim], q, "pubmed", "u", X.load_mechanisms())
    stored = gb.edges[st.kept[0]]["quote"]
    assert len(stored) <= 300 and q.startswith(stored) and q[len(stored)] == " "


def test_cloudflare_email_decoded_and_placeholder_never_stored():
    key, addr = 0x42, "info@ntsad.org"
    hexs = f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in addr)
    page = X.html_to_text(f"<p>The Sanfilippo Hope Foundation supports families living with MPS IIIA.</p>"
                          f'<a href="/cdn-cgi/l/email-protection" class="__cf_email__" data-cfemail="{hexs}">'
                          f"[email&#160;protected]</a>")
    assert addr in page and "protected]" not in page
    assert X.choose_email("[email protected]", page) == (None, None)
    assert X.choose_email(addr, page) == (addr, None)


def test_webmail_withheld_and_fundraising_address_avoided():
    page = "Contact: jane.doe@gmail.com, fundraising@sanhope.org, info@sanhope.org"
    assert X.choose_email("jane.doe@gmail.com", page) == (None, "personal address on page withheld")
    assert X.choose_email("fundraising@sanhope.org", page)[0] == "info@sanhope.org"
    assert X.choose_email("fundraising@other.org", "only fundraising@other.org here") == ("fundraising@other.org", None)


# ---------------------------------------------------------------- contribute page (POST /documents)
def test_parse_pmid_from_pubmed_url_or_bare_id():
    assert X.parse_pmid("https://pubmed.ncbi.nlm.nih.gov/41819452/") == "41819452"
    assert X.parse_pmid("pubmed.ncbi.nlm.nih.gov/41819452") == "41819452"
    assert X.parse_pmid(" 41819452 ") == "41819452"
    assert X.parse_pmid("PMID: 41819452") == "41819452"
    assert X.parse_pmid(None, "41819452") == "41819452"
    assert X.parse_pmid(None, "MPS IIIA abstract mentioning 41819452 somewhere") is None
    assert X.parse_pmid("https://example.org/page") is None


def test_pubmed_submission_fetches_abstract_and_makes_one_extraction_call(graph_dir, fake_llm, monkeypatch):
    fetched = []
    monkeypatch.setattr(X, "fetch_abstract", lambda pmid: fetched.append(pmid) or ABSTRACT)
    monkeypatch.setattr(X, "pubmed_summaries", lambda pmids: {p: {"title": "Genistein in MPS IIIA"} for p in pmids})
    res = X.add_document(url="https://pubmed.ncbi.nlm.nih.gov/12345678/", submitted_by="Dr Test, neurologist",
                         out=graph_dir)
    assert fetched == ["12345678"]
    extraction = [u for u in fake_llm if u.startswith("Target disease:")]
    assert len(extraction) == 1 and len(fake_llm) == 2  # one extraction + one contradiction check
    assert res["source_url"] == "https://pubmed.ncbi.nlm.nih.gov/12345678/"
    assert res["title"] == "Genistein in MPS IIIA"
    assert res["matched_diseases"] == [{"id": DID, "name": "Mucopolysaccharidosis type IIIA", "short": "MPS IIIA"}]
    by_target = {a["target"]: a for a in res["added"]}
    assert set(by_target) == {"MECH:substrate_storage_heparan_sulfate", "INTERVENTION:genistein", "NCBIGene:6448"}
    g = by_target["INTERVENTION:genistein"]
    assert g["relation"] == "studied_with" and g["polarity"] == "contradicts" and g["target_name"] == "genistein"
    assert g["source_name"] == "MPS IIIA" and g["quote"].startswith("In contrast, treatment with genistein")
    assert by_target["NCBIGene:6448"]["target_name"] == "SGSH"
    assert {"id": "PMID:12345678", "type": "paper", "name": "Genistein in MPS IIIA"} in res["new_node_details"]
    assert res["claims_proposed"] == 3 and res["rejected"] == [] and "reason" not in res
    edges = _edges(graph_dir)
    assert all(edges[a["id"]]["submitted_by"] == "Dr Test, neurologist" for a in res["added"])


def test_auto_detect_targets_only_the_best_matching_disease(graph_dir, fake_llm):
    gb = GraphBuilder.from_csv(graph_dir)
    gb.node("MONDO:0010000", "disease", "Mucopolysaccharidosis type IIIB", synonyms=["MPS IIIB"], short="MPS IIIB")
    gb.write(graph_dir)
    text = ABSTRACT + " MPS IIIB is mentioned once."
    res = X.add_document(text=text, out=graph_dir)
    assert [d["id"] for d in res["matched_diseases"]] == [DID]
    assert len([u for u in fake_llm if u.startswith("Target disease:")]) == 1


def test_rejected_claims_are_reported_with_reason(graph_dir, monkeypatch):
    bad = {**CLAIMS[0], "quote": "Gene therapy cured every MPS IIIA patient in the trial."}
    monkeypatch.setattr(atlas.llm, "complete", lambda *a, **k: {"claims": [bad, CLAIMS[2]]})
    res = X.add_document(text=ABSTRACT, out=graph_dir)
    assert res["dropped"]["quote_check"] == 1 and len(res["added"]) == 1
    assert res["rejected"] == [{"reason": "quote_check", "relation": "has_mechanism",
                                "object": "substrate_storage_heparan_sulfate",
                                "quote": "Gene therapy cured every MPS IIIA patient in the trial."}]


def test_unknown_pmid_is_reported_not_raised(graph_dir, fake_llm, monkeypatch):
    monkeypatch.setattr(X, "fetch_abstract", lambda pmid: "")
    monkeypatch.setattr(X, "pubmed_summaries", lambda pmids: {p: {"error": "cannot get document summary"} for p in pmids})
    res = X.add_document(url="99999999", out=graph_dir)
    assert res["added_edges"] == [] and "no abstract" in res["reason"] and not fake_llm


def test_contributions_lists_submitted_findings_by_name(graph_dir, fake_llm, monkeypatch):
    from atlas import api
    X.add_document(text=ABSTRACT, submitted_by="tester", out=graph_dir)
    api._STATE.clear()
    monkeypatch.setattr(api, "_state", lambda out=graph_dir, _f=api._state: _f(graph_dir))
    rows = api.contributions(DID)["contributions"]
    assert len(rows) == 3 and {"SGSH", "genistein"} <= {r["target_name"] for r in rows}
    assert all(r["submitted_by"] == "tester" and r["quote"] for r in rows)
    with pytest.raises(api.NotFound):
        api.contributions("NCBIGene:6448")
    api._STATE.clear()


def test_fast_tier_runs_claude_without_thinking(monkeypatch, tmp_path):
    import json as _json
    import subprocess
    envs = []

    def fake_run(cmd, **kw):
        envs.append(kw.get("env"))
        return subprocess.CompletedProcess(cmd, 0, _json.dumps({"is_error": False, "structured_output": {"a": 1}}), "")
    monkeypatch.setattr(atlas.llm, "CACHE", tmp_path)
    monkeypatch.setattr(atlas.llm.subprocess, "run", fake_run)
    monkeypatch.setenv("ATLAS_LLM", "claude")
    assert atlas.llm.complete("s", "u", schema={"type": "object"}, tier="fast", cache=False) == {"a": 1}
    atlas.llm.complete("s", "u", schema={"type": "object"}, tier="smart", cache=False)
    assert envs[0]["MAX_THINKING_TOKENS"] == "0" and envs[1] is None
