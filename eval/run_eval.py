#!/usr/bin/env python3
"""Golden-corpus evaluation for unrent.

Clones every repo in corpus.yaml at its pinned commit (cached), runs `unrent scan
--format json` on it, and compares the services found with the hand-labelled truth.

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


def pr(tp: int, fp: int, fn: int) -> tuple[float, float]:
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    return p, r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--corpus", type=Path, default=HERE / "corpus.yaml")
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
    ap.add_argument("--quiet", action="store_true", help="only the summary")
    ap.add_argument(
        "--results-json", type=Path, default=None, help="also write per-repo scores as JSON here"
    )
    args = ap.parse_args(argv)

    corpus = yaml.safe_load(args.corpus.read_text(encoding="utf-8"))
    repos = corpus["repos"]
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
    results = []
    for repo in repos:
        report_file = out_dir / f"{repo['name']}.json"
        if args.reuse and report_file.is_file():
            report = json.loads(report_file.read_text(encoding="utf-8"))
        else:
            src = checkout(repo, args.cache)
            report = run_unrent(src.resolve(), report_file.resolve(), unrent_cmd, unrent_cwd)
        results.append(score(repo, report))

    # ---------------------------------------------------------------- per repo
    tot = Counter()
    per_service = defaultdict(Counter)
    print(f"{'repo':50} {'TP':>3} {'FP':>3} {'FN':>3}  details")
    for s in results:
        tot.update(
            tp=len(s["tp"]),
            fp=len(s["fp"]),
            fn=len(s["fn"]),
            unc_hit=len(s["unc_hit"]),
            unc_miss=len(s["unc_miss"]),
        )
        for i in s["tp"]:
            per_service[i]["tp"] += 1
        for i in s["fp"]:
            per_service[i]["fp"] += 1
        for i in s["fn"]:
            per_service[i]["fn"] += 1
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
        if not args.quiet:
            for i in sorted(s["fp"]):
                for e in s["evidence"][i]:
                    print(
                        f"    FP {i}: {e['file']}:{e['line']} [{e['kind']}={e['value']}] {e['text'][:120]}"
                    )
            for i in sorted(s["fn"]):
                print(
                    f"    FN {i}: truth: {str((repo_by_name[s['name']].get('evidence') or {}).get(i, ''))[:200]}"
                )

    # ---------------------------------------------------------------- per service
    if not args.quiet:
        print(f"\n{'service':32} {'TP':>3} {'FP':>3} {'FN':>3}")
        for sid, c in sorted(
            per_service.items(), key=lambda kv: (-(kv[1]["fp"] + kv[1]["fn"]), kv[0])
        ):
            print(f"{sid:32} {c['tp']:>3} {c['fp']:>3} {c['fn']:>3}")

    # ---------------------------------------------------------------- summary
    p, r = pr(tot["tp"], tot["fp"], tot["fn"])
    ps, rs = pr(tot["tp"] + tot["unc_hit"], tot["fp"], tot["fn"] + tot["unc_miss"])
    n_truth = sum(len(s["expected"]) for s in results)
    print(
        f"\nrepos={len(results)} expected services={n_truth} uncertain={sum(len(s['uncertain']) for s in results)}"
    )
    print(
        f"lenient (uncertain ignored):   TP={tot['tp']} FP={tot['fp']} FN={tot['fn']}  "
        f"precision={p:.3f} recall={r:.3f}"
    )
    print(
        f"strict (uncertain = expected): TP={tot['tp'] + tot['unc_hit']} FP={tot['fp']} "
        f"FN={tot['fn'] + tot['unc_miss']}  precision={ps:.3f} recall={rs:.3f}"
    )

    if args.results_json:
        args.results_json.write_text(
            json.dumps(
                [
                    {k: sorted(v) if isinstance(v, set) else v for k, v in s.items()}
                    for s in results
                ],
                indent=1,
            ),
            encoding="utf-8",
        )

    ok = True
    if args.min_precision is not None and p < args.min_precision:
        print(f"FAIL: precision {p:.3f} < {args.min_precision}")
        ok = False
    if args.min_recall is not None and r < args.min_recall:
        print(f"FAIL: recall {r:.3f} < {args.min_recall}")
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
