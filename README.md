# unrent

*by [stringcutter](https://github.com/stringcutter)*

Find the strings your AI code hangs by. Cut them before they snap.

- **Closed AI services** the code calls, with file and line, and the open source that
  replaces each one, ranked.
- **Models that stop working**: model ids the code selects that their vendor has retired,
  or will on an announced date, and the vendor's replacement.
- **Open source AI** the code already runs, and where it ranks in its field.

The CLI runs offline. No account. No upload. No telemetry.

![unrent my-app: 3 strings attached, 3 can be cut, 1 will snap. Rows for OpenAI API, Pinecone and ElevenLabs marked cut with their open source replacements, gpt-4-turbo marked snaps with its retirement date and replacement, and faiss marked runs with its rank](https://github.com/stringcutter/unrent/raw/main/docs/terminal.svg)

Real output. One row per string, with its strongest location and how many more there
are:

| | |
|---|---|
| `╎ cut` | A closed service with an open source replacement. The top of its ranking is on the row. |
| `│ held` | A closed service with no open source replacement in the catalog yet. |
| `┆ snapped` | A model id the code selects that its vendor has retired. Requests to it fail now. |
| `┆ snaps` | The same, on the date shown. |
| `│ runs` | Open source already in use, and where it ranks among its kind. |

`--why` shows every line behind one row, and what replaces it:

![unrent my-app --why gpt-4-turbo: main.py line 9 selects gpt-4-turbo, which retires on 2026-10-23; openai recommends gpt-5.6-sol, with the link to OpenAI's deprecations page](https://github.com/stringcutter/unrent/raw/main/docs/why.svg)

In a pipe or a file the same scan is a Markdown report that pastes straight into an
issue or PR.

## Install

```bash
uv tool install unrent      # or: pipx install unrent
unrent .
```

Or once, without installing:

```bash
uvx unrent .
```

Python 3.11+. One dependency: PyYAML (the MCP server adds `mcp`). Large repos are read
on every core. With [ripgrep](https://github.com/BurntSushi/ripgrep) on PATH it is about
twice as fast again: 6 s for 5,500 files, 14 s for 14,000 (13 s and 26 s without, on 4
cores). Same results either way.

## Use

```bash
unrent .                               # the terminal view; Markdown in a pipe or file
unrent . --why openai                  # every location of one service, and what replaces it
unrent . --why gpt-4-turbo             # every line that selects a retiring model
unrent . --as-of 2026-12-01            # judge retirements as of another day
unrent . -o report.md                  # the Markdown report
unrent . --format json -o r.json       # everything, machine-readable
unrent . --top 5                       # more alternatives per kind
unrent . --skip-tests                  # ignore test, spec and fixture code
unrent . --exclude "examples/"         # .gitignore syntax; or a .unrentignore file
unrent catalog                         # what it knows
```

`unrent .` is short for `unrent scan .`. Colour follows `NO_COLOR` and `FORCE_COLOR`.

On Windows PowerShell, write reports with `-o`, not `>`. The redirect re-encodes the
file.

## MCP

unrent as tools for Claude Code, Cursor, Copilot, or any agent that speaks MCP. The agent
gets the facts; you decide what to swap.

```bash
claude mcp add unrent -- uvx --from "unrent[mcp]" unrent mcp
```

Other clients:

```json
{
  "mcpServers": {
    "unrent": {
      "command": "uvx",
      "args": ["--from", "unrent[mcp]", "unrent", "mcp"]
    }
  }
}
```

| Tool | |
|---|---|
| `scan` | Closed services, models that stop working, open source in use, alternatives. The report as JSON. |
| `alternatives` | Best open source for a category or a closed service: `"pinecone"`, `"speech-to-text"`. |
| `standing` | Where one project ranks, overall and among its kind: `"qdrant/qdrant"`. |
| `catalog` | What unrent recognises, and the signatures it looks for. |

Rankings come from this repo's `main`, refreshed weekly, not from the install. Cached
for six hours. When the fetch fails it uses the shipped snapshot and says so.
`UNRENT_OFFLINE=1` never fetches. Only rankings come in. Your code never goes out.

For agents that use skills, `npx skills add stringcutter/unrent` adds a workflow on top:
check what unrent can't see, sweep for services outside the catalog, and write the report.

## What it detects

**357 closed AI services** in 21 categories: LLM APIs, LLM gateways, embeddings, vector
databases, RAG platforms, document parsing, guardrails, classification and scoring, LLM
observability, model hosting, fine-tuning, speech-to-text, text-to-speech, voice agents,
image generation, web search, web scraping, browser automation, code sandboxes, agent
platforms, agent memory. 218 are written by hand; 139 hosted model providers come from
[models.dev](https://models.dev) and are regenerated every week
([`models-dev.yaml`](https://github.com/stringcutter/unrent/blob/main/catalog/services/models-dev.yaml)).

**Models that stop working.** 204 model retirements announced by OpenAI, Anthropic and
Google for their own APIs, with the date and the vendor's replacement, checked every
week against their deprecation pages
([`retirements.yaml`](https://github.com/stringcutter/unrent/blob/main/catalog/retirements.yaml))
and shipped with each release.
A model id counts when a line selects it: a default, a config value, a model passed to
a call. The same id in a model menu, a price table or a check on what the user picked
is only counted. Azure OpenAI, Bedrock and Vertex keep their own schedules and are not
covered. On the golden corpus: precision 0.939, recall 0.886.

**Services it does not know yet.** Every scan also lists `unknown_candidates`: API hosts
and keys that no catalog entry explains and that look like a hosted AI API (a `/v1/...`
path, an `api.` or `.ai` host, a matching `*_API_KEY`). They are candidates to check, not
findings, and never counted.

**89 open source projects**: vector databases, inference servers, gateways, RAG
frameworks and applications, document parsers, observability, evals, speech, scraping,
agent memory.

Evidence it reads:

| | |
|---|---|
| Packages | Python (requirements, pyproject, setup.py/cfg, Pipfile, conda, extras like `qdrant-client[fastembed]`), npm and pnpm catalogs, Go, Cargo, Maven, Gradle, NuGet, RubyGems, Composer, pub, SwiftPM |
| Imports | Python (AST, notebooks), JavaScript/TypeScript (`import`, `require`, `import()`, `npm:`, `jsr:`) |
| Install commands | `pip install`, `uv add`, `npm i`, … in Dockerfiles, shell, CI, notebook cells |
| Container images | `image:` in compose and Kubernetes, `${VAR:-default}`, Helm values, Dockerfile `FROM` |
| Code and config | API hosts, model ids, env vars, SDK symbols, `CREATE EXTENSION`, Terraform |

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

## Ranking

Pools live in [`catalog/alternatives.yaml`](https://github.com/stringcutter/unrent/blob/main/catalog/alternatives.yaml). Re-ranked every Monday by a
GitHub Action that merges itself.

- **Projects:** GitHub stars gained in the last 90 days, from unrent's own weekly
  snapshots. Until four weeks exist: total stars. The report says which.
- **Models:** Hugging Face trending. Labs' own releases only, open licence, max two per lab.
- **Open source only.** OSI licence for code. Apache, MIT, BSD or CC-BY for weights.
  Restricted weights are out, whatever the code licence. Archived projects and a year
  without a push are out. Open core is marked: LiteLLM's `enterprise/` or Langfuse's `ee/`
  is not under the licence shown.
- **No silent rot.** A renamed, archived or relicensed project blocks the auto-merge and
  waits for a human. A pool that empties or halves is not published.

The scanner reads the snapshot shipped with the release. New rankings come with new
releases.

## Contribute

The catalog is YAML. New service: [`catalog/services/`](https://github.com/stringcutter/unrent/tree/main/catalog/services). New alternative: a pool in
[`catalog/alternatives.yaml`](https://github.com/stringcutter/unrent/blob/main/catalog/alternatives.yaml). False positive or miss: open an issue with the
line that fooled it. [CONTRIBUTING.md](https://github.com/stringcutter/unrent/blob/main/CONTRIBUTING.md).

## Licence

[Apache-2.0](https://github.com/stringcutter/unrent/blob/main/LICENSE). unrent is made by
[stringcutter](https://github.com/stringcutter).
