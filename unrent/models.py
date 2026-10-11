"""Models the code runs on, closed or open, and the open-weight models that match them.

Quality comes from LMArena's text leaderboard (overall, CC BY 4.0): a model matches
when its rating's confidence interval reaches the other's. For an open model, the
match must also be no bigger, so it runs where the model runs now. Only a line that
selects a model counts, as for retirements (`retired.selects`)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import ArenaModel, Catalog, model_key
from .detect import Finding
from .detect import _registries as _closed_registries
from .facts import Fact
from .retired import _NOT_A_CALL, _TOKEN, _rel, selects

PICKS = 3
RISING_SHOWN = 2
SIZE_SLACK = 1.25  # an open match may be a quarter bigger than the model it replaces
# Commands that run an open model by name: `ollama run gemma4:31b`, `vllm serve Qwen/...`.
_RUNS = re.compile(r"(?i)(?:\bollama\s+(?:run|pull)|\bvllm\s+serve|--hf-repo|-hf)\s+\S*$")


@dataclass
class ModelUse:
    """One model the code selects, and the lines that do."""

    model: ArenaModel
    sites: list[Fact] = field(default_factory=list)
    written: str = ""  # as the code writes it, at the first site
    finding: Finding | None = None  # the closed service it is sent to


@dataclass(frozen=True)
class Matches:
    """Open-weight models for one model in use: the best rated that reach it (`close`
    when none does, so these are the nearest below), and the fastest rising among the
    rest by Hugging Face trending."""

    picks: list[ArenaModel]
    close: bool
    rising: list[ArenaModel]


def in_use(
    findings: list[Finding], facts: list[Fact], root: Path, catalog: Catalog
) -> list[ModelUse]:
    """Every model LMArena rates that a line outside tests and registries selects:
    closed ones in the evidence of the service they are sent to, open ones wherever a
    line runs them. Most selected first."""
    picked: list[tuple[ArenaModel, str, Fact, Finding | None]] = []

    def add(token: str, fact: Fact, start: int, end: int, open_: bool, f: Finding | None):
        model = catalog.arena.get(model_key(token))
        if not model or model.open != open_ or fact.in_test:
            return
        line = fact.evidence
        if _NOT_A_CALL.search(_rel(fact.file, root)):
            return
        if selects(line, start, end) or (open_ and _RUNS.search(line[:start])):
            picked.append((model, token, fact, f))

    for f in findings:
        for fact in f.facts:
            if fact.kind == "model":
                for m in _TOKEN.finditer(fact.evidence):
                    if fact.value in m.group():
                        add(_clean(m.group()), fact, m.start(), m.end(), False, f)
    for fact in facts:
        if fact.kind == "open_model":
            for m in _TOKEN.finditer(fact.evidence):
                add(_clean(m.group()), fact, m.start(), m.end(), True, None)

    listing = _registries(picked) | _closed_registries(findings)
    found: dict[str, ModelUse] = {}
    for model, token, fact, f in picked:
        if fact.file in listing or fact.file.parent in listing:
            continue
        use = found.setdefault(model_key(model.name), ModelUse(model, written=token, finding=f))
        if all((x.file, x.line) != (fact.file, fact.line) for x in use.sites):
            use.sites.append(fact)
    for use in found.values():
        use.sites.sort(key=lambda x: (str(x.file), x.line))
    return sorted(found.values(), key=lambda u: (-len(u.sites), u.model.name))


# A file, or a directory of files, that sets this many different models lists what a
# project supports, not what it runs: aider's model-settings.yml, lobe-chat's one file
# per provider (modelProviders/*.ts), a provider adapter's default in each of llms/*.
MODELS_LISTED_AT = 6


def _registries(picked: list[tuple[ArenaModel, str, Fact, Finding | None]]) -> set[Path]:
    per_file: dict[Path, set[str]] = {}
    for model, _, fact, _ in picked:
        per_file.setdefault(fact.file, set()).add(model.name)
    per_dir: dict[Path, set[str]] = {}
    for file, names in per_file.items():
        per_dir.setdefault(file.parent, set()).update(names)
    return {
        *(f for f, names in per_file.items() if len(names) >= MODELS_LISTED_AT),
        *(d for d, names in per_dir.items() if len(names) >= MODELS_LISTED_AT),
    }


def _clean(token: str) -> str:
    return token.rstrip(".,:;/-")


def matches(model: ArenaModel, catalog: Catalog, n: int = PICKS) -> Matches:
    """The open-weight models as good as `model` by LMArena, best first. For an open
    model, only those no bigger, and none when none is as good: it is the best there is
    at its size."""
    pool = [a for a in catalog.arena.values() if a.open and a is not model]
    if model.open and (size := model.params):
        pool = [a for a in pool if a.params is not None and a.params <= size * SIZE_SLACK]
    reach = [a for a in pool if a.high >= model.low]
    best = sorted(reach or ([] if model.open else pool), key=lambda a: -a.rating)
    picks = best[:n]
    # Rising stays in the picks' league: as good as the model, or as the last pick when
    # none is. A trending 27B is no news next to a frontier model.
    floor = model.low if reach else (picks[-1].low if picks else model.low)
    rest = [a for a in best[n:] if a.high >= floor and a.weights and a.weights.trending]
    rising = sorted(rest, key=lambda a: -a.weights.trending)[:RISING_SHOWN]
    return Matches(picks, close=not reach and bool(picks), rising=rising)
