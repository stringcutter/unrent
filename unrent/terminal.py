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

from .catalog import Alternative, ArenaModel, Catalog, Pool, model_key
from .detect import Finding
from .facts import Fact
from .models import ModelUse, matches
from .render import (
    ARENA_SOURCE,
    ARENA_URL,
    PICKS,
    RANKED_BY,
    Split,
    _describe,
    _split,
    standings,
    today,
)
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
    state: str  # cut, held, snapped, snaps, model, runs
    id: str
    name: str
    where: str  # file:line of the strongest evidence
    more: int  # further locations
    then: str  # what replaces it, or where it stands
    tests: bool
    notes: tuple[tuple[str, str], ...] = ()  # lines under `then`: ("rising", "a, b")


def _loc(fact: Fact, root: Path) -> str:
    return _clean(f"{_rel(fact.file, root)}:{fact.line}")


# Repo names that say little without their owner: e2b-dev/runtime, livekit/agents,
# tigerless-labs/agent-memory (every word generic).
# ponytail: a word list; grow it when a short name reads as nothing.
GENERIC = frozenset(
    {"agent", "agents", "api", "app", "core", "engine", "gateway", "inference", "memory",
     "model", "models", "platform", "runtime", "sandbox", "sdk", "search", "server",
     "speech", "tools"}
)  # fmt: skip


def _short(name: str) -> str:
    repo = name.rsplit("/", 1)[-1]
    return name if all(w in GENERIC for w in re.split(r"[-_.]", repo.lower())) else repo


def _names(alts: list[Alternative]) -> str:
    return ", ".join(_short(a.name) for a in alts)


# A row names the two fastest rising; --why and the report show the rest.
RISING_SHOWN = 2


def _open_model(service, catalog: Catalog) -> Alternative | None:
    """The top open-weight model of the first Hugging Face pool that replaces `service`."""
    pools = catalog.alternatives_for(service)
    pool = next((p for p in pools if p.source != "github" and p.alternatives), None)
    return pool.alternatives[0] if pool else None


def _best(f: Finding, catalog: Catalog) -> tuple[str, tuple[tuple[str, str], ...]]:
    """The project a hosted service runs, else the biggest few of the first pool that
    replaces it, and under them the fastest rising among the rest. A popularity ranking
    crowns no winner. An open-weight model replaces a model, not a service: it stands on
    the snaps row, next to the vendor's replacement, unless there is nothing else."""
    own = catalog.self_hosted(f.service)
    github = next(
        (p for p in catalog.alternatives_for(f.service) if p.source == "github" and p.alternatives),
        None,
    )
    if github:
        big, rising = github.lenses(PICKS, skip=(own.name,) if own else ())
        main = f"self-host {own.name}" if own else _names(big)
        return main, ((("rising", _names(rising[:RISING_SHOWN])),) if rising else ())
    if own:
        return f"self-host {own.name}", ()
    model = _open_model(f.service, catalog)
    return (_names([model]) if model else ""), ()


def _standing(f: Finding, catalog: Catalog) -> tuple[str, tuple[tuple[str, str], ...]]:
    ranked = [s for s in standings(f, catalog) if s.rank is not None]
    if not ranked:
        return "dropped from its ranking", ()
    s = min(ranked, key=lambda s: s.rank)
    rising = s.pool.rising(RISING_SHOWN, skip=(f.service.repo,))
    return f"#{s.rank} of {s.of} in {s.pool.name}", (
        (("rising", _names(rising)),) if rising else ()
    )


def snap_rows(retiring: list[Snap], root: Path, catalog: Catalog, as_of) -> list[Row]:
    out = []
    for s in retiring:
        if not s.sites:
            continue
        r = s.retirement
        gone = state(r, as_of) == "snapped"
        use = replacement(r, catalog)
        rated = catalog.arena.get(model_key(r.id))
        picks = matches(rated, catalog).picks[:1] if rated else []
        model = _open_model(s.finding.service, catalog)
        opens = _rated(picks) if picks else _names([model]) if model else ""
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
                (("open", opens),) if opens else (),
            )
        )
    return out


