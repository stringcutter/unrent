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
from .retired import Snap, _rel, replacement, snaps, state

RANKED_BY = {
    "momentum": "ranked by GitHub stars gained in the last 90 days",
    "stars": "ranked by GitHub stars (momentum needs four weeks of history)",
    "trending": "ranked by Hugging Face trending",
    None: "catalog order, not ranked",
}
EVIDENCE_SHOWN = 5
_BACKTICKS = re.compile(r"`+")
AHEAD_SHOWN = 3
# Alternatives named per pool in a summary row: a ranking by popularity is no verdict
# on which fits, so the summary never crowns one.
PICKS = 3


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


def _licence(alt: Alternative) -> str:
    return f"{alt.licence}, open core: {alt.open_core}" if alt.open_core else alt.licence


def _describe(alt: Alternative, pool: Pool) -> str:
    kind = [_kinds(alt.kind)] if alt.kind else []
    bits = kind + _stats(alt, pool) + ([_licence(alt)] if alt.licence else [])
    meta = f" ({', '.join(bits)})" if bits else ""
    return f"[{alt.name}]({alt.url}) — {alt.what}{meta}"


def _code(text: str) -> str:
    """A Markdown code span that survives backticks in the text: fence it with one
    more backtick than its longest run, and pad when it starts or ends with one."""
    runs = [len(m) for m in _BACKTICKS.findall(text)]
    fence = "`" * (max(runs, default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") or runs else ""
    return f"{fence}{pad}{text}{pad}{fence}" if text else "``"


def today() -> _dt.date:
    return _dt.datetime.now(_dt.UTC).date()


@dataclass
class Split:
    closed: list[Finding]  # closed services the code depends on
    named: list[Finding]  # closed model ids with nothing behind them
    templates: list[Finding]  # keys in example env files with nothing behind them
    running: list[Finding]  # open source components already in use


def _split(findings: list[Finding]) -> Split:
    return Split(
        closed=[
            f
            for f in findings
            if not f.service.open_source and not f.models_only and not f.template_only
        ],
        named=[f for f in findings if not f.service.open_source and f.models_only],
        templates=[
            f
            for f in findings
            if not f.service.open_source and not f.models_only and f.template_only
        ],
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


def _entry(f: Finding, root: Path, catalog: Catalog | None = None) -> dict:
    own = catalog.self_hosted(f.service) if catalog else None
    return {
        "id": f.service.id,
        "name": f.service.name,
        "category": f.service.category,
        "only_in_tests": f.test_only,
        "replace_with": list(f.service.replace_with),
        # The open source project this hosted service runs on: the smallest switch.
        **({"self_host": own.name} if own else {}),
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
        out.append(
            Standing(
                pool=pool,
                rank=None if position is None else position + 1,
                of=len(alts),
                kind_rank=None if kind_position is None else kind_position + 1,
                kind_of=len(peers),
                ahead=alts[:position] if position is not None else alts,
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


# ---------------------------------------------------------------- retiring models


def _snap_json(s: Snap, root: Path, catalog: Catalog, as_of: _dt.date) -> dict:
    r = s.retirement
    return {
        "id": r.id,
        "state": state(r, as_of),  # snapped: retired already; snaps: on `retires`
        "retires": r.retires.isoformat(),
        "vendor": r.vendor,
        "service": s.finding.service.id,
        "replacement": r.replacement,  # the vendor's recommendation
        # The replacement after following any that retire too.
        "use_instead": replacement(r, catalog),
        "source": r.url,
        "evidence": [
            {"file": _rel(x.file, root), "line": x.line, "text": x.evidence} for x in s.sites
        ],
        "also_named": len(s.named),  # lines that name it without selecting it
    }


# ---------------------------------------------------------------- JSON


def payload(
    findings: list[Finding],
    root: Path,
    catalog: Catalog,
    skipped: list[Path] = (),
    unknown: list[dict] | None = None,
    as_of: _dt.date | None = None,
    scanned: str | None = None,
) -> dict:
    """`as_of`: the day retirements are judged against; today when None. `scanned`:
    what was scanned when not the whole of root (one file)."""
    split = _split(findings)
    as_of = as_of or today()
    retiring = snaps(findings, catalog, root)
    return {
        # The folder name; a full path would leak the user's home.
        "scanned": scanned or root.name,
        "scanned_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "as_of": as_of.isoformat(),
        "catalog_services": len(catalog),
        "rankings_date": catalog.rankings_date,
        "not_scanned": [_rel(p, root) for p in skipped],
        "found": [_entry(f, root, catalog) for f in split.closed],
        # Model ids the code selects that are retired, or have a retirement date.
        "models_retiring": [_snap_json(s, root, catalog, as_of) for s in retiring if s.sites],
        # Retiring ids only named: in menus, tables, checks or sample data.
        "models_retiring_named_only": [s.id for s in retiring if not s.sites],
        # Closed model ids with no SDK, key, host or package behind them.
        "models_named": [_entry(f, root) for f in split.named],
        # Keys in an example env file that nothing else backs.
        "env_template_only": [_entry(f, root) for f in split.templates],
        # Open source components already in use, and where they stand in their pool.
        "open_source": [_running_json(f, root, catalog) for f in split.running],
        # API hosts and keys no catalog entry explains that look like a hosted AI API:
        # candidates to check, not findings (unrent/discover.py).
        "unknown_candidates": list(unknown or []),
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


def to_json(
    findings: list[Finding],
    root: Path,
    catalog: Catalog,
    skipped: list[Path] = (),
    unknown: list[dict] | None = None,
    as_of: _dt.date | None = None,
    scanned: str | None = None,
) -> str:
    return json.dumps(
        payload(findings, root, catalog, skipped, unknown, as_of, scanned),
        indent=2,
        ensure_ascii=False,
    )


# ---------------------------------------------------------------- markdown


def _listed_apart(findings: list[Finding], root: Path, title: str, why: str) -> list[str]:
    if not findings:
        return []
    out = [f"## {title}", "", why, ""]
    for f in findings:
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


def _unknown(unknown: list[dict]) -> list[str]:
    if not unknown:
        return []
    out = [
        "## Possibly closed AI services unrent does not know",
        "",
        "API hosts and keys that no catalog entry explains and that look like a hosted AI "
        "API. Not counted as findings: check each one, and open an issue for the real ones.",
        "",
    ]
    for c in unknown:
        first = c["evidence"][0]
        if c["kind"] == "provider catalog":
            out.append(
                f"- **`{first['file']}`**: a generated provider catalog naming {c['hosts']} "
                f"hosts unrent does not know (e.g. {', '.join(c['examples'][:5])})"
            )
            continue
        named = [*c["hosts"], *(f"`{s}`" for s in c["settings"])]
        own = " — the project's own hosted service" if c.get("own_project") else ""
        out.append(
            f"- **{c['name']}** ({', '.join(named)}){own}: `{first['file']}:{first['line']}` — "
            f"{_code(first['text'][:100])}"
        )
    out.append("")
    return out


def _retiring(retiring: list[Snap], root: Path, catalog: Catalog, as_of: _dt.date) -> list[str]:
    picked = [s for s in retiring if s.sites]
    if not picked:
        return []
    noun = "model id the code selects" if len(picked) == 1 else "model ids the code selects"
    out = [
        "## Models that stop working",
        "",
        f"{len(picked)} {noun}: retired, or with a retirement date announced by the vendor "
        f"(as of {as_of.isoformat()}). Requests to a retired model fail.",
        "",
        "| Model | Status | Date | Replace with | Where |",
        "|---|---|---|---|---|",
    ]
    for s in picked:
        r = s.retirement
        status = "**retired**" if state(r, as_of) == "snapped" else "retires"
        use = replacement(r, catalog)
        first = s.sites[0]
        more = f" (+{len(s.sites) - 1})" if len(s.sites) > 1 else ""
        out.append(
            f"| `{r.id}` | {status} | [{r.retires.isoformat()}]({r.url}) | "
            f"{f'`{use}`' if use else '—'} | `{_rel(first.file, root)}:{first.line}`{more} |"
        )
    named = [s.id for s in retiring if not s.sites]
    if named:
        out += [
            "",
            f"Also named, not selected (menus, tables, checks): {', '.join(named[:10])}"
            + (f" and {len(named) - 10} more." if len(named) > 10 else "."),
        ]
    out.append("")
    return out


def to_markdown(
    findings: list[Finding],
    root: Path,
    catalog: Catalog,
    top: int = 3,
    skipped: list[Path] = (),
    unknown: list[dict] | None = None,
    as_of: _dt.date | None = None,
    scanned: str | None = None,
) -> str:
    split = _split(findings)
    as_of = as_of or today()
    out: list[str] = [f"# AI dependencies in `{scanned or root.name}`", ""]
    ranked = f" · alternatives ranked {catalog.rankings_date}" if catalog.rankings_date else ""
    out += [
        f"Scanned {today().isoformat()} · "
        f"{len(catalog)} closed AI services and {len(catalog.projects)} open source "
        f"projects in the catalog{ranked}",
        "",
    ]
    out += _not_scanned(list(skipped), root)
    out += _retiring(snaps(findings, catalog, root), root, catalog, as_of)

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
            own = catalog.self_hosted(f.service)
            if own:
                picks.append(f"Self-hosted: [{own.name}]({own.url})")
            for pool in catalog.alternatives_for(f.service):
                if pool.alternatives:
                    rest = [a for a in pool.alternatives if a is not own][:PICKS]
                    tops = ", ".join(f"[{a.name}]({a.url})" for a in rest)
                    picks.append(f"{pool.name}: {tops}")
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

    out += _listed_apart(
        split.named,
        root,
        "Closed models named in code",
        "Model ids with no SDK, key, API host or package behind them. Not counted as dependencies.",
    )
    out += _listed_apart(
        split.templates,
        root,
        "Keys only in example env files",
        "A placeholder in `.env.example` or similar, with nothing in the code behind it. "
        "Not counted as dependencies.",
    )
    out += _unknown(list(unknown or []))

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
