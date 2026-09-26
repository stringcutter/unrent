# Ground-truth labelling rules: open source AI components (for independent annotators)

Goal: for each assigned repo (already cloned at a pinned commit), decide which of the 88
open source AI projects in unrent's catalog the repo REALLY uses. This is ground truth for
measuring a detector, so it must be built from the repo's code and config, NOT from any
detector output.

HARD RULES
- Do NOT run `unrent` (or `uv run ... unrent`), and do NOT open any detector output: nothing
  under `eval/.cache/_reports`, `results.json`, `eval_report.txt`, or any
  `*.json` report produced by unrent.
- Do NOT modify anything in the cloned repos or in the unrent checkout.
  Write only your truth files.
- Repos live at `<cache>/<dir>/` (some are
  directory junctions; that is fine, read-only). Scope = git-tracked files
  (`git -C <dir> ls-files`); skip .git, node_modules, vendor/, third_party/ vendored code,
  minified bundles and lockfiles (package-lock.json, yarn.lock, pnpm-lock.yaml, poetry.lock,
  uv.lock, go.sum, Cargo.lock ...). Lockfile-only (transitive) packages do not count.
- Use the Grep tool (ripgrep) or `git grep -n -i` in Git Bash. Search case-insensitively and
  broadly; do not limit yourself to the hint signatures below.

THE CATALOG (label only these 88 repos; ids = the GitHub repo exactly as written)
See `unrent catalog` (the "Open source recognised in code" list): one line per project
with its pool, kind and some HINT signatures. The hints are an INCOMPLETE starting point (they are
roughly what one detector looks for). Real usage often looks different, e.g.:
- framework integration packages: `langchain-chroma`, `langchain_community.vectorstores.Chroma`,
  `langchain-qdrant`, `llama-index-vector-stores-qdrant`, `@langchain/community/vectorstores/...`,
  Spring AI starters (`spring-ai-starter-vector-store-pgvector`, `spring-ai-ollama`...),
  langchain4j modules (`langchain4j-ollama`, `langchain4j-qdrant`...), Semantic Kernel /
  Kernel Memory connectors, Go packages talking to a server over plain REST/gRPC;
- deployment: docker-compose `image:`, Kubernetes manifests, Helm `values.yaml`
  (`repository:` + `tag:`), Helm templates, Dockerfile `FROM`, Terraform, .env templates;
- servers reached over HTTP: Ollama at :11434 or an "ollama" provider option; llama.cpp's
  `llama-server` (default :8080, llama.vim uses :8012); vLLM / SGLang / LocalAI / TEI endpoints
  named as such; SearXNG URLs; Langfuse hosts/keys; LiteLLM proxy;
- Postgres + pgvector via SQL (`CREATE EXTENSION vector`), ORM vector columns (`vector(1536)`,
  `Unsupported("vector")`, drizzle/sqlalchemy `Vector`), Supabase vector search (pgvector), a
  `pgvector/pgvector` or `ankane/pgvector` image;
- native bindings: LLamaSharp (C#) and other llama.cpp bindings count as ggml-org/llama.cpp;
  whisper.cpp bindings count as ggml-org/whisper.cpp.

WHAT COUNTS (same spirit as the closed-service rules in lockin-dist/eval/TRUTH_RULES.md)
1. The whole repo is in scope, including sub-projects, examples, samples, deploy/ and charts/,
   except vendored third-party code.
2. A project counts (`open_source_expected`) if the repo's own code or config uses it, calls it,
   depends on it as a library it imports, deploys/runs it (compose/Helm/K8s/Dockerfile), or
   supports it as an OPTIONAL backend selectable by config/UI (e.g. `VECTOR_STORE=qdrant`, a
   provider dropdown with "Ollama", a vector-store factory with a Milvus branch). The client
   library of a server (qdrant-client, pymilvus, weaviate-client, ollama) counts for the server
   project.
3. Mentions only in docs (README, *.md, docs/ sites), comments, docstrings, changelogs or
   marketing copy do NOT count. Record notable doc-only mentions in `notes` with file:line
   (they are useful false-positive bait).
4. Used ONLY in tests/specs/fixtures/test data -> put it in `open_source_expected` with
   `tests_only: true`.
5. Declared in a manifest but never imported/used/deployed -> `open_source_uncertain`
   with reason "declared but unused" (unless it is clearly a tooling/transitive dependency).
6. A self-hostable engine used ONLY through its vendor's managed cloud (e.g. only a Qdrant
   Cloud URL, Weaviate Cloud `connect_to_weaviate_cloud`, Zilliz) -> `open_source_uncertain`,
   reason "hosted cloud only". If the code can also point at a self-hosted/local instance
   (local default URL, compose service, docs-backed config option) it is expected.
7. A generic "any OpenAI-compatible endpoint" option does NOT count for vLLM / llama.cpp /
   LocalAI / SGLang etc. It counts only if the repo names/targets that server explicitly
   (a provider named "vLLM", a compose service running it, a default URL/port documented in
   config for it, its image).
8. A repo that uses a component through a wrapper that is itself a catalog project counts
   both where the code really uses both (e.g. llama_index + llama-index-vector-stores-qdrant
   => run-llama/llama_index AND qdrant/qdrant).
9. When genuinely unsure, put it in `open_source_uncertain` with a precise reason. Do not guess.
10. Components the catalog does NOT have but that are significant open source AI pieces the repo
   uses (e.g. Elasticsearch, Redis/RediSearch vector, Neo4j, LangChain, HF transformers, TGI,
   Xinference, Apache Tika, Jina reranker served locally, LM Studio (closed), etc.) go in
   `coverage_gaps` with one piece of evidence each. Only AI-relevant ones (a vector/search/AI
   store, inference, AI framework, doc parser, speech, eval...), not generic infra like MinIO,
   nginx, plain Postgres/Redis caches.

CLOSED SERVICES (only when your assignment says so, i.e. for NEW repos)
Follow `eval/TRUTH_RULES.md` exactly for the closed
side, but use the CURRENT service ids from `catalog/services/*.yaml`
(`id:` fields; there are 163, more than TRUTH_RULES.md lists). You may read those yaml files to
learn what each id means.

OUTPUT: one file per repo at
`truth/<dir>.yaml`, outside the checkout,
exactly this shape (valid YAML; quote evidence/reason strings with single quotes, doubling any
single quote inside; keep evidence to the best 1-2 pieces, `path:line text`, path relative to
the repo root):

repo: owner/name
dir: owner__name
commit: <sha given to you>
languages: [python, typescript]
summary: one line on what the project is
open_source_expected:
  - repo: qdrant/qdrant
    evidence: 'docker-compose.yml:14 image: qdrant/qdrant:v1.12.0; app/store.py:3 from qdrant_client import QdrantClient'
    tests_only: false
open_source_uncertain:
  - repo: facebookresearch/faiss
    reason: 'faiss-cpu in requirements.txt:7 but never imported'
coverage_gaps:
  - name: Elasticsearch
    evidence: 'docker/docker-compose.yaml:40 image: elasticsearch:8.11.3'
# ONLY for repos marked NEW in your assignment:
closed_expected:
  - id: openai
    evidence: 'api/llm.py:12 client = OpenAI()'
    tests_only: false
closed_uncertain:
  - id: langsmith
    reason: 'only LANGSMITH_API_KEY in .env.example, never read'
notes: 'tricky calls, rejected doc-only mentions (file:line), anything a scorer should know'

Use [] for empty lists. Be thorough: check every sub-project, compose file, chart and manifest.
When you finish, reply with a short summary per repo (expected / uncertain / gaps counts and
anything you found hard to decide).
