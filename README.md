# Dynamic Risk RAG — Graph RAG over NRC Licensee Event Reports

A knowledge-graph RAG system that answers **multi-hop, cross-document** questions about
nuclear-plant failures — the kind of relational, "connect-the-reports" questions that flat
vector search structurally cannot. It ingests U.S. NRC **Licensee Event Reports (LERs)**,
extracts a failure-analysis knowledge graph from each one, resolves entities across reports,
loads them into **Neo4j**, and answers grounded questions with citations back to the source LERs.

Built as a personal project to learn knowledge graphs, retrieval, and LLM extraction pipelines
end-to-end — with an eye toward dynamic probabilistic risk analysis (PRA) for safety-critical
systems. See [phase_0.md](phase_0.md) for the original framing and [plan.md](plan.md) for the
full build plan.

> **Status:** the MVP (Phases 1–6) is complete and verified. Extraction scores node-F1 **0.88** /
> edge-F1 **0.72** against a hand-marked oracle; the loaded graph is a single connected component;
> the golden-question suite passes **9/9**. Probabilistic reasoning (Phase 7) and full-corpus
> scale-up + a vector-RAG baseline (Phase 8) are the remaining stretch work.

---

## Why a graph?

The corpus is a set of independent failure reports. The interesting questions are *relational*:

- *"What chain of failures led to HPCI being inoperable at Limerick?"* — a **multi-hop** path within one report.
- *"What components have failed in the HPCI system across the whole corpus?"* — a **cross-document** join on a shared system.
- *"Which events trace back to a weak maintenance program?"* — a **cross-document** join on a normalized cause.

