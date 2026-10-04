# unrent: notes for coding agents

unrent scans a codebase for the **closed AI services** it depends on (with file:line
evidence) and ranks the **open source** that replaces them, plus the open source AI it
already runs. Deterministic, offline, standard library + PyYAML. The maintainer usually
writes in Danish; answer in the language of their latest message.

## Scope (decided by the maintainer; don't reopen it)

- unrent does three things: detect closed AI dependencies (edge cases matter most), list
  open source alternatives ranked by momentum, and flag model ids the code selects that
  their vendor has retired or will retire (`snapped`). No lock-in scores, effort levels
  or "what you lose" text: they were removed on purpose.
- unrent is a stringcutter product (`github.com/stringcutter/unrent`). The terminal view
  speaks of strings: `cut`, `held`, `snapped`/`snaps`, `runs`.
- Improvements go to detection precision and coverage, and to ranking quality.
- unrent lists and ranks. Whether a switch is worth it is for the user (or an agent using
  the skill) to judge.

## Layout

| Path | What |
|---|---|
| `unrent/detect.py` | Facts from files (manifests, imports, code views without comments), matching against the catalog, overlap rules (`excludes`, `part_of`, models-only) |
| `unrent/discover.py` | `unknown_candidates`: API hosts and keys no catalog entry explains that look like a hosted AI API. Never counted as findings |
| `unrent/render.py` | JSON payload and Markdown report (`report.py` is the old name; don't recreate it) |
| `unrent/terminal.py` | The terminal view (`cut`/`held`/`snapped`/`snaps`/`runs` rows), `--why`, colour and the progress line; stdlib only |
| `unrent/retired.py` | Snapped models: which model facts select a retiring id (`selects`), the replacement chain |
| `unrent/server.py` | `unrent mcp`: tools `scan`, `alternatives`, `standing`, `catalog` |
| `unrent/fresh.py` | Live rankings from `catalog/rankings.json` on `main`, cached 6 h, else the shipped snapshot |
| `catalog/services/*.yaml` | Closed services. `models-dev.yaml` is **generated**; the others are hand-written |
| `catalog/alternatives.yaml` | Pools of open source alternatives and the open source projects recognised in code |
| `catalog/rankings.json`, `star-history.json` | Written weekly by `scripts/refresh.py` |
| `catalog/retirements.yaml` | Model retirements from the vendors' pages, rewritten weekly by `scripts/retirements.py` (pull request left open for review) |
| `eval/` | Golden corpus (54 repos, pinned commits) with hand-labelled truth, `TRUTH_RULES.md`, `OSS_TRUTH_RULES.md`, `MODEL_TRUTH_RULES.md` (`corpus_models.yaml`), `run_eval.py` |
| `scripts/` | `refresh.py` (rankings), `verify_packages.py`, `new_services.py` (models.dev + corpus candidates), `mcp_smoke.py` |
| `skills/unrent/` | Agent skill (SKILL.md, `sweep.py`, `repo_facts.py`), installable with `npx skills add` once the repo is public |

## Commands

```bash
uv sync                                    # dev setup (Python >= 3.11)
uv run pytest -q                           # tests; with `rg` on PATH each detection test runs twice
uv run ruff check . && uv run ruff format --check .
uv run unrent scan path/to/repo [--format json]
uv run unrent catalog --validate           # after any catalog edit
uv run python eval/run_eval.py --side all --min-precision 0.98 --min-recall 0.94 \
  --min-oss-precision 0.98 --min-oss-recall 0.88 \
  --min-model-precision 0.93 --min-model-recall 0.88  # first run clones 54 repos (UNRENT_EVAL_CACHE)
uv run python scripts/retirements.py       # retirements.yaml against the vendors' pages
uv run python scripts/verify_packages.py   # every package signature exists in its registry
uv run python scripts/new_services.py --models-dev                  # providers not in the catalog
uv run python scripts/new_services.py --models-dev --write-catalog  # regenerate models-dev.yaml
uv run python scripts/refresh.py --check   # rankings, without writing
```

## Catalog rules

- Add a closed service to the right hand-written file (`llm.yaml`, `agents.yaml`,
  `media.yaml`, `retrieval.yaml`). **Never edit `models-dev.yaml`**: it is regenerated
  weekly, and a hand-written entry that shares a host, key or package replaces its
  generated one at the next run.
- Every signature needs evidence from real code or the vendor's own SDK. Check package
  names exist and belong to the vendor (`typesafe` on PyPI is an unrelated library; the
  TypeSafe SDK is `typesafe-sdk`). Don't invent env var names.
- Hosted APIs serving open-weight models (Together, Fireworks, Groq, the Nomic API) are
  **closed** services. Don't flag code that selects local inference (`local_mode`).
- OpenAI-compatible providers get `excludes: [openai]`, so the `openai` import in a file
  that names the provider's host is attributed to the provider.
- Endpoint needles: give a path when the bare host is also the vendor's website or docs
  (`ai.gitee.com/v1`, `sofya.co/v1`). A needle may start with `//` to exclude subdomains
  (`//ollama.com/v1` does not match `api.ollama.com`). `api.githubcopilot.com/mcp` is
  GitHub's MCP server, not the Copilot API.
- Policy calls: DuckDuckGo is not catalogued (not an AI service, no account or key). A
  vendor's own hosted backend is reported as an `own_project` candidate, and catalogued
  only when it is a public product (Cline's `api.cline.bot` is, via models.dev).
- `open_core: "<what is not open>"` on an open source project whose repo keeps part of
  the code under another licence (`enterprise/ is under a commercial licence`). SSPL,
  BSL, ELv2 and research-only licences are not open source.
- Batch catalog changes and run the eval once for the batch.

## Eval rules

- Truth is labelled from the code, independently of unrent's output (`eval/TRUTH_RULES.md`).
- Floors in `.github/workflows/eval.yml` sit just under the measured scores (2026-10-04:
  closed precision 0.993, recall 0.957; open source 1.000 / 0.902; models 0.939 / 0.886).
  Raise them when scores rise; **never lower them to pass**.
- The models side scores only the ids `corpus_models.yaml` lists under `labelled`. When
  `retirements.yaml` gains ids, grep the corpus for them, label any line that selects
  one (`MODEL_TRUTH_RULES.md`), and add them to `labelled`.
- A new false positive after a catalog change: open the cited line first. If the code
  really uses the service, add it to the truth with the line, in the existing format
  (`- id  # added YYYY-MM-DD with the new catalog ids; line checked by hand: file:line ...`).
  If not, fix the signature and add a regression test.
- Known open FPs: `openai` in bedrock-access-gateway, `google-imagen` in anything-llm
  (Gemini image model ids in a chat model list), `together` in langchaingo. Models side:
  dify's `RestrictModel(model=...)` allow-list (3) and a tokenizer default in kotaemon.

## Rankings and data sources

- GitHub no longer lists stargazers with dates, and OSSInsight is degraded since
  2026-05. Momentum comes only from unrent's own weekly snapshots
  (`star-history.json`); a pool falls back to total stars until it has four weeks of
  history, and `ranked_by` says which. Never present a star delta you computed yourself
  as momentum.
- Hugging Face pools: each listed publisher's own models (not fine-tunes or GGUF
  re-uploads) under an open licence, ranked by trending score.

## CI and automation

- `ci.yml`: tests on Linux, macOS, Windows; ruff.
- `eval.yml`: the golden corpus with floors on changes to `unrent/`, `catalog/services/`,
  `catalog/alternatives.yaml` or `eval/`; its job summary ranks the corpus's
  `unknown_candidates`.
- `refresh.yml` (Mondays 05:17 UTC): rankings PR (merges itself on a quiet week);
  `packages` (verify_packages); `new-services` (regenerates `models-dev.yaml`, runs tests
  and the corpus, opens a PR that merges itself when all pass).
- `publish.yml`: PyPI on a GitHub release (trusted publishing).
- The bots push to `main` weekly: pull before you start.
- The repo is **private** for now. While it is, `unrent mcp` cannot fetch live rankings
  (raw URL 404) and uses the shipped snapshot. Renaming the repo or moving
  `catalog/rankings.json` breaks live rankings for every installed MCP server.

## Conventions

- Match the surrounding code: comment density, naming, plain wording. Commit messages
  explain the why and include measured results where there are any.
- Nothing machine-specific in committed files: no absolute paths, usernames or email
  addresses (the repo has been public and will be again). Check `git diff` before
  committing.
- unrent scans itself in CI; `.unrentignore` keeps the catalog, tests and eval out.
