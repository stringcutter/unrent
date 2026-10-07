"""Find facts in a codebase, then match them to catalog services.

Deterministic and offline. Which files are read is `files`, what is read from each is
`facts`; here the facts are matched against the catalog, and the overlap rules decide
which service each piece of evidence belongs to.
"""

from __future__ import annotations

import dataclasses
import functools
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .catalog import Catalog, Service
from .facts import (
    CODE_SUFFIXES,
    JS_SUFFIXES,
    Fact,
    Needles,
    _needle_pattern,
    facts_for_file,
    normalise,
)
from .files import _own_repo, is_test_path, iter_files, map_files, ripgrep_hits


@dataclass(frozen=True)
class Finding:
    service: Service
    facts: tuple[Fact, ...]
    # Only model names, with no SDK, key, host or package behind them: a token-limit
    # table, a model menu in a config template, a model picked inside someone else's
    # hosted agent. Worth listing, not the same as depending on the vendor.
    models_only: bool = False

    @property
    def cited(self) -> list[Fact]:
        """One fact per source location, strongest first: production code before tests,
        and a declared package or an import before a string that merely names it."""
        seen: set[tuple[str, int]] = set()
        out: list[Fact] = []
        for fact in sorted(self.facts, key=_citation_order):
            key = (str(fact.file), fact.line)
            if key not in seen:
                seen.add(key)
                out.append(fact)
        return out

    @property
    def test_only(self) -> bool:
        return all(f.in_test for f in self.facts)

    @property
    def template_only(self) -> bool:
        """Only keys in an example env file (`HELICONE_API_KEY=` in .env.example) that
        nothing else backs: a placeholder, not proof the code calls the service."""
        return all(f.kind == "env" and _ENV_TEMPLATE.match(f.file.name) for f in self.facts)


_ENV_TEMPLATE = re.compile(
    # .env.example, .env.local.sample, env.template, example.env, .example.env
    r"^(\.?env\..*(example|sample|template|dist|defaults?)|\.?(example|sample|template)\.env)$",
    re.IGNORECASE,
)


_EVIDENCE_STRENGTH = {
    **{k: 0 for k in ("requirement", "npm", "go", "cargo", "maven", "nuget", "gem", "composer",
                      "pub", "swift", "image")},
    "python_import": 1, "terraform_resource": 1, "sql_extension": 1,
    "symbol": 2, "endpoint": 2, "env": 3, "model": 4,
}  # fmt: skip


def _citation_order(fact: Fact) -> tuple:
    return (fact.in_test, _EVIDENCE_STRENGTH.get(fact.kind, 5), str(fact.file), fact.line)


def file_root(path: Path, cwd: Path) -> Path:
    """Where a scan of one file is rooted: its repository, else `cwd` when that holds
    it, else its directory. Test code, .unrentignore and the manifests above it then
    count as in a scan of the whole project."""
    repo = next((d for d in path.parents if (d / ".git").exists()), None)
    return repo or (cwd if path.is_relative_to(cwd) else path.parent)


def _local_modules(root: Path, files: list[Path]) -> set[str]:
    """Top-level Python modules the project defines itself, at the root or under
    src/: `from perplexity import score` then means your perplexity.py."""
    names = set()
    for path in files:
        parts = path.relative_to(root).parts
        if parts[:1] == ("src",):
            parts = parts[1:]
        if parts and path.suffix == ".py":
            names.add(parts[0] if len(parts) > 1 else path.stem)
    return names


def _is_local_import(fact: Fact, local: set[str], is_file: Callable[[Path], bool]) -> bool:
    top = fact.value.split(".", 1)[0]
    if top in local:
        return True
    # A script importing a module next to it. Not inside a package, where imports are
    # absolute, and never the importing file itself: `lancedb.py` saying `import
    # lancedb` is a wrapper around the real library.
    here = fact.file.parent
    if is_file(here / "__init__.py") or fact.file.stem == top:
        return False
    return is_file(here / f"{top}.py") or is_file(here / top / "__init__.py")


