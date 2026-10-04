"""Find facts in a codebase, then match them to catalog services.

Deterministic and offline. Every finding carries the file, the line and the exact
text it was found in; a finding that cannot cite its evidence does not exist.

Facts come from three kinds of reading:

  manifests   parsed, not grepped: requirements, pyproject, setup.py/cfg, Pipfile,
              conda environments, package.json, go.mod, Cargo.toml, Maven, Gradle,
              NuGet, Gemfile, composer.json, pubspec.yaml, Package.swift.
  imports     Python (AST, notebooks included) and JavaScript/TypeScript import
              and require specifiers, plus `pip install` / `npm install` commands
              in Dockerfiles, shell scripts, CI config and notebook cells.
  text        catalog needles — symbols, model ids, API hosts, environment
              variables — searched in the *code* of source and config files of any
              language: comments are blanked per language, strings are kept.
              ripgrep, when installed, finds the candidate lines; the same Python
              checks decide what counts.

Lockfiles are deliberately not read: they list what your dependencies depend on,
and a transitive `openai` pulled in by a gateway library is not a dependency on
OpenAI.
"""

from __future__ import annotations

import ast
import bisect
import configparser
import contextlib
import dataclasses
import functools
import json
import os
import re
import shutil
import subprocess
import tempfile
import tokenize
import tomllib
import warnings
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

import yaml

from .catalog import Catalog, Service

# Never ours to scan, even when committed.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "bower_components", "jspm_packages",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".nox",
    ".next", ".nuxt", ".svelte-kit", ".turbo", ".vercel", ".terraform", ".gradle",
    ".idea", ".vscode", "site-packages", "vendor", "Pods",
}  # fmt: skip
# Build output, skipped only outside git and only when there is no .gitignore to
# say what is build output.
HEURISTIC_SKIP_DIRS = {"dist", "build", "target", "out", "coverage", ".venv", "venv"}
MAX_FILE_BYTES = 2_000_000
# Notebooks are large because of their outputs, which are never read.
MAX_NOTEBOOK_BYTES = 64_000_000
# A JSON file this large is data (a fixture, an index dump), not configuration.
MAX_JSON_BYTES = 256_000
EVIDENCE_CHARS = 200

CODE_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".vue",
    ".svelte", ".astro", ".go", ".rs", ".java", ".kt", ".kts", ".scala", ".groovy", ".cs",
    ".fs", ".vb", ".rb", ".php", ".swift", ".dart", ".ex", ".exs", ".r", ".jl", ".lua",
    ".sh", ".bash", ".zsh", ".ps1", ".tf", ".tfvars", ".hcl", ".c", ".h", ".cc", ".cpp",
    ".cxx", ".hpp", ".m", ".mm", ".sql", ".html", ".htm",
}  # fmt: skip
CONFIG_SUFFIXES = {
    ".json", ".jsonc", ".json5", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".properties", ".xml", ".gradle", ".env", ".csproj", ".fsproj", ".vbproj", ".props",
    ".tpl", ".tmpl", ".j2", ".jinja", ".jinja2",
}  # fmt: skip
CONFIG_PREFIXES = (
    ".env", "dockerfile", "containerfile", "docker-compose", "compose.", "makefile",
    "procfile", "jenkinsfile", ".envrc", ".dev.vars", "gemfile",
)  # fmt: skip
JS_SUFFIXES = {
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".vue", ".svelte", ".astro",
}  # fmt: skip
C_FAMILY = JS_SUFFIXES | {
    ".go", ".rs", ".java", ".kt", ".kts", ".scala", ".groovy", ".gradle", ".cs", ".fs",
    ".swift", ".dart", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".m", ".mm", ".php",
    ".json", ".jsonc", ".json5", ".tf", ".tfvars", ".hcl",
}  # fmt: skip
# `#` also opens a comment in these C-family files.
C_AND_HASH = {".php", ".tf", ".tfvars", ".hcl"}
DASH_COMMENT = {".sql", ".lua"}
XML_LIKE = {".xml", ".csproj", ".fsproj", ".vbproj", ".props"}
HTML = {".html", ".htm"}
# Where an install command is an instruction, not a string in someone's error message.
INSTALL_SUFFIXES = {".sh", ".bash", ".zsh", ".ps1", ".yaml", ".yml", ".toml", ".cfg", ".conf"}
LOCKFILES = {
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock",
    "poetry.lock", "uv.lock", "pdm.lock", "pipfile.lock", "cargo.lock", "go.sum",
    "gemfile.lock", "composer.lock", "packages.lock.json", "gradle.lockfile", "pubspec.lock",
    "package.resolved",
}  # fmt: skip
GENERATED = re.compile(r"\.(min|bundle|chunk)\.(js|mjs|cjs|css)$|\.map$", re.IGNORECASE)
# Not `spec`/`specs`: outside Ruby those hold API and connector specifications, which are
# production code. Ruby specs are caught by their `_spec.rb` file names instead.
TEST_DIRS = {"test", "tests", "__tests__", "e2e", "__mocks__", "mocks", "fixtures", "testdata",
             "test_data", "cypress", "playwright"}  # fmt: skip
TEST_FILE = re.compile(
    r"(^test_.*\.py$|_test\.(py|go)$|_spec\.rb$|\.(test|spec|e2e)\.[a-z]+$"
    r"|^(pytest\.ini|conftest\.py|tox\.ini|\.env\.test.*)$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Fact:
    """One observation about the codebase, before any interpretation."""

    kind: str  # a catalog detect kind, or an internal one such as "local_base_url"
    value: str  # the normalised thing observed, e.g. "openai"
    file: Path
    line: int
    evidence: str  # the source line, trimmed
    manifest: bool = False  # declared in a package manifest, not used in code
    in_test: bool = False  # found in test, spec or fixture code


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


def is_test_path(rel: str) -> bool:
    parts = rel.lower().split("/")
    return any(p in TEST_DIRS for p in parts[:-1]) or bool(TEST_FILE.search(parts[-1]))


# --------------------------------------------------------------------------
# Ignore rules: gitignore semantics, for .gitignore, .unrentignore and --exclude
# --------------------------------------------------------------------------


def _glob_regex(pattern: str) -> re.Pattern:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        elif pattern[i] == "[" and "]" in pattern[i + 1 :]:
            j = pattern.index("]", i + 1)
            body = pattern[i + 1 : j]
            out += "[" + ("^" + body[1:] if body.startswith("!") else body) + "]"
            i = j + 1
        elif pattern[i] == "\\" and i + 1 < len(pattern):
            out, i = out + re.escape(pattern[i + 1]), i + 2
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out)


@dataclass(frozen=True)
class IgnoreRule:
    base: str  # directory the rule file lives in, relative to the scan root
    regex: re.Pattern
    negate: bool
    dir_only: bool
    anchored: bool

    @classmethod
    def parse(cls, line: str, base: str = "") -> IgnoreRule | None:
        line = line.rstrip("\n").rstrip("\r")
        if not line.strip() or line.startswith("#"):
            return None
        if not line.endswith("\\ "):
            line = line.rstrip(" ")
        negate = line.startswith("!")
        if negate:
            line = line[1:]
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        anchored = "/" in line
        line = line.lstrip("/")
        if not line:
            return None
        return cls(base, _glob_regex(line), negate, dir_only, anchored)

    def applies(self, rel: str, is_dir: bool) -> bool:
        if self.dir_only and not is_dir:
            return False
        if self.base:
            if not rel.startswith(self.base + "/"):
                return False
            rel = rel[len(self.base) + 1 :]
        target = rel if self.anchored else rel.rsplit("/", 1)[-1]
        return self.regex.fullmatch(target) is not None


def is_ignored(rel: str, rules: list[IgnoreRule], is_dir: bool = False) -> bool:
    """Git's rule: a path is ignored if it, or any directory above it, is ignored.
    Within one path the last matching rule wins, so `!` re-includes."""
    if not rules:
        return False
    parts = rel.split("/")
    for i in range(1, len(parts) + 1):
        sub = "/".join(parts[:i])
        sub_is_dir = is_dir or i < len(parts)
        state = False
        for rule in rules:
            if rule.applies(sub, sub_is_dir):
                state = not rule.negate
        if state:
            return True
    return False


def _rules_from(text: str, base: str = "") -> list[IgnoreRule]:
    return [r for r in (IgnoreRule.parse(ln, base) for ln in text.splitlines()) if r]


def load_ignore(root: Path, exclude: Iterable[str] = ()) -> list[IgnoreRule]:
    """.unrentignore plus --exclude patterns, both with .gitignore syntax."""
    file = root / ".unrentignore"
    text = file.read_text("utf-8", errors="replace") if file.is_file() else ""
    return _rules_from(text) + _rules_from("\n".join(exclude))


# --------------------------------------------------------------------------
# File discovery
# --------------------------------------------------------------------------


