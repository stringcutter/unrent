# unrent

Point it at a codebase. It lists every closed AI service the code depends on, where,
and the open source projects and open-weight models that replace each one — ranked by
momentum: GitHub star growth for projects, Hugging Face trending for models.

```
$ unrent scan my-app/
# AI dependencies in `my-app`

Scanned 2026-09-26 · 106 closed AI services in the catalog · alternatives ranked 2026-09-26

Found **2** closed AI services.

| Closed service | Category | Replace with |
|---|---|---|
| OpenAI API | LLM API | Open-weight LLM: [XiaomiMiMo/MiMo-V2.6-Pro-RL](https://huggingface.co/XiaomiMiMo/MiMo-V2.6-Pro-RL)<br>Inference server: [ollama/ollama](https://github.com/ollama/ollama) |
| Pinecone | Vector database | Vector database: [milvus-io/milvus](https://github.com/milvus-io/milvus) |

## Where each one is used

### OpenAI API

5 locations:

- `.env.example:1` — `OPENAI_API_KEY=****`
- `main.py:1` — `from openai import OpenAI`
- `main.py:5` — `resp = client.chat.completions.create(model="gpt-4o", messages=[])`
...
```

The report is Markdown, so it renders as a table in a pull request or issue.

Built for the moment you fork a template or inherit an app and want to know which AI
components are rented, and what the best open replacement is today.

## Install

```bash
uvx unrent scan .     # run once, nothing installed
pipx install unrent   # or keep it
```

Python 3.11+, one dependency (PyYAML). No account, no upload, no network: the scan
reads files and prints a report.

