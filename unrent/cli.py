"""Command line entry point."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import traceback
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import yaml

from . import fresh
from .catalog import CatalogError, load_catalog
from .detect import LEFT_OUT, collect_facts, file_root, left_out, match
from .discover import scan_unknown
from .render import _split, to_json, to_markdown, to_sarif, today
from .retired import _rel, replacement, snaps, state
from .terminal import Progress, to_terminal, wants_colour, why, why_model


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


def _date(value: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from None


def _positive(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


FAIL_ON = ("snapped", "snaps", "closed")
FAILED = 1  # --fail-on found what it was asked to; 2 is a usage or catalog error


def _fail_on(value: str) -> list[str]:
    picked = [v.strip() for v in value.split(",") if v.strip()]
    if not picked or any(v not in FAIL_ON for v in picked):
        raise argparse.ArgumentTypeError(f"expected one or more of {', '.join(FAIL_ON)}")
    return picked


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="unrent",
        description="See which AI services a codebase depends on, what open source can "
        "replace them, and which models are about to stop working.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    sub = p.add_subparsers(dest="command", metavar="{scan,catalog,mcp,hook}")

    scan = sub.add_parser(
        "scan",
        help="scan a directory or one file",
        description="Scan a directory or one file for closed AI services, the open source AI "
        "it runs and model ids that are retiring. Model retirements are fetched from the "
        "unrent repository (cached for six hours; UNRENT_OFFLINE=1 keeps the shipped ones). "
        "The scanned code never leaves the machine. Exits 1 when --fail-on finds what it "
        "names, 2 on a usage or catalog error.",
    )
    scan.add_argument("path", nargs="?", default=".", help="directory or file to scan (default: .)")
    scan.add_argument(
        "--format",
        "-f",
        choices=("auto", "terminal", "markdown", "json", "sarif"),
        default="auto",
        help="auto (default): the terminal view on a terminal, the Markdown report in a "
        "pipe or file; json has every finding and alternative; sarif is for code scanning "
        "(GitHub annotates the lines)",
    )
    scan.add_argument(
        "--why",
        metavar="SERVICE",
        help="every location behind one service, open source component or retiring model "
        "id, and what replaces it",
    )
    scan.add_argument(
        "--as-of",
        type=_date,
        metavar="YYYY-MM-DD",
        help="judge model retirements as of this day instead of today",
    )
    scan.add_argument(
        "--fail-on",
        type=_fail_on,
        action="extend",
        default=[],
        metavar="WHAT",
        help="exit 1 when the code selects a model that is retired (snapped) or has a "
        "retirement date (snaps, retired ones included), or depends on any closed AI "
        "service (closed); a comma list or repeated. The report is written as usual",
    )
    scan.add_argument(
        "--within",
        type=_positive,
        metavar="DAYS",
        help="with --fail-on snaps: only retirements due within DAYS of today (or --as-of)",
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

    hook = sub.add_parser(
        "hook",
        help="tell a coding agent when it writes a model id that is retiring",
        description="A PostToolUse hook for Write, Edit and NotebookEdit: reads the hook's JSON "
        "on stdin and, when the text just written puts a retired or retiring model id on a "
        "line that selects it, prints the hook's JSON with the date and what to use "
        "instead. Prints nothing otherwise, and never fails the edit. Retirements are fetched "
        "from the unrent repository at most every six hours (UNRENT_OFFLINE=1 keeps the "
        "shipped ones); nothing about the code leaves the machine.",
    )
    hook.add_argument(
        "--as-of",
        type=_date,
        metavar="YYYY-MM-DD",
        help="judge model retirements as of this day instead of today",
    )
    hook.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG, help=argparse.SUPPRESS)
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
    only = scanned = None
    if root.is_file():
        only, root = root, file_root(root, Path.cwd())
        scanned = only.relative_to(root).as_posix()
    elif not root.is_dir():
        print(f"unrent: {root} is not a file or directory", file=sys.stderr)
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
    if only and left_out(root, only, exclude, args.skip_tests):
        print(f"unrent: {args.path} {LEFT_OUT}", file=sys.stderr)
        return 2
    fmt = args.format
    if args.within and "snaps" not in args.fail_on:
        print("unrent: --within needs --fail-on snaps", file=sys.stderr)  # exit 2: usage
        return 2
    if args.why and fmt in ("markdown", "json", "sarif"):
        print(f"unrent: --why prints text; drop --format {fmt}", file=sys.stderr)
        return 2
    if fmt == "auto":
        fmt = "terminal" if not args.output and sys.stdout.isatty() else "markdown"
    catalog = load_catalog(args.catalog)
    catalog.retirements = fresh.retirements(catalog).data
    skipped: list[Path] = []
    unknown: list[dict] = []
    progress = Progress(enabled=None if fmt == "terminal" or args.why else False)
    try:
        facts = collect_facts(
            root,
            catalog,
            exclude,
            skipped,
            skip_tests=args.skip_tests,
            progress=progress.files,
            only=only,
        )
        findings = match(facts, catalog, only)
        if not args.why:
            progress.show("looking for AI APIs the catalog does not know")
            unknown = scan_unknown(root, catalog, exclude, skip_tests=args.skip_tests, only=only)
    finally:
        progress.done()
    colour = wants_colour(sys.stdout) and not args.output
    if args.why:
        wanted = args.why.lower()
        hit = next(
            (f for f in findings if wanted in (f.service.id.lower(), f.service.name.lower())), None
        )
        picked = [s for s in snaps(findings, catalog, root) if s.sites]
        model = next((s for s in picked if s.id.lower() == wanted), None)
        if hit is not None:
            text = why(hit, root, catalog, top=args.top, colour=colour)
        elif model is not None:
            text = why_model(model, root, catalog, as_of=args.as_of, colour=colour)
        else:
            ids = ", ".join([f.service.id for f in findings] + [s.id for s in picked]) or "none"
            print(f"unrent: {args.why} is not among what was found ({ids})", file=sys.stderr)
            return 2
    elif fmt == "sarif":
        # Paths from the repository root, which is what GitHub maps them to.
        base = next((d for d in (root, *root.parents) if (d / ".git").exists()), root)
        text = to_sarif(findings, root, catalog, _version(), base, args.as_of or today())
    elif fmt == "json":
        text = to_json(findings, root, catalog, skipped, unknown, args.as_of, scanned)
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
            as_of=args.as_of,
            scanned=scanned,
        )
    else:
        text = to_markdown(
            findings,
            root,
            catalog,
            top=args.top,
            skipped=skipped,
            unknown=unknown,
            as_of=args.as_of,
            scanned=scanned,
        )
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(text)
    failed = _failures(args, findings, catalog, root)
    if failed:
        print(f"unrent: --fail-on {','.join(args.fail_on)}: {'; '.join(failed)}", file=sys.stderr)
        return FAILED
    return 0


def _failures(args, findings, catalog, root: Path) -> list[str]:
    """What the scan found that --fail-on names, one phrase each."""
    out = []
    as_of = args.as_of or today()
    due = as_of + datetime.timedelta(days=args.within or 0)
    if {"snapped", "snaps"} & set(args.fail_on):
        for s in snaps(findings, catalog, root):
            r = s.retirement
            if not s.sites:
                continue
            if state(r, as_of) == "snapped":
                out.append(f"{r.id} was retired on {r.retires.isoformat()}")
            elif "snaps" in args.fail_on and (not args.within or r.retires <= due):
                out.append(f"{r.id} retires on {r.retires.isoformat()}")
    if "closed" in args.fail_on:
        out += [f"depends on {f.service.name}" for f in _split(findings).closed]
    return out


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


HOOK_TIMEOUT = 2  # seconds the hook waits for the latest retirements, once per fresh.MAX_AGE


def _retiring(catalog_dir: Path, latest: dict | None) -> re.Pattern:
    """Any model id in the shipped retirements.yaml or the latest one, as a whole id;
    read without the rest of the catalog. `gpt-4` is not in `gpt-4o` nor `ada` in
    `metadata`; a Bedrock Region may precede one (`us.anthropic...`)."""
    text = (catalog_dir / "retirements.yaml").read_text("utf-8")
    raw = yaml.load(text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    ids = {
        re.escape(str(m))
        for r in (raw, latest or {"vendors": {}})
        for v in (*r["vendors"].values(), *(r.get("platforms") or {}).values())
        if isinstance(v, dict) and isinstance(v.get("models"), dict)
        for m in v["models"]
        if str(m)
    }
    return re.compile(rf"(?<![\w.-])(?:[a-z-]+\.)?({'|'.join(sorted(ids))})(?![\w.-])")


def hook_context(event: dict, catalog_dir: Path, as_of: datetime.date | None = None) -> str:
    """What to tell the agent about the file it just wrote: the retiring ids the file
    selects (as `models_retiring` would list them) that the written text contains, so
    an id already in the file is reported once, when it was written."""
    tool = event["tool_input"]
    # Write, Edit, NotebookEdit.
    parts = (tool.get("content"), tool.get("new_string"), tool.get("new_source"))
    written = "\n".join(p for p in parts if isinstance(p, str))
    # Most edits name no retiring model: answer those before loading the catalog.
    latest = fresh.latest_retirements(HOOK_TIMEOUT)
    hits = set(_retiring(catalog_dir, latest[0]).findall(written))
    if not hits:
        return ""
    cwd = Path(event.get("cwd") or ".").resolve()
    path = (cwd / (tool.get("file_path") or tool["notebook_path"])).resolve()
    if not path.is_file():
        return ""
    root = file_root(path, cwd)
    catalog = load_catalog(catalog_dir)
    catalog.retirements = fresh.retirements(catalog, latest).data
    findings = match(collect_facts(root, catalog, only=path), catalog, path)
    out = []
    for s in snaps(findings, catalog, root):
        if not s.sites or s.id not in hits:
            continue
        r = s.retirement
        where = ", ".join(f"{_rel(x.file, root)}:{x.line}" for x in s.sites)
        verb = "selects" if len(s.sites) == 1 else "select"
        if state(r, as_of or today()) == "snapped":
            when = f"was retired on {r.retires.isoformat()}: requests fail now."
        else:
            when = f"retires on {r.retires.isoformat()}."
        use = replacement(r, catalog)
        out.append(
            f"unrent: {where} {verb} {r.id}, which {when}"
            + (f" Use {use} instead." if use else "")
            + f" Source: {r.url}"
        )
    return "\n".join(out)


def cmd_hook(args) -> int:
    # A failing hook would put an error next to the agent's edit: bad input, a file
    # that is gone or binary, a broken catalog all end in silence on stdout.
    try:
        event = json.loads(sys.stdin.buffer.read())
        context = hook_context(event, args.catalog, args.as_of)
    except Exception:
        traceback.print_exc()
        return 0
    if context:
        output = {"hookEventName": "PostToolUse", "additionalContext": context}
        print(json.dumps({"hookSpecificOutput": output}))
    return 0


MCP_INSTALL = "uv tool install 'unrent[mcp]'"


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
    if argv and argv[0] not in ("scan", "catalog", "mcp", "hook", "-h", "--help", "--version"):
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
        if args.command == "hook":
            return cmd_hook(args)
        return cmd_catalog(args)
    except CatalogError as exc:
        print(f"unrent: catalog error: {exc}", file=sys.stderr)
    except OSError as exc:
        print(f"unrent: {exc}", file=sys.stderr)
    except Exception:
        # Exit 1 means --fail-on found what it names; a crash must not look like that.
        traceback.print_exc()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
