---
name: unrent
description: >-
  Find the closed AI services a codebase depends on (OpenAI, Anthropic, Gemini, Bedrock,
  Pinecone, Tavily, LangSmith, ElevenLabs, ...) with file:line evidence, the model ids it
  selects that OpenAI, Anthropic, Google, Azure, Bedrock or Vertex have retired or will
  retire on an announced date, and the current best open source replacements, using the
  unrent MCP server (tools scan, alternatives, standing, catalog) or the unrent CLI. Use
  this whenever someone wants to audit an AI stack for vendor lock-in, list which AI APIs
  or hosted models a repo calls, check whether the models a repo uses are deprecated or
  shutting down, replace a closed AI service with open source or self-hosted software,
  fork a provider's template and swap out its AI components, or asks which open source
  LLM, embedding model, vector database, search API or observability tool is best right
  now, even if they never say "unrent" or "lock-in".
---

# unrent: closed AI dependencies, retiring models and their open source replacements

unrent (by stringcutter) is a deterministic scanner with a hand-curated catalog of closed
AI services, the vendors' model retirements and open source alternatives, ranked weekly
from GitHub and Hugging Face. It is fast (seconds), its file:line evidence is exact, and
its rankings and retirement dates are newer than your training data. What it cannot do
is see services outside its catalog, or judge whether a replacement fits this particular
codebase. Your job is to add exactly that, and not to redo what
unrent already did well.

An evaluation of agents doing this task with and without unrent found that most time was
lost re-verifying things unrent had already verified, and most errors came from the few
things it cannot see. This workflow spends effort where it matters.

## 1. Scan once

Call `scan` with the project path (MCP), or run `unrent scan <path> --format json`
(CLI). Keep the result; everything below refers to it. Save it to a file of your own
(the sweep below can read it); service names include Chinese text, so on Windows set
`PYTHONIOENCODING=utf-8` before printing it from Python.

- `found`: closed services the code depends on, strongest evidence first.
- `models_named`: model ids with no SDK, key, host or package behind them, or services
  only a registry names (a price table, a provider catalog).
- `models_retiring`: model ids the code selects that the vendor has retired (`snapped`,
  requests fail now) or will retire on `retires` (`snaps`), with `use_instead` and the
  lines that select them. Report these first: they break without anyone touching the code.
- `env_template_only`: keys that appear only in an example env file.
- `open_source`: open source AI the code already runs, with its rank in its pool.
- `unknown_candidates`: API hosts and keys that no catalog entry explains and that look
  like a hosted AI API. Candidates, not findings: step 3 is where you decide.
- `alternatives`: ranked open source per pool, for every pool the findings need.
- `self_host` on a `found` entry: the open source project that hosted service runs on
  (Qdrant Cloud runs `qdrant/qdrant`). Running it yourself is the smallest switch.

Don't re-grep for what `found` already lists, and don't re-open every cited line: in
testing, unrent's line numbers were exact across more than a thousand citations.

## 2. Check only the weak spots

Open the code for these, and nothing else from the scan:

- **Every `models_named` and `env_template_only` entry.** Decide whether the code really
  calls the service (a model menu the user can pick from counts as supported; a
  token-limit table or a placeholder key does not).
- **Capability findings backed only by model ids** (for example Google Imagen from
  `gemini-2.5-flash-image` in a list of chat models, or OpenAI Embeddings from a model id
  in a config file). One look at the file tells you whether that capability is used.
- **Findings whose first evidence is not code**: a help string, a log line, a UI
  placeholder, an icon map. If nothing stronger follows, drop or downgrade it.

## 3. Sweep for what the catalog cannot see

This is where you add the most. Start with `unknown_candidates`: unrent already grouped
each host with its key and dropped what it knows, so open each one and decide. Then run
the bundled sweep for what that heuristic leaves out (hosts that do not look like an API,
settings with no AI words near them), giving it the scan JSON if you have it as a file:

```bash
python <this skill>/scripts/sweep.py <path> [--known scan.json]
```

It lists API hosts and credential-like settings (`*_API_KEY`, `*_BASE_URL`, ...) found in
code and config, skipping docs, lockfiles and common non-AI hosts, in a second or two.
With `--known`, anything already in unrent's evidence is dropped; with MCP output only,
compare by eye. A generated provider catalog gets one summary line (report it as a long
tail of providers, not service by service), and names seen only in tests are listed
together at the end. For each remaining host or key that could be an AI service, open
the line and decide:

- a closed AI service (hosted LLM, embeddings, search API for agents, OCR, speech,
  tracing...) — add it, with evidence, and say unrent does not know it;
