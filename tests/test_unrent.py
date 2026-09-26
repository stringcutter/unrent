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

from unrent.catalog import OPEN_LICENCES, OPEN_MODEL_LICENCES, load_catalog  # noqa: E402
from unrent.cli import main  # noqa: E402
from unrent.detect import collect_facts, image_name, js_package, match, redact  # noqa: E402
from unrent.report import standings, to_json, to_markdown  # noqa: E402

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
    """Service id → cited facts, for everything reported (models only named included)."""
    return {f.service.id: f.cited for f in match(collect_facts(root, catalog, **kw), catalog)}


def deps(root: Path, catalog, **kw) -> set[str]:
    """Closed services reported as dependencies, not merely named by a model id."""
    return {
        f.service.id
        for f in match(collect_facts(root, catalog, **kw), catalog)
        if not f.models_only and not f.service.open_source
    }


@pytest.fixture(autouse=True, params=["python", "ripgrep"])
def search_mode(request, monkeypatch):
    """Every detection test runs with and without ripgrep: results must not differ."""
    if request.param == "ripgrep":
        if not shutil.which("rg"):
            pytest.skip("ripgrep not installed")
        monkeypatch.delenv("UNRENT_NO_RIPGREP", raising=False)
    else:
        monkeypatch.setenv("UNRENT_NO_RIPGREP", "1")
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
    write(
        tmp_path,
        {
            "Dockerfile": "FROM python:3.12\nRUN pip install --no-cache-dir -r req.txt elevenlabs==1.0\n"
        },
    )
    assert "elevenlabs" in found(tmp_path, catalog)


# --- JavaScript ecosystem -----------------------------------------------------


def test_js_package_names():
    assert js_package("@ai-sdk/openai/internal") == "@ai-sdk/openai"
    assert js_package("npm:openai@4") == "openai"
    assert js_package("openai/resources") == "openai"
    assert js_package("./openai") is None
    assert js_package("node:fs") is None


def test_package_json_cites_the_right_line(tmp_path, catalog):
    write(
        tmp_path,
        {
            "package.json": '{\n "dependencies": {\n  "@ai-sdk/openai": "1",\n  "openai": "4"\n }\n}\n'
        },
    )
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
    write(
        tmp_path,
        {"a.py": '"""We migrated off Pinecone(api_key) last year."""\n# client = Anthropic()\n'},
    )
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
    write(
        tmp_path, {"data/dump.json": json.dumps([{"text": "uses OpenAI( and Pinecone("}] * 20000)}
    )
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
    hits = found(tmp_path, catalog)
    assert "azure-openai" in hits
    assert "openai" not in hits, "the openai package is how Azure is called"


def test_claude_on_bedrock_is_bedrock(tmp_path, catalog):
    write(tmp_path, {
        "requirements.txt": "anthropic[bedrock]\n",
        "a.py": "from anthropic import AnthropicBedrock\nc = AnthropicBedrock()\n",
    })  # fmt: skip
    assert set(found(tmp_path, catalog)) == {"aws-bedrock"}


def test_gemini_through_vertex_is_vertex(tmp_path, catalog):
    write(tmp_path, {
        "requirements.txt": "google-genai\n",
        "a.py": "from google import genai\nc = genai.Client(vertexai=True, project='p')\n",
    })  # fmt: skip
    assert set(found(tmp_path, catalog)) == {"google-vertex"}


def test_both_vendors_really_used_are_both_reported(tmp_path, catalog):
    write(tmp_path, {
        "azure.py": "from openai import AzureOpenAI\nc = AzureOpenAI()\n",
        "direct.py": "from openai import OpenAI\nc = OpenAI()\n",
    })  # fmt: skip
    assert {"azure-openai", "openai"} <= found(tmp_path, catalog).keys()


def test_provider_via_openai_sdk_is_the_provider(tmp_path, catalog):
    write(tmp_path, {
        "requirements.txt": "openai\n",
        "a.py": 'from openai import OpenAI\nc = OpenAI(base_url="https://api.groq.com/openai/v1")\n',
    })  # fmt: skip
    assert set(found(tmp_path, catalog)) == {"groq"}


# --- review findings: one regression test each ------------------------------------


def test_trailing_python_comment_does_not_hide_the_code(tmp_path, catalog):
    write(tmp_path, {"app.py": (
        'requests.post("https://api.deepgram.com/v1/listen", data=a)  # speech to text\n'
        'key = os.environ["ELEVENLABS_API_KEY"]  # TTS\n'
    )})  # fmt: skip
    assert {"deepgram", "elevenlabs"} <= found(tmp_path, catalog).keys()


def test_comment_markers_are_per_language(tmp_path, catalog):
    write(tmp_path, {
        "a.ts": "class A { #key = process.env.DEEPGRAM_API_KEY }\n",
        "b.js": ';(async () => { await fetch("https://api.elevenlabs.io/v1/tts") })()\n',
        "run.sh": "docker run \\\n  --env PINECONE_API_KEY=$K img\n",
        "c.rs": "let x = 'a'; // lifetime-ish\nlet u = \"https://api.anthropic.com\";\n",
    })  # fmt: skip
    assert {"deepgram", "elevenlabs", "pinecone", "anthropic"} <= found(tmp_path, catalog).keys()


def test_comment_markers_inside_strings_are_text(tmp_path, catalog):
    write(
        tmp_path, {"a.js": 'app.use("/api/*", auth);\nfetch("https://api.cohere.com/v2/chat");\n'}
    )
    assert "cohere" in found(tmp_path, catalog)


def test_real_comments_still_hide_code(tmp_path, catalog):
    write(tmp_path, {
        "a.go": "/*\nkey := os.Getenv(\"GROQ_API_KEY\")\n*/\n// url := \"https://api.anthropic.com\"\n",
        "b.yaml": "# OPENAI_API_KEY: x\nname: app\n",
        "c.sql": "-- ANTHROPIC_API_KEY\nselect 1;\n",
        "d.xml": "<!-- <key>PINECONE_API_KEY</key> -->\n",
    })  # fmt: skip
    assert found(tmp_path, catalog) == {}


