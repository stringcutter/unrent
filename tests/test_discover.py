"""Candidates for closed AI services the catalog does not know (unrent/discover.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from unrent.catalog import load_catalog  # noqa: E402
from unrent.cli import main  # noqa: E402
from unrent.discover import registered_domain, scan_unknown  # noqa: E402


@pytest.fixture(scope="session")
def catalog():
    return load_catalog(ROOT / "catalog")


def write(root: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
    return root


def names(root: Path, catalog) -> dict[str, dict]:
    return {c["name"]: c for c in scan_unknown(root, catalog)}


def test_registered_domain():
    assert registered_domain("api.typesafe.ai") == "typesafe.ai"
    assert registered_domain("search.infoquest.bytepluses.com") == "bytepluses.com"
    assert registered_domain("api.example.com.cn") == "example.com.cn"


def test_unknown_ai_api_with_its_key(tmp_path, catalog):
    write(
        tmp_path,
        {
            "app/rank.py": (
                "import os, httpx\n"
                'KEY = os.environ["NEWVENDOR_API_KEY"]\n'
                "def score(prompt):\n"
                '    return httpx.post("https://api.newvendor.ai/v1/classify",\n'
                "                      json={'model': 'nv-1', 'prompt': prompt})\n"
            ),
        },
    )
    got = names(tmp_path, catalog)
    assert set(got) == {"newvendor.ai"}
    c = got["newvendor.ai"]
    assert c["hosts"] == ["api.newvendor.ai"] and c["settings"] == ["NEWVENDOR_API_KEY"]
    assert c["evidence"][0]["file"] == "app/rank.py"


def test_known_and_non_ai_hosts_are_not_candidates(tmp_path, catalog):
    write(
        tmp_path,
        {
            "a.py": (
                'OPENAI = "https://api.openai.com/v1/chat/completions"  # in the catalog\n'
                'PAY = "https://api.stripe.com/v1/charges"  # not AI\n'
                'DOCS = "https://github.com/acme/llm"\n'
                "# https://api.commentonly.ai/v1/models is only in a comment\n"
            ),
            "docs/guide.md": "Call https://api.inprose.ai/v1/chat/completions with a model.\n",
        },
    )
    assert names(tmp_path, catalog) == {}


def test_test_only_hosts_and_code_constants_are_left_out(tmp_path, catalog):
    write(
        tmp_path,
        {
            "tests/test_client.py": 'URL = "https://api.fixture.ai/v1/chat/completions"  # model\n',
            "app/keys.py": 'TOOL_META_KEY = "tool_meta"\nRESULT_API_KEY = "result"  # model\n',
        },
    )
    assert names(tmp_path, catalog) == {}


def test_generated_provider_catalog_is_one_candidate(tmp_path, catalog):
    rows = "\n".join(
        f'  {{ id: "p{i}", baseUrl: "https://api.vendor{i}.ai/v1", models: [] }},'
        for i in range(20)
    )
    write(tmp_path, {"src/providers.generated.ts": f"export const P = [\n{rows}\n];\n"})
    got = scan_unknown(tmp_path, catalog)
    assert [c["kind"] for c in got] == ["provider catalog"]
    assert got[0]["hosts"] == 20 and got[0]["name"] == "src/providers.generated.ts"


def test_scan_json_and_markdown_carry_candidates(tmp_path, catalog, capsys):
    write(
        tmp_path,
        {
            "main.py": 'import os\nos.getenv("ACME_API_KEY")\nURL = "https://api.acme.ai/v1/embeddings"\n'
        },
    )
    assert main(["scan", str(tmp_path), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [c["name"] for c in data["unknown_candidates"]] == ["acme.ai"]
    assert data["found"] == []  # a candidate is never a finding
    assert main(["scan", str(tmp_path)]) == 0
    md = capsys.readouterr().out
    assert "## Possibly closed AI services unrent does not know" in md and "acme.ai" in md
