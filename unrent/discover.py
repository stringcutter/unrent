"""Closed AI services the catalog does not know yet: candidates, not findings.

unrent only reports services in its catalog, and new hosted AI APIs appear every week.
This pass looks at what a project actually calls (API hosts in code and config, and
credential settings read from the environment) and keeps what no catalog entry, closed
or open, explains and what looks like a hosted AI API:

  looks like an API    a path such as /v1/... or /chat/completions, an api./inference.
                       host, a .ai domain, or a matching *_API_KEY setting
  and is about AI      that key, or AI words (model, prompt, completion, embedding...)
                       on or near the line

Hosts are grouped by registered domain, and a setting joins the domain it names
(TYPESAFE_API_KEY with api.typesafe.ai). A generated provider catalog (models.dev and
the like) becomes one candidate for the whole file. The result is a short list for a
person or an agent to check; it is never counted as a dependency.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import Catalog
from .detect import (
    _is_text_source,
    _own_repo,
    _read,
    _snippet,
    code_view,
    is_test_path,
    iter_files,
    redact,
    registered_domain,
)

URL = re.compile(r"https?://([A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,})(?::\d+)?(/[^\s\"'`)<>,\\]*)?")
SETTING = re.compile(
    r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*?)_(?:API_KEY|APIKEY|API_TOKEN|ACCESS_KEY)\b"
)
ENV_READ = re.compile(
    r"getenv|environ|process\.env|import\.meta\.env|ENV\[|System\.getenv|os\.Getenv|"
    r"env\(|_env\b|\$\{?[A-Z]|valueFrom|secretKeyRef|api_key_env"
)
CONFIG_NAME = re.compile(
    r"(^|/)(\.env[^/]*|[^/]*\.(ya?ml|toml|ini|cfg|conf|properties|env)|Dockerfile[^/]*|"
    r"docker-compose[^/]*|compose[^/]*)$",
    re.IGNORECASE,
)
AI_PATH = re.compile(
    r"/v\d+(?:beta|alpha)?\d*(?:/|$)|chat/completions|/completions|/embeddings?\b|/inference|"
    r"/generate|/rerank|/classif|/predict|/infer\b|/llm|/ai/|/search|/crawl|/scrape|/extract|"
    r"/tts|/stt|/speech|/transcri|/audio|/models?\b|/openai|/agents?\b",
    re.IGNORECASE,
)
AI_LABEL = re.compile(r"^(api|inference|llm|ai|models?|gateway|router|serving|search)[.-]")
AI_WORDS = re.compile(
    r"\b(models?|llms?|completions?|embeddings?|prompts?|chat|inference|tokens?|rerank\w*|"
    r"classif\w*|tts|stt|speech|transcri\w*|agents?|generat\w*|openai|anthropic|gemini|"
    r"vision|ocr|search\w*|crawl\w*|scrap\w*|guardrails?|moderation)\b",
    re.IGNORECASE,
)
# Registered domains that are never an AI API a project depends on: code hosting,
# registries, docs and standards, social, analytics, payments, chat, storage, clouds'
# consoles. A catalog entry, not this list, covers the AI products of big clouds.
NOT_AI = {
    "github.com", "githubusercontent.com", "github.io", "gitlab.com", "bitbucket.org",
    "npmjs.com", "npmjs.org", "pypi.org", "python.org", "crates.io", "rubygems.org",
    "nuget.org", "maven.org", "golang.org", "go.dev", "pkg.go.dev", "docker.com", "docker.io",
    "w3.org", "schema.org", "json-schema.org", "example.com", "example.org", "example.net",
    "mozilla.org", "wikipedia.org", "wikimedia.org", "stackoverflow.com", "apache.org",
    "opensource.org", "creativecommons.org", "readthedocs.io", "readthedocs.org",
    "twitter.com", "x.com", "linkedin.com", "youtube.com", "youtu.be", "facebook.com",
    "instagram.com", "reddit.com", "medium.com", "discord.com", "discord.gg", "t.me",
    "telegram.org", "slack.com", "zoom.us", "whatsapp.com", "line.me", "dingtalk.com",
    "feishu.cn", "larksuite.com", "qq.com", "weixin.qq.com", "wechat.com",
    "google-analytics.com", "googletagmanager.com", "doubleclick.net", "segment.com",
    "segment.io", "mixpanel.com", "amplitude.com", "posthog.com", "sentry.io",
    "datadoghq.com", "newrelic.com", "intercom.io", "hotjar.com", "plausible.io",
    "stripe.com", "paypal.com", "lemonsqueezy.com", "paddle.com",
    "shields.io", "jsdelivr.net", "unpkg.com", "cdnjs.com", "gstatic.com", "gravatar.com",
    "fonts.googleapis.com", "googleusercontent.com", "cloudfront.net",
    "notion.so", "notion.com", "atlassian.net", "atlassian.com", "figma.com", "hubapi.com",
    "hubspot.com", "salesforce.com", "zendesk.com", "airtable.com", "trello.com",
    "dropbox.com", "box.com", "vercel.app", "vercel.com", "netlify.app", "netlify.com",
    "herokuapp.com", "fly.dev", "render.com", "railway.app", "supabase.co", "supabase.com",
    "firebaseio.com", "mongodb.net", "upstash.io", "neon.tech", "planetscale.com",
    "arxiv.org", "semanticscholar.org", "crossref.org", "openalex.org", "ncbi.nlm.nih.gov",
    "nih.gov", "doi.org", "orcid.org", "archive.org", "openstreetmap.org", "wolframalpha.com",
    "apple.com", "microsoft.com", "microsoftonline.com", "live.com", "office.com",
    "visualstudio.com", "windows.net", "bing.com", "google.com", "gmail.com", "icloud.com",
    "amazon.com", "aws.amazon.com", "localhost", "local", "internal", "test", "invalid",
    "star-history.com", "img.shields.io", "badge.fury.io", "codecov.io", "travis-ci.org",
    "circleci.com", "sonarcloud.io", "snyk.io", "dependabot.com", "renovatebot.com",
    "pythonhosted.org", "skills.sh", "linear.app", "workos.com", "auth0.com", "okta.com",
    "clerk.com", "models.dev", "jira.com", "asana.com", "monday.com", "clickup.com",
    # reserved and private top-level domains
    "example", "lan", "home", "corp", "localdomain",
}  # fmt: skip
# Not sources of dependencies: prose, pages and notebooks (their outputs are prose too).
SKIP_SUFFIXES = {".md", ".mdx", ".rst", ".txt", ".html", ".htm", ".ipynb"}
MAX_JSON_BYTES = 64_000  # a bigger JSON file is data (fixtures, demo threads), not config
# An *_API_KEY whose name is a role, not a vendor (IMAGE_GENERATION_API_KEY), says nothing.
GENERIC_KEY = re.compile(
    r"^(LLM|MODEL|CHAT|EMBEDDINGS?|IMAGE|VIDEO|AUDIO|TTS|STT|SEARCH|CLASSIFY|EVAL|ANSWER|"
    r"JUDGE|PROVIDER|CUSTOM|OPENAI_COMPATIBLE|COMPATIBLE|GENERIC|DEFAULT|MY|YOUR|API|"
    r"SERVICE|SERVER|BACKEND|GATEWAY|PROXY|INTERNAL|ADMIN|APP|CLIENT|USER|TEST|DEV|PROD|"
    r"PUBLIC|PRIVATE|SECRET|MASTER|ROOT|PLATFORM|REMOTE|LOCAL|EXTERNAL|THIRD_PARTY|"
    r"AWS|AZURE|GCP|GOOGLE|GITHUB|GITLAB|NPM|PYPI|DOCKER|SLACK|DISCORD|TELEGRAM|SENTRY|"
    r"POSTHOG|STRIPE|LINEAR|JIRA|NOTION|ERROR|LOG|METRICS|ANALYTICS|CONTEXT)(_|$)|"
    r"(^|_)(TEST|EVAL|DEMO|MOCK|FAKE|DUMMY|EXAMPLE|GENERATION|COMPLETION|LLM)(_|$)"
)
GENERATED_FILE = re.compile(r"(^|[._/-])(generated|gen)([._/-]|$)", re.IGNORECASE)
GENERATED_HEADER = re.compile(r"@generated|do not edit|auto-?generated|code generated by", re.I)
COLLAPSE_AT = 15  # a file with this many candidate domains is a provider catalog
CONTEXT_LINES = 3


def _label(domain: str) -> str:
    return domain.split(".")[0].replace("-", "")


@dataclass
class _Group:
    domain: str
    hosts: set[str] = field(default_factory=set)
    settings: set[str] = field(default_factory=set)
    evidence: list[tuple[str, int, str, bool]] = field(default_factory=list)
    signals: set[str] = field(default_factory=set)
    files: set[str] = field(default_factory=set)


def _known(catalog: Catalog) -> tuple[set[str], set[str], set[str], set[str]]:
    """Hosts, registered domains, settings and vendor names that some catalog entry,
    closed or open, already explains."""
    hosts, domains, settings, names = set(), set(), set(), set()
    for service in catalog.detectable:
        for needle in service.detect.get("endpoint", ()):
            host = needle.lower().split("/")[0]
            hosts.add(host)
            domains.add(registered_domain(host))
        settings.update(service.detect.get("env", ()))
        for word in re.split(r"[/\s-]+", f"{service.id} {service.name}".lower()):
            word = re.sub(r"[^a-z0-9]", "", word)
            if len(word) >= 4:
                names.add(word)
    return hosts, domains, settings, names


def _service_words(catalog: Catalog) -> set[str]:
    """The words of every catalog id, short ones included (exa, you, xai)."""
    return {w for s in catalog.detectable for w in re.split(r"[/-]+", s.id.lower()) if w}


def _wanted(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in SKIP_SUFFIXES or not _is_text_source(path):
        return False
    if suffix == ".json":
        try:
            return path.stat().st_size <= MAX_JSON_BYTES
        except OSError:
            return False
    return True


def _is_known_host(host: str, hosts: set[str], domains: set[str]) -> bool:
    if host in hosts or registered_domain(host) in domains:
        return True
    return any(host.endswith("." + h) for h in hosts)


def candidates(
    root: Path,
    files: list[Path],
    catalog: Catalog,
    own_repo: str | None = None,
    skip_tests: bool = False,
    limit: int = 25,
) -> list[dict]:
    known_hosts, known_domains, known_settings, known_names = _known(catalog)
    known_short = {k for k in _service_words(catalog) if len(k) >= 3}  # AGENT_EXA_API_KEY
    groups: dict[str, _Group] = {}
    # setting -> its occurrences (file, line, text, in_test), whether it is ever read
    # from the environment or set in config, and whether AI words are ever near it
    setting_hits: dict[str, list[tuple[str, int, str, bool]]] = defaultdict(list)
    setting_read: set[str] = set()
    setting_ai: set[str] = set()
    per_file: dict[str, set[str]] = defaultdict(set)

    for path in files:
        if not _wanted(path):
            continue
        rel = path.relative_to(root).as_posix()
        in_test = is_test_path(rel)
        if in_test and skip_tests:
            continue
        text = _read(path)
        if not text or ("://" not in text and "_KEY" not in text and "_TOKEN" not in text):
            continue
        lines = text.split("\n")
        code = code_view(path, text).split("\n")
        generated = bool(GENERATED_FILE.search(path.name) or GENERATED_HEADER.search(text[:600]))
        config = bool(CONFIG_NAME.search(rel))
        for n, view in enumerate(code, start=1):
            if not view or len(view) > 2000:
                continue
            original = lines[n - 1] if n - 1 < len(lines) else view
            for m in URL.finditer(view):
                host = m.group(1).lower()
                domain = registered_domain(host)
                if (
                    domain in NOT_AI
                    or host in NOT_AI
                    or domain.split(".")[-1] in NOT_AI
                    or re.match(r"^\d+(\.\d+){3}$", host)
                    or _is_known_host(host, known_hosts, known_domains)
                    or _label(domain) in known_names  # docs of a vendor the catalog knows
                ):
                    continue
                path_part = m.group(2) or ""
                looks_api = bool(AI_PATH.search(path_part)) or bool(AI_LABEL.match(host))
                if domain.endswith(".ai"):
                    looks_api = True
                window = " ".join(code[max(0, n - 1 - CONTEXT_LINES) : n + CONTEXT_LINES])
                about_ai = bool(AI_WORDS.search(window.replace(host, " ")))
                g = groups.setdefault(domain, _Group(domain))
                g.hosts.add(host)
                g.files.add(rel)
                if looks_api:
                    g.signals.add("api")
                if about_ai:
                    g.signals.add("ai")
                g.evidence.append((rel, n, _snippet(original, m.start()), in_test))
                if generated:
                    per_file[rel].add(domain)
            for m in SETTING.finditer(view):
                name = m.group(0)
                if name in known_settings:
                    continue
                setting_hits[name].append((rel, n, _snippet(original, m.start()), in_test))
                if config or ENV_READ.search(view):
                    setting_read.add(name)
                window = " ".join(code[max(0, n - 1 - CONTEXT_LINES) : n + CONTEXT_LINES])
                if AI_WORDS.search(window.replace(name, " ")):
                    setting_ai.add(name)

    # Settings join the domain they name: TYPESAFE_API_KEY -> typesafe.ai.
    by_label: dict[str, list[str]] = defaultdict(list)
    for domain in groups:
        by_label[_label(domain)].append(domain)
    orphans: dict[str, list[tuple[str, int, str, bool]]] = {}
    for name, hits in setting_hits.items():
        if name not in setting_read:
            continue  # never read from the environment or set in config: a constant
        token = re.sub(r"_(API_KEY|APIKEY|API_TOKEN|ACCESS_KEY)$", "", name).lower()
        token = token.replace("_", "")
        labels = [
            label
            for label in by_label
            if label == token or (len(label) >= 4 and token.startswith(label))
        ]
        if labels:
            for label in labels:
                for domain in by_label[label]:
                    g = groups[domain]
                    g.settings.add(name)
                    g.signals.add("key")
                    if name in setting_ai:
                        g.signals.add("ai")
                    g.evidence.extend(hits[:2])
        elif (
            name in setting_ai
            and not GENERIC_KEY.search(name.rsplit("_API", 1)[0].rsplit("_ACCESS", 1)[0])
            and not any(k in token for k in known_names if len(k) >= 5)
            and not set(name.lower().split("_")) & known_short
        ):
            orphans[name] = hits

    out: list[dict] = []
    collapsed: set[str] = set()
    for rel, domains in per_file.items():
        unknown = sorted(d for d in domains if d in groups)
        if len(unknown) >= COLLAPSE_AT:
            collapsed.update(unknown)
            out.append(
                {
                    "name": rel,
                    "kind": "provider catalog",
                    "hosts": len(unknown),
                    "examples": unknown[:12],
                    "evidence": [{"file": rel, "line": None, "text": ""}],
                    "only_in_tests": is_test_path(rel),
                    "score": 4.0,
                }
            )
    own = (own_repo or "").split("/")[-1].lower().replace("-", "").replace("_", "")
    for domain, g in groups.items():
        if domain in collapsed and not g.settings and g.files <= set(per_file):
            continue  # only ever named in a generated catalog: covered by its entry
        has_key = "key" in g.signals
        if not ("api" in g.signals or has_key) or not ("ai" in g.signals or has_key):
            continue
        evidence = sorted(set(g.evidence), key=lambda e: (e[3], e[0], e[1]))
        tests_only = all(e[3] for e in evidence)
        if tests_only:
            continue  # a fixture host is rarely a dependency, and often not a real domain
        score = (
            ("api" in g.signals)
            + ("ai" in g.signals)
            + 1.5 * has_key
            + domain.endswith(".ai")
            + math.log10(1 + len(g.files))
            - 2 * tests_only
        )
        entry = {
            "name": domain,
            "kind": "api host",
            "hosts": sorted(g.hosts),
            "settings": sorted(g.settings),
            "evidence": [{"file": f, "line": n, "text": redact(t)} for f, n, t, _ in evidence[:3]],
            "only_in_tests": tests_only,
            "score": round(score, 2),
        }
        if own and own in _label(domain):
            entry["own_project"] = True  # the vendor's own hosted service
        out.append(entry)
    for name, hits in orphans.items():
        tests_only = all(h[3] for h in hits)
        if tests_only:
            continue
        out.append(
            {
                "name": name,
                "kind": "api key",
                "hosts": [],
                "settings": [name],
                "evidence": [{"file": f, "line": n, "text": redact(t)} for f, n, t, _ in hits[:3]],
                "only_in_tests": tests_only,
                "score": round(1.5 - 2 * tests_only + math.log10(1 + len({h[0] for h in hits})), 2),
            }
        )
    out.sort(key=lambda c: (-c["score"], c["name"]))
    return out[:limit]


def scan_unknown(
    root: Path, catalog: Catalog, exclude=(), skip_tests: bool = False, limit: int = 25
) -> list[dict]:
    """`candidates` for a project directory, with the same file selection as a scan."""
    files = iter_files(root, list(exclude))
    return candidates(root, files, catalog, _own_repo(root), skip_tests, limit)