MODELS_SHOWN = 5
BEST_OPEN = "the best open model of its size on LMArena"


def _rated(models: list[ArenaModel]) -> str:
    """Open-weight models by name, each under its maker's own licence saying so."""
    return ", ".join(m.display + ("" if m.open_licence else f" ({m.licence})") for m in models)


def model_rows(models: list[ModelUse], root: Path, catalog: Catalog) -> list[Row]:
    """A model the code selects, the open-weight models LMArena rates as good (or the
    nearest below, `none as good`), and the ratings that say so."""
    out = []
    for u in models:
        m = matches(u.model, catalog)
        if not m.picks and not u.model.open:
            continue
        arena = f"{round(u.model.rating)}"
        if m.picks:
            arena += " vs " + ", ".join(f"{round(p.rating)}" for p in m.picks)
        notes = [("arena", arena + (" · none as good" if m.close else ""))]
        if m.rising:
            notes.append(("rising", _rated(m.rising)))
        out.append(
            Row(
                "model",
                u.model.display,
                u.model.display,
                _loc(u.sites[0], root),
                len(u.sites) - 1,
                _rated(m.picks) or BEST_OPEN,
                False,
                tuple(notes),
            )
        )
    return out


def rows(split: Split, root: Path, catalog: Catalog) -> list[Row]:
    out = []
    for f in split.closed:
        best, notes = _best(f, catalog)
        out.append(
            Row(
                "cut" if best else "held",
                f.service.id,
                f.service.name,
                _loc(f.cited[0], root),
                len(f.cited) - 1,
                best or "no open match yet",
                f.test_only,
                notes,
            )
        )
    # What can be cut first, and the string with the most locations first within it.
    out.sort(key=lambda r: (r.state != "cut", -r.more))
    for f in split.running:
        standing, notes = _standing(f, catalog)
        out.append(
            Row(
                "runs",
                f.service.id,
                f.service.name,
                _loc(f.cited[0], root),
                len(f.cited) - 1,
                standing,
                f.test_only,
                notes,
            )
        )
    return out


# ---------------------------------------------------------------- layout


TAGS = {
    "cut": "╎ cut",
    "held": "│ held",
    "snapped": "┆ snapped",
    "snaps": "┆ snaps",
    "model": "│ model",
    "runs": "│ runs",
}


def _bar(state: str, w: int, st: Style) -> str:
    tag = f"{TAGS[state]:<{w}}"
    if state == "cut":
        return st.pink(tag)
    if state in ("snapped", "snaps"):
        return st.amber(tag)
    return st.mute(tag) if state in ("runs", "model") else tag


def _arrow(r: Row) -> bool:
    """Does the row's answer name what replaces it?"""
    return r.state == "cut" or (r.state == "model" and r.then != BEST_OPEN)


def _then(r: Row, st: Style, arrow: bool) -> str:
    if _arrow(r):
        return f"→ {r.then}" if arrow else r.then
    if r.state in ("snapped", "snaps"):
        when, _, use = r.then.partition(" → ")
        return st.amber(when) + (f" → {use}" if use else "")
    return st.mute(r.then)


NOTE_W = len("rising")


def _notes(r: Row, indent: int, st: Style) -> list[str]:
    """The lines under a row's `then`, each with a quiet label: rising, model."""
    return [" " * indent + st.mute(f"{label:<{NOTE_W}}") + GAP + text for label, text in r.notes]


def _body(r: Row, indent: int, st: Style) -> list[str]:
    """What replaces a row, or where it stands, under its name: then its notes, in line
    with the text past the arrow."""
    pad = " " * indent
    return [pad + _then(r, st, True), *_notes(r, indent + (2 if _arrow(r) else 0), st)]


