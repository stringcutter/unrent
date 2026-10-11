<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://github.com/stringcutter/unrent/raw/main/docs/wordmark-on-dark.svg?v=5">
  <img alt="unrent" src="https://github.com/stringcutter/unrent/raw/main/docs/wordmark.svg?v=5" width="320">
</picture>


[![PyPI](https://img.shields.io/pypi/v/unrent?style=flat-square)](https://pypi.org/project/unrent/)
[![CI](https://img.shields.io/github/actions/workflow/status/stringcutter/unrent/ci.yml?branch=main&style=flat-square)](https://github.com/stringcutter/unrent/actions)
[![Python](https://img.shields.io/pypi/pyversions/unrent?style=flat-square)](https://pypi.org/project/unrent/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue?style=flat-square)](https://github.com/stringcutter/unrent/blob/main/LICENSE)

[What each row means](#what-each-row-means) • [Install](#install) • [Use](#use) • [AI agents](#use-it-from-an-ai-agent) • [How it works](#how-it-works)

</div>

**See which AI services your code depends on, what open source can replace them, and
which models are about to stop working.**

```bash
uvx unrent .
```

![unrent my-app: 3 strings attached, 3 can be cut, 1 will snap. Rows for OpenAI API, Pinecone and ElevenLabs marked cut with their open source replacements, each with the biggest open source replacements and the fastest rising ones, gpt-4-turbo marked snaps with its retirement date and replacement, and mem0 marked runs with its rank in agent memory and what is rising around it](https://github.com/stringcutter/unrent/raw/main/docs/scan.svg?v=8)

## What each row means

| | |
|---|---|
| `╎ cut` | You use a closed AI service. The biggest open source replacements are on the row, and the fastest rising under them. |
| `│ held` | You use a closed AI service with no open source replacement yet. |
| `┆ snapped` | Your code asks for a model the vendor has shut down. Those calls fail today. |
| `┆ snaps` | Your code asks for a model the vendor shuts down on the date shown, with the vendor's replacement and an open-weight one. |
| `│ runs` | Open source AI you already run, and how it ranks. |

Every row points to a file and line. `--why` shows all of them, and what to use instead:

![unrent my-app --why gpt-4-turbo: main.py line 9 selects gpt-4-turbo, which retires on 2026-10-23; openai recommends gpt-5.6-sol, with the link to OpenAI's deprecations page, or the open-weight Qwen3.8-27B](https://github.com/stringcutter/unrent/raw/main/docs/why-model.svg?v=7)

> [!NOTE]
> unrent runs on your machine. No account, no upload, no telemetry.

## Install

```bash
uv tool install unrent      # or: pipx install unrent
```

Python 3.11+.

## Use

```bash
unrent .                         # scan the current directory
unrent src/llm.py                # one file
unrent . --why openai            # every line behind one row
unrent . -o report.md            # a Markdown report for an issue or PR
unrent . --format json           # for scripts
unrent . --skip-tests            # ignore test code
unrent . --exclude "examples/"   # .gitignore syntax, or a .unrentignore file
```

`unrent scan --help` lists the rest.

## Use it from an AI agent

In Claude Code, as a plugin:

```
/plugin marketplace add stringcutter/unrent
/plugin install unrent@stringcutter
```

You get the MCP tools, the skill, and a hook that tells the agent when it writes a model
id that is retired or about to be.

Other MCP clients (Cursor, Copilot, …): command `uvx`, args
`["--from", "unrent[mcp]", "unrent", "mcp"]`.

Only the skill, with a workflow for what unrent can't see on its own:

```bash
npx skills add stringcutter/unrent
```

## Use it in CI

On GitHub, the action annotates each line that selects a retiring model (and each closed
service, as a note) in code scanning, and fails the build on what you name:

```yaml
permissions:
  contents: read
  security-events: write   # for the annotations
steps:
  - uses: actions/checkout@v5
  - uses: stringcutter/unrent@v0.5.0
    with:
      fail-on: snapped,snaps   # snapped, snaps, closed
      within: 30               # snaps only within 30 days
```

On pull requests from forks, and in private repositories without code scanning, add
`upload-sarif: false`: the build still fails on what you name.

Anywhere else:

```bash
unrent . --fail-on snaps --within 30 -o unrent.md   # exit 1, and one line saying why
unrent . --format sarif -o unrent.sarif             # SARIF 2.1.0 for code scanning
```

## How it works

<details>
<summary><b>What it finds</b></summary>

- **359 closed AI services** in 21 categories: LLM APIs and gateways, embeddings, vector
  databases, RAG, document parsing, observability, speech, image generation, search,
  scraping, browser automation, sandboxes, agents and agent memory. 139 hosted model
  providers come from [models.dev](https://models.dev), regenerated weekly.
- **381 model retirements** from OpenAI, Anthropic and Google, and from Azure, Bedrock
  and Vertex for the models they serve, with dates and the vendor's replacement, checked
  weekly against their deprecation pages. A model counts when the code picks it (a
  default, a config value, a call), not when it is only listed in a menu or price table,
  and on the schedule of the platform the code calls: gpt-4o through Azure retires on
  Azure's date. An Azure deployment name is not taken for the model.
- **89 open source projects** you may already run: vector databases, inference servers,
  gateways, RAG frameworks, document parsers, observability, evals, speech.
- **Unknown candidates:** API hosts and keys that look like a hosted AI service the
  catalog doesn't know yet. Listed for you to check, never counted.

It reads packages (Python, npm, Go, Cargo, Maven, Gradle, NuGet, RubyGems, Composer,
pub, SwiftPM), imports, install commands, container images, and API hosts, model ids,
env vars and SDK calls in code and config.

</details>

<details>
<summary><b>What it ignores</b></summary>

- Local servers behind a compatible SDK: `OpenAI(base_url="http://localhost:11434/v1")`
  is Ollama, not OpenAI.
- Comments, docs, lockfiles, `node_modules` and anything `.gitignore` excludes.
- Generic names: your `perplexity.py` isn't Perplexity.
- Open-weight models: `gpt-oss`, `deepseek-v3.2` and Ollama tags aren't closed.

Secrets in the output are masked (`OPENAI_API_KEY=****`). Files too large to scan are
listed, never skipped silently.

</details>

<details>
<summary><b>How alternatives are ranked</b></summary>

- Projects two ways: **established**, the biggest by GitHub stars, and **rising**, the
  most stars gained in the last 30 days (at least 2% of their own), from unrent's own
  daily snapshots. Rising is where attention moves, not a verdict on quality.
- Besides the projects listed by hand, a daily search finds new ones on GitHub (1,000
  stars or more, open licence, active). [Jev](https://typesafe.ai) judges each once: does
  it do the job of the pool's projects, is it software you run, and is it not just the
  SDK of a hosted service. What it lets in is marked "found by unrent".
- Models by Hugging Face trending: labs' own releases under an open licence.
- Open source only: an OSI licence for code; Apache, MIT, BSD or CC-BY for weights.
  Archived projects are dropped, and open core is marked.

The CLI uses the rankings shipped with its release. The MCP server fetches the latest
from this repo, and the CLI, the MCP server and the hook fetch the latest model
retirements, all cached for six hours (`UNRENT_OFFLINE=1` turns that off). Only these
public lists come in; your code never goes out.

</details>

<details>
<summary><b>How accurate it is</b></summary>

Every rule has a test that fails without it. A golden corpus of 77 real repos, labelled
by hand, runs in CI: closed services precision 0.987, recall 0.970; open source in use
precision 0.990, recall 0.938; models that stop working precision 0.943, recall 0.892.

With [ripgrep](https://github.com/BurntSushi/ripgrep) installed, a 5,500-file repo scans
in about 6 s.

</details>

<div align="center">
<br>
<a href="https://github.com/stringcutter">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://github.com/stringcutter/unrent/raw/main/docs/stringcutter-on-dark.svg?v=5">
  <img alt="stringcutter" src="https://github.com/stringcutter/unrent/raw/main/docs/stringcutter.svg?v=5" width="180">
</picture>
</a>
</div>
