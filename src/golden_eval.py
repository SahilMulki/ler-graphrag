"""
golden_eval.py — the golden-question eval spec + scoring for Phase 6.

Two things the notes asked for:
  * Retrieval is scored SEPARATELY from answering. Each question carries a
    hand-specified expected set (node match_keys and/or LER numbers) derived from
    ground_truth.json / out/ via `build_expected()`, and we score whether the
    GraphRetriever surfaced them — independent of what the answer LLM then says.
  * Materialize-at-scale questions (Q2, Q13, Q14 grouping) are judged on honest
    behavior, NOT on declaring a graph-vs-vector "winner" that N=3 can't support.

`kind` drives the pass rule:
  showcase / aggregation  — retrieval must surface the expected nodes+LERs and the
                            answer must be grounded and cite the expected LERs
  scale                   — retrieval must behave honestly (thin/empty as designed);
                            no winner is claimed
  negative                — the no-hallucination test: answer must refuse (answerable
                            = false, no citations)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from load_graph import load_records

HPCI_LERS = {"254-2025-006-00", "237-2025-003-00", "353-2025-001-00"}


# --------------------------------------------------------------------------- #
# expected sets, drawn from the actual records (QC oracle + Dresden/Limerick out)
# --------------------------------------------------------------------------- #
def build_expected() -> dict:
    recs = {rec.ler_number: rec for rec, _ in load_records()}

    def keys(ler: str, types: tuple[str, ...]) -> set[str]:
        return {n.match_key for n in recs[ler].nodes if n.type in types}

    def union(types: tuple[str, ...]) -> set[str]:
        out: set[str] = set()
        for ler in HPCI_LERS:
            out |= keys(ler, types)
        return out

    return {
        "chain_limerick": keys("353-2025-001-00", ("FailureMode", "Consequence")),
        "chain_qc": keys("254-2025-006-00", ("FailureMode", "Consequence")),
        "hpci_components": union(("Component",)),
        "hpci_failure_modes": union(("FailureMode",)),
        "cause_categories": union(("Cause",)),
    }


# --------------------------------------------------------------------------- #
# the golden set (MVP-now subset from phase_0.md)
# --------------------------------------------------------------------------- #
def golden(expected: dict) -> list[dict]:
    return [
        # --- LEAD showcase: pipeline-extracted multi-hop (note 3) ---------------
        {"id": "Q1-Limerick", "kind": "showcase", "intent": "failure_chain",
         "provenance": "pipeline",
         "q": "What chain of failures led to HPCI being inoperable at Limerick?",
         "exp_nodes": expected["chain_limerick"], "exp_lers": {"353-2025-001-00"},
         "note": "primary multi-hop demo — this chain came from the extraction pipeline, "
                 "not a hand-labeled record."},

        {"id": "Q1-QuadCities", "kind": "showcase", "intent": "failure_chain",
         "provenance": "oracle",
         "q": "What chain of failures led to HPCI being inoperable at the Quad Cities "
              "Power Plant?",
         "exp_nodes": expected["chain_qc"], "exp_lers": {"254-2025-006-00"},
         "note": "golden Q1, but Quad Cities is loaded from its ORACLE record (few-shot "
                 "exemplar, no raw text) — flagged so the pipeline result (Limerick) leads."},

        # --- cross-document hubs (graph's structural advantage) -----------------
        {"id": "Q3", "kind": "showcase", "intent": "system_components",
         "provenance": "mixed",
         "q": "What components have failed in the HPCI system across the whole corpus?",
         "exp_nodes": expected["hpci_components"], "exp_lers": set(HPCI_LERS),
         "note": "cross-document join on the shared System:BJ hub — all three plants."},

        {"id": "Q4", "kind": "showcase", "intent": "mitigating_backups",
         "provenance": "mixed",
         "q": "Which events were mitigated by a redundant safety system being available?",
         "exp_nodes": set(), "exp_lers": set(HPCI_LERS),
         "note": "scored on LERs/citations (backups are serialized as codes, not nodes)."},

        {"id": "Q11", "kind": "aggregation", "intent": "cause_distribution",
         "provenance": "mixed",
         "q": "What is the distribution of cause categories across the event reports?",
         "exp_nodes": expected["cause_categories"], "exp_lers": set(HPCI_LERS),
         "note": "aggregation over the corpus."},

        # --- materialize-at-scale: no winner claimed (note 2) -------------------
        {"id": "Q14", "kind": "scale", "intent": "system_failure_modes",
         "provenance": "mixed",
         "q": "For the HPCI system, group all corpus events by failure mode and show the "
              "most common one.",
         "exp_nodes": expected["hpci_failure_modes"], "exp_lers": set(HPCI_LERS),
         "note": "grouping mechanism works, but FailureModes are per-event; a meaningful "
                 "'most common' only appears at corpus scale, not at N=3."},

        {"id": "Q2", "kind": "scale", "intent": "weak_program_events",
         "provenance": "oracle",
         "q": "Which events across all these plants trace back to a weak maintenance or "
              "procedure program?",
         "exp_nodes": set(), "exp_lers": {"254-2025-006-00"},
         "note": "only one personnel-error event exists at N=3; the cross-plant pattern "
                 "is a scale result."},

        {"id": "Q13", "kind": "scale", "intent": "shared_component_cause",
         "provenance": "none",
         "q": "Find events at different plants that share both a common component and a "
              "common cause.",
         "exp_nodes": set(), "exp_lers": set(),
         "note": "no such pair exists at N=3; the join is built into the schema and "
                 "surfaces once the corpus is scaled."},

        # --- negative / out-of-corpus: the no-hallucination test (note 4) -------
        {"id": "NEG", "kind": "negative", "intent": "out_of_corpus",
         "provenance": "none",
         "q": "What caused the steam generator tube rupture at Diablo Canyon Unit 1?",
         "exp_nodes": set(), "exp_lers": set(),
         "note": "Diablo Canyon and steam-generator tube ruptures are not in this corpus; "
                 "the system must refuse rather than fabricate."},
    ]


# --------------------------------------------------------------------------- #
# scoring — retrieval and answering kept separate
# --------------------------------------------------------------------------- #
def score_retrieval(ev, spec) -> dict:
    surfaced_nodes = set(ev.node_keys)
    surfaced_lers = ev.ler_keys()
    exp_nodes, exp_lers = spec["exp_nodes"], spec["exp_lers"]

    def recall(exp, got):
        return (len(exp & got) / len(exp)) if exp else None

    return {
        "routed_intent": ev.intent,
        "intent_ok": ev.intent == spec["intent"],
        "node_recall": recall(exp_nodes, surfaced_nodes),
        "ler_recall": recall(exp_lers, surfaced_lers),
        "missing_nodes": sorted(exp_nodes - surfaced_nodes),
        "missing_lers": sorted(exp_lers - surfaced_lers),
        "surfaced_nodes": sorted(surfaced_nodes),
        "surfaced_lers": sorted(surfaced_lers),
        "empty": ev.empty,
    }


def score_answer(ans, spec) -> dict:
    citations = set(ans.get("citations", []))
    exp_lers = spec["exp_lers"]
    return {
        "answerable": bool(ans.get("answerable")),
        "citations": sorted(citations),
        "citations_cover_expected": exp_lers <= citations if exp_lers else None,
        "unexpected_citations": sorted(citations - exp_lers) if exp_lers else sorted(citations),
    }


def decide_pass(spec, rscore, ascore) -> tuple[bool, str]:
    kind = spec["kind"]
    nr, lr = rscore["node_recall"], rscore["ler_recall"]

    if kind == "negative":
        ok = (not ascore["answerable"]) and not ascore["citations"]
        return ok, ("refused, no citations" if ok
                    else "FAILED no-hallucination: answered or cited out-of-corpus")

    if kind == "scale":
        # honest behavior only; never assert a winner
        if spec["id"] == "Q13":
            ok = rscore["empty"]
            return ok, ("honestly empty (surfaces at scale)" if ok
                        else "expected no cross-plant pair at N=3")
        ok = (lr == 1.0) and ascore["answerable"]
        return ok, ("mechanism works; scale-dependent, no winner claimed" if ok
                    else "expected the thin N=3 result")

    # showcase / aggregation
    node_ok = (nr is None) or (nr >= 0.8)
    ler_ok = (lr == 1.0)
    ans_ok = ascore["answerable"] and (ascore["citations_cover_expected"] in (True, None))
    ok = node_ok and ler_ok and ans_ok
    bits = []
    if not node_ok:
        bits.append(f"node recall {nr:.2f}")
    if not ler_ok:
        bits.append(f"LER recall {lr}")
    if not ans_ok:
        bits.append("answer ungrounded/missing citations")
    return ok, ("retrieval + grounded answer OK" if ok else "; ".join(bits))
