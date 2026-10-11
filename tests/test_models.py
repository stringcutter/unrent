"""Models the code runs on, and the open-weight models LMArena rates as good."""

import datetime
import json
import shutil
from pathlib import Path

import pytest

from unrent import cli
from unrent.catalog import load_catalog, model_key, model_size
from unrent.detect import collect_facts, match
from unrent.models import in_use, matches
from unrent.render import payload, to_markdown
from unrent.terminal import to_terminal

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
        (root / name).parent.mkdir(parents=True, exist_ok=True)
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
        ("mimo-v2-flash (non-thinking)", "mimo-v2-flash"),
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


AS_OF = datetime.date(2026, 10, 11)


def scanned(root: Path, catalog, files: dict[str, str]):
    write(root, files)
    facts = collect_facts(root, catalog)
    findings = match(facts, catalog)
    return findings, in_use(findings, facts, root, catalog)


AGENT = {
    "requirements.txt": "anthropic\nollama\n",
    "agent.py": (
        "import anthropic\nimport ollama\n"
        'MODEL = "claude-opus-5-5"\n'
        "client = anthropic.Anthropic()\n"
        'ollama.chat(model="gemma4:31b", messages=[])\n'
    ),
}


def test_terminal_shows_each_model_with_the_open_ones_as_good(tmp_path, catalog):
    findings, models = scanned(tmp_path, catalog, AGENT)
    out = to_terminal(findings, tmp_path, catalog, models=models, as_of=AS_OF)
    qwen = catalog.arena[model_key("qwen3.8-27b")].display
    lines = [line.rstrip() for line in out.splitlines()]
    at = lines.index("│ model  claude-opus-5.5  agent.py:3")
    # None reaches Opus: the closest below, said so; the ratings are for --why and JSON.
    assert lines[at + 1] == "         → closest: mimo-v2.6-pro, kimi-k3 (Kimi K3 license), glm-5.3"
    assert f"         → {qwen}" in lines
    assert not any(line.lstrip().startswith("arena") for line in lines)
    assert "1 string attached." in out  # a model is not a string
    assert "model ratings LMArena (CC BY 4.0)" in out
    # Without models, the view is as it was.
    assert "│ model" not in to_terminal(findings, tmp_path, catalog, as_of=AS_OF)


def test_terminal_shows_five_models_and_counts_the_rest(tmp_path, catalog):
    names = ["mimo-v2.6-pro", "kimi-k3-max", "glm-5.3-max", "gemma-4-31b", "qwen3.8-27b",
             "big-open-400b", "tiny-open-4b"]  # fmt: skip
    # One service each, so no file or directory lists six models.
    files = {
        f"svc{i}/run.py": f'import ollama\nollama.chat(model="{n}")\n' for i, n in enumerate(names)
    }
    findings, models = scanned(tmp_path, catalog, files)
    assert len(models) == 7
    out = to_terminal(findings, tmp_path, catalog, models=models, as_of=AS_OF)
    assert out.count("│ model") == 5
    assert "2 more models in use" in out
    # The best rated open model has nothing as good: no arrow, no picks.
    assert "the best open model of its size on LMArena" in out
    assert "→ the best" not in out


def test_a_retiring_model_keeps_its_snaps_row_with_its_nearest_open_peer(tmp_path):
    rankings = json.loads((CATALOG_DIR / "rankings.json").read_text("utf-8"))
    extra = [arena("gpt-4-turbo-2024-04-09", "Proprietary", 1270)]
    rankings["arena"] = {"date": "2026-10-08", "models": ARENA + extra}
    catalog = load_catalog(CATALOG_DIR, rankings)
    findings, models = scanned(
        tmp_path,
        catalog,
        {"main.py": 'from openai import OpenAI\nclient = OpenAI()\nMODEL = "gpt-4-turbo"\n'},
    )
    assert [u.model.name for u in models] == ["gpt-4-turbo-2024-04-09"]
    out = to_terminal(findings, tmp_path, catalog, models=models, as_of=AS_OF)
    assert "│ model" not in out
    lines = [line.rstrip() for line in out.splitlines()]
    at = next(i for i, line in enumerate(lines) if line.startswith("┆ snaps  gpt-4-turbo"))
    assert lines[at + 2].split() == ["open", "tiny-open-4b"]  # 1300 for 1270, not the best
    assert "model ratings LMArena (CC BY 4.0)" in out


