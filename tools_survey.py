#!/usr/bin/env python3
"""Catalog health: is every component still alive, and is its licence what we claim.

Inclusion in this catalog should rest on evidence, not on which projects someone
happens to recall. This fetches that evidence.

Two backends, because networks differ:

  git  (default)  — shallow, blob-less clones. Gives last-commit date and the
                    licence header. Needs only git access to the host, which is
                    usually allowed where the API is not.
  api  (--api)    — the GitHub API, which additionally gives stars and the
                    archived flag. Blocked on some corporate networks (403), and
                    rate-limited to 60/hour without a token.

    python tools_survey.py
    python tools_survey.py --api --token "$GITHUB_TOKEN"

Exit code 1 if anything looks wrong, so it can run in CI.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
CATALOG = HERE / "catalog" / "components.yaml"

# A project with no commits in this long is not necessarily dead, but it is a
# question worth answering before recommending it to anyone.
QUIET_DAYS = 180
LICENSE_FILES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "COPYING.md")


def github_path(url: str) -> str | None:
    if "github.com/" not in url:
        return None
    return url.split("github.com/", 1)[1].strip("/").removesuffix(".git")


def probe_git(path: str, workdir: Path) -> dict:
    """Shallow, blob-less clone: commit metadata without downloading the tree."""
    target = workdir / path.replace("/", "_")
    clone = subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--filter=blob:none",
            "--no-checkout",
            "-q",
            f"https://github.com/{path}.git",
            str(target),
        ],
        capture_output=True,
        timeout=300,
    )
    if clone.returncode != 0:
        return {"error": clone.stderr.decode(errors="replace").strip()[:80]}

    date = subprocess.run(
        ["git", "-C", str(target), "log", "-1", "--format=%cI"],
        capture_output=True,
        text=True,
    ).stdout.strip()[:10]

    licence = ""
    for name in LICENSE_FILES:
        show = subprocess.run(
            ["git", "-C", str(target), "show", f"HEAD:{name}"],
            capture_output=True,
            text=True,
        )
        if show.returncode == 0:
            licence = " ".join(show.stdout.split()[:10])
            break
    return {"last_commit": date, "licence_head": licence}


def probe_api(path: str, token: str | None) -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "punktools"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"https://api.github.com/repos/{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        return {
            "error": f"HTTP {e.code}"
            + (" (network blocks the API)" if e.code in (403, 407) else "")
        }
    return {
        "last_commit": d["pushed_at"][:10],
        "stars": d["stargazers_count"],
        "archived": d["archived"],
        "licence_spdx": (d.get("license") or {}).get("spdx_id"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", action="store_true", help="use the GitHub API instead of git")
    ap.add_argument("--token")
    args = ap.parse_args()

    components = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    today = dt.datetime.now(dt.UTC).date()
    rows, problems = [], []
    workdir = Path(tempfile.mkdtemp(prefix="punktools-survey-"))

    try:
        for c in components:
            url = c.get("repo", "")
            path = github_path(url)
            row = {"id": c["id"], "name": c["name"], "status": c.get("status", "assessed")}
            if not path:
                # Not a failure: model cards and docs sites are legitimate homes.
                row["note"] = f"not on GitHub ({url or 'no repo'}) — check by hand"
                rows.append(row)
                continue

            row |= probe_api(path, args.token) if args.api else probe_git(path, workdir)
            rows.append(row)

            if "error" in row:
                problems.append(f"{c['name']}: {row['error']}")
                continue
            if row.get("archived"):
                problems.append(f"{c['name']}: repository is ARCHIVED")
            if row.get("last_commit"):
                age = (today - dt.date.fromisoformat(row["last_commit"])).days
                row["days_quiet"] = age
                if age > QUIET_DAYS:
                    problems.append(f"{c['name']}: no commits for {age} days")
            declared = str(c.get("licence", ""))
            spdx = row.get("licence_spdx")
            if spdx and declared and spdx not in declared:
                problems.append(f"{c['name']}: catalog says {declared}, GitHub says {spdx}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    width = max(len(r["name"]) for r in rows)
    for r in sorted(rows, key=lambda x: x.get("days_quiet", -1), reverse=True):
        if "note" in r:
            print(f"  {r['name']:<{width}}  {r['note']}")
        elif "error" in r:
            print(f"  {r['name']:<{width}}  ERROR {r['error']}")
        else:
            stars = f"{r['stars']:>8,}  " if "stars" in r else ""
            print(f"  {r['name']:<{width}}  {r['last_commit']}  {stars}{r['days_quiet']:>4}d quiet")

    (HERE / "survey.json").write_text(json.dumps(rows, indent=1))
    print(f"\n{len(rows)} components checked, written to survey.json")

    if problems:
        print(f"\n{len(problems)} thing(s) to look at:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nNothing looks wrong.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
