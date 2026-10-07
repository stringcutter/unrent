"""Snapped models: a model id the code selects that its vendor has retired, or will on
an announced date. Requests to a retired model fail, so these are the strings that
break on their own.

Only a line that picks the model counts (a default, a config value, a model passed to
a call). The same id in a menu, a price table or a check on what the user chose is a
mention: counted, not cited."""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import Catalog, Retirement
from .detect import Fact, Finding

# A model id as written in code: `gpt-4-1106-preview`, `models/gemini-2.0-flash`,
# `openai:gpt-4`, Bedrock's `us.anthropic.claude-...-v1:0`, Vertex's `claude-...@2024...`.
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]*")
# openai/, models/, google-gla:; not Bedrock's version (`-v1:0`).
_PREFIX = re.compile(r"^(?:[\w.-]+(?:/|:(?!\d)))+")
# A Bedrock cross-Region inference profile names the model after its geography.
_REGION = re.compile(r"^(?:us|us-gov|eu|apac|au|jp|ca|global)\.")

# What stands right before an id that the line selects. Matched against the text up
# to the id's opening quote, so `model == "gpt-4"` and `models = ["gpt-4"` don't count.
_SELECTS = re.compile(
    r"""(?ix)
    (?:
        # model = "x", model_name: "x", "model": "x", DefaultModel: "x", default="x",
        # OPENAI_MODEL=x, model: str | None = "x", ModelName { get; set; } = "x",
        # Bicep's param modelName string = 'x'; not == or !=, nor a deployment, whose
        # name is the user's to choose
        (?:model|engine|default)[\w.-]*["'\]]?
        (?:\s*:\s*[\w.\[\]]+(?:\s*\|\s*[\w.\[\]]+)*(?=\s*=)|\s*\{[^}]*\}|\s+string(?=\s*=))?
        \s*(?::=|(?<![=!<>])=(?!=)|:)\s*
      | (?:\.|\bwith|\bset|generative)model(?:name|id)?\(\s*  # WithModel("x"), generativeModel("x")
      | --model(?:-name|-id)?(?:\s+|=)              # az ... --model-name "x"
      | ["'`](?:model|engine)[\w-]*["'`]\s*,\s*(?:["'`]\w["'`]\s*,\s*)?  # flag("model", "m", "x"
      | (?:\|\||\?\?)\s*                            # M || "x", M ?? "x"
      | \b(?:or|else)\s*(?=["'])                    # M or "x"; not prose: `x` or `y`
      | (?:getenv|get|getProperty|GetEnvironmentVariable)\(\s*["'`][^"'`]*["'`]\s*,\s*
    )["'`]?$
    """
)
# A name that holds a model without choosing it: `deprecatedModel`, `modelPlaceholder`.
_NOT_A_CHOICE = re.compile(r"(?i)deprecat|placeholder|description|label|hint|example")
# Inside a call that counts tokens or picks an encoding: the model never reaches an API.
_LOCAL_CALL = re.compile(r"(?i)\w*(?:token|encod)\w*\([^()]*$")
# Right after the id: a comparison or a membership test (`"gpt-4" in model`, `== m`).
_CHECKED = re.compile(
    r"""^["'`]?\s*(?:not\s+in\b|in\b|[=!]==?|\.(?:includes|startswith|endswith)\b)"""
)
# Files whose model ids are sample data, test scaffolding or CI, or only pick a local
# tokenizer: LibreChat's convos.fakeData.ts, dify's mock_openai_server.py.
_NOT_A_CALL = re.compile(
    r"(?i)(?:^|/)(?:\.github|__data__|__mocks__|fixtures?|seeds?)/"
    r"|(?:^|[/._-])(?:fake|mock|tiktoken|tokeniz)"
)


@dataclass
class Snap:
    """One retiring model id found in the code: the lines that select it, and how many
    lines only name it."""

    retirement: Retirement
    finding: Finding
    sites: list[Fact] = field(default_factory=list)
    named: set[tuple[str, int]] = field(default_factory=set)  # (file, line), not selecting

    @property
    def id(self) -> str:
        return self.retirement.id


def _ids(fact: Fact) -> list[tuple[str, int, int]]:
    """Full model ids on the fact's line that its needle reaches, with where the
    written token (prefix included) starts and ends."""
    out = []
    for m in _TOKEN.finditer(fact.evidence):
        token = m.group().rstrip(".,:;/-")
        if fact.value in token:
            out.append((_PREFIX.sub("", token), m.start(), m.start() + len(token)))
    return out


