"""Reporting: the closed AI services a codebase uses and what replaces them, and where
the open source components it already runs stand in their field."""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .catalog import Alternative, Catalog, Pool
from .detect import Finding

RANKED_BY = {
    "momentum": "ranked by GitHub stars gained in the last 90 days",
    "stars": "ranked by GitHub stars (momentum needs four weeks of history)",
    "trending": "ranked by Hugging Face trending",
    None: "catalog order, not ranked",
}
EVIDENCE_SHOWN = 5
_BACKTICKS = re.compile(r"`+")
AHEAD_SHOWN = 3


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _compact(n: int | None) -> str:
    if n is None:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _params(n: int) -> str:
    if n >= 1_000_000_000_000:
        return f"{n / 1_000_000_000_000:.1f}T"
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.0f}B" if n >= 10_000_000_000 else f"{n / 1_000_000_000:.1f}B"
    return f"{n / 1_000_000:.0f}M"


def _stats(alt: Alternative, pool: Pool) -> list[str]:
    bits = []
    if pool.source == "github":
        if alt.stars is not None:
            gain = f", +{_compact(alt.stars_90d)} in 90 days" if alt.stars_90d is not None else ""
            bits.append(f"★ {_compact(alt.stars)}{gain}")
    else:
        if alt.params:
            bits.append(f"{_params(alt.params)} params")
        if alt.downloads is not None:
            bits.append(f"{_compact(alt.downloads)} downloads/month")
    return bits


def _describe(alt: Alternative, pool: Pool) -> str:
    bits = _stats(alt, pool) + ([alt.licence] if alt.licence else [])
    meta = f" ({', '.join(bits)})" if bits else ""
    return f"[{alt.name}]({alt.url}) — {alt.what}{meta}"


