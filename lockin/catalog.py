"""Catalog loading and validation.

The catalog is the product. The scanner just decides which entries to look up.
Every entry is data, not code: contributors add YAML, not Python.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path

import yaml

LOCKIN_LEVELS = ("portable", "friction", "locked")

# What an entry claims, and therefore what it must prove.
#
#   assessed  — axes verified AND a named open alternative someone has run.
#   detected  — a signature only. "You depend on this" and nothing more.
#
# The split exists because the two halves rest on different evidence. Whether a
# service binds you is largely documentable: is there a self-hosted build, does the
# data export, is the API proprietary. You do not need to have run Pinecone to
# establish that it has no self-hosted build. Which open alternative is *best*, and
# what it costs to get there, is experience — and stays scarce on purpose.
#
# A detected entry makes no claim it cannot support. It is still worth far more than
# a scan that returns nothing: it tells you where to look.
ENTRY_TIERS = ("assessed", "detected")
EFFORT_LEVELS = ("low", "medium", "high")

# An assessment older than this is reported as stale rather than silently trusted.
STALE_AFTER_DAYS = 180


@dataclass(frozen=True)
class Alternative:
    name: str
    kind: str
    compat: str  # migration-specific: how it maps onto the thing being replaced
    effort: str
    loses: str  # migration-specific, on top of the component's own cost
    url: str | None = None
    component: str | None = None  # id in components.yaml, when promoted
    component_loses: str | None = None
    # False when the named component is still a candidate: listed, never run. The
    # report says so rather than passing it off as a vetted recommendation.
    component_assessed: bool = True

    @property
    def full_loses(self) -> str:
        """What you give up: the component's standing cost plus the migration's."""
        parts = [p for p in (self.component_loses, self.loses) if p]
        return " ".join(parts)


@dataclass(frozen=True)
class Assessment:
    """The three axes of METHODOLOGY.md §2. The label is a function of these."""

    interface: int  # 0 drop-in, 1 rewrite, 2 redesign
    data: int  # 0 portable/none, 1 exportable, 2 semantically bound
    behaviour: int  # 0 identical, 1 needs re-validation, 2 no comparable open option

    @property
    def label(self) -> str:
        if max(self.interface, self.data, self.behaviour) == 2:
            return "locked"
        if self.interface == self.data == self.behaviour == 0:
            return "portable"
        return "friction"


@dataclass(frozen=True)
class Entry:
    id: str
    name: str
    category: str
    lockin: str
    why: str
    binding: tuple[str, ...]
    detect: dict[str, tuple[str, ...]]
    alternatives: tuple[Alternative, ...]
    verified: _dt.date
    tier: str = "assessed"
    assessment: Assessment | None = None
    # Vendor features whose use pushes this entry to a higher level. Layer 3 reads
    # this; a static scan cannot tell which usage profile applies.
    escalates_when: str | None = None
    # Ids of more general entries whose shared signatures this entry claims.
    excludes: tuple[str, ...] = ()

    @property
    def age_days(self) -> int:
        return (_dt.datetime.now(_dt.UTC).date() - self.verified).days

    @property
    def is_stale(self) -> bool:
        return self.age_days > STALE_AFTER_DAYS


class CatalogError(ValueError):
    """Raised when a catalog file does not meet the schema."""


@dataclass
class Catalog:
    entries: list[Entry] = field(default_factory=list)
    # Versioned independently of the code: a re-assessment should not need a release.
    version: str = "unversioned"

    def __len__(self) -> int:
        return len(self.entries)

    def by_id(self, entry_id: str) -> Entry | None:
        return next((e for e in self.entries if e.id == entry_id), None)

    @property
    def categories(self) -> list[str]:
        return sorted({e.category for e in self.entries})


def _as_tuple(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


def _require(raw: dict, key: str, entry_id: str, where: Path) -> object:
    if key not in raw or raw[key] in (None, "", []):
        raise CatalogError(f"{where.name}: entry '{entry_id}' is missing required field '{key}'")
    return raw[key]


def _parse_alternative(
    raw: dict, entry_id: str, where: Path, components: dict[str, dict]
) -> Alternative:
    """An alternative either names a component or describes one inline.

    Naming one is preferred: a component's facts then live in exactly one place.
    vLLM was described in seven places before this existed, which is seven places
    to update and six places to forget.
    """
    component_id = raw.get("component")
    component_loses = None
    component_assessed = True
    if component_id:
        comp = components.get(component_id)
        if comp is None:
            raise CatalogError(
                f"{where.name}: entry '{entry_id}' names component '{component_id}', "
                f"which is not in components.yaml"
            )
        # An alternative may name a component nobody has assessed yet. That is worth
        # saying rather than hiding: "here is the open option, and nobody verified it
        # for you" is more use than silence, and the report marks it.
        component_assessed = comp.get("status", "assessed") == "assessed"
        raw = {
            "name": comp["name"],
            # No inventing a type label by truncating prose at its first full stop:
            # for MinerU that silently dropped "Note the AGPL", which is the half
            # that matters.
            "kind": comp.get("kind") or "open source project",
            "url": comp.get("url") or comp.get("repo"),
            **{k: v for k, v in raw.items() if k != "component"},
        }
        component_loses = " ".join(str(comp["loses"]).split()) if comp.get("loses") else None
        raw.setdefault("loses", "")

    name = raw.get("name", "<unnamed>")
    required = (
        ("name", "kind", "compat", "effort")
        if component_id
        else ("name", "kind", "compat", "effort", "loses")
    )
    for key in required:
        if not raw.get(key):
            # 'loses' is deliberately required: an alternative without an honest
            # statement of what you give up is advocacy, not assessment.
            raise CatalogError(
                f"{where.name}: alternative '{name}' of entry '{entry_id}' is missing '{key}'"
            )
    effort = str(raw["effort"]).lower()
    if effort not in EFFORT_LEVELS:
        raise CatalogError(
            f"{where.name}: alternative '{name}' of '{entry_id}' has effort "
            f"'{effort}', expected one of {EFFORT_LEVELS}"
        )
    return Alternative(
        name=str(raw["name"]),
        kind=str(raw["kind"]),
        compat=str(raw["compat"]).strip(),
        effort=effort,
        loses=str(raw.get("loses", "")).strip(),
        url=raw.get("url"),
        component=component_id,
        component_loses=component_loses,
        component_assessed=component_assessed,
    )


def _parse_entry(raw: dict, where: Path, components: dict[str, dict]) -> Entry:
    entry_id = str(raw.get("id", "<no id>"))
    tier = str(raw.get("tier", "assessed")).lower()
    if tier not in ENTRY_TIERS:
        raise CatalogError(
            f"{where.name}: entry '{entry_id}' has tier '{tier}', expected one of {ENTRY_TIERS}"
        )

    if tier == "detected":
        # Detected entries carry facts and nothing else. No axes, no label, no
        # alternatives — claiming any of those without evidence is exactly the
        # failure this tier exists to avoid.
        for key in ("id", "name", "category", "binds", "detect", "listed"):
            _require(raw, key, entry_id, where)
        for key in ("lockin", "assessment", "alternatives"):
            if raw.get(key):
                raise CatalogError(
                    f"{where.name}: detected entry '{entry_id}' carries '{key}' — "
                    f"assess it properly or drop the claim"
                )
        listed = raw["listed"]
        if isinstance(listed, str):
            listed = _dt.date.fromisoformat(listed)
        return Entry(
            id=entry_id,
            name=str(raw["name"]),
            category=str(raw["category"]),
            lockin="unassessed",
            why=str(raw["binds"]).strip(),
            binding=_as_tuple(raw.get("binding")),
            detect={k: _as_tuple(v) for k, v in (raw["detect"] or {}).items()},
            alternatives=(),
            verified=listed,
            tier="detected",
            excludes=_as_tuple(raw.get("excludes")),
        )

    for key in ("id", "name", "category", "lockin", "why", "detect", "alternatives", "verified"):
        _require(raw, key, entry_id, where)

    lockin = str(raw["lockin"]).lower()
    if lockin not in LOCKIN_LEVELS:
        raise CatalogError(
            f"{where.name}: entry '{entry_id}' has lockin '{lockin}', "
            f"expected one of {LOCKIN_LEVELS}"
        )

    # The codebook is executable: a label that does not follow from the declared
    # axes is a bug in the entry, not a matter of taste.
    raw_axes = _require(raw, "assessment", entry_id, where)
    try:
        assessment = Assessment(
            interface=int(raw_axes["interface"]),
            data=int(raw_axes["data"]),
            behaviour=int(raw_axes["behaviour"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CatalogError(
            f"{where.name}: entry '{entry_id}' has a malformed 'assessment' block ({exc})"
        ) from exc
    for axis, value in vars(assessment).items():
        if value not in (0, 1, 2):
            raise CatalogError(
                f"{where.name}: entry '{entry_id}' assessment.{axis} is {value}, expected 0, 1 or 2"
            )
    if assessment.label != lockin:
        raise CatalogError(
            f"{where.name}: entry '{entry_id}' declares lockin '{lockin}' but its axes "
            f"(I{assessment.interface} D{assessment.data} B{assessment.behaviour}) "
            f"give '{assessment.label}' — see METHODOLOGY.md §3"
        )

    verified = raw["verified"]
    if isinstance(verified, str):
        verified = _dt.date.fromisoformat(verified)
    if not isinstance(verified, _dt.date):
        raise CatalogError(f"{where.name}: entry '{entry_id}' has an unparseable 'verified' date")

    detect = {k: _as_tuple(v) for k, v in (raw["detect"] or {}).items()}
    if not any(detect.values()):
        raise CatalogError(f"{where.name}: entry '{entry_id}' has no detection signatures")

    alternatives = tuple(
        _parse_alternative(a, entry_id, where, components) for a in raw["alternatives"]
    )

    return Entry(
        id=entry_id,
        name=str(raw["name"]),
        category=str(raw["category"]),
        lockin=lockin,
        why=str(raw["why"]).strip(),
        binding=_as_tuple(raw.get("binding")),
        detect=detect,
        alternatives=alternatives,
        verified=verified,
        tier="assessed",
        assessment=assessment,
        escalates_when=(str(raw["escalates_when"]).strip() if raw.get("escalates_when") else None),
        excludes=_as_tuple(raw.get("excludes")),
    )


def load_catalog(path: Path) -> Catalog:
    """Load every *.yaml under `path` (or a single file) into a Catalog."""
    # components.yaml holds what alternatives resolve against; it is not itself a
    # list of vendor entries.
    SHARED = {"components.yaml"}
    files = (
        [f for f in sorted(path.glob("*.yaml")) if f.name not in SHARED]
        if path.is_dir()
        else [path]
    )
    if not files:
        raise CatalogError(f"no catalog files found in {path}")

    version_file = (path if path.is_dir() else path.parent) / "VERSION"
    version = (
        version_file.read_text(encoding="utf-8").strip()
        if version_file.is_file()
        else "unversioned"
    )

    comp_file = (path if path.is_dir() else path.parent) / "components.yaml"
    components: dict[str, dict] = {}
    if comp_file.is_file():
        components = {c["id"]: c for c in yaml.safe_load(comp_file.read_text("utf-8"))}

    catalog = Catalog(version=version)
    seen: dict[str, Path] = {}
    for file in files:
        raw = yaml.safe_load(file.read_text(encoding="utf-8")) or []
        if not isinstance(raw, list):
            raise CatalogError(f"{file.name}: expected a list of entries")
        for item in raw:
            if not isinstance(item, dict):
                raise CatalogError(f"{file.name}: expected an entry, found {type(item).__name__}")
            entry = _parse_entry(item, file, components)
            if entry.id in seen:
                raise CatalogError(
                    f"duplicate entry id '{entry.id}' in {file.name} and {seen[entry.id].name}"
                )
            seen[entry.id] = file
            catalog.entries.append(entry)
    return catalog
