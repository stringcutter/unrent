"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from importlib.metadata import PackageNotFoundError, version
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


def _version() -> str:
    try:
        return version("unrent")
    except PackageNotFoundError:
        return "unknown"


def _positive(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="unrent",
        description="Find the closed AI services a codebase depends on and the open source "
        "that replaces them, and rank the open source AI it already runs.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    sub = p.add_subparsers(dest="command", metavar="{scan,catalog}")

    scan = sub.add_parser("scan", help="scan a directory")
    scan.add_argument("path", nargs="?", default=".", help="directory to scan (default: .)")
    scan.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="markdown report (default) or JSON with every finding and alternative",
    )
    scan.add_argument("--output", "-o", type=Path, help="write to a file instead of stdout")
    scan.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="skip paths matching a .gitignore-style pattern (repeatable; "
        "a .unrentignore file in the directory works the same way)",
    )
    scan.add_argument(
        "--skip-tests",
        action="store_true",
        help="leave out test, spec and fixture code (by default it is scanned, and "
        "services found only there are marked)",
    )
    scan.add_argument(
        "--top",
        type=_positive,
        default=3,
        metavar="N",
        help="alternatives listed per kind in markdown (default: 3)",
    )
    scan.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG, help=argparse.SUPPRESS)

    cat = sub.add_parser("catalog", help="list what the catalog covers")
    cat.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG, help=argparse.SUPPRESS)
    cat.add_argument("--validate", action="store_true", help="validate and exit")
    return p


def _output_problem(output: Path) -> str | None:
    """Checked before the scan, so a typo costs nothing."""
    if output.is_dir():
        return f"{output} is a directory"
    if not output.resolve().parent.is_dir():
        return f"{output.parent} does not exist"
    return None


def cmd_scan(args) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"unrent: {root} is not a directory", file=sys.stderr)
        return 2
    exclude = list(args.exclude)
    if args.output:
        problem = _output_problem(args.output)
        if problem:
            print(f"unrent: cannot write the report: {problem}", file=sys.stderr)
            return 2
        target = args.output.resolve()
        if target.is_relative_to(root):
            # Never scan the report we are about to write (or wrote last time).
            exclude.append("/" + target.relative_to(root).as_posix())
    catalog = load_catalog(args.catalog)
    skipped: list[Path] = []
    facts = collect_facts(root, catalog, exclude, skipped, skip_tests=args.skip_tests)
    findings = match(facts, catalog)
    if args.format == "json":
        text = to_json(findings, root, catalog, skipped)
    else:
        text = to_markdown(findings, root, catalog, top=args.top, skipped=skipped)
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
        f"{len(catalog.pools)} pools of open source alternatives (ranked {ranked}), "
        f"{len(catalog.projects)} open source projects recognised in code"
    )
    if args.validate:
        return 0
    width = max(len(s.name) for s in catalog.detectable) + 2
    for category in catalog.categories:
        print(f"\n{category}")
        for service in (s for s in catalog.services if s.category == category):
            pools = ", ".join(catalog.pools[p].name for p in service.replace_with)
            print(f"  {service.name:<{width}} → {pools}")
    print("\nOpen source recognised in code")
    for project in sorted(catalog.projects, key=lambda p: p.repo.lower()):
        pools = ", ".join(catalog.pools[p].name for p in project.replace_with)
        print(f"  {project.repo:<{width}} {project.kind} · {pools}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy code page; the report is UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return 2
    try:
        if args.command == "scan":
            return cmd_scan(args)
        return cmd_catalog(args)
    except CatalogError as exc:
        print(f"unrent: catalog error: {exc}", file=sys.stderr)
    except yaml.YAMLError as exc:
        print(f"unrent: could not parse the catalog: {exc}", file=sys.stderr)
    except json.JSONDecodeError as exc:
        print(f"unrent: could not parse the rankings snapshot: {exc}", file=sys.stderr)
    except OSError as exc:
        print(f"unrent: {exc}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