def _git(root: Path, *args: str) -> list[str] | None:
    try:
        out = subprocess.run(["git", *args], cwd=root, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    # fsdecode, not utf-8: a latin-1 file name on Linux must round-trip to a real path.
    return [p for p in os.fsdecode(out.stdout).split("\0") if p]


def _git_files(root: Path) -> list[Path] | None:
    """Tracked files (submodules included) plus untracked-but-not-ignored ones.

    None when root is not in a git repository, or when git lists nothing — which
    also happens when root is a directory the enclosing repo ignores, and scanning
    it explicitly means the user wants it scanned.
    """
    tracked = _git(root, "ls-files", "-z", "--cached", "--recurse-submodules")
    if tracked is None:
        return None
    untracked = _git(root, "ls-files", "-z", "--others", "--exclude-standard") or []
    names = sorted(set(tracked) | set(untracked))
    return [root / p for p in names] or None


def _walk(root: Path) -> list[Path]:
    """Every file under root, honouring .gitignore files as git would."""
    rules: list[IgnoreRule] = []
    heuristics = not (root / ".gitignore").is_file()
    out: list[Path] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        rel_dir = directory.relative_to(root).as_posix() if directory != root else ""
        gitignore = directory / ".gitignore"
        if gitignore.is_file():
            rules = rules + _rules_from(gitignore.read_text("utf-8", errors="replace"), rel_dir)
        try:
            children = sorted(directory.iterdir())
        except OSError:
            continue
        for child in children:
            rel = f"{rel_dir}/{child.name}" if rel_dir else child.name
            if child.is_symlink():
                continue  # loops, and files that live elsewhere
            if child.is_dir():
                if child.name in SKIP_DIRS or is_ignored(rel, rules, is_dir=True):
                    continue
                if heuristics and child.name in HEURISTIC_SKIP_DIRS:
                    continue
                if (child / "pyvenv.cfg").is_file():
                    continue  # a virtualenv, whatever it is called
                stack.append(child)
            elif child.is_file() and not is_ignored(rel, rules):
                out.append(child)
    return out


def iter_files(
    root: Path, exclude: Iterable[str] = (), skipped: list[Path] | None = None
) -> list[Path]:
    """Files to scan. Files over the size limit are left out and, when `skipped` is
    given, recorded there so the report can say so."""
    rules = load_ignore(root, exclude)
    files = _git_files(root)
    if files is None:
        files = _walk(root)
    out = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        if any(part in SKIP_DIRS for part in rel.split("/")[:-1]):
            continue
        if is_ignored(rel, rules):
            continue
        try:
            # Symlinks are skipped here as in the walk: they loop, point outside the
            # tree, and ripgrep does not follow them either.
            if path.is_symlink() or not path.is_file():
                continue
            size = path.stat().st_size
        except OSError:
            continue
        limit = MAX_NOTEBOOK_BYTES if path.suffix.lower() == ".ipynb" else MAX_FILE_BYTES
        if size > limit:
            if skipped is not None and _is_relevant(path):
                skipped.append(path)
            continue
        out.append(path)
    return sorted(out)


def _is_relevant(path: Path) -> bool:
    return (
        _manifest_parser(path) is not None
        or _is_text_source(path)
        or path.suffix.lower() == ".ipynb"
    )


@functools.lru_cache(maxsize=4)  # the code view and the imports parse the same file
def _parse_python(text: str) -> ast.Module:
    """ast.parse without the SyntaxWarnings (`invalid escape sequence`) that someone
    else's code would print on our stderr. Raises SyntaxError/ValueError as usual, also
    for code nested too deep to parse. The tree is shared between callers: read it,
    never change it."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return ast.parse(text)
        except (MemoryError, RecursionError) as exc:  # "Parser stack overflowed"
            raise SyntaxError(str(exc)) from None


_STATEMENT_FIELDS = frozenset(("body", "orelse", "finalbody", "handlers", "cases"))


def _statements(tree: ast.AST) -> Iterator[ast.AST]:
    """The tree and every statement in it, nested ones too, in `ast.walk` order but
    without the expressions, which are most of the nodes. Docstrings and imports are
    statements. (`except` handlers and `case` clauses come along as containers.)"""
    queue: deque[ast.AST] = deque([tree])
    while queue:
        node = queue.popleft()
        yield node
        for name in node._fields:
            if name in _STATEMENT_FIELDS:
                queue.extend(getattr(node, name))


def _split_lines(text: str) -> list[str]:
    """Lines as ripgrep and editors number them: split on newlines only.

    `str.splitlines` also splits on form feeds, U+2028 and friends, which puts
    every later line number out by one.
    """
    if "\r" not in text:
        return text.split("\n")
    return [ln.removesuffix("\r") for ln in text.split("\n")]


def _read(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")  # Windows editors, PowerShell
    if b"\0" in raw[:8192]:
        return None  # binary
    return raw.decode("utf-8-sig", errors="replace")


# --------------------------------------------------------------------------
# Code view: the file with comments blanked, strings kept, lines preserved
# --------------------------------------------------------------------------


# Strings first, so a comment marker inside one is text: `"/api/*"` does not open a
# comment. Single and double quotes close on their own line or not at all — an
# unclosed one is a Rust lifetime or an apostrophe, and is left as a plain char.
_C_STRING = r"""\"(?:\\.|[^"\\\n])*\"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`"""
_C_TOKENS = re.compile(_C_STRING + r"|(//[^\n]*|/\*.*?(?:\*/|\Z))", re.DOTALL)
_C_HASH_TOKENS = re.compile(_C_STRING + r"|(//[^\n]*|#(?!\[)[^\n]*|/\*.*?(?:\*/|\Z))", re.DOTALL)
_NOT_NEWLINE = re.compile(r"[^\n]")


def _c_like(text: str, hash_comments: bool = False) -> str:
    """Blank `//` and `/* */` comments (and `#` ones, for PHP and HCL), leaving
    string literals and line numbers alone."""
    pattern = _C_HASH_TOKENS if hash_comments else _C_TOKENS
    return pattern.sub(
        lambda m: _NOT_NEWLINE.sub(" ", m.group(0)) if m.group(1) else m.group(0), text
    )


def _hash_like(text: str, extra: str = "") -> str:
    """Blank `#` comments that start a line or follow whitespace outside quotes —
    `url#fragment` and `"#hex"` stay. `extra` adds line-start comment markers."""
    lines = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        if extra and stripped.startswith(tuple(extra.split())):
            lines.append(" " * len(line))
            continue
        if "#" not in line:
            lines.append(line)
            continue
        quote = None
        cut = len(line)
        for i, ch in enumerate(line):
            if quote:
                if ch == quote:
                    quote = None
            elif ch in "\"'":
                quote = ch
            elif ch == "#" and (i == 0 or line[i - 1].isspace()) and not line.startswith("#!", i):
                cut = i
                break
        lines.append(line[:cut] + " " * (len(line) - cut))
    return "\n".join(lines)


