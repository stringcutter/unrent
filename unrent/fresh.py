"""The latest rankings and model retirements, fetched from the unrent repository.

The weekly jobs commit catalog/rankings.json and catalog/retirements.yaml to main. The
MCP server reads both from there instead of the copies shipped with the release, and a
scan and the hook read the retirements, so dates and rankings are at most a week old
however old the install. Only these two public files are fetched: nothing about the
scanned code leaves the machine. When a fetch fails, or with UNRENT_OFFLINE=1, the
shipped copy is used, and the result says why.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from .catalog import Catalog, Retirement, parse_retirements

RANKINGS_URL = "https://raw.githubusercontent.com/stringcutter/unrent/main/catalog/rankings.json"
RETIREMENTS_URL = (
    "https://raw.githubusercontent.com/stringcutter/unrent/main/catalog/retirements.yaml"
)
MAX_AGE = 6 * 3600  # seconds a fetched copy is reused before asking again
TIMEOUT = 5
MAX_BYTES = 5_000_000


@dataclass(frozen=True)
class Rankings:
    data: dict  # a rankings snapshot, as in catalog/rankings.json
    source: str  # "online", "cache" or "shipped"
    note: str | None = None  # why the latest rankings are not in use

    @property
    def date(self) -> str | None:
        return self.data.get("generated")


@dataclass(frozen=True)
class Retirements:
    data: dict[str, Retirement]  # by model id, as Catalog.retirements
    source: str  # "online", "cache" or "shipped"
    note: str | None = None  # why the latest retirements are not in use


def cache_dir() -> Path:
    if os.environ.get("UNRENT_CACHE_DIR"):
        return Path(os.environ["UNRENT_CACHE_DIR"])
    home = Path.home()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = home / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
    return base / "unrent"


def _headers(url: str) -> dict[str, str]:
    headers = {"User-Agent": "unrent", "Accept": "application/json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    parts = urllib.parse.urlsplit(url)
    # A token only reaches GitHub, over TLS: it lets a private fork serve its rankings.
    if token and parts.scheme == "https" and parts.hostname == "raw.githubusercontent.com":
        headers["Authorization"] = f"token {token}"
    return headers


def _download(url: str, timeout: float = TIMEOUT) -> bytes:
    request = urllib.request.Request(url, headers=_headers(url))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError(f"larger than {MAX_BYTES} bytes")
    return body


def _snapshot(body: bytes) -> dict:
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("not a rankings snapshot")
    pools = data.get("pools")
    if (
        not isinstance(data.get("generated"), str)
        or not isinstance(pools, dict)
        or not all(
            isinstance(p, dict) and isinstance(p.get("ranked"), list) for p in pools.values()
        )
    ):
        raise ValueError("not a rankings snapshot")
    return data


def _retirements_file(body: bytes) -> dict:
    """The shape of retirements.yaml; its entries are checked against the catalog in use."""
    try:
        raw = yaml.load(body.decode("utf-8"), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    except (yaml.YAMLError, RecursionError) as exc:
        raise ValueError(f"not a retirements file: {exc}") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("vendors"), dict):
        raise ValueError("not a retirements file")
    return raw


def _reason(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)
    return str(exc) or type(exc).__name__


def _cached(file: Path, parse: Callable[[bytes], dict]) -> tuple[dict | None, float]:
    """The cached copy and its age in seconds; (None, inf) when there is none or it
    does not parse. An empty file marks a failed fetch, and is as old as that fetch."""
    try:
        age = time.time() - file.stat().st_mtime
        body = file.read_bytes()
        return (parse(body), age) if body else (None, age)
    except (OSError, ValueError):
        return None, float("inf")


def _store(file: Path, body: bytes) -> None:
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        tmp = file.with_suffix(".tmp")
        tmp.write_bytes(body)
        tmp.replace(file)
    except OSError:
        pass  # a read-only home costs a fetch per refresh, nothing more


def _fetch(
    name: str, url: str, parse: Callable[[bytes], dict], timeout: float = TIMEOUT
) -> tuple[dict | None, str, str | None]:
    """(parsed copy or None, source, note): the cached copy of `name` while it is
    younger than MAX_AGE, else a new download, else the stale cache and why."""
    file = cache_dir() / name
    data, age = _cached(file, parse)
    if 0 <= age < MAX_AGE:  # a copy dated in the future is not trusted to be fresh
        return data, "cache", None
    try:
        body = _download(url, timeout)
        data = parse(body)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return data, "cache", f"could not fetch the latest {Path(name).stem}: {_reason(exc)}"
    _store(file, body)
    return data, "online", None


def latest(shipped: dict) -> Rankings:
    """The newest of: the snapshot online, the cached copy of it, the shipped one.

    Pools the newer snapshot lacks keep their shipped ranking, so an install whose
    catalog has a pool main has since renamed still ranks it."""
    if os.environ.get("UNRENT_OFFLINE"):
        return Rankings(shipped, "shipped", "UNRENT_OFFLINE is set")
    url = os.environ.get("UNRENT_RANKINGS_URL") or RANKINGS_URL
    data, source, note = _fetch("rankings.json", url, _snapshot)
    if data is None or data["generated"] < (shipped.get("generated") or ""):
        return Rankings(shipped, "shipped", note)
    merged = {**data, "pools": {**(shipped.get("pools") or {}), **data["pools"]}}
    return Rankings(merged, source, note)


def latest_retirements(timeout: float = TIMEOUT) -> tuple[dict | None, str, str | None]:
    """retirements.yaml on main, parsed but not yet checked against a catalog, as
    `_fetch` gives it; (None, "shipped", why) when there is none to use.

    A failed fetch is not retried for MAX_AGE: the hook runs after every edit, and
    must not wait on an unreachable host each time."""
    if os.environ.get("UNRENT_OFFLINE"):
        return None, "shipped", "UNRENT_OFFLINE is set"
    url = os.environ.get("UNRENT_RETIREMENTS_URL") or RETIREMENTS_URL
    raw, source, note = _fetch("retirements.yaml", url, _retirements_file, timeout)
    if note:
        file = cache_dir() / "retirements.yaml"
        try:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.touch()  # keeps a stale copy, or an empty file marks the failure
        except OSError:
            pass
    return raw, (source if raw is not None else "shipped"), note


def retirements(
    catalog: Catalog, fetched: tuple[dict | None, str, str | None] | None = None
) -> Retirements:
    """The retirements on main, in place of the shipped ones when they load against
    this catalog. Main is where releases are cut and the weekly check lands, so its copy
    is never the older one, and an entry it no longer has was a mistake: the shipped
    entries are not merged in. Entries for services this catalog lacks are left out; a
    copy that does not load at all leaves the shipped ones in use. `fetched` is what
    latest_retirements() gave, when the caller already has it."""
    raw, source, note = fetched or latest_retirements()
    shipped = Retirements(catalog.retirements, "shipped", note)
    if raw is None:
        return shipped
    ids = {s.id for s in catalog.services}
    try:
        data = parse_retirements(raw, "retirements.yaml on main", ids, known_only=True)
    except Exception as exc:  # whatever is on main, a scan and the hook go on
        return Retirements(
            catalog.retirements, "shipped", f"the latest retirements did not load: {exc}"
        )
    if not data:
        return Retirements(catalog.retirements, "shipped", "the latest retirements list no model")
    return Retirements(data, source, note)
