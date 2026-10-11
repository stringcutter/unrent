"""Models the code runs on, and the open-weight models LMArena rates as good."""

import json
import shutil
from pathlib import Path

import pytest

from unrent.catalog import load_catalog, model_key, model_size
from unrent.detect import collect_facts, match
from unrent.models import in_use, matches

CATALOG_DIR = Path(__file__).resolve().parents[1] / "catalog"


def arena(name, licence, rating, spread=6, votes=5000, org="x"):
    return {"model": name, "org": org, "licence": licence, "rating": rating,
            "low": rating - spread, "high": rating + spread, "votes": votes}  # fmt: skip


ARENA = [
    arena("claude-opus-5.5-high", "Proprietary", 1515),
    arena("claude-opus-5.5-low", "Proprietary", 1490),
    arena("mimo-v2.6-pro", "MIT", 1492),
    arena("kimi-k3-max", "Kimi K3 license", 1476),
    arena("glm-5.3-max", "MIT", 1471),
    arena("research-model-70b", "CC-BY-NC-4.0", 1500),
    arena("gemma-4-31b", "Apache 2.0", 1443),
    arena("qwen3.8-27b", "Apache 2.0", 1441),
    arena("big-open-400b", "MIT", 1450),
    arena("tiny-open-4b", "MIT", 1300),
]


@pytest.fixture(scope="module")
def catalog():
    rankings = json.loads((CATALOG_DIR / "rankings.json").read_text("utf-8"))
    rankings["arena"] = {"date": "2026-10-08", "models": ARENA}
    return load_catalog(CATALOG_DIR, rankings)


@pytest.fixture(autouse=True, params=["python", "ripgrep"])
def search_mode(request, monkeypatch):
    if request.param == "ripgrep":
        if not shutil.which("rg"):
            pytest.skip("ripgrep not installed")
        monkeypatch.delenv("UNRENT_NO_RIPGREP", raising=False)
    else:
        monkeypatch.setenv("UNRENT_NO_RIPGREP", "1")


def write(root: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")
    return root


def test_one_key_for_one_model_however_it_is_written():
    same = [
        ("claude-opus-5-5", "claude-opus-5.5-high"),
        ("gemma4:31b", "google/gemma-4-31b-it"),
        ("gemma4:31b", "gemma-4-31b"),
        ("Qwen/Qwen3.8-27B", "qwen3.8-27b"),
        ("qwen3.8:latest", "qwen3.8"),
        ("deepseek-v4-pro-high-20260813", "deepseek-v4-pro"),
    ]
    for a, b in same:
        assert model_key(a) == model_key(b), (a, b)
    assert model_key("gpt-5.6-sol") != model_key("gpt-5.6-terra")
    assert model_size("nvidia-nemotron-3-ultra-550b-a55b") == 550
    assert model_size("claude-opus-5.5") is None


def test_variants_merge_into_the_best_rated(catalog):
    opus = catalog.arena[model_key("claude-opus-5-5")]
    assert opus.name == "claude-opus-5.5-high" and opus.rating == 1515
    assert not opus.open
    kimi = catalog.arena[model_key("kimi-k3")]
    assert kimi.open and not kimi.open_licence  # open weights, its maker's own licence
    assert not catalog.arena[model_key("research-model-70b")].open  # non-commercial


def test_a_closed_model_gets_the_best_open_ones_marked_close_when_none_reach_it(catalog):
    m = matches(catalog.arena[model_key("claude-opus-5-5")], catalog)
    assert [a.name for a in m.picks] == ["mimo-v2.6-pro", "kimi-k3-max", "glm-5.3-max"]
    assert m.close  # 1492 + 6 does not reach 1515 - 6
    assert "research-model-70b" not in {a.name for a in m.picks}


def test_an_open_model_gets_open_ones_as_good_and_no_bigger(catalog):
    m = matches(catalog.arena[model_key("gemma-4-31b")], catalog)
    names = [a.name for a in m.picks]
    assert names == ["qwen3.8-27b"]  # within the interval and smaller; the 400B is too big
    assert not m.close
    assert matches(catalog.arena[model_key("tiny-open-4b")], catalog).picks == []


def test_models_in_use_are_the_ones_a_line_selects(tmp_path, catalog):
    write(
        tmp_path,
        {
            "requirements.txt": "anthropic\nollama\n",
            "agent.py": (
                "import anthropic\nimport ollama\n"
                'MODEL = "claude-opus-5-5"\n'
                "client = anthropic.Anthropic()\n"
                'ollama.chat(model="gemma4:31b", messages=[])\n'
                'MENU = ["qwen3.8-27b", "glm-5.3"]\n'  # a menu selects nothing
                "def substring(): pass\n"
            ),
            "compose.yaml": "services:\n  llm:\n    command: --model Qwen/Qwen3.8-27B\n",
            "setup.sh": "ollama pull gemma4:31b\n",
        },
    )
    facts = collect_facts(tmp_path, catalog)
    uses = {u.model.name: u for u in in_use(match(facts, catalog), facts, tmp_path, catalog)}
    assert set(uses) == {"claude-opus-5.5-high", "gemma-4-31b", "qwen3.8-27b"}
    assert [(s.file.name, s.line) for s in uses["gemma-4-31b"].sites] == [
        ("agent.py", 5),
        ("setup.sh", 1),
    ]
    assert uses["qwen3.8-27b"].written == "Qwen/Qwen3.8-27B"
    assert uses["claude-opus-5.5-high"].finding.service.id == "anthropic"
