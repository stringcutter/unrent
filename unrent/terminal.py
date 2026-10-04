"""The terminal view: one line per string a codebase is tied by, coloured when the
terminal can show it. A pipe or a file gets the Markdown report instead (cli.py)."""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from .catalog import Catalog
from .detect import Finding
from .render import RANKED_BY, Split, _describe, _rel, _split, standings

GAP = "  "
NARROW = 60  # below this, alternatives drop to their own line even when they would fit
# A scanned line or path can carry escape sequences; a terminal would act on them.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _clean(text: str) -> str:
    return _CONTROL.sub("", text)


# ---------------------------------------------------------------- style


def wants_colour(stream: TextIO) -> bool:
    """NO_COLOR and FORCE_COLOR (no-color.org, force-color.org) win; then a dumb
    terminal; then whether the stream is a terminal at all."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    return stream.isatty() and _ansi_on_windows()


def _ansi_on_windows(handle_id: int = -11) -> bool:
    """Windows 10+ consoles understand ANSI once asked; older ones get plain text.
    `handle_id`: -11 for stdout, -12 for stderr."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(handle_id)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))  # VT processing
    except (AttributeError, OSError):
        return False


@dataclass(frozen=True)
class Style:
    colour: bool

    def _sgr(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.colour and text else text

    def pink(self, text: str) -> str:
        truecolour = os.environ.get("COLORTERM") in ("truecolor", "24bit")
        return self._sgr("38;2;255;63;140" if truecolour else "38;5;205", text)

    def mute(self, text: str) -> str:
        return self._sgr("38;5;245", text)

    def bold(self, text: str) -> str:
        return self._sgr("1", text)


# ---------------------------------------------------------------- rows


@dataclass
class Row:
    state: str  # cut, held, runs
    id: str
    name: str
    where: str  # file:line of the strongest evidence
    more: int  # further locations
    then: str  # what replaces it, or where it stands
    tests: bool


def _first(f: Finding, root: Path) -> str:
    fact = f.cited[0]
    return _clean(f"{_rel(fact.file, root)}:{fact.line}")


def _best(f: Finding, catalog: Catalog) -> str:
    """The top of each pool that replaces it: projects first, then open weights."""
    pools = sorted(catalog.alternatives_for(f.service), key=lambda p: p.source != "github")
    tops = []
    for pool in pools:
        if pool.alternatives:
            name = pool.alternatives[0].name
            top = name if pool.source == "github" else name.rsplit("/", 1)[-1]
            if top not in tops:
                tops.append(top)
    return " + ".join(tops[:2])


def _standing(f: Finding, catalog: Catalog) -> str:
    ranked = [s for s in standings(f, catalog) if s.rank is not None]
    if not ranked:
        return "dropped from its ranking"
    s = min(ranked, key=lambda s: s.rank)
    return f"#{s.rank} of {s.of} · {s.pool.name}"


def rows(split: Split, root: Path, catalog: Catalog) -> list[Row]:
    out = []
    for f in split.closed:
        best = _best(f, catalog)
        out.append(
            Row(
                "cut" if best else "held",
                f.service.id,
                f.service.name,
                _first(f, root),
                len(f.cited) - 1,
                best or "no open match yet",
                f.test_only,
            )
        )
    # What can be cut first, and the string with the most locations first within it.
    out.sort(key=lambda r: (r.state != "cut", -r.more))
    for f in split.running:
        out.append(
            Row(
                "runs",
                f.service.id,
                f.service.name,
                _first(f, root),
                len(f.cited) - 1,
                _standing(f, catalog),
                f.test_only,
            )
        )
    return out


# ---------------------------------------------------------------- layout


def _bar(state: str, st: Style) -> str:
    if state == "cut":
        return st.pink("╎ cut ")
    if state == "held":
        return "│ held"
    return st.mute("│ runs")


def _line(r: Row, widths: tuple[int, int, int], st: Style) -> str:
    name_w, where_w, more_w = widths
    name = r.name + (" (tests)" if r.tests else "")
    more = f"+{r.more}" if r.more else ""
    then = r.then if r.state == "cut" else st.mute(r.then)
    return (
        f"{_bar(r.state, st)}{GAP}{name:<{name_w}}{GAP}"
        f"{st.mute(f'{r.where:<{where_w}}  {more:<{more_w}}')}{GAP}{then}"
    ).rstrip()


def _stacked(r: Row, st: Style) -> list[str]:
    name = r.name + (" (tests)" if r.tests else "")
    indent = " " * (6 + len(GAP))
    more = f"  +{r.more}" if r.more else ""
    then = f"→ {r.then}" if r.state == "cut" else st.mute(r.then)
    return [f"{_bar(r.state, st)}{GAP}{name}", indent + st.mute(r.where + more), indent + then]


def _blocks(groups: list[list[Row]], width: int, st: Style) -> list[str]:
    """Groups apart by a blank line, in one set of columns; stacked when they don't fit."""
    groups = [g for g in groups if g]
    if not groups:
        return []
    group = [r for g in groups for r in g]
    name_w = max(len(r.name) + (8 if r.tests else 0) for r in group)
    where_w = max(len(r.where) for r in group)
    more_w = max((len(f"+{r.more}") for r in group if r.more), default=0)
    full = 6 + 3 * len(GAP) + name_w + where_w + 2 + more_w + max(len(r.then) for r in group)
    wide = width >= NARROW and full <= width
    out: list[str] = []
    for g in groups:
        if out:
            out.append("")
        for r in g:
            out += [_line(r, (name_w, where_w, more_w), st)] if wide else _stacked(r, st)
    return out


def _headline(n: int, cut: int, st: Style) -> str:
    if not n:
        return st.bold("No strings attached.")
    noun = "string" if n == 1 else "strings"
    line = st.bold(f"{n} {noun} attached.")
    return f"{line} {st.bold(st.pink(f'{cut} can be cut.'))}" if cut else line


def _also(split: Split, skipped: list[Path], unknown: list[dict]) -> str:
    bits = []
    if split.named:
        bits.append(f"{len(split.named)} closed model ids named with nothing behind them")
    if split.templates:
        bits.append(f"{len(split.templates)} keys only in example env files")
    if unknown:
        bits.append(f"{len(unknown)} possible AI APIs unrent does not know")
    if skipped:
        bits.append(f"{len(skipped)} files too large to scan")
    return " · ".join(bits)


def to_terminal(
    findings: list[Finding],
    root: Path,
    catalog: Catalog,
    *,
    command: str = "unrent .",
    width: int = 100,
    colour: bool = False,
    skipped: list[Path] = (),
    unknown: list[dict] | None = None,
) -> str:
    """`command` is how the user ran the scan, for the hints at the end."""
    st = Style(colour)
    split = _split(findings)
    unknown = list(unknown or [])
    all_rows = rows(split, root, catalog)
    closed = [r for r in all_rows if r.state != "runs"]
    running = [r for r in all_rows if r.state == "runs"]
    cut = sum(1 for r in closed if r.state == "cut")
    out = ["", _headline(len(closed), cut, st)]
    ranked = f" · alternatives ranked {catalog.rankings_date}" if catalog.rankings_date else ""
    out += [st.mute(f"{root.name}{ranked}"), ""]
    out += _blocks([closed, running], width, st)
    if all_rows:
        out.append("")

    also = _also(split, list(skipped), unknown)
    if also:
        out += [st.mute(f"  also: {also}"), ""]
    hints = []
    if all_rows:
        hints.append((f"{command} --why {all_rows[0].id}", "every line behind one string"))
    if all_rows or also:
        hints.append((f"{command} -o unrent.md", "the full report, every alternative ranked"))
    if hints:
        w = max(len(h) for h, _ in hints)
        out += [st.mute(f"  {h:<{w}}  {what}") for h, what in hints]
        out.append("")
    return "\n".join(out)


def why(
    finding: Finding, root: Path, catalog: Catalog, *, top: int = 3, colour: bool = False
) -> str:
    """Every location behind one string, and the ranked alternatives (or its standing)."""
    st = Style(colour)
    f = finding
    if f.service.open_source:
        state = "runs"
    elif f.models_only:
        state = "named"
    elif f.template_only:
        state = "env template"
    else:
        state = "cut" if _best(f, catalog) else "held"
    bar = {"cut": _bar("cut", st), "held": _bar("held", st), "runs": _bar("runs", st)}.get(
        state, st.mute(f"│ {state}")
    )
    tests = " (only in tests)" if f.test_only else ""
    out = ["", f"{bar}{GAP}{st.bold(f.service.name)}{tests}  {st.mute(f.service.category)}", ""]
    where = [(_clean(f"{_rel(x.file, root)}:{x.line}"), _clean(x.evidence)) for x in f.cited]
    w = max(len(loc) for loc, _ in where)
    out += [f"  {st.mute(f'{loc:<{w}}')}  {text[:120]}" for loc, text in where]
    out.append("")
    if f.service.open_source:
        for s in standings(f, catalog):
            rank = f"#{s.rank} of {s.of}" if s.rank else "dropped from its ranking"
            out.append(f"  {s.pool.name}: {rank}")
            for a in s.ahead[:top]:
                out.append(st.mute(f"    above it: {a.name}"))
    else:
        for pool in catalog.alternatives_for(f.service):
            out.append(f"  {st.bold(pool.name)}  {st.mute(RANKED_BY.get(pool.ranked_by, ''))}")
            for i, alt in enumerate(pool.alternatives[:top], start=1):
                line = _describe(alt, pool)
                # Markdown link to plain text: name, then the URL muted.
                text = line.replace(f"[{alt.name}]({alt.url})", alt.name, 1)
                out.append(f"    {i}. {text}  {st.mute(alt.url)}")
            if not pool.alternatives:
                out.append(st.mute("    no ranked alternatives in this catalog"))
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- progress


class Progress:
    """One self-erasing status line on stderr while the scan runs. Silent unless
    stderr is a terminal that understands the escape that erases it."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, stream: TextIO | None = None, enabled: bool | None = None):
        self.stream = stream or sys.stderr
        if enabled is None:
            enabled = (
                self.stream.isatty() and os.environ.get("TERM") != "dumb" and _ansi_on_windows(-12)
            )
        self.on = enabled
        self.frame = 0
        self.last = 0.0

    def show(self, text: str, force: bool = True) -> None:
        if not self.on:
            return
        now = time.monotonic()
        if not force and now - self.last < 0.08:
            return
        self.last = now
        self.frame = (self.frame + 1) % len(self.FRAMES)
        self.stream.write(f"\r\033[K{self.FRAMES[self.frame]} {text}")
        self.stream.flush()

    def files(self, done: int, total: int) -> None:
        self.show(f"reading {done} of {total} files", force=done == total)

    def done(self) -> None:
        if self.on:
            self.stream.write("\r\033[K")
            self.stream.flush()
