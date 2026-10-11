#!/usr/bin/env python3
"""Rank every alternative pool and write catalog/rankings.json.

The scanner never touches the network. This script is where the ranking comes from,
and it runs daily (.github/workflows/refresh.yml), so "state of the art" means what
is moving now rather than what someone remembered.

  github pools       Two lenses, which the scanner draws from the same snapshot:
                     established (total stars) and rising (stars gained in the last
                     30 days). GitHub no longer lists stargazers with dates, and the
                     public event archives undercount since 2025, so the only
                     trustworthy history is our own: every run records each repo's
                     exact star count in catalog/star-history.json, and the gain is
                     the difference (see `momentum` for repos too new to have it).
                     A pool holds the projects alternatives.yaml lists and the ones
                     this script found itself: a GitHub search per pool (`discover`)
                     yields candidates, Jev (TypeSafe) judges each once against the pool, and
                     the verdicts live in catalog/discovered.json. `deny` overrides.
  huggingface pools  Open-weight models, discovered rather than listed: each listed
                     publisher's own models (not community fine-tunes or re-uploads)
                     under an open licence, ranked by Hugging Face's trending score.

    python scripts/refresh.py            # GITHUB_TOKEN (or `gh auth token`); judging
                                         # new candidates needs TYPESAFE_API_KEY
    python scripts/refresh.py --check    # verify only, write nothing

Exit codes, so the workflow can tell "open a pull request that needs attention" from
"something is broken":
  0  rankings written, no problems
  3  rankings written, but a listed repository is missing, renamed, archived,
     inactive or not open source, so the catalog cannot silently rot
  4  nothing written: a pool emptied or halved, which is an outage, not news
  1  (Python's own) an unexpected crash
"""

from __future__ import annotations

import argparse
import base64
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

from unrent.catalog import KINDS, OPEN_LICENCES, OPEN_MODEL_LICENCES, load_catalog  # noqa: E402

CATALOG = ROOT / "catalog"
HISTORY = CATALOG / "star-history.json"
DISCOVERED = CATALOG / "discovered.json"
MOMENTUM_DAYS = 30
MIN_HISTORY_DAYS = 7
# A repo younger than this with no week of history yet: its average since creation.
YOUNG_DAYS = 90
# Momentum only ever looks 30 days back; older points are dead weight in the repo.
KEEP_HISTORY_DAYS = 2 * MOMENTUM_DAYS
DISCOVER_MIN_STARS = 1000
SEARCH_PAUSE = 2.1  # GitHub's search API allows 30 requests a minute
README_CHARS = 6000
JUDGE_MODEL = "jev-latest"  # TypeSafe's Jev
JUDGE_LIMIT = 1000  # per run, so a broken query cannot run up a bill
# A project can change what it is; a rejection is not forever.
REJUDGE_DAYS = 180
EXIT_PROBLEMS = 3
EXIT_REFUSED = 4
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


class Transient(Exception):
    """The source could not answer right now: rate-limited, down, or unreachable.
    Not evidence that anything changed, so the previous snapshot stands."""


MAX_RATE_LIMIT_WAIT = 120