def test_from_package_import_module(tmp_path, catalog):
    write(tmp_path, {"app.py": "from google.cloud import documentai, aiplatform, vision\n"})
    hits = found(tmp_path, catalog)
    assert {"google-document-ai", "google-vertex", "google-vision-ocr"} <= hits.keys()


def test_dynamic_python_import(tmp_path, catalog):
    write(tmp_path, {"app.py": 'import importlib\nm = importlib.import_module("anthropic")\n'})
    assert "anthropic" in found(tmp_path, catalog)


def test_dotted_i_does_not_hang_or_shift_lines(tmp_path, catalog):
    write(tmp_path, {"app.py": 's = "' + "İ" * 60 + '"\n\nu = "https://api.deepgram.com/v1"\n'})
    assert found(tmp_path, catalog)["deepgram"][0].line == 3


def test_openai_sdk_against_a_local_server_is_not_openai(tmp_path, catalog):
    write(tmp_path, {
        "requirements.txt": "openai\n",
        "a.py": 'from openai import OpenAI\nc = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")\n',
        "b.js": 'import OpenAI from "openai";\nconst c = new OpenAI({ baseURL: "http://192.168.1.5:8000/v1" });\n',
    })  # fmt: skip
    assert "openai" not in found(tmp_path, catalog)


def test_local_base_url_in_env_covers_the_project(tmp_path, catalog):
    write(tmp_path, {
        ".env.example": "OPENAI_BASE_URL=http://host.docker.internal:11434/v1\n",
        "a.py": "from openai import OpenAI\nc = OpenAI()\n",
    })  # fmt: skip
    assert "openai" not in found(tmp_path, catalog)


def test_real_openai_next_to_a_local_server_is_still_openai(tmp_path, catalog):
    write(tmp_path, {
        "local.py": 'from openai import OpenAI\nc = OpenAI(base_url="http://localhost:8000/v1")\n',
        "cloud.py": 'from openai import OpenAI\nc = OpenAI()\nm = "gpt-4o"\n',
    })  # fmt: skip
    assert "openai" in found(tmp_path, catalog)


def test_big_notebooks_long_lines_and_big_yaml_are_read(tmp_path, catalog):
    image = "A" * 3_000_000
    nb = {"cells": [
        {"cell_type": "code", "source": ["from pinecone import Pinecone\n"],
         "outputs": [{"output_type": "display_data", "data": {"image/png": image}}]},
    ]}  # fmt: skip
    write(tmp_path, {
        "big.ipynb": json.dumps(nb),
        "models.ts": "export const m = [" + '{id:"local",label:"L"},' * 200 + '{id:"claude-sonnet-4-5"}];\n',
        "litellm/config.yaml": "model_list:\n" + "  - model_name: m\n" * 20000 + "  - model: anthropic/claude-sonnet-4-5\n",
    })  # fmt: skip
    hits = found(tmp_path, catalog)
    assert "pinecone" in hits and "anthropic" in hits
    assert len(hits["anthropic"][0].evidence) < 300


def test_oversized_files_are_reported_not_silently_dropped(tmp_path, catalog):
    write(tmp_path, {"huge.py": "import anthropic\n" + "x = 1\n" * 400_000})
    skipped: list = []
    collect_facts(tmp_path, catalog, skipped=skipped)
    assert [p.name for p in skipped] == ["huge.py"]
    md = to_markdown([], tmp_path, catalog, skipped=skipped)
    assert "Not scanned" in md and "huge.py" in md


@pytest.mark.parametrize(
    "files",
    [
        {"ui.js": "function close() { return modal.run(); }\n"},
        {"requirements.txt": "FireWorks>=2\n", "wf.py": "from fireworks import Firework\n"},
        {"perplexity.py": "def score(): ...\n", "app.py": "from perplexity import score\n"},
        {"mem.py": "class MemoryClient:\n    pass\n"},
        {"Speech.kt": "class SpeechClient\n"},
        {"emb.py": "class TextEmbeddingModel: ...\n"},
        {"rt.js": "supabase.realtime.connect()\n"},
        {"compose.yaml": "services:\n  llm:\n    command: ollama run command-r:35b\n"},
        {"data.py": 'open("bedrock/geology.csv")\nopen("perplexity/scores.json")\n'},
        {"tok.py": 'enc = tiktoken.encoding_for_model("gpt-4")\n'},
        {".env.example": "WANDB_API_KEY=\n"},
        {"Dockerfile": "ENV NGC_API_KEY=x\n"},
        {"db.js": 'db.c.aggregate([{ $vectorSearch: { index: "v" } }])\n'},
        {"err.py": 'raise ImportError("run: pip install cohere")\n'},
    ],
    ids=lambda files: next(iter(files)),
)
def test_generic_signatures_do_not_name_a_vendor(tmp_path, catalog, files):
    write(tmp_path, files)
    assert found(tmp_path, catalog) == {}


def test_weak_signatures_corroborate_each_other(tmp_path, catalog):
    write(tmp_path, {"db.py": (
        'client = MongoClient("mongodb+srv://u:p@c0.abcde.mongodb.net")\n'
        'pipeline = [{"$vectorSearch": {"index": "v"}}]\n'
    )})  # fmt: skip
    assert "mongodb-atlas-vector-search" in found(tmp_path, catalog)


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_directory_ignored_by_its_parent_repo_is_scanned(tmp_path, catalog):
    write(
        tmp_path, {".gitignore": "downloads/\n", "downloads/template/app.py": "import anthropic\n"}
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert "anthropic" in found(tmp_path / "downloads" / "template", catalog)


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_submodules_are_scanned(tmp_path, catalog):
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "protocol.file.allow=always"]
    lib = write(tmp_path / "lib", {"llm.py": "import anthropic\n"})
    subprocess.run([*git, "init", "-q"], cwd=lib, check=True)
    subprocess.run([*git, "add", "."], cwd=lib, check=True)
    subprocess.run([*git, "commit", "-qm", "x"], cwd=lib, check=True)
    main_repo = write(tmp_path / "main", {"app.py": "print(1)\n"})
    subprocess.run([*git, "init", "-q"], cwd=main_repo, check=True)
    subprocess.run(
        [*git, "submodule", "-q", "add", str(lib), "vendorlib"], cwd=main_repo, check=True
    )
    assert "anthropic" in found(main_repo, catalog)