def _python_view(text: str) -> str:
    """Comments and docstrings blanked. Ordinary strings stay, because
    `client("bedrock-runtime")` is a string and is exactly the evidence we want."""
    lines = _split_lines(text)
    try:
        for tok in tokenize.generate_tokens(StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                row, col = tok.start
                if 0 < row <= len(lines):
                    lines[row - 1] = lines[row - 1][:col]
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return _hash_like(text)
    try:
        tree = _parse_python(text)
    except (SyntaxError, ValueError):
        return "\n".join(lines)
    for node in _statements(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        doc = node.body[0] if node.body else None
        if (
            isinstance(doc, ast.Expr)
            and isinstance(doc.value, ast.Constant)
            and isinstance(doc.value.value, str)
        ):
            for row in range(doc.lineno, (doc.end_lineno or doc.lineno) + 1):
                lines[row - 1] = ""
    return "\n".join(lines)


_SCRIPT = re.compile(r"(<script\b[^>]*>)(.*?)(</script>)", re.IGNORECASE | re.DOTALL)


def _html_view(text: str) -> str:
    """Only what runs: the contents of <script> blocks. Page prose about a vendor
    is documentation."""
    out, last = [], 0
    for m in _SCRIPT.finditer(text):
        out.append(_NOT_NEWLINE.sub(" ", text[last : m.start(2)]))
        out.append(_c_like(m.group(2)))
        last = m.end(2)
    out.append(_NOT_NEWLINE.sub(" ", text[last:]))
    return "".join(out)


_XML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _xml_view(text: str) -> str:
    return _XML_COMMENT.sub(lambda m: _NOT_NEWLINE.sub(" ", m.group(0)), text)


def code_view(path: Path, text: str) -> str:
    suffix = path.suffix.lower()
    if suffix in (".py", ".pyi"):
        return _python_view(text)
    if suffix in C_FAMILY:
        return _c_like(text, hash_comments=suffix in C_AND_HASH)
    if suffix in HTML:
        return _html_view(text)
    if suffix in XML_LIKE:
        return _xml_view(text)
    if suffix in DASH_COMMENT:
        return _hash_like(text.replace("#", "\0"), extra="--").replace("\0", "#")
    if suffix in (".ini",):
        return _hash_like(text, extra=";")
    if suffix == ".properties":
        return _hash_like(text, extra="!")
    if suffix == ".vb":
        return _hash_like(text.replace("#", "\0"), extra="'").replace("\0", "#")
    return _hash_like(text)  # shell, YAML, TOML, Ruby, R, Dockerfile, .env, …


# --------------------------------------------------------------------------
# Needles: catalog strings searched in code
# --------------------------------------------------------------------------

_NORMALISE = re.compile(r"[-_.]+")


def normalise(name: str) -> str:
    """PEP 503: `Pinecone_Client` and `pinecone-client` are one package."""
    return _NORMALISE.sub("-", name).lower()


MULTI_PART_TLDS = {"co.uk", "com.cn", "com.au", "co.jp", "com.br", "co.kr", "com.tw", "com.hk"}


def registered_domain(host: str) -> str:
    parts = host.lower().strip(".").split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in MULTI_PART_TLDS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _endpoint_head(needle: str) -> str:
    """What may not come right before an endpoint needle. A hyphen may, when the needle
    is a host below its registered domain: Google joins the region to the host with one
    (`us-central1-aiplatform.googleapis.com`), and the result is still Google's. Before
    `modal.run` a hyphen makes another domain (`my-modal.run`), and before a partial
    host like `bedrock-runtime.` a file name (`amazon-bedrock-runtime.ts`)."""
    host = needle.split("/", 1)[0].split(":", 1)[0].lower()
    if (
        host
        and not host.endswith(".")
        and not host.rsplit(".", 1)[-1].isdigit()
        and host != registered_domain(host)
    ):
        return r"(?<![A-Za-z0-9])"
    return r"(?<![A-Za-z0-9-])"


def _needle_pattern(kind: str, needle: str) -> re.Pattern:
    """Boundary rules per kind.

    symbol    word-bounded and case-sensitive: `OpenAI(` must not match inside
              `AzureOpenAI(`, and `searchclient` in a URL is not `SearchClient`.
    model     a prefix of a model id: `gpt-4` matches `gpt-4o-mini`, but not
              `chatgpt-4`, and not `bedrock/` inside a path like `./bedrock/x`.
    endpoint  a host, matched case-insensitively; a subdomain may precede it (see
              `_endpoint_head` for a hyphen), and a call may not follow it:
              `modal.run()` is code, `x.modal.run/` a host.
    env       an exact variable name.
    image     a container image reference anywhere a string can hold one: Testcontainers
              `new QdrantContainer("qdrant/qdrant:v1")`, `docker run qdrant/qdrant`.
              Not inside a longer path (`github.com/qdrant/qdrant` is the repo).
    sql_extension  `CREATE EXTENSION [IF NOT EXISTS] ["]vector["]`, any case.
    """
    escaped = re.escape(needle)
    if kind == "image":
        return re.compile(
            r"(?<![A-Za-z0-9_./@-])" + escaped + r"(?![A-Za-z0-9_./-])", re.IGNORECASE
        )
    if kind == "sql_extension":
        return re.compile(
            r"\bcreate\s+extension\s+(?:if\s+not\s+exists\s+)?[\"'`]?"
            + escaped
            + r"[\"'`]?(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
    ends_word = needle[-1:].isalnum() or needle[-1:] == "_"
    if kind == "symbol":
        head = r"(?<![A-Za-z0-9_$])" if needle[:1].isalnum() or needle[:1] in "_$" else ""
        return re.compile(head + escaped + (r"(?![A-Za-z0-9_])" if ends_word else ""))
    if kind == "model":
        return re.compile(r"(?<![A-Za-z0-9_./@-])" + escaped)
    if kind == "endpoint":
        tail = r"(?![A-Za-z0-9(-])" if ends_word else ""
        return re.compile(_endpoint_head(needle) + escaped + tail, re.IGNORECASE)
    return re.compile(r"(?<![A-Za-z0-9_])" + escaped + r"(?![A-Za-z0-9_])")


# Needle kinds searched without regard to case: hosts, image names, SQL.
FOLDED_KINDS = frozenset({"endpoint", "image", "sql_extension"})


def _probe(kind: str, needle: str) -> str:
    """The literal a line must contain before the needle's pattern is tried."""
    if kind == "sql_extension":
        return "extension"
    return needle.lower() if kind in FOLDED_KINDS else needle


_NEWLINE = re.compile(r"\n")
_MODEL_TOKEN = re.compile(r"[A-Za-z0-9_./:@-]+")
_FILE_EXTENSION = re.compile(r"\.[A-Za-z]{2,5}$")
_OLLAMA_TAG = re.compile(r":(?!\d+$)[A-Za-z0-9._-]+$")  # `command-r:35b`, not bedrock's `-v1:0`
# Lines that name a model without calling it: tokenizers and local runtimes.
_MODEL_NOT_A_CALL = re.compile(
    r"encoding_for_model|tiktoken|ollama|cl100k|o200k|p50k|tokeniz", re.IGNORECASE
)
_ENCODING_CONSTANT = re.compile(r"encoding[A-Z_]")  # Go/Java: encodingCL100KBase

Needle = tuple[str, str, str, re.Pattern]  # kind, needle, probe, pattern


def _offsets(text: str) -> list[int]:
    return [0, *(m.end() for m in _NEWLINE.finditer(text))]


class Needles:
    TEXT_KINDS = ("symbol", "model", "endpoint", "env", "image", "sql_extension")

    def __init__(self, catalog: Catalog):
        self.items: list[Needle] = []
        # Per model needle: open-weight ids it must not report.
        self.open_models: dict[str, set[str]] = {}
        seen = set()
        for service in catalog.detectable:
            for needle in service.detect.get("model", ()):
                self.open_models.setdefault(needle, set()).update(service.open_models)
            for kind in self.TEXT_KINDS:
                for needle in service.detect.get(kind, ()):
                    if (kind, needle) in seen:
                        continue
                    seen.add((kind, needle))
                    probe = _probe(kind, needle)
                    self.items.append((kind, needle, probe, _needle_pattern(kind, needle)))

    def _hits(self, text: str) -> dict[int, list[Needle]]:
        """Line number → needles whose literal text occurs on it.

        One C-speed `str.find` scan per needle. Offsets are computed on the text
        actually searched: lower-casing can change a string's length (`İ` becomes
        two code points), so the original text's offsets would be wrong.
        """
        lowered = text.lower()
        offsets = {False: _offsets(text), True: None}
        hits: dict[int, list[Needle]] = {}
        for item in self.items:
            folded = item[0] in FOLDED_KINDS
            haystack = lowered if folded else text
            at = haystack.find(item[2])
            if at < 0:
                continue
            if offsets[folded] is None:
                offsets[folded] = _offsets(haystack)
            lines = offsets[folded]
            while at >= 0:
                n = bisect.bisect_right(lines, at)
                hits.setdefault(n, []).append(item)
                at = haystack.find(item[2], lines[n] if n < len(lines) else len(haystack))
        return hits

    def _items_on(self, line: str) -> list[Needle]:
        lowered = line.lower()
        return [it for it in self.items if it[2] in (lowered if it[0] in FOLDED_KINDS else line)]

    def _closed_model_at(self, needle: str, pattern: re.Pattern, line: str) -> re.Match | None:
        """The first occurrence of the prefix that names a closed model, if any."""
        if _MODEL_NOT_A_CALL.search(line) or _ENCODING_CONSTANT.search(line):
            return None
        exempt = self.open_models.get(needle, ())
        for m in pattern.finditer(line):
            token = _MODEL_TOKEN.match(line, m.start())
            model_id = token.group(0) if token else needle
            rest = model_id[len(needle) :]
            # A vendor prefix names a model only when a model name follows it: not the
            # URI scheme `github://`, not the regex `/^snowflake/i`.
            if needle.endswith(("/", ":")) and (len(rest) < 2 or not rest[0].isalnum()):
                continue
            if _FILE_EXTENSION.search(model_id) and not rest[-1:].isdigit():
                continue  # `bedrock/geology.csv` is a path
            if ":" not in needle and _OLLAMA_TAG.search(model_id):
                continue  # `command-r:35b` is an Ollama tag, run locally
            if any(e in model_id.lower() for e in exempt):
                continue
            return m
        return None

    def search(
        self,
        path: Path,
        lines: list[str],
        code: list[str],
        hit_lines: Iterable[int] | None = None,
    ) -> list[Fact]:
        """Needles in the code view. Evidence quotes the original line.

        `hit_lines`, when ripgrep has already found them, saves searching again.
        """
        if hit_lines is None:
            hits = self._hits("\n".join(lines))
        else:
            hits = {n: self._items_on(lines[n - 1]) for n in hit_lines if 0 < n <= len(lines)}
        facts = []
        for n, items in sorted(hits.items()):
            view = code[n - 1] if n <= len(code) else ""
            if not view.strip():
                continue
            for kind, needle, _, pattern in items:
                if kind == "model":
                    m = self._closed_model_at(needle, pattern, view)
                else:
                    m = pattern.search(view)
                if m:
                    facts.append(Fact(kind, needle, path, n, _snippet(lines[n - 1], m.start())))
        return facts


def _snippet(line: str, at: int = 0) -> str:
    """The line, trimmed and with secrets masked; for long lines (minified, one-line
    configs), the part around the match."""
    stripped = line.strip()
    if len(stripped) > EVIDENCE_CHARS:
        start = max(at - 60, 0)
        stripped = ("…" if start else "") + line[start : start + EVIDENCE_CHARS].strip() + "…"
    return redact(stripped)


# Reports get pasted into issues and chats; the .env line that proves a dependency
# must not carry the key with it.
_SECRET_ASSIGNMENT = re.compile(
    r"""(?i)\b([A-Za-z0-9_]*(?:key|token|secret|password|passwd|credential)s?)(["']?\s*[:=]\s*["']?)([^\s"',;]{6,})"""
)
_SECRET_SHAPES = re.compile(
    r"\b(sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}|hf_[A-Za-z0-9]{30,}|xai-[A-Za-z0-9]{20,}|gsk_[A-Za-z0-9]{20,}"
    r"|pcsk_[A-Za-z0-9_]{20,}|r8_[A-Za-z0-9]{20,}|tvly-[A-Za-z0-9-]{20,}|fc-[a-f0-9]{24,}"
    r"|eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]*)"
)
_NOT_A_SECRET = re.compile(r"[($<{]|env|getenv|secrets\.|config\.|settings\.", re.IGNORECASE)


_ENV_NAME = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")  # FIRECRAWL_API_KEY
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


def redact(text: str) -> str:
    def assignment(m: re.Match) -> str:
        value = m.group(3)
        if _NOT_A_SECRET.search(value) or set(value) <= set("*xX.-_<>"):
            return m.group(0)
        if _ENV_NAME.fullmatch(value):
            return m.group(0)  # the name of the variable that holds the key, not the key
        rest = m.string[m.end() :].lstrip()
        quoted = m.group(2).rstrip().endswith(("'", '"'))
        if not quoted and _IDENTIFIER.fullmatch(value) and rest[:2] in ("or", "||", "??", ")"):
            return m.group(0)  # `api_key = api_key or os.getenv(...)`: code, not a secret
        return f"{m.group(1)}{m.group(2)}****"

    text = _SECRET_ASSIGNMENT.sub(assignment, text)
    return _SECRET_SHAPES.sub(lambda m: m.group(1)[:4] + "****", text)


# --------------------------------------------------------------------------
# Imports, install commands, local OpenAI-compatible servers
# --------------------------------------------------------------------------

_PY_IMPORT_FALLBACK = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import\b|import\s+([\w.,\s]+))")
_PY_DYNAMIC_IMPORT = re.compile(r"""(?:import_module|__import__)\(\s*["']([\w.]+)["']""")


def _python_imports(path: Path, text: str, lines: list[str]) -> list[Fact]:
    """`import a.b`, `from a.b import c` (recorded as `a.b` and `a.b.c`, because
    `from google.cloud import documentai` imports `google.cloud.documentai`), and
    `importlib.import_module("a")`. Falls back to a regex when the file does not
    parse (Python 2, templates): an unparseable file still imports things."""

    def fact(name: str, n: int) -> Fact:
        return Fact(
            "python_import", name, path, n, _snippet(lines[n - 1]) if 0 < n <= len(lines) else ""
        )

    facts: list[Fact] = []
    try:
        tree = _parse_python(text)
    except (SyntaxError, ValueError):
        tree = None
    if tree is not None:
        # A dynamic import is a call, which can sit in any expression: walk them all
        # only when the file names one.
        dynamic = "import_module" in text or "__import__" in text
        for node in ast.walk(tree) if dynamic else _statements(tree):
            if isinstance(node, ast.Import):
                facts += [fact(alias.name, node.lineno) for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                facts.append(fact(node.module, node.lineno))
                facts += [
                    fact(f"{node.module}.{alias.name}", node.lineno)
                    for alias in node.names
                    if alias.name != "*"
                ]
            elif (
                isinstance(node, ast.Call)
                and getattr(node.func, "attr", getattr(node.func, "id", None))
                in ("import_module", "__import__")
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                facts.append(fact(node.args[0].value, node.lineno))
        return facts
    for i, raw in enumerate(_split_lines(text), start=1):
        m = _PY_IMPORT_FALLBACK.match(raw)
        if m:
            if m.group(1):
                names = [m.group(1)]
            else:
                names = [p.split()[0] for p in m.group(2).split(",") if p.strip()]
            facts += [fact(name, i) for name in names]
        facts += [fact(d.group(1), i) for d in _PY_DYNAMIC_IMPORT.finditer(raw)]
    return facts


_JS_IMPORT = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*|\bexport\s+\*\s+from\s*)"""
    r"""['"]([^'"\n]+)['"]"""
)


def js_package(specifier: str) -> str | None:
    """`@scope/pkg/sub` → `@scope/pkg`; `npm:openai@4` and `jsr:@a/b` → the
    package; relative paths and built-ins → None."""
    spec = specifier.strip()
    for prefix in ("npm:", "jsr:"):
        if spec.startswith(prefix):
            spec = spec[len(prefix) :].lstrip("/")
    if not spec or spec.startswith((".", "/", "~", "#", "node:", "http:", "https:", "$")):
        return None
    parts = spec.split("/")
    if spec.startswith("@"):
        if len(parts) < 2:
            return None
        name = f"{parts[0]}/{parts[1].split('@', 1)[0]}"
    else:
        name = parts[0].split("@", 1)[0]
    return name or None


def _js_imports(path: Path, code: str, lines: list[str]) -> list[Fact]:
    facts = []
    for m in _JS_IMPORT.finditer(code):
        name = js_package(m.group(1))
        if name:
            n = code.count("\n", 0, m.start()) + 1
            facts.append(Fact("npm", name, path, n, _snippet(lines[n - 1])))
    return facts


_PIP_INSTALL = re.compile(
    r"(?:\bpip3?|\bpython3?\s+-m\s+pip|\buv\s+pip|\bpipenv|\bpoetry|\bpdm|\buv|\brye|\bconda|\bmamba)"
    r"\s+(?:install|add)\s+([^\n;&|#]+)"
)
_NPM_INSTALL = re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:install|i|add)\s+([^\n;&|#]+)")
_TAKES_VALUE = {"-r", "-c", "-e", "-i", "-f", "--requirement", "--constraint", "--editable",
                "--index-url", "--extra-index-url", "--find-links", "--target", "-t", "--prefix",
                "--group", "-G", "--channel"}  # fmt: skip
_SPEC_SPLIT = re.compile(r"[\[<>=!~;@ ]")


def _logical_lines(lines: list[str]) -> list[tuple[int, str]]:
    """Join `\\` (shell) and backtick (PowerShell) continuations; keep the first line
    number, so a multi-line `RUN pip install \\` is cited where it starts."""
    out: list[tuple[int, str]] = []
    buffer, start = "", 0
    for n, raw in enumerate(lines, start=1):
        stripped = raw.rstrip()
        if not buffer:
            start = n
        if stripped.endswith(("\\", "`")):
            buffer += stripped[:-1] + " "
            continue
        out.append((start, buffer + raw))
        buffer = ""
    if buffer:
        out.append((start, buffer))
    return out


def _install_commands(path: Path, lines: list[str], code: list[str]) -> list[Fact]:
    facts = []
    for n, logical in _logical_lines(code):
        if "install" not in logical and "add" not in logical:
            continue
        line = logical.lstrip().lstrip("!%").lstrip()  # notebook magics
        for pattern, kind in ((_PIP_INSTALL, "requirement"), (_NPM_INSTALL, "npm")):
            for m in pattern.finditer(line):
                tokens = iter(m.group(1).split())
                for token in tokens:
                    token = token.strip("'\"`")
                    if token in _TAKES_VALUE:
                        next(tokens, None)
                        continue
                    if not token or token.startswith(("-", ".", "/", "$", "{", "git+", "http")):
                        continue
                    if kind == "npm":
                        name = js_package(token)
                    else:
                        name = _SPEC_SPLIT.split(token, 1)[0]
                        name = (
                            normalise(name)
                            if re.fullmatch(r"[A-Za-z0-9._-]+", name or "")
                            else None
                        )
                    if name:
                        facts.append(Fact(kind, name, path, n, _snippet(lines[n - 1])))
    return facts


_BASE_URL = re.compile(
    r"""(?i)\b(?:base_?url|api_?base|openai_api_base|openai_base_url|basepath)\b["']?\s*[:=]\s*"""
    r"""[^\n"'`]*?["'`]?(?:https?://)?\[?([A-Za-z0-9.:-]+?)\]?(?::\d+)?(?:[/"'`\s]|$)"""
)
_LOCAL_HOSTS = {
    "localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal", "ollama", "vllm",
    "lmstudio", "localai", "llama-server", "llamacpp", "sglang", "tgi",
}  # fmt: skip
_PRIVATE = re.compile(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)|\.(local|internal|lan)$")


_LOCAL_INFERENCE = re.compile(r"""(?i)\binference_mode\b["']?\s*[:=]\s*["'`](local|dynamic)["'`]""")


def _local_base_urls(path: Path, lines: list[str], code: list[str]) -> list[Fact]:
    """`base_url="http://localhost:11434/v1"`: an OpenAI-compatible client talking to
    a server you run — Ollama, vLLM, LM Studio — not to OpenAI. Also
    `inference_mode="local"`: an SDK told to run the model here (Nomic)."""
    facts = []
    for n, view in enumerate(code, start=1):
        lowered = view.lower()
        if "inference_mode" in lowered:
            for m in _LOCAL_INFERENCE.finditer(view):
                facts.append(
                    Fact("local_mode", m.group(1).lower(), path, n, _snippet(lines[n - 1]))
                )
        if "base" not in lowered:  # every spelling the pattern accepts contains it
            continue
        for m in _BASE_URL.finditer(view):
            host = m.group(1).lower()
            if host in _LOCAL_HOSTS or _PRIVATE.search(host):
                facts.append(Fact("local_base_url", host, path, n, _snippet(lines[n - 1])))
    return facts


# --------------------------------------------------------------------------
# Manifests
# --------------------------------------------------------------------------

_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_EGG = re.compile(r"#egg=([A-Za-z0-9._-]+)")


def _manifest_fact(kind: str, value: str, path: Path, lines: list[str], n: int) -> Fact:
    n = n if 0 < n <= len(lines) else 1
    return Fact(kind, value, path, n, _snippet(lines[n - 1]) if lines else "", manifest=True)


def _line_of(lines: list[str], *literals: str, token: str | None = None) -> int:
    """First line containing any literal verbatim; failing that, `token` as a whole
    word on a non-comment line; failing that, 1."""
    for literal in literals:
        for i, ln in enumerate(lines, start=1):
            if literal in ln:
                return i
    if token:
        pattern = re.compile(
            r"(?<![A-Za-z0-9_.-])" + re.escape(token) + r"(?![A-Za-z0-9_-])", re.IGNORECASE
        )
        for i, ln in enumerate(lines, start=1):
            if pattern.search(ln) and not ln.lstrip().startswith("#"):
                return i
    return 1


def _requirement_spec(spec: str) -> str | None:
    spec = spec.strip()
    if not spec or spec.startswith("#"):
        return None
    egg = _EGG.search(spec)
    if egg:
        return normalise(egg.group(1))
    if spec.startswith(("-", "git+", "http:", "https:", "file:", ".", "/")):
        return None
    m = _REQ_NAME.match(spec)
    return normalise(m.group(1)) if m else None


_EXTRAS = re.compile(r"^\s*[A-Za-z0-9][A-Za-z0-9._-]*\s*\[([^\]]+)\]")


def _extras(spec: str) -> list[str]:
    """`qdrant-client[fastembed]` installs fastembed: an extra named after a package is
    that package."""
    m = _EXTRAS.match(spec)
    return [normalise(e.strip()) for e in m.group(1).split(",") if e.strip()] if m else []


def _requirements_txt(path: Path, text: str, lines: list[str]) -> list[Fact]:
    facts = []
    for n, raw in _logical_lines(lines):
        spec = raw.split(" #", 1)[0]
        name = _requirement_spec(spec)
        if name:
            facts.append(_manifest_fact("requirement", name, path, lines, n))
            facts += [_manifest_fact("requirement", e, path, lines, n) for e in _extras(spec)]
    return facts


def _specs_to_facts(
    specs: Iterable[str], path: Path, lines: list[str], keyed: bool = False
) -> list[Fact]:
    """Cite each spec where it is written: `"openai>=1"` as a quoted string, or
    `openai = "*"` as a key — not the first line that happens to mention openai.
    `keyed`: the specs are table keys (Poetry, Pipfile), so the key line comes first."""
    facts = []
    for spec in specs:
        if not isinstance(spec, str):
            continue
        name = _requirement_spec(spec)
        if not name:
            continue
        raw = (_REQ_NAME.match(spec.strip()) or re.match("(.*)", spec)).group(1)
        # A table key first (`llama-index = "0.9.7"`): its quoted name may also sit in
        # `keywords = ["llama-index"]` higher up.
        key = re.compile(r"^\s*[\"']?" + re.escape(raw) + r"[\"']?\s*=", re.IGNORECASE)
        at_key = (i for i, ln in enumerate(lines, start=1) if keyed and key.match(ln))
        n = next(at_key, None) or _line_of(
            lines,
            f'"{spec}"', f"'{spec}'", f'"{raw}"', f"'{raw}'",
            f"{raw} =", f"{raw}=", f'"{raw}" =',
            token=raw,
        )  # fmt: skip
        facts.append(_manifest_fact("requirement", name, path, lines, n))
        facts += [_manifest_fact("requirement", e, path, lines, n) for e in _extras(spec)]
    return facts


def _pyproject(path: Path, text: str, lines: list[str]) -> list[Fact]:
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError):
        return []
    project = data.get("project") or {}
    tool = data.get("tool") or {}
    specs: list[str] = list(project.get("dependencies") or [])
    for group in (project.get("optional-dependencies") or {}).values():
        specs += group or []
    for group in (data.get("dependency-groups") or {}).values():
        specs += [g for g in group or [] if isinstance(g, str)]
    specs += list((tool.get("uv") or {}).get("dev-dependencies") or [])
    for group in ((tool.get("pdm") or {}).get("dev-dependencies") or {}).values():
        specs += group or []
    for env in ((tool.get("hatch") or {}).get("envs") or {}).values():
        specs += list((env or {}).get("dependencies") or [])
        specs += list((env or {}).get("extra-dependencies") or [])
    poetry = tool.get("poetry") or {}
    tables = [poetry.get("dependencies"), poetry.get("dev-dependencies")]
    tables += [(group or {}).get("dependencies") for group in (poetry.get("group") or {}).values()]
    keys: list[str] = []
    for table in tables:
        for name, spec in (table or {}).items():
            if name.lower() == "python":
                continue
            # `qdrant-client = { extras = ["fastembed"] }`: the extras are packages too.
            extras = spec.get("extras") if isinstance(spec, dict) else None
            keys.append(f"{name}[{','.join(extras)}]" if extras else name)
    return _specs_to_facts(specs, path, lines) + _specs_to_facts(keys, path, lines, keyed=True)


def _setup_py(path: Path, text: str, lines: list[str]) -> list[Fact]:
    try:
        tree = _parse_python(text)
    except (SyntaxError, ValueError):
        return []
    facts = []
    wanted = {"install_requires", "extras_require", "setup_requires", "tests_require"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in wanted:
                    for c in ast.walk(kw.value):
                        if isinstance(c, ast.Constant) and isinstance(c.value, str):
                            name = _requirement_spec(c.value)
                            if name:
                                facts.append(
                                    _manifest_fact("requirement", name, path, lines, c.lineno)
                                )
    return facts


def _setup_cfg(path: Path, text: str, lines: list[str]) -> list[Fact]:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        return []
    specs: list[str] = []
    for key in ("install_requires", "setup_requires"):
        if parser.has_option("options", key):
            specs += parser.get("options", key).splitlines()
    if parser.has_section("options.extras_require"):
        for _, value in parser.items("options.extras_require"):
            specs += value.splitlines()
    return _specs_to_facts(specs, path, lines)


def _pipfile(path: Path, text: str, lines: list[str]) -> list[Fact]:
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError):
        return []
    names = list(data.get("packages") or {}) + list(data.get("dev-packages") or {})
    return _specs_to_facts(names, path, lines, keyed=True)


def _conda_env(path: Path, text: str, lines: list[str]) -> list[Fact]:
    data = _yaml(text)
    if not isinstance(data, dict) or not isinstance(data.get("dependencies"), list):
        return []
    specs: list[str] = []
    for dep in data["dependencies"]:
        if isinstance(dep, str):
            specs.append(re.split(r"[=<>! ]", dep.split("::")[-1], maxsplit=1)[0])
        elif isinstance(dep, dict):
            specs += [s for s in dep.get("pip") or [] if isinstance(s, str)]
    return _specs_to_facts(specs, path, lines)


def _json(text: str):
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def _yaml(text: str):
    """None for YAML that doesn't load: besides YAMLError, PyYAML raises ValueError for
    `2020-13-45`, AttributeError and KeyError for a bad `!!timestamp` or `!!bool`."""
    try:
        return yaml.safe_load(text)
    except (yaml.YAMLError, ValueError, AttributeError, KeyError, RecursionError):
        return None


def _json_keys(
    kind: str, names: Iterable[str], path: Path, lines: list[str], lower=False
) -> list[Fact]:
    """Cite `"name":` — the key — so `"keywords": ["openai"]` is never the citation."""
    return [
        _manifest_fact(
            kind,
            n.lower() if lower else n,
            path,
            lines,
            _line_of(lines, f'"{n}":', f'"{n}" :', f'"{n}"'),
        )
        for n in names
    ]


def _package_json(path: Path, text: str, lines: list[str]) -> list[Fact]:
    data = _json(text)
    if not isinstance(data, dict):
        return []
    names = []
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        block = data.get(section)
        names += list(block) if isinstance(block, dict) else []
    return _json_keys("npm", names, path, lines)


def _pnpm_workspace(path: Path, text: str, lines: list[str]) -> list[Fact]:
    """pnpm catalogs: versions declared once for the workspace, referenced from
    package.json as `"openai": "catalog:"`."""
    data = _yaml(text)
    if not isinstance(data, dict):
        return []
    names = list(data.get("catalog") or {})
    for catalog in (data.get("catalogs") or {}).values():
        names += list(catalog or {})
    return [
        _manifest_fact("npm", n, path, lines, _line_of(lines, f'"{n}":', f"'{n}':", f"{n}:"))
        for n in names
        if isinstance(n, str)
    ]


def _composer_json(path: Path, text: str, lines: list[str]) -> list[Fact]:
    data = _json(text)
    if not isinstance(data, dict):
        return []
    names = [n for s in ("require", "require-dev") for n in (data.get(s) or {}) if "/" in n]
    return _json_keys("composer", names, path, lines, lower=True)


def _pubspec(path: Path, text: str, lines: list[str]) -> list[Fact]:
    data = _yaml(text)
    if not isinstance(data, dict):
        return []
    names = [n for s in ("dependencies", "dev_dependencies") for n in (data.get(s) or {})]
    return [
        _manifest_fact("pub", n.lower(), path, lines, _line_of(lines, f"{n}:", token=n))
        for n in names
        if isinstance(n, str)
    ]


_SWIFT_PACKAGE = re.compile(
    r"""\.package\s*\(\s*(?:name\s*:\s*"[^"]*"\s*,\s*)?url\s*:\s*"([^"]+)\""""
)


def _package_swift(path: Path, text: str, lines: list[str]) -> list[Fact]:
    facts = []
    for m in _SWIFT_PACKAGE.finditer(text):
        repo = re.sub(r"^https?://|\.git$", "", m.group(1).lower())
        facts.append(_manifest_fact("swift", repo, path, lines, text.count("\n", 0, m.start()) + 1))
    return facts


_GO_REQUIRE = re.compile(r"^\s*(?:require\s+)?([a-z0-9.-]+\.[a-z]{2,}/[^\s]+)\s+v[0-9]")


def _go_mod(path: Path, text: str, lines: list[str]) -> list[Fact]:
    facts = []
    for n, raw in enumerate(lines, start=1):
        m = _GO_REQUIRE.match(raw)
        # `// indirect` marks a dependency of a dependency, which is not yours.
        if (
            m
            and "// indirect" not in raw
            and not raw.lstrip().startswith(("module", "replace", "//"))
        ):
            facts.append(_manifest_fact("go", m.group(1), path, lines, n))
    return facts


def _cargo_toml(path: Path, text: str, lines: list[str]) -> list[Fact]:
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError):
        return []
    tables = [data.get(k) for k in ("dependencies", "dev-dependencies", "build-dependencies")]
    tables.append((data.get("workspace") or {}).get("dependencies"))
    for target in (data.get("target") or {}).values():
        tables += [target.get(k) for k in ("dependencies", "dev-dependencies")]
    facts = []
    for table in tables:
        for key, spec in (table or {}).items():
            name = spec.get("package", key) if isinstance(spec, dict) else key
            n = _line_of(lines, f"{key} =", f"{key}=", token=key)
            facts.append(_manifest_fact("cargo", name.lower(), path, lines, n))
    return facts


_MAVEN_DEP = re.compile(
    r"<dependency>.*?<groupId>\s*([^<\s]+)\s*</groupId>.*?<artifactId>\s*([^<\s]+)\s*</artifactId>",
    re.DOTALL,
)


def _pom_xml(path: Path, text: str, lines: list[str]) -> list[Fact]:
    view = _xml_view(text)
    return [
        _manifest_fact(
            "maven",
            f"{m.group(1)}:{m.group(2)}".lower(),
            path,
            lines,
            view.count("\n", 0, m.start(2)) + 1,
        )
        for m in _MAVEN_DEP.finditer(view)
    ]


_GRADLE_COORD = re.compile(r"""['"]([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+)(?::[^'"\s]*)?['"]""")
_GRADLE_MAP = re.compile(
    r"""group\s*[:=]\s*['"]([^'"]+)['"]\s*,\s*(?:name|module)\s*[:=]\s*['"]([^'"]+)['"]"""
)


def _gradle(path: Path, text: str, lines: list[str]) -> list[Fact]:
    view = _split_lines(_c_like(text) if path.suffix.lower() != ".toml" else _hash_like(text))
    facts = []
    for n, raw in enumerate(view, start=1):
        for m in [*_GRADLE_COORD.finditer(raw), *_GRADLE_MAP.finditer(raw)]:
            if "." in m.group(1):  # skip `id "x:y"` plugin forms and other colon strings
                facts.append(
                    _manifest_fact("maven", f"{m.group(1)}:{m.group(2)}".lower(), path, lines, n)
                )
    return facts


_NUGET = re.compile(
    r"""<(?:PackageReference|PackageVersion|package)\b[^>]*?\b(?:Include|Update|id)\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)


def _nuget(path: Path, text: str, lines: list[str]) -> list[Fact]:
    view = _xml_view(text)
    return [
        _manifest_fact("nuget", m.group(1).lower(), path, lines, view.count("\n", 0, m.start()) + 1)
        for m in _NUGET.finditer(view)
    ]


_GEM = re.compile(
    r"""^\s*(?:gem|\w+\.add_(?:runtime_|development_)?dependency)\s*\(?\s*['"]([^'"]+)['"]"""
)


def _gems(path: Path, text: str, lines: list[str]) -> list[Fact]:
    facts = []
    for n, raw in enumerate(lines, start=1):
        m = _GEM.match(raw)
        if m:
            facts.append(_manifest_fact("gem", m.group(1).lower(), path, lines, n))
    return facts


_TF_BLOCK = re.compile(r'^\s*(?:resource|data)\s+"([a-z0-9_]+)"')
_IMAGE_KEY = re.compile(r"""^\s*(?:-\s*)?image:\s*["']?([^\s"'#]+)""")
_HELM_REPOSITORY = re.compile(r"""^\s*(?:-\s*)?repository:\s*["']?([^\s"'#]+)""")
_FROM = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", re.IGNORECASE)
_COMPOSE_DEFAULT = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:?-([^}]*)\}")


def image_name(ref: str) -> str | None:
    """`docker.io/qdrant/qdrant:v1.12@sha256:…` → `qdrant/qdrant`. A compose default,
    `${IMAGE:-qdrant/qdrant}`, is the image that runs unless overridden. Other
    templated references (`${REGISTRY}/…`, `{{ .Values… }}`) name no image we can know."""
    ref = _COMPOSE_DEFAULT.sub(lambda m: m.group(1), ref or "")
    if not ref or ref.startswith(("$", "{")) or "{{" in ref or "${" in ref:
        return None
    name = ref.split("@", 1)[0]
    head, _, last = name.rpartition("/")
    last = last.split(":", 1)[0]  # the tag; a registry port sits before the last "/"
    name = f"{head}/{last}" if head else last
    for prefix in ("docker.io/", "index.docker.io/", "registry-1.docker.io/"):
        name = name.removeprefix(prefix)
    return name.removeprefix("library/").lower() or None


def _images(path: Path, lines: list[str], code: list[str]) -> list[Fact]:
    """Images a project runs: compose and Kubernetes `image:`, Helm `repository:` in
    values files, and Dockerfile `FROM`."""
    name = path.name.lower()
    dockerfile = _is_dockerfile(name)
    patterns = [_FROM] if dockerfile else [_IMAGE_KEY]
    if "values" in name and not dockerfile:  # values.yaml, prod-values.yaml, values-gpu.yaml
        patterns.append(_HELM_REPOSITORY)
    facts = []
    for n, view in enumerate(code, start=1):
        for pattern in patterns:
            m = pattern.match(view)
            image = image_name(m.group(1)) if m else None
            if image:
                facts.append(Fact("image", image, path, n, _snippet(lines[n - 1])))
    return facts


def _terraform(path: Path, lines: list[str], code: list[str]) -> list[Fact]:
    facts = []
    for n, view in enumerate(code, start=1):
        m = _TF_BLOCK.match(view)
        if m:
            facts.append(Fact("terraform_resource", m.group(1), path, n, _snippet(lines[n - 1])))
    return facts


_MANIFESTS = {
    "pyproject.toml": _pyproject,
    "setup.py": _setup_py,
    "setup.cfg": _setup_cfg,
    "pipfile": _pipfile,
    "package.json": _package_json,
    "pnpm-workspace.yaml": _pnpm_workspace,
    "composer.json": _composer_json,
    "pubspec.yaml": _pubspec,
    "package.swift": _package_swift,
    "go.mod": _go_mod,
    "cargo.toml": _cargo_toml,
    "pom.xml": _pom_xml,
    "build.gradle": _gradle,
    "build.gradle.kts": _gradle,
    "libs.versions.toml": _gradle,
    "packages.config": _nuget,
    "directory.packages.props": _nuget,
    "gemfile": _gems,
}
_REQUIREMENTS_NAME = re.compile(r"(requirements|constraints|requires)[\w.-]*\.(txt|in)$")


def _manifest_parser(path: Path):
    name = path.name.lower()
    suffix = path.suffix.lower()
    if _REQUIREMENTS_NAME.search(name) or (
        suffix in (".txt", ".in") and path.parent.name.lower() == "requirements"
    ):
        return _requirements_txt
    if name in _MANIFESTS:
        return _MANIFESTS[name]
    if suffix in (".csproj", ".fsproj", ".vbproj"):
        return _nuget
    if suffix == ".gemspec":
        return _gems
    if name.startswith("environment") and suffix in (".yml", ".yaml"):
        return _conda_env
    return None


# --------------------------------------------------------------------------
# Per-file extraction
# --------------------------------------------------------------------------


def _is_text_source(path: Path) -> bool:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if name in LOCKFILES or GENERATED.search(name):
        return False
    return (
        suffix in CODE_SUFFIXES
        or suffix in CONFIG_SUFFIXES
        or name.startswith(CONFIG_PREFIXES)
        or name.endswith((".dockerfile", ".containerfile"))
    )


def _is_dockerfile(name: str) -> bool:
    """`name` lower-cased."""
    return name.startswith(("dockerfile", "containerfile")) or name.endswith(
        (".dockerfile", ".containerfile")
    )


def _takes_install_commands(path: Path) -> bool:
    name = path.name.lower()
    return (
        path.suffix.lower() in INSTALL_SUFFIXES
        or _is_dockerfile(name)
        or name.startswith(("makefile", "procfile", "jenkinsfile"))
    )


_KERNEL_SUFFIX = {
    "python": ".py", "kotlin": ".kt", "java": ".java", "scala": ".scala", "c#": ".cs",
    "csharp": ".cs", ".net (c#)": ".cs", "f#": ".fs", "javascript": ".js", "typescript": ".ts",
    "go": ".go", "rust": ".rs", "r": ".r", "julia": ".jl", "bash": ".sh", "sh": ".sh",
    "powershell": ".ps1", "ruby": ".rb", "sql": ".sql",
}  # fmt: skip


def _notebook_language(data: dict) -> str:
    """The suffix whose comment syntax the notebook's cells use; Python by default."""
    meta = data.get("metadata") or {}
    names = [
        (meta.get("kernelspec") or {}).get("language"),
        (meta.get("language_info") or {}).get("name"),
    ]
    for name in names:
        if isinstance(name, str) and name.strip().lower() in _KERNEL_SUFFIX:
            return _KERNEL_SUFFIX[name.strip().lower()]
    return ".py"


def _notebook(path: Path, text: str, needles: Needles) -> list[Fact]:
    """Code cells, cited at the line of the .ipynb file where each source line sits.

    Outputs and markdown are never read. Each cell's `"source"` key is located
    before its lines are, so text repeated in an earlier markdown cell or in this
    cell's outputs is never taken for the code.
    """
    data = _json(text)
    if not isinstance(data, dict):
        return []
    file_lines = _split_lines(text)
    language = _notebook_language(data)
    facts: list[Fact] = []
    cursor = 0
    for cell in data.get("cells") or []:
        source = cell.get("source") or []
        # A list of lines, or one string; Colab writes a one-element list whose single
        # string holds every line. Joining first handles all three shapes alike.
        joined = "".join(str(s) for s in source) if isinstance(source, list) else str(source)
        cell_lines = [ln.removesuffix("\r") for ln in joined.split("\n")]
        anchor = next(
            (i for i in range(cursor, len(file_lines)) if '"source"' in file_lines[i]), cursor
        )
        mapped: list[int] = []
        position = anchor
        for ln in cell_lines:
            probe = json.dumps(ln, ensure_ascii=False)[1:-1]
            found = next(
                (i for i in range(position, len(file_lines)) if probe and probe in file_lines[i]),
                None,
            )
            if found is not None:
                position = found
            mapped.append(position + 1)
        cursor = position + 1
        if cell.get("cell_type") != "code":
            continue

        magic = [ln.lstrip().startswith(("!", "%")) for ln in cell_lines]
        code = "\n".join("" if m else ln for ln, m in zip(cell_lines, magic, strict=True))
        if language == ".py":
            view = _split_lines(_python_view(code))
            imports = _python_imports(path, code, cell_lines)
        else:  # Kotlin, JavaScript, R, ... notebooks: their own comment syntax
            view = _split_lines(code_view(Path(f"cell{language}"), code))
            imports = []
        # Shell magics run as written; everything else is read through the
        # comment-blanked view, so `# !pip install x` installs nothing.
        install_view = [ln if m else v for ln, m, v in zip(cell_lines, magic, view, strict=True)]
        found_facts = (
            imports
            + _install_commands(path, cell_lines, install_view)
            + needles.search(path, cell_lines, view)
        )
        for f in found_facts:
            idx = f.line - 1
            inside = 0 <= idx < len(cell_lines)
            facts.append(dataclasses.replace(f, line=mapped[idx] if inside else 1))
    return facts


def _is_unrent_report(text: str) -> bool:
    head = text[:2000]
    return head.lstrip().startswith("{") and '"scanned_at"' in head and '"catalog_services"' in head


_API_SPEC = re.compile(r"""\A\s*(?:#[^\n]*\n\s*)*(?:\{\s*)?["']?(?:openapi|swagger)["']?\s*:""")


def facts_for_file(
    needles: Needles, path: Path, hit_lines: Iterable[int] | None = None
) -> list[Fact]:
    text = _read(path)
    if text is None:
        return []
    if "\r" in text and "\n" not in text:
        # Classic Mac line endings: one "line" to ripgrep, many to a reader. Split them
        # here and search the file in Python, so both modes agree.
        text, hit_lines = text.replace("\r", "\n"), None
    suffix = path.suffix.lower()
    # A manifest of an unexpected shape (`dependencies = 3`) is read like an unparseable
    # one: the parsers assume the shapes each format documents.
    shape_errors = (AttributeError, TypeError, KeyError, ValueError)
    if suffix == ".ipynb":
        try:
            return _notebook(path, text, needles)
        except shape_errors:
            return []
    if suffix == ".json" and _is_unrent_report(text):
        return []  # a saved report cites every vendor it found; it is not a dependency

    lines = _split_lines(text)
    facts: list[Fact] = []
    parser = _manifest_parser(path)
    if parser:
        with contextlib.suppress(shape_errors):
            facts += parser(path, text, lines)
    if not _is_text_source(path):
        return facts
    if suffix == ".json" and len(text) > MAX_JSON_BYTES:
        return facts
    if suffix in (".json", ".yaml", ".yml") and _API_SPEC.match(text):
        return facts  # a vendor's OpenAPI description documents the API; it doesn't call it

    view_text = code_view(path, text)
    code = _split_lines(view_text)
    if suffix in (".py", ".pyi"):
        facts += _python_imports(path, text, lines)
    elif suffix in JS_SUFFIXES:
        facts += _js_imports(path, view_text, lines)
    elif suffix in (".tf", ".hcl"):
        facts += _terraform(path, lines, code)
    if suffix in (".yaml", ".yml") or _is_dockerfile(path.name.lower()):
        facts += _images(path, lines, code)
    if _takes_install_commands(path):
        facts += _install_commands(path, lines, code)
    facts += _local_base_urls(path, lines, code)
    facts += needles.search(path, lines, code, hit_lines)
    return facts


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
) -> list[Fact]:
    """Read every relevant file once and extract every fact the catalog could care about.
    `progress(done, total)` is called as files are read, for a status line."""
    needles = Needles(catalog)
    files = iter_files(root, exclude, skipped)
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
            facts.append(dataclasses.replace(fact, in_test=in_test) if in_test else fact)
    own = _own_repo(root)
    if own:
        facts.append(Fact("own_repo", own, root, 0, ""))
    return facts


# From this many files on, worker processes read them; below it, starting the workers
# costs more than they save.
PARALLEL_FILES = 1500

_worker_state: object = None


def _start_worker(state: object) -> None:
    global _worker_state
    _worker_state = state


def _in_worker(work: Callable, item: tuple) -> object:
    return work(_worker_state, *item)


def map_files(work: Callable, items: list[tuple], state: object) -> Iterator:
    """`work(state, *item)` for each item, in order. A large scan spreads the items over
    one worker process per core, each given `state` once; where processes can't start
    (some sandboxes and serverless runtimes), the rest run here, with the same results.
    `work` must be a module-level function, so the workers can import it."""
    done = 0
    cores = os.cpu_count() or 1
    if len(items) >= PARALLEL_FILES and cores > 1:
        try:
            with ProcessPoolExecutor(
                max_workers=min(cores, 8), initializer=_start_worker, initargs=(state,)
            ) as pool:
                for result in pool.map(functools.partial(_in_worker, work), items, chunksize=32):
                    yield result
                    done += 1
        except (OSError, NotImplementedError, BrokenProcessPool):
            pass
    for item in items[done:]:
        yield work(state, *item)


_GITHUB_REMOTE = re.compile(r"github\.com[:/]+([^/\s]+/[^/\s]+?)(?:\.git)?/?$", re.IGNORECASE)


def _own_repo(root: Path) -> str | None:
    """`owner/repo` of the checkout being scanned, from its origin remote: a project
    is never reported as a component of itself (ragflow's Helm chart runs ragflow)."""
    urls = _git(root, "config", "--get", "remote.origin.url")
    match_ = _GITHUB_REMOTE.search(urls[0].strip()) if urls else None
    return match_.group(1).lower() if match_ else None


# Keep each ripgrep command line well under Windows' 32,767-character limit. Elsewhere
# the limit is a megabyte or more, and fewer, larger batches are 3x faster (each one
# builds its matcher for every needle again).
_RG_ARGS_BUDGET = 24_000 if os.name == "nt" else 200_000


def ripgrep_hits(root: Path, needles: Needles, files: list[Path]) -> dict[str, list[int]] | None:
    """Lines that contain any needle, per file, found by ripgrep.

    Optional and only an accelerator: ripgrep finds candidate lines (a superset —
    it matches case-insensitively), and the same Python checks as without it decide
    what counts. Without `rg` on PATH, or with UNRENT_NO_RIPGREP set, each file is
    searched in Python instead, with identical results.

    ripgrep is handed exactly the files the scan reads, in batches — never the whole
    tree, so a gitignored data directory costs nothing. Its output is streamed, and
    the line text is suppressed: only file names and line numbers come back.
    """
    rg = shutil.which("rg")
    if not rg or os.environ.get("UNRENT_NO_RIPGREP"):
        return None
    # Only the files whose text is searched for needles; notebooks are read as JSON.
    rels = [p.relative_to(root).as_posix() for p in files if _is_text_source(p)]
    probes = sorted({probe for _, _, probe, _ in needles.items})
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("\n".join(probes) + "\n")
    # --text: a NUL byte deep in a file must not end the search early, because the
    # Python reader only treats a file as binary on a NUL in its first 8 KB.
    base = [
        rg, "--no-config", "--fixed-strings", "--ignore-case", "--line-number",
        "--no-heading", "--with-filename", "--null", "--no-ignore", "--hidden", "--text",
        "--no-messages", "--max-columns", "1", "--file", f.name, "--",
    ]  # fmt: skip
    hits: dict[str, list[int]] = {}
    try:
        for batch in _batches(rels, _RG_ARGS_BUDGET - sum(len(a) + 3 for a in base)):
            if not _ripgrep_batch([*base, *batch], root, hits):
                return None
    finally:
        Path(f.name).unlink(missing_ok=True)
    return hits


def _batches(items: list[str], budget: int) -> Iterable[list[str]]:
    batch: list[str] = []
    size = 0
    for item in items:
        cost = len(item) + 3  # quotes and a space
        if batch and size + cost > budget:
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += cost
    if batch:
        yield batch


def _ripgrep_batch(cmd: list[str], root: Path, hits: dict[str, list[int]]) -> bool:
    try:
        proc = subprocess.Popen(cmd, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    assert proc.stdout is not None
    for record in proc.stdout:
        name, sep, rest = record.partition(b"\0")
        number = rest.split(b":", 1)[0]
        if not sep or not number.isdigit():
            continue
        rel = os.fsdecode(name).replace("\\", "/")
        rel = rel[2:] if rel.startswith("./") else rel
        hits.setdefault(rel, []).append(int(number))
    return proc.wait() in (0, 1)  # 1 means no matches


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

PREFIX_SEPARATORS = {"python_import": ".", "go": "/"}
CASE_SENSITIVE = {"symbol", "model", "env"}


def _canonical(kind: str, value: str) -> str:
    if kind == "requirement":
        return normalise(value)
    return value if kind in CASE_SENSITIVE else value.lower()


def _lookup_keys(kind: str, value: str) -> list[str]:
    """The signature values an observed value can match.

    Imports and Go modules match by prefix at a separator: `azure.search.documents.x`
    matches `azure.search.documents`, `github.com/a/b/v3` matches `github.com/a/b`.
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


def match(facts: list[Fact], catalog: Catalog) -> list[Finding]:
    index: dict[tuple[str, str], list[Service]] = {}
    for service in catalog.detectable:
        for kind, signatures in service.detect.items():
            for signature in signatures:
                index.setdefault((kind, _canonical(kind, signature)), []).append(service)

    hits_by_service: dict[str, dict[tuple, Fact]] = {}
    for fact in facts:
        for key in _lookup_keys(fact.kind, fact.value):
            for service in index.get((fact.kind, key), ()):
                hits_by_service.setdefault(service.id, {})[_key(fact)] = fact

    local = [f for f in facts if f.kind == "local_base_url"]
    local_modes = [f for f in facts if f.kind == "local_mode"]
    own = {f.value for f in facts if f.kind == "own_repo"}
    findings: list[Finding] = []
    for service in catalog.detectable:
        hits = hits_by_service.get(service.id)
        if not hits or (service.repo and service.repo.lower() in own):
            continue
        kept = tuple(sorted(hits.values(), key=lambda f: (str(f.file), f.line, f.kind, f.value)))
        if service.local_compatible:
            kept = _without_local_clients(kept, local)
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


def _mark_models_only(findings: list[Finding]) -> list[Finding]:
    """Flag findings whose only evidence is model names. A capability (OpenAI
    Embeddings) is exempt when the service it is part of has real evidence:
    `text-embedding-3-small` next to `from openai import OpenAI` is a real call."""
    real = {
        f.service.id
        for f in findings
        if any(fact.kind != "model" for fact in f.facts) and not f.template_only
    }
    out = []
    for finding in findings:
        only_models = all(fact.kind == "model" for fact in finding.facts)
        # A model id in a test is a fixture, not a call, even when the vendor is real.
        backed = any(base in real for base in finding.service.part_of) and not finding.test_only
        if only_models and not backed:
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

    A local base URL in a code file covers that file. One in configuration (.env,
    compose, YAML) covers the project's client code, but not model ids or the
    vendor's API host, which still name the vendor. If only the package declaration
    is left, the package is explained by the local use and nothing remains.
    """
    if not local:
        return facts
    files = {f.file for f in local if f.file.suffix.lower() in CODE_SUFFIXES}
    project_wide = any(f.file.suffix.lower() not in CODE_SUFFIXES for f in local)
    kept = tuple(
        f
        for f in facts
        if f.file not in files
        and not (project_wide and not f.manifest and f.kind not in ("model", "endpoint"))
    )
    if len(kept) < len(facts) and all(f.manifest for f in kept):
        return ()
    return kept


def _resolve_overlaps(findings: list[Finding]) -> list[Finding]:
    """A specific service claims the general service's evidence where it is used.

    Azure OpenAI is called through the `openai` package; Claude on Bedrock through
    `anthropic`; Vertex through `google-genai`. In every file where the specific
    service has evidence of its own, the general service's evidence there (the
    import, the model ids) belongs to the specific one, except the general
    vendor's own API host. If all the general service has left is the package
    declaration, the specific service explains that too, and the general one is
    not reported: naming a vendor that is not there is the one failure this tool
    cannot afford.
    """
    by_id = {f.service.id: f for f in findings}
    claimed: dict[str, set[tuple]] = {}
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
            claimed.setdefault(general_id, set()).update(
                _key(f) for f in general.facts if f.file in files and f.kind != "endpoint"
            )

    surviving: list[Finding] = []
    for finding in findings:
        taken = claimed.get(finding.service.id)
        if not taken:
            surviving.append(finding)
            continue
        rest = tuple(f for f in finding.facts if _key(f) not in taken)
        if rest and not all(f.manifest for f in rest):
            surviving.append(Finding(service=finding.service, facts=rest))
    return surviving
