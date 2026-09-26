# lockin

Point it at a codebase. It lists every closed AI service the code depends on, where,
and the open source projects and open-weight models that replace each one — ranked by
what is actually gaining ground right now.

```
$ lockin scan ai-chatbot/

Found 2 closed AI services.

| Closed service    | Category    | Replace with                                             |
|-------------------|-------------|----------------------------------------------------------|
| xAI API           | LLM API     | Open-weight LLM: XiaomiMiMo/MiMo-V2.6-Pro-RL             |
|                   |             | Inference server: ollama/ollama                          |
| Vercel AI Gateway | LLM gateway | LLM gateway: BerriAI/litellm                             |

### Vercel AI Gateway
- `.env.example:6` — `AI_GATEWAY_API_KEY=****`
- `lib/ai/models.ts:126` — `const res = await fetch("https://ai-gateway.vercel.sh/v1/models", {`
...
```

Built for the moment you fork a template or inherit an app and want to know which AI
components are rented, and what the best open replacement is today.

## Install

```bash
uvx lockin scan .          # run once
pipx install lockin        # or keep it
```

Python 3.11+, one dependency (PyYAML). No account, no upload, no network: the scan
reads files and prints a report.

If [ripgrep](https://github.com/BurntSushi/ripgrep) is on your PATH, lockin uses it to
find candidate lines and large monorepos scan about three times faster. The results
are identical either way, and CI checks that they are.

## Use

```bash
lockin scan .                          # markdown report
lockin scan . --top 5                  # more alternatives per kind
lockin scan . --format json -o r.json  # every ranked alternative, every location
lockin scan . --exclude "examples/*"   # or list globs in a .lockinignore file
lockin catalog                         # what is covered
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
| Packages | `requirements*.txt`, `pyproject.toml` (PEP 621, Poetry, PDM, uv groups), `setup.py`, `setup.cfg`, `Pipfile`, conda `environment.yml`, `package.json`, `go.mod`, `Cargo.toml`, `pom.xml`, Gradle and version catalogs, `.csproj` / `Directory.Packages.props`, `Gemfile`, `composer.json` |
| Imports | Python (AST, Jupyter notebooks included), JavaScript and TypeScript `import` / `require` / dynamic `import()` |
| Install commands | `pip install`, `uv add`, `poetry add`, `npm i`, `pnpm add`, … in Dockerfiles, shell scripts, CI config and notebook `!pip` cells |
| Framework integrations | LangChain, LlamaIndex, Vercel AI SDK, Spring AI, LangChain4j, Semantic Kernel provider packages |
| API hosts | `api.openai.com`, `*.openai.azure.com`, `ai-gateway.vercel.sh`, … in any source or config file |
| Model ids | `gpt-4o`, `claude-sonnet-4`, `gemini-2.5-pro`, `xai/grok-4`, … — but not open-weight ones like `gpt-oss` or `deepseek-v3.2` |
| Environment variables | read in code in any language, or set in `.env*`, compose files and CI config |
| Terraform | `azurerm_search_service`, `aws_bedrockagent_agent`, … |

Every finding cites file, line and text. What it deliberately does not count:

- **Lockfiles.** They list what your dependencies depend on. A gateway library that
  pulls in the `openai` SDK does not make you an OpenAI customer.
- **Comments, docstrings and docs.** Writing about a vendor is not using one.
- **Ignored and vendored files.** `.gitignore` is respected; `node_modules`, `vendor`,
  virtualenvs and minified bundles are skipped.
- **Generic strings.** `task="transcribe"` is Whisper, not Amazon Transcribe;
  `import textract` is an open source library, not AWS Textract; `HF_TOKEN` downloads
  open weights and is not Hugging Face inference.

The test suite pins each of these, and many more, as one test per edge case.

## How alternatives are chosen

Each closed service names the kinds of thing that replace it: the OpenAI API is
replaced by an open-weight LLM *and* an inference server; Pinecone by a vector
database. Each kind is a pool in [`catalog/alternatives.yaml`](catalog/alternatives.yaml),
and `scripts/refresh.py` ranks every pool weekly:

- **Open source projects** are ranked by momentum: GitHub stars gained over the last
  90 days. GitHub no longer exposes when stars were given, and the public event
  archives have undercounted since 2025, so lockin keeps its own history in
  [`catalog/star-history.json`](catalog/star-history.json). Until a project has four
  weeks of history, its pool is ranked by total stars and the report says so.
- **Open-weight models** are discovered, not listed: the listed labs' own models
  (not community fine-tunes or re-quantisations) under an open licence, ranked by
  Hugging Face's trending score, at most two per lab.
- **Only open source counts.** An OSI-approved licence for code; Apache, MIT, BSD or
  CC-BY for weights. Licences that add use restrictions or commercial thresholds are
  out, however popular the project. So are archived projects and projects without a
  push in a year. The refresh fails loudly when a listed project is renamed, archived
  or relicensed.

The scanner never touches the network: it reads the committed ranking snapshot.

## Contributing

The catalog is plain YAML. To cover a new service, add it to
[`catalog/services/`](catalog/services) with its signatures and the pools that replace
it. To suggest an alternative, add it to a pool in
[`catalog/alternatives.yaml`](catalog/alternatives.yaml) — the ranking decides where it
lands. Every false positive or missed dependency is a bug: please open an issue with
the line that fooled it.

## Licence

Apache-2.0.
