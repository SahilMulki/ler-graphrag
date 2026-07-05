"""
ask.py — Phase 6 CLI: ask the LER graph a question, or run the golden-question eval.

    python src/ask.py "What components have failed in HPCI across the corpus?"
    python src/ask.py --golden        # run the MVP-now golden set with scoring
    python src/ask.py --golden --brief # one line per question

Graph retrieval (LLM router + Cypher templates) -> grounded answer (cites LERs).
The --golden runner prints, per question: the routed intent, the nodes/LERs the
retriever actually surfaced (with oracle|pipeline provenance), the grounded answer
and its citations, and a retrieval/answer PASS-FAIL — retrieval scored separately
from answering, and materialize-at-scale questions judged honestly (no N=3 "winner").
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from answer import answer
from golden_eval import build_expected, decide_pass, golden, score_answer, score_retrieval
from llm import LLM
from load_graph import load_records
from retrieve import GraphRetriever


def _provenance_map() -> dict:
    return {rec.ler_number: src for rec, src in load_records()}


def _fmt_lers(lers, prov) -> str:
    return ", ".join(f"{k} [{prov.get(k, '?')}]" for k in lers) or "(none)"


# --------------------------------------------------------------------------- #
# single question
# --------------------------------------------------------------------------- #
def ask_one(question: str) -> None:
    llm = LLM()
    gr = GraphRetriever(llm=llm)
    prov = _provenance_map()
    try:
        ev = gr.retrieve(question)
        ans = answer(question, ev, llm=llm)
    finally:
        gr.close()

    print(f"\nQ: {question}")
    print(f"  routed intent : {ev.intent}  anchors={ev.anchors}")
    print(f"  retrieved LERs: {_fmt_lers(sorted(ev.ler_keys()), prov)}")
    if ev.node_keys:
        print(f"  retrieved nodes: {', '.join(ev.node_keys)}")
    print(f"\n  answerable: {ans['answerable']}")
    print(f"  answer    : {ans['answer']}")
    print(f"  citations : {_fmt_lers(ans.get('citations', []), prov)}")


# --------------------------------------------------------------------------- #
# golden runner
# --------------------------------------------------------------------------- #
def run_golden(brief: bool = False) -> int:
    llm = LLM()
    gr = GraphRetriever(llm=llm)
    prov = _provenance_map()
    specs = golden(build_expected())
    results = []
    try:
        for spec in specs:
            ev = gr.retrieve(spec["q"])
            ans = answer(spec["q"], ev, llm=llm)
            rs = score_retrieval(ev, spec)
            as_ = score_answer(ans, spec)
            ok, why = decide_pass(spec, rs, as_)
            results.append((spec, ev, ans, rs, as_, ok, why))
    finally:
        gr.close()

    if brief:
        _print_brief(results, prov)
    else:
        for r in results:
            _print_full(*r, prov=prov)
    return _print_summary(results)


def _rec(x):
    return "—" if x is None else f"{x:.2f}"


def _print_full(spec, ev, ans, rs, as_, ok, why, prov) -> None:
    print("\n" + "=" * 78)
    print(f"[{spec['id']}] ({spec['kind']}, provenance={spec['provenance']})  {spec['q']}")
    print(f"  routed intent : {rs['routed_intent']}"
          f"  ({'expected' if rs['intent_ok'] else 'EXPECTED ' + spec['intent']})")
    print("  --- retrieval ---")
    print(f"    node recall : {_rec(rs['node_recall'])}"
          + (f"   missing: {rs['missing_nodes']}" if rs["missing_nodes"] else ""))
    print(f"    LER  recall : {_rec(rs['ler_recall'])}"
          + (f"   missing: {rs['missing_lers']}" if rs["missing_lers"] else ""))
    print(f"    surfaced LERs: {_fmt_lers(rs['surfaced_lers'], prov)}")
    if rs["surfaced_nodes"]:
        shown = rs["surfaced_nodes"][:8]
        more = "" if len(rs["surfaced_nodes"]) <= 8 else f"  (+{len(rs['surfaced_nodes']) - 8} more)"
        print(f"    surfaced nodes: {', '.join(shown)}{more}")
    print("  --- answer ---")
    print(f"    answerable  : {as_['answerable']}")
    print(f"    answer      : {ans['answer']}")
    print(f"    citations   : {_fmt_lers(as_['citations'], prov)}"
          + (f"   UNEXPECTED: {as_['unexpected_citations']}" if as_["unexpected_citations"] else ""))
    print(f"  {'PASS' if ok else 'FAIL'}: {why}")
    print(f"  note: {spec['note']}")


def _print_brief(results, prov) -> None:
    print(f"\n{'id':14} {'kind':11} {'intent':22} {'nR':>4} {'lR':>4} {'ans':>4}  result")
    for spec, ev, ans, rs, as_, ok, why in results:
        print(f"{spec['id']:14} {spec['kind']:11} {rs['routed_intent']:22} "
              f"{_rec(rs['node_recall']):>4} {_rec(rs['ler_recall']):>4} "
              f"{('Y' if as_['answerable'] else 'N'):>4}  {'PASS' if ok else 'FAIL'} — {why}")


def _print_summary(results) -> int:
    from collections import Counter
    by_kind = Counter()
    pass_kind = Counter()
    for spec, *_rest, ok, _why in results:
        by_kind[spec["kind"]] += 1
        if ok:
            pass_kind[spec["kind"]] += 1
    total_ok = sum(1 for *_r, ok, _w in results)
    npass = sum(1 for r in results if r[5])
    print("\n" + "=" * 78)
    print("SUMMARY")
    for kind in ("showcase", "aggregation", "scale", "negative"):
        if by_kind[kind]:
            print(f"  {kind:12}: {pass_kind[kind]}/{by_kind[kind]} pass")
    print(f"  {'TOTAL':12}: {npass}/{len(results)} pass")
    print("\n  Interpretation: showcase/aggregation prove the graph answers multi-hop and")
    print("  cross-document questions now; scale rows work but claim no winner at N=3;")
    print("  the negative row is the no-hallucination check. The graph-vs-vector baseline")
    print("  is Phase 8, where the corpus is large enough for the comparison to be robust.")
    return 0 if npass == len(results) else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Ask the LER graph, or run the golden eval.")
    p.add_argument("question", nargs="?", help="a natural-language question")
    p.add_argument("--golden", action="store_true", help="run the golden-question eval")
    p.add_argument("--brief", action="store_true", help="one line per golden question")
    args = p.parse_args(argv)

    if args.golden:
        return run_golden(brief=args.brief)
    if not args.question:
        p.error("provide a question, or use --golden")
    ask_one(args.question)
    return 0


if __name__ == "__main__":
    sys.exit(main())
