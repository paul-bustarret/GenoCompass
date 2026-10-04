"""Offline tests for the read side (atlas.api, atlas.server). The LLM is always faked."""
import pytest

from atlas import api, llm

JOURNEY_KEYS = {"status", "disease", "user_country", "profile", "similar", "lookalikes", "organizations",
                "trials", "assets", "researchers", "next_steps", "cards", "searched", "missing",
                "generated_by"}


@pytest.fixture(scope="module")
def tay_sachs():
    r = api.search("Tay-Sachs")
    assert r["status"] == "ok"
    return r["matches"][0]["id"]


def any_org():
    st = api._state()
    return next(n for n, d in st["g"].nodes(data=True) if d["type"] == "organization")


def test_search_sanfilippo_is_ambiguous():
    r = api.search("sanfilippo")
    assert r["status"] == "ambiguous"
    names = {m["name"] for m in r["matches"] if m["type"] == "disease"}
    assert {"MPS IIIA", "MPS IIIB", "MPS IIIC", "MPS IIID"} <= names


def test_search_exact_and_not_found(tay_sachs):
    assert api.search(tay_sachs)["matches"][0]["matched_on"] == "id"
    assert api.search("qqqzzz")["status"] == "not_found"


def test_subgraph_respects_max_nodes_and_real_edges(tay_sachs):
    st = api._state()
    for focus in ("all", "similar", "assets", "people"):
        sg = api.subgraph(tay_sachs, focus=focus, max_nodes=20)
        assert len(sg["nodes"]) <= 20 and sg["nodes"][0]["id"] == tay_sachs
        ids = {n["id"] for n in sg["nodes"]}
        for e in sg["edges"]:
            assert e["id"] in st["edges"]
            assert e["source"] in ids and e["target"] in ids
            assert "" not in e.values()
    assert api.subgraph(tay_sachs, max_nodes=20)["truncated"] is True


def test_journey_shape(tay_sachs):
    j = api.journey(tay_sachs, country="IN")
    assert set(j) >= JOURNEY_KEYS
    assert j["cards"] == [] and j["user_country"] == "IN"
    assert set(j["profile"]) == {"genes", "mechanisms", "pathways", "top_phenotypes", "variants"}
    for s in j["similar"]:
        assert not s["lookalike"] and set(s["components"]) >= {"gene", "pathway", "phenotype"}
    for t in j["trials"]:
        assert isinstance(t["countries"], list) and isinstance(t["near_user"], bool)
    for step in j["next_steps"]:
        assert step["kind"] in {"reuse", "build", "contact", "join"}


def test_unknown_id_raises():
    with pytest.raises(api.NotFound):
        api.journey("MONDO:nope")


def test_recommend_drops_uncited_and_invented(monkeypatch, tay_sachs):
    j = api.journey(tay_sachs)
    real = api._collect_edge_ids(api._compact_journey(j))[0]

    def fake(system, user, schema=None, tier="fast", timeout=300):
        return {"cards": [
            {"key": "whats_going_on", "title": "t",
             "text": f"Good sentence [{real}]. Uncited sentence. Invented one [Edeadbee]."},
            {"key": "similar", "title": "t", "text": f"Mixed [{real}, Ebadbad1]."},
            {"key": "what_exists", "title": "t", "text": "Nothing cited."},
            {"key": "next_step", "title": "t", "text": f"Talk to them. [{real}]"}]}
    monkeypatch.setattr(llm, "complete", fake)
    r = api.recommend(tay_sachs)
    cards = {c["key"]: c for c in r["cards"]}
    assert cards["whats_going_on"]["text"] == f"Good sentence [{real}]."
    assert cards["whats_going_on"]["citations"] == [real]
    assert cards["similar"]["text"] == f"Mixed [{real}]."
    assert cards["what_exists"]["text"] == api.NO_EVIDENCE and cards["what_exists"]["citations"] == []
    assert cards["what_exists"]["status"] == "no_supported_evidence"
    assert cards["whats_going_on"]["status"] == "ok"
    assert all(c["text"] for c in r["cards"])
    assert real in cards["next_step"]["citations"]  # own-community steps may be prepended
    assert r["generated_by"]["backend"]


