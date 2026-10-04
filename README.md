# unrent

*by [stringcutter](https://github.com/stringcutter)*

**See which AI services your code depends on, what open source can replace them, and
which models are about to stop working.**

```bash
uvx unrent .
```

![unrent my-app: 3 strings attached, 3 can be cut, 1 will snap. Rows for OpenAI API, Pinecone and ElevenLabs marked cut with their open source replacements, gpt-4-turbo marked snaps with its retirement date and replacement, and faiss marked runs with its rank](https://github.com/stringcutter/unrent/raw/main/docs/terminal.svg)

## What each row means

| | |
|---|---|
| `╎ cut` | You use a closed AI service. The best open source replacement is on the row. |
| `│ held` | You use a closed AI service with no open source replacement yet. |
| `┆ snapped` | Your code asks for a model the vendor has shut down. Those calls fail today. |
| `┆ snaps` | Your code asks for a model the vendor shuts down on the date shown. |
| `│ runs` | Open source AI you already run, and how it ranks. |

Every row points to a file and line. `--why` shows all of them, and what to use instead:

![unrent my-app --why gpt-4-turbo: main.py line 9 selects gpt-4-turbo, which retires on 2026-10-23; openai recommends gpt-5.6-sol, with the link to OpenAI's deprecations page](https://github.com/stringcutter/unrent/raw/main/docs/why.svg)

Runs on your machine. No account, no upload, no telemetry.

## Install

```bash
uv tool install unrent      # or: pipx install unrent
```

Python 3.11+.

## Use

```bash
unrent .                         # scan the current directory
unrent . --why openai            # every line behind one row
unrent . -o report.md            # a Markdown report for an issue or PR
unrent . --format json           # for scripts
unrent . --skip-tests            # ignore test code
unrent . --exclude "examples/"   # .gitignore syntax, or a .unrentignore file
```

`unrent scan --help` lists the rest.

## Use it from an AI agent

As an MCP server (Claude Code, Cursor, Copilot, …):

```bash
claude mcp add unrent -- uvx --from "unrent[mcp]" unrent mcp
```

Other clients: command `uvx`, args `["--from", "unrent[mcp]", "unrent", "mcp"]`.

As a skill, with a workflow for what unrent can't see on its own:

```bash
npx skills add stringcutter/unrent
```

## How it works

<details>
<summary><b>What it finds</b></summary>

- **357 closed AI services** in 21 categories: LLM APIs and gateways, embeddings, vector
  databases, RAG, document parsing, observability, speech, image generation, search,
  scraping, browser automation, sandboxes, agents and agent memory. 139 hosted model
  providers come from [models.dev](https://models.dev), regenerated weekly.
- **204 model retirements** from OpenAI, Anthropic and Google, with dates and the
  vendor's replacement, checked weekly against their deprecation pages. A model counts
  when the code picks it (a default, a config value, a call), not when it is only listed
  in a menu or price table. Azure, Bedrock and Vertex have their own schedules and are
  not covered.
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

- Projects by GitHub stars gained in the last 90 days, from unrent's own weekly snapshots
  (total stars until four weeks of history exist; the report says which).
- Models by Hugging Face trending: labs' own releases under an open licence.
- Open source only: an OSI licence for code; Apache, MIT, BSD or CC-BY for weights.
  Archived projects are dropped, and open core is marked.

The CLI uses the rankings shipped with its release. The MCP server fetches the latest
from this repo, cached for six hours (`UNRENT_OFFLINE=1` turns that off). Only rankings
come in; your code never goes out.

</details>

<details>
<summary><b>How accurate it is</b></summary>

Every rule has a test that fails without it. A golden corpus of 54 real repos, labelled
by hand, runs in CI: closed services precision 0.993, recall 0.957; models that stop
working precision 0.939, recall 0.886.

With [ripgrep](https://github.com/BurntSushi/ripgrep) installed, a 5,500-file repo scans
in about 6 s.

</details>

## Contribute

The catalog is YAML: services in [`catalog/services/`](https://github.com/stringcutter/unrent/tree/main/catalog/services),
alternatives in [`catalog/alternatives.yaml`](https://github.com/stringcutter/unrent/blob/main/catalog/alternatives.yaml).
Found a false positive or a miss? Open an issue with the line that fooled it.
See [CONTRIBUTING.md](https://github.com/stringcutter/unrent/blob/main/CONTRIBUTING.md).

## Licence

[Apache-2.0](https://github.com/stringcutter/unrent/blob/main/LICENSE). Made by
[stringcutter](https://github.com/stringcutter).
