"""
retrieve.py — Phase 6 graph retrieval over the Neo4j LER graph.

A question is answered in two stages, behind a retriever-agnostic seam so a vector
baseline can slot in at Phase 8 without touching the answer layer:

    Retriever.retrieve(question) -> Evidence      # graph now; vector later
    answer.answer(question, evidence)             # shared, grounded, cites LERs

GraphRetriever routing is "LLM router + Cypher templates":
  1. GraphVocab pulls the graph's real controlled vocabulary (system codes+names,
     cause categories, plants, LER keys) from Neo4j.
  2. An LLM classifies the question into one of a fixed INTENTS set and extracts
     anchors *constrained to that vocabulary* — so it can only ever point at nodes
     that exist. Anything it can't ground becomes `out_of_corpus` (empty evidence),
     which is what lets the answerer refuse instead of hallucinating.
  3. Each intent dispatches to a parameterized Cypher template (the showcase paths
     from graph/queries.cypher) or a generic k-hop subgraph fallback.

Text2Cypher (LLM writes raw Cypher) is deliberately out of scope — too brittle for
a robust demo.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from llm import LLM
from load_graph import _connect

# --------------------------------------------------------------------------- #
# intents
# --------------------------------------------------------------------------- #
INTENTS = {
    "failure_chain":
        "the cause / ordered failure chain of a SINGLE event — a 'what caused' or "
        "'what led to X' question. Anchor on plant and/or system and/or LER number; "
        "use this even when only a system is named. If several events match, the "
        "system asks the user to disambiguate rather than guessing.",
    "system_components":
        "components that failed in/on a given system across the corpus (needs a system)",
    "system_failure_modes":
        "AGGREGATE: failure modes for a given system grouped ACROSS the corpus — "
        "phrasings like 'group all events', 'most common failure mode', 'what failure "
        "modes across all reports' (needs a system)",
    "mitigating_backups":
        "events where a redundant/backup safety system was available",
    "cause_distribution":
        "the distribution of cause categories across the corpus",
    "weak_program_events":
        "events attributed to personnel error / weak maintenance or procedure programs",
    "shared_component_cause":
        "events at different plants sharing BOTH a common component and a common cause",
    "subgraph":
        "a general neighborhood around one named entity (fallback)",
    "out_of_corpus":
        "the question is not answerable from this corpus of LERs",
}

# Intents that address ONE event, so a match against several distinct events is
# ambiguous and must trigger a Clarification (not a silent guess). Aggregate intents
# (system_components, cause_distribution, ...) are MEANT to span events -> exempt.
SINGLE_SUBJECT_INTENTS = {"failure_chain"}

# Most candidates we list in a Clarification before telling the user to narrow instead.
CANDIDATE_CAP = 8


# --------------------------------------------------------------------------- #
# Evidence — the retriever-agnostic hand-off to the answer layer
# --------------------------------------------------------------------------- #
@dataclass
class Evidence:
    intent: str
    anchors: dict
    text: str                                  # serialized subgraph for the answerer
    node_keys: list[str] = field(default_factory=list)   # match_keys surfaced (for scoring)
    lers: list[dict] = field(default_factory=list)       # [{key, source}] cited-able
    empty: bool = False

    def ler_keys(self) -> set[str]:
        return {l["key"] for l in self.lers}


# --------------------------------------------------------------------------- #
# Clarification — the third outcome besides answer / refuse
# --------------------------------------------------------------------------- #
@dataclass
class Clarification:
    """A single-subject question matched MULTIPLE candidate events, so we ask the user
    to disambiguate instead of silently picking one. Detected structurally in the
    retriever by candidate cardinality (>1) — never by letting the answer LLM guess.
    Single-shot: we return the candidates and the user re-asks (primary re-ask path is
    by LER number, which is unambiguous)."""
    intent: str
    anchors: dict
    question: str                                        # disambiguation prompt to show
    candidates: list[dict] = field(default_factory=list) # capped, sorted by event_date desc
    total: int = 0                                       # candidate count before the cap
    overflow: bool = False                               # total > CANDIDATE_CAP (some hidden)

    def candidate_keys(self) -> set[str]:
        return {c["key"] for c in self.candidates}


# --------------------------------------------------------------------------- #
# graph vocabulary (constrains the router to real nodes)
# --------------------------------------------------------------------------- #
@dataclass
class GraphVocab:
    systems: list[dict]      # [{code, name}]
    causes: list[dict]       # [{code, category}]
    plants: list[dict]       # [{ler, plant}]

    @classmethod
    def load(cls, session) -> "GraphVocab":
        systems = [dict(r) for r in session.run(
            "MATCH (s:System) RETURN DISTINCT s.eiis_code AS code, s.display_name AS name "
            "ORDER BY name")]
        causes = [dict(r) for r in session.run(
            "MATCH (c:Cause) RETURN DISTINCT c.cause_code AS code, c.category AS category "
            "ORDER BY code")]
        plants = [dict(r) for r in session.run(
            "MATCH (l:LER) WHERE NOT l.stub "
            "RETURN l.key AS ler, l.plant_name AS plant ORDER BY ler")]
        return cls(systems=systems, causes=causes, plants=plants)

    def as_prompt(self) -> str:
        return (
            "SYSTEMS (eiis_code — name):\n"
            + "\n".join(f"  {s['code'] or '(none)'} — {s['name']}" for s in self.systems)
            + "\n\nCAUSE CATEGORIES (code — category):\n"
            + "\n".join(f"  {c['code']} — {c['category']}" for c in self.causes)
            + "\n\nLERs (key — plant):\n"
            + "\n".join(f"  {p['ler']} — {p['plant']}" for p in self.plants)
        )


# --------------------------------------------------------------------------- #
# router
# --------------------------------------------------------------------------- #
ROUTER_SYSTEM = """You route a natural-language question to ONE retrieval intent over a
knowledge graph of U.S. NRC Licensee Event Reports (LERs), and extract anchors that
MUST come from the provided vocabulary. Return JSON only.

