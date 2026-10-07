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
from .facts import Fact
from .render import PICKS, RANKED_BY, Split, _describe, _split, standings, today
from .retired import Snap, _rel, replacement, snaps, state

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

    def amber(self, text: str) -> str:
        truecolour = os.environ.get("COLORTERM") in ("truecolor", "24bit")
        return self._sgr("38;2;242;181;68" if truecolour else "38;5;214", text)

    def bold(self, text: str) -> str:
        return self._sgr("1", text)


# ---------------------------------------------------------------- rows


@dataclass
class Row:
    state: str  # cut, held, snapped, snaps, runs
    id: str
    name: str
    where: str  # file:line of the strongest evidence
    more: int  # further locations
    then: str  # what replaces it, or where it stands
    tests: bool


def _loc(fact: Fact, root: Path) -> str:
    return _clean(f"{_rel(fact.file, root)}:{fact.line}")


def _best(f: Finding, catalog: Catalog) -> str:
    """The project a hosted service runs, else the top few of each pool that replaces
    it (projects first, then open weights): a popularity ranking crowns no winner."""
    own = catalog.self_hosted(f.service)
    if own:
        return f"self-host {own.name}"
    pools = sorted(catalog.alternatives_for(f.service), key=lambda p: p.source != "github")
    tops = []
    for pool in pools:
        if pool.source == "github":
            top = ", ".join(a.name.split("/")[-1] for a in pool.alternatives[:PICKS])
        else:
            top = pool.alternatives[0].name.rsplit("/", 1)[-1] if pool.alternatives else ""
        if top and top not in tops:
            tops.append(top)
    return " + ".join(tops[:2])


def _standing(f: Finding, catalog: Catalog) -> str:
    ranked = [s for s in standings(f, catalog) if s.rank is not None]
    if not ranked:
        return "dropped from its ranking"
    s = min(ranked, key=lambda s: s.rank)
    return f"#{s.rank} of {s.of} · {s.pool.name}"


def snap_rows(retiring: list[Snap], root: Path, catalog: Catalog, as_of) -> list[Row]:
    out = []
    for s in retiring:
        if not s.sites:
            continue
        r = s.retirement
        gone = state(r, as_of) == "snapped"
        use = replacement(r, catalog)
        out.append(
            Row(
                "snapped" if gone else "snaps",
                r.id,
                r.id,
                _loc(s.sites[0], root),
                len(s.sites) - 1,
                f"{'retired' if gone else 'retires'} {r.retires.isoformat()}"
                + (f" → {use}" if use else ""),
                False,
            )
        )
    return out


def rows(split: Split, root: Path, catalog: Catalog) -> list[Row]:
    out = []
    for f in split.closed:
        best = _best(f, catalog)
        out.append(
            Row(
                "cut" if best else "held",
                f.service.id,
                f.service.name,
                _loc(f.cited[0], root),
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
                _loc(f.cited[0], root),
                len(f.cited) - 1,
                _standing(f, catalog),
                f.test_only,
            )
        )
    return out


# ---------------------------------------------------------------- layout


TAGS = {
    "cut": "╎ cut",
    "held": "│ held",
    "snapped": "┆ snapped",
    "snaps": "┆ snaps",
    "runs": "│ runs",
}


def _bar(state: str, w: int, st: Style) -> str:
    tag = f"{TAGS[state]:<{w}}"
    if state == "cut":
        return st.pink(tag)
    if state in ("snapped", "snaps"):
        return st.amber(tag)
    return st.mute(tag) if state == "runs" else tag


def _then(r: Row, st: Style, arrow: bool) -> str:
    if r.state == "cut":
        return f"→ {r.then}" if arrow else r.then
    if r.state in ("snapped", "snaps"):
        when, _, use = r.then.partition(" → ")
        return st.amber(when) + (f" → {use}" if use else "")
    return st.mute(r.then)


def _line(r: Row, widths: tuple[int, int, int, int], st: Style) -> str:
    bar_w, name_w, where_w, more_w = widths
    name = r.name + (" (tests)" if r.tests else "")
    more = f"+{r.more}" if r.more else ""
    return (
        f"{_bar(r.state, bar_w, st)}{GAP}{name:<{name_w}}{GAP}"
        f"{st.mute(f'{r.where:<{where_w}}  {more:<{more_w}}')}{GAP}{_then(r, st, False)}"
    ).rstrip()


def _stacked(r: Row, bar_w: int, st: Style) -> list[str]:
    name = r.name + (" (tests)" if r.tests else "")
    indent = " " * (bar_w + len(GAP))
    more = f"  +{r.more}" if r.more else ""
    return [
        f"{_bar(r.state, bar_w, st)}{GAP}{name}",
        indent + st.mute(r.where + more),
        indent + _then(r, st, True),
    ]


