#!/usr/bin/env python3
"""Golden-corpus evaluation for unrent.

Clones every repo in corpus.yaml at its pinned commit (cached), runs `unrent scan
--format json` on it, and compares what it found with the hand-labelled truth, on
three sides:

  closed       the closed services in the report's `found` list, against `expected`
  open source  the projects in the report's `open_source` list (by `repo`), against
               `open_source_expected` / `open_source_uncertain` /
               `open_source_tests_only`. Only repos that carry an
               `open_source_expected` list (possibly empty) are scored on this side.
  models       the ids in the report's `models_retiring` list, against `selects` /
               `uncertain` in corpus_models.yaml (MODEL_TRUTH_RULES.md), counting
               only the ids that file says were labelled.

The repos come from corpus.yaml and corpus_oss.yaml, or with --holdout from
holdout.yaml: repos labelled the same way that unrent is never tuned against, so their
scores say how it does on code nobody fitted it to. No floors apply to them. A file's `open_source:` mapping
(repo name -> the open_source_* keys) is merged into the repo of that name, so open
source truth for an existing repo can live in its own file.

Scoring is at the service level, per repo:
  TP  expected and found          FP  found but not expected
  FN  expected but not found
Ids listed as `uncertain` in the corpus are reported but scored two ways:
  strict   uncertain ids count as expected (a miss is an FN, a hit is a TP)
  lenient  uncertain ids are ignored entirely (default headline)

Usage:
  python run_eval.py                          # all repos
  python run_eval.py --only openai__openai-quickstart-python
  python run_eval.py --gate                   # CI: lenient scores against FLOORS
  python run_eval.py --holdout                # the held-out repos

Needs git and PyYAML. unrent runs from this checkout, so no install is needed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent

# Lenient (precision, recall) per side for --gate. They sit just under the measured
# scores on 2026-10-07: closed precision 0.993, recall 0.957; open source precision
# 1.000, recall 0.902; models precision 0.933, recall 0.892 (with the platform ids).
# Raise them when the scores rise; never lower them to pass.
FLOORS = {"closed": (0.98, 0.94), "oss": (0.98, 0.88), "models": (0.93, 0.88)}


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


def run_unrent(src: Path, out: Path) -> dict:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "unrent.cli", "scan", str(src), "--format", "json", "-o", str(out)]
    # From the checkout's root, so `-m` imports this checkout's unrent; offline, so the
    # checkout's retirements.yaml is judged, not the one on main.
    env = {**os.environ, "UNRENT_OFFLINE": "1"}
    proc = subprocess.run(cmd, cwd=HERE.parent, env=env, capture_output=True, text=True)
    if proc.returncode != 0 or not out.is_file():
        raise RuntimeError(f"unrent failed on {src}: {proc.stderr.strip()[-2000:]}")
    return json.loads(out.read_text(encoding="utf-8"))


def tally(name: str, found: dict, expected: set, uncertain: set, tests_only: set | None) -> dict:
    """One repo on one side: `found` maps id -> finding; tests_only None skips that check."""
    uncertain = uncertain - expected
    got = set(found)
    return {
        "name": name,
        "expected": expected,
        "uncertain": uncertain,
        "found": got,
        "tp": expected & got,
        "fp": got - expected - uncertain,
        "fn": expected - got,
        "unc_hit": uncertain & got,
        "unc_miss": uncertain - got,
        "tests_mismatch": []
        if tests_only is None
        else sorted(
            i for i in expected & got if (i in tests_only) != bool(found[i].get("only_in_tests"))
        ),
        "evidence": {i: found[i]["evidence"][:3] for i in got},
    }


def score(repo: dict, report: dict) -> dict:
    return tally(
        repo["name"],
        {f["id"]: f for f in report["found"]},
        set(repo.get("expected") or []),
        set(repo.get("uncertain") or []),
        set(repo.get("tests_only") or []),
    )


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
    """Repos from every corpus file, in order, with the open_source: overlays merged in."""
    repos: dict[str, dict] = {}
    overlays: list[tuple[Path, dict]] = []
    for path in paths:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for entry in data.get("repos") or []:
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
    return tally(
        repo["name"],
        {o["repo"]: o for o in report.get("open_source") or []},
        set(repo.get("open_source_expected") or []),
        set(repo.get("open_source_uncertain") or []),
        set(repo.get("open_source_tests_only") or []),
    )


def load_model_truth(path: Path) -> tuple[set[str], dict[str, dict]]:
    """corpus_models.yaml: the labelled ids and, per repo, `selects` and `uncertain`."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return set(data.get("labelled") or []), data.get("repos") or {}