Intents:
{intents}

Rules:
- Choose exactly one `intent`.
- `anchors` may include: system_code (an eiis_code from the vocab, or the string "ADS"
  for the Automatic Depressurization System), ler_key (from the vocab), plant (a plant
  name substring from the vocab), cause_code (from the vocab).
- Only use anchor values that appear in the vocabulary. If the question names an entity
  (plant, system, component, event) that is NOT in the vocabulary, choose intent
  "out_of_corpus" with empty anchors — do not guess.
- "HPCI" is system BJ; "RCIC" is BN. Map common names to their eiis_code when present.

Return: {{"intent": "...", "anchors": {{...}}, "reason": "one short clause"}}"""


def route(question: str, vocab: GraphVocab, llm: LLM) -> dict:
    system = ROUTER_SYSTEM.format(
        intents="\n".join(f"  - {k}: {v}" for k, v in INTENTS.items()))
    user = f"VOCABULARY:\n{vocab.as_prompt()}\n\nQUESTION:\n{question}\n\nReturn the routing JSON."
    obj = llm.complete_json(system, user, tag="route")
    obj.setdefault("intent", "out_of_corpus")
    obj.setdefault("anchors", {})
    if obj["intent"] not in INTENTS:
        obj["intent"] = "subgraph"
    return obj


# --------------------------------------------------------------------------- #
# GraphRetriever
# --------------------------------------------------------------------------- #
class GraphRetriever:
    def __init__(self, llm: LLM | None = None):
        self.driver = _connect()
        self.driver.verify_connectivity()
        self.llm = llm or LLM()
        with self.driver.session() as s:
            self.vocab = GraphVocab.load(s)

    def close(self):
        self.driver.close()

    # -- public ------------------------------------------------------------- #
    def retrieve(self, question: str) -> "Evidence | Clarification":
        r = route(question, self.vocab, self.llm)
        intent, anchors = r["intent"], r["anchors"]
        with self.driver.session() as s:
            handler = getattr(self, f"_t_{intent}", None)
            if handler is None:
                return self._empty(intent, anchors)
            return handler(s, anchors)

    # -- helpers ------------------------------------------------------------ #
    def _empty(self, intent, anchors) -> Evidence:
        return Evidence(intent=intent, anchors=anchors,
                        text="(no matching evidence in the corpus)", empty=True)

    def _candidate_lers(self, s, anchors) -> list[dict]:
        """The candidate set for a single-subject intent: every non-stub LER matching
        ALL pinned anchors (LER number exact; plant substring; system membership).
        Sorted by event_date descending so the most recent events show first. This is
        what the cardinality branch counts — 0 refuse / 1 answer / >1 clarify."""
        if anchors.get("ler_key"):
            preds, params = ["l.key = $ler_key"], {"ler_key": anchors["ler_key"]}
        else:
            preds, params = [], {}
            if anchors.get("plant"):
                preds.append("toLower(l.plant_name) CONTAINS toLower($plant)")
                params["plant"] = anchors["plant"]
            code = (anchors.get("system_code") or "").strip()
            if code:
                if code.upper() == "ADS":
                    preds.append("EXISTS { (l)-[:INVOLVES]->(:System "
                                 "{match_key:'System:automatic-depressurization-system'}) }")
                else:
                    preds.append("EXISTS { (l)-[:INVOLVES]->(:System {eiis_code:$system_code}) }")
                    params["system_code"] = code
        where = " AND ".join(preds) if preds else "true"
        rows = s.run(
            f"MATCH (l:LER) WHERE NOT l.stub AND {where} "
            "OPTIONAL MATCH (l)-[:INVOLVES]->(sys:System) "
            "WITH l, collect(DISTINCT coalesce(sys.eiis_code, sys.display_name)) AS systems "
            "RETURN l.key AS key, l.plant_name AS plant, l.event_date AS event_date, "
            "  l.title AS title, l.source AS source, systems "
            "ORDER BY l.event_date DESC, l.key", **params)
        return [dict(r) for r in rows]

    def _clarify(self, intent, anchors, cands) -> Clarification:
        total = len(cands)
        shown = cands[:CANDIDATE_CAP]
        overflow = total > CANDIDATE_CAP
        q = (f"That matches {total} events in the corpus — which one do you mean? "
             "Re-ask with a specific LER number"
             + (", or narrow it down by adding an event year (some matches are not shown)."
                if overflow else " from the list below."))
        return Clarification(intent=intent, anchors=anchors, question=q,
                             candidates=shown, total=total, overflow=overflow)

    @staticmethod
    def _sys_match(anchors) -> tuple[str, dict]:
        """Return (cypher_predicate, params) selecting a System by code or ADS."""
        code = (anchors.get("system_code") or "").strip()
        if code.upper() == "ADS":
            return ("s.match_key = 'System:automatic-depressurization-system'", {})
        return ("s.eiis_code = $code", {"code": code})

    # -- templates ---------------------------------------------------------- #
    def _t_failure_chain(self, s, anchors) -> "Evidence | Clarification":
        # Single-subject: resolve the candidate set, then branch on cardinality.
        # 0 -> refuse (empty); >1 -> clarify (don't guess which event); 1 -> answer.
        cands = self._candidate_lers(s, anchors)
        if not cands:
            return self._empty("failure_chain", anchors)
        if len(cands) > 1:
            return self._clarify("failure_chain", anchors, cands)
        ler = cands[0]["key"]
        rows = list(s.run(
            "MATCH (l:LER {key:$ler})-[:HAS_CAUSE]->(cause:Cause) "
            "MATCH (cause)<-[:CAUSED_BY]-(origin:FailureMode) "
            "MATCH path=(origin)-[:LEADS_TO*0..]->(cons:Consequence) "
            "WITH l, cause, path, cons "
            "OPTIONAL MATCH (cons)-[:BACKED_UP_BY]->(bk:System) "
            "RETURN l.key AS ler, l.source AS source, l.plant_name AS plant, "
            "  cause.display_name AS cause, cause.category AS category, "
            "  [n IN nodes(path) | n.display_name] AS chain, "
            "  [n IN nodes(path) | n.match_key] AS chain_keys, "
            "  collect(DISTINCT coalesce(bk.eiis_code, bk.display_name)) AS backups", ler=ler))
        if not rows:
            return self._empty("failure_chain", anchors)
        keys, lines = set(), []
        src = rows[0]["source"]
        for r in rows:
            keys.update(k for k in r["chain_keys"] if k)
            lines.append(f"LER {r['ler']} ({r['plant']}, source={r['source']}) — "
                         f"cause: {r['cause']}\n    chain: " + " -> ".join(r["chain"])
                         + (f"\n    backups available: {', '.join(b for b in r['backups'] if b)}"
                            if any(r["backups"]) else ""))
        return Evidence("failure_chain", anchors, "\n".join(lines),
                        node_keys=sorted(keys),
                        lers=[{"key": rows[0]["ler"], "source": src}])

    def _t_system_components(self, s, anchors) -> Evidence:
        pred, params = self._sys_match(anchors)
        rows = list(s.run(
            f"MATCH (l:LER)-[:INVOLVES]->(s:System) WHERE NOT l.stub AND {pred} "
            "WITH collect(DISTINCT l.key) AS lers "
            "MATCH (c:Component)-[e]-() WHERE e.ler_number IN lers "
            "WITH DISTINCT c, e.ler_number AS ler "
            "MATCH (l:LER {key:ler}) "
            "RETURN c.display_name AS component, c.eiis_code AS code, "
            "  c.match_key AS mk, ler, l.source AS source, l.plant_name AS plant "
            "ORDER BY ler, component", **params))
        if not rows:
            return self._empty("system_components", anchors)
        keys = sorted({r["mk"] for r in rows})
        lers = {(r["ler"], r["source"]) for r in rows}
        lines = [f"  {r['component']} [{r['code'] or 'no EIIS code'}] "
                 f"— LER {r['ler']} ({r['plant']}, {r['source']})" for r in rows]
        text = ("Components on the queried system across the corpus:\n" + "\n".join(lines))
        return Evidence("system_components", anchors, text, node_keys=keys,
                        lers=[{"key": k, "source": v} for k, v in sorted(lers)])

    def _t_system_failure_modes(self, s, anchors) -> Evidence:
        pred, params = self._sys_match(anchors)
        rows = list(s.run(
            f"MATCH (l:LER)-[:INVOLVES]->(s:System) WHERE NOT l.stub AND {pred} "
            "MATCH (l)-[:HAS_CAUSE]->(:Cause)<-[:CAUSED_BY]-(:FailureMode)"
            "-[:LEADS_TO*0..]->(fm:FailureMode) "
            "RETURN fm.match_key AS mk, fm.display_name AS name, "
            "  collect(DISTINCT l.key) AS lers, count(DISTINCT l) AS n "
            "ORDER BY n DESC, name", **params))
        if not rows:
            return self._empty("system_failure_modes", anchors)
        keys = sorted({r["mk"] for r in rows})
        lers = sorted({x for r in rows for x in r["lers"]})
        lines = [f"  {r['name']}  (in {r['n']} event(s): {', '.join(r['lers'])})" for r in rows]
        text = ("Failure modes on the queried system, grouped across the corpus by "
                "semantic key:\n" + "\n".join(lines)
                + "\n[note] with 3 documents most failure modes appear once; this "
                  "grouping produces a meaningful 'most common' only at corpus scale.")
        return Evidence("system_failure_modes", anchors, text, node_keys=keys,
                        lers=[{"key": k, "source": None} for k in lers])

    def _t_mitigating_backups(self, s, anchors) -> Evidence:
        rows = list(s.run(
            "MATCH (l:LER)-[:HAS_CAUSE]->(:Cause)<-[:CAUSED_BY]-()-[:LEADS_TO*0..]->"
            "(cons:Consequence)-[:BACKED_UP_BY]->(bk:System) WHERE NOT l.stub "
            "RETURN l.key AS ler, l.source AS source, l.plant_name AS plant, "
            "  cons.display_name AS consequence, "
            "  collect(DISTINCT coalesce(bk.eiis_code, bk.display_name)) AS backups "
            "ORDER BY ler"))
        if not rows:
            return self._empty("mitigating_backups", anchors)
        lines = [f"  LER {r['ler']} ({r['plant']}, {r['source']}): {r['consequence']} "
                 f"— backups available: {', '.join(b for b in r['backups'] if b)}" for r in rows]
        return Evidence("mitigating_backups", anchors,
                        "Events mitigated by an available redundant safety system:\n"
                        + "\n".join(lines),
                        node_keys=[],
                        lers=[{"key": r["ler"], "source": r["source"]} for r in rows])

    def _t_cause_distribution(self, s, anchors) -> Evidence:
        rows = list(s.run(
            "MATCH (l:LER)-[:HAS_CAUSE]->(c:Cause) WHERE NOT l.stub "
            "RETURN c.category AS category, c.cause_code AS code, "
            "  collect(l.key) AS lers, count(*) AS n ORDER BY n DESC, category"))
        lines = [f"  {r['category']} [{r['code']}]: {r['n']} event(s) — {', '.join(r['lers'])}"
                 for r in rows]
        lers = sorted({x for r in rows for x in r["lers"]})
        return Evidence("cause_distribution", anchors,
                        "Cause-category distribution across the corpus:\n" + "\n".join(lines),
                        node_keys=[f"Cause:{r['category']}" for r in rows],
                        lers=[{"key": k, "source": None} for k in lers])

    def _t_weak_program_events(self, s, anchors) -> Evidence:
        rows = list(s.run(
            "MATCH (l:LER)-[hc:HAS_CAUSE]->(c:Cause) "
            "WHERE NOT l.stub AND c.cause_code = 'A' "
            "RETURN l.key AS ler, l.source AS source, l.plant_name AS plant, "
            "  c.category AS category, hc.theme AS theme, hc.proximate_text AS proximate "
            "ORDER BY ler"))
        if not rows:
            return self._empty("weak_program_events", anchors)
        lines = [f"  LER {r['ler']} ({r['plant']}, {r['source']}): {r['category']} — "
                 f"{r['theme'] or r['proximate'] or ''}" for r in rows]
        text = ("Events attributed to personnel error / weak program:\n" + "\n".join(lines)
                + "\n[note] the cross-plant 'weak program' pattern is a scale result; "
                  "at 3 documents only one such event exists.")
        return Evidence("weak_program_events", anchors, text,
                        node_keys=[], lers=[{"key": r["ler"], "source": r["source"]} for r in rows])

    def _t_shared_component_cause(self, s, anchors) -> Evidence:
        rows = list(s.run(
            "MATCH (l1:LER)-[:INVOLVES]->(comp:Component)<-[:INVOLVES]-(l2:LER), "
            "  (l1)-[:HAS_CAUSE]->(cause:Cause)<-[:HAS_CAUSE]-(l2) "
            "WHERE l1.key < l2.key AND l1.plant_name <> l2.plant_name "
            "RETURN l1.key AS a, l2.key AS b, comp.display_name AS component, "
            "  cause.category AS cause"))
        if not rows:
            return Evidence("shared_component_cause", anchors,
                            "No two different plants in the current corpus share BOTH a "
                            "component and a cause. This cross-document join is built into "
                            "the schema (coded components and cause categories are shared "
                            "hubs) and will surface once the corpus is scaled up.",
                            node_keys=[], lers=[], empty=True)
        lines = [f"  {r['a']} & {r['b']}: component {r['component']}, cause {r['cause']}"
                 for r in rows]
        return Evidence("shared_component_cause", anchors,
                        "Cross-plant events sharing a component and a cause:\n" + "\n".join(lines),
                        lers=[{"key": r["a"], "source": None} for r in rows]
                             + [{"key": r["b"], "source": None} for r in rows])

    def _t_subgraph(self, s, anchors) -> Evidence:
        # generic fallback: anchor on a system, ler, or cause, expand 2 hops
        seed_pred, params = None, {}
        if anchors.get("system_code"):
            seed_pred, params = self._sys_match(anchors)
            seed_pred = f"(a:System) WHERE {seed_pred}"
        elif anchors.get("ler_key"):
            seed_pred, params = "(a:LER {key:$k})", {"k": anchors["ler_key"]}
        elif anchors.get("cause_code"):
            seed_pred, params = "(a:Cause {cause_code:$k})", {"k": anchors["cause_code"]}
        if not seed_pred:
            return self._empty("subgraph", anchors)
        rows = list(s.run(
            f"MATCH {seed_pred} MATCH p=(a)-[*1..2]-(m) "
            "RETURN DISTINCT a.display_name AS anchor, type(last(relationships(p))) AS rel, "
            "  m.display_name AS neighbor, labels(m) AS labels LIMIT 60", **params))
        if not rows:
            return self._empty("subgraph", anchors)
        lines = [f"  {r['anchor']} … {r['rel']} … {r['neighbor']} "
                 f"[{[l for l in r['labels'] if l != 'Entity'][0]}]" for r in rows]
        return Evidence("subgraph", anchors,
                        f"Neighborhood of {rows[0]['anchor']}:\n" + "\n".join(lines))

    def _t_out_of_corpus(self, s, anchors) -> Evidence:
        return self._empty("out_of_corpus", anchors)