- a hosted endpoint for open weights (like Together or the Nomic API) — still closed;
- not AI (scholarly APIs, social media data, payments, telemetry), a local server, or
  the project's own backend — ignore it.

Also look at how the code picks a provider when nothing is configured. A silent default
to a closed API (`LLM` empty, so `gpt-3.5-turbo`) is lock-in the user will want to hear
about, and no scanner reports it.

Two kinds of dependency leave no host or key for the sweep to find. Closed coding agents
started as subprocesses (`claude -p`, the Codex or Gemini CLI, an ACP agent in a config
file) run on the vendor's account: grep for those command names. And open source that
the code reaches only over HTTP (a vLLM or SearXNG base URL on localhost, an image in a
compose file) is in use even though no package names it, so unrent's `open_source` will
not list it; the default config and compose files show it.

## 4. Recommend replacements

Start from `alternatives` (or call `alternatives` with a category or service name). The
numbers in it were checked against GitHub and Hugging Face at the last weekly refresh,
so you don't need to re-measure them. Add the judgement unrent leaves out:

- **Self-host first.** When a finding has `self_host`, that project is the recommendation
  unless the code needs something only the hosted service adds (some hosts keep parts
  closed: Firecrawl's Fire-engine, Portkey's control plane, Mem0's platform API); the
  pool is the fallback.
- **Rank is popularity, not fit.** Pick from the top few by what the code needs: its
  `kind` where given (a server, a library, a Postgres extension), the features it uses (filters,
  hybrid search, multi-tenancy) and what it already runs. Say why the pick fits.
- **Read `ranked_by`.** `stars` means biggest, not fastest rising: say so rather than
  calling it a trend. Don't compute star growth yourself; there is no reliable public
  source for it today, and figures from different methods disagree by 10x or more. If
  `stars_90d` is present, that is the growth figure.
- **Fit the model to the stack.** Hugging Face pools rank by trending, whatever the size:
  a 1-trillion-parameter model tops the list for a week after release. Match the
  recommendation to how this codebase runs models (a laptop with Ollama, one GPU,
  a cluster) and name a smaller model from the same pool when that fits better.
- **Keep the swap small.** Many open servers (vLLM, SGLang, llama.cpp, Ollama, LiteLLM)
  speak the OpenAI API, so a closed LLM behind an OpenAI-compatible client is often a
  base-URL change. Say so when the code allows it.
- **Licences.** Everything unrent lists has an OSI licence for code or an open licence
  for weights. `open_core` means part of the repo is not open (an `ee/` or
  `enterprise/` directory): mention it next to the pick. If you add projects of your
  own, check their licence; SSPL, BSL, ELv2 and "research only" are not open source.
- **Current facts, in one call.** For the projects you add yourself and for the open
  source already in use, run the bundled script once with all of them:

  ```bash
  python <this skill>/scripts/repo_facts.py owner/repo owner/repo ... --hf org/model ...
  ```

  It returns stars, last push, latest release, licence, and archived or renamed repos
  for every repo in one GraphQL request (about two seconds), plus licence, parameter
  count and downloads for models. That replaces a loop of `gh api` and Hugging Face
  calls. An archived or renamed upstream, or one quiet for months, is worth a line in
  the report.

For the open source already in use, report its standing from `open_source` and whether
something in the same pool and of the same kind ranks clearly above it. Don't push a
switch that only wins on stars. `standing` and `catalog` only know the projects in
unrent's pools: a component that `open_source` did not list (an agent framework, an MCP
SDK, a PDF library) is unknown to them too, so check it with `repo_facts.py` instead.

unrent never says whether a switch is worth it. You can, briefly, when the code gives a
reason (a deprecated package, an API with an announced shutdown, an archived upstream).

## 5. Report

Unless the user asked for a specific format, answer with:

1. **Models that stop working**, from `models_retiring`: the id, `snapped` or the date it
   `snaps`, the line that selects it, and `use_instead`.
2. **Closed AI services**: name, category, one or two `file:line` citations each, marked
   when only in tests, when optional (a provider the user can pick), and when unrent did
   not detect it.
3. **Open source already in use**, with its standing.
4. **Replacements**, per closed service or category: the top two or three, one project
   per rank, each with one line on why, its licence (with `open_core` if set) and how it
   plugs in here.
5. **What to watch**: silent defaults, deprecated or sunsetting APIs, and anything you
   could not verify.

Keep it to what the user can act on. Numbers you did not check yourself come from unrent;
say which ranking they come from (`ranked_by` and the rankings date) rather than
presenting them as your own measurement.