def score_models(name: str, report: dict, labelled: set[str], truth: dict) -> dict:
    """The models side: retiring ids the code selects, by id."""
    found = {
        m["id"]: {"evidence": [{**e, "kind": "model", "value": m["id"]} for e in m["evidence"]]}
        for m in report.get("models_retiring") or []
        if m["id"] in labelled
    }
    return tally(
        name,
        found,
        set(truth.get("selects") or []) & labelled,
        set(truth.get("uncertain") or []) & labelled,
        None,
    )


def pr(tp: int, fp: int, fn: int) -> tuple[float, float]:
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    return p, r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--side",
        choices=("closed", "oss", "models", "all"),
        default="all",
        help="what to score (default: all)",
    )
    ap.add_argument(
        "--cache",
        type=Path,
        default=Path(os.environ.get("UNRENT_EVAL_CACHE", HERE / ".cache")),
        help="where repos are cloned (env UNRENT_EVAL_CACHE)",
    )
    ap.add_argument("--only", nargs="*", help="repo names to evaluate")
    ap.add_argument(
        "--reuse", action="store_true", help="reuse existing JSON reports instead of rescanning"
    )
    ap.add_argument(
        "--gate", action="store_true", help="fail when a scored side is under its FLOORS"
    )
    ap.add_argument("--quiet", action="store_true", help="only the summary")
    ap.add_argument("--holdout", action="store_true", help="score holdout.yaml instead (no floors)")
    args = ap.parse_args(argv)
    if args.holdout and args.gate:
        ap.error("the holdout has no floors; --gate checks the corpus")

    files = ["holdout.yaml"] if args.holdout else ["corpus.yaml", "corpus_oss.yaml"]
    repos = load_corpus([HERE / f for f in files])
    if args.only:
        repos = [r for r in repos if r["name"] in set(args.only)]
    out_dir = args.cache / "_reports"

    repo_by_name = {r["name"]: r for r in repos}
    models_truth = HERE / "corpus_models.yaml"
    labelled, model_truth = load_model_truth(models_truth)
    results, oss_results, model_results = [], [], []
    for repo in repos:
        report_file = out_dir / f"{repo['name']}.json"
        if args.reuse and report_file.is_file():
            report = json.loads(report_file.read_text(encoding="utf-8"))
        else:
            src = checkout(repo, args.cache)
            report = run_unrent(src.resolve(), report_file.resolve())
        results.append(score(repo, report) if "expected" in repo else None)
        oss_results.append(score_oss(repo, report))
        if repo["name"] in model_truth:
            model_results.append(
                score_models(repo["name"], report, labelled, model_truth[repo["name"]])
            )

    ok = True
    if args.side in ("closed", "all"):
        closed = [s for s in results if s is not None]
        p, r = print_side(
            "closed services", "service", closed, repo_by_name, "evidence", args.quiet
        )
        ok &= not args.gate or gate("closed", p, r)
    if args.side in ("oss", "all"):
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
            ok &= not args.gate or gate("oss", p, r)
        else:
            print("\nno repo in the corpus has open source truth (open_source_expected)")
            ok &= not args.gate
    if args.side in ("models", "all"):
        if model_results:
            p, r = print_side("models the code selects", "model", model_results, {}, "", args.quiet)
            ok &= not args.gate or gate("models", p, r)
        else:
            print(f"\nno model truth for these repos in {models_truth.name}")
            ok &= not args.gate
    return 0 if ok else 1


def gate(side: str, precision: float, recall: float) -> bool:
    """The side's lenient scores against its FLOORS."""
    ok = True
    for label, value, minimum in zip(
        ("precision", "recall"), (precision, recall), FLOORS[side], strict=True
    ):
        if value < minimum:
            print(f"FAIL: {side} {label} {value:.3f} < {minimum}")
            ok = False
    return ok


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
                truth = (repo_by_name.get(s["name"], {}).get(evidence_key) or {}).get(i, "")
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