def _line(r: Row, widths: tuple[int, int, int], st: Style) -> list[str]:
    """Name, file and count on one line, the answer under it."""
    bar_w, name_w, where_w = widths
    name = r.name + (" (tests)" if r.tests else "")
    more = f"+{r.more}" if r.more else ""
    head = (
        f"{_bar(r.state, bar_w, st)}{GAP}{name:<{name_w}}{GAP}"
        f"{st.mute(f'{r.where:<{where_w}}  {more}')}"
    ).rstrip()
    return [head, *_body(r, bar_w + len(GAP), st)]


def _stacked(r: Row, bar_w: int, st: Style) -> list[str]:
    """Narrow terminals: the file on a line of its own too."""
    name = r.name + (" (tests)" if r.tests else "")
    indent = bar_w + len(GAP)
    more = f"  +{r.more}" if r.more else ""
    return [
        f"{_bar(r.state, bar_w, st)}{GAP}{name}",
        " " * indent + st.mute(r.where + more),
        *_body(r, indent, st),
    ]


def _width(r: Row) -> int:
    """The widest line under a row: its `then` past the arrow, or a note."""
    return max([2 + len(r.then), *(2 + NOTE_W + len(GAP) + len(text) for _, text in r.notes)])


def _blocks(groups: list[list[Row]], width: int, st: Style) -> list[str]:
    """Groups apart by a blank line, names and files in one set of columns; the file
    on a line of its own when they don't fit."""
    groups = [g for g in groups if g]
    if not groups:
        return []
    group = [r for g in groups for r in g]
    bar_w = max(len(TAGS[r.state]) for r in group)
    name_w = max(len(r.name) + (8 if r.tests else 0) for r in group)
    where_w = max(len(r.where) for r in group)
    more_w = max((len(f"+{r.more}") for r in group if r.more), default=0)
    head = bar_w + 2 * len(GAP) + name_w + where_w + 2 + more_w
    body = bar_w + len(GAP) + max(map(_width, group))
    wide = width >= NARROW and max(head, body) <= width
    out: list[str] = []
    for g in groups:
        if out:
            out.append("")
        for r in g:
            if wide:
                out += _line(r, (bar_w, name_w, where_w), st)
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


def _also(
    split: Split, skipped: list[Path], unknown: list[dict], named: int, models: int = 0
) -> str:
    bits = []
    if models:
        bits.append(f"{models} more {'model' if models == 1 else 'models'} in use")
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
    models: list[ModelUse] = (),
) -> str:
    """`command` is how the user ran the scan, for the hints at the end; `as_of` the day
    retirements are judged against (today when None); `scanned` what was scanned when
    not the whole of root; `models` the models in use (models.in_use)."""
    st = Style(colour)
    split = _split(findings)
    unknown = list(unknown or [])
    retiring = snaps(findings, catalog, root)
    tied = rows(split, root, catalog)
    closed = [r for r in tied if r.state != "runs"]
    running = [r for r in tied if r.state == "runs"]
    snapping = snap_rows(retiring, root, catalog, as_of or today())
    # A retiring model has its row already, with an open model on it.
    gone_keys = {model_key(r.id) for r in snapping}
    rated = model_rows(
        [u for u in models if model_key(u.model.name) not in gone_keys], root, catalog
    )
    in_use = rated[:MODELS_SHOWN]
    all_rows = closed + snapping + in_use + running
    cut = sum(1 for r in closed if r.state == "cut")
    gone = sum(1 for r in snapping if r.state == "snapped")
    out = ["", _headline(len(closed), cut, gone, len(snapping) - gone, st)]
    ranked = f" · alternatives ranked {catalog.rankings_date}" if catalog.rankings_date else ""
    if in_use or any(model_key(r.id) in catalog.arena for r in snapping):
        ranked += " · model ratings LMArena (CC BY 4.0)"
    out += [st.mute(f"{scanned or root.name}{ranked}"), ""]
    out += _blocks([closed, snapping, in_use, running], width, st)
    if all_rows:
        out.append("")

    named = sum(1 for s in retiring if not s.sites)
    also = _also(split, list(skipped), unknown, named, len(rated) - len(in_use))
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
        state = "cut" if _best(f, catalog)[0] else "held"
    bar = _bar(state, len(TAGS[state]), st) if state in TAGS else st.mute(f"│ {state}")
    tests = " (only in tests)" if f.test_only else ""
    out = ["", f"{bar}{GAP}{st.bold(f.service.name)}{tests}  {st.mute(f.service.category)}", ""]
    out += _where(f.cited, root, st)
    out.append("")
    if f.service.open_source:
        for s in standings(f, catalog):
            if s.rank is None:
                out.append(f"  {st.bold(s.pool.name)}  dropped from its ranking")
                continue
            rising = f", #{s.rising_rank} by stars gained in 30 days" if s.rising_rank else ""
            out.append(f"  {st.bold(s.pool.name)}  #{s.rank} of {s.of} by stars{rising}")
            out += _field(s.pool, top, (f.service.repo,), st)
    else:
        own = catalog.self_hosted(f.service)
        if own:
            core = st.bold("Its open source core, self-hosted")
            out.append(f"  {core}  {own.name}  {st.mute(own.url)}")
            out.append("")
        for pool in catalog.alternatives_for(f.service):
            out.append(f"  {st.bold(pool.name)}  {st.mute(RANKED_BY.get(pool.ranked_by, ''))}")
            out += _field(pool, top, (own.name,) if own else (), st)
            if not pool.alternatives:
                out.append(st.mute("    no ranked alternatives in this catalog"))
    out.append("")
    return "\n".join(out)


