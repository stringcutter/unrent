"""Facts with an upstream source are fetched, not stored.

The catalog mixes two kinds of claim, and only one of them has a source that
updates itself:

  upstream  — licence, last release, deprecated, yanked. Someone else maintains
              these. Storing them means storing a copy that starts rotting the
              moment it is written, which is why a "re-verify yearly" rule felt
              wrong: the right cadence is not yearly, it is *now*.
  judgement — the axes, what you give up, what the move costs. Nobody publishes
              these. There is no feed to poll and no model that can generate them
              without inventing them, so they stay stored, dated and human.

This module handles the first kind. It checks only the packages the scan actually
found — a handful, not the whole catalog — so it is fast enough to run every time.

Fails soft on purpose: a scan is useful without the network, and a tool whose core
promise is that nothing leaves the machine should keep working when nothing can.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

CACHE = Path.home() / ".cache" / "lockin" / "registry.json"
CACHE_TTL = 24 * 3600
TIMEOUT = 10


@dataclass(frozen=True)
class PackageFact:
    ecosystem: str
    name: str
    latest: str | None = None
    last_published: str | None = None
    deprecated: str | None = None
    yanked: bool = False
    error: str | None = None

    @property
    def is_notable(self) -> bool:
        return bool(self.deprecated or self.yanked)


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "lockin"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def _pypi(name: str) -> PackageFact:
    try:
        d = _get(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return PackageFact("pypi", name, error=type(e).__name__)
    info = d.get("info", {})
    version = info.get("version")
    released = ""
    for f in d.get("releases", {}).get(version, []) or []:
        released = f.get("upload_time_iso_8601", "")[:10]
        break
    return PackageFact(
        "pypi",
        name,
        latest=version,
        last_published=released or None,
        yanked=bool(info.get("yanked")),
    )


def _npm(name: str) -> PackageFact:
    try:
        d = _get(f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@')}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return PackageFact("npm", name, error=type(e).__name__)
    latest = (d.get("dist-tags") or {}).get("latest")
    meta = (d.get("versions") or {}).get(latest, {})
    deprecated = meta.get("deprecated")
    return PackageFact(
        "npm",
        name,
        latest=latest,
        last_published=(d.get("time") or {}).get(latest, "")[:10] or None,
        deprecated=str(deprecated) if deprecated else None,
    )


def _load_cache() -> dict:
    try:
        raw = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    # A cache written by another version, or by something else entirely, must not
    # be able to fail a scan — the user has no way to know the fix is deleting a
    # file nobody told them about.
    if not isinstance(raw, dict):
        return {}
    fields = set(PackageFact.__dataclass_fields__) | {"_at"}
    now = time.time()
    return {
        k: v
        for k, v in raw.items()
        if isinstance(v, dict) and set(v) <= fields and now - v.get("_at", 0) < CACHE_TTL
    }


def _save_cache(cache: dict) -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass  # a cache that cannot be written is not a reason to fail a scan


def check(packages: set[tuple[str, str]]) -> dict[tuple[str, str], PackageFact]:
    """Look up (ecosystem, name) pairs. Cached for a day, fetched in parallel."""
    cache = _load_cache()
    out: dict[tuple[str, str], PackageFact] = {}
    todo = []

    for eco, name in sorted(packages):
        hit = cache.get(f"{eco}:{name}")
        if hit:
            out[(eco, name)] = PackageFact(**{k: v for k, v in hit.items() if k != "_at"})
        else:
            todo.append((eco, name))

    if todo:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for fact in pool.map(lambda p: (_pypi if p[0] == "pypi" else _npm)(p[1]), todo):
                out[(fact.ecosystem, fact.name)] = fact
                if not fact.error:  # never cache a failure
                    cache[f"{fact.ecosystem}:{fact.name}"] = {**vars(fact), "_at": time.time()}
        _save_cache(cache)

    return out


def packages_in(findings) -> set[tuple[str, str]]:
    """The packages that actually triggered a finding — not the whole catalog."""
    eco = {"requirement": "pypi", "npm": "npm"}
    return {(eco[f.kind], f.value) for finding in findings for f in finding.facts if f.kind in eco}