def test_why_a_model_lists_its_lines_ratings_and_the_credit(tmp_path, catalog, monkeypatch, capsys):
    write(tmp_path, AGENT)
    monkeypatch.setattr(cli, "load_catalog", lambda *_: catalog)
    assert cli.main(["scan", str(tmp_path), "--why", "gemma4:31b", "-f", "terminal"]) == 0
    out = capsys.readouterr().out
    assert "agent.py:5" in out
    assert "LMArena 1443 (1437-1449), 5,000 votes, as gemma-4-31b" in out
    assert "Open weights as good" in out
    assert "1441 (1435-1447), Apache 2.0" in out
    assert out.rstrip().endswith(
        "ratings: LMArena text leaderboard, overall, 2026-10-08 (CC BY 4.0)\n"
        "  https://huggingface.co/datasets/lmarena-ai/leaderboard-dataset"
    )
    # A service still wins on its name.
    assert cli.main(["scan", str(tmp_path), "--why", "anthropic", "-f", "terminal"]) == 0
    assert "Anthropic API" in capsys.readouterr().out


def test_json_lists_models_in_use_and_credits_the_ratings(tmp_path, catalog):
    findings, models = scanned(tmp_path, catalog, AGENT)
    out = payload(findings, tmp_path, catalog, models=models)
    assert out["model_ratings"] == {
        "source": "LMArena text leaderboard, overall",
        "dataset": "lmarena-ai/leaderboard-dataset",
        "licence": "CC-BY-4.0",
        "date": "2026-10-08",
    }
    opus = next(m for m in out["models_in_use"] if m["name"] == "claude-opus-5.5-high")
    assert opus["written"] == "claude-opus-5-5" and opus["service"] == "anthropic"
    assert opus["where"] == ["agent.py:3"] and opus["close"] and not opus["open"]
    assert opus["rating"] == 1515 and opus["votes"] == 5000
    kimi = opus["picks"][1]
    assert set(kimi) == {"model", "name", "licence", "open_licence", "rating", "low", "high",
                         "params", "url"}  # fmt: skip
    assert kimi["name"] == "kimi-k3-max" and not kimi["open_licence"]
    gemma = next(m for m in out["models_in_use"] if m["name"] == "gemma-4-31b")
    assert gemma["service"] is None and gemma["picks"][0]["params"] is not None
    assert payload(findings, tmp_path, catalog)["models_in_use"] == []


def test_markdown_has_a_models_section_with_the_credit(tmp_path, catalog):
    findings, models = scanned(tmp_path, catalog, AGENT)
    out = to_markdown(findings, tmp_path, catalog, models=models)
    section = out.split("## Models in use")[1].split("\n## ")[0]
    row = "| `claude-opus-5.5` | `agent.py:3` | 1515 (1509-1521) | closest, none as good: "
    assert row in section
    assert "kimi-k3 1476 (Kimi K3 license)" in section
    assert "(https://huggingface.co/datasets/lmarena-ai/leaderboard-dataset), 2026-10-08" in section
    assert out.index("## Models in use") < out.index("## Open source alternatives")
    assert "## Models in use" not in to_markdown(findings, tmp_path, catalog)


def test_a_file_or_directory_that_sets_six_models_lists_them(tmp_path, catalog):
    names = ["mimo-v2.6-pro", "kimi-k3-max", "glm-5.3-max", "gemma-4-31b", "qwen3.8-27b",
             "big-open-400b"]  # fmt: skip
    one_file = "".join(f'PROFILES["{n}"] = {{"model": "{n}"}}\n' for n in names)
    per_file = {
        f"providers/p{i}.py": f'checkModel = {{"model": "{n}"}}\n' for i, n in enumerate(names)
    }
    for files in ({"settings.py": one_file}, per_file):
        root = tmp_path / str(len(files))
        root.mkdir()
        assert scanned(root, catalog, files)[1] == []