def test_secrets_never_reach_the_report(tmp_path, catalog):
    write(tmp_path, {
        ".env": "OPENAI_API_KEY=sk-proj-AbCdEfGhIjKlMnOpQrStUvWx\n",
        "a.py": 'import anthropic\nc = anthropic.Anthropic(api_key="sk-ant-api03-REALKEYxyz0123456789")\n',
    })  # fmt: skip
    findings = match(collect_facts(tmp_path, catalog), catalog)
    out = to_markdown(findings, tmp_path, catalog) + to_json(findings, tmp_path, catalog)
    assert "AbCdEfGh" not in out and "REALKEY" not in out
    assert "OPENAI_API_KEY=****" in out


def test_references_to_secrets_are_not_masked():
    assert redact('api_key=os.environ["OPENAI_API_KEY"]') == 'api_key=os.environ["OPENAI_API_KEY"]'
    assert (
        redact("apiKey: process.env.ANTHROPIC_API_KEY") == "apiKey: process.env.ANTHROPIC_API_KEY"
    )


def test_multiline_install_commands(tmp_path, catalog):
    write(tmp_path, {"Dockerfile": (
        "FROM python:3.12\nRUN pip install --no-cache-dir \\\n"
        "    anthropic==0.40.0 \\\n    pinecone \\\n    voyageai\n"
    )})  # fmt: skip
    hits = found(tmp_path, catalog)
    assert {"anthropic", "pinecone", "voyage"} <= hits.keys()
    assert hits["voyage"][0].line == 2


def test_requirements_with_hashes(tmp_path, catalog):
    write(tmp_path, {"requirements.txt": "anthropic==0.40 \\\n    --hash=sha256:abc\n"})
    assert "anthropic" in found(tmp_path, catalog)


def test_findings_only_in_tests_are_marked_and_skippable(tmp_path, catalog):
    write(tmp_path, {
        "app.py": "import anthropic\n",
        "tests/test_llm.py": "import cohere\n",
        "src/models.spec.ts": 'const m = "gpt-4o";\n',
    })  # fmt: skip
    findings = {f.service.id: f for f in match(collect_facts(tmp_path, catalog), catalog)}
    assert not findings["anthropic"].test_only
    assert findings["cohere"].test_only and findings["openai"].test_only
    assert "only in tests" in to_markdown(list(findings.values()), tmp_path, catalog)
    assert set(found(tmp_path, catalog, skip_tests=True)) == {"anthropic"}


def test_ignore_files_use_gitignore_semantics(tmp_path, catalog):
    write(tmp_path, {
        # As in git: `/docs/*` then `!docs/keep.py` re-includes; `/docs` would not.
        ".unrentignore": "tests\n**/fixtures/\n/docs/*\n!docs/keep.py\n",
        "tests/t.py": "import anthropic\n",
        "src/fixtures/f.py": "import cohere\n",
        "docs/d.py": "import mistralai\n",
        "docs/keep.py": "import groq\n",
        "src/docs/real.py": "import voyageai\n",
    })  # fmt: skip
    assert set(found(tmp_path, catalog)) == {"groq", "voyage"}


def test_gitignore_is_respected_without_git(tmp_path, catalog):
    write(tmp_path, {
        ".gitignore": "generated/\n",
        "generated/a.py": "import anthropic\n",
        "build/Dockerfile": "RUN pip install cohere\n",
    })  # fmt: skip
    assert set(found(tmp_path, catalog)) == {"cohere"}


@pytest.mark.parametrize(
    ("files", "service"),
    [
        ({"dev-requirements.txt": "anthropic\n"}, "anthropic"),
        ({"Containerfile": "RUN pip install anthropic\n"}, "anthropic"),
        (
            {
                "index.html": '<p>api.openai.com</p><script>\nfetch("https://api.mistral.ai/v1")\n</script>'
            },
            "mistral",
        ),
        ({"main.cpp": 'auto u = "https://api.anthropic.com/v1/messages";\n'}, "anthropic"),
        ({"chart/templates/_helpers.tpl": "- name: OPENAI_API_KEY\n"}, "openai"),
        ({"main.ts": 'import Anthropic from "jsr:@anthropic-ai/sdk";\n'}, "anthropic"),
        ({"pubspec.yaml": "dependencies:\n  dart_openai: ^5.0.0\n"}, "openai"),
        ({"pyproject.toml": '[tool.hatch.envs.default]\ndependencies = ["cohere"]\n'}, "cohere"),
        (
            {
                "Package.swift": '.package(url: "https://github.com/MacPaw/OpenAI.git", from: "0.2.0")\n'
            },
            "openai",
        ),
        ({"pnpm-workspace.yaml": 'catalog:\n  "@anthropic-ai/sdk": ^0.30.0\n'}, "anthropic"),
    ],
    ids=lambda v: next(iter(v)) if isinstance(v, dict) else v,
)
def test_more_file_types(tmp_path, catalog, files, service):
    write(tmp_path, files)
    assert service in found(tmp_path, catalog)


def test_html_prose_is_not_code(tmp_path, catalog):
    write(tmp_path, {"docs.html": "<p>Call https://api.openai.com with OPENAI_API_KEY</p>\n"})
    assert found(tmp_path, catalog) == {}


def test_utf16_files(tmp_path, catalog):
    (tmp_path / "setup.ps1").write_bytes('$env:ANTHROPIC_API_KEY = "x"\r\n'.encode("utf-16"))
    assert "anthropic" in found(tmp_path, catalog)


def test_citations_point_at_the_dependency(tmp_path, catalog):
    write(tmp_path, {
        "pyproject.toml": (
            '[project]\nname = "r"\ndescription = "Routes prompts to anthropic"\n'
            'keywords = ["openai"]\ndependencies = ["anthropic>=0.40"]\n'
        ),
        "package.json": '{\n "keywords": ["openai"],\n "dependencies": {\n  "openai": "^4"\n }\n}\n',
    })  # fmt: skip
    hits = found(tmp_path, catalog)
    assert hits["anthropic"][0].line == 5
    assert hits["openai"][0].line == 4


