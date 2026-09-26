# Ground-truth labelling rules (for independent annotators)

Goal: for each assigned repo, decide which CLOSED/proprietary AI services (from unrent's
catalog) the project really depends on. This is ground truth for measuring a detector, so
it must be built from the code itself, NOT from any detector output.

HARD RULES
- Do NOT run `unrent`, and do NOT open anything under `eval/out/` (detector output).
- Do NOT modify anything in the unrent checkout or in the cloned repos.
- You MAY read `catalog/services/*.yaml` to learn
  what each service id means. Do not restrict your search to those signatures: read the
  README, manifests, env examples, config and code, and grep broadly for provider names
  (case-insensitive: openai, azure, anthropic, claude, gemini, vertex, bedrock, cohere,
  mistral, groq, together, fireworks, deepseek, xai/grok, perplexity, openrouter, pinecone,
  qdrant, weaviate, elevenlabs, deepgram, tavily, exa, serp, langsmith, huggingface, ...).
- Search with ripgrep (`rg`)
  (use `--no-ignore-vcs` off by default; the repo scope is the git-tracked files; skip .git).

SERVICE IDS (use exactly these):
tavily exa serper serpapi brave-search bing-search firecrawl-cloud apify browserbase
hyperbrowser openai-computer-use e2b modal vercel-sandbox zep-cloud mem0-platform
openai-assistants langsmith braintrust wandb-weave helicone-cloud datadog-llm-observability
humanloop galileo openai azure-openai azure-ai-foundry anthropic google-gemini google-vertex
aws-bedrock cohere mistral xai deepseek-api groq together fireworks cerebras deepinfra
perplexity ibm-watsonx nvidia-nim-api huggingface-inference cloudflare-workers-ai replicate
openai-fine-tuning openai-moderation vercel-ai-gateway openrouter portkey cloudflare-ai-gateway
openai-speech-to-text deepgram assemblyai speechmatics gladia revai google-speech
aws-transcribe-polly azure-speech openai-text-to-speech elevenlabs cartesia playht hume
openai-realtime vapi retell bland openai-images google-imagen stability-api fal bfl-api
ideogram leonardo runway luma openai-embeddings google-embeddings aws-bedrock-embeddings
voyage jina-api mistral-embeddings pinecone upstash-vector turbopuffer astra-db
mongodb-atlas-vector-search vertex-vector-search zilliz-cloud weaviate-cloud qdrant-cloud
azure-ai-search vectara aws-bedrock-knowledge-bases google-vertex-search openai-file-search
unstructured-api llamaparse azure-document-intelligence aws-textract google-document-ai
google-vision-ocr reducto

WHAT COUNTS
1. The whole repo is in scope (all sub-projects/examples in it), except vendored/third-party
   code (node_modules, vendor/, minified bundles) and lockfiles.
2. A service counts if the project's own code or config calls it or is configured for it,
   including OPTIONAL providers the app supports via config/UI (e.g. a provider switch that
   can select Anthropic), IaC that provisions it, and env vars the app reads for it.
3. Merely mentioned in docs, README prose, comments or docstrings -> does NOT count.
4. Used ONLY in tests/specs/fixtures -> list it in `expected` with `tests_only: true`.
5. An OpenAI-compatible SDK pointed only at a local/self-hosted server (Ollama, vLLM,
   llama.cpp, LM Studio, LocalAI) is NOT `openai`. The `openai` SDK used only against Azure
   is `azure-openai`, not `openai`. OpenAI SDK with a Groq/Together/OpenRouter/... base URL
   is that vendor, not `openai`. A vendor's hosted API counts even when the model is
   open weights (Together, Fireworks, the Nomic API); an SDK told to run the model on the
   machine (`inference_mode="local"`) does not.
6. Calling a gateway (OpenRouter, Vercel AI Gateway, Portkey...) with model ids of other
   vendors (e.g. "anthropic/claude-...") counts as the GATEWAY, not the underlying vendor,
   unless the app also calls the vendor directly. If unclear, put the vendor in `uncertain`.
7. Capability-specific ids (openai-embeddings, openai-speech-to-text, openai-text-to-speech,
   openai-images, openai-realtime, openai-moderation, openai-assistants, openai-file-search,
   openai-fine-tuning, openai-computer-use, google-embeddings, google-imagen,
   aws-bedrock-embeddings, mistral-embeddings) count IN ADDITION to the base API id when the
   project uses that capability (e.g. OpenAI SDK + text-embedding-3-small => `openai` AND
   `openai-embeddings`). If the capability is reached only through Azure OpenAI (e.g. an Azure
   deployment of text-embedding-3), put the capability id in `uncertain` (reason: via Azure).
8. Vector DBs / search with self-hostable open versions (Qdrant, Weaviate, Milvus, MongoDB)
   count as the *-cloud id only if the project is configured for the hosted cloud (cloud URL,
   cloud connect helper, cloud env vars). Pinecone, Azure AI Search etc. are always closed.
9. A package declared in a manifest but never imported/used: put it in `uncertain` with
   reason "declared but unused" (unless it is clearly a transitive/tooling dep, then skip).
10. When you are genuinely unsure, put it in `uncertain` with a precise reason. Do not guess.
11. Model ids alone (e.g. a dropdown listing gpt-4o) count for the vendor only if the app
    actually sends them to that vendor's API (directly or via its SDK).

OUTPUT: one file per repo at
`truth/<dir>.yaml`, outside the checkout,
with exactly this shape (valid YAML; quote evidence strings with single quotes, doubling any
single quote inside):

repo: owner/name
dir: owner__name
languages: [python, typescript]
summary: one line on what the project is
expected:
  - id: openai
    evidence: 'app/main.py:12 client = OpenAI()'   # best 1-2 pieces of evidence, file:line + text
    tests_only: false
uncertain:
  - id: langsmith
    reason: 'only LANGSMITH_API_KEY in .env.example, never read in code'
notes: 'anything notable: tricky cases, local-server usage, docs-only mentions you rejected
  (list those with file:line too - they are useful to test false positives)'

Use `expected: []` / `uncertain: []` when empty. Be thorough: check every sub-project.
