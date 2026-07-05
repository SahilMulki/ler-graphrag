"""
answer.py — Phase 6 grounded answer layer (retriever-agnostic).

Takes a question and an `Evidence` bundle (from any Retriever) and asks Claude to
write an answer grounded ONLY in that evidence, returning structured JSON so the
citations can be scored rather than regex-scraped from prose. The refusal path is
first-class: empty/insufficient evidence must yield "not answerable from this
corpus" with no citations — this is what the negative/out-of-corpus test checks.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from llm import LLM
from retrieve import Evidence

ANSWER_SYSTEM = """You answer questions about U.S. NRC Licensee Event Reports (LERs) using
ONLY the EVIDENCE provided (a subgraph retrieved from a knowledge graph). Return JSON only.

Rules:
- Ground every statement in the EVIDENCE. Do NOT use outside knowledge and do NOT invent
  plants, systems, components, causes, or LER numbers.
- Cite the LER number(s) you used in `citations` (e.g. "353-2025-001-00").
- If the EVIDENCE is empty or does not contain the answer, set `answerable` to false, say so
  plainly in `answer`, and return an empty `citations` list. Never guess.
- Preserve any "[note]" caveats in the evidence (e.g. a pattern that only emerges at scale) —
  reflect that honestly rather than overclaiming.

Return: {"answerable": true/false, "answer": "...", "citations": ["...", ...]}"""


def answer(question: str, ev: Evidence, llm: LLM | None = None) -> dict:
    llm = llm or LLM()
    user = (f"QUESTION:\n{question}\n\n"
            f"EVIDENCE (retrieval intent = {ev.intent}):\n{ev.text}\n\n"
            "Return the answer JSON.")
    obj = llm.complete_json(ANSWER_SYSTEM, user, tag="answer")
    obj.setdefault("answerable", not ev.empty)
    obj.setdefault("answer", "")
    obj.setdefault("citations", [])
    if not isinstance(obj["citations"], list):
        obj["citations"] = []
    # keep only citations the retriever actually surfaced (belt-and-suspenders vs. drift)
    allowed = ev.ler_keys()
    if allowed:
        obj["citations"] = [c for c in obj["citations"] if c in allowed]
    return obj