def test_notebook_citation_skips_markdown_with_the_same_text(tmp_path, catalog):
    nb = {"cells": [
        {"cell_type": "markdown", "source": ["    client = OpenAI()\n"]},
        {"cell_type": "code", "outputs": [], "source": ["client = OpenAI()"]},
    ]}  # fmt: skip
    write(tmp_path, {"n.ipynb": json.dumps(nb, indent=1)})
    line = found(tmp_path, catalog)["openai"][0].line
    text = (tmp_path / "n.ipynb").read_text().split("\n")
    assert text[line - 1].strip() == '"client = OpenAI()"'
    assert line > 6


def test_refresh_refuses_to_publish_a_collapse(search_mode):
    refresh = _refresh_module()
    before = {"vector-db": {"ranked": [{}] * 8}, "rag": {"ranked": [{}] * 5}}
    assert refresh.shrunk_pools(before, {"vector-db": {"ranked": []}, "rag": {"ranked": [{}] * 5}})
    assert refresh.shrunk_pools(
        before, {"vector-db": {"ranked": [{}] * 3}, "rag": {"ranked": [{}] * 5}}
    )
    assert not refresh.shrunk_pools(
        before, {"vector-db": {"ranked": [{}] * 7}, "rag": {"ranked": [{}] * 5}}
    )


def test_refresh_treats_rate_limits_as_transient(search_mode, monkeypatch):
    import io
    import urllib.error

    refresh = _refresh_module()
    monkeypatch.setattr(refresh, "MAX_RATE_LIMIT_WAIT", 0)

    def rate_limited(*a, **k):
        headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "9999999999"}
        raise urllib.error.HTTPError("u", 403, "rate limited", headers, io.BytesIO())

    monkeypatch.setattr(refresh.urllib.request, "urlopen", rate_limited)
    result = refresh.github_project("a/b", None, "token")
    assert result["status"] == "unreachable"


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


def test_unrentignore_and_exclude(tmp_path, catalog):
    write(tmp_path, {
        ".unrentignore": "# fixtures\nfixtures/\n",
        "fixtures/a.py": "import anthropic\n",
        "scripts/b.py": "import cohere\n",
    })  # fmt: skip
    assert found(tmp_path, catalog, exclude=["scripts/*.py"]) == {}


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_gitignored_files_are_skipped(tmp_path, catalog):
    write(
        tmp_path,
        {
            ".gitignore": "generated/\n",
            "generated/a.py": "import anthropic\n",
            "a.py": "import cohere\n",
        },
    )
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


# --- golden-corpus findings: one regression test each ---------------------------


def test_model_names_alone_are_named_not_dependencies(tmp_path, catalog):
    write(
        tmp_path,
        {"limits.py": 'LIMITS = {"mistral:mistral-large": 32768, "claude-3-5-sonnet": 200000}\n'},
    )
    assert deps(tmp_path, catalog) == set()
    assert {"mistral", "anthropic"} <= found(tmp_path, catalog).keys()
    findings = match(collect_facts(tmp_path, catalog), catalog)
    md = to_markdown(findings, tmp_path, catalog)
    assert "Closed models named in code" in md and "No closed AI services found" in md
    data = json.loads(to_json(findings, tmp_path, catalog))
    assert data["found"] == [] and {f["id"] for f in data["models_named"]} == {
        "mistral",
        "anthropic",
    }


def test_capability_model_ids_count_when_the_vendor_is_used(tmp_path, catalog):
    write(
        tmp_path,
        {"a.py": 'from openai import OpenAI\nc = OpenAI()\nm = "text-embedding-3-small"\n'},
    )
    assert {"openai", "openai-embeddings"} <= deps(tmp_path, catalog)


def test_capability_model_ids_in_tests_are_fixtures(tmp_path, catalog):
    write(tmp_path, {
        "client.go": 'import "github.com/openai/openai-go"\n',
        "go.mod": "module x\nrequire github.com/openai/openai-go v1.0.0\n",
        "llm_test.go": 'realtimeModel := "gpt-realtime"\n',
    })  # fmt: skip
    hits = deps(tmp_path, catalog)
    assert "openai" in hits and "openai-realtime" not in hits


def test_tokenizer_tables_are_not_calls(tmp_path, catalog):
    write(
        tmp_path,
        {"tokenizer.go": 'var m = map[string]int{"text-embedding-ada-002": encodingCL100KBase}\n'},
    )
    assert found(tmp_path, catalog) == {}


def test_a_vendors_openapi_spec_is_not_usage(tmp_path, catalog):
    write(tmp_path, {"openai_spec.yaml": (
        "openapi: 3.0.0\ninfo:\n  title: OpenAI API\nservers:\n  - url: https://api.openai.com/v1\n"
        "x-key: $OPENAI_API_KEY\nexample: dall-e-2\n"
    )})  # fmt: skip
    assert found(tmp_path, catalog) == {}


def test_ai_sdk_model_strings_without_a_provider_go_to_the_gateway(tmp_path, catalog):
    write(tmp_path, {
        "package.json": '{"dependencies": {"ai": "^6.0.0"}}',
        "app/actions.ts": 'import { generateText } from "ai";\nawait generateText({ model: "openai/gpt-5-mini" });\n',
    })  # fmt: skip
    assert deps(tmp_path, catalog) == {"vercel-ai-gateway"}


def test_ai_sdk_with_a_provider_package_calls_the_provider(tmp_path, catalog):
    write(tmp_path, {
        "package.json": '{"dependencies": {"ai": "^6.0.0", "@ai-sdk/openai": "^2"}}',
        "app/actions.ts": 'import { openai } from "@ai-sdk/openai";\nconst m = openai("gpt-5-mini");\n',
    })  # fmt: skip
    assert "vercel-ai-gateway" not in deps(tmp_path, catalog)
    assert "openai" in deps(tmp_path, catalog)


def test_claude_on_vertex_is_vertex(tmp_path, catalog):
    write(tmp_path, {
        "requirements.txt": "anthropic[vertex]\n",
        "loop.py": "from anthropic import AnthropicVertex\nclient = AnthropicVertex()\n",
    })  # fmt: skip
    assert deps(tmp_path, catalog) == {"google-vertex"}


def test_android_speech_recognizer_is_not_azure(tmp_path, catalog):
    write(
        tmp_path,
        {
            "Voice.kt": "import android.speech.SpeechRecognizer\nval r = SpeechRecognizer.createSpeechRecognizer(ctx)\n"
        },
    )
    assert found(tmp_path, catalog) == {}


