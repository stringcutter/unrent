"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from . import live
from .catalog import CatalogError, load_catalog
from .detect import collect_facts, match
from .report import to_json, to_markdown


def _default_catalog() -> Path:
    """Ships inside the wheel; lives beside the package in a checkout."""
    here = Path(__file__).resolve().parent
    for candidate in (here / "catalog_data", here.parent / "catalog"):
        if candidate.is_dir():
            return candidate
    return here / "catalog_data"


DEFAULT_CATALOG = _default_catalog()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lockin",
        description=(
            "Scan a codebase for vendor dependencies and name the open source replacements."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="scan a directory")
    scan.add_argument("path", nargs="?", default=".", help="directory to scan (default: .)")
    scan.add_argument("--format", choices=("markdown", "json"), default="markdown")
    scan.add_argument("--output", "-o", type=Path, help="write to a file instead of stdout")
    scan.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    scan.add_argument(
        "--live",
        action="store_true",
        help=(
            "check the packages found against PyPI and npm for deprecation, yanks "
            "and last release. Opt-in: the default scan makes no network calls."
        ),
    )
    scan.add_argument(
        "--fail-on",
        choices=("locked", "friction", "none"),
        default="none",
        help="exit non-zero when a dependency at this level or worse is found (for CI)",
    )

    cat = sub.add_parser("catalog", help="inspect the catalog")
    cat.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    cat.add_argument("--validate", action="store_true", help="validate and exit")

    return p


def cmd_scan(args) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"lockin: {root} is not a directory", file=sys.stderr)
        return 2

    catalog = load_catalog(args.catalog)
    facts = collect_facts(root, catalog)
    findings = match(facts, catalog)

    # Upstream facts are fetched, never stored: a copy of a licence starts rotting
    # the moment it is written.
    package_facts = live.check(live.packages_in(findings)) if args.live else {}

    if args.format == "json":
        text = to_json(findings, root, catalog, package_facts)
    else:
        text = to_markdown(findings, root, catalog, package_facts)

    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(text)

    if args.fail_on != "none":
        threshold = {"locked": {"locked"}, "friction": {"locked", "friction"}}[args.fail_on]
        if any(f.entry.lockin in threshold for f in findings):
            return 1
    return 0


def cmd_catalog(args) -> int:
    catalog = load_catalog(args.catalog)
    if args.validate:
        print(
            f"catalog {catalog.version}: {len(catalog)} entries across "
            f"{len(catalog.categories)} categories"
        )
        return 0
    for category in catalog.categories:
        print(f"\n{category}")
        for entry in (e for e in catalog.entries if e.category == category):
            flag = " [STALE]" if entry.is_stale else ""
            alts = ", ".join(a.name for a in entry.alternatives)
            print(f"  {entry.lockin:<9} {entry.name:<28} → {alts}{flag}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "scan":
            return cmd_scan(args)
        if args.command == "catalog":
            return cmd_catalog(args)
    except CatalogError as exc:
        print(f"lockin: catalog error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"lockin: {exc.filename or exc}: no such file or directory", file=sys.stderr)
        return 2
    except yaml.YAMLError as exc:
        print(f"lockin: could not parse the catalog: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"lockin: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