def test_draft_email_never_invents_to(monkeypatch, tay_sachs):
    org = any_org()

    def fake(system, user, schema=None, tier="fast", timeout=300):
        return {"subject": "Hello", "body": "Dear team,\n\nWrite to fake@invented.org. Claim [Edeadbee].\n\nBest"}
    monkeypatch.setattr(llm, "complete", fake)
    r = api.draft_email(org, tay_sachs, {"name": "Maria", "role": "chair", "context": "x"})
    st = api._state()
    assert r["to"] == api._attr(st["g"], org, "contact_email")
    if r["to"] is None:
        assert any("contact email" in w for w in r["warnings"])
    assert "Edeadbee" not in r["body"] and r["citations"] == []
    assert r["requires_human_review"] is True


def test_chat_out_of_scope(monkeypatch, tay_sachs):
    monkeypatch.setattr(llm, "complete", lambda *a, **k: {
        "answer": "The atlas does not contain this.", "answerable_from_graph": False})
    r = api.chat(tay_sachs, [{"role": "user", "content": "What is the weather?"}])
    assert r["grounded"] is False and r["out_of_scope"] is True and r["citations"] == []


def test_chat_grounded_validates(monkeypatch, tay_sachs):
    edge_id = api._chat_edges(api._state(), tay_sachs)[0]["id"]
    monkeypatch.setattr(llm, "complete", lambda *a, **k: {
        "answer": f"Yes [{edge_id}]. Made up [Edeadbee].", "answerable_from_graph": True})
    r = api.chat(tay_sachs, [{"role": "user", "content": "q"}])
    assert r["grounded"] is True and r["citations"] == [edge_id] and "Edeadbee" not in r["answer"]


def test_server_endpoints(tay_sachs):
    from fastapi.testclient import TestClient

    from atlas.server import app
    c = TestClient(app)
    assert c.get("/health").json()["status"] == "ok"
    assert c.get("/search", params={"q": "sanfilippo"}).json()["status"] == "ambiguous"
    j = c.get(f"/journey/{tay_sachs}", params={"country": "IN"}).json()
    assert set(j) >= JOURNEY_KEYS
    assert c.get("/journey/MONDO:nope").status_code == 404


def test_tables_mirror_supabase_rows(tay_sachs):
    from fastapi.testclient import TestClient

    from atlas.server import app
    c = TestClient(app)
    nodes = c.get("/tables/nodes", params={"type": "disease"}).json()
    ts = next(n for n in nodes if n["id"] == tay_sachs)
    assert set(ts) == {"id", "type", "name", "synonyms", "attrs"} and isinstance(ts["attrs"], dict)
    assert all(n["type"] == "disease" for n in nodes)
    edges = c.get("/tables/edges", params={"relation": "caused_by,studies"}).json()
    assert edges and {e["relation"] for e in edges} <= {"caused_by", "studies"}
    assert {"edge_id", "src", "dst", "quote", "source_url"} <= set(edges[0])
    sim = c.get("/tables/similarity").json()
    assert sim and isinstance(sim[0]["therapeutic"], float) and isinstance(sim[0]["lookalike"], bool)
    clusters = c.get("/tables/clusters").json()
    assert any(r["disease_id"] == tay_sachs and isinstance(r["cluster"], int) for r in clusters)
    assert c.get("/tables/coverage").json()
    assert c.get("/tables/secrets").status_code == 404
    assert c.get("/tables/nodes", params={"bogus": "x"}).status_code == 400


