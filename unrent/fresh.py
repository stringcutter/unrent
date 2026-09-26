"""The latest rankings, fetched from the unrent repository.

The weekly refresh commits catalog/rankings.json to main. A long-running process (the
MCP server) reads that file instead of the snapshot shipped with the release, so its
rankings are at most a week old however old the install. Only the rankings are
fetched: nothing about the scanned code leaves the machine. When the fetch fails, or
with UNRENT_OFFLINE=1, the shipped snapshot is used, and the result says why.
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
from dataclasses import dataclass
from pathlib import Path

RANKINGS_URL = "https://raw.githubusercontent.com/niklasmellgren/unrent/main/catalog/rankings.json"
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


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers=_headers(url))
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("larger than a rankings snapshot can be")
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


def _reason(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)
    return str(exc) or type(exc).__name__


def _cached(file: Path) -> tuple[dict | None, float]:
    """The cached snapshot and its age in seconds; (None, inf) when there is none."""
    try:
        return _snapshot(file.read_bytes()), time.time() - file.stat().st_mtime
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


def latest(shipped: dict) -> Rankings:
    """The newest of: the snapshot online, the cached copy of it, the shipped one.

    Pools the newer snapshot lacks keep their shipped ranking, so an install whose
    catalog has a pool main has since renamed still ranks it."""
    if os.environ.get("UNRENT_OFFLINE"):
        return Rankings(shipped, "shipped", "UNRENT_OFFLINE is set")
    file = cache_dir() / "rankings.json"
    data, age = _cached(file)
    source, note = "cache", None
    if age >= MAX_AGE:
        url = os.environ.get("UNRENT_RANKINGS_URL") or RANKINGS_URL
        try:
            body = _download(url)
            data, source = _snapshot(body), "online"
            _store(file, body)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            note = f"could not fetch the latest rankings: {_reason(exc)}"
    if data is None or data["generated"] < (shipped.get("generated") or ""):
        return Rankings(shipped, "shipped", note)
    merged = {**data, "pools": {**(shipped.get("pools") or {}), **data["pools"]}}
    return Rankings(merged, source, note)
