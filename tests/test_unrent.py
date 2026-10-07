"""Tests. Detection is the product, so most of these pin down one edge case each."""

from __future__ import annotations

import dataclasses
import datetime
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from unrent.catalog import (  # noqa: E402
    OPEN_LICENCES,
    OPEN_MODEL_LICENCES,
    CatalogError,
    load_catalog,
)
from unrent.cli import main  # noqa: E402
from unrent.detect import collect_facts, image_name, js_package, match, redact  # noqa: E402
from unrent.render import payload, standings, to_json, to_markdown  # noqa: E402
from unrent.retired import replacement, snaps  # noqa: E402
from unrent.terminal import Progress, to_terminal, wants_colour, why  # noqa: E402

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
    """Closed services reported as dependencies, not merely named by a model id or an
    example env file."""
    return {
        f.service.id
        for f in match(collect_facts(root, catalog, **kw), catalog)
        if not f.models_only and not f.template_only and not f.service.open_source
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


@pytest.mark.parametrize(
    "line,service",
    [
        ('URL = "https://us-central1-aiplatform.googleapis.com/v1/projects/p"', "google-vertex"),
        ('opts = {"api_endpoint": f"{location}-documentai.googleapis.com"}', "google-document-ai"),
        ('opts = {"api_endpoint": "eu-discoveryengine.googleapis.com"}', "google-vertex-search"),
    ],
)
def test_region_joined_to_a_host_by_a_hyphen(tmp_path, catalog, line, service):
    write(tmp_path, {"client.py": line + "\n"})
    assert service in found(tmp_path, catalog)


@pytest.mark.parametrize(
    "line,service",
    [
        ('URL = "https://my-modal.run/predict"', "modal"),  # another registered domain
        ('import { client } from "./amazon-bedrock-runtime.ts";', "aws-bedrock"),  # a file
    ],
)
def test_hyphen_before_a_registered_domain_or_partial_host(tmp_path, catalog, line, service):
    write(tmp_path, {"client.ts": line + "\n"})
    assert service not in found(tmp_path, catalog)


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


def test_vendor_host_next_to_a_local_server_is_still_the_vendor(tmp_path, catalog):
    write(tmp_path, {"clients.py": (
        'hf = OpenAI(base_url="https://api-inference.huggingface.co/v1/")\n'
        'local = OpenAI(base_url="http://localhost:11434/v1")\n'
    )})  # fmt: skip
    assert "huggingface-inference" in deps(tmp_path, catalog)


def test_litellm_provider_routes_are_calls(tmp_path, catalog):
    files = {
        "settings.yml": "- name: bedrock_converse/anthropic.claude-3-7-sonnet-20250219-v1:0\n"
        "- name: azure/o1-mini\n- name: xai/grok-4\n- name: openai/qwen-max\n",
    }
    write(tmp_path, files)
    assert deps(tmp_path, catalog) == set()  # a model menu, nothing calls it
    write(tmp_path, {"llm.py": "import litellm\n"})
    assert deps(tmp_path, catalog) == {"aws-bedrock", "azure-openai", "xai"}


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


def test_rust_test_modules_are_tests(tmp_path, catalog):
    write(tmp_path, {
        "src/broker_tests.rs": 'env.insert("ANTHROPIC_API_KEY".to_string());\n',
        "src/proxy/tests.rs": 'env.insert("MISTRAL_API_KEY".to_string());\n',
    })  # fmt: skip
    findings = match(collect_facts(tmp_path, catalog), catalog)
    assert {f.service.id for f in findings} == {"anthropic", "mistral"}
    assert all(f.test_only for f in findings)


def test_jekyll_site_data_is_not_code(tmp_path, catalog):
    write(tmp_path, {
        "website/_config.yml": "title: x\n",
        "website/_data/leaderboard.yml": "- command: OPENAI_API_BASE=https://dashscope-intl.aliyuncs.com/compatible-mode/v1\n",
        "app/_data/providers.yml": "base: https://dashscope-intl.aliyuncs.com/compatible-mode/v1\n",
    })  # fmt: skip
    cited = found(tmp_path, catalog)["alibaba-model-studio"]
    assert [c.file.relative_to(tmp_path).as_posix() for c in cited] == ["app/_data/providers.yml"]


def test_docusaurus_static_data_is_not_code(tmp_path, catalog):
    # goose's documentation/static/servers.json: the MCP servers its site lists
    write(tmp_path, {
        "documentation/docusaurus.config.ts": "export default {};\n",
        "documentation/static/servers.json": '[{"environmentVariables": [{"name": "EXA_API_KEY"}]}]\n',
        "web/static/config.json": '{"key": "BROWSERBASE_API_KEY"}\n',
    })  # fmt: skip
    assert set(found(tmp_path, catalog)) == {"browserbase"}


def test_rust_cfg_test_module_is_test_code(tmp_path, catalog):
    write(tmp_path, {"src/extensions.rs": (
        'const KEY: &str = "ANTHROPIC_API_KEY";\n\n'
        '#[cfg(test)]\nmod tests {\n    const KEY: &str = "BRAVE_API_KEY";\n}\n'
    )})  # fmt: skip
    findings = {f.service.id: f for f in match(collect_facts(tmp_path, catalog), catalog)}
    assert not findings["anthropic"].test_only and findings["brave-search"].test_only


@pytest.mark.parametrize(
    ("files", "service"),
    [
        (
            {
                "client.py": "from anthropic import AsyncAnthropicBedrock\nc = AsyncAnthropicBedrock()\n"
            },
            "aws-bedrock",
        ),
        ({"copilot.rs": 'const API: &str = "https://api.githubcopilot.com";\n'}, "github-copilot"),
        (
            {"models.py": 'url = "https://api.github.com/copilot_internal/v2/token"\n'},
            "github-copilot",
        ),
        (
            {"sagemaker.go": 'import "github.com/aws/aws-sdk-go/service/sagemakerruntime"\n'},
            "aws-sagemaker",
        ),
        (
            {"go.mod": "module x\nrequire github.com/hupe1980/go-huggingface v0.0.15\n"},
            "huggingface-inference",
        ),
        ({"go.mod": "module x\nrequire github.com/IBM/watsonx-go v1.0.1\n"}, "ibm-watsonx"),
        (
            {"chains.py": "from langchain_aws import ChatBedrock\nllm = ChatBedrock(model_id=m)\n"},
            "aws-bedrock",
        ),
        # The 2026-10-07 rotation. crush names providers with catwalk's constants.
        ({"agent.go": "case string(catwalk.InferenceProviderBaseten):\n"}, "baseten"),
        ({"agent.go": "case string(catwalk.InferenceProviderDeepSeek):\n"}, "deepseek-api"),
        ({"agent.go": "case catwalk.InferenceProviderMiniMaxChina:\n"}, "minimax"),
        ({"config.go": "case catwalk.TypeVercel:\n"}, "vercel-ai-gateway"),
        (
            {"WebApp.csproj": '<PackageReference Include="Aspire.Azure.AI.OpenAI" />\n'},
            "azure-openai",
        ),
        (
            {"tts.js": "const url = schema?.url || 'https://api.openai.com/v1/audio/speech';\n"},
            "openai-text-to-speech",
        ),
        (
            {
                "stt.js": "const url = schema?.url || 'https://api.openai.com/v1/audio/transcriptions';\n"
            },
            "openai-speech-to-text",
        ),
        ({"Mod.java": "var m = OpenAiModerationModel.builder().build();\n"}, "openai-moderation"),
        (
            {"Rag.java": "var m = MistralAiEmbeddingModel.builder().build();\n"},
            "mistral-embeddings",
        ),
        (
            {
                "Together.ts": "import { ChatTogetherAI } from '@langchain/community/chat_models/togetherai'\n"
            },
            "together",
        ),
        ({"Serper.ts": "import { Serper } from '@langchain/community/tools/serper'\n"}, "serper"),
        (
            {
                "Flux.js": "const base = process.env.FLUX_API_BASE_URL || 'https://api.us1.bfl.ai';\n"
            },
            "bfl-api",
        ),
    ],
    ids=lambda v: v if isinstance(v, str) else next(iter(v)),
)
def test_holdout_sdks_and_hosts(tmp_path, catalog, files, service):
    write(tmp_path, files)
    assert service in deps(tmp_path, catalog)


def test_github_mcp_server_and_neptune_are_not_ai_apis(tmp_path, catalog):
    write(tmp_path, {
        "mcp.json": '{"url": "https://api.githubcopilot.com/mcp/"}\n',
        "pyproject.toml": '[project]\ndependencies = ["langchain-aws>=0.2"]\n',
        "neptune.py": "from langchain_aws.graphs import NeptuneGraph\n",
    })  # fmt: skip
    assert deps(tmp_path, catalog) == set()


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


def test_refresh_refuses_to_publish_a_collapse(refresh):
    before = {"vector-db": {"ranked": [{}] * 8}, "rag": {"ranked": [{}] * 5}}
    assert refresh.shrunk_pools(before, {"vector-db": {"ranked": []}, "rag": {"ranked": [{}] * 5}})
    assert refresh.shrunk_pools(
        before, {"vector-db": {"ranked": [{}] * 3}, "rag": {"ranked": [{}] * 5}}
    )
    assert not refresh.shrunk_pools(
        before, {"vector-db": {"ranked": [{}] * 7}, "rag": {"ranked": [{}] * 5}}
    )


def test_refresh_treats_rate_limits_as_transient(refresh, monkeypatch):
    import io
    import urllib.error

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


@pytest.fixture(scope="module")
def refresh():
    """scripts/refresh.py, loaded once; a test that patches it does so with monkeypatch."""
    spec = importlib.util.spec_from_file_location("refresh", ROOT / "scripts" / "refresh.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_momentum_needs_four_weeks_and_scales(refresh):
    today = datetime.date(2026, 9, 26)
    assert refresh.momentum([["2026-09-20", 100], ["2026-09-26", 150]], today) is None
    assert refresh.momentum([["2026-08-27", 100], ["2026-09-26", 400]], today) == 900


def test_newcomers_rank_after_projects_with_momentum(refresh):
    ranked, by = refresh.rank_github([
        {"repo": "a/big", "stars": 90000, "stars_90d": 100},
        {"repo": "b/hot", "stars": 5000, "stars_90d": 3000},
        {"repo": "c/new", "stars": 99999, "stars_90d": None},
    ])  # fmt: skip
    assert by == "momentum"
    assert [p["repo"] for p in ranked] == ["b/hot", "a/big", "c/new"]


def test_same_lab_fine_tunes_are_original(refresh):
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


PROVIDER_KEYS = [
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY", "COHERE_API_KEY",
    "DEEPSEEK_API_KEY", "XAI_API_KEY", "PERPLEXITY_API_KEY", "TOGETHER_API_KEY",
    "FIREWORKS_API_KEY", "OPENROUTER_API_KEY", "CEREBRAS_API_KEY", "DEEPINFRA_API_KEY",
    "SAMBANOVA_API_KEY", "NOVITA_API_KEY", "MOONSHOT_API_KEY", "AI21_API_KEY", "NEBIUS_API_KEY",
]  # fmt: skip


def test_a_provider_catalog_names_without_depending(tmp_path, catalog):
    # goose's provider_metadata.json: a host and a key for every provider models.dev knows
    entries = [{"id": k.split("_")[0].lower(), "env": [k]} for k in PROVIDER_KEYS]
    write(tmp_path, {
        "data/providers.json": json.dumps(entries, indent=2),
        "app.py": "from groq import Groq\nclient = Groq()\n",
    })  # fmt: skip
    assert len(found(tmp_path, catalog).keys() - {"groq"}) >= 15
    assert deps(tmp_path, catalog) == {"groq"}  # a call elsewhere still counts


def test_a_price_table_names_without_depending(tmp_path, catalog):
    # firecrawl's model-prices.ts, copied from LiteLLM: model ids, a few `@cf/` keys
    models = ["gpt-4o", "text-moderation-latest", "tts-1", "whisper-1", "claude-3-5-sonnet",
              "mistral-large", "gemini-1.5-pro", "command-r", "grok-2", "@cf/meta/llama-3-8b"]  # fmt: skip
    rows = "".join(f'  "{m}": {{ input_cost_per_token: 1e-6 }},\n' for m in models)
    write(tmp_path, {
        "usage/model-prices.ts": f"export const modelPrices = {{\n{rows}}};\n",
        "llm.ts": 'import OpenAI from "openai";\nconst client = new OpenAI();\n',
    })  # fmt: skip
    assert deps(tmp_path, catalog) == {"openai"}  # not its moderation, speech or Whisper


def test_a_price_table_split_per_provider_is_one_registry(tmp_path, catalog):
    # onyx's price_table/*.json, one file per provider; a test naming the model too
    tables = {"openai": "text-embedding-3-small", "anthropic": "claude-3-5-sonnet",
              "mistral": "mistral-large", "cohere": "command-r", "xai": "grok-2",
              "gemini": "gemini-1.5-pro"}  # fmt: skip
    write(tmp_path, {
        **{f"price_table/{p}.json": f'{{"{m}": {{"input": 1}}}}\n' for p, m in tables.items()},
        "tests/test_prices.py": 'assert price("text-embedding-3-small")\n',
        "llm.py": "from openai import OpenAI\nclient = OpenAI()\n",
    })  # fmt: skip
    assert deps(tmp_path, catalog) == {"openai"}


def test_a_placeholder_model_id_is_an_example(tmp_path, catalog):
    write(tmp_path, {"ChatOpenAICustom.ts": (
        "import OpenAI from 'openai'\n"
        "const field = { name: 'modelName', placeholder: 'ft:gpt-3.5-turbo:my-org:custom_suffix:id' }\n"
    )})  # fmt: skip
    assert deps(tmp_path, catalog) == {"openai"}


def test_a_class_the_project_defines_is_its_own(tmp_path, catalog):
    # firecrawl's own SearchIndexClient, not Azure AI Search's
    write(tmp_path, {
        "lib/search-index-client.ts": "export class SearchIndexClient {\n}\n",
        "search.ts": "import { SearchIndexClient } from './lib/search-index-client'\n",
        "py/types.py": "class Params(TypedDict):\n    cloud_inference: bool\n",
    })  # fmt: skip
    assert found(tmp_path, catalog) == {}


def test_capabilities_through_a_local_server_are_not_the_vendor(tmp_path, catalog):
    write(tmp_path, {
        "examples/assistants.py": (
            "from openai import OpenAI\n"
            'client = OpenAI(base_url="http://localhost:8080/openai-assistants")\n'
            "client.beta.assistants.create(name='x')\n"
        ),
        "crew/providers.py": (
            'OLLAMA = dict(base_url="http://localhost:11434/v1", api_key_env="OLLAMA_API_KEY")\n'
        ),
    })  # fmt: skip
    assert deps(tmp_path, catalog) == set()
    # A local server in configuration covers the client code, not its Assistants calls.
    write(tmp_path, {
        ".env": "OPENAI_BASE_URL=http://localhost:11434/v1\n",
        "app/assistant.py": "from openai import OpenAI\nOpenAI().beta.assistants.create(name='x')\n",
    })  # fmt: skip
    assert deps(tmp_path, catalog) == {"openai-assistants"}


def test_a_router_calling_several_providers_counts(tmp_path, catalog):
    write(tmp_path, {"router.py": (
        "from openai import OpenAI\nfrom anthropic import Anthropic\nimport replicate\n"
        "from mistralai import Mistral\nimport cohere\nfrom google import genai\n"
        'CLIENTS = {"gpt-4o": OpenAI(), "claude-3-5-sonnet": Anthropic(), "llama-3": replicate,\n'
        '    "mistral-large": Mistral(), "command-r": cohere.Client(),\n'
        '    "gemini-1.5-pro": genai.Client()}\n'
    )})  # fmt: skip
    assert {"openai", "anthropic", "replicate", "mistral", "cohere", "google-gemini"} <= deps(
        tmp_path, catalog
    )


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


def test_hosted_open_source_points_at_the_same_project(tmp_path, catalog):
    write(
        tmp_path,
        {
            "a.py": 'from qdrant_client import QdrantClient\nQdrantClient(url="https://x.cloud.qdrant.io")\n'
        },
    )
    findings = match(collect_facts(tmp_path, catalog), catalog)
    entry = next(
        e for e in payload(findings, tmp_path, catalog)["found"] if e["id"] == "qdrant-cloud"
    )
    assert entry["self_host"] == "qdrant/qdrant"
    md = to_markdown(findings, tmp_path, catalog)
    assert "Self-hosted: [qdrant/qdrant]" in md
    table_row = next(ln for ln in md.splitlines() if ln.startswith("| Qdrant Cloud"))
    assert table_row.count("[qdrant/qdrant]") == 1  # not again among the pool's picks
    row = next(ln for ln in terminal(tmp_path, catalog).splitlines() if "Qdrant Cloud" in ln)
    assert row.endswith("self-host qdrant/qdrant")
    assert "Its open source core, self-hosted" in why(
        next(f for f in findings if f.service.id == "qdrant-cloud"), tmp_path, catalog
    )


def test_self_host_dropped_from_its_ranking_falls_back_to_the_pool(tmp_path, catalog):
    # An archived or inactive project leaves the ranking; nothing should still point at it.
    pools = dict(catalog.pools)
    vdb = pools["vector-db"]
    pools["vector-db"] = dataclasses.replace(
        vdb, alternatives=tuple(a for a in vdb.alternatives if a.name != "qdrant/qdrant")
    )
    dropped = dataclasses.replace(catalog, pools=pools)
    write(
        tmp_path, {"a.py": 'QdrantClient(url="https://x.cloud.qdrant.io")\nimport qdrant_client\n'}
    )
    findings = match(collect_facts(tmp_path, dropped), dropped)
    qc = next(f for f in findings if f.service.id == "qdrant-cloud")
    assert dropped.self_hosted(qc.service) is None
    entry = next(
        e for e in payload(findings, tmp_path, dropped)["found"] if e["id"] == "qdrant-cloud"
    )
    assert "self_host" not in entry
    row = next(ln for ln in terminal(tmp_path, dropped).splitlines() if "Qdrant Cloud" in ln)
    assert "self-host" not in row and "milvus" in row


def test_summary_names_several_alternatives_not_one(tmp_path, catalog):
    # A ranking by stars is popularity, not fit: the summary row never crowns one.
    write(tmp_path, {"requirements.txt": "pinecone\n"})
    row = next(ln for ln in terminal(tmp_path, catalog).splitlines() if "Pinecone" in ln)
    tops = [a.name.split("/")[1] for a in catalog.pools["vector-db"].alternatives[:3]]
    assert row.endswith(", ".join(tops))


def test_pools_hold_only_substitutes(catalog):
    # faiss and tantivy are libraries: peers of other libraries, not of hosted databases.
    for pool, library in (
        ("vector-db", "facebookresearch/faiss"),
        ("search-engine", "quickwit-oss/tantivy"),
    ):
        assert library not in {a.name for a in catalog.pools[pool].alternatives}
    assert catalog.pools["vector-index"].alternatives[0].kind


def test_self_host_must_be_in_a_pool_it_replaces_with(tmp_path):
    cat = tmp_path / "cat"
    shutil.copytree(CATALOG_DIR, cat)
    path = cat / "services" / "retrieval.yaml"
    text = path.read_text(encoding="utf-8").replace(
        "self_host: qdrant/qdrant", "self_host: ollama/ollama"
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(CatalogError, match="self_host 'ollama/ollama' is in none"):
        load_catalog(cat)


def test_a_repo_moved_out_of_a_pool_leaves_its_old_ranking(tmp_path):
    cat = tmp_path / "cat"
    shutil.copytree(CATALOG_DIR, cat)
    rankings = json.loads((cat / "rankings.json").read_text(encoding="utf-8"))
    stale = {"repo": "facebookresearch/faiss", "url": "u", "stars": 1}
    rankings["pools"]["vector-db"]["ranked"].insert(0, stale)
    (cat / "rankings.json").write_text(json.dumps(rankings), encoding="utf-8")
    names = {a.name for a in load_catalog(cat).pools["vector-db"].alternatives}
    assert "facebookresearch/faiss" not in names


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
    pool = catalog.pools["vector-index"]
    without = tuple(a for a in pool.alternatives if a.name != "facebookresearch/faiss")
    monkeypatch.setitem(
        catalog.pools, "vector-index", dataclasses.replace(pool, alternatives=without)
    )
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


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("environment.yml", "created: 2020-13-45\ndependencies: [anthropic]\n"),
        ("pnpm-workspace.yaml", "catalog: {x: !!timestamp foo}\n"),
        ("pubspec.yaml", "flag: !!bool maybe\n"),
        ("environment.yaml", "[" * 100_000),
        ("pyproject.toml", "a = " + "[" * 100_000),
        ("package.json", "[" * 200_000),
        ("notebook.ipynb", "[" * 200_000),
        ("app.py", "x = " + "-" * 100_000 + "1\n"),
        # Valid, but not the shape the format documents.
        ("pyproject.toml", "project = 3\n"),
        ("pyproject.toml", "[tool.poetry]\ndependencies = 3\n"),
        ("Pipfile", "packages = 3\n"),
        ("Cargo.toml", "dependencies = 3\n"),
        ("pnpm-workspace.yaml", "catalogs: [1]\n"),
        ("pubspec.yaml", "dependencies: 3\n"),
        ("environment.yml", "dependencies: [{pip: 3}]\n"),
        ("composer.json", '{"require": [1]}'),
        ("notebook.ipynb", '{"cells": [1]}'),
        ("notebook.ipynb", '{"metadata": {"kernelspec": 3}}'),
    ],
    ids=lambda v: v if len(v) < 40 else f"{v[:8]}...",
)
def test_files_that_break_their_parser_are_skipped(tmp_path, catalog, name, body):
    """A scanned file is untrusted: one the parser chokes on is skipped like any
    unparseable file, and the scan still reports the rest of the repo."""
    write(tmp_path, {name: body, "requirements.txt": "openai\n"})
    assert "openai" in deps(tmp_path, catalog)


def test_ripgrep_reads_only_the_scanned_files(tmp_path, catalog, search_mode):
    write(tmp_path, {".gitignore": "cache/\n", "src/a.py": "import anthropic\n"})
    for i in range(50):
        write(tmp_path, {f"cache/{i}.txt": "OPENAI_API_KEY api.openai.com\n" * 2000})
    if shutil.which("git"):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert set(deps(tmp_path, catalog)) == {"anthropic"}


def test_bad_rankings_snapshot_is_a_clear_error(tmp_path, capsys):
    cat = tmp_path / "cat"
    shutil.copytree(CATALOG_DIR, cat)
    (cat / "rankings.json").write_text("{not json", encoding="utf-8")
    assert main(["catalog", "--validate", "--catalog", str(cat)]) == 2
    assert "rankings.json" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("file", "edit"),
    [
        ("services/zz.yaml", lambda _: "- {id: x, name: X, category: Y, replace_with: [p], detect: [a]}"),
        ("services/zz.yaml", lambda _: "- {id: x, added: 2020-13-45}\n"),
        ("services/zz.yaml", lambda _: "[" * 100_000),
        # A project that is not a mapping, listed in a pool so it gets that far.
        ("alternatives.yaml", lambda t: t.replace("  facebookresearch/faiss:\n", "  facebookresearch/faiss: 3\n  x/faiss:\n")),
        ("rankings.json", lambda _: '{"pools": [1]}'),
    ],
)  # fmt: skip
def test_malformed_catalog_is_a_clear_error(tmp_path, capsys, file, edit):
    cat = tmp_path / "cat"
    shutil.copytree(CATALOG_DIR, cat)
    target = cat / file
    target.write_text(edit(target.read_text("utf-8") if target.is_file() else ""), "utf-8")
    assert main(["catalog", "--validate", "--catalog", str(cat)]) == 2
    assert "unrent: catalog error:" in capsys.readouterr().err


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


