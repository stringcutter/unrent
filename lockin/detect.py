"""Find facts in a codebase, then match them to catalog services.

Deterministic and offline. Every finding carries the file, the line and the exact
text it was found in; a finding that cannot cite its evidence does not exist.

Facts come from three kinds of reading:

  manifests   parsed, not grepped: requirements, pyproject, setup.py/cfg, Pipfile,
              conda environments, package.json, go.mod, Cargo.toml, Maven, Gradle,
              NuGet, Gemfile, composer.json.
  imports     Python (AST, notebooks included) and JavaScript/TypeScript import
              and require specifiers, plus `pip install` / `npm install` commands
              in Dockerfiles, scripts, CI config and notebook cells.
  text        catalog needles — symbols, model ids, API hosts, environment
              variables — searched in source and config files of any language,
              with comments and docstrings skipped. ripgrep, when installed, finds
              the candidate lines; the same Python checks decide what counts.

Lockfiles are deliberately not read: they list what your dependencies depend on,
and a transitive `openai` pulled in by a gateway library is not a dependency on
OpenAI.
"""

from __future__ import annotations

import ast
import bisect
import configparser
import fnmatch
import json
import os
import re
import shutil
import subprocess
import tempfile
import tokenize
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

import yaml

from .catalog import Catalog, Service

# Never ours to scan, even when committed.
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "bower_components",
    "jspm_packages",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".turbo",
    ".vercel",
    ".terraform",
    ".gradle",
    ".idea",
    ".vscode",
    "site-packages",
    "vendor",
    "Pods",
}
# Only skipped outside git, where there is no .gitignore to say what is build output.
UNTRACKED_SKIP_DIRS = {"dist", "build", "target", "out", "coverage", ".venv", "venv"}
MAX_FILE_BYTES = 2_000_000
MAX_LINE_CHARS = 4_000  # longer lines are minified or generated
# A JSON or YAML file this large is data (a fixture, an index dump), not configuration.
MAX_CONFIG_BYTES = 256_000

CODE_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".vue",
    ".svelte", ".astro", ".go", ".rs", ".java", ".kt", ".kts", ".scala", ".groovy", ".cs",
    ".fs", ".vb", ".rb", ".php", ".swift", ".dart", ".ex", ".exs", ".r", ".jl", ".lua",
    ".sh", ".bash", ".zsh", ".ps1", ".tf", ".tfvars", ".hcl",
}  # fmt: skip
CONFIG_SUFFIXES = {
    ".json", ".jsonc", ".json5", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".properties", ".xml", ".gradle", ".env", ".csproj", ".fsproj", ".vbproj", ".props",
}  # fmt: skip
CONFIG_NAMES = {
    "dockerfile",
    "makefile",
    "procfile",
    "jenkinsfile",
    ".envrc",
    ".dev.vars",
    "gemfile",
}
JS_SUFFIXES = {
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".vue",
    ".svelte",
    ".astro",
}
LOCKFILES = {
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock",
    "poetry.lock", "uv.lock", "pdm.lock", "pipfile.lock", "cargo.lock", "go.sum",
    "gemfile.lock", "composer.lock", "packages.lock.json", "gradle.lockfile",
}  # fmt: skip
GENERATED = re.compile(r"\.(min|bundle|chunk)\.(js|css)$|\.map$", re.IGNORECASE)


@dataclass(frozen=True)
class Fact:
    """One observation about the codebase, before any interpretation."""

    kind: str  # a catalog detect kind
    value: str  # the normalised thing observed, e.g. "openai"
    file: Path
    line: int
    evidence: str  # the source line, trimmed


@dataclass(frozen=True)
class Finding:
    service: Service
    facts: tuple[Fact, ...]

    @property
    def cited(self) -> list[Fact]:
        """One fact per source location, for the report."""
        seen: set[tuple[str, int]] = set()
        out: list[Fact] = []
        for fact in self.facts:
            key = (str(fact.file), fact.line)
            if key not in seen:
                seen.add(key)
                out.append(fact)
        return out


# --------------------------------------------------------------------------
# File discovery
# --------------------------------------------------------------------------


