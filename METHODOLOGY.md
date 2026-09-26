# Methodology

How `lockin` decides what it decides. This document is the codebook: it exists so
that two people assessing the same dependency independently arrive at the same
label, and so that a reader can disagree with a specific criterion rather than with
a vibe.

If a rule here and an entry in the catalog disagree, the rule wins and the entry is
a bug.

---

## 1. Unit of assessment

The unit is not "a vendor". It is:

> **one dependency, assessed against one named open source alternative, for a
> stated usage profile.**

All three parts matter.

**Named alternative.** "How locked in are we?" has no answer. "How hard is it to
move from Pinecone to Qdrant?" does. A score without a destination is not a
measurement.

**Usage profile.** The same service binds you differently depending on how deeply
you use it. AWS Bedrock used only for `invoke_model` is a rewrite of call sites.
AWS Bedrock used with Guardrails and Agents is an architecture. The catalog assigns
its label to the **basic profile** — the service used for its core function — and
records under `escalates_when` the specific features that push it a level higher.

This is precisely the judgement a static scanner cannot make, and it is why layer 3
exists: the scanner reports the dependency and cites the lines; an agent or a human
reads those lines and decides which profile applies.

---

## 2. The three axes

Every assessment scores three axes from 0 to 2. The axes are deliberately about
observable properties, not about how the vendor is regarded.

### Interface substitutability (I)

*Can the alternative accept the same calls?*

| | Criterion | Test |
|---|---|---|
| **I0** | Wire-compatible. An open implementation accepts the same requests. | Can you switch by changing configuration (endpoint, credentials, model name) without editing call sites? |
| **I1** | Rewrite. Call sites change, but every concept maps onto a concept in the alternative. | Can each operation you use be expressed in the alternative without inventing a new component? |
| **I2** | Redesign. At least one capability you use has no functional equivalent. | Does replacing it require building or introducing a component that does not exist in the alternative? |

### Data extractability (D)

*Can you take your state with you and have it still mean something?*

| | Criterion | Test |
|---|---|---|
| **D0** | No vendor-held state, or state exports to a documented format and loads directly into the alternative. | Is there a supported export whose output the alternative ingests as-is? |
| **D1** | Exportable, but must be transformed or re-created. | Can you get the bytes out, accepting that you must reshape or re-index them? |
| **D2** | Semantically bound. The exported state is not meaningful outside the vendor's model. | Would the exported data have to be *regenerated* rather than converted? |

D2 is rarer than it looks and is the axis people miss. Embeddings are the canonical
case: the vectors export fine and are worthless, because vectors from one model
carry no meaning for another. The corpus must be re-embedded.

### Behavioural equivalence (B)

*Does the replacement do the job as well?*

| | Criterion | Test |
|---|---|---|
| **B0** | Functionally identical. Same software, or output determined by data rather than by a model. | Would your existing tests pass unchanged after the switch? |
| **B1** | Differs; requires re-validation and probably re-tuning. | Must you re-run evals and expect to adjust thresholds, prompts or parameters? |
| **B2** | No open alternative reaches comparable quality on the stated task. | Is there published or first-hand evidence that the gap is not closeable by tuning? |

B2 must be argued, not asserted, and it should be rare. A claim that no open model
can do something is a claim with a short shelf life, and claiming it carelessly is
how this catalog would lose its credibility in the other direction.

---

## 3. Assigning the label

The label is a function of the three axes, not a separate judgement:

```
locked    if max(I, D, B) == 2
portable  if I == 0 and D == 0 and B == 0
friction  otherwise
```

This rule is enforced in the test suite. An entry whose declared label does not
follow from its declared axes fails CI, which means the codebook cannot quietly
drift away from the catalog.

Two consequences worth stating plainly, because both are counter-intuitive:

**A service can be closed, commercial and proprietary and still be `portable`.**
Qdrant Cloud is a paid SaaS. It scores I0/D0/B0 because the engine is the open
source build, a snapshot restores in-house, and behaviour is identical. You are
buying operations. That is the cheapest dependency there is.

**A service can be wire-compatible and still not be `portable`.** The OpenAI API
scores I0 — vLLM speaks the same protocol — but B1, because open weights behave
differently and you will re-run your evals. The interface was never the hard part.

---

## 4. Migration effort

Effort is reported in levels, never in currency. Anyone quoting a figure in kroner
from a static scan is guessing, and the guess will be wrong in the direction that
suits whoever is quoting.

| Level | Anchor |
|---|---|
| **low** | Under a week for one engineer. Configuration and small client changes. No data migration, no re-validation beyond a smoke test. |
| **medium** | One to six weeks. Call sites rewritten, or data exported and re-indexed, and an eval pass at the end. |
| **high** | Over six weeks, or requires rebuilding a component with no equivalent, or re-processing the entire corpus. |

Effort is scored for a single service in a single system of moderate size. It does
not scale linearly with the number of call sites, and the catalog does not pretend
to know the size of your codebase.

---

## 5. The `loses` field

Every alternative must state what you give up. This is enforced by a test, and it
is the rule that separates this catalog from marketing.

A valid `loses` names something concrete that the alternative does not do, or a
new operational burden the reader takes on. "Requires self-hosting" alone is not
enough — say what that costs in this specific case.

Writing "nothing" is not permitted. If an alternative genuinely costs nothing, the
dependency was not a dependency and does not belong in the catalog.

Where the honest answer is that the managed service is the better choice, say so in
`loses`. A catalog that always concludes "self-host it" is an advertisement, and
readers detect that faster than they detect a wrong label.

---

## 6. Evidence standards for detection

A signature may go in `detect` only if a match is **specific to that service**.

