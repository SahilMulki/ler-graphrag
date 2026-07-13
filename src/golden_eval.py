"""
golden_eval.py — the golden-question eval spec + scoring.

Two things the notes asked for, preserved from Phase 6:
  * Retrieval is scored SEPARATELY from answering. Each question carries a
    hand-specified expected set (node match_keys and/or LER numbers), and we score
    whether the GraphRetriever surfaced them — independent of what the answer LLM says.
  * Ambiguity is a first-class outcome: a single-subject question matching several
    events must return a Clarification (asserted structurally, not by prose).

Phase 8 rescaled the set for the ~830-doc corpus (`kind` drives the pass rule):
  showcase     single-event answer, anchored on a specific LER number so it stays
               deterministic even though plants now have many events; must answer
               (not clarify), surface the expected chain, and cite that LER
  xdoc         cross-document breadth: the known oracle items must appear as a SUBSET
               and the result must span many LERs (the join a flat retriever can't do)
  aggregation  corpus-wide grouping: known items appear + spans the corpus + grounded
  payoff       a join that was empty at N=3 and is non-empty at scale (cross-plant)
  clarify      real same-plant / broad ambiguity -> must ask, asserted structurally
  intent       adversarial router-boundary guard: aggregate must NOT clarify
  negative     no-hallucination: an out-of-corpus question must be refused

The frozen 3-doc oracle regression (score.py) is the separate extraction-quality gate.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from load_graph import load_records
from retrieve import Clarification

# The three hand-marked HPCI LERs remain the STABLE anchors at scale: their
# extractions are frozen/known, so expected sets derived from them don't drift.
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
# the golden set (Phase-8 scale)
# --------------------------------------------------------------------------- #
def golden(expected: dict) -> list[dict]:
    return [
        # --- multi-hop, anchored on a specific LER so it stays a single answer ----
        {"id": "MH-Limerick", "kind": "showcase", "intent": "failure_chain",
         "provenance": "pipeline",
         "q": "What chain of failures led to HPCI being inoperable in LER 353-2025-001-00?",
         "exp_nodes": expected["chain_limerick"], "exp_lers": {"353-2025-001-00"},
         "note": "lead multi-hop demo — pipeline-extracted Limerick chain; anchored on the "
                 "LER number because 'HPCI at Limerick' is now genuinely ambiguous (2 events)."},

        {"id": "MH-QuadCities", "kind": "showcase", "intent": "failure_chain",
         "provenance": "oracle",
         "q": "What caused HPCI inoperability in LER 254-2025-006-00?",
         "exp_nodes": expected["chain_qc"], "exp_lers": {"254-2025-006-00"},
         "note": "the oracle-sourced QC chain, anchored on its LER number (few-shot exemplar)."},

        # --- cross-document hubs: the structural advantage, now at fleet scale ----
        {"id": "XDOC-HPCI-COMP", "kind": "xdoc", "intent": "system_components",
         "provenance": "mixed",
         "q": "What components have failed in the HPCI system across the whole corpus?",
         "exp_nodes": expected["hpci_components"], "exp_lers": set(HPCI_LERS),
         "min_lers": 8,
         "note": "join on the shared System:BJ hub across ~45 HPCI events / ~15 plants; the "
                 "known 3-doc components must appear as a subset and the result must span "
                 "many LERs — the cross-document assembly a flat retriever cannot do."},

        {"id": "AGG-CAUSE", "kind": "aggregation", "intent": "cause_distribution",
         "provenance": "mixed",
         "q": "What is the distribution of cause categories across all the event reports?",
         "exp_nodes": expected["cause_categories"], "exp_lers": set(HPCI_LERS),
         "min_lers": 100,
         "note": "corpus-wide aggregation over ~770 LERs; the known cause categories appear "
                 "and the grouping now has real mass behind each bucket."},

        {"id": "AGG-WEAK-PROG", "kind": "aggregation", "intent": "weak_program_events",
         "provenance": "mixed",
         "q": "Which events across all these plants trace back to a weak maintenance or "
              "procedure program (personnel error)?",
         "exp_nodes": set(), "exp_lers": {"254-2025-006-00"},
         "min_lers": 5,
         "note": "the cross-plant 'weak program' pattern that only exists at scale; QC is one "
                 "known personnel-error event, now among many across the fleet."},

        # --- the scale payoff: a join that was empty at N=3, non-empty now --------
        {"id": "XPLANT-SHARED", "kind": "payoff", "intent": "shared_component_cause",
         "provenance": "none",
         "q": "Find events at different plants that share both a common component and a "
              "common cause.",
         "exp_nodes": set(), "exp_lers": set(),
         "note": "empty at N=3, non-empty at scale — the cross-plant join built into the "
                 "coded-hub schema finally surfaces real pairs."},

        # --- abstain / clarify on REAL ambiguity (the whole point at scale) -------
        {"id": "CLARIFY-PLANT", "kind": "clarify", "intent": "failure_chain",
         "provenance": "mixed",
         "q": "What caused an HPCI event at Browns Ferry?",
         "min_candidates": 3, "must_include": "259-2024-002-00",
         "exp_nodes": set(), "exp_lers": set(),
         "note": "same-plant ambiguity is now real: Browns Ferry has 8 HPCI events, so the "
                 "system must ASK which one instead of guessing."},

        {"id": "CLARIFY-BROAD", "kind": "clarify", "intent": "failure_chain",
         "provenance": "mixed",
         "q": "What caused HPCI inoperability?",
         "min_candidates": 10,
         "exp_nodes": set(), "exp_lers": set(),
         "note": "no plant, ~45 HPCI events match: clarify with an overflow hint to narrow "
                 "by year or LER number (candidates are capped, not hidden silently)."},

        # --- adversarial intent: aggregate must NOT be read as a single event -----
        {"id": "ADV-AGG", "kind": "intent", "intent": "system_failure_modes",
         "provenance": "mixed",
         "q": "Across all the reports, what failure modes has the HPCI system had?",
         "exp_nodes": set(), "exp_lers": set(),
         "note": "aggregate phrasing near the single/aggregate boundary: must route to "
                 "system_failure_modes and span events, NOT clarify."},

        # --- negative / out-of-corpus: the no-hallucination test ------------------
        {"id": "NEG", "kind": "negative", "intent": "out_of_corpus",
         "provenance": "none",
         "q": "What caused the turbine failure at the Fukushima Daiichi nuclear plant?",
         "exp_nodes": set(), "exp_lers": set(),
         "note": "Fukushima Daiichi is not a U.S. NRC licensee in this corpus (verified absent); "
                 "the system must refuse rather than fabricate. (Diablo Canyon IS in-corpus, so "
                 "it would not be a valid out-of-corpus probe.)"},
    ]


# --------------------------------------------------------------------------- #
# scoring — retrieval, answering, and clarification kept separate
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


def score_clarify(outcome: Clarification, spec) -> dict:
    offered = outcome.candidate_keys()
    return {
        "routed_intent": outcome.intent,
        "intent_ok": outcome.intent == spec["intent"],
        "offered": sorted(offered),
        "total": outcome.total,
        "overflow": outcome.overflow,
    }


def score_answer(ans, spec) -> dict:
    citations = set(ans.get("citations", []))
    return {
        "answerable": bool(ans.get("answerable")),
        "citations": sorted(citations),
    }


# --------------------------------------------------------------------------- #
# judge — one entry point over both outcome types (Evidence | Clarification)
# --------------------------------------------------------------------------- #
def judge(spec, outcome, ans) -> tuple[bool, str, dict]:
    """Decide PASS/FAIL for a spec at scale. Returns (ok, why, detail); detail
    carries {"clar": ...} for a Clarification or {"rs":..., "as":...} for Evidence."""
    kind = spec["kind"]

    # --- Clarification outcome -------------------------------------------------
    if isinstance(outcome, Clarification):
        cs = score_clarify(outcome, spec)
        detail = {"clar": cs}
        if kind != "clarify":
            return False, "unexpectedly asked to disambiguate", detail
        if not cs["intent_ok"]:
            return False, f"clarified but routed to {cs['routed_intent']}", detail
        need = spec.get("min_candidates", 2)
        if outcome.total < need:
            return False, f"clarified but only {outcome.total} candidate(s) (< {need})", detail
        inc = spec.get("must_include")
        if inc and inc not in outcome.candidate_keys() and not outcome.overflow:
            return False, f"clarified but {inc} not among the shown candidates", detail
        return True, f"asked to disambiguate across {outcome.total} events", detail

    # --- Evidence outcome ------------------------------------------------------
    rs = score_retrieval(outcome, spec)
    as_ = (score_answer(ans, spec) if ans is not None
           else {"answerable": False, "citations": []})
    detail = {"rs": rs, "as": as_}
    grounded = set(as_["citations"]) <= set(rs["surfaced_lers"])
    n_lers = len(rs["surfaced_lers"])

    if kind == "clarify":
        return False, "expected a disambiguation prompt; got a single answer/refusal", detail
    if kind == "intent":
        ok = rs["intent_ok"]
        return ok, ("routed to the expected aggregate intent; no false clarification" if ok
                    else f"wrong intent {rs['routed_intent']} (expected {spec['intent']})"), detail
    if kind == "negative":
        ok = (not as_["answerable"]) and not as_["citations"]
        return ok, ("refused, no citations" if ok
                    else "FAILED no-hallucination: answered or cited out-of-corpus"), detail
    if kind == "payoff":
        ok = rs["intent_ok"] and (not rs["empty"]) and as_["answerable"]
        return ok, ("cross-plant pair surfaced at scale + grounded answer" if ok
                    else "expected a non-empty cross-plant result at scale"), detail

    # showcase / xdoc / aggregation — retrieval recall + scale breadth + grounded
    nr, lr = rs["node_recall"], rs["ler_recall"]
    node_ok = (nr is None) or (nr >= 0.8)
    ler_ok = (lr is None) or (lr >= 0.8)
    ans_ok = as_["answerable"] and grounded
    scale_ok = n_lers >= spec.get("min_lers", 0)
    cited_ok = (spec["exp_lers"] <= set(as_["citations"])) if kind == "showcase" and spec["exp_lers"] else True
    ok = rs["intent_ok"] and node_ok and ler_ok and ans_ok and scale_ok and cited_ok

    bits = []
    if not rs["intent_ok"]:
        bits.append(f"intent {rs['routed_intent']} (exp {spec['intent']})")
    if not node_ok:
        bits.append(f"node recall {nr:.2f}")
    if not ler_ok:
        bits.append(f"known-LER recall {lr:.2f}")
    if not scale_ok:
        bits.append(f"only {n_lers} LERs (< {spec['min_lers']})")
    if not ans_ok:
        bits.append("answer ungrounded/unanswerable")
    if not cited_ok:
        bits.append("expected LER not cited")
    why = ("retrieval + grounded answer OK" + (f"; spans {n_lers} LERs" if spec.get("min_lers") else "")
           if ok else "; ".join(bits))
    return ok, why, detail
