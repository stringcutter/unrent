"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

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
        description="Find the closed AI services a codebase depends on, and the open source "
        "alternatives that replace them.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="scan a directory")
    scan.add_argument("path", nargs="?", default=".", help="directory to scan (default: .)")
    scan.add_argument("--format", choices=("markdown", "json"), default="markdown")
    scan.add_argument("--output", "-o", type=Path, help="write to a file instead of stdout")
    scan.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="skip paths matching a .gitignore-style pattern (repeatable; "
        "a .lockinignore file in the directory works the same way)",
    )
    scan.add_argument(
        "--skip-tests",
        action="store_true",
        help="leave out test, spec and fixture code (by default it is scanned, and "
        "services found only there are marked)",
    )
    scan.add_argument(
        "--top", type=int, default=3, help="alternatives shown per kind in markdown (default: 3)"
    )
    scan.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG, help=argparse.SUPPRESS)

    cat = sub.add_parser("catalog", help="list what the catalog covers")
    cat.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG, help=argparse.SUPPRESS)
    cat.add_argument("--validate", action="store_true", help="validate and exit")
    return p


def cmd_scan(args) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"lockin: {root} is not a directory", file=sys.stderr)
        return 2
    catalog = load_catalog(args.catalog)
    skipped: list[Path] = []
    facts = collect_facts(root, catalog, args.exclude, skipped, skip_tests=args.skip_tests)
    findings = match(facts, catalog)
    if args.format == "json":
        text = to_json(findings, root, catalog, skipped)
    else:
        text = to_markdown(findings, root, catalog, top=max(args.top, 1), skipped=skipped)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


def cmd_catalog(args) -> int:
    catalog = load_catalog(args.catalog)
    ranked = catalog.rankings_date or "never"
    print(
        f"{len(catalog)} closed AI services in {len(catalog.categories)} categories, "
        f"{len(catalog.pools)} pools of open source alternatives (ranked {ranked})"
    )
    if args.validate:
        return 0
    for category in catalog.categories:
        print(f"\n{category}")
        for service in (s for s in catalog.services if s.category == category):
            pools = ", ".join(catalog.pools[p].name for p in service.replace_with)
            print(f"  {service.name:<44} → {pools}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy code page; the report is UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        if args.command == "scan":
            return cmd_scan(args)
        return cmd_catalog(args)
    except CatalogError as exc:
        print(f"lockin: catalog error: {exc}", file=sys.stderr)
    except yaml.YAMLError as exc:
        print(f"lockin: could not parse the catalog: {exc}", file=sys.stderr)
    except OSError as exc:
        print(f"lockin: {exc}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