@pytest.mark.parametrize(
    ("files", "service"),
    [
        (
            {
                "utils.ts": 'import { BedrockAgentRuntimeClient, RetrieveCommand } from "@aws-sdk/client-bedrock-agent-runtime";\nnew RetrieveCommand({ knowledgeBaseId: id });\n'
            },
            "aws-bedrock-knowledge-bases",
        ),
        ({"Program.cs": "OpenAITextToImageService svc = new(key, null);\n"}, "openai-images"),
        (
            {
                "route.ts": 'import { GoogleGenAI } from "@google/genai";\nconst MODEL_ID = "gemini-2.0-flash-exp-image-generation";\n'
            },
            "google-imagen",
        ),
        (
            {"config.py": "client = QdrantClient(url=u, api_key=k, cloud_inference=True)\n"},
            "qdrant-cloud",
        ),
        ({"tools.py": "from llama_hub.tools.metaphor.base import MetaphorToolSpec\n"}, "exa"),
        (
            {
                "pom.xml": "<dependency><groupId>org.springframework.ai</groupId><artifactId>spring-ai-starter-model-mistral-ai</artifactId></dependency>\n"
            },
            "mistral",
        ),
    ],
    ids=lambda v: v if isinstance(v, str) else next(iter(v)),
)
def test_signatures_found_missing_by_the_corpus(tmp_path, catalog, files, service):
    write(tmp_path, files)
    assert service in deps(tmp_path, catalog)


# --- catalog audit: false positives it found, and what they must report instead ----


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (
            {
                "db.py": 'from pymongo import MongoClient\nclient = MongoClient(os.environ["ATLAS_URI"])\n',
                ".env": "MONGODB_ATLAS_URI=mongodb+srv://u:p@cluster0.abcd.mongodb.net/db\n",
            },
            set(),
        ),
        (
            {
                "deploy.sh": 'curl -X PUT "https://api.cloudflare.com/client/v4/accounts/$A/workers/scripts/app"\n'
            },
            set(),
        ),
        (
            {
                "app.py": "from azure.ai.documentintelligence import DocumentIntelligenceClient\n"
                'endpoint = "https://myres.cognitiveservices.azure.com/"\n'
            },
            {"azure-document-intelligence"},
        ),
        (
            {
                "server.ts": 'import { createGateway } from "./apollo-gateway";\nconst gw = createGateway({ services });\n'
            },
            set(),
        ),
        ({"g.ts": 'export function build(g: Graph) { return g.createVertex("a"); }\n'}, set()),
        (
            {
                "stt.py": "from groq import Groq\nclient = Groq()\n"
                't = client.audio.transcriptions.create(file=f, model="whisper-large-v3")\n'
                's = client.audio.speech.create(model="playai-tts", input="hi")\n'
            },
            {"groq"},
        ),
        (
            {
                "agent.py": "from livekit.agents import llm\nclass A:\n    session: llm.RealtimeSession\n"
            },
            set(),
        ),
        ({"build.sh": "sonar-scanner -Dproject.settings=sonar-project.properties\n"}, set()),
    ],
    ids=[
        "atlas-without-vector-search",
        "cloudflare-deploy",
        "document-intelligence",
        "apollo-gateway",
        "graph-code",
        "groq-audio",
        "livekit-realtime",
        "sonarqube",
    ],
)
def test_audit_false_positives(tmp_path, catalog, files, expected):
    write(tmp_path, files)
    assert deps(tmp_path, catalog) == expected


def test_atlas_vector_search_still_needs_both_kinds_of_evidence(tmp_path, catalog):
    write(tmp_path, {
        ".env": "MONGODB_ATLAS_URI=mongodb+srv://u:p@cluster0.abcd.mongodb.net/db\n",
        "search.py": 'pipeline = [{"$vectorSearch": {"index": "v", "path": "e"}}]\n',
    })  # fmt: skip
    assert "mongodb-atlas-vector-search" in deps(tmp_path, catalog)


def test_regional_bedrock_model_ids(tmp_path, catalog):
    write(tmp_path, {"app.py": (
        'import boto3\nc = boto3.client("bedrock-runtime")\n'
        'MODEL = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"\n'
    )})  # fmt: skip
    assert "aws-bedrock" in deps(tmp_path, catalog)


# --- open source you already run --------------------------------------------------


def running(root: Path, catalog, **kw) -> dict:
    """Open source components found, by repo."""
    return {
        f.service.repo: f
        for f in match(collect_facts(root, catalog, **kw), catalog)
        if f.service.open_source
    }


def test_open_source_components_are_recognised_not_reported_as_closed(tmp_path, catalog):
    write(tmp_path, {"requirements.txt": "faiss-cpu==1.8\n", "app.py": "import faiss\n"})
    assert set(running(tmp_path, catalog)) == {"facebookresearch/faiss"}
    assert deps(tmp_path, catalog) == set()
    data = json.loads(to_json(match(collect_facts(tmp_path, catalog), catalog), tmp_path, catalog))
    assert data["found"] == []
    assert [f["repo"] for f in data["open_source"]] == ["facebookresearch/faiss"]


def test_standing_in_the_pool_and_among_the_same_kind(tmp_path, catalog):
    write(tmp_path, {"docker-compose.yml": "services:\n  db:\n    image: qdrant/qdrant:v1.12.0\n"})
    finding = running(tmp_path, catalog)["qdrant/qdrant"]
    (s,) = standings(finding, catalog)
    pool = catalog.pools["vector-db"]
    assert s.pool.id == "vector-db" and s.of == len(pool.alternatives)
    assert pool.alternatives[s.rank - 1].name == "qdrant/qdrant"
    # Peers share at least one kind: chroma is "embedded and server", so it counts.
    servers = [a.name for a in pool.alternatives if "server" in a.kind]
    assert s.kind_rank == servers.index("qdrant/qdrant") + 1 and s.kind_of == len(servers)
    # Everything ranked above it, in rank order.
    assert list(s.ahead) == list(pool.alternatives[: s.rank - 1])
    md = to_markdown(match(collect_facts(tmp_path, catalog), catalog), tmp_path, catalog)
    assert "Open source you already run" in md and f"#{s.rank} of {s.of}" in md


