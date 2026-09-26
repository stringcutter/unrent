#!/usr/bin/env python3
"""Golden-corpus evaluation for unrent.

Clones every repo in corpus.yaml at its pinned commit (cached), runs `unrent scan
--format json` on it, and compares what it found with the hand-labelled truth, on
two sides:

  closed       the closed services in the report's `found` list, against `expected`
  open source  the projects in the report's `open_source` list (by `repo`), against
               `open_source_expected` / `open_source_uncertain` /
               `open_source_tests_only`. Only repos that carry an
               `open_source_expected` list (possibly empty) are scored on this side.

Several corpus files can be given (`--corpus a.yaml --corpus b.yaml`); by default
corpus.yaml plus corpus_oss.yaml next to this script when it exists. A file's
`repos:` entries are added (an entry whose name is already known is merged into it
key by key), and its `open_source:` mapping (repo name -> the open_source_* keys) is
merged into the repo of that name, so open source truth for an existing repo can
live in its own file.

Scoring is at the service level, per repo:
  TP  expected and found          FP  found but not expected
  FN  expected but not found
Ids listed as `uncertain` in the corpus are reported but scored two ways:
  strict   uncertain ids count as expected (a miss is an FN, a hit is a TP)
  lenient  uncertain ids are ignored entirely (default headline)

Usage:
  python run_eval.py                          # all repos
  python run_eval.py --only openai__openai-quickstart-python
  python run_eval.py --unrent "uv run --quiet unrent" --unrent-cwd path/to/unrent
  python run_eval.py --min-precision 0.9 --min-recall 0.9   # CI gate (lenient scores)
  python run_eval.py --side oss --min-oss-precision 0.9 --min-oss-recall 0.8

Needs git and PyYAML. When moved into unrent-dist (e.g. to eval/), the default runs
unrent in-process from the checkout via `python -m`-style import, so no install is needed.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
_INPROC = (
    "import sys; sys.path.insert(0, sys.argv.pop(1)); "
    "from unrent.cli import main; sys.exit(main(sys.argv[1:]))"
)


def find_unrent_root() -> Path | None:
    """A unrent checkout next to or above this script, if any."""
    for p in [HERE, *HERE.parents]:
        if (p / "unrent" / "cli.py").is_file() and (p / "catalog").is_dir():
            return p
    return None


def git(*args: str, cwd: Path | None = None) -> str:
    out = subprocess.run(
        ["git", "-c", "core.longpaths=true", *args], cwd=cwd, capture_output=True, text=True
    )
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {out.stderr.strip()}")
    return out.stdout.strip()


def checkout(repo: dict, cache: Path) -> Path:
    """Shallow-fetch the pinned commit into cache/<name> (idempotent)."""
    dest = cache / repo["name"]
    sha = repo["commit"]
    if (dest / ".git").is_dir():
        try:
            if git("rev-parse", "HEAD", cwd=dest) == sha:
                return dest
        except RuntimeError:
            pass
    else:
        dest.mkdir(parents=True, exist_ok=True)
        git("init", "-q", cwd=dest)
        git("config", "core.longpaths", "true", cwd=dest)
        git("config", "core.autocrlf", "false", cwd=dest)
        git("remote", "add", "origin", repo["url"], cwd=dest)
    git("fetch", "-q", "--depth", "1", "origin", sha, cwd=dest)
    git("checkout", "-q", "--force", "FETCH_HEAD", cwd=dest)
    return dest


def run_unrent(src: Path, out: Path, unrent_cmd: list[str], cwd: Path | None) -> dict:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [*unrent_cmd, "scan", str(src), "--format", "json", "-o", str(out)]
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0 or not out.is_file():
        raise RuntimeError(f"unrent failed on {src}: {proc.stderr.strip()[-2000:]}")
    return json.loads(out.read_text(encoding="utf-8"))


def score(repo: dict, report: dict) -> dict:
    found = {f["id"]: f for f in report["found"]}
    expected = set(repo.get("expected") or [])
    uncertain = set(repo.get("uncertain") or []) - expected
    got = set(found)
    return {
        "name": repo["name"],
        "expected": expected,
        "uncertain": uncertain,
        "found": got,
        "tp": expected & got,
        "fp": got - expected - uncertain,
        "fn": expected - got,
        "unc_hit": uncertain & got,
        "unc_miss": uncertain - got,
        "tests_mismatch": sorted(
            i
            for i in expected & got
            if (i in set(repo.get("tests_only") or [])) != bool(found[i].get("only_in_tests"))
        ),
        "evidence": {i: found[i]["evidence"][:3] for i in got},
    }


OSS_KEYS = (
    "open_source_expected",
    "open_source_uncertain",
    "open_source_tests_only",
    "open_source_evidence",
    "open_source_uncertain_reasons",
    "open_source_notes",
    "open_source_coverage_gaps",
)


def load_corpus(paths: list[Path]) -> list[dict]:
    """Repos from every corpus file, in order, merged by name."""
    repos: dict[str, dict] = {}
    overlays: list[tuple[Path, dict]] = []
    for path in paths:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for entry in data.get("repos") or []:
            if entry["name"] in repos:
                repos[entry["name"]].update(entry)
            else:
                repos[entry["name"]] = dict(entry)
        overlays.append((path, data.get("open_source") or {}))
    for path, overlay in overlays:
        for name, fields in overlay.items():
            if name not in repos:
                raise SystemExit(f"{path.name}: open_source truth for unknown repo '{name}'")
            unknown = set(fields) - set(OSS_KEYS)
            if unknown:
                raise SystemExit(f"{path.name}: {name}: unknown keys {sorted(unknown)}")
            repos[name].update(fields)
    return list(repos.values())


def score_oss(repo: dict, report: dict) -> dict | None:
    """The open source side, by GitHub repo id. None when the repo has no OSS truth."""
    if "open_source_expected" not in repo:
        return None
    found = {o["repo"]: o for o in report.get("open_source") or []}
    expected = set(repo.get("open_source_expected") or [])
    uncertain = set(repo.get("open_source_uncertain") or []) - expected
    tests_only = set(repo.get("open_source_tests_only") or [])
    got = set(found)
    return {
        "name": repo["name"],
        "expected": expected,
        "uncertain": uncertain,
        "found": got,
        "tp": expected & got,
        "fp": got - expected - uncertain,
        "fn": expected - got,
        "unc_hit": uncertain & got,
        "unc_miss": uncertain - got,
        "tests_mismatch": sorted(
            i for i in expected & got if (i in tests_only) != bool(found[i].get("only_in_tests"))
        ),
        "evidence": {i: found[i]["evidence"][:3] for i in got},
        "standing": {
            i: [
                {k: st.get(k) for k in ("pool", "rank", "of", "rank_among_same_kind", "same_kind")}
                for st in found[i].get("standing") or []
            ]
            for i in got
        },
    }


def pr(tp: int, fp: int, fn: int) -> tuple[float, float]:
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    return p, r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--corpus",
        type=Path,
        action="append",
        default=None,
        help="corpus file; repeatable (default: corpus.yaml, plus corpus_oss.yaml if present)",
    )
    ap.add_argument(
        "--side", choices=("closed", "oss", "both"), default="both", help="what to score"
    )
    ap.add_argument(
        "--cache",
        type=Path,
        default=Path(os.environ.get("UNRENT_EVAL_CACHE", HERE / ".cache")),
        help="where repos are cloned (env UNRENT_EVAL_CACHE)",
    )
    ap.add_argument(
        "--out", type=Path, default=None, help="where JSON reports go (default <cache>/_reports)"
    )
    ap.add_argument(
        "--unrent",
        default=None,
        help='command that runs unrent, e.g. "uv run --quiet unrent" (default: in-process from checkout)',
    )
    ap.add_argument("--unrent-cwd", type=Path, default=None)
    ap.add_argument(
        "--unrent-root",
        type=Path,
        default=None,
        help="unrent checkout to run in-process (default: the one containing this script)",
    )
    ap.add_argument("--only", nargs="*", help="repo names to evaluate")
    ap.add_argument(
        "--reuse", action="store_true", help="reuse existing JSON reports instead of rescanning"
    )
    ap.add_argument("--min-precision", type=float, default=None)
    ap.add_argument("--min-recall", type=float, default=None)
    ap.add_argument("--min-oss-precision", type=float, default=None)
    ap.add_argument("--min-oss-recall", type=float, default=None)
    ap.add_argument("--quiet", action="store_true", help="only the summary")
    ap.add_argument(
        "--results-json", type=Path, default=None, help="also write per-repo scores as JSON here"
    )
    args = ap.parse_args(argv)

    corpus_files = args.corpus or [
        p for p in (HERE / "corpus.yaml", HERE / "corpus_oss.yaml") if p.is_file()
    ]
    repos = load_corpus(corpus_files)
    if args.only:
        repos = [r for r in repos if r["name"] in set(args.only)]
    out_dir = args.out or args.cache / "_reports"

    root = args.unrent_root.resolve() if args.unrent_root else find_unrent_root()
    if args.unrent:
        unrent_cmd, unrent_cwd = shlex.split(args.unrent), args.unrent_cwd
    elif root is not None:
        unrent_cmd, unrent_cwd = [sys.executable, "-c", _INPROC, str(root)], root
    else:
        unrent_cmd, unrent_cwd = ["unrent"], args.unrent_cwd

    repo_by_name = {r["name"]: r for r in repos}
    results, oss_results = [], []
    for repo in repos:
        report_file = out_dir / f"{repo['name']}.json"
        if args.reuse and report_file.is_file():
            report = json.loads(report_file.read_text(encoding="utf-8"))
        else:
            src = checkout(repo, args.cache)
            report = run_unrent(src.resolve(), report_file.resolve(), unrent_cmd, unrent_cwd)
        results.append(score(repo, report) if "expected" in repo else None)
        oss_results.append(score_oss(repo, report))

    ok = True
    dump: dict[str, list] = {}
    if args.side in ("closed", "both"):
        closed = [s for s in results if s is not None]
        p, r = print_side(
            "closed services", "service", closed, repo_by_name, "evidence", args.quiet
        )
        dump["closed"] = closed
        ok &= gate("precision", p, args.min_precision)
        ok &= gate("recall", r, args.min_recall)
    if args.side in ("oss", "both"):
        oss = [s for s in oss_results if s is not None]
        if oss:
            p, r = print_side(
                "open source components",
                "project",
                oss,
                repo_by_name,
                "open_source_evidence",
                args.quiet,
            )
            ok &= gate("open source precision", p, args.min_oss_precision)
            ok &= gate("open source recall", r, args.min_oss_recall)
        else:
            print("\nno repo in the corpus has open source truth (open_source_expected)")
            ok &= args.min_oss_precision is None and args.min_oss_recall is None
        dump["open_source"] = oss

    if args.results_json:
        args.results_json.write_text(
            json.dumps(
                {
                    side: [
                        {k: sorted(v) if isinstance(v, set) else v for k, v in s.items()}
                        for s in rows
                    ]
                    for side, rows in dump.items()
                },
                indent=1,
            ),
            encoding="utf-8",
        )
    return 0 if ok else 1


def gate(label: str, value: float, minimum: float | None) -> bool:
    if minimum is not None and value < minimum:
        print(f"FAIL: {label} {value:.3f} < {minimum}")
        return False
    return True


def print_side(
    title: str, unit: str, results: list[dict], repo_by_name: dict, evidence_key: str, quiet: bool
) -> tuple[float, float]:
    """Per-repo table, per-item table and summary for one side; returns lenient (P, R)."""
    print(f"\n=== {title} ===")
    tot = Counter()
    per_item = defaultdict(Counter)
    print(f"{'repo':50} {'TP':>3} {'FP':>3} {'FN':>3}  details")
    for s in results:
        tot.update(
            tp=len(s["tp"]),
            fp=len(s["fp"]),
            fn=len(s["fn"]),
            unc_hit=len(s["unc_hit"]),
            unc_miss=len(s["unc_miss"]),
        )
        for key in ("tp", "fp", "fn"):
            for i in s[key]:
                per_item[i][key] += 1
        bits = []
        if s["fp"]:
            bits.append("FP=" + ",".join(sorted(s["fp"])))
        if s["fn"]:
            bits.append("FN=" + ",".join(sorted(s["fn"])))
        if s["unc_hit"] or s["unc_miss"]:
            bits.append(
                "uncertain found="
                + ",".join(sorted(s["unc_hit"]))
                + " missed="
                + ",".join(sorted(s["unc_miss"]))
            )
        if s["tests_mismatch"]:
            bits.append("tests-only mismatch=" + ",".join(s["tests_mismatch"]))
        print(
            f"{s['name']:50} {len(s['tp']):>3} {len(s['fp']):>3} {len(s['fn']):>3}  {'; '.join(bits)}"
        )
        if not quiet:
            for i in sorted(s["fp"]):
                for e in s["evidence"][i]:
                    print(
                        f"    FP {i}: {e['file']}:{e['line']} [{e['kind']}={e['value']}] {e['text'][:120]}"
                    )
            for i in sorted(s["fn"]):
                truth = (repo_by_name[s["name"]].get(evidence_key) or {}).get(i, "")
                print(f"    FN {i}: truth: {str(truth)[:200]}")

    if not quiet:
        print(f"\n{unit:40} {'TP':>3} {'FP':>3} {'FN':>3}")
        for sid, c in sorted(
            per_item.items(), key=lambda kv: (-(kv[1]["fp"] + kv[1]["fn"]), kv[0])
        ):
            print(f"{sid:40} {c['tp']:>3} {c['fp']:>3} {c['fn']:>3}")

    p, r = pr(tot["tp"], tot["fp"], tot["fn"])
    ps, rs = pr(tot["tp"] + tot["unc_hit"], tot["fp"], tot["fn"] + tot["unc_miss"])
    print(
        f"\n{title}: repos={len(results)} expected={sum(len(s['expected']) for s in results)} "
        f"uncertain={sum(len(s['uncertain']) for s in results)}"
    )
    print(
        f"lenient (uncertain ignored):   TP={tot['tp']} FP={tot['fp']} FN={tot['fn']}  "
        f"precision={p:.3f} recall={r:.3f}"
    )
    print(
        f"strict (uncertain = expected): TP={tot['tp'] + tot['unc_hit']} FP={tot['fp']} "
        f"FN={tot['fn'] + tot['unc_miss']}  precision={ps:.3f} recall={rs:.3f}"
    )
    return p, r


if __name__ == "__main__":
    sys.exit(main())
