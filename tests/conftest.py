import pytest


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    """The shipped rankings and retirements, and a cache of the test's own: no test
    fetches or touches the user's cache. tests/test_mcp.py undoes the first, with the
    downloads faked."""
    monkeypatch.setenv("UNRENT_OFFLINE", "1")
    monkeypatch.setenv("UNRENT_CACHE_DIR", str(tmp_path / "unrent-cache"))