def collect_facts(
    root: Path,
    catalog: Catalog,
    exclude: Iterable[str] = (),
    skipped: list[Path] | None = None,
    skip_tests: bool = False,
    progress: Callable[[int, int], None] | None = None,
    only: Path | None = None,
) -> list[Fact]:
    """Read every relevant file once and extract every fact the catalog could care about.
    `progress(done, total)` is called as files are read, for a status line. With
    `only` (see iter_files), the manifests around the file give their dependencies and
    nothing else."""
    needles = Needles(catalog)
    files = iter_files(root, exclude, skipped, only)
    if progress:
        progress(0, len(files))
    found = ripgrep_hits(root, needles, files)
    local = _local_modules(root, files)
    is_file = functools.cache(Path.is_file)  # every import in a directory asks again
    rels = [path.relative_to(root).as_posix() for path in files]
    tests = [is_test_path(rel) for rel in rels]
    todo = [i for i, in_test in enumerate(tests) if not (in_test and skip_tests)]
    per_file = map_files(
        facts_for_file,
        [(files[i], None if found is None else found.get(rels[i], ())) for i in todo],
        needles,
    )
    facts: list[Fact] = []
    for done, in_test in enumerate(tests, start=1):
        if progress:
            progress(done, len(files))
        if in_test and skip_tests:
            continue
        for fact in next(per_file):
            if fact.kind == "python_import" and _is_local_import(fact, local, is_file):
                continue
            if only and files[done - 1] != only and not fact.manifest:
                continue
            facts.append(dataclasses.replace(fact, in_test=in_test) if in_test else fact)
    own = _own_repo(root)
    if own:
        facts.append(Fact("own_repo", own, root, 0, ""))
    return facts


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

PREFIX_SEPARATORS = {"python_import": ".", "go": "/", "npm": "/"}
CASE_SENSITIVE = {"symbol", "model", "env"}


def _canonical(kind: str, value: str) -> str:
    if kind == "requirement":
        return normalise(value)
    return value if kind in CASE_SENSITIVE else value.lower()


def _lookup_keys(kind: str, value: str) -> list[str]:
    """The signature values an observed value can match.

    Imports, npm packages and Go modules match by prefix at a separator:
    `azure.search.documents.x` matches `azure.search.documents`, `openai/resources`
    matches `openai`, `github.com/a/b/v3` matches `github.com/a/b`.
    An image also matches without its registry host: `docker.langfuse.com/langfuse/
    langfuse` is `langfuse/langfuse`. Everything else matches exactly.
    """
    value = _canonical(kind, value)
    if kind == "image":
        head, _, rest = value.partition("/")
        is_host = "." in head or ":" in head or head == "localhost"
        return [value, rest] if rest and is_host and "/" in rest else [value]
    sep = PREFIX_SEPARATORS.get(kind)
    if not sep:
        return [value]
    parts = value.split(sep)
    return [sep.join(parts[:i]) for i in range(1, len(parts) + 1)]


def match(facts: list[Fact], catalog: Catalog, only: Path | None = None) -> list[Finding]:
    """Findings from facts. `only`: a one-file scan, whose findings keep that file's
    evidence alone (its manifests have had their say in the overlap rules)."""
    index: dict[tuple[str, str], list[Service]] = {}
    for service in catalog.detectable:
        for kind, signatures in service.detect.items():
            for signature in signatures:
                index.setdefault((kind, _canonical(kind, signature)), []).append(service)

    # A symbol the project defines itself is its own, wherever it is used: firecrawl's
    # SearchIndexClient class is not Azure's.
    defined = {f.value for f in facts if f.kind == "defines" and f.value.isidentifier()}
    hits_by_service: dict[str, dict[tuple, Fact]] = {}
    for fact in facts:
        if fact.kind == "symbol" and fact.value in defined:
            continue
        for key in _lookup_keys(fact.kind, fact.value):
            for service in index.get((fact.kind, key), ()):
                hits_by_service.setdefault(service.id, {})[_key(fact)] = fact

    local = [f for f in facts if f.kind == "local_base_url"]
    local_modes = [f for f in facts if f.kind == "local_mode"]
    own = {f.value for f in facts if f.kind == "own_repo"}
    # A capability of an OpenAI-compatible API (Assistants) is called through the same
    # client: in a file that aims that client at a server you run, the server serves it.
    compatible = {s.id for s in catalog.detectable if s.local_compatible}
    local_code = [f for f in local if f.file.suffix.lower() in CODE_SUFFIXES]
    findings: list[Finding] = []
    for service in catalog.detectable:
        hits = hits_by_service.get(service.id)
        if not hits or (service.repo and service.repo.lower() in own):
            continue
        kept = tuple(sorted(hits.values(), key=lambda f: (str(f.file), f.line, f.kind, f.value)))
        if service.local_compatible:
            kept = _without_local_clients(kept, local)
        elif compatible & set(service.part_of):
            kept = _without_local_clients(kept, local_code)
        if service.local_mode:
            kept = _without_local_clients(kept, local_modes)
        if kept:
            findings.append(Finding(service=service, facts=kept))

    findings = _resolve_overlaps(findings)
    findings = [f for f in findings if not _only_weak(f)]
    # Marked before the gateway rule: model ids routed to the gateway are how it is
    # called, so the gateway finding they build is a real dependency.
    findings = _mark_models_only(findings)
    findings = _ai_sdk_default_gateway(findings, facts, catalog)
    if only:
        kept = [(f, tuple(x for x in f.facts if x.file == only)) for f in findings]
        findings = [dataclasses.replace(f, facts=facts) for f, facts in kept if facts]
    findings.sort(key=lambda f: (f.service.category, f.service.name))
    return findings


