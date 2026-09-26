"""Verify every package signature in unrent's catalog against its registry.

Usage:  python scripts/verify_packages.py [CATALOG_DIR] [--out results.json] [--extra extra.yaml]

Exits 1 when a signature names a package that does not exist: a typo there is a
silent false negative, because nothing will ever match it.

For every (service, kind, value) of a package kind, looks the value up in the
registry and records: exists, latest version, last release date, deprecation /
discontinued / abandoned / yanked flags, summary, author/owner and repository URL
(so a human can spot packages that belong to another vendor or an OSS project).

`--extra` checks a proposals file (same `detect:` shape, list of services) instead of
/ in addition to the catalog, so proposed signatures can be verified the same way.

Throttled: at most 4 concurrent requests, 0.15 s pause per request per worker.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import gzip
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

UA = "unrent-catalog-check (+https://github.com/niklasmellgren/unrent)"
PACKAGE_KINDS = (
    "requirement",
    "npm",
    "maven",
    "nuget",
    "gem",
    "composer",
    "cargo",
    "pub",
    "go",
    "swift",
)


def get(url: str, accept: str = "application/json", tries: int = 3):
    for i in range(tries):
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept": accept, "Accept-Encoding": "gzip"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
                    data = gzip.decompress(data)
                time.sleep(0.15)
                return r.status, data
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return e.code, b""
            if e.code == 429 or e.code >= 500:
                time.sleep(2 * (i + 1))
                continue
            return e.code, b""
        except Exception:
            time.sleep(2 * (i + 1))
    return 0, b""


def jget(url: str):
    status, data = get(url)
    if status == 200:
        try:
            return json.loads(data)
        except ValueError:
            return None
    return None


# ------------------------------------------------------------------ registries


def check_pypi(name):
    d = jget(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
    if not d:
        return {"exists": False}
    info = d["info"]
    rel = d.get("releases", {}).get(info["version"], [])
    uploaded = max((f["upload_time"] for f in rel), default=None)
    yanked = all(f.get("yanked") for f in rel) if rel else None
    urls = info.get("project_urls") or {}
    return {
        "exists": True,
        "canonical": info["name"],
        "latest": info["version"],
        "released": uploaded,
        "summary": info.get("summary"),
        "author": info.get("author")
        or info.get("author_email")
        or info.get("maintainer")
        or info.get("maintainer_email"),
        "repo": urls.get("Source")
        or urls.get("Repository")
        or urls.get("Homepage")
        or info.get("home_page"),
        "yanked": yanked,
        "deprecated_hint": any(
            w in (info.get("summary") or "").lower()
            for w in ("deprecated", "no longer", "moved to", "renamed")
        ),
        "classifiers_inactive": any("Inactive" in c for c in info.get("classifiers", [])),
    }


def check_npm(name):
    enc = name.replace("/", "%2f")
    d = jget(f"https://registry.npmjs.org/{enc}")
    if not d or "dist-tags" not in d:
        return {"exists": False}
    latest = d["dist-tags"].get("latest")
    v = d.get("versions", {}).get(latest, {})
    repo = v.get("repository") or d.get("repository")
    if isinstance(repo, dict):
        repo = repo.get("url")
    return {
        "exists": True,
        "latest": latest,
        "released": d.get("time", {}).get(latest),
        "summary": v.get("description") or d.get("description"),
        "author": ", ".join(m.get("name", "") for m in d.get("maintainers", [])[:5]),
        "repo": repo,
        "deprecated": v.get("deprecated"),
    }


def check_maven(ga):
    g, a = ga.split(":")
    url = f"https://repo1.maven.org/maven2/{g.replace('.', '/')}/{a}/maven-metadata.xml"
    status, data = get(url, accept="application/xml")
    if status != 200:
        return {"exists": False}
    txt = data.decode("utf-8", "replace")
    import re

    latest = re.search(r"<release>([^<]+)</release>", txt) or re.search(
        r"<latest>([^<]+)</latest>", txt
    )
    upd = re.search(r"<lastUpdated>(\d+)</lastUpdated>", txt)
    return {"exists": True, "latest": latest and latest.group(1), "released": upd and upd.group(1)}


def check_nuget(name):
    d = jget(f"https://api.nuget.org/v3/registration5-gz-semver2/{name.lower()}/index.json")
    if not d:
        return {"exists": False}
    # last page, last item
    page = d["items"][-1]
    items = page.get("items")
    if items is None:
        page = jget(page["@id"]) or {}
        items = page.get("items", [])
    listed = [i for i in items if i["catalogEntry"].get("listed", True)]
    last = (listed or items)[-1]["catalogEntry"]
    return {
        "exists": True,
        "canonical": last.get("id"),
        "latest": last.get("version"),
        "released": last.get("published"),
        "summary": (last.get("description") or "")[:200],
        "author": last.get("authors"),
        "repo": last.get("projectUrl"),
        "deprecated": last.get("deprecation"),
    }


def check_gem(name):
    d = jget(f"https://rubygems.org/api/v1/gems/{name}.json")
    if not d:
        return {"exists": False}
    return {
        "exists": True,
        "latest": d.get("version"),
        "released": d.get("version_created_at"),
        "summary": d.get("info", "")[:200],
        "author": d.get("authors"),
        "repo": d.get("source_code_uri") or d.get("homepage_uri"),
    }


def check_composer(name):
    d = jget(f"https://packagist.org/packages/{name}.json")
    if not d or "package" not in d:
        return {"exists": False}
    p = d["package"]
    return {
        "exists": True,
        "summary": p.get("description"),
        "repo": p.get("repository"),
        "abandoned": p.get("abandoned"),
        "downloads": p.get("downloads", {}).get("monthly"),
        "released": p.get("time"),
    }


def check_cargo(name):
    d = jget(f"https://crates.io/api/v1/crates/{name}")
    if not d or "crate" not in d:
        return {"exists": False}
    c = d["crate"]
    return {
        "exists": True,
        "latest": c.get("max_stable_version") or c.get("max_version"),
        "released": c.get("updated_at"),
        "summary": c.get("description"),
        "repo": c.get("repository"),
        "downloads_recent": c.get("recent_downloads"),
    }


def check_pub(name):
    d = jget(f"https://pub.dev/api/packages/{name}")
    if not d:
        return {"exists": False}
    opts = jget(f"https://pub.dev/api/packages/{name}/options") or {}
    pub = d.get("latest", {}).get("pubspec", {})
    return {
        "exists": True,
        "latest": d.get("latest", {}).get("version"),
        "released": d.get("latest", {}).get("published"),
        "summary": pub.get("description"),
        "repo": pub.get("repository") or pub.get("homepage"),
        "discontinued": opts.get("isDiscontinued"),
        "replaced_by": opts.get("replacedBy"),
    }


def _go_escape(path):
    return "".join("!" + c.lower() if c.isupper() else c for c in path)


def check_go(path):
    d = jget(f"https://proxy.golang.org/{_go_escape(path)}/@latest")
    if d:
        out = {"exists": True, "latest": d.get("Version"), "released": d.get("Time")}
        # deprecation comment in go.mod
        status, mod = get(
            f"https://proxy.golang.org/{_go_escape(path)}/@v/{d['Version']}.mod",
            accept="text/plain",
        )
        if status == 200:
            txt = mod.decode("utf-8", "replace")
            if "Deprecated:" in txt:
                out["deprecated"] = txt[txt.index("Deprecated:") :].splitlines()[0]
        return out
    # the signature is a prefix match; try the /vN majors
    for major in ("/v2", "/v3", "/v4"):
        d = jget(f"https://proxy.golang.org/{_go_escape(path + major)}/@latest")
        if d:
            return {
                "exists": True,
                "latest": d.get("Version"),
                "released": d.get("Time"),
                "note": f"only as {path + major}",
            }
    return {"exists": False}


def _github_token() -> str | None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token or not shutil.which("gh"):
        return token
    out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    return out.stdout.strip() or None if out.returncode == 0 else None


def check_swift(spec):
    """A Swift package is its GitHub repository. Only a 404 means it is gone; a rate
    limit or a missing token is 'could not check', not 'does not exist'."""
    owner_repo = spec.split("github.com/", 1)[1]
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    token = _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"https://api.github.com/repos/{owner_repo}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"exists": False}
        return {"exists": None, "error": f"GitHub returned {e.code}"}
    except (urllib.error.URLError, TimeoutError) as e:
        return {"exists": None, "error": str(e)}
    return {
        "exists": True,
        "canonical": d.get("full_name"),
        "archived": d.get("archived"),
        "released": d.get("pushed_at"),
        "summary": d.get("description"),
        "stars": d.get("stargazers_count"),
        "renamed": d.get("full_name", "").lower() != owner_repo.lower(),
    }


CHECKERS = {
    "requirement": check_pypi,
    "npm": check_npm,
    "maven": check_maven,
    "nuget": check_nuget,
    "gem": check_gem,
    "composer": check_composer,
    "cargo": check_cargo,
    "pub": check_pub,
    "go": check_go,
    "swift": check_swift,
}


def load(paths):
    rows = []
    for p in paths:
        data = yaml.safe_load(Path(p).read_text(encoding="utf-8"))
        if isinstance(data, dict):  # proposals.yaml: new_services + add_signatures
            data = [
                *(data.get("services") or data.get("new_services") or []),
                *(
                    {"id": a["service"] + " (+add)", "detect": a["detect"]}
                    for a in data.get("add_signatures") or []
                ),
            ]
        for svc in data:
            for kind, values in (svc.get("detect") or {}).items():
                if kind in PACKAGE_KINDS:
                    for v in values:
                        rows.append((Path(p).name, svc["id"], kind, v))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "catalog",
        nargs="?",
        default=str(Path(__file__).resolve().parent.parent / "catalog" / "services"),
    )
    ap.add_argument("--out", default=None, help="also write every result here as JSON")
    ap.add_argument("--extra", nargs="*", default=[])
    ap.add_argument("--no-catalog", action="store_true")
    a = ap.parse_args()
    files = [] if a.no_catalog else sorted(Path(a.catalog).glob("*.yaml"))
    rows = load([*files, *a.extra])
    unique = sorted({(k, v) for _, _, k, v in rows})
    print(f"{len(rows)} signatures, {len(unique)} unique", file=sys.stderr)
    results = {}
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(CHECKERS[k], v): (k, v) for k, v in unique}
        for f in cf.as_completed(futs):
            k, v = futs[f]
            try:
                results[f"{k}:{v}"] = f.result()
            except Exception as e:
                results[f"{k}:{v}"] = {"exists": None, "error": repr(e)}
    out = [dict(file=fi, service=s, kind=k, value=v, **results[f"{k}:{v}"]) for fi, s, k, v in rows]
    if a.out:
        Path(a.out).write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    missing = [r for r in out if r.get("exists") is False]
    # Rate limits and outages are not news about the catalog; they are reported, and
    # the check fails only on packages a registry says do not exist.
    unchecked = [r for r in out if r.get("exists") is None]
    if unchecked:
        print(f"\nCOULD NOT CHECK ({len(unchecked)}):")
        for r in unchecked:
            print(f"  {r['service']:28} {r['kind']:12} {r['value']}  {r.get('error', '')}")
    flagged = [
        r
        for r in out
        if r.get("deprecated")
        or r.get("discontinued")
        or r.get("abandoned")
        or r.get("archived")
        or r.get("yanked")
        or r.get("deprecated_hint")
        or r.get("renamed")
        or r.get("classifiers_inactive")
    ]
    print(f"\nNOT FOUND ({len(missing)}):")
    for r in missing:
        print(
            f"  {r['file']:15} {r['service']:28} {r['kind']:12} {r['value']}  {r.get('error', '')}"
        )
    print(f"\nFLAGGED ({len(flagged)}):")
    for r in flagged:
        flags = {
            k: r[k]
            for k in (
                "deprecated",
                "discontinued",
                "replaced_by",
                "abandoned",
                "archived",
                "yanked",
                "deprecated_hint",
                "renamed",
                "canonical",
                "classifiers_inactive",
                "summary",
            )
            if r.get(k)
        }
        print(f"  {r['service']:28} {r['kind']:12} {r['value']}  {flags}")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
