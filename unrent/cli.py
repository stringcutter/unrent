"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import yaml

from .catalog import CatalogError, load_catalog
from .detect import collect_facts, match
from .discover import scan_unknown
from .render import to_json, to_markdown
from .terminal import Progress, to_terminal, wants_colour, why


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
    sub = p.add_subparsers(dest="command", metavar="{scan,catalog,mcp}")

    scan = sub.add_parser("scan", help="scan a directory")
    scan.add_argument("path", nargs="?", default=".", help="directory to scan (default: .)")
    scan.add_argument(
        "--format",
        "-f",
        choices=("auto", "terminal", "markdown", "json"),
        default="auto",
        help="auto (default): the terminal view on a terminal, the Markdown report in a "
        "pipe or file; json has every finding and alternative",
    )
    scan.add_argument(
        "--why",
        metavar="SERVICE",
        help="every location behind one service or open source component, by id or name, "
        "and what replaces it",
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
        help="alternatives listed per kind in markdown and --why (default: 3)",
    )
    scan.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG, help=argparse.SUPPRESS)

    cat = sub.add_parser("catalog", help="list what the catalog covers")
    cat.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG, help=argparse.SUPPRESS)
    cat.add_argument("--validate", action="store_true", help="validate and exit")

    sub.add_parser(
        "mcp",
        help="run as an MCP server over stdio, for coding agents (needs the mcp extra)",
        description="Serve scan, alternatives, standing and catalog as MCP tools over "
        "stdio. Rankings are fetched from the unrent repository (cached for six hours; "
        "UNRENT_OFFLINE=1 keeps the shipped ones). The scanned code never leaves the "
        "machine.",
    )
    return p


def _output_problem(output: Path) -> str | None:
    """Checked before the scan, so a typo costs nothing."""
    if output.is_dir():
        return f"{output} is a directory"
    if not output.resolve().parent.is_dir():
        return f"{output.parent} does not exist"
    return None


def _quote(arg: str) -> str:
    """As the user's shell would want it in a hint they copy."""
    return subprocess.list2cmdline([arg]) if os.name == "nt" else shlex.quote(arg)


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
    fmt = args.format
    if args.why and fmt in ("markdown", "json"):
        print(f"unrent: --why prints text; drop --format {fmt}", file=sys.stderr)
        return 2
    if fmt == "auto":
        fmt = "terminal" if not args.output and sys.stdout.isatty() else "markdown"
    catalog = load_catalog(args.catalog)
    skipped: list[Path] = []
    unknown: list[dict] = []
    progress = Progress(enabled=None if fmt == "terminal" or args.why else False)
    try:
        facts = collect_facts(
            root, catalog, exclude, skipped, skip_tests=args.skip_tests, progress=progress.files
        )
        findings = match(facts, catalog)
        if not args.why:
            progress.show("looking for AI APIs the catalog does not know")
            unknown = scan_unknown(root, catalog, exclude, skip_tests=args.skip_tests)
    finally:
        progress.done()
    colour = wants_colour(sys.stdout) and not args.output
    if args.why:
        wanted = args.why.lower()
        hit = next(
            (f for f in findings if wanted in (f.service.id.lower(), f.service.name.lower())), None
        )
        if hit is None:
            ids = ", ".join(f.service.id for f in findings) or "none"
            print(f"unrent: {args.why} is not among what was found ({ids})", file=sys.stderr)
            return 2
        text = why(hit, root, catalog, top=args.top, colour=colour)
    elif fmt == "json":
        text = to_json(findings, root, catalog, skipped, unknown)
    elif fmt == "terminal":
        text = to_terminal(
            findings,
            root,
            catalog,
            command=f"unrent {_quote(args.path)}",
            width=shutil.get_terminal_size((100, 24)).columns,
            colour=colour,
            skipped=skipped,
            unknown=unknown,
        )
    else:
        text = to_markdown(findings, root, catalog, top=args.top, skipped=skipped, unknown=unknown)
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
        print(f"  {project.repo:<{width}} {' and '.join(project.kind)} · {pools}")
    return 0


MCP_INSTALL = "uv tool install 'unrent[mcp] @ git+https://github.com/stringcutter/unrent'"


def cmd_mcp(args) -> int:
    try:
        from .server import run
    except ImportError as exc:
        if not (exc.name or "").startswith("mcp"):
            raise
        print(f"unrent: the MCP server needs the mcp extra: {MCP_INSTALL}", file=sys.stderr)
        return 2
    run()
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy code page; the report is UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    argv = sys.argv[1:] if argv is None else list(argv)
    # `unrent .` is `unrent scan .`: anything that is not a command or a top-level flag.
    if argv and argv[0] not in ("scan", "catalog", "mcp", "-h", "--help", "--version"):
        argv.insert(0, "scan")
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return 2
    try:
        if args.command == "scan":
            return cmd_scan(args)
        if args.command == "mcp":
            return cmd_mcp(args)
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
