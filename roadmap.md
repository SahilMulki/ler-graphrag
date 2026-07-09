# Roadmap — remaining work after Phase 6

> Companion to `plan.md` (the original, now partly superseded on ordering). Concrete
> remaining-work plan, self-contained for a reviewer without repo access. Personal learning
> project — **not research**; pragmatism over rigor.
> **Revised after a review pass** (dedup correctness fix, clarify-UX details, intent-router
> emphasis, router-vocab scaling, batch mechanics) — changes marked *[review]*.

## Current state (context for a reviewer)

Graph RAG over NRC Licensee Event Reports (LERs). **MVP (Phases 0–6) is complete and committed.**

Pipeline: deterministic NRC Form-366 parse **+** LLM (`claude-sonnet-5`) narrative extraction
→ Pydantic **schema v4.1** (10 node types, 11 edge types) → `resolve.py` merges + canonicalizes
EIIS system/component codes → `score.py` grades against a **frozen 3-doc oracle** → `load_graph.py`
loads into **Neo4j** by `MERGE`-ing on a *graph key* (coded System/Component/Cause/… become
cross-document hubs; event-specific nodes stay per-report; a load-time `LER-[:HAS_CAUSE]->Cause`
bridge + synthesized `INVOLVES` keep each report connected) → `retrieve.py` (LLM router + Cypher
templates, **no Text2Cypher**) → `answer.py` (grounded, cites LER numbers, stamps `oracle|pipeline`).

Corpus so far: **3 hand-marked HPCI-inoperability LERs** — Quad Cities (loaded from its oracle
record as the few-shot exemplar, held out of the eval), Dresden, Limerick. Extraction scores
node-F1 **0.88** / edge-F1 **0.72**; graph = 57 nodes / 60 edges, one connected component; golden
suite **9/9**.

Key modules: `src/{parse_form366, llm, models, resolve, score, pipeline, load_graph, retrieve,
answer, golden_eval, ask}.py`; `prompts/narrative_extraction.md`; `graph/queries.cypher`;
`data/raw/` incl. `ground_truth.json` and `reference/{plants.csv [96 plants], systems_components.csv
[1056 EIIS codes]}`.

## Remaining sequence

1. **Abstain / clarify feature** — next; built on the current 3-doc graph.
2. **Phase 8 — scale-up** to 835 LERs (event dates 2020–2026).
3. **Phase 7 — probabilistic layer** — on the scaled graph.
4. **Vector-RAG baseline + comparison** — capstone.
5. **Phase 9 — writeup / demo.**

## Decisions made since Phase 6 (with reasoning)

- **Order 8 → 7 (scale before probabilistic).** Phase 8 is the thesis-proving, higher-value work;
  Phase 7's honest form wants real observed transition frequencies that only scale provides;
  scaling also shakes out extraction robustness before probability is layered on top. Dependency
  points one way. User is fine with rough probabilities (which would permit 7-first), but 8-first
  still wins.
- **Vector baseline moved to the very end (after Phase 7), split out of Phase 8.** "Phase 8" had
  bundled *scale-up* (a prerequisite) with *the baseline* (a final eval). Split: scale enables both
  Phase 7 and the baseline; the baseline then compares the **finished** system and feeds the
  writeup. Nothing downstream depends on it.
- **Clarify feature built now, before scaling.** Mechanism is scale-independent and testable today;
  the scaled corpus inherits it. Independent of the 7/8 order.
- **Corpus = 835 LERs, 2020–2026**, from `2020s_LERs.xlsx` (INL LER Search export; 837 rows).
  Recency ⇒ clean text (avoids the pre-2000s scanned-OCR cliff); ~90–100 plants (operating fleet);
  a dense ~35-doc HPCI/ECCS core **and** breadth for broader questions. **Not "all ~54k"**: ~$550–2,700,
  days to fetch, poor OCR, unverifiable — no added thesis value (thesis needs *density*, not volume).
- **Dedup on LER number, keeping the latest revision — NOT on accession.** *[review]* R00 and R01 of
  the same event have **different** accessions, so deduping on unique accession can keep multiple
  revisions of one event, which would **double-count in the aggregation questions** (event counts,
  "most common failure mode" — the thesis questions). Dedup on `(docket, year, seq)` keeping the
  highest revision (the derived `ler_number` carries the revision suffix). This matters more than the
  `REVISES` edges; wire `REVISES` only if we also ingest the superseded revisions.
- **Model: `claude-sonnet-5`; no Haiku switch.** Quality validated on Sonnet; cost trivial at 835
  (~$16), no reason to risk a regression. (Ollama seam untested; unused.)