def test_ahead_list_keeps_rank_order_and_counts_the_rest(tmp_path, catalog):
    write(tmp_path, {"requirements.txt": "tantivy\n"})
    finding = running(tmp_path, catalog)["quickwit-oss/tantivy"]
    (s,) = standings(finding, catalog)
    md = to_markdown(match(collect_facts(tmp_path, catalog), catalog), tmp_path, catalog)
    if s.rank and s.rank - 1 > 3:
        assert f"…and {s.rank - 1 - 3} more" in md
    data = json.loads(to_json(match(collect_facts(tmp_path, catalog), catalog), tmp_path, catalog))
    assert len(data["open_source"][0]["standing"][0]["ahead"]) == (s.rank - 1 if s.rank else s.of)


def test_every_kind_is_from_the_vocabulary(catalog):
    from unrent.catalog import KINDS

    for project in catalog.projects:
        assert set(project.kind) <= KINDS, project.repo


def test_models_show_their_size(tmp_path, catalog):
    write(tmp_path, {"a.py": "from openai import OpenAI\nc = OpenAI()\n"})
    md = to_markdown(match(collect_facts(tmp_path, catalog), catalog), tmp_path, catalog)
    if any(a.params for a in catalog.pools["open-llm"].alternatives[:3]):
        assert " params" in md


def test_a_component_that_dropped_out_of_the_ranking_is_flagged(tmp_path, catalog, monkeypatch):
    import dataclasses

    pool = catalog.pools["vector-db"]
    without = tuple(a for a in pool.alternatives if a.name != "facebookresearch/faiss")
    monkeypatch.setitem(catalog.pools, "vector-db", dataclasses.replace(pool, alternatives=without))
    write(tmp_path, {"app.py": "import faiss\n"})
    (s,) = standings(running(tmp_path, catalog)["facebookresearch/faiss"], catalog)
    assert s.rank is None
    md = to_markdown(match(collect_facts(tmp_path, catalog), catalog), tmp_path, catalog)
    assert "dropped from the ranking" in md


@pytest.mark.parametrize(
    ("ref", "name"),
    [
        ("qdrant/qdrant:v1.12.0", "qdrant/qdrant"),
        ("docker.io/library/postgres:16", "postgres"),
        ("docker.io/ollama/ollama@sha256:abc", "ollama/ollama"),
        ("localhost:5000/team/app:1", "localhost:5000/team/app"),
        ("ghcr.io/ggml-org/llama.cpp:server", "ghcr.io/ggml-org/llama.cpp"),
        ("${REGISTRY}/qdrant/qdrant", None),
        ("{{ .Values.image.repository }}", None),
    ],
)
def test_image_names(ref, name):
    assert image_name(ref) == name


@pytest.mark.parametrize(
    ("files", "repo"),
    [
        (
            {"Dockerfile": "FROM --platform=linux/amd64 vllm/vllm-openai:v0.9.0\n"},
            "vllm-project/vllm",
        ),
        (
            {
                "k8s/deploy.yaml": "spec:\n  containers:\n    - name: m\n      image: getmeili/meilisearch:v1.12\n"
            },
            "meilisearch/meilisearch",
        ),
        (
            {
                "chart/values.yaml": "image:\n  repository: ghcr.io/huggingface/text-embeddings-inference\n  tag: 1.5\n"
            },
            "huggingface/text-embeddings-inference",
        ),
        (
            {"migrations/001.sql": "-- enable vectors\nCREATE EXTENSION IF NOT EXISTS vector;\n"},
            "pgvector/pgvector",
        ),
        ({".env.example": "OLLAMA_BASE_URL=http://localhost:11434\n"}, "ollama/ollama"),
        ({"package.json": '{"dependencies": {"@lancedb/lancedb": "0.15.0"}}'}, "lancedb/lancedb"),
        ({"go.mod": "module x\nrequire github.com/qdrant/go-client v1.12.0\n"}, "qdrant/qdrant"),
    ],
    ids=lambda v: v if isinstance(v, str) else next(iter(v)),
)
def test_open_source_evidence(tmp_path, catalog, files, repo):
    write(tmp_path, files)
    assert repo in running(tmp_path, catalog)


def test_commented_images_and_templated_images_are_ignored(tmp_path, catalog):
    write(tmp_path, {
        "docker-compose.yml": "services:\n  db:\n    # image: qdrant/qdrant\n    image: ${DB_IMAGE}\n",
        "chart/templates/deploy.yaml": 'image: "{{ .Values.image.repository }}"\n',
    })  # fmt: skip
    assert running(tmp_path, catalog) == {}


def test_import_whisper_alone_is_not_openai_whisper(tmp_path, catalog):
    write(tmp_path, {"metrics.py": "import whisper\nwhisper.create('x.wsp', [(60, 1440)])\n"})
    assert "openai/whisper" not in running(tmp_path, catalog)
    write(tmp_path, {"requirements.txt": "openai-whisper\n"})
    assert "openai/whisper" in running(tmp_path, catalog)


def test_every_project_in_the_catalog_belongs_to_a_pool_and_has_a_kind(catalog):
    assert len(catalog.projects) > 50
    for project in catalog.projects:
        assert project.kind and project.replace_with, project.repo


# --- acceptance test findings: robustness ---------------------------------------------


def test_colab_notebook_with_one_string_per_cell(tmp_path, catalog):
    nb = {
        "cells": [
            {
                "cell_type": "code",
                "source": ["import os\nfrom openai import OpenAI\nclient = OpenAI()"],
            }
        ],
        "metadata": {},
    }
    write(tmp_path, {"x.ipynb": json.dumps(nb)})
    assert "openai" in deps(tmp_path, catalog)


def test_kotlin_notebook_comments_are_comments(tmp_path, catalog):
    nb = {
        "metadata": {"kernelspec": {"language": "kotlin", "name": "kotlin"}},
        "cells": [
            {
                "cell_type": "code",
                "source": [
                    '//   val executor = simpleAnthropicExecutor(System.getenv("ANTHROPIC_API_KEY"))\n'
                ],
            }
        ],
    }
    write(tmp_path, {"k.ipynb": json.dumps(nb, indent=1)})
    assert found(tmp_path, catalog) == {}