# ---- answer-quality regressions ------------------------------------------------------------
def test_check_email_rejects_invalid_webmail_and_deprioritises_fundraising():
    assert api.check_email("[email protected]")[0] is None and api.check_email("[email protected]")[1]
    assert api.check_email("someone@gmail.com")[0] is None
    assert api.check_email("x@yahoo.es")[0] is None
    assert api.check_email("info@ntsad.org") == ("info@ntsad.org", None, False)
    assert api.check_email("fundraising@x.org")[2] is True
    assert api.check_email("fundraising@x.org, info@x.org")[0] == "info@x.org"
    assert api.check_email(None) == (None, None, False)


def _row(i, kind, d, email):
    return {"id": f"ORG:t{i}", "name": f"t{i}", "kind": kind, "for_disease": d, "contact_email": email,
            "country": None, "website": None, "edges": []}


def test_choose_recipient_order(monkeypatch):
    d, s1 = "MONDO:A", "MONDO:B"
    monkeypatch.setattr(api, "_direct_studies", lambda g, o, dd: o in {"ORG:t2", "ORG:t5"})
    j = {"disease": {"id": d}, "similar": [{"id": s1}], "organizations": [
        _row(1, "patient_group", s1, "info@b.org"),          # similar-disease group
        _row(2, "patient_group", d, "fundraising@a.org"),    # own group, but fundraising@ only
        _row(3, "funder", d, "info@funder.org"),             # own org (not a group)
        _row(4, "patient_group", d, "[email protected]"),  # invalid
        _row(5, "patient_group", d, "info@a.org"),           # own group, studies edge, role email
    ]}
    org, why = api.choose_recipient(j)
    assert org["id"] == "ORG:t5" and "studies edge" in why
    j["organizations"].pop()
    org, why = api.choose_recipient(j)
    assert org["id"] == "ORG:t3"  # any org of this disease beats a similar disease's group / fundraising@
    j["organizations"].pop(2)
    org, why = api.choose_recipient(j)
    assert org["id"] == "ORG:t1" and "related community" in why
    j["organizations"].pop(0)
    org, _ = api.choose_recipient(j)
    assert org["id"] == "ORG:t2"  # fundraising@ only when nothing else has an email


def test_draft_email_auto_recipient_and_reason(monkeypatch, tay_sachs):
    seen = {}

    def fake(system, user, schema=None, tier="fast", timeout=300):
        seen["user"] = user
        return {"subject": "Hello", "body": "Dear team,\n\nBest"}
    monkeypatch.setattr(llm, "complete", fake)
    r = api.draft_email(None, tay_sachs, {"name": "Maria", "role": "chair", "context": "x"})
    assert r["recipient_reason"]
    if r["to"]:
        local = r["to"].split("@")[0].lower()
        assert local not in api.ROLE_SKIP and api.check_email(r["to"])[0] == r["to"]
    j = api.journey(tay_sachs)
    other = next((o for o in j["organizations"] if o["for_disease"] != tay_sachs), None)
    if other:
        r2 = api.draft_email(other["id"], tay_sachs, {})
        assert "related community" in r2["recipient_reason"] and "related disease" in seen["user"]


def test_journey_hygiene_and_own_community_first(tay_sachs):
    j = api.journey(tay_sachs, country="IN")
    for key in ("genes", "mechanisms", "pathways", "top_phenotypes", "variants"):
        ids = [x["id"] for x in j["profile"][key]]
        assert len(ids) == len(set(ids)), key
    for key in ("organizations", "trials", "assets", "researchers"):
        ids = [x["id"] for x in j[key]]
        assert len(ids) == len(set(ids)), key
    own_groups = [o for o in j["organizations"] if o["for_disease"] == tay_sachs and o["kind"] == "patient_group"]
    if own_groups:
        first = j["next_steps"][0]
        assert first["kind"] in ("contact", "join") and first["from_disease"] == tay_sachs
    kinds = [s["kind"] for s in j["next_steps"]]
    if "build" in kinds:
        assert all(k == "build" for k in kinds[kinds.index("build"):])
    g = api._state()["g"]
    for a, _ in api._in(g, tay_sachs, "studies"):
        if g.nodes[a]["type"] == "asset":
            assert a in {x["id"] for x in j["assets"]}
    rank = {"RECRUITING": 0, "NOT_YET_RECRUITING": 1, "ENROLLING_BY_INVITATION": 2, "ACTIVE_NOT_RECRUITING": 3}
    own = [t for t in j["trials"] if t["for_disease"] == tay_sachs]
    keys = [(rank.get(t["status"] or "", 9), not t["near_user"]) for t in own]
    assert keys == sorted(keys)


