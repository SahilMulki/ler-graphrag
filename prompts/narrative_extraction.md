# narrative_extraction.md — prompt template (v2)

**v2 changes** (driven by scorecard misses on Dresden + Limerick): added an explicit PART_OF rule
(explicit-only, never inferred); sharpened the backups rule to exclude design-context systems
entirely (no node or edge); directed distinct parallel failures to be split into separate
FailureMode nodes; and preserved the raw SIMILAR_TO stub key (unit digit intact) for the resolver
to canonicalize.

Versioned prompt for the LLM stage (`extract_narrative.py`). It is a **template**:
`extract_narrative.py` fills the `{{...}}` placeholders at runtime and sends SYSTEM + USER to
Claude Sonnet (temperature 0, JSON/tool mode). Output is validated against `models.LERRecord`
with a retry-on-invalid loop.

Placeholders:
- `{{JSON_SCHEMA}}` — `LERRecord.model_json_schema()` from `models.py`.
- `{{FEWSHOT_RECORD}}` — the Quad Cities record from `ground_truth.json` (LER 254-2025-006-00),
  pretty-printed. QC is the exemplar because we have its full answer key but not its raw text,
  so it never appears in the eval set.
- `{{FORM366_FIELDS}}` — JSON of the deterministic parse (identity, reporting_basis, block_13,
  cause_code, category) from `parse_form366.py`.
- `{{ABSTRACT}}` — block-16 text. Omitted entirely when `include_abstract=False` (the A/B toggle).
- `{{NARRATIVE}}` — the 366A narrative text.

---

## SYSTEM

```
You extract a structured failure-analysis knowledge graph from a single U.S. NRC Licensee Event
Report (LER) and return it as JSON conforming to the provided schema. Return JSON only — no prose,
no markdown fences.

INPUTS (all describe the SAME single event):
- FORM-366 FIELDS: deterministic header/coded data (identity, reporting basis, block-13 codes,
  official cause code). AUTHORITATIVE for those fields — copy them into your output unchanged.
- ABSTRACT (block 16): the licensee's short summary. Use it only to orient yourself and to check
  you have not missed the main event/cause/restoration. It is a summary of the same event —
  never create separate or duplicate entities from it. It may be absent.
- NARRATIVE (366A): the full account. AUTHORITATIVE for component/system identifiers, EIIS codes,
  timing, corrective actions, backups, and previous occurrences.

CORE RULES:
1. One event, one graph. Do not duplicate nodes across the abstract and narrative — but do not
   merge two genuinely distinct entities or failures into one node either (see rule 5).
2. EIIS surface forms vary between reports. A system or component may appear as a bracket code
   ("[BJ]"), a parenthetical acronym ("(RCIC)"), or a full name only. Record the bracket code in
   `eiis_code` when it is present in the text. When only an acronym/name is given, leave
   `eiis_code` null and put the exact acronym and full name in `display_name` — the downstream
   resolver assigns the code. NEVER invent or guess an EIIS code.
3. ADS (Automatic Depressurization System) has no EIIS code: emit it as a System node with
   `eiis_code` null.
4. Cause: copy `cause_code` and `category` from FORM-366 FIELDS verbatim — do not re-derive them
   from the narrative's wording (the official code governs the category). Fill `proximate_text`
   quoting the narrative's cause statement closely, and add a short `theme`. If the official code
   is "TBD", set the cause node `provisional: true`.
5. Causal chain: link failure modes with LEADS_TO (earlier -> later); the terminal step LEADS_TO
   the Consequence. Emit exactly one CAUSED_BY, from the chain's origin failure mode to the Cause
   node. Model each physically distinct failure or defect as its OWN FailureMode node — do NOT
   merge two failures into a single node joined by "and" (e.g. a damaged indicating-light socket
   and a blown 125 VDC fuse are two separate FailureModes, not one). When a single trigger
   produces two failures in parallel, that trigger LEADS_TO each of them separately, and each may
   in turn LEADS_TO the same downstream consequence.
6. Backups (BACKED_UP_BY) and design context. Assert BACKED_UP_BY ONLY for systems the narrative
   explicitly states were operable or available DURING the inoperability window ("remained operable
   throughout the event", "was available"). A system named only to describe plant or HPCI design or
   capability — typically in a "System Design" / "Safety Analysis" passage ("HPCI is designed for
   break sizes less than those for which LPCI or Core Spray can protect...") — is DESIGN CONTEXT:
   do NOT model it at all (no System node and no edge), even if it is itself an ECCS system. Only
   create a System node when the event INVOLVES that system, it is a stated-operable backup, or it
   otherwise participates in the failure chain. If it is unclear whether a system was operable
   during the event, omit the BACKED_UP_BY edge.
7. PART_OF is explicit-only — never inferred. Assert PART_OF ONLY when the narrative literally
   states one thing is contained in / a subcomponent of another (e.g. "the turning gear motor" ->
   motor PART_OF turning gear). Do NOT emit a PART_OF edge from a component to its system just
   because they are related — component-to-system membership is carried by the LER-level INVOLVES
   edge and the LEADS_TO chain, not PART_OF. A breaker feeding a valve, or a pump driven by a
   turbine, is a functional/power relationship, not containment — no PART_OF.
8. Corrective actions: one CorrectiveAction node each, with `status` "completed" or "planned".
9. Previous similar occurrence: emit a stub LER node (`stub: true`), capturing the referenced
   plant, unit, and title as node fields/properties, plus a SIMILAR_TO edge to it. Set its `key`
   to the LER number EXACTLY as the report states it (e.g. "1-2022-001", keeping the leading unit
   digit) — do NOT invent, derive, or substitute a docket number for it. The resolver canonicalizes
   the key from the plant and unit.
10. Node ids: use exactly "ler", "unit", and "cause" for those three anchor nodes; invent short
    readable ids for everything else. Every edge's source/target must be an id in your nodes list.
11. Populate `chain` with a one-line arrow summary
    (root cause -> A -> B -> consequence [backups ...]).

Output must validate against this JSON schema:
{{JSON_SCHEMA}}
```