def test_commented_notebook_install_installs_nothing(tmp_path, catalog):
    nb = {
        "cells": [
            {"cell_type": "code", "source": ["# !pip install cohere\n", "!pip install anthropic\n"]}
        ]
    }
    write(tmp_path, {"n.ipynb": json.dumps(nb, indent=1)})
    hits = deps(tmp_path, catalog)
    assert "anthropic" in hits and "cohere" not in hits


def test_saved_reports_are_not_evidence(tmp_path, catalog):
    write(tmp_path, {"main.py": "import anthropic\n"})
    report = to_json(match(collect_facts(tmp_path, catalog), catalog), tmp_path, catalog)
    write(tmp_path, {"r.json": report, "main.py": "print(1)\n"})
    assert found(tmp_path, catalog) == {}


def test_report_written_inside_the_scanned_tree_is_excluded(tmp_path, capsys):
    write(tmp_path, {"main.py": "import anthropic\n"})
    assert main(["scan", str(tmp_path), "--format", "json", "-o", str(tmp_path / "r.md")]) == 0
    assert main(["scan", str(tmp_path), "--format", "json", "-o", str(tmp_path / "r2.json")]) == 0
    data = json.loads((tmp_path / "r2.json").read_text(encoding="utf-8"))
    assert all(e["file"] == "main.py" for f in data["found"] for e in f["evidence"])


def test_output_problems_fail_before_the_scan(tmp_path, capsys):
    write(tmp_path, {"main.py": "import anthropic\n"})
    assert main(["scan", str(tmp_path), "-o", str(tmp_path / "missing" / "r.md")]) == 2
    assert main(["scan", str(tmp_path), "-o", str(tmp_path)]) == 2
    assert "cannot write the report" in capsys.readouterr().err


def test_top_must_be_positive(capsys):
    with pytest.raises(SystemExit):
        main(["scan", ".", "--top", "0"])


def test_no_command_prints_help(capsys):
    assert main([]) == 2
    assert "scan" in capsys.readouterr().err


def test_classic_mac_line_endings(tmp_path, catalog):
    (tmp_path / "cr.py").write_bytes(b"import os\rimport anthropic\r")
    cited = found(tmp_path, catalog)["anthropic"]
    assert cited[0].line == 2 and cited[0].evidence == "import anthropic"


def test_symlinked_files_are_skipped_in_git_mode_too(tmp_path, catalog):
    write(
        tmp_path,
        {"outside/conf.yaml": "url: https://api.groq.com/openai/v1\n", "repo/app.py": "print(1)\n"},
    )
    try:
        (tmp_path / "repo" / "conf.yaml").symlink_to(tmp_path / "outside" / "conf.yaml")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    if shutil.which("git"):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path / "repo", check=True)
    assert found(tmp_path / "repo", catalog) == {}


def test_minified_module_bundles_are_generated(tmp_path, catalog):
    write(tmp_path, {"web/pdf.worker.min.mjs": 'fetch("https://api.openai.com/v1")\n'})
    assert found(tmp_path, catalog) == {}


def test_python_syntax_warnings_stay_quiet(tmp_path, catalog, capsys):
    write(tmp_path, {"a.py": 'import anthropic\npath = "C:\\Your\\dir"\n'})
    collect_facts(tmp_path, catalog)
    assert "SyntaxWarning" not in capsys.readouterr().err


def test_ripgrep_reads_only_the_scanned_files(tmp_path, catalog, search_mode):
    write(tmp_path, {".gitignore": "cache/\n", "src/a.py": "import anthropic\n"})
    for i in range(50):
        write(tmp_path, {f"cache/{i}.txt": "OPENAI_API_KEY api.openai.com\n" * 2000})
    if shutil.which("git"):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert set(deps(tmp_path, catalog)) == {"anthropic"}


def test_bad_rankings_snapshot_is_a_clear_error(tmp_path, capsys):
    import shutil as sh

    cat = tmp_path / "cat"
    sh.copytree(CATALOG_DIR, cat)
    (cat / "rankings.json").write_text("{not json", encoding="utf-8")
    assert main(["catalog", "--validate", "--catalog", str(cat)]) == 2
    assert "rankings.json" in capsys.readouterr().err


# --- acceptance test findings: precision -----------------------------------------------


def test_specs_directories_are_production_code(tmp_path, catalog):
    write(tmp_path, {"src/specs/jina/jina_reader.ts": "const URL = 'https://r.jina.ai';\n"})
    findings = {f.service.id: f for f in match(collect_facts(tmp_path, catalog), catalog)}
    assert not findings["jina-api"].test_only


def test_ruby_specs_and_pytest_config_are_tests(tmp_path, catalog):
    write(tmp_path, {
        "spec/models/llm_spec.rb": 'ENV["COHERE_API_KEY"]\n',
        "api/pytest.ini": "[pytest]\nenv =\n    VOYAGE_API_KEY=x\n",
    })  # fmt: skip
    findings = match(collect_facts(tmp_path, catalog), catalog)
    assert findings and all(f.test_only for f in findings)
    assert found(tmp_path, catalog, skip_tests=True) == {}


def test_json_comments_are_comments(tmp_path, catalog):
    write(tmp_path, {"appsettings.json": (
        "{\n  // By default the system uses 'https://api.openai.com/v1'.\n"
        '  "Endpoint": "http://localhost:8080"\n}\n'
    )})  # fmt: skip
    assert found(tmp_path, catalog) == {}


def test_urls_in_json_strings_are_not_comments(tmp_path, catalog):
    write(tmp_path, {"config.json": '{"endpoint": "https://api.anthropic.com/v1"}\n'})
    assert "anthropic" in deps(tmp_path, catalog)


def test_redaction_keeps_code_and_variable_names():
    assert redact('api_key = api_key or os.getenv("X")') == 'api_key = api_key or os.getenv("X")'
    assert redact('EnvKey = "FIRECRAWL_API_KEY"') == 'EnvKey = "FIRECRAWL_API_KEY"'
    assert redact('ANTHROPIC_API_KEY="realsecretvalue123"') == 'ANTHROPIC_API_KEY="****"'


