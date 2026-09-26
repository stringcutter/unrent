#!/usr/bin/env python3
"""Rank every alternative pool and write catalog/rankings.json.

The scanner never touches the network. This script is where the ranking comes from,
and it runs weekly (.github/workflows/refresh.yml), so "state of the art" means what
is moving now rather than what someone remembered.

  github pools       Ranked by momentum: stars gained over the last ~90 days. GitHub
                     no longer lists stargazers with dates, and the public event
                     archives undercount since 2025, so the only trustworthy history
                     is our own: every run records each repo's exact star count in
                     catalog/star-history.json, and momentum is the difference.
                     Until a repo has four weeks of history the pool falls back to
                     total stars, and the report says so.
  huggingface pools  Open-weight models, discovered rather than listed: each listed
                     publisher's own models (not community fine-tunes or re-uploads)
                     under an open licence, ranked by Hugging Face's trending score.

    python scripts/refresh.py            # uses GITHUB_TOKEN, or `gh auth token`
    python scripts/refresh.py --check    # verify only, write nothing

Exit code 1 when a listed repository is missing, renamed, archived, inactive or not
open source, so the catalog cannot silently rot.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lockin.catalog import OPEN_LICENCES, OPEN_MODEL_LICENCES  # noqa: E402

CATALOG = ROOT / "catalog"
HISTORY = CATALOG / "star-history.json"
MOMENTUM_DAYS = 90
MIN_HISTORY_DAYS = 28
# A finished model repo is not a dead one; a year without a push is.
INACTIVE_AFTER_DAYS = 365
MODELS_PER_POOL = 10
# One lab's release week should not fill a whole list.
MODELS_PER_PUBLISHER = 2
# Re-uploads in another format are not a different model.
DERIVED_NAME = re.compile(
    r"(gguf|gptq|awq|exl2|mlx|fp8|fp4|nvfp4|int4|int8|bnb|4bit|8bit|onnx|openvino|quant)",
    re.IGNORECASE,
)


def _get(url: str, headers: dict | None = None, timeout: int = 30, attempts: int = 4):
    """GET JSON, backing off on rate limits and server errors."""
    req = urllib.request.Request(url, headers={"User-Agent": "lockin-refresh", **(headers or {})})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == attempts - 1:
                raise
        time.sleep(2 ** (attempt + 1))
    raise AssertionError("unreachable")


def _github_token() -> str | None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    if shutil.which("gh"):
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return None


# ---------------------------------------------------------------- github


def github_project(repo: str, declared_licence: str | None, token: str | None) -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    base = {"repo": repo, "url": f"https://github.com/{repo}"}
    try:
        data = _get(f"https://api.github.com/repos/{repo}", headers)
    except urllib.error.HTTPError as exc:
        return {**base, "status": "missing", "problem": f"GitHub returned {exc.code}"}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {**base, "status": "unreachable", "problem": str(exc)}

    detected = (data.get("license") or {}).get("spdx_id")
    licence = detected if detected and detected != "NOASSERTION" else declared_licence
    pushed = (data.get("pushed_at") or "")[:10]
    out = {
        **base,
        "stars": data.get("stargazers_count", 0),
        "licence": licence,
        "pushed": pushed,
        "status": "ok",
    }
    if data.get("full_name", "").lower() != repo.lower():
        out["status"], out["problem"] = "renamed", f"now {data['full_name']}"
    elif data.get("archived"):
        out["status"], out["problem"] = "archived", "archived on GitHub"
    elif pushed and (dt.date.today() - dt.date.fromisoformat(pushed)).days > INACTIVE_AFTER_DAYS:
        out["status"], out["problem"] = "inactive", f"no push since {pushed}"
    elif licence not in OPEN_LICENCES:
        out["status"], out["problem"] = "licence", f"licence {licence or 'unknown'} is not OSI-approved"
    return out


def momentum(points: list[list], today: dt.date) -> int | None:
    """Stars gained over MOMENTUM_DAYS, scaled from the best baseline we have.

    The baseline is the snapshot closest to MOMENTUM_DAYS ago that is at least
    MIN_HISTORY_DAYS old. Scaling a shorter window up keeps pools comparable while
    history is still accumulating.
    """
    if not points:
        return None
    latest_date, latest = dt.date.fromisoformat(points[-1][0]), points[-1][1]
    target = today - dt.timedelta(days=MOMENTUM_DAYS)
    candidates = [
        (dt.date.fromisoformat(d), s)
        for d, s in points
        if (latest_date - dt.date.fromisoformat(d)).days >= MIN_HISTORY_DAYS
    ]
    if not candidates:
        return None
    base_date, base = min(candidates, key=lambda c: abs((c[0] - target).days))
    days = (latest_date - base_date).days
    return round(max(latest - base, 0) * MOMENTUM_DAYS / days)


def rank_github(projects: list[dict]) -> tuple[list[dict], str]:
    """By momentum when any project has it; otherwise, and for newcomers, by stars."""
    with_momentum = [p for p in projects if p.get("stars_90d") is not None]
    if not with_momentum:
        return sorted(projects, key=lambda p: -p["stars"]), "stars"
    newcomers = [p for p in projects if p.get("stars_90d") is None]
    return (
        sorted(with_momentum, key=lambda p: (-p["stars_90d"], -p["stars"]))
        + sorted(newcomers, key=lambda p: -p["stars"]),
        "momentum",
    )


# ---------------------------------------------------------------- huggingface


def hf_publisher_models(author: str) -> list[dict]:
    query = urllib.parse.urlencode(
        [
            ("author", author),
            ("sort", "trendingScore"),
            ("limit", "200"),
            *[
                ("expand[]", f)
                for f in ("cardData", "downloads", "likes", "trendingScore", "createdAt", "pipeline_tag")
            ],
        ]
    )
    try:
        return _get(f"https://huggingface.co/api/models?{query}")
    except (urllib.error.URLError, TimeoutError, ValueError):
        return []


def _original(author: str, card: dict) -> bool:
    """A model its publisher trained, not someone's fine-tune or re-upload of it.

    An instruct model derived from the same lab's base model is original; a
    community fine-tune of that lab's model is not.
    """
    bases = card.get("base_model") or []
    if isinstance(bases, str):
        bases = [bases]
    return all(str(b).split("/", 1)[0].lower() == author.lower() for b in bases)


def hf_pool(pool: dict, by_author: dict[str, list[dict]]) -> list[dict]:
    tags = set(pool["pipeline_tags"])
    must_contain = [s.lower() for s in pool.get("name_contains", [])]
    picked = []
    for author in pool["publishers"]:
        for m in by_author.get(author, []):
            card = m.get("cardData") or {}
            licence = str(card.get("license") or "").lower()
            name = m["id"].split("/", 1)[1]
            if m.get("pipeline_tag") not in tags or not _original(author, card):
                continue
            if licence not in OPEN_MODEL_LICENCES or DERIVED_NAME.search(name):
                continue
            if must_contain and not any(s in name.lower() for s in must_contain):
                continue
            picked.append(
                {
                    "id": m["id"],
                    "url": f"https://huggingface.co/{m['id']}",
                    "licence": licence,
                    "trending": m.get("trendingScore") or 0,
                    "downloads": m.get("downloads") or 0,
                    "likes": m.get("likes") or 0,
                    "created": (m.get("createdAt") or "")[:10],
                    "pipeline_tag": m.get("pipeline_tag"),
                }
            )
    picked.sort(key=lambda m: (-m["trending"], -m["downloads"]))
    per_publisher: dict[str, int] = {}
    ranked = []
    for m in picked:
        publisher = m["id"].split("/", 1)[0]
        if per_publisher.get(publisher, 0) < MODELS_PER_PUBLISHER:
            per_publisher[publisher] = per_publisher.get(publisher, 0) + 1
            ranked.append(m)
    return ranked[:MODELS_PER_POOL]


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="verify only, do not write")
    args = ap.parse_args()

    today = dt.date.today()
    pools = yaml.safe_load((CATALOG / "alternatives.yaml").read_text("utf-8"))["pools"]
    history: dict[str, list[list]] = (
        json.loads(HISTORY.read_text("utf-8")) if HISTORY.is_file() else {}
    )
    token = _github_token()
    if not token:
        print("warning: no GitHub token; the API allows 60 requests an hour", file=sys.stderr)

    declared = {
        p["repo"]: p.get("licence")
        for pool in pools
        if pool["source"] == "github"
        for p in pool["projects"]
    }
    publishers = sorted(
        {a for pool in pools if pool["source"] == "huggingface" for a in pool["publishers"]}
    )
    with ThreadPoolExecutor(max_workers=8) as ex:
        repos = dict(
            zip(declared, ex.map(lambda r: github_project(r, declared[r], token), declared))
        )
        by_author = dict(zip(publishers, ex.map(hf_publisher_models, publishers)))

    for repo, info in repos.items():
        if "stars" not in info:
            continue
        points = [p for p in history.get(repo, []) if p[0] != today.isoformat()]
        history[repo] = [*points, [today.isoformat(), info["stars"]]]
        info["stars_90d"] = momentum(history[repo], today)

    problems = []
    out_pools: dict[str, dict] = {}
    for pool in pools:
        if pool["source"] == "github":
            entries = [{**repos[p["repo"]], "what": p["what"]} for p in pool["projects"]]
            problems += [
                f"{pool['id']}: {e['repo']} — {e['problem']}" for e in entries if e["status"] != "ok"
            ]
            ranked, ranked_by = rank_github([e for e in entries if e["status"] == "ok"])
            dropped = [e for e in entries if e["status"] != "ok"]
        else:
            ranked, ranked_by, dropped = hf_pool(pool, by_author), "trending", []
            if not ranked:
                problems.append(f"{pool['id']}: no models survived the filters")
        out_pools[pool["id"]] = {
            "name": pool["name"],
            "source": pool["source"],
            "ranked_by": ranked_by,
            "ranked": ranked,
            "dropped": dropped,
        }

    for pool_id, pool in out_pools.items():
        print(f"\n{pool_id}  (by {pool['ranked_by']})")
        for i, e in enumerate(pool["ranked"], 1):
            if pool["source"] == "github":
                gain = "" if e.get("stars_90d") is None else f"+{e['stars_90d']}"
                print(f"  {i:>2}. {e['repo']:<42} {e['stars']:>7} {gain}")
            else:
                print(f"  {i:>2}. {e['id']:<50} trend {e['trending']:>4}  dl {e['downloads']:>10}")
        for e in pool["dropped"]:
            print(f"   x  {e['repo']:<42} {e.get('problem')}")

    if problems:
        print(f"\n{len(problems)} problem(s):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)

    if not args.check:
        payload = {"generated": today.isoformat(), "momentum_days": MOMENTUM_DAYS, "pools": out_pools}
        (CATALOG / "rankings.json").write_text(
            json.dumps(payload, indent=1, ensure_ascii=False) + "\n", "utf-8"
        )
        HISTORY.write_text(
            json.dumps(dict(sorted(history.items())), indent=0, separators=(",", ":")) + "\n",
            "utf-8",
        )
        print("\nwrote catalog/rankings.json and catalog/star-history.json", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
