"""unrent as an MCP server: the scanner and the rankings, as tools for coding agents.

Run with `unrent mcp` (stdio). Rankings come from the unrent repository, at most a
week old (see fresh.py); the code being scanned never leaves the machine.
"""

from __future__ import annotations

import collections
import json
import re
import time
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import fresh
from .catalog import Catalog, CatalogError, Pool, Service, load_catalog
from .cli import DEFAULT_CATALOG, _version
from .detect import collect_facts, match
from .discover import scan_unknown
from .render import (
    RANKED_BY,
    Standing,
    alternative_json,
    payload,
    standing_json,
    standings,
    standings_of,
)

RETRY_AFTER = 15 * 60  # seconds before a failed fetch is tried again
MATCHES_SHOWN = 25

INSTRUCTIONS = """\
unrent finds the closed AI services a codebase depends on (LLM APIs, vector databases,
embeddings, OCR, speech, observability, ...), with file and line for every finding, and
ranks the open source that could replace them. It also ranks the open source AI
components a codebase already runs against the rest of their field.

Use `scan` on a project directory for the full picture. Use `alternatives` for the
current best open source in a category or for a named closed service, `standing` for
where one open source project ranks, and `catalog` to see what unrent recognises.

Know its limits:
- It detects the services in its catalog and nothing else. An API host, base URL or
  *_API_KEY in the code that no finding explains may be a closed service it does not
  know.
- `models_named` entries rest on model names alone (a token-limit table, a model menu),
  `env_template_only` entries on a key in an example env file: check the code before
  calling them dependencies.
- Each pool says how it is ranked (`ranked_by`): GitHub pools by stars gained over 90
  days once enough weekly history exists, by total stars until then; Hugging Face pools
  by trending, whatever the model's size. Stars, licences and activity are checked
  against GitHub at each weekly refresh.
- `open_core` marks projects where part of the repo is under a licence that is not open.

unrent lists and ranks; whether a switch makes sense is for you and the user to judge."""

server = MCPServer("unrent", version=_version(), instructions=INSTRUCTIONS)

_state: dict[str, Any] = {}


def _shipped() -> dict:
    file = DEFAULT_CATALOG / "rankings.json"
    try:
        return json.loads(file.read_text("utf-8")) if file.is_file() else {}
    except (OSError, ValueError):
        return {}


def _catalog() -> tuple[Catalog, fresh.Rankings]:
    """The catalog with the newest rankings, reloaded when they may have changed."""
    if _state and time.monotonic() < _state["until"]:
        return _state["catalog"], _state["rankings"]
    shipped = _shipped()
    rankings = fresh.latest(shipped)
    try:
        catalog = load_catalog(DEFAULT_CATALOG, rankings.data)
    except (CatalogError, KeyError, TypeError, ValueError, AttributeError) as exc:
        if rankings.source == "shipped":
            raise ToolError(f"the unrent catalog did not load: {exc}") from exc
        rankings = fresh.Rankings(shipped, "shipped", f"the latest rankings did not load: {exc}")
        catalog = load_catalog(DEFAULT_CATALOG, shipped)
    ttl = fresh.MAX_AGE if rankings.note is None else RETRY_AFTER
    _state.update(catalog=catalog, rankings=rankings, until=time.monotonic() + ttl)
    return catalog, rankings


def _rankings_json(rankings: fresh.Rankings) -> dict:
    out = {"date": rankings.date, "source": rankings.source}
    if rankings.note:
        out["note"] = rankings.note
    return out


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _ranked_by(value: str | None) -> str:
    """Spelled out, so an agent can tell momentum from total stars."""
    return RANKED_BY.get(value, value)


def _standing(s: Standing, top: int) -> dict:
    out = {"pool_name": s.pool.name, **standing_json(s, ahead=top)}
    out["ranked_by"] = _ranked_by(out["ranked_by"])
    if len(s.ahead) > top:
        out["ahead_total"] = len(s.ahead)
    return out


def _pool_json(pool: Pool, top: int) -> dict:
    return {
        "id": pool.id,
        "name": pool.name,
        "ranked_by": _ranked_by(pool.ranked_by),
        "size": len(pool.alternatives),
        "items": [alternative_json(a) for a in pool.alternatives[:top]],
    }


def _capped(entries: list[dict], key: str, limit: int) -> None:
    for entry in entries:
        items = entry[key]
        if len(items) > limit:
            entry[key] = items[:limit]
            entry[f"{key}_total"] = len(items)