def test_vscode_extension_id_is_not_a_bedrock_model(tmp_path, catalog):
    write(
        tmp_path, {".devcontainer/devcontainer.json": '{"extensions": ["anthropic.claude-code"]}\n'}
    )
    assert found(tmp_path, catalog) == {}


def test_npm_scope_is_not_a_gateway_model_prefix(tmp_path, catalog):
    write(tmp_path, {"a.ts": 'import { OpenRouter } from "@openrouter/sdk";\n'})
    hits = found(tmp_path, catalog)
    assert all(f.kind != "model" for f in hits.get("openrouter", []))


def test_open_snowflake_embeddings_are_open(tmp_path, catalog):
    write(tmp_path, {"conf.json": '{"model": "snowflake/arctic-embed-l"}\n'})
    assert "snowflake-cortex" not in found(tmp_path, catalog)


def test_github_models_is_its_own_service(tmp_path, catalog):
    write(
        tmp_path,
        {"app.properties": "spring.ai.openai.base-url=https://models.github.ai/inference\n"},
    )
    hits = deps(tmp_path, catalog)
    assert "github-models" in hits and "azure-ai-foundry" not in hits


def test_a_wrapper_module_named_like_the_library_imports_the_library(tmp_path, catalog):
    write(tmp_path, {
        "kotaemon/__init__.py": "",
        "kotaemon/storages/__init__.py": "",
        "kotaemon/storages/lancedb.py": "import lancedb\n",
    })  # fmt: skip
    assert "lancedb/lancedb" in running(tmp_path, catalog)


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_a_project_is_not_a_component_of_itself(tmp_path, catalog):
    write(tmp_path, {"helm/values.yaml": "image:\n  repository: infiniflow/ragflow\n"})
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/infiniflow/ragflow.git"],
        cwd=tmp_path,
        check=True,
    )
    assert "infiniflow/ragflow" not in running(tmp_path, catalog)


# --- open source recall: signatures, images, SQL, extras --------------------------------


@pytest.mark.parametrize(
    ("files", "repo"),
    [
        (
            {
                "QdrantExample.java": 'try (var qdrant = new QdrantContainer("qdrant/qdrant:v1.12.4")) {}\n'
            },
            "qdrant/qdrant",
        ),
        (
            {"PgVectorExample.java": 'DockerImageName.parse("pgvector/pgvector:pg16")\n'},
            "pgvector/pgvector",
        ),
        ({"run-qdrant.sh": "docker run -d -p 6333:6333 qdrant/qdrant\n"}, "qdrant/qdrant"),
        (
            {
                "docker-compose.yml": "services:\n  db:\n    image: ${QDRANT_IMAGE:-qdrant/qdrant:v1.12}\n"
            },
            "qdrant/qdrant",
        ),
        (
            {
                "helm/prod-values.yaml": 'servingEngineSpec:\n  modelSpec:\n  - repository: "vllm/vllm-openai"\n'
            },
            "vllm-project/vllm",
        ),
        (
            {
                "charts/langfuse/values.yaml": "image:\n  repository: docker.langfuse.com/langfuse/langfuse\n"
            },
            "langfuse/langfuse",
        ),
        (
            {
                "supabase/migrations/001.sql": "create extension if not exists vector with schema extensions;\n"
            },
            "pgvector/pgvector",
        ),
        ({"m.sql": 'CREATE EXTENSION "vector";\n'}, "pgvector/pgvector"),
        (
            {
                "pyproject.toml": '[tool.poetry.dependencies]\nqdrant-client = { extras = ["fastembed"], version = "^1.9" }\n'
            },
            "qdrant/fastembed",
        ),
        ({"requirements.txt": "qdrant-client[fastembed]>=1.9\n"}, "qdrant/fastembed"),
        (
            {
                "build.gradle": "implementation 'org.springframework.ai:spring-ai-starter-model-ollama'\n"
            },
            "ollama/ollama",
        ),
        (
            {
                "pom.xml": "<dependency><groupId>dev.langchain4j</groupId><artifactId>langchain4j-chroma</artifactId></dependency>\n"
            },
            "chroma-core/chroma",
        ),
        (
            {
                "Directory.Packages.props": '<PackageVersion Include="LLamaSharp" Version="0.20.0" />\n'
            },
            "ggml-org/llama.cpp",
        ),
        (
            {"go.mod": "module x\nrequire github.com/amikos-tech/chroma-go v0.1.0\n"},
            "chroma-core/chroma",
        ),
        (
            {"pyproject.toml": '[project]\ndependencies = ["llama-index-vector-stores-milvus"]\n'},
            "milvus-io/milvus",
        ),
        (
            {"config.py": 'SEARXNG_QUERY_URL = os.environ.get("SEARXNG_QUERY_URL", "")\n'},
            "searxng/searxng",
        ),
        (
            {"chains.py": 'emb = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")\n'},
            "huggingface/sentence-transformers",
        ),
    ],
    ids=lambda v: v if isinstance(v, str) else next(iter(v)),
)
def test_open_source_recall(tmp_path, catalog, files, repo):
    write(tmp_path, files)
    assert repo in running(tmp_path, catalog)


def test_image_needles_do_not_match_repo_urls(tmp_path, catalog):
    write(tmp_path, {"links.py": 'SEE = "https://github.com/qdrant/qdrant"\n'})
    assert "qdrant/qdrant" not in running(tmp_path, catalog)


def test_sql_about_other_extensions_is_not_pgvector(tmp_path, catalog):
    write(
        tmp_path, {"m.sql": "CREATE EXTENSION IF NOT EXISTS vectors;\nCREATE EXTENSION pg_trgm;\n"}
    )
    assert running(tmp_path, catalog) == {}


def test_catalog_rejects_unquoted_commas_in_descriptions(tmp_path):
    import shutil as sh

    from unrent.catalog import CatalogError

    cat = tmp_path / "cat"
    sh.copytree(CATALOG_DIR, cat)
    text = (cat / "alternatives.yaml").read_text(encoding="utf-8")
    text = text.replace(
        'what: "Run open models locally with one command, OpenAI-compatible API"',
        "what: Run open models locally with one command, OpenAI-compatible API",
    )
    (cat / "alternatives.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(CatalogError, match="unexpected keys"):
        load_catalog(cat)