def selects(line: str, start: int, end: int) -> bool:
    """Does `line` pick the model written from `start` to `end`? Not when a check
    follows it, nor when a quoted string goes on with a comma: `"gemini-2.5-flash,
    gemini-2.0-flash"` is a list (an unquoted `model: gpt-4o, temperature: 0` is not)."""
    m = _SELECTS.search(line[:start])
    if not m or _NOT_A_CHOICE.search(_key(line[:start])) or _LOCAL_CALL.search(line[:start]):
        return False
    if _CHECKED.match(line[end:]):
        return False
    return not (line[end : end + 1] == "," and line[start - 1 : start] in ('"', "'", "`", ","))


def _key(before: str) -> str:
    """The name the value is given to: the last identifier before the id."""
    names = re.findall(r"[A-Za-z_][\w.-]*", before)
    return names[-1] if names else ""


def state(r: Retirement, today: _dt.date) -> str:
    return "snapped" if r.retires <= today else "snaps"


def replacement(r: Retirement, catalog: Catalog) -> str | None:
    """The vendor's replacement, followed while that one is retiring too."""
    seen = {r.id}
    current = r.replacement
    while (step := _lookup(catalog, current, r.services[0])) and current not in seen:
        seen.add(current)
        nxt = step.replacement
        if nxt is None or nxt in seen:
            break
        current = nxt
    return current


def _lookup(catalog: Catalog, model: str | None, service: str) -> Retirement | None:
    r = catalog.retirements.get(model) if model else None
    return r.on(service) if r else None


def _retirement(catalog: Catalog, model: str, services: list[str]) -> Retirement | None:
    """The retirement of `model` by the vendor of the first of `services` that has one:
    as written, without a Bedrock Region (`us.`), without a Vertex version (`@2024...`;
    Google lists partner models by name)."""
    for service in services:
        for m in dict.fromkeys((model, _REGION.sub("", model), model.split("@")[0])):
            if r := _lookup(catalog, m, service):
                return r
    return None


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def snaps(findings: list[Finding], catalog: Catalog, root: Path) -> list[Snap]:
    """Every retiring id outside tests in findings of the vendor's own services, oldest
    retirement first. Those with `sites` are snapped or snapping; the rest are only
    named (menus, tables, checks, sample data).

    The finding's service picks the vendor: gpt-4o through Azure OpenAI retires on
    Azure's date. A capability (Imagen, OpenAI's embeddings) takes the platform with
    evidence in the same file that it is part of (Vertex) or that stands in for what it
    is part of (Azure OpenAI for OpenAI) unless the file also has OpenAI's own host,
    else its own."""
    in_file: dict[Path, set[str]] = {}
    hosts: set[tuple[str, Path]] = set()
    excludes = {f.service.id: set(f.service.excludes) for f in findings}
    for f in findings:
        for fact in f.facts:
            in_file.setdefault(fact.file, set()).add(f.service.id)
            if fact.kind == "endpoint":
                hosts.add((f.service.id, fact.file))
    found: dict[tuple[str, str], Snap] = {}
    for f in findings:
        for fact in f.facts:
            if fact.kind != "model" or fact.in_test:
                continue
            call = not _NOT_A_CALL.search(_rel(fact.file, root))
            bases = set(f.service.part_of)
            stand_in = not any((b, fact.file) in hosts for b in bases)
            services = [
                *sorted(
                    s
                    for s in in_file[fact.file]
                    if s in bases or (stand_in and bases & excludes[s])
                ),
                f.service.id,
            ]
            for model, start, end in _ids(fact):
                r = _retirement(catalog, model, services)
                if r is None:
                    continue
                snap = found.setdefault((r.vendor, r.id), Snap(r, f))
                where = (str(fact.file), fact.line)
                if not (call and selects(fact.evidence, start, end)):
                    snap.named.add(where)
                elif all((str(x.file), x.line) != where for x in snap.sites):
                    snap.sites.append(fact)
    for snap in found.values():
        snap.sites.sort(key=lambda x: (str(x.file), x.line))
        snap.named -= {(str(x.file), x.line) for x in snap.sites}
    return sorted(found.values(), key=lambda s: (s.retirement.retires, s.id))