def test_missing_uses_existing_coverage_rows(monkeypatch):
    st = {"coverage": [
        {"disease_id": "D", "source": "patient_groups", "query": "web", "n_results": "-1", "n_kept": "0"},
        {"disease_id": "D", "source": "patient_groups", "query": "url", "n_results": "1", "n_kept": "4"},
        {"disease_id": "D", "source": "ctgov", "query": "q", "n_results": "0", "n_kept": "0"},
        {"disease_id": "X", "source": "reporter", "query": "q", "n_results": "3", "n_kept": "1"}]}
    _, missing = api._searched(st, "D")
    assert missing == ["ctgov: searched, none found", "reporter: not searched yet"]


def test_weak_grant_sentences_dropped():
    import networkx as nx
    g = nx.MultiDiGraph()
    g.add_node("D", type="disease", name="Tay-Sachs disease", synonyms="TSD", attrs={"short": "Tay-Sachs"})
    g.add_node("G1", type="grant", name="g1", attrs={})
    weak = {"edge_id": "Eweak1", "src": "G1", "dst": "D", "relation": "studies",
            "quote": "Text search 'Tay-Sachs' matched: Gene therapy for Sanfilippo"}
    good = {**weak, "edge_id": "Egood1", "quote": "Text search 'Tay-Sachs' matched: Models of Tay-Sachs disease"}
    assert api._weak_grant_edge(g, weak) and not api._weak_grant_edge(g, good)
    text, cites = api.validate_cited("Weak claim [Eweak1]. Good claim [Egood1]. Mixed [Eweak1, Egood1].",
                                     {"Eweak1", "Egood1"}, weak={"Eweak1"})
    assert text == "Good claim [Egood1]. Mixed [Eweak1, Egood1]." and "Eweak1" in cites


def test_recommend_next_step_leads_with_own_community(monkeypatch, tay_sachs):
    j = api.journey(tay_sachs)
    own = [s for s in j["next_steps"] if s["kind"] in ("contact", "join") and s["from_disease"] == tay_sachs]
    other = next((s for s in j["next_steps"] if s["from_disease"] not in (None, tay_sachs) and s["edges"]), None)
    if not own or not other:
        pytest.skip("graph has no own-community step")
    seen = {}

    def fake(system, user, schema=None, tier="fast", timeout=300):
        seen["user"] = user
        return {"cards": [{"key": "next_step", "title": "t", "text": f"Contact them [{other['edges'][0]}]."}]}
    monkeypatch.setattr(llm, "complete", fake)
    r = api.recommend(tay_sachs)
    card = r["cards"][0]
    assert set(own[0]["edges"][:4]) <= set(card["citations"])
    assert card["text"].index(own[0]["edges"][0]) < card["text"].index(other["edges"][0])
    assert "NEXT STEPS IN PRIORITY ORDER" in seen["user"]


def test_stats_counts_match_graph():
    from atlas import api
    st = api.stats()
    assert st["diseases"] == 25 and st["diseases_lysosomal"] + st["diseases_controls"] == 25
    assert st["edges"] == sum(st["edges_by_evidence"].values()) and st["nodes"] > st["edges"] / 10
    assert len(st["sources"]) == 8 and st["papers"] > 0 and st["countries"] > 0
