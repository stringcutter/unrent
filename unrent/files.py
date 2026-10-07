"""Which files a scan reads: .gitignore, .unrentignore and --exclude rules, skipped
directories, size limits and test paths; and reading them, in worker processes for a
large scan and with ripgrep, when installed, to find the candidate lines."""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path

from .facts import Needles, _is_text_source, _manifest_parser

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
# Not `spec`/`specs`: outside Ruby those hold API and connector specifications, which are
# production code. Ruby specs are caught by their `_spec.rb` file names instead.
TEST_DIRS = {"test", "tests", "__tests__", "e2e", "__mocks__", "mocks", "fixtures", "testdata",
             "test_data", "cypress", "playwright"}  # fmt: skip
TEST_FILE = re.compile(
    r"(^test_.*\.py$|_test\.(py|go)$|_tests?\.rs$|^tests\.rs$|_spec\.rb$|\.(test|spec|e2e)\.[a-z]+$"
    r"|^(pytest\.ini|conftest\.py|tox\.ini|\.env\.test.*)$)",
    re.IGNORECASE,
)


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
    root: Path,
    exclude: Iterable[str] = (),
    skipped: list[Path] | None = None,
    only: Path | None = None,
) -> list[Path]:
    """Files to scan. Files over the size limit are left out and, when `skipped` is
    given, recorded there so the report can say so. `only`: one file under root to scan
    (`unrent scan app.py`), with the package manifests beside it and in the directories
    above it up to root, for the rules that look at the whole project."""
    rules = load_ignore(root, exclude)
    if only:
        # ponytail: manifests only, so project-wide local config (`.env` with
        # OPENAI_BASE_URL=http://localhost:11434/v1) is not seen and the file's `openai`
        # import counts where a directory scan drops it; model ids are unaffected. Read
        # the env and config files above it too if that matters.
        dirs = [d for d in (only.parent, *only.parent.parents) if d.is_relative_to(root)]
        files = [only, *(p for d in dirs for p in d.iterdir() if p != only and _manifest_parser(p))]
    else:
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
        if _site_data(root, rel):
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


# A documentation site's data directory, and the config file beside it.
_SITE_DATA = {
    "_data": ("_config.yml",),  # Jekyll
    "static": ("docusaurus.config.js", "docusaurus.config.ts", "docusaurus.config.mjs"),
}


def _site_data(root: Path, rel: str) -> bool:
    """A Jekyll site's `_data` files, a Docusaurus site's `static` ones: what the site's
    pages show, such as a leaderboard of models or a list of MCP servers, not what the
    code calls."""
    parts = rel.split("/")[:-1]
    for name, configs in _SITE_DATA.items():
        if name in parts:
            site = root.joinpath(*parts[: parts.index(name)])
            if any((site / config).is_file() for config in configs):
                return True
    return False


def left_out(root: Path, only: Path, exclude: Iterable[str] = (), skip_tests: bool = False) -> bool:
    """Does a scan of one file leave that file out: ignored, in a skipped directory, or
    test code under skip_tests? One over the size limit is listed as not scanned."""
    skipped: list[Path] = []
    if only not in iter_files(root, exclude, skipped, only):
        return only not in skipped
    return skip_tests and is_test_path(only.relative_to(root).as_posix())


LEFT_OUT = "is left out (.unrentignore, --exclude, test code or a skipped directory)"


def _is_relevant(path: Path) -> bool:
    return (
        _manifest_parser(path) is not None
        or _is_text_source(path)
        or path.suffix.lower() == ".ipynb"
    )


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
