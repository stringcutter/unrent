"""Layer 1 + 2: find facts in a codebase, then match them to catalog entries.

Nothing here calls a model. Everything is deterministic and reproducible, and
every finding carries the file, the line and the exact text it was found in.
A finding that cannot cite its evidence does not exist.
"""

from __future__ import annotations

import ast
import json
import re
import tokenize
import tomllib
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

from .catalog import Catalog, Entry

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".next",
    ".terraform",
    "site-packages",
    ".tox",
    ".idea",
    ".vscode",
    "target",
}
MAX_FILE_BYTES = 2_000_000


@dataclass(frozen=True)
class Fact:
    """One observation about the codebase, before any interpretation."""

    kind: str  # python_import | python_symbol | requirement | npm | env | terraform_resource
    value: str  # the normalised thing observed, e.g. "openai" or "aws_dynamodb_table"
    file: Path
    line: int
    evidence: str  # the source line, verbatim and trimmed


@dataclass(frozen=True)
class Finding:
    entry: Entry
    facts: tuple[Fact, ...]

    @property
    def confidence(self) -> str:
        """More independent signature kinds means less chance of a false positive."""
        kinds = {f.kind for f in self.facts}
        strong = {"python_import", "requirement", "npm", "terraform_resource"}
        if len(kinds) >= 2:
            return "high"
        if kinds & strong:
            return "medium"
        # A single kind, but several distinct signatures of it, is still corroboration.
        if len({f.value for f in self.facts}) >= 2:
            return "medium"
        return "low"  # e.g. a lone env var name

    @property
    def cited(self) -> list[Fact]:
        """One line of evidence per source location, for the report."""
        seen: set[tuple[str, int]] = set()
        out: list[Fact] = []
        for fact in self.facts:
            key = (str(fact.file), fact.line)
            if key in seen:
                continue
            seen.add(key)
            out.append(fact)
        return out


# --------------------------------------------------------------------------
# Layer 1: extraction
# --------------------------------------------------------------------------

_ENV_IN_CODE = re.compile(
    r"""(?:os\.environ(?:\.get)?\(\s*|os\.getenv\(\s*|getenv\(\s*|env\[\s*"""
    r"""|process\.env\.)["']?([A-Z][A-Z0-9_]{2,})["']?"""
)
_ENV_IN_DOTENV = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]{2,})\s*=")
_TF_RESOURCE = re.compile(r'^\s*(?:resource|data)\s+"([a-z0-9_]+)"')
_TF_PROVIDER = re.compile(r'^\s*provider\s+"([a-z0-9_]+)"')
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9._-]+)")
_NORMALISE = re.compile(r"[-_.]+")


def normalise(name: str) -> str:
    """PEP 503. `Pinecone_Client`, `pinecone-client` and `pinecone.client` are one
    package, and a catalog signature should not have to guess which spelling a
    manifest used."""
    return _NORMALISE.sub("-", name).lower()


