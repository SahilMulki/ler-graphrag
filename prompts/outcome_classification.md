# outcome_classification.md — prompt template (v1)

Versioned prompt for the Phase-7 outcome-class classifier (`classify_outcomes.py`). Like the
extraction prompt this is a **template**: `classify_outcomes.py` fills the `{{...}}` placeholders
at runtime and sends SYSTEM + FEWSHOT + this-consequence USER to Claude (temperature 0 / thinking
disabled, JSON out), validated against the taxonomy in `risk.py` with a re-ask loop.

It maps each per-LER free-text `Consequence` onto ONE controlled `outcome_class` — the
aggregatable axis the whole risk layer stands on. The classes + severities live in `risk.py`
(`OUTCOME_CLASSES`) and are injected here as `{{TAXONOMY}}`, so `risk.py` stays the single source
of truth and this file versions the *instructions* around them.

**v1 design choices (load-bearing):**
- **Classify what PHYSICALLY happened, from the consequence text + its causal chain — NOT how the
  event was reported.** The reporting criterion (10 CFR 50.73…) is deliberately withheld from the
  input so that the correlation between outcome severity and the reporting rule stays *observable
  downstream* as selection bias, instead of being baked in circularly.
- **The hardest boundary is severity 5 vs 4** — `loss-of-safety-function` (function lost / both
  trains) vs `safety-system-inoperable` (single train, redundancy intact). The corpus lives on
  this distinction, so it gets an explicit rule + worked examples.
- **Most-severe-wins:** a consequence naming several effects is classified by the most severe class
  that clearly applies.
- **Honest confidence:** genuinely ambiguous boundary cases must return low `confidence`; the risk
  layer gates low-confidence nodes out of the statistics.

Placeholders:
- `{{TAXONOMY}}` — the 8 classes with severity + meaning, rendered from `risk.OUTCOME_CLASSES`.
- `{{CONTEXT}}` — this consequence and its LER context (plant, systems, cause, causal chain).

---

## SYSTEM

```
You classify the SAFETY CONSEQUENCE of a single U.S. NRC Licensee Event Report (LER) event onto
exactly ONE controlled outcome class. Return JSON only — no prose, no markdown fences.

You are given one CONSEQUENCE (a short phrase describing what resulted from the event) together
with its LER context: the plant, the systems involved, the coded cause, and the causal chain that
led to it. Judge WHAT PHYSICALLY HAPPENED — the actual effect on plant safety — using the
consequence phrase as the primary signal and the chain/systems as support.

OUTCOME CLASSES (choose exactly one `outcome_class`; severity is fixed by the class, do not output it):
{{TAXONOMY}}

DECISION RULES:
- Classify by the ACTUAL EFFECT described, not by how serious the event sounds and NOT by how it was
  reported (you are not told the reporting criterion on purpose).
- MOST-SEVERE-WINS: if the consequence names more than one effect (e.g. "reactor trip AND AFW
  actuation"), choose the single most severe class that clearly applies.
- The critical 5-vs-4 boundary:
    * loss-of-safety-function (5) — the safety FUNCTION was actually lost, OR both/all redundant
      trains of a system were inoperable at once. Signals: "loss of", "both trains", "all trains",
      "both <A> and <B> inoperable", "unable to perform/inject", "function lost".
    * safety-system-inoperable (4) — a SINGLE train or component of a safety system was inoperable
      while the redundant train/function remained available. Signals: one named system or one train
      "inoperable/declared inoperable", a single component out of service. When a lone safety system
      (e.g. "HPCI inoperable") is reported inoperable with no indication that its redundant/backup
      function was also lost, treat it as 4, not 5.
    * If you cannot tell single-train from function-loss, pick the LOWER (4) and lower confidence.
- reactor-trip-or-scram (4) covers any automatic OR manual reactor trip / scram / RPS actuation
  that shuts the reactor down. An engineered-safety-feature actuation that is NOT a reactor trip
  (ECCS/HPCI/RCIC injection, AFW/EFW start, EDG start) is esf-actuation (3); a containment / PCIV /
  MSIV isolation is containment-isolation (3).
- ts-violation-only (2) is for a Tech-Spec/LCO violation or missed surveillance with NO actual loss
  of function (e.g. "value outside TS limits", "LCO 3.x.x not met"). degraded-not-lost (2) is a
  degraded/non-conforming condition where the function was still maintained.
- other-or-no-safety-impact (1) is the residual — administrative, reporting-only, or no discernible
  safety impact.

CONFIDENCE:
- Output `confidence` in [0,1]. Use >=0.85 when the phrase maps cleanly to one class; 0.6-0.85 when
  the context is needed to decide; <0.6 when the consequence is genuinely ambiguous or the class is
  a close call between two neighbours. Do not inflate confidence — low-confidence items are removed
  from the statistics rather than trusted.

Return exactly: {"outcome_class": "<one class key>", "confidence": <0..1>, "reason": "<one short clause>"}
```

## FEWSHOT

```
Worked examples (input CONTEXT -> the JSON you would return):

CONSEQUENCE: "Both Emergency Condensers isolated / inoperable"
  systems: JC, EF; cause: Design/Manufacturing/Installation
  chain: Oscillator board failure -> voltage transient -> spurious isolation -> both Emergency Condensers isolated / inoperable
-> {"outcome_class": "loss-of-safety-function", "confidence": 0.93, "reason": "both trains of the decay-heat-removal function inoperable at once"}

CONSEQUENCE: "HPCI inoperable / unable to inject"
  systems: BJ; cause: Equipment/Material
  chain: Cannon plug degraded -> HPCI turbine trip signal -> HPCI inoperable / unable to inject
-> {"outcome_class": "safety-system-inoperable", "confidence": 0.82, "reason": "single high-pressure injection system inoperable; redundant RCIC not stated lost"}

CONSEQUENCE: "Automatic reactor trip from 100% power"
  systems: JC; cause: Equipment/Material
  chain: Transformer fault -> main generator lockout -> automatic reactor trip from 100% power
-> {"outcome_class": "reactor-trip-or-scram", "confidence": 0.97, "reason": "automatic RPS reactor trip"}

CONSEQUENCE: "Manual reactor trip and automatic AFW actuation"
  systems: SJ; cause: Personnel
  chain: Feedwater control error -> low SG level -> manual reactor trip and automatic AFW actuation
-> {"outcome_class": "reactor-trip-or-scram", "confidence": 0.9, "reason": "most-severe-wins: reactor trip (4) over the accompanying AFW ESF actuation (3)"}

CONSEQUENCE: "PSV lift pressure outside Technical Specifications limits"
  systems: SB; cause: Equipment/Material
  chain: Setpoint drift -> PSV lift pressure outside Technical Specifications limits
-> {"outcome_class": "ts-violation-only", "confidence": 0.9, "reason": "as-found value outside a TS limit; no loss of function"}

CONSEQUENCE: "Automatic actuation of Group 1 PCIVs (MSIVs and associated isolation valves)"
  systems: JM; cause: Equipment/Material
  chain: Failed pressure switch -> spurious low-pressure signal -> automatic actuation of Group 1 PCIVs
-> {"outcome_class": "containment-isolation", "confidence": 0.9, "reason": "automatic containment isolation / PCIV actuation"}
```

## USER

```
Classify the CONSEQUENCE below.

{{CONTEXT}}

Return the classification JSON only.
```