- **Cost controls: prompt caching + Batch API.** Estimate ~$16 (vs ~$42 naive). *[review]* The
  caching portion is an assumption, not a guarantee — batch requests can outlive prompt-cache TTL, so
  hits may under-deliver; **worst case is batch-only ≈ $21** (batch alone halves the naive figure), so
  the run is **~$16–21** regardless. Confirm current Sonnet-5 pricing / intro window against Anthropic
  docs at run time; if desired, verify `cache_read` vs `cache_creation` on the calibration batch and
  re-project — but not required.
- **Eval stays pragmatic.** No hand-marking 835. Frozen 3-doc oracle is the **regression gate**; new
  docs are **spot-checked**; graph sanity (resolve coverage, connectivity, orphan rate) validates the
  build. **Oracle is HPCI-only** — a reviewer suggested hand-marking a couple of non-HPCI docs to
  broaden the regression; **consciously declined** (time cost; the pipeline/structure is judged solid).
  Known limitation, accepted.

---

# Feature plan — abstain / clarify when uncertain

## The gap
Phase 6 handles **empty → refuse** (NEG test) and **single clear subject → answer**. It does **not**
handle **ambiguity**: a single-subject question matching *multiple* candidate events. The system must
**ask to disambiguate** rather than silently pick one. Example: *"what caused inoperability at
Limerick 2"* when several Limerick-2 inoperability LERs exist.

## Design: a three-way outcome, detected structurally in the retriever
Replace answer/refuse with **`Answer | Refusal | Clarification`**.

Detection lives in the retriever, by **candidate cardinality** — not in the answer LLM (letting the
LLM pick which event you meant *is* the guessing we prevent). For a **single-subject intent**:
- 0 candidates → **Refusal**; 1 → **Answer**; >1 → **Clarification**.

**Define "candidate set" crisply:** *the events matching ALL pinned anchors for that intent.* *[review]*
Makes the cardinality branch unambiguous.

Detection is **intent-aware**: single-subject intents (`failure_chain`, single-event `subgraph`) guard
on cardinality; **aggregate intents** (`system_components`, `cause_distribution`, `mitigating_backups`,
`system_failure_modes`) are *meant* to span events → exempt.

## The linchpin: intent classification *[review]*
The feature's correctness rests entirely on the router's single-subject-vs-aggregate call. A
misclassified aggregate → **wrongly clarifies**; a misclassified single-subject → **silently answers
over multiple events** (the exact failure we're preventing). So add **adversarial / borderline intent
test cases** to `golden_eval` that sit near the single/aggregate boundary, asserting the *routed
intent*, not just the answer.

## Clarify UX — single-shot (locked)
Single-shot (return candidates; the user re-asks) over an interactive pick-loop — and for a real
reason beyond simplicity: it keeps the **retriever stateless**, so a `Clarification` is a
**deterministic structured return** that `golden_eval` asserts on (**assert the candidate set, not the
prose question**). A pick-loop adds session state that complicates the CLI and the eval, and is a
trivial later addition behind the same `Outcome` type.

**Load-bearing caveat *[review]*:** single-shot only works if the re-ask is *resolvable*. Same-plant
events may differ only by date/title, which the router can't anchor on (it anchors on system/cause/
plant vocab). So the **primary re-ask path is by LER number** (shown in the candidate list,
unambiguous). **Verified present** in `retrieve.py`: the `ler_key` anchor + `_resolve_ler`
(checks it first) + the subgraph handler already resolve a question anchored on an LER number — so
single-shot does **not** dead-end. (The router must reliably *extract* the LER number the user types;
see the router-vocab change in Phase 8, which also makes this robust at scale.)