def _rate_limited(exc: urllib.error.HTTPError) -> bool:
    return exc.code == 429 or (exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0")


def _get(
    url: str,
    headers: dict | None = None,
    timeout: int = 30,
    attempts: int = 4,
    body: dict | None = None,
):
    """GET JSON, or POST `body` as JSON. Retries rate limits (GitHub signals them as
    403 with no requests remaining) and server errors; raises Transient when they
    persist, and HTTPError only for real answers such as 404."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "unrent-refresh", **(headers or {})},
        data=json.dumps(body).encode() if body is not None else None,
    )
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if _rate_limited(exc):
                reset = int(exc.headers.get("X-RateLimit-Reset") or 0)
                wait = max(reset - time.time(), 2 ** (attempt + 1)) if reset else 2 ** (attempt + 1)
                if wait > MAX_RATE_LIMIT_WAIT or attempt == attempts - 1:
                    raise Transient(f"rate-limited ({exc.code})") from exc
                time.sleep(wait)
                continue
            if exc.code in (500, 502, 503, 504, 529):
                if attempt == attempts - 1:
                    raise Transient(f"server error {exc.code}") from exc
            else:
                raise
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            if attempt == attempts - 1:
                raise Transient(str(exc)) from exc
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


def _gh_headers(token: str | None) -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_project(repo: str, declared_licence: str | None, token: str | None) -> dict:
    base = {"repo": repo, "url": f"https://github.com/{repo}"}
    try:
        data = _get(f"https://api.github.com/repos/{repo}", _gh_headers(token))
    except urllib.error.HTTPError as exc:
        return {**base, "status": "missing", "problem": f"GitHub returned {exc.code}"}
    except Transient as exc:
        return {**base, "status": "unreachable", "problem": str(exc)}

    detected = (data.get("license") or {}).get("spdx_id")
    licence = detected if detected and detected != "NOASSERTION" else declared_licence
    pushed = (data.get("pushed_at") or "")[:10]
    out = {
        **base,
        "stars": data.get("stargazers_count", 0),
        "licence": licence,
        "pushed": pushed,
        "created": (data.get("created_at") or "")[:10] or None,
        "status": "ok",
    }
    if data.get("full_name", "").lower() != repo.lower():
        out["status"], out["problem"] = "renamed", f"now {data['full_name']}"
    elif data.get("archived"):
        out["status"], out["problem"] = "archived", "archived on GitHub"
    elif pushed and (dt.date.today() - dt.date.fromisoformat(pushed)).days > INACTIVE_AFTER_DAYS:
        out["status"], out["problem"] = "inactive", f"no push since {pushed}"
    elif licence not in OPEN_LICENCES:
        out["status"], out["problem"] = (
            "licence",
            f"licence {licence or 'unknown'} is not OSI-approved",
        )
    return out


def momentum(points: list[list], created: str | None, today: dt.date) -> int | None:
    """Stars gained over MOMENTUM_DAYS.

    A repo created within the window gained every star it has. Otherwise the baseline
    is the snapshot closest to MOMENTUM_DAYS ago that is at least MIN_HISTORY_DAYS old,
    scaled to the window. A repo with no such snapshot that is younger than YOUNG_DAYS
    gets its average since creation, so a fast riser counts the day it is found.
    """
    if not points:
        return None
    latest_date, latest = dt.date.fromisoformat(points[-1][0]), points[-1][1]
    age = (latest_date - dt.date.fromisoformat(created)).days if created else None
    if age is not None and age <= MOMENTUM_DAYS:
        return latest
    target = today - dt.timedelta(days=MOMENTUM_DAYS)
    candidates = [
        (dt.date.fromisoformat(d), s)
        for d, s in points
        if (latest_date - dt.date.fromisoformat(d)).days >= MIN_HISTORY_DAYS
    ]
    if candidates:
        base_date, base = min(candidates, key=lambda c: abs((c[0] - target).days))
        days = (latest_date - base_date).days
        return round(max(latest - base, 0) * MOMENTUM_DAYS / days)
    if age is not None and age < YOUNG_DAYS:
        return round(latest * MOMENTUM_DAYS / age)
    return None


# ---------------------------------------------------------------- discovery


# Jev answers questions about a state with probabilities, not prose. The README is
# part of the state: data to judge, never instructions. One property per question,
# all in one request, combined in code (TypeSafe's advice: a broad question hides
# several judgments behind one answer).
JUDGE_QUESTIONS = {
    "job": {
        "type": "noul",
        "instructions": (
            "Is the main purpose of `candidate` the same job that the projects in "
            "`list.members` do, so that a developer could use it in place of one of them?"
        ),
        "criteria": {
            "true": "It does the job the members do and could replace one of them.",
            "false": (
                "It does another job, or uses such a component as one part of a larger "
                "product, or has this job only as a side feature."
            ),
        },
    },
    "software": {
        "type": "noul",
        "instructions": "Is `candidate` software that people install and run?",
        "criteria": {
            "true": "A library, server, tool or application that people run.",
            "false": (
                "A tutorial, course, book, list of links, benchmark, paper's code or "
                "collection of examples."
            ),
        },
    },
    "client": {
        "type": "noul",
        "instructions": "Is `candidate` mainly an SDK or client for a hosted service?",
    },
    "kind": {
        "type": "choice",
        "instructions": "What kind of software is `candidate`?",
        "criteria": {
            "library": "A library imported into an application's own process.",
            "embedded": "A database or engine that runs inside the application's process.",
            "server": "A service run on its own and reached over the network.",
            "proxy": "A gateway or proxy that sits between an application and other services.",
            "Postgres extension": "An extension installed into PostgreSQL.",
            "framework": "A framework an application is built on.",
            "application": "A finished application for end users.",
            "cli": "A command-line tool.",
            "model": "Model weights.",
            "browser": "A browser or a browser engine for automation.",
            "other": "Software that fits none of the above.",
        },
    },
}
assert set(JUDGE_QUESTIONS["kind"]["criteria"]) == KINDS | {"other"}
# A member does the job and is software to run (at least MUST), and is not the SDK
# of a hosted service (under MUST_NOT). Measured 2026-10-11 on 44 hand-labelled
# candidates from the first run: precision 0.97, recall 0.97, against 0.96 / 0.76 for
# one compound question; an add-on question or requiring a member's kind only cost
# real members. Of the 140 the second run let in, `client` >= 0.6 caught Logfire's,
# Judgment's, E2B's, Tensorlake's and Bright Data's SDKs, and one self-hostable
# platform (Latitude, 0.68).
MUST = {"job": 0.5, "software": 0.5}
MUST_NOT = {"client": 0.6}


def parse_verdict(data: dict) -> dict | None:
    """Jev's answers as a verdict, or None when they are not there."""
    answers = data.get("answers") or {}
    p = {q: (answers.get(q) or {}).get("noul") for q in (*MUST, *MUST_NOT)}
    if not all(isinstance(v, (int, float)) for v in p.values()):
        return None
    kind = (answers.get("kind") or {}).get("choice")
    return {
        "p": {q: round(v, 2) for q, v in p.items()},
        "kind": [kind] if kind in KINDS else [],
        "model": data.get("model") or JUDGE_MODEL,  # the version that answered
    }


def belongs(verdict: dict) -> bool:
    """Decided here rather than stored, so the thresholds can move without judging
    again."""
    p = verdict["p"]
    return all(p[q] >= t for q, t in MUST.items()) and all(p[q] < t for q, t in MUST_NOT.items())


def judge(state: dict, key: str) -> dict | None:
    try:
        data = _get(
            "https://api.typesafe.ai/v1/systemone",
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=60,
            body={"model": JUDGE_MODEL, "state": state, "questions": JUDGE_QUESTIONS},
        )
    except (Transient, urllib.error.HTTPError) as exc:
        print(f"warning: judge unreachable: {exc}", file=sys.stderr)
        return None
    return parse_verdict(data)


def _readme(repo: str, token: str | None) -> str:
    try:
        data = _get(f"https://api.github.com/repos/{repo}/readme", _gh_headers(token))
    except (Transient, urllib.error.HTTPError):
        return ""
    return base64.b64decode(data.get("content") or "").decode("utf-8", "replace")[:README_CHARS]


def _members(pool_id: str, catalog) -> list[dict]:
    return [
        {"repo": a.name, "kind": list(a.kind), "what": a.what}
        for a in catalog.pools[pool_id].alternatives
        if not a.discovered
    ]


def discover(pools: list[dict], verdicts: dict, token: str | None, key: str, today: dt.date) -> int:
    """Search each GitHub pool's `discover` queries and judge the candidates not yet
    judged (or judged REJUDGE_DAYS ago and turned down) into `verdicts`
    ({pool id: {repo: verdict}}). Returns how many were judged."""
    catalog = load_catalog(CATALOG)
    listed = {p["repo"].lower() for pool in pools for p in pool.get("projects") or []}
    since = (today - dt.timedelta(days=MOMENTUM_DAYS)).isoformat()
    judged = 0
    for pool in pools:
        if pool["source"] != "github":
            continue
        seen = verdicts.setdefault(pool["id"], {})
        deny = {r.lower() for r in pool.get("deny") or []}
        services = [s.name for s in catalog.services if pool["id"] in s.replace_with][:10]
        for query in pool.get("discover") or []:
            q = f"{query} stars:>={DISCOVER_MIN_STARS} fork:false archived:false pushed:>={since}"
            url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(
                {"q": q, "sort": "stars", "per_page": 30}
            )
            time.sleep(SEARCH_PAUSE)
            try:
                hits = _get(url, _gh_headers(token)).get("items") or []
            except (Transient, urllib.error.HTTPError) as exc:
                print(f"warning: {pool['id']}: search '{query}' failed: {exc}", file=sys.stderr)
                continue
            for hit in hits:
                repo = hit["full_name"]
                before = seen.get(repo)
                if repo.lower() in listed or repo.lower() in deny or judged >= JUDGE_LIMIT:
                    continue
                if before and (
                    belongs(before)
                    or (today - dt.date.fromisoformat(before["judged"])).days < REJUDGE_DAYS
                ):
                    continue
                # GitHub must see the licence: a NOASSERTION repo needs a person to read it.
                if (hit.get("license") or {}).get("spdx_id") not in OPEN_LICENCES:
                    continue
                state = {
                    "list": {
                        "name": pool["name"],
                        "replaces": services or ["no closed service named"],
                        "members": _members(pool["id"], catalog),
                    },
                    "candidate": {
                        "repo": repo,
                        "description": hit.get("description") or "",
                        "readme": _readme(repo, token),
                    },
                }
                verdict = judge(state, key)
                if verdict:
                    verdict["what"] = hit.get("description") or ""
                    seen[repo] = {**verdict, "judged": today.isoformat()}
                    judged += 1
                    print(
                        f"  {'+' if belongs(verdict) else '-'} {pool['id']}: {repo} "
                        f"{verdict['p']} {verdict['kind']}",
                        file=sys.stderr,
                    )
    return judged


def clip(text: str, n: int = 100) -> str:
    """A found project's own description, cut at a word when it runs long."""
    text = " ".join(text.split())
    if len(text) <= n:
        return text
    return text[: n - 1].rsplit(" ", 1)[0].rstrip(",.;:\u2014\u2013- ") + "…"


def found(pools: list[dict], verdicts: dict) -> dict[str, dict[str, dict]]:
    """Per pool, the repos judged members that no pool lists by hand and none denies."""
    listed = {p["repo"].lower() for pool in pools for p in pool.get("projects") or []}
    out = {}
    for pool in pools:
        deny = {r.lower() for r in pool.get("deny") or []}
        out[pool["id"]] = {
            repo: v
            for repo, v in (verdicts.get(pool["id"]) or {}).items()
            if belongs(v) and repo.lower() not in listed and repo.lower() not in deny
        }
    return out


# ---------------------------------------------------------------- huggingface


def hf_publisher_models(author: str) -> list[dict] | None:
    """The publisher's models, or None when Hugging Face could not be asked."""
    query = urllib.parse.urlencode(
        [
            ("author", author),
            ("sort", "trendingScore"),
            ("limit", "200"),
            *[
                ("expand[]", f)
                for f in (
                    "cardData",
                    "downloads",
                    "likes",
                    "trendingScore",
                    "createdAt",
                    "pipeline_tag",
                    "safetensors",
                )
            ],
        ]
    )
    try:
        return _get(f"https://huggingface.co/api/models?{query}")
    except (Transient, urllib.error.HTTPError):
        return None


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
        for m in by_author.get(author) or []:
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
                    # A 1T-parameter model and a 2B one are not the same replacement.
                    "params": (m.get("safetensors") or {}).get("total"),
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


def _previous_rankings() -> dict[str, dict]:
    file = CATALOG / "rankings.json"
    if not file.is_file():
        return {}
    try:
        return json.loads(file.read_text("utf-8")).get("pools") or {}
    except ValueError:
        return {}


def shrunk_pools(previous: dict[str, dict], current: dict[str, dict]) -> list[str]:
    """Pools that came out empty, or with under half the listed entries they had.

    That is what an outage looks like, not what the open source world does in a
    week, and publishing it would leave users with no alternatives to read. Found
    projects don't count: the judge or a `deny` dropping them is not an outage.
    """

    def listed(pool: dict | None) -> int:
        return sum(not e.get("discovered") for e in (pool or {}).get("ranked") or [])

    out = []
    for pool_id, pool in current.items():
        now = listed(pool)
        before = listed(previous.get(pool_id))
        if now == 0 or (before >= 4 and now < before / 2):
            out.append(f"{pool_id}: {before} → {now}")
    return out


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

    verdicts: dict[str, dict] = (
        json.loads(DISCOVERED.read_text("utf-8")) if DISCOVERED.is_file() else {}
    )
    key = os.environ.get("TYPESAFE_API_KEY")
    if key and not args.check:
        print(f"judged {discover(pools, verdicts, token, key, today)} candidates", file=sys.stderr)
    elif not key:
        print("warning: no TYPESAFE_API_KEY; no new projects are judged", file=sys.stderr)
    members = found(pools, verdicts)

    declared = {
        p["repo"]: p.get("licence")
        for pool in pools
        if pool["source"] == "github"
        for p in pool["projects"]
    }
    for repos_found in members.values():
        declared.update(dict.fromkeys(repos_found))
    publishers = sorted(
        {a for pool in pools if pool["source"] == "huggingface" for a in pool["publishers"]}
    )
    with ThreadPoolExecutor(max_workers=8) as ex:
        repos = dict(
            zip(
                declared,
                ex.map(lambda r: github_project(r, declared[r], token), declared),
                strict=True,
            )
        )
        by_author = dict(zip(publishers, ex.map(hf_publisher_models, publishers), strict=True))

    for repo, info in repos.items():
        if "stars" not in info:
            continue
        oldest = (today - dt.timedelta(days=KEEP_HISTORY_DAYS)).isoformat()
        points = [p for p in history.get(repo, []) if oldest <= p[0] != today.isoformat()]
        history[repo] = [*points, [today.isoformat(), info["stars"]]]
        info["stars_30d"] = momentum(history[repo], info.get("created"), today)

    previous = _previous_rankings()
    problems = []
    out_pools: dict[str, dict] = {}
    for pool in pools:
        before = previous.get(pool["id"], {})
        if pool["source"] == "github":
            last_seen = {e["repo"]: e for e in before.get("ranked", [])}
            entries = []
            own = [(p["repo"], {"what": p["what"]}) for p in pool["projects"]]
            judged = [
                (repo, {"what": clip(v["what"]), "kind": v["kind"], "discovered": True})
                for repo, v in members[pool["id"]].items()
            ]
            for repo, extra in own + judged:
                entry = {**repos[repo], **extra}
                if entry["status"] == "unreachable" and repo in last_seen:
                    # A failed request is not news: keep yesterday's facts.
                    entry = {**last_seen[repo], **extra, "stale": True}
                entries.append(entry)
            # What the script found itself and lost again needs no person.
            problems += [
                f"{pool['id']}: {e['repo']} — {e['problem']}"
                for e in entries
                if e.get("status") != "ok" and not e.get("discovered")
            ]
            ranked = sorted(
                (e for e in entries if e.get("status") == "ok"), key=lambda e: -e["stars"]
            )
            ranked_by = "stars"
            dropped = [e for e in entries if e.get("status") != "ok"]
        else:
            ranked, ranked_by, dropped = hf_pool(pool, by_author), "trending", []
            failed = [a for a in pool["publishers"] if by_author.get(a) is None]
            if failed and before.get("ranked"):
                problems.append(f"{pool['id']}: Hugging Face unreachable for {', '.join(failed)}")
                ranked = before["ranked"]  # a partial view would reorder on missing data
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
                gain = "" if e.get("stars_30d") is None else f"+{e['stars_30d']}"
                new = "  (found)" if e.get("discovered") else ""
                print(f"  {i:>2}. {e['repo']:<42} {e['stars']:>7} {gain:>7}{new}")
            else:
                print(f"  {i:>2}. {e['id']:<50} trend {e['trending']:>4}  dl {e['downloads']:>10}")
        for e in pool["dropped"]:
            print(f"   x  {e['repo']:<42} {e.get('problem')}")

    if problems:
        print(f"\n{len(problems)} problem(s):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)

    collapsed = shrunk_pools(previous, out_pools)
    if collapsed:
        print("\nrefusing to write rankings; these pools emptied or halved:", file=sys.stderr)
        for line in collapsed:
            print(f"  - {line}", file=sys.stderr)
        return EXIT_REFUSED

    if not args.check:
        payload = {
            "generated": today.isoformat(),
            "momentum_days": MOMENTUM_DAYS,
            "pools": out_pools,
        }
        (CATALOG / "rankings.json").write_text(
            json.dumps(payload, indent=1, ensure_ascii=False) + "\n", "utf-8", newline="\n"
        )
        HISTORY.write_text(
            json.dumps(dict(sorted(history.items())), indent=0, separators=(",", ":")) + "\n",
            "utf-8",
            newline="\n",
        )
        DISCOVERED.write_text(
            json.dumps(
                {pool: dict(sorted(v.items())) for pool, v in sorted(verdicts.items()) if v},
                indent=1,
                ensure_ascii=False,
            )
            + "\n",
            "utf-8",
            newline="\n",
        )
        print("\nwrote catalog/rankings.json, star-history.json, discovered.json", file=sys.stderr)
    return EXIT_PROBLEMS if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