def _code(text: str) -> str:
    """A Markdown code span that survives backticks in the text: fence it with one
    more backtick than its longest run, and pad when it starts or ends with one."""
    runs = [len(m) for m in _BACKTICKS.findall(text)]
    fence = "`" * (max(runs, default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") or runs else ""
    return f"{fence}{pad}{text}{pad}{fence}" if text else "``"


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


@dataclass
class Split:
    closed: list[Finding]  # closed services the code depends on
    named: list[Finding]  # closed model ids with nothing behind them
    running: list[Finding]  # open source components already in use


def _split(findings: list[Finding]) -> Split:
    return Split(
        closed=[f for f in findings if not f.service.open_source and not f.models_only],
        named=[f for f in findings if not f.service.open_source and f.models_only],
        running=[f for f in findings if f.service.open_source],
    )


def _pools_in_order(findings: list[Finding]) -> dict[str, list[str]]:
    """Each pool the findings need, once, with the services it replaces."""
    pools: dict[str, list[str]] = {}
    for f in findings:
        for pool_id in f.service.replace_with:
            pools.setdefault(pool_id, []).append(f.service.name)
    return pools


def _evidence(f: Finding, root: Path) -> list[dict]:
    return [
        {
            "kind": fact.kind,
            "value": fact.value,
            "file": _rel(fact.file, root),
            "line": fact.line,
            "text": fact.evidence,
        }
        for fact in f.cited
    ]


def _entry(f: Finding, root: Path) -> dict:
    return {
        "id": f.service.id,
        "name": f.service.name,
        "category": f.service.category,
        "only_in_tests": f.test_only,
        "replace_with": list(f.service.replace_with),
        "evidence": _evidence(f, root),
    }


# ---------------------------------------------------------------- standing


@dataclass
class Standing:
    """Where one open source component stands in one pool."""

    pool: Pool
    rank: int | None  # None: no longer ranked (archived, inactive, relicensed)
    of: int
    kind_rank: int | None  # among projects sharing a kind with it
    kind_of: int
    ahead: list[Alternative]  # every project ranked above it, in rank order
    own: Alternative | None


def _kinds(kind: tuple[str, ...]) -> str:
    return " and ".join(kind)


def same_kind(a: Alternative, kind: tuple[str, ...]) -> bool:
    return bool(set(a.kind) & set(kind))


def standings(f: Finding, catalog: Catalog) -> list[Standing]:
    return standings_of(f.service.repo, f.service.kind, catalog.alternatives_for(f.service))


def standings_of(repo: str, kind: tuple[str, ...], pools: list[Pool]) -> list[Standing]:
    out = []
    for pool in pools:
        alts = list(pool.alternatives)
        position = next((i for i, a in enumerate(alts) if a.name == repo), None)
        peers = [a for a in alts if same_kind(a, kind)]
        kind_position = next((i for i, a in enumerate(peers) if a.name == repo), None)
        ahead = alts[:position] if position is not None else alts
        same_kind_count = len(peers)
        out.append(
            Standing(
                pool=pool,
                rank=None if position is None else position + 1,
                of=len(alts),
                kind_rank=None if kind_position is None else kind_position + 1,
                kind_of=same_kind_count,
                ahead=ahead,
                own=alts[position] if position is not None else None,
            )
        )
    return out


def alternative_json(a: Alternative) -> dict:
    return {k: list(v) if isinstance(v, tuple) else v for k, v in vars(a).items() if v is not None}


def standing_json(s: Standing, ahead: int | None = None) -> dict:
    """`ahead` caps the list of projects ranked above; None lists them all."""
    return {
        "pool": s.pool.id,
        "ranked_by": s.pool.ranked_by,
        "rank": s.rank,
        "of": s.of,
        "rank_among_same_kind": s.kind_rank,
        "same_kind": s.kind_of,
        "ahead": [alternative_json(a) for a in s.ahead[:ahead]],
    }


def _running_json(f: Finding, root: Path, catalog: Catalog) -> dict:
    return {
        "repo": f.service.repo,
        "name": f.service.name,
        "kind": list(f.service.kind),
        "only_in_tests": f.test_only,
        "standing": [standing_json(s) for s in standings(f, catalog)],
        "evidence": _evidence(f, root),
    }


# ---------------------------------------------------------------- JSON


def payload(
    findings: list[Finding], root: Path, catalog: Catalog, skipped: list[Path] = ()
) -> dict:
    split = _split(findings)
    return {
        "scanned": root.name,  # the folder name; a full path would leak the user's home
        "scanned_at": _now().isoformat(timespec="seconds"),
        "catalog_services": len(catalog),
        "rankings_date": catalog.rankings_date,
        "not_scanned": [_rel(p, root) for p in skipped],
        "found": [_entry(f, root) for f in split.closed],
        # Closed model ids with no SDK, key, host or package behind them.
        "models_named": [_entry(f, root) for f in split.named],
        # Open source components already in use, and where they stand in their pool.
        "open_source": [_running_json(f, root, catalog) for f in split.running],
        "alternatives": {
            pool_id: {
                "name": catalog.pools[pool_id].name,
                "replaces": services,
                "ranked_by": catalog.pools[pool_id].ranked_by,
                "items": [alternative_json(a) for a in catalog.pools[pool_id].alternatives],
            }
            for pool_id, services in _pools_in_order(split.closed).items()
        },
    }


def to_json(findings: list[Finding], root: Path, catalog: Catalog, skipped: list[Path] = ()) -> str:
    return json.dumps(payload(findings, root, catalog, skipped), indent=2, ensure_ascii=False)


# ---------------------------------------------------------------- markdown


def _models_named(named: list[Finding], root: Path) -> list[str]:
    if not named:
        return []
    out = [
        "## Closed models named in code",
        "",
        "Model ids with no SDK, key, API host or package behind them. Not counted as dependencies.",
        "",
    ]
    for f in named:
        first = f.cited[0]
        more = f" (+{len(f.cited) - 1} more)" if len(f.cited) > 1 else ""
        out.append(
            f"- **{f.service.name}**: `{_rel(first.file, root)}:{first.line}` — "
            f"{_code(first.evidence[:100])}{more}"
        )
    out.append("")
    return out


def _standing_line(s: Standing, kind: tuple[str, ...]) -> list[str]:
    pool = s.pool
    if s.rank is None:
        return [
            f"- **{pool.name}**: dropped from the ranking — archived, no push in a year, or "
            "no longer open source."
        ]
    head = f"- **{pool.name}**: #{s.rank} of {s.of}"
    if kind and s.kind_of > 1 and s.kind_of < s.of:
        head += f", #{s.kind_rank} of {s.kind_of} of its kind"
    stats = _stats(s.own, pool) if s.own else []
    head += f" ({', '.join(stats)})" if stats else ""
    if s.rank == 1:
        return [head + "."]
    # Only momentum says who is gaining ground; total stars say who is bigger.
    lead_in = "Gaining ground faster" if pool.ranked_by == "momentum" else "Ranked above it"
    lines = [f"{head}. {lead_in}:"]
    for a in s.ahead[:AHEAD_SHOWN]:
        mark = " *(same kind)*" if same_kind(a, kind) else ""
        label = f"{_kinds(a.kind)}, " if a.kind else ""
        stats_a = ", ".join(_stats(a, pool)) or a.what
        lines.append(f"  - [{a.name}]({a.url}) — {label}{stats_a}{mark}")
    if len(s.ahead) > AHEAD_SHOWN:
        lines.append(f"  - …and {len(s.ahead) - AHEAD_SHOWN} more")
    return lines


def _running(running: list[Finding], root: Path, catalog: Catalog) -> list[str]:
    if not running:
        return []
    out = [
        "## Open source you already run",
        "",
        "Rank in its pool, overall and among projects of the same kind.",
        "",
    ]
    for f in running:
        first = f.cited[0]
        tests = " *(only in tests)*" if f.test_only else ""
        out += [
            f"### {f.service.repo} ({_kinds(f.service.kind)}){tests}",
            "",
            f"Found at `{_rel(first.file, root)}:{first.line}` — {_code(first.evidence[:100])} · "
            f"[GitHub](https://github.com/{f.service.repo})",
            "",
        ]
        for s in standings(f, catalog):
            out += _standing_line(s, f.service.kind)
        out.append("")
    return out


def _not_scanned(skipped: list[Path], root: Path) -> list[str]:
    if not skipped:
        return []
    shown = ", ".join(f"`{_rel(p, root)}`" for p in skipped[:5])
    more = f" and {len(skipped) - 5} more" if len(skipped) > 5 else ""
    return [
        f"**Not scanned:** {len(skipped)} file(s) over the size limit: {shown}{more}. "
        "Check them by hand.",
        "",
    ]


def to_markdown(
    findings: list[Finding], root: Path, catalog: Catalog, top: int = 3, skipped: list[Path] = ()
) -> str:
    split = _split(findings)
    out: list[str] = [f"# AI dependencies in `{root.name}`", ""]
    ranked = f" · alternatives ranked {catalog.rankings_date}" if catalog.rankings_date else ""
    out += [
        f"Scanned {_now().date().isoformat()} · "
        f"{len(catalog)} closed AI services and {len(catalog.projects)} open source "
        f"projects in the catalog{ranked}",
        "",
    ]
    out += _not_scanned(list(skipped), root)

    closed = split.closed
    n_running = len(split.running)
    running = f"**{n_running}** open source component{'s' if n_running != 1 else ''} already in use"
    if not closed:
        out += [
            "No closed AI services found. If one is missing from the catalog, open an issue.",
            "",
        ]
        if n_running:
            out += [f"Found {running}.", ""]
    else:
        noun = "service" if len(closed) == 1 else "services"
        also = f", and {running}" if n_running else ""
        out += [f"Found **{len(closed)}** closed AI {noun}{also}.", ""]
        out += ["| Closed service | Category | Replace with |", "|---|---|---|"]
        for f in closed:
            picks = []
            for pool in catalog.alternatives_for(f.service):
                if pool.alternatives:
                    best = pool.alternatives[0]
                    picks.append(f"{pool.name}: [{best.name}]({best.url})")
            name = f"{f.service.name} *(only in tests)*" if f.test_only else f.service.name
            out.append(f"| {name} | {f.service.category} | {'<br>'.join(picks) or '—'} |")
        if any(f.test_only for f in closed):
            out += [
                "",
                "*Only in tests:* all evidence is in test, spec or fixture code. "
                "`--skip-tests` leaves it out.",
            ]
        out += ["", "## Where each one is used", ""]
        for f in closed:
            cited = f.cited
            noun = "location" if len(cited) == 1 else "locations"
            where = " (only in tests)" if f.test_only else ""
            out += [f"### {f.service.name}", "", f"{len(cited)} {noun}{where}:", ""]
            for fact in cited[:EVIDENCE_SHOWN]:
                out.append(
                    f"- `{_rel(fact.file, root)}:{fact.line}` — {_code(fact.evidence[:120])}"
                )
            if len(cited) > EVIDENCE_SHOWN:
                out.append(f"- …and {len(cited) - EVIDENCE_SHOWN} more")
            out.append("")

    out += _models_named(split.named, root)

    if closed:
        out += ["## Open source alternatives", ""]
        for pool_id, services in _pools_in_order(closed).items():
            pool = catalog.pools[pool_id]
            out += [
                f"### {pool.name}",
                "",
                f"Replaces {', '.join(services)} · {RANKED_BY.get(pool.ranked_by, pool.ranked_by)}",
                "",
            ]
            for i, alt in enumerate(pool.alternatives[:top], start=1):
                out.append(f"{i}. {_describe(alt, pool)}")
            if not pool.alternatives:
                out.append("No ranking in this catalog. Run `scripts/refresh.py`.")
            out.append("")

    out += _running(split.running, root, catalog)

    if closed or split.running:
        out += [
            "---",
            "",
            "Open source only: OSI licence for code, open licence for weights. No archived "
            "projects, none without a push in a year. `--top N` for more, `--format json` for "
            "everything.",
        ]
    return "\n".join(out).rstrip() + "\n"