# @ai-sdk packages that are not model providers.
_AI_SDK_TOOLING = {
    "@ai-sdk/react", "@ai-sdk/vue", "@ai-sdk/svelte", "@ai-sdk/angular", "@ai-sdk/rsc",
    "@ai-sdk/ui-utils", "@ai-sdk/provider", "@ai-sdk/provider-utils", "@ai-sdk/otel",
    "@ai-sdk/gateway", "@ai-sdk/mcp", "@ai-sdk/devtools",
}  # fmt: skip
GATEWAY_ID = "vercel-ai-gateway"


def _ai_sdk_default_gateway(
    findings: list[Finding], facts: list[Fact], catalog: Catalog
) -> list[Finding]:
    """The Vercel AI SDK sends `model: "openai/gpt-5"` to Vercel's AI Gateway when no
    provider package is installed. The gateway is then the dependency, and the
    `vendor/model` strings are evidence for it."""
    gateway = catalog.by_id(GATEWAY_ID)
    packages = {f.value for f in facts if f.kind == "npm" and f.manifest}
    providers = {p for p in packages if p.startswith("@ai-sdk/") and p not in _AI_SDK_TOOLING}
    if gateway is None or "ai" not in packages or providers:
        return findings

    def routed(fact: Fact) -> bool:
        return fact.kind == "model" and "/" in fact.value and fact.file.suffix in JS_SUFFIXES

    moved = [f for finding in findings for f in finding.facts if routed(f)]
    if not moved:
        return findings
    out = []
    for finding in findings:
        if finding.service.id == GATEWAY_ID:
            continue
        rest = tuple(f for f in finding.facts if not routed(f))
        if rest:
            out.append(Finding(service=finding.service, facts=rest))
    existing = next((f.facts for f in findings if f.service.id == GATEWAY_ID), ())
    merged = {_key(f): f for f in (*existing, *moved)}
    out.append(Finding(service=gateway, facts=tuple(sorted(merged.values(), key=_key))))
    # The vendors lost their routed model ids; re-mark what is left of them. The
    # gateway itself is called through those ids, so it stays a dependency.
    remarked = _mark_models_only([dataclasses.replace(f, models_only=False) for f in out])
    return [
        dataclasses.replace(f, models_only=False) if f.service.id == GATEWAY_ID else f
        for f in remarked
    ]


# LiteLLM's provider routes: in a project that runs LiteLLM, `bedrock/<model>` is how
# it calls Bedrock. Not `openai/`, which LiteLLM also sends to any compatible server.
LITELLM_ROUTES = {
    "ai21", "anthropic", "azure", "azure_ai", "baseten", "bedrock", "bedrock_converse",
    "cerebras", "dashscope", "databricks", "deepinfra", "fireworks_ai", "gemini", "minimax",
    "mistral", "moonshot", "nebius", "nvidia_nim", "oci", "openrouter", "perplexity",
    "sambanova", "snowflake", "together_ai", "vertex_ai", "volcengine", "watsonx", "xai",
}  # fmt: skip
LITELLM = "BerriAI/litellm"