def test_prose_in_json_strings_is_not_code(tmp_path, catalog):
    # A saved LLM answer quotes vendors and code; the config next to it calls one.
    write(tmp_path, {
        "results/run.json": '{"answer": "The docs show the agent with a model from another vendor: '
        '`create_agent(model=\\"anthropic:claude-sonnet-4-6\\")`, at https://api.anthropic.com/v1."}\n',
        "config.json": '{"endpoint": "https://api.openai.com/v1", "cmd": "pip install x"}\n',
    })  # fmt: skip
    assert set(deps(tmp_path, catalog)) == {"openai"}


# An n8n Code node and a Langflow custom component: code that runs, stored as a string.
N8N_CODE = """// Summarise the incoming text with the chat model
const res = await this.helpers.httpRequest({
  method: 'POST',
  url: 'https://api.openai.com/v1/chat/completions',
  headers: { Authorization: `Bearer ${$env.OPENAI_API_KEY}` },
  body: { model: 'gpt-4o-mini', messages: [{ role: 'user', content: $json.text }] },
  json: true,
});
return [{ json: { summary: res.choices[0].message.content } }];"""
LANGFLOW_CODE = """from anthropic import Anthropic
import os

class Summarizer(Component):
    display_name = "Summarizer"

    def build(self, text: str) -> str:
        client = Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
        reply = client.messages.create(model="claude-sonnet-4-5", max_tokens=512,
                                       messages=[{"role": "user", "content": text}])
        return reply.content[0].text"""


