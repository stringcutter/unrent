"""Tests. Detection is the product, so most of these pin down one edge case each."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lockin.catalog import OPEN_LICENCES, OPEN_MODEL_LICENCES, load_catalog  # noqa: E402
from lockin.cli import main  # noqa: E402
from lockin.detect import collect_facts, js_package, match  # noqa: E402
from lockin.report import to_json, to_markdown  # noqa: E402

CATALOG_DIR = ROOT / "catalog"


@pytest.fixture(scope="session")
def catalog():
    return load_catalog(CATALOG_DIR)


def write(root: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
    return root


def found(root: Path, catalog, **kw) -> dict[str, list]:
    """Service id → cited facts."""
    return {f.service.id: f.cited for f in match(collect_facts(root, catalog, **kw), catalog)}


@pytest.fixture(autouse=True, params=["python", "ripgrep"])
def search_mode(request, monkeypatch):
    """Every detection test runs with and without ripgrep: results must not differ."""
    if request.param == "ripgrep":
        if not shutil.which("rg"):
            pytest.skip("ripgrep not installed")
        monkeypatch.delenv("LOCKIN_NO_RIPGREP", raising=False)
    else:
        monkeypatch.setenv("LOCKIN_NO_RIPGREP", "1")
    return request.param


# --- catalog ------------------------------------------------------------------


def test_catalog_loads(catalog):
    assert len(catalog) > 80
    assert catalog.pools


def test_every_service_has_ranked_alternatives(catalog):
    for service in catalog.services:
        for pool in catalog.alternatives_for(service):
            assert pool.alternatives, f"{service.id} → {pool.id} is empty"


def test_rankings_offer_only_open_source(catalog):
    for pool in catalog.pools.values():
        for alt in pool.alternatives:
            allowed = OPEN_LICENCES if pool.source == "github" else OPEN_MODEL_LICENCES
            assert alt.licence in allowed, f"{pool.id}: {alt.name} is {alt.licence}"


def test_catalog_command_validates(capsys):
    assert main(["catalog", "--validate"]) == 0
    assert "closed AI services" in capsys.readouterr().out


# --- Python ecosystem ---------------------------------------------------------


def test_requirements_variants(tmp_path, catalog):
    write(tmp_path, {
        "requirements.txt": "Pinecone_Client[grpc]>=5 ; python_version>'3.8'\n-r base.txt\n",
        "requirements/prod.txt": "anthropic==0.40\n",
        "setup.py": "from setuptools import setup\nsetup(install_requires=['cohere>=5'])\n",
        "setup.cfg": "[options]\ninstall_requires =\n    voyageai\n",
        "Pipfile": '[packages]\ngroq = "*"\n',
        "environment.yml": "dependencies:\n  - python=3.11\n  - pip:\n    - mistralai==1.0\n",
    })  # fmt: skip
    hits = found(tmp_path, catalog)
    for service in ("pinecone", "anthropic", "cohere", "voyage", "groq", "mistral"):
        assert service in hits, service


def test_pyproject_all_dependency_tables(tmp_path, catalog):
    write(tmp_path, {"pyproject.toml": (
        '[project]\ndependencies = ["openai>=1"]\n'
        '[dependency-groups]\ndev = ["langsmith"]\n'
        '[tool.poetry.group.ml.dependencies]\ntavily-python = "*"\n'
    )})  # fmt: skip
    hits = found(tmp_path, catalog)
    assert {"openai", "langsmith", "tavily"} <= hits.keys()
    assert hits["openai"][0].line == 2


def test_python_imports_and_prefix(tmp_path, catalog):
    write(tmp_path, {"app.py": "from azure.search.documents.indexes import SearchIndexClient\n"})
    assert "azure-ai-search" in found(tmp_path, catalog)


def test_python2_file_still_yields_imports(tmp_path, catalog):
    write(tmp_path, {"old.py": "import pinecone\nprint 'hello'\n"})
    assert "pinecone" in found(tmp_path, catalog)


def test_relative_import_is_not_a_package(tmp_path, catalog):
    write(tmp_path, {"pkg/x.py": "from .openai import helper\n"})
    assert "openai" not in found(tmp_path, catalog)


def test_notebook_imports_install_magic_and_line_numbers(tmp_path, catalog):
    nb = {
        "cells": [
            {"cell_type": "markdown", "source": ["import pinecone  # prose, not code\n"]},
            {"cell_type": "code", "source": ["!pip install -q anthropic\n", "import cohere\n"]},
        ]
    }
    write(tmp_path, {"demo.ipynb": json.dumps(nb, indent=1)})
    hits = found(tmp_path, catalog)
    assert "anthropic" in hits and "cohere" in hits
    assert "pinecone" not in hits
    text = (tmp_path / "demo.ipynb").read_text().splitlines()
    assert "import cohere" in text[hits["cohere"][0].line - 1]


def test_pip_install_in_dockerfile(tmp_path, catalog):
    write(tmp_path, {"Dockerfile": "FROM python:3.12\nRUN pip install --no-cache-dir -r req.txt elevenlabs==1.0\n"})
    assert "elevenlabs" in found(tmp_path, catalog)


# --- JavaScript ecosystem -----------------------------------------------------


def test_js_package_names():
    assert js_package("@ai-sdk/openai/internal") == "@ai-sdk/openai"
    assert js_package("npm:openai@4") == "openai"
    assert js_package("openai/resources") == "openai"
    assert js_package("./openai") is None
    assert js_package("node:fs") is None


def test_package_json_cites_the_right_line(tmp_path, catalog):
    write(tmp_path, {"package.json": '{\n "dependencies": {\n  "@ai-sdk/openai": "1",\n  "openai": "4"\n }\n}\n'})
    cited = found(tmp_path, catalog)["openai"]
    assert {f.line for f in cited} == {3, 4}


def test_multiline_js_import(tmp_path, catalog):
    write(tmp_path, {"src/a.ts": 'import {\n  Pinecone,\n} from "@pinecone-database/pinecone";\n'})
    assert "pinecone" in found(tmp_path, catalog)


def test_block_comment_is_ignored(tmp_path, catalog):
    write(tmp_path, {"a.js": "/*\n const c = new Anthropic();\n ANTHROPIC_API_KEY\n*/\n"})
    assert "anthropic" not in found(tmp_path, catalog)


# --- other ecosystems ---------------------------------------------------------


def test_go_mod_skips_indirect(tmp_path, catalog):
    write(tmp_path, {"go.mod": (
        "module example.com/app\n\nrequire (\n"
        "\tgithub.com/openai/openai-go/v2 v2.1.0\n"
        "\tgithub.com/pinecone-io/go-pinecone v1.0.0 // indirect\n)\n"
    )})  # fmt: skip
    hits = found(tmp_path, catalog)
    assert "openai" in hits and "pinecone" not in hits


def test_jvm_dotnet_ruby_php_rust(tmp_path, catalog):
    write(tmp_path, {
        "pom.xml": "<dependency>\n<groupId>com.anthropic</groupId>\n<artifactId>anthropic-java</artifactId>\n</dependency>\n",
        "app/build.gradle.kts": 'dependencies { implementation("com.google.genai:google-genai:1.0") }\n',
        "Api.csproj": '<PackageReference Include="Azure.AI.OpenAI" Version="2.0" />\n',
        "Gemfile": "gem 'ruby-openai'\n",
        "composer.json": '{"require": {"openai-php/client": "^0.10"}}',
        "Cargo.toml": '[dependencies]\nbedrock = { package = "aws-sdk-bedrockruntime", version = "1" }\n',
    })  # fmt: skip
    hits = found(tmp_path, catalog)
    assert {"anthropic", "google-gemini", "azure-openai", "openai", "aws-bedrock"} <= hits.keys()


def test_terraform(tmp_path, catalog):
    write(tmp_path, {"infra/main.tf": 'resource "azurerm_search_service" "s" {}\n'})
    assert "azure-ai-search" in found(tmp_path, catalog)


# --- text needles -------------------------------------------------------------


def test_env_files_and_env_reads(tmp_path, catalog):
    write(tmp_path, {
        ".env.example": "# PINECONE_API_KEY= commented out\nTAVILY_API_KEY=\n",
        "main.go": 'key := os.Getenv("GROQ_API_KEY")\n',
    })  # fmt: skip
    hits = found(tmp_path, catalog)
    assert "tavily" in hits and "groq" in hits
    assert "pinecone" not in hits


def test_endpoints_in_any_language(tmp_path, catalog):
    write(tmp_path, {"Client.java": 'var url = "https://API.Anthropic.com/v1/messages";\n'})
    assert "anthropic" in found(tmp_path, catalog)


def test_model_ids_but_not_open_weights(tmp_path, catalog):
    write(tmp_path, {"models.ts": (
        'const a = "openai/gpt-oss-120b";\n'
        'const b = "deepseek/deepseek-v3.2";\n'
        'const c = "xai/grok-4-fast";\n'
    )})  # fmt: skip
    hits = found(tmp_path, catalog)
    assert "xai" in hits
    assert "openai" not in hits, "gpt-oss is open-weight"
    assert "deepseek-api" not in hits, "DeepSeek weights are open; the gateway is the dependency"


def test_model_prefix_is_bounded(tmp_path, catalog):
    write(tmp_path, {"a.py": 'x = "chatgpt-4-like"\nimport lib.bedrock\ny = "./bedrock/client"\n'})
    hits = found(tmp_path, catalog)
    assert "openai" not in hits and "aws-bedrock" not in hits


def test_symbol_boundaries(tmp_path, catalog):
    write(tmp_path, {"a.py": "client = AzureOpenAI(azure_endpoint=e)\n"})
    hits = found(tmp_path, catalog)
    assert "azure-openai" in hits and "openai" not in hits


def test_python_comments_and_docstrings_are_prose(tmp_path, catalog):
    write(tmp_path, {"a.py": '"""We migrated off Pinecone(api_key) last year."""\n# client = Anthropic()\n'})
    assert found(tmp_path, catalog) == {}


def test_generic_strings_that_are_not_vendors(tmp_path, catalog):
    write(tmp_path, {"a.py": (
        "import textract\n"
        'model.transcribe(audio, task="transcribe")\n'
        'token = os.environ["HF_TOKEN"]\n'
        'tools = [{"type": "file_search"}]\n'
    )})  # fmt: skip
    assert found(tmp_path, catalog) == {}


def test_line_numbers_survive_unicode_line_separators(tmp_path, catalog):
    write(tmp_path, {"a.js": 'const s = "a b";\nconst k = process.env.COHERE_API_KEY;\n'})
    assert found(tmp_path, catalog)["cohere"][0].line == 2


def test_crlf_files(tmp_path, catalog):
    write(tmp_path, {"a.py": "import os\r\nimport anthropic\r\n"})
    assert found(tmp_path, catalog)["anthropic"][0].line == 2


def test_utf8_bom_manifest(tmp_path, catalog):
    (tmp_path / "package.json").write_bytes(b'\xef\xbb\xbf{"dependencies": {"openai": "4"}}')
    assert "openai" in found(tmp_path, catalog)


def test_nul_byte_after_the_header_does_not_hide_a_file(tmp_path, catalog):
    body = "x = 1\n" * 2000 + "k = '\0'\nkey = os.environ['VOYAGE_API_KEY']\n"
    write(tmp_path, {"a.py": body})
    assert "voyage" in found(tmp_path, catalog)


def test_large_json_is_data_not_config(tmp_path, catalog):
    write(tmp_path, {"data/dump.json": json.dumps([{"text": "uses OpenAI( and Pinecone("}] * 20000)})
    assert found(tmp_path, catalog) == {}


# --- overlaps -----------------------------------------------------------------


def test_bare_openai_is_not_azure(tmp_path, catalog):
    write(tmp_path, {"requirements.txt": "openai\n"})
    assert set(found(tmp_path, catalog)) == {"openai"}


def test_azure_claims_the_shared_evidence(tmp_path, catalog):
    write(tmp_path, {
        "requirements.txt": "openai\n",
        "a.py": "from openai import AzureOpenAI\nc = AzureOpenAI()\n",
    })  # fmt: skip
    assert "azure-openai" in found(tmp_path, catalog)


# --- which files ----------------------------------------------------------------


def test_lockfiles_are_not_dependencies(tmp_path, catalog):
    write(tmp_path, {
        "uv.lock": '[[package]]\nname = "openai"\n',
        "package-lock.json": '{"packages": {"node_modules/openai": {}}}',
    })  # fmt: skip
    assert found(tmp_path, catalog) == {}


def test_vendored_dirs_and_virtualenvs_are_skipped(tmp_path, catalog):
    write(tmp_path, {
        "node_modules/x/index.js": 'require("openai")\n',
        "myenv/pyvenv.cfg": "home = /usr\n",
        "myenv/lib/a.py": "import anthropic\n",
    })  # fmt: skip
    assert found(tmp_path, catalog) == {}


def test_project_under_build_dir_is_scanned(tmp_path, catalog):
    root = tmp_path / "build" / "myapp"
    write(root, {"a.py": "import anthropic\n"})
    assert "anthropic" in found(root, catalog)


def test_lockinignore_and_exclude(tmp_path, catalog):
    write(tmp_path, {
        ".lockinignore": "# fixtures\nfixtures/\n",
        "fixtures/a.py": "import anthropic\n",
        "scripts/b.py": "import cohere\n",
    })  # fmt: skip
    assert found(tmp_path, catalog, exclude=["scripts/*.py"]) == {}


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_gitignored_files_are_skipped(tmp_path, catalog):
    write(tmp_path, {".gitignore": "generated/\n", "generated/a.py": "import anthropic\n", "a.py": "import cohere\n"})
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert set(found(tmp_path, catalog)) == {"cohere"}


def test_symlink_loop_does_not_hang(tmp_path, catalog):
    write(tmp_path, {"a.py": "import anthropic\n"})
    try:
        os.symlink(tmp_path, tmp_path / "loop", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert "anthropic" in found(tmp_path, catalog)


# --- reports ------------------------------------------------------------------


def test_reports(tmp_path, catalog):
    write(tmp_path, {"a.py": "import anthropic\n", "b.py": "import pinecone\n"})
    findings = match(collect_facts(tmp_path, catalog), catalog)
    md = to_markdown(findings, tmp_path, catalog)
    assert "Anthropic API" in md and "Open source alternatives" in md and "a.py:1" in md
    data = json.loads(to_json(findings, tmp_path, catalog))
    assert {f["id"] for f in data["found"]} == {"anthropic", "pinecone"}
    assert "vector-db" in data["alternatives"]


def test_empty_report(tmp_path, catalog):
    write(tmp_path, {"a.py": "print(1)\n"})
    assert "No closed AI services found" in to_markdown([], tmp_path, catalog)


def test_cli_scan(tmp_path, capsys):
    write(tmp_path, {"a.py": "import anthropic\n"})
    assert main(["scan", str(tmp_path)]) == 0
    assert "Anthropic API" in capsys.readouterr().out
    assert main(["scan", str(tmp_path / "missing")]) == 2


# --- ranking ------------------------------------------------------------------


def _refresh_module():
    spec = importlib.util.spec_from_file_location("refresh", ROOT / "scripts" / "refresh.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_momentum_needs_four_weeks_and_scales(search_mode):
    import datetime as dt

    refresh = _refresh_module()
    today = dt.date(2026, 9, 26)
    assert refresh.momentum([["2026-09-20", 100], ["2026-09-26", 150]], today) is None
    assert refresh.momentum([["2026-08-27", 100], ["2026-09-26", 400]], today) == 900


def test_newcomers_rank_after_projects_with_momentum(search_mode):
    refresh = _refresh_module()
    ranked, by = refresh.rank_github([
        {"repo": "a/big", "stars": 90000, "stars_90d": 100},
        {"repo": "b/hot", "stars": 5000, "stars_90d": 3000},
        {"repo": "c/new", "stars": 99999, "stars_90d": None},
    ])  # fmt: skip
    assert by == "momentum"
    assert [p["repo"] for p in ranked] == ["b/hot", "a/big", "c/new"]


def test_same_lab_fine_tunes_are_original(search_mode):
    refresh = _refresh_module()
    assert refresh._original("Qwen", {"base_model": "Qwen/Qwen3-8B-Base"})
    assert not refresh._original("Qwen", {"base_model": "meta-llama/Llama-3.1-8B"})
