# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Weekly ranking refreshes are not listed;
each release ships the rankings as of its date.

## Unreleased

### Added
- `unrent scan --fail-on snapped,snaps,closed` (and `--within DAYS` for `snaps`): exit 1
  when the code selects a retired or retiring model, or depends on a closed AI service,
  with one line on stderr saying why. The report is written as usual.
- `--format sarif`: SARIF 2.1.0 for code scanning, a result per line that selects a
  retiring model (`unrent/snapped` error, `unrent/snaps` warning) and per cited line of a
  closed service (`unrent/closed-service` note).
- A GitHub Action (`action.yml`): scans, uploads the SARIF so pull requests show the
  lines, and fails the build on `fail-on`.
- GetXAPI and Xquik, hosted X/Twitter search APIs sold for agents, as web search services.
- `self_host`: a closed service that runs on an open source project (Qdrant Cloud,
  Zilliz Cloud, Weaviate Cloud, Chroma Cloud, Firecrawl, fastCRW, Mem0, Letta, Helicone,
  Portkey, Steel, E2B, Ollama Cloud, the Unstructured API) names that project first:
  running it yourself is the smallest switch. In the terminal row, the Markdown table,
  `--why`, JSON and MCP.
- Two pools for libraries: vector search libraries (faiss, hnswlib, USearch) and search
  libraries (tantivy, bleve, Lucene).
- Model retirements on the cloud platforms, from each one's own lifecycle page and
  checked weekly: Azure OpenAI and Foundry (127 ids), Amazon Bedrock (12), Vertex AI (30,
  and 8 partner models). A model id retires on the schedule of the service the code
  calls it through: o3-mini through Azure on Azure's date, through OpenAI's API on
  OpenAI's. Bedrock ids count with a Region prefix (`us.anthropic.claude-...`), Vertex
  partner models with their version (`claude-3-5-sonnet-v2@20241022`). A value given to
  an Azure deployment name is not taken for the model. They sit under `platforms` in
  retirements.yaml, which 0.3.0 does not read, so its copy from `main` keeps loading.

- Bicep files are scanned, and `format: 'OpenAI'` in Bicep or ARM is Azure OpenAI: the
  model an azd template deploys is the model the app runs. Gemini API resource names
  (`models/text-embedding-004`) are model ids; Firebase AI on its Vertex backend is
  Vertex AI. A model is also picked by `param modelName string = 'x'`, `az ...
  --model-name x` and `generativeModel("x")`.

### Changed
- A specific service keeps the model ids it claims from the general one: `o3-mini` in a
  file that calls Azure OpenAI is Azure evidence. They used to be dropped. Not in a file
  that also calls the general vendor's own host, or that several platforms claim (a
  switch over providers): there the id could be either's.
- A pool lists only what can replace the services it serves: faiss is no longer offered
  as a vector database, nor tantivy as a search engine. Each runs ranked among its peers.
- Summary rows name the top three of a pool instead of one: a ranking by stars is
  popularity, not a verdict on which fits. Every alternative line shows its kind
  (server, library, Postgres extension).

### Fixed
- Prose in JSON files (LLM answers saved as results, translation files) is no longer read
  as code: a model id or API host in a sentence there is not a finding. Code stored in
  JSON strings (n8n, Langflow) still is.
- A model id in prose like "`gpt-4o` or `gpt-4`" counts as named, not selected.

## [0.3.0] - 2026-10-05

### Added
- A Claude Code plugin with the MCP server, the skill and a hook, from this repo's own
  marketplace: `/plugin marketplace add stringcutter/unrent`, then
  `/plugin install unrent@stringcutter` (`.claude-plugin/`).
- `unrent hook`: reads a Claude Code `PostToolUse` event on stdin and, when the agent has
  just written a model id the code selects that its vendor has retired or will retire,
  tells the agent the date and the replacement. Silent otherwise. The plugin runs it after
  every `Write`, `Edit` and `NotebookEdit`.
- `unrent scan <file>` (and the MCP `scan` tool) scans a single file, from the root of its
  repository, with the package manifests around it read for the rules that need them.

### Changed
- The MCP server's instructions cover models that stop working, not only closed services.
- Model retirements come from `catalog/retirements.yaml` on main, cached for six hours,
  in `unrent scan`, the MCP server and `unrent hook`, so an old install still knows this
  week's dates. The shipped copy is used offline (`UNRENT_OFFLINE=1`) or when the fetch
  fails.