def _git_files(root: Path) -> list[Path] | None:
    """Tracked plus untracked-but-not-ignored files, when root is inside a git repo."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [root / p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]


def _walk(root: Path) -> list[Path]:
    out: list[Path] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            children = list(directory.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_symlink():
                continue  # loops, and files that live elsewhere
            if child.is_dir():
                if child.name in SKIP_DIRS or child.name in UNTRACKED_SKIP_DIRS:
                    continue
                if (child / "pyvenv.cfg").is_file():
                    continue  # a virtualenv, whatever it is called
                stack.append(child)
            elif child.is_file():
                out.append(child)
    return out


def load_ignore(root: Path) -> list[str]:
    """Patterns from .lockinignore: one glob per line, relative to root."""
    file = root / ".lockinignore"
    if not file.is_file():
        return []
    lines = file.read_text("utf-8", errors="replace").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def _ignored(rel: str, patterns: Iterable[str]) -> bool:
    for pattern in patterns:
        p = pattern.strip("/")
        if pattern.endswith("/"):
            if rel == p or rel.startswith(p + "/") or f"/{p}/" in f"/{rel}":
                return True
        elif fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel.rsplit("/", 1)[-1], p):
            return True
    return False


def iter_files(root: Path, exclude: Iterable[str] = ()) -> list[Path]:
    patterns = [*load_ignore(root), *exclude]
    files = _git_files(root)
    if files is None:
        files = _walk(root)
    out = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        if any(part in SKIP_DIRS for part in rel.split("/")[:-1]):
            continue
        if patterns and _ignored(rel, patterns):
            continue
        try:
            if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        out.append(path)
    return sorted(out)


def _split_lines(text: str) -> list[str]:
    """Lines as ripgrep and editors number them: split on newlines only.

    `str.splitlines` also splits on form feeds, U+2028 and friends, which puts
    every later line number out by one.
    """
    return [ln.removesuffix("\r") for ln in text.split("\n")]


def _read(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None  # binary
    return raw.decode("utf-8-sig", errors="replace")


# --------------------------------------------------------------------------
# Needles: catalog strings searched as text
# --------------------------------------------------------------------------

_NORMALISE = re.compile(r"[-_.]+")


def normalise(name: str) -> str:
    """PEP 503: `Pinecone_Client` and `pinecone-client` are one package."""
    return _NORMALISE.sub("-", name).lower()


def _needle_pattern(kind: str, needle: str) -> re.Pattern:
    """Boundary rules per kind.

    symbol    word-bounded and case-sensitive: `OpenAI(` must not match inside
              `AzureOpenAI(`, and `searchclient` in a URL is not `SearchClient`.
    model     a prefix of a model id: `gpt-4` matches `gpt-4o-mini`, but not
              `chatgpt-4`, and not `bedrock/` inside a path like `./bedrock/x`.
    endpoint  a host, matched case-insensitively; a subdomain may precede it.
    env       an exact variable name.
    """
    escaped = re.escape(needle)
    ends_word = needle[-1:].isalnum() or needle[-1:] == "_"
    if kind == "symbol":
        head = r"(?<![A-Za-z0-9_$])" if needle[:1].isalnum() or needle[:1] in "_$" else ""
        return re.compile(head + escaped + (r"(?![A-Za-z0-9_])" if ends_word else ""))
    if kind == "model":
        return re.compile(r"(?<![A-Za-z0-9_./-])" + escaped)
    if kind == "endpoint":
        tail = r"(?![A-Za-z0-9-])" if ends_word else ""
        return re.compile(r"(?<![A-Za-z0-9-])" + escaped + tail, re.IGNORECASE)
    return re.compile(r"(?<![A-Za-z0-9_])" + escaped + r"(?![A-Za-z0-9_])")


_NEWLINE = re.compile(r"\n")
_MODEL_TOKEN = re.compile(r"[A-Za-z0-9_./:@-]+")

Needle = tuple[str, str, str, re.Pattern]  # kind, needle, probe, pattern


class Needles:
    TEXT_KINDS = ("symbol", "model", "endpoint", "env")

    def __init__(self, catalog: Catalog):
        self.items: list[Needle] = []
        # Per model needle: open-weight ids it must not report.
        self.open_models: dict[str, set[str]] = {}
        seen = set()
        for service in catalog.services:
            for needle in service.detect.get("model", ()):
                self.open_models.setdefault(needle, set()).update(service.open_models)
            for kind in self.TEXT_KINDS:
                for needle in service.detect.get(kind, ()):
                    if (kind, needle) in seen:
                        continue
                    seen.add((kind, needle))
                    probe = needle.lower() if kind == "endpoint" else needle
                    self.items.append((kind, needle, probe, _needle_pattern(kind, needle)))

    def _hits(self, text: str) -> dict[int, list[Needle]]:
        """Line number → needles whose literal text occurs on it.

        `str.find` runs in C and there are only a few hundred needles, so this is
        one fast scan per needle rather than a regex per line per needle.
        """
        lowered = text.lower()
        offsets: list[int] | None = None
        hits: dict[int, list[Needle]] = {}
        for item in self.items:
            haystack = lowered if item[0] == "endpoint" else text
            at = haystack.find(item[2])
            if at < 0:
                continue
            if offsets is None:
                offsets = [0, *(m.end() for m in _NEWLINE.finditer(text))]
            while at >= 0:
                n = bisect.bisect_right(offsets, at)
                hits.setdefault(n, []).append(item)
                at = haystack.find(item[2], offsets[n] if n < len(offsets) else len(text))
        return hits

    def _items_on(self, line: str) -> list[Needle]:
        lowered = line.lower()
        return [it for it in self.items if it[2] in (lowered if it[0] == "endpoint" else line)]

    def _is_closed_model(self, needle: str, pattern: re.Pattern, line: str) -> bool:
        """True when some occurrence of the prefix names a model that is not open-weight."""
        exempt = self.open_models.get(needle)
        for m in pattern.finditer(line):
            token = _MODEL_TOKEN.match(line, m.start())
            model_id = token.group(0).lower() if token else needle
            if not exempt or not any(e in model_id for e in exempt):
                return True
        return False

    def search(
        self, path: Path, lines: list[str], skip: set[int], hit_lines: Iterable[int] | None = None
    ) -> list[Fact]:
        """Needles on `lines`. `hit_lines`, when ripgrep has already found them, saves
        searching the file again."""
        if hit_lines is None:
            hits = self._hits("\n".join(lines))
        else:
            hits = {n: self._items_on(lines[n - 1]) for n in hit_lines if 0 < n <= len(lines)}
        facts = []
        for n, items in sorted(hits.items()):
            raw = lines[n - 1]
            if n in skip or len(raw) > MAX_LINE_CHARS or _is_comment(raw):
                continue
            for kind, needle, _, pattern in items:
                if kind == "model":
                    hit = self._is_closed_model(needle, pattern, raw)
                else:
                    hit = pattern.search(raw) is not None
                if hit:
                    facts.append(Fact(kind, needle, path, n, raw.strip()[:200]))
        return facts


_COMMENT_PREFIXES = ("#", "//", "/*", "*", "<!--", "--", ";", "'''", '"""', "rem ", "REM ")


