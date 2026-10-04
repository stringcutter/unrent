# unrent

Find the rented parts of your AI stack. Swap them for open source.

- Lists every closed AI service a codebase calls, with file and line.
- Names the open source replacements, ranked.
- Ranks the open source you already run against its field.

The CLI runs offline. No account. No upload. No telemetry.

```
$ unrent scan my-app/

Found 4 closed AI services, and 1 open source component already in use.

| Closed service | Category        | Replace with                                         |
|----------------|-----------------|------------------------------------------------------|
| OpenAI API     | LLM API         | Open-weight LLM: XiaomiMiMo/MiMo-V2.6-Pro-RL         |
|                |                 | Inference server: ollama/ollama                      |
| Pinecone       | Vector database | Vector database: milvus-io/milvus                    |
| ...            |                 |                                                      |

### OpenAI API
- requirements.txt:1  openai>=1
- main.py:1           from openai import OpenAI
- main.py:5           client = OpenAI()
- .env.example:1      OPENAI_API_KEY=****

## Open source you already run

### facebookresearch/faiss (library and embedded)
Found at requirements.txt:3 — faiss-cpu
- Vector database: #2 of 8, #1 of 3 of its kind (★ 41.0k). Ranked above it:
  - milvus-io/milvus — server, ★ 46.3k
```

Real output, trimmed and with links stripped. The full report is Markdown and pastes
straight into an issue or PR.

## Install

Not on PyPI yet. Straight from GitHub:

```bash
uv tool install git+https://github.com/niklasmellgren/unrent
unrent scan .
```

Or once, without installing:

```bash
uvx --from git+https://github.com/niklasmellgren/unrent unrent scan .
```

Python 3.11+. One dependency: PyYAML (the MCP server adds `mcp`). With [ripgrep](https://github.com/BurntSushi/ripgrep)
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

On Windows PowerShell, write reports with `-o`, not `>`. The redirect re-encodes the
file.

## MCP

unrent as tools for Claude Code, Cursor, Copilot, or any agent that speaks MCP. The agent
gets the facts; you decide what to swap.

```bash
claude mcp add unrent -- uvx --from "unrent[mcp] @ git+https://github.com/niklasmellgren/unrent" unrent mcp
```

Other clients:

```json
{
  "mcpServers": {
    "unrent": {
      "command": "uvx",
      "args": ["--from", "unrent[mcp] @ git+https://github.com/niklasmellgren/unrent", "unrent", "mcp"]
    }
  }
}
```

| Tool | |
|---|---|
| `scan` | Closed services, open source in use, alternatives. The report as JSON. |
| `alternatives` | Best open source for a category or a closed service: `"pinecone"`, `"speech-to-text"`. |
| `standing` | Where one project ranks, overall and among its kind: `"qdrant/qdrant"`. |
| `catalog` | What unrent recognises, and the signatures it looks for. |

Rankings come from this repo's `main`, refreshed weekly, not from the install. Cached
for six hours. When the fetch fails it uses the shipped snapshot and says so.
`UNRENT_OFFLINE=1` never fetches. Only rankings come in. Your code never goes out.

## What it detects

**357 closed AI services** in 21 categories: LLM APIs, LLM gateways, embeddings, vector
databases, RAG platforms, document parsing, guardrails, classification and scoring, LLM
observability, model hosting, fine-tuning, speech-to-text, text-to-speech, voice agents,
image generation, web search, web scraping, browser automation, code sandboxes, agent
platforms, agent memory. 218 are written by hand; 139 hosted model providers come from
[models.dev](https://models.dev) and are regenerated every week
([`models-dev.yaml`](https://github.com/niklasmellgren/unrent/blob/main/catalog/services/models-dev.yaml)).

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

Pools live in [`catalog/alternatives.yaml`](https://github.com/niklasmellgren/unrent/blob/main/catalog/alternatives.yaml). Re-ranked every Monday by a
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

The catalog is YAML. New service: [`catalog/services/`](https://github.com/niklasmellgren/unrent/tree/main/catalog/services). New alternative: a pool in
[`catalog/alternatives.yaml`](https://github.com/niklasmellgren/unrent/blob/main/catalog/alternatives.yaml). False positive or miss: open an issue with the
line that fooled it. [CONTRIBUTING.md](https://github.com/niklasmellgren/unrent/blob/main/CONTRIBUTING.md).

## Licence

[Apache-2.0](https://github.com/niklasmellgren/unrent/blob/main/LICENSE)