A vector store can retrieve *a* relevant chunk, but it can't assemble a causal chain or join three
reports through a shared component code. A graph keyed on the NRC's own **EIIS component/system codes**
does both natively. The full list of target questions lives in [phase_0.md](phase_0.md#golden-questions).

## Demo

```
$ python src/ask.py "What chain of failures led to HPCI being inoperable at Limerick?"

  routed intent : failure_chain   anchors={'plant': 'Limerick'}
  retrieved LERs: 353-2025-001-00 [pipeline]
  answer: At Limerick (LER 353-2025-001-00), the failure chain began with a degraded
    Cannon plug connector, which caused an electrical short circuit within the ECCS
    Contact Test Unit. This short circuit resulted in the HPCI Turbine Exhaust Line
    Vacuum Breaker Outboard PCIV closing, which in turn rendered the HPCI System
    inoperable, constituting a loss of safety function. Root cause: Design/Manufacturing/
    Installation (B). Backups available: ADS, RCIC, RHR, Core Spray.
  citations: 353-2025-001-00 [pipeline]
```

```
$ python src/ask.py "What components have failed in the HPCI system across the whole corpus?"

  routed intent : system_components   anchors={'system_code': 'BJ'}
  retrieved LERs: 237-2025-003-00 [pipeline], 254-2025-006-00 [oracle], 353-2025-001-00 [pipeline]
  answer: ...at Dresden — the HPCI Turbine, HPCI pumps, a Negative 125 VDC fuse, the stop
    valve indicating light circuit, the turning gear, the turning gear motor; at Quad
    Cities — a breaker feeding an MOV, an opening coil, a roller assembly, a thermal
    overload relay, Turbine Inlet MOV 1-2301-3; at Limerick — a cannon plug connector,
    the ECCS Contact Test Unit, the HPCI Turbine Exhaust Line Vacuum Breaker PCIV.
  citations: all three LERs
```

The second answer is the payoff: **one query fuses three separate reports** through the shared
`System:BJ` (HPCI) hub — something no single-document retriever can do.

## How it works

```
 raw LER (NRC ADAMS)
        │
        ▼
 ┌──────────────────────────────────────────────────────────┐
 │ EXTRACTION  (Phase 4)                                     │
 │  parse_form366.py  deterministic Form-366 header/blocks   │  ← authoritative
 │  llm.py + prompt   LLM narrative → schema JSON            │  ← causal chain
 │  resolve.py        merge + canonicalize EIIS codes        │
 │  score.py          grade vs ground_truth.json (oracle)   │
 └──────────────────────────────────────────────────────────┘
        │  validated LERRecord (Pydantic, schema v4.1)
        ▼
 ┌──────────────────────────────────────────────────────────┐
 │ GRAPH BUILD  (Phase 5)  load_graph.py                    │
 │  MERGE on a graph key (not record-local id) → coded       │
 │  System/Component/Cause/… become cross-document hubs;     │
 │  event-specific nodes stay per-report; connectivity       │
 │  normalized; loaded into Neo4j                            │
 └──────────────────────────────────────────────────────────┘
        │  connected Neo4j graph
        ▼
 ┌──────────────────────────────────────────────────────────┐
 │ RETRIEVAL + ANSWER  (Phase 6)                            │
 │  retrieve.py  LLM router → Cypher templates → subgraph    │
 │  answer.py    Claude, grounded in evidence, cites LERs    │
 │  ask.py       CLI + golden-question eval                  │
 └──────────────────────────────────────────────────────────┘
```

**Mixed extraction.** The deterministic parser owns everything structured (identity, coded
Block-13 table, official cause code) and is authoritative; the LLM owns only the narrative
(the causal chain, corrective actions, backups). This keeps the fragile part small and the
factual part exact.

**Entity resolution is up-front, not a graph-merge afterthought.** Every node carries a
canonical `match_key` (`System:BJ`, `Component:V|1-2301-3`, `Cause:Design/Manufacturing/Installation`).
Coded systems/components anchor to their EIIS codes; un-coded ones use deterministic fuzzy matching
(`rapidfuzz`); free-text causes normalize into shared categories. The graph loader then just
`MERGE`s on that key, so the same system across three reports is one node.

## The knowledge graph (schema v4.1)

10 node types and 11 edge types, drawn directly from the LER structure — full spec in
[ler_schema_v4.1.md](ler_schema_v4.1.md).

| Nodes | Edges |
|---|---|
| LER, Unit, System, Component, FailureMode, Cause, Consequence, CorrectiveAction, Manufacturer, RegulatoryReference | OCCURRED_AT, INVOLVES, LEADS_TO, CAUSED_BY, MITIGATED_BY, BACKED_UP_BY, REPORTED_UNDER, MANUFACTURED_BY, PART_OF, SIMILAR_TO, REVISES |

**Cross-document hubs** are the coded/canonical types (System, Component, non-provisional Cause,
Unit, Manufacturer, RegulatoryReference, LER). **Event-specific** types (FailureMode, Consequence,
CorrectiveAction, and provisional causes) are keyed per-report so distinct events never collapse.
At load time each report is wired into one connected subgraph with a structural
`LER-[:HAS_CAUSE]->Cause` bridge (details and rationale in [phase_5.md](phase_5.md)).

## Results

| Stage | Metric |
|---|---|
| **Extraction** (Phase 4) | identity 100%, cause-code 100%; aggregate **node-F1 0.88 / edge-F1 0.72**; Limerick 1.00 / 0.94 |
| **Graph** (Phase 5) | 57 nodes, 60 edges, **one connected corpus component**; hubs `System:BJ`, ADS, `50.73(a)(2)(v)(D)`, `System:BN` link the reports |
| **Retrieval** (Phase 6) | golden suite **9/9 pass**; retrieval recall 1.00 where applicable; out-of-corpus questions refused (no hallucination) |

Evaluation is honest about the small corpus: cross-document *aggregation* questions that need more
than three documents (e.g. "most common failure mode") are marked **materialize-at-scale** and
claim no graph-vs-vector "winner" until Phase 8.

## Corpus

Three real LERs, all High-Pressure Coolant Injection (HPCI) inoperability events, chosen so the
cross-document links appear quickly:

| LER | Plant | Event | In graph via |
|---|---|---|---|
| 254-2025-006-00 | Quad Cities 1 | Turbine inlet valve failed to open | **oracle** (few-shot exemplar, held out of the extraction eval to avoid leakage) |
| 237-2025-003-00 | Dresden 2 | Failed indicating light + blown fuse | **pipeline** |
| 353-2025-001-00 | Limerick 2 | HPCI valve isolated by a degraded test connector | **pipeline** |

`ground_truth.json` is a frozen, hand-marked answer key; scorer tolerances live in `score.py` so
the oracle is never edited to fit the model.

## Repository layout

```
src/
  parse_form366.py   deterministic Form-366 parser
  llm.py             thin Claude interface (swappable; token logging)
  models.py          Pydantic v2 schema v4.1 (LERRecord, nodes, edges)
  resolve.py         merge parse + LLM, canonicalize EIIS codes
  score.py           grade extraction vs the oracle
  pipeline.py        raw LER → validated JSON → scored  (Phase 4)
  load_graph.py      load records into Neo4j            (Phase 5)
  retrieve.py        LLM router + Cypher templates       (Phase 6)
  answer.py          grounded, LER-citing answerer       (Phase 6)
  golden_eval.py     golden-question eval spec + scoring
  ask.py             CLI: ask a question / run the eval
prompts/             versioned extraction prompt
graph/queries.cypher constraints + gate/verification Cypher
data/raw/            LER text + reference tables + ground_truth.json
out/                 extracted records (Dresden, Limerick)
ler_schema_v4.1.md   the schema spec
phase_*.md           per-phase notes; plan.md = the overall build plan
```

## Setup & usage

**Prerequisites:** Python 3.12+, [Neo4j](https://neo4j.com/download/) (Community Edition, local via
Neo4j Desktop), and an Anthropic API key.

```bash
pip install -r requirements.txt
```

Create a git-ignored `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
NEO4J_URI=bolt://localhost:7687      # or neo4j://127.0.0.1:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your-db-password
ADAMS_APS_KEY=...                    # only for fetching new LERs
```

Then, with Neo4j running:

```bash
python src/pipeline.py                 # extract + score Dresden & Limerick  → out/
python src/load_graph.py --dry-run     # validate the graph build in memory (no DB)
python src/load_graph.py               # load into Neo4j (idempotent; --wipe --yes to reset)
python src/load_graph.py --verify      # run the gate queries against the DB

python src/ask.py "Which events were mitigated by a redundant safety system?"
python src/ask.py --golden             # run the full golden-question eval with scoring
```

To grow the corpus, `fetch_ler.py` pulls LER text from the NRC ADAMS public library by accession
number (see `data/raw/manifest.csv`).

## Build status

| Phase | | |
|---|---|---|
| 0 | Frame + golden questions | ✅ |
| 1 | Acquire & hand-read LERs | ✅ |
| 2 | Design the schema (→ v4.1) | ✅ |
| 3 | Pick the framework (custom Python + LLM) | ✅ |
| 4 | Extraction pipeline | ✅ |
| 5 | Entity resolution + Neo4j graph | ✅ |
| 6 | Graph retrieval + grounded answering | ✅ |
| 7 | Probabilistic layer (PRA-style path ranking) | ⬜ stretch |
| 8 | Scale-up + vector-RAG baseline comparison | ⬜ stretch |

## Design decisions worth calling out

- **Deterministic-first extraction** — coded fields are parsed, not guessed; the LLM only fills the narrative.
- **Deterministic fuzzy matching** over embeddings for un-coded entity resolution (transparent, free, good enough given EIIS codes carry most identity).
- **Frozen oracle** — the answer key is never edited to fit the model; tolerances live in the scorer.
- **Vector baseline deferred to Phase 8** — with three documents there is no retrieval pressure (top-k returns the whole corpus), so a graph-vs-vector comparison now would be non-robust. The retrieval layer sits behind a `Retriever` interface so the baseline drops in cleanly at scale.
- **No Text2Cypher** — retrieval uses an LLM router with anchors constrained to the graph's real vocabulary plus vetted Cypher templates, which is far more robust than free-form LLM-generated Cypher.

## Tech stack

Python · Pydantic v2 · rapidfuzz · Anthropic SDK (Claude Sonnet) · Neo4j (Community) + `neo4j` driver

## Data & references

Source data is public: the [NRC ADAMS](https://www.nrc.gov/reading-rm/adams.html) library of Licensee
Event Reports, structured per NRC Form 366/366A and the EIIS component/system code standards (IEEE 803.1 /
805). Domain framing follows NUREG-1022. This is a personal learning project and is not affiliated with
or endorsed by the NRC.
