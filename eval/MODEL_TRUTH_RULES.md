# Ground-truth labelling rules: models the code selects

Goal: for each assigned repo, decide which model ids from the retirement list the
project's own code **selects**: the id is what the code sends to the vendor's API, on
some path, without anyone typing the id in. This is ground truth for `snapped` (a model
the code relies on that has stopped working, or will on an announced date), so it must
be built from the code itself, NOT from any detector output.

HARD RULES
- Do NOT run `unrent`, and do NOT open anything under `eval/out/` (detector output).
- Do NOT modify anything in the unrent checkout or in the cloned repos.
- The ids to look for are the retirement list (one per line). Search the repo's
  git-tracked files with ripgrep (`rg -F -w -f ids.txt`), then read each hit in context.
  Ids are exact: `gpt-4` is not `gpt-4o` or `gpt-4-turbo`, which are ids of their own.

SELECTS (put the id in `selects`)
1. A default: a function or constructor default (`model="gpt-4-1106-preview"`), a
   module constant used as the model, a CLI flag default, a settings or config default
   the app ships with (`default="gemini-2.0-flash"`, `model: dall-e-3` in an
   application.yml, `OPENAI_MODEL=gpt-4` in `.env.example` that the code reads).
2. A hard-coded model in a call (`OpenAI(model="gpt-4-vision-preview")`).
3. A fallback the code uses when nothing is configured (`env.get("M") or "gpt-3.5-turbo"`).
4. Sample apps and `examples/` directories count: they are code people run.

DOES NOT SELECT (leave it out)
5. A menu or list of models a user can pick from, even when it is the default value of
   a setting (`default="gemini-2.5-flash,gemini-2.0-flash,..."` listing allowed models).
6. Tables keyed by model: prices, token or context limits, capabilities, aliases.
7. Checks on a model the user chose: `if model == "gpt-3.5-turbo-0301"`, `startswith`,
   regexes, `in [...]`.
8. Docs, README prose, comments, docstrings, help text, i18n strings, changelogs.
9. Tests, specs, fixtures, mocks, fake or sample data (`fakeData.ts`), recorded
   responses, database migrations (history; the current model definition counts), and
   CI configuration under `.github/` (it runs the project's checks, not the product).
10. The id sent through a platform the retirement list has no vendor for: OpenRouter or
    another gateway (`openai/gpt-4`). Put these in `uncertain` with the reason.

PLATFORMS (Azure, Bedrock, Vertex AI have vendors of their own in the list)
Each vendor's ids count only where the code reaches that vendor: `gpt-4o` is on Azure's
list, not OpenAI's, so `gpt-4o` sent to api.openai.com selects nothing.
11. An id sent through Azure OpenAI or Azure AI Foundry, Amazon Bedrock or Vertex AI is
    judged against that platform's vendor only (`azure`, `bedrock`, `vertex`,
    `vertex-partners`), and selects by rules 1-4 when that vendor lists it:
    - Bedrock: the model id (`anthropic.claude-3-haiku-20240307-v1:0`; a Region prefix
      such as `us.` or `global.` names the same id).
    - Vertex: Gemini, Imagen, Veo and embedding ids as written; a partner model by the
      name Google lists, without its version: `claude-3-5-sonnet-v2@20241022` is
      `claude-3-5-sonnet-v2`.
    - Azure: the model name as Azure lists it (`gpt-4o`, `o3-mini`) where the code names
      the model: a `model` given to an Azure client, a Foundry model name, the model
      name in the IaC that deploys it. A deployment name (`azure_deployment=`,
      `AZURE_OPENAI_DEPLOYMENT`, `deploymentName`) is the user's name for a deployment,
      not the model: `uncertain`, with that reason.
12. An id sent through a platform whose vendor does not list it (OpenAI's `gpt-4`
    through Azure, Anthropic's `claude-3-5-sonnet-20240620` on Bedrock) is not on that
    platform's schedule: `uncertain`, with the reason.

When a hit is genuinely ambiguous, put it in `uncertain` with the reason. Unsure whether
a setting is a menu or a default: read where it is consumed.

OUTPUT, per repo (YAML), one line per id, the strongest site as a comment:

    LibreChat-AI__LibreChat:
      selects:
        - dall-e-3  # api/app/clients/tools/structured/DALLE3.js:161 model: 'dall-e-3' in the image tool call
      uncertain:
        - gpt-4  # api/models/x.js:12 default of an Azure-or-OpenAI client; reason: may only reach Azure

A repo with no selected id gets `selects: []`.
