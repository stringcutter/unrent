# unrent

Find the rented parts of your AI stack. Swap them for open source.

- Lists every closed AI service a codebase calls, with file and line.
- Names the open source replacements, ranked.
- Ranks the open source you already run against its field.

Offline. No account. No upload. No telemetry.

```
$ unrent scan my-app/

Found 2 closed AI services, and 2 open source components already in use.

| Closed service | Category        | Replace with                                                     |
|----------------|-----------------|------------------------------------------------------------------|
| OpenAI API     | LLM API         | Open-weight LLM: XiaomiMiMo/MiMo-V2.6-Pro-RL                     |
|                |                 | Inference server: ollama/ollama                                  |
| Pinecone       | Vector database | Vector database: milvus-io/milvus                                |

### OpenAI API
- .env.example:1      OPENAI_API_KEY=****
- main.py:1           from openai import OpenAI
- main.py:5           client = OpenAI()
- requirements.txt:1  openai>=1

## Open source you already run

### faiss (library)
found at main.py:3 — import faiss
- Vector database: #2 of 9 (★ 41.0k). Ranked above it:
  - milvus-io/milvus — server, ★ 46.3k
```

Real output, trimmed and with links stripped. The full report is Markdown and pastes
straight into an issue or PR.

## Install

Not on PyPI yet. Straight from GitHub:

```bash
uvx --from git+https://github.com/niklasmellgren/unrent unrent scan .
```

Python 3.11+. One dependency: PyYAML. With [ripgrep](https://github.com/BurntSushi/ripgrep)
on PATH it runs about 3× faster on large repos (18.4 s → 6.5 s on 5,500 files). Same
results either way.

## Use

```bash
unrent scan .                          # markdown report
unrent scan . --format json -o r.json  # everything, machine-readable
unrent scan . --top 5                  # more alternatives per kind
unrent scan . --skip-tests             # ignore test, spec and fixture code
unrent scan . --exclude "examples/"    # .gitignore syntax; or a .unrentignore file
unrent catalog                         # what it knows
```

## What it detects

**163 closed AI services** in 20 categories: LLM APIs, gateways, embeddings, rerankers,
vector databases, RAG platforms, OCR and document parsing, guardrails, observability,
speech, voice agents, image and video generation, web search, scraping, browser
automation, sandboxes, agent platforms, agent memory.

**88 open source components**: vector databases, search engines, inference servers,
gateways, embedding libraries, document parsers, observability, speech, scraping, agent
memory.

Evidence it reads:

| | |
|---|---|
| Packages | Python (requirements, pyproject, setup.py/cfg, Pipfile, conda), npm and pnpm catalogs, Go, Cargo, Maven, Gradle, NuGet, RubyGems, Composer, pub, SwiftPM |
| Imports | Python (AST, notebooks), JavaScript/TypeScript (`import`, `require`, `import()`, `npm:`, `jsr:`) |
| Install commands | `pip install`, `uv add`, `npm i`, … in Dockerfiles, shell, CI, notebook cells |
| Container images | `image:` in compose and Kubernetes, Helm values, Dockerfile `FROM` |
| Code and config | API hosts, model ids, env vars, SDK symbols, SQL (`CREATE EXTENSION vector`), Terraform |

Every package name in the catalog exists in its registry. Checked weekly.

## What it ignores

- **Local servers behind a compatible SDK.** `OpenAI(base_url="http://localhost:11434/v1")` is Ollama, not OpenAI.
- **Lockfiles.** Your dependencies' dependencies aren't yours.
- **Comments and docs.** Per-language comment syntax. `"/api/*"` in a string is not a comment.
- **Ignored and vendored files.** `.gitignore` with or without git. Submodules scanned. `node_modules` not.
- **Generic names.** `task="transcribe"` isn't Amazon. `import textract` isn't AWS. Your `perplexity.py` isn't Perplexity.
- **Open-weight models.** `gpt-oss`, `deepseek-v3.2` and Ollama tags aren't closed.

Also:

- **Secrets are masked.** `OPENAI_API_KEY=****`.
- **Test-only findings are marked.** `--skip-tests` drops them.
- **Model names alone don't count.** A model id with no SDK, key, host or package behind it is listed apart, as *closed models named in code*.
- **Vendor through vendor.** Azure via `openai`, Claude on Bedrock or Vertex via `anthropic`: the report names the one you actually call.
- **Oversized files are listed, never skipped silently.**

Every rule above has a test that fails without it.

## Accuracy

39 real repos it was never tuned on: provider quickstarts and templates in Python,
TypeScript, Go, Java, Kotlin and C#, plus local-only apps. Pinned commits, hand-labelled
truth ([`eval/`](https://github.com/niklasmellgren/unrent/tree/main/eval)).

| | |
|---|---|
| Precision | **0.993** (137 of 138) |
| Recall | **0.986** (137 of 139) |

Misses: two framework defaults invisible in code. False positive: one API key passed on
under another name. CI fails below 0.98 / 0.97. Closed services only; the open source
side isn't in the corpus yet.

## Ranking

Pools live in [`catalog/alternatives.yaml`](https://github.com/niklasmellgren/unrent/blob/main/catalog/alternatives.yaml). Re-ranked every Monday by a
GitHub Action that merges itself.

- **Projects:** GitHub stars gained in the last 90 days, from unrent's own weekly
  snapshots. Until four weeks exist: total stars. The report says which.
- **Models:** Hugging Face trending. Labs' own releases only, open licence, max two per lab.
- **Open source only.** OSI licence for code. Apache, MIT, BSD or CC-BY for weights.
  Restricted weights are out, whatever the code licence. Archived projects and a year
  without a push are out.
- **No silent rot.** A renamed, archived or relicensed project blocks the auto-merge and
  waits for a human. A pool that empties or halves is not published.

The scanner reads the snapshot shipped with the release. New rankings come with new
releases.

## Contribute

The catalog is YAML. New service: [`catalog/services/`](https://github.com/niklasmellgren/unrent/tree/main/catalog/services). New alternative: a pool in
[`catalog/alternatives.yaml`](https://github.com/niklasmellgren/unrent/blob/main/catalog/alternatives.yaml). False positive or miss: open an issue with the
line that fooled it. [CONTRIBUTING.md](https://github.com/niklasmellgren/unrent/blob/main/CONTRIBUTING.md).

## Licence

[Apache-2.0](https://github.com/niklasmellgren/unrent/blob/main/LICENSE)
