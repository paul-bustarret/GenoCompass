"""Offline tests for the RePORTER grant filter (atlas.sources.grant_match)."""
from atlas import sources as S
from atlas.graph import GraphBuilder

SUB = {"A": ("MONDO:0009655", "SGSH"), "B": ("MONDO:0009656", "NAGLU"),
       "C": ("MONDO:0009657", "HGSNAT"), "D": ("MONDO:0009658", "GNS")}


def graph() -> GraphBuilder:
    gb = GraphBuilder()
    for letter, (did, gene) in SUB.items():
        gb.node(did, "disease", f"Mucopolysaccharidosis type III{letter}", short=f"MPS III{letter}",
                synonyms=[f"MPS III{letter}", f"Sanfilippo syndrome type {letter}", f"Sanfilippo syndrome {letter.lower()}"])
        gb.node(f"NCBIGene:{gene}", "gene", gene)
        gb.edge(did, f"NCBIGene:{gene}", "caused_by", "hpo", "https://x")
    gb.node("MONDO:0010526", "disease", "Fabry disease", short="Fabry", synonyms=["FD", "Fabry"])
    gb.node("NCBIGene:GLA", "gene", "GLA")
    gb.edge("MONDO:0010526", "NCBIGene:GLA", "caused_by", "hpo", "https://x")
    gb.node("MONDO:0010100", "disease", "Tay-Sachs disease", short="Tay-Sachs", synonyms=["TSD"])
    gb.node("MONDO:0008769", "disease", "Ceroid lipofuscinosis, neuronal, 2", short="CLN2")
    return gb


def match(did, title, abstract=""):
    return S.grant_match(S.grant_terms(graph()), did, title, abstract)


def test_fabry_perot_optics_rejected():
    assert match("MONDO:0010526", "Optical microcavities", "We build Fabry-Pérot resonators. Fabry–Perot cavities.") is None
    assert match("MONDO:0010526", "Pain in Fabry Disease") == "Pain in Fabry Disease"


def test_unrelated_grant_rejected():
    assert match("MONDO:0010100", "Cannabis, Depression and Neurobiological Function",
                 "We study THC in youth. TSD is not mentioned as a disease here.") is None


def test_quote_is_the_matched_sentence_and_ends_on_a_word():
    q = match("MONDO:0010100", "Sphingolipid biology", "First sentence. A humanized late-onset Tay-Sachs model "
              "carrying HEXA was made. More words follow here." * 3)
    assert q == "A humanized late-onset Tay-Sachs model carrying HEXA was made."
    long_ = match("MONDO:0010100", "Tay-Sachs " + "abcdefghij " * 40)
    assert len(long_) <= 300 and long_.endswith("abcdefghij")


def test_sanfilippo_subtype_gene_links_only_that_subtype():
    t, a = "AAV8 codon optimized NAGLU vector for Sanfilippo syndrome", ""
    assert match(SUB["B"][0], t, a)
    for k in "ACD":
        assert match(SUB[k][0], t, a) is None


def test_generic_sanfilippo_links_all_four_when_no_subtype_named():
    t = "Peripheral nervous system dysfunction in Sanfilippo syndrome"
    assert all(match(did, t) for did, _ in SUB.values())
    # 'Sanfilippo syndrome, a ...' must not read as subtype A
    assert all(match(did, "Sanfilippo syndrome, a fatal disease") for did, _ in SUB.values())
    t2 = "Mucopolysaccharidosis type IIIA (MPS IIIA), also known as Sanfilippo syndrome type A."
    assert match(SUB["A"][0], "x", t2) and match(SUB["B"][0], "x", t2) is None


def test_yeast_cln_cyclins_are_not_batten_disease():
    assert match("MONDO:0008769", "Cyclin C kinases", "Saccharomyces cerevisiae lacking CLN1, CLN2 and CLN3 genes.") is None
    assert match("MONDO:0008769", "Enteric nervous system damage in CLN2 disease")