## [0.2.2] - 2026-10-04

### Changed
- The README, which is also the PyPI page, shows the hand-drawn unrent wordmark, and its
  screenshots show the output on paper with the strings drawn. No changes to detection.

## [0.2.1] - 2026-10-04

### Changed
- A shorter README, which is also the PyPI page: what unrent does in one sentence, what
  each row means, install and the common commands, with the details folded away.
- `unrent --help` describes unrent the same way, models that stop working included.

## [0.2.0] - 2026-10-04

First public release.

### Added
- Snapped models: model ids the code selects that the vendor has retired (`snapped`) or
  will retire on an announced date (`snaps`), with the vendor's replacement, followed
  while that one retires too. 204 retirements from OpenAI, Anthropic and Google in
  `catalog/retirements.yaml`, checked weekly against their pages by
  `scripts/retirements.py`, which opens a pull request. In the terminal view, the
  Markdown report (`Models that stop working`), JSON and MCP (`models_retiring`).
  `--why <model id>` and `--as-of YYYY-MM-DD`. A model menu, a price table, a check on
  the user's choice, sample data and Bedrock or Vertex spellings are not counted as
  selecting. A third eval side, `--side all`, scores it against hand-labelled truth
  (`eval/corpus_models.yaml`, `eval/MODEL_TRUTH_RULES.md`): precision 0.939, recall 0.886.
- A terminal view, the default when the output is a terminal: one row per service,
  `cut` (an open source replacement exists), `held` (none yet) or `runs` (open source in
  use, with its rank), with its strongest location and a count of the rest. Columns stack
  on narrow terminals; colour follows `NO_COLOR` and `FORCE_COLOR`. A pipe or `-o` still
  gets the Markdown report. `--format terminal|markdown|json|auto` and `-f`.
- `--why SERVICE`: every location behind one service or open source component, and its
  ranked alternatives or standing.
- `unrent <path>` as short for `unrent scan <path>`, and a status line on stderr while
  a terminal scan runs.
- `unknown_candidates` in every scan (JSON, Markdown and MCP): API hosts and keys that
  no catalog entry explains and that look like a hosted AI API, grouped by domain with
  their key (`TYPESAFE_API_KEY` with `api.typesafe.ai`). A generated provider catalog
  is one candidate. Never counted as a finding.