def _is_comment(line: str) -> bool:
    stripped = line.lstrip()
    if stripped.startswith("#!") or stripped.startswith("#include"):
        return False
    # `#` opens a comment in most of what we scan, but not `#[derive]` or C# regions.
    return stripped.startswith(_COMMENT_PREFIXES) and not stripped.startswith("#[")


def _python_prose(text: str) -> set[int]:
    """Comment and docstring lines: someone writing about a vendor, not using one.

    Ordinary string literals stay in scope, because `client("bedrock-runtime")` is a
    string and is exactly the evidence we are looking for.
    """
    lines: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                lines.add(tok.start[0])
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        pass
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return lines
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        doc = node.body[0] if node.body else None
        if (
            isinstance(doc, ast.Expr)
            and isinstance(doc.value, ast.Constant)
            and isinstance(doc.value.value, str)
        ):
            lines.update(range(doc.lineno, (doc.end_lineno or doc.lineno) + 1))
    return lines


def _js_block_comments(text: str) -> set[int]:
    lines: set[int] = set()
    for m in re.finditer(r"/\*.*?\*/", text, re.DOTALL):
        start = text.count("\n", 0, m.start()) + 1
        end = text.count("\n", 0, m.end()) + 1
        lines.update(range(start, end + 1))
    return lines