def _blocks(groups: list[list[Row]], width: int, st: Style) -> list[str]:
    """Groups apart by a blank line, in one set of columns; stacked when they don't fit."""
    groups = [g for g in groups if g]
    if not groups:
        return []
    group = [r for g in groups for r in g]
    bar_w = max(len(TAGS[r.state]) for r in group)
    name_w = max(len(r.name) + (8 if r.tests else 0) for r in group)
    where_w = max(len(r.where) for r in group)
    more_w = max((len(f"+{r.more}") for r in group if r.more), default=0)
    full = bar_w + 3 * len(GAP) + name_w + where_w + 2 + more_w + max(len(r.then) for r in group)
    wide = width >= NARROW and full <= width
    out: list[str] = []
    for g in groups:
        if out:
            out.append("")
        for r in g:
            if wide:
                out.append(_line(r, (bar_w, name_w, where_w, more_w), st))
            else:
                out += _stacked(r, bar_w, st)
    return out


def _headline(n: int, cut: int, snapped: int, snapping: int, st: Style) -> str:
    noun = "string" if n == 1 else "strings"
    parts = [st.bold(f"{n} {noun} attached." if n else "No strings attached.")]
    if cut:
        parts.append(st.bold(st.pink(f"{cut} can be cut.")))
    if snapped:
        parts.append(st.bold(st.amber(f"{snapped} {'has' if snapped == 1 else 'have'} snapped.")))
    if snapping:
        parts.append(st.bold(st.amber(f"{snapping} will snap.")))
    return " ".join(parts)


def _also(split: Split, skipped: list[Path], unknown: list[dict], named: int) -> str:
    bits = []
    if named:
        bits.append(f"{named} retiring model ids only named, in menus or tables")
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
    as_of=None,
    scanned: str | None = None,
) -> str:
    """`command` is how the user ran the scan, for the hints at the end; `as_of` the day
    retirements are judged against (today when None); `scanned` what was scanned when
    not the whole of root."""
    st = Style(colour)
    split = _split(findings)
    unknown = list(unknown or [])
    retiring = snaps(findings, catalog, root)
    tied = rows(split, root, catalog)
    closed = [r for r in tied if r.state != "runs"]
    running = [r for r in tied if r.state == "runs"]
    snapping = snap_rows(retiring, root, catalog, as_of or today())
    all_rows = closed + snapping + running
    cut = sum(1 for r in closed if r.state == "cut")
    gone = sum(1 for r in snapping if r.state == "snapped")
    out = ["", _headline(len(closed), cut, gone, len(snapping) - gone, st)]
    ranked = f" · alternatives ranked {catalog.rankings_date}" if catalog.rankings_date else ""
    out += [st.mute(f"{scanned or root.name}{ranked}"), ""]
    out += _blocks([closed, snapping, running], width, st)
    if all_rows:
        out.append("")

    named = sum(1 for s in retiring if not s.sites)
    also = _also(split, list(skipped), unknown, named)
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


def _where(facts: list[Fact], root: Path, st: Style) -> list[str]:
    """One line per location, the locations aligned."""
    where = [(_loc(x, root), _clean(x.evidence)) for x in facts]
    w = max(len(loc) for loc, _ in where)
    return [f"  {st.mute(f'{loc:<{w}}')}  {text[:120]}" for loc, text in where]


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
    bar = _bar(state, len(TAGS[state]), st) if state in TAGS else st.mute(f"│ {state}")
    tests = " (only in tests)" if f.test_only else ""
    out = ["", f"{bar}{GAP}{st.bold(f.service.name)}{tests}  {st.mute(f.service.category)}", ""]
    out += _where(f.cited, root, st)
    out.append("")
    if f.service.open_source:
        for s in standings(f, catalog):
            rank = f"#{s.rank} of {s.of}" if s.rank else "dropped from its ranking"
            out.append(f"  {s.pool.name}: {rank}")
            for a in s.ahead[:top]:
                out.append(st.mute(f"    above it: {a.name}"))
    else:
        own = catalog.self_hosted(f.service)
        if own:
            core = st.bold("Its open source core, self-hosted")
            out.append(f"  {core}  {own.name}  {st.mute(own.url)}")
            out.append("")
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


def why_model(s: Snap, root: Path, catalog: Catalog, *, as_of=None, colour: bool = False) -> str:
    """Every line that selects one retiring model, and what the vendor says to use."""
    st = Style(colour)
    r = s.retirement
    gone = state(r, as_of or today()) == "snapped"
    tag = "snapped" if gone else "snaps"
    use = replacement(r, catalog)
    when = f"{'retired' if gone else 'retires'} {r.retires.isoformat()}"
    out = ["", f"{_bar(tag, len(TAGS[tag]), st)}{GAP}{st.bold(r.id)}  {st.amber(when)}", ""]
    out += _where(s.sites, root, st)
    out.append("")
    if use:
        via = f" (via {r.replacement})" if r.replacement != use else ""
        out.append(f"  {r.vendor} recommends {st.bold(use)}{via}")
    if s.named:
        out.append(st.mute(f"  also named on {len(s.named)} lines that don't select it"))
    out += [st.mute(f"  {r.url}"), ""]
    return "\n".join(out)


# ---------------------------------------------------------------- progress


class Progress:
    """One self-erasing status line on stderr while the scan runs. Silent unless
    stderr is a terminal that understands the escape that erases it."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, enabled: bool | None = None):
        self.stream = sys.stderr
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