- 139 hosted model providers from [models.dev](https://models.dev), in the generated
  `catalog/services/models-dev.yaml`; a weekly job regenerates it, runs the golden
  corpus and opens a pull request. `scripts/new_services.py` also ranks the
  `unknown_candidates` of the eval corpus in the eval's job summary.
- 15 hand-written closed services (357 in all): TypeSafe AI (Jev), GitHub Copilot API,
  Ollama Cloud, Ask Sage, Hicap, Nous Research, Huawei ModelArts MaaS, SAP AI Core,
  Oracle Code Assist, BytePlus InfoQuest, Sofya, Tencent Cloud WSA, Unbrowse, Tenki and
  Volcengine Speech. New endpoints and keys for Moonshot (Kimi For Coding), Gitee AI (MoArk),
  Tencent (TokenHub), watsonx, Cloudflare AI Gateway and OCI Generative AI.
- An agent skill, `skills/unrent`, with `sweep.py` and `repo_facts.py`.
- `unrent mcp`: an MCP server with `scan`, `alternatives`, `standing` and `catalog`
  tools (install the `mcp` extra). It reads the latest rankings from the repository,
  cached for six hours, and falls back to the shipped snapshot, saying why.
  `UNRENT_OFFLINE=1` keeps it offline.
- 19 closed services (182 in all): GitHub Models, SageMaker, Qianfan, SiliconFlow,
  Hunyuan, Spark, Baichuan, StepFun, Novita, Clarifai, Aleph Alpha, Reka, GigaChat and
  six managed vector databases.
- Open source signatures for Java (Spring AI, LangChain4j), .NET, Go, LangChain and
  LlamaIndex integration packages, env vars and more images; four new projects (89).
- Pools: RAG split into applications and frameworks; new LLM evaluation and answer
  engine pools. Model rows show parameter counts.
- Package extras named after a package (`qdrant-client[fastembed]`), compose
  `${VAR:-default}` images and `CREATE EXTENSION` statements.
- The eval corpus labels open source components too (54 repositories), checked in CI.
- 21 more closed services (203 in all), found by agents in anything-llm and
  gpt-researcher: Gitee AI, PPIO, APIpie, CometAPI, Privatemode, Atlas Cloud, AI/ML API,
  TensorBlock Forge, Avian, NetMind, ModelsLab, Chroma Cloud, the Nomic API, Okahu, and
  the search APIs SearchApi.io, Serply, fastCRW, Keenable, AnySearch, Bocha and
  GroundRoute. The Nomic API is not reported where the code asks for local inference.
- `open_core` on open source projects whose repo keeps part of the code under another
  licence (LiteLLM, Langfuse, Agenta, Onyx, Meilisearch, Weaviate), shown next to the
  licence.
- `env_template_only`: a key that appears only in `.env.example` or similar, with
  nothing in the code behind it, is listed apart and not counted as a dependency.

- `unrent scan <dir>`: finds closed AI services (about 160: LLM APIs, gateways,
  embeddings, vector databases, RAG, OCR, observability, speech, image generation,
  search, browser automation, sandboxes, agent memory) from packages, imports,
  install commands, framework integrations, API hosts, model ids, environment
  variables and Terraform resources.
- Markdown and JSON reports, with file, line and masked text for every finding.
- Open source alternatives per service, ranked weekly by GitHub star momentum and
  Hugging Face trending (`scripts/refresh.py`).
- `--exclude`, `.unrentignore`, `--skip-tests`, `--top`, `--output`.
- `unrent catalog` and `unrent catalog --validate`.
- Optional ripgrep acceleration with identical results.
- Closed model ids with no SDK, key, host or package behind them are listed
  separately ("closed models named in code") instead of counted as dependencies.
- "Open source you already run": 88 open source AI components (vector databases,
  inference servers, gateways, document parsers, observability, ...) are recognised
  from packages, imports, SQL and container images (`image:`, Helm values, `FROM`),
  and each is placed in its pool, overall and among projects of the same kind.
- A golden corpus of hand-labelled repositories (`eval/`), run in CI.

### Changed
- Faster scans of large repos, with identical results on all 54 corpus repos: from 1,500
  files on, worker processes read the files (one per core, falling back to one process
  where they can't start), each Python file is parsed once and only its statements are
  walked, and ripgrep gets larger batches outside Windows. dify (14,000 files) went
  from 59 s to 14 s, LibreChat (5,500) from 14 s to 6 s, the whole corpus from 193 s
  to 112 s.
- The MCP instructions state the limits: only catalog services are detected, and each
  pool's `ranked_by` says whether it is ranked by 90-day star gain or total stars.
- `unrent/report.py` is now `unrent/render.py`.

### Fixed
- Odd files no longer end a scan: invalid YAML dates, deep JSON or TOML, a `.py` file
  that overflows the parser, or a manifest of the wrong shape (`dependencies = 3`) gives
  no facts, and the rest of the repo is still reported. A malformed `--catalog` is a
  catalog error, not a traceback.
- A TOML dependency is cited at its own line (`llama-index = "0.9.7"`), not at a
  `keywords` entry higher up that names the same package.
- ripgrep only reads the files the scan covers: a repo with gigabytes of ignored data
  went from 99 s to about 1 s.
- Notebooks: source lists joined correctly, non-Python kernels read with their own
  comment syntax.
- Old Mac (CR-only) files, symlinks, non-UTF-8 paths, saved unrent reports and OpenAPI
  specs no longer produce wrong lines or findings.
- A repo is never listed as a component of itself.
- Descriptions containing commas were cut short in the catalog; the loader now rejects
  them unquoted and names the file on any catalog error.
- `-o` is checked before the scan and excluded from it; `--top` must be positive.
- Evidence is ordered strongest first; code spans survive backticks.
- LangChain.js `VoyageEmbeddings` and `VOYAGEAI_API_KEY` are Voyage AI.
- A vendor prefix needs a model name after it: `github://` URIs and `/^snowflake/i`
  regexes no longer name GitHub Models or Snowflake Cortex.
- Open source signatures: `SEARX_URL` and `SearxSearch` (SearXNG), vLLM behind an
  OpenAI-compatible base URL, a self-hosted Firecrawl URL, LangChain's `FAISS`.

[0.3.0]: https://github.com/stringcutter/unrent/releases/tag/v0.3.0
[0.2.2]: https://github.com/stringcutter/unrent/releases/tag/v0.2.2
[0.2.1]: https://github.com/stringcutter/unrent/releases/tag/v0.2.1
[0.2.0]: https://github.com/stringcutter/unrent/releases/tag/v0.2.0