## USER

```
Here is a completed example for a different HPCI event (use it as the target shape and modeling
style, not as content to copy):

{{FEWSHOT_RECORD}}

Now extract the LER below.

[FORM-366 FIELDS]
{{FORM366_FIELDS}}

[ABSTRACT block 16]
{{ABSTRACT}}

[NARRATIVE 366A]
{{NARRATIVE}}

Return the LERRecord JSON only.
```

---

## Assembly notes (`extract_narrative.py`)
- Fill `{{JSON_SCHEMA}}` from `LERRecord.model_json_schema()` so the prompt and validator never
  drift.
- `include_abstract` config flag: when `False`, drop the entire `[ABSTRACT block 16]` section
  (this is the A/B ablation from `phase_4.md` — run both, score on Dresden + Limerick, keep the
  winner).
- Call with temperature 0 and JSON/tool output mode; validate the response with
  `LERRecord.model_validate`; on `ValidationError`, resend with the error text appended and ask for
  a corrected JSON (cap retries, e.g. 2).
- The model will echo the deterministic fields; `resolve.py` re-stamps identity / reporting_basis /
  block_13 / cause_code / category authoritatively afterward and canonicalizes every `eiis_code`
  against `systems_components.csv`, so LLM drift on those fields is corrected, not trusted.

## Prompt-tuning loop
This template is a starting point, not a finished prompt. Iterate it against `score.py`:
watch edge-F1 and cause handling on Dresden + Limerick, and add a targeted instruction only when
the scorecard shows a recurring miss (e.g., if backups leak in from design context, sharpen rule 6).
Bump the version header when you change it, and keep old versions in git.
```
