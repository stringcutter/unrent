# lockin

Scans a codebase. Finds what binds you to a closed AI service. Names the open source
replacement and what it costs you.

No account, no upload, no telemetry, no network call by default. It reads files and
prints a report.

```
$ lockin scan .

# Lock-in report: ai-chatbot

Scanned 2026-09-25 (UTC) · catalog 2026.09 · 33 entries

Found 4 vendor dependencies: 0 locked, 3 friction, 1 portable, 0 not yet assessed.

| Dependency                 | Lock-in  | Confidence | Best open source replacement  | Effort |
|----------------------------|----------|------------|-------------------------------|--------|
| Vercel AI Gateway          | Friction | high       | LiteLLM                       | low    |
| Vercel Blob                | Friction | high       | Any S3-compatible provider    | low    |
| Vercel platform primitives | Friction | medium     | A container and OpenTelemetry | medium |
| Vercel AI SDK              | Portable | medium     | Keep it                       | low    |
```

## Who it is for

Somebody who did not choose these dependencies.

Increasingly, nobody did. An app is forked from a template or built by an agent, and
the stack arrives with it. `vercel/ai-chatbot` — the most-forked AI starter there is —
brings a gateway, an object store and a function runtime, and whoever now owns the
result has never been told what any of it costs to leave.

Meanwhile the hosted option has a marketing budget and the open one does not. Nobody's
job is to tell you about vLLM at the moment you are choosing. This is that.

## Install

```bash
uvx lockin scan .
uv tool install lockin
```

One runtime dependency (PyYAML), Python 3.11+, and `pip install lockin` works — the
build produces ordinary wheels. A tool about exit costs should have one.

## Use

```bash
lockin scan .                        # markdown to stdout
lockin scan . --format json -o r.json
lockin scan . --live                 # also check PyPI and npm, opt-in
lockin scan . --fail-on locked       # non-zero exit, for CI
lockin catalog                       # what the catalog covers
lockin catalog --validate
```

## What it is not

**Not Dependabot or Renovate.** They ask whether a newer version of what you use
exists. This asks whether you should be using it at all. They move you along the
version axis; this one is about the vendor axis. Run both — the first is hygiene, the
second is a decision. Version lag is deliberately not reported here.

**Not a security scanner.** Nothing here says whether a dependency is safe, only how
hard it is to leave.

**Not a migration tool.** It describes; it does not move anything.

## The three levels

**Portable** — an open implementation speaks the same interface. You are paying for
operations, not access. Leaving is a config change.

**Friction** — replaceable, but not by repointing a URL. Real work, and a quality
re-test at the end.

**Locked** — no compatible replacement. Leaving means rewriting against a different
model of the problem.

The label is not a judgement call. Every entry scores three axes — interface
substitutability, data extractability, behavioural equivalence — and the label is
derived:

```
locked    if max(I, D, B) == 2
portable  if I == 0 and D == 0 and B == 0
friction  otherwise
```

The report shows the axes, so you can disagree with a score rather than with a
verdict. An entry whose label does not follow from its axes fails CI.
[METHODOLOGY.md](METHODOLOGY.md) is the codebook: decision rules, effort anchors, the
evaluation protocol and the threats to validity.

Two consequences worth knowing. A closed commercial SaaS can be `portable` — Qdrant
Cloud is, because the engine is the open build and a snapshot moves you in-house. And
a wire-compatible API can fail to be: the OpenAI API scores I0, but open weights behave
differently, so you will re-run your evals. The interface was never the hard part.

## Every alternative states what you lose

Enforced in the test suite, not just in a style guide:

```python
def test_every_alternative_states_what_you_lose(catalog):
    """An alternative with no stated cost is advocacy, not assessment."""
```

A tool that only ever says "self-host it" is an advertisement. Sometimes the managed
service is the right call and the honest answer is "this will cost you six weeks and
you will lose OCR". Say that, and the rest of the report becomes worth reading.

Same reason `vercel.ai-sdk` sits in the catalog labelled **portable**: a scan that
flags everything it recognises is one nobody believes twice.

## Every finding cites its source

```
- `package.json:37` — `"@vercel/blob": "^0.24.1",`
```

File, line, and the text it was found in. A finding that cannot cite its evidence does
not appear. Confidence reflects how many independent signature kinds agreed — a lone
environment variable is weaker than an import plus a Terraform resource.

## Two tiers, because the halves need different evidence

**assessed** — axes scored and a named open alternative someone has run. Full report.

**detected** — a signature and one documentable line about what it binds you through.
No label, no alternative. A test fails if a detected entry carries either.

*Whether* a service binds you is largely documentable: is there a self-hosted build,
does the data export, is the API proprietary. You do not need to have run Pinecone to
establish it has no self-hosted build. *Which* open alternative is best, and what the
move costs, is experience — and stays scarce on purpose.

Coverage is the binding constraint. A scan that returns nothing gets uninstalled the
same day, and "you depend on Clerk for auth, nobody here has assessed the exit" is
worth more than silence: it is where to look next.

## Fetched, not stored

The catalog holds two kinds of claim, and only one has a source that updates itself:

| Upstream source | No upstream source |
|---|---|
| licence, last release, deprecated, yanked | the three axes |
| package renamed or removed | `loses`, `effort` |

Nobody publishes "moving from Pinecone to Qdrant is medium effort and you lose managed
scaling". There is no feed to poll, and no amount of continuous scanning generates it —
only invents it. So the right answer was never a faster cadence: the left column should
not be stored at all.

`--live` checks the packages the scan actually found — a handful, not the whole
catalog — against PyPI and npm. Cached for a day, fetched in parallel, reported under
the finding it belongs to. Opt-in, because the default promise is that nothing leaves
the machine, and it fails soft: an unreachable registry is reported, never fatal, and
never cached as though it were an answer.

```bash
python tools_survey.py    # catalog health: is every alternative still alive
```

## Never our documentation

The catalog records **which** component and **why**. Never **how** — that is what goes
stale, and it is not ours to keep current. Where a vendor ships an official skill, the
component points at it:

```bash
npx skills add qdrant/skills/meta/qdrant-advisor
npx skills add pydantic/skills
```

Where none exists, the entry says so and gives the docs URL. A component pointing
nowhere leaves an agent recalling an API from memory, which is the staleness this
project exists to avoid.

## What it gets wrong

An independent review found the tool naming vendors that were not there — a plain
Postgres helper reported as Pinecone because `upsert(` was a signature, an S3 script
reported as Bedrock because a bare `boto3` import was one, a chat-only app reported
as **locked** into embeddings because `OPENAI_API_KEY` alone was enough. It also
reported any project checked out under a directory named `build` or `target` as
clean, and could not scan its own repository without inventing twelve findings from
its own prose.

Those are fixed and each has a test that fails without the fix. They are recorded
here rather than quietly repaired because a scanner's whole value is that you can
trust a finding, and the honest position is that this one has been wrong before.

Known and unfixed: there is still no measured precision or recall. Until the
evaluation in [METHODOLOGY.md](METHODOLOGY.md) §8 is run against a labelled corpus,
nobody — including us — can say how often it is wrong. `confidence` is an ordering,
not a probability.

A dependency used only in test fixtures is reported like any other. That is usually
correct: a test that mocks Pinecone means you use Pinecone.

## Open source, not open contribution

Read it, run it, fork it. Pull requests are not accepted; issues are — especially "this
assessment is wrong, here is why". The catalog is only worth reading because every
assessed entry was verified by someone who ran the thing.

## Licence

Apache-2.0.
