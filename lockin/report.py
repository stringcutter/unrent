"""Reporting: which closed AI services a codebase uses, and what replaces them."""

from __future__ import annotations

import datetime as _dt
import json
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


def _describe(alt: Alternative, pool: Pool) -> str:
    bits = []
    if pool.source == "github":
        if alt.stars is not None:
            gain = f", +{_compact(alt.stars_90d)} in 90 days" if alt.stars_90d is not None else ""
            bits.append(f"★ {_compact(alt.stars)}{gain}")
    elif alt.downloads is not None:
        bits.append(f"{_compact(alt.downloads)} downloads/month")
    if alt.licence:
        bits.append(alt.licence)
    meta = f" ({', '.join(bits)})" if bits else ""
    return f"[{alt.name}]({alt.url}) — {alt.what}{meta}"


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


def _pools_in_order(findings: list[Finding], catalog: Catalog) -> dict[str, list[str]]:
    """Each pool the findings need, once, with the services it replaces."""
    pools: dict[str, list[str]] = {}
    for f in findings:
        for pool_id in f.service.replace_with:
            pools.setdefault(pool_id, []).append(f.service.name)
    return pools


def to_json(findings: list[Finding], root: Path, catalog: Catalog) -> str:
    payload = {
        "scanned": str(root),
        "scanned_at": _now().isoformat(timespec="seconds"),
        "catalog_services": len(catalog),
        "rankings_date": catalog.rankings_date,
        "found": [
            {
                "id": f.service.id,
                "name": f.service.name,
                "category": f.service.category,
                "replace_with": list(f.service.replace_with),
                "evidence": [
                    {
                        "kind": fact.kind,
                        "value": fact.value,
                        "file": _rel(fact.file, root),
                        "line": fact.line,
                        "text": fact.evidence,
                    }
                    for fact in f.cited
                ],
            }
            for f in findings
        ],
        "alternatives": {
            pool_id: {
                "name": catalog.pools[pool_id].name,
                "replaces": services,
                "ranked_by": catalog.pools[pool_id].ranked_by,
                "items": [
                    {k: v for k, v in vars(a).items() if v is not None}
                    for a in catalog.pools[pool_id].alternatives
                ],
            }
            for pool_id, services in _pools_in_order(findings, catalog).items()
        },
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def to_markdown(findings: list[Finding], root: Path, catalog: Catalog, top: int = 3) -> str:
    out: list[str] = [f"# AI dependencies in `{root.name}`", ""]
    ranked = f" · alternatives ranked {catalog.rankings_date}" if catalog.rankings_date else ""
    out += [
        f"Scanned {_now().date().isoformat()} · {len(catalog)} closed AI services in the catalog{ranked}",
        "",
    ]

    if not findings:
        out.append(
            "No closed AI services found. Either this codebase has none, or it uses one "
            "the catalog does not cover yet — please open an issue if so."
        )
        return "\n".join(out)

    noun = "service" if len(findings) == 1 else "services"
    out += [f"Found **{len(findings)}** closed AI {noun}.", ""]
    out += ["| Closed service | Category | Replace with |", "|---|---|---|"]
    for f in findings:
        picks = []
        for pool in catalog.alternatives_for(f.service):
            if pool.alternatives:
                best = pool.alternatives[0]
                picks.append(f"{pool.name}: [{best.name}]({best.url})")
        out.append(f"| {f.service.name} | {f.service.category} | {'<br>'.join(picks) or '—'} |")
    out += ["", "## Where each one is used", ""]

    for f in findings:
        cited = f.cited
        noun = "location" if len(cited) == 1 else "locations"
        out += [f"### {f.service.name}", "", f"{len(cited)} {noun}:", ""]
        for fact in cited[:EVIDENCE_SHOWN]:
            out.append(f"- `{_rel(fact.file, root)}:{fact.line}` — `{fact.evidence[:120]}`")
        if len(cited) > EVIDENCE_SHOWN:
            out.append(f"- …and {len(cited) - EVIDENCE_SHOWN} more")
        out.append("")

    out += ["## Open source alternatives", ""]
    for pool_id, services in _pools_in_order(findings, catalog).items():
        pool = catalog.pools[pool_id]
        out += [
            f"### {pool.name}",
            "",
            f"Replaces {', '.join(services)} · {RANKED_BY.get(pool.ranked_by, pool.ranked_by)}",
            "",
        ]
        for i, alt in enumerate(pool.alternatives[:top], start=1):
            out.append(f"{i}. {_describe(alt, pool)}")
        out.append("")

    out += [
        "---",
        "",
        "Open source means an OSI-approved licence for code and an open licence for model "
        "weights. Archived projects, projects without a push in a year, and "
        "source-available licences are left out. `--top N` shows more; "
        "`--format json` gives every ranked alternative.",
    ]
    return "\n".join(out)
