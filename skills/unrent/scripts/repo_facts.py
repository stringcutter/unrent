#!/usr/bin/env python3
"""Current facts for GitHub repos and Hugging Face models, in one go.

Usage:
  python repo_facts.py owner/repo [owner/repo ...] [--hf org/model ...] [--json]

GitHub: stars, last push, latest release, licence (SPDX), archived, and the current name
when a repo was renamed. One GraphQL request per 40 repos through the `gh` CLI when it is
logged in; otherwise the public REST API (60 requests an hour without a token).
Hugging Face: licence, parameter count, downloads (30 days), likes, last modified, gated.

These are point-in-time facts. Star growth is not among them on purpose: GitHub no
longer exposes when stars were given, so there is no reliable public source for it.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

UA = "unrent-skill-repo-facts"
FIELDS = (
    "nameWithOwner stargazerCount pushedAt isArchived"
    " licenseInfo { spdxId } latestRelease { publishedAt }"
)


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError) as e:
        return {"error": str(e)}


def _gh_ready() -> bool:
    if not shutil.which("gh"):
        return False
    return subprocess.run(["gh", "auth", "status"], capture_output=True).returncode == 0


def github_graphql(repos: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(repos), 40):
        chunk = repos[i : i + 40]
        parts = []
        for j, full in enumerate(chunk):
            owner, _, name = full.partition("/")
            args = f"owner: {json.dumps(owner)}, name: {json.dumps(name)}"
            parts.append(f"r{j}: repository({args}) {{ {FIELDS} }}")
        query = "query { " + " ".join(parts) + " }"
        # gh exits 1 when some aliases fail (unknown repo) but still prints the data
        res = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        try:
            body = json.loads(res.stdout or "{}")
        except ValueError:
            body = {}
        data = body.get("data") or {}
        for j, full in enumerate(chunk):
            node = data.get(f"r{j}")
            if not node:
                out[full] = {
                    "error": "not found" if body else (res.stderr.strip() or "gh failed")[:120]
                }
                continue
            out[full] = {
                "name": node["nameWithOwner"],
                "stars": node["stargazerCount"],
                "pushed": (node["pushedAt"] or "")[:10],
                "release": ((node.get("latestRelease") or {}).get("publishedAt") or "")[:10]
                or None,
                "licence": (node.get("licenseInfo") or {}).get("spdxId"),
                "archived": node["isArchived"],
            }
    return out


def github_rest(repos: list[str]) -> dict[str, dict]:
    out = {}
    for full in repos:
        d = _get(f"https://api.github.com/repos/{full}")
        if "error" in d or "full_name" not in d:
            out[full] = {"error": d.get("error") or d.get("message", "not found")}
            continue
        out[full] = {
            "name": d["full_name"],
            "stars": d["stargazers_count"],
            "pushed": (d.get("pushed_at") or "")[:10],
            "release": None,
            "licence": (d.get("license") or {}).get("spdx_id"),
            "archived": d.get("archived"),
        }
    return out


def huggingface(models: list[str]) -> dict[str, dict]:
    out = {}
    for mid in models:
        d = _get(f"https://huggingface.co/api/models/{mid}")
        if "error" in d or "id" not in d:
            out[mid] = {"error": d.get("error", "not found")}
            continue
        lic = (d.get("cardData") or {}).get("license") or next(
            (t.split(":", 1)[1] for t in d.get("tags", []) if t.startswith("license:")), None
        )
        params = (d.get("safetensors") or {}).get("total")
        out[mid] = {
            "name": d["id"],
            "licence": lic,
            "params_b": round(params / 1e9, 1) if params else None,
            "downloads_30d": d.get("downloads"),
            "likes": d.get("likes"),
            "modified": (d.get("lastModified") or "")[:10],
            "gated": bool(d.get("gated")),
            "task": d.get("pipeline_tag"),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("repos", nargs="*", help="GitHub owner/repo")
    ap.add_argument("--hf", nargs="*", default=[], help="Hugging Face model ids")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    a = ap.parse_args()
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    repos = list(
        dict.fromkeys(r.strip().rstrip("/").removeprefix("https://github.com/") for r in a.repos)
    )
    gh = (github_graphql if _gh_ready() else github_rest)(repos) if repos else {}
    hf = huggingface(list(dict.fromkeys(a.hf))) if a.hf else {}

    if a.json:
        print(json.dumps({"github": gh, "huggingface": hf}, indent=1))
        return 0
    if gh:
        print("repo\tstars\tpushed\trelease\tlicence\tnote")
        for full, f in gh.items():
            if "error" in f:
                print(f"{full}\t-\t-\t-\t-\t{f['error']}")
                continue
            notes = []
            if f["archived"]:
                notes.append("ARCHIVED")
            if f["name"].lower() != full.lower():
                notes.append(f"renamed to {f['name']}")
            cols = [full, f["stars"], f["pushed"], f["release"] or "-", f["licence"] or "-"]
            print("\t".join(map(str, [*cols, ", ".join(notes)])))
    if hf:
        print("\nmodel\tlicence\tparams_B\tdownloads_30d\tlikes\tmodified\tnote")
        for mid, f in hf.items():
            if "error" in f:
                print(f"{mid}\t-\t-\t-\t-\t-\t{f['error']}")
                continue
            note = "gated" if f["gated"] else ""
            cols = [mid, f["licence"] or "-", f["params_b"] or "-", f["downloads_30d"], f["likes"]]
            print("\t".join(map(str, [*cols, f["modified"], note])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