## Candidate presentation *[review]*
A `Clarification` returns a short question + candidate LERs with distinguishing fields from the `:LER`
node — **LER# · event date · title · system**. Details:
- **Sort by `event_date` descending** so the shown candidates are the likely-intended ones.
- **Cap ~5–8** (never triggers at N=3).
- **On overflow, do NOT hide with "…and M more"** — hidden candidates are unreachable in single-shot.
  Instead tell the user **how to narrow** (add a year, or use an LER#).

## Where it lives
- `retrieve.py` — anchor→candidate resolution, the cardinality branch, a `Clarification`/`Outcome` type.
- `answer.py` — a backstop instruction (decline + ask if evidence spans multiple distinct events for a
  single-subject question).
- `ask.py` — render clarifications.
- `golden_eval.py` — ambiguity cases + the adversarial intent cases.

## Testing
- **Now (N=3):** *"What caused HPCI inoperability?"* (no plant) → clarify among 3; *"…at Limerick"* → 1
  → answer. This is a **mechanism test** (no-plant), *not* the realistic same-plant case — **don't
  over-tune to it.** *[review]*
- **At scale (Phase 8):** same-plant multi-event ambiguity becomes real; add those cases then.

## Scope / non-goals
Ambiguity is **structural** (candidate cardinality), not a confidence score on answer *content*. Keep
it deterministic and robust.

---

# Phase 8 plan — scale-up to 835 LERs

## Goal
Run the trusted pipeline over 835 LERs (2020–2026), build the full graph, re-run + expand the golden
questions. Shared input for Phase 7 and the baseline. **Baseline excluded** (capstone).

## Steps
1. **Build the fetch list.** `2020s_LERs.xlsx` → regex the `ML…` accession out of each "Accession #"
   HTML cell → `accessions.txt`. **Dedup on LER number (docket, year, seq), keeping the latest
   revision** — not on accession (see the dedup decision; protects aggregation counts). *[review]*
2. **Fetch.** `python fetch_ler.py --from-file accessions.txt --out data/raw` (caches `.json`/`.txt`,
   appends `manifest.csv`). ~835 ADAMS calls with backoff. **Expect some fetch failures; log them and
   keep the run resumable** — the cache already skips fetched docs on re-run. *[review]*
3. **Pipeline changes for scale** *(implementation decisions — see open items)*:
   - **Prompt caching** — mark the shared system(schema)+few-shot block with `cache_control` in
     `llm.py`.
   - **Batch API** — restructure `pipeline.py` into *submit-all → collect → resolve* (Anthropic Message
     Batches, 50% off, async ≤24h). **Mechanics *[review]*:** `custom_id = accession`; handle partial
     per-request failures; reconcile fetched-count vs the 835 target before/after the run.
   - **Router-vocab scaling** *[review]* — `GraphVocab.as_prompt()` currently lists **every** LER
     (key — plant); at 835 that bloats every routing call and degrades routing. Keep only the small
     controlled sets (systems ~50, cause categories ~6) in the router prompt; **resolve plants and
     LER-numbers deterministically in the retriever** (Cypher `CONTAINS` / exact-match / an LER-number
     regex), not via the LLM vocab list. Also underpins the clarify feature's LER-number re-ask.
4. **Calibrate cost** on the first ~10 docs via `logs/tokens.csv`; confirm the ~$16–21 projection
   (and, if desired, check `cache_read` vs `cache_creation`) before the full run.
5. **Extract + harden.** Reference tables already cover plants/EIIS codes. Watch: parser edge cases on
   new (recent, clean) docs; prompt generalization to new event types (tighten *only* on recurring
   misses); revised/supplement LERs. Pipeline already retries on schema-invalid output; log & triage
   failures (rough bar: investigate if >~10% fail, else drop the bad docs). Keep the **3-doc oracle as
   the regression gate**; spot-check a sample of new docs. Keep the `source: oracle|pipeline` stamp
   visible in answers — it matters more at 835 where one hand-marked doc sits among hundreds. *[review]*
6. **Load the full graph.** Generalize `load_graph.py` `GRAPH_SOURCES` (currently hardcoded 3) to load
   all `out/*.json` + the QC oracle; `--wipe --yes` for a clean build. Verify connectivity, hub
   density, orphan rate, and that **same-plant ambiguity now exists** (for the clarify feature).
7. **Re-run + expand the golden set.** MVP-now questions get richer; add broader-corpus golden
   questions the events now support (**data-driven — decide after extraction**). Sanity-check vs source.

## Gate
Full graph built + connected; cross-document hubs dense; golden questions (incl. some broader ones)
answered; 3-doc oracle regression still green; clarify feature exercises real same-plant ambiguity;
extraction failure rate acceptable/triaged.

## Cost
~$16–21 (Sonnet-5 + cache + batch; cache may under-deliver in batch, batch-only ≈ $21). Calibrate on
the first ~10 docs. Budget ~$30–50 with a re-run buffer. Confirm pricing at run time.

---

# Open decisions

**Resolved by the review** (baked in above): clarify UX = single-shot; candidate cap 5–8 with
date-desc sort + narrow-guidance on overflow; dedup on LER number (latest revision); oracle
extension declined.

**Still open:**
1. **Batch vs sequential** for the 835 run. Batch = 50% off + async (≤24h), restructures `pipeline.py`;
   sequential = simpler, ~2× cost, ~1–2 h. *Recommend batch.* (Phase-8; not needed for the feature.)
2. **Which broader golden questions to add** — data-driven; decide after seeing the extracted corpus.
3. **Extraction failure bar** for triage — *suggest investigate if >~10% fail, else drop.*
4. **`REVISES` edges** — only relevant if we choose to ingest superseded revisions (default: keep only
   latest per LER number, so no `REVISES` needed).