# Registries: files that list what a project could call, not what it calls. A model list
# or price table names this many closed services by model id alone (a source URL or an
# `@cf/` key beside them doesn't change that); a provider catalog in a data file
# (models.dev and the like) names this many with their hosts and keys. A router that
# calls six providers gives each a client, a host or a key, and a config that sets up a
# dozen (mods' config_template.yml) stays under the catalog's count.
MODEL_LIST_AT = 6
PROVIDER_CATALOG_AT = 15
DATA_SUFFIXES = {".json", ".yaml", ".yml", ".toml"}


def _registries(findings: list[Finding]) -> set[Path]:
    services: dict[Path, set[str]] = {}
    not_models: dict[Path, set[str]] = {}
    for finding in findings:
        if finding.service.open_source:
            continue
        for fact in finding.facts:
            if not fact.manifest:
                services.setdefault(fact.file, set()).add(finding.service.id)
                if fact.kind != "model":
                    not_models.setdefault(fact.file, set()).add(finding.service.id)
    named = {file: ids - not_models.get(file, set()) for file, ids in services.items()}
    # A model list split into one data file per provider (onyx's price_table/*.json)
    # counts as one list: the data files of a directory that name models and nothing else.
    data = {file for file in services if file.suffix.lower() in DATA_SUFFIXES}
    per_dir: dict[Path, set[str]] = {}
    for file in data:
        if named[file] == services[file]:
            per_dir.setdefault(file.parent, set()).update(named[file])
    return {
        file
        for file, ids in services.items()
        if len(named[file]) >= MODEL_LIST_AT
        or (file in data and len(ids) >= PROVIDER_CATALOG_AT)
        or (file in data and named[file] == ids and len(per_dir[file.parent]) >= MODEL_LIST_AT)
    }


def _mark_models_only(findings: list[Finding]) -> list[Finding]:
    """Flag findings whose only evidence is model names, or anything in a registry
    (`_registries`). A capability (OpenAI Embeddings) is exempt when the service it is
    part of has real evidence: `text-embedding-3-small` next to `from openai import
    OpenAI` is a real call. So is a model id on a LiteLLM provider route in a project
    that runs LiteLLM. Neither holds in a registry, which lists without calling."""
    litellm = any(f.service.repo == LITELLM for f in findings)
    registry = _registries(findings)

    def named(fact: Fact) -> bool:
        routed = litellm and "/" in fact.value and fact.value.split("/")[0] in LITELLM_ROUTES
        return fact.file in registry or (fact.kind == "model" and not routed)

    real = {
        f.service.id
        for f in findings
        if not all(named(fact) for fact in f.facts) and not f.template_only
    }
    out = []
    for finding in findings:
        only_models = all(named(fact) for fact in finding.facts)
        # A model id in a test is a fixture, not a call, even when the vendor is real.
        backed = any(base in real for base in finding.service.part_of)
        if only_models and not (
            backed and any(f.file not in registry and not f.in_test for f in finding.facts)
        ):
            finding = dataclasses.replace(finding, models_only=True)
        out.append(finding)
    return out


def _key(fact: Fact) -> tuple[str, int, str, str]:
    return (str(fact.file), fact.line, fact.kind, fact.value)


def _only_weak(finding: Finding) -> bool:
    """A signature marked weak (`import fireworks` is also the FireWorks workflow
    library) cannot establish a dependency on its own."""
    groups = finding.service.weak_groups
    if not groups:
        return False

    def weak_group(fact: Fact) -> int | None:
        # Compare the signature that matched, not the observed value:
        # `from fireworks import Firework` is observed as `fireworks.Firework`.
        keys = set(_lookup_keys(fact.kind, fact.value))
        return next(
            (
                i
                for i, group in enumerate(groups)
                for w in group
                if _canonical(fact.kind, w) in keys
            ),
            None,
        )

    matched = [weak_group(f) for f in finding.facts]
    # Weak signatures from two different groups corroborate each other:
    # `$vectorSearch` next to a `mongodb.net` host is Atlas Vector Search.
    return None not in matched and len(set(matched)) < 2