def _field(pool: Pool, top: int, skip: tuple[str, ...], st: Style) -> list[str]:
    """The established projects of a pool, then the rising ones, one per line."""
    big, rising = pool.lenses(top, skip)
    out = []
    for label, alts in (("", big), ("rising", rising)):
        if alts and label:
            out.append(st.mute(f"    {label}"))
        for i, alt in enumerate(alts, start=1):
            # Markdown link to plain text: name, then the URL muted.
            text = _describe(alt, pool).replace(f"[{alt.name}]({alt.url})", alt.name, 1)
            out.append(f"    {i}. {text}  {st.mute(alt.url)}")
    return out


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
    if model := _open_model(s.finding.service, catalog):
        out += [f"  or an open-weight model: {st.bold(model.name)}", st.mute(f"  {model.url}"), ""]
    return "\n".join(out)


def _arena(a: ArenaModel) -> str:
    return f"{round(a.rating)} ({round(a.low)}-{round(a.high)})"


def why_use(use: ModelUse, root: Path, catalog: Catalog, *, colour: bool = False) -> str:
    """Every line that selects one model, its rating, and the open-weight models LMArena
    rates as good, credited."""
    st = Style(colour)
    a, m = use.model, matches(use.model, catalog)
    out = ["", f"{_bar('model', len(TAGS['model']), st)}{GAP}{st.bold(a.display)}"]
    out[-1] += "  " + st.mute(f"{a.org}, {a.licence}" if a.org else a.licence)
    out.append("")
    out += _where(use.sites, root, st)
    out += ["", f"  LMArena {_arena(a)}, {a.votes:,} votes, as {a.name}", ""]
    if m.picks:
        title = "Nearest open weights, none as good" if m.close else "Open weights as good"
        out.append(f"  {st.bold(title)}")
        out += _models_field(m.picks, st)
    elif a.open:
        out.append(f"  {BEST_OPEN}")
    if m.rising:
        out.append(st.mute("    rising"))
        out += _models_field(m.rising, st)
    credit = f"ratings: {ARENA_SOURCE}, {catalog.arena_date} (CC BY 4.0) {ARENA_URL}"
    out += ["", st.mute(f"  {credit}"), ""]
    return "\n".join(out)


def _models_field(models: list[ArenaModel], st: Style) -> list[str]:
    out = []
    for i, a in enumerate(models, start=1):
        size = f", {round(a.params, 1):g}B" if a.params else ""
        url = f"  {st.mute(a.weights.url)}" if a.weights else ""
        out.append(f"    {i}. {a.display}  {_arena(a)}, {a.licence}{size}{url}")
    return out


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
