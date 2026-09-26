"""Reporting. Aggregation is code, never a model.

The report states what was found, how locked it is, what open source replaces it,
and what you lose by switching. The last one is what makes the rest trustworthy.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from .catalog import Catalog
from .detect import Finding

LOCKIN_LABEL = {
    "unassessed": "Not assessed",
    "locked": "Locked",
    "friction": "Friction",
    "portable": "Portable",
}
LOCKIN_BLURB = {
    "unassessed": (
        "Detected, not assessed. Nobody here has run this, so there is no claim "
        "about how hard it is to leave."
    ),
    "locked": (
        "No compatible replacement. Leaving means rewriting against a different "
        "model of the problem."
    ),
    "friction": "Replaceable, but not by repointing a URL. Expect real work and a quality re-test.",
    "portable": (
        "An open implementation exists that speaks the same interface. You are buying "
        "operations, not access."
    ),
}


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def to_json(
    findings: list[Finding], root: Path, catalog: Catalog, packages: dict | None = None
) -> str:
    payload = {
        "scanned": str(root),
        "scanned_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "catalog_version": catalog.version,
        "catalog_entries": len(catalog),
        "summary": summarise(findings),
        "packages": {
            f"{eco}:{name}": {k: v for k, v in vars(fact).items()}
            for (eco, name), fact in (packages or {}).items()
        },
        "findings": [
            {
                "id": f.entry.id,
                "name": f.entry.name,
                "category": f.entry.category,
                "lockin": f.entry.lockin,
                "assessment": (
                    {
                        "interface": f.entry.assessment.interface,
                        "data": f.entry.assessment.data,
                        "behaviour": f.entry.assessment.behaviour,
                    }
                    if f.entry.assessment
                    else None
                ),
                "escalates_when": f.entry.escalates_when,
                "binding": list(f.entry.binding),
                "confidence": f.confidence,
                "why": f.entry.why,
                "verified": f.entry.verified.isoformat(),
                "stale": f.entry.is_stale,
                "evidence": [
                    {
                        "kind": fact.kind,
                        "value": fact.value,
                        "file": _rel(fact.file, root),
                        "line": fact.line,
                        "text": fact.evidence,
                    }
                    for fact in f.cited[:20]
                ],
                "alternatives": [
                    {
                        "name": a.name,
                        "url": a.url,
                        "kind": a.kind,
                        "compat": a.compat,
                        "effort": a.effort,
                        "loses": a.full_loses,
                        "assessed": a.component_assessed,
                    }
                    for a in f.entry.alternatives
                ],
            }
            for f in findings
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def summarise(findings: list[Finding]) -> dict:
    counts = {"locked": 0, "friction": 0, "portable": 0, "unassessed": 0}
    for f in findings:
        counts[f.entry.lockin] += 1
    return {
        "dependencies_found": len(findings),
        **counts,
        "assessed": sum(1 for f in findings if f.entry.tier == "assessed"),
        "stale_assessments": sum(
            1 for f in findings if f.entry.tier == "assessed" and f.entry.is_stale
        ),
    }


def to_markdown(
    findings: list[Finding], root: Path, catalog: Catalog, packages: dict | None = None
) -> str:
    s = summarise(findings)
    out: list[str] = []
    out.append(f"# Lock-in report: `{root.name}`")
    out.append("")
    out.append(
        f"Scanned {_dt.datetime.now(_dt.UTC).date().isoformat()} (UTC) · "
        f"catalog {catalog.version} · "
        f"{len(catalog)} entries"
    )
    out.append("")
    out.append(
        f"Found **{s['dependencies_found']}** vendor dependencies: "
        f"**{s['locked']} locked**, **{s['friction']} friction**, "
        f"**{s['portable']} portable**, **{s['unassessed']} not yet assessed**."
    )
    out.append("")

    packages = packages or {}
    notable = [f for f in packages.values() if f.is_notable]
    if notable:
        out.append("**Checked against the registries just now:**")
        out.append("")
        for f in notable:
            what = f.deprecated or "yanked"
            out.append(f"- `{f.name}` ({f.ecosystem}) — {what}")
        out.append("")

    if not findings:
        out.append(
            "No catalogued vendor dependencies found. That means one of two things: "
            "this codebase is clean, or the catalog does not cover its stack yet."
        )
        return "\n".join(out)

    assessed = [f for f in findings if f.entry.tier == "assessed"]
    detected = [f for f in findings if f.entry.tier == "detected"]

    if assessed:
        out.append("| Dependency | Lock-in | Confidence | Best open source replacement | Effort |")
        out.append("|---|---|---|---|---|")
        for f in assessed:
            best = min(
                f.entry.alternatives, key=lambda a: ("low", "medium", "high").index(a.effort)
            )
            out.append(
                f"| {f.entry.name} | {LOCKIN_LABEL[f.entry.lockin]} | {f.confidence} "
                f"| {best.name} | {best.effort} |"
            )
        out.append("")

    if detected:
        out.append(f"### Also found, not assessed ({len(detected)})")
        out.append("")
        out.append(
            "Real dependencies with no verified assessment behind them. Nobody here has "
            "run these, so there is no lock-in label and no recommendation — only that "
            "they are present and what they bind you through. That is still worth "
            "knowing; it is where to look next."
        )
        out.append("")
        out.append("| Dependency | Binds you through | Found in |")
        out.append("|---|---|---|")
        for f in detected:
            where = f"`{_rel(f.cited[0].file, root)}:{f.cited[0].line}`" if f.cited else "—"
            binds = ", ".join(f.entry.binding) or "—"
            out.append(f"| {f.entry.name} | {binds} | {where} |")
        out.append("")

    for f in assessed:
        e = f.entry
        out.append(f"## {e.name}")
        out.append("")
        out.append(f"**{LOCKIN_LABEL[e.lockin]}** — {LOCKIN_BLURB[e.lockin]}")
        out.append("")
        out.append(e.why)
        out.append("")
        if e.assessment:
            a = e.assessment
            out.append(
                f"Assessed I{a.interface} · D{a.data} · B{a.behaviour} "
                f"(interface · data · behaviour — see METHODOLOGY.md)."
            )
            out.append("")
        if e.escalates_when:
            out.append(f"**Escalates if:** {e.escalates_when}")
            out.append("")
        if e.binding:
            out.append(f"Binds you through: {', '.join(e.binding)}.")
            out.append("")

        out.append(f"**Found in** ({f.confidence} confidence, {len(f.cited)} locations)")
        out.append("")
        for fact in f.cited[:6]:
            out.append(f"- `{_rel(fact.file, root)}:{fact.line}` — `{fact.evidence[:120]}`")
        if len(f.cited) > 6:
            out.append(f"- …and {len(f.cited) - 6} more")
        out.append("")

        for (eco, name), fact in sorted(packages.items()):
            if not any(fact_.value == name for fact_ in f.facts):
                continue
            if fact.error:
                line = f"registry unreachable ({fact.error})"
            else:
                line = f"latest {fact.latest}"
                if fact.last_published:
                    line += f", published {fact.last_published}"
                if fact.deprecated:
                    line += f" — DEPRECATED: {fact.deprecated}"
                if fact.yanked:
                    line += " — YANKED"
            out.append(f"*{name} ({eco}): {line}*")
            out.append("")

        out.append("**Open source replacements**")
        out.append("")
        for a in e.alternatives:
            title = f"[{a.name}]({a.url})" if a.url else a.name
            unassessed = (
                "  ⚠️ not assessed — nobody here has run it" if not a.component_assessed else ""
            )
            out.append(f"- **{title}** — {a.kind}, migration effort: {a.effort}{unassessed}")
            out.append(f"  - Compatibility: {a.compat}")
            out.append(f"  - What you lose: {a.full_loses}")
        out.append("")

        stale = "  ⚠️ older than 6 months — re-verify before relying on it" if e.is_stale else ""
        out.append(
            f"*Assessment verified {e.verified.isoformat()} ({e.age_days} days ago).{stale}*"
        )
        out.append("")

    out.append("---")
    out.append("")
    out.append(
        "This report describes dependencies, not verdicts. Every alternative above lists what "
        "you give up, because a migration recommendation that names no cost is not an assessment."
    )
    return "\n".join(out)