# --------------------------------------------------------------------------
# Imports and install commands
# --------------------------------------------------------------------------

_PY_IMPORT_FALLBACK = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import\b|import\s+([\w.,\s]+))")


def _python_imports(path: Path, text: str, lines: list[str]) -> list[Fact]:
    """`import a.b`, `from a.b import c`. Falls back to a regex when the file does not
    parse (Python 2, templates), because an unparseable file still imports things."""

    def line_text(n: int) -> str:
        return lines[n - 1].strip() if 0 < n <= len(lines) else ""

    facts: list[Fact] = []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    facts.append(
                        Fact("python_import", alias.name, path, node.lineno, line_text(node.lineno))
                    )
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                facts.append(
                    Fact("python_import", node.module, path, node.lineno, line_text(node.lineno))
                )
        return facts
    for i, raw in enumerate(_split_lines(text), start=1):
        m = _PY_IMPORT_FALLBACK.match(raw)
        if not m:
            continue
        if m.group(1):
            names = [m.group(1)]
        else:
            names = [p.split()[0] for p in m.group(2).split(",") if p.strip()]
        for name in names:
            facts.append(Fact("python_import", name, path, i, line_text(i)))
    return facts


_JS_IMPORT = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*|\bexport\s+\*\s+from\s*)"""
    r"""['"]([^'"\n]+)['"]"""
)


def js_package(specifier: str) -> str | None:
    """`@scope/pkg/sub` → `@scope/pkg`; `npm:openai@4` → `openai`; relative → None."""
    spec = specifier.strip()
    if spec.startswith("npm:"):
        spec = spec[4:]
    if not spec or spec.startswith((".", "/", "~", "#", "node:", "http:", "https:", "jsr:", "$")):
        return None
    parts = spec.split("/")
    if spec.startswith("@"):
        if len(parts) < 2:
            return None
        name = f"{parts[0]}/{parts[1].split('@', 1)[0]}"
    else:
        name = parts[0].split("@", 1)[0]
    return name or None


def _js_imports(path: Path, text: str, lines: list[str], skip: set[int]) -> list[Fact]:
    facts = []
    for m in _JS_IMPORT.finditer(text):
        name = js_package(m.group(1))
        if not name:
            continue
        n = text.count("\n", 0, m.start()) + 1
        if n in skip or _is_comment(lines[n - 1]):
            continue
        facts.append(Fact("npm", name, path, n, lines[n - 1].strip()[:200]))
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


def _install_commands(path: Path, lines: list[str], skip: set[int]) -> list[Fact]:
    facts = []
    for n, raw in enumerate(lines, start=1):
        if ("install" not in raw and "add" not in raw) or n in skip or len(raw) > MAX_LINE_CHARS:
            continue
        line = raw.lstrip().lstrip("!%").lstrip()  # notebook magics
        if _is_comment(raw) and not raw.lstrip().startswith(("!", "%")):
            continue
        for pattern, kind in ((_PIP_INSTALL, "requirement"), (_NPM_INSTALL, "npm")):
            for m in pattern.finditer(line):
                tokens = m.group(1).replace("\\", " ").split()
                skip_next = False
                for token in tokens:
                    token = token.strip("'\"`")
                    if skip_next:
                        skip_next = False
                        continue
                    if token in _TAKES_VALUE:
                        skip_next = True
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
                        facts.append(Fact(kind, name, path, n, raw.strip()[:200]))
    return facts


# --------------------------------------------------------------------------
# Manifests
# --------------------------------------------------------------------------

_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_EGG = re.compile(r"#egg=([A-Za-z0-9._-]+)")


def _line_of(lines: list[str], name: str) -> int:
    """First line naming `name` as a whole token, for citation."""
    pattern = re.compile(
        r"(?<![A-Za-z0-9_.-])" + re.escape(name) + r"(?![A-Za-z0-9_-])", re.IGNORECASE
    )
    for i, ln in enumerate(lines, start=1):
        if pattern.search(ln) and not _is_comment(ln):
            return i
    return 1


def _cite(kind: str, value: str, path: Path, lines: list[str], name: str | None = None) -> Fact:
    n = _line_of(lines, name or value)
    return Fact(kind, value, path, n, lines[n - 1].strip()[:200] if lines else "")


def _cite_literal(kind: str, value: str, path: Path, lines: list[str], literal: str) -> Fact:
    """Cite the first line containing `literal` verbatim — for JSON keys, where the
    quotes make the match exact and `"openai"` cannot hit `"@ai-sdk/openai"`."""
    n = next((i for i, ln in enumerate(lines, 1) if literal in ln), 1)
    return Fact(kind, value, path, n, lines[n - 1].strip()[:200] if lines else "")


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


def _requirements_txt(path: Path, text: str, lines: list[str]) -> list[Fact]:
    facts = []
    for n, raw in enumerate(lines, start=1):
        name = _requirement_spec(raw.split(" #", 1)[0])
        if name:
            facts.append(Fact("requirement", name, path, n, raw.strip()))
    return facts


def _specs_to_facts(specs: Iterable[str], path: Path, lines: list[str]) -> list[Fact]:
    facts = []
    for spec in specs:
        if not isinstance(spec, str):
            continue
        name = _requirement_spec(spec)
        if name:
            raw = _REQ_NAME.match(spec.strip())
            facts.append(_cite("requirement", name, path, lines, raw.group(1) if raw else name))
    return facts


def _pyproject(path: Path, text: str, lines: list[str]) -> list[Fact]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
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
    poetry = tool.get("poetry") or {}
    names = list(poetry.get("dependencies") or {}) + list(poetry.get("dev-dependencies") or {})
    for group in (poetry.get("group") or {}).values():
        names += list((group or {}).get("dependencies") or {})
    specs += [n for n in names if n.lower() != "python"]
    return _specs_to_facts(specs, path, lines)


def _setup_py(path: Path, text: str, lines: list[str]) -> list[Fact]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    specs: list[str] = []
    wanted = {"install_requires", "extras_require", "setup_requires", "tests_require"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in wanted:
                    specs += [
                        c.value
                        for c in ast.walk(kw.value)
                        if isinstance(c, ast.Constant) and isinstance(c.value, str)
                    ]
    return _specs_to_facts(specs, path, lines)


def _setup_cfg(path: Path, text: str, lines: list[str]) -> list[Fact]:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        return []
    specs: list[str] = []
    for section, key in (("options", "install_requires"), ("options", "setup_requires")):
        if parser.has_option(section, key):
            specs += parser.get(section, key).splitlines()
    if parser.has_section("options.extras_require"):
        for _, value in parser.items("options.extras_require"):
            specs += value.splitlines()
    return _specs_to_facts(specs, path, lines)


def _pipfile(path: Path, text: str, lines: list[str]) -> list[Fact]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    names = list(data.get("packages") or {}) + list(data.get("dev-packages") or {})
    return _specs_to_facts(names, path, lines)


def _conda_env(path: Path, text: str, lines: list[str]) -> list[Fact]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
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
    except ValueError:
        return None


def _package_json(path: Path, text: str, lines: list[str]) -> list[Fact]:
    data = _json(text)
    if not isinstance(data, dict):
        return []
    facts = []
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        block = data.get(section)
        for name in block if isinstance(block, dict) else ():
            facts.append(_cite_literal("npm", name, path, lines, f'"{name}"'))
    return facts


def _composer_json(path: Path, text: str, lines: list[str]) -> list[Fact]:
    data = _json(text)
    if not isinstance(data, dict):
        return []
    names = [n for s in ("require", "require-dev") for n in (data.get(s) or {})]
    return [_cite_literal("composer", n.lower(), path, lines, f'"{n}"') for n in names if "/" in n]


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
            facts.append(Fact("go", m.group(1), path, n, raw.strip()))
    return facts


def _cargo_toml(path: Path, text: str, lines: list[str]) -> list[Fact]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    tables = [data.get(k) for k in ("dependencies", "dev-dependencies", "build-dependencies")]
    tables.append((data.get("workspace") or {}).get("dependencies"))
    for target in (data.get("target") or {}).values():
        tables += [target.get(k) for k in ("dependencies", "dev-dependencies")]
    facts = []
    for table in tables:
        for key, spec in (table or {}).items():
            name = spec.get("package", key) if isinstance(spec, dict) else key
            facts.append(_cite("cargo", name.lower(), path, lines, key))
    return facts


_MAVEN_DEP = re.compile(
    r"<dependency>.*?<groupId>\s*([^<\s]+)\s*</groupId>.*?<artifactId>\s*([^<\s]+)\s*</artifactId>",
    re.DOTALL,
)


def _pom_xml(path: Path, text: str, lines: list[str]) -> list[Fact]:
    facts = []
    for m in _MAVEN_DEP.finditer(text):
        n = text.count("\n", 0, m.start(2)) + 1
        coordinate = f"{m.group(1)}:{m.group(2)}".lower()
        facts.append(Fact("maven", coordinate, path, n, lines[n - 1].strip()))
    return facts


_GRADLE_COORD = re.compile(r"""['"]([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+)(?::[^'"\s]*)?['"]""")
_GRADLE_MAP = re.compile(
    r"""group\s*[:=]\s*['"]([^'"]+)['"]\s*,\s*(?:name|module)\s*[:=]\s*['"]([^'"]+)['"]"""
)


def _gradle(path: Path, text: str, lines: list[str]) -> list[Fact]:
    facts = []
    for n, raw in enumerate(lines, start=1):
        if _is_comment(raw):
            continue
        for m in [*_GRADLE_COORD.finditer(raw), *_GRADLE_MAP.finditer(raw)]:
            if "." not in m.group(1):
                continue  # `id "x:y"` plugin forms and other colon strings
            coordinate = f"{m.group(1)}:{m.group(2)}".lower()
            facts.append(Fact("maven", coordinate, path, n, raw.strip()))
    return facts


_NUGET = re.compile(
    r"""<(?:PackageReference|PackageVersion|package)\b[^>]*?\b(?:Include|Update|id)\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)