@pytest.mark.parametrize(
    ("flow", "service"),
    [
        ({"nodes": [{"type": "n8n-nodes-base.code", "parameters": {"jsCode": N8N_CODE}}]}, "openai"),
        ({"data": {"nodes": [{"data": {"node": {"template": {"code": {"value": LANGFLOW_CODE}}}}}]}},
         "anthropic"),
    ],
)  # fmt: skip
def test_code_in_json_strings_is_code(tmp_path, catalog, flow, service):
    write(tmp_path, {"flows/flow.json": json.dumps(flow, indent=2)})
    assert service in deps(tmp_path, catalog)


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
        # gpt-researcher (held out): three spellings the catalog missed.
        ({"searx.py": 'host = os.environ["SEARX_URL"]\n'}, "searxng/searxng"),
        (
            {"base.py": 'llm = ChatOpenAI(openai_api_base=os.environ["VLLM_OPENAI_API_BASE"])\n'},
            "vllm-project/vllm",
        ),
        (
            {
                "firecrawl.py": 'url = os.getenv("FIRECRAWL_SERVER_URL", "https://api.firecrawl.dev")\n'
            },
            "firecrawl/firecrawl",
        ),
        (
            {"store.py": "from langchain_community.vectorstores import FAISS\n"},
            "facebookresearch/faiss",
        ),
        # The 2026-10-07 holdout: providers named in code, LiteLLM routes, CMake.
        ({"models.py": 'local = name.startswith("ollama_chat/")\n'}, "ollama/ollama"),
        ({"models.py": 'local = name.startswith("ollama/")\n'}, "ollama/ollama"),
        (
            {
                "CMakeLists.txt": "FetchContent_Declare(\n  llama\n"
                "  GIT_REPOSITORY https://github.com/ggml-org/llama.cpp.git\n)\n"
            },
            "ggml-org/llama.cpp",
        ),
        ({"llama_cpp.rs": 'const PROVIDER_ID: &str = "llama.cpp";\n'}, "ggml-org/llama.cpp"),
        ({"localai.go": 'const localAIClientName = "localai"\n'}, "mudler/LocalAI"),
        ({"Api.php": "$url = $base . '.well-known/localai.json';\n"}, "mudler/LocalAI"),
        ({"litellm.go": 'const liteLLMClientName = "litellm"\n'}, "BerriAI/litellm"),
        (
            {"search.py": "from graphrag.vector_stores.lancedb import LanceDBVectorStore\n"},
            "lancedb/lancedb",
        ),
        (
            {
                "application.yml": "otlp:\n  endpoint: "
                "${LANGFUSE_OTLP:https://cloud.langfuse.com/api/public/otel/v1/traces}\n"
            },
            "langfuse/langfuse",
        ),
        # The 2026-10-07 rotation: providers named for the project, integration imports.
        ({"enums.py": 'SEARXNG = "searxng"\n'}, "searxng/searxng"),
        ({"Searxng.ts": "this.label = 'SearXNG'\n"}, "searxng/searxng"),
        ({"Vllm.ts": 'static providerName = "vllm";\n'}, "vllm-project/vllm"),
        (
            {"chat_model.py": '_FACTORY_NAME = ["VLLM", "OpenAI-API-Compatible"]\n'},
            "vllm-project/vllm",
        ),
        ({"constants.py": 'BIFROST_PROVIDER_NAME = "bifrost"\n'}, "maximhq/bifrost"),
        (
            {"llamacpp.go": 'RegisterEnricher("llamacpp", &llamacppEnricher{})\n'},
            "ggml-org/llama.cpp",
        ),
        ({"rag.py": "from langchain_chroma import Chroma\n"}, "chroma-core/chroma"),
        (
            {"store.ts": "import { Chroma } from '@langchain/community/vectorstores/chroma'\n"},
            "chroma-core/chroma",
        ),
        (
            {
                "st.py": "from chromadb.utils import embedding_functions\n"
                "ef = embedding_functions.SentenceTransformerEmbeddingFunction()\n"
            },
            "huggingface/sentence-transformers",
        ),
        (
            {
                "AppHost.csproj": '<PackageReference Include="CommunityToolkit.Aspire.Hosting.Ollama" />\n'
            },
            "ollama/ollama",
        ),
        (
            {"handler.ts": "baseUrl: endpoint ?? 'https://www.comet.com/opik/api'\n"},
            "comet-ml/opik",
        ),
    ],
    ids=lambda v: v if isinstance(v, str) else next(iter(v)),
)
def test_open_source_recall(tmp_path, catalog, files, repo):
    write(tmp_path, files)
    assert repo in running(tmp_path, catalog)


def test_provider_names_in_stories_and_dependabot_are_not_used(tmp_path, catalog):
    write(tmp_path, {
        "combobox.stories.tsx": "const options = [{ value: 'localai', label: 'LocalAI' }];\n",
        ".github/dependabot.yml": "groups:\n  llm:\n    patterns:\n      - 'litellm'\n",
    })  # fmt: skip
    assert running(tmp_path, catalog) == {}


def test_image_needles_do_not_match_repo_urls(tmp_path, catalog):
    write(tmp_path, {"links.py": 'SEE = "https://github.com/qdrant/qdrant"\n'})
    assert "qdrant/qdrant" not in running(tmp_path, catalog)


def test_sql_about_other_extensions_is_not_pgvector(tmp_path, catalog):
    write(
        tmp_path, {"m.sql": "CREATE EXTENSION IF NOT EXISTS vectors;\nCREATE EXTENSION pg_trgm;\n"}
    )
    assert running(tmp_path, catalog) == {}