Permitted, roughly in descending order of strength: an import of a vendor SDK, a
package name in a manifest, a provider-specific IaC resource type, an SDK symbol
that exists only in that vendor's client, a distinctive environment variable name.

Not permitted: generic names (`API_KEY`, `MODEL`, `ENDPOINT`), a string that is a
substring of a more specific identifier, or a signature shared with another
catalogued service unless the entry declares `excludes` and can be distinguished by
independent evidence.

Two rules follow from failures found in testing, both of which produced a report
naming a vendor the codebase did not use:

1. Symbol matching respects identifier boundaries. `OpenAI(` must not match inside
   `AzureOpenAI(`.
2. An entry that declares `excludes` may only claim shared evidence if it also has
   evidence of its own. Otherwise the more general entry is the simpler explanation
   of the same lines and wins.

**Every finding cites file, line and source text.** A finding that cannot cite its
evidence is not reported. This is not a nicety: it is what lets a reader check the
tool in seconds rather than trusting it.

---

## 7. Confidence

Confidence describes the **detection**, not the assessment. It answers "is this
dependency really here?", never "is this label right?".

It is currently a heuristic: the number of independent signature kinds that agreed,
with a single environment variable counting as weak. It has not been calibrated
against ground truth, so it must not be read as a probability.

This is a known weakness. Until the benchmark in section 8 exists, the honest
position is that `confidence` is an ordering, not a measurement.

---

## 8. Evaluation protocol

Not yet performed. Specified here so that it is performed the same way twice.

**Corpus.** 30–50 public repositories that use the AI stack, selected before
running the tool, by a stated rule (for example: GitHub search for the relevant SDK
names, most-starred first, excluding the vendor's own samples and excluding
anything already used while developing detectors). The selection rule is recorded
with the results; picking repositories after seeing how the tool does on them is
the easiest way to produce a meaningless number.

**Labelling.** Each repository is annotated by hand for which catalogued services
are actually present, before the tool is run on it.

**Reported metrics.**

- Precision and recall overall and per signature kind
- An ablation: results with each signature kind removed, to show what each earns
- The false positive list in full, not just its count

**Precision is the headline.** Missing a dependency is an inconvenience; naming a
vendor that is not there destroys the report's credibility, and one such error
costs more trust than many correct findings earn.

**Determinism.** Two runs over the same corpus with the same catalog version must
produce byte-identical output. This is asserted as a test, not assumed.

**Inter-rater agreement.** Two raters independently score I, D and B for a random
sample of at least ten entries. Cohen's kappa is reported per axis. A kappa below
0.6 means the criteria in section 2 are too vague and must be sharpened — the
finding is about the codebook, not about the raters.

---

## 9. Re-verification

Every entry carries a `verified` date, set by a human who checked the assessment
against the current state of both the service and the alternative.

Entries older than six months are reported as stale and fail the test suite. The
tool is built to complain about itself, because a silently rotting catalog is the
characteristic failure of this kind of project.

Automated checks against the GitHub and package-registry APIs (archived repository,
no recent commits, deprecated package) open issues; they never edit the catalog. A
machine may say "look at this again". Only a human writes `lockin`, `effort` and
`loses`.

---

## 10. Threats to validity

Stated here rather than discovered by a reader.

**Indirection.** When a codebase calls a model through LiteLLM, LangChain or a
gateway, the actual provider is in configuration, not in an import. The scanner
sees the gateway. This is not solvable deterministically in general, and it is the
largest known source of false negatives.

**Usage-profile dependence.** Labels are assigned for the basic profile (section 1).
A codebase using a vendor's deeper features is more locked in than the report says.
`escalates_when` mitigates but does not remove this.

**Single-author bias.** The catalog is currently written by one person. Section 8's
inter-rater protocol exists to measure this; until it is run, the labels reflect one
practitioner's judgement and should be read that way.

**Alternative maturity drifts.** An open source project can be archived between two
scans. Section 9 is the mitigation, and it is partial.

**Transitive dependencies.** A service reached through a wrapper package may not
appear in any manifest the scanner reads. Lockfile parsing would reduce this and is
not yet implemented.

**Not a security tool.** Nothing here says anything about whether a dependency is
safe, only about how hard it is to leave.

---

## 11. Worked examples

Borderline cases, resolved, so the rules above have precedent.

**OpenAI API → vLLM.** I0: vLLM serves an OpenAI-compatible endpoint; switching is
`base_url` and a model name. D0: no vendor-held state. B1: open weights behave
differently on reasoning-heavy and long-context work; evals must be re-run.
→ `friction`. Wire compatibility is not portability.

**Qdrant Cloud → Qdrant.** I0, D0 (snapshot and restore), B0 (same software).
→ `portable`. A paid closed service, correctly labelled portable, because what is
being bought is operations.

**OpenAI Embeddings → BGE-M3.** I0: the call is trivial to swap. D2: existing
vectors are meaningless under a different model and the corpus must be re-embedded.
B1: retrieval thresholds need re-tuning.
→ `locked`, on the data axis alone. The lock-in is in the database, not in the code,
which is why a code review never catches it.

**Azure AI Search → OpenSearch.** I2: skillset-based document cracking (OCR, entity
extraction) has no equivalent and must be rebuilt from separate tools. D1: the index
can be exported but must be re-created. B1: ranking differs.
→ `locked`.

**LangSmith → Langfuse.** I1: decorators and callbacks are swapped. D1: historical
traces do not come with you, but nothing downstream depends on them. B0: observing
a system does not change it.
→ `friction`. Losing history is a real cost and belongs in `loses`, not in the label.