If [ripgrep](https://github.com/BurntSushi/ripgrep) is on your PATH, unrent uses it to
find candidate lines, and large monorepos scan two to three times faster. The results
are identical either way; the test suite runs every detection test in both modes.

## Use

```bash
unrent scan .                          # markdown report
unrent scan . --top 5                  # more alternatives per kind
unrent scan . --format json -o r.json  # every ranked alternative, every location
unrent scan . --skip-tests             # leave test, spec and fixture code out
unrent scan . --exclude "examples/"    # .gitignore syntax; or put it in .unrentignore
unrent catalog                         # what is covered
```

## What it finds

About 100 closed AI services: LLM APIs (OpenAI, Anthropic, Gemini, Vertex, Bedrock,
Azure OpenAI, Mistral, xAI, Groq, Together, Fireworks, …), gateways, embeddings and
rerankers, vector databases, RAG platforms, document parsing and OCR, observability,
speech-to-text, text-to-speech, voice agents, image generation, web search and
scraping, browser automation, code sandboxes and agent memory.

A dependency is recognised from whichever evidence the codebase has:

| Evidence | Read from |
|---|---|
| Packages | `*requirements*.txt` (continuations and `--hash` lines included), `pyproject.toml` (PEP 621, Poetry, PDM, Hatch, uv groups), `setup.py`, `setup.cfg`, `Pipfile`, conda `environment.yml`, `package.json`, `pnpm-workspace.yaml` catalogs, `go.mod`, `Cargo.toml`, `pom.xml`, Gradle and version catalogs, `.csproj` / `Directory.Packages.props`, `Gemfile`, `composer.json`, `pubspec.yaml`, `Package.swift` |
| Imports | Python (AST, `from pkg import module`, `importlib.import_module`, Jupyter notebooks of any size), JavaScript and TypeScript `import` / `require` / `import()` including `npm:` and `jsr:` specifiers |
| Install commands | `pip install`, `uv add`, `poetry add`, `npm i`, `pnpm add`, … in Dockerfiles, Containerfiles, shell scripts, CI config and notebook `!pip` cells, across `\` line continuations |
| Framework integrations | LangChain, LlamaIndex, Vercel AI SDK, Spring AI, LangChain4j, Semantic Kernel provider packages |
| API hosts | `api.openai.com`, `*.openai.azure.com`, `ai-gateway.vercel.sh`, … in source or config of any language, HTML `<script>` blocks and Helm templates included |
| Model ids | `gpt-4o`, `claude-sonnet-4`, `gemini-2.5-pro`, `xai/grok-4`, … — but not open-weight ones like `gpt-oss` or `deepseek-v3.2`, and not local Ollama tags like `command-r:35b` |
| Environment variables | read in code in any language, or set in `.env*`, compose files, Helm charts and CI config |
| Terraform | `azurerm_search_service`, `aws_bedrockagent_agent`, … |

Every finding cites file, line and text, with API keys and tokens masked, so a report
can be pasted into an issue. Where one vendor is reached through another's SDK — Azure
through `openai`, Claude on Bedrock through `anthropic`, Groq through the OpenAI SDK
with a Groq base URL — the report names the vendor actually called.

What it deliberately does not count:

- **Self-hosted models behind a compatible SDK.** `OpenAI(base_url="http://localhost:11434/v1")`
  is Ollama, not OpenAI. A local or private base URL in a file (or in `.env` and
  config, for the whole project) means the `openai` package there is a client for a
  server you run.
- **Lockfiles.** They list what your dependencies depend on. A gateway library that
  pulls in the `openai` SDK does not make you an OpenAI customer.
- **Comments, docstrings and docs.** Writing about a vendor is not using one. Comment
  syntax is per language, and comment markers inside strings are text.
- **Ignored and vendored files.** `.gitignore` is respected with or without git;
  submodules are scanned; `node_modules`, `vendor`, virtualenvs and minified bundles
  are skipped. Files too large to read are listed in the report, never dropped silently.
- **Generic names.** `task="transcribe"` is Whisper, not Amazon Transcribe;
  `import textract` is an open source library; `from fireworks import Firework` is a
  workflow library; your own `perplexity.py` is not Perplexity; `HF_TOKEN` downloads
  open weights. Signatures that are ambiguous on their own need a second, different
  signature to count.

Findings whose only evidence is in test, spec or fixture code are marked *only in
tests*: usually a mock of a real dependency, sometimes a leftover.

Each of these is pinned by a test that fails without it.

## How alternatives are chosen

Each closed service names the kinds of thing that replace it: the OpenAI API is
replaced by an open-weight LLM *and* an inference server; Pinecone by a vector
database. Each kind is a pool in [`catalog/alternatives.yaml`](https://github.com/niklasmellgren/unrent/blob/main/catalog/alternatives.yaml),
and `scripts/refresh.py` ranks every pool weekly:

- **Open source projects** are ranked by momentum: GitHub stars gained over the last
  90 days. GitHub no longer exposes when stars were given, and the public event
  archives have undercounted since 2025, so unrent keeps its own history in
  [`catalog/star-history.json`](https://github.com/niklasmellgren/unrent/blob/main/catalog/star-history.json). Until that history covers
  four weeks, a pool is ranked by total stars — which is where it stands today — and
  the report says so.
- **Open-weight models** are discovered, not listed: the listed labs' own models
  (not community fine-tunes or re-quantisations) under an open licence, ranked by
  Hugging Face's trending score, at most two per lab. Trending favours new releases:
  the top model may be days old. Read the model card before you bet on it.
- **Only open source counts.** An OSI-approved licence for code; Apache, MIT, BSD or
  CC-BY for weights. A project that runs a model is only as open as its weights, so
  tools with revenue-capped or use-restricted weights are left out even when their
  code is Apache or GPL. So are archived projects and projects without a push in a
  year.
- **Rankings can't silently rot.** The refresh fails loudly when a listed project is
  renamed, archived or relicensed; keeps last week's data for a project it could not
  reach; and refuses to publish when a pool would empty or halve — that is an
  outage, not news.

The scanner never touches the network: it reads the ranking snapshot that ships with
the release, so upgrading unrent is how you get newer rankings.

## Contributing

The catalog is plain YAML. To cover a new service, add it to
[`catalog/services/`](https://github.com/niklasmellgren/unrent/tree/main/catalog/services) with its signatures and the pools that replace
it. To suggest an alternative, add it to a pool in
[`catalog/alternatives.yaml`](https://github.com/niklasmellgren/unrent/blob/main/catalog/alternatives.yaml) — the ranking decides where it
lands. Every false positive or missed dependency is a bug: please open an issue with
the line that fooled it. See [CONTRIBUTING.md](https://github.com/niklasmellgren/unrent/blob/main/CONTRIBUTING.md).

## Licence

[Apache-2.0](https://github.com/niklasmellgren/unrent/blob/main/LICENSE).