def _positive(name: str, value: int) -> None:
    if value < 1:
        raise ToolError(f"{name} must be 1 or more")


READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)


@server.tool(annotations=READ_ONLY, structured_output=True)
def scan(
    path: str,
    skip_tests: bool = False,
    exclude: list[str] | None = None,
    top: int = 3,
    evidence: int = 5,
) -> dict[str, Any]:
    """Scan a project directory for closed AI services and the open source AI it runs.

    Returns `found` (closed services the code depends on, each with file:line evidence
    and the pools that replace it), `models_named` (closed model ids with no SDK, key,
    host or package behind them: not dependencies), `models_retiring` (model ids the
    code selects that the vendor has retired, `snapped`, or will retire on `retires`,
    `snaps`, with `use_instead`), `env_template_only` (keys only in
    an example env file: not dependencies), `open_source` (components already
    in use and their rank in their pool), `unknown_candidates` (API hosts and keys that
    no catalog entry explains and that look like a hosted AI API: closed services
    unrent may not know yet, to check one by one), and `alternatives` (the top open
    source per pool). Secrets in evidence are masked.

    path: directory to scan; absolute, or relative to where the server runs.
    skip_tests: leave out test, spec and fixture code.
    exclude: .gitignore-style patterns to skip, e.g. ["examples/", "*.ipynb"].
    top: alternatives and projects-ranked-above listed per pool.
    evidence: locations listed per finding (the total is given when there are more).
    """
    _positive("top", top)
    _positive("evidence", evidence)
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ToolError(f"{path} is not a directory")
    catalog, rankings = _catalog()
    skipped: list[Path] = []
    try:
        facts = collect_facts(root, catalog, list(exclude or []), skipped, skip_tests=skip_tests)
    except OSError as exc:
        raise ToolError(f"could not scan {path}: {exc}") from exc
    findings = match(facts, catalog)
    unknown = scan_unknown(root, catalog, list(exclude or []), skip_tests=skip_tests)
    result = payload(findings, root, catalog, skipped, unknown)
    for key in ("found", "models_named", "models_retiring", "env_template_only", "open_source"):
        _capped(result[key], "evidence", evidence)
    running = [f for f in findings if f.service.open_source]
    for entry, f in zip(result["open_source"], running, strict=True):
        entry["standing"] = [_standing(s, top) for s in standings(f, catalog)]
    for pool in result["alternatives"].values():
        pool["ranked_by"] = _ranked_by(pool["ranked_by"])
        _capped([pool], "items", top)
    result["rankings"] = _rankings_json(rankings)
    del result["rankings_date"]
    return result


def _pools_for(query: str, catalog: Catalog) -> tuple[list[Pool], list[Service]]:
    q = _norm(query)
    exact_pools = [p for p in catalog.pools.values() if q in (_norm(p.id), _norm(p.name))]
    exact_services = [
        s
        for s in catalog.detectable
        if q
        in (_norm(s.id), _norm(s.name), _norm(s.repo or ""), _norm((s.repo or "/").split("/")[1]))
    ]
    pools, services = exact_pools, exact_services
    if not pools and not services:
        pools = [p for p in catalog.pools.values() if q in _norm(p.name) or q in _norm(p.id)]
        services = [s for s in catalog.detectable if q in _norm(s.name) or q in _norm(s.id)]
    for s in services:
        pools += [catalog.pools[p] for p in s.replace_with if catalog.pools[p] not in pools]
    return pools, services


@server.tool(annotations=READ_ONLY, structured_output=True)
def alternatives(query: str, top: int = 5) -> dict[str, Any]:
    """The current best open source for a category or a closed service, ranked.

    query: a category or pool ("vector database", "speech-to-text", "llm gateway") or a
      closed service ("Pinecone", "OpenAI API", "elevenlabs").
    top: entries per pool.

    GitHub pools are ranked by stars gained in the last 90 days (total stars until
    enough history exists; see each pool's `ranked_by`), Hugging Face pools by
    trending. Open source only: OSI licence for code (`open_core` marks a part that is
    not), open licence for weights, no archived projects or projects without a push in
    a year.
    """
    _positive("top", top)
    if not query.strip():
        raise ToolError("query is empty; call `catalog` to see the pools")
    catalog, rankings = _catalog()
    pools, services = _pools_for(query, catalog)
    if not pools:
        names = ", ".join(sorted(p.name for p in catalog.pools.values()))
        raise ToolError(f"nothing matches {query!r}. Pools: {names}")
    return {
        "query": query,
        "services": [{"id": s.id, "name": s.name, "open_source": s.open_source} for s in services][
            :MATCHES_SHOWN
        ],
        "pools": [_pool_json(p, top) for p in pools],
        "rankings": _rankings_json(rankings),
    }