def _iter_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        # Relative to the root: `build/myapp` is a project, not a build artefact.
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        out.append(path)
    return sorted(out)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _python_facts(path: Path, text: str) -> list[Fact]:
    facts: list[Fact] = []
    lines = text.splitlines()

    def line_text(n: int) -> str:
        return lines[n - 1].strip() if 0 < n <= len(lines) else ""

    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None

    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    facts.append(
                        Fact("python_import", alias.name, path, node.lineno, line_text(node.lineno))
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                facts.append(
                    Fact("python_import", node.module, path, node.lineno, line_text(node.lineno))
                )

    for n, raw in enumerate(lines, start=1):
        for match in _ENV_IN_CODE.finditer(raw):
            facts.append(Fact("env", match.group(1), path, n, raw.strip()))
    return facts


def _requirement_facts(path: Path, text: str) -> list[Fact]:
    facts: list[Fact] = []
    for n, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith(("#", "-", "git+")):
            continue
        match = _REQ_NAME.match(line)
        if match:
            facts.append(Fact("requirement", normalise(match.group(1)), path, n, line))
    return facts


def _pyproject_facts(path: Path, text: str) -> list[Fact]:
    """Parsed, not guessed.

    This was a hand-rolled line scanner, justified by avoiding a TOML dependency.
    It silently missed `dependencies = ["openai"]` written on one line — the form
    this project's own pyproject.toml uses — because it took the first token of the
    line, which is the key. tomllib has been in the standard library since 3.11, so
    the parser was never buying anything.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    if not isinstance(data, dict):
        return []

    project = data.get("project") or {}
    specs: list[str] = list(project.get("dependencies") or [])
    for group in (project.get("optional-dependencies") or {}).values():
        specs += group or []
    for group in (data.get("dependency-groups") or {}).values():
        specs += [g for g in (group or []) if isinstance(g, str)]
    poetry = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {}
    specs += list(poetry)

    lines = text.splitlines()
    facts: list[Fact] = []
    for spec in specs:
        if not isinstance(spec, str):
            continue
        match = _REQ_NAME.match(spec)
        if not match:
            continue
        name = match.group(1)
        line_no = next((i for i, ln in enumerate(lines, 1) if name in ln), 1)
        facts.append(
            Fact("requirement", normalise(name), path, line_no, lines[line_no - 1].strip())
        )
    return facts


def _package_json_facts(path: Path, text: str) -> list[Fact]:
    facts: list[Fact] = []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return facts
    if not isinstance(data, dict):
        return facts  # one malformed file must not take the run down
    lines = text.splitlines()
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        block = data.get(section)
        for name in block if isinstance(block, dict) else ():
            line_no = next(
                (i for i, text_line in enumerate(lines, 1) if f'"{name}"' in text_line), 1
            )
            facts.append(Fact("npm", name, path, line_no, lines[line_no - 1].strip()))
    return facts


def _terraform_facts(path: Path, text: str) -> list[Fact]:
    facts: list[Fact] = []
    for n, raw in enumerate(text.splitlines(), start=1):
        for pattern, kind in (
            (_TF_RESOURCE, "terraform_resource"),
            (_TF_PROVIDER, "terraform_provider"),
        ):
            match = pattern.match(raw)
            if match:
                facts.append(Fact(kind, match.group(1), path, n, raw.strip()))
    return facts


def _dotenv_facts(path: Path, text: str) -> list[Fact]:
    facts: list[Fact] = []
    for n, raw in enumerate(text.splitlines(), start=1):
        match = _ENV_IN_DOTENV.match(raw)
        if match:
            facts.append(Fact("env", match.group(1), path, n, raw.split("=")[0].strip()))
    return facts


def _symbol_pattern(needle: str) -> re.Pattern:
    """Boundary-aware, case-sensitive search.

    A bare substring search makes `OpenAI(` match inside `AzureOpenAI(`, and a
    right-unbounded one makes `GenerativeModel` match `GenerativeModelWrapper`.
    Both name a dependency the codebase does not have.

    Case-sensitive on purpose: these are identifiers, and `searchclient` in a URL
    is not `SearchClient` in code.
    """
    tail = r"(?![A-Za-z0-9_])" if needle[-1:].isalnum() or needle[-1:] == "_" else ""
    return re.compile(r"(?<![A-Za-z0-9_.])" + re.escape(needle) + tail)


_SYMBOL_CACHE: dict[str, re.Pattern] = {}


def _prose_lines(text: str) -> set[int]:
    """Lines that are documentation rather than code.

    A symbol inside a comment or a docstring is someone writing *about* a vendor,
    not depending on one. This tool could not scan its own repository without
    reporting five dependencies, all of them its own prose explaining why those
    signatures were tricky.

    Comments and docstrings only. Ordinary string literals stay in scope, because
    `boto3.client("bedrock-runtime")` is a string and is exactly the evidence we
    are looking for.
    """
    lines: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                lines.update(range(tok.start[0], tok.end[0] + 1))
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        pass  # a file we cannot tokenise is one we treat conservatively

    try:
        tree = ast.parse(text)
    except SyntaxError:
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


def _symbol_facts(
    path: Path, text: str, needles: set[str], skip: set[int] | None = None
) -> list[Fact]:
    """Search for catalog-declared symbols, with citation."""
    facts: list[Fact] = []
    lowered = text.lower()
    candidates = [n for n in needles if n.lower() in lowered]
    if not candidates:
        return facts
    skip = skip or set()
    for n, raw in enumerate(text.splitlines(), start=1):
        if n in skip or raw.lstrip().startswith(("#", "//", "*")):
            continue
        for needle in candidates:
            pattern = _SYMBOL_CACHE.setdefault(needle, _symbol_pattern(needle))
            if pattern.search(raw):
                facts.append(Fact("python_symbol", needle, path, n, raw.strip()[:200]))
    return facts


def collect_facts(root: Path, catalog: Catalog) -> list[Fact]:
    """Walk the tree once and extract every fact the catalog could care about."""
    symbols: set[str] = set()
    for entry in catalog.entries:
        symbols.update(entry.detect.get("python_symbol", ()))

    facts: list[Fact] = []
    for path in _iter_files(root):
        name = path.name.lower()
        suffix = path.suffix.lower()

        if suffix == ".py":
            text = _read(path)
            if text is None:
                continue
            facts += _python_facts(path, text)
            facts += _symbol_facts(path, text, symbols, _prose_lines(text))
        elif name.startswith("requirements") and suffix in (".txt", ".in"):
            text = _read(path)
            if text:
                facts += _requirement_facts(path, text)
        elif name == "pyproject.toml":
            text = _read(path)
            if text:
                facts += _pyproject_facts(path, text)
        elif name == "package.json":
            text = _read(path)
            if text:
                facts += _package_json_facts(path, text)
        elif suffix in (".tf", ".tfvars"):
            text = _read(path)
            if text:
                facts += _terraform_facts(path, text)
        elif name.startswith(".env") or name == "env.example":
            text = _read(path)
            if text:
                facts += _dotenv_facts(path, text)
        elif suffix in (".ts", ".js", ".tsx", ".jsx"):
            text = _read(path)
            if text:
                facts += _symbol_facts(path, text, symbols)
                for n, raw in enumerate(text.splitlines(), start=1):
                    for match in _ENV_IN_CODE.finditer(raw):
                        facts.append(Fact("env", match.group(1), path, n, raw.strip()[:200]))
    return facts


# --------------------------------------------------------------------------
# Layer 2: matching
# --------------------------------------------------------------------------


def _import_matches(observed: str, wanted: str) -> bool:
    """`from azure.search.documents import X` matches signature `azure.search.documents`."""
    observed = observed.lower()
    wanted = wanted.lower()
    return observed == wanted or observed.startswith(wanted + ".")


def match(facts: list[Fact], catalog: Catalog) -> list[Finding]:
    findings: list[Finding] = []
    for entry in catalog.entries:
        hits: list[Fact] = []
        for kind, signatures in entry.detect.items():
            wanted = {normalise(s) if kind == "requirement" else s.lower() for s in signatures}
            for fact in facts:
                if fact.kind != kind:
                    continue
                value = fact.value.lower()
                if kind == "python_import":
                    if any(_import_matches(value, w) for w in wanted):
                        hits.append(fact)
                elif value in wanted:
                    hits.append(fact)
        if hits:
            # Sort for a stable report, and drop exact duplicate observations.
            unique = {(str(f.file), f.line, f.kind, f.value): f for f in hits}
            hits = sorted(unique.values(), key=lambda f: (str(f.file), f.line, f.kind, f.value))
            findings.append(Finding(entry=entry, facts=tuple(hits)))

    findings = _resolve_overlaps(findings)

    # Assessed findings first, worst binding at the top; detected ones after, since
    # they carry no claim to rank by.
    order = {"locked": 0, "friction": 1, "portable": 2, "unassessed": 3}
    findings.sort(key=lambda f: (order[f.entry.lockin], f.entry.name))
    return findings


def _resolve_overlaps(findings: list[Finding]) -> list[Finding]:
    """Handle an entry that shares signatures with a more specific one.

    `AzureOpenAI` lives inside the `openai` package, so a plain `import openai` is
    claimed by both entries. The specific entry keeps the shared evidence — but only
    if it has evidence of its own. Without that, a bare `openai` dependency would be
    reported as Azure in a codebase with nothing Azure about it, and naming a vendor
    that is not there is the one failure this tool cannot afford.
    """
    surviving: list[Finding] = []
    for finding in findings:
        shared, claimed = set(), set()
        for other in findings:
            if other.entry.id in finding.entry.excludes:
                # `finding` is the specific one: the general entry's facts are the
                # ones it might be claiming without support.
                shared |= {_key(f) for f in other.facts}
            if finding.entry.id in other.entry.excludes and _has_own(other, findings):
                claimed |= {_key(f) for f in other.facts}

        if finding.entry.excludes and not any(_key(f) not in shared for f in finding.facts):
            continue  # every signature is shared: the general entry explains it better

        own = tuple(f for f in finding.facts if _key(f) not in claimed)
        if own:
            surviving.append(Finding(entry=finding.entry, facts=own))
    return surviving


def _key(fact: Fact) -> tuple[str, int, str, str]:
    return (str(fact.file), fact.line, fact.kind, fact.value)


def _has_own(finding: Finding, findings: list[Finding]) -> bool:
    shared = {
        _key(g)
        for other in findings
        if other.entry.id in finding.entry.excludes
        for g in other.facts
    }
    return any(_key(f) not in shared for f in finding.facts)


def _import_matches(observed: str, wanted: str) -> bool:
    """`from azure.search.documents import X` matches signature `azure.search.documents`."""
    observed = observed.lower()
    wanted = wanted.lower()
    return observed == wanted or observed.startswith(wanted + ".")


def _resolve_overlaps(findings: list[Finding]) -> list[Finding]:
    """Handle entries that share signatures with a more specific one.

    `AzureOpenAI` lives inside the `openai` package, so a plain import of `openai`
    is claimed by both entries. When the specific entry declares `excludes`, the
    shared evidence belongs to it, and the general entry survives only on evidence
    of its own.
    """
    surviving: list[Finding] = []

    def key(f: Fact) -> tuple[str, int, str, str]:
        return (str(f.file), f.line, f.kind, f.value)

    # An entry may only claim shared evidence if it has evidence of its own.
    # Otherwise `openai` in requirements.txt would be reported as Azure OpenAI in a
    # codebase with nothing Azure about it — naming a vendor that is not there is
    # the one failure this tool cannot afford.
    entitled: set[str] = set()
    for other in findings:
        if not other.entry.excludes:
            continue
        shared = {
            key(f)
            for f in other.facts
            for excluded in findings
            if excluded.entry.id in other.entry.excludes
            for g in excluded.facts
            if key(f) == key(g)
        }
        if any(key(f) not in shared for f in other.facts):
            entitled.add(other.entry.id)

    for finding in findings:
        # A specific entry whose every signature is shared with the general one it
        # excludes has not been evidenced at all: the general entry is the simpler
        # explanation of the same lines, so the specific one drops out.
        if finding.entry.excludes and finding.entry.id not in entitled:
            continue

        claimed: set[tuple[str, int, str, str]] = set()
        for other in findings:
            if finding.entry.id in other.entry.excludes and other.entry.id in entitled:
                claimed.update(key(f) for f in other.facts)
        if not claimed:
            surviving.append(finding)
            continue
        own = tuple(f for f in finding.facts if key(f) not in claimed)
        if own:
            surviving.append(Finding(entry=finding.entry, facts=own))
    return surviving