def _nuget(path: Path, text: str, lines: list[str]) -> list[Fact]:
    facts = []
    for m in _NUGET.finditer(text):
        n = text.count("\n", 0, m.start()) + 1
        facts.append(Fact("nuget", m.group(1).lower(), path, n, lines[n - 1].strip()))
    return facts


_GEM = re.compile(
    r"""^\s*(?:gem|\w+\.add_(?:runtime_|development_)?dependency)\s*\(?\s*['"]([^'"]+)['"]"""
)


def _gems(path: Path, text: str, lines: list[str]) -> list[Fact]:
    facts = []
    for n, raw in enumerate(lines, start=1):
        m = _GEM.match(raw)
        if m:
            facts.append(Fact("gem", m.group(1).lower(), path, n, raw.strip()))
    return facts


_TF_BLOCK = re.compile(r'^\s*(?:resource|data)\s+"([a-z0-9_]+)"')


def _terraform(path: Path, text: str, lines: list[str]) -> list[Fact]:
    facts = []
    for n, raw in enumerate(lines, start=1):
        m = _TF_BLOCK.match(raw)
        if m:
            facts.append(Fact("terraform_resource", m.group(1), path, n, raw.strip()))
    return facts


_MANIFESTS = {
    "pyproject.toml": _pyproject,
    "setup.py": _setup_py,
    "setup.cfg": _setup_cfg,
    "pipfile": _pipfile,
    "package.json": _package_json,
    "composer.json": _composer_json,
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


def _manifest_parser(path: Path):
    name = path.name.lower()
    suffix = path.suffix.lower()
    if suffix in (".txt", ".in") and (
        name.startswith(("requirements", "constraints", "requires"))
        or path.parent.name.lower() == "requirements"
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
        or name in CONFIG_NAMES
        or name.startswith((".env", "dockerfile", "docker-compose", "compose."))
        or name.endswith(".dockerfile")
    )


def _notebook(path: Path, text: str, needles: Needles) -> list[Fact]:
    """Code cells, cited at the line of the .ipynb file where the source sits."""
    data = _json(text)
    if not isinstance(data, dict):
        return []
    file_lines = _split_lines(text)
    facts: list[Fact] = []
    cursor = 0
    for cell in data.get("cells") or []:
        if cell.get("cell_type") != "code":
            continue
        source = cell.get("source") or []
        cell_lines = source if isinstance(source, list) else source.splitlines(True)
        cell_lines = [ln.rstrip("\n") for ln in cell_lines]
        # Map each cell line to its line in the file (pretty-printed notebooks keep
        # one source line per JSON line).
        mapped: list[int] = []
        for ln in cell_lines:
            probe = json.dumps(ln, ensure_ascii=False)[1:-1]
            found = next(
                (i for i in range(cursor, len(file_lines)) if probe and probe in file_lines[i]),
                None,
            )
            if found is not None:
                cursor = found
            mapped.append((found if found is not None else cursor) + 1)

        def relocate(fs: list[Fact], mapped=mapped, cell_lines=cell_lines) -> list[Fact]:
            out = []
            for f in fs:
                idx = f.line - 1
                inside = 0 <= idx < len(cell_lines)
                out.append(
                    Fact(
                        f.kind,
                        f.value,
                        path,
                        mapped[idx] if inside else 1,
                        cell_lines[idx].strip()[:200] if inside else f.evidence,
                    )
                )
            return out

        code = "\n".join("" if ln.lstrip().startswith(("!", "%")) else ln for ln in cell_lines)
        facts += relocate(_python_imports(path, code, cell_lines))
        facts += relocate(_install_commands(path, cell_lines, set()))
        facts += relocate(needles.search(path, cell_lines, _python_prose(code)))
    return facts


def facts_for_file(
    path: Path, needles: Needles, hit_lines: Iterable[int] | None = None
) -> list[Fact]:
    text = _read(path)
    if text is None:
        return []
    suffix = path.suffix.lower()
    if suffix == ".ipynb":
        return _notebook(path, text, needles)

    lines = _split_lines(text)
    facts: list[Fact] = []
    parser = _manifest_parser(path)
    if parser:
        facts += parser(path, text, lines)
    if not _is_text_source(path):
        return facts
    if suffix in CONFIG_SUFFIXES and len(text) > MAX_CONFIG_BYTES:
        return facts

    skip: set[int] = set()
    if suffix in (".py", ".pyi"):
        skip = _python_prose(text)
        facts += _python_imports(path, text, lines)
    elif suffix in JS_SUFFIXES:
        skip = _js_block_comments(text)
        facts += _js_imports(path, text, lines, skip)
    elif suffix in (".tf", ".hcl"):
        facts += _terraform(path, text, lines)
    if path.name.lower() != "package.json":
        facts += _install_commands(path, lines, skip)
    facts += needles.search(path, lines, skip, hit_lines)
    return facts


def collect_facts(root: Path, catalog: Catalog, exclude: Iterable[str] = ()) -> list[Fact]:
    """Read every relevant file once and extract every fact the catalog could care about."""
    needles = Needles(catalog)
    files = iter_files(root, exclude)
    found = ripgrep_hits(root, needles)
    facts: list[Fact] = []
    for path in files:
        hit_lines = None if found is None else found.get(path.relative_to(root).as_posix(), ())
        facts += facts_for_file(path, needles, hit_lines)
    return facts


def ripgrep_hits(root: Path, needles: Needles) -> dict[str, list[int]] | None:
    """Lines that contain any needle, per file, found by ripgrep in one pass.

    Optional and only an accelerator: ripgrep finds candidate lines (a superset —
    it matches case-insensitively), and the same Python checks as without it decide
    what counts. Without `rg` on PATH, or with LOCKIN_NO_RIPGREP set, each file is
    searched in Python instead, with identical results. Searching every needle in
    one SIMD pass is what makes a 5,000-file monorepo take seconds rather than
    tens of seconds.
    """
    rg = shutil.which("rg")
    if not rg or os.environ.get("LOCKIN_NO_RIPGREP"):
        return None
    probes = sorted({probe for _, _, probe, _ in needles.items})
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("\n".join(probes) + "\n")
    # --text: a NUL byte deep in a file must not end the search early, because the
    # Python reader only treats a file as binary on a NUL in its first 8 KB.
    cmd = [
        rg, "--no-config", "--fixed-strings", "--ignore-case", "--line-number",
        "--no-heading", "--with-filename", "--null", "--hidden", "--no-ignore", "--text",
        "--no-messages", "--max-filesize", str(MAX_FILE_BYTES), "--file", f.name,
    ]  # fmt: skip
    for name in sorted(SKIP_DIRS):
        cmd += ["--glob", f"!{name}"]
    cmd.append(".")
    try:
        out = subprocess.run(cmd, cwd=root, capture_output=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        Path(f.name).unlink(missing_ok=True)
    if out.returncode not in (0, 1):  # 1 means no matches
        return None
    hits: dict[str, list[int]] = {}
    for record in out.stdout.split(b"\n"):
        name, sep, rest = record.partition(b"\0")
        number = rest.split(b":", 1)[0]
        if not sep or not number.isdigit():
            continue
        rel = name.decode("utf-8", "replace").replace("\\", "/")
        rel = rel[2:] if rel.startswith("./") else rel
        hits.setdefault(rel, []).append(int(number))
    return hits


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
    Everything else matches exactly.
    """
    value = _canonical(kind, value)
    sep = PREFIX_SEPARATORS.get(kind)
    if not sep:
        return [value]
    parts = value.split(sep)
    return [sep.join(parts[:i]) for i in range(1, len(parts) + 1)]


def match(facts: list[Fact], catalog: Catalog) -> list[Finding]:
    index: dict[tuple[str, str], list[Service]] = {}
    for service in catalog.services:
        for kind, signatures in service.detect.items():
            for signature in signatures:
                index.setdefault((kind, _canonical(kind, signature)), []).append(service)

    hits_by_service: dict[str, dict[tuple, Fact]] = {}
    for fact in facts:
        for key in _lookup_keys(fact.kind, fact.value):
            for service in index.get((fact.kind, key), ()):
                hits_by_service.setdefault(service.id, {})[_key(fact)] = fact

    findings: list[Finding] = []
    for service in catalog.services:
        hits = hits_by_service.get(service.id)
        if hits:
            ordered = sorted(hits.values(), key=lambda f: (str(f.file), f.line, f.kind, f.value))
            findings.append(Finding(service=service, facts=tuple(ordered)))

    findings = _resolve_overlaps(findings)
    findings.sort(key=lambda f: (f.service.category, f.service.name))
    return findings


def _key(fact: Fact) -> tuple[str, int, str, str]:
    return (str(fact.file), fact.line, fact.kind, fact.value)


def _resolve_overlaps(findings: list[Finding]) -> list[Finding]:
    """A specific service claims the evidence it shares with a general one it excludes.

    `AzureOpenAI` lives inside the `openai` package, so a plain `import openai` is
    evidence for both. The specific service takes the shared lines only when it has
    evidence of its own; without that, the general service is the simpler
    explanation, and naming a vendor that is not there is the one failure this tool
    cannot afford.
    """
    by_id = {f.service.id: f for f in findings}
    dropped: set[str] = set()
    claimed: dict[str, set[tuple]] = {}
    for finding in findings:
        generals = [by_id[g] for g in finding.service.excludes if g in by_id]
        if not generals:
            continue
        general_keys = {_key(f) for g in generals for f in g.facts}
        mine = {_key(f) for f in finding.facts}
        if mine <= general_keys:
            dropped.add(finding.service.id)
            continue
        for general in generals:
            claimed.setdefault(general.service.id, set()).update(mine)

    surviving: list[Finding] = []
    for finding in findings:
        if finding.service.id in dropped:
            continue
        taken = claimed.get(finding.service.id, set())
        own = tuple(f for f in finding.facts if _key(f) not in taken)
        if own:
            surviving.append(Finding(service=finding.service, facts=own))
    return surviving