def test_catalog_rejects_unquoted_commas_in_descriptions(tmp_path):
    cat = tmp_path / "cat"
    shutil.copytree(CATALOG_DIR, cat)
    text = (cat / "alternatives.yaml").read_text(encoding="utf-8")
    text = text.replace(
        'what: "Run open models locally with one command, OpenAI-compatible API"',
        "what: Run open models locally with one command, OpenAI-compatible API",
    )
    (cat / "alternatives.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(CatalogError, match="unexpected keys"):
        load_catalog(cat)


def test_catalog_rejects_a_key_given_twice(tmp_path):
    cat = tmp_path / "cat"
    shutil.copytree(CATALOG_DIR, cat)
    text = (cat / "services" / "llm.yaml").read_text(encoding="utf-8")
    text = text.replace(
        "    endpoint: [api.reka.ai]\n",
        "    endpoint: [api.reka.ai]\n    endpoint: [reka.ai/v1]\n",
    )
    (cat / "services" / "llm.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(CatalogError, match=r"llm\.yaml: .*duplicate key 'endpoint'"):
        load_catalog(cat)


# The vendors' own examples point the OpenAI (or Anthropic) SDK at their host
# (2026-10-04 review of the catalog against current docs).
SDK_POINTED_AT_VENDOR = [
    ("reka", "openai", 'base_url="https://api.reka.ai/v1"'),
    ("asksage", "openai", 'base_url="https://api.asksage.ai/server/openai/v1"'),
    ("inworld-tts", "openai", 'base_url="https://api.inworld.ai/v1"'),
    ("mixedbread-api", "openai", 'base_url="https://api.mixedbread.com/v1"'),
    (
        "oci-generative-ai",
        "openai",
        'base_url="https://inference.generativeai.us-chicago-1.oci.oraclecloud.com/openai/v1"',
    ),
    ("modelslab", "openai", 'base_url="https://modelslab.com/api/v7/llm"'),
    ("recraft", "openai", 'base_url="https://external.api.recraft.ai/v1"'),
    (
        "snowflake-cortex",
        "openai",
        'base_url="https://myorg-myaccount.snowflakecomputing.com/api/v2/cortex/v1"',
    ),
    (
        "snowflake-cortex",
        "anthropic",
        'base_url="https://myorg-myaccount.snowflakecomputing.com/api/v2/cortex"',
    ),
]


@pytest.mark.parametrize("service,sdk,base_url", SDK_POINTED_AT_VENDOR)
def test_sdk_pointed_at_a_vendor_is_that_vendor(tmp_path, catalog, service, sdk, base_url):
    client = "OpenAI" if sdk == "openai" else "Anthropic"
    write(tmp_path, {
        "requirements.txt": f"{sdk}\n",
        "client.py": f"from {sdk} import {client}\n\n"
        f'client = {client}(api_key=os.environ["API_KEY"], {base_url})\n',
    })  # fmt: skip
    hits = deps(tmp_path, catalog)
    assert service in hits and sdk not in hits


def test_turbopuffer_regional_host_but_not_its_website(tmp_path, catalog):
    api, docs = tmp_path / "api", tmp_path / "docs"
    write(api, {
        "upsert.py": "import requests\n\n"
        'URL = "https://gcp-us-central1.turbopuffer.com/v2/namespaces/docs"\n'
        "requests.post(URL, json=rows)\n",
    })  # fmt: skip
    write(docs, {
        "stores.py": 'VECTOR_DBS = {"turbopuffer": "https://turbopuffer.com/docs/quickstart"}\n',
    })  # fmt: skip
    assert "turbopuffer" in deps(api, catalog)
    assert "turbopuffer" not in found(docs, catalog)


# Hosts, SDKs and model ids from the vendors' current SDKs and docs (2026-10-04 review).
CURRENT_SDKS_FOUND = [
    ({"chat.js": 'await fetch("https://api.giga.chat/v1/chat/completions", opts);\n'}, "gigachat"),
    (
        {
            "stt.ts": 'const client = new SonioxNodeClient({ apiUrl: "https://api.eu.soniox.com" });\n'
        },
        "soniox",
    ),
    ({"parse.py": 'BASE = "https://api.cloud.eu.llamaindex.ai/api/v1/parsing"\n'}, "llamaparse"),
    ({"ade.py": 'URL = "https://api.va.eu-west-1.landing.ai/v1/ade/parse"\n'}, "landingai-ade"),
    ({"calls.ts": 'const base = "https://api.eu.vapi.ai";\n'}, "vapi"),
    (
        {"agent.py": 'url = f"https://bedrock-agentcore-control.{region}.amazonaws.com"\n'},
        "aws-bedrock-agentcore",
    ),
    ({"trace.py": 'HH_URL = "https://api.dp1.us.honeyhive.ai"\n'}, "honeyhive"),
    (
        {"gw.ts": 'const baseURL = "https://gateway-eu.pydantic.dev/proxy/openai";\n'},
        "pydantic-ai-gateway",
    ),
    (
        {"llm.py": 'client = Groq(base_url="https://groq.helicone.ai/openai/v1")\n'},
        "helicone-cloud",
    ),
    ({"video.py": "from luma_agents import Luma\n\nclient = Luma()\n"}, "luma"),
    ({"package.json": '{"dependencies": {"@github/copilot-sdk": "^1.0.0"}}\n'}, "github-copilot"),
    (
        {
            "stream.py": "from aws_sdk_sagemaker_runtime_http2.client import AsyncSageMakerRuntimeHTTP2Client\n"
        },
        "aws-sagemaker",
    ),
    ({"search.py": "from google.cloud import vectorsearch_v1\n"}, "vertex-vector-search"),
    (
        {"search.py": 'r = requests.post("https://ollama.com/api/web_search", json=q)\n'},
        "ollama-cloud",
    ),
    ({"prompts.ts": 'const api = "https://acme.freeplay.ai/api";\n'}, "freeplay"),
]
CURRENT_MODELS_FOUND = [
    ('completion = client.chat.completions.create(model="jamba-mini", messages=m)', "ai21"),
    ('tts = client.tts.generate(model_id="sonic-latest", transcript=t)', "cartesia"),
    ('image = client.images.generate(model="recraftv4_1", prompt=p)', "recraft"),
    (
        'r = client.chat.completions.create(model="system.ai.claude-sonnet-4-5", messages=m)',
        "databricks-model-serving",
    ),
]
CURRENT_LOOKALIKES = [
    # Jamba's open weights, run locally.
    ({"llm.py": 'llm = LLM(model="ai21labs/AI21-Jamba-Mini-1.7")\n'}, "ai21"),
    (
        {"r.py": 'r = client.chat.completions.create(model="system.ai.gpt-oss-120b")\n'},
        "databricks-model-serving",
    ),
    ({"links.md": "Pricing: https://freeplay.ai/pricing\n"}, "freeplay"),
    # Weak alone: Sonic 3 is also the game.
    ({"game.py": 'TITLE = "sonic-3"\n'}, "cartesia"),
]


@pytest.mark.parametrize(
    "files,service", CURRENT_SDKS_FOUND, ids=lambda v: v if isinstance(v, str) else None
)
def test_current_sdks_and_hosts_are_found(tmp_path, catalog, files, service):
    write(tmp_path, files)
    assert service in deps(tmp_path, catalog)


@pytest.mark.parametrize("line,service", CURRENT_MODELS_FOUND)
def test_current_model_ids_are_found(tmp_path, catalog, line, service):
    write(tmp_path, {"call.py": line + "\n"})
    assert service in found(tmp_path, catalog)


@pytest.mark.parametrize(
    "files,service", CURRENT_LOOKALIKES, ids=lambda v: v if isinstance(v, str) else None
)
def test_current_lookalikes_are_not(tmp_path, catalog, files, service):
    write(tmp_path, files)
    assert service not in found(tmp_path, catalog)


# --- catalog gaps from real repos ------------------------------------------------------


@pytest.mark.parametrize(
    ("files", "service"),
    [
        ({"a.py": 'import boto3\nrt = boto3.client("sagemaker-runtime")\n'}, "aws-sagemaker"),
        (
            {"conf/models/baidu.json": '{"base_url": "https://qianfan.baidubce.com/v2"}\n'},
            "baidu-qianfan",
        ),
        (
            {"conf/models/siliconflow.json": '{"base_url": "https://api.siliconflow.cn/v1"}\n'},
            "siliconflow",
        ),
        (
            {"a.py": 'url = "https://spark-api-open.xf-yun.com/v1/chat/completions"\n'},
            "iflytek-spark",
        ),
        ({"a.py": 'url = "https://api.stepfun.com/v1"\n'}, "stepfun-api"),
        ({"requirements.txt": "gigachat\n"}, "gigachat"),
        (
            {
                "a.py": "from langchain_community.vectorstores import AzureCosmosDBNoSqlVectorSearch\n"
            },
            "azure-cosmos-vector",
        ),
        ({"requirements.txt": "tcvectordb\n"}, "tencent-vectordb"),
        (
            {
                "go.mod": "module x\nrequire github.com/aws/aws-sdk-go-v2/service/bedrockagent v1.0.0\n"
            },
            "aws-bedrock-knowledge-bases",
        ),
    ],
    ids=lambda v: v if isinstance(v, str) else next(iter(v)),
)
def test_catalog_gaps(tmp_path, catalog, files, service):
    write(tmp_path, files)
    assert service in deps(tmp_path, catalog)


def test_apache_spark_settings_are_not_iflytek(tmp_path, catalog):
    write(tmp_path, {".env": "SPARK_APP_ID=etl-job\n"})
    assert "iflytek-spark" not in found(tmp_path, catalog)


def test_provider_through_openai_sdk_names_the_provider(tmp_path, catalog):
    write(tmp_path, {
        "requirements.txt": "openai\n",
        "a.py": 'from openai import OpenAI\nc = OpenAI(base_url="https://api.siliconflow.cn/v1")\n',
    })  # fmt: skip
    assert deps(tmp_path, catalog) == {"siliconflow"}


# --- from the 2026-09-27 evaluation: agent alone vs agent + MCP, and a held-out repo -


def test_capability_model_ids_next_to_the_sdk_are_a_call(tmp_path, catalog):
    write(tmp_path, {
        "image.ts": 'import { GoogleGenAI } from "@google/genai";\n'
        'const MODEL_ID = "gemini-2.5-flash-image";\n',
    })  # fmt: skip
    assert "google-imagen" in deps(tmp_path, catalog)


def test_vendor_prefix_needs_a_model_name(tmp_path, catalog):
    write(tmp_path, {
        "loader.js": 'const url = "github://" + doc.metadata.source;\n',
        "icons.jsx": "const icons = [{ pattern: /^snowflake/i, icon: Snowflake }];\n",
    })  # fmt: skip
    hits = found(tmp_path, catalog)
    assert "github-models" not in hits and "snowflake-cortex" not in hits


def test_langchain_js_voyage_embeddings(tmp_path, catalog):
    write(tmp_path, {
        "voyage.js": 'const { VoyageEmbeddings } = require("@langchain/community/embeddings/voyage");\n'
        "const e = new VoyageEmbeddings({ apiKey: process.env.VOYAGEAI_API_KEY });\n",
    })  # fmt: skip
    assert "voyage" in deps(tmp_path, catalog)


def test_nomic_hosted_api_is_closed(tmp_path, catalog):
    write(tmp_path, {
        "requirements.txt": "langchain-nomic\n",
        "emb.py": "from langchain_nomic import NomicEmbeddings\n"
        'e = NomicEmbeddings(model="nomic-embed-text-v1.5")\n',
    })  # fmt: skip
    assert "nomic-api" in deps(tmp_path, catalog)


def test_nomic_run_locally_is_not_the_hosted_api(tmp_path, catalog):
    write(tmp_path, {
        "requirements.txt": "langchain-nomic\n",
        "emb.py": "from langchain_nomic import NomicEmbeddings\n"
        'e = NomicEmbeddings(model="nomic-embed-text-v1.5", inference_mode="local")\n',
    })  # fmt: skip
    assert "nomic-api" not in found(tmp_path, catalog)


def test_open_core_is_shown_with_the_licence(tmp_path, catalog):
    litellm = next(
        a for p in catalog.pools.values() for a in p.alternatives if a.name == "BerriAI/litellm"
    )
    assert litellm.open_core and "enterprise/" in litellm.open_core
    # Azure OpenAI is replaced from the LLM gateway pool, where LiteLLM ranks.
    write(tmp_path, {"requirements.txt": "llama-index-llms-azure-openai\n"})
    findings = match(collect_facts(tmp_path, catalog), catalog)
    assert "open core: enterprise/" in to_markdown(findings, tmp_path, catalog, top=10)


# Closed services first seen in anything-llm and gpt-researcher (2026-09-27 evaluation).
NEW_SERVICES_FOUND = [
    (
        {
            "provider.js": 'const { OpenAI: OpenAIApi } = require("openai");\nthis.openai = new OpenAIApi({\n  apiKey: process.env.GITEE_AI_API_KEY,\n  baseURL: "https://ai.gitee.com/v1",\n});\n'
        },
        "gitee-ai",
    ),
    (
        {
            "provider.js": 'const { OpenAI: OpenAIApi } = require("openai");\nthis.basePath = "https://api.ppinfra.com/v3/openai/";\nthis.openai = new OpenAIApi({\n  baseURL: this.basePath,\n  apiKey: process.env.PPIO_API_KEY ?? null,\n});\n'
        },
        "ppio",
    ),
    (
        {
            "provider.js": 'const { OpenAI: OpenAIApi } = require("openai");\nthis.basePath = "https://apipie.ai/v1";\nthis.openai = new OpenAIApi({\n  baseURL: this.basePath,\n  apiKey: process.env.APIPIE_LLM_API_KEY ?? null,\n});\n'
        },
        "apipie",
    ),
    (
        {
            "llm.py": 'import os\nfrom openai import OpenAI\n\nclient = OpenAI(base_url="https://api.cometapi.com/v1", api_key=os.environ["COMETAPI_KEY"])\n'
        },
        "cometapi",
    ),
    (
        {
            "provider.js": 'const { OpenAI: OpenAIApi } = require("openai");\nif (!process.env.PRIVATEMODE_LLM_BASE_PATH)\n  throw new Error("Privatemode must have a valid base path to use for the api.");\nthis.openai = new OpenAIApi({ baseURL: PrivatemodeLLM.parseBasePath(), apiKey: null });\n'
        },
        "privatemode",
    ),
    (
        {
            "base.py": "import os\nfrom langchain_openai import ChatOpenAI\n\nllm = ChatOpenAI(openai_api_base='https://api.atlascloud.ai/v1',\n                 openai_api_key=os.environ[\"ATLASCLOUD_API_KEY\"])\n"
        },
        "atlas-cloud",
    ),
    (
        {
            "base.py": "import os\nfrom langchain_openai import ChatOpenAI\n\nllm = ChatOpenAI(openai_api_base='https://api.aimlapi.com/v1',\n                 openai_api_key=os.environ[\"AIMLAPI_API_KEY\"])\n"
        },
        "aimlapi",
    ),
    (
        {
            "base.py": "import os\nfrom langchain_openai import ChatOpenAI\n\nllm = ChatOpenAI(openai_api_base='https://api.forge.tensorblock.co/v1',\n                 openai_api_key=os.environ[\"FORGE_API_KEY\"])\n"
        },
        "tensorblock-forge",
    ),
    (
        {
            "base.py": "import os\nfrom langchain_openai import ChatOpenAI\n\nllm = ChatOpenAI(openai_api_base='https://api.avian.io/v1',\n                 openai_api_key=os.environ[\"AVIAN_API_KEY\"])\n"
        },
        "avian",
    ),
    (
        {
            "base.py": 'from langchain_netmind import ChatNetmind\n\nllm = ChatNetmind(model="deepseek-ai/DeepSeek-V3", temperature=0)\n'
        },
        "netmind",
    ),
    (
        {
            "index.js": 'const { CloudClient } = require("chromadb");\nconst client = new CloudClient({\n  apiKey: process.env.CHROMACLOUD_API_KEY,\n  tenant: process.env.CHROMACLOUD_TENANT,\n  database: process.env.CHROMACLOUD_DATABASE,\n});\n'
        },
        "chroma-cloud",
    ),
    (
        {
            "web-browsing.js": "const url = `https://www.searchapi.io/api/v1/search?${params.toString()}`;\nconst res = await fetch(url, {\n  headers: { Authorization: `Bearer ${process.env.AGENT_SEARCHAPI_API_KEY}` },\n});\n"
        },
        "searchapi-io",
    ),
    (
        {
            "agents.py": "from crewai_tools import SerplyWebSearchTool\n\nsearch = SerplyWebSearchTool(limit=10)\n"
        },
        "serply",
    ),
    (
        {
            "crw.py": 'import os\n\nbase_url = os.environ.get("CRW_API_URL", "https://fastcrw.com/api")\nheaders = {"Authorization": f"Bearer {os.environ[\'CRW_API_KEY\']}"}\n'
        },
        "fastcrw-cloud",
    ),
    (
        {
            "search.py": 'from keenable import Keenable\n\nclient = Keenable()\nresults = client.search("open source vector databases")\n'
        },
        "keenable",
    ),
    (
        {
            "web-browsing.js": 'const apiKey = (process.env.AGENT_ANYSEARCH_API_KEY || "").trim();\nconst res = await fetch("https://api.anysearch.com/v1/search", {\n  method: "POST",\n  headers: { Authorization: `Bearer ${apiKey}` },\n});\n'
        },
        "anysearch",
    ),
    (
        {
            "bocha.py": 'import os\nimport requests\n\napi_key = os.environ["BOCHA_API_KEY"]\nurl = \'https://api.bochaai.com/v1/web-search\'\nresp = requests.post(url, headers={"Authorization": f"Bearer {api_key}"}, json={"query": q})\n'
        },
        "bocha",
    ),
    (
        {
            "groundroute.py": 'import os\n\nbase_url = "https://api.groundroute.ai/v1/search"\napi_key = os.environ["GROUNDROUTE_API_KEY"]\n'
        },
        "groundroute",
    ),
    (
        {".env": "MONOCLE_TRACING=true\nMONOCLE_EXPORTER=okahu\nOKAHU_API_KEY=okh_xxxxxxxx\n"},
        "okahu-cloud",
    ),
    (
        {
            "modelslab_image_generator.py": 'import os\n\nTEXT2IMG_URL = "https://modelslab.com/api/v6/images/text2img"\napi_key = os.getenv("MODELSLAB_API_KEY")\n'
        },
        "modelslab",
    ),
]
NEW_SERVICES_LOOKALIKES = [
    (
        {
            "provider.js": 'const DOCS_URL = "https://ai.gitee.com/docs/getting-started";\nconst MIRROR = "https://gitee.com/mindspore/mindformers";\n'
        },
        "gitee-ai",
    ),
    (
        {
            "provider.js": 'const PPIO = require("ppio");\nconst storage = new PPIO({ bucket: "uploads" });\n'
        },
        "ppio",
    ),
    (
        {
            "provider.js": 'const apipie = require("apipie");\nconst api = apipie.create({ baseURL: "/api/v1" });\n'
        },
        "apipie",
    ),
    (
        {
            "llm.py": 'import os\nimport comet_ml\n\nexperiment = comet_ml.Experiment(api_key=os.environ["COMET_API_KEY"], project_name="demo")\n'
        },
        "cometapi",
    ),
    (
        {
            "provider.js": 'const PRIVATE_MODE = process.env.PRIVATE_MODE === "true";\nconst privateModeLabel = "Private mode";\n'
        },
        "privatemode",
    ),
    (
        {
            "base.py": 'import os\n\nATLAS_CLOUD_REGION = os.environ.get("ATLAS_CLOUD_REGION", "us-east-1")\natlas_project = "cloud-atlas"\n'
        },
        "atlas-cloud",
    ),
    (
        {
            "base.py": 'import aiml\n\nkernel = aiml.Kernel()\nkernel.learn("aiml/std-startup.xml")\nBRAIN = "aiml/alice"\n'
        },
        "aimlapi",
    ),
    (
        {
            "base.py": 'import os\nfrom langchain_openai import ChatOpenAI\n\n# Self-hosted Forge (github.com/TensorBlock/forge) on this machine.\nllm = ChatOpenAI(openai_api_base="http://localhost:8000/v1",\n                 openai_api_key=os.environ["FORGE_API_KEY"])\n'
        },
        "tensorblock-forge",
    ),
    (
        {
            "base.py": 'AVIAN_TAXONOMY_URL = "https://avibase.bsc-eoc.org/api"\nspecies_class = "avian"\n'
        },
        "avian",
    ),
    ({"base.py": "NETMIND_ENABLED = False\nnet_mind_layers = [64, 32]\n"}, "netmind"),
    (
        {
            "index.js": 'const { ChromaClient } = require("chromadb");\nconst client = new ChromaClient({\n  path: process.env.CHROMA_ENDPOINT,\n  tenant: process.env.CHROMA_TENANT,\n  database: process.env.CHROMA_DATABASE,\n});\n'
        },
        "chroma-cloud",
    ),
    (
        {
            "web-browsing.js": 'const searchApi = new SearchApi({ baseUrl: "/api/v1/search" });\nconst results = await searchApi.query(q);\n'
        },
        "searchapi-io",
    ),
    (
        {
            "agents.py": 'from serplib import parse_serp\n\nSERP_PROVIDER = "serply-mock"\nresults = parse_serp(html)\n'
        },
        "serply",
    ),
    (
        {
            "crw.py": 'import os\n\nbase_url = os.environ.get("CRW_API_URL", "http://localhost:3000")\nheaders = {"Authorization": f"Bearer {os.environ[\'CRW_API_KEY\']}"}\n'
        },
        "fastcrw-cloud",
    ),
    ({"search.py": 'settings = {"keenable_mode": False, "keen": True}\n'}, "keenable"),
    (
        {
            "web-browsing.js": 'const { AnySearch } = require("anysearch-es");\nconst es = new AnySearch({ node: "http://localhost:9200" });\n'
        },
        "anysearch",
    ),
    (
        {
            "bocha.py": 'search_provider = "bocha"\nSIGNUP_URL = "https://open.bochaai.com/api-keys"\n'
        },
        "bocha",
    ),
    (
        {
            "groundroute.py": 'GROUND_ROUTE = "/api/ground"\nKEYS_PAGE = "https://groundroute.ai/keys"\n'
        },
        "groundroute",
    ),
    ({".env": "MONOCLE_TRACING=true\nMONOCLE_EXPORTER=file\n"}, "okahu-cloud"),
    (
        {
            "modelslab_image_generator.py": 'def missing_key():\n    raise ValueError("No image API key found. Get one at https://modelslab.com/account")\n'
        },
        "modelslab",
    ),
]


@pytest.mark.parametrize(
    "files,service", NEW_SERVICES_FOUND, ids=lambda v: v if isinstance(v, str) else None
)
def test_new_services_are_found(tmp_path, catalog, files, service):
    write(tmp_path, files)
    assert service in deps(tmp_path, catalog)


@pytest.mark.parametrize(
    "files,service", NEW_SERVICES_LOOKALIKES, ids=lambda v: v if isinstance(v, str) else None
)
def test_new_services_lookalikes_are_not(tmp_path, catalog, files, service):
    write(tmp_path, files)
    assert service not in found(tmp_path, catalog)


def test_key_only_in_an_env_template_is_listed_apart(tmp_path, catalog):
    # gpt-researcher: HELICONE_API_KEY= in frontend/nextjs/.example.env, read nowhere.
    write(tmp_path, {".example.env": "HELICONE_API_KEY=\n", "app.py": "print('hi')\n"})
    assert "helicone-cloud" not in deps(tmp_path, catalog)
    data = json.loads(to_json(match(collect_facts(tmp_path, catalog), catalog), tmp_path, catalog))
    assert [f["id"] for f in data["env_template_only"]] == ["helicone-cloud"]


def test_key_in_an_env_template_and_read_in_code_is_a_dependency(tmp_path, catalog):
    write(tmp_path, {
        ".env.example": "HELICONE_API_KEY=\n",
        "app.py": 'import os\nkey = os.environ["HELICONE_API_KEY"]\n',
    })  # fmt: skip
    assert "helicone-cloud" in deps(tmp_path, catalog)


def test_a_real_env_file_is_not_a_template(tmp_path, catalog):
    write(tmp_path, {".env": "HELICONE_API_KEY=sk-123\n"})
    assert "helicone-cloud" in deps(tmp_path, catalog)


def test_github_mcp_server_is_not_copilot(tmp_path, catalog):
    write(tmp_path, {"setup.sh": 'URL="https://api.githubcopilot.com/mcp/"\n'})
    assert "github-copilot" not in deps(tmp_path, catalog)
    write(tmp_path, {"chat.ts": 'fetch("https://api.githubcopilot.com/chat/completions")\n'})
    assert "github-copilot" in deps(tmp_path, catalog)


def test_ollama_cloud_is_the_hosted_api_only(tmp_path, catalog):
    write(tmp_path, {"url.spec.ts": "const u = 'https://api.ollama.com/v1/chat/completions'\n"})
    assert "ollama-cloud" not in deps(tmp_path, catalog)
    write(tmp_path, {"client.py": 'BASE = "https://ollama.com/v1"\n'})
    assert "ollama-cloud" in deps(tmp_path, catalog)


def test_typesafe_jev(tmp_path, catalog):
    write(
        tmp_path,
        {
            "requirements.txt": "typesafe-sdk>=0.7\n",
            "screen.py": "from typesafe_sdk import TypeSafeClient\nclient = TypeSafeClient()\n",
        },
    )
    assert "typesafe" in deps(tmp_path, catalog)
    # `typesafe` on PyPI is an unrelated decorator library
    write(tmp_path / "other", {"requirements.txt": "typesafe==0.9.1\n"})
    assert "typesafe" not in deps(tmp_path / "other", catalog)


# ---------------------------------------------------------------- terminal view


TIED = {
    "requirements.txt": "openai>=1\npinecone\nfaiss-cpu\n",
    ".env.example": "OPENAI_API_KEY=\n",
    "app.py": 'import openai\nBASE = "https://api.openai.com/v1"\n',
}


def terminal(root: Path, catalog, **kw) -> str:
    return to_terminal(match(collect_facts(root, catalog), catalog), root, catalog, **kw)


def test_terminal_counts_one_string_per_service(tmp_path, catalog):
    # The package, the key and the host are one dependency on OpenAI, not three.
    text = terminal(write(tmp_path, TIED), catalog)
    assert "2 strings attached. 2 can be cut." in text
    openai = next(ln for ln in text.splitlines() if "OpenAI API" in ln)
    assert openai.startswith("╎ cut") and "requirements.txt:1" in openai and "+" in openai
    assert next(ln for ln in text.splitlines() if "faiss" in ln).startswith("│ runs")
    assert "--why openai" in text  # the hint names the top row


def test_terminal_holds_a_service_with_no_open_match(tmp_path, catalog):
    empty = dataclasses.replace(
        catalog,
        pools={k: dataclasses.replace(p, alternatives=()) for k, p in catalog.pools.items()},
    )
    text = terminal(write(tmp_path, {"a.py": "import anthropic\n"}), empty)
    assert "1 string attached." in text and "can be cut" not in text
    assert "│ held" in text and "no open match yet" in text


def test_terminal_with_nothing_found(tmp_path, catalog):
    text = terminal(write(tmp_path, {"a.py": "print(1)\n"}), catalog)
    assert "No strings attached." in text and "--why" not in text


def test_terminal_stacks_rows_when_narrow(tmp_path, catalog):
    write(tmp_path, TIED)
    wide, narrow = terminal(tmp_path, catalog, width=200), terminal(tmp_path, catalog, width=50)
    assert "→ " not in wide and "→ " in narrow
    assert max(len(ln) for ln in narrow.splitlines() if "╎" in ln) <= 50


def test_terminal_colour_only_when_asked(tmp_path, catalog):
    write(tmp_path, TIED)
    assert "\033[" not in terminal(tmp_path, catalog)
    assert "\033[" in terminal(tmp_path, catalog, colour=True)


class _Stream:
    def __init__(self, tty: bool):
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def test_colour_follows_no_color_and_force_color(monkeypatch):
    for name in ("NO_COLOR", "FORCE_COLOR", "TERM"):
        monkeypatch.delenv(name, raising=False)
    assert not wants_colour(_Stream(False))
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert wants_colour(_Stream(False))
    monkeypatch.setenv("NO_COLOR", "1")
    assert not wants_colour(_Stream(True))


def test_progress_is_silent_off_a_terminal(capsys):
    p = Progress()  # stderr is captured by pytest: not a terminal
    p.files(3, 10)
    p.done()
    assert capsys.readouterr().err == ""


def test_a_path_alone_scans_and_a_pipe_gets_markdown(tmp_path, capsys):
    write(tmp_path, {"a.py": "import anthropic\n"})
    assert main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# AI dependencies") and "Anthropic API" in out
    assert main([str(tmp_path), "-f", "terminal"]) == 0
    assert "1 string attached." in capsys.readouterr().out


def test_why_lists_every_location_of_one_string(tmp_path, capsys):
    write(tmp_path, TIED)
    assert main([str(tmp_path), "--why", "OpenAI API"]) == 0
    out = capsys.readouterr().out
    assert all(loc in out for loc in ("requirements.txt:1", ".env.example:1", "app.py:1"))
    assert "Inference server" in out
    assert main([str(tmp_path), "--why", "cohere"]) == 2
    assert "not among what was found" in capsys.readouterr().err


def test_toml_dependency_cited_at_its_key_not_its_keywords(tmp_path, catalog):
    write(
        tmp_path,
        {
            "pyproject.toml": '[tool.poetry]\nname = "x"\nkeywords = ["openai", "rag"]\n\n'
            '[tool.poetry.dependencies]\npython = "^3.11"\nopenai = "^1.0"\n'
        },
    )
    assert [(f.file.name, f.line) for f in found(tmp_path, catalog)["openai"]] == [
        ("pyproject.toml", 7)
    ]


def test_why_strips_escape_sequences_from_scanned_lines(tmp_path, catalog):
    write(
        tmp_path, {"a.py": 'import anthropic\nc = anthropic.Anthropic(); t = "\x1b]0;pwned\x07"\n'}
    )
    f = next(
        f for f in match(collect_facts(tmp_path, catalog), catalog) if f.service.id == "anthropic"
    )
    assert any("\x1b" in fact.evidence for fact in f.cited)  # the scan keeps the line as is
    text = why(f, tmp_path, catalog)
    assert "\x1b" not in text and "\x07" not in text and "pwned" in text


def test_why_is_text_only(tmp_path, capsys):
    write(tmp_path, {"a.py": "import anthropic\n"})
    assert main([str(tmp_path), "-f", "json", "--why", "anthropic"]) == 2
    assert "--why prints text" in capsys.readouterr().err


def test_flags_before_the_path_still_scan(tmp_path, capsys):
    write(tmp_path, {"a.py": "import anthropic\n"})
    assert main(["-f", "json", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["found"][0]["id"] == "anthropic"


def test_key_line_only_for_table_keys(tmp_path, catalog):
    # A `[tool.uv.sources]` entry or an extra named after the package is not where a
    # PEP 621 dependency is declared.
    write(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "x"\ndependencies = ["openai>=1", "anthropic>=0.3"]\n\n'
            '[project.optional-dependencies]\nanthropic = ["tiktoken"]\n\n'
            '[tool.uv.sources]\nopenai = { git = "https://github.com/openai/openai-python" }\n'
        },
    )
    facts = found(tmp_path, catalog)
    assert [f.line for f in facts["openai"]] == [3]
    assert [f.line for f in facts["anthropic"]] == [3]


# ---------------------------------------------------------------- retiring models


def _retiring(catalog, **models):
    """The catalog with only these retirements: id=(date, replacement)."""
    from unrent.catalog import Retirement

    vendor = {"gpt": "openai", "cla": "anthropic", "gem": "google"}
    services = {
        "openai": ("openai", "openai-images"),
        "anthropic": ("anthropic",),
        "google": ("google-gemini", "google-embeddings"),
    }
    out = {}
    for model, (date, use) in models.items():
        model = model.replace("_", "-")
        v = vendor[model[:3]]
        out[model] = Retirement(model, v, "https://example.org/x", services[v], date, use)
    return dataclasses.replace(catalog, retirements=out)


def _snaps(root: Path, catalog):
    return snaps(match(collect_facts(root, catalog), catalog), catalog, root)


D = datetime.date


@pytest.mark.parametrize(
    "line",
    [
        'client.chat.completions.create(model="gpt-4-0613")',
        'MODEL = "gpt-4-0613"',
        'model: str = "gpt-4-0613"',
        'model = os.getenv("CHAT_MODEL", "gpt-4-0613")',
        'model = os.environ.get("CHAT_MODEL") or "gpt-4-0613"',
        'llm = ChatOpenAI(model_name="openai/gpt-4-0613")',
        'name = cfg.model if cfg.model else "gpt-4-0613"',
        'model: str | None = "gpt-4-0613"',
        'llm = {"provider": "openai", model: gpt-4-0613, "temperature": 0}',
        'resp = client.chat.completions.create(max_tokens=100, model="gpt-4-0613")',
        "param chatModelName string = 'gpt-4-0613'",
        '  --model-name "gpt-4-0613" \\',
        'model = ai.generativeModel("gpt-4-0613")',
    ],
)
def test_a_line_that_selects_a_retiring_model(tmp_path, catalog, line):
    write(tmp_path, {"app.py": f"import openai\n{line}\n"})
    found = _snaps(tmp_path, _retiring(catalog, gpt_4_0613=(D(2026, 10, 23), "gpt-5.6-sol")))
    assert [(s.id, [x.line for x in s.sites]) for s in found] == [("gpt-4-0613", [2])]


@pytest.mark.parametrize(
    "line",
    [
        'if model == "gpt-4-0613":',
        'MODELS = ["gpt-4-0613", "gpt-4o"]',
        'PRICES = {"gpt-4-0613": 30.0}',
        'ALLOWED = "gpt-4o,gpt-4-0613,"',
        'default = "gpt-4-0613,gpt-4o"',
        'return "gpt-4o" in model or "gpt-4-0613" in model',
        'ok = supportsModel("gpt-4-0613")',
        'deprecatedModel = "gpt-4-0613"',
        'n = num_tokens_from_messages(msgs, model="gpt-4-0613")',
        'llm = Client(azure_deployment="gpt-4-0613")',
    ],
)
def test_a_line_that_only_names_a_retiring_model(tmp_path, catalog, line):
    write(tmp_path, {"app.py": f"import openai\n{line}\n"})
    found = _snaps(tmp_path, _retiring(catalog, gpt_4_0613=(D(2026, 10, 23), None)))
    assert [(s.id, s.sites, len(s.named)) for s in found] == [("gpt-4-0613", [], 1)]


def test_model_ids_in_prose_are_named_not_selected(tmp_path, catalog):
    # A judge's verdict saved as JSON, a description in YAML: they talk about models.
    write(tmp_path, {
        "app.py": "import openai\n",
        "results/run.json": '{"evidence": "Mentions `gpt-4o` in the example references, '
        'but gives no prices for `gpt-4o` or `gpt-4-0613`."}\n',
        "models.yaml": "description: Works with `gpt-4o` or `gpt-4-0613`.\n",
        "config.json": '{"model": "gpt-4-0613"}\n',
    })  # fmt: skip
    found = _snaps(tmp_path, _retiring(catalog, gpt_4_0613=(D(2026, 10, 23), None)))
    assert [(s.id, [x.file.name for x in s.sites], len(s.named)) for s in found] == [
        ("gpt-4-0613", ["config.json"], 1)
    ]


def test_partner_spellings_and_sample_data_are_not_snapped(tmp_path, catalog):
    # Bedrock and Vertex keep their own schedules; fake data and mocks are not calls.
    write(
        tmp_path,
        {
            "bedrock.py": 'import boto3\nMODEL_ID = "anthropic.claude-3-haiku-20240307-v1:0"\n',
            "vertex.py": 'import anthropic\nMODEL = "claude-3-haiku@20240307"\n',
            "src/convos.fakeData.ts": "import Anthropic from '@anthropic-ai/sdk';\nconst c = { model: 'claude-3-haiku-20240307' };\n",
        },
    )
    cat = _retiring(catalog, claude_3_haiku_20240307=(D(2026, 4, 20), "claude-haiku-4-5-20251001"))
    assert all(not s.sites for s in _snaps(tmp_path, cat))


def _platforms(catalog):
    """The catalog with one retirement per vendor, and o3-mini and veo on two of them."""
    from unrent.catalog import parse_retirements

    def vendor(services, **models):
        return {
            "url": "https://example.org/x",
            "services": services,
            "models": {m.replace("_", "-"): {"retires": d, "replacement": r} for m, (d, r) in models.items()},
        }  # fmt: skip

    veo = "veo-3.0-generate-001"
    raw = {"vendors": {
        "openai": vendor(["openai"], o3_mini=(D(2026, 10, 23), "gpt-5.6-sol")),
        "google": vendor(["google-gemini", "google-imagen"], **{veo: (D(2025, 11, 12), None)}),
    }, "platforms": {
        "azure": vendor(["azure-openai"], o3_mini=(D(2026, 11, 19), "gpt-5.6-terra"), text_embedding_3_small=(D(2028, 2, 9), None)),
        "bedrock": vendor(["aws-bedrock"], **{"anthropic.claude-3-haiku-20240307-v1:0": (D(2026, 9, 10), None)}),
        "vertex": vendor(["google-vertex"], claude_3_5_sonnet_v2=(D(2026, 2, 19), None), **{veo: (D(2026, 6, 30), None), "gemini-2.0-flash": (D(2026, 6, 1), None)}),
    }}  # fmt: skip
    ids = {s.id for s in catalog.services}
    return dataclasses.replace(catalog, retirements=parse_retirements(raw, "t", ids))


def test_platform_spellings_retire_on_the_platforms_dates(tmp_path, catalog):
    write(tmp_path, {
        "app.py": 'import openai\nMODEL = "o3-mini"\n',
        "azure.py": 'from openai import AzureOpenAI\nc = AzureOpenAI(azure_endpoint=E)\n'
        'c.chat.completions.create(model="o3-mini")\nc.chat.completions.create(azure_deployment="o3-mini")\n',
        "bedrock.py": 'import boto3\nc = boto3.client("bedrock-runtime")\n'
        'MODEL_ID = "us.anthropic.claude-3-haiku-20240307-v1:0"\n',
        "vertex.py": 'from anthropic import AnthropicVertex\nc = AnthropicVertex(region="us-east5")\n'
        'c.messages.create(model="claude-3-5-sonnet-v2@20241022")\n',
        "video.py": 'import vertexai\nvertexai.init(project=P)\nmodel = "veo-3.0-generate-001"\n',
        "flash.py": 'from google import genai\nc = genai.Client(vertexai=True)\nmodel = "gemini-2.0-flash"\n',
        "imagine.py": 'from google import genai\nc = genai.Client()\nmodel = "veo-3.0-generate-001"\n',
        # Bicep that deploys an Azure OpenAI model; OpenAI's embeddings on Azure.
        "main.bicep": "var chat = {\n  format: 'OpenAI'\n  modelName: 'o3-mini'\n}\n",
        "embed.py": 'from openai import AzureOpenAI\nc = AzureOpenAI(azure_endpoint=E)\n'
        'c.embeddings.create(model="text-embedding-3-small")\n',
        # Reaches OpenAI's API too (its host, its client): OpenAI's date, the one known.
        "both.py": 'from openai import AzureOpenAI\nURL = "https://api.openai.com/v1"\n'
        'MODEL = "o3-mini"\nEMBEDDING_MODEL = "text-embedding-3-small"\n',
        "switch.py": 'from openai import AzureOpenAI, OpenAI\n'
        'client = AzureOpenAI(azure_endpoint=E) if azure else OpenAI()\nMODEL = "o3-mini"\n',
        # Picks among providers: whose id it is, the file does not say.
        "dispatch.py": 'from openai import AzureOpenAI\nKEY = os.environ["GROQ_API_KEY"]\nMODEL = "o3-mini"\n',
        # Bedrock ARNs: a foundation model, an inference profile.
        "arn.py": 'import boto3\nc = boto3.client("bedrock-runtime")\n'
        'MODEL_ARN = "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-haiku-20240307-v1:0"\n'
        'MODEL_ID = "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-3-haiku-20240307-v1:0"\n',
    })  # fmt: skip
    found = _snaps(tmp_path, _platforms(catalog))
    assert sorted(
        (s.retirement.vendor, s.id, s.retirement.retires, [f"{x.file.name}:{x.line}" for x in s.sites])
        for s in found
    ) == [
        # A deployment named after the model is the user's choice, not the model's id.
        ("azure", "o3-mini", D(2026, 11, 19), ["azure.py:3", "main.bicep:3"]),
        ("azure", "text-embedding-3-small", D(2028, 2, 9), ["embed.py:3"]),
        ("bedrock", "anthropic.claude-3-haiku-20240307-v1:0", D(2026, 9, 10), ["arn.py:3", "arn.py:4", "bedrock.py:3"]),
        ("google", "veo-3.0-generate-001", D(2025, 11, 12), ["imagine.py:3"]),
        ("openai", "o3-mini", D(2026, 10, 23), ["app.py:2", "both.py:3", "switch.py:3"]),
        ("vertex", "claude-3-5-sonnet-v2", D(2026, 2, 19), ["vertex.py:3"]),
        ("vertex", "gemini-2.0-flash", D(2026, 6, 1), ["flash.py:3"]),
        ("vertex", "veo-3.0-generate-001", D(2026, 6, 30), ["video.py:3"]),
    ]  # fmt: skip


def test_gemini_api_resource_names_are_model_ids(tmp_path, catalog):
    write(tmp_path, {"app.py": 'from google import genai\nMODEL = "models/gemini-2.0-flash"\n'})
    found = _snaps(tmp_path, _retiring(catalog, **{"gemini-2.0-flash": (D(2026, 6, 1), None)}))
    assert [(s.id, [x.line for x in s.sites]) for s in found] == [("gemini-2.0-flash", [2])]


def test_a_replacement_is_followed_on_its_own_platform(catalog):
    cat = _platforms(catalog)
    assert replacement(cat.retirements["o3-mini"].on("azure-openai"), cat) == "gpt-5.6-terra"


def test_retired_or_retiring_depends_on_the_day(tmp_path, catalog):
    write(tmp_path, {"app.py": 'import openai\nMODEL = "gpt-4-0613"\n'})
    cat = _retiring(catalog, gpt_4_0613=(D(2026, 10, 23), "gpt-5.6-sol"))
    findings = match(collect_facts(tmp_path, cat), cat)
    before = payload(findings, tmp_path, cat, as_of=D(2026, 10, 22))["models_retiring"]
    after = payload(findings, tmp_path, cat, as_of=D(2026, 10, 23))["models_retiring"]
    assert [m["state"] for m in before + after] == ["snaps", "snapped"]


def test_replacement_is_followed_while_it_retires_too(catalog):
    cat = _retiring(
        catalog,
        gpt_4_0314=(D(2026, 3, 26), "gpt-5-2025-08-07"),
        gpt_5_2025_08_07=(D(2026, 12, 11), "gpt-5.6-sol"),
    )
    assert replacement(cat.retirements["gpt-4-0314"], cat) == "gpt-5.6-sol"


def test_reports_show_what_snaps(tmp_path, catalog):
    write(
        tmp_path, {"app.py": 'import openai\nMODEL = "gpt-4-0613"\nFALLBACK_MODEL = "gpt-4-0314"\n'}
    )
    cat = _retiring(
        catalog,
        gpt_4_0613=(D(2026, 10, 23), "gpt-5.6-sol"),
        gpt_4_0314=(D(2026, 3, 26), "gpt-5.6-sol"),
    )
    findings = match(collect_facts(tmp_path, cat), cat)
    kw = {"as_of": D(2026, 10, 4)}
    text = to_terminal(findings, tmp_path, cat, **kw)
    assert "1 has snapped. 1 will snap." in text
    assert "┆ snapped" in text and "retired 2026-03-26 → gpt-5.6-sol" in text
    assert "┆ snaps" in text and "retires 2026-10-23" in text
    md = to_markdown(findings, tmp_path, cat, **kw)
    assert "## Models that stop working" in md and "| `gpt-4-0314` | **retired** |" in md
    data = payload(findings, tmp_path, cat, **kw)
    assert [m["id"] for m in data["models_retiring"]] == ["gpt-4-0314", "gpt-4-0613"]
    assert data["models_retiring"][0]["evidence"][0]["line"] == 3


def test_why_and_as_of_on_the_command_line(tmp_path, capsys):
    # A catalog of its own, so the vendors' pages changing cannot break the test.
    shutil.copytree(CATALOG_DIR, tmp_path / "cat")
    (tmp_path / "cat" / "retirements.yaml").write_text(
        "vendors:\n  openai:\n    url: https://example.org/x\n    services: [openai]\n"
        "    models:\n      gpt-4-0613: {retires: 2026-10-23, replacement: gpt-5.6-sol}\n",
        encoding="utf-8",
    )
    app = write(tmp_path / "app", {"app.py": 'import openai\nMODEL = "gpt-4-0613"\n'})
    cat = ["--catalog", str(tmp_path / "cat")]
    assert main([str(app), "--why", "gpt-4-0613", "--as-of", "2030-01-01", *cat]) == 0
    out = capsys.readouterr().out
    assert "snapped" in out and "app.py:2" in out and "recommends gpt-5.6-sol" in out
    with pytest.raises(SystemExit):
        main([str(app), "--as-of", "soon"])


def _own_catalog(tmp_path: Path) -> Path:
    """The shipped catalog with retirements of its own, which the vendors' pages
    changing cannot break."""
    shutil.copytree(CATALOG_DIR, tmp_path / "cat", dirs_exist_ok=True)
    (tmp_path / "cat" / "retirements.yaml").write_text(
        "vendors:\n  openai:\n    url: https://example.org/x\n    services: [openai]\n"
        "    models:\n      gpt-4-0613: {retires: 2026-10-23, replacement: gpt-5.6-sol}\n"
        "      gpt-4-0314: {retires: 2026-03-26, replacement: gpt-4-0613}\n"
        "      gpt-4: {retires: 2026-10-23, replacement: gpt-5.6-sol}\n"
        "platforms:\n  bedrock:\n    url: https://example.org/b\n    services: [aws-bedrock]\n"
        "    models:\n      anthropic.claude-3-haiku-20240307-v1:0: {retires: 2026-09-10, replacement: null}\n",
        encoding="utf-8",
    )
    return tmp_path / "cat"


# The Vercel AI SDK with no provider package sends `openai/...` to Vercel's gateway: a
# rule that needs the project's package.json, which a scan of one file reads too.
GATEWAY = {
    "package.json": '{"dependencies": {"ai": "^5.0.0"}}',
    "src/app.ts": 'import { generateText } from "ai";\n'
    'const r = await generateText({ model: "openai/gpt-4-0613" });\n',
}


def test_scan_one_file(tmp_path, monkeypatch, capsys):
    cat = ["--catalog", str(_own_catalog(tmp_path))]
    app = write(tmp_path / "app", {**GATEWAY, "other.py": "import anthropic\n"})
    monkeypatch.chdir(app)  # no repository: rooted where it is run
    assert main(["scan", "src/app.ts", "-f", "json", *cat]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["scanned"] == "src/app.ts"
    assert [f["id"] for f in data["found"]] == ["vercel-ai-gateway"]
    assert {e["file"] for f in data["found"] for e in f["evidence"]} == {"src/app.ts"}
    assert data["models_retiring"] == []
    assert main(["scan", "src/app.ts", "-f", "markdown", *cat]) == 0
    assert "# AI dependencies in `src/app.ts`" in capsys.readouterr().out
    assert main(["scan", "nothing.py"]) == 2


@pytest.mark.parametrize(
    ("file", "args"),
    [
        ("node_modules/x/app.py", []),
        ("vendor.py", []),  # in .unrentignore
        ("app.py", ["--exclude", "app.py"]),
        ("tests/helpers.py", ["--skip-tests"]),
    ],
)
def test_scan_of_a_file_left_out_says_so(tmp_path, capsys, file, args):
    app = write(
        tmp_path, {".git/HEAD": "", ".unrentignore": "vendor.py\n", file: "import openai\n"}
    )
    assert main(["scan", str(app / file), *args]) == 2
    assert "is left out (.unrentignore, --exclude" in capsys.readouterr().err


# gpt-4-0314 retired 2026-03-26, gpt-4-0613 retires 2026-10-23 (_own_catalog).
SNAPPING = {"app.py": 'import openai\nMODEL = "gpt-4-0613"\nFALLBACK_MODEL = "gpt-4-0314"\n'}


@pytest.mark.parametrize(
    ("flags", "code", "why"),
    [
        ([], 0, ""),
        (["--fail-on", "snapped"], 1, ": gpt-4-0314 was retired on 2026-03-26\n"),
        (["--fail-on", "snaps"], 1, "2026-03-26; gpt-4-0613 retires on 2026-10-23\n"),
        (["--fail-on", "snaps", "--within", "15"], 1, ": gpt-4-0314 was retired on 2026-03-26\n"),
        (["--fail-on", "snaps", "--within", "16"], 1, "; gpt-4-0613 retires on 2026-10-23\n"),
        (["--fail-on", "closed"], 1, ": depends on OpenAI API\n"),
        (["--fail-on", "snapped,closed"], 1, "2026-03-26; depends on OpenAI API\n"),
        (["--fail-on", "snapped", "--fail-on", "closed"], 1, "--fail-on snapped,closed: "),
        (["--within", "16"], 2, "--within needs --fail-on snaps"),
    ],
)
def test_fail_on(tmp_path, capsys, flags, code, why):
    cat = ["--catalog", str(_own_catalog(tmp_path))]
    app = write(tmp_path / "app", SNAPPING)
    assert main(["scan", str(app), "-f", "json", "--as-of", "2026-10-07", *flags, *cat]) == code
    out, err = capsys.readouterr()
    assert why in err
    if code != 2:  # the report as usual
        assert json.loads(out)["found"][0]["id"] == "openai"


def test_fail_on_passes_when_nothing_is_due(tmp_path, capsys):
    cat = ["--catalog", str(_own_catalog(tmp_path))]
    app = write(tmp_path / "app", {"app.py": 'import anthropic\nMODEL = "claude-sonnet-4-6"\n'})
    assert main(["scan", str(app), "--fail-on", "snapped,snaps", *cat]) == 0
    assert capsys.readouterr().err == ""
    with pytest.raises(SystemExit):
        main(["scan", str(app), "--fail-on", "closd"])


def test_sarif(tmp_path, monkeypatch, capsys):
    cat = ["--catalog", str(_own_catalog(tmp_path))]
    write(tmp_path / "repo" / "a b#c", SNAPPING)
    (tmp_path / "repo" / ".git").mkdir()
    monkeypatch.chdir(tmp_path)  # paths from the repository root, wherever unrent runs
    assert main(["scan", "repo/a b#c", "-f", "sarif", "--as-of", "2026-10-07", *cat]) == 0
    sarif = json.loads(capsys.readouterr().out)
    assert sarif["version"] == "2.1.0"
    run = sarif["runs"][0]
    assert run["tool"]["driver"]["name"] == "unrent"
    rules = {r["id"] for r in run["tool"]["driver"]["rules"]}
    where = [r["locations"][0]["physicalLocation"] for r in run["results"]]
    got = [
        (r["ruleId"], r["level"], w["artifactLocation"]["uri"], w["region"]["startLine"])
        for r, w in zip(run["results"], where, strict=True)
    ]
    assert {g[0] for g in got} <= rules
    assert got[:2] == [
        ("unrent/snapped", "error", "a%20b%23c/app.py", 3),
        ("unrent/snaps", "warning", "a%20b%23c/app.py", 2),
    ]
    assert ("unrent/closed-service", "note", "a%20b%23c/app.py", 1) in got
    assert {w["artifactLocation"]["uriBaseId"] for w in where} == {"%SRCROOT%"}
    texts = [r["message"]["text"] for r in run["results"]]
    assert "retired on 2026-03-26: requests fail now. Use gpt-5.6-sol instead." in texts[0]
    assert "gpt-4-0613 retires on 2026-10-23. Use gpt-5.6-sol instead." in texts[1]
    assert texts[-1].startswith("OpenAI API is a closed AI service. Open source:")


def _hook(monkeypatch, capsys, tmp_path, event, *args: str) -> str:
    data = event if isinstance(event, str) else json.dumps(event)
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(data.encode())))
    assert main(["hook", "--catalog", str(_own_catalog(tmp_path)), *args]) == 0
    return capsys.readouterr().out


APP = 'import openai\nMODEL = "gpt-4-0613"\n'


@pytest.mark.parametrize("written", [{"content": APP}, {"new_string": APP}])
@pytest.mark.parametrize(
    ("as_of", "says"),
    [
        ("2026-10-04", "which retires on 2026-10-23. Use gpt-5.6-sol instead."),
        ("2030-01-01", "which was retired on 2026-10-23: requests fail now. Use gpt-5.6-sol"),
    ],
)
def test_hook_reports_a_retiring_model_just_written(
    tmp_path, monkeypatch, capsys, written, as_of, says
):
    repo = write(tmp_path / "repo", {".git/HEAD": "", "src/app.py": APP})
    event = {"tool_input": {"file_path": str(repo / "src" / "app.py"), **written}}
    out = json.loads(_hook(monkeypatch, capsys, tmp_path, event, "--as-of", as_of))
    context = out["hookSpecificOutput"].pop("additionalContext")
    assert out == {"hookSpecificOutput": {"hookEventName": "PostToolUse"}}
    assert context.startswith(f"unrent: src/app.py:2 selects gpt-4-0613, {says}")


def test_hook_prefilter_takes_a_region_not_any_attribute(tmp_path):
    from unrent.cli import _retiring

    pattern = _retiring(_own_catalog(tmp_path), None)
    text = 'self.gpt-4 = x\nM = "us.anthropic.claude-3-haiku-20240307-v1:0"\n'
    assert pattern.findall(text) == ["anthropic.claude-3-haiku-20240307-v1:0"]


def test_hook_reports_a_bedrock_model_behind_a_region(tmp_path, monkeypatch, capsys):
    app = 'import boto3\nc = boto3.client("bedrock-runtime")\nMODEL = "us.anthropic.claude-3-haiku-20240307-v1:0"\n'
    path = write(tmp_path / "repo", {"app.py": app}) / "app.py"
    event = {"tool_input": {"file_path": str(path), "content": app}}
    out = _hook(monkeypatch, capsys, tmp_path, event, "--as-of", "2026-10-07")
    assert "app.py:3 selects anthropic.claude-3-haiku-20240307-v1:0, which was retired" in out


def test_hook_reads_notebook_edits(tmp_path, monkeypatch, capsys):
    cell = {"cell_type": "code", "metadata": {}, "outputs": [], "source": APP}
    notebook = {"cells": [cell], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    path = write(tmp_path / "nb", {"app.ipynb": json.dumps(notebook)}) / "app.ipynb"
    event = {"tool_input": {"notebook_path": str(path), "new_source": APP}}
    assert "selects gpt-4-0613, which" in _hook(monkeypatch, capsys, tmp_path, event)


@pytest.mark.parametrize(
    ("file", "expected"),
    [("src/app.py", "src/app.py:2 selects gpt-4-0613"), ("tests/helpers.py", None)],
)
def test_hook_judges_from_the_repository_root(tmp_path, monkeypatch, capsys, file, expected):
    # The repository, not the directory the agent runs in, decides the path and what is
    # test code (left out, as in a scan).
    repo = write(tmp_path / "repo", {".git/HEAD": "", file: APP})
    event = {"cwd": str(tmp_path), "tool_input": {"file_path": str(repo / file), "content": APP}}
    out = _hook(monkeypatch, capsys, tmp_path, event)
    assert (expected in out) if expected else out == ""


@pytest.mark.parametrize(
    ("files", "file", "written"),
    [
        # Selected before this edit: told when it was written, not on every later edit.
        ({"app.py": APP + 'MENU = ["gpt-4-0314"]\n'}, "app.py", 'MENU = ["gpt-4-0314"]\n'),
        # Only listed, not selected.
        ({"app.py": 'import openai\nMENU = ["gpt-4-0613", "gpt-4o"]\n'}, "app.py", "gpt-4-0613"),
        # `gpt-4o` is not `gpt-4`.
        (
            {"app.py": 'import openai\nMODEL = "gpt-4"\nOTHER_MODEL = "gpt-4o"\n'},
            "app.py",
            'OTHER_MODEL = "gpt-4o"',
        ),
        # Test code, found from the agent's working directory when there is no repository.
        ({"tests/helpers.py": APP}, "tests/helpers.py", APP),
        # Routed to Vercel's gateway, as a scan of the whole project says.
        (GATEWAY, "src/app.ts", GATEWAY["src/app.ts"]),
    ],
)
def test_hook_is_silent_on_what_was_not_just_selected(
    tmp_path, monkeypatch, capsys, files, file, written
):
    repo = write(tmp_path / "repo", files)
    event = {"cwd": str(repo), "tool_input": {"file_path": str(repo / file), "new_string": written}}
    assert _hook(monkeypatch, capsys, tmp_path, event) == ""


@pytest.mark.parametrize(
    "event",
    [
        "not json",
        "",
        "[]",
        {"tool_input": {"content": APP}},
        {"tool_input": {"file_path": "/nowhere/app.py", "content": APP}},
        {"tool_input": {"file_path": ".", "content": APP}},
        {"tool_input": {"file_path": "app.py", "content": 3}},
    ],
)
def test_hook_never_fails_the_edit(tmp_path, monkeypatch, capsys, event):
    assert _hook(monkeypatch, capsys, tmp_path, event) == ""


def test_hook_is_silent_on_binary_files(tmp_path, monkeypatch, capsys):
    path = tmp_path / "app.bin"
    path.write_bytes(b"\0" + APP.encode())
    event = {"tool_input": {"file_path": str(path), "content": APP}}
    assert _hook(monkeypatch, capsys, tmp_path, event) == ""


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (
            "vendors:\n  x:\n    url: u\n    services: [nope]\n    models: {}\n",
            "unknown services",
        ),
        ("vendors:\n  x:\n    services: [openai]\n    models: {}\n", "`url`"),
        (
            "vendors:\n  x:\n    url: u\n    services: [openai]\n    models:\n      gpt-9: {retires: 2026-01-01 10:00:00, replacement: null}\n",
            "needs a `retires` date",
        ),
        (
            "vendors:\n  x:\n    url: u\n    services: [openai]\n    models:\n      gpt-9: {retires: soon}\n",
            "needs a `retires` date",
        ),
        (
            "vendors:\n  x:\n    url: u\n    services: [openai]\n    models:\n      gpt-9: {retires: 2020-13-45}\n",
            "retirements.yaml: month must be in 1..12",
        ),
        (
            "vendors:\n  a:\n    url: u\n    services: [openai]\n    models:\n      m: {retires: 2026-01-01, replacement: null}\n"
            "  b:\n    url: u\n    services: [openai]\n    models:\n      m: {retires: 2026-01-01, replacement: null}\n",
            "also under a",
        ),
    ],
)
def test_retirements_file_is_validated(tmp_path, body, error):
    shutil.copytree(CATALOG_DIR, tmp_path / "cat")
    (tmp_path / "cat" / "retirements.yaml").write_text(body, encoding="utf-8")
    with pytest.raises(CatalogError, match=error):
        load_catalog(tmp_path / "cat")


def test_shipped_retirements_load(catalog):
    assert len(catalog.retirements) > 100
    vendors = {x.vendor for r in catalog.retirements.values() for x in (r, *r.also)}
    assert vendors == {
        "openai",
        "anthropic",
        "google",
        "azure",
        "bedrock",
        "vertex",
        "vertex-partners",
    }


# --------------------------------------------------------------------------
# Speed: worker processes and the statement walk change nothing in the results
# --------------------------------------------------------------------------

SPREAD = {
    "app.py": 'from openai import OpenAI\n# from anthropic import x\nOpenAI(model="gpt-4")\n',
    "tools/search.py": 'import importlib\nexa = importlib.import_module("exa_py")\n',
    "web/client.ts": 'import Anthropic from "@anthropic-ai/sdk";\nfetch("https://api.newco.ai/v1/chat")\n',
    ".env.example": "NEWCO_API_KEY=\nPINECONE_API_KEY=\n",
    "requirements.txt": "pinecone\nqdrant-client\n",
    "tests/test_app.py": "import cohere\n",
}


def _scan(root, catalog, **kw):
    from unrent.discover import scan_unknown

    facts = sorted(map(repr, collect_facts(root, catalog, **kw)))
    return facts, scan_unknown(root, catalog, skip_tests=kw.get("skip_tests", False))


@pytest.mark.parametrize("skip_tests", [False, True])
def test_worker_processes_find_what_one_process_finds(tmp_path, catalog, monkeypatch, skip_tests):
    import unrent.detect as detect

    write(tmp_path, SPREAD)
    serial = _scan(tmp_path, catalog, skip_tests=skip_tests)
    started = []
    real = detect.ProcessPoolExecutor

    def spy(*a, **kw):
        started.append(kw["max_workers"])
        return real(*a, **kw)

    monkeypatch.setattr(detect, "PARALLEL_FILES", 1)
    monkeypatch.setattr(detect, "ProcessPoolExecutor", spy)
    if (os.cpu_count() or 1) < 2:
        monkeypatch.setattr(detect.os, "cpu_count", lambda: 2)
    assert _scan(tmp_path, catalog, skip_tests=skip_tests) == serial
    assert len(started) == 2  # the facts, and the unknown candidates
    assert any("openai" in f for f in serial[0])
    assert any(c["name"] == "newco.ai" for c in serial[1])


def test_scan_without_worker_processes(tmp_path, catalog, monkeypatch):
    """A sandbox that can't start processes: the scan reads every file itself."""
    import unrent.detect as detect

    write(tmp_path, SPREAD)
    serial = _scan(tmp_path, catalog)

    def no_processes(*a, **kw):
        raise OSError("no processes here")

    monkeypatch.setattr(detect, "PARALLEL_FILES", 1)
    monkeypatch.setattr(detect, "ProcessPoolExecutor", no_processes)
    assert _scan(tmp_path, catalog) == serial


def test_statement_walk_visits_what_ast_walk_visits():
    import ast

    from unrent.detect import _statements

    tree = ast.parse(
        '"""doc"""\nimport a\nclass C:\n    """c"""\n    import b\n'
        "    def f(self):\n        try:\n            import c\n        except E:\n"
        "            import d\n        else:\n            import e\n        finally:\n"
        "            import f\n"
        "match x:\n    case 1:\n        import g\n"
        "for i in y:\n    import h\nelse:\n    import i\n"
        "with z:\n    if q:\n        import j\n    else:\n        import k\n"
        "async def g():\n    async with w:\n        import l\n"
        "try:\n    pass\nexcept* E:\n    import m\n"
    )
    kinds = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Import)
    full = [n for n in ast.walk(tree) if isinstance(n, kinds)]
    assert [n for n in _statements(tree) if isinstance(n, kinds)] == full
    assert len([n for n in full if isinstance(n, ast.Import)]) == 13


def test_a_crash_exits_2_not_the_fail_on_code(tmp_path, monkeypatch, capsys):
    import unrent.cli as cli

    def boom(args):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "cmd_scan", boom)
    assert main(["scan", str(tmp_path)]) == 2
    assert "RuntimeError: boom" in capsys.readouterr().err
