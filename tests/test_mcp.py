"""The MCP server and the rankings it fetches. No test touches the network: downloads
are replaced, and one that slips through fails the test."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from unrent import fresh  # noqa: E402
from unrent.cli import main  # noqa: E402

SHIPPED = json.loads((ROOT / "catalog" / "rankings.json").read_text("utf-8"))


def snapshot(generated: str, pools: dict | None = None) -> dict:
    return {"generated": generated, "momentum_days": 90, "pools": pools or {}}


def vector_db_led_by(repo: str) -> dict:
    """The shipped vector-db pool with `repo` moved to the top."""
    pool = json.loads(json.dumps(SHIPPED["pools"]["vector-db"]))
    pool["ranked"].sort(key=lambda e: e["repo"] != repo)
    return {"vector-db": pool}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("UNRENT_CACHE_DIR", str(tmp_path / "cache"))
    for var in ("UNRENT_OFFLINE", "UNRENT_RANKINGS_URL", "GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    def no_network(url):
        raise AssertionError(f"test tried to fetch {url}")

    monkeypatch.setattr(fresh, "_download", no_network)


class Remote:
    """Stands in for GitHub: serves one body and counts the requests."""

    def __init__(self, body):
        self.body, self.urls = body, []

    def __call__(self, url):
        self.urls.append(url)
        if isinstance(self.body, Exception):
            raise self.body
        return self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()


def serve(monkeypatch, body) -> Remote:
    remote = Remote(body)
    monkeypatch.setattr(fresh, "_download", remote)
    return remote


# ---------------------------------------------------------------- fresh rankings


def test_newer_online_rankings_win_and_keep_pools_they_lack(monkeypatch):
    remote = serve(monkeypatch, snapshot("2099-01-01", vector_db_led_by("qdrant/qdrant")))
    r = fresh.latest(SHIPPED)
    assert (r.source, r.date, r.note) == ("online", "2099-01-01", None)
    assert r.data["pools"]["vector-db"]["ranked"][0]["repo"] == "qdrant/qdrant"
    # A pool the fetched snapshot lacks keeps its shipped ranking.
    assert r.data["pools"]["llm-serving"] == SHIPPED["pools"]["llm-serving"]
    assert remote.urls == [fresh.RANKINGS_URL]


def test_fetched_rankings_are_cached(monkeypatch):
    remote = serve(monkeypatch, snapshot("2099-01-01"))
    fresh.latest(SHIPPED)
    r = fresh.latest(SHIPPED)
    assert (r.source, len(remote.urls)) == ("cache", 1)


def test_stale_cache_is_refetched(monkeypatch):
    remote = serve(monkeypatch, snapshot("2099-01-01"))
    fresh.latest(SHIPPED)
    old = time.time() - fresh.MAX_AGE - 60
    os.utime(fresh.cache_dir() / "rankings.json", (old, old))
    assert fresh.latest(SHIPPED).source == "online"
    assert len(remote.urls) == 2


def test_older_online_rankings_lose_to_the_shipped_ones(monkeypatch):
    serve(monkeypatch, snapshot("2000-01-01", vector_db_led_by("qdrant/qdrant")))
    r = fresh.latest(SHIPPED)
    assert (r.source, r.data) == ("shipped", SHIPPED)


def test_failed_fetch_falls_back_and_says_why(monkeypatch):
    serve(monkeypatch, urllib.error.URLError("no route to host"))
    r = fresh.latest(SHIPPED)
    assert (r.source, r.data) == ("shipped", SHIPPED)
    assert "no route to host" in r.note


def test_failed_fetch_uses_a_stale_cache_when_it_is_newer(monkeypatch):
    serve(monkeypatch, snapshot("2099-01-01"))
    fresh.latest(SHIPPED)
    old = time.time() - fresh.MAX_AGE - 60
    os.utime(fresh.cache_dir() / "rankings.json", (old, old))
    serve(monkeypatch, urllib.error.HTTPError(fresh.RANKINGS_URL, 503, "down", {}, None))
    r = fresh.latest(SHIPPED)
    assert (r.source, r.date) == ("cache", "2099-01-01")
    assert "HTTP 503" in r.note


@pytest.mark.parametrize(
    "body",
    [
        b"<html>rate limited</html>",
        b"\xff\xfe not utf-8",
        b"[]",
        json.dumps({"generated": "2099-01-01"}).encode(),
        json.dumps({"generated": "2099-01-01", "pools": {"x": {"ranked": "no"}}}).encode(),
    ],
)
def test_a_body_that_is_not_a_snapshot_is_rejected(monkeypatch, body):
    serve(monkeypatch, body)
    r = fresh.latest(SHIPPED)
    assert (r.source, r.data) == ("shipped", SHIPPED)
    assert r.note
    assert not (fresh.cache_dir() / "rankings.json").exists()


def test_offline_never_fetches(monkeypatch):
    monkeypatch.setenv("UNRENT_OFFLINE", "1")
    r = fresh.latest(SHIPPED)  # the autouse fixture fails any fetch
    assert (r.source, r.note) == ("shipped", "UNRENT_OFFLINE is set")


def test_rankings_url_can_be_overridden(monkeypatch):
    monkeypatch.setenv("UNRENT_RANKINGS_URL", "https://example.org/fork/rankings.json")
    remote = serve(monkeypatch, snapshot("2099-01-01"))
    fresh.latest(SHIPPED)
    assert remote.urls == ["https://example.org/fork/rankings.json"]


def test_a_token_only_goes_to_github_over_tls(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    assert fresh._headers(fresh.RANKINGS_URL)["Authorization"] == "token secret"
    for url in (
        "http://raw.githubusercontent.com/a/b/main/rankings.json",
        "https://example.org/rankings.json",
        "https://raw.githubusercontent.com.evil.example/rankings.json",
    ):
        assert "Authorization" not in fresh._headers(url)


def test_an_unwritable_cache_costs_nothing_but_a_refetch(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("UNRENT_CACHE_DIR", str(blocker / "cache"))  # under a file
    remote = serve(monkeypatch, snapshot("2099-01-01"))
    assert fresh.latest(SHIPPED).source == "online"
    assert fresh.latest(SHIPPED).source == "online"
    assert len(remote.urls) == 2


# ---------------------------------------------------------------- MCP server

try:
    import anyio
    import mcp

    from unrent import server
except ImportError:  # the mcp extra is not installed: the server tests skip
    mcp = None

pytestmark_mcp = pytest.mark.skipif(mcp is None, reason="needs the mcp extra")


@pytest.fixture(autouse=True)
def fresh_server_state():
    if mcp is not None:
        server._state.clear()
    yield
    if mcp is not None:
        server._state.clear()


def call(tool: str, **args):
    """Call a tool through a real MCP client session, in memory."""

    async def go():
        async with mcp.Client(server.server) as client:
            return await client.call_tool(tool, args)

    result = anyio.run(go)
    if result.is_error:
        return {"error": result.content[0].text}
    return result.structured_content


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "app"
    files = {
        "requirements.txt": "openai>=1\npinecone>=5\nfaiss-cpu\n",
        "main.py": "\n".join(
            ["from openai import OpenAI", "from pinecone import Pinecone", "import faiss"]
            + [f"client{i} = OpenAI()" for i in range(8)]
        ),
        ".env.example": "OPENAI_API_KEY=sk-live-abcdefghijklmnop\n",
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


@pytestmark_mcp
def test_the_server_lists_four_read_only_tools():
    async def go():
        async with mcp.Client(server.server) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in anyio.run(go)}
    assert set(tools) == {"scan", "alternatives", "standing", "catalog"}
    assert all(t.annotations.read_only_hint for t in tools.values())
    assert all(t.description and t.output_schema for t in tools.values())


@pytestmark_mcp
def test_scan_reports_services_evidence_and_standing(monkeypatch, project):
    monkeypatch.setenv("UNRENT_OFFLINE", "1")
    out = call("scan", path=str(project), top=1, evidence=2)
    found = {f["id"]: f for f in out["found"]}
    assert set(found) == {"openai", "pinecone"}
    openai = found["openai"]
    assert len(openai["evidence"]) == 2 and openai["evidence_total"] > 2
    assert "sk-live" not in json.dumps(out)
    assert openai["evidence"][0]["file"] == "requirements.txt"  # strongest first
    assert [p["repo"] for p in out["open_source"]] == ["facebookresearch/faiss"]
    standing = out["open_source"][0]["standing"][0]
    assert standing["ranked_by"].startswith("ranked by")
    assert len(standing["ahead"]) <= 1
    assert all(len(p["items"]) == 1 for p in out["alternatives"].values())
    assert out["rankings"] == {
        "date": SHIPPED["generated"],
        "source": "shipped",
        "note": "UNRENT_OFFLINE is set",
    }


@pytestmark_mcp
def test_scan_honours_skip_tests_and_exclude(monkeypatch, project):
    monkeypatch.setenv("UNRENT_OFFLINE", "1")
    (project / "tests").mkdir()
    (project / "tests" / "test_x.py").write_text("import anthropic\n", encoding="utf-8")
    (project / "examples").mkdir()
    (project / "examples" / "demo.py").write_text("import cohere\n", encoding="utf-8")
    ids = lambda out: {f["id"] for f in out["found"]}  # noqa: E731
    assert {"anthropic", "cohere"} <= ids(call("scan", path=str(project)))
    out = call("scan", path=str(project), skip_tests=True, exclude=["examples/"])
    assert ids(out) == {"openai", "pinecone"}
    assert ids(call("scan", path=str(project / "examples" / "demo.py"))) == {"cohere"}
    out = call("scan", path=str(project / "tests" / "test_x.py"), skip_tests=True)
    assert "is left out" in out["error"]


@pytestmark_mcp
@pytest.mark.parametrize(
    "args, message",
    [
        ({"path": "/definitely/not/here"}, "is not a file or directory"),
        ({"path": ".", "top": 0}, "top must be 1 or more"),
        ({"path": ".", "evidence": 0}, "evidence must be 1 or more"),
    ],
)
def test_scan_rejects_bad_input(monkeypatch, args, message):
    monkeypatch.setenv("UNRENT_OFFLINE", "1")
    assert message in call("scan", **args)["error"]


@pytestmark_mcp
@pytest.mark.parametrize(
    "query, pool",
    [
        ("pinecone", "vector-db"),
        ("Pinecone", "vector-db"),
        ("vector database", "vector-db"),
        ("vector-db", "vector-db"),
        ("speech to text", "speech-to-text"),
        ("ElevenLabs", "text-to-speech"),
    ],
)
def test_alternatives_by_service_or_category(monkeypatch, query, pool):
    monkeypatch.setenv("UNRENT_OFFLINE", "1")
    out = call("alternatives", query=query, top=2)
    assert pool in [p["id"] for p in out["pools"]]
    assert all(len(p["items"]) <= 2 for p in out["pools"])


@pytestmark_mcp
def test_alternatives_for_nothing_lists_the_pools(monkeypatch):
    monkeypatch.setenv("UNRENT_OFFLINE", "1")
    assert "Vector database" in call("alternatives", query="zzzz")["error"]
    assert "empty" in call("alternatives", query="  ")["error"]


@pytestmark_mcp
@pytest.mark.parametrize(
    "repo", ["faiss", "facebookresearch/faiss", "https://github.com/facebookresearch/faiss/"]
)
def test_standing_accepts_names_repos_and_urls(monkeypatch, repo):
    monkeypatch.setenv("UNRENT_OFFLINE", "1")
    out = call("standing", repo=repo, top=1)
    assert out["repo"] == "facebookresearch/faiss"
    assert "library" in out["kind"]
    vector = next(s for s in out["standing"] if s["pool"] == "vector-db")
    assert vector["pool_name"] == "Vector database"
    assert vector["rank"] >= 1 and len(vector["ahead"]) <= 1


@pytestmark_mcp
def test_standing_of_an_unknown_repo_is_an_error(monkeypatch):
    monkeypatch.setenv("UNRENT_OFFLINE", "1")
    assert "not in any unrent pool" in call("standing", repo="nope/nope")["error"]


@pytestmark_mcp
def test_catalog_summary_and_search(monkeypatch):
    monkeypatch.setenv("UNRENT_OFFLINE", "1")
    summary = call("catalog")
    assert summary["closed_services"] == sum(summary["categories"].values())
    assert "vector-db" in [p["id"] for p in summary["pools"]]
    hits = call("catalog", query="cohere")
    cohere = next(m for m in hits["matches"] if m["id"] == "cohere")
    assert "cohere" in cohere["signatures"]["requirement"]
    qdrant = next(m for m in call("catalog", query="qdrant")["matches"] if m["open_source"])
    assert qdrant["repo"] == "qdrant/qdrant" and qdrant["kind"]


@pytestmark_mcp
def test_newer_rankings_reach_every_tool(monkeypatch, project):
    shipped_first = SHIPPED["pools"]["vector-db"]["ranked"][0]["repo"]
    leader = next(
        e["repo"] for e in SHIPPED["pools"]["vector-db"]["ranked"] if e["repo"] != shipped_first
    )
    serve(monkeypatch, snapshot("2099-01-01", vector_db_led_by(leader)))
    out = call("alternatives", query="vector database", top=1)
    assert out["pools"][0]["items"][0]["name"] == leader
    assert out["rankings"] == {"date": "2099-01-01", "source": "online"}
    assert call("standing", repo=leader)["standing"][0]["rank"] == 1
    assert (
        call("scan", path=str(project))["alternatives"]["vector-db"]["items"][0]["name"] == leader
    )


@pytestmark_mcp
def test_rankings_that_do_not_load_fall_back_to_the_shipped_ones(monkeypatch):
    broken = {"vector-db": {"ranked_by": "stars", "ranked": [{"what": "no repo, no url"}]}}
    serve(monkeypatch, snapshot("2099-01-01", broken))
    out = call("alternatives", query="vector database")
    assert out["rankings"]["source"] == "shipped"
    assert "did not load" in out["rankings"]["note"]
    assert out["pools"][0]["items"]


@pytestmark_mcp
def test_the_catalog_is_reloaded_only_when_rankings_may_have_changed(monkeypatch):
    remote = serve(monkeypatch, snapshot("2099-01-01"))
    for _ in range(3):
        call("catalog")
    assert len(remote.urls) == 1


def test_mcp_command_without_the_extra_says_how_to_install(monkeypatch, capsys):
    for name in [n for n in sys.modules if n == "mcp" or n.startswith("mcp.")] + ["mcp"]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.delitem(sys.modules, "unrent.server", raising=False)
    assert main(["mcp"]) == 2
    assert "unrent[mcp]" in capsys.readouterr().err


@pytestmark_mcp
def test_mcp_over_stdio(monkeypatch, tmp_path):
    """The real thing: `unrent mcp` in a subprocess, spoken to as a client would."""
    env = {**os.environ, "UNRENT_OFFLINE": "1", "PYTHONPATH": str(ROOT)}
    params = mcp.StdioServerParameters(
        command=sys.executable, args=["-m", "unrent.cli", "mcp"], env=env, cwd=str(tmp_path)
    )

    async def go():
        async with mcp.Client(params) as client:
            tools = await client.list_tools()
            result = await client.call_tool("alternatives", {"query": "pinecone", "top": 1})
            return [t.name for t in tools.tools], result

    names, result = anyio.run(go)
    assert sorted(names) == ["alternatives", "catalog", "scan", "standing"]
    assert not result.is_error
    assert result.structured_content["pools"][0]["id"] == "vector-db"