def _find_repo(query: str, catalog: Catalog) -> tuple[str, tuple[str, ...]] | None:
    q = query.strip().rstrip("/").lower()
    q = re.sub(r"^(https?://)?(www\.)?github\.com/", "", q)
    names = {a.name.lower(): a for p in catalog.pools.values() for a in p.alternatives}
    kinds = {s.repo.lower(): s for s in catalog.projects}
    if q in kinds:
        return kinds[q].repo, kinds[q].kind
    if q in names:
        return names[q].name, names[q].kind
    if "/" in q:
        return None
    by_name = {
        n: a
        for n, a in names.items()
        if n.split("/")[-1] == q or _norm(n.split("/")[-1]) == _norm(q)
    }
    if len(by_name) == 1:
        a = next(iter(by_name.values()))
        project = kinds.get(a.name.lower())
        return a.name, project.kind if project else a.kind
    return None


@server.tool(annotations=READ_ONLY, structured_output=True)
def standing(repo: str, top: int = 5) -> dict[str, Any]:
    """Where one open source project ranks in its field, overall and among projects of
    the same kind (library, server, embedded, ...), with the projects ranked above it.

    repo: "owner/name" on GitHub ("qdrant/qdrant"), a GitHub URL, or a bare name when
      it is unambiguous ("faiss").
    top: projects ranked above it listed per pool.
    """
    _positive("top", top)
    catalog, rankings = _catalog()
    found = _find_repo(repo, catalog)
    if found is None:
        raise ToolError(
            f"{repo!r} is not in any unrent pool. unrent ranks the projects in its "
            "catalog; call `catalog` with a name to search it."
        )
    name, kind = found
    project = next((s for s in catalog.projects if s.repo == name), None)
    pools = [p for p in catalog.pools.values() if any(a.name == name for a in p.alternatives)]
    if project:
        pools += [catalog.pools[p] for p in project.replace_with if catalog.pools[p] not in pools]
    return {
        "repo": name,
        "kind": list(kind),
        "url": f"https://github.com/{name}",
        "standing": [_standing(s, top) for s in standings_of(name, kind, pools)],
        "rankings": _rankings_json(rankings),
    }


def _service_json(s: Service, catalog: Catalog) -> dict:
    out = {"id": s.id, "name": s.name, "category": s.category, "open_source": s.open_source}
    if s.open_source:
        out.update(repo=s.repo, kind=list(s.kind))
    out["pools"] = [catalog.pools[p].name for p in s.replace_with]
    out["signatures"] = {k: list(v) for k, v in s.detect.items()}
    return out


@server.tool(annotations=READ_ONLY, structured_output=True)
def catalog(query: str = "") -> dict[str, Any]:
    """What unrent recognises. With no query: counts, categories and pools. With a
    query: the matching closed services and open source projects, with the signatures
    (packages, imports, hosts, env vars, images, ...) that detect them.

    query: a name, id, category or repo, e.g. "cohere", "Vector database", "qdrant".
    """
    cat, rankings = _catalog()
    if not query.strip():
        categories = collections.Counter(s.category for s in cat.services)
        return {
            "closed_services": len(cat.services),
            "categories": dict(sorted(categories.items())),
            "open_source_projects": len(cat.projects),
            "pools": [
                {
                    "id": p.id,
                    "name": p.name,
                    "source": p.source,
                    "ranked_by": _ranked_by(p.ranked_by),
                    "size": len(p.alternatives),
                }
                for p in cat.pools.values()
            ],
            "rankings": _rankings_json(rankings),
        }
    q = _norm(query)
    hits = [
        s
        for s in cat.detectable
        if any(q in _norm(field) for field in (s.id, s.name, s.category, s.repo or ""))
    ]
    return {
        "query": query,
        "matches": [_service_json(s, cat) for s in hits[:MATCHES_SHOWN]],
        "total": len(hits),
        "rankings": _rankings_json(rankings),
    }


def run() -> None:
    server.run("stdio")