def _without_local_clients(facts: tuple[Fact, ...], local: list[Fact]) -> tuple[Fact, ...]:
    """Drop evidence of an OpenAI-compatible SDK aimed at a server you run.

    A local base URL in a code file covers that file, except the vendor's API host: a
    file that names both may call either. One in configuration (.env, compose, YAML)
    covers the project's client code, but not model ids or the vendor's API host,
    which still name the vendor. If only the package declaration is left, the package
    is explained by the local use and nothing remains.
    """
    if not local:
        return facts
    files = {f.file for f in local if f.file.suffix.lower() in CODE_SUFFIXES}
    project_wide = any(f.file.suffix.lower() not in CODE_SUFFIXES for f in local)
    kept = tuple(
        f
        for f in facts
        if (f.file not in files or f.kind == "endpoint")
        and not (project_wide and not f.manifest and f.kind not in ("model", "endpoint"))
    )
    if len(kept) < len(facts) and all(f.manifest for f in kept):
        return ()
    return kept


def _configured(symbol: Fact, own: list[Fact]) -> bool:
    """Does the call `symbol` opens take the specific service's evidence as an argument?"""
    m = _needle_pattern("symbol", symbol.value).search(symbol.evidence)
    if not symbol.value.endswith("(") or not m:
        return False
    args = symbol.evidence[m.end() :].partition(")")[0]
    return any(o.file == symbol.file and o.line == symbol.line and o.value in args for o in own)


def _resolve_overlaps(findings: list[Finding]) -> list[Finding]:
    """A specific service claims the general service's evidence where it is used.

    Azure OpenAI is called through the `openai` package; Claude on Bedrock through
    `anthropic`; Vertex through `google-genai`. In every file where the specific
    service has evidence of its own, the general service's evidence there (the
    import, the model ids) belongs to the specific one, except the general
    vendor's own API host. If all the general service has left is the package
    declaration, the specific service explains that too, and the general one is
    not reported: naming a vendor that is not there is the one failure this tool
    cannot afford. The model ids go with the specific service, which retires them on
    its own schedule (gpt-4o on Azure), where it is the only one that claims them and
    has more than weak evidence. In a file that may also call the general vendor
    (its host or client) they stay with the general vendor: which line goes where,
    the file does not say, and the vendor's own date is the one known to apply.
    """
    by_id = {f.service.id: f for f in findings}
    claimed: dict[str, set[tuple]] = {}
    claimers: dict[tuple[str, Path], set[str]] = {}
    models: list[tuple[str, str, Fact]] = []  # specific, general, model fact
    for finding in findings:
        for general_id in finding.service.excludes:
            general = by_id.get(general_id)
            if general is None:
                continue
            general_keys = {_key(f) for f in general.facts}
            own = [f for f in finding.facts if _key(f) not in general_keys and not f.manifest]
            if not own:
                continue
            files = {f.file for f in own}
            # A file with the general vendor's own host or client may call either. Not
            # a client the specific one configures: genai.Client(vertexai=True).
            both = {
                g.file
                for g in general.facts
                if g.kind == "endpoint" or (g.kind == "symbol" and not _configured(g, own))
            }
            here = [f for f in general.facts if f.file in files]
            claimed.setdefault(general_id, set()).update(
                _key(f)
                for f in here
                if f.kind != "endpoint" and not (f.kind == "model" and f.file in both)
            )
            for file in files:
                claimers.setdefault((general_id, file), set()).add(finding.service.id)
            if not _only_weak(Finding(service=finding.service, facts=tuple(own))):
                models += [
                    (finding.service.id, general_id, f)
                    for f in here
                    if f.kind == "model" and f.file not in both
                ]
    moved: dict[str, list[Fact]] = {}
    for specific, general_id, f in models:
        if len(claimers[(general_id, f.file)]) == 1:  # a dispatch over providers: unknown
            moved.setdefault(specific, []).append(f)

    surviving: list[Finding] = []
    for finding in findings:
        if moved.get(finding.service.id):
            facts = {_key(f): f for f in (*finding.facts, *moved[finding.service.id])}
            finding = Finding(finding.service, tuple(sorted(facts.values(), key=_key)))
        taken = claimed.get(finding.service.id)
        if not taken:
            surviving.append(finding)
            continue
        rest = tuple(f for f in finding.facts if _key(f) not in taken)
        if rest and not all(f.manifest for f in rest):
            surviving.append(Finding(service=finding.service, facts=rest))
    return surviving
